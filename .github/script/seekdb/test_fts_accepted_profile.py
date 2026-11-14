import argparse
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

import fts_accepted_profile as profile


class ProfileBoundaryTests(unittest.TestCase):
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
                    mock.patch.object(profile.common, 'load_module', return_value=types.SimpleNamespace()), \
                    mock.patch.object(profile.validation, 'benchmark_callback'), \
                    mock.patch.object(profile.driver, 'run_owned', return_value=failed), \
                    mock.patch.object(profile.common, 'capture_group') as decode:
                with self.assertRaisesRegex(RuntimeError, 'cleanup incomplete'):
                    profile.validate(argparse.Namespace(root=root, source=root))
            decode.assert_not_called()


if __name__ == '__main__':
    unittest.main()
