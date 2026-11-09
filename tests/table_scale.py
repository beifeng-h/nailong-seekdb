"""Exercise persistent rows beyond a process address-space cap; not cgroup scoring."""

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from crash_recovery import run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('driver', type=Path)
    parser.add_argument('--rows', type=int, default=100000)
    parser.add_argument('--address-space-mib', type=int, default=32)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'posix':
        raise RuntimeError('This test requires Linux/POSIX RLIMIT_AS')
    limit = args.address_space_mib * 1024 * 1024
    logical_bytes = args.rows * 525
    if logical_bytes <= limit or args.rows < 17 + 7063 * 9 + 1:
        raise RuntimeError('Input must exceed address-space cap and contain the fixed TopK truth')
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='minivectordb-table-scale-') as directory:
        write = run(args.driver.resolve(strict=True), 'scale-write', Path(directory), str(args.rows), limit=limit)
        read = run(args.driver.resolve(strict=True), 'scale-read', Path(directory), str(args.rows), limit=limit)
    report = {'rows': args.rows, 'vector_dimensions': 128, 'logical_record_bytes': logical_bytes,
              'address_space_limit_bytes': limit, 'passed': True,
              'write_output': write, 'restart_read_and_query_output': read,
              'seconds': time.monotonic() - started,
              'scope': 'development typed-table/query kernel; RLIMIT_AS excludes filesystem page cache; not cgroup, official SQL, container or score'}
    args.report.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
