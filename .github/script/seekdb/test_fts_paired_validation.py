"""Synthetic paired-contract tests. Never builds or starts SQL; one Linux test owns synthetic subprocesses."""
import argparse
from contextlib import ExitStack, redirect_stdout
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import time
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import fts_paired_validation as p

REPO = Path(__file__).resolve().parents[3]
DRIVER = 'a' * 40
TREE = 'b' * 40
BASELINE = json.loads((REPO / (p.BENCH + '_baseline.json')).read_text(encoding='utf-8'))
SCORER = p.load_module('test_original_scorer', REPO / (p.BENCH + '_score.py'))


class Contracts(unittest.TestCase):
    def setUp(self):
        signals = patch.object(p.signal, 'signal')
        signals.start()
        self.addCleanup(signals.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'owned'
        self.source = Path(self.tmp.name) / 'source'
        self.source.mkdir()
        self.root.mkdir()
        self.args = argparse.Namespace(root=self.root, source=self.source, driver_sha=DRIVER, role='candidate')
        self.quiet = redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def identity(self, role='candidate'):
        binary = self.root / 'runtime' / role / 'seekdb'
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b'\x7fELF' + role.encode() + (p.BASELINE if role == 'baseline' else DRIVER).encode())
        return dict(role=role, sourceCommit=p.BASELINE if role == 'baseline' else DRIVER,
                    sourceTree=TREE, kernelOrigin=p.BASELINE if role == 'baseline' else p.KERNEL,
                    driverCommit=DRIVER, binary=str(binary), bytes=binary.stat().st_size,
                    sha256=p.digest(binary), buildId='abcdef', officialInputHashes={'official': 'fixed'})

    def metrics(self, factor=1):
        metrics = dict(BASELINE['baseline_metrics'], **p.CONFIG, git_dirty=0)
        for names in BASELINE['score_categories'].values():
            for name in names:
                metrics[name] *= factor
        return metrics

    def report_file(self, fields=None):
        data = dict(label='candidate-' + DRIVER[:12], git_head=DRIVER, **self.metrics())
        if fields:
            data.update(fields)
        path = self.root / 'metrics.txt'
        path.write_text(''.join('%s: %s\n' % item for item in data.items()), encoding='utf-8')
        return path

    def saved_plan(self):
        plan = p.plan_for(self.source, self.root, DRIVER)
        plan['driverFiles'] = {}
        for name in p.DRIVER_FILES:
            path = self.root / 'driver' / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('synthetic ' + name)
            plan['driverFiles'][name] = p.digest(path)
        p.write_json(self.root / 'plan.json', plan)
        return plan

    def test_source_exact_clean_and_tree(self):
        with patch.object(p, 'git', side_effect=[DRIVER.encode(), b'', TREE.encode()]) as git:
            self.assertEqual(p.source_identity(self.source, DRIVER), dict(commit=DRIVER, tree=TREE))
            self.assertIn('--untracked-files=all', git.call_args_list[1].args)

    def test_source_rejects_wrong_commit_dirty_staged_untracked(self):
        for head, status in [(p.BASELINE, b''), (DRIVER, b' M src/a'), (DRIVER, b'M  src/a'), (DRIVER, b'?? new')]:
            with self.subTest(head=head, status=status), patch.object(p, 'git', side_effect=[head.encode(), status]):
                with self.assertRaises(RuntimeError):
                    p.source_identity(self.source, DRIVER)

    def test_driver_allows_only_exact_ci_paths(self):
        with patch.object(p, 'git', side_effect=['\n'.join(p.ALLOWED_DRIVER_PATHS).encode(), b'']):
            self.assertEqual(p.verify_driver(self.source, DRIVER)['kernelOrigin'], p.KERNEL)
        for forbidden in ['src/sql/executor.cpp', p.PREFIX + 'fts_contest_validation.py', p.PREFIX + 'another_test.py', 'tools/benchmark/fts_large_bench.sh']:
            with self.subTest(path=forbidden), patch.object(p, 'git', return_value=forbidden.encode()):
                with self.assertRaises(RuntimeError):
                    p.verify_driver(self.source, DRIVER)
        with patch.object(p, 'git', side_effect=[b'', b'build.sh']):
            with self.assertRaises(RuntimeError):
                p.verify_driver(self.source, DRIVER)
        with self.assertRaises(RuntimeError):
            p.verify_driver(self.source, '8db524b')

    def test_source_switch_verifies_before_and_after(self):
        with patch.object(p, 'source_identity', side_effect=[{'commit': DRIVER}, {'commit': p.BASELINE}]) as identity, patch.object(p, 'git') as git:
            self.assertEqual(p.switch_source(self.source, DRIVER, p.BASELINE)['commit'], p.BASELINE)
            self.assertEqual(identity.call_args_list[0].args, (self.source, DRIVER))
            self.assertEqual(identity.call_args_list[1].args, (self.source, p.BASELINE))
            git.assert_called_once_with(self.source, 'checkout', '--detach', p.BASELINE)
        with patch.object(p, 'source_identity', side_effect=RuntimeError('dirty')), patch.object(p, 'git') as git:
            with self.assertRaises(RuntimeError):
                p.switch_source(self.source, DRIVER, p.BASELINE)
            git.assert_not_called()
        with patch.object(p, 'source_identity', side_effect=[{}, RuntimeError('wrong target')]), patch.object(p, 'git'):
            with self.assertRaises(RuntimeError):
                p.switch_source(self.source, DRIVER, p.BASELINE)

    def test_identity_binds_role_commit_kernel_tree_bytes_hash_build_id(self):
        for role in ('baseline', 'candidate'):
            identity = self.identity(role)
            commit = identity['sourceCommit']
            self.assertEqual(p.validate_identity(identity, role, commit, self.root), Path(identity['binary']))
            for key, value in [('role', 'other'), ('sourceCommit', 'c' * 40), ('kernelOrigin', 'd' * 40), ('sourceTree', 'bad'), ('bytes', 0), ('sha256', '0' * 64), ('buildId', '')]:
                with self.subTest(role=role, key=key), self.assertRaises(RuntimeError):
                    p.validate_identity(dict(identity, **{key: value}), role, commit, self.root)

    def test_baseline_candidate_confusion_and_wrong_path(self):
        baseline = self.identity('baseline')
        candidate = self.identity('candidate')
        with self.assertRaises(RuntimeError):
            p.validate_identity(baseline, 'candidate', DRIVER, self.root)
        for binary in [baseline['binary'], 'runtime/candidate/seekdb', str(self.root / 'missing')]:
            with self.subTest(binary=binary), self.assertRaises(RuntimeError):
                p.validate_identity(dict(candidate, binary=binary), 'candidate', DRIVER, self.root)
        Path(candidate['binary']).write_bytes(b'not ELF')
        with self.assertRaises(RuntimeError):
            p.validate_identity(candidate, 'candidate', DRIVER, self.root)

    def test_identity_detects_post_snapshot_mutation(self):
        identity = self.identity()
        with Path(identity['binary']).open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaises(RuntimeError):
            p.validate_identity(identity, 'candidate', DRIVER, self.root)

    def test_identity_rejects_hardlink(self):
        identity = self.identity()
        os.link(identity['binary'], self.root / 'alias')
        with self.assertRaises(RuntimeError):
            p.validate_identity(identity, 'candidate', DRIVER, self.root)

    def test_loader_success_missing_dependency_nonzero_and_unclean(self):
        log = self.root / 'ldd.log'
        for text, code, clean, valid in [('libc => found', 0, True, True), ('libbad => not found', 0, True, False), ('error', 1, True, False), ('found', 0, False, False)]:
            log.write_text(text)
            with self.subTest(text=text, code=code, clean=clean), patch.object(p, 'capture_group', return_value=dict(exitCode=code, childrenCleaned=clean)):
                if valid:
                    self.assertEqual(p.verify_loader('seekdb', log, self.source, {})['exitCode'], 0)
                else:
                    with self.assertRaises(RuntimeError):
                        p.verify_loader('seekdb', log, self.source, {})
        with patch.object(p, 'capture_group', side_effect=FileNotFoundError('ldd missing')):
            with self.assertRaises(FileNotFoundError):
                p.verify_loader('seekdb', log, self.source, {})

    def test_capture_group_preserves_nonzero_and_error_log(self):
        process = Mock(pid=123, returncode=1)
        with patch.object(p.subprocess, 'Popen', return_value=process) as launch, patch.object(p.os, 'killpg', side_effect=ProcessLookupError, create=True):
            row = p.capture_group(['fake'], self.root / 'error.log', self.source, 3)
        self.assertEqual(row['exitCode'], 1)
        self.assertTrue(row['childrenCleaned'])
        self.assertTrue(launch.call_args.kwargs['start_new_session'])
        self.assertEqual(json.loads((self.root / 'error.log.json').read_text())['exitCode'], 1)

    def test_capture_group_timeout_not_pass_and_kills_owned_group(self):
        process = Mock(pid=123, returncode=-9)
        process.communicate.side_effect = [subprocess.TimeoutExpired('fake', 1), None]
        with patch.object(p.subprocess, 'Popen', return_value=process), patch.object(p.os, 'killpg', create=True) as kill, patch.object(p.signal, 'SIGKILL', 9, create=True):
            row = p.capture_group(['fake'], self.root / 'timeout.log', self.source, 1)
        self.assertEqual(row['exitCode'], 124)
        self.assertFalse(row['childrenCleaned'])
        self.assertTrue(row['timedOut'])
        self.assertEqual([call.args[1] for call in kill.call_args_list], [0, p.signal.SIGTERM, 9])

    def test_strict_report_positive(self):
        result = p.strict_report(self.report_file(), 'candidate', DRIVER, BASELINE)
        self.assertEqual(result['query_cn_hits'], 8001)

    def test_strict_report_rejects_missing_and_duplicate_metrics(self):
        path = self.report_file()
        original = path.read_text()
        path.write_text('\n'.join(line for line in original.splitlines() if not line.startswith('query_cn_avg_ms:')))
        with self.assertRaises(RuntimeError):
            p.strict_report(path, 'candidate', DRIVER, BASELINE)
        path.write_text(original + 'query_cn_hits: 8001\n')
        with self.assertRaises(RuntimeError):
            p.strict_report(path, 'candidate', DRIVER, BASELINE)

    def test_strict_report_rejects_invalid_metrics_hits_config_role_and_commit(self):
        bad = [('query_cn_avg_ms', value) for value in ['NaN', 'inf', '-inf', -1, 0, 'N/A']]
        bad += [('raw_load_sec', 0), ('raw_load_rows_per_sec', 0), ('select1_avg_ms', 0), ('query_cn_hits', 8000), ('query_limit_hits', 20.5), ('rows', 1), ('skip_load', 1), ('git_dirty', 1), ('label', 'baseline-' + DRIVER[:12]), ('git_head', p.BASELINE), ('git_head', DRIVER[:6])]
        for key, value in bad:
            with self.subTest(key=key, value=value), self.assertRaises(RuntimeError):
                p.strict_report(self.report_file({key: value}), 'candidate', DRIVER, BASELINE)

    def test_error_log_cannot_be_report(self):
        path = self.root / 'error.txt'
        path.write_text('ERROR: connection failed\nquery_cn_hits: 8001\nscore: 100\n')
        with self.assertRaises(RuntimeError):
            p.strict_report(path, 'candidate', DRIVER, BASELINE)

    def test_exact_four_original_query_literals(self):
        benchmark = REPO / (p.BENCH + '.sh')
        before = p.digest(benchmark)
        queries = p.queries_from_benchmark(benchmark)
        expected = {
            'query_cn': "SELECT COUNT(*) FROM fts_large_bench.docs WHERE MATCH(title, content) AGAINST('\u5168\u6587\u7d22\u5f15 分词器');",
            'query_beng': "SELECT COUNT(*) FROM fts_large_bench.docs WHERE MATCH(title_en, content_en) AGAINST('database performance audit');",
            'query_mixed': "SELECT COUNT(*) FROM fts_large_bench.docs WHERE MATCH(title, content) AGAINST('OceanBase 稳定 database');",
            'query_limit': "SELECT COUNT(*) FROM (SELECT id FROM fts_large_bench.docs WHERE MATCH(content) AGAINST('\u5012\u6392\u7d22\u5f15 tokenizer') LIMIT 20) t;",
        }
        self.assertEqual(queries, expected)
        self.assertEqual(p.digest(benchmark), before)

    def test_query_extraction_rejects_missing_duplicate_nonliteral(self):
        text = (REPO / (p.BENCH + '.sh')).read_text(encoding='utf-8')
        for altered in [text.replace('run_query_bench "query_cn"', 'run_query_bench "wrong"'), text + '\n' + text, text.replace("AGAINST('database performance audit')", "AGAINST('$variable')")]:
            path = self.root / 'bench.sh'
            path.write_text(altered, encoding='utf-8')
            with self.assertRaises(RuntimeError):
                p.queries_from_benchmark(path)

    def pairs(self, factors):
        return [dict(baseline=a, candidate=b, baselineMetrics=self.metrics(), candidateMetrics=self.metrics(factor)) for (a, b), factor in zip(p.PAIRS, factors)]

    def test_performance_positive_negative_and_inconclusive(self):
        for factor, expected in [(0.9, 'improvement_established'), (1.1, 'regression'), (1, 'inconclusive'), (0.981, 'inconclusive'), (1.019, 'inconclusive'), (3, 'regression')]:
            with self.subTest(factor=factor):
                result = p.performance(self.pairs([factor] * 4), BASELINE, SCORER)
                self.assertEqual(result['classification'], expected)
                self.assertAlmostEqual(result['meanUnboundedImprovement'], 1 - factor)
                self.assertFalse(result['formalContestScore'])
                if factor == 3:
                    self.assertEqual(result['pairs'][0]['diagnosticClampedScore'], 0)
                    self.assertEqual(result['meanUnboundedImprovement'], -2)

    def test_performance_variability_and_invalid_pair_count(self):
        self.assertEqual(p.performance(self.pairs([0.6, 1.2, 0.7, 1.1]), BASELINE, SCORER)['classification'], 'inconclusive')
        with self.assertRaises(RuntimeError):
            p.performance(self.pairs([1, 1, 1]), BASELINE, SCORER)
        fake = Mock()
        fake.calculate_score.return_value = dict(mean_improvement=float('nan'), categories={}, score=0)
        with self.assertRaises(RuntimeError):
            p.performance(self.pairs([1] * 4), BASELINE, fake)

    def test_require_instance_never_accepts_cleanup_failure_or_error(self):
        p.require_instance(dict(status='passed', cleanupPassed=True))
        for row in [{}, dict(status='failed', cleanupPassed=True), dict(status='infrastructure_error', cleanupPassed=True), dict(status='passed', cleanupPassed=False), dict(status='passed', cleanupPassed=1)]:
            with self.subTest(row=row), self.assertRaises(RuntimeError):
                p.require_instance(row)

    def test_plan_has_complete_eight_run_abba_baab_and_adjacent_pairs(self):
        plan = p.plan_for(self.source, self.root, DRIVER)
        self.assertEqual([(row['name'], row['role']) for row in plan['schedule']], [('A1', 'baseline'), ('B1', 'candidate'), ('B2', 'candidate'), ('A2', 'baseline'), ('B3', 'candidate'), ('A3', 'baseline'), ('A4', 'baseline'), ('B4', 'candidate')])
        self.assertEqual(plan['pairs'], [dict(baseline='A%d' % n, candidate='B%d' % n) for n in range(1, 5)])
        self.assertEqual(plan['performanceMargin'], .02)
        self.assertEqual(plan['caseRetries'], 0)

    def test_saved_plan_checks_identity_and_driver_hashes(self):
        plan = self.saved_plan()
        self.assertEqual(p.load_plan(self.args), plan)
        for key, bad in [('driverCommit', p.BASELINE), ('workspace', 'wrong'), ('root', 'wrong'), ('kernelOrigin', DRIVER), ('baselineCommit', DRIVER)]:
            broken = dict(plan, **{key: bad})
            p.write_json(self.root / 'plan.json', broken)
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                p.load_plan(self.args)
        p.write_json(self.root / 'plan.json', plan)
        (self.root / 'driver' / p.DRIVER_FILES[0]).write_text('tampered')
        with self.assertRaises(RuntimeError):
            p.load_plan(self.args)

    def test_saved_plan_rejects_missing_driver_file_entry(self):
        plan = self.saved_plan()
        plan['driverFiles'].pop(p.DRIVER_FILES[0])
        p.write_json(self.root / 'plan.json', plan)
        with self.assertRaises(RuntimeError):
            p.load_plan(self.args)

    def test_main_default_is_plan_only_no_execution(self):
        with patch.object(p, 'prepare') as prepare, patch.object(p, 'validate') as validate:
            self.assertEqual(p.main(['--source', str(self.source), '--root', str(self.root), '--driver-sha', DRIVER]), 0)
        prepare.assert_not_called()
        validate.assert_not_called()

    def test_main_exception_retains_partial_report(self):
        self.saved_plan()
        prior = dict(builds={'baseline': {'sha256': 'preserved'}}, instances=[{'name': 'A1'}], roundVerdicts=[{'name': 'A1'}], status='running')
        p.write_json(self.root / 'report.json', prior)
        def fail(args, plan, report):
            report['instances'].append({'name': 'B1', 'status': 'failed'})
            raise RuntimeError('synthetic worker failure')
        with patch.object(p.sys, 'platform', 'linux'), patch.object(p, 'validate', side_effect=fail):
            result = p.main(['--source', str(self.source), '--root', str(self.root), '--driver-sha', DRIVER, '--phase', 'validate', '--execute'])
        saved = json.loads((self.root / 'report.json').read_text())
        self.assertEqual(result, 1)
        self.assertEqual(saved['status'], 'invalid_calibration')
        self.assertEqual(saved['builds'], prior['builds'])
        self.assertEqual(len(saved['instances']), 2)
        self.assertIn('synthetic worker failure', saved['failure']['reason'])

    def fake_validation(self, fail_at=None, inputs=None, cleanup=True, factor=1, invoke_work=False, process_matches=True, parameter_drift=False):
        plan = self.saved_plan()
        p.write_json(self.source / (p.BENCH + '_baseline.json'), BASELINE)
        report = dict(scope='synthetic', status='running', builds={}, instances=[], caseVerdicts=[], roundVerdicts=[], roundMetrics={})
        for role in ('baseline', 'candidate'):
            identity = self.identity(role)
            p.write_json(self.root / 'runtime' / role / 'identity.json', identity)
        calls = []
        self.ports = []
        native = SimpleNamespace(discover_cases=lambda source: [SimpleNamespace(name=n) for n in p.CASES + (p.REGRESSION,)])
        harness = Mock()
        harness.original_inputs.side_effect = inputs or (lambda source: {'official': 'fixed'})
        harness.load_native.return_value = native
        harness.original_case.return_value = lambda call, out: dict(passed=True)
        def run(args, native, name, port, work, initialize=False):
            calls.append((name, args.role, initialize))
            self.ports.append(port)
            row = dict(name=name, status='passed', cleanupPassed=True, work={})
            if not initialize:
                row['work']['metrics'] = self.metrics(factor if args.role == 'candidate' else 1)
            if invoke_work:
                self._running_binary = args.binary
                out = args.out / name
                out.mkdir(parents=True, exist_ok=True)
                text = self.parameter_text()
                if parameter_drift and args.role == 'candidate':
                    text = text.replace('INFO', 'WARN')
                (out / 'actual-parameters.log').write_text(text)
                try:
                    row['work'] = work(SimpleNamespace(base_dir=out / 'database', port=port), out)
                except Exception as error:
                    row.update(status='infrastructure_error', error=str(error))
            if name == fail_at:
                row.update(status='passed' if not cleanup else 'infrastructure_error', cleanupPassed=cleanup)
            return row
        harness.run_instance.side_effect = run
        original_digest = p.digest
        def digest(path):
            return p.CLIENTS[Path(path).name] if Path(path).name in p.CLIENTS else original_digest(path)
        stack = ExitStack()
        self.addCleanup(stack.close)
        for target, value in [('load_module', lambda name, path: harness if 'harness' in name else SimpleNamespace(inspect_instance_process=lambda *a: 123) if 'sdb' in name else SCORER), ('source_identity', lambda source, commit: dict(commit=commit, tree=TREE)), ('verify_driver', lambda *a: {}), ('verify_loader', lambda *a: {}), ('digest', digest), ('benchmark_work', lambda args, *a: lambda call, out: dict(passed=True, metrics=self.metrics(factor if args.role == 'candidate' else 1)))]:
            stack.enter_context(patch.object(p, target, side_effect=value))
        stack.enter_context(patch.object(p.time, 'sleep'))
        if invoke_work:
            original_resolve = Path.resolve
            def resolve(path, *a, **kw):
                if path.name == 'exe' and path.parent.name == '123':
                    return self._running_binary if process_matches else self.root / 'wrong-elf'
                return original_resolve(path, *a, **kw)
            stack.enter_context(patch.object(Path, 'resolve', resolve))
        return plan, report, calls

    def test_validate_runs_all_cases_and_exact_schedule(self):
        plan, report, calls = self.fake_validation(factor=1.1)
        p.validate(self.args, plan, report)
        self.assertEqual(calls[:5], [(n, 'baseline', True) for n in p.CASES])
        self.assertEqual(calls[5:11], [(n, 'candidate', True) for n in p.CASES + (p.REGRESSION,)])
        self.assertEqual(calls[11:], [(n, r, False) for n, r in p.SCHEDULE])
        self.assertEqual(report['status'], 'valid_calibration_completed')
        self.assertEqual(report['sameHostPerformance']['classification'], 'regression')
        self.assertEqual(len(report['instances']), 19)
        self.assertEqual(self.ports, list(range(7300, 7319)))
        self.assertEqual(len(set(self.ports)), 19)
        self.assertTrue(report['allOfficialInputsUnchanged'])

    def test_validate_stops_after_failed_or_unclean_round_preserves_evidence(self):
        for cleanup in (False, True):
            with self.subTest(cleanup=cleanup):
                plan, report, calls = self.fake_validation(fail_at='B1', cleanup=cleanup)
                with self.assertRaises(RuntimeError):
                    p.validate(self.args, plan, report)
                self.assertEqual([row[0] for row in calls[11:]], ['A1', 'B1'])
                saved = json.loads((self.root / 'report.json').read_text())
                self.assertEqual(saved['roundVerdicts'][-1]['name'], 'B1')
                self.assertNotIn('sameHostPerformance', saved)

    def test_validate_rejects_input_hash_mismatch_before_any_case(self):
        plan, report, calls = self.fake_validation(inputs=lambda source: {'official': 'changed'})
        with self.assertRaisesRegex(RuntimeError, 'hashes differ'):
            p.validate(self.args, plan, report)
        self.assertEqual(calls, [])

    def test_validate_rejects_midrun_input_mutation(self):
        count = 0
        def inputs(source):
            nonlocal count
            count += 1
            return {'official': 'fixed' if count <= 3 else 'changed'}
        plan, report, calls = self.fake_validation(inputs=inputs)
        with self.assertRaisesRegex(RuntimeError, 'inputs changed'):
            p.validate(self.args, plan, report)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(report['instances']), 1)

    def test_original_benchmark_and_harness_are_unchanged(self):
        protected = {
            'tools/benchmark/fts_large_bench.sh': '596ea5d1c0038e666b78aa94bef22d2dfb87ffc9e84dbc7e2a1da1f746424462',
            'tools/benchmark/fts_large_bench_gen.py': 'b0c816dd8211ac0471f60b72a37a3fb5c62580357a5b6d7ac95f5a8c2e3f143d',
            'tools/benchmark/fts_large_bench_score.py': 'bea52eb1c68d05693bb3ac5b910ab0e2fa0e70e01582f40466898ee1e2e3ef55',
            'tools/benchmark/fts_large_bench_setup.sql': '311826751df7f51ac45156fb4284aeca9bd9197370aceb88f4fb92832b458108',
            'tools/benchmark/fts_large_bench_baseline.json': 'f4e97eddb2e49c982b967fc8047f7cf37177eebe53d648f6746d85aacda91f0c',
            p.PREFIX + 'fts_contest_validation.py': '0a0d0dd8d6651fa9082d66d2821bb9e5dd25fdd4c7955315c64229281c95a174',
            p.PREFIX + 'mysqltest_for_seekdb.py': 'fc7bb1cb8e10ab0c3fd68bade220caa69b7cda94f459ef7d9cdef2e431629b22',
            p.PREFIX + 'sdb.py': '53d88d4372e5534fbd37e17be3f418a2353efea648395f0ffb859926ae9eecd6',
        }
        for relative, expected in protected.items():
            with self.subTest(path=relative):
                # Git checkouts may use CRLF on Windows. Normalize only CRLF,
                # keeping all other bytes protected by the canonical LF hash.
                canonical = (REPO / relative).read_bytes().replace(b'\r\n', b'\n')
                self.assertEqual(hashlib.sha256(canonical).hexdigest(), expected)
                crlf = canonical.replace(b'\n', b'\r\n')
                self.assertEqual(hashlib.sha256(crlf.replace(b'\r\n', b'\n')).hexdigest(), expected)


    def snapshot_fixture(self):
        built = self.source / 'build_release/src/observer/seekdb'
        built.parent.mkdir(parents=True)
        built.write_bytes(b'\x7fELFsynthetic-candidate' + DRIVER.encode())
        (built.parent / 'ob_version.cpp').write_text('version ' + DRIVER)
        (self.source / 'build_release/CMakeCache.txt').write_text('CMAKE_BUILD_TYPE:STRING=RelWithDebInfo\nOB_ENABLE_UNITY:BOOL=ON\nOB_USE_LLD:BOOL=ON\n')
        (self.source / 'rust').mkdir()
        (self.root / 'runtime/candidate').mkdir(parents=True)
        (self.root / 'evidence/candidate').mkdir(parents=True)
        harness = Mock()
        harness.original_inputs.return_value = {'official': 'fixed'}
        def capture(command, output, *args, **kwargs):
            Path(output).write_text('Build ID: abcdef\n')
            return dict(exitCode=0, childrenCleaned=True)
        original_digest = p.digest
        def digest(path):
            return p.CLIENTS[Path(path).name] if Path(path).name in p.CLIENTS else original_digest(path)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(p, 'source_identity', return_value=dict(commit=DRIVER, tree=TREE)))
        stack.enter_context(patch.object(p, 'verify_driver', return_value={}))
        stack.enter_context(patch.object(p, 'load_module', return_value=harness))
        stack.enter_context(patch.object(p, 'capture_group', side_effect=capture))
        def loader(binary, log, *args):
            Path(log).write_text('synthetic loader resolved dependencies\n')
            return dict(exitCode=0, childrenCleaned=True)
        stack.enter_context(patch.object(p, 'verify_loader', side_effect=loader))
        stack.enter_context(patch.object(p, 'digest', side_effect=digest))
        stack.enter_context(patch.object(p.subprocess, 'check_output', return_value=b'rustc 1.98.1 (synthetic)'))
        return built

    def test_snapshot_binds_original_build_artifact_and_version(self):
        built = self.snapshot_fixture()
        record = p.snapshot(self.args, {})
        self.assertEqual(record['sourceCommit'], DRIVER)
        self.assertEqual(record['sourceTree'], TREE)
        self.assertEqual(record['kernelOrigin'], p.KERNEL)
        self.assertEqual(record['sha256'], p.digest(built))
        self.assertEqual(record['buildId'], 'abcdef')
        self.assertEqual(record['officialInputHashes'], {'official': 'fixed'})
        self.assertEqual(Path(record['binary']).read_bytes(), built.read_bytes())
        self.assertEqual(json.loads((self.root / 'runtime/candidate/identity.json').read_text())['sha256'], record['sha256'])

    def test_snapshot_rejects_stale_generated_version(self):
        built = self.snapshot_fixture()
        (built.parent / 'ob_version.cpp').write_text('old version ' + p.BASELINE)
        with self.assertRaisesRegex(RuntimeError, 'version'):
            p.snapshot(self.args, {})
        self.assertFalse((self.root / 'runtime/candidate/identity.json').exists())

    def test_snapshot_rejects_changed_build_settings(self):
        self.snapshot_fixture()
        (self.source / 'build_release/CMakeCache.txt').write_text('CMAKE_BUILD_TYPE:STRING=Debug\n')
        with self.assertRaisesRegex(RuntimeError, 'build setting'):
            p.snapshot(self.args, {})

    def test_snapshot_rejects_wrong_rust(self):
        self.snapshot_fixture()
        with patch.object(p.subprocess, 'check_output', return_value=b'rustc 1.99.0 synthetic'):
            with self.assertRaisesRegex(RuntimeError, 'Rust'):
                p.snapshot(self.args, {})

    def test_snapshot_rejects_invalid_build_id(self):
        self.snapshot_fixture()
        def capture(command, output, *args, **kwargs):
            Path(output).write_text('error: no ELF notes')
            return dict(exitCode=0, childrenCleaned=True)
        with patch.object(p, 'capture_group', side_effect=capture):
            with self.assertRaisesRegex(RuntimeError, 'BuildID'):
                p.snapshot(self.args, {})

    def test_snapshot_refuses_replacing_preserved_elf(self):
        self.snapshot_fixture()
        destination = self.root / 'runtime/candidate/seekdb'
        destination.write_bytes(b'preserve me')
        with self.assertRaisesRegex(RuntimeError, 'replace'):
            p.snapshot(self.args, {})
        self.assertEqual(destination.read_bytes(), b'preserve me')

    def test_prepare_preserves_driver_and_checks_exact_source_switch(self):
        self.root.rmdir()
        for name in p.DRIVER_FILES:
            path = self.source / p.PREFIX / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic ' + name)
        def git(source, *arguments):
            if arguments[0] == 'show':
                return (self.source / arguments[1].split(':', 1)[1]).read_bytes()
            return b''
        with patch.object(p, 'source_identity', return_value=dict(commit=DRIVER, tree=TREE)), patch.object(p, 'verify_driver', return_value={}), patch.object(p, 'git', side_effect=git) as mocked_git, patch.object(p, 'switch_source') as switch:
            plan = p.prepare(self.args)
        switch.assert_called_once_with(self.source, DRIVER, p.BASELINE)
        self.assertEqual(set(plan['driverFiles']), set(p.DRIVER_FILES))
        self.assertTrue((self.root / 'sources').is_dir())
        self.assertIn(('worktree', 'add', '--detach', str(self.root / 'sources/baseline'), p.BASELINE), [call.args[1:] for call in mocked_git.call_args_list])
        for name in p.DRIVER_FILES:
            self.assertEqual((self.root / 'driver' / name).read_bytes(), (self.source / p.PREFIX / name).read_bytes())

    def test_prepare_rejects_existing_or_nested_root_before_git(self):
        with patch.object(p, 'git') as git:
            with self.assertRaises(RuntimeError):
                p.prepare(self.args)
            self.args.root = self.source / 'inside'
            with self.assertRaises(RuntimeError):
                p.prepare(self.args)
        git.assert_not_called()

    @staticmethod
    def parameter_text():
        return 'memory_budget\t4G\nvector_memory_limit\t1GB\ndatafile_maxsize\t2GiB\nlog_disk_size\t2147483648\ndatafile_size\t1G\nsyslog_level\tINFO\n'

    def test_effective_parameters_normalizes_units(self):
        path = self.root / 'parameters.log'
        path.write_text(self.parameter_text())
        observed = p.effective_parameters(path)
        self.assertEqual(observed['memory_budget'], 4 * 1024 ** 3)
        self.assertEqual(observed['vector_memory_limit'], 1024 ** 3)
        self.assertEqual(observed['syslog_level'], 'INFO')

    def test_effective_parameters_rejects_missing_malformed_conflicting_and_wrong(self):
        text = self.parameter_text()
        for bad in [text.replace('4G', '2G'), text.replace('4G', 'unlimited'), text + 'memory_budget\t2G\n', text.replace('syslog_level\tINFO\n', ''), text.replace('datafile_size\t1G\n', ''), 'ERROR 123\n']:
            path = self.root / 'parameters.log'
            path.write_text(bad)
            with self.subTest(text=bad), self.assertRaises(RuntimeError):
                p.effective_parameters(path)

    def test_owned_callback_checks_live_executable_and_effective_parameters(self):
        plan, report, calls = self.fake_validation(invoke_work=True)
        p.validate(self.args, plan, report)
        self.assertEqual(report['status'], 'valid_calibration_completed')
        self.assertEqual(len(calls), 19)
        self.assertEqual(set(report['observedParameters']), {'cases', 'rounds'})
        for name, role in p.SCHEDULE:
            proof = json.loads((self.root / 'validation/rounds' / name / 'owned-process.json').read_text())
            self.assertEqual(proof['binary'], str(self.root / 'runtime' / role / 'seekdb'))
            self.assertEqual(proof['pid'], 123)

    def test_owned_callback_rejects_wrong_live_executable(self):
        plan, report, calls = self.fake_validation(invoke_work=True, process_matches=False)
        with self.assertRaises(RuntimeError):
            p.validate(self.args, plan, report)
        self.assertEqual(len(calls), 1)
        self.assertIn('exact role ELF', report['instances'][0]['error'])
        self.assertEqual(report['instances'][0]['status'], 'infrastructure_error')

    def test_owned_callback_rejects_role_parameter_drift(self):
        plan, report, calls = self.fake_validation(invoke_work=True, parameter_drift=True)
        with self.assertRaises(RuntimeError):
            p.validate(self.args, plan, report)
        self.assertEqual(len(calls), 6)
        self.assertIn('settings differ', report['instances'][-1]['error'])
        self.assertEqual(report['roundVerdicts'], [])

    def test_finalize_marks_incomplete_invalid_without_destroying_evidence(self):
        self.saved_plan()
        for status, expected in [('running', 1), ('phase_completed', 1), ('invalid_calibration', 1), ('valid_calibration_completed', 0)]:
            p.write_json(self.root / 'report.json', dict(status=status, builds={'baseline': 'kept'}, failure={'reason': 'original failure'}))
            with self.subTest(status=status), patch.object(p.sys, 'platform', 'linux'):
                code = p.main(['--source', str(self.source), '--root', str(self.root), '--driver-sha', DRIVER, '--phase', 'finalize', '--execute'])
            self.assertEqual(code, expected)
            saved = json.loads((self.root / 'report.json').read_text())
            self.assertEqual(saved['builds'], {'baseline': 'kept'})
            self.assertEqual(saved['failure']['reason'], 'original failure')
            self.assertEqual(saved['status'], 'invalid_calibration' if expected else 'valid_calibration_completed')

    def test_finalize_missing_report_is_invalid(self):
        self.saved_plan()
        with patch.object(p.sys, 'platform', 'linux'):
            code = p.main(['--source', str(self.source), '--root', str(self.root), '--driver-sha', DRIVER, '--phase', 'finalize', '--execute'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads((self.root / 'report.json').read_text())['status'], 'invalid_calibration')

    def test_exact_two_percent_boundary_is_inconclusive(self):
        scorer = Mock()
        for value in (.02, -.02):
            scorer.calculate_score.return_value = dict(mean_improvement=value, categories={}, score=0)
            self.assertEqual(p.performance(self.pairs([1] * 4), BASELINE, scorer)['classification'], 'inconclusive')

    def test_elf_requires_embedded_exact_source_revision(self):
        identity = self.identity()
        path = Path(identity['binary'])
        path.write_bytes(b'\x7fELF' + p.BASELINE.encode())
        identity.update(bytes=path.stat().st_size, sha256=p.digest(path))
        with self.assertRaisesRegex(RuntimeError, 'exact build-source'):
            p.validate_identity(identity, 'candidate', DRIVER, self.root)

    def test_benchmark_failure_does_not_parse_or_score(self):
        args = SimpleNamespace(workspace=self.source, binary=self.root / 'seekdb', obclient='/client', source=self.source, role='candidate', expected_sha=DRIVER)
        for result in [dict(exitCode=1, childrenCleaned=True), dict(exitCode=0, childrenCleaned=False)]:
            with self.subTest(result=result), patch.object(p.time, 'sleep'), patch.object(p, 'capture_group', return_value=result), patch.object(p, 'strict_report') as parse:
                row = p.benchmark_work(args, BASELINE, SCORER, False)(SimpleNamespace(port=7300), self.root)
            self.assertFalse(row['passed'])
            self.assertEqual(row['failureKind'], 'benchmark_process_failure')
            parse.assert_not_called()

    def test_benchmark_positive_uses_original_config_no_override(self):
        args = SimpleNamespace(workspace=self.source, binary=self.root / 'seekdb', obclient='/client', source=self.source, role='candidate', expected_sha=DRIVER)
        path = self.report_file()
        path.rename(self.root / 'report.txt')
        captured = []
        def capture(command, output, *a, **kw):
            captured.append((command, kw))
            Path(output).write_text('{}' if Path(output).name == 'frozen-score.json' else 'benchmark completed')
            return dict(exitCode=0, childrenCleaned=True)
        with patch.object(p.time, 'sleep'), patch.object(p, 'capture_group', side_effect=capture), patch.object(p, 'explain_queries', return_value={'exact': True}) as explain:
            row = p.benchmark_work(args, BASELINE, SCORER, True)(SimpleNamespace(port=7300), self.root)
        self.assertTrue(row['passed'])
        self.assertEqual(captured[0][0], ['bash', str(self.source / (p.BENCH + '.sh'))])
        self.assertEqual({key: captured[0][1]['env'][key.upper()] for key in p.CONFIG}, {key: str(value) for key, value in p.CONFIG.items()})
        self.assertNotIn('--allow-config-mismatch', captured[1][0])
        self.assertNotIn('--baseline', captured[1][0])
        explain.assert_called_once()

    def test_explain_uses_exact_four_queries_and_preserves_partial_failure(self):
        args = SimpleNamespace(source=REPO, role='candidate', obclient='/client')
        captured = []
        def capture(command, output, *a, **kw):
            captured.append(kw['stdin'])
            Path(output).write_text('calc_relevance=false\n')
            return dict(exitCode=0, childrenCleaned=True)
        with patch.object(p, 'capture_group', side_effect=capture):
            result = p.explain_queries(args, SimpleNamespace(port=7300), self.root, {})
        queries = p.queries_from_benchmark(REPO / (p.BENCH + '.sh'))
        self.assertEqual(captured, [(p.SESSION + 'EXPLAIN ' + sql + '\n').encode('utf-8') for sql in queries.values()])
        self.assertEqual(list(result), list(queries))
        for row in result.values():
            self.assertEqual(row['calcRelevanceEvidence'], ['calc_relevance=false'])
        def failed(command, output, *a, **kw):
            Path(output).write_text('ERROR 1000')
            return dict(exitCode=1, childrenCleaned=True)
        with patch.object(p, 'capture_group', side_effect=failed):
            with self.assertRaises(RuntimeError):
                p.explain_queries(args, SimpleNamespace(port=7300), self.root, {})
        partial = json.loads((self.root / 'explain-result.json').read_text())
        self.assertEqual(list(partial), ['query_cn'])
        self.assertEqual(partial['query_cn']['exitCode'], 1)

    @unittest.skipUnless(sys.platform == 'linux', 'Real SIGTERM lifecycle requires Linux')
    def test_real_sigterm_cleans_owned_child_and_prevents_next_work(self):
        pid_path = self.root / 'synthetic-child.pid'
        interrupted = self.root / 'interrupted.txt'
        subsequent = self.root / 'subsequent-work.txt'
        child_code = 'import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)'
        wrapper_code = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import fts_paired_validation as p
p.install_termination_handlers()
root = Path(sys.argv[2])
try:
    p.capture_group([sys.executable, '-c', sys.argv[3], str(root / 'synthetic-child.pid')], root / 'child.log', root, 70)
    (root / 'subsequent-work.txt').write_text('must not execute')
except InterruptedError:
    (root / 'interrupted.txt').write_text('caught')
'''
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        wrapper = subprocess.Popen([sys.executable, '-c', wrapper_code, str(Path(p.__file__).parent), str(self.root), child_code],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
        child_pid = None
        def gone(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and wrapper.poll() is None:
                if pid_path.is_file() and pid_path.read_text().strip():
                    child_pid = int(pid_path.read_text())
                    break
                time.sleep(.05)
            self.assertIsNotNone(child_pid, 'Synthetic child never published its PID')
            os.kill(wrapper.pid, signal.SIGTERM)
            stdout, stderr = wrapper.communicate(timeout=20)
            self.assertEqual(wrapper.returncode, 0, (stdout + stderr).decode('utf-8', 'replace'))
            self.assertEqual(interrupted.read_text(), 'caught')
            self.assertFalse(subsequent.exists(), 'Work ran after termination')
            deadline = time.monotonic() + 3
            while not gone(child_pid) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(gone(child_pid), 'Owned child survived wrapper SIGTERM')
        finally:
            # Only these two freshly created, positively identified process groups
            # may be signalled. Never enumerate or kill unrelated processes.
            if wrapper.poll() is None:
                os.killpg(wrapper.pid, signal.SIGKILL)
                wrapper.communicate(timeout=10)
            if child_pid is None and pid_path.is_file() and pid_path.read_text().strip():
                child_pid = int(pid_path.read_text())
            if child_pid is not None and not gone(child_pid):
                cmdline = Path('/proc') / str(child_pid) / 'cmdline'
                try:
                    owned = os.fsencode(str(pid_path)) in cmdline.read_bytes().split(b'\0')
                except FileNotFoundError:
                    owned = False
                if owned:
                    try:
                        os.killpg(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if wrapper.stdout:
                wrapper.stdout.close()
            if wrapper.stderr:
                wrapper.stderr.close()

if __name__ == '__main__':
    unittest.main()
