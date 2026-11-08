#pragma once
#include <stddef.h>
#include <stdint.h>
// Host-logic shim only: Windows long matches the production local shape arrays.
// This intentionally does not validate the Linux DLPack ABI.
enum { kDLCPU=1, kDLCUDA=2, kDLFloat=2, kDLUInt=1 };
struct DLDevice { int device_type, device_id; };
struct DLDataType { unsigned char code, bits; unsigned short lanes; };
struct DLTensor { void* data; DLDevice device; int ndim; DLDataType dtype;
  long* shape; long* strides; uint64_t byte_offset; };
struct DLManagedTensor { DLTensor dl_tensor; };
struct FakeResource; struct FakeIndex;
typedef FakeResource* cuvsResources_t;
typedef FakeIndex* cuvsCagraIndex_t;
struct FakeIndexParams { int build_algo; size_t graph_degree, intermediate_graph_degree, nn_descent_niter; };
typedef FakeIndexParams* cuvsCagraIndexParams_t;
struct FakeSearchParams { size_t itopk_size, search_width; };
typedef FakeSearchParams* cuvsCagraSearchParams_t;
enum { CUVS_SUCCESS=0, CUVS_ERROR=1, NN_DESCENT=3, NO_FILTER=0 };
struct cuvsFilter { int type; uintptr_t addr; };
int cuvsResourcesCreate(cuvsResources_t*);
int cuvsResourcesDestroy(cuvsResources_t);
int cuvsCagraIndexParamsCreate(cuvsCagraIndexParams_t*);
int cuvsCagraIndexParamsDestroy(cuvsCagraIndexParams_t);
int cuvsCagraIndexCreate(cuvsCagraIndex_t*);
int cuvsCagraIndexDestroy(cuvsCagraIndex_t);
int cuvsCagraBuild(cuvsResources_t,cuvsCagraIndexParams_t,DLManagedTensor*,cuvsCagraIndex_t);
int cuvsCagraSearchParamsCreate(cuvsCagraSearchParams_t*);
int cuvsCagraSearchParamsDestroy(cuvsCagraSearchParams_t);
int cuvsCagraSearch(cuvsResources_t,cuvsCagraSearchParams_t,cuvsCagraIndex_t,DLManagedTensor*,DLManagedTensor*,DLManagedTensor*,cuvsFilter);
int cuvsCagraSerialize(cuvsResources_t,const char*,cuvsCagraIndex_t,bool);
int cuvsCagraDeserialize(cuvsResources_t,const char*,cuvsCagraIndex_t);
int cuvsCagraIndexGetSize(cuvsCagraIndex_t,int64_t*);
int cuvsCagraIndexGetDims(cuvsCagraIndex_t,int64_t*);
const char* cuvsGetLastErrorText();
int cuvsRMMAlloc(cuvsResources_t,void**,size_t);
int cuvsRMMFree(cuvsResources_t,void*,size_t);
enum { cudaSuccess=0, cudaMemcpyDefault=4 };
typedef int cudaError_t;
cudaError_t cudaMemcpy(void*,const void*,size_t,int);
const char* cudaGetErrorString(cudaError_t);
struct FakeMutex;
struct pthread_mutex_t { FakeMutex* native; };
int pthread_mutex_init(pthread_mutex_t*,const void*);
int pthread_mutex_destroy(pthread_mutex_t*);
int pthread_mutex_lock(pthread_mutex_t*);
int pthread_mutex_unlock(pthread_mutex_t*);
