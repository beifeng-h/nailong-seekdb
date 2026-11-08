#!/usr/bin/env python3
"""Collect only candidate ELF and explicitly owned validation evidence, with caps.

No runner home, environment dump, credentials, DB datafiles, core dumps, or
unrelated files are eligible. ELF bytes are never stripped or truncated.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile

MIB = 1024 * 1024
BINARY_ARCHIVE_CAP = 416 * MIB
LOG_ARCHIVE_CAP = 60 * MIB
LOG_TOTAL_CAP = 56 * MIB
LOG_FILE_CAP = 8 * MIB
BINARY_RAW_CAP = 4096 * MIB
BUILD_EVIDENCE = frozenset(('checkout-sha.txt', 'configure.log', 'configuration.txt',
    'build.log', 'native-fts-validation.log', 'dependency-source-before.sha256',
    'expected-clients.sha256', 'obclient-ldd.txt', 'mysqltest-ldd.txt',
    'obclient-version.txt', 'mysqltest-version.txt', 'binaries.txt',
    'binaries.sha256', 'binary-sizes.txt', 'rust-versions.txt', 'seekdb-ldd.txt'))


class LimitedWriter:
    def __init__(self, stream, cap):
        self.stream, self.cap, self.count = stream, cap, 0

    def write(self, data):
        if self.count + len(data) > self.cap:
            raise RuntimeError('Compressed artifact exceeds the explicit storage cap')
        count = self.stream.write(data)
        self.count += count
        return count

    def flush(self):
        self.stream.flush()


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def archive(path, cap, add):
    try:
        with path.open('xb') as raw:
            limit = LimitedWriter(raw, cap)
            with gzip.GzipFile(filename='', fileobj=limit, mode='wb', compresslevel=6, mtime=0) as zipped:
                with tarfile.open(fileobj=zipped, mode='w|') as tar:
                    add(tar)
        return dict(path=path.name, bytes=path.stat().st_size, sha256=sha(path))
    except Exception:
        # Only remove this collector's newly created, incomplete archive.
        path.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--validation', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.source = args.source.resolve(strict=True)
    for name in ('evidence', 'validation', 'out'):
        value = getattr(args, name)
        if value.is_symlink():
            raise RuntimeError('Collector root must not be a symlink')
        setattr(args, name, value.resolve(strict=False))
    roots = (args.source, args.evidence, args.validation, args.out)
    if len(set(roots)) != len(roots) or any(a in b.parents for a in roots for b in roots if a != b):
        raise RuntimeError('Source, owned evidence, validation and output roots must be disjoint')
    args.out.mkdir(parents=True, exist_ok=False)
    manifest = dict(scope='EXACT_CANDIDATE_ELF_AND_OWNED_FTS_EVIDENCE_ONLY',
        maximumCombinedCompressedBytes=476 * MIB, binaryPolicy='FULL_BYTES_NO_STRIP_NO_TRUNCATION',
        binaryArchiveCapBytes=BINARY_ARCHIVE_CAP, logArchiveCapBytes=LOG_ARCHIVE_CAP,
        files=[], errors=[], archives=[])
    # Official seekdb links oceanbase_static. liboceanbase.so is a separate
    # pretest-only EXCLUDE_FROM_ALL target, not a runtime dependency or part of
    # this artifact contract. Do not copy an unused SO merely because it exists.
    binaries = [args.source / 'build_release/src/observer/seekdb']
    manifest['requiredBuildArtifacts'] = ['build_release/src/observer/seekdb']
    manifest['runtimeDependencyEvidence'] = 'build-evidence/seekdb-ldd.txt (actual ldd; missing dependencies fail validation)'
    try:
        if not all(path.is_file() and not path.is_symlink() for path in binaries):
            raise RuntimeError('Required candidate seekdb ELF absent; build may have failed')
        if sum(path.stat().st_size for path in binaries) > BINARY_RAW_CAP:
            raise RuntimeError('Candidate seekdb ELF exceeds 4 GiB raw admission limit')
        for path in binaries:
            with path.open('rb') as stream:
                if stream.read(4) != b'\x7fELF':
                    raise RuntimeError('Candidate artifact is not ELF: ' + path.name)
            manifest['files'].append(dict(path=path.relative_to(args.source).as_posix(),
                bytes=path.stat().st_size, sha256=sha(path), truncated=False))
        if shutil.disk_usage(args.out).free < BINARY_ARCHIVE_CAP + LOG_ARCHIVE_CAP + 128 * MIB:
            raise RuntimeError('Insufficient free disk for bounded artifact staging')

        def add_binaries(tar):
            for path in binaries:
                tar.add(path, arcname=path.relative_to(args.source).as_posix(), recursive=False)
        manifest['archives'].append(archive(args.out / 'candidate-elf.tar.gz', BINARY_ARCHIVE_CAP, add_binaries))
    except Exception as error:
        manifest['errors'].append(str(error))

    def add_logs(tar):
        remaining = LOG_TOTAL_CAP
        for label, root in (('build-evidence', args.evidence), ('fts-validation', args.validation)):
            if not root.is_dir():
                continue
            # Preserve verdicts and SQL/benchmark output before optional DB log tails.
            ordered = sorted(root.rglob('*'), key=lambda path: (
                'db-log-tails' in path.relative_to(root).parts,
                path.suffix != '.json', path.as_posix()))
            for path in ordered:
                relative = path.relative_to(root)
                if label == 'build-evidence' and relative.as_posix() not in BUILD_EVIDENCE:
                    continue
                if (path.is_symlink() or not path.is_file() or 'database' in relative.parts
                        or path.suffix not in ('.json', '.jsonl', '.log', '.txt', '.sha256', '.sql', '.reject', '.result')
                        or any(parent.is_symlink() for parent in path.parents if parent != root)):
                    continue
                size = path.stat().st_size
                take = min(size, LOG_FILE_CAP, remaining)
                row = dict(path=label + '/' + relative.as_posix(), bytes=size,
                           savedBytes=take, truncated=take != size)
                if take:
                    with path.open('rb') as stream:
                        stream.seek(size - take)
                        data = stream.read(take)
                    info = tarfile.TarInfo(row['path'])
                    info.size, info.mode = len(data), 0o644
                    tar.addfile(info, io.BytesIO(data))
                    row['savedSha256'] = hashlib.sha256(data).hexdigest()
                    remaining -= len(data)
                manifest['files'].append(row)
    try:
        manifest['archives'].append(archive(args.out / 'fts-logs.tar.gz', LOG_ARCHIVE_CAP, add_logs))
    except Exception as error:
        manifest['errors'].append(str(error))
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(manifest, indent=2))
    return 1 if manifest['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
