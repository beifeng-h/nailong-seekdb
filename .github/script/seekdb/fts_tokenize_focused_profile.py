#!/usr/bin/env python3
"""Profile each original TOKENIZE statement using the preserved complete ELF."""

from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import struct
import subprocess
import time

import fts_accepted_profile as profile
import fts_string_decoder_validation as baseline


validation, driver, common = baseline.validation, baseline.driver, baseline.common
ROUNDS = 120000
WARMUP = 100
MIN_REQUEST_SAMPLES = 100
CLIENT_TIMEOUT_SECONDS = 180
READINESS_TIMEOUT_SECONDS = 15


class WorkerReadiness:
    """Wait for a stable worker set, recent CPU activity, and SQL output."""

    def __init__(self, start_ns):
        self.start_ns = start_ns
        self.stable_since_ns = start_ns
        self.previous = {}
        self.identities = set()
        self.last_progress_ns = None

    def observe(self, now_ns, threads, output_bytes):
        current = {tid: item for tid, item in threads.items() if item['comm'].startswith('ReqWorker')}
        identities = {(tid, item['startTicks']) for tid, item in current.items()}
        if identities != self.identities:
            self.stable_since_ns = now_ns
            self.identities = identities
        for tid, item in current.items():
            before = self.previous.get(tid)
            if before and before['startTicks'] == item['startTicks'] and item['userTicks'] > before['userTicks']:
                self.last_progress_ns = now_ns
        self.previous = current
        return bool(output_bytes > 0 and self.last_progress_ns is not None
                    and now_ns - self.start_ns >= 1_500_000_000
                    and now_ns - self.stable_since_ns >= 500_000_000
                    and now_ns - self.last_progress_ns <= 300_000_000)


def wait_for_request_activity(process, task_root, output_path):
    start = time.monotonic_ns()
    probe = WorkerReadiness(start)
    observations = []
    while time.monotonic_ns() - start < READINESS_TIMEOUT_SECONDS * 1_000_000_000:
        driver.require(process.poll() is None, 'SQL client exited before request threads became active')
        stamp = time.monotonic_ns()
        threads = profile.owned_threads(task_root)
        size = output_path.stat().st_size
        ready = probe.observe(stamp, threads, size)
        observations.append(dict(monotonicNs=stamp, outputBytes=size,
            requestThreads={tid: item for tid, item in threads.items() if item['comm'].startswith('ReqWorker')}))
        if ready:
            return dict(readyMonotonicNs=stamp, observations=observations,
                        clientStillRunning=process.poll() is None)
        time.sleep(0.1)
    raise RuntimeError('Request threads did not become stably active before the deadline')


def reap_client(process):
    """Reap only the process group created for this diagnostic client."""
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        process.wait(timeout=10)
        return True
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=10)
    return False


def capture_repeated_sql(call, out, pid, process_path, directory, capture_module, parser, statement):
    """Start one sustained client, then attach while its requests are active."""
    input_path = out / (parser + '-repeated.sql')
    output_path = out / (parser + '-repeated.log')
    input_path.write_text(common.SESSION + (statement + '\n') * ROUNDS, encoding='utf-8')
    command = [str(call.obclient), '-h127.0.0.1', '-P' + str(call.port), '-uroot', '-A', '-N', '-B',
               '--raw', '--default-character-set=utf8mb4', '-Doceanbase']
    with input_path.open('rb') as sql_stream, output_path.open('wb') as output_stream:
        started = time.monotonic_ns()
        process = subprocess.Popen(command, cwd=Path.cwd(), env=os.environ.copy(), stdin=sql_stream,
                                   stdout=output_stream, stderr=subprocess.STDOUT, start_new_session=True)
        cleaned = False
        try:
            readiness = wait_for_request_activity(process, Path('/proc') / str(pid) / 'task', output_path)
            driver.require(readiness['clientStillRunning'], 'SQL client exited before perf attachment')
            with capture_module.PerfCapture(process_path, directory) as capture:
                driver.require(process.poll() is None, 'SQL client exited during perf attachment')
                capture.mark('sample_window_started', parser=parser, clientStartedNs=started,
                             readyNs=readiness['readyMonotonicNs'])
                try:
                    code = process.wait(timeout=CLIENT_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError('Diagnostic SQL client timed out') from error
                capture.mark('sample_window_finished', parser=parser)
                driver.require(code == 0, 'Diagnostic SQL client failed')
        finally:
            cleaned = reap_client(process)
    driver.require(cleaned, 'Diagnostic SQL process group required forced cleanup')
    driver.dump(out / (parser + '-client.json'), dict(command=command, clientStartedNs=started,
        readiness=readiness, returnCode=code, childrenCleaned=cleaned,
        sqlSha256=common.digest(input_path), outputSha256=common.digest(output_path)))
    return output_path.read_text(encoding='utf-8', errors='strict'), capture


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


def add_request_thread_fractions(profile_result):
    """Keep perf's original percentages and add explicit filtered-sample fractions."""
    total = profile_result['sampleCount']
    driver.require(total > 0, 'A request-thread sample denominator is required')
    for row in profile_result['functionHotspots']:
        row['requestSamplePercent'] = 100.0 * row['samples'] / total
    profile_result['requestSamplePercentScope'] = 'Fraction of selected request-thread samples'
    profile_result['overheadPercentScope'] = 'Original perf output; denominator may include filtered samples'


def request_coverage(processes, samples, start_ns, stop_ns):
    """Compare samples with observed request-worker user ticks, not wall time."""
    ticks = defaultdict(int)
    names = {}
    identities = defaultdict(set)
    for row in processes:
        if start_ns <= row['monotonic_ns'] < stop_ns:
            for tid, item in row['ownedThreads'].items():
                if item['comm'].startswith('ReqWorker'):
                    identities[int(tid)].add(item['startTicks'])
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
    uncovered = [tid for tid, value in ticks.items()
                 if value >= 20 and samples.get(tid, 0) < value / hz * 99 * 0.5]
    reused = sorted(tid for tid, starts in identities.items() if len(starts) > 1)
    return dict(threads=rows, requestSamples=request_samples, observedUserTicks=total_ticks,
                clockTicksPerSecond=hz, minimumSamples=minimum,
                uncoveredBusyThreads=uncovered, reusedThreadIds=reused,
                passed=total_ticks > 0 and request_samples >= minimum and not uncovered and not reused,
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
            output, capture = capture_repeated_sql(call, out, pid, process_path, directory,
                                                   capture_module, parser, statement)
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
        driver.require([x['label'] for x in marks] == ['sample_window_started', 'sample_window_finished'],
                       'Expected exactly one marked sampling interval')
        start, stop = (x['monotonic_ns'] for x in marks)
        driver.require(marks[0]['fields']['clientStartedNs'] < marks[0]['fields']['readyNs'] < start < stop,
                       'Invalid active-client sampling interval')
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
            add_request_thread_fractions(request_profile)
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
