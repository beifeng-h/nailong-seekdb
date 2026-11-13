#ifndef SEEKDB_CUVS_BRIDGE_H
#define SEEKDB_CUVS_BRIDGE_H
#ifdef __cplusplus
extern "C" {
#endif
/* One-shot: build a CAGRA index over base (n x dim, row-major f32) on the GPU and
 * search queries (nq x dim), writing top-k uint32 neighbor ids to out_ids
 * (length nq*topk). Returns 0 on success, non-zero on error. */
int seekdb_cuvs_cagra_knn(const float* base, long n, long dim,
                          const float* query, long nq, long topk,
                          unsigned int* out_ids);

/* Build-once / search-many (used by seekdb's ob_vsag_adaptor GPU path).
 *  - seekdb_cuvs_build: builds a CAGRA index on the GPU from base (n x dim,
 *    row-major f32) and returns an opaque handle (NULL on error). cuVS copies
 *    the dataset to the device, so `base` may be freed after this returns.
 *  - seekdb_cuvs_search: runs top-k search for nq queries (row-major f32, dim
 *    cols), writing uint32 row offsets to out_ids[nq*topk] and (if non-NULL) L2
 *    distances to out_dist[nq*topk]. Returns 0 on success.
 *  - seekdb_cuvs_free: releases the handle.
 * Search workspace is retained by the handle and reused across calls. Searches
 * on the same handle are serialized; callers must stop using it before free.
 * Query copies and search use the handle's resource stream. Successful search
 * returns only after that stream completes, including host output copies.
 * Serialize requires no concurrent search. A failed release or stream wait disables
 * search and serialize until the handle is freed and rebuilt. An ambiguous
 * failed free is never retried; complete resource recovery is not guaranteed.
 * Free waits for queued work and releases before destroying the resource;
 * synchronization failures are reported and cleanup then remains best effort.
 * On AMD, the first build/load configures process-wide libomp defaults only
 * when OMP_NUM_THREADS is absent. Each host team uses at most eight threads
 * and half the available CPU budget (at least one); the cgroup-v2 root quota
 * is used when available. Existing environment settings take precedence.
 * All ROCm/cuVS state lives inside the .so; seekdb only ever sees these C symbols. */
void* seekdb_cuvs_build(const float* base, long n, long dim);
int   seekdb_cuvs_search(void* handle, const float* query, long nq, long topk,
                         unsigned int* out_ids, float* out_dist);
void  seekdb_cuvs_free(void* handle);
int seekdb_cuvs_serialize(void* handle, const char* filename);
void* seekdb_cuvs_deserialize(const char* filename, long n, long dim);
#ifdef __cplusplus
}
#endif
#endif
