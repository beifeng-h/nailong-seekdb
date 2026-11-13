#!/usr/bin/env python3
"""Resume native/ABBA validation of the exact c3d7988 ELF without rebuilding."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tarfile
import traceback
import zipfile

import fts_encoded_sort_validation as validation
import fts_expr_cache_targeted as targeted

driver, common = validation.driver, validation.common
CANDIDATE = 'c3d798873b700987b6983043d2abf0defd8e2500'
TREE = '1377b9b22a2f5c3301579647b84507c076bed98e'
ZIP_BYTES = 329610377
ZIP_SHA = '346dc72b8784606d2c7d8c2ff9d5e0a15addbf4626461d98b601d8fcb7509480'
ELF_BYTES = 1332420904
ELF_SHA = '61720b07ee1c34fdac0c4a8a67172e5729ad2ad08eacdf74c3014e5a96584c9f'
BUILD_ID = '2161c86c66582589b65323bd3a53ece1282e21b4'
EVIDENCE_BYTES = 750602
EVIDENCE_SHA = '091abaa7940f79d7c6f396a22bff20ac70b8a925583b8dd5cf35412caaaa0bd1'
ALLOWED_CHANGES = {
    '.github/script/seekdb/fts_encoded_sort_revalidation.py',
    '.github/script/seekdb/test_fts_encoded_sort_revalidation.py',
    '.github/workflows/fts-encoded-sort-revalidation.yml',
    '.github/workflows/fts-profile-diagnostics.yml',
}


def supported_ddl_sql():
    statements = ['ALTER SYSTEM SET _enable_add_fulltext_index_to_existing_table=true;',
                  'CREATE DATABASE fts_sort_ddl;']
    for suffix, collation in (('ci', 'utf8mb4_general_ci'), ('bin', 'utf8mb4_bin')):
        table = 'fts_sort_ddl.docs_' + suffix
        statements.append(f'CREATE TABLE {table} (id BIGINT PRIMARY KEY, body TEXT) '
                          f'CHARSET=utf8mb4 COLLATE={collation};')
        values = ','.join(f"({i},'{('database audit' if i % 2 == 0 else 'orchard zebra')}')"
                          for i in range(1, 65))
        statements.extend((f'INSERT INTO {table} VALUES {values};',
            f'ALTER TABLE {table} ADD FULLTEXT INDEX ft_{suffix}(body) WITH PARSER beng;',
            f"SELECT COUNT(*) FROM {table} WHERE MATCH(body) AGAINST('database');",
            f'DELETE FROM {table} WHERE id=2;',
            f"UPDATE {table} SET body='orchard zebra' WHERE id=4;",
            f"INSERT INTO {table} VALUES (101,'database audit');",
            f"SELECT COUNT(*) FROM {table} WHERE MATCH(body) AGAINST('database');"))
    return '\n'.join(statements)


def unsupported_charset_control(call, out):
    text = common.SESSION + 'CREATE TABLE fts_sort_ddl.unsupported_latin1 (id INT) CHARSET=latin1;\n'
    (out / 'unsupported-latin1.sql').write_text(text)
    log = out / 'unsupported-latin1.log'
    result = common.capture_group([str(call.obclient), '-h127.0.0.1', '-P' + str(call.port),
        '-uroot', '-A', '-N', '-B', '--raw', '--default-character-set=utf8mb4', '-Doceanbase'],
        log, Path.cwd(), 30, env=os.environ.copy(), stdin=text.encode())
    output = log.read_text(encoding='utf-8')
    driver.require(result['exitCode'] == 1 and result['childrenCleaned']
                   and re.fullmatch(r"ERROR 1115 \(42000\) at line \d+: Unknown character set: 'latin1'\n", output),
                   'Expected exact inherited unsupported-latin1 error')
    driver.dump(out / 'unsupported-latin1.json', dict(expectedError=1115, **result))
    return output


def corrected_equality_work(call, out):
    raw = driver.sql(call, out, 'encoded_sort-equality', validation.REGRESSION_SQL)
    rows = [line.split('\t') for line in raw.splitlines()]
    labels = re.findall(r"^SELECT '([^']+)'", validation.REGRESSION_SQL, re.M)
    driver.require(len(rows) == len(labels) and [row[0] for row in rows] == labels
                   and all(len(row) == 2 for row in rows), 'Incomplete collation SQL parity output')
    ddl = driver.sql(call, out, 'fts-sort-ddl', supported_ddl_sql())
    driver.require(ddl.splitlines() == ['32', '31'] * 2, 'FTS build and DML count mismatch')
    rejected = unsupported_charset_control(call, out)
    output = (raw + ddl + rejected).encode()
    return dict(passed=True, canonical=output.hex(), outputBytes=len(output),
                outputSha256=hashlib.sha256(output).hexdigest(), supportedDdlCharsets=2,
                inheritedUnsupportedCharsetError=1115,
                sqlSha256=hashlib.sha256(validation.REGRESSION_SQL.encode()).hexdigest(),
                executedSqlSha256=common.digest(out / 'encoded_sort-equality.sql'))


def verify_driver(args):
    identity = common.source_identity(args.source, args.driver_sha)
    changed = set(common.git(args.source, 'diff', '--name-only', CANDIDATE, args.driver_sha, '--').decode().splitlines())
    driver.require(changed <= ALLOWED_CHANGES, 'Revalidation changes exceed the explicit driver/workflow scope')
    driver.require(common.git(args.source, 'rev-parse', CANDIDATE + '^{tree}').decode().strip() == TREE,
                   'Frozen c3d7988 source tree differs')
    return dict(driverCommit=args.driver_sha, driverTree=identity['tree'], changed=sorted(changed),
                candidateCommit=CANDIDATE, candidateTree=TREE, newBuilds=0,
                originalInputHashes=driver.inventory(args.source))


def extract_candidate(archive_path, args, hashes):
    driver.require(archive_path.stat().st_size == ZIP_BYTES and common.digest(archive_path) == ZIP_SHA,
                   'Exact complete candidate ZIP differs')
    with zipfile.ZipFile(archive_path) as archive:
        driver.require(len(archive.namelist()) == 3 and set(archive.namelist()) == {
            'candidate-artifact-manifest.json', 'candidate-identity.json', 'candidate-elf.tar.gz'},
            'Candidate ZIP inventory differs')
        raw_identity = archive.read('candidate-identity.json')
        identity = json.loads(raw_identity)
        manifest = json.loads(archive.read('candidate-artifact-manifest.json'))
        driver.require(manifest['identity'] == identity and manifest['fullBytes']
                       and not manifest['stripped'] and not manifest['truncated'], 'Incomplete archived candidate')
        driver.require(identity['sourceCommit'] == identity['kernelOrigin'] == CANDIDATE
                       and identity['sourceTree'] == identity['kernelTree'] == TREE
                       and identity['bytes'] == ELF_BYTES and identity['sha256'] == ELF_SHA
                       and identity['buildId'] == BUILD_ID and identity['officialInputHashes'] == hashes,
                       'Candidate ELF identity differs')
        payload = archive.read('candidate-elf.tar.gz')
        driver.require(len(payload) == manifest['archive']['bytes']
                       and hashlib.sha256(payload).hexdigest() == manifest['archive']['sha256'], 'Candidate TAR differs')
    binary = args.root / 'runtime/candidate/seekdb'
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
        members = archive.getmembers()
        driver.require(len(members) == 1 and members[0].isfile() and members[0].name == 'candidate/seekdb'
                       and members[0].size == ELF_BYTES, 'Expected one complete regular candidate ELF')
        with archive.extractfile(members[0]) as incoming, binary.open('xb') as outgoing:
            shutil.copyfileobj(incoming, outgoing)
    binary.chmod(0o555)
    driver.safe_elf(binary, ELF_SHA, ELF_BYTES, BUILD_ID, CANDIDATE)
    (args.root / 'evidence/candidate/archived-identity.json').write_bytes(raw_identity)
    relocated = dict(identity, binary=str(binary), archiveRun=43350361661,
                     archiveId=14006965532, archiveZipSha256=ZIP_SHA)
    driver.dump(args.root / 'runtime/candidate/identity.json', relocated)
    return relocated


def preserve_build_evidence(archive_path, root):
    driver.require(archive_path.stat().st_size == EVIDENCE_BYTES and common.digest(archive_path) == EVIDENCE_SHA,
                   'Original failed-run evidence ZIP differs')
    with zipfile.ZipFile(archive_path) as bundle:
        prefix = 'seekdb-count-only-evidence-43350361661-1/'
        old_report = json.loads(bundle.read(prefix + 'report.json'))
        driver.require(old_report['status'] == 'failed_or_blocked' and len(old_report['instances']) == 11
                       and not old_report['rounds'] and all(r['cleanupPassed'] for r in old_report['instances']),
                       'Original failure or cleanup provenance differs')
        manifest = json.loads(bundle.read(prefix + 'evidence-manifest.json'))
        raw = bundle.read(prefix + 'evidence.tar.gz')
        driver.require(len(raw) == manifest['archive']['bytes']
                       and hashlib.sha256(raw).hexdigest() == manifest['archive']['sha256'], 'Build evidence TAR differs')
    records = {row['path']: row for row in manifest['files'] if not row.get('omitted')}
    retained = []
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for name, record in records.items():
            if not (name.startswith('evidence/candidate/profile-')
                    or name == 'evidence/candidate/affected-object.json' or name == 'evidence/prebuild.json'):
                continue
            member = archive.getmember(name)
            data = archive.extractfile(member).read()
            driver.require(member.isfile() and not record['truncated'] and len(data) == record['bytes']
                           and hashlib.sha256(data).hexdigest() == record['sha256'], 'Original build evidence member differs')
            destination = root / (name if name != 'evidence/prebuild.json' else 'evidence/original-build-preflight.json')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            retained.append(record)
    driver.require(len(retained) == 14, 'Missing complete original compiler/object/preflight evidence')
    driver.dump(root / 'evidence/reused-build-evidence.json', dict(runId=43350361661,
        artifactId=14006931540, zipSha256=EVIDENCE_SHA, originalFailurePreserved=True, files=retained))


def install_clients(source, rpm_path, root):
    extractor = common.load_module('sort_pinned_rpm', Path(__file__).parent / 'fts-profile-runtime/extract_official_obclient.py')
    spec = extractor.SPECS['obclient']
    raw = extractor.pinned_rpm(rpm_path, spec)
    payload, _ = extractor.unpack_rpm(raw, 'obclient', spec)
    out = source / 'deps/3rd/u01/obclient/bin'
    out.mkdir(parents=True, exist_ok=True)
    receipts = []
    for name, digest in common.CLIENTS.items():
        data, count = extractor.cpio_member(payload, 'u01/obclient/bin/' + name)
        driver.require(count == spec['entries'] and hashlib.sha256(data).hexdigest() == digest, 'Pinned native client differs')
        path = out / name
        with path.open('xb') as stream:
            stream.write(data)
        path.chmod(0o555)
        common.verify_loader(path, root / 'evidence' / (name + '-ldd.log'), source, common.runtime_env(source, path))
        receipts.append(dict(name=name, bytes=len(data), sha256=digest))
    driver.dump(root / 'evidence/client-extraction.json', dict(rpmSha256=spec['sha256'], files=receipts))


def fresh_baseline_preflight(args, report):
    identity = report['builds']['baseline']
    row = driver.run_owned(args, report, {'baseline': identity}, 'baseline', 'targeted-baseline',
        lambda call, out, pid: targeted.run(call, out, args, driver.sql))
    common.require_instance(row)
    report['targeted']['baseline'] = row['work']
    report['reusedElfBaselinePreflight'] = dict(passed=True, baselineElfSha256=validation.BASE_ELF_SHA,
        executedBeforeCandidateValidation=True, executedBeforeCandidateCompile=False,
        resultSha256=common.digest(args.root / 'validation/targeted-baseline/result.json'))
    driver.dump(args.root / 'report.json', report)


def run(args):
    proof = verify_driver(args)
    driver.require(not args.frozen_source.exists(), 'Fresh frozen source checkout required')
    common.git(args.source, 'worktree', 'add', '--detach', str(args.frozen_source), CANDIDATE)
    runtime = argparse.Namespace(source=args.frozen_source, root=args.root, driver_sha=CANDIDATE,
                                 artifact_zip=args.baseline_zip, phase='validate')
    validation.prepare(runtime)
    driver.dump(args.root / 'evidence/revalidation-driver.json', proof)
    extract_candidate(args.candidate_zip, runtime, proof['originalInputHashes'])
    preserve_build_evidence(args.build_evidence_zip, args.root)
    install_clients(runtime.source, args.client_rpm, args.root)
    for role, commit in (('baseline', validation.BASE), ('candidate', CANDIDATE)):
        binary = args.root / 'runtime' / role / 'seekdb'
        common.verify_loader(binary, args.root / 'evidence' / role / 'ldd.log', runtime.source,
                             common.runtime_env(runtime.source, binary))
        driver.verify_version(runtime, binary, args.root / 'evidence' / role / 'version.log', commit)
    plan = json.loads((args.root / 'plan.json').read_text())
    plan.update(candidateBuildsRequired=0, newBuilds=0, reusedCandidateRun=43350361661, revalidationDriver=proof)
    driver.dump(args.root / 'plan.json', plan)
    report = json.loads((args.root / 'report.json').read_text())
    report.update(plan=plan, newBuilds=0, revalidationDriver=proof)
    driver.dump(args.root / 'report.json', report)
    validation.equality_work = corrected_equality_work
    driver.check_baseline_targeted = fresh_baseline_preflight
    return validation.bounded_validate(runtime)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('run', 'collect'), required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--driver-sha')
    parser.add_argument('--frozen-source', type=Path)
    parser.add_argument('--baseline-zip', type=Path)
    parser.add_argument('--candidate-zip', type=Path)
    parser.add_argument('--build-evidence-zip', type=Path)
    parser.add_argument('--client-rpm', type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    common.install_termination_handlers()
    try:
        if args.phase == 'collect':
            return driver.collect(args)
        return run(args)
    except Exception as error:
        path = args.root / 'report.json'
        if path.is_file() and args.phase != 'collect':
            report = json.loads(path.read_text())
            report.update(status='failed_or_blocked', failurePhase='revalidation', error=str(error),
                          traceback=traceback.format_exc())
            driver.dump(path, report)
        raise


if __name__ == '__main__':
    main()
