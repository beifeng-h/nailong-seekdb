"""Offline regressions for the cs45 proof and reused validation callbacks."""
import argparse
import copy
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import fts_cs45_equality_validation as cs45


class SourceProofTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name)
        self.args = argparse.Namespace(source=self.source, driver_sha='c' * 40)
        for name in cs45.driver.CASES:
            suite, case = name.split('.')
            for directory, suffix in (('t', '.test'), ('r/mysql', '.result')):
                path = self.source / 'tools/deploy/mysql_test/test_suite' / suite / directory / (case + suffix)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'unchanged native input\n')

    def proof(self, changed):
        def git(source, *args):
            if args[0] == 'rev-parse':
                return cs45.BASE_TREE.encode()
            if args[0] == 'diff':
                return ('\n'.join(changed)).encode() if '--name-only' in args else b'actual cpp patch'
            if args[0] == 'show':
                commit, relative = args[1].split(':', 1)
                return b'unchanged native input\n' if commit == cs45.BASE else b'github tool blob'
            raise AssertionError(args)
        with patch.object(cs45.common, 'source_identity', return_value={'tree': 'd' * 40}), \
                patch.object(cs45.common, 'git', side_effect=git), \
                patch.object(cs45.driver, 'inventory', return_value={'original': 'hash'}), \
                patch.object(cs45.driver, 'git_identity', return_value='verified author and committer'):
            return cs45.source_proof(self.args)

    def test_single_charset_change_and_existing_profile_tools_are_allowed(self):
        tools = ['.github/script/seekdb/fts-profile-runtime/run_profile.py',
                 '.github/workflows/fts-profile-recording-analysis.yml',
                 '.github/script/seekdb/fts_cs45_equality_validation.py']
        proof = self.proof([cs45.PRODUCT] + tools)
        self.assertEqual(proof['productChanges'], [cs45.PRODUCT])
        self.assertEqual(set(proof['ciBlobSha256']), set(tools))
        self.assertEqual(proof['baselineCommit'], cs45.BASE)
        self.assertEqual(proof['kernelCommit'], self.args.driver_sha)
        self.assertEqual(cs45.driver.BASE_ELF_SHA, cs45.BASE_ELF_SHA)

    def test_other_product_changes_and_missing_kernel_are_rejected(self):
        for paths in ([cs45.PRODUCT, 'src/sql/extra.cpp'], ['.github/workflows/new.yml']):
            with self.subTest(paths=paths), self.assertRaises(RuntimeError):
                self.proof(paths)

    def test_changed_selected_native_input_is_rejected(self):
        case = cs45.driver.CASES[0].split('.')[1]
        path = self.source / 'tools/deploy/mysql_test/test_suite/fts_index/t' / (case + '.test')
        path.write_bytes(b'changed native input')
        with self.assertRaises(RuntimeError):
            self.proof([cs45.PRODUCT])


class CallbackTests(unittest.TestCase):
    def test_original_benchmark_and_same_plan_expectations_for_both_roles(self):
        args = argparse.Namespace(source=Path('/candidate'), root=Path('/owned'), driver_sha='c' * 40)
        identities = {role: {'binary': '/runtime/' + role} for role in ('baseline', 'candidate')}
        queries = {'query_cn': 'SELECT 1', 'query_limit': 'SELECT 2',
                   'control_score_projection': 'SELECT 3'}
        metrics = {'original-metrics': 1}
        for role in identities:
            def sql(call, out, label, query):
                return 'false' if label == 'query_cn-explain' else 'true'
            with patch.object(cs45.common, 'benchmark_work', return_value=lambda call, out:
                              {'passed': True, 'metrics': metrics}) as factory, \
                    patch.object(cs45.common, 'queries_from_benchmark', return_value={}), \
                    patch.object(cs45.common, 'plan_queries', return_value=queries), \
                    patch.object(cs45.driver, 'sql', side_effect=sql), \
                    patch.object(cs45.common, 'plan_relevance', side_effect=lambda text:
                                 {'actualCalcRelevance': text == 'true'}):
                result = cs45.benchmark_callback(args, identities, role, {}, None)(None, Path('/output'), 42)
            self.assertIs(result['metrics'], metrics)
            self.assertFalse(factory.call_args.args[3])
            self.assertEqual(factory.call_args.args[0].role, role)
            self.assertFalse(result['roleConsistentPlans']['query_cn']['actualCalcRelevance'])

    def test_failed_benchmark_does_not_run_explain(self):
        args = argparse.Namespace(source=Path('/candidate'), root=Path('/owned'), driver_sha='c' * 40)
        with patch.object(cs45.common, 'benchmark_work', return_value=lambda call, out:
                          {'passed': False, 'failureKind': 'benchmark_process_failure'}), \
                patch.object(cs45.driver, 'sql') as sql:
            result = cs45.benchmark_callback(args, {'candidate': {'binary': '/elf'}},
                                             'candidate', {}, None)(None, Path('/output'), 42)
        self.assertFalse(result['passed'])
        sql.assert_not_called()

    def test_two_abba_pairs_use_original_scorer_without_four_pair_gate(self):
        scorer = SimpleNamespace(check_config=lambda *args: None,
            calculate_score=lambda metrics, reference:
            {'mean_improvement': metrics['value'], 'categories': {'build': metrics['value']}})
        rounds = [dict(name=name, metrics={'value': value}) for (name, role), value in
                  zip(cs45.SCHEDULE, (0, 0.1, 0.3, 0))]
        result = cs45.pair_summary(rounds, {}, scorer)
        self.assertEqual(len(result['pairs']), 2)
        self.assertAlmostEqual(result['meanUnboundedImprovement'], 0.2)
        self.assertFalse(result['statisticalSignificanceClaimed'])

    def test_native_pass_is_accepted_without_requiring_old_fail(self):
        row = {'status': 'passed', 'cleanupPassed': True, 'work': {'passed': True}}
        before = copy.deepcopy(row)
        with patch.object(cs45.common, 'require_instance') as require:
            self.assertIsNone(cs45.native_verdict('candidate', 'fts_index.simple_query', row, None, None))
        require.assert_called_once_with(row)
        self.assertEqual(row, before)

    def test_baseline_native_failure_requires_exact_archived_reject(self):
        row = {'status': 'failed', 'cleanupPassed': True,
               'work': {'passed': False, 'inputsUnchanged': True, 'exitCode': 1}}
        before = copy.deepcopy(row)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / 'mysqltest-log').mkdir()
            (out / 'mysqltest-log/simple_query.reject').write_bytes(b'original reject')
            digest = '737c4257fabf57dbd5365fdd04cfe2573206129d19991a8a1616e7e3db1e4896'
            with patch.object(cs45.common, 'digest', return_value=digest):
                proof = cs45.native_verdict('baseline', 'fts_index.simple_query', row, None, out)
            self.assertTrue(proof['accepted'])
            with patch.object(cs45.common, 'digest', return_value='changed'), self.assertRaises(RuntimeError):
                cs45.native_verdict('baseline', 'fts_index.simple_query', row, None, out)
        self.assertEqual(row, before)

    def test_candidate_failure_preserves_original_classifier_instance_name(self):
        case = 'fts_index.simple_query'
        original_verdict = cs45.native_verdict
        names = []
        class ReachedCandidateClassifier(Exception):
            pass
        def classifier(**arguments):
            self.assertEqual(arguments['row']['name'], case)
            self.assertEqual(arguments['case_name'], case)
            return {'accepted': True, 'completeBytesCompared': True}
        def verdict(role, name, row, source, out):
            if role == 'candidate':
                original_verdict(role, name, row, source, out)
                raise ReachedCandidateClassifier
        def run_owned(args, report, identities, role, name, work, **kwargs):
            names.append(name)
            (args.root / 'validation' / name).mkdir(parents=True)
            return {'name': name, 'role': role, 'cleanupPassed': True,
                    'status': 'passed' if name.startswith('equality-') else 'failed',
                    'work': {'passed': True, 'canonical': '00'}}
        native = SimpleNamespace(discover_cases=lambda source: [SimpleNamespace(name=case)])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            baseline = source / 'tools/benchmark/fts_large_bench_baseline.json'
            baseline.parent.mkdir(parents=True)
            cs45.driver.dump(baseline, {})
            cs45.driver.dump(root / 'report.json', {'status': 'prepared', 'nativeCases': []})
            for role in ('baseline', 'candidate'):
                path = root / 'runtime' / role / 'identity.json'
                path.parent.mkdir(parents=True)
                cs45.driver.dump(path, {'binary': '/elf', 'sha256': 'hash', 'bytes': 1, 'buildId': 'id'})
            args = SimpleNamespace(root=root, source=source, driver_sha='c' * 40)
            with patch.object(cs45.driver, 'load_plan'), patch.object(cs45.driver, 'safe_elf'), \
                    patch.object(cs45.driver, 'check_baseline_targeted'), \
                    patch.object(cs45.common, 'load_module'), \
                    patch.object(cs45.driver, 'CASES', (case,)), \
                    patch.object(cs45.driver.native_harness, 'load_native', return_value=native), \
                    patch.object(cs45.driver.native_harness, 'original_case'), \
                    patch.object(cs45.driver, 'run_owned', side_effect=run_owned), \
                    patch.object(cs45, 'native_verdict', side_effect=verdict), \
                    patch.object(cs45.driver, 'classify_plan_metadata_compatibility', side_effect=classifier), \
                    self.assertRaises(ReachedCandidateClassifier):
                cs45.validate(args)
        self.assertEqual(names, ['native-baseline-' + case, 'equality-baseline', case])

    def test_equality_callback_preserves_complete_stdout_bytes(self):
        labels = re.findall(r"^SELECT '([^']+)'", cs45.REGRESSION_SQL, re.M)
        raw = ''.join(label + '\t0\n' for label in labels)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            def sql(call, directory, label, query):
                (directory / (label + '.sql')).write_text(query, encoding='utf-8')
                (directory / (label + '.log')).write_bytes(raw.encode())
                return raw
            with patch.object(cs45.driver, 'sql', side_effect=sql):
                result = cs45.equality_work(None, out)
        self.assertEqual(bytes.fromhex(result['canonical']), raw.encode())
        self.assertEqual(result['outputBytes'], len(raw.encode()))

    def test_process_local_nofile_preserves_hard_limit(self):
        for before, expected in (((1024, 1048576), (8192, 1048576)),
                                 ((16384, 1048576), (16384, 1048576)), ((-1, -1), (-1, -1))):
            state = [before]
            resource = SimpleNamespace(RLIMIT_NOFILE=7, RLIM_INFINITY=-1,
                getrlimit=lambda kind: state[0],
                setrlimit=lambda kind, value: state.__setitem__(0, value))
            with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, resource=resource):
                root = Path(tmp)
                (root / 'evidence').mkdir()
                cs45.prepare_nofile(SimpleNamespace(root=root, phase='validate'))
                self.assertEqual(state[0], expected)


if __name__ == '__main__':
    unittest.main()
