"""Exercise the explicitly versioned development protocol through real process pipes."""

import argparse
import hashlib
import json
import queue
import re
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from public_samples import compare, split_commands

PROTOCOL = 'mvdb-dev-jsonl-v1'


class Client:
    def __init__(self, command):
        self.container = None
        if command[:2] == ['docker', 'run']:
            self.container = 'mvdb-pipe-' + uuid.uuid4().hex
            command = command[:2] + ['--name', self.container] + command[2:]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding='utf-8', bufsize=1)
        self.events = queue.Queue(maxsize=32)
        self.stderr = []

        def read_stdout():
            for line in self.process.stdout:
                self.events.put(line)
            self.events.put(None)

        def read_stderr():
            for line in self.process.stderr:
                self.stderr.append(line)

        self.readers = [threading.Thread(target=read_stdout, daemon=True),
                        threading.Thread(target=read_stderr, daemon=True)]
        for reader in self.readers:
            reader.start()
        try:
            ready = self.receive()
            assert ready == {'protocol': PROTOCOL, 'event': 'ready', 'formal_ready': False}, ready
        except BaseException:
            self.abort()
            raise

    def receive(self):
        line = self.events.get(timeout=30)
        assert line is not None, 'Unexpected stdout EOF ' + ''.join(self.stderr)
        event = json.loads(line)
        assert event.get('protocol') == PROTOCOL, 'Non-protocol text on stdout'
        return event

    def send(self, text):
        self.process.stdin.write(text + '\n')
        self.process.stdin.flush()

    def mode(self, mode):
        self.send('@mode ' + mode)
        assert self.receive() == {'protocol': PROTOCOL, 'event': 'mode', 'mode': mode}

    def inspect_container(self):
        if not self.container:
            return None
        config = subprocess.run(['docker', 'inspect', self.container, '--format', '{{json .HostConfig.NetworkMode}}'],
                                capture_output=True, text=True, timeout=15, check=True)
        assert json.loads(config.stdout) == 'none'
        probe = subprocess.run(['docker', 'exec', self.container, '/bin/sh', '-ec',
                                'readlink /proc/1/exe; stat -c "%a %u %g %n" /data; '
                                'for f in /proc/1/fd/*; do readlink "$f"; done; '
                                'for f in /proc/net/tcp /proc/net/tcp6 /proc/net/udp /proc/net/udp6; do cat "$f"; done'],
                               capture_output=True, text=True, timeout=15, check=True)
        lines = probe.stdout.splitlines()
        assert lines[0] == '/build/minivectordb_development_server'
        assert lines[1].endswith(' /data')
        assert not any(re.match(r'\s*\d+:', line) for line in lines), 'Unexpected network socket'
        assert all(line.startswith('/data/') for line in lines[2:] if line.startswith('/')), lines
        return {'network_mode': 'none', 'process_executable': lines[0], 'data_directory_stat': lines[1],
                'socket_entries': 0, 'raw_probe': probe.stdout}

    def execute(self, sql, visit=None):
        self.send(sql)
        result = {'columns': [], 'rows': []}
        seen = 0
        while True:
            event = self.receive()
            if event['event'] == 'columns':
                result['columns'] = event['columns']
            elif event['event'] == 'row':
                seen += 1
                if visit:
                    visit(event['values'])
                else:
                    result['rows'].append(event['values'])
            elif event['event'] == 'complete':
                assert event['emitted_rows'] == seen, 'Row frames and completion count differ'
                result['affected_rows'] = event['affected_rows']
                return result
            elif event['event'] == 'error':
                assert event['kind'] == 'sql' and event['message']
                return {'error': event['message'], 'category': event['category']}
            else:
                raise AssertionError('Unexpected event ' + str(event))

    def close(self, quit_command=False):
        try:
            if quit_command:
                self.send('@quit')
            self.process.stdin.close()
            assert self.process.wait(timeout=30) == 0, ''.join(self.stderr)
            for reader in self.readers:
                reader.join(timeout=5)
                assert not reader.is_alive(), 'Process pipe did not close'
            assert self.events.get(timeout=5) is None, 'Extra stdout after final result'
            assert 'development protocol ' + PROTOCOL in ''.join(self.stderr)
        finally:
            self.abort()

    def abort(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=15)
            if self.container:
                subprocess.run(['docker', 'rm', '--force', self.container], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=15, check=False)
        for stream in [self.process.stdin, self.process.stdout, self.process.stderr]:
            if not stream.closed:
                stream.close()


def run_cases(command_for, cases, owned):
    manifest = json.loads((cases / 'manifest.json').read_text(encoding='utf-8'))
    results = []
    for case in manifest['cases']:
        directory = owned / case['id']
        directory.mkdir()
        segments, statements = split_commands((cases / case['test']).read_text(encoding='utf-8').splitlines())
        expected = json.loads((cases / case['result']).read_text(encoding='utf-8'))
        assert len(expected) == len(statements) == case['statements']
        actual = []
        for segment in segments:
            client = Client(command_for(directory))
            try:
                for line in segment:
                    if line.startswith('--mode '):
                        client.mode(line.split()[-1])
                    else:
                        actual.append(client.execute(line))
                client.close()
            finally:
                client.abort()
        failures = [{'statement': index + 1, 'errors': compare(assertion, result)}
                    for index, (assertion, result) in enumerate(zip(expected, actual)) if compare(assertion, result)]
        assert len(actual) == len(expected) and not failures, (case['id'], failures, actual)
        results.append({'id': case['id'], 'statements': len(actual), 'processes': len(segments), 'passed': True})
        print('PASS development pipes ' + case['id'], flush=True)
    return results


def lifecycle(command_for, owned):
    directory = owned / '中文 路径'
    directory.mkdir()
    client = Client(command_for(directory))
    try:
        container = client.inspect_container()
        client.send('@mode invalid')
        error = client.receive()
        assert error['event'] == 'error' and error['kind'] == 'command'
        assert not compare({}, client.execute('CREATE TABLE docs(id BIGINT PRIMARY KEY, content VARCHAR(2048))'))
        text = '中文😀' + 'x' * 1000 + '\t"\\'
        quoted = "'" + text.replace("'", "''") + "'"
        for start in range(0, 1200, 100):
            values = ','.join('(' + str(key) + ',' + quoted + ')' for key in range(start, start + 100))
            assert not compare({'affected_rows': 100}, client.execute('INSERT INTO docs VALUES ' + values))
        seen = set()

        def visit(row):
            assert len(row) == 2 and row[0] == len(seen) and row[1] == text
            seen.add(row[0])

        result = client.execute('SELECT * FROM docs ORDER BY id', visit)
        assert len(seen) == 1200 and result['rows'] == []
        assert not compare({}, client.execute('BEGIN'))
        assert not compare({'affected_rows': 1200}, client.execute('DELETE FROM docs'))
        client.close(quit_command=True)
    finally:
        client.abort()
    client = Client(command_for(directory))
    try:
        assert not compare({'rows': [[0, text]]}, client.execute('SELECT * FROM docs WHERE id = 0'))
        assert not compare({}, client.execute('BEGIN'))
        assert not compare({'affected_rows': 1}, client.execute('DELETE FROM docs WHERE id = 0'))
        client.close()
    finally:
        client.abort()
    client = Client(command_for(directory))
    try:
        assert not compare({'rows': [[0, text]]}, client.execute('SELECT * FROM docs WHERE id = 0'))
        client.close()
    finally:
        client.abort()
    files = sorted(path.relative_to(directory).as_posix() for path in directory.rglob('*') if path.is_file())
    assert files == ['pages.bin', 'redo.wal'], files
    return {'streamed_rows': 1200, 'row_payload_characters': len(text), 'quit_rollback': True,
            'eof_rollback': True, 'mode_error_then_continue': True, 'files_after_shutdown': files,
            'container_inspection': container}


def main():
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--driver', type=Path)
    source.add_argument('--container-image')
    parser.add_argument('--cases', type=Path, default=Path(__file__).resolve().parents[1] / 'mysqltest')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    args.report = args.report.resolve()
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def command_for(directory):
        if args.driver:
            return [str(args.driver.resolve(strict=True)), '--development-protocol', PROTOCOL, '--data', str(directory)]
        return ['docker', 'run', '--rm', '-i', '--network', 'none', '--cpus', '2', '--memory', '2g',
                '--mount', 'type=bind,source=' + str(directory) + ',target=/data', args.container_image]

    cases = args.cases.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='development-pipe-', dir=args.report.parent) as temporary:
        owned = Path(temporary).resolve()
        groups = run_cases(command_for, cases, owned)
        checks = lifecycle(command_for, owned)
        invalid_directory = owned / 'invalid-start'
        invalid_directory.mkdir()
        command = command_for(invalid_directory)
        if args.driver:
            command[2] = 'unsupported-development-version'
        else:
            command.extend(['--development-protocol', 'unsupported-development-version', '--data', '/data'])
        invalid = subprocess.run(command, input=b'', capture_output=True, timeout=30)
        assert invalid.returncode == 2 and invalid.stdout == b'' and b'Usage:' in invalid.stderr
        checks['unknown_protocol_rejected_before_ready'] = True
        if args.container_image:
            readonly = owned / 'readonly-start'
            readonly.mkdir()
            denied = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--cpus', '2', '--memory', '2g',
                                     '--mount', 'type=bind,source=' + str(readonly) + ',target=/data,readonly', args.container_image],
                                    input=b'', capture_output=True, timeout=30)
            assert denied.returncode == 1 and denied.stdout == b'' and b'Development process failed:' in denied.stderr
            checks['readonly_data_rejected_before_ready'] = True
    report = {'protocol': PROTOCOL, 'scope': 'Development process or default Docker entry through stdin/stdout pipes',
              'container_image': args.container_image, 'formal_process_protocol_tested': False,
              'container_entrypoint_overridden': False, 'network_mode': 'none' if args.container_image else 'local process',
              'fixture_hashes': {path.relative_to(cases).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in sorted(cases.rglob('*')) if path.is_file()},
              'cases': groups, 'sql_statements': sum(case['statements'] for case in groups),
              'lifecycle': checks, 'passed': True}
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
