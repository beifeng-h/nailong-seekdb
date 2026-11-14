/* seekdb <-> hipVS bridge: plain-C CAGRA kNN; all cuVS/ROCm deps live here. */
#include "seekdb_cuvs_bridge.h"
#include <cuvs/core/c_api.h>
#include <cuvs/neighbors/cagra.h>
#include <dlpack/dlpack.h>
#ifdef __HIP_PLATFORM_AMD__
#include <cuvs/cuda_runtime.h>
#else
#include <cuda_runtime.h>
#endif
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <pthread.h>

typedef struct {
  cuvsResources_t        res;
  cuvsCagraIndexParams_t ip;
  cuvsCagraIndex_t       idx;
  long n, dim;
  pthread_mutex_t search_mutex;
  void *query_workspace, *neighbor_workspace, *distance_workspace;
  size_t query_capacity, neighbor_capacity, distance_capacity;
  int workspace_failed;
} sk_cuvs_index;

/* Successful cuVS cleanup clears its thread-local last-error text. */
static void sk_cuvs_report_error(const char* operation)
{
  const char* detail = cuvsGetLastErrorText();
  fprintf(stderr, "[seekdb_cuvs] %s failed: %s\n",
          operation, detail && detail[0] ? detail : "cuVS error text unavailable");
}

static void sk_cuvs_report_copy_error(const char* operation, cudaError_t error)
{
  const char* detail = cudaGetErrorString(error);
  fprintf(stderr, "[seekdb_cuvs] %s failed: runtime code=%d: %s\n",
          operation, (int)error, detail && detail[0] ? detail : "runtime error text unavailable");
}

static void sk_cuvs_destroy_search_mutex(sk_cuvs_index* index)
{
  int rc = pthread_mutex_destroy(&index->search_mutex);
  if (rc != 0) fprintf(stderr, "[seekdb_cuvs] search mutex destroy failed: %d\n", rc);
}

/* A failed deallocation does not establish whether the pointer is still live. */
static int sk_cuvs_release(sk_cuvs_index* index, void* buffer, size_t bytes)
{
  if (buffer && cuvsRMMFree(index->res, buffer, bytes) != CUVS_SUCCESS) {
    sk_cuvs_report_error("workspace release");
    fprintf(stderr, "[seekdb_cuvs] workspace ownership unknown: %zu bytes; handle disabled\n", bytes);
    index->workspace_failed = 1;
    return 1;
  }
  return 0;
}

static sk_cuvs_index* sk_cuvs_index_alloc(void)
{
  sk_cuvs_index* index = (sk_cuvs_index*)calloc(1, sizeof(*index));
  if (index && pthread_mutex_init(&index->search_mutex, NULL) != 0) {
    free(index);
    index = NULL;
  }
  return index;
}

static int sk_cuvs_tensor_bytes(long rows, long columns, size_t element_size,
                                size_t* bytes)
{
  int rc = 0;
  if (rows <= 0 || columns <= 0 || element_size == 0 ||
      (uintmax_t)rows > (uintmax_t)SIZE_MAX / element_size / (uintmax_t)columns) {
    rc = 1;
  } else {
    *bytes = (size_t)rows * (size_t)columns * element_size;
  }
  return rc;
}

static int sk_cuvs_reserve(sk_cuvs_index* index, void** buffer,
                           size_t* capacity, size_t bytes)
{
  int rc = 0;
  if (bytes > *capacity) {
    void* replacement = NULL;
    if (cuvsRMMAlloc(index->res, &replacement, bytes) != CUVS_SUCCESS || !replacement) {
      sk_cuvs_report_error("workspace allocation");
      sk_cuvs_release(index, replacement, bytes);
      rc = 1;
    } else {
      rc = sk_cuvs_release(index, *buffer, *capacity);
      /* Track the known-owned replacement; never retry an ambiguous old free. */
      *buffer = replacement;
      *capacity = bytes;
    }
  }
  return rc;
}

void* seekdb_cuvs_build(const float* base, long n, long dim)
{
  sk_cuvs_index* h = sk_cuvs_index_alloc();
  if (!h) return NULL;
  if (cuvsResourcesCreate(&h->res) != CUVS_SUCCESS) {
    sk_cuvs_report_error("build resources");
    sk_cuvs_destroy_search_mutex(h); free(h); return NULL;
  }

  DLManagedTensor dset; long ds[2] = {n, dim};
  dset.dl_tensor.data = (void*)base;
  dset.dl_tensor.device.device_type = kDLCPU; dset.dl_tensor.device.device_id = 0;
  dset.dl_tensor.ndim = 2;
  dset.dl_tensor.dtype.code = kDLFloat; dset.dl_tensor.dtype.bits = 32; dset.dl_tensor.dtype.lanes = 1;
  dset.dl_tensor.shape = ds; dset.dl_tensor.strides = 0; dset.dl_tensor.byte_offset = 0;

  if (cuvsCagraIndexParamsCreate(&h->ip) != CUVS_SUCCESS) {
    sk_cuvs_report_error("build params"); goto fail_res;
  }
  h->ip->build_algo = NN_DESCENT;
  h->ip->graph_degree = 64;
  h->ip->intermediate_graph_degree = 64;
  h->ip->nn_descent_niter = 4;
  if (cuvsCagraIndexCreate(&h->idx) != CUVS_SUCCESS) {
    sk_cuvs_report_error("build index"); goto fail_ip;
  }
  if (cuvsCagraBuild(h->res, h->ip, &dset, h->idx) != CUVS_SUCCESS) {
    sk_cuvs_report_error("CAGRA build"); goto fail_idx;
  }
  h->n = n; h->dim = dim;
  return h;

fail_idx: cuvsCagraIndexDestroy(h->idx);
fail_ip:  cuvsCagraIndexParamsDestroy(h->ip);
fail_res: cuvsResourcesDestroy(h->res); sk_cuvs_destroy_search_mutex(h); free(h);
  return NULL;
}

int seekdb_cuvs_search(void* handle, const float* query, long nq, long topk,
                       unsigned int* out_ids, float* out_dist)
{
  sk_cuvs_index* h = (sk_cuvs_index*)handle;
  if (!h) return 1;
  if (pthread_mutex_lock(&h->search_mutex) != 0) return 1;
  int rc = 0;
  int unlock_rc = 0;
  int stream_ready = 0;
  cudaStream_t stream = 0;
  size_t query_bytes = 0, neighbor_bytes = 0, distance_bytes = 0;
  float* qd = 0; unsigned* nd = 0; float* dd = 0; cuvsCagraSearchParams_t sp = 0;
  cudaError_t copy_error = cudaSuccess;
  if (h->workspace_failed) { rc=19; goto done; }
  if (sk_cuvs_tensor_bytes(nq, h->dim, sizeof(float), &query_bytes)) { rc=10; goto done; }
  if (sk_cuvs_tensor_bytes(nq, topk, sizeof(unsigned), &neighbor_bytes)) { rc=11; goto done; }
  if (sk_cuvs_tensor_bytes(nq, topk, sizeof(float), &distance_bytes)) { rc=12; goto done; }
  if (cuvsStreamGet(h->res, &stream) != CUVS_SUCCESS) {
    sk_cuvs_report_error("search stream"); rc=20; goto done;
  }
  stream_ready = 1;
  if (sk_cuvs_reserve(h, &h->query_workspace, &h->query_capacity, query_bytes)) { rc=10; goto done; }
  if (sk_cuvs_reserve(h, &h->neighbor_workspace, &h->neighbor_capacity, neighbor_bytes)) { rc=11; goto done; }
  if (sk_cuvs_reserve(h, &h->distance_workspace, &h->distance_capacity, distance_bytes)) { rc=12; goto done; }
  qd = (float*)h->query_workspace;
  nd = (unsigned*)h->neighbor_workspace;
  dd = (float*)h->distance_workspace;
  copy_error = cudaMemcpyAsync(qd, query, query_bytes, cudaMemcpyDefault, stream);
  if (copy_error != cudaSuccess) { sk_cuvs_report_copy_error("query H2D", copy_error); rc=13; goto done; }
  {
    DLManagedTensor qt, nt, dt; long qs[2]={nq,h->dim}, nsh[2]={nq,topk}, dsh[2]={nq,topk};
    qt.dl_tensor.data=qd; qt.dl_tensor.device.device_type=kDLCUDA; qt.dl_tensor.device.device_id=0;
    qt.dl_tensor.ndim=2; qt.dl_tensor.dtype.code=kDLFloat; qt.dl_tensor.dtype.bits=32; qt.dl_tensor.dtype.lanes=1;
    qt.dl_tensor.shape=qs; qt.dl_tensor.strides=0; qt.dl_tensor.byte_offset=0;
    nt=qt; nt.dl_tensor.data=nd; nt.dl_tensor.dtype.code=kDLUInt; nt.dl_tensor.shape=nsh;
    dt=qt; dt.dl_tensor.data=dd; dt.dl_tensor.dtype.code=kDLFloat; dt.dl_tensor.shape=dsh;
    if (cuvsCagraSearchParamsCreate(&sp) != CUVS_SUCCESS) { sk_cuvs_report_error("search params"); rc=14; goto done; }
    {
      size_t required_itopk = (size_t)topk * 4;
      if (required_itopk < 3072) required_itopk = 3072;
      if (sp->itopk_size < required_itopk) sp->itopk_size = required_itopk;
      sp->search_width = 4;
      sp->algo = MULTI_CTA;
    }
    cuvsFilter f; f.type=NO_FILTER; f.addr=(uintptr_t)0;
    if (cuvsCagraSearch(h->res, sp, h->idx, &qt, &nt, &dt, f) != CUVS_SUCCESS) { sk_cuvs_report_error("CAGRA search"); rc=15; goto done; }
    copy_error = cudaMemcpyAsync(out_ids, nd, neighbor_bytes, cudaMemcpyDefault, stream);
    if (copy_error != cudaSuccess) { sk_cuvs_report_copy_error("IDs D2H", copy_error); rc=16; goto done; }
    if (out_dist) {
      copy_error = cudaMemcpyAsync(out_dist, dd, distance_bytes, cudaMemcpyDefault, stream);
      if (copy_error != cudaSuccess) { sk_cuvs_report_copy_error("distances D2H", copy_error); rc=18; goto done; }
    }
  }
done:
  /* The caller owns host buffers only until return; drain even error exits. */
  if (stream_ready && cuvsStreamSync(h->res) != CUVS_SUCCESS) {
    sk_cuvs_report_error("search stream completion");
    h->workspace_failed = 1;
    if (rc == 0) rc = 21;
  }
  if (sp) cuvsCagraSearchParamsDestroy(sp);
  unlock_rc = pthread_mutex_unlock(&h->search_mutex);
  if (unlock_rc != 0) {
    fprintf(stderr, "[seekdb_cuvs] search mutex unlock failed: %d\n", unlock_rc);
    if (rc == 0) rc = 17;
  }
  if (rc) fprintf(stderr, "[seekdb_cuvs] search rc=%d\n", rc);
  return rc;
}

void seekdb_cuvs_free(void* handle)
{
  sk_cuvs_index* h = (sk_cuvs_index*)handle;
  if (!h) return;
  if (cuvsStreamSync(h->res) != CUVS_SUCCESS) {
    sk_cuvs_report_error("free pending work");
    fprintf(stderr, "[seekdb_cuvs] stream completion unknown; cleanup is best effort\n");
  }
  sk_cuvs_release(h, h->distance_workspace, h->distance_capacity);
  sk_cuvs_release(h, h->neighbor_workspace, h->neighbor_capacity);
  sk_cuvs_release(h, h->query_workspace, h->query_capacity);
  if (cuvsCagraIndexDestroy(h->idx) != CUVS_SUCCESS) sk_cuvs_report_error("free index");
  if (cuvsCagraIndexParamsDestroy(h->ip) != CUVS_SUCCESS) sk_cuvs_report_error("free params");
  if (cuvsStreamSync(h->res) != CUVS_SUCCESS) {
    sk_cuvs_report_error("free stream completion");
    fprintf(stderr, "[seekdb_cuvs] device memory reclamation unknown\n");
  }
  if (cuvsResourcesDestroy(h->res) != CUVS_SUCCESS) sk_cuvs_report_error("free resources");
  sk_cuvs_destroy_search_mutex(h);
  free(h);
}

int seekdb_cuvs_serialize(void* handle, const char* filename)
{
  sk_cuvs_index* index = (sk_cuvs_index*)handle;
  if (!index || !filename) return 1;
  if (index->workspace_failed) return 1;
  if (cuvsCagraSerialize(index->res, filename, index->idx, true) != CUVS_SUCCESS) {
    sk_cuvs_report_error("serialize");
    return 1;
  }
  return 0;
}

void* seekdb_cuvs_deserialize(const char* filename, long n, long dim)
{
  if (!filename || n <= 0 || dim <= 0) return NULL;
  sk_cuvs_index* index = sk_cuvs_index_alloc();
  if (!index) return NULL;
  if (cuvsResourcesCreate(&index->res) != CUVS_SUCCESS) {
    sk_cuvs_report_error("deserialize resources"); goto fail;
  }
  if (cuvsCagraIndexParamsCreate(&index->ip) != CUVS_SUCCESS) {
    sk_cuvs_report_error("deserialize params"); goto fail_res;
  }
  if (cuvsCagraIndexCreate(&index->idx) != CUVS_SUCCESS) {
    sk_cuvs_report_error("deserialize index"); goto fail_ip;
  }
  if (cuvsCagraDeserialize(index->res, filename, index->idx) != CUVS_SUCCESS) {
    sk_cuvs_report_error("CAGRA deserialize"); goto fail_idx;
  }
  {
    int64_t actual_n = 0;
    int64_t actual_dim = 0;
    if (cuvsCagraIndexGetSize(index->idx, &actual_n) != CUVS_SUCCESS) {
      sk_cuvs_report_error("snapshot size"); goto fail_idx;
    }
    if (cuvsCagraIndexGetDims(index->idx, &actual_dim) != CUVS_SUCCESS) {
      sk_cuvs_report_error("snapshot dimensions"); goto fail_idx;
    }
    if (actual_n != n || actual_dim != dim) {
      fprintf(stderr, "[seekdb_cuvs] snapshot shape mismatch: %ld x %ld, expected %ld x %ld\n",
              (long)actual_n, (long)actual_dim, n, dim);
      goto fail_idx;
    }
  }
  index->n = n;
  index->dim = dim;
  return index;
fail_idx:
  cuvsCagraIndexDestroy(index->idx);
fail_ip:
  cuvsCagraIndexParamsDestroy(index->ip);
fail_res:
  cuvsResourcesDestroy(index->res);
fail:
  sk_cuvs_destroy_search_mutex(index);
  free(index);
  return NULL;
}

int seekdb_cuvs_cagra_knn(const float* base, long n, long dim,
                          const float* query, long nq, long topk,
                          unsigned int* out_ids)
{
  void* h = seekdb_cuvs_build(base, n, dim);
  if (!h) return 10;
  int rc = seekdb_cuvs_search(h, query, nq, topk, out_ids, NULL);
  seekdb_cuvs_free(h);
  return rc;
}
