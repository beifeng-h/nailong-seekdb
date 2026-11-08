#!/usr/bin/env python3
"""Plan by default; validate the already-built candidate with untouched FTS inputs.

This is development CI, not formal contest submission or a same-host A/B result.
It never builds, patches production/tests, retries cases, or changes score rules.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import argparse
import difflib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import traceback
from types import SimpleNamespace

REFERENCE = '5bce9e06d1281347ad61344e4b01fab9158c58a2'
CASES = ('fts_index.tokenize_function', 'fts_index.cn_word',
         'fts_index.parser_properties', 'fts_index.basic_dml', 'fts_index.simple_query',
         'fts_index.tokenize_casefold_contract')
NATIVE = '.github/script/seekdb/mysqltest_for_seekdb.py'
SDB = '.github/script/seekdb/sdb.py'
BENCH = 'tools/benchmark/fts_large_bench'
PARAMETERS = ('memory_budget=4G', 'vector_memory_limit=1G',
              'datafile_maxsize=2G', 'log_disk_size=2G')
BENCH_CONFIG = dict(rows=20000, batch=500, rounds=3000, query_rounds=200,
                    samples=3, warmup=30, skip_load=0)
ACTIVE_REPORT = None


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def emit(label, value):
    print(label + ' ' + json.dumps(value, sort_keys=True, ensure_ascii=False), flush=True)


def show_log(path, limit=16000):
    if path.is_file():
        with path.open('rb') as stream:
            size = path.stat().st_size
            stream.seek(max(0, size - limit))
            data = stream.read(limit)
        print('\n--- %s (%d bytes, showing tail up to %d) ---' % (path, size, limit), flush=True)
        print(data.decode('utf-8', 'replace'), flush=True)


def git(source, *arguments):
    return subprocess.check_output(['git', '-C', str(source), *arguments],
                                   env=dict(os.environ, GIT_NO_LAZY_FETCH='1'))


def source_identity(source, expected):
    head = git(source, 'rev-parse', 'HEAD').decode().strip()
    if head != expected or git(source, 'status', '--porcelain', '--untracked-files=no').strip():
        raise RuntimeError('Expected exact clean candidate commit before/after validation')
    return dict(commit=head, tree=git(source, 'rev-parse', 'HEAD^{tree}').decode().strip())


def original_inputs(source):
    paths = [NATIVE, SDB, 'build.sh', 'tools/deploy/init.sql',
             'tools/deploy/init_user.sql', 'tools/deploy/mysqltest_config.yaml',
             '.github/actions/setup-rust/action.yml', 'rust/rust-toolchain.toml',
             'deps/init/dep_create.sh', 'deps/init/oceanbase.el9.x86_64.deps']
    paths += [BENCH + suffix for suffix in ('.sh', '_gen.py', '_setup.sql', '_score.py', '_baseline.json')]
    # Protect all FTS tests/results and includes, not just selected SQL files.
    paths += git(source, 'ls-tree', '-r', '--name-only', REFERENCE, '--',
                 'tools/deploy/mysql_test/test_suite/fts_index',
                 'tools/deploy/mysql_test/include').decode().splitlines()
    hashes = {}
    for relative in sorted(set(paths)):
        expected = hashlib.sha256(git(source, 'show', REFERENCE + ':' + relative)).hexdigest()
        if digest(source / relative) != expected:
            raise RuntimeError('Official input differs from pinned reference: ' + relative)
        hashes[relative] = expected
    return hashes


def load_native(source):
    spec = importlib.util.spec_from_file_location('untouched_native_mysqltest', source / NATIVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def capture(command, output, cwd, timeout, stdin=None, env=None):
    row = dict(command=list(map(str, command)), startedUtc=utc(), timeoutSeconds=timeout)
    with output.open('wb') as stream:
        try:
            result = subprocess.run(row['command'], cwd=cwd, input=stdin, env=env,
                                    stdout=stream, stderr=subprocess.STDOUT,
                                    timeout=timeout, check=False)
            row['exitCode'] = result.returncode
        except subprocess.TimeoutExpired:
            row.update(exitCode=124, timedOut=True)
    row.update(finishedUtc=utc(), output=str(output), outputSha256=digest(output))
    write(output.with_suffix(output.suffix + '.json'), row)
    return row


def validate_loader(path, output, source):
    """The real loader decides runtime dependencies; an optional SO is not a prerequisite."""
    row = capture(['ldd', path], output, source, 30)
    if row['exitCode'] or b'not found' in output.read_bytes():
        show_log(output)
        raise RuntimeError('Actual artifact loader prerequisite failed: ' + str(path))
    return row


@contextmanager
def capture_fds(path):
    sys.stdout.flush()
    sys.stderr.flush()
    original = [os.dup(1), os.dup(2)]
    try:
        with path.open('wb') as stream:
            os.dup2(stream.fileno(), 1)
            os.dup2(stream.fileno(), 2)
            yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        for descriptor, saved in zip((1, 2), original):
            os.dup2(saved, descriptor)
            os.close(saved)


def bounded_db_logs(base, out):
    """Preserve only owned DB log tails; never copy datafiles/cores/config/env."""
    target = out / 'db-log-tails'
    target.mkdir()
    manifest = []
    remaining = 16 * 1024 * 1024
    for path in sorted((base / 'log').rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        size = path.stat().st_size
        take = min(size, 2 * 1024 * 1024, remaining)
        row = dict(path=path.relative_to(base).as_posix(), bytes=size,
                   savedBytes=take, truncated=take != size)
        if take:
            destination = target / path.relative_to(base / 'log')
            destination.parent.mkdir(parents=True, exist_ok=True)
            with path.open('rb') as stream:
                stream.seek(size - take)
                destination.write_bytes(stream.read(take))
            row['savedSha256'] = digest(destination)
            remaining -= take
        manifest.append(row)
    write(target / 'manifest.json', manifest)
    return dict(manifest=str(target / 'manifest.json'), savedBytes=16 * 1024 * 1024 - remaining)


def run_instance(args, native, name, port, work, initialize=False):
    out = args.out / name
    out.mkdir()
    base = out / 'database'
    row = dict(name=name, startedUtc=utc(), parameters=PARAMETERS, status='running')
    call = SimpleNamespace(seekdb=args.binary, obclient=args.obclient,
        mysqltest=args.mysqltest, base_dir=base, work_dir=out,
        host='127.0.0.1', port=port)
    try:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
        command = [sys.executable, args.source / SDB, 'start', '--binary', args.binary,
                   '--base-dir', base, '--port', port]
        for parameter in PARAMETERS:
            command.extend(('--parameter', parameter))
        row['start'] = capture(command, out / 'start.log', args.source, 60)
        if row['start']['exitCode']:
            raise RuntimeError('Owned database start failed')
        row['ready'] = capture([sys.executable, args.source / SDB, 'wait-ready',
            '--client', args.obclient, '--base-dir', base, '--host', call.host,
            '--port', port, '--user', 'root', '--timeout', 600],
            out / 'ready.log', args.source, 620)
        if row['ready']['exitCode']:
            raise RuntimeError('Owned database did not become ready')
        if initialize:
            with capture_fds(out / 'original-init.log'):
                native.execute_init_sql(args.obclient, call.host, port, args.source / 'tools/deploy')
        row['originalNativeInitializer'] = initialize
        row['actualParameters'] = capture([args.obclient, '-h127.0.0.1', '-P' + str(port),
            '-uroot', '-A', '-N', '-B', '-Doceanbase'],
            out / 'actual-parameters.log', args.source, 30,
            b"select name,value from oceanbase.__all_virtual_sys_parameter_stat "
            b"where name in ('memory_budget','vector_memory_limit','datafile_size',"
            b"'datafile_maxsize','log_disk_size','syslog_level') order by name;\n")
        if row['actualParameters']['exitCode']:
            raise RuntimeError('Cannot observe actual DB parameters')
        row['work'] = work(call, out)
        row['status'] = 'passed' if row['work']['passed'] else 'failed'
    except Exception as error:
        row.update(status='infrastructure_error', error=str(error), traceback=traceback.format_exc())
    finally:
        # sdb verifies the marker, executable and base-dir before signalling/removal.
        if base.exists():
            try:
                row['stop'] = capture([sys.executable, args.source / SDB, 'stop', '--base-dir', base],
                    out / 'own-stop.log', args.source, 60)
                row['dbLogs'] = bounded_db_logs(base, out)
                if row['stop']['exitCode'] == 0:
                    row['destroy'] = capture([sys.executable, args.source / SDB, 'destroy', '--base-dir', base],
                        out / 'own-destroy.log', args.source, 60)
            except Exception as error:
                row['cleanupError'] = str(error)
        row['cleanupPassed'] = (row.get('stop', {}).get('exitCode') == 0
            and row.get('destroy', {}).get('exitCode') == 0 and not base.exists()
            and not row.get('cleanupError'))
        row['finishedUtc'] = utc()
        write(out / 'result.json', row)
        show_log(out / 'actual-parameters.log')
        if row['status'] != 'passed':
            for path in sorted(out.glob('*.log')):
                show_log(path)
        emit('INSTANCE_RESULT', row)
    return row


def original_case(args, native, case):
    def execute(call, out):
        tmp, logs = out / 'tmp', out / 'mysqltest-log'
        tmp.mkdir()
        logs.mkdir()
        before = (digest(case.test_file), digest(case.result_file))
        with capture_fds(out / 'original-run-case.log'):
            code, output = native.run_case(call, args.source / 'tools/deploy', case, tmp, logs)
        (out / 'mysqltest-output.log').write_text(output, encoding='utf-8')
        unchanged = before == (digest(case.test_file), digest(case.result_file))
        show_log(out / 'original-run-case.log')
        for reject in sorted(logs.glob('*.reject')):
            difference = ''.join(difflib.unified_diff(
                case.result_file.read_text(encoding='utf-8', errors='replace').splitlines(True),
                reject.read_text(encoding='utf-8', errors='replace').splitlines(True),
                fromfile='official_expected.result', tofile=reject.name))
            (out / 'original-result-diff.log').write_text(difference, encoding='utf-8')
            print('OFFICIAL_RESULT_DIFF ' + case.name + '\n' + difference[:24000], flush=True)
        return dict(passed=code == 0 and unchanged, exitCode=code, invocations=1,
                    inputsUnchanged=unchanged, testSha256=before[0], expectedSha256=before[1])
    return execute


def benchmark(args):
    def execute(call, out):
        env = os.environ.copy()
        base_command = [str(args.obclient), '-h127.0.0.1', '-P' + str(call.port),
                        '-uroot', '-A', '--default-character-set=utf8mb4']
        env['MYSQL'] = shlex.join(base_command + ['-N', '-s'])
        env['MYSQL_VERBOSE'] = shlex.join(base_command)
        env.update({key.upper(): str(value) for key, value in BENCH_CONFIG.items()})
        env.update(LABEL='candidate-' + args.expected_sha[:12], OUTPUT=str(out / 'report.txt'))
        result = capture(['bash', args.source / (BENCH + '.sh')],
                         out / 'benchmark.log', args.source, 1800, env=env)
        show_log(out / 'benchmark.log')
        row = dict(passed=False, invocation=result, invocations=1, config=BENCH_CONFIG,
                   scoreInterpretation='Official frozen baseline on a different host; informative only, not formal score or same-host speedup')
        if result['exitCode'] == 0:
            # No config-mismatch waiver, alternate baseline, or metric substitution.
            row['officialScore'] = capture([sys.executable, args.source / (BENCH + '_score.py'),
                out / 'report.txt', '--json'], out / 'score.json', args.source, 30)
            if row['officialScore']['exitCode'] == 0:
                score = json.loads((out / 'score.json').read_text(encoding='utf-8'))
                row['informativeOfficialBaselineScore'] = score
                spec = importlib.util.spec_from_file_location('official_fts_score', args.source / (BENCH + '_score.py'))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                metrics = module.load_report(out / 'report.txt')
                baseline = module.load_baseline(args.source / (BENCH + '_baseline.json'))
                hits = ('query_cn_hits', 'query_beng_hits', 'query_mixed_hits', 'query_limit_hits')
                row['expectedHits'] = {key: baseline['baseline_metrics'][key] for key in hits}
                row['actualHits'] = {key: metrics.get(key) for key in hits}
                row['passed'] = (row['expectedHits'] == row['actualHits']
                    and not score['config_mismatches'] and metrics.get('git_dirty') == 0)
            show_log(out / 'score.json')
        return row
    return execute


def main():
    global ACTIVE_REPORT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    args.source = args.source.resolve(strict=True)
    args.out = args.out.resolve(strict=False)
    report = dict(scope='DEVELOPMENT_CI_NOT_FORMAL_CONTEST_SUBMISSION', reference=REFERENCE,
        expectedCommit=args.expected_sha, cases=list(CASES), caseRetries=0,
        buildsByHarness=0, parameters=PARAMETERS, benchmarkConfig=BENCH_CONFIG,
        addedRegression='fts_index.tokenize_casefold_contract', status='plan_only_not_executed')
    if not args.execute:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    if sys.platform != 'linux' or args.out.exists() or args.out.is_symlink():
        raise RuntimeError('Execution requires Linux and a fresh dedicated output path')
    if args.source == args.out or args.source in args.out.parents or args.out in args.source.parents:
        raise RuntimeError('Source and output must be disjoint')
    report['identityBefore'] = source_identity(args.source, args.expected_sha)
    pinned = original_inputs(args.source)
    native = load_native(args.source)
    selected = {case.name: case for case in native.discover_cases(args.source)}
    if any(case not in selected for case in CASES):
        raise RuntimeError('A required official case is excluded or not discoverable')
    args.binary = args.source / 'build_release/src/observer/seekdb'
    args.obclient = args.source / 'deps/3rd/u01/obclient/bin/obclient'
    args.mysqltest = args.obclient.with_name('mysqltest')
    # The official seekdb target links oceanbase_static. The separate
    # pretest-only oceanbase SHARED EXCLUDE_FROM_ALL target is not required,
    # even if a liboceanbase.so from another build happens to exist.
    artifacts = (args.binary, args.obclient, args.mysqltest)
    for path in artifacts:
        if not path.is_file() or path.is_symlink():
            raise RuntimeError('Required exact built artifact missing or symlink: ' + str(path))
    args.out.mkdir(parents=True)
    ACTIVE_REPORT = args.out / 'report.json'
    report.update(status='running', startedUtc=utc(), originalInputHashes=pinned,
        artifactContract='Official seekdb executable plus native SQL clients; no pretest-only shared-library assumption',
        builtArtifacts={str(path.relative_to(args.source)): dict(bytes=path.stat().st_size,
                        sha256=digest(path)) for path in artifacts}, results=[])
    write(ACTIVE_REPORT, report)
    os.environ['LD_LIBRARY_PATH'] = ':'.join(map(str, [args.binary.parent,
        args.obclient.parent.parent / 'lib', args.obclient.parent.parent / 'lib64',
        args.source / 'deps/3rd/usr/local/oceanbase/deps/devel/lib',
        args.source / 'deps/3rd/usr/local/oceanbase/deps/devel/lib64'])) + ':' + os.environ.get('LD_LIBRARY_PATH', '')
    os.environ['PATH'] = str(args.obclient.parent) + os.pathsep + os.environ.get('PATH', '')
    report['actualLoaders'] = {}
    for path in artifacts:
        report['actualLoaders'][str(path.relative_to(args.source))] = validate_loader(
            path, args.out / (path.name + '.ldd.log'), args.source)
        write(ACTIVE_REPORT, report)
    for number, name in enumerate(CASES):
        report['results'].append(run_instance(args, native, name, 7200 + number,
            original_case(args, native, selected[name]), initialize=True))
        write(ACTIVE_REPORT, report)
    # Fresh benchmark DB uses the original benchmark's own schema initializer,
    # without mysqltest init.sql failpoints or previous test tables/configuration.
    report['results'].append(run_instance(args, native, 'official-fts-benchmark', 7291, benchmark(args)))
    write(ACTIVE_REPORT, report)
    report['identityAfter'] = source_identity(args.source, args.expected_sha)
    report['originalInputsUnchanged'] = original_inputs(args.source) == pinned
    report['builtArtifactsUnchanged'] = all(digest(path) == report['builtArtifacts'][
        str(path.relative_to(args.source))]['sha256'] for path in artifacts)
    passed = (report['originalInputsUnchanged'] and report['builtArtifactsUnchanged']
        and all(row['status'] == 'passed' and row['cleanupPassed'] for row in report['results']))
    report.update(status='selected_fts_validation_passed' if passed else 'selected_fts_validation_failed',
        selectedValidationPassed=passed, fullSuiteRun=False, formalContestRun=False, finishedUtc=utc())
    write(ACTIVE_REPORT, report)
    emit('FTS_VALIDATION_RESULT', dict(status=report['status'], out=str(args.out),
                                    selectedValidationPassed=passed, formalContestRun=False))
    return 0 if passed else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        if ACTIVE_REPORT is not None:
            report = json.loads(ACTIVE_REPORT.read_text(encoding='utf-8'))
            report.update(status='harness_error', error=str(error),
                          traceback=traceback.format_exc(), finishedUtc=utc())
            write(ACTIVE_REPORT, report)
            emit('FTS_HARNESS_ERROR', dict(error=str(error)))
        raise
