#!/usr/bin/env python3
"""Compare old and encoded SQL sorting in the preserved complete bd5d ELF."""
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
import fts_encoded_sort_cases as cases


driver, common = count_only.driver, count_only.common
BASE = count_only.BASE
CANDIDATE = 'bd5d28fda9552daca40ab81a2ef32b8ca663d426'
CANDIDATE_TREE = 'c82876324f044cd66f5d707815c1a3a4dbd3f448'
SCOPE = 'PRESERVED_ELF_ENCODED_SORT_DIAGNOSTICS'
SCHEDULE = (('B1', 'candidate'), ('B2', 'candidate'))
RUN_SECONDS, CLEANUP_SECONDS = 900, 180
CLIENT_BYTES = 14592368
CLIENT_SHA = '798aa952d536563ee0291e69251a6d0cd0d85e0c470d5499768fa0731ff3c1be'
RPM_HASHES = {
    'obclient-2.2.3-1.el8.x86_64.rpm': '35e38b580996fc583b0eaa961bd78911feff08981624154851db51d7e5aa935d',
    'libobclient-2.2.3-1.el8.x86_64.rpm': 'e53cadb0d91c1a0d78369f0c9488be8d774f03ea09346e7b218799a5318a9a68',
}
PINS = {'candidate': {'commit': 'bd5d28fda9552daca40ab81a2ef32b8ca663d426', 'tree': 'c82876324f044cd66f5d707815c1a3a4dbd3f448', 'artifact': 13941216422, 'run': 43204498012, 'zipBytes': 329590277, 'zipSha256': 'aa61f704b2af0cfb070473123f984f457b9145050ac3913e90f6430cb67d8ea2', 'tarBytes': 329567268, 'tarSha256': '33df822f7840d6829b739bb2e0c568c1c492268496dfb99ced8915d107430c21', 'elfBytes': 1332384520, 'elfSha256': '8b83cb8fc4b2f99196437e63b8d6a6c4c3d08443c3030942552c52d2ee0b63e3', 'buildId': '66082815f1b9cf9fc5261081d5c3535471990b9a'}}


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def diagnostic_source_proof(source, commit):
    identity = common.source_identity(source, commit)
    changed = common.git(source, 'diff', '--name-only', CANDIDATE, commit, '--').decode().splitlines()
    driver.require(all(path.startswith('.github/') for path in changed),
                   'Diagnostic source contains a product change beyond fixed bd5d')
    products = {}
    for path in count_only.PRODUCTS:
        raw = common.git(source, 'show', CANDIDATE + ':' + path)
        driver.require(raw == (source / path).read_bytes(), 'Fixed bd5d product bytes differ: ' + path)
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


def diagnostic_work(runtime_args, identities, role, position, baseline, scorer):
    def work(call, out, pid):
        return cases.work(driver, common, call, out, pid, position)
    return work


def prepare(args, report):
    proof = diagnostic_source_proof(args.source, args.driver_sha)
    driver.dump(args.root / 'evidence/diagnostic-source-proof.json', proof)
    report['diagnosticDriver'] = proof
    for role in ('candidate',):
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
        diagnosticConfig=dict(rows=len(cases.WORDS) * len(cases.DOC_IDS), caseModule='fts_encoded_sort_cases'))
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
                  for role in ('candidate',)}
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
        adapter = common.load_module('sort_original_profile_adapter', profile_path)
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
        scorer = common.load_module('sort_original_scorer', runtime_args.source / 'tools/benchmark/fts_large_bench_score.py')
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
            report['rounds'].append(dict(name=name, **row['work']))
            driver.dump(args.root / 'report.json', report)
        driver.require(len(report['instances']) == 2 and all(row['cleanupPassed'] for row in report['instances']),
                       'Both owned instances must finish cleanup')
        diagnostic_source_proof(args.source, args.driver_sha)
        report.update(status='sort_diagnostics_completed', passed=True, finishedUtc=utc(),
                      allRowOrdersEqual=True, significanceClaimed=False)
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
