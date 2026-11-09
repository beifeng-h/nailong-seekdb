"""Kill actual child processes at commit/recovery boundaries; no external services."""

import argparse
import json
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path


def run(driver, mode, directory, *extra, limit=None):
    def constrain():
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    result = subprocess.run(
        [str(driver), mode, str(directory), *extra],
        capture_output=True,
        text=True,
        timeout=240,
        preexec_fn=constrain if limit else None,
    )
    if result.returncode:
        raise RuntimeError(f"{mode}: exit={result.returncode}; {result.stdout}; {result.stderr}")
    return result.stdout.strip()


def kill_at(driver, mode, directory, stage):
    process = subprocess.Popen(
        [str(driver), mode, str(directory), stage],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    messages = queue.Queue()

    def collect():
        for line in process.stdout:
            messages.put(line.strip())
        messages.put(None)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    try:
        line = messages.get(timeout=30)
        if line != f"FAULT_READY {stage}":
            raise RuntimeError(f"Missing fault boundary {stage}: {line}")
        process.kill()
        process.wait(timeout=15)
        if process.returncode == 0:
            raise RuntimeError("Expected a forcibly terminated process")
        return process.returncode
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=15)
        reader.join(timeout=2)
        process.stdout.close()
        process.stderr.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("driver", type=Path)
    parser.add_argument("--stream-pages", type=int, default=0)
    parser.add_argument("--address-space-mib", type=int, default=48)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--sort-cleanup", action="store_true")
    parser.add_argument("--commit-returned-stage")
    parser.add_argument("--scope", default="development storage-page layer; not SQL, official protocol, container or formal acceptance")
    args = parser.parse_args()
    driver = args.driver.resolve(strict=True)
    records = []
    started = time.monotonic()
    stages = [
        ("uncommitted", "transaction.uncommitted", "old"),
        ("commit", "commit.header_written", "old"),
        ("commit", "commit.frame_written", "old"),
        ("commit", "commit.marker_written", "new"),
        ("commit", "commit.wal_synced", "new"),
        ("commit", "commit.verified", "new"),
        ("commit", "commit.applied_page", "new"),
        ("commit", "commit.data_synced", "new"),
        ("commit", "commit.wal_cleared", "new"),
    ]
    if args.commit_returned_stage:
        stages.append(("commit", args.commit_returned_stage, "new"))
    for mode, stage, expected in stages:
        with tempfile.TemporaryDirectory(prefix="minivectordb-kill-") as temporary:
            directory = Path(temporary)
            run(driver, "seed", directory)
            exit_code = kill_at(driver, mode, directory, stage)
            run(driver, expected, directory)
            run(driver, expected, directory)
            records.append({"stage": stage, "killed_exit": exit_code, "expected": expected, "reopens": 2})
            print(f"PASS kill/reopen {stage}", flush=True)
    for stage in ["recover.verified", "recover.applied_page", "recover.data_synced"]:
        with tempfile.TemporaryDirectory(prefix="minivectordb-rekill-") as temporary:
            directory = Path(temporary)
            run(driver, "seed", directory)
            kill_at(driver, "commit", directory, "commit.wal_synced")
            exit_code = kill_at(driver, "recover", directory, stage)
            run(driver, "new", directory)
            run(driver, "new", directory)
            records.append({"stage": stage, "killed_exit": exit_code, "expected": "new", "reopens": 2})
            print(f"PASS kill during recovery {stage}", flush=True)
    if args.sort_cleanup:
        with tempfile.TemporaryDirectory(prefix="minivectordb-sortkill-") as temporary:
            directory = Path(temporary)
            run(driver, "seed", directory)
            exit_code = kill_at(driver, "sort-kill", directory, "query.first_row")
            run(driver, "old", directory)
            records.append({"stage": "query.first_row", "killed_exit": exit_code, "expected": "old", "reopens": 1})
            print("PASS interrupted sort cleanup", flush=True)
    stream = None
    if args.stream_pages:
        import os

        if os.name != "posix":
            raise RuntimeError("RLIMIT_AS test requires Linux/POSIX; no substitute pass is reported")
        limit = args.address_space_mib * 1024 * 1024
        with tempfile.TemporaryDirectory(prefix="minivectordb-stream-") as temporary:
            directory = Path(temporary)
            run(driver, "stream-write", directory, str(args.stream_pages), limit=limit)
            run(driver, "stream-read", directory, str(args.stream_pages), limit=limit)
            stream = {"pages": args.stream_pages, "data_bytes": args.stream_pages * 4096, "address_space_limit_bytes": limit, "passed": True, "scope": "RLIMIT_AS; excludes filesystem page-cache memory and is not a cgroup or official resource pass"}
            print("PASS bounded address-space streaming write/reopen/read", flush=True)
    report = {"forced_kill_cases": records, "passed": len(records), "failed": 0, "stream": stream, "elapsed_seconds": time.monotonic() - started, "scope": args.scope}
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
