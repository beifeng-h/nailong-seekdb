#!/usr/bin/env python3
"""One native FTS expression-cache validation; b147 reuse, never formal scoring.

Historical drivers/fixtures keep their identities. This independent entry point
uses their unchanged process, original-case, inventory and strict-plan helpers.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import traceback
import zipfile

import fts_contest_validation as native_harness
import fts_paired_validation as common
from fts_paired_collect import archive, add_bytes, BINARY_ARCHIVE_CAP, LOG_ARCHIVE_CAP
from fts_plan_compatibility import classify_plan_metadata_compatibility

BASE = 'b14755bed10d1399cf7d1dca6444cfa757727b21'
BASE_TREE = 'e7ef67d5fc1b15cbdd15f893fe3ff19c476a98bd'
KERNEL = 'afa3e3540c5830555d59bf7fe871bf361536a441'
KERNEL_TREE = '2933c65f932a10beb1b509fab2e36587d611a287'
PATCH_SHA = '12b654df178f5d13bfb22507ccc35ab7957384bb295712c9f15e0d75ad8ec55e'
CANONICAL_PATCH_SHA = '9e2998fedde18d9fa4a5bc04572f3ba4d0bfb1bae8a0267f22738964e3c9c863'
CANONICAL_DIFF_FLAGS = ('--no-color','--no-ext-diff','--no-textconv','--no-renames',
    '--src-prefix=a/','--dst-prefix=b/','--diff-algorithm=myers','--unified=3',
    '--indent-heuristic','--full-index')
INVENTORY = 'f36e2d1c8aaabd3e9ffc4f8e3635defca6dc3046e3d575e6e73165fa8d80a135'
BASE_ZIP_SHA = '02799ccf9fe5ff358cb6cb2403442ce1f3626dce80d211044636256c94140e96'
BASE_TAR_SHA = '964858da1ad7bcc70fbfff20a9ac2df8ea758512a123c6f7d492d73138fcddf0'
BASE_ELF_SHA = 'fa2be2a166c042421080e142adbb98b83f277be6f77b2a47ce63dc69f0f6db03'
BASE_BUILD_ID = '983ae843c7765c615f6a3ba870bdbd65b5d86c74'
LOG_PAYLOAD_CAP = 56*1024**2
LOG_FILE_CAP = 256*1024
SERVER_LOG_ARCHIVE_CAP = 96*1024**2
SERVER_LOG_RAW_CAP = 2*1024**3
SERVER_LOG_TOTAL_ARCHIVE_CAP = 4*SERVER_LOG_ARCHIVE_CAP
DATA_SHA = 'bc385b3c9586d40d667093947be20a8714ed65301eae68487af8fac22956d235'
USER_IDENTITY = b'fcoox1111-rgb <288271307+fcoox1111-rgb@users.noreply.github.com>'
PRODUCT = frozenset(('src/sql/engine/table/ob_table_scan_op.cpp', 'src/sql/engine/table/ob_table_scan_op.h'))
PREFIX = '.github/script/seekdb/'
ALLOWED_CI = frozenset((PREFIX+'fts_expr_cache_validation.py', PREFIX+'fts_expr_cache_targeted.py',
    PREFIX+'test_fts_expr_cache_validation.py', '.github/workflows/fts-expr-cache-validation.yml',
    '.github/workflows/fts-contest-validation.yml'))
PROTECTED = ('build.sh', 'CMakeLists.txt', 'cmake', 'rust', 'deps',
    '.github/actions/setup-rust', 'tools/cmake', 'tools/benchmark', 'tools/deploy')
CASES = common.CASES + common.REGRESSIONS + (
    'fts_index.tokenize_beng_noop_casefold_contract',
    'fts_index.tokenize_last_word_representative_contract')
SCHEDULE = (('A1', 'baseline'), ('B1', 'candidate'), ('B2', 'candidate'), ('A2', 'baseline'))
DDL = (
    ('build_ik_all', 'ALTER TABLE docs ADD FULLTEXT INDEX fti_all_ik(title, content) WITH PARSER ik;'),
    ('build_ik_content', 'ALTER TABLE docs ADD FULLTEXT INDEX fti_content_ik(content) WITH PARSER ik;'),
    ('build_beng_en', 'ALTER TABLE docs ADD FULLTEXT INDEX fti_en_beng(title_en, content_en) WITH PARSER beng;'))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def dump(path, obj):
    common.write_json(path, obj)


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def inventory(source):
    hashes = native_harness.original_inputs(source)
    require(len(hashes) == 75 and sha(json.dumps(hashes, sort_keys=True,
        separators=(',', ':')).encode()) == INVENTORY, 'Original 75-input inventory mismatch')
    return hashes


def git_identity(source, commit):
    raw = common.git(source, 'cat-file', '-p', commit)
    for field in (b'author', b'committer'):
        rows = [line for line in raw.splitlines() if line.startswith(field+b' ')]
        require(len(rows) == 1 and rows[0].startswith(field+b' '+USER_IDENTITY+b' '),
                'Unverified user Git '+field.decode()+' for '+commit)
    return raw.decode('utf-8')


def canonical_patch(source):
    # Full object IDs and explicit rendering avoid repository-size/config defaults.
    return common.git(source,'diff',*CANONICAL_DIFF_FLAGS,BASE,KERNEL,'--')


def source_proof(args):
    source, driver = args.source, args.driver_sha
    require(re.fullmatch('[0-9a-f]{40}', driver) is not None, 'Full driver SHA required')
    identity = common.source_identity(source, driver)
    require(common.git(source, 'rev-parse', BASE+'^{tree}').decode().strip() == BASE_TREE, 'Baseline tree mismatch')
    require(common.git(source, 'rev-parse', KERNEL+'^{tree}').decode().strip() == KERNEL_TREE, 'Kernel tree mismatch')
    changed = lambda a, b: set(common.git(source, 'diff', '--name-only', a, b, '--').decode().splitlines())
    require(changed(BASE, KERNEL) == PRODUCT, 'Kernel must change exactly two approved product files')
    require(sha(canonical_patch(source)) == CANONICAL_PATCH_SHA, 'Canonical kernel patch byte mismatch')
    ci = changed(KERNEL, driver)
    require(ci <= ALLOWED_CI, 'Unexpected CI changes: '+repr(sorted(ci-ALLOWED_CI)))
    require(not common.git(source, 'diff', '--name-only', BASE, driver, '--', *PROTECTED).strip(),
            'Original benchmark/native inputs/build/dependency/Rust files changed')
    require(not common.git(source, 'diff', '--name-only', KERNEL, driver, '--', 'src').strip(),
            'CI driver changes production source')
    topics={KERNEL:'perf(fts): cache operator-local expression resolution',
        driver:'ci(fts): normalize source proof Git diff [dedicated-native-validation]'}
    for commit,title in topics.items():
        require(common.git(source,'show','-s','--format=%s',commit).decode().strip()==title,
            'New publication topic differs from reviewed neutral title')
    branch=os.environ.get('GITHUB_REF_NAME')
    if branch is None:
        branch=common.git(source,'symbolic-ref','--short','HEAD').decode().strip()
    require(branch=='perf/fts-expr-cache-validation-20261110','Wrong isolated validation branch')
    hashes = inventory(source)
    case_inputs = {}
    for name in CASES:
        suite, case = name.split('.')
        p = source / 'tools/deploy/mysql_test/test_suite' / suite
        case_inputs[name] = dict(testSha256=common.digest(p/'t'/(case+'.test')),
                                expectedSha256=common.digest(p/'r/mysql'/(case+'.result')))
        for path in ('t/'+case+'.test', 'r/mysql/'+case+'.result'):
            rel = (p/path).relative_to(source).as_posix()
            require(common.git(source, 'show', BASE+':'+rel) == (p/path).read_bytes(), 'Selected native input changed')
    return dict(baselineCommit=BASE, baselineTree=BASE_TREE, kernelCommit=KERNEL, kernelTree=KERNEL_TREE,
        driverCommit=driver, driverTree=identity['tree'], productChanges=sorted(PRODUCT),
        ciChanges=sorted(ci), patchSha256=PATCH_SHA, canonicalFullIndexPatchSha256=CANONICAL_PATCH_SHA,
        canonicalDiffOptions=list(CANONICAL_DIFF_FLAGS), originalInputCount=75,
        originalInputInventorySha256=INVENTORY, originalInputHashes=hashes, caseInputs=case_inputs,
        commitObjects={KERNEL:git_identity(source,KERNEL),driver:git_identity(source,driver)})


def resource_record(path, disk, memory=6*1024**3):
    mem = dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())
    available = int(mem['MemAvailable'].split()[0])*1024
    fs = os.statvfs(path.parent)
    free = fs.f_bavail*fs.f_frsize
    cgroup = {}
    for name in ('cpu.max','memory.max','memory.current','cpu.stat'):
        p = Path('/sys/fs/cgroup') / name
        if p.is_file():
            cgroup[name] = p.read_text().strip()
    effective = available
    if cgroup.get('memory.max','max') != 'max':
        effective = min(effective,int(cgroup['memory.max'])-int(cgroup.get('memory.current','0')))
    record = dict(memoryAvailableBytes=available,effectiveAvailableBytes=effective,freeDiskBytes=free,
        requiredDiskBytes=disk,requiredMemoryBytes=memory,cpuCount=os.cpu_count(),
        affinity=sorted(os.sched_getaffinity(0)),cgroup=cgroup,
        uname=list(os.uname()),monotonicNs=time.monotonic_ns())
    dump(path,record)
    require(free >= disk and effective >= memory and len(record['affinity']) >= 2,
            'Resource admission failed; refusing smaller database limits')
    return record


def safe_elf(path, digest, size, build_id, commit):
    common.no_symlinks(path)
    st = path.stat()
    require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_size == size
        and 0 < size <= 4*1024**3, 'Full unique regular ELF required')
    with path.open('rb') as stream: magic=stream.read(4)
    require(common.digest(path)==digest and magic==b'\x7fELF', 'ELF SHA/bytes mismatch')
    require(common.contains_bytes(path,commit.encode()), 'Actual full source commit not embedded in ELF')
    notes = subprocess.check_output(['readelf','-n',str(path)],timeout=30)
    require(re.findall(rb'Build ID:\s*([0-9a-f]+)',notes)==[build_id.encode()], 'Actual ELF BuildID mismatch')


def prepare(args):
    root = args.root
    common.no_symlinks(root)
    require(not root.exists() and args.source != root and args.source not in root.parents
        and root not in args.source.parents, 'Fresh disjoint validation root required')
    proof = source_proof(args)
    require(args.artifact_zip is not None and common.digest(args.artifact_zip)==BASE_ZIP_SHA
        and args.artifact_zip.stat().st_size==328426886, 'Immutable b147 ZIP identity mismatch')
    root.mkdir()
    for name in ('runtime/baseline','runtime/candidate','evidence/baseline','evidence/candidate','validation','sources'):
        (root/name).mkdir(parents=True)
    resource_record(root/'evidence/resources-before.json',16*1024**3)
    common.git(args.source,'worktree','add','--detach',str(root/'sources/baseline'),BASE)
    with zipfile.ZipFile(args.artifact_zip) as z:
        names = z.namelist()
        require(len(names)==len(set(names)) and len(names)<5000, 'ZIP duplicate/unbounded members')
        def member(suffix, cap):
            found=[n for n in names if n==suffix or n.endswith('/'+suffix)]
            require(len(found)==1,'Exact immutable artifact member unavailable: '+suffix)
            info=z.getinfo(found[0])
            require(info.file_size<=cap and not stat.S_ISLNK(info.external_attr>>16), 'Oversize/link artifact member')
            return z.read(found[0])
        identity_raw=member('candidate-build/candidate-identity.json',2*1024**2)
        original=json.loads(identity_raw)
        require(original['sourceCommit']==BASE and original['sourceTree']==BASE_TREE
            and original['sha256']==BASE_ELF_SHA and original['buildId']==BASE_BUILD_ID
            and original['bytes']==1332362136 and original['officialInputHashes']==proof['originalInputHashes'],
            'Archived candidate is not the new b147 baseline')
        tar_bytes=member('candidate-build/candidate-elf.tar.gz',416*1024**2)
        require(len(tar_bytes)==329567024 and sha(tar_bytes)==BASE_TAR_SHA, 'Immutable b147 tar identity mismatch')
    baseline=root/'runtime/baseline/seekdb'
    with tarfile.open(fileobj=io.BytesIO(tar_bytes),mode='r:gz') as tar:
        members=tar.getmembers()
        require(len(members)==1 and members[0].name=='candidate/seekdb' and members[0].isreg()
            and members[0].size==1332362136, 'b147 tar exact one-member contract mismatch')
        with tar.extractfile(members[0]) as src, baseline.open('xb') as out:
            shutil.copyfileobj(src,out)
    baseline.chmod(0o555)
    safe_elf(baseline,BASE_ELF_SHA,1332362136,BASE_BUILD_ID,BASE)
    (root/'evidence/baseline/archived-identity.json').write_bytes(identity_raw)
    dump(root/'runtime/baseline/identity.json',dict(original,role='baseline',binary=str(baseline),
        archiveRole='candidate',archiveRun=42797329842,archiveId=13757273442,archiveZipSha256=BASE_ZIP_SHA))
    plan=dict(scope='NATIVE_DEVELOPMENT_VALIDATION_NOT_FORMAL_CONTEST',proof=proof,
        baselineCommit=BASE,baselineRebuilt=False,candidateBuildsRequired=1,formalContestRun=False,
        historicalResultRelabeled=False,schedule=[dict(name=n,role=r) for n,r in SCHEDULE],
        performance='descriptive two-samples-per-role, DDL wall spans include all sort/write/checks',
        benchmarkInput=dict(rows=20000,batch=500,seed=42,sha256=DATA_SHA),
        parameters=native_harness.PARAMETERS,baselineArtifact=dict(run=42797329842,id=13757273442,
            zipSha256=BASE_ZIP_SHA,tarSha256=BASE_TAR_SHA,elfSha256=BASE_ELF_SHA))
    dump(root/'plan.json',plan)
    report=dict(status='prepared',formalContestRun=False,baselineRebuilt=False,instances=[],
        nativeCases=[],targeted={},rounds=[],plan=plan)
    dump(root/'report.json',report)
    return plan


def load_plan(args):
    plan=json.loads((args.root/'plan.json').read_text())
    require(plan['proof']==source_proof(args),'Source/kernel/input proof changed')
    return plan


def build_profile(args):
    settings={}
    for line in (args.source/'build_release/CMakeCache.txt').read_text().splitlines():
        m=re.fullmatch(r'([^#/:][^:]*):[^=]+=(.*)',line)
        if m: settings[m[1]]=m[2]
    expected=dict(CMAKE_BUILD_TYPE='RelWithDebInfo',OB_ENABLE_UNITY='ON',OB_USE_LLD='ON')
    require(all(settings.get(k)==v for k,v in expected.items()),'Original CMake settings changed')
    for key in ('CMAKE_C_COMPILER','CMAKE_CXX_COMPILER'):
        text=subprocess.check_output([settings[key],'--version'],timeout=30).decode()
        require('clang version 17.0.6' in text,'Original Clang 17.0.6 required')
        (args.root/'evidence/candidate'/(key+'-version.log')).write_text(text)
    rust=subprocess.check_output(['rustc','--version'],cwd=args.source/'rust',timeout=30).decode().strip()
    require(rust.startswith('rustc 1.98.1 '),'Original Rust 1.98.1 required')
    return expected,rust


def verify_version(args,binary,out,commit):
    result=common.capture_group([str(binary),'--version'],out,args.source,30,env=common.runtime_env(args.source,binary))
    require(result['exitCode']==0 and result['childrenCleaned'] and commit in out.read_text(),
        'Actual --version does not identify exact role commit')


def prebuild(args):
    load_plan(args)
    require(not (args.source/'build_release/src/observer/seekdb').exists(), 'Candidate already built before one-build gate')
    resource_record(args.root/'evidence/prebuild-resources.json',16*1024**3)
    build_profile(args)
    for name,pin in common.CLIENTS.items():
        path=args.source/'deps/3rd/u01/obclient/bin'/name
        require(common.digest(path)==pin,'Original SQL client identity mismatch')
        common.verify_loader(path,args.root/'evidence/candidate'/(name+'-prebuild-ldd.log'),args.source,
                             common.runtime_env(args.source,path))
    binary=args.root/'runtime/baseline/seekdb'
    common.verify_loader(binary,args.root/'evidence/baseline/prebuild-ldd.log',args.source,
                         common.runtime_env(args.source,binary))
    verify_version(args,binary,args.root/'evidence/baseline/version.log',BASE)
    import fts_expr_cache_targeted as targeted
    report=json.loads((args.root/'report.json').read_text())
    require('baseline' not in report['targeted'],'Baseline SQL preflight must execute once, not be retried silently')
    identity=json.loads((args.root/'runtime/baseline/identity.json').read_text())
    row=run_owned(args,report,{'baseline':identity},'baseline','targeted-baseline',
        lambda call,out,pid:targeted.run(call,out,args,sql))
    common.require_instance(row)
    report['targeted']['baseline']=row['work']
    canonical=sha(json.dumps(row['work']['canonical'],sort_keys=True,separators=(',',':')).encode())
    report['baselineTargetedPreflight']=dict(driverCommit=args.driver_sha,baselineCommit=BASE,
        baselineElfSha256=BASE_ELF_SHA,helperSha256=common.digest(args.source/PREFIX/'fts_expr_cache_targeted.py'),
        canonicalSha256=canonical,resultSha256=common.digest(args.root/'validation/targeted-baseline/result.json'),
        executedBeforeCandidateCompile=True,rawStatus=row['status'],cleanupPassed=row['cleanupPassed'])
    dump(args.root/'report.json',report)
    dump(args.root/'evidence/prebuild.json',dict(passed=True,driverCommit=args.driver_sha,
        baselineSha256=BASE_ELF_SHA,baselineTargetedPreflight=report['baselineTargetedPreflight']))
    print('EXPR_CACHE_BASELINE_SQL_PREFLIGHT '+json.dumps(report['baselineTargetedPreflight']),flush=True)


def check_baseline_targeted(args,report):
    receipt=report.get('baselineTargetedPreflight',{})
    expected=dict(driverCommit=args.driver_sha,baselineCommit=BASE,baselineElfSha256=BASE_ELF_SHA,
        helperSha256=common.digest(args.source/PREFIX/'fts_expr_cache_targeted.py'),
        canonicalSha256=sha(json.dumps(report.get('targeted',{}).get('baseline',{}).get('canonical'),
            sort_keys=True,separators=(',',':')).encode()),
        resultSha256=common.digest(args.root/'validation/targeted-baseline/result.json'),
        executedBeforeCandidateCompile=True,rawStatus='passed',cleanupPassed=True)
    require(receipt==expected,'Baseline prebuild tiny SQL receipt changed or failed')
    row=json.loads((args.root/'validation/targeted-baseline/result.json').read_text())
    require(row['work']==report['targeted']['baseline'] and row['status']=='passed'
        and row['cleanupPassed'] and not (args.root/'validation/targeted-baseline/database').exists(),
        'Saved baseline native SQL result or cleanup changed')
    prebuild=json.loads((args.root/'evidence/prebuild.json').read_text())
    require(prebuild['baselineTargetedPreflight']==receipt,'Prebuild baseline SQL identity binding changed')
    return receipt


def snapshot(args):
    plan=load_plan(args)
    require(json.loads((args.root/'evidence/prebuild.json').read_text())['passed'], 'Missing prebuild gate')
    check_baseline_targeted(args,json.loads((args.root/'report.json').read_text()))
    expected,rust=build_profile(args)
    version=args.source/'build_release/src/observer/ob_version.cpp'
    require(args.driver_sha in version.read_text(),'Generated version is not actual driver commit')
    built=args.source/'build_release/src/observer/seekdb'
    common.no_symlinks(built)
    dest=args.root/'runtime/candidate/seekdb'
    require(not dest.exists() and built.is_file() and built.stat().st_nlink==1,'New complete unique candidate required')
    shutil.copyfile(built,dest)
    dest.chmod(0o555)
    notes=subprocess.check_output(['readelf','-n',str(dest)],timeout=30)
    build_ids=re.findall(rb'Build ID:\s*([0-9a-f]+)',notes)
    require(len(build_ids)==1,'Candidate single BuildID required')
    record=dict(role='candidate',sourceCommit=args.driver_sha,sourceTree=plan['proof']['driverTree'],
        kernelOrigin=KERNEL,kernelTree=KERNEL_TREE,binary=str(dest),bytes=dest.stat().st_size,
        sha256=common.digest(dest),buildId=build_ids[0].decode(),buildSettings=expected,rustVersion=rust,
        officialInputHashes=plan['proof']['originalInputHashes'],generatedVersionSha256=common.digest(version))
    safe_elf(dest,record['sha256'],record['bytes'],record['buildId'],args.driver_sha)
    common.verify_loader(dest,args.root/'evidence/candidate/seekdb-ldd.log',args.source,common.runtime_env(args.source,dest))
    verify_version(args,dest,args.root/'evidence/candidate/version.log',args.driver_sha)
    dump(args.root/'runtime/candidate/identity.json',record)
    print('EXPR_CACHE_CANDIDATE_IDENTITY '+json.dumps(record),flush=True)
    return record


def preserve(args):
    load_plan(args)
    identity=json.loads((args.root/'runtime/candidate/identity.json').read_text())
    binary=Path(identity['binary'])
    safe_elf(binary,identity['sha256'],identity['bytes'],identity['buildId'],args.driver_sha)
    require(not args.out.exists(),'Fresh candidate artifact output required')
    args.out.mkdir(parents=True)
    def populate(tar):
        info=tarfile.TarInfo('candidate/seekdb')
        info.size=identity['bytes']; info.mode=0o555
        with binary.open('rb') as f: tar.addfile(info,f)
    row=archive(args.out/'candidate-elf.tar.gz',BINARY_ARCHIVE_CAP,populate)
    require(common.digest(binary)==identity['sha256'],'Candidate changed during archival')
    shutil.copyfile(args.root/'runtime/candidate/identity.json',args.out/'candidate-identity.json')
    dump(args.out/'candidate-artifact-manifest.json',dict(archive=row,identity=identity,
        fullBytes=True,stripped=False,truncated=False,formalContestRun=False,baselineReuploaded=False))
    print('EXPR_CACHE_PRESERVED '+json.dumps(row),flush=True)


def sql(call,out,label,query):
    require(re.fullmatch('[A-Za-z0-9_.-]+',label) is not None,'Unsafe SQL evidence label')
    text=common.SESSION+query+'\n'
    (out/(label+'.sql')).write_text(text)
    p=out/(label+'.log')
    result=common.capture_group([str(call.obclient),'-h127.0.0.1','-P'+str(call.port),'-uroot','-A','-N','-B',
        '--raw','--default-character-set=utf8mb4','-Doceanbase'],p,Path.cwd(),900,
        env=os.environ.copy(),stdin=text.encode())
    require(result['exitCode']==0 and result['childrenCleaned'], 'Native SQL failed: '+label)
    return p.read_text(encoding='utf-8',errors='strict')


def cpu_ticks(pid):
    line=(Path('/proc')/str(pid)/'stat').read_text()
    fields=line[line.rindex(')')+2:].split()
    return int(fields[11])+int(fields[12])


def build_work(args,pid):
    def work(call,out):
        resource_record(out/'resources-admitted.json',8*1024**3)
        setup=(args.source/'tools/benchmark/fts_large_bench_setup.sql').read_text()
        sql(call,out,'setup',setup)
        data=out/'generated-input.sql'
        with data.open('xb') as stream:
            subprocess.run([sys.executable,str(args.source/'tools/benchmark/fts_large_bench_gen.py'),
                '--rows','20000','--batch','500','--seed','42'],stdout=stream,check=True,timeout=60)
        require(common.digest(data)==DATA_SHA,'Original 20K generator bytes differ')
        start=time.perf_counter()
        sql(call,out,'load',data.read_text())
        raw=time.perf_counter()-start
        require(sql(call,out,'rows','SELECT COUNT(*) FROM fts_large_bench.docs;').strip()=='20000','Row-count mismatch')
        timings={}; cpu={}
        total=time.perf_counter()
        for label,ddl in DDL:
            before=cpu_ticks(pid)
            start=time.perf_counter()
            sql(call,out,label,'USE fts_large_bench; '+ddl)
            timings[label+'_sec']=time.perf_counter()-start
            after=cpu_ticks(pid)
            require(after>before,'No observed owned server CPU progress for DDL')
            cpu[label]=dict(beforeTicks=before,afterTicks=after,hz=os.sysconf('SC_CLK_TCK'),
                cpuSeconds=(after-before)/os.sysconf('SC_CLK_TCK'))
        timings['build_total_sec']=time.perf_counter()-total
        timings['raw_load_sec']=raw
        queries=common.queries_from_benchmark(args.source/'tools/benchmark/fts_large_bench.sh')
        counts={name:int(sql(call,out,name,q).strip()) for name,q in queries.items()}
        require(counts==dict(query_cn=8001,query_beng=11000,query_mixed=7332,query_limit=20),'Original query counts mismatch')
        plans={}
        for name,q in common.plan_queries(queries).items():
            text=sql(call,out,name+'-explain','EXPLAIN '+q)
            parsed=common.plan_relevance(text)
            expected=name=='query_limit' or name in common.PLAN_CONTROLS
            require(parsed['actualCalcRelevance']==expected,'Role-consistent query scoring plan mismatch: '+name)
            plans[name]=dict(expectedCalcRelevance=expected,**parsed)
        resource_record(out/'resources-after.json',8*1024**3)
        return dict(passed=True,wallSeconds=timings,serverCpu=cpu,queryCounts=counts,plans=plans,
            inputSha256=DATA_SHA,executedLoadSqlSha256=common.digest(out/'load.sql'),rows=20000,batch=500,seed=42,
            timingScope='Whole ALTER wall spans include segmentation, sort, writes and checks; not pure cache lookup time')
    return work



def process_state(pid):
    try:
        raw=(Path('/proc')/str(pid)/'stat').read_text()
    except FileNotFoundError:
        return None
    fields=raw[raw.rindex(')')+2:].split()
    return dict(state=fields[0],startTicks=int(fields[19]))


def capture_full_server_logs(base,out,role,name):
    require(name in dict(SCHEDULE) and dict(SCHEDULE)[name]==role,'Full server logs must bind one ABBA role')
    require(base==out/'database' and base.is_dir(),'Capture must precede owned database destruction')
    stop=json.loads((out/'own-stop.log.json').read_text())
    require(stop['exitCode']==0,'Owned server must stop successfully before full log capture')
    owned=json.loads((out/'owned-process.json').read_text())
    state=process_state(owned['pid'])
    require(state is None or state['startTicks']!=owned['startTicks'] or state['state']=='Z',
        'Original owned process is still live; refusing non-final log bytes')
    logdir=base/'log'
    common.no_symlinks(logdir)
    require(logdir.is_dir(),'Owned raw server log directory missing')
    files=[]; total=0
    for path in sorted(logdir.iterdir()):
        # Only server-owned stdout/stderr and known server log families, never configs or datafiles.
        if path.name!='console.log' and re.fullmatch(r'seekdb\.log(?:\.wf)?(?:\.[0-9]+)?(?:\.wf)?',path.name) is None:
            continue
        common.no_symlinks(path)
        st=path.stat()
        require(stat.S_ISREG(st.st_mode) and st.st_nlink==1,'Raw server log must be a unique regular file')
        total+=st.st_size
        require(len(files)<64 and total<=SERVER_LOG_RAW_CAP,'Raw server log count/byte cap exceeded; no truncation allowed')
        files.append((path,dict(path=path.relative_to(base).as_posix(),bytes=st.st_size,
            sha256=common.digest(path),mtimeNs=st.st_mtime_ns,fullBytes=True,truncated=False)))
    names={p.name for p,_ in files}
    require({'console.log','seekdb.log'}<=names,'At least complete main log and original server console required per round')
    destination=out/'full-server-logs.tar.gz'
    def populate(tar):
        for path,record in files:
            info=tarfile.TarInfo(record['path']); info.size=record['bytes']; info.mode=0o644
            with path.open('rb') as stream: tar.addfile(info,stream)
            current=path.stat()
            require(current.st_size==record['bytes'] and current.st_mtime_ns==record['mtimeNs']
                and common.digest(path)==record['sha256'],'Stopped raw server log changed during capture')
    record=archive(destination,SERVER_LOG_ARCHIVE_CAP,populate)
    receipt=dict(name=name,role=role,archive=record,fileCount=len(files),rawBytes=total,
        captureMoment='after_owned_stop_before_database_destroy',ownedPid=owned['pid'],
        ownedStartTicks=owned['startTicks'],postStopProcessState=state,stopExitCode=0,
        files=[r for _,r in files],fullBytes=True,truncated=False,
        rawCapBytes=SERVER_LOG_RAW_CAP,archiveCapBytes=SERVER_LOG_ARCHIVE_CAP)
    dump(out/'full-server-logs.json',receipt)
    return receipt


def run_owned(args,report,identities,role,name,work,initialize=False,native=False):
    source_proof(args)
    source=args.root/'sources/baseline' if role=='baseline' else args.source
    expected=BASE if role=='baseline' else args.driver_sha
    common.source_identity(source,expected)
    b=Path(identities[role]['binary'])
    role_args=argparse.Namespace(source=source,binary=b,
        obclient=args.source/'deps/3rd/u01/obclient/bin/obclient',
        mysqltest=args.source/'deps/3rd/u01/obclient/bin/mysqltest',out=args.root/'validation')
    old={key:os.environ.get(key) for key in ('PATH','LD_LIBRARY_PATH')}
    env=common.runtime_env(args.source,b)
    os.environ.update({key:env[key] for key in old})
    def checked(call,out):
        observed=common.effective_parameters(out/'actual-parameters.log')
        if 'observedParameters' in report: require(observed==report['observedParameters'],'Actual database settings differ')
        report['observedParameters']=observed
        sdb=common.load_module('expr_cache_owned_sdb',source/PREFIX/'sdb.py')
        pid=sdb.inspect_instance_process(call.base_dir,b)
        require(pid and (Path('/proc')/str(pid)/'exe').resolve(strict=True)==b,'Owned process role ELF mismatch')
        state=process_state(pid)
        require(state is not None and state['state']!='Z','Live owned process state required')
        dump(out/'owned-process.json',dict(pid=pid,startTicks=state['startTicks'],role=role,binary=str(b),sha256=identities[role]['sha256']))
        resource_record(out/'resources-before.json',8*1024**3)
        return work(call,out,pid)
    original_log_capture=native_harness.bounded_db_logs
    if name in dict(SCHEDULE):
        def full_capture(base,out):
            tails=original_log_capture(base,out)
            return dict(tails,fullServerLogs=capture_full_server_logs(base,out,role,name))
        native_harness.bounded_db_logs=full_capture
    try:
        row=native_harness.run_instance(role_args,native_harness.load_native(source),name,
            7500+len(report['instances']),checked,initialize=initialize)
    finally:
        native_harness.bounded_db_logs=original_log_capture
        for key,value in old.items():
            if value is None: os.environ.pop(key,None)
            else: os.environ[key]=value
    row['role']=role
    report['instances'].append(row)
    dump(args.root/'report.json',report)
    common.source_identity(source,expected)
    source_proof(args)
    return row

def validate(args):
    plan=load_plan(args)
    report=json.loads((args.root/'report.json').read_text())
    report['status']='running'
    identities={r:json.loads((args.root/'runtime'/r/'identity.json').read_text()) for r in ('baseline','candidate')}
    for r,i in identities.items():
        safe_elf(Path(i['binary']),i['sha256'],i['bytes'],i['buildId'],BASE if r=='baseline' else args.driver_sha)
    report['builds']=identities
    dump(args.root/'report.json',report)
    native=native_harness.load_native(args.source)
    available={c.name:c for c in native.discover_cases(args.source)}
    case_args=argparse.Namespace(source=args.source)
    for case in CASES:
        require(case in available,'Selected unchanged native case unavailable: '+case)
        inner=native_harness.original_case(case_args,native,available[case])
        row=run_owned(args,report,identities,'candidate',case,lambda call,out,pid,inner=inner:inner(call,out),initialize=True,native=True)
        proof=None
        if case=='fts_index.simple_query':
            # Preserve FAIL and require complete-byte proof of only four known flags.
            proof=classify_plan_metadata_compatibility(source=args.source,out=args.root/'validation'/case,
                role='candidate',case_name=case,row=row)
            dump(args.root/'validation'/case/'supplemental-plan-compatibility.json',proof)
            require(row['status']=='failed' and proof['accepted'] and proof['approvedDifferenceCount']==4
                and proof['completeBytesCompared'] and row['cleanupPassed'], 'Raw known FAIL differs outside four exact approved annotations')
        else:
            common.require_instance(row)
        report['nativeCases'].append(dict(name=case,rawNativeStatus=row['status'],
            cleanupPassed=row['cleanupPassed'],compatibility=proof))
        dump(args.root/'report.json',report)
    import fts_expr_cache_targeted as targeted
    check_baseline_targeted(args,report)
    row=run_owned(args,report,identities,'candidate','targeted-candidate',
        lambda call,out,pid:targeted.run(call,out,args,sql))
    common.require_instance(row)
    report['targeted']['candidate']=row['work']
    dump(args.root/'report.json',report)
    require(report['targeted']['baseline']['canonical']==report['targeted']['candidate']['canonical'],'New targeted b147/candidate canonical parity failed')
    report['correctnessPassed']=True
    dump(args.root/'report.json',report)
    for name,role in SCHEDULE:
        row=run_owned(args,report,identities,role,name,lambda call,out,pid:build_work(args,pid)(call,out))
        common.require_instance(row)
        report['rounds'].append(dict(name=name,role=role,**row['work']))
        dump(args.root/'report.json',report)
    full_logs=[r.get('dbLogs',{}).get('fullServerLogs',{}) for r in report['instances'] if r['name'] in dict(SCHEDULE)]
    require(len(full_logs)==4 and all(r.get('fullBytes') and not r.get('truncated') for r in full_logs)
        and sum(r.get('fileCount',0) for r in full_logs)>=8,'Complete raw ABBA server logs missing')
    report['completeAbbaServerLogs']=dict(rounds=4,fileCount=sum(r['fileCount'] for r in full_logs),fullBytes=True,truncated=False)
    means={role:{key:sum(r['wallSeconds'][key] for r in report['rounds'] if r['role']==role)/2
        for key in report['rounds'][0]['wallSeconds']} for role in ('baseline','candidate')}
    report.update(status='native_correctness_and_descriptive_abba_completed',meansSeconds=means,
        allOfficialInputsUnchanged=True,formalContestRun=False,performanceSignificanceClaimed=False,
        speedupClaimed=False,baselineRebuilt=False,originalBenchmarkScorerRun=False,
        limitations=['Only two samples per role; no statistical performance conclusion',
            'No native fault injection of resolver/conversion failures or explicit ObTableScanOp rescan/cache reset introspection',
            'SQL coverage and canonical aux parity do not prove every operator-lifetime/eval-context path'])
    source_proof(args)
    dump(args.root/'report.json',report)
    print('EXPR_CACHE_RESULT '+json.dumps(dict(status=report['status'],correctnessPassed=True,
        meansSeconds=means,formalContestRun=False,speedupClaimed=False)),flush=True)


def collect(args):
    """Explicit owned evidence only. No datafiles, env, core or credentials."""
    require(not args.out.exists(),'Fresh evidence output required')
    args.out.mkdir(parents=True)
    report_path=args.root/'report.json'
    if report_path.is_file(): shutil.copyfile(report_path,args.out/'report.json')
    server_archives=[]; archive_total=0
    for name,role in SCHEDULE:
        directory=args.root/'validation'/name
        receipt_path=directory/'full-server-logs.json'
        if not receipt_path.is_file(): continue
        receipt=json.loads(receipt_path.read_text())
        path=directory/'full-server-logs.tar.gz'
        common.no_symlinks(path)
        require(receipt['name']==name and receipt['role']==role and receipt['fullBytes'] and not receipt['truncated']
            and path.stat().st_size==receipt['archive']['bytes'] and common.digest(path)==receipt['archive']['sha256'],
            'Full raw ABBA server archive changed')
        archive_total+=path.stat().st_size
        require(path.stat().st_size<=SERVER_LOG_ARCHIVE_CAP and archive_total<=SERVER_LOG_TOTAL_ARCHIVE_CAP,
            'Full raw ABBA archive output cap exceeded')
        destination=args.out/'server-logs'/(name+'-full-server-logs.tar.gz')
        destination.parent.mkdir(exist_ok=True)
        shutil.copyfile(path,destination)
        server_archives.append(dict(name=name,role=role,path=destination.relative_to(args.out).as_posix(),
            **{k:receipt[k] for k in ('archive','fileCount','rawBytes','fullBytes','truncated')}))
    manifest=[]; payload=[]; budget=LOG_PAYLOAD_CAP; complete=True
    selected=[args.root/'report.json',args.root/'plan.json']
    for directory in (args.root/'evidence',args.root/'validation'):
        if directory.is_dir():
            for p in sorted(directory.rglob('*')):
                if 'database' not in p.relative_to(args.root).parts and p.is_file() and not p.is_symlink():
                    selected.append(p)
    def priority(p):
        if p.name in ('report.json','plan.json'): return (0,str(p))
        if p.name in ('supplemental-plan-compatibility.json','mysqltest-output.log','original-run-case.log') or p.suffix in ('.reject','.result'): return (1,str(p))
        return (2 if p.suffix=='.json' else 3,str(p))
    for p in sorted(set(selected),key=priority):
        if not p.exists(): continue
        common.no_symlinks(p)
        rel=p.relative_to(args.root).as_posix()
        if p.suffix not in ('.json','.log','.txt','.sql','.reject','.result'): continue
        original=p.stat().st_size
        full=p.suffix in ('.json','.reject','.result') or p.name in ('mysqltest-output.log','original-run-case.log')
        cap=8*1024**2 if full or p.suffix=='.sql' else LOG_FILE_CAP
        require(not full or original<=cap,'Refusing truncated complete native/JSON evidence')
        take=min(original,cap)
        # Generated 20K INSERT SQL identity is already recorded, not reuploaded.
        if p.name in ('load.sql','generated-input.sql'):
            manifest.append(dict(path=rel,bytes=original,sha256=common.digest(p),omitted=True,reason='Original generator input and executed SQL hashes retained; large INSERT payload omitted')); continue
        if take>budget:
            complete=False
            manifest.append(dict(path=rel,bytes=original,sha256=common.digest(p),omitted=True,
                reason='Explicit aggregate evidence cap; report and highest-priority native proof saved first'))
            continue
        with p.open('rb') as f:
            f.seek(original-take); raw=f.read(take)
        budget-=len(raw)
        manifest.append(dict(path=rel,bytes=original,savedBytes=len(raw),offset=original-len(raw),
            sha256=sha(raw),truncated=original!=len(raw)))
        payload.append((rel,raw))
    def populate(tar):
        for rel,raw in payload: add_bytes(tar,rel,raw)
    record=archive(args.out/'evidence.tar.gz',LOG_ARCHIVE_CAP,populate)
    dump(args.out/'evidence-manifest.json',dict(archive=record,files=manifest,
        archiveCapBytes=LOG_ARCHIVE_CAP,payloadCapBytes=LOG_PAYLOAD_CAP,allSelectedEvidenceSaved=complete,
        fullCandidateSeparate=True,baselineNotReuploaded=True,completeServerArchives=server_archives,
        completeServerArchiveCapBytes=SERVER_LOG_TOTAL_ARCHIVE_CAP))
    if (args.root/'report.json').is_file(): shutil.copyfile(args.root/'report.json',args.out/'report.json')
    print('EXPR_CACHE_EVIDENCE '+json.dumps(record),flush=True)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--phase',choices=('prepare','prebuild','snapshot','preserve','validate','collect','proof'),required=True)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--driver-sha',required=True)
    p.add_argument('--artifact-zip',type=Path)
    p.add_argument('--out',type=Path)
    a=p.parse_args(argv)
    a.source=a.source.resolve(strict=True); a.root=a.root.absolute()
    try:
        if a.phase=='proof': print(json.dumps(source_proof(a),indent=2))
        else: globals()[a.phase](a)
        return 0
    except Exception as e:
        report_path=a.root/'report.json'
        if report_path.is_file() and a.phase!='collect':
            report=json.loads(report_path.read_text())
            report.update(status='failed_or_blocked',failurePhase=a.phase,error=str(e),traceback=traceback.format_exc())
            dump(report_path,report)
        print('EXPR_CACHE_BLOCKED '+json.dumps(dict(phase=a.phase,error=str(e))),flush=True)
        raise


if __name__=='__main__':
    raise SystemExit(main())
