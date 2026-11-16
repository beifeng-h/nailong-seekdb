import struct
import tempfile
import json
from pathlib import Path
import types
import unittest
from unittest import mock

import fts_tokenize_focused_profile as focused


class FocusedTokenizeProfileTests(unittest.TestCase):
    def test_original_statements_and_changed_template(self):
        script = (Path(__file__).resolve().parents[3] / 'tools/benchmark/fts_large_bench.sh').read_text(encoding='utf-8')
        statements = focused.benchmark_statements(script)
        self.assertEqual(set(statements), {'ik', 'beng'})
        self.assertIn("', 'ik');", statements['ik'])
        self.assertIn("', 'beng');", statements['beng'])
        with self.assertRaisesRegex(RuntimeError, 'structure changed'):
            focused.benchmark_statements(script.replace('run_tokenize_bench "tokenize_ik"',
                                                        'run_tokenize_changed "tokenize_ik"'))

    @staticmethod
    def perf_file(path, extra_record=b''):
        def sample(pid, tid, stamp):
            payload = struct.pack('<QIIQ', 0x1234, pid, tid, stamp)
            return struct.pack('<IHH', 9, 0, len(payload) + 8) + payload
        records = sample(42, 101, 1000) + sample(42, 101, 2000) + sample(42, 102, 3000) + extra_record
        header = struct.pack('<8s12Q', b'PERFILE2', 104, 48, 104, 48, 152, len(records), 0, 0, 0, 0, 0, 0)
        attr = struct.pack('<IIQQQQQ', 1, 0, 0, 99, 7, 0, 0)
        path.write_bytes(header + attr + records)

    def test_raw_windows_and_lost_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'perf.data'
            self.perf_file(path)
            self.assertEqual(dict(focused.raw_samples(path, 42, 1500, 3000)), {101: 1})
            with self.assertRaisesRegex(RuntimeError, 'unowned'):
                focused.raw_samples(path, 99, 0, 4000)
            self.perf_file(path, struct.pack('<IHH', 2, 0, 8))
            with self.assertRaisesRegex(RuntimeError, 'lost'):
                focused.raw_samples(path, 42, 0, 4000)

    def test_request_coverage_excludes_reused_thread_identity(self):
        def sample(time, ticks, start=8):
            return dict(monotonic_ns=time, clock_ticks_per_second=100,
                        ownedThreads={'101': dict(comm='ReqWorker1', userTicks=ticks, startTicks=start),
                                      '102': dict(comm='Timer', userTicks=1000, startTicks=9)})
        processes = [sample(10, 10), sample(20, 110), sample(30, 210), sample(40, 500, start=99)]
        coverage = focused.request_coverage(processes, {101: 150, 102: 300}, 5, 50)
        self.assertEqual(coverage['observedUserTicks'], 200)
        self.assertEqual(coverage['requestSamples'], 150)
        self.assertTrue(coverage['passed'])
        self.assertFalse(focused.request_coverage(processes, {101: 20}, 5, 50)['passed'])

    def test_failed_instance_cleanup_prevents_profile_decoding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'runtime/baseline').mkdir(parents=True)
            source = root / 'sources/baseline/tools/benchmark'
            source.mkdir(parents=True)
            script = Path(__file__).resolve().parents[3] / 'tools/benchmark/fts_large_bench.sh'
            (source / script.name).write_bytes(script.read_bytes())
            (root / 'runtime/baseline/identity.json').write_text(json.dumps(dict(
                binary=str(root / 'seekdb'), sha256='0' * 64, bytes=1, buildId='test')))
            (root / 'report.json').write_text(json.dumps(dict(
                status='prepared', plan={'candidateBuildsRequired': 0}, instances=[])))
            failed = dict(name='focused-tokenize', status='passed', cleanupPassed=False)
            capture = types.SimpleNamespace(_proc_snapshot=mock.Mock())
            with mock.patch.object(focused.validation, 'prepare_nofile'), \
                    mock.patch.object(focused.driver, 'safe_elf'), \
                    mock.patch.object(focused.common, 'load_module', return_value=capture), \
                    mock.patch.object(focused.driver, 'run_owned', return_value=failed), \
                    mock.patch.object(focused.common, 'capture_group') as decode:
                with self.assertRaisesRegex(RuntimeError, 'cleanup incomplete'):
                    focused.validate(types.SimpleNamespace(root=root, source=root))
            decode.assert_not_called()


if __name__ == '__main__':
    unittest.main()
