#!/usr/bin/env python3
"""Build one new B, reuse only original A, and run a fresh strict calibration.

Historical two-ELF continuation remains in fts_paired_validation.py. This explicit
source-hybrid mode never changes its identity pins or re-labels its old candidate.
Plan-only unless --execute is supplied. No downloads or builds occur in Python.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import traceback

import fts_paired_validation as legacy
from fts_plan_compatibility import classify_plan_metadata_compatibility
from fts_paired_validation import (BASELINE, PREFIX, CASES, REGRESSIONS, SCHEDULE, PAIRS,
    BENCH, CONFIG, CLIENTS, QUERY_NAMES, PLAN_CONTROLS, MARGIN, SOURCE_TREES,
    digest, write_json, git, source_identity, no_symlinks, contains_bytes,
    capture_group, verify_loader, runtime_env, load_module, require_instance,
    effective_parameters, benchmark_work, accepted_plan_assertions, performance,
    install_termination_handlers)
from fts_verified_elf_reuse import prepare_verified_runtime

MODE = 'verified_original_A_new_source_B_v1'
PRODUCTION_KERNEL = 'c235713689325d2bb8882b86f8f89ee99ff9b43a'
PRODUCTION_KERNEL_TREE = 'f782796c4ac16163aa0b776630609fd73a102e2c'
INVENTORY_SHA256 = 'f36e2d1c8aaabd3e9ffc4f8e3635defca6dc3046e3d575e6e73165fa8d80a135'
REJECT_SHA256 = '737c4257fabf57dbd5365fdd04cfe2573206129d19991a8a1616e7e3db1e4896'
# The integrating production commit supplies this new native contract.
NEW_CASES = ('fts_index.tokenize_beng_noop_casefold_contract',)
DRIVER_FILES = legacy.DRIVER_FILES + ('fts_source_hybrid_validation.py',)
ALLOWED_DRIVER_PATHS = legacy.ALLOWED_DRIVER_PATHS | frozenset((
    PREFIX + 'fts_source_hybrid_validation.py', PREFIX + 'test_fts_source_hybrid_validation.py',
    PREFIX + 'fixtures/fts-verified-elf-continuation.yml',
    PREFIX + 'test_fts_beng_noop_casefold.py'))
PROTECTED_BUILD = ('build.sh', 'CMakeLists.txt', 'cmake', 'rust', 'deps',
    '.github/actions/setup-rust', 'tools/cmake')
OLD_KERNELS = {BASELINE, legacy.CANDIDATE, legacy.KERNEL,
               'fe876f8f805969970621a18a1cf812ff322fc78c'}


def exact_sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value) is not None


def inventory(source):
    harness = load_module('hybrid_unchanged_harness', source / PREFIX / 'fts_contest_validation.py')
    result = harness.original_inputs(source)
    canonical = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if len(result) != 75 or canonical != INVENTORY_SHA256:
        raise RuntimeError('All 75 original official inputs must match the canonical inventory')
    return result


def selected_cases(role):
    if role not in ('baseline', 'candidate'):
        raise RuntimeError('Native case role confusion')
    return CASES + (REGRESSIONS + NEW_CASES if role == 'candidate' else ())


def case_input_hashes(source):
    result = {}
    for name in selected_cases('candidate'):
        suite, case = name.split('.')
        base = source / 'tools/deploy/mysql_test/test_suite' / suite
        result[name] = dict(testSha256=digest(base / 't' / (case + '.test')),
            expectedSha256=digest(base / 'r/mysql' / (case + '.result')))
    if result[legacy.DIAGNOSTIC_CASE] != dict(testSha256=legacy.DIAGNOSTIC_TEST_SHA,
            expectedSha256=legacy.DIAGNOSTIC_RESULT_SHA):
        raise RuntimeError('The current syntax-correct match-only diagnostic changed')
    if result[NEW_CASES[0]] != dict(
            testSha256='1af6a11fbc111cf26b6d0ef68752b4d26558a3382d21b4f4044f133aada91354',
            expectedSha256='62afb026fd38fb30cdb2a11c6b023df963ea85d8d01e76930e3fdb1d63a89300'):
        raise RuntimeError('New Beng native contract differs from pinned production input')
    return result


def verify_driver(source, driver_sha, kernel_sha):
    if not exact_sha(driver_sha) or kernel_sha != PRODUCTION_KERNEL or driver_sha in OLD_KERNELS:
        raise RuntimeError('An explicit new production kernel and actual driver full SHA are required')
    identity = source_identity(source, driver_sha)
    if git(source, 'rev-parse', kernel_sha + '^{tree}').decode().strip() != PRODUCTION_KERNEL_TREE:
        raise RuntimeError('Pinned production kernel tree mismatch')
    # An exact tree comparison needs no history beyond the fetched commit objects.
    changed = set(git(source, 'diff', '--name-only', kernel_sha, driver_sha, '--').decode().splitlines())
    if not changed <= ALLOWED_DRIVER_PATHS:
        raise RuntimeError('Driver differs from explicit production kernel outside CI allowlist: '
                           + ', '.join(sorted(changed - ALLOWED_DRIVER_PATHS)))
    if git(source, 'diff', '--name-only', BASELINE, driver_sha, '--', *PROTECTED_BUILD).strip():
        raise RuntimeError('Original build/dependency/Rust profile changed')
    inputs = inventory(source)
    return dict(driverCommit=driver_sha, driverTree=identity['tree'], kernelOrigin=kernel_sha,
        kernelTree=git(source, 'rev-parse', kernel_sha + '^{tree}').decode().strip(),
        allowedDriverChanges=sorted(changed), originalInputCount=len(inputs),
        originalInputInventorySha256=INVENTORY_SHA256, caseInputs=case_input_hashes(source))


def plan_for(source, root, driver_sha, kernel_sha):
    return dict(scope='SAME_HOST_DEVELOPMENT_CALIBRATION_NOT_FORMAL_CONTEST', mode=MODE,
        driverCommit=driver_sha, validationDriverCommit=driver_sha, candidateBuildCommit=driver_sha,
        kernelOrigin=kernel_sha, baselineCommit=BASELINE, workspace=str(source), root=str(root),
        baselineSource=str(root / 'sources/baseline'), candidateSource=str(source),
        schedule=[dict(name=n, role=r) for n, r in SCHEDULE],
        pairs=[dict(baseline=a, candidate=b) for a, b in PAIRS], benchmarkConfig=CONFIG,
        performanceMargin=MARGIN, profiler=False, caseRetries=0,
        originalInputCount=75, originalInputInventorySha256=INVENTORY_SHA256,
        historicalCandidateExecuted=False, baselineRebuilt=False, candidateBuildsRequired=1)


def prepare(args):
    root, source = args.root, args.source
    no_symlinks(root)
    if root.exists() or root == source or source in root.parents or root in source.parents:
        raise RuntimeError('Preparation needs a fresh root disjoint from driver source')
    proof = verify_driver(source, args.driver_sha, args.kernel_sha)
    if args.artifact_zip is None:
        raise RuntimeError('Original immutable artifact ZIP is required')
    root.mkdir(parents=True)
    plan = plan_for(source, root, args.driver_sha, args.kernel_sha)
    plan.update(equivalence=proof, driverFiles={})
    for relative in ('driver', 'sources', 'historical', 'runtime/candidate',
                     'evidence/baseline', 'evidence/candidate', 'validation/cases/baseline',
                     'validation/cases/candidate', 'validation/untimed', 'validation/rounds'):
        (root / relative).mkdir(parents=True, exist_ok=True)
    for name in DRIVER_FILES:
        path = source / PREFIX / name
        expected = hashlib.sha256(git(source, 'show', args.driver_sha + ':' + PREFIX + name)).hexdigest()
        if digest(path) != expected:
            raise RuntimeError('Driver script hash mismatch: ' + name)
        shutil.copyfile(path, root / 'driver' / name)
        plan['driverFiles'][name] = expected
    write_json(root / 'plan.json', plan)
    git(source, 'worktree', 'add', '--detach', str(root / 'sources/baseline'), BASELINE)
    if source_identity(root / 'sources/baseline', BASELINE)['tree'] != SOURCE_TREES['baseline']:
        raise RuntimeError('Original A source tree changed')
    verified = prepare_verified_runtime(args.artifact_zip, root / 'historical/runtime')
    # Old B remains static evidence only, and cannot accidentally be executed.
    Path(verified['roles']['candidate']['binary']).chmod(0o444)
    plan.update(artifact=verified['artifact'],
        runtimeVerificationSha256=digest(root / 'historical/runtime/verification.json'))
    write_json(root / 'plan.json', plan)
    source_identity(source, args.driver_sha)
    return plan


def load_plan(args):
    plan = json.loads((args.root / 'plan.json').read_text(encoding='utf-8'))
    expected = plan_for(args.source, args.root, args.driver_sha, args.kernel_sha)
    if any(plan.get(key) != value for key, value in expected.items()):
        raise RuntimeError('Saved source-hybrid plan identity or configuration mismatch')
    if set(plan.get('driverFiles', {})) != set(DRIVER_FILES):
        raise RuntimeError('Incomplete saved driver identity')
    for name, expected_hash in plan['driverFiles'].items():
        path = args.root / 'driver' / name
        no_symlinks(path)
        if digest(path) != expected_hash:
            raise RuntimeError('Saved driver script changed: ' + name)
    if verify_driver(args.source, args.driver_sha, args.kernel_sha) != plan['equivalence']:
        raise RuntimeError('Driver/kernel/case source identity changed')
    return plan


def historical_baseline(args, plan):
    directory = args.root / 'historical/runtime'
    path = directory / 'verification.json'
    no_symlinks(path)
    if digest(path) != plan.get('runtimeVerificationSha256'):
        raise RuntimeError('Historical runtime verification changed')
    record = json.loads(path.read_text(encoding='utf-8'))
    if (record.get('status') != 'verified_runtime_prepared' or record.get('artifact') != plan.get('artifact')
            or record.get('runtimeRoot') != str(directory) or record.get('verificationPath') != str(path)
            or record.get('oldValidationAccepted') is not False
            or record.get('currentValidationPerformed') is not False
            or record.get('originalManifest', {}).get('ok') is not False
            or set(record.get('roles', {})) != {'baseline', 'candidate'}):
        raise RuntimeError('Historical verification or original collector failure missing')
    for role in ('baseline', 'candidate'):
        data = record['roles'][role]
        if (data.get('binary') != str(directory / role / 'seekdb')
                or data.get('identityOriginalPath') != str(directory / role / 'identity.original.json')
                or data.get('relocationPath') != str(directory / role / 'relocation.json')):
            raise RuntimeError('Historical role paths changed')
        legacy.validate_relocated_identity(data['identity'], role, args.root / 'historical', data['provenance'])
    if (directory / 'candidate/seekdb').stat().st_mode & 0o111:
        raise RuntimeError('Historical candidate must remain non-executable static evidence')
    data = record['roles']['baseline']
    return Path(data['binary']), data['identity']


def candidate_identity(identity, args, plan):
    expected = args.root / 'runtime/candidate/seekdb'
    if (identity.get('role') != 'candidate' or identity.get('sourceCommit') != args.driver_sha
            or identity.get('driverCommit') != args.driver_sha
            or identity.get('validationDriverCommit') != args.driver_sha
            or identity.get('kernelOrigin') != args.kernel_sha
            or identity.get('sourceTree') != plan['equivalence']['driverTree']
            or identity.get('binary') != str(expected)
            or identity.get('buildSettings') != dict(CMAKE_BUILD_TYPE='RelWithDebInfo', OB_ENABLE_UNITY='ON', OB_USE_LLD='ON')
            or not str(identity.get('rustVersion', '')).startswith('rustc 1.98.1 ')
            or identity.get('officialInputHashes') != inventory(args.source)
            or identity.get('caseInputs') != plan['equivalence']['caseInputs']):
        raise RuntimeError('New B actual source/driver/kernel/build/input identity mismatch')
    no_symlinks(expected)
    info = expected.stat()
    with expected.open('rb') as stream:
        magic = stream.read(4)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= 4 * 1024 ** 3
            or info.st_size != identity.get('bytes') or digest(expected) != identity.get('sha256')
            or magic != b'\x7fELF'
            or not contains_bytes(expected, args.driver_sha.encode('ascii'))
            or not re.fullmatch('[0-9a-f]+', str(identity.get('buildId', '')))):
        raise RuntimeError('New B full ELF byte/embedded source/BuildID identity mismatch')
    notes = subprocess.check_output(['readelf', '-n', str(expected)], timeout=30)
    if len(notes) > 1024 ** 2 or re.findall(rb'Build ID:\s*([0-9a-f]+)', notes) != [identity['buildId'].encode('ascii')]:
        raise RuntimeError('New B actual readelf BuildID differs from identity')
    version = args.source / 'build_release/src/observer/ob_version.cpp'
    if digest(version) != identity.get('generatedVersionSha256') or args.driver_sha not in version.read_text(encoding='utf-8'):
        raise RuntimeError('New B generated-version source identity changed')
    return expected


def prebuild(args, plan):
    source_identity(args.source, args.driver_sha)
    if (args.source / 'build_release/src/observer/seekdb').exists():
        raise RuntimeError('Prebuild requires no existing B binary')
    for client, expected in CLIENTS.items():
        path = args.source / 'deps/3rd/u01/obclient/bin' / client
        if digest(path) != expected:
            raise RuntimeError('Original client hash mismatch before B build: ' + client)
        verify_loader(path, args.root / 'evidence/candidate' / (client + '-prebuild-ldd.log'),
                      args.source, runtime_env(args.source, path))
    # Also make the actual original A loader decide before the expensive B build.
    binary, _ = historical_baseline(args, plan)
    verify_loader(binary, args.root / 'evidence/baseline/prebuild-ldd.log', args.source,
                  runtime_env(args.source, binary))
    record = dict(status='actual_ldd_prebuild_passed', driverCommit=args.driver_sha,
                  kernelOrigin=args.kernel_sha, clients=CLIENTS)
    write_json(args.root / 'evidence/candidate/prebuild.json', record)
    return record

def snapshot(args, plan):
    role = 'candidate'
    commit = args.driver_sha
    if args.role != role:
        raise RuntimeError('Source-hybrid snapshots only newly built B; A must never rebuild')
    source = args.source
    preflight = json.loads((args.root / 'evidence/candidate/prebuild.json').read_text())
    if preflight != dict(status='actual_ldd_prebuild_passed', driverCommit=args.driver_sha,
                         kernelOrigin=args.kernel_sha, clients=CLIENTS):
        raise RuntimeError('Actual client/A loader preflight was not completed before B compilation')
    source_id = source_identity(source, commit)
    verify_driver(source, args.driver_sha, args.kernel_sha)
    harness = load_module('paired_original_harness', args.root / 'driver/fts_contest_validation.py')
    inputs = inventory(source)
    built = source / 'build_release/src/observer/seekdb'
    no_symlinks(built)
    if not built.is_file() or built.stat().st_nlink != 1 or built.stat().st_size > 4 * 1024 ** 3:
        raise RuntimeError('Expected complete independently built ELF is missing/oversize/linked')
    version_path = source / 'build_release/src/observer/ob_version.cpp'
    if commit not in version_path.read_text(encoding='utf-8'):
        raise RuntimeError('Generated executable version does not identify actual build source')
    cache = {}
    for line in (source / 'build_release/CMakeCache.txt').read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([^#/:][^:]*):[^=]+=(.*)', line)
        if match:
            cache[match[1]] = match[2]
    for key, expected in dict(CMAKE_BUILD_TYPE='RelWithDebInfo', OB_ENABLE_UNITY='ON', OB_USE_LLD='ON').items():
        if cache.get(key) != expected:
            raise RuntimeError('Unexpected original build setting: ' + key)
    rust = subprocess.check_output(['rustc', '--version'], cwd=source / 'rust').decode().strip()
    if not rust.startswith('rustc 1.98.1 '):
        raise RuntimeError('Repository-pinned Rust 1.98.1 is required')
    destination = args.root / 'runtime' / role / 'seekdb'
    if destination.exists():
        raise RuntimeError('Refusing to replace a preserved role ELF')
    shutil.copyfile(built, destination)
    destination.chmod(0o555)
    evidence = args.root / 'evidence' / role
    note = capture_group(['readelf', '-n', str(destination)], evidence / 'elf-notes.log', source, 30)
    ids = re.findall(r'Build ID:\s*([0-9a-f]+)', (evidence / 'elf-notes.log').read_text(encoding='utf-8'))
    if note['exitCode'] or not note['childrenCleaned'] or len(ids) != 1:
        raise RuntimeError('Cannot bind exact ELF BuildID')
    record = dict(role=role, sourceCommit=commit, sourceTree=source_id['tree'],
        kernelOrigin=args.kernel_sha, driverCommit=args.driver_sha, validationDriverCommit=args.driver_sha,
        binary=str(destination), bytes=destination.stat().st_size, sha256=digest(destination), buildId=ids[0],
        generatedVersionSha256=digest(version_path), officialInputHashes=inputs,
        buildSettings={key: cache[key] for key in ('CMAKE_BUILD_TYPE', 'OB_ENABLE_UNITY', 'OB_USE_LLD')},
        rustVersion=rust, caseInputs=case_input_hashes(source))
    candidate_identity(record, args, plan)
    env = runtime_env(source, destination)
    record['loader'] = verify_loader(destination, evidence / 'seekdb-ldd.log', source, env)
    for client, expected in CLIENTS.items():
        path = source / 'deps/3rd/u01/obclient/bin' / client
        if digest(path) != expected:
            raise RuntimeError('Official client hash mismatch: ' + client)
        verify_loader(path, evidence / (client + '-ldd.log'), source, env)
    write_json(destination.parent / 'identity.json', record)
    visible = {key: record[key] for key in ('role', 'sourceCommit', 'sourceTree', 'kernelOrigin',
        'driverCommit', 'bytes', 'sha256', 'buildId', 'buildSettings', 'rustVersion', 'generatedVersionSha256')}
    visible.update(originalInputHashCount=len(inputs), originalInputInventorySha256=hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest(),
        loaderExitCode=record['loader']['exitCode'], loaderChildrenCleaned=record['loader']['childrenCleaned'])
    print('PAIRED_BUILD_IDENTITY ' + json.dumps(visible), flush=True)
    print('PAIRED_ACTUAL_LDD ' + role + '\n' + (evidence / 'seekdb-ldd.log').read_text(
        encoding='utf-8', errors='replace')[:16000], flush=True)
    return record

def accepted_compatibility(proof, row, validation_driver, candidate_commit):
    """Consume complete role-bound byte proof, never an accepted flag alone."""
    from fts_plan_compatibility import SCHEME, CASE, SPECS, TEST_SHA256, GOLDEN_SHA256, GOLDEN, TEST
    if not isinstance(proof, dict) or not isinstance(row, dict):
        return False
    context = row.get('validationContext')
    if (not isinstance(context, dict) or proof.get('validationContext') != context
            or context.get('sourceCommit') != candidate_commit
            or context.get('kernelOrigin') != PRODUCTION_KERNEL
            or candidate_commit != validation_driver or candidate_commit in OLD_KERNELS
            or not re.fullmatch('[0-9a-f]{40}', str(context.get('validationDriverCommit', '')))
            or (validation_driver is not None and context['validationDriverCommit'] != validation_driver)
            or not isinstance(context.get('source'), str) or not Path(context['source']).is_absolute()
            or not isinstance(context.get('out'), str) or not Path(context['out']).is_absolute()):
        return False
    source, out = Path(context['source']), Path(context['out'])
    result = context.get('nativeResult')
    if (not isinstance(result, dict) or result.get('path') != str(out / 'result.json')
            or not re.fullmatch('[0-9a-f]{64}', str(result.get('sha256', '')))
            or type(result.get('bytes')) is not int or not 0 < result['bytes'] <= 2 * 1024 ** 2):
        return False
    evidence_paths = dict(originalGolden=source / GOLDEN, originalTest=source / TEST,
        actualReject=out / 'mysqltest-log/simple_query.reject',
        nativeOutput=out / 'mysqltest-output.log', nativeWrapper=out / 'original-run-case.log')
    if any(not isinstance(proof.get(key), dict) or proof[key].get('path') != str(path)
           for key, path in evidence_paths.items()):
        return False
    work = row.get('work') if isinstance(row.get('work'), dict) else {}
    if (row.get('role') != 'candidate' or row.get('name') != CASE
            or row.get('status') != 'failed' or row.get('cleanupPassed') is not True
            or work.get('passed') is not False or type(work.get('exitCode')) is not int
            or work['exitCode'] != 1 or type(work.get('invocations')) is not int
            or work['invocations'] != 1 or work.get('inputsUnchanged') is not True
            or work.get('testSha256') != TEST_SHA256 or work.get('expectedSha256') != GOLDEN_SHA256):
        return False
    for record in (row, work):
        if any(record.get(key) for key in ('error', 'traceback', 'timedOut', 'signal', 'cleanupError', 'failureKind', 'failureReason')):
            return False
    if (proof.get('scheme') != SCHEME or proof.get('role') != 'candidate'
            or proof.get('caseName') != CASE or proof.get('rawNativeStatus') != 'failed'
            or proof.get('nativePassed') is not False or proof.get('formalContestPass') is not False
            or type(proof.get('approvedDifferenceCount')) is not int or proof['approvedDifferenceCount'] != 4
            or any(proof.get(key) is not True for key in ('accepted', 'completeBytesCompared', 'eofCompared', 'inputsUnchanged', 'ownedCleanupPassed'))
            or proof.get('adjacentScoredControl') != dict(annotationLine=1603, calcRelevance=True)):
        return False
    changes = [dict(sqlStartLine=start, annotationLine=line, scanId=scan,
                    statement=sql.decode('utf-8').rstrip('\n'), purpose=purpose,
                    expectedCalcRelevance=True, actualCalcRelevance=False)
               for start, line, scan, purpose, sql in SPECS]
    if proof.get('changes') != changes:
        return False
    for name, sha, count, lines in (('originalGolden', GOLDEN_SHA256, 184285, 2833),
            ('actualReject', '737c4257fabf57dbd5365fdd04cfe2573206129d19991a8a1616e7e3db1e4896', 184289, 2833),
            ('originalTest', TEST_SHA256, None, None), ('nativeOutput', None, None, None), ('nativeWrapper', None, None, None)):
        info = proof.get(name)
        if (not isinstance(info, dict) or not isinstance(info.get('path'), str)
                or not Path(info['path']).is_absolute()
                or not re.fullmatch('[0-9a-f]{64}', str(info.get('sha256', '')))
                or type(info.get('bytes')) is not int or not 0 < info['bytes'] <= 1024 ** 2
                or type(info.get('lines')) is not int or info['lines'] <= 0
                or info.get('endsWithNewline') is not True
                or (sha is not None and info['sha256'] != sha)
                or (count is not None and info['bytes'] != count)
                or (lines is not None and info['lines'] != lines)):
            return False
    try:
        raw_path = out / 'result.json'
        no_symlinks(raw_path)
        info = raw_path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 2 * 1024 ** 2:
            return False
        raw = json.loads(raw_path.read_text(encoding='utf-8'))
        if info.st_size != result['bytes'] or digest(raw_path) != result['sha256']:
            return False
        if any(row.get(key) != raw.get(key) for key in ('name', 'status', 'cleanupPassed', 'work')):
            return False
        rebuilt = classify_plan_metadata_compatibility(source=source, out=out, role='candidate',
            case_name=CASES[-1], row=raw)
        return rebuilt.get('accepted') is True and dict(rebuilt, validationContext=context) == proof
    except (OSError, ValueError, TypeError):
        return False

def validate(args, plan, report):
    harness = load_module('paired_original_harness', args.root / 'driver/fts_contest_validation.py')
    scorer = load_module('paired_official_scorer', args.source / (BENCH + '_score.py'))
    baseline = scorer.load_baseline(args.source / (BENCH + '_baseline.json'))
    verify_driver(args.source, args.driver_sha, args.kernel_sha)
    original_binary, original_identity = historical_baseline(args, plan)
    report.update(mode=MODE, validationDriverCommit=args.driver_sha,
        candidateBuildCommit=args.driver_sha, kernelOrigin=args.kernel_sha,
        historicalCandidateExecuted=False, baselineRebuilt=False,
        artifactReuse=dict(artifact=plan['artifact'], verificationSha256=plan['runtimeVerificationSha256'],
            oldValidationAccepted=False, historicalCollectorPassed=False,
            originalManifestOk=False, historicalCandidateExecuted=False))
    roles = {}
    pinned = {}
    for role in ('baseline', 'candidate'):
        source = Path(plan[role + 'Source'])
        commit = BASELINE if role == 'baseline' else args.driver_sha
        identity = original_identity if role == 'baseline' else json.loads(
            (args.root / 'runtime/candidate/identity.json').read_text(encoding='utf-8'))
        source_id = source_identity(source, commit)
        if source_id['tree'] != identity['sourceTree']:
            raise RuntimeError('Saved source tree differs from exact role snapshot')
        binary = original_binary if role == 'baseline' else candidate_identity(identity, args, plan)
        provenance = None
        role_args = argparse.Namespace(role=role, source=source, expected_sha=commit, binary=binary,
            workspace=args.source, obclient=args.source / 'deps/3rd/u01/obclient/bin/obclient',
            mysqltest=args.source / 'deps/3rd/u01/obclient/bin/mysqltest')
        env = runtime_env(args.source, binary)
        verify_loader(binary, args.root / 'evidence' / role / 'runtime-ldd.log', source, env)
        for client, expected in CLIENTS.items():
            path = getattr(role_args, client)
            if digest(path) != expected:
                raise RuntimeError('Official native client changed')
            verify_loader(path, args.root / 'evidence' / role / (client + '-runtime-ldd.log'), source, env)
        pinned[role] = harness.original_inputs(source)
        if pinned[role] != identity['officialInputHashes']:
            raise RuntimeError('Official input hashes differ from build-time identity')
        roles[role] = (role_args, harness.load_native(source), identity, provenance)
        report['builds'][role] = {key: identity[key] for key in ('sourceCommit', 'sourceTree', 'kernelOrigin', 'driverCommit', 'sha256', 'bytes', 'buildId')}
    persist_report(args.root, report)

    def guard(role):
        call_args, _, identity, provenance = roles[role]
        source_identity(call_args.source, call_args.expected_sha)
        source_identity(args.source, args.driver_sha)
        historical_baseline(args, plan)
        if role == 'candidate':
            candidate_identity(identity, args, plan)
        if case_input_hashes(args.source) != plan['equivalence']['caseInputs']:
            raise RuntimeError('Extra diagnostic inputs changed during validation')
        for client, expected in CLIENTS.items():
            if digest(getattr(call_args, client)) != expected:
                raise RuntimeError('Official native client changed during validation')
        if harness.original_inputs(call_args.source) != pinned[role]:
            raise RuntimeError('Official inputs changed during validation')

    def run(role, name, work, cases=False, untimed=False):
        guard(role)
        call_args, native, _, _ = roles[role]
        call_args.out = args.root / 'validation' / ('cases/' + role if cases else 'untimed' if untimed else 'rounds')
        env = runtime_env(args.source, call_args.binary)
        old_library, old_path = os.environ.get('LD_LIBRARY_PATH'), os.environ.get('PATH')
        os.environ.update(LD_LIBRARY_PATH=env['LD_LIBRARY_PATH'], PATH=env['PATH'])
        def checked_work(call, out):
            observed = effective_parameters(out / 'actual-parameters.log')
            stage = 'cases' if cases else 'untimed' if untimed else 'rounds'
            reference = report.setdefault('observedParameters', {}).get(stage)
            if reference is not None and reference != observed:
                raise RuntimeError('Observed effective settings differ across roles/instances')
            report['observedParameters'][stage] = observed
            sdb = load_module('paired_owned_sdb', call_args.source / PREFIX / 'sdb.py')
            pid = sdb.inspect_instance_process(call.base_dir, Path(call_args.binary))
            executable = Path('/proc') / str(pid) / 'exe'
            if not pid or executable.resolve(strict=True) != call_args.binary:
                raise RuntimeError('Owned live process does not execute the exact role ELF')
            write_json(out / 'owned-process.json', dict(pid=pid, binary=str(call_args.binary),
                sourceCommit=call_args.expected_sha, sha256=digest(call_args.binary), observedParameters=observed))
            guard(role)
            return work(call, out)
        try:
            # Avoid TIME_WAIT false failures in the helper's strict bind probe.
            # Port identity does not change SQL/configuration or scoring.
            port = 7300 + len(report['instances'])
            row = harness.run_instance(call_args, native, name, port, checked_work, initialize=cases)
        finally:
            for key, old in (('LD_LIBRARY_PATH', old_library), ('PATH', old_path)):
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old
        report['instances'].append(dict(role=role, **row))
        verdict = dict(name=name, role=role, status=row.get('status'), cleanupPassed=row.get('cleanupPassed'))
        if cases or untimed:
            verdict['work'] = row.get('work', {})
            for key in ('error', 'traceback', 'cleanupError', 'timedOut', 'signal', 'failureKind', 'failureReason'):
                if key in row:
                    verdict[key] = row[key]
        report.setdefault('caseVerdicts' if cases else 'untimedVerdicts' if untimed else 'roundVerdicts', []).append(verdict)
        proof = None
        if cases and role == 'candidate' and name == CASES[-1] and row.get('status') == 'failed':
            from fts_plan_compatibility import classify_plan_metadata_compatibility
            proof = classify_plan_metadata_compatibility(source=call_args.source, out=call_args.out / name,
                role=role, case_name=name, row=row)
            out = call_args.out / name
            native_result = out / 'result.json'
            no_symlinks(native_result)
            if (not native_result.is_file() or native_result.stat().st_nlink != 1
                    or native_result.stat().st_size > 2 * 1024 ** 2
                    or json.loads(native_result.read_text(encoding='utf-8'))
                        != json.loads(json.dumps(row, ensure_ascii=False, allow_nan=False))):
                raise RuntimeError('Raw native result differs from returned unmodified row')
            context = dict(sourceCommit=call_args.expected_sha, validationDriverCommit=args.driver_sha, kernelOrigin=args.kernel_sha,
                source=str(call_args.source), out=str(out), nativeResult=dict(path=str(native_result),
                    bytes=native_result.stat().st_size, sha256=digest(native_result)))
            proof = dict(proof, validationContext=context)
            verdict['validationContext'] = context
            write_json(out / 'supplemental-plan-compatibility.json', proof)
            report.setdefault('supplementalCompatibility', {})[role + '/' + name] = proof
        if not cases and 'metrics' in row.get('work', {}):
            report['roundMetrics'][name] = row['work']['metrics']
        if not cases and 'explain' in row.get('work', {}):
            report.setdefault('planAssertions', {})[name] = dict(role=role, queries=row['work']['explain'])
        persist_report(args.root, report)
        if not (proof is not None and accepted_compatibility(proof, verdict, args.driver_sha, args.driver_sha)):
            require_instance(row)
        guard(role)
        return row

    for role in ('baseline', 'candidate'):
        call_args, native, _, _ = roles[role]
        available = {case.name: case for case in native.discover_cases(call_args.source)}
        selected = selected_cases(role)
        if any(name not in available for name in selected):
            raise RuntimeError('Required native case unavailable/excluded')
        for name in selected:
            # Both existing diagnostics and new IK/Beng cases are actual new-B
            # source inputs. The corrected wide SQL is not substituted at runtime.
            work = harness.original_case(call_args, native, available[name])
            run(role, name, work, cases=True)
    if not case_evidence_complete(report['caseVerdicts'], report.get('supplementalCompatibility'), args.driver_sha, plan['equivalence']['caseInputs']):
        raise RuntimeError('Complete native cases and strict supplemental proof required')
    for role in ('baseline', 'candidate'):
        run(role, 'tokenize-' + role, tokenize_work(roles[role][0]), untimed=True)
    if not tokenize_complete(report.get('untimedVerdicts'), args.driver_sha, args.root):
        raise RuntimeError('Independent untimed A/B TOKENIZE frequency/doc_len equivalence failed')
    time.sleep(30)
    for name, role in SCHEDULE:
        call_args = roles[role][0]
        run(role, name, benchmark_work(call_args, baseline, scorer, name in ('A4', 'B4')))
    if not accepted_plan_assertions(report.get('planAssertions')):
        raise RuntimeError('Complete role-bound A4/B4 actual-plan assertions are required')
    pairs = [dict(baseline=a, candidate=b, baselineMetrics=report['roundMetrics'][a],
                  candidateMetrics=report['roundMetrics'][b]) for a, b in PAIRS]
    report['sameHostPerformance'] = performance(pairs, baseline, scorer)
    for role in roles:
        guard(role)
    report.update(status='valid_calibration_completed', allOfficialInputsUnchanged=True)
    persist_report(args.root, report)

def case_evidence_complete(rows, proofs, driver, inputs):
    expected = {(role, name) for role in ('baseline', 'candidate') for name in selected_cases(role)}
    if (not isinstance(rows, list) or len(rows) != len(expected)
            or any(not isinstance(row, dict) for row in rows)
            or {(row.get('role'), row.get('name')) for row in rows} != expected
            or not isinstance(proofs, dict) or set(proofs) != {'candidate/' + CASES[-1]}):
        return False
    for row in rows:
        work = row.get('work', {})
        if not isinstance(work, dict) or any(work.get(key) != value for key, value in inputs[row['name']].items()):
            return False
        if row['role'] == 'candidate' and row['name'] == CASES[-1]:
            if not accepted_compatibility(proofs['candidate/' + CASES[-1]], row, driver, driver):
                return False
        elif (row.get('status') != 'passed' or row.get('cleanupPassed') is not True
                or work.get('passed') is not True or type(work.get('exitCode')) is not int
                or work['exitCode'] != 0 or type(work.get('invocations')) is not int
                or work['invocations'] != 1 or work.get('inputsUnchanged') is not True
                or any(record.get(key) for record in (row, work) for key in
                    ('error', 'traceback', 'timedOut', 'signal', 'cleanupError', 'failureKind', 'failureReason'))):
            return False
    return True


def persist_report(root, report):
    write_json(root / 'report.json', report)
    print('HYBRID_REPORT ' + json.dumps({key: value for key, value in report.items()
        if key not in ('instances', 'roundMetrics')}, ensure_ascii=False, allow_nan=False), flush=True)


def completed(report, args, plan):
    if not isinstance(report, dict):
        return False
    if (report.get('status') != 'valid_calibration_completed' or report.get('failure')
            or report.get('mode') != MODE or report.get('driverCommit') != args.driver_sha
            or report.get('candidateBuildCommit') != args.driver_sha
            or report.get('validationDriverCommit') != args.driver_sha
            or report.get('kernelOrigin') != args.kernel_sha
            or report.get('baselineCommit') != BASELINE
            or report.get('allOfficialInputsUnchanged') is not True
            or report.get('formalContestRun') is not False
            or report.get('historicalCandidateExecuted') is not False
            or report.get('baselineRebuilt') is not False
            or not case_evidence_complete(report.get('caseVerdicts'), report.get('supplementalCompatibility'),
                args.driver_sha, plan['equivalence']['caseInputs'])
            or not accepted_plan_assertions(report.get('planAssertions'))
            or not tokenize_complete(report.get('untimedVerdicts'), args.driver_sha, args.root)):
        return False
    rounds = report.get('roundVerdicts', [])
    if (len(rounds) != len(SCHEDULE) or [(r.get('name'), r.get('role')) for r in rounds] != list(SCHEDULE)
            or any(r.get('status') != 'passed' or r.get('cleanupPassed') is not True for r in rounds)):
        return False
    builds = report.get('builds', {})
    if (builds.get('baseline', {}).get('sourceCommit') != BASELINE
            or builds.get('baseline', {}).get('driverCommit') != legacy.CANDIDATE
            or builds.get('candidate', {}).get('sourceCommit') != args.driver_sha
            or builds.get('candidate', {}).get('kernelOrigin') != args.kernel_sha
            or builds.get('candidate', {}).get('driverCommit') != args.driver_sha):
        return False
    try:
        scorer = load_module('hybrid_final_scorer', args.source / (BENCH + '_score.py'))
        base = scorer.load_baseline(args.source / (BENCH + '_baseline.json'))
        pairs = [dict(baseline=a, candidate=b, baselineMetrics=report['roundMetrics'][a],
            candidateMetrics=report['roundMetrics'][b]) for a, b in PAIRS]
        return performance(pairs, base, scorer) == report['sameHostPerformance']
    except (KeyError, TypeError, ValueError, RuntimeError):
        return False


def present(report, args, plan=None):
    valid = plan is not None and completed(report, args, plan)
    perf = report.get('sameHostPerformance', {})
    lines = ['Status: ' + str(report.get('status', 'unavailable')),
        'Source-hybrid development calibration: ' + (str(perf.get('classification')) if valid else 'invalid or incomplete; no improvement claim'),
        'Original A source: ' + BASELINE + '; original build driver: ' + legacy.CANDIDATE + '; not rebuilt.',
        'New B actual source/driver: ' + args.driver_sha + '; kernel origin: ' + args.kernel_sha + '.',
        'Historical old B: verified static evidence only; never executed or relabeled.',
        'Historical validation was not accepted; original manifest ok=false collector failure remains recorded.',
        'Candidate simple_query remains raw native FAIL. Only exact full-byte four-flag proof can be accepted separately.',
        'Expected native coverage: original 5 per role; current 2 diagnostics plus the new Beng case on B; separate IK/Beng A/B TOKENIZE comparison.',
        'Expected timing: 8 fresh same-host rounds, original 75 inputs/SQL/config, 14 actual EXPLAIN assertions.',
        'No formal contest result, no performance improvement inferred from frozen different-host scores.']
    if perf:
        lines.append('All paired results and category/metric improvements: ' + json.dumps(perf, ensure_ascii=True, allow_nan=False))
    if report.get('failure'):
        lines.append('Failure: ' + json.dumps(report['failure'], ensure_ascii=True))
    for line in lines:
        # Bounded visible lines; every fragment is retained in summary and logs.
        escaped = json.dumps(line, ensure_ascii=True)[1:-1]
        for offset in range(0, len(escaped), 440):
            print('FTS_HYBRID_RESULT ' + escaped[offset:offset + 440], flush=True)
    if args.summary:
        with args.summary.open('a', encoding='utf-8') as stream:
            stream.write('\n## FTS original-A/new-B calibration\n\n')
            stream.writelines('- ' + line + '\n' for line in lines)


def collect_evidence(root, out):
    """Only explicit bounded UTF-8 evidence, never ELF/DB/env or raw DB tails."""
    if out is None:
        raise RuntimeError('Evidence output is required')
    no_symlinks(root)
    no_symlinks(out)
    if not root.is_dir() or out.exists() or root == out or root in out.parents or out in root.parents:
        raise RuntimeError('Evidence output must be fresh and disjoint')
    selected = []
    def select(path, cap=2 * 1024 ** 2):
        no_symlinks(path)
        if not path.exists():
            return
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > cap
                or len(selected) >= 640):
            raise RuntimeError('Evidence type/link/size/count violation: ' + str(path))
        raw = path.read_bytes()
        if b'\0' in raw or raw.startswith(b'\x7fELF'):
            raise RuntimeError('Binary evidence is forbidden: ' + str(path))
        raw.decode('utf-8', errors='strict')
        if path.suffix == '.json':
            json.loads(raw)
        selected.append((path, info, digest(path)))
    for name in ('plan.json', 'report.json', 'runtime/candidate/identity.json',
                 'historical/runtime/verification.json', 'historical/runtime/verification.failed.json',
                 'historical/runtime/manifest.original.json', 'historical/runtime/plan.original.json'):
        select(root / name, 8 * 1024 ** 2)
    for role in ('baseline', 'candidate'):
        for name in ('identity.original.json', 'relocation.json', 'readelf-notes.log'):
            select(root / 'historical/runtime' / role / name, 8 * 1024 ** 2)
        for name in ('runtime-ldd.log', 'runtime-ldd.log.json', 'prebuild-ldd.log', 'prebuild-ldd.log.json',
                     'elf-notes.log', 'elf-notes.log.json', 'seekdb-ldd.log', 'seekdb-ldd.log.json',
                     'obclient-ldd.log', 'obclient-ldd.log.json', 'mysqltest-ldd.log', 'mysqltest-ldd.log.json',
                     'prebuild.json', 'resources-before.txt', 'resources-after.txt',
                     'obclient-prebuild-ldd.log', 'obclient-prebuild-ldd.log.json',
                     'mysqltest-prebuild-ldd.log', 'mysqltest-prebuild-ldd.log.json',
                     'obclient-runtime-ldd.log', 'obclient-runtime-ldd.log.json',
                     'mysqltest-runtime-ldd.log', 'mysqltest-runtime-ldd.log.json'):
            select(root / 'evidence' / role / name)
    # Expensive build output is tailed at production time, not silently truncated here.
    for name in ('initialize-tail.log', 'initialize-tail.json', 'build-tail.log', 'build-tail.json'):
        select(root / 'evidence/candidate' / name, 1024 ** 2)
    instances = [root / 'validation/cases' / role / name for role in ('baseline', 'candidate')
                 for name in selected_cases(role)] + [root / 'validation/rounds' / name for name, _ in SCHEDULE] + [root / 'validation/untimed' / ('tokenize-' + role) for role in ('baseline', 'candidate')]
    names = {'result.json', 'owned-process.json', 'start.log', 'ready.log', 'original-init.log',
             'actual-parameters.log', 'own-stop.log', 'own-destroy.log', 'mysqltest-output.log',
             'original-run-case.log', 'original-result-diff.log', 'supplemental-plan-compatibility.json',
             'benchmark.log', 'benchmark.log.json', 'report.txt', 'frozen-score.json',
             'frozen-score.json.json', 'explain-result.json', 'tokenize.sql', 'tokenize.log', 'tokenize.log.json', 'tokenize-canonical.json'}
    names.update(name + suffix for name, _, _, _ in tokenize_preflight()
                 for suffix in ('.sql', '.log', '.log.json'))
    names.update(name + suffix for name in QUERY_NAMES + PLAN_CONTROLS
                 for suffix in ('-explain.sql', '-explain.log', '-explain.log.json'))
    for directory in instances:
        no_symlinks(directory)
        for name in sorted(names):
            select(directory / name)
        logs = directory / 'mysqltest-log'
        no_symlinks(logs)
        if logs.exists():
            entries = list(logs.iterdir())
            if len(entries) > 16:
                raise RuntimeError('Native log directory entry cap exceeded')
            for path in sorted(entries):
                if path.name != 'timer' and not re.fullmatch(r'[A-Za-z0-9_.-]+\.(?:reject|log)', path.name):
                    raise RuntimeError('Unexpected native evidence filename: ' + path.name)
                select(path)
    total = sum(info.st_size for _, info, _ in selected)
    if total > 64 * 1024 ** 2:
        raise RuntimeError('Evidence total cap exceeded')
    out.mkdir(parents=True)
    records = []
    for path, before, expected in selected:
        target = out / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        after = path.stat()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or digest(path) != expected or digest(target) != expected):
            raise RuntimeError('Evidence changed while copying')
        records.append(dict(path=str(path.relative_to(root)), bytes=before.st_size, sha256=expected))
    manifest = dict(schema=1, kind=MODE, files=records, bytes=total,
        unchangedElfsReuploaded=False, historicalCollectorPassed=False, historicalValidationAccepted=False,
        historicalFailure='Original manifest ok=false: baseline/cn_word non-text DB log collector failure.',
        excludes=['ELF', 'database', 'core', 'environment', 'db-log-tails'],
        maxFileBytes=8 * 1024 ** 2, maxTotalBytes=64 * 1024 ** 2, maxEntries=640)
    write_json(out / 'source-hybrid-evidence-manifest.json', manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('plan', 'prepare-reuse', 'prebuild', 'snapshot',
        'validate', 'finalize', 'present', 'collect-evidence', 'preserve-candidate'), default='plan')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--driver-sha', required=True)
    parser.add_argument('--kernel-sha', required=True)
    parser.add_argument('--role', choices=('candidate',))
    parser.add_argument('--summary', type=Path)
    parser.add_argument('--artifact-zip', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    no_symlinks(args.source)
    no_symlinks(args.root)
    args.source = args.source.resolve(strict=True)
    args.root = args.root.resolve(strict=False)
    if (not args.execute and args.phase != 'collect-evidence') or args.phase == 'plan':
        print(json.dumps(plan_for(args.source, args.root, args.driver_sha, args.kernel_sha), indent=2))
        return 0
    if sys.platform != 'linux':
        raise RuntimeError('Actual source-hybrid execution requires Linux')
    install_termination_handlers()
    if args.phase in ('collect-evidence', 'preserve-candidate'):
        try:
            result = (collect_evidence(args.root, args.out) if args.phase == 'collect-evidence'
                      else preserve_candidate(args, load_plan(args)))
            print('HYBRID_ARTIFACT_RESULT ' + json.dumps(dict(phase=args.phase, ok=True, result=result)))
            return 0
        except Exception as error:
            # Artifact outcomes never rewrite native/timing validity or historical metadata.
            print('HYBRID_ARTIFACT_RESULT ' + json.dumps(dict(phase=args.phase, ok=False, reason=str(error))))
            return 1
    report = dict(scope='SAME_HOST_DEVELOPMENT_CALIBRATION_NOT_FORMAL_CONTEST', mode=MODE,
        status='running', driverCommit=args.driver_sha, kernelOrigin=args.kernel_sha,
        baselineCommit=BASELINE, phase=args.phase, formalContestRun=False, builds={}, instances=[],
        caseVerdicts=[], roundVerdicts=[], roundMetrics={})
    try:
        if args.phase == 'prepare-reuse':
            prepare(args)
        else:
            if (args.root / 'report.json').is_file():
                report = json.loads((args.root / 'report.json').read_text(encoding='utf-8'))
                report['phase'] = args.phase
            plan = load_plan(args)
            if args.phase == 'prebuild':
                prebuild(args, plan)
            elif args.phase == 'snapshot':
                record = snapshot(args, plan)
                report['builds']['candidate'] = {key: record[key] for key in
                    ('sourceCommit', 'sourceTree', 'kernelOrigin', 'driverCommit', 'sha256', 'bytes', 'buildId')}
            elif args.phase == 'present':
                present(report, args, plan)
                return 0
            elif args.phase == 'finalize':
                if not completed(report, args, plan):
                    report.update(status='invalid_calibration', failure=report.get('failure', dict(
                        phase=report.get('phase'), reason='Workflow ended without complete valid source-hybrid evidence')))
                persist_report(args.root, report)
                return 0 if report['status'] == 'valid_calibration_completed' else 1
            else:
                validate(args, plan, report)
        if args.phase != 'validate':
            report['status'] = 'phase_completed'
            persist_report(args.root, report)
        return 0
    except Exception as error:
        report.update(status='invalid_calibration', failure=dict(phase=args.phase,
            reason=str(error), traceback=traceback.format_exc()))
        if args.root.is_dir() and (args.root / 'plan.json').is_file():
            persist_report(args.root, report)
        if args.phase == 'present':
            present(report, args)
        return 1


TOKENIZE_CORPUS = (
    ('ascii', 'Hello WORLD alpha-beta foo_bar'),
    ('multibyte', '数据库 中文 café Straße Ελληνικά 🙂 𠀀'),
    ('numbers_connectors', 'abc123 123abc 3.14 1,000 x+y email@example.test'),
    ('fullwidth', 'ＡＢＣ１２３　数据库，全文。'),
    ('repeated', 'Repeat repeat repeat 数据库 数据库'),
    ('empty', ''), ('null', None),
    ('long', ('Generic multilingual 中文 ABC123 alpha-beta. ' * 512)))


# CAST changes the expression type/collation, not its SQL NULL datum. Bare NULL
# has binary collation; IK loads its charset-specific dictionary even for NULL.
TOKENIZE_UTF8_NULL = 'CAST(NULL AS CHAR CHARACTER SET utf8mb4)'


def tokenize_preflight():
    """Separate real invocations: prove NULL/type, then exact expected errors.

    A negative is never mixed into the 16 successful ALL-mode corpus queries.
    Source: ob_expr_tokenize.cpp parse_fulltext/try_load_dictionary_for_ik and
    ob_gen_dic_loader.cpp gen_dic_loader (only CHARSET_UTF8MB4 supports IK).
    latin1 instead fails the earlier SQL charset lookup: the compiled registry
    accepts binary/utf8mb4 (plus UTF8 aliases), not every ObCharsetType enum.
    Exact latin1 bytes were observed on A in Actions run 42583955784.
    """
    sql = legacy.SESSION + (
        "SELECT 'raw_null', NULL IS NULL, CHARSET(NULL);\n"
        "SELECT 'typed_null', %s IS NULL, CHARSET(%s);\n"
        "SELECT 'utf8_empty', CONVERT(X'' USING utf8mb4) IS NULL, "
        "CHARSET(CONVERT(X'' USING utf8mb4));\n") % (TOKENIZE_UTF8_NULL, TOKENIZE_UTF8_NULL)
    result = [('tokenize-input', sql, 0,
               b'raw_null\t1\tbinary\ntyped_null\t1\tutf8mb4\nutf8_empty\t0\tutf8mb4\n')]
    for name, expression, charset in (
            ('raw-null', 'NULL', 'binary'),
            ('binary', "CONVERT(X'616263' USING binary)", 'binary'),
            ('latin1', "CONVERT(X'616263' USING latin1)", 'latin1')):
        sql = legacy.SESSION + "SELECT TOKENIZE(%s, 'ik', '[{\"output\": \"all\"}]');\n" % expression
        if charset == 'latin1':
            error = b"ERROR 1115 (42000) at line 2: Unknown character set: 'latin1'\n"
        else:
            error = ('ERROR 1235 (0A000) at line 2: ik with the %s charset is not supported\n' % charset).encode('ascii')
        result.append(('tokenize-negative-' + name, sql, 1, error))
    return tuple(result)


def tokenize_sql():
    statements = [legacy.SESSION]
    for parser in ('ik', 'beng'):
        for name, value in TOKENIZE_CORPUS:
            literal = TOKENIZE_UTF8_NULL if value is None else "CONVERT(X'%s' USING utf8mb4)" % value.encode('utf-8').hex()
            statements.append("SELECT '%s/%s', TOKENIZE(%s, '%s', '[{\"output\": \"all\"}]');\n" %
                              (parser, name, literal, parser))
    return ''.join(statements)


def canonical_tokenize(raw):
    """Canonicalize only the documented unordered token array, preserving freq/doc_len."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key in TOKENIZE output')
            result[key] = value
        return result
    if not raw.endswith(b'\n') or b'\r' in raw or b'\0' in raw:
        raise ValueError('TOKENIZE output must be complete LF-delimited UTF-8 rows')
    rows = {}
    for line in raw.decode('utf-8', errors='strict')[:-1].split('\n'):
        name, data = line.split('\t')
        if name in rows:
            raise ValueError('Duplicate TOKENIZE case')
        value = json.loads(data, object_pairs_hook=pairs)
        if (not isinstance(value, dict) or set(value) != {'tokens', 'doc_len'}
                or type(value['doc_len']) is not int or value['doc_len'] < 0
                or not isinstance(value['tokens'], list)):
            raise ValueError('Unexpected ALL-mode TOKENIZE output shape')
        tokens = {}
        for item in value['tokens']:
            if not isinstance(item, dict) or len(item) != 1:
                raise ValueError('Unexpected TOKENIZE frequency record')
            token, frequency = next(iter(item.items()))
            if not isinstance(token, str) or not token or type(frequency) is not int or frequency <= 0 or token in tokens:
                raise ValueError('Ambiguous TOKENIZE token/frequency')
            tokens[token] = frequency
        value = dict(tokens=dict(sorted(tokens.items())), doc_len=value['doc_len'])
        if name.rsplit('/', 1)[-1] in ('empty', 'null') and value != dict(tokens={}, doc_len=0):
            raise ValueError('Empty and SQL NULL inputs require empty ALL-mode details')
        rows[name] = value
    expected = [parser + '/' + name for parser in ('ik', 'beng') for name, _ in TOKENIZE_CORPUS]
    if list(rows) != expected:
        raise ValueError('Missing, unknown or out-of-order TOKENIZE case')
    return dict(sorted(rows.items()))


def tokenize_invocation_ok(result, expected_exit):
    return (isinstance(result, dict) and type(result.get('exitCode')) is int and result['exitCode'] == expected_exit
            and result.get('childrenCleaned') is True and not result.get('timedOut'))


def tokenize_work(args):
    def execute(call, out):
        def invoke(name, sql):
            (out / (name + '.sql')).write_text(sql, encoding='utf-8')
            return capture_group([str(args.obclient), '-h127.0.0.1', '-P' + str(call.port),
                '-uroot', '-A', '-N', '-B', '--raw', '--default-character-set=utf8mb4'],
                out / (name + '.log'), args.source, 120, env=runtime_env(args.workspace, args.binary),
                stdin=sql.encode('utf-8'))
        preflight = {}
        for name, sql, expected_exit, expected_output in tokenize_preflight():
            result = invoke(name, sql)
            passed = (tokenize_invocation_ok(result, expected_exit)
                      and (out / (name + '.log')).stat().st_size == len(expected_output)
                      and (out / (name + '.log')).read_bytes() == expected_output)
            preflight[name] = dict(passed=passed, invocation=result,
                sqlSha256=digest(out / (name + '.sql')))
            if not passed:
                return dict(passed=False, preflight=preflight)
        sql = tokenize_sql()
        result = invoke('tokenize', sql)
        if not tokenize_invocation_ok(result, 0):
            return dict(passed=False, invocation=result, preflight=preflight)
        data = canonical_tokenize((out / 'tokenize.log').read_bytes())
        write_json(out / 'tokenize-canonical.json', data)
        return dict(passed=True, invocation=result, preflight=preflight, canonical=data,
            sqlSha256=digest(out / 'tokenize.sql'), canonicalSha256=digest(out / 'tokenize-canonical.json'),
            kind='independent_untimed_generic_corpus_not_official', sourceCommit=args.expected_sha)
    return execute


def tokenize_complete(rows, driver, root):
    if (not isinstance(rows, list) or len(rows) != 2
            or any(not isinstance(r, dict) or not isinstance(r.get('work'), dict) for r in rows)
            or [(r.get('role'), r.get('name')) for r in rows] !=
               [('baseline', 'tokenize-baseline'), ('candidate', 'tokenize-candidate')]
            or any(r.get('status') != 'passed' or r.get('cleanupPassed') is not True
                   or r.get('work', {}).get('passed') is not True for r in rows)):
        return False
    works = [row['work'] for row in rows]
    def checked_output(directory, name, sql, invocation, expected_exit):
        path = directory / (name + '.log')
        sql_path = directory / (name + '.sql')
        for target, cap in ((path, 2 * 1024 ** 2), (sql_path, 1024 ** 2)):
            no_symlinks(target)
            info = target.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > cap:
                raise ValueError('Unsafe or oversized TOKENIZE evidence')
        if (not isinstance(invocation, dict) or sql_path.read_bytes() != sql.encode('utf-8')
                or invocation.get('output') != str(path)
                or not tokenize_invocation_ok(invocation, expected_exit)
                or invocation.get('outputSha256') != digest(path)):
            raise ValueError('TOKENIZE SQL/invocation/output proof mismatch')
        return path.read_bytes()
    sql_sha = hashlib.sha256(tokenize_sql().encode('utf-8')).hexdigest()
    try:
        for row, work in zip(rows, works):
            role = row['role']
            directory = root / 'validation/untimed' / ('tokenize-' + role)
            raw = checked_output(directory, 'tokenize', tokenize_sql(), work.get('invocation', {}), 0)
            probes = work.get('preflight', {})
            if not isinstance(probes, dict) or set(probes) != {spec[0] for spec in tokenize_preflight()}:
                return False
            for name, sql, expected_exit, expected_output in tokenize_preflight():
                proof = probes[name]
                if not isinstance(proof, dict):
                    return False
                output = checked_output(directory, name, sql, proof.get('invocation', {}), expected_exit)
                if (proof.get('passed') is not True or output != expected_output
                        or proof.get('sqlSha256') != hashlib.sha256(sql.encode('utf-8')).hexdigest()):
                    return False
            if (work.get('sourceCommit') != (BASELINE if role == 'baseline' else driver)
                    or work.get('kind') != 'independent_untimed_generic_corpus_not_official'
                    or work.get('sqlSha256') != sql_sha
                    or work.get('canonical') != canonical_tokenize(raw)):
                return False
            serialized = (json.dumps(work['canonical'], indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
            if work.get('canonicalSha256') != hashlib.sha256(serialized).hexdigest():
                return False
        return bool(works[0].get('canonical')) and works[0]['canonical'] == works[1]['canonical']
    except (OSError, ValueError, TypeError, RuntimeError):
        return False



def preserve_candidate(args, plan):
    """Independent complete-B archive, also available when native validation fails."""
    import tarfile
    from fts_paired_collect import archive, BINARY_ARCHIVE_CAP, BINARY_RAW_CAP, regular
    identity_path = args.root / 'runtime/candidate/identity.json'
    identity = json.loads(identity_path.read_text(encoding='utf-8'))
    binary = candidate_identity(identity, args, plan)
    out = args.out
    if out is None:
        raise RuntimeError('Candidate archive output is required')
    no_symlinks(out)
    if out.exists() or out == args.root or out in args.root.parents or args.root in out.parents:
        raise RuntimeError('Candidate archive output must be fresh and disjoint')
    before = regular(binary)
    if before.st_size > BINARY_RAW_CAP:
        raise RuntimeError('Full candidate exceeds raw ELF cap')
    out.mkdir(parents=True)
    def populate(tar):
        entry = tarfile.TarInfo('candidate/seekdb')
        entry.size, entry.mode = before.st_size, 0o555
        with binary.open('rb') as stream:
            tar.addfile(entry, stream)
        after = regular(binary)
        if (before.st_size != after.st_size or digest(binary) != identity['sha256']):
            raise RuntimeError('Candidate ELF changed during complete archive')
    record = archive(out / 'candidate-elf.tar.gz', BINARY_ARCHIVE_CAP, populate)
    shutil.copyfile(identity_path, out / 'candidate-identity.json')
    write_json(out / 'candidate-artifact-manifest.json', dict(schema=1, kind=MODE,
        archive=record, binaryBytes=before.st_size, binarySha256=identity['sha256'],
        fullBytes=True, stripped=False, truncated=False, sourceCommit=args.driver_sha,
        driverCommit=args.driver_sha, kernelOrigin=args.kernel_sha, buildId=identity['buildId'],
        validationPassClaimed=False, historicalCandidateReuploaded=False,
        maxRawBytes=BINARY_RAW_CAP, maxArchiveBytes=BINARY_ARCHIVE_CAP))
    return record


if __name__ == '__main__':
    raise SystemExit(main())
