#include "fake_api.h"
#include "seekdb_cuvs_bridge.h"
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <exception>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#define CHECK(x) do { if (!(x)) throw std::runtime_error(std::string(__func__)+": "+#x); } while (0)
struct FakeResource {};
struct FakeIndex { int64_t n=0, dim=0; };
struct FakeMutex { std::mutex value; };
struct Block { std::unique_ptr<unsigned char[]> bytes; size_t size; bool live=true; };
struct State {
 std::mutex mu; std::condition_variable cv;
 std::map<void*,Block> blocks;
 int alloc_calls=0, fail_at=0, searches=0, resources=0, indexes=0, index_params=0, search_params=0, mutexes=0;
 bool gate=false, entered=false, release=false, second_blocked=false;
 int attempts=0, cross_thread_api=0;
 bool fail_distance_copy=false; int d2h_copy_count=0;
 std::thread::id owner;
} S;
static int64_t snapshot_n=0, snapshot_dim=0;
static bool device_range(const void* p,size_t n) {
 for (auto& b:S.blocks) if (b.second.live) {
  auto a=reinterpret_cast<uintptr_t>(p), first=reinterpret_cast<uintptr_t>(b.first);
  if(a>=first && a-first<=b.second.size && n<=b.second.size-(a-first)) return true;
 }
 return false;
}
static void api_entry() { if(S.gate && S.entered && !S.release && std::this_thread::get_id()!=S.owner) ++S.cross_thread_api; }
static size_t live_blocks() { size_t n=0; for(auto& b:S.blocks) n+=b.second.live; return n; }
static void clean_state() {
 std::lock_guard<std::mutex> lk(S.mu);
 CHECK(live_blocks()==0); CHECK(S.resources==0); CHECK(S.indexes==0); CHECK(S.index_params==0); CHECK(S.search_params==0); CHECK(S.mutexes==0);
 S.blocks.clear(); S.alloc_calls=S.fail_at=S.searches=0;
 S.gate=S.entered=S.release=S.second_blocked=false; S.attempts=S.cross_thread_api=0;
 S.fail_distance_copy=false; S.d2h_copy_count=0;
}
int cuvsResourcesCreate(cuvsResources_t* p) { *p=new FakeResource; ++S.resources; return 0; }
int cuvsResourcesDestroy(cuvsResources_t p) { delete p; --S.resources; return 0; }
int cuvsCagraIndexParamsCreate(cuvsCagraIndexParams_t* p) { *p=new FakeIndexParams{}; ++S.index_params; return 0; }
int cuvsCagraIndexParamsDestroy(cuvsCagraIndexParams_t p) { delete p; --S.index_params; return 0; }
int cuvsCagraIndexCreate(cuvsCagraIndex_t* p) { *p=new FakeIndex; ++S.indexes; return 0; }
int cuvsCagraIndexDestroy(cuvsCagraIndex_t p) { delete p; --S.indexes; return 0; }
int cuvsCagraBuild(cuvsResources_t,cuvsCagraIndexParams_t p,DLManagedTensor* t,cuvsCagraIndex_t i) {
 CHECK(p->build_algo==NN_DESCENT); CHECK(p->graph_degree==128); CHECK(p->intermediate_graph_degree==256); CHECK(p->nn_descent_niter==100);
 i->n=t->dl_tensor.shape[0]; i->dim=t->dl_tensor.shape[1]; return 0;
}
int cuvsCagraSearchParamsCreate(cuvsCagraSearchParams_t* p) { *p=new FakeSearchParams{64,1}; std::lock_guard<std::mutex> lk(S.mu); ++S.search_params; return 0; }
int cuvsCagraSearchParamsDestroy(cuvsCagraSearchParams_t p) { delete p; std::lock_guard<std::mutex> lk(S.mu); --S.search_params; return 0; }
int cuvsRMMAlloc(cuvsResources_t,void** p,size_t n) {
 std::lock_guard<std::mutex> lk(S.mu); api_entry(); ++S.alloc_calls;
 if(S.alloc_calls==S.fail_at) { *p=nullptr; return CUVS_ERROR; }
 Block b{std::make_unique<unsigned char[]>(n),n,true}; *p=b.bytes.get(); S.blocks.emplace(*p,std::move(b)); return 0;
}
int cuvsRMMFree(cuvsResources_t,void* p,size_t n) {
 std::lock_guard<std::mutex> lk(S.mu); api_entry(); auto it=S.blocks.find(p);
 CHECK(it!=S.blocks.end()); CHECK(it->second.live); CHECK(it->second.size==n); it->second.live=false; return 0;
}
int cudaMemcpy(void* dst,const void* src,size_t n,int kind) {
 std::unique_lock<std::mutex> lk(S.mu); api_entry(); CHECK(kind==cudaMemcpyDefault);
 const bool src_device=device_range(src,n), dst_device=device_range(dst,n);
 CHECK(src_device!=dst_device);
 if(S.fail_distance_copy && src_device && ++S.d2h_copy_count==2) return 1;
 if(S.gate && src_device && !S.entered) {
  S.entered=true; S.owner=std::this_thread::get_id(); S.cv.notify_all();
  CHECK(S.cv.wait_for(lk,std::chrono::seconds(8),[]{return S.release;}));
 }
 std::memcpy(dst,src,n); return cudaSuccess;
}
int cuvsCagraSearch(cuvsResources_t,cuvsCagraSearchParams_t p,cuvsCagraIndex_t idx,DLManagedTensor* q,DLManagedTensor* n,DLManagedTensor* d,cuvsFilter f) {
 std::lock_guard<std::mutex> lk(S.mu); api_entry(); ++S.searches;
 const long nq=q->dl_tensor.shape[0], dim=q->dl_tensor.shape[1], topk=n->dl_tensor.shape[1];
 CHECK(dim==idx->dim); CHECK(n->dl_tensor.shape[0]==nq); CHECK(d->dl_tensor.shape[0]==nq); CHECK(d->dl_tensor.shape[1]==topk);
 CHECK(p->itopk_size==std::max<size_t>(512,static_cast<size_t>(topk)*4)); CHECK(p->search_width==4); CHECK(f.type==NO_FILTER); CHECK(f.addr==0);
 CHECK(device_range(q->dl_tensor.data,sizeof(float)*nq*dim)); CHECK(device_range(n->dl_tensor.data,sizeof(unsigned)*nq*topk)); CHECK(device_range(d->dl_tensor.data,sizeof(float)*nq*topk));
 auto* queries=static_cast<float*>(q->dl_tensor.data); auto* ids=static_cast<unsigned*>(n->dl_tensor.data); auto* dist=static_cast<float*>(d->dl_tensor.data);
 // Deliberate fake protocol, NOT a simulated ANN/Recall implementation.
 for(long a=0;a<nq;++a) for(long b=0;b<topk;++b) { ids[a*topk+b]=static_cast<unsigned>(queries[a*dim])*10+static_cast<unsigned>(b); dist[a*topk+b]=queries[a*dim]+0.25f*b; }
 return 0;
}
int cuvsCagraSerialize(cuvsResources_t,const char*,cuvsCagraIndex_t i,bool dataset) { CHECK(dataset); snapshot_n=i->n; snapshot_dim=i->dim; return 0; }
int cuvsCagraDeserialize(cuvsResources_t,const char*,cuvsCagraIndex_t i) { i->n=snapshot_n; i->dim=snapshot_dim; return 0; }
int cuvsCagraIndexGetSize(cuvsCagraIndex_t i,int64_t* n) { *n=i->n; return 0; }
int cuvsCagraIndexGetDims(cuvsCagraIndex_t i,int64_t* d) { *d=i->dim; return 0; }
const char* cuvsGetLastErrorText() { return "host-test controlled error"; }
int pthread_mutex_init(pthread_mutex_t* p,const void*) { p->native=new FakeMutex; ++S.mutexes; return 0; }
int pthread_mutex_destroy(pthread_mutex_t* p) { delete p->native; p->native=nullptr; --S.mutexes; return 0; }
int pthread_mutex_lock(pthread_mutex_t* p) {
 CHECK(p->native!=nullptr);
 if(p->native->value.try_lock()) { std::lock_guard<std::mutex> lk(S.mu); if(S.gate) {++S.attempts; S.cv.notify_all();} return 0; }
 {std::lock_guard<std::mutex> lk(S.mu); if(S.gate) {++S.attempts; S.second_blocked=true; S.cv.notify_all();}}
 p->native->value.lock(); return 0;
}
int pthread_mutex_unlock(pthread_mutex_t* p) { p->native->value.unlock(); return 0; }
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
 // Different topk sizes retain exact per-call search params, including >512 itopk.
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
 S.gate=false; seekdb_cuvs_free(h); clean_state(); std::cout<<"PASS same-handle serialization includes D2H IDs copyback\n";
}
int main(int argc,char** argv) {
 const bool strict=argc==2 && std::string(argv[1])=="--require-reuse";
 if(argc>1 && !strict) {std::cerr<<"usage: host_test [--require-reuse]\n"; return 2;}
 try {reuse_and_growth(strict); allocation_failure_recovery(); deserialize_empty(strict); if(strict) {invalid_sizes_no_allocation(); distance_copy_failure_recovery(); serialized_copyback();} std::cout<<"HOST LOGIC PASS (no GPU/POSIX/Recall claim)\n"; return 0;}
 catch(const std::exception& e) {std::cerr<<"FAIL "<<e.what()<<"\n"; return 1;}
}
