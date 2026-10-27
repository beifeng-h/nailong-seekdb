"""Launch the SeekDB observer fully detached from the calling shell.

PowerShell's Start-Process lets the child inherit the caller's handles
(bInheritHandles = TRUE), which keeps the parent's stdout pipe open. Using
subprocess with close_fds=True hands the child only the three stdio handles
we redirect, so the observer survives independently of the shell.

    python launch_detached.py [--port 2881] [--memory 2G] [--cpu 4]
"""

import argparse
import os
import subprocess
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", default=os.path.join(os.environ.get("TEMP", ""), "seekdb"))
    ap.add_argument("--port", type=int, default=2881)
    ap.add_argument("--memory", default="2G")
    ap.add_argument("--cpu", type=int, default=4)
    ap.add_argument("--log-level", default="WARN")
    args = ap.parse_args()

    exe = os.path.join(args.runtime, "app", "bin", "seekdb.exe")
    base = os.path.join(args.runtime, "base")
    if not os.path.exists(exe):
        sys.exit(f"seekdb.exe not found at {exe}")

    log_dir = os.path.join(base, "logs")
    os.makedirs(log_dir, exist_ok=True)

    cmd = [
        exe,
        f"--base-dir={base}",
        "--nodaemon",
        f"--port={args.port}",
        "--parameter", f"memory_limit={args.memory}",
        "--parameter", f"cpu_count={args.cpu}",
        "--log-level", args.log_level,
    ]

    out = open(os.path.join(log_dir, "observer.stdout.log"), "wb")
    proc = subprocess.Popen(
        cmd,
        stdout=out,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        cwd=base,
    )
    with open(os.path.join(base, "observer.pid"), "w") as f:
        f.write(str(proc.pid))
    print(f"[seekdb] started, pid={proc.pid}, base-dir={base}")


if __name__ == "__main__":
    main()
