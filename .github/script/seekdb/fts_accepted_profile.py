#!/usr/bin/env python3
"""Profile the exact saved c3d7988 executable through the original benchmark."""
import json
import os
from pathlib import Path

import fts_tokenize_config_validation as typed


validation, driver, common = typed.validation, typed.driver, typed.common
ORIGINAL_PREPARE = validation.prepare


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
    analysis = common.load_module('accepted_profile_analysis', runtime / 'run_profile.py')
    scorer = common.load_module('accepted_original_scorer', args.source / 'tools/benchmark/fts_large_bench_score.py')
    baseline = json.loads((args.source / 'tools/benchmark/fts_large_bench_baseline.json').read_text())
    benchmark = validation.benchmark_callback(args, {'baseline': identity}, 'baseline', baseline, scorer)
    directory = args.root / 'profiling/accepted-benchmark'

    def work(call, out, pid):
        owned = json.loads((out / 'owned-process.json').read_text())
        driver.require(owned['pid'] == pid and owned['sha256'] == identity['sha256'],
                       'The sampled process must own the accepted executable')
        process = dict(pid=pid, exe=identity['binary'],
                       state=dict(pid=pid, startTicks=owned['startTicks']))
        process_path = out / 'profile-owned-process.json'
        driver.dump(process_path, process)
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
            result = benchmark(call, out, pid)
        finally:
            common.capture_group = original_capture
        driver.require(capture is not None, 'The original benchmark was not sampled')
        driver.require(capture.summary['status'] in ('raw_recorded', 'raw_recorded_sample_count_unverified')
                       and not capture.summary['errors'], 'The raw accepted-ELF capture did not complete')
        driver.require(0 < capture.summary['perf_data_bytes'] <= 512 * 1024**2,
                       'The raw profile is empty or exceeds the evidence cap')
        return dict(result, timingsDiagnosticOnly=True, profileScope=process)

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
    for name, phase in phases.items():
        phase_output = directory / ('perf-' + name + '.txt')
        phase_receipt = common.capture_group(command + ['--time', phase['perfTime']], phase_output,
                                              args.source, 60, env=env)
        driver.require(phase_receipt['exitCode'] == 0 and phase_receipt['childrenCleaned'],
                       'Scored phase profile decoding failed: ' + name)
        phase.update(analysis.report_samples(phase_output.read_text(encoding='utf-8', errors='replace')))
    files = []
    for path in sorted(directory.iterdir()):
        common.no_symlinks(path)
        driver.require(path.is_file(), 'Unexpected profile member')
        files.append(dict(path=path.name, bytes=path.stat().st_size, sha256=common.digest(path)))
    driver.dump(directory / 'profile-manifest.json', dict(files=files, fullBytes=True, truncated=False,
        sourceCommit=validation.BASE, elfSha256=identity['sha256'], buildId=identity['buildId']))
    report.update(status='accepted_original_benchmark_profile_completed', profileAnalysis=samples,
                  profilePhases=phases, phaseBoundaryScope='Polled log observations; not exact operator timings',
                  profiledCommit=validation.BASE, performanceComparisonComputed=False,
                  timingsDiagnosticOnly=True, baselineRebuilt=False, candidateBuilds=0,
                  formalContestRun=False)
    driver.dump(report_path, report)


if __name__ == '__main__':
    typed.configure()
    driver.prepare = prepare
    driver.validate = validate
    raise SystemExit(validation.main())
