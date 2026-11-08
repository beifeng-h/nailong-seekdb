#!/usr/bin/env python3
"""Supplemental exact-byte plan metadata proof; never an official native pass.

Only the candidate's pinned fts_index.simple_query failure is eligible. This
module reads evidence after owned cleanup, does not mutate the native row, and
never writes an expected file or calls the native runner.
"""
import difflib
import hashlib
import os
from pathlib import Path
import re
import stat
from collections.abc import Mapping
from datetime import datetime

CASE = 'fts_index.simple_query'
SUITE = 'tools/deploy/mysql_test/test_suite/fts_index'
TEST = SUITE + '/t/simple_query.test'
GOLDEN = SUITE + '/r/mysql/simple_query.result'
TEST_SHA256 = '4786c566b8862bbbfab6a4b6de2f8940f41fed6aae943be020f83969617dcd07'
GOLDEN_SHA256 = '6172886af795b7e6e12f990bfc355985dbf51560da25a10775e20190ed7675e8'
GOLDEN_BYTES = 184285
GOLDEN_LINES = 2833
SCHEME = 'pinned-simple-query-four-plan-flags-v1'
MAX_BYTES = 1024 * 1024
SPECS = (
    (1191, 1206, 1, 'COUNT(1)', b'explain select count(1) from t_ir_single_column where match (detail) against ("distributed database");\n'),
    (1541, 1563, 1, 'seq,digest ORDER BY seq DESC', b'explain\nselect\n    seq, digest\nfrom\n    t_ir_single_column\nwhere\n    match (detail) against ("distributed relational database")\norder by seq desc;\n'),
    (1621, 1633, 0, 'DISTINCT 1', b'explain select distinct 1 from t_ir_single_column where match (detail) against ("distributed database");\n'),
    (1642, 1657, 1, 'DISTINCT digest', b'explain select distinct digest from t_ir_single_column where match (detail) against ("distributed database");\n'),
)
TRUE_TOKEN = b'calc_relevance=true, match_expr(MATCH('
FALSE_TOKEN = b'calc_relevance=false, match_expr(MATCH('


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _no_links(path):
    path = Path(path)
    _require(path.is_absolute(), 'Evidence paths must be absolute')
    _require('..' not in path.parts, 'Path traversal is forbidden')
    for part in (path, *path.parents):
        info = part.lstat()
        _require(not stat.S_ISLNK(info.st_mode) and
                 not getattr(info, 'st_file_attributes', 0) & 0x400,
                 'Symlink or reparse path is forbidden: ' + str(part))
    return path


def _read(path):
    path = _no_links(path)
    info = path.stat()
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
             'Evidence must be one ordinary unlinked file: ' + str(path))
    _require(0 < info.st_size <= MAX_BYTES, 'Evidence size is outside limit')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        _require(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1
                 and (info.st_dev, info.st_ino) == (opened.st_dev, opened.st_ino),
                 'Evidence replaced before reading')
        data = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
    current = _no_links(path).stat()
    _require(len(data) == info.st_size and len(data) <= MAX_BYTES
             and (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
             == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
             'Evidence changed while reading')
    _require((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
             == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
             'Evidence path changed while reading')
    return data


def _info(path, data):
    return dict(path=str(path), bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                lines=len(data.splitlines(keepends=True)), endsWithNewline=data.endswith(b'\n'))


def _statement_plan(lines, start, annotation, scan_id, sql, value):
    # Hash pinning establishes the original shape; still bind the approved
    # annotation to its exact SQL block and actual retrieval operator.
    first = start - 1
    query_plan = next((i for i in range(first, annotation) if lines[i] == b'Query Plan\n'), None)
    _require(query_plan is not None and b''.join(lines[first:query_plan]) == sql,
             'Pinned SQL statement mapping mismatch')
    block = b''.join(lines[query_plan:annotation + 1])
    scans = re.findall(rb'^\|\s*(\d+)\s*\|[^|\n]*TEXT RETRIEVAL SCAN[^|\n]*\|t_ir_single_column\(i1\)\|[^\n]*\n', block, re.MULTILINE)
    _require(scans == [str(scan_id).encode('ascii')], 'Retrieval scan identity mismatch')
    _require(block.count(b'Outputs & filters:\n') == 1, 'Ambiguous plan annotations')
    annotations = block.split(b'Outputs & filters:\n', 1)[1]
    outputs = list(re.finditer(rb'^  (\d+) - output\(', annotations, re.MULTILINE))
    owner = [annotations[item.start():outputs[i + 1].start() if i + 1 < len(outputs) else len(annotations)]
             for i, item in enumerate(outputs) if item[1] == str(scan_id).encode('ascii')]
    token = TRUE_TOKEN if value else FALSE_TOKEN
    _require(len(owner) == 1 and owner[0].count(token) == 1 and block.count(b'calc_relevance=') == 1,
             'Flag is not uniquely owned by retrieval scan')
    _require(lines[annotation - 1].startswith(b'      ' + token)
             and lines[annotation - 1].endswith(b')), \n'),
             'Exact annotation shape mismatch')


def _diagnostics(output, wrapper, expected, actual, expected_path, reject_path):
    output.decode('utf-8', errors='strict')
    wrapper.decode('utf-8', errors='strict')
    lines = output.splitlines(keepends=True)
    _require(len(lines) >= 3, 'Missing native mismatch diagnostic')
    timestamp = rb'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{9} [+-]\d{4}\n'
    for line, prefix, path in ((lines[0], b'--- ', expected_path),
                               (lines[1], b'+++ ', reject_path)):
        header = prefix + str(path).encode('utf-8') + b'\t'
        _require(line.startswith(header) and re.fullmatch(timestamp, line[len(header):]) is not None,
                 'Native diff header does not identify exact owned files')
    body = b''.join(difflib.diff_bytes(difflib.unified_diff,
                        expected.splitlines(keepends=True), actual.splitlines(keepends=True),
                        fromfile=b'expected', tofile=b'actual'))
    body = b''.join(body.splitlines(keepends=True)[2:])
    rest = b''.join(lines[2:])
    _require(rest.startswith(body), 'Native diff body differs from complete byte evidence')
    diagnostic = rest[len(body):]
    match = re.fullmatch(rb'\n*\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] mysqltest: Result length mismatch\n*', diagnostic)
    _require(match is not None, 'Unexpected, absent, or additional native error diagnostic')
    datetime.strptime(match[1].decode('ascii'), '%Y-%m-%d %H:%M:%S')
    prefix = b'[ RUN      ] ' + CASE.encode('ascii') + b'\n'
    emitted = output + (b'' if output.endswith(b'\n') else b'\n')
    _require(wrapper.startswith(prefix + emitted), 'Native wrapper differs from native output')
    suffix = wrapper[len(prefix + emitted):]
    _require(re.fullmatch(rb'\[  FAILED  \] fts_index\.simple_query \(\d+\.\d{3}s, exit=1\)\n', suffix) is not None,
             'Unexpected native wrapper, retry, or failure')
    return match[1].decode('ascii')


def classify_plan_metadata_compatibility(*, source, out, role, case_name, row):
    """Return an independent proof, without changing any native work/status.

    source: exact original case source checkout. out: owned per-instance output
    from original_case/run_instance, after cleanup. Missing or ambiguous evidence
    is incompatible. No errors are converted into native passes.
    """
    proof = dict(scheme=SCHEME, accepted=False, reason='Evidence not validated',
                 role=role, caseName=case_name,
                 rawNativeStatus=row.get('status') if isinstance(row, Mapping) else None,
                 nativePassed=False, formalContestPass=False)
    try:
        _require(role == 'candidate' and case_name == CASE, 'Only candidate pinned simple_query is eligible')
        _require(isinstance(row, Mapping) and row.get('name') == CASE and row.get('status') == 'failed',
                 'Native row must retain the named failed case')
        _require(row.get('role', role) == role, 'Recorded native role conflicts with candidate')
        work = row.get('work')
        _require(isinstance(work, Mapping) and work.get('passed') is False
                 and type(work.get('exitCode')) is int and work['exitCode'] == 1
                 and type(work.get('invocations')) is int and work['invocations'] == 1
                 and work.get('inputsUnchanged') is True,
                 'Native failure is not exactly one unchanged ordinary exit 1')
        _require(row.get('cleanupPassed') is True and not row.get('cleanupError'),
                 'Owned cleanup did not pass')
        for record in (row, work):
            _require(not any(record.get(key) for key in ('error', 'traceback', 'timedOut', 'signal', 'failureKind', 'failureReason')),
                     'Abnormal native or infrastructure failure')
        _require(work.get('testSha256') == TEST_SHA256 and work.get('expectedSha256') == GOLDEN_SHA256,
                 'Native recorded input hashes differ from pins')
        source, out = _no_links(source), _no_links(out)
        _require(source.is_dir() and out.is_dir()
                 and source != out and source not in out.parents and out not in source.parents,
                 'Source and owned output must be disjoint directories')
        _require(not (out / 'database').exists() and not (out / 'database').is_symlink(),
                 'Owned database remains after cleanup')
        expected_path, test_path = source / GOLDEN, source / TEST
        expected, test = _read(expected_path), _read(test_path)
        proof.update(originalGolden=_info(expected_path, expected), originalTest=_info(test_path, test))
        _require(hashlib.sha256(expected).hexdigest() == GOLDEN_SHA256
                 and len(expected) == GOLDEN_BYTES
                 and len(expected.splitlines(keepends=True)) == GOLDEN_LINES,
                 'Original golden bytes/hash/lines differ from immutable pin')
        _require(hashlib.sha256(test).hexdigest() == TEST_SHA256,
                 'Original test differs from immutable pin')
        logs = _no_links(out / 'mysqltest-log')
        _require(logs.is_dir(), 'Native log directory missing')
        entries = []
        with os.scandir(logs) as directory:
            for entry in directory:
                _require(len(entries) < 16, 'Unexpected native log directory size')
                entries.append(Path(entry.path))
        rejects = []
        for entry in entries:
            _no_links(entry)
            _require(entry.is_file(), 'Unexpected native log directory entry')
            if entry.suffix == '.reject':
                rejects.append(entry)
        reject_path = logs / 'simple_query.reject'
        _require(rejects == [reject_path], 'Expected exactly one named native reject file')
        actual = _read(reject_path)
        proof['actualReject'] = _info(reject_path, actual)
        lines = expected.splitlines(keepends=True)
        changed = list(lines)
        changes = []
        for start, annotation, scan_id, purpose, sql in SPECS:
            _statement_plan(lines, start, annotation, scan_id, sql, True)
            _require(changed[annotation - 1].count(TRUE_TOKEN) == 1, 'Pinned flag is ambiguous')
            changed[annotation - 1] = changed[annotation - 1].replace(TRUE_TOKEN, FALSE_TOKEN, 1)
            changes.append(dict(sqlStartLine=start, annotationLine=annotation, scanId=scan_id,
                                statement=sql.decode('utf-8').rstrip('\n'), purpose=purpose,
                                expectedCalcRelevance=True, actualCalcRelevance=False))
        approved = b''.join(changed)
        _require(actual == approved, 'Complete reject bytes contain differences outside exactly four approved flags')
        actual_lines = actual.splitlines(keepends=True)
        for start, annotation, scan_id, purpose, sql in SPECS:
            _statement_plan(actual_lines, start, annotation, scan_id, sql, False)
        _require(actual_lines[1602] == lines[1602] and TRUE_TOKEN in actual_lines[1602],
                 'Adjacent scored projection must remain scored')
        output_path, wrapper_path = out / 'mysqltest-output.log', out / 'original-run-case.log'
        output, wrapper = _read(output_path), _read(wrapper_path)
        timestamp = _diagnostics(output, wrapper, expected, actual, expected_path, reject_path)
        proof.update(accepted=True, reason='Complete bytes differ only at four pinned non-score-consuming EXPLAIN flags',
                     approvedDifferenceCount=4, changes=changes,
                     adjacentScoredControl=dict(annotationLine=1603, calcRelevance=True),
                     nativeOutput=_info(output_path, output), nativeWrapper=_info(wrapper_path, wrapper),
                     nativeMismatchTimestamp=timestamp, completeBytesCompared=True,
                     eofCompared=True, inputsUnchanged=True, ownedCleanupPassed=True)
    except (OSError, ValueError, TypeError, IndexError) as error:
        proof['reason'] = str(error)
    return proof
