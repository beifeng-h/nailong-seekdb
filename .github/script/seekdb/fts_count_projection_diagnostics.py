#!/usr/bin/env python3
"""Diagnose four COUNT/LIMIT projections using two preserved complete ELFs."""
import argparse
import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import statistics
import sys
import tarfile
import time
import traceback
import zipfile

import fts_count_only_validation as count_only


driver, common = count_only.driver, count_only.common
BASE = count_only.BASE
CANDIDATE = 'd1e6befb5310c2add82d4c4ac2513db443ed96d2'
CANDIDATE_TREE = '02faadbdf4269b7514f243982cb39fcd405c93c6'
SCOPE = 'PRESERVED_ELF_COUNT_PROJECTION_DIAGNOSTICS'
SCHEDULE = (('A1', 'baseline'), ('B1', 'candidate'), ('B2', 'candidate'), ('A2', 'baseline'))
CASE_NAMES = ('id_unordered', 'constant_unordered', 'id_ordered', 'constant_ordered')
WARMUP, SAMPLES, QUERY_ROUNDS = 30, 3, 200
RUN_SECONDS, CLEANUP_SECONDS = 900, 180
CLIENT_BYTES = 14592368
CLIENT_SHA = '798aa952d536563ee0291e69251a6d0cd0d85e0c470d5499768fa0731ff3c1be'
RPM_HASHES = {
    'obclient-2.2.3-1.el8.x86_64.rpm': '35e38b580996fc583b0eaa961bd78911feff08981624154851db51d7e5aa935d',
    'libobclient-2.2.3-1.el8.x86_64.rpm': 'e53cadb0d91c1a0d78369f0c9488be8d774f03ea09346e7b218799a5318a9a68',
}
PINS = {
    'baseline': dict(commit=BASE, tree=count_only.BASE_TREE, artifact=13828533821, run=42953505169,
        zipBytes=329563859, zipSha256=count_only.parent.BASE_ZIP_SHA,
        tarBytes=329540878, tarSha256=count_only.parent.BASE_TAR_SHA,
        elfBytes=count_only.parent.BASE_ELF_BYTES, elfSha256=count_only.parent.BASE_ELF_SHA,
        buildId=count_only.parent.BASE_BUILD_ID),
    'candidate': dict(commit=CANDIDATE, tree=CANDIDATE_TREE, artifact=13929153838, run=43181771127,
        zipBytes=329566356, zipSha256='0987f18f495cabd5a9a42bd71cb24d5b32ba45d085230f46fff63c7adcac762e',
        tarBytes=329543347, tarSha256='7176f6461d3b64aa6207c0597d64092f3c021fe614bc190808d1a48c370b98b1',
        elfBytes=1332375112, elfSha256='78bd77ebeca7f9ba54966dbee4d59bfcbe164dd452a05e870d2bccf614e0cf7b',
        buildId='ab462f4df53540d5278eca9f8e6a8014045d32d0'),
}


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def diagnostic_source_proof(source, commit):
    identity = common.source_identity(source, commit)
    changed = common.git(source, 'diff', '--name-only', CANDIDATE, commit, '--').decode().splitlines()
    driver.require(all(path.startswith('.github/') for path in changed),
                   'Diagnostic source contains a product change beyond fixed d1e6')
    products = {}
    for path in count_only.PRODUCTS:
        raw = common.git(source, 'show', CANDIDATE + ':' + path)
        driver.require(raw == (source / path).read_bytes(), 'Fixed d1e6 product bytes differ: ' + path)
        products[path] = hashlib.sha256(raw).hexdigest()
    inputs = driver.inventory(source)
    driver.require(common.git(source, 'rev-parse', CANDIDATE + '^{tree}').decode().strip() == CANDIDATE_TREE,
                   'Fixed candidate source tree differs')
    return dict(driverCommit=commit, driverTree=identity['tree'], elfSourceCommit=CANDIDATE,
                elfSourceTree=CANDIDATE_TREE, productBlobSha256=products,
                originalInputCount=len(inputs), originalInputHashes=inputs,
                originalInputInventorySha256=driver.INVENTORY,
                diagnosticChanges=changed, newBuilds=0)


def extract_elf(archive_path, role, root, original_inputs):
    pin = PINS[role]
    common.no_symlinks(archive_path)
    driver.require(archive_path.stat().st_size == pin['zipBytes']
                   and common.digest(archive_path) == pin['zipSha256'], 'Pinned complete ZIP differs: ' + role)
    with zipfile.ZipFile(archive_path) as archive:
        expected = {'candidate-artifact-manifest.json', 'candidate-identity.json', 'candidate-elf.tar.gz'}
        driver.require(len(archive.infolist()) == 3 and set(archive.namelist()) == expected,
                       'Complete ELF ZIP inventory differs: ' + role)
        identity_raw = archive.read('candidate-identity.json')
        identity = json.loads(identity_raw)
        manifest = json.loads(archive.read('candidate-artifact-manifest.json'))
        driver.require(identity['sourceCommit'] == pin['commit'] and identity['sourceTree'] == pin['tree']
                       and identity['sha256'] == pin['elfSha256'] and identity['bytes'] == pin['elfBytes']
                       and identity['buildId'] == pin['buildId'] and identity['officialInputHashes'] == original_inputs,
                       'Archived complete ELF source or content identity differs: ' + role)
        driver.require(manifest['identity'] == identity and manifest['fullBytes'] is True
                       and manifest['stripped'] is False and manifest['truncated'] is False,
                       'Archived complete ELF preservation proof differs: ' + role)
        payload = archive.read('candidate-elf.tar.gz')
    driver.require(len(payload) == pin['tarBytes'] and hashlib.sha256(payload).hexdigest() == pin['tarSha256'],
                   'Pinned complete ELF TAR differs: ' + role)
    binary = root / 'runtime' / role / 'seekdb'
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
        members = archive.getmembers()
        driver.require(len(members) == 1 and members[0].name == 'candidate/seekdb'
                       and members[0].isreg() and members[0].size == pin['elfBytes'],
                       'Expected one full regular ELF: ' + role)
        with archive.extractfile(members[0]) as incoming, binary.open('xb') as output:
            shutil.copyfileobj(incoming, output)
    binary.chmod(0o555)
    driver.safe_elf(binary, pin['elfSha256'], pin['elfBytes'], pin['buildId'], pin['commit'])
    (root / 'evidence' / role / 'archived-identity.json').write_bytes(identity_raw)
    relocated = dict(identity, role=role, binary=str(binary), archiveRole=identity['role'],
                     archiveRun=pin['run'], archiveId=pin['artifact'], archiveZipSha256=pin['zipSha256'])
    driver.dump(root / 'runtime' / role / 'identity.json', relocated)
    return relocated


def query_cases(source):
    original = common.queries_from_benchmark(source / 'tools/benchmark/fts_large_bench.sh')['query_limit']
    projection = '(SELECT id FROM '
    boundary = ' LIMIT 20) t;'
    driver.require(original.count(projection) == 1 and original.endswith(boundary),
                   'Original query_limit text differs from its admitted shape')
    constant = original.replace(projection, '(SELECT 1 FROM ', 1)
    order = " ORDER BY MATCH(content) AGAINST('倒排索引 tokenizer') DESC"
    return {
        'id_unordered': original,
        'constant_unordered': constant,
        'id_ordered': original.replace(boundary, order + boundary, 1),
        'constant_ordered': constant.replace(boundary, order + boundary, 1),
    }


def rotated_order(position, sample):
    offset = (position + sample) % len(CASE_NAMES)
    return list(CASE_NAMES[offset:] + CASE_NAMES[:offset])


def measure(call, out, name, query, phase, repetitions, executions):
    label = 'diagnostic-' + name + '-' + phase
    sql_path, output_path = out / (label + '.sql'), out / (label + '.log')
    text = common.SESSION + (query + '\n') * repetitions
    sql_path.write_text(text, encoding='utf-8')
    row = dict(case=name, phase=phase, repetitions=repetitions, query=query,
               querySha256=hashlib.sha256(query.encode('utf-8')).hexdigest(),
               executedSqlPath=str(sql_path), executedSqlSha256=common.digest(sql_path),
               startedUtc=utc(), startedMonotonicNs=time.monotonic_ns(), passed=False)
    executions.append(row)
    try:
        row['invocation'] = common.capture_group(
            [str(call.obclient), '-h127.0.0.1', '-P' + str(call.port), '-uroot', '-A', '-N', '-B',
             '--raw', '--default-character-set=utf8mb4', '-Doceanbase'],
            output_path, Path.cwd(), 60, env=os.environ.copy(), stdin=text.encode('utf-8'))
    except Exception as error:
        row['captureError'] = dict(type=type(error).__name__, message=str(error))
        raise
    finally:
        row.update(finishedUtc=utc(), finishedMonotonicNs=time.monotonic_ns())
        row['elapsedNs'] = row['finishedMonotonicNs'] - row['startedMonotonicNs']
        row['averageMs'] = row['elapsedNs'] / 1_000_000 / repetitions
        raw = output_path.read_bytes() if output_path.is_file() else b''
        row.update(outputPath=str(output_path), outputBytes=len(raw), outputSha256=hashlib.sha256(raw).hexdigest(),
                   allResultsEqual20=raw.splitlines() == [b'20'] * repetitions)
        invocation = row.get('invocation', {})
        row['passed'] = (invocation.get('exitCode') == 0 and invocation.get('childrenCleaned') is True
                         and row['allResultsEqual20'] and row['elapsedNs'] > 0 and not row.get('captureError'))
        driver.dump(out / (label + '-measurement.json'), row)
    driver.require(row['passed'], 'Diagnostic repeated SQL failed: ' + label)
    return row


def diagnostic_work(runtime_args, identities, role, position, baseline, scorer):
    source = runtime_args.root / 'sources/baseline' if role == 'baseline' else runtime_args.source
    commit = PINS[role]['commit']
    benchmark_args = argparse.Namespace(source=source, workspace=runtime_args.source, role=role,
        expected_sha=commit, binary=Path(identities[role]['binary']),
        obclient=runtime_args.source / 'deps/3rd/u01/obclient/bin/obclient')
    benchmark = common.benchmark_work(benchmark_args, baseline, scorer, False)
    cases = query_cases(source)

    def work(call, out, pid):
        preparation = benchmark(call, out)
        driver.require(preparation.get('passed'), 'Original 20K benchmark preparation failed')
        diagnostics = dict(scope=SCOPE, role=role, elfSourceCommit=commit, ownedPid=pid,
            warmup=WARMUP, samples=SAMPLES, queryRounds=QUERY_ROUNDS, passed=False,
            queries=cases, checks={}, countAndPlanCheckOrder=[], executionOrder=[], executions=[],
            timingMethod='Wall-clock monotonic nanoseconds around capture_group, including client launch and receipt finalization, divided by repetitions',
            originalNineMetricResult=False, formalContestRun=False)
        try:
            for name in rotated_order(position, 0):
                diagnostics['countAndPlanCheckOrder'].append(name)
                query = cases[name]
                result = driver.sql(call, out, 'diagnostic-' + name + '-count', query)
                driver.require(result.splitlines() == ['20'], 'Diagnostic cardinality differs: ' + name)
                plan = driver.sql(call, out, 'diagnostic-' + name + '-explain', 'EXPLAIN ' + query)
                relevance = role == 'baseline' or name.endswith('_ordered')
                check = count_only.check_count_plan(plan, relevance, not relevance)
                diagnostics['checks'][name] = dict(count=20, plan=check,
                    countOutputSha256=common.digest(out / ('diagnostic-' + name + '-count.log')),
                    explainOutputSha256=common.digest(out / ('diagnostic-' + name + '-explain.log')))
            phases = [('warmup', WARMUP)] + [('sample-' + str(index), QUERY_ROUNDS)
                                               for index in range(1, SAMPLES + 1)]
            for sample, (phase, repetitions) in enumerate(phases):
                order = rotated_order(position, sample)
                actual_order = dict(phase=phase, plannedCases=order, cases=[])
                diagnostics['executionOrder'].append(actual_order)
                for name in order:
                    actual_order['cases'].append(name)
                    measure(call, out, name, cases[name], phase, repetitions, diagnostics['executions'])
            diagnostics['meansMs'] = {name: statistics.mean(row['averageMs'] for row in diagnostics['executions']
                if row['case'] == name and row['phase'].startswith('sample-')) for name in CASE_NAMES}
            diagnostics['passed'] = True
            return dict(passed=True, originalBenchmarkPreparation=preparation, projectionDiagnostics=diagnostics)
        finally:
            driver.dump(out / 'projection-diagnostics.json', diagnostics)
    return work


def prepare(args, report):
    proof = diagnostic_source_proof(args.source, args.driver_sha)
    driver.dump(args.root / 'evidence/diagnostic-source-proof.json', proof)
    report['diagnosticDriver'] = proof
    for role in ('baseline', 'candidate'):
        target = args.root / 'sources' / role
        receipt = common.capture_group(['git', '-c', 'gc.auto=0', 'clone', '--shared', '--no-checkout',
            str(args.source), str(target)], args.root / 'evidence' / role / 'clone.log', args.source, 120)
        driver.require(receipt['exitCode'] == 0 and receipt['childrenCleaned'], 'Frozen source clone failed')
        common.git(target, 'checkout', '--detach', PINS[role]['commit'])
        common.source_identity(target, PINS[role]['commit'])
        driver.require(driver.inventory(target) == proof['originalInputHashes'], 'Frozen original inputs differ')
    frozen = args.root / 'sources/candidate'
    runtime_args = argparse.Namespace(source=frozen, root=args.root, driver_sha=CANDIDATE)
    frozen_proof = count_only.source_proof(runtime_args)
    plan = dict(scope=SCOPE, proof=frozen_proof, diagnosticDriver=proof, baselineRebuilt=False,
        candidateBuildsRequired=0, newBuilds=0, formalContestRun=False,
        benchmarkConfig=common.CONFIG, schedule=[dict(name=name, role=role) for name, role in SCHEDULE],
        diagnosticConfig=dict(warmup=WARMUP, samples=SAMPLES, query_rounds=QUERY_ROUNDS))
    driver.dump(args.root / 'plan.json', plan)
    driver.require(args.obclient.stat().st_size == CLIENT_BYTES and common.digest(args.obclient) == CLIENT_SHA,
                   'Exact official obclient differs')
    client_receipt = json.loads(args.client_receipt.read_text())
    driver.require({row['name']: row['sha256'] for row in client_receipt['packages']} == RPM_HASHES,
                   'Official RPM extraction receipt differs')
    client = frozen / 'deps/3rd/u01/obclient/bin/obclient'
    client.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.obclient, client)
    client.chmod(0o555)
    driver.dump(args.root / 'evidence/client-extraction.json', client_receipt)
    identities = {role: extract_elf(getattr(args, role + '_zip'), role, args.root, proof['originalInputHashes'])
                  for role in ('baseline', 'candidate')}
    report['builds'] = identities
    for role, identity in identities.items():
        binary = Path(identity['binary'])
        driver.verify_version(runtime_args, binary, args.root / 'evidence' / role / 'version.log', PINS[role]['commit'])
        common.verify_loader(binary, args.root / 'evidence' / role / 'ldd.log', frozen,
                             common.runtime_env(frozen, binary))
    common.verify_loader(client, args.root / 'evidence/client-ldd.log', frozen, common.runtime_env(frozen, client))
    driver.dump(args.root / 'report.json', report)
    return runtime_args, identities


def run(args):
    common.no_symlinks(args.root)
    driver.require(not args.root.exists() and args.root != args.source and args.root not in args.source.parents
                   and args.source not in args.root.parents, 'Fresh disjoint diagnostic root required')
    args.root.mkdir()
    for directory in ('evidence/baseline', 'evidence/candidate', 'runtime/baseline', 'runtime/candidate',
                      'sources', 'validation'):
        (args.root / directory).mkdir(parents=True)
    report = dict(scope=SCOPE, status='preparing', formalContestRun=False, newBuilds=0,
                  originalNineMetricResult=False, instances=[], rounds=[], startedUtc=utc())
    driver.dump(args.root / 'report.json', report)
    deadline = datetime.datetime.fromisoformat(args.stop_deadline_utc.replace('Z', '+00:00'))
    remaining = (deadline - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    budget = min(RUN_SECONDS, math.floor(remaining - CLEANUP_SECONDS))
    driver.require(budget > 0, 'No execution window remains before cleanup reserve')
    report['budget'] = dict(executionSeconds=budget, cleanupReserveSeconds=CLEANUP_SECONDS,
                           stopDeadlineUtc=args.stop_deadline_utc)

    def expired(signum, frame):
        raise TimeoutError('Diagnostic execution budget expired; preserve owned cleanup')

    signal.signal(signal.SIGALRM, expired)
    execution_end = time.monotonic() + budget
    signal.alarm(budget)
    try:
        driver.resource_record(args.root / 'evidence/resources.json', 12 * 1024**3)
        count_only.parent.prepare_nofile(argparse.Namespace(root=args.root, phase='run'))
        runtime_args, identities = prepare(args, report)
        profile_path = args.source / '.github/script/seekdb/fts-profile-runtime/run_profile.py'
        adapter = common.load_module('projection_original_profile_adapter', profile_path)
        portable = args.root / 'portable'
        portable.mkdir()
        archive_path = profile_path.with_name('portable-original-v2.tar.gz')
        driver.require(common.digest(archive_path) == 'b384f615bd0953dd3ca5602d002007d0fdebb16204a7d23f71b275625867eda5',
                       'Original portable archive differs')
        with tarfile.open(archive_path, 'r:gz') as archive:
            archive.extractall(portable, filter='data')
        bundle = portable / 'fts-portable-original-benchmark-v2-20261111'
        report['originalPortableProof'] = adapter.verify_portable(bundle)
        guard = adapter.load_driver(bundle)
        observation_report = {}
        adapter.install_observation_recovery(guard, argparse.Namespace(
            window_deadline_utc=args.stop_deadline_utc, window_buffer_seconds=CLEANUP_SECONDS), observation_report)
        report['processObservationRecovery'] = observation_report
        baseline = json.loads((runtime_args.source / 'tools/benchmark/fts_large_bench_baseline.json').read_text())
        scorer = common.load_module('projection_original_scorer', runtime_args.source / 'tools/benchmark/fts_large_bench_score.py')
        report['status'] = 'running'
        for position, (name, role) in enumerate(SCHEDULE):
            remaining_execution = math.floor(execution_end - time.monotonic())
            driver.require(remaining_execution > 0, 'Execution window exhausted before next owned instance')
            signal.alarm(remaining_execution)
            observation = guard.process_observation(1)
            driver.dump(args.root / 'evidence' / (name + '-process-observation.json'), observation)
            guard.guard_processes(observation)
            inner = diagnostic_work(runtime_args, identities, role, position, baseline, scorer)

            def work_with_cleanup_reserve(call, out, pid):
                try:
                    remaining_work = math.floor(execution_end - time.monotonic())
                    driver.require(remaining_work > 0, 'Execution window exhausted during owned startup')
                    signal.alarm(remaining_work)
                    return inner(call, out, pid)
                finally:
                    signal.alarm(0)

            signal.alarm(0)
            original_capture = driver.native_harness.capture

            def capture_with_startup_window(command, output, cwd, timeout, *other, **kwargs):
                if Path(output).name in ('start.log', 'ready.log', 'actual-parameters.log'):
                    remaining_startup = execution_end - time.monotonic()
                    driver.require(remaining_startup > 0, 'Execution window exhausted during owned startup capture')
                    timeout = min(timeout, math.ceil(remaining_startup))
                return original_capture(command, output, cwd, timeout, *other, **kwargs)

            driver.native_harness.capture = capture_with_startup_window
            try:
                row = driver.run_owned(runtime_args, report, identities, role, name, work_with_cleanup_reserve)
            finally:
                driver.native_harness.capture = original_capture
            common.require_instance(row)
            report['rounds'].append(dict(name=name, **row['work']['projectionDiagnostics']))
            driver.dump(args.root / 'report.json', report)
        report['comparisons'] = [dict(baseline=a, candidate=b, cases={case: dict(
            baselineMs=report['rounds'][ia]['meansMs'][case], candidateMs=report['rounds'][ib]['meansMs'][case],
            improvement=(report['rounds'][ia]['meansMs'][case] - report['rounds'][ib]['meansMs'][case])
                        / report['rounds'][ia]['meansMs'][case]) for case in CASE_NAMES})
            for a, b, ia, ib in (('A1', 'B1', 0, 1), ('A2', 'B2', 3, 2))]
        driver.require(len(report['instances']) == 4 and all(row['cleanupPassed'] for row in report['instances']),
                       'All four owned instances must finish cleanup')
        diagnostic_source_proof(args.source, args.driver_sha)
        report.update(status='projection_diagnostics_completed', passed=True, finishedUtc=utc(),
                      allFourCountsEqual20=True, significanceClaimed=False)
    except Exception as error:
        report.update(status='failed', passed=False, finishedUtc=utc(),
                      failure=dict(type=type(error).__name__, message=str(error), traceback=traceback.format_exc()))
        raise
    finally:
        signal.alarm(0)
        driver.dump(args.root / 'report.json', report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('run', 'collect'), required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--driver-sha')
    parser.add_argument('--baseline-zip', type=Path)
    parser.add_argument('--candidate-zip', type=Path)
    parser.add_argument('--obclient', type=Path)
    parser.add_argument('--client-receipt', type=Path)
    parser.add_argument('--stop-deadline-utc')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    if args.phase == 'collect':
        return driver.collect(args)
    driver.require(sys.platform == 'linux', 'Owned database diagnostics require Linux')
    common.install_termination_handlers()
    return run(args)


if __name__ == '__main__':
    main()
