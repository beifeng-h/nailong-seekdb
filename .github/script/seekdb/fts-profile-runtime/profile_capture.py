"""Linux perf sidecar for a driver-owned SeekDB process."""

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time


MAX_CLOSE_SECONDS = 15
CAPTURE_RESERVE_SECONDS = 20
STARTUP_CONFIRM_SECONDS = 0.25
SAMPLE_INTERVAL_SECONDS = 0.25


class CaptureError(RuntimeError):
    """The requested capture could not be verified."""


def _positive_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise CaptureError(f"Invalid {name}")
    if isinstance(value, str) and not value.isdecimal():
        raise CaptureError(f"Invalid {name}")
    number = int(value)
    if number <= 0:
        raise CaptureError(f"Invalid {name}")
    return number


def _load_identity(path):
    document = json.loads(path.read_text(encoding="utf-8"))
    pid = _positive_integer(document["pid"], "pid")
    state = document["state"]
    if _positive_integer(state["pid"], "state.pid") != pid:
        raise CaptureError("owned-process.json contains conflicting PIDs")
    executable = document["exe"]
    if not isinstance(executable, str) or not executable.startswith("/"):
        raise CaptureError("The owned executable must be an absolute Linux path")
    return {
        "pid": pid,
        "exe": executable,
        "startTicks": _positive_integer(state["startTicks"], "state.startTicks"),
    }


def _read_stat(pid):
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    closing_parenthesis = raw.rfind(")")
    if closing_parenthesis < 0 or int(raw.split(" ", 1)[0]) != pid:
        raise CaptureError("Malformed process stat")
    fields = raw[closing_parenthesis + 2:].split()
    return {
        "startTicks": int(fields[19]),
        "utime_ticks": int(fields[11]),
        "stime_ticks": int(fields[12]),
        "rss_pages": int(fields[21]),
    }


def _proc_snapshot(identity, clock_ticks, page_size):
    pid = identity["pid"]
    before = _read_stat(pid)
    executable = os.readlink(f"/proc/{pid}/exe")
    if before["startTicks"] != identity["startTicks"] or executable != identity["exe"]:
        raise CaptureError("Target process identity changed or PID was reused")
    io = {}
    for line in Path(f"/proc/{pid}/io").read_text(encoding="utf-8").splitlines():
        name, value = line.split(":", 1)
        io[name] = int(value.strip())
    after = _read_stat(pid)
    if after["startTicks"] != identity["startTicks"]:
        raise CaptureError("Target PID was reused while sampling")
    return {
        "scope": identity,
        "monotonic_ns": time.monotonic_ns(),
        **after,
        "clock_ticks_per_second": clock_ticks,
        "page_size_bytes": page_size,
        "utime_seconds": after["utime_ticks"] / clock_ticks,
        "stime_seconds": after["stime_ticks"] / clock_ticks,
        "rss_bytes": after["rss_pages"] * page_size,
        "io": io,
    }


class PerfCapture:
    """Capture one diagnostic interval and reap only its own perf child."""

    MAX_CLOSE_SECONDS = MAX_CLOSE_SECONDS
    CAPTURE_RESERVE_SECONDS = CAPTURE_RESERVE_SECONDS

    def __init__(self, owned_process_path, output_dir, *, benchmark_log=None):
        self.owned_process_path = Path(owned_process_path)
        self.output_dir = Path(output_dir)
        self.benchmark_log = Path(benchmark_log) if benchmark_log is not None else None
        self.summary = {
            "schema_version": 1,
            "timing_scope": "diagnostic",
            "status": "starting",
            "owned_process_path": str(self.owned_process_path),
            "created_monotonic_ns": time.monotonic_ns(),
            "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
            "errors": [],
            "stop_signals": [],
        }
        self._process = None
        self._thread = None
        self._stop = threading.Event()
        self._sampling_error = None
        self._files = {}
        self._closed = False
        self._mark_lock = threading.Lock()
        self._benchmark_offset = 0
        self._benchmark_pending = b""
        if self.benchmark_log is not None:
            self.summary["benchmark_log"] = str(self.benchmark_log)
            self.summary["stage_boundary_precision"] = (
                "Complete-line observation times, delayed by the polling interval "
                "plus sampler work and scheduling delay; not exact operator timings."
            )

    def __enter__(self):
        # A fresh directory also prevents reusing or truncating old perf data.
        try:
            self.output_dir.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            raise CaptureError(f"A fresh capture directory is required: {error}") from error
        self.summary["entered_monotonic_ns"] = time.monotonic_ns()
        try:
            if sys.platform != "linux":
                raise CaptureError("PerfCapture requires Linux /proc and perf")
            identity = _load_identity(self.owned_process_path)
            self.summary["scope"] = identity
            self._clock_ticks = os.sysconf("SC_CLK_TCK")
            self._page_size = os.sysconf("SC_PAGE_SIZE")
            _proc_snapshot(identity, self._clock_ticks, self._page_size)
            environment = os.environ.copy()
            environment["LC_ALL"] = "C"
            version = subprocess.run(
                ["perf", "--version"], capture_output=True, text=True,
                check=True, timeout=3, env=environment,
            )
            self.summary["perf_version"] = version.stdout.strip()
            command = [
                "perf", "record", "-N", "-p", str(identity["pid"]), "-F", "99",
                "-e", "cpu-clock:u", "--call-graph", "dwarf,16384",
                "--clockid", "mono", "-o", str(self.output_dir / "perf.data"),
            ]
            self.summary["command"] = command
            names = ("perf.stdout.log", "perf.stderr.log", "proc-samples.jsonl", "capture-marks.jsonl")
            for name in names:
                self._files[name] = (self.output_dir / name).open("x", encoding="utf-8")
            self.summary["outputs"] = {
                name: {"path": str(self.output_dir / name), "scope": identity}
                for name in (*names, "perf.data", "capture-summary.json")
            }
            self._process = subprocess.Popen(
                command, stdout=self._files["perf.stdout.log"],
                stderr=self._files["perf.stderr.log"],
                start_new_session=True, env=environment,
            )
            self.summary["perf_pid"] = self._process.pid
            self.summary["record_started_monotonic_ns"] = time.monotonic_ns()
            self._sample_once()
            self._poll_benchmark_log()
            self._thread = threading.Thread(target=self._sample_loop, daemon=True)
            self._thread.start()
            self._stop.wait(STARTUP_CONFIRM_SECONDS)
            self._check_running()
            self.summary["status"] = "recording"
            self._write_summary()
            return self
        except BaseException as error:
            self._finish(error)
            if isinstance(error, (CaptureError, KeyboardInterrupt, SystemExit)):
                raise
            raise CaptureError(f"Could not start perf capture: {error}") from error

    def _check_running(self):
        if self._sampling_error:
            raise CaptureError(self._sampling_error)
        if self._process.poll() is not None:
            raise CaptureError("perf exited before the diagnostic interval finished")

    def _sample_once(self):
        sample = _proc_snapshot(self.summary["scope"], self._clock_ticks, self._page_size)
        stream = self._files["proc-samples.jsonl"]
        stream.write(json.dumps(sample, sort_keys=True) + "\n")
        stream.flush()
        self.summary["proc_sample_count"] = self.summary.get("proc_sample_count", 0) + 1

    def _sample_loop(self):
        try:
            while not self._stop.wait(SAMPLE_INTERVAL_SECONDS):
                self._check_running()
                self._sample_once()
                self._poll_benchmark_log()
        except Exception as error:
            self._sampling_error = f"{type(error).__name__}: {error}"
            self._stop.set()

    def mark(self, label, **fields):
        self._check_running()
        with self._mark_lock:
            row = {"scope": self.summary["scope"], "monotonic_ns": time.monotonic_ns(),
                   "label": label, "fields": fields}
            self._files["capture-marks.jsonl"].write(json.dumps(row, sort_keys=True) + "\n")
            self._files["capture-marks.jsonl"].flush()

    def _poll_benchmark_log(self):
        if self.benchmark_log is None:
            return
        try:
            stream = self.benchmark_log.open("rb")
        except FileNotFoundError:
            return
        with stream:
            if os.fstat(stream.fileno()).st_size < self._benchmark_offset:
                self._benchmark_offset = 0
                self._benchmark_pending = b""
                self.summary["benchmark_log_truncations"] = self.summary.get("benchmark_log_truncations", 0) + 1
                self.mark("benchmark_log_truncated", benchmark_log=str(self.benchmark_log))
            stream.seek(self._benchmark_offset)
            appended = stream.read()
            self._benchmark_offset = stream.tell()
        lines = (self._benchmark_pending + appended).split(b"\n")
        self._benchmark_pending = lines.pop()
        self.summary["benchmark_log_pending_bytes"] = len(self._benchmark_pending)
        for line in lines:
            stage = re.search(rb"\[[1-5]/5\]", line)
            if stage is not None:
                self.mark(
                    "benchmark_stage", stage=stage.group().decode("ascii"),
                    raw_label=line.rstrip(b"\r").decode("utf-8", errors="replace"),
                    benchmark_log=str(self.benchmark_log),
                )
                self.summary["benchmark_stage_count"] = self.summary.get("benchmark_stage_count", 0) + 1

    def _stop_perf(self):
        process = self._process
        if process is None:
            return
        for number, timeout in ((signal.SIGINT, 5), (signal.SIGTERM, 3), (getattr(signal, "SIGKILL", 9), 2)):
            if process.poll() is None:
                self.summary["stop_signals"].append(int(number))
                try:
                    os.killpg(process.pid, number)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    self.summary["errors"].append(f"Could not signal owned perf group: {error}")
            try:
                self.summary["perf_returncode"] = process.wait(timeout=timeout)
                self.summary["perf_reaped"] = True
                return
            except subprocess.TimeoutExpired:
                continue
        self.summary["errors"].append("Owned perf child could not be reaped within its stop budget")
        self.summary["perf_reaped"] = False

    def _write_summary(self):
        path = self.output_dir / "capture-summary.json"
        path.write_text(json.dumps(self.summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _finish(self, body_error=None):
        if self._closed:
            return
        self._closed = True
        if body_error is not None:
            self.summary["errors"].append(f"{type(body_error).__name__}: {body_error}")
        if self._process is not None and self._process.poll() is not None and body_error is None:
            self.summary["errors"].append("perf exited before the diagnostic interval finished")
        self._stop.set()
        if self._thread is not None:
            try:
                self._thread.join(timeout=2)
                if self._thread.is_alive():
                    self.summary["errors"].append("The /proc sampler did not stop within two seconds")
            except RuntimeError as error:
                self.summary["errors"].append(f"Could not join the /proc sampler: {error}")
        if self._sampling_error:
            self.summary["errors"].append(self._sampling_error)
        if self._process is not None and (self._thread is None or not self._thread.is_alive()):
            try:
                self._poll_benchmark_log()
            except Exception as error:
                self.summary["errors"].append(f"Could not collect final benchmark stages: {error}")
        self._stop_perf()
        self.summary["record_stopped_monotonic_ns"] = time.monotonic_ns()
        for stream in self._files.values():
            stream.close()
        if self._process is not None:
            with (self.output_dir / "perf.stderr.log").open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 16384))
                stderr = stream.read().decode("utf-8", errors="replace")
            self.summary["perf_stderr_tail"] = stderr
            counts = re.findall(r"\((\d+) samples\)", stderr)
            self.summary["sample_count"] = int(counts[-1]) if counts else None
            data = self.output_dir / "perf.data"
            self.summary["perf_data_bytes"] = data.stat().st_size if data.is_file() else 0
            if not self.summary["perf_data_bytes"]:
                self.summary["errors"].append("perf did not produce a nonempty perf.data")
            if counts and int(counts[-1]) <= 0:
                self.summary["errors"].append("perf explicitly reported zero samples")
            if self.summary["stop_signals"] not in ([], [int(signal.SIGINT)]):
                self.summary["errors"].append("perf required stop escalation; the capture is incomplete")
            if self.summary.get("perf_returncode") not in (0, -int(signal.SIGINT), 128 + int(signal.SIGINT)):
                self.summary["errors"].append("perf exited with an unsuccessful return code")
        self.summary["status"] = "raw_recorded"
        if self.summary.get("sample_count") is None:
            self.summary["status"] = "raw_recorded_sample_count_unverified"
        if self.summary["errors"]:
            self.summary["status"] = "failed"
        self.summary["profile_verified"] = False
        self._write_summary()

    def __exit__(self, exception_type, error, traceback):
        self._finish(error)
        if error is None and self.summary["errors"]:
            raise CaptureError("; ".join(self.summary["errors"]))
        return False


Capture = PerfCapture
