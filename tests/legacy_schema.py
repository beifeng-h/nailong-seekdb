"""Read an actual M5 data image through the current SqlSession entry point."""

import argparse
import hashlib
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path

from public_samples import compare


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('driver', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--vector-index', action='store_true')
    args = parser.parse_args()
    fixtures = Path(__file__).resolve().parent / 'fixtures'
    metadata = json.loads((fixtures / 'schema_v1.json').read_text(encoding='utf-8'))
    args.report = args.report.resolve()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='legacy-schema-', dir=args.report.parent) as temporary:
        owned = Path(temporary).resolve()
        if not owned.is_relative_to(args.report.parent):
            raise RuntimeError('Legacy data outside owned report directory')
        directory = owned / 'data'
        directory.mkdir()
        with zipfile.ZipFile(fixtures / 'schema_v1.zip') as package:
            for name, identity in metadata['files'].items():
                target = (directory / name).resolve()
                if not target.is_relative_to(directory):
                    raise RuntimeError('Legacy fixture path outside data directory')
                data = package.read(name)
                if len(data) != identity['bytes'] or hashlib.sha256(data).hexdigest() != identity['sha256']:
                    raise RuntimeError('Legacy fixture identity mismatch')
                target.write_bytes(data)
        statements = list(metadata['statements'])
        expected = list(metadata['expected'])
        if args.vector_index:
            statements.extend(['CREATE VECTOR INDEX legacy_vec ON docs(embedding);', '--mode ann', metadata['statements'][-1]])
            expected.extend([{}, metadata['expected'][-1]])
        sql = owned / 'input.test'
        sql.write_text('\n'.join(statements) + '\n', encoding='utf-8')
        capture = owned / 'actual.json'
        process = subprocess.run([str(args.driver.resolve(strict=True)), str(directory), str(sql), str(capture)],
                                 capture_output=True, timeout=60)
        actual = json.loads(capture.read_text(encoding='utf-8')) if capture.is_file() else []
        passed = process.returncode == 0 and len(actual) == len(expected) and all(
            not compare(assertion, result) for assertion, result in zip(expected, actual))
        restart = None
        if args.vector_index and passed:
            sql.write_text('--mode ann\n' + metadata['statements'][-1] + '\n', encoding='utf-8')
            reopened = subprocess.run([str(args.driver.resolve(strict=True)), str(directory), str(sql), str(capture)],
                                      capture_output=True, timeout=60)
            restart = json.loads(capture.read_text(encoding='utf-8')) if reopened.returncode == 0 else []
            passed = reopened.returncode == 0 and len(restart) == 1 and not compare(expected[-1], restart[0])
        report = {'passed': passed, 'producer_commit': metadata['producer_commit'],
                  'fixture_files': metadata['files'], 'expected': expected, 'actual': actual,
                  'vector_index': args.vector_index, 'restarted_index_query': restart,
                  'driver_exit': process.returncode, 'stdout': process.stdout.decode('utf-8'),
                  'stderr': process.stderr.decode('utf-8'), 'scope': 'M5 schema image read and updated by current SqlSession'}
    args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'passed': passed, 'producer_commit': metadata['producer_commit'], 'statements': len(actual)}))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
