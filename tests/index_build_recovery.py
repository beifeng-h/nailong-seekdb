"""Kill index creation and record changes in the same real SQL transaction."""

import argparse
import json
import tempfile
from pathlib import Path

from crash_recovery import kill_at, run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('driver', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    driver = args.driver.resolve(strict=True)
    records = []
    stages = [
        ('index-build-uncommitted', 'transaction.uncommitted', 'old-no-index'),
        ('index-build', 'commit.header_written', 'old-no-index'),
        ('index-build', 'commit.frame_written', 'old-no-index'),
        ('index-build', 'commit.marker_written', 'new'),
        ('index-build', 'commit.wal_synced', 'new'),
        ('index-build', 'commit.verified', 'new'),
        ('index-build', 'commit.applied_page', 'new'),
        ('index-build', 'commit.data_synced', 'new'),
        ('index-build', 'commit.wal_cleared', 'new'),
        ('index-build', 'session.commit_returned', 'new'),
    ]
    for mode, stage, expected in stages:
        with tempfile.TemporaryDirectory(prefix='minivectordb-index-build-kill-') as temporary:
            directory = Path(temporary)
            run(driver, 'seed-no-index', directory)
            exit_code = kill_at(driver, mode, directory, stage)
            run(driver, expected, directory)
            run(driver, expected, directory)
            records.append({'stage': stage, 'killed_exit': exit_code, 'expected': expected, 'reopens': 2})
            print('PASS index build kill/reopen ' + stage, flush=True)
    for stage in ['recover.verified', 'recover.applied_page', 'recover.data_synced']:
        with tempfile.TemporaryDirectory(prefix='minivectordb-index-build-rekill-') as temporary:
            directory = Path(temporary)
            run(driver, 'seed-no-index', directory)
            kill_at(driver, 'index-build', directory, 'commit.wal_synced')
            exit_code = kill_at(driver, 'recover', directory, stage)
            run(driver, 'new', directory)
            run(driver, 'new', directory)
            records.append({'stage': stage, 'killed_exit': exit_code, 'expected': 'new', 'reopens': 2})
            print('PASS index build rekill/reopen ' + stage, flush=True)
    report = {'scope': 'Development SQL index-build transaction and record updates, exact and flat-index queries',
              'formal_process_protocol_tested': False, 'forced_kill_cases': records, 'passed': len(records), 'failed': 0}
    args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
