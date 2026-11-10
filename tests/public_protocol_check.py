"""Public protocol check for hosts without POSIX-only os helpers.

``protocol_check/_transport.py`` uses ``os.set_blocking`` and ``os.killpg``,
so the published driver only runs on POSIX hosts. This script keeps the same
request and response order and reuses the published assertion semantics, so
the public fixtures can also be checked from a Windows development machine.

Usage:
    python3 tests/public_protocol_check.py --driver build/minivectordb_development_server
"""

import argparse
import json
import math
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def strict_json(text):
    def invalid(value):
        raise ValueError("non-finite JSON number: " + value)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result

    return json.loads(text, parse_constant=invalid, object_pairs_hook=pairs)


def same(actual, expected, abs_tol, rel_tol):
    if isinstance(expected, float):
        return (type(actual) in (int, float) and math.isfinite(actual)
                and math.isclose(actual, expected, abs_tol=abs_tol, rel_tol=rel_tol))
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(same(a, b, abs_tol, rel_tol) for a, b in zip(actual, expected)))
    return type(actual) is type(expected) and actual == expected


def assert_response(response, expected, config):
    """The published assertion semantics from protocol_check/_assertions.py."""
    if "error" in expected:
        assert response.get("ok") is False, "statement should fail"
        code = response.get("error", {}).get("code")
        assert isinstance(code, str) and code, "missing error.code"
        required = config.get("error_codes", {}).get(expected["error"])
        if required is not None:
            assert code == required, "error.code %r != %r" % (code, required)
        return
    assert response.get("ok") is True, "statement failed: %s" % response
    for key in ("affected_rows", "columns"):
        if key in expected:
            assert same(response.get(key), expected[key], config["abs_tol"], config["rel_tol"]), \
                "%s: %r != %r" % (key, response.get(key), expected[key])
    if "rows" not in expected:
        return
    actual, wanted = response.get("rows"), expected["rows"]
    assert isinstance(actual, list), "missing rows"
    if expected.get("unordered"):
        remaining = list(actual)
        for row in wanted:
            index = next((i for i, candidate in enumerate(remaining)
                          if same(candidate, row, config["abs_tol"], config["rel_tol"])), None)
            assert index is not None, "missing row: %r" % row
            remaining.pop(index)
        assert not remaining, "unexpected rows: %r" % remaining[:5]
    else:
        assert same(actual, wanted, config["abs_tol"], config["rel_tol"]), \
            "rows: %r != %r" % (actual, wanted)


class Session:
    def __init__(self, command, data_dir):
        self.process = subprocess.Popen(
            command + ["--data", str(data_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1)
        self.buffer = ""
        self.stderr = []
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        for line in self.process.stderr:
            self.stderr.append(line)

    def read_line(self):
        while "\n" not in self.buffer:
            chunk = self.process.stdout.readline()
            if not chunk:
                raise RuntimeError("database exited before response")
            self.buffer += chunk
        line, self.buffer = self.buffer.split("\n", 1)
        return line

    def start(self):
        message = strict_json(self.read_line())
        assert message == {"type": "ready"}, "expected ready, got %r" % (message,)

    def request(self, sql=None, mode="exact", op="execute"):
        self.sequence = getattr(self, "sequence", 0) + 1
        request = {"id": self.sequence, "op": op}
        if sql is not None:
            request.update(sql=sql, search_mode=mode)
        self.process.stdin.write(json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n")
        self.process.stdin.flush()
        response = strict_json(self.read_line())
        if type(response.get("id")) is not type(request["id"]) or response["id"] != request["id"]:
            raise RuntimeError("response id differs from request")
        if type(response.get("ok")) is not bool:
            raise RuntimeError("missing boolean ok")
        return response

    def shutdown(self):
        response = self.request(op="shutdown")
        assert response["ok"] is True, "shutdown failed"
        self.process.stdin.close()
        assert self.process.wait(timeout=60) == 0, "nonzero exit after shutdown"
        extra = self.buffer + self.process.stdout.read()
        assert not extra, "extra stdout after shutdown: %r" % extra[:200]

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=30)


def run_case(command, case, cases, config, work):
    expectations = strict_json((cases / case["result"]).read_text(encoding="utf-8"))
    statements = (cases / case["test"]).read_text(encoding="utf-8").splitlines()
    data_dir = work / case["id"]
    data_dir.mkdir()
    session = Session(command, data_dir)
    try:
        session.start()
        index, mode = 0, "exact"
        for number, text in enumerate(statements, 1):
            sql = text.strip()
            if not sql or sql.startswith("#"):
                continue
            if sql == "--restart":
                session.shutdown()
                session.close()
                session = Session(command, data_dir)
                session.start()
                continue
            if sql in ("--mode exact", "--mode ann"):
                mode = sql.split()[1]
                continue
            if sql.startswith("--"):
                raise ValueError("unsupported public directive: " + sql)
            if index >= len(expectations):
                raise ValueError("missing expected result")
            response = session.request(sql, mode)
            try:
                assert_response(response, expectations[index], config)
            except AssertionError as exc:
                raise AssertionError("line %d: %s: %s" % (number, sql, exc)) from exc
            index += 1
        if index != len(expectations):
            raise ValueError("SQL/result count mismatch")
        session.shutdown()
    finally:
        session.close()
    return index


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--driver", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=ROOT / "mysqltest")
    parser.add_argument("--config", type=Path, default=ROOT / "protocol_check" / "config.json")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    command = [str(args.driver.resolve(strict=True)), "--development-protocol", "mvdb-judge-json-v1"]
    config = strict_json(args.config.read_text(encoding="utf-8"))
    cases = args.cases.resolve(strict=True)
    manifest = strict_json((cases / "manifest.json").read_text(encoding="utf-8"))
    failures = []
    with tempfile.TemporaryDirectory(prefix="minivectordb-public-protocol-") as temporary:
        work = Path(temporary)
        for case in manifest["cases"]:
            try:
                statements = run_case(command, case, cases, config, work)
                print("PASS %s statements=%d" % (case["id"], statements), flush=True)
            except Exception as exc:
                failures.append({"id": case["id"], "error": str(exc)})
                print("FAIL %s: %s" % (case["id"], exc), flush=True)
    report = {"scope": "public fixtures over the published JSONL protocol",
              "passed": not failures, "cases": len(manifest["cases"]), "failures": failures}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "cases": report["cases"],
                      "failed": len(failures)}))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
