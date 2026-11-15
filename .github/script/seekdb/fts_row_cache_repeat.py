#!/usr/bin/env python3
"""Repeat the original nine metrics using two pinned, already built executables."""
import argparse
import datetime
import hashlib
import json
import math
import shutil
import signal
import statistics
import sys
import tarfile
import time
import traceback
from pathlib import Path

import fts_count_projection_diagnostics as archived
import fts_ddl_row_cache_validation as row_cache


driver, common = archived.driver, archived.common
BASE_SOURCE = '2abe2633f85f41c1825fb144b29e2d3496e614df'
CANDIDATE = '603075716540b4f14dff9e8650bbcb0d4d60d338'
CANDIDATE_TREE = '28040eace1ed1439f7e28d2dd4fc4ac7e47fe901'
PRODUCTS = frozenset(row_cache.PRODUCTS)
SCHEDULE = (('A1', 'baseline'), ('B1', 'candidate'), ('B2', 'candidate'), ('A2', 'baseline'),
            ('B3', 'candidate'), ('A3', 'baseline'), ('A4', 'baseline'), ('B4', 'candidate'))
PAIRS = (('A1', 'B1'), ('A2', 'B2'), ('A3', 'B3'), ('A4', 'B4'))
ALLOWED_DRIVER_FILES = frozenset((
    '.github/script/seekdb/fts_row_cache_repeat.py',
    '.github/script/seekdb/test_fts_row_cache_repeat.py',
    '.github/workflows/fts-row-cache-repeat.yml',
    '.github/workflows/fts-profile-diagnostics.yml',
))
archived.PINS = {
    'baseline': dict(commit='9eaede23451fa9bfeae878c73aa56a7e0a04785a',
        tree='7d7ca86448201fc8c39e99c45d8c3036f96a66e2', artifact=14115653708, run=43596282503,
        zipBytes=329599861, zipSha256='18f35d2509c2e4dab0cf28e70c9bb06f9d6dc7b71c2105923081646cbb22f84e',
        tarBytes=329576852, tarSha256='3eb6fb87c38ee3cc5ad9dc32fb76a9667a40fc9f4975ce1c3bd03cb3a7887029',
        elfBytes=1332421056, elfSha256='44e7d8d1054acf40811f4925ccc914331f45b793002dd8b54784d6b9385153b4',
        buildId='41599745ef85ddee1ef586763c3da42b34f8561f'),
    'candidate': dict(commit=CANDIDATE, tree=CANDIDATE_TREE, artifact=14144705933, run=43667118747,
        zipBytes=329606064, zipSha256='486c4cfe5246b4e59295686e408ebcb2ca3c4299315a69acbae0398978a88555',
        tarBytes=329583055, tarSha256='4e9d55e3b38bdc04d0292e1fabf4d2990ff75bb829195901cc21048457b9a010',
        elfBytes=1332426096, elfSha256='04fc8e7df52a9f6596d4e4666709972b5a8998684d248aa190bf25d63af5b3bd',
        buildId='356b93d1932ce02ef58ab2fe7a97de72db3b7e92'),
}


def configure():
    row_cache.configure()
    driver.SCHEDULE = SCHEDULE


def diagnostic_source_proof(source, commit):
    identity = common.source_identity(source, commit)
    changed = set(common.git(source, 'diff', '--name-only', CANDIDATE, commit, '--').decode().splitlines())
    driver.require(changed == ALLOWED_DRIVER_FILES, 'Unexpected changes in the repeat driver')
    driver.require(common.git(source, 'rev-parse', CANDIDATE + '^{tree}').decode().strip() == CANDIDATE_TREE,
                   'Candidate source tree differs')
    products = {}
    for path in PRODUCTS:
        original = common.git(source, 'show', CANDIDATE + ':' + path)
        expected_blob = common.git(source, 'rev-parse', CANDIDATE + ':' + path).decode().strip()
        working_blob = common.git(source, 'hash-object', '--path=' + path, path).decode().strip()
        driver.require(working_blob == expected_blob, 'Candidate product file differs')
        products[path] = hashlib.sha256(original).hexdigest()
    inputs = driver.inventory(source)
    return dict(driverCommit=commit, driverTree=identity['tree'], candidateCommit=CANDIDATE,
                candidateTree=CANDIDATE_TREE, productBlobSha256=products,
                originalInputCount=len(inputs), originalInputHashes=inputs,
                originalInputInventorySha256=driver.INVENTORY, newBuilds=0)


def prepare(args, report):
    proof = diagnostic_source_proof(args.source, args.driver_sha)
    driver.dump(args.root / 'evidence/diagnostic-source-proof.json', proof)
    for role, commit in (('baseline', BASE_SOURCE), ('candidate', CANDIDATE)):
        target = args.root / 'sources' / role
        receipt = common.capture_group(['git', '-c', 'gc.auto=0', 'clone', '--shared', '--no-checkout',
            str(args.source), str(target)], args.root / 'evidence' / role / 'clone.log', args.source, 120)
        driver.require(receipt['exitCode'] == 0 and receipt['childrenCleaned'], 'Frozen source clone failed')
        common.git(target, 'checkout', '--detach', commit)
        common.source_identity(target, commit)
        driver.require(driver.inventory(target) == proof['originalInputHashes'], 'Frozen original inputs differ')
    frozen = args.root / 'sources/candidate'
    runtime_args = argparse.Namespace(source=frozen, root=args.root, driver_sha=CANDIDATE)
    frozen_proof = row_cache.source_proof(runtime_args)
    driver.require(frozen_proof['baselineCommit'] == BASE_SOURCE and
                   frozen_proof['originalInputHashes'] == proof['originalInputHashes'], 'Frozen source proof differs')
    plan = dict(scope='PRESERVED_ELF_NINE_METRIC_REPEAT', proof=frozen_proof, diagnosticDriver=proof,
                baselineRebuilt=False, candidateBuildsRequired=0, newBuilds=0, formalContestRun=False,
                benchmarkConfig=common.CONFIG, warmupRoles=['baseline', 'candidate'],
                schedule=[dict(name=name, role=role) for name, role in SCHEDULE],
                archivedBuilds=archived.PINS)
    driver.dump(args.root / 'plan.json', plan)
    driver.require(args.obclient.stat().st_size == archived.CLIENT_BYTES and
                   common.digest(args.obclient) == archived.CLIENT_SHA, 'SQL client identity differs')
    client_receipt = json.loads(args.client_receipt.read_text())
    driver.require({x['name']: x['sha256'] for x in client_receipt['packages']} == archived.RPM_HASHES,
                   'SQL client extraction receipt differs')
    client = frozen / 'deps/3rd/u01/obclient/bin/obclient'
    client.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.obclient, client)
    client.chmod(0o555)
    driver.dump(args.root / 'evidence/client-extraction.json', client_receipt)
    identities = {role: archived.extract_elf(getattr(args, role + '_zip'), role, args.root,
                                             proof['originalInputHashes']) for role in ('baseline', 'candidate')}
    report['builds'] = identities
    report['plan'] = plan
    for role, identity in identities.items():
        binary = Path(identity['binary'])
        driver.verify_version(runtime_args, binary, args.root / 'evidence' / role / 'version.log',
                              archived.PINS[role]['commit'])
        common.verify_loader(binary, args.root / 'evidence' / role / 'ldd.log', frozen,
                             common.runtime_env(frozen, binary))
    common.verify_loader(client, args.root / 'evidence/client-ldd.log', frozen, common.runtime_env(frozen, client))
    driver.dump(args.root / 'report.json', report)
    return runtime_args, identities


def summarize(rounds, baseline, scorer):
    metrics = {row['name']: row['metrics'] for row in rounds}
    driver.require(len(rounds) == len(SCHEDULE) and set(metrics) == dict(SCHEDULE).keys(),
                   'Missing or duplicate measurement rounds')
    pairs = []
    for a, b in PAIRS:
        reference = dict(baseline, baseline_metrics=metrics[a])
        driver.require(scorer.check_config(metrics[a], reference, False) == [] and
                       scorer.check_config(metrics[b], reference, False) == [], 'Benchmark configuration differs')
        score = scorer.calculate_score(metrics[b], reference)
        pairs.append(dict(baseline=a, candidate=b, improvement=score['mean_improvement'],
                          categories=score['categories']))
    return dict(pairs=pairs, meanImprovement=statistics.mean(x['improvement'] for x in pairs),
                samplesPerRole=4, significanceClaimed=False, formalContestScore=False)


def run(args):
    common.no_symlinks(args.root)
    driver.require(not args.root.exists() and args.root != args.source and args.root not in args.source.parents
                   and args.source not in args.root.parents, 'Fresh disjoint diagnostic root required')
    args.root.mkdir()
    for directory in ('evidence/baseline', 'evidence/candidate', 'runtime/baseline', 'runtime/candidate',
                      'sources', 'validation'):
        (args.root / directory).mkdir(parents=True)
    report = dict(scope='PRESERVED_ELF_NINE_METRIC_REPEAT', status='preparing', formalContestRun=False,
                  newBuilds=0, instances=[], rounds=[], warmups=[], startedUtc=archived.utc())
    driver.dump(args.root / 'report.json', report)
    deadline = datetime.datetime.fromisoformat(args.stop_deadline_utc.replace('Z', '+00:00'))
    remaining = (deadline - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    cleanup_reserve = 180
    budget = min(2100, math.floor(remaining - cleanup_reserve))
    driver.require(budget > 0, 'No execution window remains before cleanup reserve')
    report['budget'] = dict(executionSeconds=budget, cleanupReserveSeconds=cleanup_reserve,
                            stopDeadlineUtc=args.stop_deadline_utc)

    def expired(signum, frame):
        raise TimeoutError('Repeat execution budget expired')

    signal.signal(signal.SIGALRM, expired)
    execution_end = time.monotonic() + budget
    signal.alarm(budget)
    try:
        driver.resource_record(args.root / 'evidence/resources.json', 12 * 1024**3)
        archived.count_only.parent.prepare_nofile(argparse.Namespace(root=args.root, phase='run'))
        runtime_args, identities = prepare(args, report)
        profile_path = args.source / '.github/script/seekdb/fts-profile-runtime/run_profile.py'
        adapter = common.load_module('repeat_original_profile_adapter', profile_path)
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
        observations = {}
        adapter.install_observation_recovery(guard, argparse.Namespace(
            window_deadline_utc=args.stop_deadline_utc, window_buffer_seconds=cleanup_reserve), observations)
        report['processObservationRecovery'] = observations
        baseline = json.loads((runtime_args.source / 'tools/benchmark/fts_large_bench_baseline.json').read_text())
        scorer = common.load_module('repeat_original_scorer', runtime_args.source / 'tools/benchmark/fts_large_bench_score.py')
        report['status'] = 'running'
        schedule = (('warmup-baseline', 'baseline'), ('warmup-candidate', 'candidate')) + SCHEDULE
        for name, role in schedule:
            driver.require(math.floor(execution_end - time.monotonic()) > 0, 'Execution window exhausted')
            observation = guard.process_observation(1)
            driver.dump(args.root / 'evidence' / (name + '-process-observation.json'), observation)
            guard.guard_processes(observation)
            inner = row_cache.validation.benchmark_callback(runtime_args, identities, role, baseline, scorer)

            def work_with_cleanup_reserve(call, out, pid):
                try:
                    available = math.floor(execution_end - time.monotonic())
                    driver.require(available > 0, 'Execution window exhausted during owned startup')
                    signal.alarm(available)
                    return inner(call, out, pid)
                finally:
                    signal.alarm(0)

            signal.alarm(0)
            original_capture = driver.native_harness.capture

            def capture_with_startup_window(command, output, cwd, timeout, *other, **kwargs):
                if Path(output).name in ('start.log', 'ready.log', 'actual-parameters.log'):
                    available = execution_end - time.monotonic()
                    driver.require(available > 0, 'Execution window exhausted during startup')
                    timeout = min(timeout, math.ceil(available))
                return original_capture(command, output, cwd, timeout, *other, **kwargs)

            driver.native_harness.capture = capture_with_startup_window
            try:
                row = driver.run_owned(runtime_args, report, identities, role, name, work_with_cleanup_reserve)
            finally:
                driver.native_harness.capture = original_capture
            common.require_instance(row)
            record = dict(name=name, role=role, **row['work'])
            report['warmups' if name.startswith('warmup-') else 'rounds'].append(record)
            driver.dump(args.root / 'report.json', report)
        driver.require(len(report['instances']) == 10 and all(x['cleanupPassed'] for x in report['instances']),
                       'All owned instances must finish cleanup')
        for row in report['rounds']:
            m = row['metrics']
            driver.require([m['query_' + x + '_hits'] for x in ('cn', 'beng', 'mixed', 'limit')]
                           == [8001, 11000, 7332, 20], 'Benchmark hit counts differ')
        diagnostic_source_proof(args.source, args.driver_sha)
        report.update(status='original_nine_metric_repeat_completed', passed=True, finishedUtc=archived.utc(),
                      originalNineMetricResult=True, comparisons=summarize(report['rounds'], baseline, scorer))
    except BaseException as error:
        report.update(status='failed', passed=False, finishedUtc=archived.utc(),
                      failure=dict(type=type(error).__name__, message=str(error), traceback=traceback.format_exc()))
        raise
    finally:
        signal.alarm(0)
        driver.dump(args.root / 'report.json', report)


def main():
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
    args = parser.parse_args()
    configure()
    if args.phase == 'collect':
        return driver.collect(args)
    driver.require(sys.platform == 'linux', 'Owned database diagnostics require Linux')
    common.install_termination_handlers()
    return run(args)


if __name__ == '__main__':
    main()
