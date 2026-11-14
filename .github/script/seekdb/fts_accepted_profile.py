#!/usr/bin/env python3
"""Profile the exact saved c3d7988 executable through the original benchmark."""
import argparse
import json
import os
from pathlib import Path

import fts_tokenize_config_validation as typed


validation, driver, common = typed.validation, typed.driver, typed.common
ORIGINAL_PREPARE = validation.prepare


def source_proof(args):
    return dict(typed.ORIGINAL_SOURCE_PROOF(args, ()), experiment='ACCEPTED_C3D7988_PROFILE')


def owned_threads(task_root):
    result = {}
    for entry in task_root.iterdir():
        try:
            raw = (entry / 'stat').read_text()
        except (FileNotFoundError, ProcessLookupError):
            continue
        close = raw.rindex(')')
        fields = raw[close + 2:].split()
        tid = int(entry.name)
        driver.require(int(raw.split(' ', 1)[0]) == tid, 'Owned thread stat identity changed')
        result[str(tid)] = dict(comm=raw[raw.index('(') + 1:close],
                               userTicks=int(fields[11]), systemTicks=int(fields[12]),
                               startTicks=int(fields[19]))
    return result


def coverage(phase, processes):
    first = min(processes, key=lambda row: abs(row['monotonic_ns'] - phase['startMonotonicNs']))
    last = min(processes, key=lambda row: abs(row['monotonic_ns'] - phase['stopMonotonicNs']))
    user_cpu = max(0, last['utime_seconds'] - first['utime_seconds'])
    minimum = max(20, 0.25 * user_cpu * 99)
    return dict(processUserCpuSeconds=user_cpu, minimumSamples=minimum,
                passed=phase['sampleCount'] >= minimum,
                scope='Diagnostic sanity gate, not a statistical significance test')


def phase_windows(marks):
    stages = [row for row in marks if row['label'] == 'benchmark_stage']
    driver.require([row['fields']['stage'] for row in stages] == [f'[{i}/5]' for i in range(1, 6)],
                   'All five original benchmark stage marks are required')
    end = next(row['monotonic_ns'] for row in reversed(marks)
               if row['label'] == 'accepted_original_benchmark_finished')
    def stamp(value):
        return f'{value // 1000000000}.{value % 1000000000:09d}'
    result = {}
    for index, name in ((2, 'build'), (3, 'tokenize'), (4, 'query')):
        start = stages[index]['monotonic_ns']
        stop = stages[index + 1]['monotonic_ns'] if index < 4 else end
        driver.require(start < stop, 'A scored phase has no positive observation interval')
        result[name] = dict(startMonotonicNs=start, stopMonotonicNs=stop,
                            perfTime=stamp(start) + ',' + stamp(stop - 1))
    return result


def prepare(args):
    ORIGINAL_PREPARE(args)
    plan_path = args.root / 'plan.json'
    plan = json.loads(plan_path.read_text())
    plan.update(scope='ACCEPTED_C3D7988_PROFILE_NOT_PERFORMANCE_COMPARISON',
                candidateBuildsRequired=0, schedule=[], profiledRole='baseline')
    driver.dump(plan_path, plan)
    report_path = args.root / 'report.json'
    report = json.loads(report_path.read_text())
    report.update(plan=plan, performanceComparisonComputed=False, timingsDiagnosticOnly=True)
    driver.dump(report_path, report)


def validate(args):
    validation.prepare_nofile(args)
    report_path = args.root / 'report.json'
    report = json.loads(report_path.read_text())
    driver.require(report['status'] == 'prepared' and report['plan']['candidateBuildsRequired'] == 0,
                   'A fresh profiling root without a candidate build is required')
    identity = json.loads((args.root / 'runtime/baseline/identity.json').read_text())
    driver.safe_elf(Path(identity['binary']), identity['sha256'], identity['bytes'],
                    identity['buildId'], validation.BASE)
    report.update(status='profiling_accepted_baseline', builds={'baseline': identity})
    driver.dump(report_path, report)
    runtime = args.source / '.github/script/seekdb/fts-profile-runtime'
    capture_module = common.load_module('accepted_profile_capture', runtime / 'profile_capture.py')
    original_snapshot = capture_module._proc_snapshot
    def snapshot_with_threads(identity, clock_ticks, page_size):
        result = original_snapshot(identity, clock_ticks, page_size)
        result['ownedThreads'] = owned_threads(Path('/proc') / str(identity['pid']) / 'task')
        return result
    capture_module._proc_snapshot = snapshot_with_threads
    analysis = common.load_module('accepted_profile_analysis', runtime / 'run_profile.py')
    scorer = common.load_module('accepted_original_scorer', args.source / 'tools/benchmark/fts_large_bench_score.py')
    baseline = json.loads((args.source / 'tools/benchmark/fts_large_bench_baseline.json').read_text())
    role_args = argparse.Namespace(source=args.root / 'sources/baseline', workspace=args.source,
        role='baseline', expected_sha=validation.BASE, binary=Path(identity['binary']),
        obclient=args.source / 'deps/3rd/u01/obclient/bin/obclient')
    benchmark = common.benchmark_work(role_args, baseline, scorer, False)
    directory = args.root / 'profiling/accepted-benchmark'

    def work(call, out, pid):
        owned = json.loads((out / 'owned-process.json').read_text())
        driver.require(owned['pid'] == pid and owned['sha256'] == identity['sha256'],
                       'The sampled process must own the accepted executable')
        process = dict(pid=pid, exe=identity['binary'],
                       state=dict(pid=pid, startTicks=owned['startTicks']))
        process_path = out / 'profile-owned-process.json'
        driver.dump(process_path, process)
        warmup_out = out / 'original-prewarm'
        warmup_out.mkdir()
        warmup = benchmark(call, warmup_out)
        driver.require(warmup.get('passed'), 'Original benchmark worker prewarm failed')
        driver.dump(out / 'prewarm-thread-state.json', owned_threads(Path('/proc') / str(pid) / 'task'))
        original_capture = common.capture_group
        capture = None
        command_path = args.root / 'sources/baseline/tools/benchmark/fts_large_bench.sh'
        def capture_benchmark(command, output, *values, **options):
            nonlocal capture
            if list(map(str, command)) != ['bash', str(command_path)] or Path(output) != out / 'benchmark.log':
                return original_capture(command, output, *values, **options)
            driver.require(capture is None, 'The original benchmark may be captured once')
            capture = capture_module.PerfCapture(process_path, directory, benchmark_log=out / 'benchmark.log')
            with capture:
                capture.mark('accepted_original_benchmark_started')
                receipt = original_capture(command, output, *values, **options)
                capture.mark('accepted_original_benchmark_finished')
            return receipt
        common.capture_group = capture_benchmark
        try:
            result = benchmark(call, out)
        finally:
            common.capture_group = original_capture
        driver.require(capture is not None, 'The original benchmark was not sampled')
        driver.require(capture.summary['status'] in ('raw_recorded', 'raw_recorded_sample_count_unverified')
                       and not capture.summary['errors'], 'The raw accepted-ELF capture did not complete')
        driver.require(0 < capture.summary['perf_data_bytes'] <= 512 * 1024**2,
                       'The raw profile is empty or exceeds the evidence cap')
        return dict(result, timingsDiagnosticOnly=True, profileScope=process,
                    uninstrumentedOriginalPrewarm=warmup)

    row = driver.run_owned(args, report, {'baseline': identity}, 'baseline', 'accepted-profile', work)
    common.require_instance(row)
    driver.require(len(report['instances']) == 1 and row['cleanupPassed'],
                   'Database cleanup must precede profile decoding')
    env = dict(os.environ, LC_ALL='C', LANG='C', DEBUGINFOD_URLS='')
    output = directory / 'perf-report.txt'
    command = ['perf', 'report', '--stdio', '--header', '--show-nr-samples', '--sort=dso,symbol',
               '--no-children', '--call-graph=none', '--percent-limit=0', '-i', str(directory / 'perf.data')]
    receipt = common.capture_group(command, output, args.source, 60, env=env)
    driver.require(receipt['exitCode'] == 0 and receipt['childrenCleaned'], 'Perf decoding failed')
    samples = analysis.report_samples(output.read_text(encoding='utf-8', errors='replace'))
    marks = [json.loads(line) for line in (directory / 'capture-marks.jsonl').read_text().splitlines()]
    phases = phase_windows(marks)
    processes = [json.loads(line) for line in (directory / 'proc-samples.jsonl').read_text().splitlines()]
    for name, phase in phases.items():
        phase_output = directory / ('perf-' + name + '.txt')
        phase_receipt = common.capture_group(command + ['--time', phase['perfTime']], phase_output,
                                              args.source, 60, env=env)
        driver.require(phase_receipt['exitCode'] == 0 and phase_receipt['childrenCleaned'],
                       'Scored phase profile decoding failed: ' + name)
        phase.update(analysis.report_samples(phase_output.read_text(encoding='utf-8', errors='replace')))
        phase['coverage'] = coverage(phase, processes)
    files = []
    for path in sorted(directory.iterdir()):
        common.no_symlinks(path)
        driver.require(path.is_file(), 'Unexpected profile member')
        files.append(dict(path=path.name, bytes=path.stat().st_size, sha256=common.digest(path)))
    driver.dump(directory / 'profile-manifest.json', dict(files=files, fullBytes=True, truncated=False,
        sourceCommit=validation.BASE, elfSha256=identity['sha256'], buildId=identity['buildId']))
    report.update(profileAnalysis=samples, profilePhases=phases)
    driver.dump(report_path, report)
    driver.require(all(row['coverage']['passed'] for row in phases.values()),
                   'Scored phases are underrepresented relative to owned process user CPU time')
    report.update(status='accepted_original_benchmark_profile_completed', profileAnalysis=samples,
                  profilePhases=phases, phaseBoundaryScope='Polled log observations; not exact operator timings',
                  profiledCommit=validation.BASE, performanceComparisonComputed=False,
                  timingsDiagnosticOnly=True, baselineRebuilt=False, candidateBuilds=0,
                  formalContestRun=False)
    driver.dump(report_path, report)


if __name__ == '__main__':
    typed.configure()
    validation.source_proof = driver.source_proof = source_proof
    driver.prepare = prepare
    driver.validate = validate
    raise SystemExit(validation.main())
