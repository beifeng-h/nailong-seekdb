#!/usr/bin/env python3
"""Bounded collector for owned paired FTS evidence; never searches runner home.

Output must be fresh and disjoint. Full ELF bytes are independently archived;
logs may be tailed with explicit manifest accounting, JSON is never truncated.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tarfile

MIB = 1024 * 1024
BINARY_ARCHIVE_CAP = 416 * MIB
BINARY_RAW_CAP = 4096 * MIB
LOG_ARCHIVE_CAP = 60 * MIB
LOG_TOTAL_CAP = 56 * MIB
LOG_FILE_CAP = 8 * MIB
MAX_ENTRIES = 4000
MANIFEST_CAP = 4 * MIB
BASELINE = '5bce9e06d1281347ad61344e4b01fab9158c58a2'
KERNEL = '8db524bb0a3a847b4eb384232bd576761e4075b3'
EXTENSIONS = {'.json', '.log', '.txt', '.sha256', '.sql', '.reject', '.result'}
FORBIDDEN = re.compile(r'(^|[._-])(env|config|configuration|credential|credentials|secret|secrets|token|password|core|database|datafile)([._-]|$)', re.I)


def owned_db_log(path, root):
    parts = path.relative_to(root).parts
    return (parts[:2] in (('validation', 'cases'), ('validation', 'rounds'))
            and path.parent.name == 'db-log-tails'
            and re.fullmatch(r'seekdb\.log(?:\.wf)?(?:\.[0-9]+)?(?:\.wf)?', path.name) is not None)


def escaped_db_diagnostic(raw, budget):
    """Escape sparse encoding damage, not arbitrary binary, within output caps.

    Literal backslashes are doubled; invalid bytes become backslash-xNN text. The caller
    records the selected raw bytes separately from the diagnostic representation.
    """
    text = raw.decode('utf-8', errors='surrogateescape')
    invalid = sum(0xdc80 <= ord(ch) <= 0xdcff for ch in text)
    if (len(raw) < 100 or invalid > min(4096, len(raw) // 100)
            or any(ord(ch) < 32 and ch not in '\t\r\n' or ord(ch) == 127 for ch in text)):
        raise ValueError('DB log is not predominantly text; refusing binary conversion')

    def render(value):
        value = value.decode('utf-8', errors='surrogateescape').replace('\\', '\\\\')
        return re.sub('[\udc80-\udcff]', lambda m: '\\x%02x' % (ord(m[0]) - 0xdc00), value).encode('utf-8')

    encoded = render(raw)
    if len(encoded) > budget:
        # Each raw byte expands to at most four ASCII bytes. Preserve a bounded
        # suffix, never cut a diagnostic escape or silently exceed either cap.
        raw = raw[-(budget // 4):] if budget >= 4 else b''
        encoded = render(raw)
    return raw, encoded


def checked_path(path):
    """Reject links/reparse points in every component before resolving."""
    path = Path(os.path.abspath(path))
    for item in reversed((path,) + tuple(path.parents)):
        if item.exists() or item.is_symlink():
            st = item.lstat()
            if stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Symlink/reparse point rejected: ' + str(item))
    return path


def regular(path):
    checked_path(path)
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise ValueError('Not a unique regular file: ' + str(path))
    return st


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(MIB), b''):
            h.update(block)
    return h.hexdigest()


class LimitedWriter:
    def __init__(self, stream, cap):
        self.stream, self.cap, self.count = stream, cap, 0

    def write(self, data):
        if self.count + len(data) > self.cap:
            raise ValueError('Compressed archive cap exceeded')
        n = self.stream.write(data)
        self.count += n
        return n

    def flush(self):
        self.stream.flush()


def archive(path, cap, populate):
    created = False
    try:
        with path.open('xb') as f:
            created = True
            with gzip.GzipFile(filename='', fileobj=LimitedWriter(f, cap), mode='wb', mtime=0) as z:
                with tarfile.open(fileobj=z, mode='w|') as tar:
                    populate(tar)
        return {'path': path.name, 'bytes': path.stat().st_size, 'sha256': digest(path)}
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise


def add_bytes(tar, name, data, mode=0o644):
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(data), mode
    tar.addfile(info, io.BytesIO(data))


def collect(root, out):
    root, out = checked_path(root), checked_path(out)
    if root == out or root in out.parents or out in root.parents:
        raise ValueError('Input and output must be disjoint')
    if not root.is_dir():
        raise ValueError('Owned input root is absent')
    out.mkdir(parents=True, exist_ok=False)
    manifest = {'scope': 'OWNED_PAIRED_FTS_ONLY', 'binaryPolicy': 'FULL_BYTES_NO_STRIP_NO_TRUNCATION',
                'binaryArchiveCapBytes': BINARY_ARCHIVE_CAP, 'binaryRawCapBytes': BINARY_RAW_CAP,
                'logArchiveCapBytes': LOG_ARCHIVE_CAP, 'logPayloadCapBytes': LOG_TOTAL_CAP,
                'logFileCapBytes': LOG_FILE_CAP, 'manifestCapBytes': MANIFEST_CAP,
                'maximumCombinedCompressedBytes': 2 * BINARY_ARCHIVE_CAP + LOG_ARCHIVE_CAP + MANIFEST_CAP, 'archives': [], 'files': [], 'errors': []}
    evidence = []

    def error(label, exc):
        manifest['errors'].append((label + ': ' + str(exc))[:2048])

    plan = {}
    try:
        plan_path = root / 'plan.json'
        if regular(plan_path).st_size > LOG_FILE_CAP:
            raise ValueError('Plan exceeds file cap')
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
        if not isinstance(plan, dict) or not re.fullmatch('[0-9a-f]{40}', str(plan.get('driverCommit', ''))):
            raise ValueError('Plan driver commit invalid')
        if plan.get('baselineCommit') != BASELINE or plan.get('kernelOrigin') != KERNEL:
            raise ValueError('Plan pinned baseline/kernel mismatch')
    except (OSError, ValueError, TypeError) as exc:
        error('plan', exc)
        plan = {}

    # Both roles are attempted even when one build failed.
    for role in ('baseline', 'candidate'):
        binary = root / 'runtime' / role / 'seekdb'
        ident = binary.with_name('identity.json')
        try:
            st = regular(binary)
            if st.st_size > BINARY_RAW_CAP:
                raise ValueError('ELF exceeds raw cap')
            with binary.open('rb') as f:
                if f.read(4) != b'\x7fELF':
                    raise ValueError('Not an ELF file')
            if regular(ident).st_size > LOG_FILE_CAP:
                raise ValueError('Identity exceeds file cap')
            identity = json.loads(ident.read_text(encoding='utf-8'))
            for key in ('sourceCommit', 'sourceTree', 'kernelOrigin'):
                if not re.fullmatch('[0-9a-f]{40}', str(identity.get(key, ''))):
                    raise ValueError('Invalid identity ' + key)
            if identity.get('role') != role or not Path(identity.get('binary', '')).is_absolute() or checked_path(identity['binary']) != binary:
                raise ValueError('Identity role/binary mismatch')
            if not plan or identity.get('driverCommit') != plan['driverCommit']:
                raise ValueError('Identity driver commit mismatch')
            if role == 'baseline' and identity['kernelOrigin'] != BASELINE:
                raise ValueError('Baseline kernel origin mismatch')
            if role == 'candidate' and identity['sourceCommit'] != plan['driverCommit']:
                raise ValueError('Candidate source commit mismatch')
            if role == 'baseline' and identity['sourceCommit'] != BASELINE:
                raise ValueError('Baseline source commit mismatch')
            if role == 'candidate' and identity['kernelOrigin'] != KERNEL:
                raise ValueError('Candidate kernel origin mismatch')
            sha = digest(binary)
            if identity.get('bytes') != st.st_size or identity.get('sha256') != sha:
                raise ValueError('Identity bytes/SHA256 mismatch')

            def add_elf(tar):
                info = tarfile.TarInfo(role + '/seekdb')
                info.size, info.mode = st.st_size, 0o755
                with binary.open('rb') as f:
                    tar.addfile(info, f)
                if regular(binary).st_size != st.st_size or digest(binary) != sha:
                    raise ValueError('ELF changed during collection')

            manifest['archives'].append(archive(out / (role + '-elf.tar.gz'), BINARY_ARCHIVE_CAP, add_elf))
            manifest['files'].append({'path': binary.relative_to(root).as_posix(), 'bytes': st.st_size,
                                      'sha256': sha, 'truncated': False, 'role': role,
                                      'sourceCommit': identity['sourceCommit'], 'buildId': identity.get('buildId')})
        except (OSError, ValueError, TypeError, KeyError, tarfile.TarError) as exc:
            error(role, exc)

    def consider(path):
        relative = path.relative_to(root).as_posix()
        try:
            st = regular(path)
            parts = path.relative_to(root).parts
            helper_text = parts[:2] in (('validation', 'cases'), ('validation', 'rounds')) and (
                (path.parent.name == 'mysqltest-log' and path.name == 'timer') or
                owned_db_log(path, root))
            if (path.suffix.lower() not in EXTENSIONS and not helper_text) or any(FORBIDDEN.search(p) for p in parts):
                raise ValueError('Not an allowed owned evidence name')
            evidence.append((path, st.st_size))
        except (OSError, ValueError) as exc:
            error(relative, exc)

    for name in ('plan.json', 'report.json', 'runtime/baseline/identity.json', 'runtime/candidate/identity.json'):
        consider(root / name)
    # Walk only explicit evidence subtrees, never runtime/database/source trees.
    count = 0
    for name in ('evidence/baseline', 'evidence/candidate', 'validation/cases', 'validation/rounds'):
        base = root / name
        try:
            checked_path(base)
            if not base.is_dir():
                raise ValueError('Owned evidence directory absent')
            for directory, dirs, files in os.walk(base, followlinks=False):
                for child in list(dirs):
                    if child == 'tmp' and name.startswith('validation/'):
                        dirs.remove(child)
                        continue
                    p = Path(directory) / child
                    count += 1
                    if count > MAX_ENTRIES:
                        raise ValueError('Evidence entry count exceeds cap')
                    try:
                        checked_path(p)
                        if FORBIDDEN.search(child):
                            raise ValueError('Forbidden evidence directory')
                    except ValueError as exc:
                        dirs.remove(child)
                        error(p.relative_to(root).as_posix(), exc)
                for child in files:
                    count += 1
                    if count > MAX_ENTRIES:
                        raise ValueError('Evidence entry count exceeds cap')
                    consider(Path(directory) / child)
                if count > MAX_ENTRIES:
                    raise ValueError('Evidence entry count exceeds cap')
        except (OSError, ValueError) as exc:
            error(name, exc)

    def priority(item):
        p = item[0]
        return (0 if p.suffix.lower() == '.json' else 1 if any(s in p.name.lower() for s in ('explain', 'benchmark', 'result', 'score')) else 2, str(p))

    def add_evidence(tar):
        total = 0
        for path, original in sorted(evidence, key=priority):
            name = path.relative_to(root).as_posix()
            entry = {'path': name, 'originalBytes': original, 'bytes': 0, 'truncated': False}
            manifest['files'].append(entry)
            try:
                regular(path)
                size = min(original, LOG_FILE_CAP, max(0, LOG_TOTAL_CAP - total))
                if path.suffix.lower() == '.json' and size != original:
                    raise ValueError('Essential JSON cannot be truncated or dropped')
                with path.open('rb') as f:
                    head = f.read(min(original, 4096))
                    if b'\x00' in head or head.startswith(b'\x7fELF'):
                        raise ValueError('Unexpected binary evidence')
                    f.seek(original - size)
                    data = f.read(size)
                    if len(data) != size:
                        raise ValueError('Evidence changed during collection')
                # ELF/NUL protections apply before any narrow text conversion.
                if b'\x00' in data or data.startswith(b'\x7fELF'):
                    raise ValueError('Unexpected binary evidence')
                raw_selected = data
                if path.suffix.lower() != '.json' and not owned_db_log(path, root):
                    # Our tail or an upstream bounded log can start inside a code point.
                    while data and data[0] & 0xc0 == 0x80:
                        data = data[1:]
                        size -= 1
                try:
                    data.decode('utf-8')
                except UnicodeDecodeError:
                    if not owned_db_log(path, root):
                        raise
                    selected, data = escaped_db_diagnostic(raw_selected, min(LOG_FILE_CAP, max(0, LOG_TOTAL_CAP - total)))
                    size = len(selected)
                    entry.update(converted=True, representation='UTF8_DIAGNOSTIC_BACKSLASH_ESCAPES',
                                 inputTailBytes=len(raw_selected),
                                 inputTailSha256=hashlib.sha256(raw_selected).hexdigest(),
                                 conversion='Literal backslashes doubled; invalid bytes escaped as backslash-xNN',
                                 diagnosticLoss='Raw byte representation changed; any omitted prefix is recorded by tailOffset',
                                 selectedRawBytes=size, selectedRawSha256=hashlib.sha256(selected).hexdigest(),
                                 escapedInvalidByteCount=sum(0xdc80 <= ord(ch) <= 0xdcff
                                     for ch in selected.decode('utf-8', errors='surrogateescape')))
                    if len(raw_selected) == original:
                        entry['originalSha256'] = entry['inputTailSha256']
                if path.suffix.lower() == '.json':
                    json.loads(data)
                if regular(path).st_size != original:
                    raise ValueError('Evidence changed during collection')
                entry.update(bytes=len(data), truncated=size != original, tailOffset=original - size,
                             sha256=hashlib.sha256(data).hexdigest())
                if data or original == 0:
                    add_bytes(tar, name, data)
                else:
                    entry['omitted'] = 'Shared evidence payload cap exhausted'
                total += len(data)
            except (OSError, ValueError, UnicodeError) as exc:
                entry['error'] = str(exc)
                error(name, exc)
        manifest['logPayloadBytes'] = total

    try:
        manifest['archives'].append(archive(out / 'evidence.tar.gz', LOG_ARCHIVE_CAP, add_evidence))
    except (OSError, ValueError, tarfile.TarError) as exc:
        error('evidence archive', exc)
    manifest['ok'] = not manifest['errors']
    encoded = (json.dumps(manifest, indent=2) + '\n').encode('utf-8')
    if len(encoded) > MANIFEST_CAP:
        # Never silently exceed the final artifact budget, even on hostile names.
        manifest['ok'] = False
        manifest['omittedFileRecords'] = len(manifest['files'])
        manifest['files'] = []
        error('manifest', 'Manifest cap exceeded; detailed file records omitted')
        encoded = (json.dumps(manifest, indent=2) + '\n').encode('utf-8')
        if len(encoded) > MANIFEST_CAP:
            manifest['omittedErrorRecords'] = len(manifest['errors'])
            manifest['errors'] = ['Manifest cap exceeded; error details omitted', manifest['errors'][0][:256]]
            encoded = (json.dumps(manifest) + '\n').encode('utf-8')
    (out / 'manifest.json').write_bytes(encoded)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = collect(args.root, args.out)
        print(json.dumps({'ok': manifest['ok'], 'archives': len(manifest['archives']),
                          'errors': manifest['errors'], 'manifest': str(args.out / 'manifest.json')}))
        return 0 if manifest['ok'] else 1
    except (OSError, ValueError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
