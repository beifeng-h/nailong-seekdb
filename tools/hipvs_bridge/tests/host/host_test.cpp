#include "fake_api.h"
#include "seekdb_cuvs_bridge.h"
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <cstdio>
#include <cstring>
#include <exception>
#include <functional>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <io.h>
#ifdef SEEKDB_HOST_FROZEN_PARAMETERS
static constexpr size_t expected_build_iterations=100;
static constexpr size_t expected_intermediate_degree=256;
static constexpr size_t expected_minimum_itopk=512;
static constexpr cuvsCagraSearchAlgo expected_search_algorithm=SINGLE_CTA;
#else
static constexpr size_t expected_build_iterations=4;
static constexpr size_t expected_intermediate_degree=128;
static constexpr size_t expected_minimum_itopk=1536;
static constexpr cuvsCagraSearchAlgo expected_search_algorithm=MULTI_CTA;
#endif
#define CHECK(x) do { if (!(x)) throw std::runtime_error(std::string(__func__)+": "+#x); } while (0)
struct PendingWork {
 enum class Kind { Copy, Search, Free };
 Kind kind;
 void* referenced_object;
 std::function<void(std::unique_lock<std::mutex>&)> execute;
};
struct FakeStream {
 std::deque<PendingWork> pending;
 bool async_used=false;
};
struct FakeResource { FakeStream stream; };
struct FakeIndex { int64_t n=0, dim=0; };
struct FakeMutex { std::mutex value; };
struct Block { std::unique_ptr<unsigned char[]> bytes; size_t size; bool live=true; };
enum class ApiFailure { None, Resource, Params, Index, Build, Deserialize, Size, Dims, Serialize, SearchParams, Search };
enum class Cleanup { Index, Params, Resource, Mutex };
enum class StreamEvent { Sync, Free, Index, Params, Resource };
struct State {
 std::mutex mu; std::condition_variable cv;
 std::map<void*,Block> blocks;
 int alloc_calls=0, fail_at=0, searches=0, resources=0, indexes=0, index_params=0, search_params=0, mutexes=0;
 bool gate=false, entered=false, release=false, second_blocked=false;
 int attempts=0, cross_thread_api=0;
 bool fail_distance_copy=false; int d2h_copy_count=0;
 int free_calls=0, free_fail_at=0, serialize_calls=0;
 bool free_after_release=false;
 void* failed_free=nullptr;
 size_t failed_free_bytes=0;
 std::map<void*,int> free_attempts;
 ApiFailure api_failure=ApiFailure::None;
 int error_text_mode=0;
 std::vector<Cleanup> cleanup;
 int copy_calls=0, copy_fail_at=0, copy_error=701, runtime_error_queries=0, last_runtime_code=0;
 int runtime_text_mode=0;
 std::thread::id owner;
 std::vector<FakeResource*> active_resources;
 std::vector<StreamEvent> stream_events;
 int stream_get_calls=0, stream_get_fail_at=0, stream_sync_calls=0, stream_sync_fail_at=0;
 int async_copy_calls=0, completed_work=0, unknown_work=0;
 int unlock_with_pending=0, params_destroyed_with_pending=0;
 size_t pending_at_gate=0;
 bool defer_frees=false;
} S;
static int64_t snapshot_n=0, snapshot_dim=0;
static thread_local const char* sdk_error=nullptr;
static int api_ok() { sdk_error=nullptr; return CUVS_SUCCESS; }
static int api_error(const char* detail) { sdk_error=detail; return CUVS_ERROR; }
static const char* failure_text(ApiFailure failure) {
 switch(failure) {
  case ApiFailure::Resource: return "FIRST_RESOURCE_ERROR";
  case ApiFailure::Params: return "FIRST_PARAMS_ERROR";
  case ApiFailure::Index: return "FIRST_INDEX_ERROR";
  case ApiFailure::Build: return "FIRST_BUILD_ERROR";
  case ApiFailure::Deserialize: return "FIRST_DESERIALIZE_ERROR";
  case ApiFailure::Size: return "FIRST_SIZE_ERROR";
  case ApiFailure::Dims: return "FIRST_DIMS_ERROR";
  case ApiFailure::Serialize: return "FIRST_SERIALIZE_ERROR";
  case ApiFailure::SearchParams: return "FIRST_SEARCH_PARAMS_ERROR";
  case ApiFailure::Search: return "FIRST_SEARCH_ERROR";
  default: return "UNEXPECTED_FAILURE";
 }
}
static bool inject_failure(ApiFailure failure) {
 if(S.api_failure!=failure) return false;
 api_error(S.error_text_mode==1?nullptr:S.error_text_mode==2?"":failure_text(failure));
 return true;
}
static bool device_range(const void* p,size_t n) {
 for (auto& b:S.blocks) if (b.second.live) {
  auto a=reinterpret_cast<uintptr_t>(p), first=reinterpret_cast<uintptr_t>(b.first);
  if(a>=first && a-first<=b.second.size && n<=b.second.size-(a-first)) return true;
 }
 return false;
}
static void api_entry() { if(S.gate && S.entered && !S.release && std::this_thread::get_id()!=S.owner) ++S.cross_thread_api; }
static size_t live_blocks() { size_t n=0; for(auto& b:S.blocks) n+=b.second.live; return n; }
static size_t pending_work() {
 size_t count=0;
 for(auto* resource:S.active_resources) count+=resource->stream.pending.size();
 return count;
}
static void clean_state() {
 std::lock_guard<std::mutex> lk(S.mu);
 CHECK(live_blocks()==0); CHECK(S.resources==0); CHECK(S.indexes==0); CHECK(S.index_params==0); CHECK(S.search_params==0); CHECK(S.mutexes==0);
 S.blocks.clear(); S.alloc_calls=S.fail_at=S.searches=0;
 S.gate=S.entered=S.release=S.second_blocked=false; S.attempts=S.cross_thread_api=0;
 S.fail_distance_copy=false; S.d2h_copy_count=0;
 S.free_calls=S.free_fail_at=S.serialize_calls=0; S.free_after_release=false;
 S.failed_free=nullptr; S.failed_free_bytes=0; S.free_attempts.clear();
 S.api_failure=ApiFailure::None; S.error_text_mode=0; S.cleanup.clear(); sdk_error=nullptr;
 S.copy_calls=S.copy_fail_at=S.runtime_error_queries=S.last_runtime_code=0;
 S.copy_error=701; S.runtime_text_mode=0;
 CHECK(S.active_resources.empty());
 S.stream_events.clear();
 S.stream_get_calls=S.stream_get_fail_at=S.stream_sync_calls=S.stream_sync_fail_at=0;
 S.async_copy_calls=S.completed_work=S.unknown_work=0;
 S.unlock_with_pending=S.params_destroyed_with_pending=0;
 S.pending_at_gate=0; S.defer_frees=false;
}
int cuvsResourcesCreate(cuvsResources_t* p) { if(inject_failure(ApiFailure::Resource)) return CUVS_ERROR; *p=new FakeResource; S.active_resources.push_back(*p); ++S.resources; return api_ok(); }
int cuvsResourcesDestroy(cuvsResources_t p) {
 CHECK(p->stream.pending.empty());
 S.stream_events.push_back(StreamEvent::Resource);
 S.active_resources.erase(std::find(S.active_resources.begin(),S.active_resources.end(),p));
 S.cleanup.push_back(Cleanup::Resource); delete p; --S.resources; return api_ok();
}
int cuvsStreamGet(cuvsResources_t resource,cudaStream_t* stream) {
 std::lock_guard<std::mutex> lock(S.mu); api_entry();
 if(++S.stream_get_calls==S.stream_get_fail_at) return api_error("FIRST_STREAM_GET_ERROR");
 CHECK(resource); *stream=&resource->stream; return api_ok();
}
int cuvsStreamSync(cuvsResources_t resource) {
 std::unique_lock<std::mutex> lock(S.mu); api_entry(); CHECK(resource);
 S.stream_events.push_back(StreamEvent::Sync);
 if(++S.stream_sync_calls==S.stream_sync_fail_at) {
  // Failed synchronization leaves execution unknown. Discard model callbacks
  // without claiming that a real runtime cancels device work or frees memory.
  S.unknown_work+=static_cast<int>(resource->stream.pending.size());
  resource->stream.pending.clear();
  return api_error("FIRST_STREAM_SYNC_ERROR");
 }
 while(!resource->stream.pending.empty()) {
  auto work=std::move(resource->stream.pending.front());
  resource->stream.pending.pop_front();
  work.execute(lock); ++S.completed_work;
 }
 return api_ok();
}
int cuvsCagraIndexParamsCreate(cuvsCagraIndexParams_t* p) { if(inject_failure(ApiFailure::Params)) return CUVS_ERROR; *p=new FakeIndexParams{}; ++S.index_params; return api_ok(); }
int cuvsCagraIndexParamsDestroy(cuvsCagraIndexParams_t p) { S.stream_events.push_back(StreamEvent::Params); S.cleanup.push_back(Cleanup::Params); delete p; --S.index_params; return api_ok(); }
int cuvsCagraIndexCreate(cuvsCagraIndex_t* p) { if(inject_failure(ApiFailure::Index)) return CUVS_ERROR; *p=new FakeIndex; ++S.indexes; return api_ok(); }
int cuvsCagraIndexDestroy(cuvsCagraIndex_t p) { S.stream_events.push_back(StreamEvent::Index); S.cleanup.push_back(Cleanup::Index); delete p; --S.indexes; return api_ok(); }
int cuvsCagraBuild(cuvsResources_t,cuvsCagraIndexParams_t p,DLManagedTensor* t,cuvsCagraIndex_t i) {
 CHECK(p->build_algo==NN_DESCENT); CHECK(p->graph_degree==128); CHECK(p->intermediate_graph_degree==expected_intermediate_degree); CHECK(p->nn_descent_niter==expected_build_iterations);
 if(inject_failure(ApiFailure::Build)) return CUVS_ERROR;
 i->n=t->dl_tensor.shape[0]; i->dim=t->dl_tensor.shape[1]; return api_ok();
}
int cuvsCagraSearchParamsCreate(cuvsCagraSearchParams_t* p) { if(inject_failure(ApiFailure::SearchParams)) return CUVS_ERROR; *p=new FakeSearchParams{64,1,SINGLE_CTA}; std::lock_guard<std::mutex> lk(S.mu); ++S.search_params; return 0; }
int cuvsCagraSearchParamsDestroy(cuvsCagraSearchParams_t p) {
 std::lock_guard<std::mutex> lock(S.mu);
 for(auto* resource:S.active_resources) for(const auto& work:resource->stream.pending)
  if(work.kind==PendingWork::Kind::Search && work.referenced_object==p) ++S.params_destroyed_with_pending;
 delete p; --S.search_params; return 0;
}
int cuvsRMMAlloc(cuvsResources_t,void** p,size_t n) {
 std::lock_guard<std::mutex> lk(S.mu); api_entry(); ++S.alloc_calls;
 if(S.alloc_calls==S.fail_at) { *p=nullptr; return api_error("host-test controlled allocation error"); }
 Block b{std::make_unique<unsigned char[]>(n),n,true}; *p=b.bytes.get(); S.blocks.emplace(*p,std::move(b)); return 0;
}
int cuvsRMMFree(cuvsResources_t resource,void* p,size_t n) {
 std::lock_guard<std::mutex> lk(S.mu); api_entry(); auto it=S.blocks.find(p);
 CHECK(it!=S.blocks.end()); CHECK(++S.free_attempts[p]==1);
 CHECK(it->second.live); CHECK(it->second.size==n);
 S.stream_events.push_back(StreamEvent::Free);
 if(++S.free_calls==S.free_fail_at) {
  S.failed_free=p; S.failed_free_bytes=n;
  if(S.free_after_release) it->second.live=false;
  return api_error("host-test controlled release error");
 }
 if(S.defer_frees) {
  resource->stream.pending.push_back({PendingWork::Kind::Free,p,[p](std::unique_lock<std::mutex>&) {
   auto block=S.blocks.find(p); CHECK(block!=S.blocks.end()); CHECK(block->second.live);
   block->second.live=false;
  }});
 } else it->second.live=false;
 return 0;
}
static void copy_bytes(void* dst,const void* src,size_t n,bool src_device,std::unique_lock<std::mutex>& lock) {
 CHECK(device_range(src,n)==src_device); CHECK(device_range(dst,n)!=src_device);
 if(S.gate && src_device && !S.entered) {
  S.entered=true; S.owner=std::this_thread::get_id(); S.pending_at_gate=pending_work()+1; S.cv.notify_all();
  CHECK(S.cv.wait_for(lock,std::chrono::seconds(8),[]{return S.release;}));
 }
 std::memcpy(dst,src,n);
}
cudaError_t cudaMemcpy(void* dst,const void* src,size_t n,int kind) {
 std::unique_lock<std::mutex> lk(S.mu); api_entry(); CHECK(kind==cudaMemcpyDefault);
 const bool src_device=device_range(src,n), dst_device=device_range(dst,n);
 CHECK(src_device!=dst_device);
 if(++S.copy_calls==S.copy_fail_at) return S.copy_error;
 if(S.fail_distance_copy && src_device && ++S.d2h_copy_count==2) return 1;
 copy_bytes(dst,src,n,src_device,lk); return cudaSuccess;
}
cudaError_t cudaMemcpyAsync(void* dst,const void* src,size_t n,int kind,cudaStream_t stream) {
 std::lock_guard<std::mutex> lock(S.mu); api_entry(); CHECK(kind==cudaMemcpyDefault); CHECK(stream);
 const bool src_device=device_range(src,n), dst_device=device_range(dst,n);
 CHECK(src_device!=dst_device);
 CHECK(std::any_of(S.active_resources.begin(),S.active_resources.end(),[stream](FakeResource* resource){return &resource->stream==stream;}));
 ++S.async_copy_calls;
 if(++S.copy_calls==S.copy_fail_at) return S.copy_error;
 if(S.fail_distance_copy && src_device && ++S.d2h_copy_count==2) return 1;
 stream->async_used=true;
 stream->pending.push_back({PendingWork::Kind::Copy,nullptr,[dst,src,n,src_device](std::unique_lock<std::mutex>& queue_lock) {
  copy_bytes(dst,src,n,src_device,queue_lock);
 }});
 return cudaSuccess;
}
const char* cudaGetErrorString(cudaError_t error) {
 std::lock_guard<std::mutex> lk(S.mu); ++S.runtime_error_queries; S.last_runtime_code=error;
 return S.runtime_text_mode==1?nullptr:S.runtime_text_mode==2?"":"HOST_RUNTIME_COPY_ERROR";
}
int cuvsCagraSearch(cuvsResources_t resource,cuvsCagraSearchParams_t p,cuvsCagraIndex_t idx,DLManagedTensor* q,DLManagedTensor* n,DLManagedTensor* d,cuvsFilter f) {
 std::lock_guard<std::mutex> lk(S.mu); api_entry(); ++S.searches;
 const long nq=q->dl_tensor.shape[0], dim=q->dl_tensor.shape[1], topk=n->dl_tensor.shape[1];
 CHECK(dim==idx->dim); CHECK(n->dl_tensor.shape[0]==nq); CHECK(d->dl_tensor.shape[0]==nq); CHECK(d->dl_tensor.shape[1]==topk);
 CHECK(p->itopk_size==std::max<size_t>(expected_minimum_itopk,static_cast<size_t>(topk)*4)); CHECK(p->search_width==4); CHECK(p->algo==expected_search_algorithm); CHECK(f.type==NO_FILTER); CHECK(f.addr==0);
 CHECK(device_range(q->dl_tensor.data,sizeof(float)*nq*dim)); CHECK(device_range(n->dl_tensor.data,sizeof(unsigned)*nq*topk)); CHECK(device_range(d->dl_tensor.data,sizeof(float)*nq*topk));
 if(inject_failure(ApiFailure::Search)) return CUVS_ERROR;
 auto* queries=static_cast<float*>(q->dl_tensor.data); auto* ids=static_cast<unsigned*>(n->dl_tensor.data); auto* dist=static_cast<float*>(d->dl_tensor.data);
 // Deliberate fake protocol, NOT a simulated ANN/Recall implementation.
 const auto execute=[queries,ids,dist,nq,dim,topk](std::unique_lock<std::mutex>&) {
  CHECK(device_range(queries,sizeof(float)*nq*dim)); CHECK(device_range(ids,sizeof(unsigned)*nq*topk)); CHECK(device_range(dist,sizeof(float)*nq*topk));
  for(long a=0;a<nq;++a) for(long b=0;b<topk;++b) { ids[a*topk+b]=static_cast<unsigned>(queries[a*dim])*10+static_cast<unsigned>(b); dist[a*topk+b]=queries[a*dim]+0.25f*b; }
 };
 if(resource->stream.async_used) resource->stream.pending.push_back({PendingWork::Kind::Search,p,execute});
 else {std::unique_lock<std::mutex> unused; execute(unused);}
 return 0;
}
int cuvsCagraSerialize(cuvsResources_t,const char*,cuvsCagraIndex_t i,bool dataset) { ++S.serialize_calls; CHECK(dataset); if(inject_failure(ApiFailure::Serialize)) return CUVS_ERROR; snapshot_n=i->n; snapshot_dim=i->dim; return api_ok(); }
int cuvsCagraDeserialize(cuvsResources_t,const char*,cuvsCagraIndex_t i) { if(inject_failure(ApiFailure::Deserialize)) return CUVS_ERROR; i->n=snapshot_n; i->dim=snapshot_dim; return api_ok(); }
int cuvsCagraIndexGetSize(cuvsCagraIndex_t i,int64_t* n) { if(inject_failure(ApiFailure::Size)) return CUVS_ERROR; *n=i->n; return api_ok(); }
int cuvsCagraIndexGetDims(cuvsCagraIndex_t i,int64_t* d) { if(inject_failure(ApiFailure::Dims)) return CUVS_ERROR; *d=i->dim; return api_ok(); }
const char* cuvsGetLastErrorText() { return sdk_error; }
int pthread_mutex_init(pthread_mutex_t* p,const void*) { p->native=new FakeMutex; ++S.mutexes; return 0; }
int pthread_mutex_destroy(pthread_mutex_t* p) { S.cleanup.push_back(Cleanup::Mutex); delete p->native; p->native=nullptr; --S.mutexes; return 0; }
int pthread_mutex_lock(pthread_mutex_t* p) {
 CHECK(p->native!=nullptr);
 if(p->native->value.try_lock()) { std::lock_guard<std::mutex> lk(S.mu); if(S.gate) {++S.attempts; S.cv.notify_all();} return 0; }
 {std::lock_guard<std::mutex> lk(S.mu); if(S.gate) {++S.attempts; S.second_blocked=true; S.cv.notify_all();}}
 p->native->value.lock(); return 0;
}
int pthread_mutex_unlock(pthread_mutex_t* p) {
 {std::lock_guard<std::mutex> lock(S.mu); if(pending_work()!=0) ++S.unlock_with_pending;}
 p->native->value.unlock(); return 0;
}
static void* build() { const float base[512]={}; void* h=seekdb_cuvs_build(base,256,2); CHECK(h); return h; }
static void search(void* h,long nq,long topk,float marker=2,bool distances=true) {
 std::vector<float> q(static_cast<size_t>(nq)*2); for(long a=0;a<nq;++a) q[a*2]=marker+a;
 std::vector<unsigned> ids(static_cast<size_t>(nq)*topk,0); std::vector<float> dist(ids.size(),-1);
 CHECK(seekdb_cuvs_search(h,q.data(),nq,topk,ids.data(),distances?dist.data():nullptr)==0);
 for(long a=0;a<nq;++a) for(long b=0;b<topk;++b) { CHECK(ids[a*topk+b]==static_cast<unsigned>(marker+a)*10+static_cast<unsigned>(b)); if(distances) CHECK(dist[a*topk+b]==marker+a+0.25f*b); }
}
static void reuse_and_growth(bool strict) {
 clean_state(); void* h=build(); CHECK(S.alloc_calls==0); search(h,1,1); CHECK(S.alloc_calls==3);
 const int first=S.alloc_calls; search(h,1,1,7); const int second=S.alloc_calls;
 std::cout<<"steady allocation delta="<<(second-first)<<" (required reuse: 0; baseline: 3)\n";
 if(strict) CHECK(second==first);
 search(h,3,4,9); const int grown=S.alloc_calls; search(h,2,2,11,false); if(strict) CHECK(S.alloc_calls==grown);
 // Different topk sizes retain exact per-call workspace and search parameters.
 search(h,1,129,5); search(h,1,1,4); seekdb_cuvs_free(h); clean_state();
 std::cout<<"PASS reuse/growth/copy/params/free\n";
}
static void allocation_failure_recovery() {
 for(int ordinal=1;ordinal<=3;++ordinal) {
  clean_state(); void* h=build(); search(h,1,1); const int old_searches=S.searches; S.fail_at=S.alloc_calls+ordinal;
  const float q[6]={2,0,3,0,4,0}; unsigned ids[12]={}; float dist[12]={};
  CHECK(seekdb_cuvs_search(h,q,3,4,ids,dist)!=0); CHECK(S.searches==old_searches);
  S.fail_at=0; search(h,1,1); search(h,3,4); seekdb_cuvs_free(h); clean_state();
 }
 std::cout<<"PASS allocation failure ordinals 1/2/3 and recovery\n";
}
static void deserialize_empty(bool strict) {
 clean_state(); void* h=build(); search(h,2,3); CHECK(seekdb_cuvs_serialize(h,"fake.snapshot")==0); seekdb_cuvs_free(h); clean_state();
 CHECK(!seekdb_cuvs_deserialize("fake.snapshot",255,2)); clean_state();
 h=seekdb_cuvs_deserialize("fake.snapshot",256,2); CHECK(h); CHECK(S.alloc_calls==0); CHECK(live_blocks()==0);
 search(h,2,3); CHECK(S.alloc_calls==3); const int first=S.alloc_calls; search(h,1,1); if(strict) CHECK(S.alloc_calls==first);
 seekdb_cuvs_free(h); clean_state(); std::cout<<"PASS deserialize empty workspace / shape rejection / cleanup\n";
}
static void invalid_sizes_no_allocation() {
 clean_state(); void* h=build(); const float q[6]={2,0,3,0,4,0}; unsigned ids[4]={};
 const long cases[4][2]={{3,0},{3,-1},{0,1},{-1,1}};
 for(const auto& c:cases) { const int alloc=S.alloc_calls, calls=S.searches; CHECK(seekdb_cuvs_search(h,q,c[0],c[1],ids,nullptr)!=0); CHECK(S.alloc_calls==alloc); CHECK(S.searches==calls); }
 search(h,1,1); seekdb_cuvs_free(h); clean_state(); std::cout<<"PASS invalid sizes before allocation / handle remains usable\n";
}
static void distance_copy_failure_recovery() {
 clean_state(); void* h=build(); const float q[2]={2,0}; unsigned ids[1]={0}; float dist[1]={-999};
 S.fail_distance_copy=true; S.d2h_copy_count=0;
 const int rc=seekdb_cuvs_search(h,q,1,1,ids,dist);
 CHECK(S.d2h_copy_count==2); CHECK(ids[0]==20); CHECK(dist[0]==-999); CHECK(rc!=0);
 S.fail_distance_copy=false; search(h,1,1,7); seekdb_cuvs_free(h); clean_state();
 std::cout<<"PASS distance copy failure reported / handle remains usable\n";
}
static void serialized_copyback() {
 clean_state(); void* h=build(); search(h,1,1); {std::lock_guard<std::mutex> lk(S.mu); S.gate=true;}
 std::exception_ptr e1,e2;
 std::thread a([&]{try{search(h,1,1,7);}catch(...){e1=std::current_exception();}});
 bool entered=false,attempted=false;
 {std::unique_lock<std::mutex> lk(S.mu); entered=S.cv.wait_for(lk,std::chrono::seconds(5),[]{return S.entered;});}
 std::thread b([&]{try{search(h,1,1,11);}catch(...){e2=std::current_exception();}});
 {std::unique_lock<std::mutex> lk(S.mu); attempted=S.cv.wait_for(lk,std::chrono::seconds(5),[]{return S.attempts>=2;}); S.release=true; S.cv.notify_all();}
 a.join(); b.join(); if(e1) std::rethrow_exception(e1); if(e2) std::rethrow_exception(e2);
 CHECK(entered); CHECK(attempted); CHECK(S.second_blocked); CHECK(S.cross_thread_api==0);
 if(S.async_copy_calls>0) {
  CHECK(S.pending_at_gate>=2); CHECK(S.unlock_with_pending==0); CHECK(S.params_destroyed_with_pending==0);
  std::cout<<"PASS mutex covers queued IDs and distance copyback through stream synchronization\n";
 }
 S.gate=false; seekdb_cuvs_free(h); clean_state(); std::cout<<"PASS same-handle serialization includes D2H IDs copyback\n";
}
static void release_failure_disables_handle(bool after_release) {
 for(long dim : {128L,256L}) for(int workspace=0;workspace<3;++workspace) {
  clean_state();
  std::vector<float> base(static_cast<size_t>(256*dim),0), query(static_cast<size_t>(2*dim),0);
  query[0]=1; query[dim]=2;
  unsigned ids[200]={}; float distances[200]={};
  void* h=seekdb_cuvs_build(base.data(),256,dim); CHECK(h);
  CHECK(seekdb_cuvs_search(h,query.data(),1,10,ids,distances)==0);
  CHECK(live_blocks()==3);
  S.free_after_release=after_release; S.free_fail_at=S.free_calls+(workspace==2?2:1);
  const int searches=S.searches;
  const int rc=seekdb_cuvs_search(h,query.data(),workspace==0?2:1,workspace==0?10:100,ids,distances);
  const int allocs=S.alloc_calls;
  const bool stopped_before_search=S.searches==searches;
  const int later_rc=seekdb_cuvs_search(h,query.data(),1,10,ids,distances);
  const int serialize_rc=seekdb_cuvs_serialize(h,"fake.disabled.snapshot");
  const bool no_later_work=S.alloc_calls==allocs && S.searches==searches && S.serialize_calls==0;
  seekdb_cuvs_free(h);
  const size_t unknown_live=live_blocks();
  const bool one_attempt=S.failed_free && S.free_attempts[S.failed_free]==1;
  std::cout<<"release failure dim="<<dim<<" workspace="<<workspace<<" after_release="<<after_release
   <<" rc="<<rc<<" later_rc="<<later_rc<<" serialize_rc="<<serialize_rc
   <<" unknown_live_blocks="<<unknown_live<<" uncertain_bytes="<<S.failed_free_bytes<<"\n";
  CHECK(rc!=0); CHECK(stopped_before_search); CHECK(later_rc!=0); CHECK(serialize_rc!=0);
  CHECK(no_later_work); CHECK(one_attempt); CHECK(unknown_live==(after_release?0U:1U));
  // Model-only unknown allocation; its real ownership cannot be inferred from an error.
  for(auto& block:S.blocks) block.second.live=false;
  clean_state();
 }
 std::cout<<"PASS release failure "<<(after_release?"after":"before")<<" deallocation / disabled handle / no retry\n";
}
struct StderrCapture {
 FILE* file=nullptr; int saved=-1;
 StderrCapture() {
  file=std::tmpfile(); CHECK(file); std::fflush(stderr);
  saved=_dup(_fileno(stderr)); CHECK(saved>=0); CHECK(_dup2(_fileno(file),_fileno(stderr))==0);
 }
 ~StderrCapture() {
  if(saved>=0) {std::fflush(stderr); _dup2(saved,_fileno(stderr)); _close(saved);}
  if(file) std::fclose(file);
 }
 std::string finish() {
  std::fflush(stderr); CHECK(_dup2(saved,_fileno(stderr))==0); _close(saved); saved=-1;
  std::rewind(file); std::string result; char bytes[512]; size_t n;
  while((n=std::fread(bytes,1,sizeof(bytes),file))>0) result.append(bytes,n);
  CHECK(!std::ferror(file)); return result;
 }
};
static void lifecycle_diagnostics() {
 struct Case { bool load; ApiFailure failure; const char* operation; std::vector<Cleanup> cleanup; };
 const std::vector<Cleanup> full={Cleanup::Index,Cleanup::Params,Cleanup::Resource,Cleanup::Mutex};
 const Case cases[]={
  {false,ApiFailure::Resource,"build resources",{Cleanup::Mutex}},
  {false,ApiFailure::Params,"build params",{Cleanup::Resource,Cleanup::Mutex}},
  {false,ApiFailure::Index,"build index",{Cleanup::Params,Cleanup::Resource,Cleanup::Mutex}},
  {false,ApiFailure::Build,"CAGRA build",full},
  {true,ApiFailure::Resource,"deserialize resources",{Cleanup::Mutex}},
  {true,ApiFailure::Params,"deserialize params",{Cleanup::Resource,Cleanup::Mutex}},
  {true,ApiFailure::Index,"deserialize index",{Cleanup::Params,Cleanup::Resource,Cleanup::Mutex}},
  {true,ApiFailure::Deserialize,"CAGRA deserialize",full},
  {true,ApiFailure::Size,"snapshot size",full},
  {true,ApiFailure::Dims,"snapshot dimensions",full}
 };
 int missing=0;
 for(const auto& c:cases) for(int mode=0;mode<3;++mode) {
  clean_state(); S.api_failure=c.failure; S.error_text_mode=mode; snapshot_n=256; snapshot_dim=2;
  const float base[512]={}; StderrCapture capture;
  void* h=c.load?seekdb_cuvs_deserialize("fake.snapshot",256,2):seekdb_cuvs_build(base,256,2);
  const std::string text=capture.finish();
  CHECK(h==nullptr); CHECK(S.cleanup==c.cleanup);
  const std::string expected=std::string("[seekdb_cuvs] ")+c.operation+" failed: "+(mode==0?failure_text(c.failure):"cuVS error text unavailable");
  const bool kept=text.find(expected)!=std::string::npos && text.find("(null)")==std::string::npos && text.find("snapshot shape mismatch")==std::string::npos;
  std::cout<<"lifecycle "<<(c.load?"load":"build")<<" op="<<c.operation<<" mode="<<mode<<" kept="<<kept<<" cleanup="<<S.cleanup.size()<<"\n";
  if(!kept) {++missing; std::cerr<<"MISSING "<<expected<<"\nACTUAL "<<text<<"\n";}
  clean_state();
 }
 for(int wrong=0;wrong<2;++wrong) {
  clean_state(); snapshot_n=256; snapshot_dim=2; StderrCapture capture;
  void* h=seekdb_cuvs_deserialize("fake.snapshot",wrong==0?255:256,wrong==1?3:2);
  const std::string text=capture.finish(); CHECK(!h); CHECK(S.cleanup==full);
  CHECK(text.find("snapshot shape mismatch")!=std::string::npos); clean_state();
 }
 std::cout<<"lifecycle diagnostic missing="<<missing<<" / 30; shape mismatch controls=2\n";
 CHECK(missing==0);
 std::cout<<"PASS build/load first error / null and empty text / exact cleanup order (HOST ONLY)\n";
}
static void copy_serialize_diagnostics() {
 const char* operations[]={"query H2D","IDs D2H","distances D2H"};
 const int returns[]={13,16,18}; int missing=0;
 for(int direction=0;direction<3;++direction) for(int mode=0;mode<3;++mode) {
  clean_state(); void* h=build(); search(h,1,10);
  const float query[2]={5,0}; std::vector<unsigned> ids(10,99999); std::vector<float> distances(10,-999);
  S.copy_fail_at=S.copy_calls+direction+1; S.copy_error=701+direction; S.runtime_text_mode=mode;
  const int calls=S.copy_calls, searches=S.searches; StderrCapture capture;
  int rc=seekdb_cuvs_search(h,query,1,10,ids.data(),distances.data());
  const std::string text=capture.finish();
  CHECK(rc==returns[direction]); CHECK(S.copy_calls==calls+direction+1);
  CHECK(S.searches==searches+(direction==0?0:1));
  for(size_t j=0;j<ids.size();++j) {CHECK(ids[j]==(direction==2?50U+static_cast<unsigned>(j):99999U)); CHECK(distances[j]==-999);}
  const std::string expected=std::string("[seekdb_cuvs] ")+operations[direction]+" failed: runtime code="+std::to_string(S.copy_error)+": "+(mode==0?"HOST_RUNTIME_COPY_ERROR":"runtime error text unavailable");
  const bool kept=text.find(expected)!=std::string::npos && text.find("(null)")==std::string::npos && S.runtime_error_queries==1 && S.last_runtime_code==S.copy_error;
  std::cout<<"copy diagnostic op="<<operations[direction]<<" mode="<<mode<<" rc="<<rc<<" runtime="<<S.copy_error<<" kept="<<kept<<"\n";
  if(!kept) {++missing; std::cerr<<"MISSING "<<expected<<"\nACTUAL "<<text<<"\n";}
  S.copy_fail_at=0; const int queries=S.runtime_error_queries; search(h,1,10,7); CHECK(S.runtime_error_queries==queries);
  seekdb_cuvs_free(h); clean_state();
 }
 for(int mode=0;mode<3;++mode) {
  clean_state(); void* h=build(); S.api_failure=ApiFailure::Serialize; S.error_text_mode=mode; StderrCapture capture;
  int rc=seekdb_cuvs_serialize(h,"fake.snapshot"); const std::string text=capture.finish();
  CHECK(rc==1); CHECK(S.serialize_calls==1); CHECK(S.cleanup.empty());
  const std::string expected=std::string("[seekdb_cuvs] serialize failed: ")+(mode==0?failure_text(ApiFailure::Serialize):"cuVS error text unavailable");
  const bool kept=text.find(expected)!=std::string::npos && text.find("(null)")==std::string::npos;
  std::cout<<"serialize diagnostic mode="<<mode<<" rc="<<rc<<" kept="<<kept<<"\n";
  if(!kept) {++missing; std::cerr<<"MISSING "<<expected<<"\nACTUAL "<<text<<"\n";}
  S.api_failure=ApiFailure::None; search(h,1,10); CHECK(seekdb_cuvs_serialize(h,"fake.snapshot")==0);
  seekdb_cuvs_free(h); clean_state();
 }
 std::cout<<"copy/serialize diagnostic missing="<<missing<<" / 12\n"; CHECK(missing==0);
 std::cout<<"PASS copy/serialize diagnostics / return codes / output order / handle reuse (HOST ONLY)\n";
}
static void stream_model_defers_work() {
 clean_state();
 cuvsResources_t resource=nullptr;
 CHECK(cuvsResourcesCreate(&resource)==CUVS_SUCCESS);
 cudaStream_t stream=nullptr;
 CHECK(cuvsStreamGet(resource,&stream)==CUVS_SUCCESS);
 void* device=nullptr;
 CHECK(cuvsRMMAlloc(resource,&device,4)==CUVS_SUCCESS);
 std::memset(device,0,4);
 const unsigned char input[4]={3,5,7,11};
 unsigned char output[4]={};
 CHECK(cudaMemcpyAsync(device,input,4,cudaMemcpyDefault,stream)==cudaSuccess);
 CHECK(cudaMemcpyAsync(output,device,4,cudaMemcpyDefault,stream)==cudaSuccess);
 CHECK(static_cast<unsigned char*>(device)[0]==0);
 CHECK(output[0]==0); CHECK(pending_work()==2); CHECK(S.completed_work==0);
 CHECK(cuvsStreamSync(resource)==CUVS_SUCCESS);
 CHECK(std::memcmp(input,output,4)==0); CHECK(S.completed_work==2); CHECK(pending_work()==0);
 CHECK(cuvsRMMFree(resource,device,4)==CUVS_SUCCESS);
 CHECK(cuvsResourcesDestroy(resource)==CUVS_SUCCESS);
 clean_state();
 std::cout<<"PASS fake asynchronous copies execute only at Sync, in FIFO order (HOST ONLY)\n";
}
static void stream_workspace_growth_and_reuse() {
 clean_state();
 void* handle=build();
 search(handle,1,10,3);
 const bool bridge_uses_resource_stream=S.stream_get_calls==1 && S.stream_sync_calls==1 &&
  S.async_copy_calls==3 && S.completed_work==4 && pending_work()==0;
 if(!bridge_uses_resource_stream) {
  std::cout<<"stream contract observed get="<<S.stream_get_calls<<" sync="<<S.stream_sync_calls
   <<" async_copy="<<S.async_copy_calls<<" completed="<<S.completed_work<<"\n";
  seekdb_cuvs_free(handle); clean_state();
  CHECK(bridge_uses_resource_stream);
 }
 S.defer_frees=true;
 struct Case {long queries,topk; int allocations;};
 const Case cases[]={{64,10,3},{1,10,0},{64,100,2},{1,100,0},{1,10,0},{64,100,0}};
 for(const auto& entry:cases) {
  const int allocations=S.alloc_calls, gets=S.stream_get_calls, syncs=S.stream_sync_calls;
  search(handle,entry.queries,entry.topk,7);
  CHECK(S.alloc_calls==allocations+entry.allocations);
  CHECK(S.stream_get_calls==gets+1); CHECK(S.stream_sync_calls==syncs+1);
  CHECK(pending_work()==0); CHECK(S.unlock_with_pending==0); CHECK(S.params_destroyed_with_pending==0);
 }
 seekdb_cuvs_free(handle); clean_state();
 std::cout<<"PASS stream ordering for nq 1/64/1, Top10/100, growth and workspace reuse\n";
}
static void stream_get_failure_recovery() {
 for(bool warm:{false,true}) {
  clean_state(); void* handle=build();
  if(warm) search(handle,1,10);
  const int allocations=S.alloc_calls, searches=S.searches, copies=S.async_copy_calls, syncs=S.stream_sync_calls;
  S.stream_get_fail_at=S.stream_get_calls+1;
  const float query[2]={5,0}; unsigned ids[10]={}; float distances[10]={};
  const int result=seekdb_cuvs_search(handle,query,1,10,ids,distances);
  CHECK(result==20); CHECK(S.alloc_calls==allocations); CHECK(S.searches==searches);
  CHECK(S.async_copy_calls==copies); CHECK(S.stream_sync_calls==syncs); CHECK(pending_work()==0);
  S.stream_get_fail_at=0;
  search(handle,1,10,9); seekdb_cuvs_free(handle); clean_state();
 }
 std::cout<<"PASS StreamGet failure rc20 before work, cold/warm handle recovery\n";
}
static void stream_sync_failure_disables_handle() {
 clean_state(); void* handle=build(); search(handle,1,10);
 S.stream_sync_fail_at=S.stream_sync_calls+1;
 const float query[2]={5,0}; unsigned ids[10]={}; float distances[10]={};
 CHECK(seekdb_cuvs_search(handle,query,1,10,ids,distances)==21);
 CHECK(S.unknown_work==4);
 const int searches=S.searches, copies=S.async_copy_calls, allocations=S.alloc_calls;
 S.stream_sync_fail_at=0;
 CHECK(seekdb_cuvs_search(handle,query,1,10,ids,distances)==19);
 CHECK(seekdb_cuvs_serialize(handle,"fake.disabled.snapshot")!=0);
 CHECK(S.searches==searches); CHECK(S.async_copy_calls==copies); CHECK(S.alloc_calls==allocations);
 seekdb_cuvs_free(handle);
 CHECK(S.unknown_work==4);
 std::cout<<"PASS failed Sync rc21 disables handle; model work execution remains unknown=4\n";
 clean_state();
}
static void stream_failure_preserves_first_return() {
 struct Case {ApiFailure api; int copy; bool allocation; int result;};
 const Case cases[]={
  {ApiFailure::None,0,true,10}, {ApiFailure::SearchParams,0,false,14},
  {ApiFailure::Search,0,false,15}, {ApiFailure::None,1,false,13},
  {ApiFailure::None,2,false,16}, {ApiFailure::None,3,false,18}
 };
 for(const auto& entry:cases) {
  clean_state(); void* handle=build(); search(handle,1,10);
  S.api_failure=entry.api;
  if(entry.copy) S.copy_fail_at=S.copy_calls+entry.copy;
  if(entry.allocation) S.fail_at=S.alloc_calls+1;
  S.stream_sync_fail_at=S.stream_sync_calls+1;
  const int prior_syncs=S.stream_sync_calls;
  std::vector<float> query(entry.allocation?128U:2U,0);
  query[0]=5;
  std::vector<unsigned> ids(entry.allocation?640U:10U,0);
  std::vector<float> distances(ids.size(),0);
  StderrCapture capture;
  const int result=seekdb_cuvs_search(handle,query.data(),entry.allocation?64:1,10,ids.data(),distances.data());
  const std::string diagnostic=capture.finish();
  CHECK(result==entry.result); CHECK(S.stream_sync_calls==prior_syncs+1);
  CHECK(diagnostic.find("FIRST_STREAM_SYNC_ERROR")!=std::string::npos);
  S.api_failure=ApiFailure::None; S.copy_fail_at=0; S.fail_at=0; S.stream_sync_fail_at=0;
  CHECK(seekdb_cuvs_search(handle,query.data(),1,10,ids.data(),distances.data())==19);
  seekdb_cuvs_free(handle);
  std::cout<<"PASS earlier rc="<<entry.result<<" preserved through failed Sync; unknown model work="<<S.unknown_work<<"\n";
  clean_state();
 }
}
static void stream_free_ordering() {
 // The pre/post-failure controls record unknown execution explicitly. Only the
 // success control establishes completion of these synthetic queued operations.
 for(int failed_phase:{0,1,2}) {
  clean_state(); void* handle=build(); search(handle,1,10);
  CHECK(S.active_resources.size()==1);
  auto* resource=S.active_resources.front();
  cudaStream_t stream=nullptr;
  CHECK(cuvsStreamGet(resource,&stream)==CUVS_SUCCESS);
  const unsigned char marker[4]={17,19,23,29};
  CHECK(cudaMemcpyAsync(S.blocks.begin()->first,marker,4,cudaMemcpyDefault,stream)==cudaSuccess);
  S.defer_frees=true;
  const int completed=S.completed_work, unknown=S.unknown_work;
  if(failed_phase) S.stream_sync_fail_at=S.stream_sync_calls+failed_phase;
  S.stream_events.clear();
  seekdb_cuvs_free(handle);
  const std::vector<StreamEvent> expected={StreamEvent::Sync,StreamEvent::Free,StreamEvent::Free,
   StreamEvent::Free,StreamEvent::Index,StreamEvent::Params,StreamEvent::Sync,StreamEvent::Resource};
  CHECK(S.stream_events==expected); CHECK(pending_work()==0);
  if(failed_phase==0) {CHECK(S.completed_work==completed+4); CHECK(S.unknown_work==unknown); CHECK(live_blocks()==0);}
  if(failed_phase==1) {CHECK(S.completed_work==completed+3); CHECK(S.unknown_work==unknown+1); CHECK(live_blocks()==0);}
  if(failed_phase==2) {CHECK(S.completed_work==completed+1); CHECK(S.unknown_work==unknown+3); CHECK(live_blocks()==3);}
  std::cout<<"PASS free pre/post Sync order phase="<<failed_phase<<" unknown model work="<<S.unknown_work
   <<" model allocations unresolved="<<live_blocks()<<"\n";
  // Dispose fixture-owned backing storage after recording the unknown state.
  for(auto& block:S.blocks) block.second.live=false;
  clean_state();
 }
}
static void stream_ordering() {
 stream_model_defers_work();
 stream_workspace_growth_and_reuse();
 stream_get_failure_recovery();
 stream_sync_failure_disables_handle();
 stream_failure_preserves_first_return();
 serialized_copyback();
 stream_free_ordering();
 std::cout<<"PASS stream-ordering regression suite (HOST ONLY)\n";
}
int main(int argc,char** argv) {
 const bool strict=argc==2 && std::string(argv[1])=="--require-reuse";
 const bool before_only=argc==2 && std::string(argv[1])=="--release-failure-before";
 const bool after_only=argc==2 && std::string(argv[1])=="--release-failure-after";
 const bool diagnostic_only=argc==2 && std::string(argv[1])=="--lifecycle-diagnostics";
 const bool copy_only=argc==2 && std::string(argv[1])=="--copy-serialize-diagnostics";
 const bool stream_only=argc==2 && std::string(argv[1])=="--stream-ordering";
 if(argc>1 && !strict && !before_only && !after_only && !diagnostic_only && !copy_only && !stream_only) {std::cerr<<"usage: host_test [--require-reuse|--release-failure-before|--release-failure-after|--lifecycle-diagnostics|--copy-serialize-diagnostics|--stream-ordering]\n"; return 2;}
 try {
  if(before_only || after_only) {release_failure_disables_handle(after_only); return 0;}
  if(diagnostic_only) {lifecycle_diagnostics(); return 0;}
  if(copy_only) {copy_serialize_diagnostics(); return 0;}
  if(stream_only) {stream_ordering(); return 0;}
  reuse_and_growth(strict); allocation_failure_recovery(); deserialize_empty(strict);
  if(strict) {invalid_sizes_no_allocation(); distance_copy_failure_recovery(); serialized_copyback(); release_failure_disables_handle(false); release_failure_disables_handle(true); lifecycle_diagnostics(); copy_serialize_diagnostics(); stream_ordering();}
  std::cout<<"HOST LOGIC PASS (no GPU/POSIX/Recall claim)\n"; return 0;
 }
 catch(const std::exception& e) {std::cerr<<"FAIL "<<e.what()<<"\n"; return 1;}
}
