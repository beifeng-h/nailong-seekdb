"""Check JSONL framing, official public assertions, and process lifecycle on native pipes."""

import argparse
import hashlib
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'protocol_check'))
from _assertions import assert_response
from _transport import strict_json


class Client:
    def __init__(self, driver, data):
        self.process = subprocess.Popen([str(driver), '--data', str(data)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
        self.lines = queue.Queue()
        self.stderr = bytearray()

        def read_stdout():
            for line in self.process.stdout:
                self.lines.put(line)
            self.lines.put(None)

        def read_stderr():
            self.stderr.extend(self.process.stderr.read())

        self.readers = [threading.Thread(target=read_stdout, daemon=True),
                        threading.Thread(target=read_stderr, daemon=True)]
        for reader in self.readers:
            reader.start()
        try:
            assert self.receive() == {'type': 'ready'}
        except BaseException:
            self.abort()
            raise
        self.sequence = 0

    def receive(self):
        raw = self.lines.get(timeout=30)
        assert raw is not None, self.stderr.decode('utf-8', errors='replace')
        assert raw.endswith(b'\n'), raw
        response = strict_json(raw.decode('utf-8'))
        assert isinstance(response, dict), response
        return response

    def raw_request(self, raw, ident):
        self.process.stdin.write(raw + b'\n')
        self.process.stdin.flush()
        response = self.receive()
        assert type(response.get('id')) is type(ident) and response['id'] == ident, response
        assert type(response.get('ok')) is bool, response
        return response

    def request(self, request):
        return self.raw_request(json.dumps(request, ensure_ascii=False, allow_nan=False).encode(),
                                request['id'])

    def execute(self, sql, expected, mode='exact', ident=None):
        self.sequence += 1
        if ident is None:
            ident = self.sequence
        response = self.request(dict(id=ident, op='execute', sql=sql, search_mode=mode))
        assert_response(response, expected, dict(abs_tol=1e-5, rel_tol=1e-5, error_codes={}))
        return response

    def finish(self, shutdown=True):
        if shutdown:
            assert self.request({'id': 'shutdown', 'op': 'shutdown'}) == {'id': 'shutdown', 'ok': True}
        self.process.stdin.close()
        assert self.process.wait(timeout=30) == 0
        for reader in self.readers:
            reader.join(timeout=5)
            assert not reader.is_alive()
        assert self.lines.get(timeout=5) is None, 'Extra response after shutdown or EOF'
        self.process.stdout.close()
        self.process.stderr.close()

    def abort(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        for reader in self.readers:
            reader.join(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


def public_cases(driver, root):
    manifest = strict_json((ROOT / 'mysqltest/manifest.json').read_text(encoding='utf-8'))
    statements = 0
    for case in manifest['cases']:
        data = root / case['id']
        expected = strict_json((ROOT / 'mysqltest' / case['result']).read_text(encoding='utf-8'))
        client = Client(driver, data)
        mode, index = 'exact', 0
        try:
            for text in (ROOT / 'mysqltest' / case['test']).read_text(encoding='utf-8').splitlines():
                sql = text.strip()
                if not sql or sql.startswith('#'):
                    continue
                if sql == '--restart':
                    client.finish()
                    client = Client(driver, data)
                elif sql.startswith('--mode '):
                    mode = sql.split()[1]
                else:
                    client.execute(sql, expected[index], mode)
                    index += 1
                    statements += 1
            assert index == len(expected), case['id']
            client.finish()
        finally:
            client.abort()
        assert sorted(path.name for path in data.iterdir()) == ['pages.bin', 'redo.wal']
    return dict(groups=len(manifest['cases']), sql_statements=statements)


def protocol_edges(driver, root):
    data = root / 'unicode data \u4e2d\u6587'
    client = Client(driver, data)
    id_checks = 0
    malformed_checks = 0
    try:
        client.execute('CREATE TABLE docs(id BIGINT PRIMARY KEY, content VARCHAR(2048), v VECTOR(2));', {})
        text = 'quote\' and " and slash\\;\n\t\u4e2d\u6587\U0001f600'
        literal = text.replace("'", "''")
        client.execute(f"INSERT INTO docs VALUES(9223372036854775807, '{literal}', '[3,4]');",
                       {'affected_rows': 1})
        for ident in (-(2**63), 2**63-1, 2**64-1, 2**64+1, 2**100+123, -(2**100+123),
                      'request;\n\u4e2d\u6587', True, False, 1.25, [],
                      {'nested': [2**100+123, '\U0001f600', '{"id":7}']}):
            client.execute('SELECT id, content, v, L2_DISTANCE(v, \'[0,0]\') AS d FROM docs;',
                           {'columns': ['id', 'content', 'v', 'd'],
                            'rows': [[2**63-1, text, [3.0, 4.0], 5.0]]}, ident=ident)
            id_checks += 1
        escaped_key = ('{"sql":"SELECT id FROM docs;","op":"execute",'
                       '"search_mode":"exact","i\\u0064":' + str(2**100+123) + '}')
        response = client.raw_request(escaped_key.encode('utf-8'), 2**100+123)
        assert response['ok'] is True
        id_checks += 1
        response = client.request(dict(id=None, op='execute', sql='SELECT id FROM docs WHERE id < 0;',
                                       search_mode='exact'))
        assert response == dict(id=None, ok=True, columns=['id'], rows=[])
        id_checks += 1

        bad_requests = [
            (b'{', None),
            (b'[]', None),
            (b'{"id":1,"op":"execute","sql":"\xff","search_mode":"exact"}', None),
            (b'{"id":1,"op":"execute","sql":"\\uD800","search_mode":"exact"}', None),
            (b'{"id":2,"op":"other"}', 2),
            (b'{"id":3,"op":false}', 3),
            (b'{"id":4,"op":"execute","sql":2,"search_mode":"exact"}', 4),
            (b'{"id":5,"op":"execute","sql":"DELETE FROM docs;","search_mode":"bad"}', 5),
            (b'{"id":6,"op":"execute","sql":"DELETE FROM docs;"}', 6),
            (b'{"id":7,"id":8,"op":"execute","sql":"DELETE FROM docs;","search_mode":"exact"}', None),
            (b'{"id":{"x":1,"x":2},"op":"execute","sql":"DELETE FROM docs;","search_mode":"exact"}', None),
        ]
        for raw, ident in bad_requests:
            response = client.raw_request(raw, ident)
            assert response['ok'] is False and response['error']['code'], response
            malformed_checks += 1
        client.execute('SELECT id FROM docs;', {'rows': [[2**63-1]]})

        client.execute('CREATE TABLE large_rows(id BIGINT PRIMARY KEY, content VARCHAR(2048));', {})
        payload = '\u4e2d\u6587' * 200
        values = ','.join(f"({key},'{payload}')" for key in range(600))
        client.execute('INSERT INTO large_rows VALUES' + values + ';', {'affected_rows': 600})
        result = client.execute('SELECT id, content FROM large_rows ORDER BY id;',
                                {'rows': [[key, payload] for key in range(600)]})
        assert len(json.dumps(result, ensure_ascii=False).encode()) > 65536
        client.execute('BEGIN;', {})
        client.execute('DELETE FROM docs;', {'affected_rows': 1})
        client.finish()
    finally:
        client.abort()

    for shutdown in (True, False):
        client = Client(driver, data)
        try:
            client.execute('SELECT id FROM docs;', {'rows': [[2**63-1]]})
            client.execute('BEGIN;', {})
            client.execute('DELETE FROM docs;', {'affected_rows': 1})
            client.finish(shutdown=shutdown)
        finally:
            client.abort()
    client = Client(driver, data)
    try:
        client.execute('SELECT id FROM docs;', {'rows': [[2**63-1]]})
        client.finish()
    finally:
        client.abort()
    assert sorted(path.name for path in data.iterdir()) == ['pages.bin', 'redo.wal']
    return dict(id_type_checks=id_checks, malformed_request_checks=malformed_checks,
                single_response_rows=600, shutdown_and_eof_rollback=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--driver', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--edges-only', action='store_true')
    args = parser.parse_args()
    driver = args.driver.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='mvdb-stdio-') as directory:
        root = Path(directory)
        public = None if args.edges_only else public_cases(driver, root)
        edges = protocol_edges(driver, root)
    report = dict(scope='native-stdio-public-and-edge-checks', passed=True,
                  driver=str(driver), driver_sha256=hashlib.sha256(driver.read_bytes()).hexdigest(),
                  public=public, protocol=edges, docker_tested=False)
    args.report.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
