"""Compare uninstrumented development processes on one fixed read-only database."""

import argparse
import hashlib
import json
import platform
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

PROTOCOL = 'mvdb-dev-jsonl-v1'


def identity(path):
    data = path.read_bytes()
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def run(program, data, source, target):
    with source.open('rb') as input_stream, target.open('wb') as output:
        started = time.perf_counter()
        process = subprocess.run([str(program), '--development-protocol', PROTOCOL, '--data', str(data)],
                                 stdin=input_stream, stdout=output, stderr=subprocess.PIPE, timeout=120)
        elapsed = time.perf_counter() - started
    if process.returncode:
        raise RuntimeError('Process failed ' + process.stderr.decode('utf-8'))
    return elapsed


def verify(path, group, rows, vectors):
    seen = set()
    metadata = False
    complete = False
    with path.open(encoding='utf-8') as stream:
        ready = json.loads(next(stream))
        assert ready == {'protocol': PROTOCOL, 'event': 'ready', 'formal_ready': False}
        for line in stream:
            event = json.loads(line)
            assert event['protocol'] == PROTOCOL and not complete
            if event['event'] == 'columns':
                assert not metadata and not seen
                assert event['columns'] == (['id'] if group == 'ids' else ['id', 'v'])
                metadata = True
            elif event['event'] == 'row':
                row = event['values']
                assert metadata and len(row) == (1 if group == 'ids' else 2)
                key = row[0]
                assert type(key) is int and 0 <= key < rows and key not in seen
                seen.add(key)
                if group == 'vectors':
                    assert row[1] == vectors[key % 1009], 'Vector differs from independent quarter-integer truth'
            elif event['event'] == 'complete':
                assert event['is_query'] and event['emitted_rows'] == rows and event['affected_rows'] == 0
                complete = True
            else:
                raise AssertionError('Unexpected protocol event ' + str(event))
    assert metadata and complete and len(seen) == rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--blocks', type=int, default=3)
    args = parser.parse_args()
    assert args.blocks > 0
    programs = {'baseline': args.baseline.resolve(strict=True), 'candidate': args.candidate.resolve(strict=True)}
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report_path = args.report.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    rows, dimensions = 10000, 128
    vectors = [[((key * (dimension + 3) + dimension * dimension) % 1009 - 504) / 4
                for dimension in range(dimensions)] for key in range(1009)]
    report = {'scope': 'Development stdio vector projection serialization comparison', 'platform': platform.platform(),
              'rows': rows, 'dimensions': dimensions, 'blocks': args.blocks, 'runs': [], 'passed': False,
              'programs': {name: {'path': str(path), **identity(path)} for name, path in programs.items()},
              'script': identity(Path(__file__)),
              'timing': 'External process wall time including startup, SQL parsing, page reads and stdout redirected to a file',
              'verification_timed': False, 'cache_state': 'Retained OS cache, balanced A/B/B/A blocks',
              'code_instrumentation': False, 'official_protocol_tested': False, 'formal_performance_score_measured': False}

    def save():
        report_path.write_bytes((json.dumps(report, indent=2) + '\n').encode('utf-8'))

    save()
    setup = output / 'setup.sql'
    with setup.open('w', encoding='utf-8', newline='\n') as stream:
        stream.write('CREATE TABLE docs(id BIGINT PRIMARY KEY, v VECTOR(128));\n')
        for start in range(0, rows, 100):
            values = []
            for key in range(start, start + 100):
                literal = ','.join(format(value, '.2f') for value in vectors[key % 1009])
                values.append('(' + str(key) + ",'[" + literal + "]')")
            stream.write('INSERT INTO docs VALUES ' + ','.join(values) + ';\n')
    groups = {'ids': output / 'ids.sql', 'vectors': output / 'vectors.sql'}
    groups['ids'].write_bytes(b'SELECT id FROM docs;\n')
    groups['vectors'].write_bytes(b'SELECT id, v FROM docs;\n')
    report['inputs'] = {path.name: identity(path) for path in [setup, *groups.values()]}
    try:
        with tempfile.TemporaryDirectory(prefix='owned-data-', dir=output) as temporary:
            data = Path(temporary).resolve()
            report['setup_seconds'] = run(programs['baseline'], data, setup, output / 'setup.jsonl')
            original = identity(data / 'pages.bin')
            report['database'] = original
            reference_outputs = {}
            for block in range(args.blocks):
                for order, variant in enumerate(['baseline', 'candidate', 'candidate', 'baseline']):
                    for group, source in groups.items():
                        capture = output / (str(block) + '-' + str(order) + '-' + variant + '-' + group + '.jsonl')
                        elapsed = run(programs[variant], data, source, capture)
                        verify(capture, group, rows, vectors)
                        digest = identity(capture)
                        if group in reference_outputs:
                            assert digest == reference_outputs[group], 'Output bytes differ between variants'
                        else:
                            reference_outputs[group] = digest
                        report['runs'].append({'block': block, 'order': order, 'variant': variant, 'group': group,
                                               'wall_seconds': elapsed, 'rows_checked': rows,
                                               'capture': digest, 'passed': True})
                        save()
                        print('PASS ' + variant + ' ' + group + ' seconds=' + format(elapsed, '.6f'), flush=True)
            assert identity(data / 'pages.bin') == original, 'Queries changed the frozen database'
        report['summary'] = {}
        for group in groups:
            medians = {variant: statistics.median(record['wall_seconds'] for record in report['runs']
                                                  if record['variant'] == variant and record['group'] == group)
                       for variant in programs}
            report['summary'][group] = {'baseline_median_seconds': medians['baseline'],
                                        'candidate_median_seconds': medians['candidate'],
                                        'elapsed_reduction_fraction': 1 - medians['candidate'] / medians['baseline']}
        report['temporary_database_cleaned'] = True
        report['passed'] = True
        save()
        print(json.dumps(report['summary']), flush=True)
    except BaseException as error:
        report['error'] = str(error)
        save()
        raise


if __name__ == '__main__':
    main()
