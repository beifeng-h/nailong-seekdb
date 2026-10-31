#!/usr/bin/env python3
r"""Exercise AI-column DDL against an explicitly selected disposable local server.

Run beside the server after building and starting the candidate:
  python3 unittest/sql/ai_column/supplemental_regression.py \
      --host 127.0.0.1 --port 2881 --user root --disposable \
      --obclient /absolute/path/to/obclient --report /tmp/ai-column-extra.json

Only Python's standard library and obclient are required. Alternatively select
--driver pymysql with an already installed PyMySQL. Read the database password
from AI_COLUMN_TEST_PASSWORD or the environment variable named by --password-env.
No model credentials, downloads, production writes, or test-specific server
behavior are needed. The official mock implementation is imported unchanged.
Its HTTP server runs in a child process with a private event-log directory.

Each case uses real SQL. Unsupported syntax, wrong results, and timeouts fail.
--keep-database retains this run's uniquely named database and model metadata
for diagnosis; otherwise they are removed on success and failure.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid


def load_mock(source: Path, state: Path):
    spec = importlib.util.spec_from_file_location("official_ai_column_mock", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the official AI-column mock")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    module.state_dir = lambda port: str(state)
    return module


class SqlError(RuntimeError):
    def __init__(self, code: int | None, message: str):
        super().__init__(message)
        self.code = code


class SqlClient:
    def __init__(self, args):
        self.args = args
        self.connection = None
        self.binary = None
        if args.driver == "pymysql":
            try:
                import pymysql
            except ImportError as error:
                raise RuntimeError("--driver pymysql requires an already installed PyMySQL") from error
            self.connection = pymysql.connect(
                host=args.host, port=args.port, user=args.user,
                password=os.environ.get(args.password_env, ""),
                charset="utf8mb4", autocommit=True, connect_timeout=5,
                read_timeout=args.statement_timeout, write_timeout=args.statement_timeout,
            )
        else:
            self.binary = shutil.which(args.obclient)
            if self.binary is None:
                raise RuntimeError("obclient was not found; provide --obclient /absolute/path/to/obclient")

    def query(self, sql: str, *, strict: bool = False) -> list[tuple[str, ...]]:
        setting = "SET SESSION ai_column_strict_read = " + ("ON" if strict else "OFF")
        if self.connection is not None:
            try:
                with self.connection.cursor() as cursor:
                    cursor.execute(setting)
                    cursor.execute(sql)
                    return [tuple("NULL" if value is None else str(value) for value in row)
                            for row in cursor.fetchall()] if cursor.description else []
            except Exception as error:
                code = error.args[0] if error.args and isinstance(error.args[0], int) else None
                raise SqlError(code, str(error)) from error
        command = [
            self.binary, "--no-defaults", "--protocol=tcp", "--host=" + self.args.host,
            "--port=" + str(self.args.port), "--user=" + self.args.user,
            "--connect-timeout=5", "--default-character-set=utf8mb4",
            "--batch", "--raw", "--skip-column-names",
        ]
        environment = os.environ.copy()
        environment["MYSQL_PWD"] = os.environ.get(self.args.password_env, "")
        try:
            result = subprocess.run(
                command, input=setting + ";\n" + sql + ";\n", capture_output=True,
                text=True, encoding="utf-8", env=environment,
                timeout=self.args.statement_timeout, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise SqlError(None, "SQL client exceeded --statement-timeout") from error
        if result.returncode:
            match = re.search(r"\bERROR\s+(\d+)", result.stderr)
            raise SqlError(int(match.group(1)) if match else None, result.stderr.strip())
        return [tuple(line.split("\t")) for line in result.stdout.splitlines() if line]

    def close(self):
        if self.connection is not None:
            self.connection.close()


class MockServer:
    def __init__(self, source: Path):
        self.source = source.resolve(strict=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="seekdb-ai-extra-")
        self.state = Path(self.temporary.name)
        self.module = load_mock(self.source, self.state)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.process = None
        self.output = None

    def start(self):
        if self.process is not None:
            if self.process.poll() is None:
                return
            raise RuntimeError("the official mock exited unexpectedly")
        self.output = (self.state / "server.out").open("ab")
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "_serve_mock",
             str(self.source), str(self.state), str(self.port)],
            stdin=subprocess.DEVNULL, stdout=self.output, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("the official mock could not start: "
                                   + (self.state / "server.out").read_text(encoding="utf-8"))
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("the official mock did not become ready within five seconds")

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None
        if self.output is not None:
            self.output.close()
            self.output = None

    def reset_events(self):
        (self.state / "events.jsonl").write_text("", encoding="utf-8")

    def close(self):
        self.stop()
        self.temporary.cleanup()


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class Regression:
    def __init__(self, client: SqlClient, mock: MockServer, args):
        self.client = client
        self.mock = mock
        self.args = args
        self.name = "ai_extra_" + uuid.uuid4().hex[:16]
        self.complete = self.name + "_complete"
        self.embed = self.name + "_embed"
        self.created_database = False
        self.models = []
        self.endpoints = []
        self.checks = 0

    def table(self, name: str) -> str:
        return "`" + self.name + "`.`" + name + "`"

    def run_sql(self, sql: str, *, strict: bool = False):
        return self.client.query(sql, strict=strict)

    def scalar(self, sql: str, *, strict: bool = False) -> str:
        rows = self.run_sql(sql, strict=strict)
        if len(rows) != 1 or len(rows[0]) != 1:
            raise AssertionError("expected one SQL value; received " + repr(rows))
        return rows[0][0]

    def expect(self, label: str, sql: str, expected="1", *, strict: bool = False):
        actual = self.scalar(sql, strict=strict)
        if actual != expected:
            raise AssertionError(label + ": expected " + repr(expected) + ", received " + repr(actual))
        self.checks += 1

    def wait(self, label: str, sql: str, *, strict: bool = False):
        deadline = time.monotonic() + self.args.wait_seconds
        actual = None
        while time.monotonic() < deadline:
            try:
                actual = self.scalar(sql, strict=strict)
            except SqlError as error:
                if not strict or error.code != 11120:
                    raise
                actual = "AI_COLUMN_NOT_READY (11120)"
            if actual == "1":
                self.checks += 1
                return
            time.sleep(self.args.poll_interval)
        raise AssertionError(label + ": timed out; last SQL result was " + repr(actual))

    def incomplete(self, table: str, column: str = "result"):
        selection = "SELECT " + column + " FROM " + self.table(table)
        for sql in (selection, selection + " WHERE 0",
                    "SELECT 1 WHERE EXISTS (" + selection + " WHERE 0)"):
            try:
                self.run_sql(sql, strict=True)
            except SqlError as error:
                if error.code == 11120:
                    self.checks += 1
                    continue
                raise
            raise AssertionError(table + ": strict read accepted an incomplete AI column: " + sql)

    def tasks(self, table: str, predicate: str = "1 = 1") -> str:
        return (
            "SELECT COUNT(*) FROM oceanbase.__all_ai_column_task task "
            "JOIN oceanbase.__all_table t ON t.table_id = task.table_id "
            "JOIN oceanbase.__all_database d ON d.database_id = t.database_id "
            "WHERE d.database_name = " + literal(self.name)
            + " AND t.table_name = " + literal(table) + " AND (" + predicate + ")"
        )

    def version(self, table: str, column="result") -> str:
        return (
            "SELECT a.definition_version FROM oceanbase.__all_ai_column a "
            "JOIN oceanbase.__all_table t ON t.table_id = a.table_id "
            "JOIN oceanbase.__all_database d ON d.database_id = t.database_id "
            "WHERE d.database_name = " + literal(self.name)
            + " AND t.table_name = " + literal(table) + " AND a.column_name = " + literal(column)
        )

    def drained(self, table: str):
        self.wait(table + " tasks drained", "SELECT (" + self.tasks(table) + ") = 0")

    def frozen_tasks(self, table: str, prefix: str, count: int):
        version = self.scalar(self.version(table))
        predicate = (
            "task.definition_version = " + str(int(version))
            + " AND JSON_UNQUOTE(JSON_EXTRACT(task.input_json, '$.input')) LIKE "
            + literal(prefix + "%")
        )
        self.expect(table + " task cardinality", self.tasks(table), str(count))
        self.expect(table + " tasks use the current definition and frozen input",
                    self.tasks(table, predicate), str(count))

    def result_column(self, prefix: str) -> str:
        return ("result VARCHAR(500) AI COLUMN (AI_COMPLETE(" + literal(self.complete)
                + ", CONCAT(" + literal(prefix) + ", content))) "
                "WITH (sync_mode='async', max_retries=100)")

    def setup(self):
        self.expect("SQL connection", "SELECT 1")
        self.run_sql("CREATE DATABASE `" + self.name + "`")
        self.created_database = True
        for name, kind, path in (
            (self.complete, "completion", "/v1/chat/completions"),
            (self.embed, "dense_embedding", "/v1/embeddings"),
        ):
            self.run_sql("CALL DBMS_AI_SERVICE.CREATE_AI_MODEL(" + literal(name) + ", "
                         + literal(json.dumps({"type": kind, "model_name": "mock-extra"})) + ")")
            self.models.append(name)
            endpoint = name + "_ep"
            config = {"ai_model_name": name, "url": "http://127.0.0.1:" + str(self.mock.port) + path,
                      "access_key": "public-test-key", "provider": "openai",
                      "request_model_name": "mock-extra"}
            self.run_sql("CALL DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT(" + literal(endpoint)
                         + ", " + literal(json.dumps(config)) + ")")
            self.endpoints.append(endpoint)

    def existing_rows_add(self):
        self.mock.stop()
        table = self.table("existing_add")
        self.run_sql("CREATE TABLE " + table + " (id BIGINT PRIMARY KEY, content VARCHAR(100))")
        self.run_sql("INSERT INTO " + table + " VALUES (1,'alpha'),(2,'beta'),(3,NULL)")
        self.run_sql("ALTER TABLE " + table + " ADD COLUMN " + self.result_column("add-v1:"))
        self.expect("ADD preserves all existing rows with initially null results",
                    "SELECT COUNT(*) = 3 AND SUM(result IS NULL) = 3 FROM " + table)
        self.wait("ADD schedules both non-null existing rows",
                  "SELECT (" + self.tasks("existing_add", "JSON_EXTRACT(task.input_json, '$.input') IS NOT NULL "
                                         "AND JSON_TYPE(JSON_EXTRACT(task.input_json, '$.input')) <> 'NULL'") + ") = 2")
        self.expect("ADD freezes the new expression in existing-row tasks",
                    self.tasks("existing_add", "JSON_UNQUOTE(JSON_EXTRACT(task.input_json, '$.input')) "
                               "IN ('add-v1:alpha','add-v1:beta')"), "2")
        self.incomplete("existing_add")
        self.mock.start()
        self.wait("ADD generates exact existing-row results",
                  "SELECT COUNT(*) = 3 AND SUM((id = 1 AND result = 'mock:add-v1:alpha') "
                  "OR (id = 2 AND result = 'mock:add-v1:beta') OR (id = 3 AND result IS NULL)) = 3 FROM " + table,
                  strict=True)
        self.drained("existing_add")

    def ready_modify(self):
        self.mock.start()
        table = self.table("ready_modify")
        stable = ("stable VARCHAR(500) AI COLUMN (AI_COMPLETE(" + literal(self.complete)
                  + ", CONCAT('stable:', content))) WITH (sync_mode='async', max_retries=100)")
        self.run_sql("CREATE TABLE " + table + " (id BIGINT PRIMARY KEY, content VARCHAR(100), "
                     + self.result_column("ready-v1:") + ", " + stable + ")")
        self.run_sql("INSERT INTO " + table + " (id,content) VALUES (1,'alpha'),(2,'beta')")
        self.wait("original definition becomes READY",
                  "SELECT COUNT(*) = 2 AND SUM(result = CONCAT('mock:ready-v1:',content) "
                  "AND stable = CONCAT('mock:stable:',content)) = 2 FROM " + table, strict=True)
        self.drained("ready_modify")
        previous = int(self.scalar(self.version("ready_modify")))
        self.mock.stop()
        self.run_sql("ALTER TABLE " + table + " MODIFY COLUMN " + self.result_column("ready-v2:"))
        self.expect("MODIFY advances the definition version",
                    "SELECT (" + self.version("ready_modify") + ") > " + str(previous))
        self.expect("MODIFY invalidates READY results and preserves the other AI column",
                    "SELECT COUNT(*) = 2 AND SUM(result IS NULL "
                    "AND stable = CONCAT('mock:stable:',content)) = 2 FROM " + table)
        self.frozen_tasks("ready_modify", "ready-v2:", 2)
        self.expect("strict read still accepts the unaffected AI column",
                    "SELECT COUNT(*) = 2 AND SUM(stable = CONCAT('mock:stable:',content)) = 2 FROM " + table,
                    strict=True)
        self.incomplete("ready_modify")
        self.mock.start()
        self.wait("MODIFY regenerates all formerly READY rows",
                  "SELECT COUNT(*) = 2 AND SUM(result = CONCAT('mock:ready-v2:',content)) = 2 FROM " + table,
                  strict=True)
        self.drained("ready_modify")

    def pending_modify(self):
        self.mock.stop()
        table = self.table("pending_modify")
        self.run_sql("CREATE TABLE " + table + " (id BIGINT PRIMARY KEY, content VARCHAR(100), "
                     + self.result_column("pending-v1:") + ")")
        self.run_sql("INSERT INTO " + table + " (id,content) VALUES (1,'alpha'),(2,'beta')")
        previous = int(self.scalar(self.version("pending_modify")))
        self.frozen_tasks("pending_modify", "pending-v1:", 2)
        self.incomplete("pending_modify")
        self.run_sql("ALTER TABLE " + table + " MODIFY COLUMN " + self.result_column("pending-v2:"))
        self.expect("offline redefinition advances the version",
                    "SELECT (" + self.version("pending_modify") + ") > " + str(previous))
        self.expect("offline redefinition keeps pending results hidden",
                    "SELECT COUNT(*) = 2 AND SUM(result IS NULL) = 2 FROM " + table)
        self.frozen_tasks("pending_modify", "pending-v2:", 2)
        self.expect("old pending tasks are removed", self.tasks("pending_modify",
                    "task.definition_version = " + str(previous)), "0")
        self.incomplete("pending_modify")
        self.mock.start()
        sql = ("SELECT COUNT(*) = 2 AND SUM(result = CONCAT('mock:pending-v2:',content)) = 2 FROM " + table)
        self.wait("resuming the model uses only the new definition", sql, strict=True)
        self.drained("pending_modify")
        time.sleep(2)
        self.expect("old pending work cannot overwrite the new result", sql, strict=True)

    def ordinary_column_to_ai(self):
        self.mock.stop()
        table = self.table("ordinary_to_ai")
        self.run_sql("CREATE TABLE " + table
                     + " (id BIGINT PRIMARY KEY, content VARCHAR(100), result VARCHAR(500))")
        self.run_sql("INSERT INTO " + table
                     + " VALUES (1,'alpha','legacy-alpha'),(2,'beta','legacy-beta')")
        self.expect("ordinary result values exist before conversion",
                    "SELECT COUNT(*) = 2 AND SUM(result = CONCAT('legacy-',content)) = 2 FROM " + table)
        self.expect("ordinary result has no AI definition before conversion",
                    "SELECT COUNT(*) FROM (" + self.version("ordinary_to_ai") + ") metadata", "0")
        self.run_sql("ALTER TABLE " + table + " MODIFY COLUMN " + self.result_column("converted:"))
        self.expect("same-type conversion creates a versioned AI definition",
                    "SELECT (" + self.version("ordinary_to_ai") + ") > 0")
        self.expect("same-type conversion invalidates every ordinary stored result",
                    "SELECT COUNT(*) = 2 AND SUM(result IS NULL) = 2 FROM " + table)
        self.frozen_tasks("ordinary_to_ai", "converted:", 2)
        self.incomplete("ordinary_to_ai")
        self.mock.start()
        self.wait("same-type conversion replaces the ordinary values with generated results",
                  "SELECT COUNT(*) = 2 AND SUM(result = CONCAT('mock:converted:',content)) = 2 FROM " + table,
                  strict=True)
        self.drained("ordinary_to_ai")

    def vector_index_ddl(self):
        self.mock.start()
        table = self.table("indexed_ddl")
        embedding = ("emb VECTOR(4) AI COLUMN (AI_EMBED(" + literal(self.embed)
                     + ", content)) WITH (sync_mode='async', max_retries=100)")
        self.run_sql("CREATE TABLE " + table + " (id BIGINT PRIMARY KEY, content VARCHAR(100), " + embedding + ")")
        self.run_sql("INSERT INTO " + table + " (id,content) VALUES (1,'vec-zero'),(2,'vec-ten')")
        self.wait("indexed table initial embeddings",
                  "SELECT COUNT(*) = 2 AND SUM((id = 1 AND emb = [0,0,0,0]) "
                  "OR (id = 2 AND emb = [10,0,0,0])) = 2 FROM " + table, strict=True)
        self.drained("indexed_ddl")
        self.run_sql("CREATE VECTOR INDEX idx_extra_emb ON " + table + "(emb) "
                     "WITH(distance=l2,type=hnsw,lib=vsag,m=10,ef_construction=12,ef_search=40)")
        ann = ("SELECT COUNT(*) = 2 AND SUM(id IN (1,2)) = 2 FROM (SELECT id FROM " + table
               + " ORDER BY L2_DISTANCE(emb,[0,0,0,0]) APPROXIMATE LIMIT 2) ann")
        self.expect("initial ANN index is usable", ann)
        self.mock.stop()
        self.run_sql("ALTER TABLE " + table + " ADD COLUMN " + self.result_column("index-add:"))
        self.expect("ADD on an indexed AI table preserves existing vectors", ann)
        self.frozen_tasks("indexed_ddl", "index-add:", 2)
        self.mock.start()
        self.wait("ADD on an indexed AI table generates results",
                  "SELECT COUNT(*) = 2 AND SUM(result = CONCAT('mock:index-add:',content)) = 2 FROM " + table,
                  strict=True)
        self.drained("indexed_ddl")
        self.mock.stop()
        self.run_sql("ALTER TABLE " + table + " MODIFY COLUMN " + self.result_column("index-modify:"))
        self.expect("MODIFY text AI on an indexed AI table preserves ANN", ann)
        self.frozen_tasks("indexed_ddl", "index-modify:", 2)
        self.mock.start()
        self.wait("MODIFY on an indexed AI table regenerates results",
                  "SELECT COUNT(*) = 2 AND SUM(result = CONCAT('mock:index-modify:',content)) = 2 FROM " + table,
                  strict=True)
        self.drained("indexed_ddl")
        self.mock.stop()
        self.run_sql("ALTER TABLE " + table + " MODIFY COLUMN emb VECTOR(4) AI COLUMN (AI_EMBED("
                     + literal(self.embed) + ", CONCAT('vec-twenty:',id))) "
                     "WITH(sync_mode='async',max_retries=100)")
        self.expect("redefining an indexed AI vector invalidates old vectors",
                    "SELECT COUNT(*) = 2 AND SUM(emb IS NULL) = 2 FROM " + table)
        self.expect("old vectors disappear from ANN during redefinition",
                    "SELECT COUNT(*) = 0 FROM (SELECT id FROM " + table
                    + " ORDER BY L2_DISTANCE(emb,[0,0,0,0]) APPROXIMATE LIMIT 2) ann")
        self.incomplete("indexed_ddl", "emb")
        self.mock.start()
        self.wait("indexed vector redefinition publishes the new values",
                  "SELECT COUNT(*) = 2 AND SUM(emb = [20,0,0,0]) = 2 FROM " + table, strict=True)
        self.drained("indexed_ddl")
        self.expect("ANN contains all regenerated vectors", ann, strict=True)

    def null_ready_strict(self):
        self.mock.stop()
        table = self.table("null_ready")
        self.run_sql("CREATE TABLE " + table + " (id BIGINT PRIMARY KEY, content VARCHAR(100), "
                     "result VARCHAR(500) AI COLUMN (AI_COMPLETE(" + literal(self.complete)
                     + ", content)) WITH(sync_mode='async',max_retries=0), "
                     "emb VECTOR(4) AI COLUMN (AI_EMBED(" + literal(self.embed)
                     + ", content)) WITH(sync_mode='async',max_retries=0))")
        self.run_sql("INSERT INTO " + table + " (id,content) VALUES (1,NULL),(2,NULL)")
        nulls = "SELECT COUNT(*) = 2 AND SUM(result IS NULL AND emb IS NULL) = 2 FROM " + table
        self.wait("NULL input reaches READY even while the endpoint is offline", nulls, strict=True)
        self.drained("null_ready")
        self.expect("optimized empty strict query accepts READY NULL values",
                    "SELECT COUNT(*) = 0 FROM (SELECT result FROM " + table + " WHERE 0) empty_rows",
                    strict=True)
        self.mock.start()
        self.run_sql("UPDATE " + table + " SET content = 'vec-zero' WHERE id = 1")
        self.wait("NULL input can transition to a generated value",
                  "SELECT result = 'mock:vec-zero' AND emb = [0,0,0,0] FROM " + table + " WHERE id = 1",
                  strict=True)
        self.drained("null_ready")
        self.mock.reset_events()
        self.run_sql("UPDATE " + table + " SET content = NULL WHERE id = 1")
        self.wait("a generated value can transition to READY NULL", nulls, strict=True)
        self.drained("null_ready")
        self.mock.module.assert_events(self.mock.port, "no-requests")

    def parallel_dml_hint(self):
        source = self.table("parallel_source")
        self.run_sql("CREATE TABLE " + source + " (id BIGINT PRIMARY KEY, content VARCHAR(100))")
        values = ",".join("(" + str(row) + ",'row-" + str(row) + "')" for row in range(1, 9))
        self.run_sql("INSERT INTO " + source + " VALUES " + values)
        groups = (
            ("hint_pdml", "ENABLE_PARALLEL_DML PARALLEL(4)", "pdml:"),
            ("hint_das", "DISABLE_PARALLEL_DML ENABLE_PARALLEL_DAS_DML PARALLEL(4)", "das:"),
        )
        for name, hint, prefix in groups:
            self.mock.stop()
            table = self.table(name)
            self.run_sql("CREATE TABLE " + table + " (id BIGINT PRIMARY KEY, content VARCHAR(100), "
                         + self.result_column(prefix) + ")")
            self.run_sql("INSERT /*+ " + hint + " */ INTO " + table + " (id,content) "
                         "SELECT id,content FROM " + source)
            self.expect(name + " hinted INSERT SELECT preserves every source row",
                        "SELECT COUNT(*) = 8 AND SUM(result IS NULL) = 8 FROM " + table)
            self.frozen_tasks(name, prefix, 8)
            matching_input = (
                "EXISTS (SELECT 1 FROM " + table + " live "
                "WHERE task.rowkey_predicate = CONCAT('`id` = ',live.id) "
                "AND JSON_UNQUOTE(JSON_EXTRACT(task.input_json,'$.input')) = CONCAT("
                + literal(prefix) + ",live.content))"
            )
            self.expect(name + " hinted INSERT SELECT freezes each row's own input",
                        self.tasks(name, matching_input), "8")
            self.run_sql("UPDATE /*+ " + hint + " */ " + table
                         + " SET content = CONCAT(content,':updated') WHERE id <= 4")
            self.expect(name + " hinted UPDATE writes the selected rows",
                        "SELECT COUNT(*) = 8 AND SUM(result IS NULL) = 8 "
                        "AND SUM(content LIKE '%:updated') = 4 FROM " + table)
            self.frozen_tasks(name, prefix, 8)
            self.expect(name + " hinted UPDATE replaces stale input without duplicating tasks",
                        self.tasks(name, matching_input), "8")
            self.run_sql("DELETE /*+ " + hint + " */ FROM " + table + " WHERE id IN (2,4,8)")
            self.expect(name + " hinted DELETE preserves precisely the surviving rows",
                        "SELECT COUNT(*) = 5 AND SUM(id IN (1,3,5,6,7)) = 5 "
                        "AND SUM(result IS NULL) = 5 FROM " + table)
            self.frozen_tasks(name, prefix, 5)
            self.expect(name + " hinted DELETE removes the deleted rows' tasks",
                        self.tasks(name, "task.rowkey_predicate IN ('`id` = 2','`id` = 4','`id` = 8')"), "0")
            self.expect(name + " surviving tasks still match the latest input",
                        self.tasks(name, matching_input), "5")
            self.incomplete(name)
            self.mock.start()
            self.wait(name + " surviving hinted writes produce the latest results",
                      "SELECT COUNT(*) = 5 AND SUM(result = CONCAT('mock:',"
                      + literal(prefix) + ",content)) = 5 FROM " + table, strict=True)
            self.drained(name)

    def cleanup(self) -> list[str]:
        failures = []
        if self.args.keep_database:
            return failures
        statements = []
        if self.created_database:
            statements.append("DROP DATABASE `" + self.name + "`")
        statements.extend("CALL DBMS_AI_SERVICE.DROP_AI_MODEL_ENDPOINT(" + literal(name) + ")"
                          for name in reversed(self.endpoints))
        statements.extend("CALL DBMS_AI_SERVICE.DROP_AI_MODEL(" + literal(name) + ")"
                          for name in reversed(self.models))
        for statement in statements:
            try:
                self.run_sql(statement)
            except Exception as error:
                failures.append(statement + ": " + str(error))
        return failures


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="numeric loopback address; run beside the server")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--user", required=True)
    parser.add_argument("--disposable", action="store_true", help="confirm this is a disposable test instance")
    parser.add_argument("--driver", choices=("obclient", "pymysql"), default="obclient")
    parser.add_argument("--obclient", default="obclient", help="SQL client executable name or absolute path")
    parser.add_argument("--password-env", default="AI_COLUMN_TEST_PASSWORD")
    parser.add_argument("--wait-seconds", type=float, default=60)
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--statement-timeout", type=int, default=180)
    parser.add_argument("--keep-database", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        local = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        local = False
    if not local or not args.disposable:
        parser.error("provide a numeric loopback --host and --disposable; remote/implicit targets are rejected")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.wait_seconds <= 0 or args.poll_interval <= 0 or args.statement_timeout <= 0:
        parser.error("timeouts and poll interval must be positive")
    return args


def main() -> int:
    args = parse_args()
    source = Path(__file__).resolve().parents[3] / "tools/deploy/mysql_test/test_suite/ai_column/include/ai_column_mock.py"
    mock = None
    client = None
    suite = None
    report = {"status": "failed", "cases": [], "checks": 0,
              "official_mock_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    started = time.monotonic()
    try:
        mock = MockServer(source)
        client = SqlClient(args)
        suite = Regression(client, mock, args)
        report["database"] = suite.name
        report["retained"] = args.keep_database
        print("Disposable database: " + suite.name, flush=True)
        suite.setup()
        for name in ("existing_rows_add", "ready_modify", "pending_modify", "ordinary_column_to_ai",
                     "vector_index_ddl", "null_ready_strict", "parallel_dml_hint"):
            case_started = time.monotonic()
            case = {"name": name, "status": "failed"}
            report["cases"].append(case)
            print("RUN " + name, flush=True)
            getattr(suite, name)()
            case.update(status="passed", elapsed_seconds=round(time.monotonic() - case_started, 3))
            print("PASS " + name, flush=True)
        report["status"] = "passed"
    except Exception as error:
        report["error"] = type(error).__name__ + ": " + str(error)
        print("FAIL " + report["error"], file=sys.stderr, flush=True)
    finally:
        if suite is not None:
            report["checks"] = suite.checks
            failures = suite.cleanup()
            if failures:
                report["cleanup_errors"] = failures
                report["status"] = "failed"
        if client is not None:
            client.close()
        if mock is not None:
            mock.close()
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "_serve_mock":
        load_mock(Path(sys.argv[2]), Path(sys.argv[3])).serve(int(sys.argv[4]))
    else:
        raise SystemExit(main())
