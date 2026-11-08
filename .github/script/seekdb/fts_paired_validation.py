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
import subprocess
import sys
import time
import traceback

sys.dont_write_bytecode = True
BASELINE = '5bce9e06d1281347ad61344e4b01fab9158c58a2'
KERNEL = '8db524bb0a3a847b4eb384232bd576761e4075b3'
PREFIX = '.github/script/seekdb/'
ALLOWED_DRIVER_PATHS = frozenset((
    '.github/workflows/fts-contest-validation.yml',
    PREFIX + 'fts_paired_validation.py', PREFIX + 'fts_paired_collect.py',
    PREFIX + 'test_fts_paired_validation.py', PREFIX + 'test_fts_paired_collect.py'))
DRIVER_FILES = ('fts_paired_validation.py', 'fts_paired_collect.py', 'fts_contest_validation.py')
CASES = ('fts_index.tokenize_function', 'fts_index.cn_word',
         'fts_index.parser_properties', 'fts_index.basic_dml', 'fts_index.simple_query')
REGRESSION = 'fts_index.tokenize_casefold_contract'
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
    if len(matches) != 4 or [name for name, _ in matches] != ['query_cn', 'query_beng', 'query_mixed', 'query_limit']:
        raise RuntimeError('Cannot extract exactly four original benchmark SELECT literals')
    for _, sql in matches:
        if not sql.startswith('SELECT ') or not sql.endswith(';') or '$' in sql or '\\' in sql:
            raise RuntimeError('Benchmark query is not a literal SELECT')
    return dict(matches)


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
    queries = queries_from_benchmark(args.source / (BENCH + '.sh'))
    results = {}
    for name, sql in queries.items():
        query = SESSION + 'EXPLAIN ' + sql + '\n'
        sql_path = out / (name + '-explain.sql')
        sql_path.write_text(query, encoding='utf-8')
        result = capture_group([str(args.obclient), '-h127.0.0.1', '-P' + str(call.port),
            '-uroot', '-A', '--default-character-set=utf8mb4'], out / (name + '-explain.log'),
            args.source, 60, env=env, stdin=query.encode('utf-8'))
        text = (out / (name + '-explain.log')).read_text(encoding='utf-8', errors='replace')
        result.update(sqlSha256=digest(sql_path), calcRelevanceEvidence=[
            line for line in text.splitlines() if 'calc_relevance' in line],
            calcRelevanceInterpretation='visible plan text only; absent means unknown')
        results[name] = result
        write_json(out / 'explain-result.json', results)
        print('PAIRED_EXPLAIN ' + json.dumps(dict(role=args.role, query=name, **result)), flush=True)
        print(text[:24000], flush=True)
        if result['exitCode'] or not result['childrenCleaned']:
            raise RuntimeError('Untimed exact-query EXPLAIN failed: ' + name)
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
            result['explain'] = explain_queries(args, call, out, env)
        return result
    return execute


def small_report(report):
    return {key: report[key] for key in ('scope', 'status', 'driverCommit', 'kernelOrigin',
        'baselineCommit', 'builds', 'caseVerdicts', 'roundVerdicts', 'sameHostPerformance',
        'failure', 'formalContestRun', 'allOfficialInputsUnchanged') if key in report}


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
    verify_driver(args.source, args.driver_sha)
    roles = {}
    pinned = {}
    for role in ('baseline', 'candidate'):
        source = Path(plan[role + 'Source'])
        commit = BASELINE if role == 'baseline' else args.driver_sha
        identity = json.loads((args.root / 'runtime' / role / 'identity.json').read_text(encoding='utf-8'))
        source_id = source_identity(source, commit)
        if source_id['tree'] != identity['sourceTree']:
            raise RuntimeError('Saved source tree differs from exact role snapshot')
        binary = validate_identity(identity, role, commit, args.root)
        role_args = argparse.Namespace(role=role, source=source, expected_sha=commit, binary=binary,
            workspace=args.source, obclient=args.source / 'deps/3rd/u01/obclient/bin/obclient',
            mysqltest=args.source / 'deps/3rd/u01/obclient/bin/mysqltest')
        env = runtime_env(args.source, binary)
        verify_loader(binary, args.root / 'evidence' / role / 'runtime-ldd.log', source, env)
        for client, expected in CLIENTS.items():
            path = getattr(role_args, client)
            if digest(path) != expected:
                raise RuntimeError('Official native client changed')
        pinned[role] = harness.original_inputs(source)
        if pinned[role] != identity['officialInputHashes']:
            raise RuntimeError('Official input hashes differ from build-time identity')
        roles[role] = (role_args, harness.load_native(source), identity)
        report['builds'][role] = {key: identity[key] for key in ('sourceCommit', 'sourceTree', 'kernelOrigin', 'driverCommit', 'sha256', 'bytes', 'buildId')}
    persist_report(args.root, report)

    def guard(role):
        call_args, _, identity = roles[role]
        source_identity(call_args.source, call_args.expected_sha)
        validate_identity(identity, role, call_args.expected_sha, args.root)
        if harness.original_inputs(call_args.source) != pinned[role]:
            raise RuntimeError('Official inputs changed during validation')

    def run(role, name, work, cases=False):
        guard(role)
        call_args, native, _ = roles[role]
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
        report['caseVerdicts' if cases else 'roundVerdicts'].append(verdict)
        if not cases and 'metrics' in row.get('work', {}):
            report['roundMetrics'][name] = row['work']['metrics']
        persist_report(args.root, report)
        require_instance(row)
        guard(role)
        return row

    for role in ('baseline', 'candidate'):
        call_args, native, _ = roles[role]
        available = {case.name: case for case in native.discover_cases(call_args.source)}
        selected = CASES + ((REGRESSION,) if role == 'candidate' else ())
        if any(name not in available for name in selected):
            raise RuntimeError('Required native case unavailable/excluded')
        for name in selected:
            run(role, name, harness.original_case(call_args, native, available[name]), cases=True)
    time.sleep(30)
    for name, role in SCHEDULE:
        call_args = roles[role][0]
        run(role, name, benchmark_work(call_args, baseline, scorer, name in ('A4', 'B4')))
    pairs = [dict(baseline=a, candidate=b, baselineMetrics=report['roundMetrics'][a],
                  candidateMetrics=report['roundMetrics'][b]) for a, b in PAIRS]
    report['sameHostPerformance'] = performance(pairs, baseline, scorer)
    for role in roles:
        guard(role)
    report.update(status='valid_calibration_completed', allOfficialInputsUnchanged=True)
    persist_report(args.root, report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('plan', 'prepare', 'snapshot', 'switch-candidate', 'validate', 'finalize'), default='plan')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--driver-sha', required=True)
    parser.add_argument('--role', choices=('baseline', 'candidate'))
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    args.source = args.source.resolve(strict=True)
    no_symlinks(args.root)
    args.root = args.root.resolve(strict=False)
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
        if args.phase == 'prepare':
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
