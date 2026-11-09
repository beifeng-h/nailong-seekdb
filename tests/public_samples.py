"""Compare unmodified public fixtures with file-captured SqlSession results."""

import argparse
import hashlib
import json
import math
import subprocess
import tempfile
from pathlib import Path


def equal(expected, actual):
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(
            equal(left, right) for left, right in zip(expected, actual))
    if type(expected) is float:
        return type(actual) in (int, float) and math.isfinite(actual) and math.isclose(
            expected, actual, rel_tol=1e-5, abs_tol=1e-5)
    return type(expected) is type(actual) and expected == actual


def rows_equal(expected, actual, unordered):
    if not unordered:
        return equal(expected, actual)
    if not isinstance(actual, list) or len(expected) != len(actual):
        return False
    assigned = [None] * len(actual)

    def assign(index, seen):
        for candidate, row in enumerate(actual):
            if candidate in seen or not equal(expected[index], row):
                continue
            seen.add(candidate)
            if assigned[candidate] is None or assign(assigned[candidate], seen):
                assigned[candidate] = index
                return True
        return False

    return all(assign(index, set()) for index in range(len(expected)))


def compare(expected, actual):
    if not isinstance(actual, dict) or 'error' in actual:
        return ['SQL execution failed']
    unknown = set(expected) - {'affected_rows', 'columns', 'rows', 'unordered'}
    if unknown:
        raise ValueError('Unknown assertion fields ' + str(sorted(unknown)))
    failures = []
    for key in ['affected_rows', 'columns']:
        if key in expected and not equal(expected[key], actual.get(key)):
            failures.append(key + ' mismatch')
    if 'rows' in expected and not rows_equal(expected['rows'], actual.get('rows'), expected.get('unordered', False)):
        failures.append('rows mismatch')
    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('driver', type=Path)
    parser.add_argument('--cases', type=Path, default=Path(__file__).resolve().parents[1] / 'mysqltest')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if equal(1, 1.0) or equal(1, True) or equal('id', 'ID') or not equal(5.0, 5.00004):
        raise RuntimeError('Scalar assertion self-check failed')
    if rows_equal([['a'], ['a']], [['a'], ['b']], True) or not rows_equal([['a'], ['a'], ['b']], [['b'], ['a'], ['a']], True):
        raise RuntimeError('Multiset assertion self-check failed')
    if not compare({}, {'error': 'failure'}):
        raise RuntimeError('Empty assertion accepted a SQL failure')
    cases = args.cases.resolve(strict=True)
    manifest = json.loads((cases / 'manifest.json').read_text(encoding='utf-8'))
    report = {'scope': 'public SQL assertions through SqlSession with file capture',
              'formal_process_protocol_tested': False, 'manifest_version': manifest['version'],
              'fixture_hashes': {}, 'cases': [], 'passed': False}
    for path in sorted(cases.rglob('*')):
        if path.is_file():
            report['fixture_hashes'][path.relative_to(cases).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    args.report = args.report.resolve()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='public-samples-', dir=args.report.parent) as temporary:
        owned = Path(temporary).resolve()
        if not owned.is_relative_to(args.report.parent):
            raise RuntimeError('Temporary data outside owned report directory')
        for case in manifest['cases']:
            name = case['id']
            directory = owned / name
            directory.mkdir()
            sql_path = cases / 'cases' / (name + '.test')
            expected = json.loads((cases / 'cases' / (name + '.result')).read_text(encoding='utf-8'))
            statements = [line for line in sql_path.read_text(encoding='utf-8').splitlines() if line]
            if len(statements) != case['statements'] or len(expected) != len(statements):
                raise RuntimeError('Fixture statement count mismatch for ' + name)
            capture = directory / 'actual.json'
            process = subprocess.run([str(args.driver.resolve(strict=True)), str(directory), str(sql_path), str(capture)],
                                     capture_output=True, timeout=60)
            actual = json.loads(capture.read_text(encoding='utf-8')) if capture.is_file() else []
            failures = []
            if process.returncode != 0 or len(actual) != len(expected):
                failures.append({'driver_exit': process.returncode, 'captured_statements': len(actual)})
            for index, assertion in enumerate(expected):
                errors = compare(assertion, actual[index]) if index < len(actual) else ['missing result']
                if errors:
                    failures.append({'statement': index + 1, 'errors': errors, 'sql': statements[index]})
            report['cases'].append({'id': name, 'statements': len(statements), 'passed': not failures,
                                    'failures': failures, 'actual': actual,
                                    'stdout': process.stdout.decode('utf-8'), 'stderr': process.stderr.decode('utf-8')})
            print(('PASS ' if not failures else 'FAIL ') + name + ' statements=' + str(len(statements)), flush=True)
    report['passed'] = all(case['passed'] for case in report['cases'])
    report['statements'] = sum(case['statements'] for case in report['cases'])
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'statements': report['statements'],
                      'passed_cases': sum(case['passed'] for case in report['cases']), 'cases': len(report['cases'])}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
