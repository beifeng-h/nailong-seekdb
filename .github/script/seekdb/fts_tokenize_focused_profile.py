#!/usr/bin/env python3
"""Profile each original TOKENIZE statement using the preserved complete ELF."""

from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import struct

import fts_accepted_profile as profile
import fts_string_decoder_validation as baseline


validation, driver, common = baseline.validation, baseline.driver, baseline.common
ROUNDS = 60000
WARMUP = 100
MIN_REQUEST_SAMPLES = 100


def benchmark_statements(script):
    """Read the two exact constant statements from the frozen benchmark."""
    result = {}
    for label, parser in (('IK', 'ik'), ('BENG', 'beng')):
        matches = re.findall(r'^' + label + r'_TEXT="([^"\r\n]*)"$', script, re.M)
        driver.require(len(matches) == 1 and "'" not in matches[0],
                       'Original benchmark text is missing or needs SQL escaping')
        result[parser] = f"SELECT tokenize('{matches[0]}', '{parser}');"
    driver.require('run_tokenize_bench "tokenize_ik" "ik" "${IK_TEXT}"' in script and
                   'run_tokenize_bench "tokenize_beng" "beng" "${BENG_TEXT}"' in script and
                   '"SELECT tokenize(\'${text}\', \'${parser}\');"' in script,
                   'Original benchmark TOKENIZE statement structure changed')
    return result


def raw_samples(path, expected_pid, start_ns, stop_ns):
    """Count owned samples in the exact marked window and reject lost records."""
    result = Counter()
    records = Counter()
    with path.open('rb') as stream:
        header = struct.unpack('<8s12Q', stream.read(104))
        driver.require(header[0] == b'PERFILE2' and header[4] == header[2],
                       'Unexpected perf header or attribute layout')
        stream.seek(header[3])
        attr = struct.unpack('<IIQQQQQ', stream.read(48))
        driver.require(attr[0] == 1 and attr[2] == 0 and attr[3] == 99 and attr[4] & 7 == 7,
                       'Unexpected perf sample settings')
        stream.seek(header[5])
        offset, end = header[5], header[5] + header[6]
        while offset < end:
            first = stream.read(8)
            driver.require(len(first) == 8, 'Incomplete perf record header')
            kind, _, size = struct.unpack('<IHH', first)
            driver.require(8 <= size <= 65535 and offset + size <= end,
                           'Invalid perf record size')
            payload = stream.read(size - 8)
            driver.require(len(payload) == size - 8, 'Incomplete perf record')
            records[kind] += 1
            if kind == 9:
                sample_offset = 8 if attr[4] & (1 << 16) else 0
                driver.require(len(payload) >= sample_offset + 24, 'Incomplete perf sample')
                _, pid, tid, stamp = struct.unpack_from('<QIIQ', payload, sample_offset)
                driver.require(pid == expected_pid, 'Sample belongs to an unowned process')
                if start_ns <= stamp < stop_ns:
                    result[tid] += 1
            offset += size
    driver.require(offset == end and not records[2] and not records[13],
                   'Perf recording has lost or truncated records')
    return result


def request_coverage(processes, samples, start_ns, stop_ns):
    """Compare samples with observed request-worker user ticks, not wall time."""
    ticks = defaultdict(int)
    names = {}
    for first, last in zip(processes, processes[1:]):
        if first['monotonic_ns'] < start_ns or last['monotonic_ns'] >= stop_ns:
            continue
        for tid in first['ownedThreads'].keys() & last['ownedThreads'].keys():
            a, b = first['ownedThreads'][tid], last['ownedThreads'][tid]
            if a['startTicks'] == b['startTicks'] and b['comm'].startswith('ReqWorker'):
                driver.require(b['userTicks'] >= a['userTicks'], 'Thread user ticks decreased')
                ticks[int(tid)] += b['userTicks'] - a['userTicks']
                names[int(tid)] = b['comm']
    rows = [dict(tid=tid, name=names[tid], observedUserTicks=value,
                 samples=samples.get(tid, 0)) for tid, value in sorted(ticks.items())]
    total_ticks = sum(ticks.values())
    request_samples = sum(samples.get(tid, 0) for tid in ticks)
    hz = processes[0]['clock_ticks_per_second'] if processes else 0
    driver.require(hz > 0, 'Missing process tick frequency')
    minimum = max(MIN_REQUEST_SAMPLES, total_ticks / hz * 99 * 0.25)
    return dict(threads=rows, requestSamples=request_samples, observedUserTicks=total_ticks,
                clockTicksPerSecond=hz, minimumSamples=minimum,
                passed=total_ticks > 0 and request_samples >= minimum,
                scope='Observed request threads only; not proof of statistical significance')


def prepare(args):
    profile.ORIGINAL_PREPARE(args)
    plan_path, report_path = args.root / 'plan.json', args.root / 'report.json'
    plan = json.loads(plan_path.read_text())
    plan.update(scope='FOCUSED_TOKENIZE_PROFILE_NOT_PERFORMANCE_COMPARISON',
                candidateBuildsRequired=0, schedule=[], diagnosticRounds=ROUNDS,
                diagnosticWarmup=WARMUP)
    driver.dump(plan_path, plan)
    report = json.loads(report_path.read_text())
    report.update(plan=plan, performanceComparisonComputed=False, timingsDiagnosticOnly=True)
    driver.dump(report_path, report)


def validate(args):
    validation.prepare_nofile(args)
    report_path = args.root / 'report.json'
    report = json.loads(report_path.read_text())
    driver.require(report['status'] == 'prepared' and report['plan']['candidateBuildsRequired'] == 0,
                   'A fresh baseline-only profiling root is required')
    identity = json.loads((args.root / 'runtime/baseline/identity.json').read_text())
    driver.safe_elf(Path(identity['binary']), identity['sha256'], identity['bytes'],
                    identity['buildId'], baseline.BASE)
    report.update(status='profiling_tokenize', builds={'baseline': identity})
    driver.dump(report_path, report)
    capture_module = common.load_module('focused_profile_capture',
        args.source / '.github/script/seekdb/fts-profile-runtime/profile_capture.py')
    original_snapshot = capture_module._proc_snapshot

    def snapshot_with_threads(process_identity, clock_ticks, page_size):
        result = original_snapshot(process_identity, clock_ticks, page_size)
        result['ownedThreads'] = profile.owned_threads(Path('/proc') / str(process_identity['pid']) / 'task')
        return result

    capture_module._proc_snapshot = snapshot_with_threads
    script = (args.root / 'sources/baseline/tools/benchmark/fts_large_bench.sh').read_text(encoding='utf-8')
    statements = benchmark_statements(script)
    results = {}

    def work(call, out, pid):
        owned = json.loads((out / 'owned-process.json').read_text())
        driver.require(owned['pid'] == pid and owned['sha256'] == identity['sha256'],
                       'The sampled process must own the preserved executable')
        process_path = out / 'profile-owned-process.json'
        driver.dump(process_path, dict(pid=pid, exe=identity['binary'],
                    state=dict(pid=pid, startTicks=owned['startTicks'])))
        for parser, statement in statements.items():
            control = driver.sql(call, out, parser + '-control', statement)
            driver.require(control and len(control.splitlines()) == 1,
                           'The original TOKENIZE statement must return one row')
            driver.sql(call, out, parser + '-warmup', (statement + '\n') * WARMUP)
            directory = args.root / 'profiling' / parser
            with capture_module.PerfCapture(process_path, directory) as capture:
                capture.mark('sql_started', parser=parser)
                output = driver.sql(call, out, parser + '-repeated', (statement + '\n') * ROUNDS)
                capture.mark('sql_finished', parser=parser)
            rows = output.splitlines(keepends=True)
            driver.require(len(rows) == ROUNDS and all(row == control for row in rows),
                           'Repeated diagnostic output differs from the single original statement')
            driver.require(not capture.summary['errors'] and
                           0 < capture.summary['perf_data_bytes'] <= 512 * 1024**2,
                           'The raw diagnostic capture is incomplete')
            results[parser] = dict(statementSha256=hashlib.sha256(statement.encode()).hexdigest(),
                controlOutputSha256=hashlib.sha256(control.encode()).hexdigest(),
                repeatedRows=len(rows), outputEqualToControl=True,
                perfBytes=capture.summary['perf_data_bytes'])
        return dict(passed=True, workloads=results, timingsDiagnosticOnly=True)

    row = driver.run_owned(args, report, {'baseline': identity}, 'baseline', 'focused-tokenize', work)
    common.require_instance(row)
    driver.require(len(report['instances']) == 1 and row['cleanupPassed'],
                   'Database cleanup must precede profile decoding')
    analysis = common.load_module('focused_profile_analysis',
        args.source / '.github/script/seekdb/fts-profile-runtime/run_profile.py')
    env = dict(os.environ, LC_ALL='C', LANG='C', DEBUGINFOD_URLS='')
    for parser in statements:
        directory = args.root / 'profiling' / parser
        marks = [json.loads(line) for line in (directory / 'capture-marks.jsonl').read_text().splitlines()]
        driver.require([x['label'] for x in marks] == ['sql_started', 'sql_finished'],
                       'Expected exactly one marked SQL interval')
        start, stop = (x['monotonic_ns'] for x in marks)
        driver.require(start < stop, 'Empty diagnostic SQL interval')
        samples = raw_samples(directory / 'perf.data', marks[0]['scope']['pid'], start, stop)
        processes = [json.loads(line) for line in (directory / 'proc-samples.jsonl').read_text().splitlines()]
        coverage = request_coverage(processes, samples, start, stop)
        output = directory / 'perf-report.txt'
        window = f'{start // 1000000000}.{start % 1000000000:09d},{(stop-1) // 1000000000}.{(stop-1) % 1000000000:09d}'
        command = ['perf', 'report', '--stdio', '--header', '--show-nr-samples', '--sort=dso,symbol',
                   '--no-children', '--call-graph=none', '--percent-limit=0', '--time', window,
                   '-i', str(directory / 'perf.data')]
        receipt = common.capture_group(command, output, args.source, 60, env=env)
        driver.require(receipt['exitCode'] == 0 and receipt['childrenCleaned'], 'Perf decoding failed')
        decoded = analysis.report_samples(output.read_text(encoding='utf-8', errors='replace'))
        driver.require(decoded['sampleCount'] == sum(samples.values()), 'Raw and decoded sample counts differ')
        request_profile = None
        if coverage['requestSamples']:
            tids = ','.join(str(item['tid']) for item in coverage['threads'])
            request_output = directory / 'perf-request-threads.txt'
            request_receipt = common.capture_group(command + ['--tid=' + tids], request_output,
                                                   args.source, 60, env=env)
            driver.require(request_receipt['exitCode'] == 0 and request_receipt['childrenCleaned'],
                           'Request-thread perf decoding failed')
            request_profile = analysis.report_samples(request_output.read_text(encoding='utf-8', errors='replace'))
            driver.require(request_profile['sampleCount'] == coverage['requestSamples'],
                           'Request-thread raw and decoded sample counts differ')
        results[parser].update(windowStartNs=start, windowStopNs=stop,
            allOwnedSamples=sum(samples.values()), coverage=coverage, profile=decoded,
            requestThreadProfile=request_profile)
        files = [dict(path=p.name, bytes=p.stat().st_size, sha256=common.digest(p))
                 for p in sorted(directory.iterdir()) if p.is_file() and not p.is_symlink()]
        driver.dump(directory / 'profile-manifest.json', dict(files=files, fullBytes=True,
                    sourceCommit=baseline.BASE, elfSha256=identity['sha256'], buildId=identity['buildId']))
    report.update(status='focused_tokenize_profile_completed', profileResults=results,
                  baselineRebuilt=False, candidateBuilds=0, formalContestRun=False,
                  performanceComparisonComputed=False, timingsDiagnosticOnly=True,
                  limitations=['Repeated original statements are diagnostic and may alter cache warmth',
                               'Request-thread identity is observed, not mapped to an individual SQL session',
                               'Sampling cannot establish official score or an optimization gain'])
    driver.dump(report_path, report)
    driver.require(all(item['coverage']['passed'] for item in results.values()),
                   'Request-thread samples did not meet the diagnostic coverage gate')


def configure():
    for key, value in baseline.PIN.items():
        setattr(validation, key, value)
        setattr(driver, key, value)
    validation.source_proof = driver.source_proof = baseline.source_proof
    driver.prepare, driver.validate = prepare, validate


if __name__ == '__main__':
    configure()
    raise SystemExit(validation.main())
