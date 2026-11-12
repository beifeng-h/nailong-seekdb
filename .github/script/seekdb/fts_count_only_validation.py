#!/usr/bin/env python3
"""Validate COUNT-consumer rewriting against the exact complete b963 ELF."""
import argparse
from collections import deque
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import stat
import time


_spec = importlib.util.spec_from_file_location(
    '_fts_count_only_parent', Path(__file__).with_name('fts_cs45_equality_validation.py'))
parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(parent)
driver = parent.driver
common = parent.common
profile = parent.profile
BASE, BASE_TREE = parent.BASE, parent.BASE_TREE
REWRITE_CPP = 'src/sql/rewrite/ob_transform_pre_process.cpp'
REWRITE_HEADER = 'src/sql/rewrite/ob_transform_pre_process.h'
PRODUCTS = (REWRITE_CPP, REWRITE_HEADER)
SCOPE = 'SAME_HOST_COUNT_CONSUMER_DEVELOPMENT_VALIDATION_NOT_FORMAL_CONTEST'
TABLE = 'fts_count_consumer.docs'
MATCH = "MATCH(body) AGAINST('database performance audit')"


def limited_count(limit, offset=0, aggregate='COUNT(*)', parent_filter='', projection='id'):
    query = f'SELECT {projection} FROM {TABLE} WHERE {MATCH} LIMIT {limit}'
    if offset:
        query += f' OFFSET {offset}'
    return f'SELECT {aggregate} FROM ({query}) counted{parent_filter};'


COUNT_CASES = (
    ('rows_remaining', (f'SELECT COUNT(*) FROM {TABLE};', 69)),
    ('null_rows', (f'SELECT COUNT(*) FROM {TABLE} WHERE body IS NULL;', 1)),
    ('empty_rows', (f"SELECT COUNT(*) FROM {TABLE} WHERE body = '';", 1)),
    ('plain_count', (f'SELECT COUNT(*) FROM {TABLE} WHERE {MATCH};', 32)),
    ('limit_2', (limited_count(2), 2)),
    ('limit_32', (limited_count(32), 32)),
    ('limit_40', (limited_count(40), 32)),
    ('limit_0', (limited_count(0), 0)),
    ('offset_30', (limited_count(40, 30), 2)),
    ('offset_33', (limited_count(40, 33), 0)),
    ('no_match', (f'SELECT COUNT(*) FROM (SELECT id FROM {TABLE} '
                  "WHERE MATCH(body) AGAINST('absentnomatchtoken') LIMIT 40) counted;", 0)),
    ('count_column', (limited_count(2, aggregate='COUNT(nullable_value)', projection='id, nullable_value'), 0)),
    ('parent_filter', (limited_count(40, parent_filter=' WHERE counted.id > 30'), 5)),
)
_values = ',\n'.join(f"({index}, 'database performance audit', NULL)" for index in range(1, 36))
DENSE_COUNT_SETUP = f"""ALTER SYSTEM SET _enable_add_fulltext_index_to_existing_table = true;
CREATE DATABASE fts_count_consumer;
CREATE TABLE {TABLE} (id BIGINT PRIMARY KEY, body TEXT, nullable_value BIGINT)
DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
INSERT INTO {TABLE} VALUES
{_values},
(101, NULL, 7), (102, '', 8);
ALTER TABLE {TABLE} ADD FULLTEXT INDEX body_fts(body) WITH PARSER beng;
DELETE FROM {TABLE} WHERE id IN (1, 2, 3);
"""
_negative_values = ',\n'.join(f"({index}, 'unrelated zebra orchard', NULL)" for index in range(201, 236))


def add_unmatched_documents(setup):
    return setup.replace("(101, NULL, 7), (102, '', 8);", _negative_values + ",\n(101, NULL, 7), (102, '', 8);")


COUNT_SETUP = add_unmatched_documents(DENSE_COUNT_SETUP)
COUNT_SQL = COUNT_SETUP + ''.join(
    f"SELECT '{name}';\n{query}\n" for name, (query, expected) in COUNT_CASES)
COUNT_PLAN_CASES = ('limit_2', 'limit_32', 'limit_40', 'offset_30', 'offset_33')
SCORING_CONTROLS = {
    'plain_select_limit': f'SELECT id FROM {TABLE} WHERE {MATCH} LIMIT 2;',
    'score_projection': f'SELECT id, {MATCH} AS relevance FROM {TABLE} WHERE {MATCH} LIMIT 2;',
    'score_order': f'SELECT id FROM {TABLE} WHERE {MATCH} ORDER BY {MATCH} DESC, id LIMIT 2;',
    'count_column': limited_count(2, aggregate='COUNT(nullable_value)', projection='id, nullable_value'),
    'parent_filter': limited_count(40, parent_filter=' WHERE counted.id > 30'),
}
DIAGNOSTIC_FIXTURES = (
    dict(name='beng_deleted_dense', parser='beng', deleted=True, mixed=False),
    dict(name='beng_dense_no_delete', parser='beng', deleted=False, mixed=False),
    dict(name='ik_deleted_dense', parser='ik', deleted=True, mixed=False),
    dict(name='beng_deleted_mixed', parser='beng', deleted=True, mixed=True),
)
DENSE_TABLES = frozenset(('beng_deleted_dense', 'ik_deleted_dense'))
INHERITED_BASELINE_CASES = frozenset(('limit_2', 'limit_20', 'projection_constant_limit_2', 'count_column'))
CONNECTION_ERRORS = frozenset((2002, 2003, 2006, 2013, 2055))
LIVE_LOG_BYTE_CAP = 1024 * 1024
LIVE_LOG_LINE_CAP = 128 * 1024
LIVE_LOG_RECORD_CAP = 256
LIVE_LOG_JSON_CAP = 6 * 1024 * 1024
LIVE_LOG_PATTERNS = (
    ('warn', re.compile(rb'\bWARN\b')),
    ('error', re.compile(rb'\bERROR\b')),
    ('wdiag', re.compile(rb'\bWDIAG\b')),
    ('ediag', re.compile(rb'\bEDIAG\b')),
    ('error_4016', re.compile(rb'-4016\b')),
    ('send_error_packet', re.compile(rb'\bsend_error_packet\b')),
)


def diagnostics_only():
    return os.environ.get('COUNT_BASELINE_DIAGNOSTICS_ONLY') == '1'


def diagnostic_fixture(fixture):
    table = 'fts_count_consumer.' + fixture['name']
    setup = DENSE_COUNT_SETUP.replace('CREATE DATABASE fts_count_consumer;\n', '').replace(TABLE, table)
    setup = setup.replace('WITH PARSER beng;', 'WITH PARSER ' + fixture['parser'] + ';')
    if not fixture['deleted']:
        setup = setup.replace(f'DELETE FROM {table} WHERE id IN (1, 2, 3);\n', '')
    if fixture['mixed']:
        setup = add_unmatched_documents(setup)
    positive = 32 if fixture['deleted'] else 35
    expected = {name: value for name, (query, value) in COUNT_CASES}
    expected.update(rows_remaining=positive + 2 + (35 if fixture['mixed'] else 0),
                    plain_count=positive, limit_40=positive,
                    offset_30=max(positive - 30, 0), offset_33=max(positive - 33, 0))
    cases = [(name, query.replace(TABLE, table), expected[name]) for name, (query, value) in COUNT_CASES]
    cases.extend((('limit_20', limited_count(20).replace(TABLE, table), min(positive, 20)),
                  ('projection_constant_limit_2', limited_count(2, projection='1').replace(TABLE, table), 2)))
    plans = {name: query for name, query, value in cases}
    plans.update({name: query.replace(TABLE, table) for name, query in SCORING_CONTROLS.items()})
    return dict(**fixture, table=table, positiveRows=positive, totalRows=expected['rows_remaining'],
                setup=setup, cases=cases, plans=plans)


def log_size(out):
    try:
        return (out / 'database/log/seekdb.log').stat().st_size
    except FileNotFoundError:
        return None


def diagnostic_sql(call, out, label, query, kind, table=None, expected=None, case_name=None):
    driver.require(re.fullmatch('[A-Za-z0-9_.-]+', label) is not None, 'Unsafe diagnostic label')
    text = common.SESSION + query + '\n'
    sql_path, log_path = out / (label + '.sql'), out / (label + '.log')
    sql_path.write_text(text, encoding='utf-8')
    record = dict(label=label, kind=kind, table=table, caseName=case_name, sql=query, sqlPath=str(sql_path),
                  executedSqlSha256=common.digest(sql_path), expectedCount=expected,
                  startedUtc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  startedMonotonicNs=time.monotonic_ns(), serverLogOffsetBefore=log_size(out))
    try:
        result = common.capture_group(
            [str(call.obclient), '-h127.0.0.1', '-P' + str(call.port), '-uroot', '-A', '-N', '-B',
             '--raw', '--default-character-set=utf8mb4', '-Doceanbase'],
            log_path, Path.cwd(), 120 if kind == 'setup' else 30,
            env=os.environ.copy(), stdin=text.encode('utf-8'))
        record.update(exitCode=result['exitCode'], childrenCleaned=result['childrenCleaned'], invocation=result)
    except Exception as error:
        record.update(exitCode=None, childrenCleaned=False,
                      captureError=dict(type=type(error).__name__, message=str(error)))
    raw = log_path.read_bytes() if log_path.exists() else b''
    error = re.search(rb'\bERROR\s+(\d+)(?:\s+\(([A-Z0-9]+)\))?', raw)
    error_code = None if error is None else int(error[1])
    observed = None
    if kind == 'count' and record['exitCode'] == 0 and re.fullmatch(rb'\s*[0-9]+\s*', raw):
        observed = int(raw.strip())
    record.update(finishedUtc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  finishedMonotonicNs=time.monotonic_ns(), serverLogOffsetAfter=log_size(out),
                  outputPath=str(log_path), outputBytes=len(raw), outputSha256=hashlib.sha256(raw).hexdigest(),
                  outputHex=raw.hex(), outputUtf8=raw.decode('utf-8', errors='replace'),
                  serverErrorCode=error_code,
                  sqlState=None if error is None or error[2] is None else error[2].decode('ascii'),
                  connectionLost=error_code in CONNECTION_ERRORS, observedCount=observed,
                  countMatchesExpected=None if kind != 'count' or observed is None else observed == expected)
    if kind == 'explain':
        record['planObservation'] = dict(
            calcRelevanceValues=[value.decode('ascii') for value in re.findall(rb'calc_relevance\s*=\s*(true|false)', raw)],
            indexBackValues=[value.decode('ascii') for value in re.findall(rb'is_index_back\s*=\s*(true|false)', raw)],
            sortKeyLines=[line.decode('utf-8', errors='replace') for line in raw.splitlines() if b'sort_keys' in line])
    return record


def capture_live_diagnostic_log(call, out, outcomes, label='count-baseline-live-log', payload_cap=None):
    driver.require(re.fullmatch('[A-Za-z0-9_.-]+', label) is not None, 'Unsafe diagnostic log label')
    payload_cap = LIVE_LOG_BYTE_CAP if payload_cap is None else payload_cap
    path = out / 'database/log/seekdb.log'
    driver.require(call.base_dir == out / 'database' and call.base_dir.is_dir()
                   and not (out / 'own-stop.log').exists(), 'Diagnostic log capture must precede owned stop')
    common.no_symlinks(path)
    selected, recent = deque(), deque(maxlen=2)
    saved_bytes, evicted_count, oversized_count, matching_count = 0, 0, 0, 0
    evicted_begin, evicted_end, after_context = None, None, 0
    last_saved_offset = -1

    def save(record, reason):
        nonlocal saved_bytes, evicted_count, evicted_begin, evicted_end, last_saved_offset
        if record['offset'] <= last_saved_offset:
            return
        entry = dict(record, selectionReason=reason)
        selected.append(entry)
        saved_bytes += entry.get('savedBytes', 0)
        last_saved_offset = entry['offset']
        while len(selected) > LIVE_LOG_RECORD_CAP or saved_bytes > payload_cap:
            removed = selected.popleft()
            saved_bytes -= removed.get('savedBytes', 0)
            evicted_count += 1
            evicted_begin = removed['offset'] if evicted_begin is None else evicted_begin
            evicted_end = removed['offset'] + removed['bytes']

    capture_started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    snapshot_hash = hashlib.sha256()
    with path.open('rb') as stream:
        initial = os.fstat(stream.fileno())
        driver.require(stat.S_ISREG(initial.st_mode) and initial.st_nlink == 1, 'Owned diagnostic log must be regular and unique')
        remaining, line_offset, line_bytes = initial.st_size, 0, 0
        line_hash, pieces, matches, overlap = hashlib.sha256(), [], set(), b''
        while remaining:
            chunk = stream.readline(min(remaining, LIVE_LOG_LINE_CAP))
            driver.require(bool(chunk), 'Diagnostic log shortened during initial-size snapshot')
            remaining -= len(chunk)
            snapshot_hash.update(chunk)
            line_hash.update(chunk)
            line_bytes += len(chunk)
            if line_bytes <= LIVE_LOG_LINE_CAP:
                pieces.append(chunk)
            else:
                pieces.clear()
            for name, pattern in LIVE_LOG_PATTERNS:
                if pattern.search(overlap + chunk):
                    matches.add(name)
            overlap = chunk[-64:]
            if not chunk.endswith(b'\n') and remaining:
                continue
            complete = chunk.endswith(b'\n')
            record = dict(offset=line_offset, bytes=line_bytes, sha256=line_hash.hexdigest(),
                          endsWithNewline=complete, matchedPatterns=sorted(matches), savedBytes=0)
            if line_bytes <= LIVE_LOG_LINE_CAP:
                raw = b''.join(pieces)
                record.update(rawHex=raw.hex(), savedBytes=len(raw))
            else:
                record['omittedReason'] = 'Complete line exceeds the explicit per-line payload cap'
                oversized_count += 1
            if matches:
                matching_count += 1
                for previous in recent:
                    save(previous, 'context_before')
                save(record, 'match')
                after_context = 2
            elif after_context:
                save(record, 'context_after')
                after_context -= 1
            recent.append(record)
            line_offset += line_bytes
            line_bytes, line_hash, pieces, matches, overlap = 0, hashlib.sha256(), [], set(), b''
        final = os.fstat(stream.fileno())
    driver.require(final.st_size >= initial.st_size, 'Diagnostic log truncated during snapshot')
    ranges, unsaved, cursor = [], [], 0
    for record in selected:
        record['sqlAssociations'] = [row['label'] for row in outcomes
            if row['serverLogOffsetBefore'] is not None and row['serverLogOffsetAfter'] is not None
            and row['serverLogOffsetBefore'] <= record['offset'] < row['serverLogOffsetAfter']]
        if record.get('savedBytes'):
            if cursor < record['offset']:
                unsaved.append(dict(offset=cursor, bytes=record['offset'] - cursor))
            ranges.append(dict(offset=record['offset'], bytes=record['bytes']))
            cursor = record['offset'] + record['bytes']
    if cursor < initial.st_size:
        unsaved.append(dict(offset=cursor, bytes=initial.st_size - cursor))
    proof = dict(sourcePath=str(path), captureStartedUtc=capture_started,
                 captureFinishedUtc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 capturedBeforeOwnedStop=True, initialSizeBytes=initial.st_size, finalSizeBytes=final.st_size,
                 initialSizePrefixSha256=snapshot_hash.hexdigest(), sourceDevice=initial.st_dev, sourceInode=initial.st_ino,
                 selection='Recent matching lines plus two preceding/following snapshot line records, bounded by payload and record count; endsWithNewline identifies boundary fragments',
                 patterns=[name for name, pattern in LIVE_LOG_PATTERNS], matchedLineCount=matching_count,
                 perLineCapBytes=LIVE_LOG_LINE_CAP, payloadCapBytes=payload_cap, recordCap=LIVE_LOG_RECORD_CAP,
                 savedRawBytes=saved_bytes, records=list(selected), evictedOlderSelectedRecords=evicted_count,
                 evictedSelectedBoundingRange=None if evicted_begin is None else dict(offset=evicted_begin, bytes=evicted_end - evicted_begin),
                 oversizedLinesObserved=oversized_count, savedByteRanges=ranges, unsavedByteRanges=unsaved,
                 associationCaveat='SQL association uses observed byte ranges and does not prove causality. Appended bytes after initialSizeBytes are outside this snapshot.')
    raw = (json.dumps(proof, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    driver.require(len(raw) <= LIVE_LOG_JSON_CAP, 'Diagnostic log JSON exceeds its explicit complete-file cap')
    destination = out / (label + '.json')
    destination.write_bytes(raw)
    return dict(path=str(destination), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                initialSizeBytes=initial.st_size, initialSizePrefixSha256=proof['initialSizePrefixSha256'],
                matchedLineCount=matching_count, savedRecords=len(selected), capturedBeforeOwnedStop=True)


def matrix_identity(out, role):
    driver.require(out.parent.name == 'validation' and role in ('baseline', 'candidate'),
                   'Matrix evidence must bind an owned validation role')
    root = out.parent.parent
    identity_path = root / 'runtime' / role / 'identity.json'
    identity = json.loads(identity_path.read_text())
    proof = json.loads((root / 'plan.json').read_text())['proof']
    owned_path = out / 'owned-process.json'
    owned = json.loads(owned_path.read_text())
    expected_commit = BASE if role == 'baseline' else proof['driverCommit']
    expected_tree = BASE_TREE if role == 'baseline' else proof['driverTree']
    driver.require(identity['role'] == role and identity['sourceCommit'] == expected_commit
                   and identity['sourceTree'] == expected_tree, 'Matrix build source/role identity mismatch')
    driver.require(role != 'baseline' or identity['sha256'] == parent.BASE_ELF_SHA,
                   'Matrix baseline must be the exact b963 ELF')
    driver.require(owned['role'] == role and owned['binary'] == identity['binary']
                   and owned['sha256'] == identity['sha256'], 'Matrix owned process differs from role ELF')
    return dict(role=role, sourceCommit=expected_commit, sourceTree=expected_tree,
                elfSha256=identity['sha256'], buildId=identity['buildId'], binary=identity['binary'],
                validationDriverCommit=proof['driverCommit'], identitySha256=common.digest(identity_path),
                ownedProcessEvidenceSha256=common.digest(owned_path),
                candidateBuildAttempted=(root / 'runtime/candidate/identity.json').is_file())


def matrix_verdict(outcomes, role):
    violations, inherited, controls, expected_inventory, actual_inventory = [], [], [], set(), []
    expected_counts = {}
    for fixture in DIAGNOSTIC_FIXTURES:
        matrix = diagnostic_fixture(fixture)
        expected_inventory.add(('setup', matrix['table'], None))
        expected_inventory.update(('count', matrix['table'], name) for name, query, expected in matrix['cases'])
        expected_counts.update({('count', matrix['table'], name): expected for name, query, expected in matrix['cases']})
        expected_inventory.update(('explain', matrix['table'], name) for name in matrix['plans'])
    expected_inventory.update((('setup', None, None), ('connection', None, None)))
    for row in outcomes:
        case = row.get('caseName')
        if case is None and row['kind'] in ('count', 'explain'):
            case = row['label'].split('-' + row['kind'] + '-', 1)[-1]
        actual_inventory.append((row['kind'], row['table'], case))
        if not row['childrenCleaned'] or row['connectionLost'] or row.get('captureError'):
            violations.append(dict(label=row['label'], reason='SQL client connection and process completion must remain valid'))
        table = '' if row['table'] is None else row['table'].rsplit('.', 1)[-1]
        if row['kind'] == 'count':
            expected_count = expected_counts.get(('count', row['table'], case))
            if expected_count is None or row['expectedCount'] != expected_count:
                violations.append(dict(label=row['label'], reason='Recorded expected count differs from the independent fixed corpus'))
            allowed = (table in DENSE_TABLES and row['exitCode'] == 1 and row['serverErrorCode'] == 4016
                       and (case == 'count_column' or (role == 'baseline' and case in INHERITED_BASELINE_CASES)))
            if allowed:
                row['acceptance'] = 'inherited_scored_control_error' if case == 'count_column' else 'inherited_baseline_count_error'
                (controls if case == 'count_column' else inherited).append(dict(
                    role=role, table=row['table'], caseName=case, exitCode=row['exitCode'],
                    serverErrorCode=row['serverErrorCode'], expectedCount=row['expectedCount'],
                    outputSha256=row['outputSha256'], label=row['label']))
            elif row['exitCode'] == 0 and row['serverErrorCode'] is None and row['observedCount'] == expected_count:
                row['acceptance'] = 'independent_expected_count_matched'
            else:
                row['acceptance'] = 'rejected_count_outcome'
                violations.append(dict(label=row['label'], reason='Count must succeed with its independent expected value or match the exact inherited error policy'))
        elif row['exitCode'] != 0 or row['serverErrorCode'] is not None:
            violations.append(dict(label=row['label'], reason='Setup, EXPLAIN and connection SQL must succeed'))
        elif row['kind'] == 'explain' and 'MATCH(' in row['sql']:
            expected = case in SCORING_CONTROLS or (case != 'plain_count' and role == 'baseline')
            try:
                row['planVerification'] = check_count_plan(bytes.fromhex(row['outputHex']).decode('utf-8'), expected, not expected)
            except (RuntimeError, UnicodeError, ValueError, StopIteration) as error:
                violations.append(dict(label=row['label'], reason=str(error)))
    if len(actual_inventory) != len(expected_inventory) or set(actual_inventory) != expected_inventory:
        violations.append(dict(label='matrix-inventory', reason='Exactly one execution of every fixed matrix SQL case is required'))
    return dict(passed=not violations, violations=violations,
                inheritedBaselineCountErrors=inherited, legacyScoredControlErrors=controls,
                newDenseCountExpectedResultsPassed=(not violations) if role == 'candidate' else None,
                allRawSqlSucceeded=all(row['exitCode'] == 0 for row in outcomes),
                rawSqlCounts=dict(total=len(outcomes), succeeded=sum(row['exitCode'] == 0 for row in outcomes),
                                  failed=sum(row['exitCode'] != 0 for row in outcomes)))


def matrix_work(call, out, role, strict=False):
    identity = matrix_identity(out, role)
    outcomes, tables = [], []
    first_log_errors = []

    def retain(record, table_report=None):
        outcomes.append(record)
        if table_report is not None and record['exitCode'] != 0 and not table_report.get('firstErrorCaptureAttempted'):
            table_report['firstErrorCaptureAttempted'] = True
            table_report['firstErrorSqlLabel'] = record['label']
            try:
                table_report['firstErrorServerLog'] = capture_live_diagnostic_log(
                    call, out, outcomes, 'count-baseline-first-error-' + table_report['name'], 256 * 1024)
            except Exception as error:
                first_log_errors.append(dict(table=table_report['table'], sqlLabel=record['label'],
                                              type=type(error).__name__, message=str(error)))

    retain(diagnostic_sql(call, out, 'diag-schema',
        'ALTER SYSTEM SET _enable_add_fulltext_index_to_existing_table = true;\nCREATE DATABASE IF NOT EXISTS fts_count_consumer;', 'setup'))
    for fixture in DIAGNOSTIC_FIXTURES:
        matrix = diagnostic_fixture(fixture)
        table = matrix['table']
        table_report = {key: matrix[key] for key in ('name', 'parser', 'deleted', 'mixed', 'positiveRows', 'totalRows', 'table')}
        tables.append(table_report)
        retain(diagnostic_sql(call, out, 'diag-' + fixture['name'] + '-setup', matrix['setup'], 'setup', table), table_report)
        for name, query, expected in matrix['cases']:
            retain(diagnostic_sql(call, out, 'diag-' + fixture['name'] + '-count-' + name,
                                   query, 'count', table, expected, name), table_report)
        for name, query in matrix['plans'].items():
            retain(diagnostic_sql(call, out, 'diag-' + fixture['name'] + '-explain-' + name,
                                   'EXPLAIN ' + query, 'explain', table, case_name=name), table_report)
    connection = diagnostic_sql(call, out, 'diag-final-connection', 'SELECT 1;', 'connection')
    outcomes.append(connection)
    verdict = matrix_verdict(outcomes, role) if strict else None
    report = dict(diagnosticOnly=not strict, candidateBuildAttempted=identity['candidateBuildAttempted'], correctnessPassed=False,
                  roleIdentity=identity, strictCountAndPlanValidation=verdict, tables=tables, outcomes=outcomes,
                  firstErrorLogCaptureErrors=first_log_errors,
                  outputEncoding='outputHex preserves complete original bytes; outputUtf8 is a replacement-decoded display',
                  interpretation='Observed SQL outcomes only. Parser, deletion and term-density associations require analysis.')
    log_error = None
    try:
        report['liveServerLog'] = capture_live_diagnostic_log(call, out, outcomes)
    except Exception as error:
        log_error = error
        report['liveServerLogError'] = dict(type=type(error).__name__, message=str(error))
    destination = out / 'count-matrix-diagnostics.json'
    driver.dump(destination, report)
    driver.require(connection['exitCode'] == 0 and connection['childrenCleaned']
                   and bytes.fromhex(connection['outputHex']).strip() == b'1', 'Diagnostic baseline connection failed')
    driver.require(not any(row['connectionLost'] or row.get('captureError') or not row['childrenCleaned']
                           or row['exitCode'] not in (0, 1) for row in outcomes),
                   'Diagnostic SQL encountered a client connection or process failure')
    driver.require(log_error is None, 'Pre-stop diagnostic server log capture failed')
    driver.require(not first_log_errors, 'First-error diagnostic server log capture failed')
    driver.require(not strict or verdict['passed'], 'COUNT matrix failed independent count or consumer-plan validation')
    compact = [{key: row[key] for key in ('label', 'kind', 'table', 'caseName', 'exitCode', 'serverErrorCode',
                'sqlState', 'expectedCount', 'observedCount', 'countMatchesExpected', 'outputSha256')} for row in outcomes]
    return dict(passed=True, diagnosticOnly=not strict, correctnessPassed=False,
                candidateBuildAttempted=identity['candidateBuildAttempted'], roleIdentity=identity,
                canonical=dict(diagnosticJsonSha256=common.digest(destination)), rawOutcomes=compact,
                diagnosticJsonPath=str(destination), liveServerLog=report['liveServerLog'],
                strictCountAndPlanValidation=verdict)


def diagnostic_work(call, out, args, sql):
    role = {'targeted-baseline': 'baseline', 'equality-baseline': 'baseline',
            'equality-candidate': 'candidate'}.get(out.name)
    return matrix_work(call, out, role, strict=False)


def source_proof(args):
    source, commit = args.source, args.driver_sha
    driver.require(re.fullmatch('[0-9a-f]{40}', commit) is not None, 'Full source SHA required')
    identity = common.source_identity(source, commit)
    driver.require(common.git(source, 'rev-parse', BASE + '^{tree}').decode().strip() == BASE_TREE,
                   'Exact b963 source tree mismatch')
    changed = set(common.git(source, 'diff', '--name-only', BASE, commit, '--').decode().splitlines())
    products = {path for path in changed if not path.startswith('.github/')}
    driver.require(products == set(PRODUCTS), 'Exactly the two COUNT-consumer rewrite products are required')
    hashes = driver.inventory(source)
    case_inputs = {}
    for name in driver.CASES:
        suite, case = name.split('.')
        directory = source / 'tools/deploy/mysql_test/test_suite' / suite
        paths = (directory / 't' / (case + '.test'), directory / 'r/mysql' / (case + '.result'))
        for path in paths:
            relative = path.relative_to(source).as_posix()
            driver.require(common.git(source, 'show', BASE + ':' + relative) == path.read_bytes(),
                           'Existing selected native input changed: ' + relative)
        case_inputs[name] = dict(testSha256=common.digest(paths[0]), expectedSha256=common.digest(paths[1]))
    test = source / 'tools/deploy/mysql_test/test_suite/fts_index/t/simple_query.test'
    test_text = test.read_text(encoding='utf-8')
    explains = list(re.finditer(r'^\s*explain\b.*?;', test_text, re.I | re.M | re.S))
    count_parents = [dict(line=test_text.count('\n', 0, match.start()) + 1,
                          sql=match.group().strip()) for match in explains
                     if re.search(r'\bcount\s*\(', match.group(), re.I)
                     and re.search(r'\bfrom\s*\(\s*select\b', match.group(), re.I)
                     and re.search(r'\blimit\b', match.group(), re.I)]
    patch = common.git(source, 'diff', *driver.CANONICAL_DIFF_FLAGS, BASE, commit, '--', *PRODUCTS)
    ci_files = {path: hashlib.sha256(common.git(source, 'show', commit + ':' + path)).hexdigest()
                for path in sorted(changed - products)}
    driver.KERNEL, driver.KERNEL_TREE = commit, identity['tree']
    return dict(baselineCommit=BASE, baselineTree=BASE_TREE, kernelCommit=commit,
                kernelTree=identity['tree'], driverCommit=commit, driverTree=identity['tree'],
                productChanges=sorted(PRODUCTS), ciChanges=sorted(ci_files), ciBlobSha256=ci_files,
                canonicalFullIndexPatchSha256=hashlib.sha256(patch).hexdigest(),
                originalInputCount=75, originalInputInventorySha256=driver.INVENTORY,
                originalInputHashes=hashes, caseInputs=case_inputs,
                simpleQueryExplainAudit=dict(testSha256=common.digest(test), explainStatements=len(explains),
                                             countParentsWithDerivedLimit=count_parents),
                commitObjects={commit: driver.git_identity(source, commit)})


_prepare = parent.prepare


def prepare(args):
    _prepare(args)
    plan = json.loads((args.root / 'plan.json').read_text())
    plan.update(scope=SCOPE, candidateFeatures=['count_consumer_filter_only_limit'])
    driver.dump(args.root / 'plan.json', plan)
    report = json.loads((args.root / 'report.json').read_text())
    report.update(plan=plan, validationScope=SCOPE)
    if diagnostics_only():
        report.update(diagnosticOnly=True, candidateBuildAttempted=False, correctnessPassed=False)
        plan.update(diagnosticOnly=True, candidateBuildsRequired=0)
        driver.dump(args.root / 'plan.json', plan)
    driver.dump(args.root / 'report.json', report)


_build_profile = parent.build_profile


def build_profile(args):
    settings, rust = _build_profile(args)
    build = args.source / 'build_release'
    out = args.root / 'evidence/candidate' / ('profile-' + args.phase)
    original = json.loads((out / 'profile-proof.json').read_text())
    compiler = original['signature']['compilers']['CXX']['COMPILER']
    commands = json.loads(profile.regular(build / 'compile_commands.json', profile.COMMAND_CAP))
    affected = []
    for row in commands:
        unit = Path(row['file'])
        if not unit.is_absolute():
            unit = Path(row['directory']) / unit
        included = {unit.resolve()}
        if 'Unity' in unit.parts:
            for name in re.findall(r'^#include "([^"\n]+)"', unit.read_text(encoding='utf-8'), re.M):
                path = Path(name)
                included.add((path if path.is_absolute() else unit.parent / path).resolve())
        if (args.source / REWRITE_CPP).resolve() in included:
            affected.append(row)
    driver.require(len(affected) == 1, 'One actual preprocess compilation unit required')
    row = affected[0]
    argv = row.get('arguments') or shlex.split(row['command'])
    driver.require(compiler in argv[:2] and '-o' in argv, 'Preprocess command uses an unexpected compiler')
    obj = Path(argv[argv.index('-o') + 1])
    if not obj.is_absolute():
        obj = Path(row['directory']) / obj
    driver.require(obj.resolve().is_relative_to(build.resolve()), 'Preprocess object escapes the build root')
    signature = dict(compiler=compiler, compileCommand=row,
                     productSourceSha256={name: common.digest(args.source / name) for name in PRODUCTS})
    if args.phase == 'snapshot':
        prior = args.root / 'evidence/candidate/profile-prebuild/count-compilation-proof.json'
        driver.require(json.loads(prior.read_text())['signature'] == signature,
                       'Preprocess source or compilation command changed after prebuild')
        common.no_symlinks(obj)
        driver.require(obj.is_file() and obj.stat().st_nlink == 1 and obj.stat().st_size > 0,
                       'The actual unique preprocess object was not built')
        driver.dump(args.root / 'evidence/candidate/affected-count-object.json',
                    dict(source=REWRITE_CPP, path=str(obj), bytes=obj.stat().st_size,
                         sha256=common.digest(obj), compileCommand=row))
    driver.dump(out / 'count-compilation-proof.json', dict(passed=True, signature=signature))
    return settings, rust


def dependency_record(path, row, source, out):
    candidates = [Path(str(path) + '.d')]
    for directory in path.parents:
        if directory.name.endswith('.dir'):
            candidates.append(directory / 'compiler_depend.make')
            break
    resolved = path.resolve()
    targets = {resolved.as_posix()}
    for directory in (Path(row['directory']).resolve(), (source / 'build_release').resolve()):
        if resolved.is_relative_to(directory):
            targets.add(resolved.relative_to(directory).as_posix())
    headers, records = set(), []
    for index, candidate in enumerate(candidates):
        if not candidate.is_file():
            records.append(dict(path=str(candidate), exists=False))
            continue
        raw = profile.regular(candidate, profile.COMMAND_CAP)
        saved = out / ('preprocess-dependency-' + str(index) + '.txt')
        saved.write_bytes(raw)
        matching = []
        for line in re.sub(r'\\\r?\n', ' ', raw.decode('utf-8')).splitlines():
            if ': ' in line:
                target, inputs = line.split(': ', 1)
                if target.replace('\\ ', ' ') in targets:
                    matching.append(line)
                    for name in shlex.split(inputs):
                        value = Path(name)
                        headers.add((value if value.is_absolute() else Path(row['directory']) / value).resolve())
        records.append(dict(path=str(candidate), exists=True, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                            rawEvidencePath=str(saved), unfoldedMatchingRules=matching))
        if (source / REWRITE_HEADER).resolve() in headers:
            break
    return dict(records=records, header=REWRITE_HEADER,
                headerPresent=(source / REWRITE_HEADER).resolve() in headers)


def verify_header_dependencies(args):
    snapshot = args.root / 'evidence/candidate/affected-count-object.json'
    record = json.loads(snapshot.read_text())
    proof = dict(phase='validate', snapshotSha256=common.digest(snapshot), passed=False,
                 compilationSource=REWRITE_CPP, checkingObject=record['path'])
    try:
        obj = Path(record['path'])
        common.no_symlinks(obj)
        driver.require(obj.is_file() and obj.stat().st_nlink == 1 and obj.stat().st_size == record['bytes']
                       and common.digest(obj) == record['sha256'], 'Saved preprocess object changed')
        proof['objectSha256'] = record['sha256']
        proof['dependencies'] = dependency_record(obj, record['compileCommand'], args.source,
                                                  args.root / 'evidence/candidate')
        driver.require(proof['dependencies']['headerPresent'],
                       'Actual preprocess compiler dependencies must include its modified header')
        proof['passed'] = True
    finally:
        driver.dump(args.root / 'evidence/candidate/count-header-dependencies.json', proof)


def check_count_plan(text, expected_relevance, require_unordered):
    parsed = common.plan_relevance(text)
    annotations = text.split('Outputs & filters:', 1)[1]
    blocks = list(re.finditer(r'^\s*(\d+)\s+-\s+output\(', annotations, re.M))
    owned = next(annotations[block.start():blocks[index + 1].start() if index + 1 < len(blocks) else len(annotations)]
                 for index, block in enumerate(blocks) if int(block[1]) == parsed['scanId'])
    sort_keys = re.findall(r'\bsort_keys\s*\((nil|\[[^\n]*\])\)', owned)
    driver.require(len(sort_keys) == len(re.findall(r'\bsort_keys\s*\(', owned)),
                   'Malformed implicit sort-key annotation')
    index_back = re.findall(r'\bis_index_back\s*=\s*(true|false)', owned)
    driver.require(len(index_back) <= 1, 'Ambiguous retrieval index-back annotation')
    parsed.update(expectedCalcRelevance=expected_relevance, implicitSortKeys=sort_keys,
                  hasImplicitSortKeys=any(value != 'nil' for value in sort_keys),
                  actualIndexBack=None if not index_back else index_back[0] == 'true',
                  indexBackAnnotationPresent=bool(index_back), retrievalAnnotation=owned)
    driver.require(parsed['actualCalcRelevance'] == expected_relevance, 'COUNT-consumer relevance plan mismatch')
    driver.require(not require_unordered or not parsed['hasImplicitSortKeys'],
                   'COUNT-consumer plan retains implicit relevance sorting')
    return parsed


_equality_work = parent.equality_work


def equality_work(call, out):
    role = {'targeted-baseline': 'baseline', 'equality-baseline': 'baseline',
            'equality-candidate': 'candidate'}.get(out.name)
    driver.require(role is not None, 'COUNT parity must bind a named validation role')
    collation = _equality_work(call, out)
    raw = driver.sql(call, out, 'count-consumer', COUNT_SQL)
    expected = []
    for name, (query, value) in COUNT_CASES:
        expected.extend((name, str(value)))
    driver.require(raw.splitlines() == expected, 'COUNT-consumer results differ from the fixed fixture')
    plans = {}
    cases = dict(COUNT_CASES)
    queries = {name: cases[name][0] for name in COUNT_PLAN_CASES}
    queries.update(SCORING_CONTROLS)
    for name, query in queries.items():
        label = 'count-plan-' + name
        text = driver.sql(call, out, label, 'EXPLAIN ' + query)
        expected_relevance = name in SCORING_CONTROLS or role == 'baseline'
        proof = check_count_plan(text, expected_relevance, not expected_relevance)
        proof.update(sql=query, sqlSha256=hashlib.sha256(query.encode()).hexdigest(),
                     outputSha256=common.digest(out / (label + '.log')))
        plans[name] = proof
    driver.dump(out / 'count-consumer-plans.json', dict(role=role, passed=True, queries=plans))
    matrix = matrix_work(call, out, role, strict=True)
    output = (out / 'count-consumer.log').read_bytes()
    canonical = json.dumps(dict(collation=collation['canonical'], counts=output.hex()),
                           sort_keys=True, separators=(',', ':')).encode()
    return dict(passed=True, canonical=canonical.hex(), cs45=collation,
                countOutputBytes=len(output), countOutputSha256=hashlib.sha256(output).hexdigest(),
                countSqlSha256=hashlib.sha256(COUNT_SQL.encode()).hexdigest(),
                expectedCounts={name: value for name, (query, value) in COUNT_CASES},
                countConsumerPlans=plans, countMatrix=matrix)


def benchmark_callback(args, identities, role, baseline, scorer):
    source = args.root / 'sources/baseline' if role == 'baseline' else args.source
    role_args = argparse.Namespace(source=source, workspace=args.source, role=role,
        expected_sha=BASE if role == 'baseline' else args.driver_sha,
        binary=Path(identities[role]['binary']), obclient=args.source / 'deps/3rd/u01/obclient/bin/obclient')
    benchmark = common.benchmark_work(role_args, baseline, scorer, False)

    def work(call, out, pid):
        result = benchmark(call, out)
        if not result.get('passed'):
            return result
        queries = common.queries_from_benchmark(source / 'tools/benchmark/fts_large_bench.sh')
        plans = {}
        for name, query in common.plan_queries(queries).items():
            text = driver.sql(call, out, name + '-explain', 'EXPLAIN ' + query)
            expected = name in common.PLAN_CONTROLS or (name == 'query_limit' and role == 'baseline')
            plans[name] = check_count_plan(text, expected, not expected)
        result['roleConsistentPlans'] = plans
        return result
    return work


_prebuild = parent.prebuild


def prebuild(args):
    import fts_expr_cache_targeted as targeted
    original = targeted.run

    def run(call, out, args, sql):
        result = original(call, out, args, sql)
        result['countOnlyPreflight'] = equality_work(call, out)
        return result

    diagnostic = diagnostics_only()
    targeted.run = diagnostic_work if diagnostic else run
    try:
        result = _prebuild(args)
        if diagnostic:
            report = json.loads((args.root / 'report.json').read_text())
            report.update(status='baseline_diagnostics_completed', diagnosticOnly=True,
                          candidateBuildAttempted=False, correctnessPassed=False)
            driver.dump(args.root / 'report.json', report)
        return result
    finally:
        targeted.run = original
        if diagnostic:
            report_path = args.root / 'report.json'
            if report_path.is_file():
                report = json.loads(report_path.read_text())
                report.update(diagnosticOnly=True, candidateBuildAttempted=False, correctnessPassed=False)
                diagnostic_path = args.root / 'validation/targeted-baseline/count-matrix-diagnostics.json'
                if diagnostic_path.is_file():
                    data = json.loads(diagnostic_path.read_text())
                    report['baselineDiagnostics'] = dict(
                        path=str(diagnostic_path), sha256=common.digest(diagnostic_path),
                        outcomes=[{key: row[key] for key in ('label', 'kind', 'table', 'caseName', 'exitCode',
                                  'serverErrorCode', 'sqlState', 'expectedCount', 'observedCount',
                                  'countMatchesExpected', 'outputSha256')} for row in data['outcomes']],
                        liveServerLog=data.get('liveServerLog'), liveServerLogError=data.get('liveServerLogError'))
                    report['baselineDiagnostics']['firstErrorServerLogs'] = [table['firstErrorServerLog']
                        for table in data['tables'] if 'firstErrorServerLog' in table]
                    report['baselineDiagnostics']['firstErrorLogCaptureErrors'] = data.get('firstErrorLogCaptureErrors', [])
                driver.dump(report_path, report)


_validate = parent.validate


def validate(args):
    driver.require(not diagnostics_only(), 'Diagnostic-only mode cannot start candidate validation or ABBA')
    verify_header_dependencies(args)
    _validate(args)
    report = json.loads((args.root / 'report.json').read_text())
    preflight = report['targeted']['baseline']['countOnlyPreflight']
    driver.require(preflight['canonical'] == report['equality']['baseline']['canonical'],
                   'Baseline COUNT prebuild SQL differs from the later fresh-instance output')
    matrices = {role: report['equality'][role]['countMatrix']['strictCountAndPlanValidation']
                for role in ('baseline', 'candidate')}
    driver.require(matrices['candidate']['newDenseCountExpectedResultsPassed'],
                   'New dense COUNT target results must pass independently')
    report.update(validationScope=SCOPE, baselineCountPreflightPassed=True,
                  countOnlySqlParityPassed=True, countOnlyPlanProofPassed=True, candidateBuildAttempted=True,
                  newDenseCountExpectedResultsPassed=True,
                  legacyScoredControlErrors=[row for matrix in matrices.values() for row in matrix['legacyScoredControlErrors']],
                  inheritedBaselineCountErrors=matrices['baseline']['inheritedBaselineCountErrors'],
                  matrixRawSqlCounts={role: matrix['rawSqlCounts'] for role, matrix in matrices.items()},
                  allMatrixSqlSucceeded=all(matrix['allRawSqlSucceeded'] for matrix in matrices.values()))
    driver.dump(args.root / 'report.json', report)


parent.source_proof = source_proof
parent.equality_work = equality_work
parent.benchmark_callback = benchmark_callback
parent.validate = validate
driver.source_proof = source_proof
driver.prepare = prepare
driver.prebuild = prebuild
driver.build_profile = build_profile


def main(argv=None):
    return parent.main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
