import argparse
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

import fts_accepted_profile as profile


class ProfileBoundaryTests(unittest.TestCase):
    def test_sparse_phase_is_rejected_despite_a_successful_perf_exit(self):
        processes = [dict(monotonic_ns=0, utime_seconds=1),
                     dict(monotonic_ns=4000000000, utime_seconds=5)]
        phase = dict(startMonotonicNs=0, stopMonotonicNs=4000000000, sampleCount=2)
        result = profile.coverage(phase, processes)
        self.assertFalse(result['passed'])
        self.assertEqual(result['minimumSamples'], 99)
        phase['sampleCount'] = 300
        self.assertTrue(profile.coverage(phase, processes)['passed'])

    def test_thread_telemetry_reads_only_the_owned_task_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '101').mkdir()
            fields = ['S'] + ['0'] * 21
            fields[11], fields[12], fields[19] = '25', '3', '123'
            (root / '101/stat').write_text('101 (Req Worker) ' + ' '.join(fields))
            (root / '102').mkdir()
            self.assertEqual(profile.owned_threads(root), {'101': dict(
                comm='Req Worker', userTicks=25, systemTicks=3, startTicks=123)})

    def test_phase_ranges_keep_nanosecond_precision_and_do_not_overlap(self):
        start = 123456789012345
        marks = [dict(label='benchmark_stage', fields={'stage': f'[{i}/5]'},
                      monotonic_ns=start + i * 1000000001) for i in range(1, 6)]
        marks.append(dict(label='accepted_original_benchmark_finished', monotonic_ns=start + 7000000000))
        phases = profile.phase_windows(marks)
        self.assertEqual(phases['build']['perfTime'], '123459.789012348,123460.789012348')
        self.assertEqual(phases['build']['stopMonotonicNs'], phases['tokenize']['startMonotonicNs'])

    def test_missing_stage_is_not_silently_assigned_to_another_phase(self):
        with self.assertRaisesRegex(RuntimeError, 'five original'):
            profile.phase_windows([])

    def test_prepare_removes_build_and_pair_schedule(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = dict(candidateBuildsRequired=1, schedule=[{'name': 'A1'}])
            (root / 'plan.json').write_text(json.dumps(plan))
            (root / 'report.json').write_text(json.dumps(dict(plan=plan)))
            with mock.patch.object(profile, 'ORIGINAL_PREPARE') as prepare:
                profile.prepare(argparse.Namespace(root=root))
            prepare.assert_called_once()
            result = json.loads((root / 'report.json').read_text())
            self.assertEqual(result['plan']['candidateBuildsRequired'], 0)
            self.assertEqual(result['plan']['schedule'], [])
            self.assertFalse(result['performanceComparisonComputed'])
            self.assertTrue(result['timingsDiagnosticOnly'])

    def test_failed_database_cleanup_prevents_perf_decoding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'runtime/baseline').mkdir(parents=True)
            (root / 'tools/benchmark').mkdir(parents=True)
            (root / 'runtime/baseline/identity.json').write_text(json.dumps(dict(
                binary=str(root / 'seekdb'), sha256='0' * 64, bytes=1, buildId='test')))
            (root / 'tools/benchmark/fts_large_bench_baseline.json').write_text('{}')
            (root / 'report.json').write_text(json.dumps(dict(
                status='prepared', plan={'candidateBuildsRequired': 0}, instances=[])))
            failed = dict(name='accepted-profile', status='passed', cleanupPassed=False)
            with mock.patch.object(profile.validation, 'prepare_nofile'), \
                    mock.patch.object(profile.driver, 'safe_elf'), \
                    mock.patch.object(profile.common, 'load_module', return_value=types.SimpleNamespace(_proc_snapshot=mock.Mock())), \
                    mock.patch.object(profile.validation, 'benchmark_callback'), \
                    mock.patch.object(profile.driver, 'run_owned', return_value=failed), \
                    mock.patch.object(profile.common, 'capture_group') as decode:
                with self.assertRaisesRegex(RuntimeError, 'cleanup incomplete'):
                    profile.validate(argparse.Namespace(root=root, source=root))
            decode.assert_not_called()


if __name__ == '__main__':
    unittest.main()
