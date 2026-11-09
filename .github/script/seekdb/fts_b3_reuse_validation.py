#!/usr/bin/env python3
"""Explicit, fail-closed continuation for the exact archived B3, with no rebuild.

No download implementation is present. Supplying already-authorized local ZIPs
and --execute is required to prepare/run. The production pins cannot be supplied
by the CLI. Original B3 build/source/driver identity and failed report are kept
unchanged; the new validation driver has a separate identity. Plan is default.
"""
import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import sys
import time
import tempfile
import traceback
import zipfile

import fts_source_hybrid_validation as hybrid
import fts_paired_validation as legacy
import fts_verified_elf_reuse as checked
from fts_plan_compatibility import classify_plan_metadata_compatibility
from fts_paired_validation import (BASELINE, PREFIX, CASES, SCHEDULE, PAIRS, BENCH,
    CONFIG, CLIENTS, MARGIN, SOURCE_TREES, digest,
    write_json, git, source_identity, no_symlinks, contains_bytes, capture_group,
    runtime_env, load_module, require_instance, effective_parameters,
    benchmark_work, accepted_plan_assertions, performance, install_termination_handlers)

MODE = 'verified_original_A_exact_archived_B3_v1'
B3 = 'b3eed52688fc1b178632a9dbb38aabb32f805c5d'
B3_TREE = 'c20b77107c7c0557ea1f4b33bc455603af8b5f53'
PRODUCTION_KERNEL = hybrid.PRODUCTION_KERNEL
PRODUCTION_KERNEL_TREE = hybrid.PRODUCTION_KERNEL_TREE
BUILD_SETTINGS = dict(checked.BUILD_SETTINGS)
INVENTORY_FILE = 'fixtures/fts-b3-completed-log-inventory.json'
DRIVER_FILES = hybrid.DRIVER_FILES + ('fts_b3_reuse_validation.py', INVENTORY_FILE)
# A continuation may repair validation/untimed preparation, but no SQL, native
# inputs, build/dependency settings, proof implementation or production C++.
ALLOWED_CHANGES = frozenset((
    PREFIX + 'fts_b3_reuse_validation.py', PREFIX + 'test_fts_b3_reuse_validation.py',
    PREFIX + INVENTORY_FILE, PREFIX + 'fixtures/fts-b3-reuse-proposal.yml',
    PREFIX + 'fixtures/fts-source-hybrid-b3-build.yml',
    PREFIX + 'test_fts_b3_live_workflow.py',
    PREFIX + 'fts_source_hybrid_validation.py', PREFIX + 'test_fts_source_hybrid_validation.py',
    '.github/workflows/fts-contest-validation.yml'))


@dataclass(frozen=True)
class _Pins:
    zip_bytes: int
    zip_sha256: str
    archive_bytes: int
    archive_sha256: str
    elf_bytes: int
    elf_sha256: str
    build_id: str
    version_sha256: str
    identity_bytes: int
    identity_sha256: str


_PRODUCTION_PINS = _Pins(328291894,
    'a284cb7e4cd7c3a657b78b14f05e320a21645246a820f74381bcaddc0d91e703',
    329567714, '62134e26c8c23e8dcd1651b499f9741656660468f328e50b6742e67c4f97c04f',
    1332364368, '42a9b50508cb80798254707ff264a6bf153866726a2aa006a0718c78bbfa59f8',
    '3a7c32ed6c7f7eb2085a9eec107dc5196b91750d',
    '00360d43f60c5ee0292716f8f95d62c4b5025c2ca1870872b76bd6ba040d9bcc',
    13288, '7898d90149faaff800d20d34d651341f64c1a3f334dade49f74f73f24ad01306')



def verify_loader(binary, log, source, env):
    """Actual A/B/client ldd checks each own a 30s group and a 1 MiB output cap."""
    # Set the file-size limit only in the isolated child, never this process.
    script = ('import os, resource, sys; '
        'resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576)); '
        'os.execv("/usr/bin/ldd", ["ldd", sys.argv[1]])')
    row = capture_group([sys.executable, '-c', script, str(binary)], log, source, 30, env=env)
    no_symlinks(log); info = log.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > checked.NOTE_CAP
            or row.get('exitCode') != 0 or type(row.get('exitCode')) is not int
            or row.get('childrenCleaned') is not True or row.get('timedOut')
            or b'not found' in log.read_bytes()):
        raise RuntimeError('Actual bounded ldd failed: ' + str(binary))
    return row

def _json_bytes(value):
    # Matches the original hybrid writer, preserving its field order.
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')


def _metadata():
    raw = (Path(__file__).parent / INVENTORY_FILE).read_bytes()
    checked._require(checked._sha(raw) == '657c1a3e65519844aba6adcb20bff44a9f3f3c6e0a7fcbb737f4db081d114def', 'Completed-log inventory bytes changed')
    data = checked._strict_json(raw, 'completed-log inventory')
    checked._require(data.get('artifactId') == 13652898100 and data.get('runId') == 42565036644
        and data.get('jobId') == 128989606853
        and data.get('zipBytes') == _PRODUCTION_PINS.zip_bytes
        and data.get('zipSha256') == _PRODUCTION_PINS.zip_sha256,
        'Completed-log artifact identity changed')
    return data['evidenceManifest']


def _candidate_manifest(pins):
    return dict(schema=1, kind=hybrid.MODE,
        archive=dict(path='candidate-elf.tar.gz', bytes=pins.archive_bytes, sha256=pins.archive_sha256),
        binaryBytes=pins.elf_bytes, binarySha256=pins.elf_sha256,
        fullBytes=True, stripped=False, truncated=False, sourceCommit=B3, driverCommit=B3,
        kernelOrigin=PRODUCTION_KERNEL, buildId=pins.build_id,
        validationPassClaimed=False, historicalCandidateReuploaded=False,
        maxRawBytes=checked.BINARY_RAW_CAP, maxArchiveBytes=checked.BINARY_ARCHIVE_CAP)


def _member_inventory(manifest, pins):
    checked._require(manifest.get('schema') == 1 and manifest.get('kind') == hybrid.MODE
        and manifest.get('historicalValidationAccepted') is False
        and manifest.get('historicalCollectorPassed') is False
        and len(manifest.get('files', [])) <= 640, 'Invalid completed-log evidence inventory')
    result = {}
    for record in manifest['files']:
        checked._require(set(record) == {'path', 'bytes', 'sha256'}, 'Invalid evidence entry schema')
        name = 'evidence/' + checked._safe_name(record['path'])
        checked._require(name not in result, 'Duplicate evidence inventory entry')
        checked._integer(record['bytes'], 8 * checked.MIB, 'evidence entry bytes')
        checked._hex(record['sha256'], 64, 'evidence entry hash')
        result[name] = dict(bytes=record['bytes'], sha256=record['sha256'])
    checked._require(sum(x['bytes'] for x in result.values()) == manifest['bytes'] <= 64 * checked.MIB,
        'Evidence aggregate byte cap/accounting mismatch')
    for name, raw in (
        ('evidence/source-hybrid-evidence-manifest.json', _json_bytes(manifest)),
        ('candidate-build/candidate-artifact-manifest.json', _json_bytes(_candidate_manifest(pins)))):
        result[name] = dict(bytes=len(raw), sha256=checked._sha(raw))
    result['candidate-build/candidate-identity.json'] = dict(bytes=pins.identity_bytes, sha256=pins.identity_sha256)
    result['candidate-build/candidate-elf.tar.gz'] = dict(bytes=pins.archive_bytes, sha256=pins.archive_sha256)
    checked._require(result.get('evidence/runtime/candidate/identity.json') ==
        dict(bytes=pins.identity_bytes, sha256=pins.identity_sha256), 'Missing exact duplicate build identity')
    for name in ('evidence/plan.json', 'evidence/report.json'):
        checked._require(name in result, 'Missing essential historical path: ' + name)
    return result


def _identity(identity, old_plan, source, pins):
    old_root = checked._historical_path(old_plan.get('root'))
    workspace = checked._historical_path(old_plan.get('workspace'))
    checked._require(old_plan.get('mode') == hybrid.MODE and old_plan.get('driverCommit') == B3
        and old_plan.get('candidateBuildCommit') == B3 and old_plan.get('validationDriverCommit') == B3
        and old_plan.get('kernelOrigin') == PRODUCTION_KERNEL
        and old_plan.get('candidateSource') == str(workspace), 'Original B3 plan identity mismatch')
    expected = dict(role='candidate', sourceCommit=B3, sourceTree=B3_TREE,
        driverCommit=B3, validationDriverCommit=B3, kernelOrigin=PRODUCTION_KERNEL,
        binary=str(old_root / 'runtime/candidate/seekdb'), bytes=pins.elf_bytes,
        sha256=pins.elf_sha256, buildId=pins.build_id,
        generatedVersionSha256=pins.version_sha256, buildSettings=BUILD_SETTINGS,
        rustVersion=checked.RUST_VERSION, officialInputHashes=hybrid.inventory(source),
        caseInputs=hybrid.case_input_hashes(source))
    checked._require(set(identity) == set(expected) | {'loader'} and
        all(type(identity.get(k)) is type(v) and identity[k] == v for k, v in expected.items()),
        'Original B3 source/driver/kernel/version/profile/input identity mismatch')
    checked._require(identity['loader'].get('exitCode') == 0 and
        identity['loader'].get('childrenCleaned') is True, 'Original B3 loader identity missing')
    return identity



def _zip_framing(stream, archive, byte_count, entry_count):
    """Bounded single-disk ZIP/ZIP64 framing, including bounded ZIP comments.

    This is a supported-format rule, not a claim about unread production bytes.
    Full SHA256 still pins every byte of the eventual authorized input.
    """
    stream.seek(0)
    checked._require(stream.read(4) == b'PK\x03\x04', 'ZIP prefix rejected')
    stream.seek(max(0,byte_count-65557))
    tail=stream.read(65557)
    position=len(tail)
    end=None
    while position >= 0:
        position=tail.rfind(b'PK\x05\x06',0,position)
        if position < 0:
            break
        if len(tail)-position >= 22:
            values=struct.unpack('<4s4H2LH',tail[position:position+22])
            if position+22+values[-1] == len(tail):
                end=values;break
    checked._require(end is not None, 'ZIP end/full-stream EOF framing rejected')
    end_offset=byte_count-len(tail)+position
    _,disk,cd_disk,disk_count,count,cd_size,cd_offset,comment=end
    checked._require(disk == cd_disk == 0 and tail[position+22:] == archive.comment,
        'ZIP multidisk/comment mismatch')
    locator_offset=end_offset-20
    stream.seek(max(0,locator_offset))
    locator=stream.read(20)
    zip64=len(locator)==20 and locator[:4]==b'PK\x06\x07'
    if zip64:
        _,record_disk,record_offset,disks=struct.unpack('<4sLQL',locator)
        checked._require(record_disk==0 and disks==1 and 0 <= record_offset <= locator_offset-56,
            'ZIP64 multidisk/record offset rejected')
        stream.seek(record_offset)
        record=checked._read_exact(stream,56)
        sig,length,_,_,record_disk,record_cd_disk,record_disk_count,record_count,record_cd_size,record_cd_offset=struct.unpack('<4sQ2H2L4Q',record)
        checked._require(sig==b'PK\x06\x06' and 44 <= length <= checked.NOTE_CAP
            and record_offset+12+length==locator_offset and record_disk==record_cd_disk==0
            and record_count==record_disk_count==entry_count,
            'ZIP64 end-record bounds/count mismatch')
        checked._require(count in (0xffff,record_count) and disk_count in (0xffff,record_count)
            and cd_size in (0xffffffff,record_cd_size) and cd_offset in (0xffffffff,record_cd_offset),
            'ZIP64/legacy end-record mismatch')
        cd_size,cd_offset,central_end=record_cd_size,record_cd_offset,record_offset
    else:
        checked._require(disk_count==count==entry_count and count!=0xffff
            and cd_size!=0xffffffff and cd_offset!=0xffffffff,
            'ZIP count/required ZIP64 record mismatch')
        central_end=end_offset
    checked._require(cd_offset==archive.start_dir and 0 <= cd_size <= byte_count
        and cd_offset+cd_size==central_end, 'ZIP central-directory bounds mismatch')

def _prepare_candidate(artifact_zip, root, source, pins, manifest):
    """Private fixture seam; public preparation has no identity override argument."""
    artifact_zip, root = checked._checked_path(artifact_zip), checked._checked_path(root)
    checked._require(not root.exists() and root.parent.is_dir() and artifact_zip != root
        and root not in artifact_zip.parents, 'Candidate runtime needs a fresh disjoint directory')
    members = _member_inventory(manifest, pins)
    owned = False
    try:
        with artifact_zip.open('rb') as original:
            before = os.fstat(original.fileno())
            checked._require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and before.st_size == pins.zip_bytes, 'Exact candidate ZIP size/type/link mismatch')
            count, sha, _, _ = checked._stream_digest(original, pins.zip_bytes)
            checked._require(count == pins.zip_bytes and sha == pins.zip_sha256, 'Full candidate ZIP SHA256 mismatch')
            original.seek(0)
            with zipfile.ZipFile(original) as archive:
                infos = archive.infolist()
                directories={str(parent) for name in members for parent in Path(name).parents if str(parent)!='.'}
                checked._require(len(members) <= len(infos) <= len(members)+len(directories),
                    'Candidate ZIP entry count exceeds bounded files/ancestor directories')
                _zip_framing(original,archive,pins.zip_bytes,len(infos))
                seen,seen_directories=set(),set()
                for info in infos:
                    directory=info.is_dir()
                    name=checked._safe_name(info.filename[:-1] if directory else info.filename)
                    mode=info.external_attr >> 16
                    checked._require(info.orig_filename==name+('/' if directory else '')
                        and not info.flag_bits & 1
                        and info.compress_type in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED),
                        'Unsafe/encrypted/unsupported ZIP entry')
                    if directory:
                        checked._require(name in directories and name not in seen_directories
                            and stat.S_IFMT(mode) in (0,stat.S_IFDIR) and info.file_size==info.CRC==0,
                            'ZIP directory is not an empty expected ancestor')
                        seen_directories.add(name)
                        claim=dict(bytes=0,sha256=checked._sha(b''))
                    else:
                        checked._require(name in members and name not in seen
                            and stat.S_IFMT(mode) in (0,stat.S_IFREG) and not info.external_attr & 0x10
                            and info.file_size==members[name]['bytes'],
                            'Unexpected/duplicate/unsafe ZIP file entry')
                        seen.add(name);claim=members[name]
                    # Reading every stream to EOF validates all CRCs and all bytes,
                    # even evidence not needed by the continuation, and empty dirs.
                    with archive.open(info) as stream:
                        size,sha,_,_=checked._stream_digest(stream,claim['bytes'])
                    checked._require(size==claim['bytes'] and sha==claim['sha256'],
                        'Full ZIP member digest mismatch: '+name)
                checked._require(seen==set(members), 'Missing candidate ZIP path')
                root.mkdir(mode=0o700); owned = True
                for info in infos:
                    if info.is_dir() or info.filename == 'candidate-build/candidate-elf.tar.gz':
                        continue
                    target = root / 'original' / info.filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as stream:
                        raw = checked._read_exact(stream, info.file_size)
                        checked._require(not stream.read(1) and checked._sha(raw) == members[info.filename]['sha256'],
                            'ZIP metadata changed during materialization')
                    raw.decode('utf-8', errors='strict')
                    checked._new_file(target, raw)
                raw_identity = (root / 'original/candidate-build/candidate-identity.json').read_bytes()
                checked._new_file(root / 'identity.original.json', raw_identity)
                identity = checked._strict_json(raw_identity, 'original B3 identity')
                old_plan = checked._strict_json((root / 'original/evidence/plan.json').read_bytes(), 'original B3 plan')
                _identity(identity, old_plan, source, pins)
                old_report = checked._strict_json((root / 'original/evidence/report.json').read_bytes(), 'original invalid B3 report')
                checked._require(old_report.get('status') == 'invalid_calibration'
                    and not old_report.get('sameHostPerformance'), 'Original invalid B3 verdict must be retained')
                binary = root / 'seekdb'
                def elf(name, size, stream):
                    checked._require(name == 'candidate/seekdb' and size == pins.elf_bytes, 'B3 tar path/size mismatch')
                    fd = os.open(binary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
                    with os.fdopen(fd, 'wb') as out:
                        count, sha, head, embedded = checked._stream_digest(stream, pins.elf_bytes, out, B3.encode('ascii'))
                    checked._require(count == pins.elf_bytes and sha == pins.elf_sha256 and
                        head == b'\x7fELF' and embedded, 'Full B3 ELF hash/magic/embedded build source mismatch')
                with archive.open('candidate-build/candidate-elf.tar.gz') as stream:
                    names = checked._walk_tar(stream, pins.elf_bytes, 1, elf)
                checked._require(names == {'candidate/seekdb'}, 'Missing complete B3 ELF')
                build_id = checked._read_build_id(binary, root / 'readelf-notes.log')
                checked._require(build_id == pins.build_id, 'Actual B3 static BuildID mismatch')
            checked._require(checked._same_file(before, os.fstat(original.fileno())), 'Candidate ZIP changed during preparation')
        relocation = dict(binary=str(binary), identityOriginalPath=str(root / 'identity.original.json'),
            identityOriginalSha256=pins.identity_sha256, artifactId=13652898100, runId=42565036644,
            artifactZipSha256=pins.zip_sha256, sourceCommit=B3, sourceTree=B3_TREE, buildDriverCommit=B3,
            kernelOrigin=PRODUCTION_KERNEL, previousValidationAccepted=False,
            currentValidationPerformed=False, generatedVersionEvidence='archived original identity plus exact full ELF; no D-generated version comparison')
        checked._new_file(root / 'relocation.json', checked._json_bytes(relocation))
        checked._new_file(root / 'verification.json', checked._json_bytes(dict(
            status='exact_B3_static_bytes_verified', relocation=relocation,
            originalReportStatus='invalid_calibration', currentValidationPerformed=False)))
        binary.chmod(0o555)
        return relocation
    except Exception:
        if owned and (root / 'seekdb').is_file() and not (root / 'seekdb').is_symlink():
            (root / 'seekdb').chmod(0o444)
        raise


def prepare_candidate(artifact_zip, root, source):
    if source_identity(source,B3)['tree'] != B3_TREE:
        raise RuntimeError('Public reuse requires actual clean B3 source/tree')
    return _prepare_candidate(artifact_zip, root, source, _PRODUCTION_PINS, _metadata())


def verify_driver(source, driver_sha, kernel_sha):
    if (not hybrid.exact_sha(driver_sha) or driver_sha in hybrid.OLD_KERNELS | {B3}
            or kernel_sha != PRODUCTION_KERNEL):
        raise RuntimeError('Explicit new validation driver D and exact B3 production kernel required')
    identity = source_identity(source, driver_sha)
    if git(source, 'rev-parse', B3 + '^{tree}').decode().strip() != B3_TREE:
        raise RuntimeError('Pinned actual B3 tree mismatch')
    if git(source, 'rev-parse', kernel_sha + '^{tree}').decode().strip() != PRODUCTION_KERNEL_TREE:
        raise RuntimeError('Pinned production kernel tree mismatch')
    changed = set(git(source, 'diff', '--name-only', B3, driver_sha, '--').decode().splitlines())
    if not changed <= ALLOWED_CHANGES:
        raise RuntimeError('Validation driver differs from B3 outside explicit CI allowlist: ' + ', '.join(sorted(changed - ALLOWED_CHANGES)))
    # Existing proof/loader and all protected build/SQL/native inputs cannot change.
    if git(source, 'diff', '--name-only', B3, driver_sha, '--', *hybrid.PROTECTED_BUILD).strip():
        raise RuntimeError('Original build/dependency profile changed')
    inputs = hybrid.inventory(source)
    return dict(driverCommit=driver_sha, driverTree=identity['tree'], candidateBuildCommit=B3,
        candidateBuildTree=B3_TREE, kernelOrigin=kernel_sha, kernelTree=PRODUCTION_KERNEL_TREE,
        allowedDriverChanges=sorted(changed), officialInputCount=len(inputs),
        officialInputInventorySha256=hybrid.INVENTORY_SHA256,
        caseInputs=hybrid.case_input_hashes(source))


def plan_for(source, root, driver_sha, kernel_sha):
    return dict(scope='SAME_HOST_DEVELOPMENT_CALIBRATION_NOT_FORMAL_CONTEST', mode=MODE,
        driverCommit=driver_sha, validationDriverCommit=driver_sha, candidateBuildCommit=B3,
        candidateBuildDriverCommit=B3, candidateBuildTree=B3_TREE, kernelOrigin=kernel_sha,
        baselineCommit=BASELINE, workspace=str(source), root=str(root),
        baselineSource=str(root / 'sources/baseline'), candidateSource=str(root / 'sources/candidate'),
        schedule=[dict(name=n, role=r) for n, r in SCHEDULE], pairs=[dict(baseline=a,candidate=b) for a,b in PAIRS],
        benchmarkConfig=CONFIG, performanceMargin=MARGIN, profiler=False, caseRetries=0,
        candidateBuildsRequired=0, baselineRebuilt=False, historicalCandidateExecuted=False,
        historicalB3ValidationAccepted=False, freshNativeCasesRequired=13,
        freshUntimedInstancesRequired=2, freshTimedRoundsRequired=8, ownedCleanupsRequired=23)


def prepare(args):
    root, source = args.root, args.source
    no_symlinks(root)
    if root.exists() or root == source or source in root.parents or root in source.parents:
        raise RuntimeError('Preparation needs a fresh root disjoint from validation driver')
    proof = verify_driver(source, args.driver_sha, args.kernel_sha)
    if args.artifact_zip is None or args.candidate_artifact_zip is None:
        raise RuntimeError('Both immutable original-A ZIP and exact B3 ZIP are required')
    root.mkdir(parents=True)
    for relative in ('driver', 'sources', 'historical', 'runtime', 'evidence/baseline',
        'evidence/candidate', 'validation/cases/baseline', 'validation/cases/candidate',
        'validation/untimed', 'validation/rounds'):
        (root / relative).mkdir(parents=True, exist_ok=True)
    plan = plan_for(source, root, args.driver_sha, args.kernel_sha)
    plan.update(equivalence=proof, driverFiles={})
    for name in DRIVER_FILES:
        path = source / PREFIX / name
        expected = hashlib.sha256(git(source, 'show', args.driver_sha + ':' + PREFIX + name)).hexdigest()
        if digest(path) != expected:
            raise RuntimeError('Validation driver script hash mismatch: ' + name)
        target = root / 'driver' / name; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target); plan['driverFiles'][name] = expected
    for role, commit, tree in (('baseline',BASELINE,SOURCE_TREES['baseline']), ('candidate',B3,B3_TREE)):
        target = root / 'sources' / role
        git(source, 'worktree', 'add', '--detach', str(target), commit)
        if source_identity(target, commit)['tree'] != tree:
            raise RuntimeError('Exact role source tree changed')
    original = checked.prepare_verified_runtime(args.artifact_zip, root / 'historical/runtime')
    Path(original['roles']['candidate']['binary']).chmod(0o444)
    plan.update(artifact=original['artifact'],
        runtimeVerificationSha256=digest(root / 'historical/runtime/verification.json'))
    plan['candidateRelocation'] = prepare_candidate(args.candidate_artifact_zip,
        root / 'runtime/candidate', root / 'sources/candidate')
    plan['candidateVerificationSha256'] = digest(root / 'runtime/candidate/verification.json')
    plan['candidateRelocationSha256'] = digest(root / 'runtime/candidate/relocation.json')
    write_json(root / 'plan.json', plan)
    return plan


def load_plan(args):
    plan = checked._strict_json((args.root / 'plan.json').read_bytes(), 'saved reuse plan')
    if any(plan.get(k) != v for k,v in plan_for(args.source,args.root,args.driver_sha,args.kernel_sha).items()):
        raise RuntimeError('Saved B3 reuse plan identity/configuration mismatch')
    if set(plan.get('driverFiles', {})) != set(DRIVER_FILES):
        raise RuntimeError('Incomplete saved validation driver identity')
    for name, sha in plan['driverFiles'].items():
        no_symlinks(args.root / 'driver' / name)
        if digest(args.root / 'driver' / name) != sha:
            raise RuntimeError('Saved validation driver changed: ' + name)
    if verify_driver(args.source,args.driver_sha,args.kernel_sha) != plan['equivalence']:
        raise RuntimeError('Validation driver/source changed')
    for role, commit, tree in (('baseline',BASELINE,SOURCE_TREES['baseline']),('candidate',B3,B3_TREE)):
        if source_identity(Path(plan[role + 'Source']),commit)['tree'] != tree:
            raise RuntimeError('Exact role source tree changed')
    return plan


def candidate_identity(identity, args, plan):
    directory = args.root / 'runtime/candidate'
    for name, key in (('verification.json','candidateVerificationSha256'),('relocation.json','candidateRelocationSha256')):
        no_symlinks(directory / name)
        if digest(directory / name) != plan.get(key):
            raise RuntimeError('Exact B3 saved verification/relocation changed')
    expected_members = _member_inventory(_metadata(),_PRODUCTION_PINS)
    for name in ('evidence/plan.json','evidence/report.json',
        'evidence/runtime/candidate/identity.json','candidate-build/candidate-artifact-manifest.json'):
        path = directory/'original'/name; no_symlinks(path)
        info = path.stat(); claim=expected_members[name]
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != claim['bytes'] or digest(path) != claim['sha256']:
            raise RuntimeError('Original B3 historical identity/report bytes changed')
    no_symlinks(directory/'identity.original.json')
    raw = (directory / 'identity.original.json').read_bytes()
    if len(raw) != _PRODUCTION_PINS.identity_bytes or checked._sha(raw) != _PRODUCTION_PINS.identity_sha256:
        raise RuntimeError('Original B3 build identity bytes changed')
    old_plan = checked._strict_json((directory / 'original/evidence/plan.json').read_bytes(), 'original B3 plan')
    if checked._strict_json(raw, 'original B3 identity') != identity:
        raise RuntimeError('B3 original identity was relabeled')
    _identity(identity, old_plan, Path(plan['candidateSource']), _PRODUCTION_PINS)
    relocation = checked._strict_json((directory/'relocation.json').read_bytes(), 'B3 relocation')
    if relocation != plan.get('candidateRelocation') or relocation.get('binary') != str(directory/'seekdb'):
        raise RuntimeError('Local B3 relocation changed')
    binary = directory / 'seekdb'
    no_symlinks(binary); info = binary.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != _PRODUCTION_PINS.elf_bytes
        or digest(binary) != _PRODUCTION_PINS.elf_sha256 or not contains_bytes(binary,B3.encode('ascii'))):
        raise RuntimeError('Exact B3 full ELF bytes/embedded source changed')
    with tempfile.TemporaryDirectory(prefix='static-notes-',dir=directory) as notes:
        if checked._read_build_id(binary,Path(notes)/'readelf.log') != _PRODUCTION_PINS.build_id:
            raise RuntimeError('Exact B3 current BuildID changed')
    return binary


historical_baseline = hybrid.historical_baseline
selected_cases = hybrid.selected_cases
tokenize_work = hybrid.tokenize_work
tokenize_complete = hybrid.tokenize_complete
persist_report = hybrid.persist_report


def accepted_compatibility(proof, row, validation_driver, candidate_commit):
    """Consume complete role-bound byte proof, never an accepted flag alone."""
    from fts_plan_compatibility import SCHEME, CASE, SPECS, TEST_SHA256, GOLDEN_SHA256, GOLDEN, TEST
    if not isinstance(proof, dict) or not isinstance(row, dict):
        return False
    context = row.get('validationContext')
    if (not isinstance(context, dict) or proof.get('validationContext') != context
            or context.get('sourceCommit') != candidate_commit
            or context.get('kernelOrigin') != PRODUCTION_KERNEL
            or candidate_commit != B3 or not hybrid.exact_sha(validation_driver)
            or validation_driver in hybrid.OLD_KERNELS | {B3}
            or candidate_commit in hybrid.OLD_KERNELS
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
    if any(report.get(key) for key in ('instances','caseVerdicts','untimedVerdicts','roundVerdicts',
        'roundMetrics','supplementalCompatibility','sameHostPerformance','planAssertions')):
        raise RuntimeError('Fresh B3 validation cannot resume/reuse any earlier instances')
    for stage in ('cases/baseline','cases/candidate','untimed','rounds'):
        directory = args.root/'validation'/stage
        no_symlinks(directory)
        if not directory.is_dir() or any(directory.iterdir()):
            raise RuntimeError('Every fresh validation stage must exist and be empty')
    harness = load_module('paired_original_harness', args.root / 'driver/fts_contest_validation.py')
    candidate_source = Path(plan['candidateSource'])
    scorer = load_module('paired_official_scorer', candidate_source / (BENCH + '_score.py'))
    baseline = scorer.load_baseline(candidate_source / (BENCH + '_baseline.json'))
    verify_driver(args.source, args.driver_sha, args.kernel_sha)
    source_identity(candidate_source, B3)
    original_binary, original_identity = historical_baseline(args, plan)
    report.update(mode=MODE, validationDriverCommit=args.driver_sha,
        candidateBuildCommit=B3, candidateBuildDriverCommit=B3, candidateRebuilt=False,
        historicalB3ValidationAccepted=False, kernelOrigin=args.kernel_sha,
        historicalCandidateExecuted=False, baselineRebuilt=False,
        candidateReuse=plan['candidateRelocation'],
        artifactReuse=dict(artifact=plan['artifact'], verificationSha256=plan['runtimeVerificationSha256'],
            oldValidationAccepted=False, historicalCollectorPassed=False,
            originalManifestOk=False, historicalCandidateExecuted=False))
    roles = {}
    pinned = {}
    for role in ('baseline', 'candidate'):
        source = Path(plan[role + 'Source'])
        commit = BASELINE if role == 'baseline' else B3
        identity = original_identity if role == 'baseline' else json.loads(
            (args.root / 'runtime/candidate/identity.original.json').read_text(encoding='utf-8'))
        source_id = source_identity(source, commit)
        if source_id['tree'] != identity['sourceTree']:
            raise RuntimeError('Saved source tree differs from exact role snapshot')
        binary = original_binary if role == 'baseline' else candidate_identity(identity, args, plan)
        provenance = None
        role_args = argparse.Namespace(role=role, source=source, expected_sha=commit, binary=binary,
            workspace=candidate_source, obclient=candidate_source / 'deps/3rd/u01/obclient/bin/obclient',
            mysqltest=candidate_source / 'deps/3rd/u01/obclient/bin/mysqltest')
        env = runtime_env(candidate_source, binary)
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
        source_identity(candidate_source,B3)
        candidate_identity(roles['candidate'][2],args,plan)
        if hybrid.case_input_hashes(candidate_source) != plan['equivalence']['caseInputs']:
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
        env = runtime_env(candidate_source, call_args.binary)
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
        if not (proof is not None and accepted_compatibility(proof, verdict, args.driver_sha, B3)):
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
    if not tokenize_complete(report.get('untimedVerdicts'), B3, args.root):
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
        if row['name'] not in inputs or not isinstance(work, dict) or any(work.get(key) != value for key, value in inputs[row['name']].items()):
            return False
        if row['role'] == 'candidate' and row['name'] == CASES[-1]:
            if not accepted_compatibility(proofs['candidate/' + CASES[-1]], row, driver, B3):
                return False
        elif (row.get('status') != 'passed' or row.get('cleanupPassed') is not True
                or work.get('passed') is not True or type(work.get('exitCode')) is not int
                or work['exitCode'] != 0 or type(work.get('invocations')) is not int
                or work['invocations'] != 1 or work.get('inputsUnchanged') is not True
                or any(record.get(key) for record in (row, work) for key in
                    ('error', 'traceback', 'timedOut', 'signal', 'cleanupError', 'failureKind', 'failureReason'))):
            return False
    return True



def completed(report, args, plan):
    if not isinstance(report, dict):
        return False
    if (report.get('status') != 'valid_calibration_completed' or report.get('failure')
            or report.get('mode') != MODE or report.get('driverCommit') != args.driver_sha
            or report.get('candidateBuildCommit') != B3
            or report.get('candidateBuildDriverCommit') != B3
            or report.get('candidateRebuilt') is not False
            or report.get('historicalB3ValidationAccepted') is not False
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
            or not tokenize_complete(report.get('untimedVerdicts'), B3, args.root)):
        return False
    rounds = report.get('roundVerdicts', [])
    if (len(rounds) != len(SCHEDULE) or [(r.get('name'), r.get('role')) for r in rounds] != list(SCHEDULE)
            or any(r.get('status') != 'passed' or r.get('cleanupPassed') is not True for r in rounds)):
        return False
    builds = report.get('builds', {})
    if (builds.get('baseline', {}).get('sourceCommit') != BASELINE
            or builds.get('baseline', {}).get('driverCommit') != legacy.CANDIDATE
            or builds.get('candidate', {}).get('sourceCommit') != B3
            or builds.get('candidate', {}).get('kernelOrigin') != args.kernel_sha
            or builds.get('candidate', {}).get('driverCommit') != B3):
        return False
    instances = report.get('instances', [])
    expected = [(r,n) for r in ('baseline','candidate') for n in selected_cases(r)] + [('baseline','tokenize-baseline'),('candidate','tokenize-candidate')] + [(r,n) for n,r in SCHEDULE]
    if len(instances) != 23 or [(r.get('role'),r.get('name')) for r in instances] != expected or any(r.get('cleanupPassed') is not True for r in instances):
        return False
    verdicts = report['caseVerdicts'] + report['untimedVerdicts'] + report['roundVerdicts']
    for instance, verdict in zip(instances, verdicts):
        keys = ('role','name','status','cleanupPassed','work') if 'work' in verdict else ('role','name','status','cleanupPassed')
        if any(instance.get(key) != verdict.get(key) for key in keys):
            return False
    try:
        load_plan(args)
        candidate_identity(json.loads((args.root/'runtime/candidate/identity.original.json').read_text()),args,plan)
        historical_baseline(args,plan)
        scorer = load_module('hybrid_final_scorer', Path(plan['candidateSource']) / (BENCH + '_score.py'))
        base = scorer.load_baseline(Path(plan['candidateSource']) / (BENCH + '_baseline.json'))
        pairs = [dict(baseline=a, candidate=b, baselineMetrics=report['roundMetrics'][a],
            candidateMetrics=report['roundMetrics'][b]) for a, b in PAIRS]
        return performance(pairs, base, scorer) == report['sameHostPerformance']
    except (OSError, KeyError, TypeError, ValueError, RuntimeError):
        return False



def collect_evidence(root, out):
    # Reuse the bounded explicit fresh-evidence selector, then add only the new
    # static reuse records. Never re-upload either unchanged ELF or old SQL logs.
    manifest = hybrid.collect_evidence(root, out)
    extras = ('runtime/candidate/identity.original.json', 'runtime/candidate/relocation.json',
        'runtime/candidate/verification.json',
        'runtime/candidate/original/evidence/plan.json',
        'runtime/candidate/original/evidence/report.json',
        'runtime/candidate/original/candidate-build/candidate-artifact-manifest.json')
    for relative in extras:
        path = root / relative
        no_symlinks(path)
        if not path.exists():
            continue
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 8 * checked.MIB:
            raise RuntimeError('Reuse evidence type/link/cap violation')
        raw = path.read_bytes(); checked._strict_json(raw, relative)
        target = out / relative; target.parent.mkdir(parents=True, exist_ok=True)
        checked._new_file(target, raw)
        if digest(path) != checked._sha(raw):
            raise RuntimeError('Reuse evidence changed during copy')
        manifest['files'].append(dict(path=relative,bytes=len(raw),sha256=checked._sha(raw)))
        manifest['bytes'] += len(raw)
    if len(manifest['files']) > 640 or manifest['bytes'] > 64 * checked.MIB:
        raise RuntimeError('Reuse evidence aggregate cap violation')
    manifest.update(kind=MODE, historicalB3ValidationAccepted=False, candidateRebuilt=False)
    write_json(out / 'source-hybrid-evidence-manifest.json', manifest)
    return manifest


def present(report, args, plan=None):
    valid = plan is not None and completed(report,args,plan)
    visible = dict(status=report.get('status','unavailable'), mode=MODE,
        candidateBuildCommit=B3, candidateBuildDriverCommit=B3,
        validationDriverCommit=args.driver_sha, kernelOrigin=PRODUCTION_KERNEL,
        originalB3ValidationAccepted=False, formalContestRun=False,
        result=report.get('sameHostPerformance') if valid else 'invalid/incomplete; no performance claim',
        rawSimpleQueryFailRetained=True, failure=report.get('failure'))
    print('B3_REUSE_VISIBLE_RESULT ' + json.dumps(visible,ensure_ascii=False,allow_nan=False),flush=True)
    # Keep complete fresh evidence readable in logs even when collection fails.
    # Collection has its own outcome and cannot redefine this calibration verdict.
    print('B3_REUSE_FULL_REPORT\n' + json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2),flush=True)
    if args.summary:
        with args.summary.open('a',encoding='utf-8') as stream:
            stream.write('\n## Exact B3 reuse calibration\n\n' + json.dumps(visible,ensure_ascii=False) + '\n')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase',choices=('plan','verify-source','prepare-reuse','validate','finalize','present','collect-evidence'),default='plan')
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--driver-sha',required=True)
    parser.add_argument('--kernel-sha',required=True)
    parser.add_argument('--artifact-zip',type=Path)
    parser.add_argument('--candidate-artifact-zip',type=Path)
    parser.add_argument('--out',type=Path)
    parser.add_argument('--summary',type=Path)
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args(argv)
    no_symlinks(args.source); no_symlinks(args.root)
    args.source=args.source.resolve(strict=True); args.root=args.root.resolve(strict=False)
    if args.phase == 'verify-source':
        print(json.dumps(verify_driver(args.source,args.driver_sha,args.kernel_sha),indent=2)); return 0
    if args.phase == 'plan' or (not args.execute and args.phase != 'collect-evidence'):
        print(json.dumps(plan_for(args.source,args.root,args.driver_sha,args.kernel_sha),indent=2)); return 0
    if sys.platform != 'linux':
        raise RuntimeError('Exact B3 reuse requires Linux')
    if args.phase == 'collect-evidence':
        collect_evidence(args.root,args.out); return 0
    install_termination_handlers()
    report=dict(scope='SAME_HOST_DEVELOPMENT_CALIBRATION_NOT_FORMAL_CONTEST',mode=MODE,
        status='running',driverCommit=args.driver_sha,validationDriverCommit=args.driver_sha,
        candidateBuildCommit=B3,candidateBuildDriverCommit=B3,kernelOrigin=args.kernel_sha,
        baselineCommit=BASELINE,phase=args.phase,formalContestRun=False,builds={},instances=[],
        caseVerdicts=[],roundVerdicts=[],roundMetrics={})
    plan=None
    try:
        if args.phase == 'prepare-reuse':
            plan=prepare(args)
        else:
            if (args.root/'report.json').is_file():
                report=checked._strict_json((args.root/'report.json').read_bytes(),'current report')
                report['phase']=args.phase
            plan=load_plan(args)
            if args.phase == 'validate':
                validate(args,plan,report)
            elif args.phase == 'finalize':
                if not completed(report,args,plan):
                    report.update(status='invalid_calibration',failure=report.get('failure') or
                        dict(phase=args.phase,reason='Incomplete fresh B3 reuse evidence'))
                persist_report(args.root,report)
                return 0 if report['status']=='valid_calibration_completed' else 1
            else:
                present(report,args,plan); return 0
        if args.phase != 'validate':
            report['status']='phase_completed'; persist_report(args.root,report)
        return 0
    except Exception as error:
        report.update(status='invalid_calibration',failure=dict(phase=args.phase,
            reason=str(error),traceback=traceback.format_exc()))
        if args.root.is_dir() and (args.root/'plan.json').is_file():
            persist_report(args.root,report)
        if args.phase == 'present':
            present(report,args,None)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
