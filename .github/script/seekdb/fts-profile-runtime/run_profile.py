#!/usr/bin/env python3
"""Profile the candidate benchmark through the unchanged portable FTS v2 driver."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tarfile
import time
import types


RUNNER_SHA256 = "f9b84e60d711c1878cd5cc65fe01c6c319597574fc66b8736bfacc9d6879fc20"
MANIFEST_SHA256 = "5383dad68d3654cd31d94d43ed745104d961cb5fde6d62a32c861579c187b087"
INVENTORY_SHA256 = "f36e2d1c8aaabd3e9ffc4f8e3635defca6dc3046e3d575e6e73165fa8d80a135"
PERF_REPORT_TIMEOUT_SECONDS = 30
PERF_COMMAND_CLEANUP_SECONDS = 15
PERF_REPORT_RESERVE_SECONDS = PERF_REPORT_TIMEOUT_SECONDS + PERF_COMMAND_CLEANUP_SECONDS + 5
DIAGNOSTIC_SCOPE = "Candidate-only profiling of the original benchmark"
OVERHEAD_NOTE = "Candidate timings include perf and /proc sampling overhead."
MIN_NOFILE_SOFT_LIMIT = 8192
PROCESS_OBSERVATION_WAIT_SECONDS = 30
FOREIGN_ACTIVE_MESSAGE = "Another active workload is present; refuse to start"
FOREIGN_RELATED_MESSAGE = "Foreign SQL/compiler/GPU-related processes present; refuse to start"


class AdapterError(RuntimeError):
    """The profiling adapter could not satisfy its execution contract."""


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def verify_portable(portable_root):
    """Check the exact manifest and the same 75-input inventory as verify_bundle."""
    root = Path(portable_root).resolve()
    runner = root / "runner/run_pair.py"
    manifest_path = root / "bundle-manifest.json"
    for path, expected in ((runner, RUNNER_SHA256), (manifest_path, MANIFEST_SHA256)):
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise AdapterError(f"Original portable file identity mismatch: {path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    inventory = manifest["originalInputs"]
    encoded = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    if len(inventory) != 75 or hashlib.sha256(encoded).hexdigest() != INVENTORY_SHA256:
        raise AdapterError("Original 75-input inventory mismatch")
    for relative, expected in manifest["payloadHashes"].items():
        name = Path(relative)
        path = root / name
        if name.is_absolute() or ".." in name.parts or not path.resolve().is_relative_to(root):
            raise AdapterError(f"Portable member escapes the original bundle: {relative}")
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise AdapterError(f"Original portable payload mismatch: {relative}")
    for relative, expected in inventory.items():
        if manifest["payloadHashes"].get("inputs/" + relative) != expected:
            raise AdapterError(f"Original input is missing from the payload: {relative}")
    return {
        "runnerSha256": RUNNER_SHA256,
        "manifestSha256": MANIFEST_SHA256,
        "originalInputCount": 75,
        "inventorySha256": INVENTORY_SHA256,
        "payloadFileCount": len(manifest["payloadHashes"]),
    }


def load_driver(portable_root):
    path = Path(portable_root) / "runner/run_pair.py"
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != RUNNER_SHA256:
        raise AdapterError("Original portable runner changed before import")
    module = types.ModuleType("fts_portable_profile_driver")
    module.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), module.__dict__)
    module.verify_bundle(Path(portable_root))
    return module


def prepare_nofile_limit(resource, record):
    """Satisfy the original driver's descriptor gate for this process tree."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    record.update(
        scope="adapter_process_and_future_descendants",
        requiredSoftLimit=MIN_NOFILE_SOFT_LIMIT,
        infinityValue=resource.RLIM_INFINITY,
        before=[soft, hard],
        after=[soft, hard],
        changed=False,
    )
    if soft != resource.RLIM_INFINITY and soft >= MIN_NOFILE_SOFT_LIMIT:
        return
    if hard != resource.RLIM_INFINITY and hard < MIN_NOFILE_SOFT_LIMIT:
        raise AdapterError(f"RLIMIT_NOFILE hard limit {hard} is below required soft limit {MIN_NOFILE_SOFT_LIMIT}")
    resource.setrlimit(resource.RLIMIT_NOFILE, (MIN_NOFILE_SOFT_LIMIT, hard))
    observed_soft, observed_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    record.update(after=[observed_soft, observed_hard], changed=(observed_soft, observed_hard) != (soft, hard))
    if observed_hard != hard or observed_soft != MIN_NOFILE_SOFT_LIMIT:
        raise AdapterError("RLIMIT_NOFILE adjustment did not preserve the hard limit and satisfy the original soft-limit gate")


def install_observation_recovery(module, args, report):
    """Return a fresh original observation after a bounded transient workload."""
    original_observation = module.process_observation
    original_guard = module.guard_processes
    original_pair_budget = module.required_pair_budget
    additional_wait = 0.0
    report.update(maximumAdditionalWaitSeconds=PROCESS_OBSERVATION_WAIT_SECONDS,
                  cumulativeAdditionalWaitSeconds=0.0, additionalBudgetSeconds=PROCESS_OBSERVATION_WAIT_SECONDS,
                  originalDeadlinesExtended=False, calls=[])

    def process_observation(seconds):
        nonlocal additional_wait
        call = {"startedUtc": module.now().isoformat(), "observationSeconds": seconds,
                "observations": [], "outcome": "observing", "returnedObservationIndex": None}
        report["calls"].append(call)
        waiting_started = None
        blocked_index = None
        try:
            snapshot = original_observation(seconds)
            call["observations"].append(snapshot)
            while True:
                current_index = len(call["observations"]) - 1
                try:
                    original_guard(snapshot)
                except module.Blocked as error:
                    if snapshot["foreignRelated"] and str(error) == FOREIGN_RELATED_MESSAGE:
                        call.update(outcome="foreign_related", returnedObservationIndex=current_index)
                        return snapshot
                    if snapshot["foreignRelated"] or not snapshot["foreignActive"] or str(error) != FOREIGN_ACTIVE_MESSAGE:
                        raise
                    blocked_index = current_index
                    if waiting_started is None:
                        waiting_started = time.monotonic()
                    remaining = PROCESS_OBSERVATION_WAIT_SECONDS - additional_wait - (time.monotonic() - waiting_started)
                    call["remainingWaitSeconds"] = max(0.0, remaining)
                    if remaining <= seconds:
                        call.update(outcome="busy_wait_budget_exhausted", returnedObservationIndex=current_index)
                        return snapshot
                    if args.window_deadline_utc:
                        utc_remaining = (module.instant(args.window_deadline_utc) - module.now()).total_seconds()
                        call["remainingUtcWindowSeconds"] = utc_remaining
                        if utc_remaining <= args.window_buffer_seconds + seconds:
                            call.update(outcome="busy_utc_window_exhausted", returnedObservationIndex=current_index)
                            return snapshot
                    snapshot = original_observation(seconds)
                    call["observations"].append(snapshot)
                else:
                    if waiting_started is not None:
                        elapsed = time.monotonic() - waiting_started
                        if additional_wait + elapsed > PROCESS_OBSERVATION_WAIT_SECONDS:
                            call.update(outcome="idle_after_wait_budget_exhausted", returnedObservationIndex=blocked_index)
                            return call["observations"][blocked_index]
                        if args.window_deadline_utc:
                            utc_remaining = (module.instant(args.window_deadline_utc) - module.now()).total_seconds()
                            if utc_remaining < args.window_buffer_seconds:
                                call.update(outcome="idle_after_utc_reserve_exhausted", returnedObservationIndex=blocked_index)
                                return call["observations"][blocked_index]
                    call["outcome"] = "idle_after_wait" if waiting_started is not None else "original_guard_passed"
                    call["returnedObservationIndex"] = current_index
                    return snapshot
        except BaseException as error:
            call.update(outcome="observation_or_guard_exception", error=f"{type(error).__name__}: {error}")
            raise
        finally:
            elapsed = time.monotonic() - waiting_started if waiting_started is not None else 0.0
            additional_wait += elapsed
            call["additionalWaitSeconds"] = elapsed
            call["latestObservationIndex"] = len(call["observations"]) - 1 if call["observations"] else None
            call["finishedUtc"] = module.now().isoformat()
            report["cumulativeAdditionalWaitSeconds"] = additional_wait
            report["waitBudgetOverrunObserved"] = additional_wait > PROCESS_OBSERVATION_WAIT_SECONDS

    module.process_observation = process_observation
    module.required_pair_budget = lambda values: original_pair_budget(values) + PROCESS_OBSERVATION_WAIT_SECONDS


def install_hooks(module, portable_root, args, capture_factory, capture_reserve_seconds):
    """Inject sampling at the existing checked call without replacing DB lifecycle."""
    root = Path(args.run_root).resolve()
    benchmark = Path(portable_root) / "inputs/tools/benchmark/fts_large_bench.sh"
    original_checked = module.checked
    original_pair_budget = module.required_pair_budget
    original_guard_window = module.guard_window
    original_emit = module.emit
    state = {"capture": None, "captureDirectory": root / "profiling/candidate-benchmark"}

    def checked(command, output, env, timeout=30, input_bytes=None):
        output = Path(output)
        candidate_benchmark = (
            list(map(str, command)) == ["bash", str(benchmark)]
            and output.name == "benchmark.log"
            and output.parent.name == "candidate"
            and output.parent.resolve() == root / "candidate"
        )
        if not candidate_benchmark:
            return original_checked(command, output, env, timeout, input_bytes)
        capture = capture_factory(output.parent / "owned-process.json", state["captureDirectory"], benchmark_log=output)
        state["capture"] = capture
        with capture:
            capture.mark("candidate_benchmark_started")
            receipt = original_checked(command, output, env, timeout, input_bytes)
            capture.mark("candidate_benchmark_finished")
        return receipt

    def comparison(report):
        return {
            "scope": DIAGNOSTIC_SCOPE,
            "candidateBenchmarkInstrumented": True,
            "samplingOverhead": OVERHEAD_NOTE,
            "performanceComparisonComputed": False,
            "statisticalSignificanceClaimed": False,
            "formalContestRun": False,
            "rawMetrics": {role: f"{role}/metrics.json" for role in ("baseline", "candidate")},
            "originalScorerOutputsPreserved": True,
        }

    def guard_window(values):
        result = original_guard_window(values)
        remaining = (module.instant(values.window_deadline_utc) - module.now()).total_seconds()
        if remaining < values.total_timeout_seconds + PERF_REPORT_RESERVE_SECONDS:
            raise AdapterError("The UTC stop deadline cannot cover the bounded post-cleanup perf report")
        result["profilingCaptureReserveSeconds"] = capture_reserve_seconds
        result["postCleanupPerfReportReserveSeconds"] = PERF_REPORT_RESERVE_SECONDS
        return result

    def emit(value):
        value = dict(value, profilingScope=DIAGNOSTIC_SCOPE,
                     candidateBenchmarkInstrumented=state["capture"] is not None)
        if value.get("status") == "original_full_pair_completed_local_only":
            value["originalDriverStatus"] = value["status"]
            value["status"] = "raw_capture_completed_verification_pending"
        original_emit(value)

    module.checked = checked
    module.comparison = comparison
    module.required_pair_budget = lambda values: original_pair_budget(values) + capture_reserve_seconds
    module.CLEANUP_RESERVE_SECONDS += capture_reserve_seconds
    module.guard_window = guard_window
    module.emit = emit
    return state


def install_collection_hook(module):
    """Add complete raw profiling data to one fresh, atomically published export."""
    original_collect = module.collect

    def collect(root, out, download_outcomes, deadline=None):
        root, out = Path(root), Path(out)
        deadline = deadline if deadline is not None else time.monotonic() + 180
        if out.exists():
            raise AdapterError("Fresh evidence output required")
        staging = out.with_name(out.name + ".partial")
        if staging.exists():
            raise AdapterError("Previous partial evidence output is preserved; use a fresh output path")
        original_collect(root, staging, download_outcomes, deadline)
        collection_path = staging / "collection.json"
        collection = json.loads(collection_path.read_text(encoding="utf-8"))
        collection.update(scope=DIAGNOSTIC_SCOPE, profilingRawFilesIncluded=False)
        profiling = root / "profiling"
        if profiling.exists():
            if profiling.is_symlink() or not profiling.is_dir():
                raise AdapterError("Profiling source must be the owned regular directory")
            sources = []
            for path in sorted(profiling.rglob("*")):
                if path.is_symlink():
                    raise AdapterError(f"Profiling export refuses a symbolic link: {path}")
                if path.is_file():
                    sources.append(path)
            for relative in ("candidate/owned-process.json", "runtime-verification.json"):
                path = root / relative
                if path.is_file():
                    sources.append(path)
            entries = []
            for path in sources:
                actual = module.regular(path)
                if not actual.is_relative_to(root.resolve()):
                    raise AdapterError(f"Profiling source escapes the owned run: {path}")
                before = module.stamp(path)
                checksum = module.deadline_sha(path, deadline)
                if module.stamp(path) != before:
                    raise AdapterError(f"Profiling source changed while hashing: {path}")
                entries.append({"path": path.relative_to(root).as_posix(), "bytes": before["bytes"],
                                "sha256": checksum, "stamp": before})
            inventory = {
                "scope": DIAGNOSTIC_SCOPE,
                "originalRunnerSha256": RUNNER_SHA256,
                "originalInventorySha256": INVENTORY_SHA256,
                "files": [{key: entry[key] for key in ("path", "bytes", "sha256")} for entry in entries],
            }
            archive = staging / "profiling-raw.tar.gz"
            working = archive.with_name(archive.name + ".partial")
            with working.open("xb") as stream, tarfile.open(fileobj=stream, mode="w:gz", compresslevel=1) as packed:
                raw = (json.dumps(inventory, indent=2) + "\n").encode()
                info = tarfile.TarInfo("profiling-payload-manifest.json")
                info.size = len(raw)
                packed.addfile(info, io.BytesIO(raw))
                for path, entry in zip(sources, entries):
                    if module.stamp(path) != entry["stamp"]:
                        raise AdapterError(f"Profiling source changed before compression: {path}")
                    module.bounded_tar_add(packed, path, entry["path"], deadline)
                    if module.stamp(path) != entry["stamp"]:
                        raise AdapterError(f"Profiling source changed during compression: {path}")
            checksum = module.deadline_sha(working, deadline)
            size = working.stat().st_size
            working.rename(archive)
            record = {"path": archive.name, "bytes": size, "sha256": checksum}
            collection["files"].append(record)
            collection.update(profilingRawFilesIncluded=True,
                              profilingSupplement=dict(record, regularFiles=len(entries),
                                                       rawBytes=sum(entry["bytes"] for entry in entries)))
        collection_path.write_text(json.dumps(collection, indent=2) + "\n", encoding="utf-8")
        module.check_deadline(deadline, "profiling evidence finalization")
        staging.rename(out)

    module.collect = collect


def report_samples(text):
    headers = re.findall(r"^# Samples:\s+([\d,.]+[KMGT]?)\s+of event", text, re.MULTILINE)
    if any(float(value.rstrip("KMGT").replace(",", "")) == 0 for value in headers):
        raise AdapterError("perf report decoded zero samples")
    rows = []
    ipc_columns = re.search(r"^#.*\bSymbol[ \t]+IPC[ \t]+\[IPC Coverage\][ \t]*$", text, re.MULTILINE)
    pattern = r"^[ \t]*([\d.]+)%[ \t]+([\d,]+)[ \t]+(.+?)[ \t]+\[([.kugh])\][ \t]+(.+)$"
    for overhead, count, dso, context, symbol in re.findall(pattern, text, re.MULTILINE):
        symbol = symbol.strip()
        if ipc_columns:
            columns = symbol.rsplit(None, 2)
            if len(columns) == 3 and columns[1:] == ["-", "-"]:
                symbol = columns[0]
        rows.append({"samples": int(count.replace(",", "")), "overheadPercent": float(overhead),
                     "dso": dso, "context": context, "symbol": symbol})
    count = sum(row["samples"] for row in rows)
    if count <= 0:
        raise AdapterError("perf report did not expose positive integer sample rows with DSO and symbols")
    unknown_symbols = sum(row["samples"] for row in rows
                          if row["symbol"] in {"[unknown]", "??"}
                          or re.fullmatch(r"(?:0[xX][\da-fA-F]+|[\da-fA-F]{16})", row["symbol"]))
    unknown_dsos = sum(row["samples"] for row in rows if row["dso"] == "[unknown]")
    return {
        "sampleCount": count,
        "method": "perf_report_integer_sample_rows",
        "positiveSamplesVerified": True,
        "symbolResolutionScope": "Flat perf report rows",
        "symbolColumnParsed": True,
        "resolvedSymbolSamples": count - unknown_symbols,
        "unknownSymbolSamples": unknown_symbols,
        "unknownSymbolFraction": unknown_symbols / count,
        "unknownDsoSamples": unknown_dsos,
        "unknownDsoFraction": unknown_dsos / count,
        "functionHotspots": sorted(rows, key=lambda row: row["samples"], reverse=True)[:30],
    }


def finish_profile(module, args, state):
    """Decode perf only after the original driver has completed all DB cleanup."""
    root = Path(args.run_root)
    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    if not report.get("roles") or not all(row.get("cleanupPassed") for row in report["roles"].values()):
        raise AdapterError("Original driver has not verified database cleanup")
    capture = state["capture"]
    if capture is None:
        raise AdapterError("The original candidate benchmark was never captured")
    if capture.summary.get("status") not in {"raw_recorded", "raw_recorded_sample_count_unverified"}:
        raise AdapterError("The candidate capture did not complete successfully")
    remaining = (module.instant(args.window_deadline_utc) - module.now()).total_seconds()
    if remaining < PERF_REPORT_RESERVE_SECONDS:
        raise AdapterError("Insufficient UTC stop-window reserve for perf report")
    output = state["captureDirectory"] / "perf-report.txt"
    command = [
        capture.summary["command"][0], "report", "--stdio", "--header", "--show-nr-samples",
        "--sort=dso,symbol", "--no-children", "--call-graph=none", "--percent-limit=0",
        "-i", str(state["captureDirectory"] / "perf.data"),
    ]
    env = module.runtime_env(args)
    env.update(LC_ALL="C", LANG="C", DEBUGINFOD_URLS="")
    receipt = module.checked(command, output, env, PERF_REPORT_TIMEOUT_SECONDS)
    samples = report_samples(output.read_text(encoding="utf-8", errors="replace"))
    return dict(samples, profileVerified=True, report=str(output.relative_to(root)), commandReceipt=receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog="Additional options are forwarded to the original v2 CLI.")
    parser.add_argument("--portable-root", type=Path, required=True)
    parser.add_argument("--mode", choices=["check-bundle", "extract", "collect", "preflight", "run"], default="check-bundle")
    options, forwarded = parser.parse_known_args(argv)
    root = options.portable_root.resolve()
    verification = verify_portable(root)
    if options.mode == "check-bundle":
        print(json.dumps(dict(status="offline_profile_bundle_check_passed", **verification, databaseStarts=0)))
        return 0
    if sys.platform != "linux":
        raise AdapterError("Actual portable-driver operations require Linux; check-bundle is available offline")

    previous_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        module = load_driver(root)
        forwarded.extend(["--mode", options.mode, "--bundle-root", str(root)])
        args = module.parser().parse_args(forwarded)
        if options.mode == "collect":
            install_collection_hook(module)
        state = None
        reserve = 0
        if options.mode == "run":
            if args.run_root is None:
                raise AdapterError("Profiling run requires --run-root")
            from profile_capture import CAPTURE_RESERVE_SECONDS, Capture
            reserve = CAPTURE_RESERVE_SECONDS
            state = install_hooks(module, root, args, Capture, reserve)
        existed = args.run_root is not None and Path(args.run_root).exists()
        summary = {
            "scope": DIAGNOSTIC_SCOPE,
            "mode": options.mode,
            "samplingOverhead": OVERHEAD_NOTE,
            "performanceComparisonComputed": False,
            "formalContestRun": False,
            "captureReserveSeconds": reserve,
            "postCleanupPerfReportTimeoutSeconds": PERF_REPORT_TIMEOUT_SECONDS,
            "postCleanupPerfReportReserveSeconds": PERF_REPORT_RESERVE_SECONDS,
            "bundleVerification": verification,
            "profileVerified": False,
        }
        result = 2
        try:
            if options.mode in {"preflight", "run"}:
                summary["fileDescriptorLimit"] = {}
                prepare_nofile_limit(module.resource, summary["fileDescriptorLimit"])
                summary["processObservationRecovery"] = {}
                install_observation_recovery(module, args, summary["processObservationRecovery"])
            result = module.main(forwarded)
            summary["driverReturnCode"] = result
            if options.mode == "run" and result == 0:
                summary["decodedProfile"] = finish_profile(module, args, state)
                summary["profileVerified"] = True
        except Exception as error:
            result = 2
            summary["error"] = f"{type(error).__name__}: {error}"
        finally:
            if state is not None and state["capture"] is not None:
                summary["capture"] = state["capture"].summary
            try:
                summary["originalInputsUnchanged"] = verify_portable(root) == verification
            except Exception as error:
                result = 2
                summary["originalInputsUnchanged"] = False
                summary["inputVerificationError"] = str(error)
            summary["status"] = "profiling_diagnostic_incomplete"
            if result == 0:
                summary["status"] = "profiling_operation_completed"
                if summary["profileVerified"]:
                    summary["status"] = "profiled_candidate_diagnostic_completed"
            if args.run_root is not None and not existed and Path(args.run_root).is_dir():
                report_path = Path(args.run_root) / "report.json"
                if report_path.is_file():
                    original = json.loads(report_path.read_text(encoding="utf-8"))
                    summary["originalDriverStatus"] = original.get("status")
                destination = Path(args.run_root) / "profiling-adapter-report.json"
                destination.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary))
        return result
    finally:
        sys.dont_write_bytecode = previous_bytecode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AdapterError as error:
        print(json.dumps({"status": "profiling_diagnostic_blocked", "error": str(error)}))
        raise SystemExit(2)
