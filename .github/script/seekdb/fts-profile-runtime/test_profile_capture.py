"""Offline lifecycle and identity tests, including on Windows."""

import json
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import profile_capture as capture


IDENTITY = {"pid": 2345, "exe": "/opt/seekdb/bin/observer", "startTicks": 76543}
STAT = {"startTicks": 76543, "utime_ticks": 200, "stime_ticks": 30, "rss_pages": 12}
REAL_THREAD = threading.Thread


class FakeThread:
    def __init__(self, **kwargs):
        self.target = kwargs["target"]

    def start(self):
        pass

    def join(self, timeout):
        pass

    def is_alive(self):
        return False


class FakeProcess:
    pid = 9876

    def __init__(self, command, **kwargs):
        self.command = command
        self.options = kwargs
        self.returncode = None
        self.waits = []
        self.ignore_signals = set()
        Path(command[-1]).write_bytes(b"PERFILE2-test-samples")
        kwargs["stderr"].write("[ perf record: Captured and wrote 0.1 MB (7 samples) ]\n")
        kwargs["stderr"].flush()

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode

    def signal_group(self, pid, number):
        if pid != self.pid:
            raise AssertionError("Only the owned perf process group may be signalled")
        if int(number) not in self.ignore_signals:
            self.returncode = 0


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.owner = self.directory / "owned-process.json"
        self.owner.write_text(json.dumps({"pid": IDENTITY["pid"], "exe": IDENTITY["exe"],
                                          "state": {"pid": IDENTITY["pid"], "startTicks": IDENTITY["startTicks"]}}), encoding="utf-8")
        self.output = self.directory / "capture"
        self.process = None
        self.snapshot = {"scope": IDENTITY, "monotonic_ns": 1234, **STAT,
                         "rss_bytes": 49152, "io": {"read_bytes": 42}}
        for patcher in (
            mock.patch.object(capture.sys, "platform", "linux"),
            mock.patch.object(capture.os, "sysconf", return_value=100, create=True),
            mock.patch.object(capture, "_proc_snapshot", return_value=self.snapshot),
            mock.patch.object(capture.subprocess, "run", return_value=subprocess.CompletedProcess(["perf"], 0, "perf version 6.8\n", "")),
            mock.patch.object(capture.subprocess, "Popen", side_effect=self.start_process),
            mock.patch.object(capture.os, "killpg", side_effect=self.signal_group, create=True),
            mock.patch.object(capture.threading, "Thread", FakeThread),
            mock.patch.object(capture, "STARTUP_CONFIRM_SECONDS", 0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def start_process(self, *args, **kwargs):
        self.process = FakeProcess(*args, **kwargs)
        return self.process

    def signal_group(self, pid, number):
        self.process.signal_group(pid, number)

    def summary(self):
        return json.loads((self.output / "capture-summary.json").read_text(encoding="utf-8"))

    def test_command_normal_stop_scope_and_marks(self):
        with capture.PerfCapture(self.owner, self.output) as recording:
            recording.mark("benchmark", samples=75)
        self.assertEqual(self.process.command, [
            "perf", "record", "-N", "-p", "2345", "-F", "99", "-e", "cpu-clock:u",
            "--call-graph", "dwarf,16384", "--clockid", "mono", "-o", str(self.output / "perf.data"),
        ])
        self.assertTrue(self.process.options["start_new_session"])
        self.assertEqual(self.process.options["env"]["LC_ALL"], "C")
        self.assertEqual(recording.summary["status"], "raw_recorded")
        self.assertTrue(recording.summary["perf_reaped"])
        self.assertEqual(recording.summary["stop_signals"], [int(signal.SIGINT)])
        self.assertEqual(recording.summary["sample_count"], 7)
        self.assertFalse(recording.summary["profile_verified"])
        for output in recording.summary["outputs"].values():
            self.assertEqual(output["scope"], IDENTITY)
        mark = json.loads((self.output / "capture-marks.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(mark["scope"], IDENTITY)
        self.assertEqual(mark["fields"], {"samples": 75})

    def test_conflicting_owner_pid_rejected_before_perf(self):
        document = json.loads(self.owner.read_text(encoding="utf-8"))
        document["state"]["pid"] += 1
        self.owner.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(capture.CaptureError, "conflicting PIDs"):
            with capture.Capture(self.owner, self.output):
                self.fail("The benchmark must not start")
        self.assertIsNone(self.process)
        self.assertEqual(self.summary()["status"], "failed")

    def test_identity_mismatch_rejected_before_perf(self):
        with mock.patch.object(capture, "_proc_snapshot", side_effect=capture.CaptureError("PID reused")):
            with self.assertRaisesRegex(capture.CaptureError, "PID reused"):
                with capture.Capture(self.owner, self.output):
                    self.fail("The benchmark must not start")
        self.assertIsNone(self.process)

    def test_popen_failure_is_reported(self):
        with mock.patch.object(capture.subprocess, "Popen", side_effect=OSError("cannot execute perf")):
            with self.assertRaisesRegex(capture.CaptureError, "cannot execute perf"):
                with capture.Capture(self.owner, self.output):
                    self.fail("The benchmark must not start")
        self.assertEqual(self.summary()["status"], "failed")

    def test_missing_perf_is_reported_before_launch(self):
        with mock.patch.object(capture.subprocess, "run", side_effect=FileNotFoundError("perf unavailable")):
            with self.assertRaisesRegex(capture.CaptureError, "perf unavailable"):
                with capture.Capture(self.owner, self.output):
                    self.fail("The benchmark must not start")
        self.assertIsNone(self.process)
        self.assertEqual(self.summary()["status"], "failed")

    def test_permission_denied_early_exit_is_reported_and_reaped(self):
        original_start = self.start_process
        def denied(*args, **kwargs):
            process = original_start(*args, **kwargs)
            kwargs["stderr"].write("Operation not permitted\n")
            kwargs["stderr"].flush()
            process.returncode = 255
            return process
        with mock.patch.object(capture.subprocess, "Popen", side_effect=denied):
            with self.assertRaisesRegex(capture.CaptureError, "perf exited"):
                with capture.Capture(self.owner, self.output):
                    self.fail("The benchmark must not start")
        summary = self.summary()
        self.assertIn("Operation not permitted", summary["perf_stderr_tail"])
        self.assertTrue(summary["perf_reaped"])
        self.assertEqual(summary["stop_signals"], [])

    def test_body_exception_propagates_after_owned_child_cleanup(self):
        with self.assertRaisesRegex(ValueError, "benchmark failed"):
            with capture.Capture(self.owner, self.output):
                raise ValueError("benchmark failed")
        self.assertTrue(self.summary()["perf_reaped"])
        self.assertEqual(self.summary()["status"], "failed")

    def test_post_launch_identity_failure_cleans_up_on_enter(self):
        with mock.patch.object(capture, "_proc_snapshot", side_effect=[self.snapshot, capture.CaptureError("PID reused")]):
            with self.assertRaisesRegex(capture.CaptureError, "PID reused"):
                with capture.Capture(self.owner, self.output):
                    self.fail("The benchmark must not start")
        self.assertTrue(self.summary()["perf_reaped"])

    def test_escalation_is_bounded_and_never_reported_as_complete(self):
        with self.assertRaisesRegex(capture.CaptureError, "escalation"):
            with capture.Capture(self.owner, self.output):
                self.process.ignore_signals.add(int(signal.SIGINT))
        self.assertEqual(self.process.waits, [5, 3])
        self.assertTrue(self.summary()["perf_reaped"])
        self.assertEqual(self.summary()["status"], "failed")

    def test_empty_or_missing_perf_data_is_failure(self):
        with self.assertRaisesRegex(capture.CaptureError, "nonempty perf.data"):
            with capture.Capture(self.owner, self.output):
                (self.output / "perf.data").unlink()

    def test_zero_length_perf_data_is_failure(self):
        with self.assertRaisesRegex(capture.CaptureError, "nonempty perf.data"):
            with capture.Capture(self.owner, self.output):
                (self.output / "perf.data").write_bytes(b"")

    def test_zero_sample_summary_is_failure(self):
        with self.assertRaisesRegex(capture.CaptureError, "zero samples"):
            with capture.Capture(self.owner, self.output) as recording:
                stream = recording._files["perf.stderr.log"]
                stream.write("[ perf record: Captured and wrote 0 MB (0 samples) ]\n")
                stream.flush()

    def test_unknown_sample_count_remains_unverified_for_external_report(self):
        with capture.Capture(self.owner, self.output) as recording:
            stream = recording._files["perf.stderr.log"]
            stream.seek(0)
            stream.truncate()
            stream.write("Recorded data; this perf version uses another summary format.\n")
            stream.flush()
        self.assertIsNone(self.summary()["sample_count"])
        self.assertEqual(self.summary()["status"], "raw_recorded_sample_count_unverified")
        self.assertFalse(self.summary()["profile_verified"])

    def test_sampler_failure_is_reported_after_cleanup(self):
        with self.assertRaisesRegex(capture.CaptureError, "identity lost"):
            with capture.Capture(self.owner, self.output) as recording:
                with mock.patch.object(capture, "_proc_snapshot", side_effect=capture.CaptureError("identity lost")), \
                        mock.patch.object(recording._stop, "wait", return_value=False):
                    recording._sample_loop()
        self.assertTrue(self.summary()["perf_reaped"])

    def test_sampler_uses_250_millisecond_intervals(self):
        with capture.Capture(self.owner, self.output) as recording:
            with mock.patch.object(recording._stop, "wait", side_effect=[False, False, True]) as wait:
                recording._sample_loop()
            self.assertEqual(wait.call_args_list, [mock.call(0.25)] * 3)
        self.assertEqual(self.summary()["proc_sample_count"], 3)

    def test_thread_start_failure_still_reaps_perf(self):
        with mock.patch.object(FakeThread, "start", side_effect=RuntimeError("thread unavailable")), \
                mock.patch.object(FakeThread, "join", side_effect=RuntimeError("not started")):
            with self.assertRaisesRegex(capture.CaptureError, "thread unavailable"):
                with capture.Capture(self.owner, self.output):
                    self.fail("The benchmark must not start")
        self.assertTrue(self.summary()["perf_reaped"])

    def test_unreapable_child_exhausts_only_the_bounded_stop_budget(self):
        with self.assertRaisesRegex(capture.CaptureError, "could not be reaped"):
            with capture.Capture(self.owner, self.output):
                self.process.ignore_signals.update((int(signal.SIGINT), int(signal.SIGTERM), getattr(signal, "SIGKILL", 9)))
        self.assertEqual(self.process.waits, [5, 3, 2])
        self.assertFalse(self.summary()["perf_reaped"])

    def test_existing_output_directory_is_never_overwritten(self):
        self.output.mkdir()
        data = self.output / "perf.data"
        data.write_bytes(b"previous evidence")
        with self.assertRaisesRegex(capture.CaptureError, "fresh capture directory"):
            with capture.Capture(self.owner, self.output):
                self.fail("The benchmark must not start")
        self.assertEqual(data.read_bytes(), b"previous evidence")

    def test_non_linux_platform_fails_explicitly(self):
        with mock.patch.object(capture.sys, "platform", "win32"):
            with self.assertRaisesRegex(capture.CaptureError, "requires Linux"):
                with capture.Capture(self.owner, self.output):
                    self.fail("The benchmark must not start")
        self.assertIsNone(self.process)

    def marks(self):
        path = self.output / "capture-marks.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_benchmark_log_can_be_created_after_capture_starts(self):
        benchmark_log = self.directory / "benchmark.log"
        with capture.Capture(self.owner, self.output, benchmark_log=benchmark_log) as recording:
            self.assertEqual(self.marks(), [])
            raw = b"[11:12:13] [1/5] setup raw schema ...\n"
            benchmark_log.write_bytes(raw)
            recording._poll_benchmark_log()
            recording._poll_benchmark_log()
            self.assertEqual(benchmark_log.read_bytes(), raw)
        stages = [mark for mark in self.marks() if mark["label"] == "benchmark_stage"]
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0]["fields"]["stage"], "[1/5]")
        self.assertEqual(stages[0]["fields"]["raw_label"], raw.decode().rstrip("\n"))
        self.assertEqual(stages[0]["scope"], IDENTITY)
        self.assertIn("not exact operator timings", self.summary()["stage_boundary_precision"])

    def test_partial_lines_and_utf8_bytes_are_retained_until_newline(self):
        benchmark_log = self.directory / "benchmark.log"
        raw = "[3/5] build 全文 indexes ...\n".encode("utf-8")
        first_part = raw[:raw.index("文".encode("utf-8")) + 1]
        benchmark_log.write_bytes(first_part)
        with capture.Capture(self.owner, self.output, benchmark_log=benchmark_log) as recording:
            self.assertEqual(self.marks(), [])
            self.assertEqual(recording.summary["benchmark_log_pending_bytes"], len(first_part))
            with benchmark_log.open("ab") as stream:
                stream.write(raw[len(first_part):])
            recording._poll_benchmark_log()
        self.assertEqual(benchmark_log.read_bytes(), raw)
        self.assertEqual(self.marks()[0]["fields"]["raw_label"], raw.decode("utf-8").rstrip("\n"))
        self.assertEqual(self.summary()["benchmark_log_pending_bytes"], 0)

    def test_multiple_stages_in_one_read_keep_source_order_and_bytes(self):
        benchmark_log = self.directory / "benchmark.log"
        raw = b"noise\n[3/5] build ...\r\n[4/5] TOKENIZE ...\n[5/5] MATCH ...\n"
        benchmark_log.write_bytes(raw)
        with capture.Capture(self.owner, self.output, benchmark_log=benchmark_log):
            pass
        marks = self.marks()
        self.assertEqual([mark["fields"]["stage"] for mark in marks], ["[3/5]", "[4/5]", "[5/5]"])
        self.assertEqual([mark["monotonic_ns"] for mark in marks], sorted(mark["monotonic_ns"] for mark in marks))
        self.assertEqual(benchmark_log.read_bytes(), raw)

    def test_log_truncation_resets_offset_and_partial_fragment(self):
        benchmark_log = self.directory / "benchmark.log"
        benchmark_log.write_bytes(b"[1/5] old stage with a long label ...\npartial previous line")
        with capture.Capture(self.owner, self.output, benchmark_log=benchmark_log) as recording:
            benchmark_log.write_bytes(b"[2/5] new\n")
            recording._poll_benchmark_log()
        marks = self.marks()
        self.assertEqual([mark["label"] for mark in marks], ["benchmark_stage", "benchmark_log_truncated", "benchmark_stage"])
        self.assertEqual(marks[-1]["fields"]["raw_label"], "[2/5] new")
        self.assertEqual(self.summary()["benchmark_log_truncations"], 1)
        self.assertEqual(benchmark_log.read_bytes(), b"[2/5] new\n")

    def test_final_stage_append_is_collected_before_perf_stops(self):
        benchmark_log = self.directory / "benchmark.log"
        with capture.Capture(self.owner, self.output, benchmark_log=benchmark_log):
            benchmark_log.write_bytes(b"[5/5] MATCH query ...\n")
        self.assertEqual(self.marks()[0]["fields"]["stage"], "[5/5]")

    def test_stage_reader_uses_only_existing_sampler_thread_and_joins_it(self):
        benchmark_log = self.directory / "benchmark.log"
        with mock.patch.object(capture.threading, "Thread", wraps=REAL_THREAD) as thread_factory:
            with capture.Capture(self.owner, self.output, benchmark_log=benchmark_log) as recording:
                sampler = recording._thread
                self.assertTrue(sampler.is_alive())
                benchmark_log.write_bytes(b"[4/5] TOKENIZE hot-path ...\n")
                deadline = time.monotonic() + 2
                while not recording.summary.get("benchmark_stage_count") and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(recording.summary["benchmark_stage_count"], 1)
                recording.mark("benchmark_end")
            self.assertFalse(sampler.is_alive())
            self.assertTrue(recording._stop.is_set())
            self.assertEqual(thread_factory.call_count, 1)
        self.assertEqual([mark["label"] for mark in self.marks()], ["benchmark_stage", "benchmark_end"])


class ProcIdentityTests(unittest.TestCase):
    def test_stat_parser_handles_spaces_and_parentheses_in_comm(self):
        fields = ["S"] + ["0"] * 21
        fields[11], fields[12], fields[19], fields[21] = "200", "30", "76543", "12"
        raw = "2345 (observer worker) test) " + " ".join(fields)
        with mock.patch.object(Path, "read_text", return_value=raw):
            self.assertEqual(capture._read_stat(2345), STAT)

    def test_snapshot_checks_exe_start_time_and_units(self):
        with mock.patch.object(capture, "_read_stat", return_value=STAT), \
                mock.patch.object(capture.os, "readlink", return_value=IDENTITY["exe"]), \
                mock.patch.object(Path, "read_text", return_value="read_bytes: 42\nwrite_bytes: 13\n"):
            snapshot = capture._proc_snapshot(IDENTITY, 100, 4096)
        self.assertEqual(snapshot["rss_bytes"], 49152)
        self.assertEqual(snapshot["utime_seconds"], 2)
        self.assertEqual(snapshot["stime_seconds"], 0.3)
        self.assertEqual(snapshot["io"], {"read_bytes": 42, "write_bytes": 13})

    def test_reused_pid_and_executable_mismatch_are_rejected(self):
        for changed_stat, executable in ((dict(STAT, startTicks=99999), IDENTITY["exe"]), (STAT, "/wrong/observer")):
            with self.subTest(stat=changed_stat, executable=executable), \
                    mock.patch.object(capture, "_read_stat", return_value=changed_stat), \
                    mock.patch.object(capture.os, "readlink", return_value=executable):
                with self.assertRaisesRegex(capture.CaptureError, "identity changed"):
                    capture._proc_snapshot(IDENTITY, 100, 4096)

    def test_pid_reuse_during_io_read_is_rejected(self):
        with mock.patch.object(capture, "_read_stat", side_effect=[STAT, dict(STAT, startTicks=99999)]), \
                mock.patch.object(capture.os, "readlink", return_value=IDENTITY["exe"]), \
                mock.patch.object(Path, "read_text", return_value="read_bytes: 42\n"):
            with self.assertRaisesRegex(capture.CaptureError, "while sampling"):
                capture._proc_snapshot(IDENTITY, 100, 4096)


if __name__ == "__main__":
    unittest.main()
