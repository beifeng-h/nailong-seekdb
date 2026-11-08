#!/usr/bin/env python3
"""Offline, fail-closed materialization of one immutable paired-build artifact.

This module never downloads, runs a preserved executable, invokes ldd, changes a
source checkout, or accepts caller-supplied production identity pins. The public
entry point only prepares independently verified ELF files. Historical collector
errors and original identity bytes remain evidence, never a validation pass.
"""
from dataclasses import dataclass
import gzip
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import resource
import stat
import subprocess
import tarfile
import zipfile


MIB = 1024 * 1024
CHUNK = MIB
MANIFEST_CAP = 4 * MIB
JSON_CAP = 8 * MIB
BINARY_ARCHIVE_CAP = 416 * MIB
BINARY_RAW_CAP = 4096 * MIB
EVIDENCE_ARCHIVE_CAP = 60 * MIB
EVIDENCE_PAYLOAD_CAP = 56 * MIB
ENTRY_CAP = 4000
NOTE_CAP = MIB
ROLES = ('baseline', 'candidate')
ARCHIVES = ('baseline-elf.tar.gz', 'candidate-elf.tar.gz', 'evidence.tar.gz')
ZIP_NAMES = frozenset(('manifest.json',) + ARCHIVES)
BUILD_SETTINGS = (('CMAKE_BUILD_TYPE', 'RelWithDebInfo'), ('OB_ENABLE_UNITY', 'ON'), ('OB_USE_LLD', 'ON'))
RUST_VERSION = 'rustc 1.98.1 (48a229cea 2026-09-01)'
_HISTORICAL_DRIVER_FILES = (
    ('fts_paired_validation.py', 'f55168af47eb3aecd503d53b994333b5caf16f0967169a27a1c4875a77412522'),
    ('fts_paired_collect.py', 'd0fe9108fcc93355e7733aa159485f822886e2ecbe454d3dbec43095c5f6ac6a'),
    ('fts_contest_validation.py', '93098702921142f731624c5133ab05f9236589dea90e35f54e8a3586e4159583'))
_OPTIONAL_ERROR = 'DB log is not predominantly text; refusing binary conversion'
_OPTIONAL_PATH = re.compile(r'validation/cases/baseline/fts_index\.cn_word/db-log-tails/seekdb\.log(?:\.wf)?(?:\.[0-9]+)?(?:\.wf)?\Z')


class VerificationError(ValueError):
    """No reusable executable is approved when this exception is raised."""


@dataclass(frozen=True)
class _RolePin:
    role: str
    source_commit: str
    source_tree: str
    kernel_origin: str
    byte_count: int
    sha256: str
    build_id: str
    generated_version_sha256: str


@dataclass(frozen=True)
class _Pins:
    zip_bytes: int
    zip_sha256: str
    driver_commit: str
    inventory_count: int
    inventory_sha256: str
    roles: tuple


_PRODUCTION_PINS = _Pins(
    661059403, '23f8b2f8f6322edcd5dd0ed2b485c0d3271b3992d24417866de9eb90f77d8f75',
    'bc4dde62eee39acb630ffe54741aecebe4ab0a2a', 75,
    'f36e2d1c8aaabd3e9ffc4f8e3635defca6dc3046e3d575e6e73165fa8d80a135', (
        _RolePin('baseline', '5bce9e06d1281347ad61344e4b01fab9158c58a2',
                 '7397f01a796c5ab1cae81c90303b22b63945d11d',
                 '5bce9e06d1281347ad61344e4b01fab9158c58a2', 1332352208,
                 '0053a8ac0a76a24508db194f6c1df3134e0505f56429f83e59c7062fbc80f82d',
                 '0d31622e448cbde161365f2d978285c6003e7ca5',
                 '6d5acc3792bb3b2ae161b566dcf9383ac0b9a9ab119e2fe3fe5bb3f157da69c1'),
        _RolePin('candidate', 'bc4dde62eee39acb630ffe54741aecebe4ab0a2a',
                 '1ea510152c6274df5915ec9423fa76cda0ed4a2a',
                 'f1f4d0d4b225ec528dc3802dbfd93d9d4870aa65', 1332372408,
                 'cd76e18c6c1e0f50e2d30f62edf077946c761d7d335269f1cfd785022b83d95a',
                 '9a505bf0cb17bcdfac4b6c29a0703abccfccb6de',
                 '7b6ac43a7b13f268d2cebc3a998ebef39942e189c927b376bd61a7ede2d6a1ae')))


def _require(condition, message):
    if not condition:
        raise VerificationError(message)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _integer(value, maximum, label):
    _require(type(value) is int and 0 <= value <= maximum, 'Invalid ' + label)
    return value


def _hex(value, count, label):
    _require(isinstance(value, str) and re.fullmatch('[0-9a-f]{%d}' % count, value) is not None,
             'Invalid ' + label)


def _safe_name(name):
    _require(isinstance(name, str) and 0 < len(name) <= 1024 and not name.startswith('/')
             and '\\' not in name and ':' not in name and '\x00' not in name
             and all(part not in ('', '.', '..') for part in name.split('/')), 'Unsafe archive path')
    return name


def _historical_path(value):
    _require(isinstance(value, str) and value.startswith('/') and len(value) <= 4096
             and '\\' not in value and '\x00' not in value
             and all(part not in ('', '.', '..') for part in value[1:].split('/')),
             'Invalid historical absolute path')
    return PurePosixPath(value)


def _checked_path(path):
    path = Path(os.path.abspath(path))
    for part in reversed((path,) + tuple(path.parents)):
        if part.exists() or part.is_symlink():
            item = part.lstat()
            _require(not stat.S_ISLNK(item.st_mode) and not getattr(item, 'st_file_attributes', 0) & 0x400,
                     'Symlink/reparse path rejected: ' + str(part))
    return path


def _strict_json(raw, label, cap=JSON_CAP):
    _require(len(raw) <= cap, label + ' exceeds JSON cap')

    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, 'Duplicate JSON key in ' + label)
            result[key] = value
        return result

    def constant(value):
        raise VerificationError('Nonfinite JSON value in ' + label)

    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise VerificationError('Malformed ' + label + ': ' + str(error)) from error

    def bound(item, depth=0):
        _require(depth <= 32, label + ' nesting exceeds cap')
        if isinstance(item, dict):
            _require(len(item) <= ENTRY_CAP, label + ' object exceeds cap')
            for key, child in item.items():
                _require(len(key) <= 1024, label + ' key exceeds cap')
                bound(child, depth + 1)
        elif isinstance(item, list):
            _require(len(item) <= ENTRY_CAP, label + ' array exceeds cap')
            for child in item:
                bound(child, depth + 1)
        elif isinstance(item, str):
            _require(len(item) <= 65536 and not any(0xd800 <= ord(ch) <= 0xdfff for ch in item),
                     label + ' string invalid/exceeds cap')
        elif isinstance(item, float):
            _require(math.isfinite(item), 'Nonfinite JSON value in ' + label)
        elif type(item) is int:
            _require(abs(item) <= 2 ** 63 - 1, label + ' integer exceeds cap')

    bound(value)
    _require(isinstance(value, dict), label + ' must be a JSON object')
    return value


def _read_exact(stream, count):
    result = bytearray()
    while len(result) < count:
        block = stream.read(count - len(result))
        _require(bool(block), 'Truncated archive')
        result.extend(block)
    return bytes(result)


def _stream_digest(stream, cap, output=None, needle=None):
    digest, count, head, previous, found = hashlib.sha256(), 0, b'', b'', False
    while True:
        block = stream.read(CHUNK)
        if not block:
            break
        count += len(block)
        _require(count <= cap, 'Stream exceeds byte cap')
        digest.update(block)
        if not head:
            head = block[:4]
        if needle:
            found = found or needle in previous + block
            previous = (previous + block)[-(len(needle) - 1):]
        if output is not None:
            output.write(block)
    return count, digest.hexdigest(), head, found


class _BoundedReader:
    def __init__(self, stream, cap):
        self.stream, self.remaining = stream, cap

    def read(self, count=-1):
        _require(count >= 0, 'Unbounded archive read rejected')
        count = min(count, self.remaining + 1)
        data = self.stream.read(count)
        self.remaining -= len(data)
        _require(self.remaining >= 0, 'Decompressed tar exceeds byte cap')
        return data


class _MemberReader:
    def __init__(self, stream, size):
        self.stream, self.remaining = stream, size

    def read(self, count):
        data = self.stream.read(min(count, self.remaining))
        _require(bool(data) or self.remaining == 0, 'Truncated tar member')
        self.remaining -= len(data)
        return data


def _walk_tar(stream, payload_cap, entry_cap, visit):
    """Read ordinary ustar records only; tarfile never extracts or handles PAX."""
    raw_cap = payload_cap + entry_cap * 1024 + 10240
    with gzip.GzipFile(fileobj=stream, mode='rb') as decompressed:
        raw = _BoundedReader(decompressed, raw_cap)
        seen, total = set(), 0
        while True:
            header = _read_exact(raw, 512)
            if header == bytes(512):
                _require(_read_exact(raw, 512) == bytes(512), 'Tar has only one end marker')
                while True:
                    tail = raw.read(CHUNK)
                    if not tail:
                        return seen
                    _require(not any(tail), 'Nonzero data after tar end')
            _require(header[257:265] == b'ustar\x0000', 'Only ordinary ustar records are accepted')
            for field in (header[:100], header[345:500]):
                if b'\x00' in field:
                    _require(not any(field[field.index(b'\x00'):]), 'Hidden tar path bytes')
            try:
                info = tarfile.TarInfo.frombuf(header, encoding='utf-8', errors='strict')
            except (tarfile.HeaderError, UnicodeError, ValueError) as error:
                raise VerificationError('Malformed tar header') from error
            name = _safe_name(info.name)
            _require(info.type in (tarfile.REGTYPE, tarfile.AREGTYPE) and not info.linkname
                     and not info.pax_headers and info.sparse is None, 'Tar link/special/extension rejected')
            _require(name not in seen and len(seen) < entry_cap, 'Duplicate/excess tar member')
            seen.add(name)
            _integer(info.size, payload_cap, 'tar member size')
            total += info.size
            _require(total <= payload_cap, 'Tar payload exceeds byte cap')
            member = _MemberReader(raw, info.size)
            visit(name, info.size, member)
            _require(member.remaining == 0, 'Tar visitor did not consume full member')
            padding = _read_exact(raw, (-info.size) % 512)
            _require(not any(padding), 'Nonzero tar member padding')


def _zip_members(archive):
    items = archive.infolist()
    _require(len(items) == 4, 'Artifact must contain exactly four ZIP members')
    result = {}
    for item in items:
        name = _safe_name(item.filename)
        _require(item.orig_filename == name and name in ZIP_NAMES and name not in result,
                 'Unexpected/duplicate ZIP member')
        mode = item.external_attr >> 16
        _require(not item.is_dir() and stat.S_IFMT(mode) in (0, stat.S_IFREG)
                 and not item.external_attr & 0x10 and not item.flag_bits & 1,
                 'ZIP encrypted/link/special member rejected')
        cap = MANIFEST_CAP if name == 'manifest.json' else EVIDENCE_ARCHIVE_CAP if name == 'evidence.tar.gz' else BINARY_ARCHIVE_CAP
        _integer(item.file_size, cap, 'ZIP member size')
        _require(item.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED), 'Unsupported ZIP compression')
        result[name] = item
    _require(set(result) == ZIP_NAMES, 'Missing ZIP member')
    return result


def _manifest(raw, members, pins):
    manifest = _strict_json(raw, 'manifest', MANIFEST_CAP)
    fields = {'scope', 'binaryPolicy', 'binaryArchiveCapBytes', 'binaryRawCapBytes',
              'logArchiveCapBytes', 'logPayloadCapBytes', 'logFileCapBytes', 'manifestCapBytes',
              'maximumCombinedCompressedBytes', 'archives', 'files', 'errors', 'logPayloadBytes', 'ok'}
    _require(set(manifest) == fields, 'Unexpected/incomplete manifest schema')
    expected = dict(scope='OWNED_PAIRED_FTS_ONLY', binaryPolicy='FULL_BYTES_NO_STRIP_NO_TRUNCATION',
                    binaryArchiveCapBytes=BINARY_ARCHIVE_CAP, binaryRawCapBytes=BINARY_RAW_CAP,
                    logArchiveCapBytes=EVIDENCE_ARCHIVE_CAP, logPayloadCapBytes=EVIDENCE_PAYLOAD_CAP,
                    logFileCapBytes=JSON_CAP, manifestCapBytes=MANIFEST_CAP,
                    maximumCombinedCompressedBytes=2 * BINARY_ARCHIVE_CAP + EVIDENCE_ARCHIVE_CAP + MANIFEST_CAP)
    for key, value in expected.items():
        _require(type(manifest.get(key)) is type(value) and manifest[key] == value, 'Manifest policy mismatch: ' + key)
    _require(type(manifest['ok']) is bool and isinstance(manifest['errors'], list)
             and all(isinstance(error, str) and len(error) <= 2048 for error in manifest['errors'])
             and manifest['ok'] == (not manifest['errors']), 'Manifest error/status schema mismatch')
    archives = manifest['archives']
    _require(isinstance(archives, list) and len(archives) == 3, 'Three archive records required')
    claims = {}
    for record in archives:
        _require(isinstance(record, dict) and set(record) == {'path', 'bytes', 'sha256'}, 'Invalid archive claim schema')
        name = record['path']
        _require(isinstance(name, str) and name in ARCHIVES and name not in claims, 'Unexpected/duplicate archive claim')
        _integer(record['bytes'], members[name].file_size, 'archive bytes')
        _require(record['bytes'] == members[name].file_size, 'Archive/ZIP byte count mismatch')
        _hex(record['sha256'], 64, 'archive digest')
        claims[name] = record
    files = manifest['files']
    _require(isinstance(files, list) and len(files) <= ENTRY_CAP + 2, 'Invalid manifest file inventory')
    records, optional_errors, evidence_bytes = {}, [], 0
    binary_fields = {'path', 'bytes', 'sha256', 'truncated', 'role', 'sourceCommit', 'buildId'}
    evidence_fields = {'path', 'originalBytes', 'bytes', 'truncated', 'tailOffset', 'sha256', 'error',
                       'converted', 'representation', 'inputTailBytes', 'inputTailSha256', 'conversion',
                       'diagnosticLoss', 'selectedRawBytes', 'selectedRawSha256', 'escapedInvalidByteCount', 'originalSha256'}
    for record in files:
        _require(isinstance(record, dict) and isinstance(record.get('path'), str), 'Invalid manifest file record')
        name = _safe_name(record['path'])
        _require(name not in records, 'Duplicate manifest file record')
        records[name] = record
        if name in ('runtime/baseline/seekdb', 'runtime/candidate/seekdb'):
            role = name.split('/')[1]
            pin = next(item for item in pins.roles if item.role == role)
            _require(set(record) == binary_fields and record.get('role') == role
                     and record.get('sourceCommit') == pin.source_commit
                     and type(record.get('bytes')) is int and record['bytes'] == pin.byte_count
                     and record.get('sha256') == pin.sha256 and record.get('buildId') == pin.build_id
                     and record.get('truncated') is False, 'Manifest ELF pin mismatch: ' + role)
        else:
            _require({'path', 'originalBytes', 'bytes', 'truncated'} <= set(record)
                     and set(record) <= evidence_fields and type(record['truncated']) is bool,
                     'Invalid evidence file schema')
            _integer(record['originalBytes'], 2 ** 63 - 1, 'evidence original bytes')
            _integer(record['bytes'], JSON_CAP, 'evidence bytes')
            evidence_bytes += record['bytes']
            if 'error' in record:
                _require(_OPTIONAL_PATH.fullmatch(name) is not None and record['error'] == _OPTIONAL_ERROR
                         and record['bytes'] == 0 and record['truncated'] is False and 'sha256' not in record
                         and set(record) == {'path', 'originalBytes', 'bytes', 'truncated', 'error'},
                         'Fatal collector file error: ' + name)
                optional_errors.append(name + ': ' + record['error'])
            else:
                base_fields = {'path', 'originalBytes', 'bytes', 'truncated', 'tailOffset', 'sha256'}
                _require(base_fields <= set(record), 'Missing evidence digest/offset')
                _hex(record['sha256'], 64, 'evidence digest')
                _integer(record['tailOffset'], record['originalBytes'], 'evidence tail offset')
                if 'converted' in record:
                    conversion_fields = {'converted', 'representation', 'inputTailBytes', 'inputTailSha256',
                                         'conversion', 'diagnosticLoss', 'selectedRawBytes', 'selectedRawSha256',
                                         'escapedInvalidByteCount'}
                    _require(set(record) in (base_fields | conversion_fields,
                                             base_fields | conversion_fields | {'originalSha256'})
                             and record['converted'] is True
                             and record['representation'] == 'UTF8_DIAGNOSTIC_BACKSLASH_ESCAPES'
                             and record['conversion'] == 'Literal backslashes doubled; invalid bytes escaped as backslash-xNN'
                             and record['diagnosticLoss'] == 'Raw byte representation changed; any omitted prefix is recorded by tailOffset',
                             'Invalid diagnostic conversion schema')
                    _integer(record['inputTailBytes'], min(JSON_CAP, record['originalBytes']), 'conversion input bytes')
                    _integer(record['selectedRawBytes'], record['inputTailBytes'], 'conversion selected bytes')
                    _integer(record['escapedInvalidByteCount'], record['selectedRawBytes'], 'conversion invalid-byte count')
                    for key in ('inputTailSha256', 'selectedRawSha256'):
                        _hex(record[key], 64, key)
                    _require(record['tailOffset'] + record['selectedRawBytes'] == record['originalBytes']
                             and record['truncated'] == (record['selectedRawBytes'] != record['originalBytes'])
                             and record['selectedRawBytes'] <= record['bytes'] <= 4 * record['selectedRawBytes'],
                             'Invalid diagnostic conversion accounting')
                    _require(('originalSha256' in record) == (record['inputTailBytes'] == record['originalBytes']),
                             'Invalid original diagnostic digest presence')
                    if 'originalSha256' in record:
                        _require(record['originalSha256'] == record['inputTailSha256'], 'Invalid original diagnostic digest')
                else:
                    _require(set(record) == base_fields
                             and record['tailOffset'] + record['bytes'] == record['originalBytes']
                             and record['truncated'] == (record['bytes'] != record['originalBytes']),
                             'Invalid evidence byte accounting/schema')
                if name.endswith('.json'):
                    _require(record['truncated'] is False and record['tailOffset'] == 0
                             and record['bytes'] == record['originalBytes'] and 'converted' not in record,
                             'JSON record truncated/converted')
    _require(sorted(manifest['errors']) == sorted(optional_errors), 'Fatal/unaccounted collector errors')
    _integer(manifest['logPayloadBytes'], EVIDENCE_PAYLOAD_CAP, 'evidence total bytes')
    _require(evidence_bytes == manifest['logPayloadBytes'], 'Evidence payload accounting mismatch')
    for role in ROLES:
        _require('runtime/' + role + '/seekdb' in records, 'Missing manifest ELF record')
    for name in ('plan.json', 'runtime/baseline/identity.json', 'runtime/candidate/identity.json'):
        _require(name in records and 'error' not in records[name], 'Missing essential identity/plan record')
    return manifest, claims, records


def _new_file(path, raw, mode=0o444):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)


def _json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')


def _validate_metadata(plan, identities, pins):
    baseline, candidate = pins.roles
    plan_fields = {'scope', 'driverCommit', 'kernelOrigin', 'baselineCommit', 'workspace', 'root',
                   'baselineSource', 'candidateSource', 'schedule', 'pairs', 'benchmarkConfig',
                   'performanceMargin', 'profiler', 'caseRetries', 'driverTree', 'equivalence', 'driverFiles'}
    _require(set(plan) == plan_fields and plan.get('scope') == 'SAME_HOST_DEVELOPMENT_CALIBRATION_NOT_FORMAL_CONTEST'
             and plan.get('driverCommit') == pins.driver_commit and plan.get('baselineCommit') == baseline.source_commit
             and plan.get('kernelOrigin') == candidate.kernel_origin and plan.get('driverTree') == candidate.source_tree,
             'Original plan identity/schema mismatch')
    old_root, workspace = _historical_path(plan['root']), _historical_path(plan['workspace'])
    _require(old_root != workspace and old_root not in workspace.parents and workspace not in old_root.parents
             and plan['baselineSource'] == str(old_root / 'sources/baseline')
             and plan['candidateSource'] == str(workspace), 'Original plan source/root mismatch')
    _require(plan['driverFiles'] == dict(_HISTORICAL_DRIVER_FILES), 'Original driver inventory mismatch')
    required = {'role', 'sourceCommit', 'sourceTree', 'kernelOrigin', 'driverCommit', 'binary', 'bytes',
                'sha256', 'buildId', 'generatedVersionSha256', 'officialInputHashes', 'buildSettings', 'rustVersion', 'loader'}
    for pin in pins.roles:
        identity = identities[pin.role]
        _require(set(identity) == required, 'Unexpected/incomplete identity schema: ' + pin.role)
        expected = dict(role=pin.role, sourceCommit=pin.source_commit, sourceTree=pin.source_tree,
                        kernelOrigin=pin.kernel_origin, driverCommit=pins.driver_commit, bytes=pin.byte_count,
                        sha256=pin.sha256, buildId=pin.build_id, generatedVersionSha256=pin.generated_version_sha256,
                        buildSettings=dict(BUILD_SETTINGS), rustVersion=RUST_VERSION)
        for key, value in expected.items():
            _require(type(identity.get(key)) is type(value) and identity[key] == value,
                     'Identity pin mismatch: ' + pin.role + '/' + key)
        _require(_historical_path(identity['binary']) == old_root / 'runtime' / pin.role / 'seekdb',
                 'Original identity role/path mismatch')
        inventory = identity['officialInputHashes']
        _require(isinstance(inventory, dict) and len(inventory) == pins.inventory_count, 'Official input inventory count mismatch')
        for name, value in inventory.items():
            _safe_name(name)
            _hex(value, 64, 'official input digest')
        _require(_sha(json.dumps(inventory, sort_keys=True, separators=(',', ':')).encode('utf-8')) == pins.inventory_sha256,
                 'Official input inventory digest mismatch')
        loader = identity['loader']
        loader_fields = {'command', 'timeoutSeconds', 'exitCode', 'childrenCleaned', 'output', 'outputSha256'}
        _require(isinstance(loader, dict) and set(loader) == loader_fields
                 and loader['command'] == ['ldd', identity['binary']] and type(loader['exitCode']) is int
                 and loader['exitCode'] == 0 and loader['childrenCleaned'] is True
                 and type(loader['timeoutSeconds']) is int and loader['timeoutSeconds'] == 30
                 and loader['output'] == str(old_root / 'evidence' / pin.role / 'seekdb-ldd.log'),
                 'Historical loader metadata mismatch (not a current loader check)')
        _hex(loader['outputSha256'], 64, 'historical loader output digest')


def _read_build_id(binary, output):
    """Only trusted system readelf reads bytes; the preserved ELF is never run."""
    def limit_output():
        # A limit in this short-lived child only, never a host security setting.
        resource.setrlimit(resource.RLIMIT_FSIZE, (NOTE_CAP, NOTE_CAP))

    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as log:
        result = subprocess.run(['/usr/bin/readelf', '-n', '--', str(binary)], stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, timeout=30, check=False,
                                preexec_fn=limit_output,
                                env={'LC_ALL': 'C', 'PATH': '/usr/bin:/bin'})
    _require(result.returncode == 0 and output.stat().st_size <= NOTE_CAP, 'Static readelf failed/exceeded note cap')
    text = output.read_bytes().decode('utf-8', errors='strict')
    ids = re.findall(r'Build ID:\s*([0-9a-f]+)', text)
    _require(len(ids) == 1, 'Static readelf BuildID missing/ambiguous')
    return ids[0]


def _same_file(before, after):
    return all(getattr(before, key) == getattr(after, key) for key in
               ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))


def _prepare_verified_runtime(artifact_zip, runtime_root, pins):
    """Private synthetic-fixture seam. Production callers must use the public API."""
    artifact_zip, root = _checked_path(artifact_zip), _checked_path(runtime_root)
    _require(not root.exists() and not root.is_symlink() and root.parent.is_dir(),
             'Runtime requires a fresh path with an existing parent')
    _require(artifact_zip != root and root not in artifact_zip.parents, 'Runtime/artifact paths overlap')
    owned, binaries = False, []
    try:
        fd = os.open(artifact_zip, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, 'Artifact is not a unique ordinary file')
            count, digest, _, _ = _stream_digest(source, pins.zip_bytes)
            _require(count == pins.zip_bytes and digest == pins.zip_sha256, 'Pinned outer ZIP size/SHA256 mismatch')
            _require(_same_file(before, os.fstat(source.fileno())), 'Artifact changed during ZIP verification')
            source.seek(0)
            with zipfile.ZipFile(source) as archive:
                members = _zip_members(archive)
                with archive.open(members['manifest.json']) as stream:
                    raw_manifest = stream.read(MANIFEST_CAP + 1)
                manifest, claims, records = _manifest(raw_manifest, members, pins)
                # All three complete compressed archives are hashed before any tar
                # parser or runtime write. Outer ZIP identity alone is not ELF proof.
                for name in ARCHIVES:
                    with archive.open(members[name]) as stream:
                        size, digest, _, _ = _stream_digest(stream, claims[name]['bytes'])
                    _require(size == claims[name]['bytes'] and digest == claims[name]['sha256'], 'Archive bytes/SHA256 mismatch: ' + name)
                root.mkdir(mode=0o700)
                owned = True
                for role in ROLES:
                    (root / role).mkdir(mode=0o700)
                _new_file(root / 'manifest.original.json', raw_manifest)
                selected, original_raw = {}, {}

                def evidence(name, size, stream):
                    _require(name in records and 'role' not in records[name] and 'error' not in records[name],
                             'Unexpected evidence tar member: ' + name)
                    record = records[name]
                    _require(size == record['bytes'] and size <= JSON_CAP, 'Evidence member size mismatch')
                    essential = name in ('plan.json', 'runtime/baseline/identity.json', 'runtime/candidate/identity.json')
                    if essential:
                        data = _read_exact(stream, size)
                        _require(_sha(data) == record['sha256'], 'Essential JSON digest mismatch')
                        original_raw[name] = data
                        destination = root / 'plan.original.json' if name == 'plan.json' else root / name.split('/')[1] / 'identity.original.json'
                        _new_file(destination, data)
                        selected[name] = _strict_json(data, name)
                    else:
                        count, digest, _, _ = _stream_digest(stream, size)
                        _require(count == size and digest == record['sha256'], 'Evidence member digest mismatch')

                with archive.open(members['evidence.tar.gz']) as stream:
                    seen = _walk_tar(stream, EVIDENCE_PAYLOAD_CAP, ENTRY_CAP, evidence)
                expected_evidence = {name for name, record in records.items() if 'role' not in record and 'error' not in record}
                _require(seen == expected_evidence, 'Evidence tar/manifest inventory mismatch')
                plan = selected['plan.json']
                identities = {role: selected['runtime/' + role + '/identity.json'] for role in ROLES}
                _validate_metadata(plan, identities, pins)
                results = {}
                for pin in pins.roles:
                    binary = root / pin.role / 'seekdb'

                    def elf(name, size, stream):
                        _require(name == pin.role + '/seekdb' and size == pin.byte_count, 'ELF tar role/size mismatch')
                        output_fd = os.open(binary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
                        binaries.append(binary)
                        with os.fdopen(output_fd, 'wb') as output:
                            count, digest, magic, embedded = _stream_digest(stream, pin.byte_count, output, pin.source_commit.encode('ascii'))
                        _require(count == pin.byte_count and digest == pin.sha256 and magic == b'\x7fELF' and embedded,
                                 'ELF full bytes/hash/magic/embedded source mismatch: ' + pin.role)

                    with archive.open(members[pin.role + '-elf.tar.gz']) as stream:
                        names = _walk_tar(stream, pin.byte_count, 1, elf)
                    _require(names == {pin.role + '/seekdb'}, 'Missing expected role ELF')
                    results[pin.role] = {'binary': str(binary), 'identity': identities[pin.role],
                        'identityOriginalPath': str(binary.parent / 'identity.original.json')}
                # Neither role becomes executable until both independent files and
                # both original metadata records have passed the complete checks.
                for pin in pins.roles:
                    binary = root / pin.role / 'seekdb'
                    build_id = _read_build_id(binary, binary.parent / 'readelf-notes.log')
                    _require(build_id == pin.build_id, 'Actual static ELF BuildID mismatch: ' + pin.role)
                    provenance = dict(role=pin.role, originalBinaryPath=identities[pin.role]['binary'],
                        originalIdentitySha256=_sha(original_raw['runtime/' + pin.role + '/identity.json']),
                        runtimeBinaryPath=str(binary), sourceCommit=pin.source_commit, sourceTree=pin.source_tree,
                        kernelOrigin=pin.kernel_origin, buildDriverCommit=pins.driver_commit,
                        bytes=pin.byte_count, sha256=pin.sha256, buildId=build_id,
                        generatedVersionSha256=pin.generated_version_sha256,
                        artifactId=13621468966, runId=42486120779, runAttempt=1,
                        artifactZipSha256=pins.zip_sha256, staticIdentityVerified=True,
                        actualValidationDriver='UNASSIGNED', previousValidationAccepted=False)
                    _new_file(binary.parent / 'relocation.json', _json_bytes(provenance))
                    results[pin.role].update(provenance=provenance, relocationPath=str(binary.parent / 'relocation.json'))
                _require(_same_file(before, os.fstat(source.fileno())), 'Artifact changed during materialization')
        report = dict(status='verified_runtime_prepared', runtimeRoot=str(root), roles=results,
                      artifact=dict(repository='beifeng-h/nailong-seekdb', repositoryId=1431820979,
                                    artifactId=13621468966, runId=42486120779, runAttempt=1,
                                    headSha=pins.driver_commit, bytes=pins.zip_bytes, sha256=pins.zip_sha256,
                                    expiresAt='2026-11-11T14:51:18Z'),
                      originalManifest=manifest, originalPlan=plan, oldValidationAccepted=False,
                      currentValidationPerformed=False, verificationPath=str(root / 'verification.json'))
        _new_file(root / '.verification.pending', _json_bytes(report))
        for binary in binaries:
            _require(binary.lstat().st_nlink == 1 and stat.S_ISREG(binary.lstat().st_mode), 'Owned ELF changed type/link count')
            binary.chmod(0o555)
        # A new owned directory and exclusive pending file prevent overwrites.
        _require(not (root / 'verification.json').exists(), 'Existing verification record rejected')
        (root / '.verification.pending').rename(root / 'verification.json')
        return report
    except Exception as error:
        if owned:
            for binary in binaries:
                try:
                    if stat.S_ISREG(binary.lstat().st_mode) and not binary.is_symlink():
                        binary.chmod(0o444)
                except OSError:
                    pass
            try:
                _new_file(root / 'verification.failed.json', _json_bytes(dict(
                    status='verification_failed', reason=str(error)[:2048], executableReuseApproved=False)))
            except OSError:
                pass
        if isinstance(error, VerificationError):
            raise
        raise VerificationError('Artifact verification failed: ' + str(error)) from error


def prepare_verified_runtime(artifact_zip: Path, runtime_root: Path) -> dict:
    """Prepare ONLY artifact 13621468966, run 42486120779/1, built at bc4dde62.

    Both arguments are consumer-local paths. runtime_root's parent must already
    exist, and runtime_root must be fresh. Returned roles[role]['binary'] is the
    new owned executable path. roles[role]['identity'] is ORIGINAL metadata with
    its historical binary path, not rewritten metadata for a new driver. See
    relocation.json for explicit path binding. Exceptions leave bounded evidence
    and no successful verification record. No SQL/loader/runtime test is done.
    """
    return _prepare_verified_runtime(artifact_zip, runtime_root, _PRODUCTION_PINS)
