"""Offline adapter checks. All runtime and perf calls use mocks."""

import argparse
import contextlib
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock

import run_profile


class FakeResource:
    RLIMIT_NOFILE = 7
    RLIM_INFINITY = -1

    def __init__(self, soft, hard):
        self.limits = (soft, hard)
        self.changes = []
        self.reads = 0

    def getrlimit(self, limit):
        if limit != self.RLIMIT_NOFILE:
            raise AssertionError("Only RLIMIT_NOFILE may be read")
        self.reads += 1
        return self.limits

    def setrlimit(self, limit, values):
        if limit != self.RLIMIT_NOFILE:
            raise AssertionError("Only RLIMIT_NOFILE may be changed")
        self.changes.append((limit, values))
        self.limits = tuple(values)


class PortableChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        archive_path = Path(__file__).resolve().parent / "portable-original-v2.tar.gz"
        if not archive_path.is_file():
            archive_path = Path(__file__).resolve().parents[1] / (
                "fts-expr-cache-validation-20261111/inputs/"
                "fts-portable-original-benchmark-20261111-v2.tar.gz"
            )
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                relative = Path(*Path(member.name).parts[1:])
                target = cls.root / relative
                if not target.resolve().is_relative_to(cls.root):
                    raise AssertionError("Fixture escapes its temporary directory")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_real_original_bundle_checks_offline_without_changes(self):
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        verification = run_profile.verify_portable(self.root)
        self.assertEqual(verification["originalInputCount"], 75)
        self.assertEqual(verification["payloadFileCount"], 81)
        with mock.patch.object(run_profile.sys, "platform", "win32"), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run_profile.main(["--portable-root", str(self.root)]), 0)
        self.assertEqual(json.loads(output.getvalue())["databaseStarts"], 0)
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_original_input_change_is_rejected(self):
        manifest = json.loads((self.root / "bundle-manifest.json").read_text())
        target = self.root / "inputs" / next(iter(manifest["originalInputs"]))
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\n")
            with self.assertRaisesRegex(run_profile.AdapterError, "payload mismatch"):
                run_profile.verify_portable(self.root)
        finally:
            target.write_bytes(original)

    def test_runner_change_is_rejected_before_loading(self):
        path = self.root / "runner/run_pair.py"
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"\n")
            with mock.patch.object(run_profile, "load_driver") as load:
                with self.assertRaisesRegex(run_profile.AdapterError, "identity mismatch"):
                    run_profile.main(["--portable-root", str(self.root), "--mode", "run"])
                load.assert_not_called()
        finally:
            path.write_bytes(original)

    def test_non_linux_runtime_rejected_before_loading_driver(self):
        with mock.patch.object(run_profile.sys, "platform", "win32"), \
                mock.patch.object(run_profile, "load_driver") as load:
            with self.assertRaisesRegex(run_profile.AdapterError, "require Linux"):
                run_profile.main(["--portable-root", str(self.root), "--mode", "run"])
            load.assert_not_called()

    def test_preflight_raises_low_soft_limit_before_original_resource_gate(self):
        resource = FakeResource(1024, 1048576)
        with mock.patch.dict(sys.modules, {"resource": resource}):
            module = run_profile.load_driver(self.root)
        with tempfile.TemporaryDirectory() as directory:
            owned = Path(directory) / "preflight"

            def original_preflight(argv):
                args = module.parser().parse_args(argv)
                owned.mkdir()
                snapshot = {
                    "effectiveMemAvailableBytes": 16 * 1024**3,
                    "diskFreeBytes": 20 * 1024**3,
                    "affinity": [0, 1],
                    "nofileLimit": list(resource.getrlimit(resource.RLIMIT_NOFILE)),
                    "interfaces": "header\nheader\neth0: 0 0\n",
                }
                try:
                    module.require_resources(snapshot, args)
                    report = {"status": "runtime_preflight_passed_no_database_started", "roles": {}}
                    result = 0
                except module.Blocked as error:
                    report = {"status": "blocked", "error": str(error), "roles": {}}
                    result = 1
                module.dump(owned / "report.json", report)
                return result

            module.main = original_preflight
            with mock.patch.object(run_profile.sys, "platform", "linux"), \
                    mock.patch.object(run_profile, "load_driver", return_value=module), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = run_profile.main([
                    "--portable-root", str(self.root), "--mode", "preflight", "--run-root", str(owned),
                ])
            self.assertEqual(result, 0)
            self.assertEqual(resource.limits, (8192, 1048576))
            summary = json.loads((owned / "profiling-adapter-report.json").read_text())
            self.assertEqual(summary["fileDescriptorLimit"]["before"], [1024, 1048576])
            self.assertEqual(summary["fileDescriptorLimit"]["after"], [8192, 1048576])
            self.assertEqual(summary["fileDescriptorLimit"]["scope"], "adapter_process_and_future_descendants")
            self.assertEqual(json.loads((owned / "report.json").read_text())["roles"], {})

    def test_infinite_soft_limit_passes_real_original_gate_after_adjustment(self):
        resource = FakeResource(FakeResource.RLIM_INFINITY, FakeResource.RLIM_INFINITY)
        with mock.patch.dict(sys.modules, {"resource": resource}):
            module = run_profile.load_driver(self.root)
        args = module.parser().parse_args([])
        snapshot = {
            "effectiveMemAvailableBytes": 16 * 1024**3,
            "diskFreeBytes": 20 * 1024**3,
            "affinity": [0, 1],
            "nofileLimit": list(resource.limits),
            "interfaces": "header\nheader\neth0: 0 0\n",
        }
        with self.assertRaisesRegex(module.Blocked, "open-file limit"):
            module.require_resources(snapshot, args)
        run_profile.prepare_nofile_limit(resource, {})
        snapshot["nofileLimit"] = list(resource.limits)
        module.require_resources(snapshot, args)
        self.assertEqual(resource.limits, (8192, FakeResource.RLIM_INFINITY))

    def test_insufficient_hard_limit_blocks_before_original_driver(self):
        resource = FakeResource(1024, 4096)
        with mock.patch.dict(sys.modules, {"resource": resource}):
            module = run_profile.load_driver(self.root)
        module.main = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            owned = Path(directory) / "preflight"
            with mock.patch.object(run_profile.sys, "platform", "linux"), \
                    mock.patch.object(run_profile, "load_driver", return_value=module), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                result = run_profile.main([
                    "--portable-root", str(self.root), "--mode", "preflight", "--run-root", str(owned),
                ])
            self.assertEqual(result, 2)
            module.main.assert_not_called()
            self.assertFalse(owned.exists())
            self.assertEqual(resource.changes, [])
            summary = json.loads(output.getvalue())
            self.assertIn("hard limit 4096", summary["error"])
            self.assertEqual(summary["fileDescriptorLimit"]["before"], [1024, 4096])
            self.assertEqual(summary["fileDescriptorLimit"]["after"], [1024, 4096])

    def test_real_original_collect_cli_exports_raw_perf_and_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            owned = parent / "run"
            capture = owned / "profiling/candidate-benchmark"
            capture.mkdir(parents=True)
            (owned / "candidate").mkdir()
            (owned / ".fts-owned-run.json").write_text(json.dumps({"bundle": str(self.root.resolve())}))
            (owned / "report.json").write_text(json.dumps({"status": "original_full_pair_completed_local_only", "roles": {}}))
            original = {
                "profiling/candidate-benchmark/perf.data": b"PERFILE2\x00complete raw fixture\xff",
                "profiling/candidate-benchmark/proc-samples.jsonl": b'{"pid": 123, "utime_ticks": 2}\n',
                "profiling/candidate-benchmark/capture-marks.jsonl": b'{"label": "benchmark_stage"}\n',
                "profiling/candidate-benchmark/capture-summary.json": b'{"scope": {"pid": 123, "startTicks": 456}}\n',
                "candidate/owned-process.json": b'{"pid": 123, "exe": "/opt/seekdb", "state": {"pid": 123, "startTicks": 456}}\n',
                "runtime-verification.json": b'{"candidate": {"buildId": "fixture_only"}}\n',
            }
            for relative, data in original.items():
                (owned / relative).write_bytes(data)
            out = parent / "evidence"
            # Only the unused OS import is isolated. Actual collector functions
            # and the complete original pinned source run without alterations.
            with mock.patch.dict(sys.modules, {"resource": types.ModuleType("resource")}), \
                    mock.patch.object(run_profile.sys, "platform", "linux"), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = run_profile.main([
                    "--portable-root", str(self.root), "--mode", "collect",
                    "--run-root", str(owned), "--evidence-out", str(out),
                ])
            self.assertEqual(result, 0)
            self.assertFalse(out.with_name(out.name + ".partial").exists())
            collection = json.loads((out / "collection.json").read_text())
            self.assertTrue(collection["profilingRawFilesIncluded"])
            archive_path = out / "profiling-raw.tar.gz"
            recorded = next(row for row in collection["files"] if row["path"] == archive_path.name)
            self.assertEqual(recorded["sha256"], run_profile.digest(archive_path))
            with tarfile.open(archive_path, "r:gz") as archive:
                manifest = json.loads(archive.extractfile("profiling-payload-manifest.json").read())
                self.assertEqual(len(manifest["files"]), len(original))
                self.assertEqual(manifest["originalInventorySha256"], run_profile.INVENTORY_SHA256)
                for row in manifest["files"]:
                    data = archive.extractfile(row["path"]).read()
                    self.assertEqual(data, original[row["path"]])
                    self.assertEqual(len(data), row["bytes"])
                    self.assertEqual(hashlib.sha256(data).hexdigest(), row["sha256"])
            for relative, expected in original.items():
                self.assertEqual((owned / relative).read_bytes(), expected)

    def test_collect_failure_preserves_partial_and_original_raw_data(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            owned = parent / "run"
            capture = owned / "profiling/candidate-benchmark"
            capture.mkdir(parents=True)
            raw = capture / "perf.data"
            raw.write_bytes(b"complete raw data")
            (owned / "report.json").write_text(json.dumps({"roles": {}}))
            with mock.patch.dict(sys.modules, {"resource": types.ModuleType("resource")}):
                module = run_profile.load_driver(self.root)
            run_profile.install_collection_hook(module)
            out = parent / "evidence"
            with mock.patch.object(module, "bounded_tar_add", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    module.collect(owned, out, {})
            self.assertFalse(out.exists())
            self.assertTrue(out.with_name(out.name + ".partial").is_dir())
            self.assertEqual(raw.read_bytes(), b"complete raw data")

    def test_collect_existing_destination_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            owned = parent / "run"
            owned.mkdir()
            out = parent / "evidence"
            out.mkdir()
            previous = out / "previous.txt"
            previous.write_bytes(b"previous evidence")
            with mock.patch.dict(sys.modules, {"resource": types.ModuleType("resource")}):
                module = run_profile.load_driver(self.root)
            run_profile.install_collection_hook(module)
            with self.assertRaisesRegex(run_profile.AdapterError, "Fresh evidence"):
                module.collect(owned, out, {})
            self.assertEqual(previous.read_bytes(), b"previous evidence")


class HookChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "run"
        self.bundle = Path(self.temporary.name) / "portable"
        self.args = types.SimpleNamespace(
            run_root=self.root,
            total_timeout_seconds=3900,
            window_deadline_utc="2030-01-01T02:00:00+00:00",
        )
        self.events = []
        self.calls = []
        self.now = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
        self.module = types.SimpleNamespace(
            resource=FakeResource(8192, 1048576),
            checked=self.original_checked,
            comparison=mock.Mock(),
            required_pair_budget=lambda args: 2740,
            CLEANUP_RESERVE_SECONDS=340,
            guard_window=mock.Mock(return_value={}),
            emit=mock.Mock(),
            instant=dt.datetime.fromisoformat,
            now=lambda: self.now,
            runtime_env=lambda args: {"PATH": "/usr/bin"},
        )
        events = self.events

        class FakeCapture:
            def __init__(self, owned_process_path, output_dir, *, benchmark_log=None):
                self.owned_process_path = owned_process_path
                self.output_dir = output_dir
                self.benchmark_log = benchmark_log
                self.summary = {"status": "raw_recorded", "command": ["perf", "record"]}
                events.append("capture_created")

            def __enter__(self):
                events.append("capture_started")
                return self

            def mark(self, label, **fields):
                events.append(label)

            def __exit__(self, kind, error, traceback):
                events.append("capture_stopped")
                return False

        self.capture_factory = FakeCapture

    def original_checked(self, command, output, env, timeout=30, input_bytes=None):
        self.calls.append((command, output, env, timeout, input_bytes))
        self.events.append("original_checked")
        return {"exitCode": 0}

    def install(self):
        return run_profile.install_hooks(self.module, self.bundle, self.args, self.capture_factory, 20)

    def benchmark_command(self):
        return ["bash", self.bundle / "inputs/tools/benchmark/fts_large_bench.sh"]

    def test_only_exact_candidate_original_benchmark_is_captured(self):
        state = self.install()
        command = self.benchmark_command()
        environment = {"ROWS": "20000", "ROUNDS": "3000"}
        for role in ("baseline-tiny", "baseline"):
            self.module.checked(command, self.root / role / "benchmark.log", environment, 600)
        self.module.checked(["bash", "another.sh"], self.root / "candidate/benchmark.log", environment, 600)
        self.module.checked(command, self.root / "candidate/report.txt", environment, 600)
        self.module.checked(command, self.root.parent / "other/candidate/benchmark.log", environment, 600)
        self.assertIsNone(state["capture"])
        receipt = self.module.checked(command, self.root / "candidate/benchmark.log", environment, 600)
        self.assertEqual(receipt, {"exitCode": 0})
        self.assertEqual(state["capture"].owned_process_path, self.root / "candidate/owned-process.json")
        self.assertEqual(state["capture"].output_dir, self.root / "profiling/candidate-benchmark")
        self.assertEqual(state["capture"].benchmark_log, self.root / "candidate/benchmark.log")
        self.assertEqual(self.events.count("capture_started"), 1)
        self.assertEqual(self.calls[-1], (command, self.root / "candidate/benchmark.log", environment, 600, None))
        self.assertEqual(environment, {"ROWS": "20000", "ROUNDS": "3000"})

    def test_capture_stops_before_driver_database_cleanup_on_error(self):
        def failed(*args):
            self.events.append("benchmark_failed")
            raise RuntimeError("benchmark failure")
        self.module.checked = failed
        self.install()
        with self.assertRaisesRegex(RuntimeError, "benchmark failure"):
            try:
                self.module.checked(self.benchmark_command(), self.root / "candidate/benchmark.log", {}, 600)
            finally:
                self.events.append("original_driver_database_cleanup")
        self.assertLess(self.events.index("capture_stopped"), self.events.index("original_driver_database_cleanup"))

    def test_original_cleanup_commands_remain_uncaptured(self):
        state = self.install()
        for name in ("own-stop.log", "own-destroy.log"):
            self.module.checked(["python", "sdb.py", "stop"], self.root / "candidate" / name, {}, 65)
        self.assertIsNone(state["capture"])
        self.assertEqual(len(self.calls), 2)

    def test_comparison_contains_diagnostic_scope_without_gain_fields(self):
        original_comparison = self.module.comparison
        self.install()
        comparison = self.module.comparison({"roles": {"candidate": {"metrics": {"query_cn_avg_ms": 2}}}})
        original_comparison.assert_not_called()
        self.assertFalse(comparison["performanceComparisonComputed"])
        self.assertTrue(comparison["candidateBenchmarkInstrumented"])
        self.assertEqual(comparison["samplingOverhead"], run_profile.OVERHEAD_NOTE)
        self.assertNotIn("improvement", json.dumps(comparison).lower())
        self.assertNotIn("ratio", json.dumps(comparison).lower())

    def test_capture_reserve_extends_existing_budget_and_cleanup_margin(self):
        self.install()
        self.assertEqual(self.module.required_pair_budget(self.args), 2760)
        self.assertEqual(self.module.CLEANUP_RESERVE_SECONDS, 360)
        self.args.window_deadline_utc = (self.now + dt.timedelta(seconds=3949)).isoformat()
        with self.assertRaisesRegex(run_profile.AdapterError, "UTC stop deadline"):
            self.module.guard_window(self.args)
        self.args.window_deadline_utc = (self.now + dt.timedelta(seconds=3950)).isoformat()
        self.assertEqual(self.module.guard_window(self.args)["profilingCaptureReserveSeconds"], 20)

    def test_emitted_completion_is_labeled_without_mutating_driver_value(self):
        original_emit = self.module.emit
        self.install()
        value = {"status": "original_full_pair_completed_local_only"}
        self.module.emit(value)
        emitted = original_emit.call_args.args[0]
        self.assertEqual(emitted["status"], "raw_capture_completed_verification_pending")
        self.assertEqual(emitted["originalDriverStatus"], value["status"])
        self.assertEqual(value, {"status": "original_full_pair_completed_local_only"})

    def test_postprocessing_waits_for_database_cleanup(self):
        state = self.install()
        self.root.mkdir()
        (self.root / "report.json").write_text(json.dumps({"roles": {"candidate": {"cleanupPassed": False}}}))
        self.module.checked = mock.Mock()
        with self.assertRaisesRegex(run_profile.AdapterError, "database cleanup"):
            run_profile.finish_profile(self.module, self.args, state)
        self.module.checked.assert_not_called()

    def test_postprocessing_decodes_positive_samples_with_timeout(self):
        state = self.install()
        state["capture"] = self.capture_factory(self.root / "candidate/owned-process.json", state["captureDirectory"])
        state["captureDirectory"].mkdir(parents=True)
        report = {"roles": {role: {"cleanupPassed": True} for role in ("baseline-tiny", "baseline", "candidate")}}
        (self.root / "report.json").write_text(json.dumps(report))
        def decoded(command, output, env, timeout):
            self.assertEqual(command[:2], ["perf", "report"])
            self.assertEqual(timeout, 30)
            self.assertEqual(env["LC_ALL"], "C")
            output.write_text("# Samples: 5 of event 'cpu-clock:u'\n  100.00%  5  seekdb  [.] symbol\n")
            return {"exitCode": 0}
        self.module.checked = decoded
        decoded = run_profile.finish_profile(self.module, self.args, state)
        self.assertTrue(decoded["profileVerified"])
        self.assertEqual(decoded["sampleCount"], 5)
        self.assertEqual(json.loads((self.root / "report.json").read_text()), report)

    def test_mock_driver_main_preserves_metrics_and_scorer_then_reports_after_cleanup(self):
        import profile_capture

        parser = argparse.ArgumentParser()
        parser.add_argument("--mode")
        parser.add_argument("--bundle-root")
        parser.add_argument("--run-root", type=Path)
        parser.add_argument("--stop-deadline-utc", dest="window_deadline_utc")
        parser.add_argument("--total-timeout-seconds", type=int, default=3900)
        self.module.parser = lambda: parser
        original_files = {}

        def checked(command, output, env, timeout=30, input_bytes=None):
            if command[:2] == ["perf", "report"]:
                self.assertIn("original_driver_database_cleanup", self.events)
                self.events.append("perf_report")
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("# Samples: 2 of event 'cpu-clock:u'\n  100.00%  2  seekdb [.] tokenize\n")
            else:
                self.events.append("benchmark")
            return {"exitCode": 0}

        def driver_main(argv):
            values = parser.parse_args(argv)
            self.module.guard_window(values)
            self.root.mkdir()
            report = {"status": "original_full_pair_completed_local_only", "roles": {}}
            try:
                self.module.checked(self.benchmark_command(), self.root / "candidate/benchmark.log", {"ROWS": "20000"}, 600)
                for role in ("baseline", "candidate"):
                    directory = self.root / role
                    directory.mkdir(exist_ok=True)
                    metrics = json.dumps({"metric_" + str(index): index for index in range(9)}).encode()
                    score = b'{"score": 100, "config_mismatches": []}\n'
                    for filename, data in (("metrics.json", metrics), ("frozen-reference-score.json", score)):
                        path = directory / filename
                        path.write_bytes(data)
                        original_files[path] = data
                    report["roles"][role] = {"cleanupPassed": False}
            finally:
                self.events.append("original_driver_database_cleanup")
                for row in report["roles"].values():
                    row["cleanupPassed"] = True
                report["sameHostIncrementalComparison"] = self.module.comparison(report)
                (self.root / "report.json").write_text(json.dumps(report))
            self.module.emit({"status": report["status"]})
            return 0

        self.module.checked = checked
        self.module.main = driver_main
        with mock.patch.object(run_profile.sys, "platform", "linux"), \
                mock.patch.object(run_profile, "verify_portable", return_value={"originalInputCount": 75}), \
                mock.patch.object(run_profile, "load_driver", return_value=self.module), \
                mock.patch.object(profile_capture, "Capture", self.capture_factory), \
                contextlib.redirect_stdout(io.StringIO()):
            result = run_profile.main([
                "--portable-root", str(self.bundle), "--mode", "run", "--run-root", str(self.root),
                "--stop-deadline-utc", self.args.window_deadline_utc,
            ])
        self.assertEqual(result, 0)
        summary = json.loads((self.root / "profiling-adapter-report.json").read_text())
        self.assertTrue(summary["profileVerified"])
        self.assertTrue(summary["originalInputsUnchanged"])
        self.assertFalse(summary["performanceComparisonComputed"])
        self.assertEqual(summary["originalDriverStatus"], "original_full_pair_completed_local_only")
        self.assertLess(self.events.index("capture_stopped"), self.events.index("original_driver_database_cleanup"))
        self.assertLess(self.events.index("original_driver_database_cleanup"), self.events.index("perf_report"))
        for path, expected in original_files.items():
            self.assertEqual(path.read_bytes(), expected)


class NoFileLimitChecks(unittest.TestCase):
    def test_sufficient_soft_limit_is_unchanged(self):
        for soft in (8192, 16384):
            with self.subTest(soft=soft):
                resource = FakeResource(soft, 1048576)
                report = {}
                run_profile.prepare_nofile_limit(resource, report)
                self.assertEqual(resource.changes, [])
                self.assertEqual(report["before"], [soft, 1048576])
                self.assertEqual(report["after"], [soft, 1048576])
                self.assertFalse(report["changed"])

    def test_low_soft_limit_is_raised_to_original_gate(self):
        resource = FakeResource(1024, 1048576)
        report = {}
        run_profile.prepare_nofile_limit(resource, report)
        self.assertEqual(resource.changes, [(resource.RLIMIT_NOFILE, (8192, 1048576))])
        self.assertEqual(report["before"], [1024, 1048576])
        self.assertEqual(report["after"], [8192, 1048576])
        self.assertTrue(report["changed"])

    def test_insufficient_hard_limit_fails_without_change(self):
        resource = FakeResource(1024, 4096)
        report = {}
        with self.assertRaisesRegex(run_profile.AdapterError, "hard limit 4096.*8192"):
            run_profile.prepare_nofile_limit(resource, report)
        self.assertEqual(resource.changes, [])
        self.assertEqual(report["before"], report["after"])
        self.assertFalse(report["changed"])

    def test_infinite_hard_limit_is_preserved_when_soft_is_raised(self):
        resource = FakeResource(1024, FakeResource.RLIM_INFINITY)
        report = {}
        run_profile.prepare_nofile_limit(resource, report)
        self.assertEqual(resource.limits, (8192, FakeResource.RLIM_INFINITY))
        self.assertEqual(report["after"], [8192, FakeResource.RLIM_INFINITY])

    def test_infinite_soft_limit_is_made_finite_for_original_gate(self):
        resource = FakeResource(FakeResource.RLIM_INFINITY, FakeResource.RLIM_INFINITY)
        report = {}
        run_profile.prepare_nofile_limit(resource, report)
        self.assertEqual(resource.changes, [(resource.RLIMIT_NOFILE, (8192, FakeResource.RLIM_INFINITY))])
        self.assertEqual(report["before"], [FakeResource.RLIM_INFINITY, FakeResource.RLIM_INFINITY])
        self.assertEqual(report["after"], [8192, FakeResource.RLIM_INFINITY])
        self.assertTrue(report["changed"])

    def test_extract_and_collect_do_not_read_or_change_limits(self):
        for mode in ("extract", "collect"):
            with self.subTest(mode=mode):
                resource = FakeResource(1024, 1048576)
                parser = argparse.ArgumentParser()
                parser.add_argument("--mode")
                parser.add_argument("--bundle-root")
                parser.add_argument("--run-root", type=Path)
                module = types.SimpleNamespace(
                    resource=resource, parser=lambda: parser, collect=mock.Mock(), main=mock.Mock(return_value=0),
                )
                with mock.patch.object(run_profile.sys, "platform", "linux"), \
                        mock.patch.object(run_profile, "verify_portable", return_value={"originalInputCount": 75}), \
                        mock.patch.object(run_profile, "load_driver", return_value=module), \
                        contextlib.redirect_stdout(io.StringIO()):
                    result = run_profile.main(["--portable-root", "unused-fixture", "--mode", mode])
                self.assertEqual(result, 0)
                self.assertEqual(resource.reads, 0)
                self.assertEqual(resource.changes, [])


class SampleChecks(unittest.TestCase):
    def test_zero_samples_fail(self):
        with self.assertRaisesRegex(run_profile.AdapterError, "zero samples"):
            run_profile.report_samples("# Samples: 0 of event 'cpu-clock:u'\n")

    def test_unrecognized_report_is_unverified(self):
        with self.assertRaisesRegex(run_profile.AdapterError, "positive integer"):
            run_profile.report_samples("# perf version 6.8\n")

    def test_human_header_without_integer_rows_stays_unverified(self):
        with self.assertRaisesRegex(run_profile.AdapterError, "positive integer"):
            run_profile.report_samples("# Samples: 1.2K of event 'cpu-clock:u'\n")

    def test_sample_rows_sum_exact_counts(self):
        result = run_profile.report_samples("  80.00%  800  seekdb [.] symbol\n  20.00%  200  libc [.] symbol\n")
        self.assertEqual(result["sampleCount"], 1000)

    def test_unknown_symbol_and_dso_fractions_are_reported(self):
        result = run_profile.report_samples(
            "  75.00%  3  seekdb [.] tokenizer\n  25.00%  1  [unknown] [.] 0x00001234\n"
        )
        self.assertEqual(result["resolvedSymbolSamples"], 3)
        self.assertEqual(result["unknownSymbolFraction"], 0.25)
        self.assertEqual(result["unknownDsoFraction"], 0.25)
        self.assertEqual(result["functionHotspots"][0]["symbol"], "tokenizer")


if __name__ == "__main__":
    unittest.main()
