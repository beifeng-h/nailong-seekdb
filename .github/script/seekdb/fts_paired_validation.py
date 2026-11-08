#!/usr/bin/env python3
"""Exact-source same-host FTS calibration. Plan-only unless --execute is given.

The official builder, native inputs, benchmark, scorer and candidate harness are
never edited. A valid calibration exits zero even when performance is negative
or inconclusive; correctness/infrastructure failures exit one and retain evidence.
"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import statistics
import stat
import subprocess
import sys
import time
import traceback

sys.dont_write_bytecode = True
BASELINE = '5bce9e06d1281347ad61344e4b01fab9158c58a2'
CANDIDATE = 'bc4dde62eee39acb630ffe54741aecebe4ab0a2a'
SOURCE_TREES = {'baseline': '7397f01a796c5ab1cae81c90303b22b63945d11d',
                'candidate': '1ea510152c6274df5915ec9423fa76cda0ed4a2a'}
REUSE_MODE = 'verified_elf_continuation'
KERNEL = 'f1f4d0d4b225ec528dc3802dbfd93d9d4870aa65'
PREFIX = '.github/script/seekdb/'
ALLOWED_DRIVER_PATHS = frozenset((
    '.github/workflows/fts-contest-validation.yml',
    PREFIX + 'fts_paired_validation.py', PREFIX + 'fts_paired_collect.py',
    PREFIX + 'test_fts_paired_validation.py', PREFIX + 'test_fts_paired_collect.py',
    PREFIX + 'fts_plan_compatibility.py', PREFIX + 'test_fts_plan_compatibility.py',
    PREFIX + 'fts_verified_elf_reuse.py', PREFIX + 'test_fts_verified_elf_reuse.py'))
DRIVER_FILES = ('fts_paired_validation.py', 'fts_paired_collect.py', 'fts_contest_validation.py',
                'fts_plan_compatibility.py', 'fts_verified_elf_reuse.py')
CASES = ('fts_index.tokenize_function', 'fts_index.cn_word',
         'fts_index.parser_properties', 'fts_index.basic_dml', 'fts_index.simple_query')
REGRESSIONS = ('fts_index.tokenize_casefold_contract', 'fts_index.match_filter_only_contract')
QUERY_NAMES = ('query_cn', 'query_beng', 'query_mixed', 'query_limit')
PLAN_CONTROLS = ('control_score_projection', 'control_score_order', 'control_nonzero_threshold')
SCHEDULE = (('A1', 'baseline'), ('B1', 'candidate'), ('B2', 'candidate'),
            ('A2', 'baseline'), ('B3', 'candidate'), ('A3', 'baseline'),
            ('A4', 'baseline'), ('B4', 'candidate'))
PAIRS = (('A1', 'B1'), ('A2', 'B2'), ('A3', 'B3'), ('A4', 'B4'))
BENCH = 'tools/benchmark/fts_large_bench'
CONFIG = dict(rows=20000, batch=500, rounds=3000, query_rounds=200,
              samples=3, warmup=30, skip_load=0)
HITS = dict(query_cn_hits=8001, query_beng_hits=11000,
            query_mixed_hits=7332, query_limit_hits=20)
CLIENTS = dict(obclient='798aa952d536563ee0291e69251a6d0cd0d85e0c470d5499768fa0731ff3c1be',
               mysqltest='64e0323b385af2831c5b166a375e8320ec40e3ead0bd925295de1b8f5a2196c6')
MARGIN = 0.02
SETTLE_SECONDS = 10
SESSION = 'SET NAMES utf8mb4; SET ob_query_timeout = 100000000000; SET ob_trx_timeout = 100000000000;\n'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def git(source, *arguments):
    return subprocess.check_output(['git', '-C', str(source), *arguments],
        env=dict(os.environ, GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0'))


def source_identity(source, expected):
    actual = git(source, 'rev-parse', 'HEAD').decode().strip()
    dirty = git(source, 'status', '--porcelain=v1', '--untracked-files=all').strip()
    if actual != expected or dirty:
        raise RuntimeError('Source commit or staged/untracked cleanliness mismatch: ' + str(source))
    return dict(commit=actual, tree=git(source, 'rev-parse', 'HEAD^{tree}').decode().strip())


def verify_driver(source, driver_sha):
    if not re.fullmatch('[0-9a-f]{40}', driver_sha):
        raise RuntimeError('Driver requires an exact full commit SHA')
    changed = set(git(source, 'diff', '--name-only', KERNEL, driver_sha, '--').decode().splitlines())
    if not changed <= ALLOWED_DRIVER_PATHS:
        raise RuntimeError('Driver tree differs from kernel origin outside paired CI: ' + ', '.join(sorted(changed - ALLOWED_DRIVER_PATHS)))
    # These complete build/benchmark/native input trees must also equal A.
    protected = ('build.sh', 'CMakeLists.txt', 'cmake', 'rust', 'deps',
                 '.github/actions/setup-rust', 'tools/cmake', BENCH + '.sh',
                 BENCH + '_gen.py', BENCH + '_setup.sql', BENCH + '_score.py',
                 BENCH + '_baseline.json', PREFIX + 'mysqltest_for_seekdb.py',
                 PREFIX + 'sdb.py', 'tools/deploy/init.sql', 'tools/deploy/init_user.sql',
                 'tools/deploy/mysqltest_config.yaml')
    if git(source, 'diff', '--name-only', BASELINE, driver_sha, '--', *protected).strip():
        raise RuntimeError('Official build/benchmark/native inputs differ from A')
    return dict(kernelOrigin=KERNEL, driverCommit=driver_sha,
                allowedDriverChanges=sorted(changed))


def verify_reuse_driver(source, driver_sha):
    source_identity(source, driver_sha)
    result = verify_driver(source, driver_sha)
    changed = set(git(source, 'diff', '--name-only', CANDIDATE, driver_sha, '--').decode().splitlines())
    if not changed <= ALLOWED_DRIVER_PATHS:
        raise RuntimeError('Reuse driver changes actual B outside explicit diagnostic allowlist: '
                           + ', '.join(sorted(changed - ALLOWED_DRIVER_PATHS)))
    return dict(result, actualCandidateBuildCommit=CANDIDATE,
                validationDriverCommit=driver_sha, continuationChanges=sorted(changed))


def validate_relocated_identity(identity, role, root, provenance):
    """Bind untouched historical metadata to the new owned path, without edits."""
    commit = BASELINE if role == 'baseline' else CANDIDATE
    directory = root / 'runtime' / role
    original, relocation = directory / 'identity.original.json', directory / 'relocation.json'
    for path in (original, relocation):
        no_symlinks(path)
        if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 8 * 1024 ** 2:
            raise RuntimeError('Invalid original identity/relocation file')
    if (json.loads(original.read_text(encoding='utf-8')) != identity
            or json.loads(relocation.read_text(encoding='utf-8')) != provenance
            or provenance.get('originalIdentitySha256') != digest(original)
            or provenance.get('originalBinaryPath') != identity.get('binary')
            or provenance.get('runtimeBinaryPath') != str(directory / 'seekdb')
            or provenance.get('staticIdentityVerified') is not True
            or provenance.get('previousValidationAccepted') is not False
            or provenance.get('actualValidationDriver') != 'UNASSIGNED'
            or provenance.get('artifactId') != 13621468966
            or provenance.get('runId') != 42486120779 or provenance.get('runAttempt') != 1
            or provenance.get('artifactZipSha256') != '23f8b2f8f6322edcd5dd0ed2b485c0d3271b3992d24417866de9eb90f77d8f75'
            or identity.get('sourceTree') != SOURCE_TREES[role]
            or identity.get('driverCommit') != CANDIDATE
            or provenance.get('buildDriverCommit') != CANDIDATE):
        raise RuntimeError('Original identity or explicit relocation integrity mismatch')
    for key in ('sourceCommit', 'sourceTree', 'kernelOrigin', 'bytes', 'sha256', 'buildId', 'generatedVersionSha256'):
        if provenance.get(key) != identity.get(key):
            raise RuntimeError('Relocation changes original identity: ' + key)
    # A local validation view is not an identity file or a build-source relabel.
    local = dict(identity, binary=provenance['runtimeBinaryPath'])
    return validate_identity(local, role, commit, root)


def verified_runtime_record(args, plan):
    path = args.root / 'runtime/verification.json'
    no_symlinks(path)
    if digest(path) != plan.get('runtimeVerificationSha256'):
        raise RuntimeError('Runtime verification changed')
    record = json.loads(path.read_text(encoding='utf-8'))
    if (record.get('status') != 'verified_runtime_prepared'
            or record.get('runtimeRoot') != str(args.root / 'runtime')
            or record.get('verificationPath') != str(path)
            or record.get('oldValidationAccepted') is not False
            or record.get('currentValidationPerformed') is not False
            or record.get('artifact') != plan.get('artifact')
            or set(record.get('roles', {})) != {'baseline', 'candidate'}):
        raise RuntimeError('Runtime verification record mismatch')
    for role in ('baseline', 'candidate'):
        data = record['roles'][role]
        directory = args.root / 'runtime' / role
        if (data.get('binary') != str(directory / 'seekdb')
                or data.get('identityOriginalPath') != str(directory / 'identity.original.json')
                or data.get('relocationPath') != str(directory / 'relocation.json')):
            raise RuntimeError('Runtime verification role path mismatch')
    return record


def accepted_compatibility(proof, row, validation_driver=None):
    """Consume complete role-bound byte proof, never an accepted flag alone."""
    from fts_plan_compatibility import SCHEME, CASE, SPECS, TEST_SHA256, GOLDEN_SHA256, GOLDEN, TEST
    if not isinstance(proof, dict) or not isinstance(row, dict):
        return False
    context = row.get('validationContext')
    if (not isinstance(context, dict) or proof.get('validationContext') != context
            or context.get('sourceCommit') != CANDIDATE
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
    return bool(re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', str(proof.get('nativeMismatchTimestamp', ''))))


def case_evidence_complete(rows, proofs=None, validation_driver=None):
    expected = {(role, name) for role in ('baseline', 'candidate') for name in selected_cases(role)}
    if (not isinstance(rows, list) or len(rows) != len(expected)
            or any(not isinstance(row, dict) for row in rows)
            or {(row.get('role'), row.get('name')) for row in rows} != expected):
        return False
    proofs = proofs if isinstance(proofs, dict) else {}
    if set(proofs) - {'candidate/' + CASES[-1]}:
        return False
    for row in rows:
        if row.get('status') == 'passed' and row.get('cleanupPassed') is True:
            if row['role'] + '/' + row['name'] in proofs:
                return False
        elif not accepted_compatibility(proofs.get(row['role'] + '/' + row['name']), row, validation_driver):
            return False
    return True


def switch_source(source, expected_before, target):
    source_identity(source, expected_before)
    git(source, 'checkout', '--detach', target)
    return source_identity(source, target)


def no_symlinks(path):
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise RuntimeError('Symlink path forbidden: ' + str(path))


def validate_identity(identity, role, source_commit, root):
    expected = Path(root).resolve() / 'runtime' / role / 'seekdb'
    path = Path(identity.get('binary', ''))
    if role not in ('baseline', 'candidate') or identity.get('role') != role:
        raise RuntimeError('ELF role confusion')
    if (identity.get('sourceCommit') != source_commit
            or identity.get('kernelOrigin') != (BASELINE if role == 'baseline' else KERNEL)
            or not re.fullmatch('[0-9a-f]{40}', identity.get('sourceTree', ''))):
        raise RuntimeError('ELF source/kernel identity mismatch')
    no_symlinks(path)
    if not path.is_absolute() or path != expected or not path.is_file():
        raise RuntimeError('ELF is not the exact owned role path')
    if path.stat().st_nlink != 1 or path.stat().st_size > 4 * 1024 ** 3:
        raise RuntimeError('ELF hardlink or size violation')
    with path.open('rb') as stream:
        if stream.read(4) != b'\x7fELF':
            raise RuntimeError('Runtime file is not ELF')
    if identity.get('bytes') != path.stat().st_size or identity.get('sha256') != digest(path):
        raise RuntimeError('ELF bytes/hash mismatch')
    if not re.fullmatch('[0-9a-f]+', identity.get('buildId', '')):
        raise RuntimeError('Missing ELF BuildID')
    if not contains_bytes(path, source_commit.encode('ascii')):
        raise RuntimeError('Actual ELF does not contain its exact build-source revision')
    return path


def contains_bytes(path, needle):
    previous = b''
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            if needle in previous + block:
                return True
            previous = block[-len(needle):]
    return False


def capture_group(command, output, cwd, timeout, env=None, stdin=None):
    """Own an isolated process group, including mysql clients, on every outcome."""
    row = dict(command=list(map(str, command)), timeoutSeconds=timeout)
    with Path(output).open('wb') as stream:
        process = subprocess.Popen(row['command'], cwd=cwd, env=env, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            process.communicate(input=stdin, timeout=timeout)
            row['exitCode'] = process.returncode
        except subprocess.TimeoutExpired:
            row.update(exitCode=124, timedOut=True)
        finally:
            # The benchmark never intentionally detaches a process. Any remaining
            # process in this group is owned and must not overlap the next round.
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                row['childrenCleaned'] = True
            else:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=10)
                # A timed-out group is killed, but no further timing is allowed.
                row['childrenCleaned'] = False
    row.update(output=str(output), outputSha256=digest(output))
    write_json(str(output) + '.json', row)
    return row


def verify_loader(binary, log, source, env):
    row = capture_group(['ldd', str(binary)], log, source, 30, env=env)
    if row['exitCode'] or not row['childrenCleaned'] or b'not found' in Path(log).read_bytes():
        raise RuntimeError('Actual ldd failed: ' + str(binary))
    return row


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def strict_report(path, role, commit, baseline):
    fields = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'\s*([A-Za-z0-9_]+):\s*(.*?)\s*', line)
        if match:
            key, value = match.groups()
            if key in fields:
                raise RuntimeError('Duplicate report field: ' + key)
            fields[key] = value
    head = fields.get('git_head', '')
    if not re.fullmatch('[0-9a-f]{7,40}', head) or not commit.startswith(head):
        raise RuntimeError('Report git_head does not identify role source')
    if fields.get('label') != role + '-' + commit[:12]:
        raise RuntimeError('Report role label mismatch')
    required = set(baseline['baseline_metrics']) | set(CONFIG) | {'git_dirty'}
    metrics = {}
    for key in required:
        try:
            value = float(fields[key])
        except (KeyError, ValueError):
            raise RuntimeError('Missing or malformed report metric: ' + key) from None
        if not math.isfinite(value) or value < 0:
            raise RuntimeError('Nonfinite or negative report metric: ' + key)
        metrics[key] = value
    for key, value in dict(CONFIG, **HITS, git_dirty=0).items():
        if metrics[key] != value:
            raise RuntimeError('Report configuration/hits/cleanliness mismatch: ' + key)
    for names in baseline['score_categories'].values():
        for key in names:
            if metrics[key] <= 0:
                raise RuntimeError('Scored timing must be positive: ' + key)
    for key, value in metrics.items():
        if (key.endswith(('_sec', '_avg_ms', '_median_ms')) or key == 'raw_load_rows_per_sec') and value <= 0:
            raise RuntimeError('Observed timing/rate must be positive: ' + key)
    return metrics


def queries_from_benchmark(path):
    text = Path(path).read_text(encoding='utf-8')
    matches = re.findall(r'^run_query_bench "(query_(?:cn|beng|mixed|limit))" \\\n  "([^"\n]+)"\s*$', text, re.MULTILINE)
    if len(matches) != len(QUERY_NAMES) or tuple(name for name, _ in matches) != QUERY_NAMES:
        raise RuntimeError('Cannot extract exactly four original benchmark SELECT literals')
    for _, sql in matches:
        if not sql.startswith('SELECT ') or not sql.endswith(';') or '$' in sql or '\\' in sql:
            raise RuntimeError('Benchmark query is not a literal SELECT')
    return dict(matches)


def selected_cases(role):
    if role not in ('baseline', 'candidate'):
        raise RuntimeError('Native case role confusion')
    return CASES + (REGRESSIONS if role == 'candidate' else ())


def expected_plan_relevance(role, query):
    if role not in ('baseline', 'candidate'):
        raise RuntimeError('EXPLAIN role confusion')
    if query not in QUERY_NAMES + PLAN_CONTROLS:
        raise RuntimeError('Unknown EXPLAIN query expectation: ' + str(query))
    # preserve_order_for_fulltext_search() in ob_transform_pre_process.cpp
    # injects a real ORDER BY MATCH DESC into ordinary single-table MATCH
    # statements, but skips aggregate COUNT statements. The original LIMIT
    # query has that score-consuming order (also visible in its actual plan).
    # Preserve its ranked TopK; this patch does not rewrite the outer COUNT
    # consumer across a subquery. Only the three direct COUNTs become unscored.
    return role == 'baseline' or query == 'query_limit' or query in PLAN_CONTROLS


def plan_queries(queries):
    """Untimed controls derive their schema, columns and text from official SQL."""
    match = re.fullmatch(r"SELECT COUNT\(\*\) FROM (fts_large_bench\.docs) WHERE "
                        r"(MATCH\([A-Za-z_, ]+\) AGAINST\('[^'\n]+'\));", queries['query_cn'])
    if not match:
        raise RuntimeError('Cannot derive scored controls from exact benchmark query_cn')
    table, expression = match.groups()
    return dict(queries, **dict(zip(PLAN_CONTROLS, (
        'SELECT id, %s AS relevance FROM %s WHERE %s;' % (expression, table, expression),
        'SELECT id FROM %s WHERE %s ORDER BY %s DESC, id LIMIT 20;' % (table, expression, expression),
        'SELECT COUNT(*) FROM %s WHERE %s > 0.5;' % (table, expression)))))


def plan_relevance(text):
    """Read one actual text-retrieval operator annotation, never a flag echo."""
    scans = re.findall(r'^\|\s*(\d+)\s*\|[^|\n]*\bTEXT RETRIEVAL SCAN\b[^|\n]*\|[^\n]*\|\s*$', text, re.MULTILINE)
    if len(scans) != 1 or text.count('Outputs & filters:') != 1:
        raise RuntimeError('Missing or ambiguous actual TEXT RETRIEVAL SCAN plan')
    annotations = text.split('Outputs & filters:', 1)[1]
    blocks = list(re.finditer(r'^\s*(\d+)\s+-\s+output\(', annotations, re.MULTILINE))
    owned = [annotations[block.start():blocks[index + 1].start() if index + 1 < len(blocks) else len(annotations)]
             for index, block in enumerate(blocks) if block[1] == scans[0]]
    if len(owned) != 1 or text.count('calc_relevance') != 1:
        raise RuntimeError('Missing, conflicting or ambiguous calc_relevance evidence')
    values = re.findall(r'\bcalc_relevance\s*=\s*(true|false)\s*,\s*match_expr\(MATCH\(', owned[0])
    if len(values) != 1:
        raise RuntimeError('Malformed calc_relevance annotation for actual retrieval scan')
    return dict(scanId=int(scans[0]), actualCalcRelevance=values[0] == 'true',
                calcRelevanceEvidence=[line for line in owned[0].splitlines() if 'calc_relevance' in line])


class PlanEvidenceError(RuntimeError):
    def __init__(self, reason, results):
        super().__init__(reason)
        self.results = results


def accepted_plan_assertions(assertions):
    """Validate recorded proof shape and roles before accepting a calibration."""
    def mapping(value):
        return value if isinstance(value, dict) else {}
    assertions = mapping(assertions)
    if set(assertions) != {'A4', 'B4'}:
        return False
    for name, role in (('A4', 'baseline'), ('B4', 'candidate')):
        record = mapping(assertions[name])
        queries = mapping(record.get('queries'))
        if record.get('role') != role or set(queries) != set(QUERY_NAMES + PLAN_CONTROLS):
            return False
        for query in QUERY_NAMES + PLAN_CONTROLS:
            data = mapping(queries.get(query))
            expected = expected_plan_relevance(role, query)
            evidence = data.get('calcRelevanceEvidence')
            if (data.get('role') != role
                    or data.get('kind') != ('official' if query in QUERY_NAMES else 'scored_control')
                    or data.get('passed') is not True or type(data.get('exitCode')) is not int or data['exitCode'] != 0
                    or data.get('childrenCleaned') is not True
                    or data.get('expectedCalcRelevance') is not expected or data.get('actualCalcRelevance') is not expected
                    or type(data.get('scanId')) is not int or data['scanId'] < 0
                    or not re.fullmatch('[0-9a-f]{64}', str(data.get('sqlSha256', '')))
                    or not re.fullmatch('[0-9a-f]{64}', str(data.get('outputSha256', '')))
                    or not isinstance(evidence, list) or len(evidence) != 1 or not isinstance(evidence[0], str)
                    or evidence[0].count('calc_relevance') != 1
                    or not re.search(r'\bcalc_relevance\s*=\s*' + ('true' if expected else 'false')
                                     + r'\s*,\s*match_expr\(MATCH\(', evidence[0])):
                return False
    return True


def performance(pairs, baseline, scorer):
    rows = []
    for pair in pairs:
        actual = dict(baseline, baseline_metrics=pair['baselineMetrics'])
        scorer.check_config(pair['baselineMetrics'], actual, False)
        scorer.check_config(pair['candidateMetrics'], actual, False)
        score = scorer.calculate_score(pair['candidateMetrics'], actual)
        rows.append(dict(baseline=pair['baseline'], candidate=pair['candidate'],
                         unboundedImprovement=score['mean_improvement'], categories=score['categories'],
                         diagnosticClampedScore=score['score']))
    values = [row['unboundedImprovement'] for row in rows]
    if len(values) != 4 or not all(math.isfinite(value) for value in values):
        raise RuntimeError('Four valid adjacent pairs are required')
    average = statistics.mean(values)
    half_width = 3.182446305 * statistics.stdev(values) / math.sqrt(4)
    lower, upper = average - half_width, average + half_width
    classification = ('improvement_established' if lower > MARGIN else
                      'regression' if upper < -MARGIN else 'inconclusive')
    return dict(classification=classification, margin=MARGIN, meanUnboundedImprovement=average,
                descriptive95PercentInterval=[lower, upper], pairs=rows,
                intervalCaveat='Four adjacent pairs; Student-t interval assumes independent stationary pair effects. Shared-VM noise may violate this; not a population or contest guarantee.',
                formalContestScore=False, calibrationExitPolicy='Valid calibration exits 0 for all performance classifications; invalid evidence/correctness exits 1.')


def require_instance(row):
    if row.get('status') != 'passed' or row.get('cleanupPassed') is not True:
        raise RuntimeError('Owned instance failed or cleanup incomplete: ' + row.get('name', 'unknown'))


def effective_parameters(path):
    values = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        fields = line.split('\t')
        if len(fields) != 2:
            raise RuntimeError('Malformed observed DB parameter row')
        name, value = fields
        if name in values and values[name] != value:
            raise RuntimeError('Conflicting observed DB parameter: ' + name)
        values[name] = value
    expected = dict(memory_budget=4 * 1024 ** 3, vector_memory_limit=1024 ** 3,
                    datafile_maxsize=2 * 1024 ** 3, log_disk_size=2 * 1024 ** 3)
    normalized = {}
    for name, value in values.items():
        if name == 'syslog_level':
            normalized[name] = value.upper()
            continue
        match = re.fullmatch(r'([0-9]+)([KMGT]?)(?:I?B)?', value.upper())
        if not match:
            raise RuntimeError('Unrecognized observed size: ' + name + '=' + value)
        normalized[name] = int(match[1]) * 1024 ** ('KMGT'.find(match[2]) + 1 if match[2] else 0)
    if any(normalized.get(key) != value for key, value in expected.items()):
        raise RuntimeError('Effective database limits differ from requested limits')
    if 'syslog_level' not in normalized or 'datafile_size' not in normalized:
        raise RuntimeError('Missing effective logging/datafile parameters')
    return normalized


def plan_for(source, root, driver_sha):
    return dict(scope='SAME_HOST_DEVELOPMENT_CALIBRATION_NOT_FORMAL_CONTEST',
        driverCommit=driver_sha, kernelOrigin=KERNEL, baselineCommit=BASELINE,
        workspace=str(source), root=str(root), baselineSource=str(root / 'sources' / 'baseline'),
        candidateSource=str(source), schedule=[dict(name=n, role=r) for n, r in SCHEDULE],
        pairs=[dict(baseline=a, candidate=b) for a, b in PAIRS], benchmarkConfig=CONFIG,
        performanceMargin=MARGIN, profiler=False, caseRetries=0)


def prepare(args):
    root, source = args.root, args.source
    no_symlinks(root)
    if root.exists() or root == source or source in root.parents or root in source.parents:
        raise RuntimeError('Preparation needs a fresh root disjoint from source')
    identity = source_identity(source, args.driver_sha)
    equivalence = verify_driver(source, args.driver_sha)
    root.mkdir(parents=True)
    plan = plan_for(source, root, args.driver_sha)
    plan.update(driverTree=identity['tree'], equivalence=equivalence, driverFiles={})
    (root / 'driver').mkdir()
    for name in DRIVER_FILES:
        path = source / PREFIX / name
        expected = hashlib.sha256(git(source, 'show', args.driver_sha + ':' + PREFIX + name)).hexdigest()
        if digest(path) != expected:
            raise RuntimeError('Driver script hash mismatch: ' + name)
        shutil.copyfile(path, root / 'driver' / name)
        plan['driverFiles'][name] = expected
    write_json(root / 'plan.json', plan)
    for relative in ('sources', 'runtime/baseline', 'runtime/candidate', 'evidence/baseline',
                     'evidence/candidate', 'validation/cases/baseline', 'validation/cases/candidate', 'validation/rounds'):
        (root / relative).mkdir(parents=True, exist_ok=True)
    git(source, 'worktree', 'add', '--detach', str(root / 'sources' / 'baseline'), BASELINE)
    source_identity(root / 'sources' / 'baseline', BASELINE)
    switch_source(source, args.driver_sha, BASELINE)
    return plan


def prepare_reuse(args):
    root, source = args.root, args.source
    no_symlinks(root)
    if root.exists() or root == source or source in root.parents or root in source.parents:
        raise RuntimeError('Reuse preparation needs a fresh root disjoint from driver')
    identity = source_identity(source, args.driver_sha)
    equivalence = verify_reuse_driver(source, args.driver_sha)
    if args.artifact_zip is None:
        raise RuntimeError('Reuse preparation requires original artifact ZIP')
    root.mkdir(parents=True)
    plan = plan_for(source, root, args.driver_sha)
    plan.update(mode=REUSE_MODE, validationDriverCommit=args.driver_sha,
                candidateBuildCommit=CANDIDATE, candidateSource=str(root / 'sources/candidate'),
                driverTree=identity['tree'], equivalence=equivalence, driverFiles={})
    (root / 'driver').mkdir()
    for name in DRIVER_FILES:
        path = source / PREFIX / name
        expected = hashlib.sha256(git(source, 'show', args.driver_sha + ':' + PREFIX + name)).hexdigest()
        if digest(path) != expected:
            raise RuntimeError('Driver script hash mismatch: ' + name)
        shutil.copyfile(path, root / 'driver' / name)
        plan['driverFiles'][name] = expected
    for relative in ('sources', 'evidence/baseline', 'evidence/candidate',
                     'validation/cases/baseline', 'validation/cases/candidate', 'validation/rounds'):
        (root / relative).mkdir(parents=True, exist_ok=True)
    for role, commit in (('baseline', BASELINE), ('candidate', CANDIDATE)):
        path = root / 'sources' / role
        git(source, 'worktree', 'add', '--detach', str(path), commit)
        if source_identity(path, commit)['tree'] != SOURCE_TREES[role]:
            raise RuntimeError('Actual build source tree mismatch')
    write_json(root / 'plan.json', plan)
    loader = load_module('paired_verified_loader', root / 'driver/fts_verified_elf_reuse.py')
    verified = loader.prepare_verified_runtime(args.artifact_zip, root / 'runtime')
    plan['runtimeVerificationSha256'] = digest(root / 'runtime/verification.json')
    plan['artifact'] = verified['artifact']
    source_identity(source, args.driver_sha)
    write_json(root / 'plan.json', plan)
    return plan


def load_plan(args):
    no_symlinks(args.root)
    plan = json.loads((args.root / 'plan.json').read_text(encoding='utf-8'))
    if (plan['driverCommit'] != args.driver_sha or plan['workspace'] != str(args.source)
            or plan['root'] != str(args.root) or plan['baselineCommit'] != BASELINE
            or plan['kernelOrigin'] != KERNEL):
        raise RuntimeError('Saved plan identity mismatch')
    for name, expected in plan['driverFiles'].items():
        if name not in DRIVER_FILES or digest(args.root / 'driver' / name) != expected:
            raise RuntimeError('Saved driver script changed')
    if set(plan['driverFiles']) != set(DRIVER_FILES):
        raise RuntimeError('Incomplete saved driver identity')
    if plan.get('mode') == REUSE_MODE:
        if (plan.get('validationDriverCommit') != args.driver_sha or plan.get('candidateBuildCommit') != CANDIDATE
                or plan.get('baselineSource') != str(args.root / 'sources/baseline')
                or plan.get('candidateSource') != str(args.root / 'sources/candidate')
                or plan.get('runtimeVerificationSha256') != digest(args.root / 'runtime/verification.json')):
            raise RuntimeError('Reuse source/verification plan mismatch')
    return plan


def runtime_env(source, binary):
    paths = [Path(binary).parent, source / 'deps/3rd/u01/obclient/lib',
             source / 'deps/3rd/u01/obclient/lib64', source / 'deps/3rd/usr/local/oceanbase/deps/devel/lib',
             source / 'deps/3rd/usr/local/oceanbase/deps/devel/lib64']
    env = os.environ.copy()
    env['LD_LIBRARY_PATH'] = ':'.join(map(str, paths))
    env['PATH'] = str(source / 'deps/3rd/u01/obclient/bin') + os.pathsep + env.get('PATH', '')
    return env


def snapshot(args, plan):
    role = args.role
    commit = BASELINE if role == 'baseline' else args.driver_sha
    source = args.source
    source_id = source_identity(source, commit)
    verify_driver(source, args.driver_sha)
    harness = load_module('paired_original_harness', args.root / 'driver/fts_contest_validation.py')
    inputs = harness.original_inputs(source)
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
        kernelOrigin=BASELINE if role == 'baseline' else KERNEL, driverCommit=args.driver_sha,
        binary=str(destination), bytes=destination.stat().st_size, sha256=digest(destination), buildId=ids[0],
        generatedVersionSha256=digest(version_path), officialInputHashes=inputs,
        buildSettings={key: cache[key] for key in ('CMAKE_BUILD_TYPE', 'OB_ENABLE_UNITY', 'OB_USE_LLD')},
        rustVersion=rust)
    validate_identity(record, role, commit, args.root)
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


def explain_queries(args, call, out, env):
    if args.role not in ('baseline', 'candidate'):
        raise RuntimeError('EXPLAIN role confusion')
    queries = plan_queries(queries_from_benchmark(args.source / (BENCH + '.sh')))
    results = {}
    for name, sql in queries.items():
        query = SESSION + 'EXPLAIN ' + sql + '\n'
        sql_path = out / (name + '-explain.sql')
        sql_path.write_text(query, encoding='utf-8')
        log_path = out / (name + '-explain.log')
        failure = None
        try:
            result = capture_group([str(args.obclient), '-h127.0.0.1', '-P' + str(call.port),
                '-uroot', '-A', '-N', '-B', '--raw', '--default-character-set=utf8mb4'], log_path,
                args.source, 60, env=env, stdin=query.encode('utf-8'))
        except (OSError, subprocess.SubprocessError) as error:
            result = dict(exitCode=None, childrenCleaned=False)
            failure = 'Untimed EXPLAIN process error: ' + str(error)
        text = log_path.read_text(encoding='utf-8', errors='replace') if log_path.is_file() else ''
        result.update(role=args.role, kind='official' if name in QUERY_NAMES else 'scored_control',
            sqlSha256=digest(sql_path), output=str(log_path),
            outputSha256=digest(log_path) if log_path.is_file() else None,
            expectedCalcRelevance=expected_plan_relevance(args.role, name), passed=False,
            calcRelevanceEvidence=[line for line in text.splitlines() if 'calc_relevance' in line])
        if failure is None:
            if type(result.get('exitCode')) is not int or result['exitCode'] != 0 or result.get('childrenCleaned') is not True:
                failure = 'Untimed EXPLAIN process failed or cleanup incomplete'
            else:
                try:
                    result.update(plan_relevance(text))
                    if result['actualCalcRelevance'] != result['expectedCalcRelevance']:
                        raise RuntimeError('Actual calc_relevance differs from required role/query behavior')
                    result['passed'] = True
                except RuntimeError as error:
                    failure = str(error)
        if failure is not None:
            result['failureReason'] = failure
        results[name] = result
        write_json(out / 'explain-result.json', results)
        print('PAIRED_EXPLAIN ' + json.dumps(dict(query=name, **result)), flush=True)
        print(text[:24000], flush=True)
        if failure is not None:
            raise PlanEvidenceError('Untimed EXPLAIN assertion failed for %s/%s: %s' % (args.role, name, failure), results)
    return results


def benchmark_work(args, baseline, scorer, final_role_round):
    def execute(call, out):
        env = runtime_env(args.workspace, args.binary)
        base = [str(args.obclient), '-h127.0.0.1', '-P' + str(call.port), '-uroot', '-A', '--default-character-set=utf8mb4']
        # The original script expands MYSQL as shell words; runner-owned paths
        # contain no whitespace or shell metacharacters.
        if any(re.search(r'[^A-Za-z0-9_./:=+-]', value) for value in base):
            raise RuntimeError('Client command contains unsupported shell-word characters')
        env.update(MYSQL=' '.join(base + ['-N', '-s']), MYSQL_VERBOSE=' '.join(base),
                   LABEL=args.role + '-' + args.expected_sha[:12], OUTPUT=str(out / 'report.txt'),
                   **{key.upper(): str(value) for key, value in CONFIG.items()})
        time.sleep(SETTLE_SECONDS)
        invocation = capture_group(['bash', str(args.source / (BENCH + '.sh'))],
            out / 'benchmark.log', args.source, 1800, env=env)
        if invocation['exitCode'] or not invocation['childrenCleaned']:
            return dict(passed=False, invocation=invocation, failureKind='benchmark_process_failure')
        metrics = strict_report(out / 'report.txt', args.role, args.expected_sha, baseline)
        scorer.check_config(metrics, baseline, False)
        frozen = capture_group([sys.executable, str(args.source / (BENCH + '_score.py')),
            str(out / 'report.txt'), '--json'], out / 'frozen-score.json', args.source, 30, env=env)
        if frozen['exitCode'] or not frozen['childrenCleaned']:
            return dict(passed=False, invocation=invocation, frozenScore=frozen, failureKind='official_scorer_failure')
        result = dict(passed=True, metrics=metrics, invocation=invocation,
            informativeFrozenBaselineScore=json.loads((out / 'frozen-score.json').read_text(encoding='utf-8')),
            frozenBaselineCaveat='Different-host reference only, not same-host speedup or formal contest score')
        if final_role_round:
            try:
                result['explain'] = explain_queries(args, call, out, env)
            except PlanEvidenceError as error:
                # Return evidence through run_instance so its normal cleanup and
                # report persistence precede the enclosing validation exception.
                result.update(passed=False, explain=error.results,
                    failureKind='untimed_plan_assertion_failure', failureReason=str(error))
        return result
    return execute


def small_report(report):
    return {key: report[key] for key in ('scope', 'status', 'driverCommit', 'kernelOrigin',
        'baselineCommit', 'builds', 'caseVerdicts', 'roundVerdicts', 'sameHostPerformance',
        'failure', 'formalContestRun', 'allOfficialInputsUnchanged', 'planAssertions',
        'mode', 'validationDriverCommit', 'candidateBuildCommit', 'supplementalCompatibility', 'artifactReuse') if key in report}


def presentation_lines(report):
    """Read existing results only; never recalculate scores or change the report."""
    report = report if isinstance(report, dict) else {}
    lines = []

    def add(text):
        # Escape controls, retain the full text in bounded continuation lines.
        text = json.dumps(str(text), ensure_ascii=True)[1:-1]
        if len(text) <= 480:
            lines.append(text)
        else:
            chunks = [text[i:i + 440] for i in range(0, len(text), 440)]
            lines.extend('Continuation %d/%d: %s' % (i + 1, len(chunks), chunk)
                         for i, chunk in enumerate(chunks))

    def number(value, percent=False):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 'unavailable'
        try:
            value = float(value)
        except OverflowError:
            return 'unavailable'
        if not math.isfinite(value):
            return 'unavailable'
        scaled = value * 100 if percent else value
        if not math.isfinite(scaled):
            return 'unavailable'
        return format(scaled, '+.9g' if percent else '.12g') + ('%' if percent else '')

    def mapping(value):
        return value if isinstance(value, dict) else {}

    def sequence(value):
        return value if isinstance(value, list) else []

    perf = mapping(report.get('sameHostPerformance'))
    pairs = sequence(perf.get('pairs'))
    cases = sequence(report.get('caseVerdicts'))
    rounds = sequence(report.get('roundVerdicts'))
    builds = mapping(report.get('builds'))
    expected_categories = {
        'build': ('build_ik_all_sec', 'build_ik_content_sec', 'build_beng_en_sec'),
        'tokenize': ('tokenize_ik_avg_ms', 'tokenize_beng_avg_ms'),
        'query': ('query_cn_avg_ms', 'query_beng_avg_ms', 'query_mixed_avg_ms', 'query_limit_avg_ms')}
    expected_cases = {(role, name) for role in ('baseline', 'candidate')
                      for name in selected_cases(role)}
    expected_rounds = {(role, name) for name, role in SCHEDULE}
    assertions = mapping(report.get('planAssertions'))
    plans_valid = accepted_plan_assertions(assertions)

    def complete(rows, expected):
        return (len(rows) == len(expected)
                and {(mapping(row).get('role'), mapping(row).get('name')) for row in rows} == expected
                and all(mapping(row).get('status') == 'passed' and mapping(row).get('cleanupPassed') is True for row in rows))

    valid = (report.get('status') == 'valid_calibration_completed'
             and report.get('allOfficialInputsUnchanged') is True
             and report.get('baselineCommit') == BASELINE and report.get('kernelOrigin') == KERNEL
             and mapping(builds.get('baseline')).get('sourceCommit') == BASELINE
             and mapping(builds.get('candidate')).get('sourceCommit') == (CANDIDATE if report.get('mode') == REUSE_MODE else report.get('driverCommit'))
             and (report.get('mode') != REUSE_MODE or (report.get('validationDriverCommit') == report.get('driverCommit')
                  and report.get('candidateBuildCommit') == CANDIDATE))
             and bool(re.fullmatch('[0-9a-f]{40}', str(report.get('driverCommit', ''))))
             and case_evidence_complete(cases, report.get('supplementalCompatibility'), report.get('driverCommit')) and complete(rounds, expected_rounds) and plans_valid
             and perf.get('classification') in ('improvement_established', 'regression', 'inconclusive')
             and [(mapping(row).get('baseline'), mapping(row).get('candidate')) for row in pairs] == list(PAIRS))
    interval = sequence(perf.get('descriptive95PercentInterval'))
    required_numbers = [perf.get('meanUnboundedImprovement'), perf.get('margin'), *interval]
    valid = valid and len(interval) == 2
    for row in pairs:
        row = mapping(row)
        required_numbers.append(row.get('unboundedImprovement'))
        categories = mapping(row.get('categories'))
        valid = valid and set(categories) == set(expected_categories)
        for category, names in expected_categories.items():
            data = mapping(categories.get(category))
            metrics = sequence(data.get('metrics'))
            valid = valid and [mapping(item).get('metric') for item in metrics] == list(names)
            required_numbers.append(data.get('improvement'))
            for item in metrics:
                item = mapping(item)
                required_numbers.extend(item.get(key) for key in ('baseline', 'current', 'improvement'))
    valid = valid and all(number(value) != 'unavailable' for value in required_numbers)
    if valid:
        lower, upper = interval
        expected_classification = ('improvement_established' if lower > MARGIN else
                                   'regression' if upper < -MARGIN else 'inconclusive')
        valid = (perf['margin'] == MARGIN and lower <= perf['meanUnboundedImprovement'] <= upper
                 and perf['classification'] == expected_classification)
    add('Status: ' + str(report.get('status', 'report unavailable')))
    add('Paired development classification: ' + (perf['classification'] if valid else 'unavailable; calibration invalid, incomplete, or malformed'))
    add('Unbounded mean: %s; descriptive 95%% interval: [%s, %s]; predeclared margin: %s.' % (
        number(perf.get('meanUnboundedImprovement'), True),
        number(interval[0], True) if len(interval) > 0 else 'unavailable',
        number(interval[1], True) if len(interval) > 1 else 'unavailable', number(perf.get('margin'), True)))
    add('A actual source: ' + str(mapping(builds.get('baseline')).get('sourceCommit', report.get('baselineCommit', 'unavailable'))))
    add('B actual build source: ' + str(mapping(builds.get('candidate')).get('sourceCommit', 'unavailable')))
    add('B kernel origin: ' + str(report.get('kernelOrigin', 'unavailable')) + '; driver source: ' + str(report.get('driverCommit', 'unavailable')))
    for role in ('baseline', 'candidate'):
        total = len(selected_cases(role))
        selected = [mapping(row) for row in cases if mapping(row).get('role') == role]
        if role == 'candidate' and report.get('mode') == REUSE_MODE:
            originals = [row for row in selected if row.get('name') in CASES]
            extras = [row for row in selected if row.get('name') in REGRESSIONS]
            compatible = sum(accepted_compatibility(mapping(report.get('supplementalCompatibility')).get('candidate/' + row.get('name', '')), row, report.get('driverCommit')) for row in originals)
            add('Correctness candidate originals raw: %d/5 native passes; plan-compatible: %d/1; extra regressions: %d/2 native passes (61 new assertions). No native 5/5 or 7/7 claim.' % (sum(row.get('status') == 'passed' for row in originals), compatible, sum(row.get('status') == 'passed' for row in extras)))
            simple = [row for row in originals if row.get('name') == CASES[-1]]
            add('Candidate simple_query raw native status: ' + str(simple[0].get('status') if len(simple) == 1 else 'unavailable') + '; supplemental compatibility never changes a native FAIL.')
        else:
            add('Correctness %s: %d/%d selected cases passed; %d recorded. No full-suite claim.' % (
                role, sum(row.get('status') == 'passed' for row in selected), total, len(selected)))
    verdicts = [mapping(row) for row in cases + rounds]
    add('Timed rounds: %d/%d passed; %d recorded. Owned DB cleanup: %d/%d recorded instances passed (%d expected).' % (
        sum(mapping(row).get('status') == 'passed' for row in rounds), len(SCHEDULE), len(rounds),
        sum(row.get('cleanupPassed') is True for row in verdicts), len(verdicts), len(expected_cases) + len(SCHEDULE)))
    add('Untimed actual-plan assertions: ' + ('passed for A4/B4; three candidate COUNT queries unscored; protected implicit-ranked LIMIT and three score-consuming controls scored.'
        if plans_valid else 'unavailable or failed; no accepted no-score evidence.'))
    for name, record in assertions.items():
        record = mapping(record)
        for query, data in mapping(record.get('queries')).items():
            data = mapping(data)
            if data.get('passed') is not True:
                add('Untimed plan failure %s/%s/%s: %s' % (name, record.get('role'), query,
                    data.get('failureReason', 'incomplete or malformed plan evidence')))
    failure = mapping(report.get('failure'))
    if failure:
        add('Failure phase: ' + str(failure.get('phase', 'unavailable')))
        add('Failure reason: ' + str(failure.get('reason', 'unavailable')))
    for row in verdicts:
        if row.get('status') != 'passed' or row.get('cleanupPassed') is not True:
            add('Incomplete instance: %s/%s status=%s cleanup=%s' % (
                row.get('role'), row.get('name'), row.get('status'), row.get('cleanupPassed')))
    if not valid:
        add('No improvement claim. Any available paired numbers below are retained diagnostics, not an accepted calibration.')
    for index, row in enumerate(pairs, 1):
        row = mapping(row)
        pair = '%s/%s' % (row.get('baseline', '?'), row.get('candidate', '?'))
        add('Pair %d %s: unbounded improvement=%s (every pair retained).' % (index, pair, number(row.get('unboundedImprovement'), True)))
        for category, data in mapping(row.get('categories')).items():
            data = mapping(data)
            add('Pair %s category %s: improvement=%s.' % (pair, category, number(data.get('improvement'), True)))
            for item in sequence(data.get('metrics')):
                item = mapping(item)
                add('Pair %s metric %s: A=%s B=%s improvement=%s.' % (pair, item.get('metric', '?'),
                    number(item.get('baseline')), number(item.get('current')), number(item.get('improvement'), True)))
    for row in sequence(report.get('instances')):
        row = mapping(row)
        frozen = mapping(mapping(row.get('work')).get('informativeFrozenBaselineScore'))
        if frozen:
            add('Frozen reference %s: score=%s/100; different-host reference only, separate from paired development.' % (
                row.get('name', '?'), number(frozen.get('score'))))
    add('No formal contest score is produced by this workflow. Frozen reference scores are not same-host improvements.')
    add('Interval caveat: four adjacent pairs; independent/stationary effects assumed. Shared-VM noise can violate this; no population guarantee.')
    return lines


def present_report(report, summary=None):
    lines = presentation_lines(report)
    for line in lines:
        print('FTS_RESULT ' + line, flush=True)
    if summary is not None:
        with Path(summary).open('a', encoding='utf-8') as stream:
            stream.write('\n## FTS same-host result\n\n')
            stream.writelines('- ' + line + '\n' for line in lines)


def persist_report(root, report):
    write_json(root / 'report.json', report)
    print('PAIRED_REPORT ' + json.dumps(small_report(report), ensure_ascii=False, allow_nan=False), flush=True)


def install_termination_handlers():
    def terminate_owned_work(signum, frame):
        raise InterruptedError('Termination requested; stop owned work and preserve partial calibration')
    signal.signal(signal.SIGTERM, terminate_owned_work)
    signal.signal(signal.SIGINT, terminate_owned_work)


def validate(args, plan, report):
    harness = load_module('paired_original_harness', args.root / 'driver/fts_contest_validation.py')
    scorer = load_module('paired_official_scorer', args.source / (BENCH + '_score.py'))
    baseline = scorer.load_baseline(args.source / (BENCH + '_baseline.json'))
    reuse = plan.get('mode') == REUSE_MODE
    if reuse:
        verify_reuse_driver(args.source, args.driver_sha)
        verified = verified_runtime_record(args, plan)
        report.update(mode=REUSE_MODE, validationDriverCommit=args.driver_sha, candidateBuildCommit=CANDIDATE,
                      artifactReuse=dict(artifact=plan['artifact'], verificationSha256=plan['runtimeVerificationSha256'],
                          oldValidationAccepted=False, historicalCollectorPassed=False))
    else:
        verify_driver(args.source, args.driver_sha)
    roles = {}
    pinned = {}
    for role in ('baseline', 'candidate'):
        source = Path(plan[role + 'Source'])
        commit = BASELINE if role == 'baseline' else CANDIDATE if reuse else args.driver_sha
        identity = json.loads((args.root / 'runtime' / role / ('identity.original.json' if reuse else 'identity.json')).read_text(encoding='utf-8'))
        provenance = json.loads((args.root / 'runtime' / role / 'relocation.json').read_text(encoding='utf-8')) if reuse else None
        if reuse and (verified['roles'][role].get('identity') != identity
                      or verified['roles'][role].get('provenance') != provenance):
            raise RuntimeError('Original identity/relocation differs from verified runtime record')
        source_id = source_identity(source, commit)
        if source_id['tree'] != identity['sourceTree']:
            raise RuntimeError('Saved source tree differs from exact role snapshot')
        binary = validate_relocated_identity(identity, role, args.root, provenance) if reuse else validate_identity(identity, role, commit, args.root)
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
        if reuse:
            source_identity(args.source, args.driver_sha)
            if digest(args.root / 'runtime/verification.json') != plan['runtimeVerificationSha256']:
                raise RuntimeError('Runtime verification changed')
            validate_relocated_identity(identity, role, args.root, provenance)
        else:
            validate_identity(identity, role, call_args.expected_sha, args.root)
        for client, expected in CLIENTS.items():
            if digest(getattr(call_args, client)) != expected:
                raise RuntimeError('Official native client changed during validation')
        if harness.original_inputs(call_args.source) != pinned[role]:
            raise RuntimeError('Official inputs changed during validation')

    def run(role, name, work, cases=False):
        guard(role)
        call_args, native, _, _ = roles[role]
        call_args.out = args.root / 'validation' / ('cases/' + role if cases else 'rounds')
        env = runtime_env(args.source, call_args.binary)
        old_library, old_path = os.environ.get('LD_LIBRARY_PATH'), os.environ.get('PATH')
        os.environ.update(LD_LIBRARY_PATH=env['LD_LIBRARY_PATH'], PATH=env['PATH'])
        def checked_work(call, out):
            observed = effective_parameters(out / 'actual-parameters.log')
            stage = 'cases' if cases else 'rounds'
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
        if cases:
            verdict['work'] = row.get('work', {})
            for key in ('error', 'traceback', 'cleanupError', 'timedOut', 'signal', 'failureKind', 'failureReason'):
                if key in row:
                    verdict[key] = row[key]
        report['caseVerdicts' if cases else 'roundVerdicts'].append(verdict)
        proof = None
        if reuse and cases and role == 'candidate' and name == CASES[-1] and row.get('status') == 'failed':
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
            context = dict(sourceCommit=call_args.expected_sha, validationDriverCommit=args.driver_sha,
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
        if not (reuse and proof is not None and accepted_compatibility(proof, verdict, args.driver_sha)):
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
            run(role, name, harness.original_case(call_args, native, available[name]), cases=True)
    if not case_evidence_complete(report['caseVerdicts'], report.get('supplementalCompatibility'), args.driver_sha):
        raise RuntimeError('Complete native cases and strict supplemental proof required')
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


def collect_continuation_evidence(root, out):
    """Copy only new bounded evidence. Never include ELF, DB, core or env files."""
    root = Path(root)
    if out is None:
        raise RuntimeError('Evidence collection requires a fresh output directory')
    out = Path(out)
    no_symlinks(root)
    no_symlinks(out)
    if not root.is_dir() or out.exists() or root == out or root in out.parents or out in root.parents:
        raise RuntimeError('Evidence output must be fresh and disjoint')
    selected = []
    def select(path, cap=2 * 1024 ** 2):
        if path.is_symlink():
            raise RuntimeError('Evidence symlink rejected: ' + str(path))
        if not path.exists():
            return
        no_symlinks(path)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > cap:
            raise RuntimeError('Evidence file type/link/size violation: ' + str(path))
        if len(selected) >= 512:
            raise RuntimeError('Evidence entry cap exceeded')
        selected.append((path, info, digest(path)))
    for name in ('plan.json', 'report.json'):
        select(root / name, 8 * 1024 ** 2)
    for name in ('manifest.original.json', 'plan.original.json', 'verification.json', 'verification.failed.json'):
        select(root / 'runtime' / name, 8 * 1024 ** 2)
    for role in ('baseline', 'candidate'):
        for name in ('identity.original.json', 'relocation.json', 'readelf-notes.log'):
            select(root / 'runtime' / role / name, 8 * 1024 ** 2)
        for name in ('runtime-ldd.log', 'runtime-ldd.log.json', 'obclient-runtime-ldd.log',
                     'obclient-runtime-ldd.log.json', 'mysqltest-runtime-ldd.log',
                     'mysqltest-runtime-ldd.log.json', 'resources-before.txt', 'resources-after.txt'):
            select(root / 'evidence' / role / name)
    instances = [root / 'validation/cases' / role / name for role in ('baseline', 'candidate')
                 for name in selected_cases(role)] + [root / 'validation/rounds' / name for name, _ in SCHEDULE]
    names = {'result.json', 'owned-process.json', 'start.log', 'ready.log', 'original-init.log',
             'actual-parameters.log', 'own-stop.log', 'own-destroy.log', 'mysqltest-output.log',
             'original-run-case.log', 'original-result-diff.log', 'supplemental-plan-compatibility.json',
             'benchmark.log', 'benchmark.log.json', 'report.txt', 'frozen-score.json',
             'frozen-score.json.json', 'explain-result.json'}
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
        destination = out / path.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        after = path.stat()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or digest(path) != expected or digest(destination) != expected):
            raise RuntimeError('Evidence changed while copying: ' + str(path))
        records.append(dict(path=str(path.relative_to(root)), bytes=before.st_size, sha256=expected))
    manifest = dict(schema=1, kind='new-continuation-evidence-only', files=records, bytes=total,
        unchangedElfsReuploaded=False, excludes=['database', 'core', 'environment', 'db-log-tails'],
        historicalCollectorPassed=False,
        historicalCollectorFailure='Original collector independently failed on baseline/cn_word non-text DB log; no old collector pass is claimed.',
        maxFileBytes=8 * 1024 ** 2, maxTotalBytes=64 * 1024 ** 2, maxEntries=512)
    write_json(out / 'continuation-evidence-manifest.json', manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('plan', 'prepare', 'snapshot', 'switch-candidate', 'validate', 'finalize', 'present', 'prepare-reuse', 'collect-evidence'), default='plan')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--driver-sha', required=True)
    parser.add_argument('--role', choices=('baseline', 'candidate'))
    parser.add_argument('--summary', type=Path)
    parser.add_argument('--artifact-zip', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    args.source = args.source.resolve(strict=True)
    no_symlinks(args.root)
    args.root = args.root.resolve(strict=False)
    if args.phase == 'collect-evidence':
        collect_continuation_evidence(args.root, args.out)
        return 0
    if args.phase == 'present':
        try:
            report = json.loads((args.root / 'report.json').read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            report = dict(status='invalid_calibration', failure=dict(phase='presentation', reason=str(error)))
        present_report(report, args.summary)
        return 0
    if not args.execute or args.phase == 'plan':
        print(json.dumps(plan_for(args.source, args.root, args.driver_sha), indent=2))
        return 0
    if sys.platform != 'linux':
        raise RuntimeError('Actual paired execution requires Linux')
    install_termination_handlers()
    report = dict(scope='SAME_HOST_DEVELOPMENT_CALIBRATION_NOT_FORMAL_CONTEST', status='running',
        driverCommit=args.driver_sha, kernelOrigin=KERNEL, baselineCommit=BASELINE,
        phase=args.phase, formalContestRun=False, builds={}, instances=[], caseVerdicts=[],
        roundVerdicts=[], roundMetrics={})
    try:
        if args.phase == 'prepare-reuse':
            prepare_reuse(args)
        elif args.phase == 'prepare':
            prepare(args)
        else:
            plan = load_plan(args)
            if (args.root / 'report.json').is_file():
                report = json.loads((args.root / 'report.json').read_text(encoding='utf-8'))
                report['phase'] = args.phase
            if args.phase == 'snapshot':
                if not args.role:
                    raise RuntimeError('Snapshot requires an explicit role')
                record = snapshot(args, plan)
                report['builds'][args.role] = {key: record[key] for key in ('sourceCommit', 'sourceTree', 'kernelOrigin', 'driverCommit', 'sha256', 'bytes', 'buildId')}
            elif args.phase == 'switch-candidate':
                identity = json.loads((args.root / 'runtime/baseline/identity.json').read_text(encoding='utf-8'))
                validate_identity(identity, 'baseline', BASELINE, args.root)
                switch_source(args.source, BASELINE, args.driver_sha)
                verify_driver(args.source, args.driver_sha)
            elif args.phase == 'finalize':
                if report.get('status') != 'valid_calibration_completed':
                    report.update(status='invalid_calibration', failure=report.get('failure', dict(
                        phase=report.get('phase'), reason='Workflow ended before a complete valid calibration; inspect preserved phase/build logs')))
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
        else:
            print('PAIRED_REPORT ' + json.dumps(small_report(report)), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
