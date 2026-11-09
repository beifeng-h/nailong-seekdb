"""Offline synthetic fixtures only: never fetch or execute a production ELF."""
import argparse
from dataclasses import replace
import copy
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import tarfile
import tempfile
import textwrap
import subprocess
import types
import unittest
from unittest.mock import patch
import zipfile

import fts_b3_reuse_validation as r
import fts_source_hybrid_validation as h
import test_fts_plan_compatibility as proof_fixtures

REPO=Path(__file__).resolve().parents[3]
D='a'*40
INPUTS={'original':'f'*64}
CASES={'native':dict(testSha256='1'*64,expectedSha256='2'*64)}


class ArchiveFixture:
    def __init__(self, base, mutate=None, tar_mutate=None, elf_mutate=None):
        self.root=base/'candidate';self.source=base/'source';self.source.mkdir(exist_ok=True)
        self.elf=b'\x7fELF'+r.B3.encode()+b'fixture-full-ELF\x00'
        if elf_mutate:self.elf=elf_mutate(self.elf)
        memory=io.BytesIO()
        with tarfile.open(fileobj=memory,mode='w:gz',format=tarfile.USTAR_FORMAT) as tar:
            info=tarfile.TarInfo('candidate/seekdb');info.size=len(self.elf);info.mode=0o555
            tar.addfile(info,io.BytesIO(self.elf))
        self.tar=memory.getvalue()
        if tar_mutate:self.tar=tar_mutate(self.tar)
        self.identity=dict(role='candidate',sourceCommit=r.B3,sourceTree=r.B3_TREE,
            driverCommit=r.B3,validationDriverCommit=r.B3,kernelOrigin=r.PRODUCTION_KERNEL,
            binary='/old/run/runtime/candidate/seekdb',bytes=len(self.elf),sha256=r.checked._sha(self.elf),
            buildId='abc123',generatedVersionSha256='3'*64,buildSettings=r.BUILD_SETTINGS,
            rustVersion=r.checked.RUST_VERSION,officialInputHashes=INPUTS,caseInputs=CASES,
            loader=dict(exitCode=0,childrenCleaned=True))
        old_plan=dict(mode=h.MODE,driverCommit=r.B3,candidateBuildCommit=r.B3,
            validationDriverCommit=r.B3,kernelOrigin=r.PRODUCTION_KERNEL,
            root='/old/run',workspace='/old/source',candidateSource='/old/source')
        data={'evidence/plan.json':r._json_bytes(old_plan),
            'evidence/report.json':r._json_bytes(dict(status='invalid_calibration',failure={'reason':'missing parent'})),
            'evidence/runtime/candidate/identity.json':r._json_bytes(self.identity),
            'evidence/diagnostic.log':b'actual complete fixture diagnostic\n'}
        identity_raw=data['evidence/runtime/candidate/identity.json']
        self.pins=r._Pins(0,'0'*64,len(self.tar),r.checked._sha(self.tar),len(self.elf),
            r.checked._sha(self.elf),'abc123','3'*64,len(identity_raw),r.checked._sha(identity_raw))
        self.manifest=dict(schema=1,kind=h.MODE,files=[dict(path=k.removeprefix('evidence/'),bytes=len(v),sha256=r.checked._sha(v)) for k,v in data.items()],
            bytes=sum(map(len,data.values())),historicalValidationAccepted=False,historicalCollectorPassed=False)
        data.update({'evidence/source-hybrid-evidence-manifest.json':r._json_bytes(self.manifest),
            'candidate-build/candidate-artifact-manifest.json':r._json_bytes(r._candidate_manifest(self.pins)),
            'candidate-build/candidate-identity.json':identity_raw,
            'candidate-build/candidate-elf.tar.gz':self.tar})
        self.data=data
        if mutate:mutate(data)
        self.zip=base/'fixture.zip'
        self.write_zip(data)

    def write_zip(self,data):
        with zipfile.ZipFile(self.zip,'w',compression=zipfile.ZIP_STORED) as out:
            for name,raw in data.items():out.writestr(name,raw)
        self.repin_zip()

    def repin_zip(self):
        self.pins=replace(self.pins,zip_bytes=self.zip.stat().st_size,zip_sha256=r.checked._sha(self.zip.read_bytes()))

    def prepare(self):
        with patch.object(h,'inventory',return_value=INPUTS),patch.object(h,'case_input_hashes',return_value=CASES),\
            patch.object(r.checked,'_read_build_id',return_value=self.pins.build_id):
            return r._prepare_candidate(self.zip,self.root,self.source,self.pins,self.manifest)


class ReuseContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)

    def test_production_metadata_is_log_evidence_not_materialization_claim(self):
        m=r._metadata();members=r._member_inventory(m,r._PRODUCTION_PINS)
        self.assertEqual(len(m['files']),202);self.assertEqual(len(members),206)
        self.assertEqual(members['candidate-build/candidate-elf.tar.gz']['bytes'],329567714)
        self.assertEqual(r._PRODUCTION_PINS.elf_bytes,1332364368)
        self.assertIn('NOT a claim of local artifact byte verification',
            (REPO/r.PREFIX/r.INVENTORY_FILE).read_text())

    def test_original_production_modules_and_historical_workflow_remain_exact_B3(self):
        for name in r.legacy.DRIVER_FILES:
            path=r.PREFIX+name
            self.assertEqual((REPO/path).read_bytes(),r.git(REPO,'show',r.B3+':'+path))
        archived=REPO/r.PREFIX/'fixtures/fts-source-hybrid-b3-build.yml'
        self.assertNotEqual(archived.parent, REPO/'.github/workflows')
        self.assertEqual(r.digest(archived), '699080e5bb0229d9d982b286a74bba26c63a5b5ac9173a975911c90c8b61e2f4')
        self.assertEqual(archived.read_bytes(),r.git(REPO,'show',r.B3+':.github/workflows/fts-contest-validation.yml'))

    def test_existing_75_inputs_and_three_native_diagnostics_remain_B3(self):
        self.assertEqual(len(h.inventory(REPO)),75)
        hashes=h.case_input_hashes(REPO)
        self.assertEqual(len(hashes),8)
        for name in (*r.legacy.REGRESSIONS,*h.NEW_CASES):
            self.assertIn(name,hashes)

    def test_plan_keeps_B3_build_D_validation_and_zero_builds(self):
        p=r.plan_for(self.base/'D',self.base/'run',D,r.PRODUCTION_KERNEL)
        self.assertEqual(p['candidateBuildCommit'],r.B3)
        self.assertEqual(p['candidateBuildDriverCommit'],r.B3)
        self.assertEqual(p['validationDriverCommit'],D)
        self.assertNotEqual(p['candidateSource'],p['workspace'])
        self.assertEqual([p[k] for k in ('candidateBuildsRequired','freshNativeCasesRequired','freshUntimedInstancesRequired','freshTimedRoundsRequired','ownedCleanupsRequired')],[0,13,2,8,23])
        self.assertFalse(p['historicalB3ValidationAccepted'])

    def test_public_preparer_has_no_caller_identity_override(self):
        import inspect
        self.assertEqual(tuple(inspect.signature(r.prepare_candidate).parameters),('artifact_zip','root','source'))
        with patch.object(r,'source_identity',return_value={'tree':'bad'}),self.assertRaisesRegex(RuntimeError,'actual clean B3'):
            r.prepare_candidate(self.base/'no',self.base/'none',self.base)

    def test_positive_all_streams_identity_original_and_invalid_report_preserved(self):
        f=ArchiveFixture(self.base);record=f.prepare()
        self.assertEqual((f.root/'seekdb').read_bytes(),f.elf)
        self.assertEqual((f.root/'identity.original.json').read_bytes(),f.data['candidate-build/candidate-identity.json'])
        self.assertEqual((f.root/'original/evidence/report.json').read_bytes(),f.data['evidence/report.json'])
        self.assertEqual(record['buildDriverCommit'],r.B3)
        self.assertFalse(record['currentValidationPerformed'])
        self.assertTrue((f.root/'seekdb').stat().st_mode & 0o111)
        self.assertEqual(json.loads((f.root/'identity.original.json').read_text())['validationDriverCommit'],r.B3)

    def test_wrong_zip_hash_and_size_rejected_before_materialization(self):
        for key,value in (('zip_sha256','0'*64),('zip_bytes',3)):
            with self.subTest(key=key),tempfile.TemporaryDirectory(dir=self.base) as directory:
                f=ArchiveFixture(Path(directory));f.pins=replace(f.pins,**{key:value})
                with self.assertRaises(ValueError):f.prepare()
                self.assertFalse(f.root.exists())

    def test_missing_unexpected_or_traversal_zip_member_fails(self):
        for mutate in (lambda d:d.pop('evidence/report.json'),lambda d:d.update({'../evil':b'x'}),
            lambda d:d.update({'unrequested':b'x'})):
            with self.subTest(mutate=mutate),tempfile.TemporaryDirectory(dir=self.base) as directory:
                f=ArchiveFixture(Path(directory),mutate=mutate)
                with self.assertRaises(ValueError):f.prepare()
                self.assertFalse(f.root.exists())

    def test_wrong_unselected_evidence_hash_is_rejected(self):
        f=ArchiveFixture(self.base,mutate=lambda d:d.update({'evidence/diagnostic.log':b'wrong same bytes diagnostic length\n'}))
        with self.assertRaises(ValueError):f.prepare()
        self.assertFalse(f.root.exists())

    def test_zip_crc_corruption_is_detected_even_with_fixture_zip_hash_repinned(self):
        f=ArchiveFixture(self.base);raw=f.zip.read_bytes()
        old=f.data['evidence/diagnostic.log'];offset=raw.index(old)
        f.zip.write_bytes(raw[:offset]+bytes([old[0]^1])+raw[offset+1:]);f.repin_zip()
        with self.assertRaises((ValueError,zipfile.BadZipFile)):f.prepare()

    def test_zip_duplicate_link_and_encrypted_entries_rejected(self):
        for mode in ('duplicate','link','encrypted'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory(dir=self.base) as directory:
                f=ArchiveFixture(Path(directory))
                with zipfile.ZipFile(f.zip,'w',compression=zipfile.ZIP_STORED) as out:
                    for name,raw in f.data.items():
                        info=zipfile.ZipInfo(name)
                        if mode=='link' and name=='evidence/report.json':info.external_attr=(stat.S_IFLNK|0o777)<<16
                        out.writestr(info,raw)
                    if mode=='duplicate':out.writestr('evidence/report.json',f.data['evidence/report.json'])
                if mode=='encrypted':
                    raw=bytearray(f.zip.read_bytes());i=raw.index(b'PK\x01\x02');raw[i+8:i+10]=struct.pack('<H',1);f.zip.write_bytes(raw)
                f.repin_zip()
                with self.assertRaises((ValueError,RuntimeError)):f.prepare()

    def test_zip_suffix_prefix_comment_or_truncation_rejected(self):
        for mutate in (lambda x:x+b'junk',lambda x:b'junk'+x,lambda x:x[:-1],
            lambda x:x[:-2]+b'\x02\x00x'):
            with self.subTest(mutate=mutate),tempfile.TemporaryDirectory(dir=self.base) as directory:
                f=ArchiveFixture(Path(directory));f.zip.write_bytes(mutate(f.zip.read_bytes()));f.repin_zip()
                with self.assertRaises((ValueError,zipfile.BadZipFile,struct.error)):f.prepare()

    def test_standard_ZIP64_end_records_and_per_file_ZIP64_are_supported(self):
        for mode in ('end-records','per-file'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory(dir=self.base) as directory:
                f=ArchiveFixture(Path(directory))
                if mode=='end-records':
                    raw=f.zip.read_bytes();end=struct.unpack('<4s4H2LH',raw[-22:])
                    offset=len(raw)-22
                    record=struct.pack('<4sQ2H2L4Q',b'PK\x06\x06',44,45,45,0,0,end[4],end[4],end[5],end[6])
                    locator=struct.pack('<4sLQL',b'PK\x06\x07',0,offset,1)
                    legacy=struct.pack('<4s4H2LH',b'PK\x05\x06',0,0,0xffff,0xffff,0xffffffff,0xffffffff,0)
                    f.zip.write_bytes(raw[:-22]+record+locator+legacy)
                else:
                    with zipfile.ZipFile(f.zip,'w',compression=zipfile.ZIP_DEFLATED) as out:
                        for name,raw in f.data.items():
                            with out.open(name,'w',force_zip64=True) as member:member.write(raw)
                f.repin_zip();f.prepare()
                self.assertEqual((f.root/'seekdb').read_bytes(),f.elf)

    def test_empty_ancestor_directory_entries_and_bounded_comments_are_supported(self):
        f=ArchiveFixture(self.base)
        directories=sorted({str(parent)+'/' for name in f.data for parent in Path(name).parents if str(parent)!='.'})
        with zipfile.ZipFile(f.zip,'w',compression=zipfile.ZIP_DEFLATED) as out:
            for name in directories:out.writestr(name,b'')
            for name,raw in f.data.items():out.writestr(name,raw)
            out.comment=b'bounded synthetic ZIP comment'
        f.repin_zip();f.prepare()
        self.assertEqual((f.root/'seekdb').read_bytes(),f.elf)

    def test_nonempty_unknown_or_duplicate_directories_rejected(self):
        for mode in ('nonempty','unknown','duplicate'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory(dir=self.base) as directory:
                f=ArchiveFixture(Path(directory))
                with zipfile.ZipFile(f.zip,'w') as out:
                    for name,raw in f.data.items():out.writestr(name,raw)
                    out.writestr('unknown/' if mode=='unknown' else 'evidence/',b'not empty' if mode=='nonempty' else b'')
                    if mode=='duplicate':out.writestr('evidence/',b'')
                f.repin_zip()
                with self.assertRaises(ValueError):f.prepare()

    def test_ZIP64_multidisk_record_offsets_and_counts_fail_closed(self):
        for field,value in ((4,1),(7,999),(9,1)):
            with self.subTest(field=field),tempfile.TemporaryDirectory(dir=self.base) as directory:
                f=ArchiveFixture(Path(directory));raw=f.zip.read_bytes();end=struct.unpack('<4s4H2LH',raw[-22:]);offset=len(raw)-22
                fields=[b'PK\x06\x06',44,45,45,0,0,end[4],end[4],end[5],end[6]];fields[field]=value
                record=struct.pack('<4sQ2H2L4Q',*fields)
                locator=struct.pack('<4sLQL',b'PK\x06\x07',0,offset,1)
                legacy=struct.pack('<4s4H2LH',b'PK\x05\x06',0,0,0xffff,0xffff,0xffffffff,0xffffffff,0)
                f.zip.write_bytes(raw[:-22]+record+locator+legacy);f.repin_zip()
                with self.assertRaises((ValueError,zipfile.BadZipFile)):f.prepare()

    def test_tar_fullstream_truncation_nonzero_tail_and_wrong_path_fail_closed(self):
        import gzip
        def bad_tail(raw):return gzip.compress(gzip.decompress(raw)+b'forbidden')
        def bad_path(raw):
            data=bytearray(gzip.decompress(raw));data[:len(b'candidate/seekdb')]=b'candidate/xekdb';data[148:156]=b'        '
            data[148:156]=('%06o\0 ' % sum(data[:512])).encode();return gzip.compress(data)
        for mutate in (lambda x:x[:-8],bad_tail,bad_path):
            with self.subTest(mutate=mutate),tempfile.TemporaryDirectory(dir=self.base) as directory:
                f=ArchiveFixture(Path(directory),tar_mutate=mutate)
                with self.assertRaises((ValueError,EOFError,OSError)):f.prepare()
                self.assertFalse((f.root/'seekdb').exists() and (f.root/'seekdb').stat().st_mode & 0o111)

    def test_tar_wrong_elf_magic_or_missing_embedded_B3_never_executable(self):
        for mutate in (lambda x:b'XXXX'+x[4:],lambda x:x.replace(r.B3.encode(),b'z'*40)):
            with self.subTest(mutate=mutate),tempfile.TemporaryDirectory(dir=self.base) as directory:
                f=ArchiveFixture(Path(directory),elf_mutate=mutate)
                with self.assertRaises(ValueError):f.prepare()
                self.assertFalse((f.root/'seekdb').stat().st_mode & 0o111)

    def test_static_BuildID_mismatch_never_executable(self):
        f=ArchiveFixture(self.base)
        with patch.object(h,'inventory',return_value=INPUTS),patch.object(h,'case_input_hashes',return_value=CASES),\
            patch.object(r.checked,'_read_build_id',return_value='bad'),self.assertRaisesRegex(ValueError,'BuildID'):
            r._prepare_candidate(f.zip,f.root,f.source,f.pins,f.manifest)
        self.assertFalse((f.root/'seekdb').stat().st_mode & 0o111)

    def test_wrong_source_driver_kernel_tree_version_profile_or_inputs_rejected(self):
        f=ArchiveFixture(self.base);old=json.loads(f.data['evidence/plan.json'])
        with patch.object(h,'inventory',return_value=INPUTS),patch.object(h,'case_input_hashes',return_value=CASES):
            r._identity(f.identity,old,f.source,f.pins)
            for key,bad in (('sourceCommit',D),('driverCommit',D),('validationDriverCommit',D),
                ('sourceTree','b'*40),('kernelOrigin',r.legacy.KERNEL),('generatedVersionSha256','0'*64),
                ('officialInputHashes',{}),('caseInputs',{}),('buildSettings',{}),('binary','/relabel/seekdb'),
                ('sha256','0'*64),('bytes',1),('buildId','bad')):
                with self.subTest(key=key),self.assertRaises(ValueError):
                    r._identity(dict(f.identity,**{key:bad}),old,f.source,f.pins)

    def test_missing_identity_required_path_or_wrong_75_inventory_rejected(self):
        f=ArchiveFixture(self.base)
        for name in ('evidence/plan.json','evidence/report.json','evidence/runtime/candidate/identity.json'):
            changed=copy.deepcopy(f.manifest)
            changed['files']=[x for x in changed['files'] if 'evidence/'+x['path']!=name]
            changed['bytes']=sum(x['bytes'] for x in changed['files'])
            with self.subTest(name=name),self.assertRaises(ValueError):r._member_inventory(changed,f.pins)
        old=json.loads(f.data['evidence/plan.json'])
        with patch.object(h,'inventory',return_value={'tampered':'1'*64}),patch.object(h,'case_input_hashes',return_value=CASES),self.assertRaises(ValueError):
            r._identity(f.identity,old,f.source,f.pins)

    def test_input_artifact_symlink_hardlink_or_existing_output_rejected(self):
        for mode in ('symlink','hardlink','existing'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory(dir=self.base) as directory:
                f=ArchiveFixture(Path(directory))
                if mode=='symlink':
                    original=f.zip;f.zip=original.with_name('alias.zip');f.zip.symlink_to(original)
                elif mode=='hardlink':os.link(f.zip,f.zip.with_name('alias.zip'))
                else:f.root.mkdir()
                with self.assertRaises(ValueError):f.prepare()

    def test_new_driver_must_be_distinct_and_kernel_exact_before_source_reads(self):
        for driver,kernel in ((r.B3,r.PRODUCTION_KERNEL),(r.legacy.CANDIDATE,r.PRODUCTION_KERNEL),(D,r.legacy.KERNEL),('short',r.PRODUCTION_KERNEL)):
            with patch.object(r,'source_identity') as read,self.assertRaises(RuntimeError):r.verify_driver(self.base,driver,kernel)
            read.assert_not_called()

    def test_new_driver_allows_only_explicit_CI_changes(self):
        good=[r.B3_TREE.encode(),r.PRODUCTION_KERNEL_TREE.encode(),(r.PREFIX+'fts_b3_reuse_validation.py').encode(),b'']
        with patch.object(r,'source_identity',return_value={'tree':'b'*40}),patch.object(r,'git',side_effect=good),patch.object(h,'inventory'),patch.object(h,'case_input_hashes',return_value=CASES):
            result=r.verify_driver(self.base,D,r.PRODUCTION_KERNEL)
            self.assertEqual(result['candidateBuildCommit'],r.B3)
        for changed in ('src/share/evil.cpp','tools/benchmark/fts_large_bench.py',r.PREFIX+'fts_plan_compatibility.py'):
            with patch.object(r,'source_identity',return_value={'tree':'b'*40}),patch.object(r,'git',side_effect=[r.B3_TREE.encode(),r.PRODUCTION_KERNEL_TREE.encode(),changed.encode()]),self.assertRaisesRegex(RuntimeError,'allowlist'):
                r.verify_driver(self.base,D,r.PRODUCTION_KERNEL)

    def test_fresh_validation_rejects_old_flags_or_missing_untimed_parent_before_loader(self):
        args=argparse.Namespace(root=self.base/'run')
        with patch.object(r,'load_module') as loader:
            for old in ({'instances':[{}]},{'roundMetrics':{'A1':{}}},{'supplementalCompatibility':{'accepted':True}},{}):
                with self.assertRaises(RuntimeError):r.validate(args,{},old)
            loader.assert_not_called()

    def test_prepare_creates_all_fresh_stages_including_untimed(self):
        args=argparse.Namespace(source=self.base/'D',root=self.base/'run',driver_sha=D,kernel_sha=r.PRODUCTION_KERNEL,
            artifact_zip=self.base/'A.zip',candidate_artifact_zip=self.base/'B3.zip')
        args.source.mkdir()
        # Stop on the first script copy after observing the real fresh directory topology.
        def git(*a):raise RuntimeError('fixture stops before copy')
        with patch.object(r,'verify_driver',return_value={}),patch.object(r,'git',side_effect=git),self.assertRaisesRegex(RuntimeError,'fixture stops'):
            r.prepare(args)
        for stage in ('cases/baseline','cases/candidate','untimed','rounds'):
            self.assertTrue((args.root/'validation'/stage).is_dir())

    def test_invalid_partial_report_cannot_be_completed_or_claim_improvement(self):
        args=argparse.Namespace(driver_sha=D,kernel_sha=r.PRODUCTION_KERNEL,summary=None)
        report={'status':'invalid_calibration','sameHostPerformance':{'classification':'improvement'}}
        self.assertFalse(r.completed(report,args,{}))
        out=io.StringIO()
        with redirect_stdout(out):r.present(report,args,None)
        visible=json.loads(out.getvalue().splitlines()[0].split(' ',1)[1])
        self.assertIn('no performance claim',visible['result'])

    def test_current_A_B_and_client_loader_checks_are_bounded_and_fail_closed(self):
        log=self.base/'ldd.log';log.write_text('libc.so => /lib/libc.so\n')
        with patch.object(r,'capture_group',return_value={'exitCode':0,'childrenCleaned':True}) as capture:
            r.verify_loader(self.base/'fixture-ELF',log,self.base,{})
            command=capture.call_args.args[0]
            self.assertIn('1048576',command[2]);self.assertIn('RLIMIT_FSIZE',command[2])
            self.assertEqual(capture.call_args.args[3],30)
        for row,data in (({'exitCode':124,'childrenCleaned':False},b'timed out'),
            ({'exitCode':0,'childrenCleaned':True},b'lib.so => not found'),
            ({'exitCode':0,'childrenCleaned':True},b'x'*(r.checked.NOTE_CAP+1))):
            log.write_bytes(data)
            with patch.object(r,'capture_group',return_value=row),self.assertRaises(RuntimeError):
                r.verify_loader(self.base/'fixture-ELF',log,self.base,{})

    def test_inactive_proposal_has_no_compile_only_exact_downloads_and_parses_Bash(self):
        path=REPO/r.PREFIX/'fixtures/fts-b3-reuse-proposal.yml'
        self.assertNotEqual(path.parent,REPO/'.github/workflows')
        text=path.read_text()
        self.assertIn('INACTIVE PROPOSAL ONLY',text)
        self.assertEqual(text.count('artifact-ids:'),2)
        for identity in ('13621468966','13652898100','42486120779','42565036644'):
            self.assertIn(identity,text)
        for forbidden in ('--build build_release','--phase snapshot','--phase preserve-candidate','actions: write','secrets.'):
            self.assertNotIn(forbidden,text)
        self.assertIn('cd "$PAIRED_ROOT/sources/candidate"',text)
        self.assertEqual(text.count('./build.sh release --init'),1)
        self.assertIn('--phase validate',text)
        for block in text.split('        run: |\n')[1:]:
            lines=[]
            for line in block.splitlines():
                if line and not line.startswith('          '):break
                lines.append(line[10:] if line else '')
            result=subprocess.run(['bash','-n'],input='\n'.join(lines)+'\n',text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_fresh_loop_runs_13_native_2_untimed_8_timed_in_separate_B3_source(self):
        # A scheduling/topology fixture. No server, client, ELF or SQL is run.
        args=argparse.Namespace(root=self.base/'run',source=self.base/'D',driver_sha=D,kernel_sha=r.PRODUCTION_KERNEL)
        args.source.mkdir();args.root.mkdir()
        A=args.root/'sources/baseline';B=args.root/'sources/candidate'
        A.mkdir(parents=True);B.mkdir()
        for stage in ('cases/baseline','cases/candidate','untimed','rounds'):
            (args.root/'validation'/stage).mkdir(parents=True)
        inputs=h.case_input_hashes(REPO)
        plan=dict(baselineSource=str(A),candidateSource=str(B),artifact={},runtimeVerificationSha256='a'*64,
            candidateRelocation={'buildDriverCommit':r.B3},equivalence={'caseInputs':inputs})
        binary=Path('/proc/self/exe').resolve()
        identities={role:dict(sourceCommit=r.BASELINE if role=='baseline' else r.B3,
            sourceTree='b'*40,kernelOrigin=r.BASELINE if role=='baseline' else r.PRODUCTION_KERNEL,
            driverCommit=r.legacy.CANDIDATE if role=='baseline' else r.B3,sha256='f'*64,
            bytes=1,buildId='abc',officialInputHashes={}) for role in ('baseline','candidate')}
        identity_path=args.root/'runtime/candidate/identity.original.json'
        h.write_json(identity_path,identities['candidate'])
        calls=[]
        native=types.SimpleNamespace(discover_cases=lambda source:[types.SimpleNamespace(name=name) for name in h.selected_cases('candidate')])
        class Harness:
            def original_inputs(self,source):return {}
            def load_native(self,source):return native
            def original_case(self,call,native,case):
                fail=case.name==r.CASES[-1] and call.source==B
                return lambda call,out:dict(passed=not fail,exitCode=1 if fail else 0,
                    invocations=1,inputsUnchanged=True,**inputs[case.name])
            def run_instance(self,call,native,name,port,work,initialize):
                out=call.out/name;out.mkdir()  # Exercises real non-recursive stage topology.
                result=work(types.SimpleNamespace(base_dir=out,port=port),out)
                row=dict(name=name,status='passed' if result['passed'] else 'failed',cleanupPassed=True,work=result)
                h.write_json(out/'result.json',row)
                calls.append((call.role,name,call.source,call.expected_sha,call.binary,initialize))
                return row
        harness=Harness()
        scorer=types.SimpleNamespace(load_baseline=lambda p:{})
        sdb=types.SimpleNamespace(inspect_instance_process=lambda *a:os.getpid())
        def load(name,path):return harness if name=='paired_original_harness' else sdb if name=='paired_owned_sdb' else scorer
        report=dict(driverCommit=D,baselineCommit=r.BASELINE,formalContestRun=False,
            builds={},instances=[],caseVerdicts=[],roundVerdicts=[],roundMetrics={})
        with ExitStack() as stack:
            replacements={
                'load_module':load,'verify_driver':lambda *a: {},'source_identity':lambda *a:{'tree':'b'*40},
                'historical_baseline':lambda *a:(binary,identities['baseline']),
                'candidate_identity':lambda *a:binary,'verify_loader':lambda *a:{},
                'runtime_env':lambda *a:{'LD_LIBRARY_PATH':'fixture','PATH':'fixture'},
                'digest':lambda path:r.CLIENTS.get(Path(path).name,'f'*64),
                'effective_parameters':lambda *a:{},'persist_report':lambda *a:None,
                'accepted_compatibility':lambda *a:True,'case_evidence_complete':lambda *a:True,
                'tokenize_work':lambda *a:(lambda call,out:{'passed':True}),
                'tokenize_complete':lambda *a:True,'benchmark_work':lambda *a:(lambda call,out:{'passed':True,'metrics':{'fixture':1}}),
                'accepted_plan_assertions':lambda *a:True,'performance':lambda *a:{'fixtureOnly':True}}
            for name,value in replacements.items():stack.enter_context(patch.object(r,name,value))
            stack.enter_context(patch.object(h,'case_input_hashes',return_value=inputs))
            stack.enter_context(patch.object(r.time,'sleep'))
            stack.enter_context(patch('fts_plan_compatibility.classify_plan_metadata_compatibility',return_value={'accepted':True}))
            r.validate(args,plan,report)
        self.assertEqual(len(calls),23)
        self.assertEqual([len(report[k]) for k in ('caseVerdicts','untimedVerdicts','roundVerdicts')],[13,2,8])
        self.assertEqual(sum(c[-1] for c in calls),13)
        self.assertTrue(all(call[2]==(A if call[0]=='baseline' else B) for call in calls))
        self.assertTrue(all(call[3]==(r.BASELINE if call[0]=='baseline' else r.B3) for call in calls))
        self.assertEqual(report['builds']['candidate']['driverCommit'],r.B3)
        self.assertEqual(report['validationDriverCommit'],D)
        self.assertTrue(all(row['cleanupPassed'] for row in report['instances']))
        simple=[row for row in report['caseVerdicts'] if row['role']=='candidate' and row['name']==r.CASES[-1]][0]
        self.assertEqual(simple['status'],'failed');self.assertFalse(simple['work']['passed'])
        self.assertEqual(simple['validationContext']['sourceCommit'],r.B3)
        self.assertEqual(simple['validationContext']['validationDriverCommit'],D)

    def test_strict_proof_context_separates_B3_from_D_without_relabel(self):
        proof_fixtures.PlanMetadataCompatibility.setUpClass()
        fixture=proof_fixtures.PlanMetadataCompatibility(methodName='test_positive_real_pinned_fixture_complete_four_flag_proof')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        raw=fixture.row;h.write_json(fixture.out/'result.json',raw)
        context=dict(sourceCommit=r.B3,validationDriverCommit=D,kernelOrigin=r.PRODUCTION_KERNEL,
            source=str(fixture.source),out=str(fixture.out),nativeResult=dict(path=str(fixture.out/'result.json'),
                bytes=(fixture.out/'result.json').stat().st_size,sha256=h.digest(fixture.out/'result.json')))
        proof=dict(fixture.classify(),validationContext=context)
        row=dict(raw,role='candidate',validationContext=context)
        before=copy.deepcopy(row)
        self.assertTrue(r.accepted_compatibility(proof,row,D,r.B3))
        self.assertEqual(row,before);self.assertEqual(row['status'],'failed')
        self.assertFalse(proof['nativePassed'])
        for key,bad in (('sourceCommit',D),('validationDriverCommit',r.B3),('kernelOrigin',r.legacy.KERNEL),('out','/stale/out')):
            changed_row=copy.deepcopy(row);changed_proof=copy.deepcopy(proof)
            changed_row['validationContext'][key]=bad;changed_proof['validationContext'][key]=bad
            self.assertFalse(r.accepted_compatibility(changed_proof,changed_row,D,r.B3))
        self.assertFalse(r.accepted_compatibility(proof,row,None,r.B3))
        self.assertFalse(r.accepted_compatibility(proof,row,D,D))
        changed_row=copy.deepcopy(row);changed_row['status']='passed';changed_row['work']['passed']=True
        self.assertFalse(r.accepted_compatibility(proof,changed_row,D,r.B3))
        fixture.reject.write_bytes(fixture.compatible+b'\n')
        self.assertFalse(r.accepted_compatibility(proof,row,D,r.B3))


if __name__=='__main__':unittest.main()
