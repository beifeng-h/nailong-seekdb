"""Offline regressions for fixed-ELF projection diagnostics."""
import argparse
import datetime
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import tarfile
import zipfile

import fts_count_projection_diagnostics as diagnostics


def retrieval_plan(relevance):
    sort = '[MATCH(docs.content), DESC]' if relevance else 'nil'
    return ('|0|SCALAR GROUP BY||1|1|\n|1|  TEXT RETRIEVAL SCAN|docs(fts)|20|1|\n'
            'Outputs & filters:\n  0 - output([T_FUN_COUNT(*)]), filter(nil)\n'
            '  1 - output([docs.id]), filter(nil), rowset=256\n'
            '      is_index_back=true, is_global_index=false,\n'
            f'      calc_relevance={str(relevance).lower()}, match_expr(MATCH(docs.content)),\n'
            f'      sort_keys({sort}), limit(20), offset(nil)\n')


class QueryTests(unittest.TestCase):
    def test_four_queries_preserve_all_text_except_the_two_admitted_changes(self):
        source = Path(__file__).resolve().parents[3]
        cases = diagnostics.query_cases(source)
        original = ("SELECT COUNT(*) FROM (SELECT id FROM fts_large_bench.docs WHERE "
                    "MATCH(content) AGAINST('倒排索引 tokenizer') LIMIT 20) t;")
        self.assertEqual(cases['id_unordered'], original)
        self.assertEqual(cases['constant_unordered'], original.replace('(SELECT id FROM ', '(SELECT 1 FROM '))
        for name in ('id_ordered', 'constant_ordered'):
            without_order = cases[name].replace(" ORDER BY MATCH(content) AGAINST('倒排索引 tokenizer') DESC", '')
            self.assertEqual(without_order, cases[name.replace('_ordered', '_unordered')])
        self.assertEqual(tuple(cases), diagnostics.CASE_NAMES)

    def test_rotation_covers_every_first_position_for_each_phase(self):
        for sample in range(4):
            orders = [diagnostics.rotated_order(position, sample) for position in range(4)]
            self.assertEqual({order[0] for order in orders}, set(diagnostics.CASE_NAMES))
            for order in orders:
                self.assertEqual(set(order), set(diagnostics.CASE_NAMES))
                self.assertEqual(len(order), 4)
        self.assertNotEqual(diagnostics.rotated_order(0, 1), diagnostics.rotated_order(0, 2))

    def test_changed_original_shape_is_rejected(self):
        with patch.object(diagnostics.common, 'queries_from_benchmark', return_value={'query_limit': 'SELECT 1;'}):
            with self.assertRaisesRegex(RuntimeError, 'admitted shape'):
                diagnostics.query_cases(Path('/source'))


class MeasurementTests(unittest.TestCase):
    def capture(self, wrong=False, failed=False):
        def run(command, output, cwd, timeout, **kwargs):
            repetitions = len(kwargs['stdin'].splitlines()) - 1
            output.write_bytes((b'19\n' if wrong else b'20\n') * repetitions)
            return dict(exitCode=1 if failed else 0, childrenCleaned=True)
        return run

    def test_original_repeated_sql_and_raw_nanoseconds_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            rows = []
            call = SimpleNamespace(obclient=Path('/client'), port=7500)
            with patch.object(diagnostics.common, 'capture_group', side_effect=self.capture()):
                result = diagnostics.measure(call, out, 'id_unordered', 'SELECT COUNT(*);', 'sample-1', 200, rows)
            self.assertEqual(len(rows), 1)
            self.assertTrue(result['passed'])
            self.assertEqual(result['repetitions'], 200)
            self.assertEqual(Path(result['executedSqlPath']).read_text(), diagnostics.common.SESSION + 'SELECT COUNT(*);\n' * 200)
            self.assertEqual(result['elapsedNs'], result['finishedMonotonicNs'] - result['startedMonotonicNs'])
            self.assertEqual(result['averageMs'], result['elapsedNs'] / 1_000_000 / 200)
            self.assertEqual(Path(result['outputPath']).read_bytes(), b'20\n' * 200)
            self.assertEqual(json.loads((out / 'diagnostic-id_unordered-sample-1-measurement.json').read_text()), result)

    def test_wrong_count_and_client_failure_preserve_raw_measurements_before_rejection(self):
        for wrong, failed in ((True, False), (False, True)):
            with self.subTest(wrong=wrong, failed=failed), tempfile.TemporaryDirectory() as tmp:
                rows = []
                with patch.object(diagnostics.common, 'capture_group', side_effect=self.capture(wrong, failed)):
                    with self.assertRaisesRegex(RuntimeError, 'repeated SQL failed'):
                        diagnostics.measure(SimpleNamespace(obclient=Path('/client'), port=7500),
                            Path(tmp), 'constant_ordered', 'SELECT 1;', 'sample-2', 200, rows)
                self.assertFalse(rows[0]['passed'])
                self.assertTrue(Path(rows[0]['outputPath']).is_file())
                self.assertTrue((Path(tmp) / 'diagnostic-constant_ordered-sample-2-measurement.json').is_file())


class CallbackTests(unittest.TestCase):
    def callback(self, out, role='candidate', wrong_check=False, bad_plan=False):
        queries = dict(zip(diagnostics.CASE_NAMES, ('SELECT ' + str(i) + ';' for i in range(4))))
        args = SimpleNamespace(root=out.parent, source=out.parent / 'source')
        identities = {role: {'binary': '/binary'}}

        def sql(call, target, label, query):
            if label.endswith('-count'):
                text = '19\n' if wrong_check else '20\n'
            else:
                ordered = label.startswith(('diagnostic-id_ordered-', 'diagnostic-constant_ordered-'))
                text = retrieval_plan(bad_plan or role == 'baseline' or ordered)
            (target / (label + '.log')).write_text(text)
            return text

        with patch.object(diagnostics, 'query_cases', return_value=queries), \
                patch.object(diagnostics.common, 'benchmark_work', return_value=lambda call, target: {'passed': True}), \
                patch.object(diagnostics.driver, 'sql', side_effect=sql), \
                patch.object(diagnostics.common, 'capture_group', side_effect=MeasurementTests().capture()):
            callback = diagnostics.diagnostic_work(args, identities, role, 1, {}, None)
            return callback(SimpleNamespace(obclient=Path('/client'), port=7500), out, 1234)

    def test_each_role_checks_four_plans_then_executes_rotated_warmup_and_three_samples(self):
        for role in ('baseline', 'candidate'):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                result = self.callback(Path(tmp), role)
                report = result['projectionDiagnostics']
                self.assertTrue(report['passed'])
                self.assertEqual(report['elfSourceCommit'], diagnostics.PINS[role]['commit'])
                self.assertEqual(len(report['checks']), 4)
                self.assertEqual(len(report['executions']), 16)
                self.assertEqual(sum(row['repetitions'] for row in report['executions']), 2520)
                for sample, row in enumerate(report['executionOrder']):
                    self.assertEqual(row['cases'], diagnostics.rotated_order(1, sample))
                self.assertEqual([row['phase'] for row in report['executionOrder']],
                                 ['warmup', 'sample-1', 'sample-2', 'sample-3'])
                self.assertEqual(set(report['meansMs']), set(diagnostics.CASE_NAMES))
                self.assertFalse(report['originalNineMetricResult'])

    def test_failed_cardinality_or_plan_saves_partial_evidence_without_timing(self):
        for wrong_count, bad_plan in ((True, False), (False, True)):
            with self.subTest(wrong_count=wrong_count), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(RuntimeError):
                    self.callback(Path(tmp), wrong_check=wrong_count, bad_plan=bad_plan)
                report = json.loads((Path(tmp) / 'projection-diagnostics.json').read_text())
                self.assertFalse(report['passed'])
                self.assertEqual(report['executions'], [])


class IdentityTests(unittest.TestCase):
    def fixture(self, root, role='baseline'):
        raw = b'\x7fELF preserved source'
        tar_bytes = io.BytesIO()
        with tarfile.open(fileobj=tar_bytes, mode='w:gz') as archive:
            member = tarfile.TarInfo('candidate/seekdb')
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
        payload = tar_bytes.getvalue()
        pin = dict(diagnostics.PINS[role], elfBytes=len(raw), elfSha256=hashlib.sha256(raw).hexdigest(),
                   tarBytes=len(payload), tarSha256=hashlib.sha256(payload).hexdigest())
        identity = dict(role='candidate', sourceCommit=pin['commit'], sourceTree=pin['tree'],
            kernelOrigin='original-legacy-kernel', kernelTree='original-legacy-tree', bytes=len(raw),
            sha256=pin['elfSha256'], buildId=pin['buildId'], officialInputHashes={'input': 'hash'})
        manifest = dict(identity=identity, fullBytes=True, stripped=False, truncated=False)
        path = root / 'original.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('candidate-identity.json', json.dumps(identity))
            archive.writestr('candidate-artifact-manifest.json', json.dumps(manifest))
            archive.writestr('candidate-elf.tar.gz', payload)
        pin.update(zipBytes=path.stat().st_size, zipSha256=hashlib.sha256(path.read_bytes()).hexdigest())
        (root / 'runtime' / role).mkdir(parents=True)
        (root / 'evidence' / role).mkdir(parents=True)
        return path, pin, identity

    def test_role_relocation_preserves_archived_kernel_and_fixed_embedded_source(self):
        for role in ('baseline', 'candidate'):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path, pin, original = self.fixture(root, role)
                with patch.dict(diagnostics.PINS, {role: pin}), patch.object(diagnostics.driver, 'safe_elf') as safe:
                    relocated = diagnostics.extract_elf(path, role, root, {'input': 'hash'})
                self.assertEqual(relocated['role'], role)
                self.assertEqual(relocated['archiveRole'], 'candidate')
                self.assertEqual(relocated['sourceCommit'], pin['commit'])
                self.assertEqual(relocated['kernelOrigin'], original['kernelOrigin'])
                self.assertEqual(relocated['kernelTree'], original['kernelTree'])
                self.assertEqual(safe.call_args.args[-1], pin['commit'])
                self.assertEqual(json.loads((root / 'evidence' / role / 'archived-identity.json').read_text()), original)

    def test_archive_or_original_input_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, pin, original = self.fixture(root)
            with patch.dict(diagnostics.PINS, {'baseline': pin}), patch.object(diagnostics.driver, 'safe_elf'):
                with self.assertRaisesRegex(RuntimeError, 'source or content identity'):
                    diagnostics.extract_elf(path, 'baseline', root, {'input': 'changed'})
            with patch.dict(diagnostics.PINS, {'baseline': dict(pin, zipSha256='0' * 64)}):
                with self.assertRaisesRegex(RuntimeError, 'complete ZIP differs'):
                    diagnostics.extract_elf(path, 'baseline', root, {'input': 'hash'})

    def test_new_driver_identity_and_fixed_product_blobs_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for path in diagnostics.count_only.PRODUCTS:
                file = root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(b'fixed product\n')

            def git(source, *arguments):
                if arguments[0] == 'diff':
                    return b'.github/script/seekdb/fts_count_projection_diagnostics.py\n'
                if arguments[0] == 'show':
                    return b'fixed product\n'
                return (diagnostics.CANDIDATE_TREE + '\n').encode()

            with patch.object(diagnostics.common, 'git', side_effect=git), \
                    patch.object(diagnostics.common, 'source_identity', return_value={'tree': 'a' * 40}), \
                    patch.object(diagnostics.driver, 'inventory', return_value={'input': 'hash'}):
                proof = diagnostics.diagnostic_source_proof(root, 'b' * 40)
            self.assertEqual(proof['driverCommit'], 'b' * 40)
            self.assertEqual(proof['elfSourceCommit'], diagnostics.CANDIDATE)
            self.assertNotEqual(proof['driverCommit'], proof['elfSourceCommit'])


class LifecycleTests(unittest.TestCase):
    def entrypoint(self, startup='normal'):
        source = Path(__file__).resolve().parents[3]
        adapter = diagnostics.common.load_module('projection_test_adapter',
            source / '.github/script/seekdb/fts-profile-runtime/run_profile.py')
        guard = SimpleNamespace(process_observation=lambda seconds: {'foreignRelated': [], 'foreignActive': []},
                                guard_processes=lambda observation: None)
        with tempfile.TemporaryDirectory() as tmp:
            current_alarm, clock, inner_alarms, owner_alarms = [0], [0.0], [], []
            capture_limits = []
            root = Path(tmp) / 'run'
            args = SimpleNamespace(root=root, source=source, driver_sha='a' * 40,
                stop_deadline_utc=(datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(minutes=20)).isoformat())
            runtime_args = SimpleNamespace(root=root, source=root / 'sources/candidate', driver_sha=diagnostics.CANDIDATE)

            def prepare(values, report):
                baseline = runtime_args.source / 'tools/benchmark/fts_large_bench_baseline.json'
                baseline.parent.mkdir(parents=True)
                baseline.write_text('{}')
                report['diagnosticDriver'] = {'driverCommit': values.driver_sha, 'elfSourceCommit': diagnostics.CANDIDATE}
                return runtime_args, {}

            def owned(values, report, identities, role, name, work):
                self.assertEqual(values.driver_sha, diagnostics.CANDIDATE)
                self.assertNotEqual(values.driver_sha, args.driver_sha)
                owner_alarms.append(current_alarm[0])
                self.assertEqual(current_alarm[0], 0)
                if startup == 'slow_ready':
                    clock[0] = 890.0
                    harness = diagnostics.driver.native_harness
                    native_args = SimpleNamespace(out=root / 'validation', source=values.source,
                        binary=Path('/binary'), obclient=Path('/client'), mysqltest=Path('/mysqltest'))
                    with patch.object(harness, 'bounded_db_logs', return_value={}), \
                            patch.object(harness, 'show_log'), patch.object(harness, 'emit'):
                        row = harness.run_instance(native_args, None, name, 0,
                                                   lambda call, out: work(call, out, 1234))
                    report['instances'].append(row)
                    return row
                failed = startup != 'normal'
                if startup == 'exhausted':
                    clock[0] = 1000.0
                    with self.assertRaisesRegex(RuntimeError, 'exhausted during owned startup'):
                        work(None, None, None)
                elif startup == 'normal':
                    work(None, None, None)
                self.assertEqual(current_alarm[0], 0)
                row = dict(name=name, role=role, status='infrastructure_error' if failed else 'passed',
                    cleanupPassed=True, stop={'exitCode': 0}, destroy={'exitCode': 0},
                    work={'projectionDiagnostics': dict(role=role, elfSourceCommit=diagnostics.PINS[role]['commit'],
                        meansMs={case: 1.0 for case in diagnostics.CASE_NAMES})})
                report['instances'].append(row)
                return row

            def load(name, path):
                return adapter if name == 'projection_original_profile_adapter' else None

            def arm(seconds):
                current_alarm[0] = seconds

            def inner(call, out, pid):
                inner_alarms.append(current_alarm[0])
                self.assertGreater(current_alarm[0], 0)

            def native_capture(command, path, source, timeout, *other, **kwargs):
                capture_limits.append(dict(file=path.name, timeout=timeout))
                self.assertEqual(current_alarm[0], 0)
                action = str(command[2])
                base = Path(command[command.index('--base-dir') + 1])
                if action == 'start':
                    base.mkdir()
                    clock[0] += 2
                elif action == 'wait-ready':
                    clock[0] += timeout
                elif action == 'stop':
                    clock[0] += 60
                elif action == 'destroy':
                    clock[0] += 60
                    base.rmdir()
                path.write_bytes(b'bounded original receipt\n')
                return dict(exitCode=124 if action == 'wait-ready' else 0, timeoutSeconds=timeout)

            with patch.object(diagnostics, 'prepare', side_effect=prepare), \
                    patch.object(diagnostics, 'diagnostic_source_proof'), \
                    patch.object(diagnostics.driver, 'resource_record'), \
                    patch.object(diagnostics.count_only.parent, 'prepare_nofile') as nofile, \
                    patch.object(diagnostics.common, 'load_module', side_effect=load), \
                    patch.object(adapter, 'load_driver', return_value=guard), \
                    patch.object(adapter, 'install_observation_recovery'), \
                    patch.object(diagnostics, 'diagnostic_work', return_value=inner), \
                    patch.object(diagnostics.driver, 'run_owned', side_effect=owned), \
                    patch.object(diagnostics.driver.native_harness, 'capture', side_effect=native_capture) as original_capture, \
                    patch.object(diagnostics.time, 'monotonic', side_effect=lambda: clock[0]), \
                    patch.object(diagnostics.signal, 'SIGALRM', 14, create=True), \
                    patch.object(diagnostics.signal, 'signal'), \
                    patch.object(diagnostics.signal, 'alarm', side_effect=arm, create=True) as alarm:
                if startup == 'normal':
                    diagnostics.run(args)
                else:
                    with self.assertRaisesRegex(RuntimeError, 'Owned instance failed'):
                        diagnostics.run(args)
                self.assertIs(diagnostics.driver.native_harness.capture, original_capture)
            report = json.loads((root / 'report.json').read_text())
            self.assertEqual(current_alarm[0], 0)
            self.assertEqual(nofile.call_args.args[0].phase, 'run')
            return report, owner_alarms, inner_alarms, alarm.call_args_list, capture_limits

    def test_four_position_entrypoint_keeps_driver_and_elf_source_identities_separate(self):
        report, owner_alarms, inner_alarms, alarm_calls, _ = self.entrypoint()
        self.assertTrue(report['passed'])
        self.assertEqual([(row['name'], row['role']) for row in report['rounds']], list(diagnostics.SCHEDULE))
        self.assertEqual(len(report['instances']), 4)
        self.assertEqual(report['diagnosticDriver']['driverCommit'], 'a' * 40)
        self.assertEqual(report['diagnosticDriver']['elfSourceCommit'], diagnostics.CANDIDATE)
        self.assertEqual(report['comparisons'][1]['baseline'], 'A2')
        self.assertEqual(report['comparisons'][1]['candidate'], 'B2')
        self.assertEqual(owner_alarms, [0] * 4)
        self.assertEqual(len(inner_alarms), 4)
        self.assertTrue(all(value > 0 for value in inner_alarms))
        self.assertEqual(sum(call.args == (0,) for call in alarm_calls), 9)

    def test_startup_failure_before_work_keeps_cleanup_disarmed(self):
        report, owner_alarms, inner_alarms, alarm_calls, _ = self.entrypoint('failed')
        self.assertFalse(report['passed'])
        self.assertEqual(owner_alarms, [0])
        self.assertEqual(inner_alarms, [])
        self.assertEqual(report['instances'][0]['stop']['exitCode'], 0)
        self.assertEqual(report['instances'][0]['destroy']['exitCode'], 0)
        self.assertTrue(report['instances'][0]['cleanupPassed'])
        self.assertEqual(sum(call.args[0] > 0 for call in alarm_calls), 2)

    def test_startup_exhausted_absolute_budget_disarms_before_cleanup(self):
        report, owner_alarms, inner_alarms, alarm_calls, _ = self.entrypoint('exhausted')
        self.assertFalse(report['passed'])
        self.assertEqual(owner_alarms, [0])
        self.assertEqual(inner_alarms, [])
        self.assertTrue(report['instances'][0]['cleanupPassed'])
        self.assertEqual(sum(call.args[0] > 0 for call in alarm_calls), 2)

    def test_slow_ready_is_bounded_by_remaining_window_and_cleanup_keeps_original_timeout(self):
        report, owner_alarms, inner_alarms, alarm_calls, limits = self.entrypoint('slow_ready')
        self.assertFalse(report['passed'])
        self.assertEqual(owner_alarms, [0])
        self.assertEqual(inner_alarms, [])
        self.assertEqual(limits, [dict(file='start.log', timeout=10), dict(file='ready.log', timeout=8),
                                 dict(file='own-stop.log', timeout=60), dict(file='own-destroy.log', timeout=60)])
        instance = report['instances'][0]
        self.assertEqual(instance['ready']['timeoutSeconds'], 8)
        self.assertEqual(instance['stop']['timeoutSeconds'], 60)
        self.assertEqual(instance['destroy']['timeoutSeconds'], 60)
        self.assertTrue(instance['cleanupPassed'])

    def test_existing_native_lifecycle_preserves_stop_destroy_on_success_and_work_failure(self):
        harness = diagnostics.driver.native_harness
        for failure in (None, 'work', 'startup'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                args = argparse.Namespace(out=root, binary=Path('/binary'), obclient=Path('/client'),
                                          mysqltest=Path('/mysqltest'), source=root)
                calls, work_calls = [], []

                def capture(command, path, source, timeout, *other):
                    action = str(command[2]) if len(command) > 2 else ''
                    if action in ('start', 'stop', 'destroy'):
                        calls.append(action)
                        base = Path(command[command.index('--base-dir') + 1])
                        if action == 'start':
                            base.mkdir()
                        elif action == 'destroy':
                            base.rmdir()
                    path.write_bytes(b'original receipt\n')
                    return dict(exitCode=1 if failure == 'startup' and action == 'start' else 0)

                def work(call, out):
                    work_calls.append(True)
                    if failure == 'work':
                        raise RuntimeError('deliberate diagnostic failure')
                    return {'passed': True}

                with patch.object(harness, 'capture', side_effect=capture), \
                        patch.object(harness, 'bounded_db_logs', return_value={}), \
                        patch.object(harness, 'show_log'), patch.object(harness, 'emit'):
                    result = harness.run_instance(args, None, 'A1', 0, work)
                self.assertEqual(calls, ['start', 'stop', 'destroy'])
                self.assertEqual(work_calls, [] if failure == 'startup' else [True])
                self.assertTrue(result['cleanupPassed'])
                self.assertEqual(result['stop']['exitCode'], 0)
                self.assertEqual(result['destroy']['exitCode'], 0)
                self.assertEqual(result['status'], 'infrastructure_error' if failure else 'passed')
                self.assertTrue((root / 'A1/result.json').is_file())


class WorkflowTests(unittest.TestCase):
    def test_short_workflow_uses_fixed_archives_and_has_no_build_entrypoint(self):
        workflow = Path(__file__).resolve().parents[3] / '.github/workflows/fts-count-projection-diagnostics.yml'
        text = workflow.read_text(encoding='utf-8')
        self.assertIn('timeout-minutes: 30', text)
        self.assertIn("artifact-ids: '13828533821'", text)
        self.assertIn("artifact-ids: '13929153838'", text)
        self.assertIn("run-id: '43181771127'", text)
        self.assertIn(diagnostics.BASE + ':refs/heads/codex/frozen-b963', text)
        self.assertIn(diagnostics.CANDIDATE + ':refs/heads/codex/frozen-d1e6', text)
        self.assertIn('5bce9e06d1281347ad61344e4b01fab9158c58a2:refs/heads/codex/frozen-official', text)
        self.assertNotRegex(text, r'(?m)^\s+(?:sudo[^\n]+)?(?:cmake|make|ninja|\./build\.sh)\b')
        self.assertNotIn('COUNT_BASELINE_DIAGNOSTICS_ONLY', text)
        self.assertIn('if: ${{ always() }}', text)
        self.assertIn('--phase collect', text)
        self.assertIn('skip-decompress: true', text)


if __name__ == '__main__':
    unittest.main()
