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
        self.assertFalse(coverage['passed'])
        self.assertEqual(coverage['reusedThreadIds'], [101])
        self.assertTrue(focused.request_coverage(processes[:3], {101: 150}, 5, 50)['passed'])
        self.assertFalse(focused.request_coverage(processes[:3], {101: 20}, 5, 50)['passed'])

    def test_busy_unobserved_worker_invalidates_aggregate_coverage(self):
        def sample(time, one, two):
            return dict(monotonic_ns=time, clock_ticks_per_second=100,
                        ownedThreads={'101': dict(comm='ReqWorker', userTicks=one, startTicks=8),
                                      '102': dict(comm='ReqWorker', userTicks=two, startTicks=9)})
        processes = [sample(10, 0, 0), sample(20, 150, 100), sample(30, 250, 200)]
        coverage = focused.request_coverage(processes, {101: 240}, 5, 40)
        self.assertFalse(coverage['passed'])
        self.assertEqual(coverage['uncoveredBusyThreads'], [102])

    def test_attach_waits_for_stable_active_request_thread_and_output(self):
        probe = focused.WorkerReadiness(start_ns=0)
        thread = lambda ticks, start=1: {'101': dict(comm='ReqWorker', userTicks=ticks, startTicks=start)}
        self.assertFalse(probe.observe(100_000_000, thread(1), 0))
        self.assertFalse(probe.observe(800_000_000, thread(11), 0))
        self.assertFalse(probe.observe(1_200_000_000, thread(21), 100))
        self.assertTrue(probe.observe(1_600_000_000, thread(35), 100))
        self.assertFalse(probe.observe(1_700_000_000, thread(36, start=2), 100))

    def test_readiness_failure_reaps_only_owned_client(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            client = mock.Mock(pid=123)
            call = types.SimpleNamespace(obclient=out / 'obclient', port=7500)
            with mock.patch.object(focused, 'ROUNDS', 1), \
                    mock.patch.object(focused.subprocess, 'Popen', return_value=client), \
                    mock.patch.object(focused, 'wait_for_request_activity', side_effect=RuntimeError('no worker')), \
                    mock.patch.object(focused, 'reap_client', return_value=False) as reap:
                with self.assertRaisesRegex(RuntimeError, 'no worker'):
                    focused.capture_repeated_sql(call, out, 42, out / 'identity.json', out / 'capture',
                                                 mock.Mock(), 'ik', 'SELECT 1;')
            reap.assert_called_once_with(client)

    def test_recording_starts_after_client_activity_and_reaps_after_exit(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            call = types.SimpleNamespace(obclient=out / 'obclient', port=7500)

            class Client:
                pid = 123
                running = True

                def poll(self):
                    return None if self.running else 0

                def wait(self, timeout):
                    events.append('client_finished')
                    output_stream.write(b'result\nresult\n')
                    output_stream.flush()
                    self.running = False
                    return 0

            class Capture:
                def __init__(self, identity, directory):
                    pass

                def __enter__(self):
                    events.append('capture_started')
                    return self

                def mark(self, label, **fields):
                    events.append(label)

                def __exit__(self, *args):
                    events.append('capture_finished')

            client = Client()

            def launch(*args, **kwargs):
                nonlocal output_stream
                output_stream = kwargs['stdout']
                events.append('client_started')
                return client

            def ready(*args):
                events.append('request_active')
                return dict(clientStillRunning=True, readyMonotonicNs=1, observations=[])

            output_stream = None
            with mock.patch.object(focused, 'ROUNDS', 2), \
                    mock.patch.object(focused.subprocess, 'Popen', side_effect=launch), \
                    mock.patch.object(focused, 'wait_for_request_activity', side_effect=ready), \
                    mock.patch.object(focused, 'reap_client', return_value=True):
                output, _ = focused.capture_repeated_sql(call, out, 42, out / 'identity.json', out / 'capture',
                    types.SimpleNamespace(PerfCapture=Capture), 'ik', 'SELECT 1;')
            self.assertEqual(output, 'result\nresult\n')
            self.assertEqual(events, ['client_started', 'request_active', 'capture_started',
                'sample_window_started', 'client_finished', 'sample_window_finished', 'capture_finished'])

    def test_early_client_exit_is_rejected_before_thread_inspection(self):
        with mock.patch.object(focused.profile, 'owned_threads') as threads:
            with self.assertRaisesRegex(RuntimeError, 'client exited'):
                focused.wait_for_request_activity(mock.Mock(poll=mock.Mock(return_value=0)),
                                                  Path('.'), Path('unused'))
        threads.assert_not_called()

    def test_request_thread_hotspot_percent_uses_filtered_sample_count(self):
        profile = dict(sampleCount=137, functionHotspots=[dict(samples=7, overheadPercent=4.49)])
        focused.add_request_thread_fractions(profile)
        self.assertAlmostEqual(profile['functionHotspots'][0]['requestSamplePercent'], 700 / 137)
        self.assertEqual(profile['functionHotspots'][0]['overheadPercent'], 4.49)

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
