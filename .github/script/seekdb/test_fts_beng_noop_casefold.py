#!/usr/bin/env python3
"""Compile exact production methods against small value/allocator facades.
No server SQL or integration claims. Charset algorithms/tables are production.
ObString, ObDatum, token input, and arena are minimal facades: allocation counts
are logical calls, not production allocator latency. The normalizer declaration
is read from the actual header (access widened for counters), old/new method
bodies come from the selected git revision/worktree, and charset .cc files are
included unchanged. G++ plus its standard library are the only dependencies.
"""
import argparse
import os
import pathlib
import subprocess
import tempfile
import sys
import unittest

def main():
    PARSER = argparse.ArgumentParser(description=__doc__)
    PARSER.add_argument("--repo", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[3])
    PARSER.add_argument("--baseline", default="fe876f8f805969970621a18a1cf812ff322fc78c")
    PARSER.add_argument("--output-dir", type=pathlib.Path)
    PARSER.add_argument("--benchmark", action="store_true", help="Run only a helper microbenchmark, not SQL.")
    PARSER.add_argument("--sanitize", action="store_true", help="Use GCC ASan/UBSan; leak detection unavailable under ptrace.")
    ARGS = PARSER.parse_args()
    ROOT = ARGS.repo.resolve()
    TEMP = tempfile.TemporaryDirectory(prefix="seekdb-beng-native-") if ARGS.output_dir is None else None
    HERE = pathlib.Path(TEMP.name) if TEMP else ARGS.output_dir.resolve()
    HERE.mkdir(parents=True, exist_ok=True)
    def function(s, marker):
        start=s.index(marker)
        left=s.index('{', start)
        depth=1
        end=left+1
        while depth:
            depth += (s[end]=='{')-(s[end]=='}')
            end+=1
        return s[start:end]
    HARNESS_MAIN = r'''
    static size_t fallback_other(const ObCharsetInfo*,char*src,size_t n,char*dst,size_t cap){
      assert(cap>=n);if(src!=dst)std::memcpy(dst,src,n);for(size_t i=0;i<n;++i)if(dst[i]=='q')dst[i]='Q';return n;
    }
    static ObCharsetInfo make_cs(int number,ObUnicaseInfo *info=&ob_unicase_default){
      static ObCharsetHandler utf8={},binary={},other={};
      utf8.casedn=ob_casedn_utf8mb4;binary.casedn=ob_case_bin;other.casedn=fallback_other;
      ObCharsetInfo cs={};cs.number=number;cs.ctype=number==63?ctype_bin:ctype_utf8mb4;
      cs.casedn_multiply=1;cs.caseinfo=info;cs.cset=number==63?&binary:number==999?&other:&utf8;return cs;
    }
    static uint64_t comparisons=0,fast_tokens=0,allocations_saved=0;
    static bool eligible(const std::string&s,const ObCharsetInfo&cs){
      if(ObCharset::charset_type_by_coll(static_cast<ObCollationType>(cs.number))!=CHARSET_UTF8MB4)return false;
      size_t lo=0,hi=s.size(); while(lo<hi&&!ob_isalnum(&cs,s[lo]))++lo;while(hi>lo&&!ob_isalnum(&cs,s[hi-1]))--hi; if(lo==hi)return false;
      return std::all_of(s.begin()+lo,s.begin()+hi,[](unsigned char c){return c<128&&(c<'A'||c>'Z');});
    }
    static void one_case(const std::string&text,const ObCharsetInfo&cs){
      static InputStream a,b;
      static BaselineNormalizer oldn;static CandidateNormalizer newn;
      a.words={text};b.words={text};a.index=b.index=0;
      oldn.in_stream_=&a;newn.in_stream_=&b;oldn.cs_=newn.cs_=&cs;oldn.is_inited_=newn.is_inited_=true;
      oldn.norm_allocator_.calls=newn.norm_allocator_.calls=0;
      ObDatum x,y;int64_t fx=-1,fy=-2;
      int rx=oldn.get_next(x,fx),ry=newn.get_next(y,fy);
      assert(rx==ry&&fx==fy&&x.len_==y.len_);
      if(rx==OB_SUCCESS){
        assert(!std::memcmp(x.ptr_,y.ptr_,x.len_));
        std::string normalized(x.ptr_,x.len_);
        if(eligible(text,cs)){
          assert(oldn.norm_allocator_.calls==1&&newn.norm_allocator_.calls==0);
          assert(y.ptr_>=b.words[0].data()&&y.ptr_+y.len_<=b.words[0].data()+b.words[0].size());
          ++fast_tokens;++allocations_saved;
        }else assert(oldn.norm_allocator_.calls==newn.norm_allocator_.calls);
      }
      ++comparisons;
    }
    static void append_utf8(std::string&s,uint32_t cp){
      unsigned char bytes[4];size_t n=0;
      if(cp<128)bytes[n++]=cp;
      else if(cp<2048){bytes[n++]=0xc0|(cp>>6);bytes[n++]=0x80|(cp&63);}
      else if(cp<65536){bytes[n++]=0xe0|(cp>>12);bytes[n++]=0x80|((cp>>6)&63);bytes[n++]=0x80|(cp&63);}
      else{bytes[n++]=0xf0|(cp>>18);bytes[n++]=0x80|((cp>>12)&63);bytes[n++]=0x80|((cp>>6)&63);bytes[n++]=0x80|(cp&63);}
      s.append(reinterpret_cast<char*>(bytes),n);
    }
    static void durable_copy_test(const ObCharsetInfo&cs){
      InputStream input;input.words={"alpha","BRAVO","charlie","delta","alpha",std::string("x\0y",3),"A\xc3\x84" "B"};
      CandidateNormalizer norm;norm.cs_=&cs;norm.in_stream_=&input;norm.is_inited_=true;
      CandidateParser parser(&norm);
      std::vector<std::pair<const char*,size_t>> retained;
      std::vector<std::string> expected={"alpha","bravo","charlie","delta","alpha",std::string("x\0y",3),"a\xc3\xa4" "b"};
      for(auto&s:expected){const char*p=nullptr;int64_t n=0,c=0,f=0;assert(parser.get_next_token(p,n,c,f)==OB_SUCCESS);assert(n==static_cast<int64_t>(s.size())&&c==n&&f==1);retained.emplace_back(p,n);}
      const char*p=nullptr;int64_t n,c,f;assert(parser.get_next_token(p,n,c,f)==OB_ITER_END);
      norm.norm_allocator_.reuse();std::memset(norm.norm_allocator_.storage.data(),0xAA,norm.norm_allocator_.storage.size());
      for(auto&s:input.words)std::fill(s.begin(),s.end(),'Z');
      for(size_t i=0;i<retained.size();++i)assert(std::string(retained[i].first,retained[i].second)==expected[i]);
      assert(parser.allocator_.calls==expected.size());
      std::cout<<"parser durable copies: "<<expected.size()<<" retained tokens PASS\n";
    }
    static void error_tests(const ObCharsetInfo&cs){
      InputStream a,b;a.words=b.words={"UPPER"};
      BaselineNormalizer oldn;CandidateNormalizer newn;oldn.in_stream_=&a;newn.in_stream_=&b;oldn.cs_=newn.cs_=&cs;oldn.is_inited_=newn.is_inited_=true;
      oldn.norm_allocator_.fail=newn.norm_allocator_.fail=true;ObDatum x,y;int64_t f,g;
      assert(oldn.get_next(x,f)==OB_ALLOCATE_MEMORY_FAILED&&newn.get_next(y,g)==OB_ALLOCATE_MEMORY_FAILED);
      oldn.is_inited_=newn.is_inited_=false;assert(oldn.get_next(x,f)==OB_NOT_INIT&&newn.get_next(y,g)==OB_NOT_INIT);
      oldn.is_inited_=newn.is_inited_=true;a.words=b.words={"lower"};a.index=b.index=0;
      assert(oldn.get_next(x,f)==OB_ALLOCATE_MEMORY_FAILED&&newn.get_next(y,g)==OB_SUCCESS);
      std::cout<<"fallback allocation failures/uninitialized states: PASS; lowercase view succeeds without allocation\n";
    }
    static volatile uint64_t checksum=0;
    template<class N>static double bench(N&norm,InputStream&in,const ObCharsetInfo&cs,int loops){
      norm.cs_=&cs;norm.in_stream_=&in;norm.is_inited_=true;
      auto begin=std::chrono::steady_clock::now();
      for(int r=0;r<loops;++r){in.index=0;for(size_t i=0;i<in.words.size();++i){ObDatum d;int64_t f;int ret=norm.get_next(d,f);assert(ret==OB_SUCCESS);checksum+=d.len_+static_cast<unsigned char>(d.ptr_[d.len_-1]);}}
      return std::chrono::duration<double,std::nano>(std::chrono::steady_clock::now()-begin).count()/(loops*in.words.size());
    }
    int main(){
      auto utf=make_cs(45),bin=make_cs(46),uni=make_cs(224,&ob_unicase_unicode520),binary=make_cs(63),other=make_cs(999);
      for(auto&cs:{utf,bin,uni,binary,other}){
        std::vector<std::string> fixed={""," ","!!!","alpha","12345","ALPHA","AlPhA","x\0y",std::string("x\0y",3),"__hello__","\xc3\x84" "BC","A\xc3\x84" "B","A\xff" "B","alpha "," hello "};
        for(auto&s:fixed)one_case(s,cs);
        for(int b=0;b<256;++b)one_case("a"+std::string(1,static_cast<char>(b))+"z",cs);
        for(int b=0;b<128;++b)for(int c=0;c<128;++c)one_case("a"+std::string(1,static_cast<char>(b))+std::string(1,static_cast<char>(c))+"z",cs);
      }
      // Every Unicode scalar appears inside an ASCII alphanumeric envelope, so
      // non-ASCII paths execute the real production lowercasing and are compared.
      for(uint32_t cp=0;cp<=0x10ffff;++cp){if(cp>=0xd800&&cp<=0xdfff)continue;std::string s="a";append_utf8(s,cp);s+='z';one_case(s,utf);}
      std::mt19937_64 rng(0x10bace);
      for(int t=0;t<100000;++t){std::string s="a";int n=rng()%32;for(int i=0;i<n;++i)s+=static_cast<char>(rng());s+='z';one_case(s,t%2?utf:binary);}
      std::cout<<"exact production method differential: "<<comparisons<<" PASS; zero-allocation identity tokens: "<<fast_tokens<<"; normalizer allocations saved: "<<allocations_saved<<"\n";
      durable_copy_test(bin);error_tests(utf);
      if(std::getenv("BENG_SKIP_BENCH"))return 0;
      std::cout<<"HELPER MICROBENCH ONLY: exact get_next + production charset lower, facade value/arena types; no server/integration speed claim\n";
      for(auto kind:{"lowercase","uppercase","nonascii","mixed"}){
        InputStream input;for(int i=0;i<128;++i){std::string s=std::string(kind)=="uppercase"?"GENERALPURPOSE":std::string(kind)=="nonascii"?"a\xc3\x84" "b":"generalpurpose";if(std::string(kind)=="mixed"&&i%4==0)s="GeneralPurpose";s+=std::to_string(i);input.words.push_back(s);}
        BaselineNormalizer a;CandidateNormalizer b;std::vector<double> oldtimes,newtimes;
        for(int r=0;r<7;++r){if(r%2){newtimes.push_back(bench(b,input,utf,20000));oldtimes.push_back(bench(a,input,utf,20000));}else{oldtimes.push_back(bench(a,input,utf,20000));newtimes.push_back(bench(b,input,utf,20000));}}
        std::sort(oldtimes.begin(),oldtimes.end());std::sort(newtimes.begin(),newtimes.end());
        std::cout<<kind<<" baseline_ns/token="<<oldtimes[3]<<" candidate_ns/token="<<newtimes[3]<<" ratio="<<newtimes[3]/oldtimes[3]<<"\n";
      }
      std::cout<<"checksum="<<checksum<<"\n";
    }

    '''
    header=(ROOT/'src/share/text_analysis/ob_token_stream.h').read_text()
    cls=function(header,'class ObBasicEnglishNormalizer final')+';'
    old=subprocess.check_output(['git','show',f'{ARGS.baseline}:src/share/text_analysis/ob_token_stream.cpp'],cwd=ROOT,text=True)
    new=(ROOT/'src/share/text_analysis/ob_token_stream.cpp').read_text()
    charset=(ROOT/'src/oblib/lib/charset/ob_charset.cpp').read_text()
    basecharset=function(charset, 'int ObCharset::tolower(const ObCharsetInfo *cs_info,')
    parser=(ROOT/'src/storage/fts/ob_beng_ft_parser.cpp').read_text()
    parser_method=function(parser,'int ObBEngFTParser::get_next_token(')
    # Tables and UTF-8 conversion methods are included, not copied or reimplemented.
    prologue=r'''
    #include <algorithm>
    #include <array>
    #include <cassert>
    #include <chrono>
    #include <cstdint>
    #include <cstdio>
    #include <cstdlib>
    #include <cstring>
    #include <iostream>
    #include <random>
    #include <string>
    #include <vector>
    #include "lib/charset/ob_ctype_utf8.cc"
    #include "lib/charset/ob_ctype_bin.cc"
    extern "C" void right_to_die_or_duty_to_live_c() { std::abort(); }
    namespace oceanbase { namespace common {
    constexpr int OB_SUCCESS=0, OB_NOT_INIT=-4001, OB_ITER_END=-4002, OB_ERR_UNEXPECTED=-4003;
    constexpr int OB_ALLOCATE_MEMORY_FAILED=-4004, OB_INVALID_ARGUMENT=-4005;
    }}
    using namespace oceanbase::common;
    #undef IS_NOT_INIT
    #define IS_NOT_INIT (!is_inited_)
    #undef LOG_DEBUG
    #define LOG_DEBUG(...)
    #define MEMCPY std::memcpy
    constexpr int OB_MALLOC_NORMAL_BLOCK_SIZE=7936;
    enum ObCollationType { CS_TYPE_UTF8MB4_GENERAL_CI=45, CS_TYPE_UTF8MB4_BIN=46, CS_TYPE_BINARY=63, CS_TYPE_UTF8MB4_UNICODE_CI=224, CS_TYPE_OTHER=999 };
    enum ObCharsetType { CHARSET_INVALID=0, CHARSET_BINARY=1, CHARSET_UTF8MB4=2 };
    struct ObString {
      int32_t len_=0; const char *ptr_=nullptr;
      using obstr_size_t=int32_t;
      ObString()=default; ObString(int32_t n,const char *p):len_(n),ptr_(p){}
      int32_t length()const{return len_;} char* ptr()const{return const_cast<char*>(ptr_);}
      void assign_ptr(char*p,int32_t n){ptr_=p;len_=n;}
    };
    struct ObDatum {
      const char *ptr_=nullptr; uint32_t len_=0;
      void reset(){ptr_=nullptr;len_=0;}
      void set_string(const char*p,uint32_t n){ptr_=p;len_=n;}
      void set_string(const ObString&s){set_string(s.ptr(),s.length());}
    };
    // Facade deliberately counts logical allocator calls; its timing is not the
    // production arena's timing. Bump storage preserves retained parser copies.
    struct ObArenaAllocator {
      std::vector<char> storage=std::vector<char>(1<<20); size_t used=0,calls=0,reuses=0;
      bool fail=false;
      ObArenaAllocator(const char * = "", int = 0){}
      void*alloc(size_t n){++calls;if(fail)return nullptr;assert(used+n<=storage.size());char*p=storage.data()+used;used+=n;return p;}
      void reuse(){used=0;++reuses;}
      void reset(){reuse();}
    };
    int ob_write_string(ObArenaAllocator&arena,const ObString&src,ObString&dst){
      char*p=static_cast<char*>(arena.alloc(src.length()));if(!p)return OB_ALLOCATE_MEMORY_FAILED;
      std::memcpy(p,src.ptr(),src.length());dst.assign_ptr(p,src.length());return OB_SUCCESS;
    }
    struct ObCharset {
      static ObCharsetType charset_type_by_coll(ObCollationType c){
        switch(c){case CS_TYPE_UTF8MB4_GENERAL_CI:case CS_TYPE_UTF8MB4_BIN:case CS_TYPE_UTF8MB4_UNICODE_CI:return CHARSET_UTF8MB4;
        case CS_TYPE_BINARY:return CHARSET_BINARY;default:return CHARSET_INVALID;}
      }
      static int tolower(const ObCharsetInfo*,const ObString&,ObString&,ObArenaAllocator&);
    };
    struct ObITokenStream { virtual ~ObITokenStream()=default; virtual int get_next(ObDatum&,int64_t&)=0; virtual void reuse(){}; };
    struct InputStream : ObITokenStream {
      std::vector<std::string> words; size_t index=0;
      int get_next(ObDatum&d,int64_t&freq)override{if(index==words.size())return OB_ITER_END;
        auto&w=words[index++];d.set_string(w.data(),w.size());freq=1;return OB_SUCCESS;}
    };
    struct ObTokenNormalizer : ObITokenStream {
      ObITokenStream*in_stream_=nullptr; const ObCharsetInfo*cs_=nullptr; bool is_inited_=false;
      virtual void reset(){in_stream_=nullptr;cs_=nullptr;is_inited_=false;}
      void reuse()override{if(in_stream_)in_stream_->reuse();}
    };
    '''
    basecharset=basecharset.replace('ObIAllocator &allocator','ObArenaAllocator &allocator')
    content=prologue+basecharset+'\n'
    for name, source in [('BaselineNormalizer',old),('CandidateNormalizer',new)]:
        content+=cls.replace('ObBasicEnglishNormalizer',name).replace('private:','public:')+'\n'
        for marker in ['ObBasicEnglishNormalizer::ObBasicEnglishNormalizer()', 'void ObBasicEnglishNormalizer::reset()', 'void ObBasicEnglishNormalizer::reuse()', 'int ObBasicEnglishNormalizer::get_next(']:
            content+=function(source,marker).replace('ObBasicEnglishNormalizer',name)+'\n'
    for name in ['BaselineParser','CandidateParser']:
        content+=f'''struct {name} {{
      bool is_inited_=true; ObITokenStream *token_stream_; ObArenaAllocator allocator_;
      explicit {name}(ObITokenStream*t):token_stream_(t){{}}
      int get_next_token(const char*&,int64_t&,int64_t&,int64_t&);
    }};\n'''
        content+=parser_method.replace('ObBEngFTParser',name)+'\n'
    content+=HARNESS_MAIN
    (HERE/'generated.cpp').write_text(content)
    flags = ["-O2"]
    if ARGS.sanitize:
        # Full charset translation units define unrelated global handler tables.
        # Disabling global registration lets section GC discard their unavailable
        # link dependencies, without replacing or stubbing a charset algorithm.
        flags = ["-O1", "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "--param", "asan-globals=0"]
    subprocess.run(["g++", "-std=c++17", *flags, "-g", "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections", "-I", str(ROOT / "src/oblib"), str(HERE / "generated.cpp"), "-o", str(HERE / "beng_diff")], check=True)
    env = os.environ.copy()
    if not ARGS.benchmark:
        env["BENG_SKIP_BENCH"] = "1"
    if ARGS.sanitize:
        env["ASAN_OPTIONS"] = "detect_leaks=0"
        print("ASan/UBSan enabled; leak detection and global registration disabled.", flush=True)
    print("Native facade test only: exact selected C++ method bodies and production charset algorithms/tables; not a full-header, server, SQL or production allocator build.", flush=True)
    subprocess.run([str(HERE / "beng_diff")], env=env, check=True)


class BengNoopCasefoldContract(unittest.TestCase):
    def test_exact_methods_and_production_charset_algorithms(self):
        result = subprocess.run([sys.executable, '-B', str(pathlib.Path(__file__).resolve())],
            capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('exact production method differential: 1295339 PASS', result.stdout)
        self.assertIn('parser durable copies: 7 retained tokens PASS', result.stdout)
        self.assertIn('fallback allocation failures/uninitialized states: PASS', result.stdout)
        print(result.stdout.strip())


if __name__ == "__main__":
    main()
