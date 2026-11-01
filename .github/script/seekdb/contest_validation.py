#!/usr/bin/env python3
"""Plan by default; build and run selected original native cases once on Linux."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import difflib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import traceback
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import zipfile

CASES = [
    'ai_function.ai_prompt', 'ai_function.ai_model_endpoint_ddl',
    'ai_column.ai_column_ddl', 'ai_column.ai_column_t1_dml',
    'ai_column.ai_column_worker', 'ai_column.ai_column_retry_strict',
    'ai_column.ai_column_vector_index',
]
MOCK_PORTS = dict(zip(CASES[3:], (19121, 19122, 19124, 19123)))
NATIVE = '.github/script/seekdb/mysqltest_for_seekdb.py'
SDB = '.github/script/seekdb/sdb.py'
MOCK = 'tools/deploy/mysql_test/test_suite/ai_column/include/ai_column_mock.py'
PARAMETERS = ('memory_budget=4G', 'vector_memory_limit=1G',
              'datafile_maxsize=2G', 'log_disk_size=2G')
SCOPE = 'LOCAL_CANDIDATE_BUILD_AND_ORIGINAL_CASES_NOT_FORMAL_ACCEPTANCE'
ACTIVE_REPORT = None


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def emit(label, value):
    print(label + ' ' + json.dumps(value, sort_keys=True), flush=True)


def show_log(path, limit=24000):
    if path.is_file():
        content = path.read_bytes()
        print('\n--- ' + str(path) + ' ---', flush=True)
        if len(content) > limit:
            print('[tail only: %d of %d bytes]' % (limit, len(content)), flush=True)
        print(content[-limit:].decode('utf-8', 'replace'), flush=True)


def git(source, *arguments):
    return subprocess.check_output(['git', '-C', str(source), *arguments])


def capture(command, output, cwd, timeout=None, stdin=None):
    row = {'command': list(map(str, command)), 'startedUtc': utc(), 'timeoutSeconds': timeout}
    with output.open('wb') as stream:
        try:
            result = subprocess.run(row['command'], cwd=cwd, input=stdin,
                                    stdout=stream, stderr=subprocess.STDOUT,
                                    timeout=timeout, check=False)
            row['exitCode'] = result.returncode
        except subprocess.TimeoutExpired:
            row['exitCode'] = 124
            row['timedOut'] = True
    row.update(finishedUtc=utc(), output=str(output), outputSha256=digest(output))
    write(output.with_suffix(output.suffix + '.json'), row)
    return row


@contextmanager
def capture_fds(path):
    sys.stdout.flush()
    sys.stderr.flush()
    originals = [os.dup(1), os.dup(2)]
    try:
        with path.open('wb') as stream:
            os.dup2(stream.fileno(), 1)
            os.dup2(stream.fileno(), 2)
            yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        for descriptor, saved in zip((1, 2), originals):
            os.dup2(saved, descriptor)
            os.close(saved)


def source_identity(source, expected):
    head = git(source, 'rev-parse', 'HEAD').decode().strip()
    if head != expected:
        raise RuntimeError('Checkout SHA does not match the requested candidate')
    if git(source, 'status', '--porcelain', '--untracked-files=no').strip():
        raise RuntimeError('Tracked source is dirty; refusing ambiguous build identity')
    return {'commit': head, 'tree': git(source, 'rev-parse', 'HEAD^{tree}').decode().strip()}


def original_inputs(source, reference):
    paths = [NATIVE, SDB, 'tools/deploy/init.sql', 'tools/deploy/init_user.sql',
             'tools/deploy/mysqltest_config.yaml']
    paths += git(source, 'ls-files', 'tools/deploy/mysql_test/test_suite/ai_function',
                 'tools/deploy/mysql_test/test_suite/ai_column',
                 'tools/deploy/mysql_test/test_suite/file_sql').decode().splitlines()
    hashes = {}
    for relative in sorted(set(paths)):
        baseline = hashlib.sha256(git(source, 'show', reference + ':' + relative)).hexdigest()
        if digest(source / relative) != baseline:
            raise RuntimeError('Original input differs from the explicit reference: ' + relative)
        hashes[relative] = baseline
    return hashes


def load_native(source):
    spec = importlib.util.spec_from_file_location('candidate_original_native', source / NATIVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_free_port(port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('127.0.0.1', port))


def cleanup_mock(source, state, port, out):
    if state is None or not state.exists():
        return {'needed': False, 'passed': True}
    if state.is_symlink():
        raise RuntimeError('Fixture state became a symlink')
    destination = out / 'mock-state'
    destination.mkdir()
    for name in ('events.jsonl', 'server.out', 'server.pid'):
        path = state / name
        if path.is_file() and not path.is_symlink():
            shutil.copy2(path, destination / name)
    pidfile = state / 'server.pid'
    if not pidfile.exists():
        assert_free_port(port)
        return {'needed': False, 'passed': True}
    pid = int(pidfile.read_text().strip())
    proc = Path('/proc') / str(pid)
    if not proc.exists():
        assert_free_port(port)
        return {'needed': False, 'passed': True, 'recordedProcessExists': False,
                'pidFileRemains': True, 'noSignalSent': True}
    fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] == 'Z':
        assert_free_port(port)
        return {'needed': False, 'passed': True, 'recordedProcessState': 'zombie',
                'pidFileRemains': True, 'noSignalSent': True}
    argv = (proc / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
    expected = [str(source / MOCK).encode(), b'serve', str(port).encode()]
    if len(argv) != 4 or argv[1:] != expected or proc.stat().st_uid != os.geteuid():
        raise RuntimeError('Fixture PID is not this case original Mock; not signalled')
    before = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    if before != (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]:
        raise RuntimeError('Fixture PID identity changed before normal stop')
    result = capture([sys.executable, source / MOCK, 'stop', str(port)],
                     out / 'own-mock-stop.log', source, 15)
    assert_free_port(port)
    result['passed'] = result['exitCode'] == 0 and not pidfile.exists()
    return result


def zip_logs(base, target):
    with zipfile.ZipFile(target, 'x', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((base / 'log').rglob('*')):
            if path.is_file() and not path.is_symlink():
                archive.write(path, path.relative_to(base).as_posix())
    return {'path': str(target), 'bytes': target.stat().st_size, 'sha256': digest(target)}


def database_cleanup_passed(row):
    return (row.get('ownDatabaseDestroyed') is True and
            row.get('stop', {}).get('exitCode') == 0 and
            row.get('destroy', {}).get('exitCode') == 0 and
            row.get('logs', {}).get('stoppedBeforeCapture') is True and
            bool(row.get('logs', {}).get('sha256')) and
            not row.get('cleanupError'))


def run_case(args, source, native, case, number):
    out = args.out / case.name
    out.mkdir()
    base, work = out / 'database', out / 'work'
    work.mkdir()
    tmp, logdir = work / 'tmp', work / 'mysqltest-log'
    tmp.mkdir()
    logdir.mkdir()
    port = 7200 + number
    assert_free_port(port)
    mock_port = MOCK_PORTS.get(case.name)
    state = Path('/tmp/seekdb_ai_column_mock_' + str(mock_port)) if mock_port else None
    if mock_port:
        assert_free_port(mock_port)
        if state.exists() or state.is_symlink():
            raise RuntimeError('Existing fixture state must be inspected; original start not invoked')
    call = SimpleNamespace(seekdb=args.binary, obclient=args.obclient, mysqltest=args.mysqltest,
                           base_dir=base, work_dir=work, host='127.0.0.1', port=port)
    row = {'case': case.name, 'startedUtc': utc(), 'invocations': 0,
           'parameters': PARAMETERS, 'testSha256': digest(case.test_file),
           'expectedSha256': digest(case.result_file), 'status': 'started'}
    started = False
    try:
        start = [sys.executable, source / SDB, 'start', '--binary', args.binary,
                 '--base-dir', base, '--port', str(port)]
        for parameter in PARAMETERS:
            start += ['--parameter', parameter]
        row['start'] = capture(start, out / 'start.log', source, 60)
        started = base.exists()
        if row['start']['exitCode']:
            raise RuntimeError('Owned DB start failed')
        ready = [sys.executable, source / SDB, 'wait-ready', '--client', args.obclient,
                 '--base-dir', base, '--host', '127.0.0.1', '--port', str(port),
                 '--user', 'root', '--timeout', '600']
        row['ready'] = capture(ready, out / 'ready.log', source, 620)
        if row['ready']['exitCode']:
            raise RuntimeError('Owned DB not ready')
        with capture_fds(out / 'init.log'):
            native.execute_init_sql(args.obclient, call.host, port, source / 'tools/deploy')
        observation = (b"select name,value from oceanbase.__all_virtual_sys_parameter_stat "
                       b"where name in ('memory_budget','vector_memory_limit','datafile_size',"
                       b"'datafile_maxsize','log_disk_size','syslog_level') order by name;\n")
        row['resourceObservation'] = capture(
            [args.obclient, '-h127.0.0.1', '-P' + str(port), '-uroot', '-A', '-N', '-B',
             '-Doceanbase'], out / 'actual-parameters.log', source, 30, observation)
        if row['resourceObservation']['exitCode']:
            raise RuntimeError('Cannot observe actual DB resource configuration')
        row['invocations'] = 1
        with capture_fds(out / 'original-run-case.log'):
            code, output = native.run_case(call, source / 'tools/deploy', case, tmp, logdir)
        (out / 'mysqltest-output.log').write_text(output, encoding='utf-8')
        row.update(exitCode=code, status='passed' if code == 0 else 'failed')
    except Exception as error:
        row.update(status='infrastructure_error', error=str(error), traceback=traceback.format_exc())
    finally:
        try:
            row['mockCleanup'] = cleanup_mock(source, state, mock_port, out)
        except Exception as error:
            row['mockCleanup'] = {'passed': False, 'error': str(error)}
        if started:
            try:
                row['stop'] = capture([sys.executable, source / SDB, 'stop', '--base-dir', base],
                                      out / 'own-stop.log', source, 60)
                if row['status'] != 'passed':
                    for path in sorted((base / 'log').glob('*.log')):
                        show_log(path, 8000)
                # Stop/flush first; preserve before original safe destroy removes own storage.
                row['logs'] = zip_logs(base, out / 'own-db-logs.zip')
                row['logs']['stoppedBeforeCapture'] = row['stop']['exitCode'] == 0
                if row['stop']['exitCode'] == 0:
                    row['destroy'] = capture(
                        [sys.executable, source / SDB, 'destroy', '--base-dir', base],
                        out / 'own-destroy.log', source, 60)
            except Exception as error:
                row['cleanupError'] = str(error)
            row['ownDatabaseDestroyed'] = not base.exists()
        else:
            row['ownDatabaseDestroyed'] = not base.exists()
        row['testUnchanged'] = digest(case.test_file) == row['testSha256']
        row['expectedUnchanged'] = digest(case.result_file) == row['expectedSha256']
        row['finishedUtc'] = utc()
        row['cleanupPassed'] = database_cleanup_passed(row)
        write(out / 'result.json', row)
        show_log(out / 'actual-parameters.log')
        show_log(out / 'original-run-case.log')
        for reject in sorted(logdir.glob('*.reject')):
            difference = ''.join(difflib.unified_diff(
                case.result_file.read_text(encoding='utf-8', errors='replace').splitlines(True),
                reject.read_text(encoding='utf-8', errors='replace').splitlines(True),
                fromfile='original_expected.result', tofile=reject.name))
            print('ORIGINAL_RESULT_DIFF ' + case.name + '\n' + difference[:24000], flush=True)
        if row['status'] != 'passed':
            for name in ('start.log', 'ready.log', 'init.log'):
                show_log(out / name, 8000)
            for path in sorted((base / 'log').glob('*.log')):
                show_log(path, 8000)
        emit('CASE_RESULT', row)
    return row


# Integration snippet for contest_validation.py; uses its existing helpers.
# DEV_ONLY_NOT_OFFICIAL: the probe is copied byte-for-byte, never rewritten.
URL_PROBE = 'unittest/sql/ai_column/dev_url_shared_cap.py'
URL_PROBE_SHA256 = 'a2eea17499ca386697f7c2d9175f84672848c1517a0ce36796af9f31c63621cf'


def run_url_probe(args, source, native):
    out = args.out / 'url-shared-cap'
    out.mkdir()
    base = out / 'database'
    port = 7290
    probe = source / URL_PROBE
    row = {'classification': 'DEV_ONLY_NOT_OFFICIAL',
           'case': 'developer.url_shared_cap', 'startedUtc': utc(),
           'invocations': 0, 'status': 'started', 'port': port,
           'parameters': PARAMETERS, 'probeSha256': digest(probe),
           'binarySha256': digest(args.binary),
           'obclientSha256': digest(args.obclient)}
    started = False
    try:
        if row['probeSha256'] != URL_PROBE_SHA256:
            raise RuntimeError('Frozen original developer URL probe differs')
        assert_free_port(port)
        start = [sys.executable, source / SDB, 'start', '--binary', args.binary,
                 '--base-dir', base, '--port', str(port)]
        for parameter in PARAMETERS:
            start += ['--parameter', parameter]
        row['start'] = capture(start, out / 'start.log', source, 60)
        started = base.exists()
        if row['start']['exitCode']:
            raise RuntimeError('Developer URL own DB start failed')
        ready = [sys.executable, source / SDB, 'wait-ready', '--client', args.obclient,
                 '--base-dir', base, '--host', '127.0.0.1', '--port', str(port),
                 '--user', 'root', '--timeout', '600']
        row['ready'] = capture(ready, out / 'ready.log', source, 620)
        if row['ready']['exitCode']:
            raise RuntimeError('Developer URL own DB not ready')
        with capture_fds(out / 'init.log'):
            native.execute_init_sql(args.obclient, '127.0.0.1', port,
                                    source / 'tools/deploy')
        row['resourceObservation'] = capture([
            args.obclient, '-h127.0.0.1', '-P' + str(port), '-uroot', '-A', '-N', '-B',
            '-Doceanbase'], out / 'actual-parameters.log', source, 30,
            b"select name,value from oceanbase.__all_virtual_sys_parameter_stat "
            b"where name in ('memory_budget','vector_memory_limit','datafile_size',"
            b"'datafile_maxsize','log_disk_size','syslog_level') order by name;\n")
        if row['resourceObservation']['exitCode']:
            raise RuntimeError('Cannot observe developer URL own DB resource configuration')
        row['invocations'] = 1
        # A fresh child report directory is required by the untouched probe.
        # It owns its dynamic loopback HTTP port and UUID SQL objects.
        row['probe'] = capture([
            sys.executable, probe, '--execute', '--disposable',
            '--sql-host', '127.0.0.1', '--sql-port', str(port), '--sql-user', 'root',
            '--obclient', args.obclient, '--password-env', 'OB_TEST_PASSWORD',
            '--report-dir', out / 'probe', '--requests', '8', '--delay-ms', '300',
            '--run-budget-seconds', '180', '--statement-timeout', '30'],
            out / 'probe.log', source, 300)
        evidence_file = out / 'probe/report.json'
        if not evidence_file.is_file() or evidence_file.is_symlink():
            raise RuntimeError('Developer URL probe did not produce its report')
        evidence = json.loads(evidence_file.read_text(encoding='utf-8'))
        row['reportSha256'] = digest(evidence_file)
        row['probeEvidence'] = evidence
        # This preserves the original verdict: incomplete or inconclusive is
        # not a pass. Do not infer a result from low target pressure alone.
        passed = (row['probe']['exitCode'] == 0
                  and evidence.get('classification') == 'DEV_ONLY_NOT_OFFICIAL'
                  and evidence.get('script_sha256') == URL_PROBE_SHA256
                  and evidence.get('status') == 'passed')
        row.update(status='passed' if passed else 'failed',
                   exitCode=row['probe']['exitCode'])
    except Exception as error:
        row.update(status='infrastructure_error', error=str(error),
                   traceback=traceback.format_exc())
    finally:
        if started:
            try:
                row['stop'] = capture([sys.executable, source / SDB, 'stop', '--base-dir', base],
                                      out / 'own-stop.log', source, 60)
                if row['status'] != 'passed':
                    for path in sorted((base / 'log').glob('*.log')):
                        show_log(path, 8000)
                row['logs'] = zip_logs(base, out / 'own-db-logs.zip')
                row['logs']['stoppedBeforeCapture'] = row['stop']['exitCode'] == 0
                if row['stop']['exitCode'] == 0:
                    row['destroy'] = capture([
                        sys.executable, source / SDB, 'destroy', '--base-dir', base],
                        out / 'own-destroy.log', source, 60)
            except Exception as error:
                row['cleanupError'] = str(error)
        row['ownDatabaseDestroyed'] = not base.exists()
        row['probeUnchanged'] = probe.is_file() and digest(probe) == URL_PROBE_SHA256
        row['binaryUnchanged'] = digest(args.binary) == row['binarySha256']
        row['finishedUtc'] = utc()
        row['cleanupPassed'] = database_cleanup_passed(row)
        write(out / 'result.json', row)
        show_log(out / 'actual-parameters.log')
        show_log(out / 'probe.log')
        if row['status'] != 'passed':
            for name in ('start.log', 'ready.log', 'init.log'):
                show_log(out / name, 8000)
        emit('DEVELOPER_URL_CAP_RESULT', row)
    return row


def main():
    global ACTIVE_REPORT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--reference-sha', required=True,
                        help='Explicit known original-test reference commit available locally')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    args.source = args.source.resolve(strict=True)
    args.out = args.out.resolve(strict=False)
    plan = {'scope': SCOPE, 'commit': args.expected_sha, 'originalReference': args.reference_sha,
            'source': str(args.source), 'out': str(args.out), 'cases': CASES,
            'parameters': PARAMETERS, 'caseRetries': 0, 'parallelDatabases': 1,
            'developerUrlProbe': {'classification': 'DEV_ONLY_NOT_OFFICIAL',
                                  'invocations': 1, 'databasePort': 7290,
                                  'requestsPerPhase': 8, 'controlCap': 1024, 'targetCap': 3},
            'blocked': {'file_sql.file_sql_basic': 'Original fixture requires existing local SSH/SCP; not provisioned',
                        'ai_function.ai_function_enhancement': 'Official port8000 Mock is unavailable; not fabricated'},
            'status': 'plan_only_not_executed'}
    if not args.execute:
        print(json.dumps(plan, indent=2))
        return 0
    if sys.platform != 'linux' or args.out.exists() or args.out.is_symlink():
        raise RuntimeError('Linux and fresh dedicated output required; no implicit replay')
    if args.out == args.source or args.out in args.source.parents or args.source in args.out.parents:
        raise RuntimeError('Output and source must be separate directories')
    identity = source_identity(args.source, args.expected_sha)
    pinned = original_inputs(args.source, args.reference_sha)
    native = load_native(args.source)
    selected = {case.name: case for case in native.discover_cases(args.source)}
    if any(name not in selected for name in CASES):
        raise RuntimeError('A requested original case is not discoverable')
    args.out.mkdir(parents=True)
    ACTIVE_REPORT = args.out / 'report.json'
    report = dict(plan, status='running', startedUtc=utc(), sourceIdentity=identity,
                  originalInputHashes=pinned, cases=[])
    write(args.out / 'report.json', report)
    emit('CANDIDATE_SOURCE_IDENTITY', identity)
    emit('ORIGINAL_INPUT_REFERENCE', {'commit': args.reference_sha, 'hashes': pinned})
    build = args.source / 'build_release'
    cmake = args.source / 'deps/3rd/usr/local/oceanbase/devtools/bin/cmake'
    if not cmake.is_file():
        raise RuntimeError('Pinned CMake is absent; no environment reinstall attempted')
    args.obclient = args.source / 'deps/3rd/u01/obclient/bin/obclient'
    args.mysqltest = args.obclient.with_name('mysqltest')
    for client in (args.obclient, args.mysqltest):
        if not client.is_file():
            raise RuntimeError('Original client prerequisite missing before build: ' + str(client))
    result = capture([cmake, '--build', build, '--target', 'seekdb', 'ai_function_contracts',
                      'ai_url_concurrency_contracts', '-j2'],
                     args.out / 'build.log', args.source, 18000)
    report['build'] = result
    write(args.out / 'report.json', report)
    emit('ACTUAL_CACHED_BUILD', result)
    if result['exitCode']:
        show_log(args.out / 'build.log')
        report.update(status='build_failed', finishedUtc=utc())
        write(args.out / 'report.json', report)
        return 1
    args.binary = build / 'src/observer/seekdb'
    library = build / 'src/observer/liboceanbase.so'
    unit = build / 'unittest/ai_function_contracts'
    url_unit = build / 'unittest/ai_url_concurrency_contracts'
    artifacts = [args.binary, library, unit, url_unit, args.obclient, args.mysqltest]
    report['artifacts'] = {str(path.relative_to(args.source)):
                           {'bytes': path.stat().st_size, 'sha256': digest(path)} for path in artifacts}
    write(args.out / 'report.json', report)
    emit('BUILT_ARTIFACTS_SHA256', report['artifacts'])
    os.environ['LD_LIBRARY_PATH'] = ':'.join([str(library.parent),
        str(args.obclient.parent.parent / 'lib'), str(args.obclient.parent.parent / 'lib64'),
        str(args.source / 'deps/3rd/usr/local/oceanbase/deps/devel/lib'),
        str(args.source / 'deps/3rd/usr/local/oceanbase/deps/devel/lib64'),
        os.environ.get('LD_LIBRARY_PATH', '')]).rstrip(':')
    os.environ['PATH'] = str(args.obclient.parent) + os.pathsep + os.environ.get('PATH', '')
    for path in artifacts:
        if path == library:
            continue
        checked = capture(['ldd', path], args.out / (path.name + '.ldd.log'), args.source, 30)
        if checked['exitCode'] or b'not found' in Path(checked['output']).read_bytes():
            show_log(Path(checked['output']))
            raise RuntimeError('Loader prerequisite failed: ' + str(path))
    unit_result = capture([unit, '--gtest_output=xml:' + str(args.out / 'contracts.xml')],
                          args.out / 'contracts.log', args.source, 300)
    show_log(args.out / 'contracts.log')
    xml = ET.parse(args.out / 'contracts.xml').getroot()
    nodes = xml.findall('.//testcase')
    unit_result.update(tests=len(nodes), failures=len(xml.findall('.//failure')),
                       skipped=len(xml.findall('.//skipped')),
                       notRun=sum(node.get('status') != 'run' for node in nodes),
                       passed=unit_result['exitCode'] == 0 and len(nodes) == 9 and
                              all(node.get('status') == 'run' for node in nodes) and
                              not xml.findall('.//error') and not xml.findall('.//failure') and
                              not xml.findall('.//skipped'))
    report['contracts'] = unit_result
    write(args.out / 'report.json', report)
    emit('ORIGINAL_NINE_CONTRACTS', unit_result)
    url_result = capture([url_unit, '--gtest_output=xml:' + str(args.out / 'url-contracts.xml')],
                         args.out / 'url-contracts.log', args.source, 120)
    show_log(args.out / 'url-contracts.log')
    url_xml = ET.parse(args.out / 'url-contracts.xml').getroot()
    url_nodes = url_xml.findall('.//testcase')
    url_result.update(tests=len(url_nodes), failures=len(url_xml.findall('.//failure')),
                      skipped=len(url_xml.findall('.//skipped')),
                      notRun=sum(node.get('status') != 'run' for node in url_nodes),
                      passed=url_result['exitCode'] == 0 and len(url_nodes) == 5 and
                             all(node.get('status') == 'run' for node in url_nodes) and
                             not url_xml.findall('.//error') and not url_xml.findall('.//failure') and
                             not url_xml.findall('.//skipped'))
    report['urlContracts'] = url_result
    write(args.out / 'report.json', report)
    emit('FIVE_URL_HTTP_CONTRACTS', url_result)
    for number, name in enumerate(CASES):
        report['cases'].append(run_case(args, args.source, native, selected[name], number))
        write(args.out / 'report.json', report)
    report['developerUrlSharedCap'] = run_url_probe(args, args.source, native)
    write(args.out / 'report.json', report)
    report['sourceIdentityAfter'] = source_identity(args.source, args.expected_sha)
    report['originalInputsUnchanged'] = original_inputs(args.source, args.reference_sha) == pinned
    emit('SOURCE_IDENTITY_AFTER', report['sourceIdentityAfter'])
    url_probe = report['developerUrlSharedCap']
    passed = (unit_result['passed'] and url_result['passed'] and
              url_probe['status'] == 'passed' and database_cleanup_passed(url_probe) and
              url_probe['probeUnchanged'] and url_probe['binaryUnchanged']) and all(
        row['status'] == 'passed' and database_cleanup_passed(row) and row['mockCleanup']['passed']
        and row['testUnchanged'] and row['expectedUnchanged'] for row in report['cases'])
    report.update(status='selected_cases_passed_coverage_blocked' if passed else 'selected_cases_failed',
                  selectedCasesPassed=passed, fullCoveragePassed=False, finishedUtc=utc())
    write(args.out / 'report.json', report)
    print(json.dumps({'status': report['status'], 'out': str(args.out),
                      'selectedCasesPassed': passed, 'blocked': report['blocked']}, indent=2))
    return 2 if passed else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        if ACTIVE_REPORT is not None:
            current = json.loads(ACTIVE_REPORT.read_text(encoding='utf-8'))
            current.update(status='harness_error', error=str(error),
                           traceback=traceback.format_exc(), finishedUtc=utc())
            write(ACTIVE_REPORT, current)
            emit('HARNESS_ERROR', {'status': current['status'], 'error': str(error),
                                  'sourceIdentity': current.get('sourceIdentity'),
                                  'finishedCases': [row.get('case') for row in current.get('cases', [])]})
        raise
