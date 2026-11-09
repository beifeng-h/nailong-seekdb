"""Offline contracts for the archived one-new-build B3 workflow, separate from legacy proof.

No production compile, artifact download, preserved-ELF execution or SQL starts.
"""
import argparse
import copy
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import fts_source_hybrid_validation as h
import fts_paired_validation as legacy
import test_fts_plan_compatibility as proof_fixtures

REPO = Path(__file__).resolve().parents[3]
DRIVER = 'a' * 40
TREE = 'b' * 40
WORKFLOW = REPO / '.github/script/seekdb/fixtures/fts-source-hybrid-b3-build.yml'


class HybridContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root, self.source = self.base / 'root', self.base / 'source'
        self.root.mkdir()
        self.source.mkdir()
        self.args = argparse.Namespace(source=self.source, root=self.root, driver_sha=DRIVER,
            kernel_sha=h.PRODUCTION_KERNEL, role='candidate', out=self.base / 'artifact')
        self.plan = h.plan_for(self.source, self.root, DRIVER, h.PRODUCTION_KERNEL)
        self.plan['equivalence'] = dict(driverTree=TREE, caseInputs={})

    def test_historical_workflow_is_byte_exact_and_not_runnable(self):
        path = REPO / '.github/script/seekdb/fixtures/fts-verified-elf-continuation.yml'
        self.assertEqual(h.digest(path), 'cd6e921249f308159a50be74a1315d395d486a4a20f96e333c17e4a56dd6de8f')
        self.assertNotEqual(path.parent, REPO / '.github/workflows')

    def test_legacy_production_contract_files_unmodified(self):
        for name in ('fts_paired_validation.py', 'fts_verified_elf_reuse.py', 'fts_plan_compatibility.py',
                     'fts_contest_validation.py', 'fts_paired_collect.py'):
            path = '.github/script/seekdb/' + name
            self.assertEqual((REPO / path).read_bytes(), h.git(REPO, 'show',
                'fe876f8f805969970621a18a1cf812ff322fc78c:' + path))

    def test_all_75_original_inputs_have_exact_canonical_hash(self):
        inputs = h.inventory(REPO)
        self.assertEqual(len(inputs), 75)
        self.assertEqual(hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(',', ':')).encode()).hexdigest(), h.INVENTORY_SHA256)

    def test_inventory_rejects_any_count_or_hash_drift(self):
        for inputs in ({}, {'x': 'y'}, {str(i): 'f' * 64 for i in range(75)}):
            with patch.object(h, 'load_module') as load:
                load.return_value.original_inputs.return_value = inputs
                with self.assertRaisesRegex(RuntimeError, '75 original'):
                    h.inventory(self.source)

    def test_source_roles_and_schedule_do_not_relabel_old_B(self):
        self.assertEqual(self.plan['candidateBuildCommit'], DRIVER)
        self.assertNotEqual(self.plan['candidateBuildCommit'], legacy.CANDIDATE)
        self.assertEqual(self.plan['kernelOrigin'], h.PRODUCTION_KERNEL)
        self.assertFalse(self.plan['baselineRebuilt'])
        self.assertFalse(self.plan['historicalCandidateExecuted'])
        self.assertEqual(self.plan['candidateBuildsRequired'], 1)
        self.assertEqual(len(self.plan['schedule']), 8)
        self.assertEqual(self.plan['benchmarkConfig'], legacy.CONFIG)
        self.assertEqual(len(h.selected_cases('baseline')), 5)
        self.assertEqual(len(h.selected_cases('candidate')), 8)
        self.assertIn('fts_index.tokenize_beng_noop_casefold_contract', h.selected_cases('candidate'))

    def test_old_and_malformed_kernel_pins_rejected_before_source_reads(self):
        for kernel in list(h.OLD_KERNELS) + ['bad', '0' * 40]:
            with patch.object(h, 'source_identity') as read:
                with self.assertRaisesRegex(RuntimeError, 'explicit new production'):
                    h.verify_driver(self.source, DRIVER, kernel)
                read.assert_not_called()

    def test_verify_driver_protects_kernel_tree_and_non_CI_changes(self):
        with patch.object(h, 'source_identity', return_value=dict(tree=TREE)), patch.object(h, 'git', return_value=b'wrong'):
            with self.assertRaisesRegex(RuntimeError, 'kernel tree'):
                h.verify_driver(self.source, DRIVER, h.PRODUCTION_KERNEL)
        for changed in ('src/sql/evil.cpp', 'tools/deploy/mysql_test/test_suite/fts_index/r/mysql/simple_query.result'):
            with patch.object(h, 'source_identity', return_value=dict(tree=TREE)), patch.object(h, 'git',
                    side_effect=[h.PRODUCTION_KERNEL_TREE.encode(), changed.encode()]):
                with self.assertRaisesRegex(RuntimeError, 'outside CI allowlist'):
                    h.verify_driver(self.source, DRIVER, h.PRODUCTION_KERNEL)

    def test_verify_driver_rejects_build_profile_changes(self):
        with patch.object(h, 'source_identity', return_value=dict(tree=TREE)), patch.object(h, 'git',
                side_effect=[h.PRODUCTION_KERNEL_TREE.encode(), b'', b'build.sh']):
            with self.assertRaisesRegex(RuntimeError, 'build/dependency/Rust'):
                h.verify_driver(self.source, DRIVER, h.PRODUCTION_KERNEL)

    def test_prebuild_rejects_existing_B_before_loader_checks(self):
        binary = self.source / 'build_release/src/observer/seekdb'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'existing')
        with patch.object(h, 'source_identity'), patch.object(h, 'verify_loader') as loader:
            with self.assertRaisesRegex(RuntimeError, 'no existing B'):
                h.prebuild(self.args, self.plan)
            loader.assert_not_called()

    def test_prebuild_checks_both_real_client_loaders_and_A(self):
        (self.root / 'evidence/candidate').mkdir(parents=True)
        with patch.object(h, 'source_identity'), patch.object(h, 'digest', side_effect=lambda p: h.CLIENTS[Path(p).name]), \
                patch.object(h, 'verify_loader') as loader, \
                patch.object(h, 'historical_baseline', return_value=(self.root/'original-A', {})):
            h.prebuild(self.args, self.plan)
            self.assertEqual([call.args[0].name for call in loader.call_args_list], ['obclient', 'mysqltest', 'original-A'])
            self.assertTrue((self.root / 'evidence/candidate/prebuild.json').is_file())

    def test_snapshot_refuses_A_role_and_missing_prebuild(self):
        self.args.role = 'baseline'
        with self.assertRaisesRegex(RuntimeError, 'A must never rebuild'):
            h.snapshot(self.args, self.plan)
        self.args.role = 'candidate'
        with self.assertRaises(FileNotFoundError):
            h.snapshot(self.args, self.plan)

    def candidate(self):
        binary = self.root / 'runtime/candidate/seekdb'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'\x7fELF' + DRIVER.encode() + b'full-elf-bytes')
        version = self.source / 'build_release/src/observer/ob_version.cpp'
        version.parent.mkdir(parents=True)
        version.write_text(DRIVER)
        identity = dict(role='candidate', sourceCommit=DRIVER, sourceTree=TREE, driverCommit=DRIVER,
            validationDriverCommit=DRIVER, kernelOrigin=h.PRODUCTION_KERNEL, binary=str(binary),
            bytes=binary.stat().st_size, sha256=h.digest(binary), buildId='abc123',
            generatedVersionSha256=h.digest(version), officialInputHashes={}, caseInputs={},
            buildSettings=dict(CMAKE_BUILD_TYPE='RelWithDebInfo', OB_ENABLE_UNITY='ON', OB_USE_LLD='ON'),
            rustVersion='rustc 1.98.1 synthetic')
        h.write_json(binary.parent / 'identity.json', identity)
        return binary, identity

    def test_candidate_identity_binds_actual_source_driver_kernel_hash_and_BuildID(self):
        binary, identity = self.candidate()
        with patch.object(h, 'inventory', return_value={}), patch.object(h.subprocess, 'check_output', return_value=b'Build ID: abc123\n'):
            self.assertEqual(h.candidate_identity(identity, self.args, self.plan), binary)
            for key, bad in (('sourceCommit', legacy.CANDIDATE), ('driverCommit', legacy.CANDIDATE),
                             ('validationDriverCommit', legacy.CANDIDATE), ('kernelOrigin', legacy.KERNEL),
                             ('sourceTree', 'f'*40), ('sha256', 'f'*64), ('bytes', 1), ('buildId', 'bad')):
                changed = dict(identity, **{key:bad})
                with self.subTest(key=key), self.assertRaises(RuntimeError):
                    h.candidate_identity(changed, self.args, self.plan)

    def test_candidate_identity_rejects_symlink_hardlink_and_embedded_revision_drift(self):
        binary, identity = self.candidate()
        with patch.object(h, 'inventory', return_value={}):
            alias = binary.with_name('alias'); os.link(binary, alias)
            with self.assertRaises(RuntimeError): h.candidate_identity(identity, self.args, self.plan)
            alias.unlink()
            binary.write_bytes(b'\x7fELF' + b'x'*40)
            identity.update(bytes=binary.stat().st_size, sha256=h.digest(binary))
            with self.assertRaises(RuntimeError): h.candidate_identity(identity, self.args, self.plan)

    def test_complete_B_archived_even_if_native_report_failed(self):
        binary, identity = self.candidate()
        h.write_json(self.root/'report.json', dict(status='invalid_calibration', native='FAIL'))
        with patch.object(h, 'candidate_identity', return_value=binary):
            record = h.preserve_candidate(self.args, self.plan)
        with tarfile.open(self.args.out/record['path'], 'r:gz') as archive:
            self.assertEqual(archive.getnames(), ['candidate/seekdb'])
            self.assertEqual(archive.extractfile('candidate/seekdb').read(), binary.read_bytes())
        self.assertFalse(json.loads((self.args.out/'candidate-artifact-manifest.json').read_text())['validationPassClaimed'])
        self.assertEqual(json.loads((self.root/'report.json').read_text())['status'], 'invalid_calibration')

    def test_collector_bounded_text_preserves_historical_failure_and_excludes_ELFs(self):
        h.write_json(self.root/'report.json', dict(status='invalid_calibration'))
        (self.root/'runtime').mkdir(); (self.root/'runtime/secret-env').write_text('secret')
        manifest = h.collect_evidence(self.root, self.args.out)
        self.assertFalse(manifest['historicalCollectorPassed'])
        self.assertFalse(manifest['historicalValidationAccepted'])
        self.assertEqual([x['path'] for x in manifest['files']], ['report.json'])
        self.assertFalse((self.args.out/'runtime/secret-env').exists())

    def test_collector_rejects_binary_invalid_utf8_oversize_or_link(self):
        for data in (b'\x00', b'\x7fELF', b'\xff', b'a'*(8*1024**2+1)):
            (self.root/'report.json').write_bytes(data)
            with self.subTest(data=data[:4]), self.assertRaises((RuntimeError, UnicodeError, ValueError)):
                h.collect_evidence(self.root, self.args.out)
        (self.root/'report.json').unlink()
        (self.root/'report.json').symlink_to(self.source/'whatever')
        with self.assertRaises(RuntimeError): h.collect_evidence(self.root, self.args.out)

    def test_collector_failure_does_not_overwrite_validation_report(self):
        before = b'{"status":"valid_calibration_completed"}\n'
        (self.root/'report.json').write_bytes(before)
        argv = ['--phase','collect-evidence','--source',str(self.source),'--root',str(self.root),
                '--driver-sha',DRIVER,'--kernel-sha',h.PRODUCTION_KERNEL,'--out',str(self.root/'nested')]
        with redirect_stdout(io.StringIO()): self.assertEqual(h.main(argv), 1)
        self.assertEqual((self.root/'report.json').read_bytes(), before)

    def token_rows(self, reverse=False):
        items = [{'alpha':2}, {'中文':1}]
        if reverse: items.reverse()
        return ('\n'.join(p+'/'+n+'\t'+json.dumps(
                dict(tokens=[], doc_len=0) if n in ('empty','null') else dict(tokens=items, doc_len=3),
                ensure_ascii=False) for p in ('ik','beng') for n,_ in h.TOKENIZE_CORPUS)+'\n').encode()

    def test_tokenize_canonical_sorts_only_unordered_tokens_preserving_freq_doclen(self):
        a, b = h.canonical_tokenize(self.token_rows()), h.canonical_tokenize(self.token_rows(True))
        self.assertEqual(a,b)
        self.assertEqual(a['ik/ascii'], dict(tokens={'alpha':2,'中文':1},doc_len=3))
        for old,new in ((b'"alpha": 2',b'"alpha": 3'),(b'"doc_len": 3',b'"doc_len": 4')):
            self.assertNotEqual(a,h.canonical_tokenize(self.token_rows().replace(old,new,1)))

    def test_tokenize_rejects_missing_duplicate_unknown_or_ambiguous_values(self):
        raw=self.token_rows()
        for bad in (b'', raw+raw.splitlines(True)[0], raw.replace(b'ik/ascii',b'unknown',1),
                    raw.replace(b'{"alpha": 2}',b'{"alpha": 2, "alpha": 1}',1),
                    raw.replace(b'{"alpha": 2}',b'{"alpha": true}',1),
                    raw.replace(b'"doc_len": 3',b'"doc_len": -1',1),
                    raw.replace(b'{"alpha": 2}','{"中文": 2}'.encode(),1)):
            with self.subTest(raw=bad[:80]), self.assertRaises((ValueError, UnicodeError)):
                h.canonical_tokenize(bad)

    def test_tokenize_SQL_nonzero_exit_or_unclean_children_refused(self):
        self.args.obclient=self.source/'obclient'
        self.args.workspace=self.source
        self.args.binary=self.root/'new-B'
        call=argparse.Namespace(port=7300)
        for result in (dict(exitCode=1,childrenCleaned=True),dict(exitCode=0,childrenCleaned=False)):
            with patch.object(h,'capture_group',return_value=result):
                self.assertFalse(h.tokenize_work(self.args)(call,self.root)['passed'])

    def test_tokenize_SQL_is_independent_generic_corpus_ALL_mode(self):
        sql=h.tokenize_sql()
        self.assertEqual(sql.count('SELECT '), 2*len(h.TOKENIZE_CORPUS))
        self.assertEqual(sql.count('[{"output": "all"}]'), 2*len(h.TOKENIZE_CORPUS))
        self.assertEqual(sql.count('TOKENIZE(CAST(NULL AS CHAR CHARACTER SET utf8mb4)'),2)
        self.assertNotIn('TOKENIZE(NULL',sql)
        self.assertNotIn('fts_large_bench',sql)
        self.assertIn(('Generic multilingual 中文 ABC123 alpha-beta. '*512).encode().hex(),sql)


class NullableTokenizeMatrix(unittest.TestCase):
    token_rows = HybridContracts.token_rows
    # These tests are offline process-boundary simulations, not SQL execution.
    def setUp(self):
        HybridContracts.setUp(self)
        self.args.obclient = self.source / 'never-executed-client'
        self.args.workspace = self.source
        self.args.binary = self.root / 'never-executed-server'
        self.args.expected_sha = DRIVER
        self.call = argparse.Namespace(port=7300)

    def capture_fixture(self, command, output, cwd, timeout, *, env, stdin):
        name = output.stem
        specs = {n:(sql,code,raw) for n,sql,code,raw in h.tokenize_preflight()}
        sql, code, raw = specs.get(name, (h.tokenize_sql(),0,self.token_rows()))
        self.assertEqual(stdin, sql.encode('utf-8'))
        self.assertEqual(command, [str(self.args.obclient),'-h127.0.0.1','-P7300',
            '-uroot','-A','-N','-B','--raw','--default-character-set=utf8mb4'])
        self.assertEqual(cwd,self.source)
        self.assertEqual(timeout,120)
        output.write_bytes(raw)
        row = dict(output=str(output),exitCode=code,childrenCleaned=True,timedOut=False,
            outputSha256=h.digest(output))
        h.write_json(Path(str(output)+'.json'),row)
        return row

    def run_fixture(self, out=None):
        with patch.object(h,'capture_group',side_effect=self.capture_fixture) as capture:
            work=h.tokenize_work(self.args)(self.call,out or self.root)
        return work,capture

    def completed_fixture(self):
        rows=[]
        for role,commit in (('baseline',h.BASELINE),('candidate',DRIVER)):
            out=self.root/'validation/untimed'/('tokenize-'+role)
            out.mkdir(parents=True)
            self.args.expected_sha=commit
            work,_=self.run_fixture(out)
            rows.append(dict(role=role,name='tokenize-'+role,status='passed',cleanupPassed=True,work=work))
        return rows

    def test_exact_sql_values_typed_null_preserves_16_original_cases(self):
        lines=h.tokenize_sql().splitlines()
        self.assertEqual(lines[0],h.legacy.SESSION.rstrip('\n'))
        self.assertEqual(len(lines),17)
        for line,(parser,name,value) in zip(lines[1:],
                [(p,n,v) for p in ('ik','beng') for n,v in h.TOKENIZE_CORPUS]):
            literal='CAST(NULL AS CHAR CHARACTER SET utf8mb4)' if value is None else \
                "CONVERT(X'%s' USING utf8mb4)" % value.encode('utf-8').hex()
            self.assertEqual(line,"SELECT '%s/%s', TOKENIZE(%s, '%s', '[{\"output\": \"all\"}]');" %
                             (parser,name,literal,parser))
        specs=h.tokenize_preflight()
        self.assertEqual([n for n,*_ in specs],['tokenize-input','tokenize-negative-raw-null',
            'tokenize-negative-binary','tokenize-negative-latin1'])
        self.assertEqual(specs[0][1],h.legacy.SESSION+
            "SELECT 'raw_null', NULL IS NULL, CHARSET(NULL);\n"
            "SELECT 'typed_null', CAST(NULL AS CHAR CHARACTER SET utf8mb4) IS NULL, "
            "CHARSET(CAST(NULL AS CHAR CHARACTER SET utf8mb4));\n"
            "SELECT 'utf8_empty', CONVERT(X'' USING utf8mb4) IS NULL, CHARSET(CONVERT(X'' USING utf8mb4));\n")
        for (_,sql,code,raw),expr,charset in zip(specs[1:],
                ('NULL',"CONVERT(X'616263' USING binary)","CONVERT(X'616263' USING latin1)"),
                ('binary','binary','latin1')):
            self.assertEqual(sql,h.legacy.SESSION+"SELECT TOKENIZE(%s, 'ik', '[{\"output\": \"all\"}]');\n"%expr)
            self.assertEqual(code,1)
            expected=(b"ERROR 1115 (42000) at line 2: Unknown character set: 'latin1'\n" if charset=='latin1' else
                ('ERROR 1235 (0A000) at line 2: ik with the %s charset is not supported\n'%charset).encode())
            self.assertEqual(raw,expected)

    def test_five_separate_invocations_capture_exact_sql_and_raw_error_proofs(self):
        work,capture=self.run_fixture()
        self.assertTrue(work['passed'])
        self.assertEqual(capture.call_count,5)
        self.assertEqual([c.args[1].name for c in capture.call_args_list],
            [n+'.log' for n,*_ in h.tokenize_preflight()]+['tokenize.log'])
        self.assertNotIn('--force',capture.call_args_list[0].args[0])
        for name,sql,_,raw in h.tokenize_preflight():
            proof=work['preflight'][name]
            self.assertEqual(proof['sqlSha256'],hashlib.sha256(sql.encode()).hexdigest())
            self.assertEqual(proof['invocation']['outputSha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(work['canonical']['ik/null'],dict(tokens={},doc_len=0))
        self.assertEqual(work['canonical']['beng/null'],dict(tokens={},doc_len=0))

    def test_isolated_negative_never_accepts_generic_1235_or_process_failure(self):
        original=self.capture_fixture
        for mutate in ('wrong-error','wrong-line','extra-stdout','missing-LF','success','timeout','unclean','bool-exit'):
            def capture(*args,**kwargs):
                row=original(*args,**kwargs)
                if args[1].stem=='tokenize-negative-raw-null':
                    if mutate in ('wrong-error','wrong-line','extra-stdout','missing-LF'):
                        raw=args[1].read_bytes()
                        raw={'wrong-error':raw.replace(b'ik with the binary',b'other with the binary'),
                            'wrong-line':raw.replace(b'line 2:',b'line 3:'),
                            'extra-stdout':b'partial result\n'+raw,'missing-LF':raw[:-1]}[mutate]
                        args[1].write_bytes(raw)
                        row['outputSha256']=h.digest(args[1])
                    else:
                        row.update({'success':dict(exitCode=0),'timeout':dict(timedOut=True),
                            'unclean':dict(childrenCleaned=False),'bool-exit':dict(exitCode=True)}[mutate])
                return row
            with self.subTest(mutate=mutate),patch.object(h,'capture_group',side_effect=capture) as mocked:
                work=h.tokenize_work(self.args)(self.call,self.root)
                self.assertFalse(work['passed'])
                self.assertEqual(mocked.call_count,2)
                self.assertNotIn('canonical',work)

    def test_actual_latin1_lookup_error_hash_and_ik_boundaries_are_not_interchangeable(self):
        # Complete actual A bytes reconstructed from run42583955784/job129049151912.
        actual=b"ERROR 1115 (42000) at line 2: Unknown character set: 'latin1'\n"
        self.assertEqual(len(actual),62)
        self.assertEqual(hashlib.sha256(actual).hexdigest(),
            '36d9dcdec06c663c193169adc27258b0a68e0122278d663f1a45c8626db3ee5d')
        specs={name:(sql,code,raw) for name,sql,code,raw in h.tokenize_preflight()}
        self.assertEqual(specs['tokenize-negative-latin1'][2],actual)
        for name in ('tokenize-negative-raw-null','tokenize-negative-binary'):
            self.assertEqual(hashlib.sha256(specs[name][2]).hexdigest(),
                'd3425c289f84acd847eb6687cf492cfa48b4b1172c22fbdc845797483c41d9c3')
        original=self.capture_fixture
        for name,wrong in (
                ('tokenize-negative-latin1',b'ERROR 1235 (0A000) at line 2: ik with the latin1 charset is not supported\n'),
                ('tokenize-negative-latin1',actual.replace(b'1115',b'1235')),
                ('tokenize-negative-latin1',actual.replace(b"'latin1'",b"'other'")),
                ('tokenize-negative-raw-null',actual),('tokenize-negative-binary',actual)):
            def capture(*args,**kwargs):
                row=original(*args,**kwargs)
                if args[1].stem==name:
                    args[1].write_bytes(wrong)
                    row['outputSha256']=h.digest(args[1])
                return row
            with self.subTest(name=name,wrong=wrong),patch.object(h,'capture_group',side_effect=capture):
                work=h.tokenize_work(self.args)(self.call,self.root)
                self.assertFalse(work['passed'])
                self.assertNotIn('canonical',work)
        rows=self.completed_fixture()
        self.assertTrue(h.tokenize_complete(rows,DRIVER,self.root))
        for role_index,role in enumerate(('baseline','candidate')):
            directory=self.root/'validation/untimed'/('tokenize-'+role)
            for name,wrong in (('tokenize-negative-latin1',specs['tokenize-negative-binary'][2]),
                    ('tokenize-negative-raw-null',actual),('tokenize-negative-binary',actual)):
                path=directory/(name+'.log');before=path.read_bytes()
                altered=copy.deepcopy(rows)
                path.write_bytes(wrong)
                altered[role_index]['work']['preflight'][name]['invocation']['outputSha256']=h.digest(path)
                with self.subTest(completed_role=role,name=name):
                    self.assertFalse(h.tokenize_complete(altered,DRIVER,self.root))
                path.write_bytes(before)

    def test_exact_compiled_charset_lookup_excludes_enum_only_latin1(self):
        # Compile exact lookup/validity bodies with only ObString/value facades.
        # This checks registry semantics, not a server or a production ELF build.
        source=(REPO/'src/oblib/lib/charset/ob_charset.cpp').read_text()
        header=(REPO/'src/oblib/lib/charset/ob_charset.h').read_text()
        def function(marker):
            start=source.index(marker);left=source.index('{',start);end=left+1;depth=1
            while depth:
                depth+=(source[end]=='{')-(source[end]=='}');end+=1
            return source[start:end]
        enum=re.search(r'enum ObCharsetType\s*\{.*?\n\};',header,re.S).group()
        utf8=re.search(r'^#define OB_UTF8MB4\s+("[^"]+")',
            (REPO/'src/oblib/lib/charset/ob_ctype.h').read_text(),re.M).group(1)
        binary=re.search(r'ObCharsetInfo ob_charset_bin\s*=\s*\{\s*63,0,0,\s*[^,]+,\s*("[^"]+")',
            (REPO/'src/oblib/lib/charset/ob_ctype_bin.cc').read_text()).group(1)
        facade="""#include <cassert>
#include <cctype>
#include <cstdint>
#include <string>
struct ObString { std::string value;
  int case_compare(const char *other) const {
    std::string right(other),left(value);
    for(char &c:left)c=std::tolower(static_cast<unsigned char>(c));
    for(char &c:right)c=std::tolower(static_cast<unsigned char>(c));
    return left.compare(right);
  }
};
struct CharsetName { const char *csname; };
"""+enum+"\nCharsetName ob_charset_utf8mb4_bin={"+utf8+"},ob_charset_bin={"+binary+"};\n"+"""
struct ObCharset { static ObCharsetType charset_type(const ObString &);
  static bool is_valid_charset(int64_t); };
"""+function('ObCharsetType ObCharset::charset_type(const ObString &cs_name)')+"\n"+function(
            'bool ObCharset::is_valid_charset(int64_t cs_type_int)')+"""
int main() {
  assert(ObCharset::charset_type({"utf8mb4"})==CHARSET_UTF8MB4);
  assert(ObCharset::charset_type({"UTF8MB4"})==CHARSET_UTF8MB4);
  assert(ObCharset::charset_type({"utf8"})==CHARSET_UTF8MB4);
  assert(ObCharset::charset_type({"utf8mb3"})==CHARSET_UTF8MB4);
  assert(ObCharset::charset_type({"binary"})==CHARSET_BINARY);
  assert(ObCharset::charset_type({"latin1"})==CHARSET_INVALID);
  assert(!ObCharset::is_valid_charset(CHARSET_LATIN1));
  assert(ObCharset::is_valid_charset(CHARSET_BINARY));
  assert(ObCharset::is_valid_charset(CHARSET_UTF8MB4));
}
"""
        cpp=self.base/'charset-registry.cpp';binary=self.base/'charset-registry'
        cpp.write_text(facade)
        subprocess.run(['g++','-std=c++17','-Wall','-Wextra','-Werror',str(cpp),'-o',str(binary)],
            check=True,capture_output=True,timeout=30)
        subprocess.run([str(binary)],check=True,capture_output=True,timeout=10)
        self.assertIn('return CHARSET_BINARY == charset_type || CHARSET_UTF8MB4 == charset_type;',source)
        registry=re.search(r'charset_wrap_arr_\[.*?\]\s*=\s*\{(.*?)\n\};',source,re.S).group(1)
        self.assertEqual(re.findall(r'\{(CHARSET_\w+),',registry),['CHARSET_BINARY','CHARSET_UTF8MB4'])
        self.assertIn('static const int64_t VALID_CHARSET_TYPES = 2;',header)
        positive=h.tokenize_sql()
        self.assertEqual(set(re.findall(r'USING (\w+)|CHARACTER SET (\w+)',positive)),
            {('utf8mb4',''),('','utf8mb4')})
        self.assertEqual(set(re.findall(r", '(ik|beng)',",positive)),{'ik','beng'})
        self.assertEqual(set(re.findall(r'"output": "([^"\n]+)"',positive)),{'all'})
        all_sql=positive+''.join(sql for _,sql,_,_ in h.tokenize_preflight())
        self.assertEqual(set(re.findall(r'USING (\w+)',all_sql)),{'utf8mb4','binary','latin1'})
        convert=(REPO/'src/sql/engine/expr/ob_expr_convert.cpp').read_text()
        self.assertIn('charset_type = ObCharset::charset_type(cs_name.trim())',convert)
        self.assertIn('ret = OB_ERR_UNKNOWN_CHARSET;',convert)
        resolver=(REPO/'src/sql/resolver/expr/ob_raw_expr_resolver_impl.cpp').read_text()
        self.assertIn('case T_CHAR_CHARSET:',resolver)
        self.assertIn('ObCharsetType charset_type = ObCharset::charset_type(charset_str);',resolver)
        self.assertIn('LOG_USER_ERROR(OB_ERR_UNKNOWN_CHARSET, charset_str.length(), charset_str.ptr());',resolver)
        self.assertIn('#define ER_UNKNOWN_CHARACTER_SET 1115',
            (REPO/'src/share/mysql_errno.h').read_text())

    def test_decoder_refuses_sql_null_wrong_empty_shape_and_bad_bytes(self):
        raw=self.token_rows()
        empty=b'{"tokens": [], "doc_len": 0}'
        for bad in (raw[:-1],raw.replace(b'\n',b'\r\n'),raw+b'\n',b'\xff'+raw,
                raw.replace(empty,b'NULL',1),raw.replace(empty,b'null',1),
                raw.replace(empty,b'[]',1),raw.replace(empty,b'{"tokens": [], "doc_len": true}',1),
                raw.replace(empty,b'{"tokens": [{"unexpected": 1}], "doc_len": 1}',1),
                raw.replace(empty,b'{"tokens": [], "doc_len": 1}',1),
                raw.replace(b'"alpha": 2',b'"": 2',1),
                raw.replace(b'"doc_len": 3',b'"doc_len": 3, "doc_len": 3',1),
                raw.replace(b'ik/empty',b'ik/null',1)):
            with self.subTest(bad=bad[:100]),self.assertRaises((ValueError,UnicodeError)):
                h.canonical_tokenize(bad)
        lines=raw.splitlines(True)
        with self.assertRaises(ValueError):h.canonical_tokenize(b''.join(lines[1:2]+lines[:1]+lines[2:]))

    def test_completed_reopens_sql_outputs_negative_proofs_and_both_sources(self):
        rows=self.completed_fixture()
        self.assertTrue(h.tokenize_complete(rows,DRIVER,self.root))
        for mutate in ('missing','hash','exit','output','sql','typed-null','no-negative','source','positive-sql','positive-SQL-NULL'):
            altered=copy.deepcopy(rows)
            directory=self.root/'validation/untimed/tokenize-candidate'
            proof=altered[1]['work']['preflight']['tokenize-negative-raw-null']
            changed=None
            if mutate=='missing':altered[1]['work']['preflight'].pop('tokenize-negative-raw-null')
            elif mutate=='hash':proof['sqlSha256']='0'*64
            elif mutate=='exit':proof['invocation']['exitCode']=0
            elif mutate=='no-negative':altered[1]['work'].pop('preflight')
            elif mutate=='source':altered[1]['work']['sourceCommit']=h.BASELINE
            else:
                filename={'output':'tokenize-negative-raw-null.log','sql':'tokenize-negative-raw-null.sql',
                    'typed-null':'tokenize-input.log','positive-sql':'tokenize.sql',
                    'positive-SQL-NULL':'tokenize.log'}[mutate]
                path=directory/filename;before=path.read_bytes();changed=(path,before)
                if mutate=='positive-SQL-NULL':
                    path.write_bytes(before.replace(b'{"tokens": [], "doc_len": 0}',b'NULL'))
                    altered[1]['work']['invocation']['outputSha256']=h.digest(path)
                else:path.write_bytes(before+b'extra')
            with self.subTest(mutate=mutate):self.assertFalse(h.tokenize_complete(altered,DRIVER,self.root))
            if changed:changed[0].write_bytes(changed[1])

    def test_collector_keeps_all_probe_sql_raw_logs_and_invocations_only(self):
        rows=self.completed_fixture()
        h.write_json(self.root/'report.json',dict(status='invalid_calibration',untimedVerdicts=rows))
        directory=self.root/'validation/untimed/tokenize-baseline'
        (directory/'tokenize-negative-unapproved.log').write_bytes(b'not allowlisted')
        manifest=h.collect_evidence(self.root,self.args.out)
        found={x['path']:x for x in manifest['files']}
        for role in ('baseline','candidate'):
            for name,*_ in h.tokenize_preflight():
                for suffix in ('.sql','.log','.log.json'):
                    relative='validation/untimed/tokenize-'+role+'/'+name+suffix
                    self.assertIn(relative,found)
                    self.assertEqual(found[relative]['sha256'],h.digest(self.root/relative))
        self.assertFalse(any('unapproved' in x for x in found))

    def test_null_charset_mode_signatures_are_pinned_to_real_engine_source(self):
        # Original A and archived B3 have byte-identical versions of these files.
        pins={
            'src/oblib/lib/charset/ob_charset.cpp':'4e1749c9eacea63161553a38baac18995db8164c6b418e805e6d4e800539f75e',
            'src/sql/engine/expr/ob_expr_convert.cpp':'cbd3724543385549650f5c4dfbf5684c8d260f46059f6004ee72e6e6c0517239',
            'src/sql/resolver/expr/ob_raw_expr_resolver_impl.cpp':'0d227a43708cae3a874caf4ae715b153acbf2d345b4f4254dd5ef1870b784af5',
            'src/sql/engine/expr/ob_expr_tokenize.cpp':'c722685d91119a41ca1f1e5cdbd047b612c1932f26a6ff2aef25adfc9cb169af',
            'src/sql/engine/expr/ob_expr_cast.cpp':'ae09302d045cc855daa79e972ece11e57d232e909f30d427e97b3cbb03b9082e',
            'src/sql/engine/expr/ob_expr_collation.cpp':'6c0fce50d3e7632ff34a15ec8255410460f6dfb7c45c28b5748c268ac55bafde',
            'src/sql/engine/expr/ob_datum_cast.cpp':'16539d9fc39081f897168b88456afd0522cb401dff1e0e6cf5cf775bc080bba4',
            'src/sql/parser/sql_parser_mysql_mode.y':'edcba6fc1f2349c048855db077a369891393564af23ec34b87e4315613c03b85',
            'src/storage/fts/dict/ob_gen_dic_loader.cpp':'e652616500baea120c1b1fd2dde738147a464c1837ca3923120593e5195017ef',
            'src/storage/fts/ob_fts_parser_helper.cpp':'deef82c8ce2e0fd123d823bb91b784f99f6d4e92998838f98c4438880308f11c'}
        for path,digest in pins.items():self.assertEqual(h.digest(REPO/path),digest)
        expr=(REPO/'src/sql/engine/expr/ob_expr_tokenize.cpp').read_text()
        self.assertIn('param_num < 1 || param_num > 3',expr)
        self.assertIn('param.fulltext_ = ObString::make_empty_string();',expr)
        self.assertIn('param.meta_.set_collation_type(expr.args_[0]->obj_meta_.get_collation_type());',expr)
        self.assertIn('param.try_load_dictionary_for_ik()',expr)
        self.assertIn('ObCharset::charset_type_by_coll(meta_.get_collation_type())',expr)
        self.assertIn('0 != param.fulltext_.length()',expr)
        self.assertIn('tokenize_helper.make_detail_json(token_map, doc_len, result)',expr)
        self.assertIn('case_compare("ALL")',expr)
        self.assertIn('cast func of null -> xxx is cast_eval_arg',
            (REPO/'src/sql/engine/expr/ob_datum_cast.cpp').read_text())
        self.assertIn('expr.args_[0]->datum_meta_.get_cs_type()',
            (REPO/'src/sql/engine/expr/ob_expr_collation.cpp').read_text())
        self.assertIn('| CHARACTER opt_string_length_i_v2 charset_key charset_name',
            (REPO/'src/sql/parser/sql_parser_mysql_mode.y').read_text())
        self.assertIn('case ObCharsetType::CHARSET_UTF8MB4:',
            (REPO/'src/storage/fts/dict/ob_gen_dic_loader.cpp').read_text())


class HybridPreparationFilesystem(unittest.TestCase):
    """Exercise prepare's real directories with the unchanged native mkdir contract."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source, self.root = self.base / 'source', self.base / 'root'
        scripts = self.source / h.PREFIX
        scripts.mkdir(parents=True)
        for name in h.DRIVER_FILES:
            shutil.copyfile(REPO / h.PREFIX / name, scripts / name)
        self.args = argparse.Namespace(source=self.source, root=self.root, driver_sha=DRIVER,
            kernel_sha=h.PRODUCTION_KERNEL, artifact_zip=self.base / 'never-opened.zip')
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.verify = stack.enter_context(patch.object(h, 'verify_driver', return_value={}))
        stack.enter_context(patch.object(h, 'source_identity',
            return_value=dict(tree=h.SOURCE_TREES['baseline'])))
        self.git = stack.enter_context(patch.object(h, 'git', side_effect=self.stub_git))
        self.loader = stack.enter_context(patch.object(h, 'prepare_verified_runtime',
            side_effect=self.stub_runtime))

    def stub_git(self, source, *arguments):
        self.assertEqual(source, self.source)
        if arguments[0] == 'show':
            commit, relative = arguments[1].split(':', 1)
            self.assertEqual(commit, DRIVER)
            return (source / relative).read_bytes()
        self.assertEqual(arguments, ('worktree', 'add', '--detach',
            str(self.args.root / 'sources/baseline'), h.BASELINE))
        (self.args.root / 'sources/baseline').mkdir()
        return b''

    def stub_runtime(self, archive, destination):
        self.assertEqual(archive, self.args.artifact_zip)
        self.assertFalse(archive.exists())
        binary = destination / 'candidate/seekdb'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'synthetic static evidence, never executed')
        record = dict(artifact={}, roles=dict(candidate=dict(binary=str(binary))))
        h.write_json(destination / 'verification.json', record)
        return record

    def harness_args(self):
        return argparse.Namespace(out=self.root / 'validation/untimed', source=self.source,
            binary=self.base / 'never-executed', obclient=self.base / 'never-used-client',
            mysqltest=self.base / 'never-used-mysqltest')

    def test_prepared_topology_allows_both_untimed_native_output_directories(self):
        h.prepare(self.args)
        harness = h.load_module('hybrid_prepared_filesystem_harness',
            self.root / 'driver/fts_contest_validation.py')
        args = self.harness_args()
        # Keep run_instance and its exclusive out.mkdir real. Stop at its first
        # command boundary, before any process, database, native init or SQL.
        with patch.object(harness.socket, 'socket'), \
                patch.object(harness, 'capture', return_value=dict(exitCode=1)) as start, \
                patch.object(harness, 'show_log'), patch.object(harness, 'emit'):
            for role in ('baseline', 'candidate'):
                name = 'tokenize-' + role
                self.assertFalse((args.out / name).exists())
                row = harness.run_instance(args, None, name, 7300,
                    lambda *_: self.fail('Untimed work must not execute in an offline test'))
                self.assertEqual(row['error'], 'Owned database start failed')
                self.assertTrue((args.out / name / 'result.json').is_file())
                self.assertFalse((args.out / name / 'database').exists())
            self.assertEqual(start.call_count, 2)
        self.loader.assert_called_once()

    def test_prepare_rejects_existing_directory_or_file_without_overwrite(self):
        for directory in (True, False):
            with self.subTest(directory=directory):
                if directory:
                    self.root.mkdir()
                    marker = self.root / 'keep'
                else:
                    marker = self.root
                marker.write_bytes(b'preserve existing bytes')
                with self.assertRaisesRegex(RuntimeError, 'fresh root'):
                    h.prepare(self.args)
                self.assertEqual(marker.read_bytes(), b'preserve existing bytes')
                marker.unlink()
                if directory:
                    self.root.rmdir()
        self.verify.assert_not_called()
        self.git.assert_not_called()
        self.loader.assert_not_called()

    def test_prepare_rejects_symlink_root_and_ancestor_before_writes(self):
        target = self.base / 'target'
        target.mkdir()
        marker = target / 'keep'
        marker.write_bytes(b'preserve target')
        self.root.symlink_to(target, target_is_directory=True)
        for root in (self.root, self.root / 'nested'):
            with self.subTest(root=root), self.assertRaisesRegex(RuntimeError, 'Symlink path forbidden'):
                self.args.root = root
                h.prepare(self.args)
        self.assertEqual(list(target.iterdir()), [marker])
        self.assertEqual(marker.read_bytes(), b'preserve target')
        self.verify.assert_not_called()
        self.git.assert_not_called()
        self.loader.assert_not_called()

    def test_prepare_file_ancestor_fails_without_git_or_runtime_loading(self):
        blocker = self.base / 'file'
        blocker.write_bytes(b'not a directory')
        self.args.root = blocker / 'root'
        with self.assertRaises(NotADirectoryError):
            h.prepare(self.args)
        self.assertEqual(blocker.read_bytes(), b'not a directory')
        self.git.assert_not_called()
        self.loader.assert_not_called()

    def test_native_untimed_outputs_still_reject_existing_directory_file_or_symlink(self):
        h.prepare(self.args)
        harness = h.load_module('hybrid_fresh_filesystem_harness',
            self.root / 'driver/fts_contest_validation.py')
        args = self.harness_args()
        target = self.base / 'target'
        target.write_bytes(b'preserve target')
        with patch.object(harness, 'capture') as start:
            for kind in ('directory', 'file', 'symlink'):
                for role in ('baseline', 'candidate'):
                    name = 'tokenize-' + role
                    out = args.out / name
                    if kind == 'directory':
                        out.mkdir()
                        (out / 'keep').write_bytes(b'preserve output')
                    elif kind == 'file':
                        out.write_bytes(b'preserve output')
                    else:
                        out.symlink_to(target)
                    with self.subTest(kind=kind, role=role), self.assertRaises(FileExistsError):
                        harness.run_instance(args, None, name, 7300, None)
                    if kind == 'directory':
                        self.assertEqual((out / 'keep').read_bytes(), b'preserve output')
                        (out / 'keep').unlink()
                        out.rmdir()
                    else:
                        self.assertEqual(out.read_bytes(), b'preserve target' if kind == 'symlink' else b'preserve output')
                        out.unlink()
            start.assert_not_called()
        self.assertEqual(target.read_bytes(), b'preserve target')

    def test_native_untimed_parent_file_conflict_fails_before_commands(self):
        h.prepare(self.args)
        harness = h.load_module('hybrid_conflict_filesystem_harness',
            self.root / 'driver/fts_contest_validation.py')
        args = self.harness_args()
        args.out.rmdir()
        args.out.write_bytes(b'preserve conflicting file')
        with patch.object(harness, 'capture') as start:
            for role in ('baseline', 'candidate'):
                with self.subTest(role=role), self.assertRaises(NotADirectoryError):
                    harness.run_instance(args, None, 'tokenize-' + role, 7300, None)
            start.assert_not_called()
        self.assertEqual(args.out.read_bytes(), b'preserve conflicting file')


class HistoricalHybridWorkflowContracts(unittest.TestCase):
    def workflow(self, text=None):
        # A strict projection of this workflow's indentation/schema, not a
        # general YAML parser. Full YAML parsing is a separate preparation check.
        # Keep the CI offline suite standard-library-only on a fresh runner.
        text = WORKFLOW.read_text() if text is None else text
        def scalar(value):
            value = value.strip().split(' #', 1)[0]
            if value in ('true', 'false'):
                return value == 'true'
            return value.strip("'\"")
        def fields(block, indentation):
            return {key: scalar(value) for key, value in re.findall(
                r'^' + ' ' * indentation + r'([A-Za-z0-9_-]+):[ ]+([^\n]+)$', block, re.MULTILINE)}
        global_permissions = text.split('permissions:\n', 1)[1].split('\n\n', 1)[0]
        job_text = text.split('jobs:\n', 1)[1]
        job_names = re.findall(r'^  ([A-Za-z0-9_-]+):$', job_text, re.MULTILINE)
        setup, step_text = job_text.split('    steps:\n', 1)
        permissions = setup.split('    permissions:\n', 1)[1].split('    env:\n', 1)[0]
        environment = setup.split('    env:\n', 1)[1]
        steps = []
        for block in step_text.split('      - name: ')[1:]:
            step = {'name': block.splitlines()[0]}
            if '        run: |\n' in block:
                step['run'] = textwrap.dedent(block.split('        run: |\n', 1)[1])
            else:
                step.update(fields(block, 8))
                if '        with:\n' in block:
                    step['with'] = fields(block.split('        with:\n', 1)[1], 10)
            steps.append(step)
        branches = re.search(r'^    branches: \[([^\n]+)\]$', text, re.MULTILINE)
        if not branches:
            raise AssertionError('Exact push branch declaration missing')
        return {'on': {'push': {'branches': [value.strip() for value in branches[1].split(',')]}},
            'permissions': fields(global_permissions, 2),
            'jobs': {name: dict(permissions=fields(permissions, 6), env=fields(environment, 6),
                steps=steps, **{'runs-on': fields(setup, 4).get('runs-on')}) for name in job_names}}

    def assert_contract(self, text):
        workflow=self.workflow(text)
        trigger=workflow.get('on',workflow.get(True))
        self.assertEqual(trigger, {'push':{'branches':['perf/fts-official-contract-20261108']}})
        self.assertEqual(workflow['permissions'], {'contents':'read'})
        self.assertEqual(list(workflow['jobs']), ['paired'])
        job=workflow['jobs']['paired']
        self.assertEqual(job['permissions'],{'contents':'read','actions':'read'})
        self.assertEqual(job['env']['HYBRID_KERNEL_SHA'], h.PRODUCTION_KERNEL)
        steps=job['steps']
        runs='\n'.join(x.get('run','') for x in steps)
        self.assertEqual(runs.count('--build build_release --target seekdb -j2'),1)
        self.assertNotIn('--role baseline',runs)
        self.assertNotIn('switch-candidate',runs)
        self.assertEqual(runs.count('./build.sh release --init'),1)
        order=[runs.index(token) for token in ('--phase prepare-reuse','./build.sh release --init',
            '--phase prebuild','--build build_release --target seekdb -j2','--phase snapshot --role candidate',
            '--phase preserve-candidate','--phase validate')]
        self.assertEqual(order,sorted(order))
        self.assertLess(runs.index("-p 'test_fts*.py'"), runs.index('--phase prepare-reuse'))
        downloads=[x for x in steps if x.get('uses','').startswith('actions/download-artifact@')]
        self.assertEqual(len(downloads),1)
        self.assertEqual(downloads[0]['uses'],'actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333')
        self.assertEqual(downloads[0]['with'],dict({'artifact-ids':'13621468966','run-id':'42486120779',
            'repository':'beifeng-h/nailong-seekdb','github-token':'${{ github.token }}',
            'path':'${{ runner.temp }}/seekdb-original-artifact','skip-decompress':True,'digest-mismatch':'error'}))
        self.assertIn('historical B without execution',text)
        self.assertIn('\"$GITHUB_SHA\" \"$HYBRID_KERNEL_SHA\" 5bce9e06d1281347ad61344e4b01fab9158c58a2 fe876f8f805969970621a18a1cf812ff322fc78c',runs)
        for forbidden in ('13608812363','secrets.','printenv','set -x','actions: write','curl api.github.com'):
            self.assertNotIn(forbidden,text)
        self.assertEqual(sum('runs-on' in x for x in workflow['jobs'].values()),1)

    def test_historical_hybrid_workflow_positive_contract(self):
        self.assert_contract(WORKFLOW.read_text())

    def test_historical_hybrid_rejects_extra_build_rebuild_A_wrong_artifact_expanded_permission_or_kernel(self):
        text=WORKFLOW.read_text()
        for bad in (text.replace('--build build_release --target seekdb -j2','--build build_release --target seekdb -j2; cmake --build build_release --target seekdb -j2'),
                    text.replace('--role candidate','--role baseline'),text.replace('13621468966','13608812363'),
                    text.replace('actions: read','actions: write'),text.replace(h.PRODUCTION_KERNEL,legacy.KERNEL),
                    text.replace(' fe876f8f805969970621a18a1cf812ff322fc78c','')):
            with self.assertRaises(AssertionError): self.assert_contract(bad)

    def test_every_historical_Bash_step_parses(self):
        for step in self.workflow()['jobs']['paired']['steps']:
            if 'run' in step:
                result=subprocess.run(['bash','-n'],input=step['run'],text=True,capture_output=True)
                self.assertEqual(result.returncode,0,(step['name'],result.stderr))

    def run_checkout(self, **overrides):
        first=self.workflow()['jobs']['paired']['steps'][0]['run']
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); stub=root/'git'
            stub.write_text(textwrap.dedent('''\
                #!/usr/bin/env python3
                import json, os, sys
                a=sys.argv[1:]
                with open(os.environ['TRACE'],'a') as f:f.write(json.dumps(a)+'\\n')
                while a and a[0] in ('-c','-C'): a=a[2:]
                if a[0]=='fetch':sys.exit(int(os.environ.get('FETCH_EXIT','0')))
                if a[0]=='cat-file' and a[-1]==os.environ.get('MISSING','')+'^{commit}':sys.exit(72)
                if a[0]=='rev-parse':print(os.environ.get('HEAD',os.environ['GITHUB_SHA']))
                if a[0]=='status':print(os.environ.get('DIRTY',''),end='')
                '''));stub.chmod(0o755)
            env=dict(os.environ,PATH=str(root)+os.pathsep+os.environ['PATH'],TRACE=str(root/'trace'),
                GITHUB_WORKSPACE=str(root/'workspace'),RUNNER_TEMP=str(root),GITHUB_ENV=str(root/'env'),
                HYBRID_KERNEL_SHA=h.PRODUCTION_KERNEL,GITHUB_SHA=DRIVER,GITHUB_SERVER_URL='https://github.com',
                GITHUB_REPOSITORY='beifeng-h/nailong-seekdb')
            env.update(overrides)
            result=subprocess.run(['bash','-c',first],env=env,text=True,capture_output=True)
            calls=[json.loads(x) for x in (root/'trace').read_text().splitlines()] if (root/'trace').exists() else []
            return result,calls

    def test_atomic_checkout_exact_transaction_before_any_checkout(self):
        result,calls=self.run_checkout(); self.assertEqual(result.returncode,0,result.stderr)
        fetch=[c for c in calls if 'fetch' in c]
        self.assertEqual(len(fetch),1)
        self.assertEqual(fetch[0][-4:],[DRIVER,h.PRODUCTION_KERNEL,h.BASELINE,'fe876f8f805969970621a18a1cf812ff322fc78c'])
        for flag in ('--atomic','--no-auto-maintenance','--no-tags','--depth=1'):
            self.assertIn(flag,fetch[0])
        self.assertEqual(len([c for c in calls if 'cat-file' in c]),4)
        self.assertGreater(next(i for i,c in enumerate(calls) if 'checkout'in c),max(i for i,c in enumerate(calls) if 'cat-file'in c))

    def test_checkout_failures_stop_without_retries_or_expensive_work(self):
        for overrides in (dict(HYBRID_KERNEL_SHA='bad'),dict(FETCH_EXIT='71'),dict(MISSING=h.PRODUCTION_KERNEL),
                          dict(HEAD='f'*40),dict(DIRTY='?? unexpected')):
            result,calls=self.run_checkout(**overrides)
            self.assertNotEqual(result.returncode,0)
            self.assertLessEqual(len([c for c in calls if 'fetch'in c]),1)
            if any(k in overrides for k in ('HYBRID_KERNEL_SHA','FETCH_EXIT','MISSING')):
                self.assertFalse(any('checkout'in c for c in calls))


class HybridFourFlagProof(unittest.TestCase):
    def setUp(self):
        proof_fixtures.PlanMetadataCompatibility.setUpClass()
        self.fixture=proof_fixtures.PlanMetadataCompatibility(methodName='test_positive_real_pinned_fixture_complete_four_flag_proof')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f=self.fixture
        raw=f.row
        h.write_json(f.out/'result.json',raw)
        self.context=dict(sourceCommit=DRIVER, validationDriverCommit=DRIVER, kernelOrigin=h.PRODUCTION_KERNEL,
            source=str(f.source),out=str(f.out),nativeResult=dict(path=str(f.out/'result.json'),
                bytes=(f.out/'result.json').stat().st_size,sha256=h.digest(f.out/'result.json')))
        self.proof=dict(f.classify(),validationContext=self.context)
        self.row=dict(raw,role='candidate',validationContext=self.context)

    def test_new_source_exact_four_flags_accepted_separately_native_remains_FAIL(self):
        before=copy.deepcopy(self.row)
        self.assertTrue(h.accepted_compatibility(self.proof,self.row,DRIVER,DRIVER))
        self.assertEqual(self.row,before)
        self.assertEqual(self.row['status'],'failed')
        self.assertEqual(self.proof['actualReject']['sha256'],h.REJECT_SHA256)

    def test_old_B_or_wrong_kernel_driver_or_native_result_binding_rejected(self):
        for key,bad in (('sourceCommit',legacy.CANDIDATE),('validationDriverCommit',legacy.CANDIDATE),
                        ('kernelOrigin',legacy.KERNEL),('out','/stale/out')):
            row=copy.deepcopy(self.row);proof=copy.deepcopy(self.proof)
            row['validationContext'][key]=bad;proof['validationContext'][key]=bad
            self.assertFalse(h.accepted_compatibility(proof,row,DRIVER,DRIVER))
        (self.fixture.out/'result.json').write_text('{}')
        self.assertFalse(h.accepted_compatibility(self.proof,self.row,DRIVER,DRIVER))

    def test_current_full_bytes_rechecked_cannot_accept_stale_cached_proof(self):
        self.fixture.reject.write_bytes(self.fixture.compatible+b'\n')
        self.assertFalse(h.accepted_compatibility(self.proof,self.row,DRIVER,DRIVER))

    def test_simple_native_PASS_or_omitted_case_never_waives_expected_failure(self):
        row=copy.deepcopy(self.row);row['status']='passed';row['work']['passed']=True
        self.assertFalse(h.accepted_compatibility(self.proof,row,DRIVER,DRIVER))
        self.assertFalse(h.case_evidence_complete([],{},DRIVER,{}))


if __name__=='__main__': unittest.main()
