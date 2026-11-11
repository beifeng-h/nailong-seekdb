"""Local contract tests, not native SQL/build/performance results."""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile
import json
import tarfile
import fts_expr_cache_profile as profile
from contextlib import ExitStack
import unittest
from unittest.mock import patch
import fts_expr_cache_validation as driver
from fts_expr_cache_targeted import PureTests


class ValidationContractTests(unittest.TestCase):
    def test_exact_source_contract(self):
        source=Path(__file__).resolve().parents[3]
        head=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD']).decode().strip()
        proof=driver.source_proof(argparse.Namespace(source=source,driver_sha=head))
        self.assertEqual(proof['originalInputCount'],75)
        self.assertEqual(proof['patchSha256'],driver.PATCH_SHA)
        self.assertEqual(set(proof['productChanges']),driver.PRODUCT)
        self.assertEqual(proof['baselineCommit'],'b14755bed10d1399cf7d1dca6444cfa757727b21')
        self.assertEqual(len(proof['caseInputs']),9)

    def test_canonical_diff_ignores_abbreviation_and_rendering_defaults(self):
        source=Path(__file__).resolve().parents[3]
        expected=driver.canonical_patch(source)
        self.assertEqual(driver.sha(expected),driver.CANONICAL_PATCH_SHA)
        self.assertEqual(driver.PATCH_SHA,'12b654df178f5d13bfb22507ccc35ab7957384bb295712c9f15e0d75ad8ec55e')
        for abbrev in (7,8,12):
            command=['git','-C',str(source),'-c','core.abbrev='+str(abbrev),
                '-c','color.ui=always','-c','diff.noprefix=true','-c','diff.context=0',
                '-c','diff.algorithm=histogram','diff',*driver.CANONICAL_DIFF_FLAGS,driver.BASE,driver.KERNEL,'--']
            self.assertEqual(subprocess.check_output(command),expected)
        indexes=[line for line in expected.decode().splitlines() if line.startswith('index ')]
        self.assertEqual(indexes,[
            'index 1a9cea5806d7b490c6a8b1fb604cf61d6d87e1c8..20f8f167dcc1902a5c6c527c4a4820f698274cab 100644',
            'index c81c665c97353c6c503711b41c34dd2388f994a5..93d3e8548e77d86883fada10bd21f65b0f6aa6bd 100644'])

    def test_canonical_diff_with_shallow_exact_object_fetch(self):
        source=Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as tmp:
            shallow=Path(tmp)/'shallow'; shallow.mkdir()
            subprocess.run(['git','-C',str(shallow),'init','--quiet'],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            subprocess.run(['git','-C',str(shallow),'fetch','--quiet','--no-tags','--depth=1',
                source.as_uri(),driver.BASE,driver.KERNEL],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            self.assertEqual(subprocess.check_output(['git','-C',str(shallow),'rev-parse','--is-shallow-repository']).strip(),b'true')
            expected=driver.canonical_patch(source)
            for abbrev in (7,8,12):
                actual=subprocess.check_output(['git','-C',str(shallow),'-c','core.abbrev='+str(abbrev),
                    'diff',*driver.CANONICAL_DIFF_FLAGS,driver.BASE,driver.KERNEL,'--'])
                self.assertEqual(actual,expected)
            self.assertEqual(driver.sha(expected),driver.CANONICAL_PATCH_SHA)

    def test_abba_only(self):
        self.assertEqual(driver.SCHEDULE,(('A1','baseline'),('B1','candidate'),('B2','candidate'),('A2','baseline')))
        self.assertEqual([x[0] for x in driver.DDL],['build_ik_all','build_ik_content','build_beng_en'])
        self.assertEqual(driver.DATA_SHA,'bc385b3c9586d40d667093947be20a8714ed65301eae68487af8fac22956d235')

    def test_query_shape_pins(self):
        source=Path(__file__).resolve().parents[3]
        q=driver.common.queries_from_benchmark(source/'tools/benchmark/fts_large_bench.sh')
        self.assertEqual(tuple(q),driver.common.QUERY_NAMES)
        self.assertEqual(len(driver.common.plan_queries(q)),7)
        self.assertEqual(tuple(name=='query_limit' or name in driver.common.PLAN_CONTROLS
            for name in driver.common.plan_queries(q)),(False,False,False,True,True,True,True))

    def test_wrong_commit_identity_rejected(self):
        wrong=b'author other <other@example.com> 0 +0000\ncommitter other <other@example.com> 0 +0000\n\nx\n'
        with patch.object(driver.common,'git',return_value=wrong):
            with self.assertRaisesRegex(RuntimeError,'Unverified user Git'):
                driver.git_identity(Path('.'),'a'*40)

    def test_no_historical_timing_or_scorer(self):
        text=Path(driver.__file__).read_text()
        self.assertNotIn('common.benchmark_work(',text)
        self.assertNotIn("globals()['BASELINE']",text)
        self.assertNotIn('--allow-config-mismatch',text)
        self.assertNotIn('3000',text)
        self.assertNotIn('query_rounds',text)
        self.assertLess(driver.BINARY_ARCHIVE_CAP,512*1024**2)
        self.assertLess(driver.LOG_ARCHIVE_CAP,512*1024**2)

    def test_personal_trigger_optout_only(self):
        source=Path(__file__).resolve().parents[3]
        old_text=(source/'.github/workflows/fts-contest-validation.yml').read_text()
        new_text=(source/'.github/workflows/fts-expr-cache-validation.yml').read_text()
        old_branches=re.search(r'^    branches: \[([^]]+)\]',old_text,re.M).group(1).split(',')
        new_branches=re.search(r'^    branches: \[([^]]+)\]',new_text,re.M).group(1).split(',')
        registered='perf/fts-official-contract-20261108'
        dedicated='perf/fts-expr-cache-validation-20261110'
        self.assertEqual(old_branches,[registered])
        self.assertEqual(new_branches,[dedicated])
        condition=next(line for line in old_text.splitlines() if line.lstrip().startswith('if:'))
        marker='[dedicated-native-validation]'
        self.assertIn("!contains(github.event.head_commit.message, '"+marker+"')",condition)
        self.assertNotIn(marker,new_text)
        def old_runs(branch,message):
            return branch in old_branches and marker not in message
        def new_runs(branch,message):
            return branch in new_branches
        self.assertTrue(old_runs(registered,'ordinary existing commit'))
        self.assertFalse(old_runs(dedicated,marker))
        self.assertTrue(new_runs(dedicated,marker))
        self.assertFalse(old_runs(registered,marker))
        self.assertFalse(new_runs(registered,marker))
        previous=subprocess.check_output(['git','-C',str(source),'show',driver.BASE+':.github/workflows/fts-contest-validation.yml']).decode()
        self.assertEqual(old_text,previous.replace(
            "if: ${{ github.repository == 'beifeng-h/nailong-seekdb' && github.event.repository.private == false }}",
            "if: ${{ github.repository == 'beifeng-h/nailong-seekdb' && github.event.repository.private == false && !contains(github.event.head_commit.message, '[dedicated-native-validation]') }}"))

    def test_bounded_collector_preserves_partial_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'root'; root.mkdir()
            (root/'evidence').mkdir()
            (root/'report.json').write_text('{"status":"failed_or_blocked"}\n')
            (root/'plan.json').write_text('{}\n')
            for name in ('one.log','two.log','three.log'):
                (root/'evidence'/name).write_text('x'*80)
            out=Path(tmp)/'out'
            with patch.object(driver,'LOG_PAYLOAD_CAP',140):
                driver.collect(argparse.Namespace(root=root,out=out))
            manifest=json.loads((out/'evidence-manifest.json').read_text())
            self.assertFalse(manifest['allSelectedEvidenceSaved'])
            self.assertEqual((out/'report.json').read_text(),(root/'report.json').read_text())
            self.assertTrue((out/'evidence.tar.gz').is_file())
            self.assertTrue(any(r.get('omitted') for r in manifest['files']))

    def test_baseline_fixture_precedes_compile_and_receipt_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'root'; source=Path(tmp)/'source'
            (root/'runtime/baseline').mkdir(parents=True)
            (root/'runtime/baseline/seekdb').write_bytes(b'baseline-placeholder')
            (root/'runtime/baseline/identity.json').write_text(json.dumps(dict(role='baseline',sha256=driver.BASE_ELF_SHA)))
            (root/'evidence').mkdir()
            (root/'report.json').write_text(json.dumps(dict(instances=[],targeted={})))
            for name in driver.common.CLIENTS:
                path=source/'deps/3rd/u01/obclient/bin'/name
                path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(name.encode())
            helper=source/driver.PREFIX/'fts_expr_cache_targeted.py'
            helper.parent.mkdir(parents=True); helper.write_text('fixture-v1')
            pins={name:driver.common.digest(source/'deps/3rd/u01/obclient/bin'/name) for name in driver.common.CLIENTS}
            args=argparse.Namespace(root=root,source=source,driver_sha='d'*40)
            calls=[]
            def fake_run(a,report,identities,role,name,work,**kwargs):
                self.assertEqual((role,name),('baseline','targeted-baseline'))
                self.assertFalse((source/'build_release/src/observer/seekdb').exists())
                calls.append((role,name))
                row=dict(name=name,status='passed',cleanupPassed=True,
                    work=dict(passed=True,canonical={'alpha':[[1,2,3]]}))
                out=root/'validation'/name; out.mkdir(parents=True)
                (out/'result.json').write_text(json.dumps(row))
                report['instances'].append(row)
                return row
            with ExitStack() as stack:
                for name in ('load_plan','resource_record','build_profile','verify_version'):
                    stack.enter_context(patch.object(driver,name,return_value={}))
                stack.enter_context(patch.object(driver,'run_owned',side_effect=fake_run))
                stack.enter_context(patch.object(driver.common,'CLIENTS',pins))
                stack.enter_context(patch.object(driver.common,'verify_loader',return_value={}))
                driver.prebuild(args)
                report=json.loads((root/'report.json').read_text())
                receipt=driver.check_baseline_targeted(args,report)
                self.assertTrue(receipt['executedBeforeCandidateCompile'])
                self.assertEqual(calls,[('baseline','targeted-baseline')])
                with self.assertRaisesRegex(RuntimeError,'execute once'):
                    driver.prebuild(args)
            helper.write_text('fixture-v2')
            with self.assertRaisesRegex(RuntimeError,'receipt changed'):
                driver.check_baseline_targeted(args,report)
        source=Path(__file__).resolve().parents[3]
        workflow=(source/'.github/workflows/fts-expr-cache-validation.yml').read_text()
        self.assertLess(workflow.index('--phase prebuild'),workflow.index('--target ob_sql'))
        self.assertLess(workflow.index('--target ob_sql'),workflow.index('--target seekdb'))

    def test_complete_round_server_logs_are_original_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'A1'; out.mkdir()
            base=out/'database'; log=base/'log'; log.mkdir(parents=True)
            data={'seekdb.log':b'complete-main-log\n'*20000,'console.log':b'startup stdout and stderr\n',
                  'seekdb.log.wf':b'warning detail\n','seekdb.log.20261111091500000':b'rotated full bytes\n'}
            for name,raw in data.items(): (log/name).write_bytes(raw)
            (out/'own-stop.log.json').write_text('{"exitCode":0}')
            (out/'owned-process.json').write_text('{"pid":1073741824,"startTicks":0}')
            receipt=driver.capture_full_server_logs(base,out,'baseline','A1')
            self.assertTrue(base.is_dir())
            self.assertEqual(receipt['captureMoment'],'after_owned_stop_before_database_destroy')
            self.assertEqual(receipt['fileCount'],4)
            self.assertTrue(receipt['fullBytes']); self.assertFalse(receipt['truncated'])
            with tarfile.open(out/'full-server-logs.tar.gz','r:gz') as tar:
                actual={m.name:tar.extractfile(m).read() for m in tar.getmembers()}
            self.assertEqual(actual,{'log/'+name:raw for name,raw in data.items()})
            for record in receipt['files']:
                self.assertEqual(record['sha256'],driver.sha(data[Path(record['path']).name]))
                self.assertEqual(record['bytes'],len(data[Path(record['path']).name]))

    def test_full_server_log_cap_or_live_process_never_claim_full(self):
        for mode in ('live','cap'):
            with tempfile.TemporaryDirectory() as tmp:
                out=Path(tmp)/'B1'; out.mkdir(); base=out/'database'; log=base/'log'; log.mkdir(parents=True)
                for name in ('seekdb.log','console.log'): (log/name).write_bytes(b'original logs'*100)
                (out/'own-stop.log.json').write_text('{"exitCode":0}')
                (out/'owned-process.json').write_text('{"pid":1073741824,"startTicks":42}')
                with ExitStack() as stack:
                    if mode=='live':
                        stack.enter_context(patch.object(driver,'process_state',return_value=dict(state='S',startTicks=42)))
                    else:
                        stack.enter_context(patch.object(driver,'SERVER_LOG_ARCHIVE_CAP',10))
                    with self.assertRaises((RuntimeError,ValueError)):
                        driver.capture_full_server_logs(base,out,'candidate','B1')
                self.assertFalse((out/'full-server-logs.json').exists())
                self.assertFalse((out/'full-server-logs.tar.gz').exists())
                self.assertTrue(base.exists())

    def test_collector_keeps_complete_archives_outside_tail_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'root'; root.mkdir()
            (root/'report.json').write_text('{"status":"failed_or_blocked"}')
            (root/'plan.json').write_text('{}')
            for name,role in driver.SCHEDULE:
                out=root/'validation'/name; out.mkdir(parents=True)
                base=out/'database'; log=base/'log'; log.mkdir(parents=True)
                for file in ('seekdb.log','console.log'): (log/file).write_bytes((name+' original raw\n').encode()*100)
                (out/'own-stop.log.json').write_text('{"exitCode":0}')
                (out/'owned-process.json').write_text('{"pid":1073741824,"startTicks":0}')
                driver.capture_full_server_logs(base,out,role,name)
            dest=Path(tmp)/'out'
            with patch.object(driver,'LOG_PAYLOAD_CAP',140): driver.collect(argparse.Namespace(root=root,out=dest))
            manifest=json.loads((dest/'evidence-manifest.json').read_text())
            logs=manifest['completeServerArchives']
            self.assertEqual(len(logs),4)
            self.assertEqual(sum(r['fileCount'] for r in logs),8)
            for row in logs:
                self.assertEqual(driver.common.digest(dest/row['path']),row['archive']['sha256'])
                self.assertTrue(row['fullBytes']); self.assertFalse(row['truncated'])
            self.assertLess(driver.SERVER_LOG_TOTAL_ARCHIVE_CAP+driver.LOG_ARCHIVE_CAP,512*1024**2)

    def test_round_hook_uses_original_stop_destroy_gap_and_is_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'root'; root.mkdir(); source=Path(tmp)/'source'; source.mkdir()
            args=argparse.Namespace(root=root,source=source,driver_sha='d'*40)
            report=dict(instances=[])
            identity=dict(binary=str(root/'candidate-placeholder'),sha256='e'*64)
            original=driver.native_harness.bounded_db_logs
            def fake_instance(role_args,native,name,port,work,initialize=False):
                self.assertIsNot(driver.native_harness.bounded_db_logs,original)
                out=role_args.out/name; out.mkdir(parents=True)
                base=out/'database'; log=base/'log'; log.mkdir(parents=True)
                for file in ('console.log','seekdb.log'): (log/file).write_bytes(b'complete raw')
                (out/'own-stop.log.json').write_text('{"exitCode":0}')
                (out/'owned-process.json').write_text('{"pid":1073741824,"startTicks":0}')
                captured=driver.native_harness.bounded_db_logs(base,out)
                self.assertTrue(base.is_dir())
                self.assertTrue((out/'full-server-logs.tar.gz').is_file())
                # A native harness would destroy here; fake row only checks the seam.
                return dict(name=name,status='passed',cleanupPassed=True,dbLogs=captured,work={'passed':True})
            with ExitStack() as stack:
                stack.enter_context(patch.object(driver,'source_proof',return_value={}))
                stack.enter_context(patch.object(driver.common,'source_identity',return_value={}))
                stack.enter_context(patch.object(driver.common,'runtime_env',return_value={'PATH':'same','LD_LIBRARY_PATH':'same'}))
                stack.enter_context(patch.object(driver.native_harness,'load_native',return_value=None))
                stack.enter_context(patch.object(driver.native_harness,'run_instance',side_effect=fake_instance))
                row=driver.run_owned(args,report,{'candidate':identity},'candidate','B1',lambda *a:None)
            self.assertIs(driver.native_harness.bounded_db_logs,original)
            self.assertEqual(row['dbLogs']['fullServerLogs']['fileCount'],2)
        source=Path(__file__).resolve().parents[3]
        text=(source/'.github/script/seekdb/fts_contest_validation.py').read_text()
        self.assertLess(text.index("row['stop'] = capture"),text.index("row['dbLogs'] = bounded_db_logs"))
        self.assertLess(text.index("row['dbLogs'] = bounded_db_logs"),text.index("row['destroy'] = capture"))

    def test_identical_product_after_kernel(self):
        source=Path(__file__).resolve().parents[3]
        diff=subprocess.check_output(['git','-C',str(source),'diff','--name-only',driver.KERNEL,'HEAD','--','src'])
        self.assertFalse(diff)


class ProfileTests(unittest.TestCase):
    # These positive Clang fixtures are synthetic. The actual failed CI did not
    # retain its cache/compiler files; it retained only configure stdout.
    def fixture(self, root):
        source=root/'source'; build=source/'build_release'; files=build/'CMakeFiles/3.27.9'
        files.mkdir(parents=True); (source/'rust').mkdir()
        args=argparse.Namespace(source=source,root=root/'out',phase='prebuild')
        rows=[]
        cache='\n'.join(k+':STRING='+v for k,v in dict(profile.EXPECTED,
            CMAKE_GENERATOR='Unix Makefiles',CMAKE_EXPORT_COMPILE_COMMANDS='ON').items())+'\n'
        (build/'CMakeCache.txt').write_text(cache)
        unity=build/'src/sql/CMakeFiles/ob_sql.dir/Unity/affected.cxx'
        unity.parent.mkdir(parents=True); unity.write_text('#include "src/sql/engine/table/ob_table_scan_op.cpp"\n')
        for lang,name in (('C','clang-17'),('CXX','clang++-17')):
            compiler=source/'deps/3rd/usr/local/oceanbase/devtools/bin'/name
            compiler.parent.mkdir(parents=True,exist_ok=True); compiler.write_text('synthetic compiler path only')
            record=''.join('set(CMAKE_'+lang+'_'+key+' "'+value+'")\n' for key,value in
                (('COMPILER',str(compiler)),('COMPILER_ID','Clang'),('COMPILER_VERSION','17.0.6')))
            (files/('CMake'+lang+'Compiler.cmake')).write_text(record)
            file=unity if lang=='CXX' else source/'probe.c'
            rows.append(dict(directory=str(build),command=str(compiler)+' -c '+str(file)+' -o '+str(build/(lang+'.o')),file=str(file)))
        (build/'compile_commands.json').write_text(json.dumps(rows))
        return args,build,files,rows

    @staticmethod
    def version(command, **kwargs):
        return b'rustc 1.98.1 (fixture)\n' if command[0]=='rustc' else b'clang version 17.0.6 (fixture)\n'

    def test_generated_records_without_compiler_cache_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            args,build,files,rows=self.fixture(Path(tmp))
            self.assertNotIn('CMAKE_C_COMPILER',profile.cache_values((build/'CMakeCache.txt').read_bytes()))
            with patch.object(profile.subprocess,'check_output',side_effect=self.version):
                settings,rust=profile.verify_build_profile(args)
                args.phase='snapshot'; profile.verify_build_profile(args)
            self.assertEqual(settings,profile.EXPECTED); self.assertTrue(rust.startswith('rustc 1.98.1 '))
            out=args.root/'evidence/candidate/profile-prebuild'
            for name in ('CMakeCache.txt','CMakeCCompiler.cmake','CMakeCXXCompiler.cmake','compile-commands-proof.json','profile-proof.json'):
                self.assertTrue((out/name).is_file())
            proof=json.loads((out/'compile-commands-proof.json').read_text())
            self.assertEqual(proof['selectedEntries'],[rows[1]])
            self.assertEqual(proof['sha256'],profile.digest(build/'compile_commands.json'))
            self.assertFalse(proof['wholeFileCopied'])

    def test_read_layout_from_genuine_local_msvc_excerpt(self):
        # Verbatim field shape observed from existing CMake 4.3.1-msvc1 local
        # configure. This proves the layout only, not Linux Clang17 behavior.
        path='C:/Program Files/Microsoft Visual Studio/18/Community/VC/Tools/MSVC/14.51.36231/bin/HostX64/x64/cl.exe'
        for lang in ('C','CXX'):
            text='set(CMAKE_'+lang+'_COMPILER "'+path+'")\nset(CMAKE_'+lang+'_COMPILER_ID "MSVC")\nset(CMAKE_'+lang+'_COMPILER_VERSION "19.51.36257.0")\nset(CMAKE_'+lang+'_COMPILER_WORKS TRUE)\n'
            values=profile.compiler_values(text.encode(),lang)
            self.assertEqual(values,dict(COMPILER=path,COMPILER_ID='MSVC',COMPILER_VERSION='19.51.36257.0'))

    def test_reject_changed_generated_or_cache_profile(self):
        for mode in ('version','id','path','cache','generator','missing','duplicate'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                args,build,files,rows=self.fixture(Path(tmp)); record=files/'CMakeCXXCompiler.cmake'
                if mode=='version':record.write_text(record.read_text().replace('17.0.6','17.0.7'))
                if mode=='id':record.write_text(record.read_text().replace('"Clang"','"GNU"'))
                if mode=='path':record.write_text(record.read_text().replace('clang++-17','other-clang'))
                if mode=='cache':
                    with (build/'CMakeCache.txt').open('a') as f:f.write('CMAKE_CXX_COMPILER:FILEPATH=/other/clang\n')
                if mode=='generator':(build/'CMakeCache.txt').write_text((build/'CMakeCache.txt').read_text().replace('Unix Makefiles','Ninja'))
                if mode=='missing':record.unlink()
                if mode=='duplicate':
                    other=files.parent/'other';other.mkdir();(other/record.name).write_bytes(record.read_bytes())
                with patch.object(profile.subprocess,'check_output',side_effect=self.version):
                    with self.assertRaises(RuntimeError):profile.verify_build_profile(args)
                self.assertTrue((args.root/'evidence/candidate/profile-prebuild/CMakeCache.txt').is_file())

    def test_actual_version_still_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            args,_,_,_=self.fixture(Path(tmp))
            with patch.object(profile.subprocess,'check_output',return_value=b'clang version 17.0.7\n'):
                with self.assertRaisesRegex(RuntimeError,'Actual compiler'):profile.verify_build_profile(args)

    def test_compile_commands_with_and_without_known_launcher(self):
        for launcher in (False,True):
            with tempfile.TemporaryDirectory() as tmp:
                args,build,_,rows=self.fixture(Path(tmp))
                if launcher:
                    with (build/'CMakeCache.txt').open('a') as f:f.write('OB_COMPILE_EXECUTABLE:FILEPATH=/trusted/ob-compile\n')
                    for row in rows:row['command']='/trusted/ob-compile '+row['command']
                    (build/'compile_commands.json').write_text(json.dumps(rows))
                with patch.object(profile.subprocess,'check_output',side_effect=self.version):profile.verify_build_profile(args)

    def test_command_uses_other_compiler_or_loses_affected_unit(self):
        for mode in ('compiler','missing','duplicate'):
            with tempfile.TemporaryDirectory() as tmp:
                args,build,_,rows=self.fixture(Path(tmp))
                if mode=='compiler':rows[1]['command']='/other/compiler -c '+rows[1]['file']
                if mode=='missing':rows=rows[:1]
                if mode=='duplicate':rows.append(rows[1])
                (build/'compile_commands.json').write_text(json.dumps(rows))
                with patch.object(profile.subprocess,'check_output',side_effect=self.version):
                    with self.assertRaises(RuntimeError):profile.verify_build_profile(args)

    def test_large_command_database_saved_only_as_hash_and_selected_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            args,build,_,rows=self.fixture(Path(tmp));rows[0]['irrelevantPadding']='x'*(9*1024**2)
            (build/'compile_commands.json').write_text(json.dumps(rows))
            with patch.object(profile.subprocess,'check_output',side_effect=self.version):profile.verify_build_profile(args)
            out=args.root/'evidence/candidate/profile-prebuild'
            self.assertFalse((out/'compile_commands.json').exists())
            proof=out/'compile-commands-proof.json'
            self.assertLess(proof.stat().st_size,1024**2)
            self.assertEqual(json.loads(proof.read_text())['bytes'],(build/'compile_commands.json').stat().st_size)

    def test_snapshot_rejects_profile_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            args,_,files,_=self.fixture(Path(tmp))
            with patch.object(profile.subprocess,'check_output',side_effect=self.version):
                profile.verify_build_profile(args)
                with (files/'CMakeCCompiler.cmake').open('a') as f:f.write('# changed generated record\n')
                args.phase='snapshot'
                with self.assertRaisesRegex(RuntimeError,'changed since prebuild'):profile.verify_build_profile(args)

    def test_collector_preserves_whitelisted_profile_files_in_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'root';out=root/'evidence/candidate/profile-prebuild';out.mkdir(parents=True)
            (root/'report.json').write_text('{"status":"failed_or_blocked"}')
            raw=b'# full cache evidence\n'*20000
            (out/'CMakeCache.txt').write_bytes(raw)
            (out/'CMakeCCompiler.cmake').write_bytes(b'set(CMAKE_C_COMPILER "/compiler")\n')
            destination=Path(tmp)/'collected';driver.collect(argparse.Namespace(root=root,out=destination))
            with tarfile.open(destination/'evidence.tar.gz','r:gz') as tar:
                self.assertEqual(tar.extractfile('evidence/candidate/profile-prebuild/CMakeCache.txt').read(),raw)
                self.assertIsNotNone(tar.extractfile('evidence/candidate/profile-prebuild/CMakeCCompiler.cmake'))



if __name__=='__main__':
    unittest.main()
