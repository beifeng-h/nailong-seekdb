#!/usr/bin/env python3
"""DEV_ONLY_NOT_OFFICIAL: real HTTP + parallel SQL URL-cap probe; plan-only by default."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

CLASSIFICATION = "DEV_ONLY_NOT_OFFICIAL"
FORBIDDEN_PORTS = {8000, 19121, 19122}
ROUTE = "/v1/chat/completions"

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")

def literal(value):
    return "'" + value.replace("'", "''") + "'"

def verdict(control, target, count):
    """Pure evidence rule: lack of pressure or missing evidence can never pass."""
    phases = (control, target)
    if any(p.get("sql_success", 0) != count or p.get("completed", 0) < count
           or p.get("http_success", 0) < count or p.get("active_processing", 0) != 0
           or not p.get("configuration_verified") or not p.get("binding_verified")
           or p.get("invalid_http", 0) != 0 for p in phases):
        return "failed", "SQL/HTTP completion evidence is incomplete"
    if target["peak_active_processing"] > 3:
        return "failed", "same-URL cap 3 was exceeded"
    if control["peak_active_processing"] <= 3:
        return "inconclusive", "cap-1024 control did not demonstrate pressure above 3"
    return "passed", "control pressure exceeded 3 and shared target peak stayed at most 3"

class SqlError(RuntimeError):
    def __init__(self, code, returncode, stderr_sha256):
        super().__init__("SQL client failed; code=" + str(code))
        self.details = {"sql_error_code": code, "returncode": returncode,
                        "stderr_sha256": stderr_sha256}

class SqlClient:
    """Every query gets a fresh obclient process/SQL connection; no shell or key logging."""
    def __init__(self, args, binary, deadline=None, timeout=None):
        self.args, self.binary, self.deadline = args, binary, deadline
        self.timeout = timeout or args.statement_timeout
    def query(self, sql):
        remaining = self.timeout if self.deadline is None else min(self.timeout, self.deadline-time.monotonic())
        if remaining <= 0:
            raise TimeoutError("developer run budget exhausted")
        command = [self.binary, "--no-defaults", "--protocol=tcp", "--host="+self.args.sql_host,
                   "--port="+str(self.args.sql_port), "--user="+self.args.sql_user,
                   "--connect-timeout=5", "--default-character-set=utf8mb4",
                   "--batch", "--raw", "--skip-column-names"]
        environment = os.environ.copy()
        environment["MYSQL_PWD"] = os.environ.get(self.args.password_env, "")
        result = subprocess.run(command, input=sql+";\n", capture_output=True, text=True,
                                encoding="utf-8", env=environment, timeout=remaining, check=False)
        if result.returncode:
            match = re.search(r"\bERROR\s+(\d+)", result.stderr)
            raise SqlError(int(match.group(1)) if match else None, result.returncode,
                           sha(result.stderr.encode("utf-8")))
        return [tuple(row.split("\t")) for row in result.stdout.splitlines() if row]

def error_summary(error):
    if isinstance(error, SqlError):
        return {"type": type(error).__name__, **error.details}
    if isinstance(error, subprocess.TimeoutExpired):
        return {"type": "SqlClientTimeout"}
    # Fixed classes/codes only: neither SQL text nor arbitrary exception content is persisted.
    return {"type": type(error).__name__}

class Audit:
    def __init__(self, output):
        self.path = output / "http_events.jsonl"
        self.lock = threading.Lock()
        self.phase = None
        self.sequence = 0
        self.total_active = 0
        self.stats = {}
    def write(self, kind, **values):
        record = {"classification": CLASSIFICATION, "kind": kind,
                  "monotonic_ns": time.monotonic_ns(),
                  "utc": datetime.now(timezone.utc).isoformat(), **values}
        with self.path.open("a", encoding="utf-8", newline="\n") as out:
            out.write(json.dumps(record, ensure_ascii=False, allow_nan=False)+"\n")
    def begin_phase(self, name):
        with self.lock:
            if self.total_active or any(x["completed"] != x["arrived"] for x in self.stats.values()):
                raise RuntimeError("previous HTTP phase still active")
            self.phase = name
            self.stats[name] = {"arrived": 0, "completed": 0, "http_success": 0,
                                "active_processing": 0, "peak_active_processing": 0, "invalid_http": 0,
                                "user_input_sha256": [], "model_sha256": []}
    def arrive(self, raw, body, authorization_present):
        with self.lock:
            phase = self.phase
            if phase is None:
                raise RuntimeError("HTTP arrived outside a probe phase")
            self.sequence += 1
            request_id = self.sequence
            stat = self.stats[phase]
            stat["arrived"] += 1; stat["active_processing"] += 1; self.total_active += 1
            stat["peak_active_processing"] = max(stat["peak_active_processing"], stat["active_processing"])
            contents = [m["content"].encode("utf-8") for m in body["messages"]]
            users = [sha(m["content"].encode("utf-8")) for m in body["messages"] if m["role"] == "user"]
            model_hash = sha(body["model"].encode("utf-8"))
            stat["user_input_sha256"].extend(users); stat["model_sha256"].append(model_hash)
            self.write("arrive", phase=phase, request_id=request_id, path=ROUTE,
                       request_sha256=sha(raw), request_bytes=len(raw),
                       model_sha256=model_hash, user_input_sha256=users,
                       input_sha256=[sha(x) for x in contents], input_bytes=[len(x) for x in contents],
                       authorization_present=authorization_present,
                       active_processing=stat["active_processing"], peak_active_processing=stat["peak_active_processing"])
            return phase, request_id, time.monotonic_ns()
    def work_end(self, phase, request_id, started):
        # End before response delivery: a client may immediately reuse its released permit.
        with self.lock:
            stat = self.stats[phase]
            stat["active_processing"] -= 1; self.total_active -= 1
            self.write("work_end", phase=phase, request_id=request_id,
                       active_processing=stat["active_processing"], peak_active_processing=stat["peak_active_processing"],
                       work_duration_ns=time.monotonic_ns()-started)
    def complete(self, phase, request_id, started, success, response):
        with self.lock:
            stat = self.stats[phase]
            stat["completed"] += 1; stat["http_success"] += int(success)
            self.write("end", phase=phase, request_id=request_id, success=success,
                       active_processing=stat["active_processing"], peak_active_processing=stat["peak_active_processing"],
                       duration_ns=time.monotonic_ns()-started,
                       response_sha256=sha(response), response_bytes=len(response))
    def invalid(self, raw):
        with self.lock:
            if self.phase is not None:
                self.stats[self.phase]["invalid_http"] += 1
            self.write("invalid", phase=self.phase, request_sha256=sha(raw))
    def snapshot(self, phase):
        with self.lock:
            return dict(self.stats[phase])

def make_handler(audit, delay_seconds):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 10
        def log_message(self, *args):
            pass
        def do_POST(self):
            ticket = None; response = b""; raw = b""; success = False; work_finished = False
            self.close_connection = True
            try:
                if self.path != ROUTE or self.headers.get("Transfer-Encoding"):
                    raise ValueError("unsupported request framing")
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= length <= 2*1024*1024:
                    raise ValueError("invalid length")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("truncated body")
                body = json.loads(raw)
                if not isinstance(body, dict) or not isinstance(body.get("model"), str) or not body["model"]:
                    raise ValueError("invalid model")
                messages = body.get("messages")
                if not isinstance(messages, list) or not messages:
                    raise ValueError("invalid messages")
                if any(not isinstance(m, dict) or not isinstance(m.get("role"), str)
                       or not isinstance(m.get("content"), str) for m in messages):
                    raise ValueError("invalid message")
                ticket = audit.arrive(raw, body, "Authorization" in self.headers)
                time.sleep(delay_seconds)
                # Generic protocol text, independent of names/prompts/expected outputs.
                response = encoded({"choices": [{"message": {"role": "assistant",
                                    "content": "DEV_ONLY_GENERIC_NONEMPTY_RESPONSE"}}]})
                audit.work_end(*ticket); work_finished = True
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.send_header("Connection", "close")
                self.end_headers(); self.wfile.write(response); self.wfile.flush()
                success = True
            except (ValueError, TypeError, UnicodeError, OSError):
                # Invalid/closed requests never masquerade as completed model responses.
                if ticket is None:
                    audit.invalid(raw)
                    try:
                        self.send_error(400, "DEV_ONLY invalid request")
                    except OSError:
                        pass
            finally:
                if ticket is not None:
                    if not work_finished:
                        audit.work_end(*ticket)
                    audit.complete(*ticket, success, response)
    return Handler

def run_phase(name, cap, models, endpoints, database, client, args, binary, deadline, audit):
    audit.begin_phase(name)
    for endpoint in endpoints:
        client.query("CALL DBMS_AI_SERVICE.ALTER_AI_MODEL_ENDPOINT("+literal(endpoint)+","
                     +literal(json.dumps({"max_concurrency": cap}))+ ")")
    actual_config = client.query("SELECT ENDPOINT_NAME,MAX_CONCURRENCY FROM oceanbase.DBA_OB_AI_MODEL_ENDPOINTS "
        "WHERE ENDPOINT_NAME IN ("+",".join(literal(x) for x in endpoints)+") ORDER BY ENDPOINT_NAME")
    configuration_verified = actual_config == [(x,str(cap)) for x in sorted(endpoints)]
    if not configuration_verified:
        stat = audit.snapshot(name)
        stat.update(cap=cap,actual_config=actual_config,configuration_verified=False,
                    binding_verified=False,sql_success=0,requests=args.requests,clients=[])
        return stat
    barrier = threading.Barrier(args.requests, timeout=10)
    def one(index):
        query_client = SqlClient(args, binary, deadline)
        model = models[index % len(models)]
        prompt = "developer-independent-"+uuid.uuid4().hex
        sql = "USE `"+database+"`; SELECT LENGTH(AI_COMPLETE("+literal(model)+","+literal(prompt)+")) > 0"
        started = time.monotonic_ns()
        try:
            barrier.wait()
            rows = query_client.query(sql)
            ok = rows == [("1",)]
            return {"index": index, "success": ok, "duration_ns": time.monotonic_ns()-started,
                    "input_sha256": sha(prompt.encode("utf-8")), "sql_sha256": sha(sql.encode("utf-8"))}
        except Exception as error:
            return {"index": index, "success": False, "duration_ns": time.monotonic_ns()-started,
                    "error": error_summary(error)}
    with ThreadPoolExecutor(max_workers=args.requests) as pool:
        results = list(pool.map(one, range(args.requests)))
    until = min(deadline, time.monotonic()+10)
    while time.monotonic() < until:
        pending = audit.snapshot(name)
        if not pending["active_processing"] and pending["completed"] == pending["arrived"]:
            break
        time.sleep(0.02)
    stat = audit.snapshot(name)
    expected_inputs = [r["input_sha256"] for r in results if r["success"]]
    binding_verified = (Counter(stat["user_input_sha256"]) == Counter(expected_inputs)
                        and len(expected_inputs) == args.requests
                        and len(stat["model_sha256"]) == stat["arrived"]
                        and all(x == sha(b"developer-generic-chat") for x in stat["model_sha256"]))
    stat.update(cap=cap, actual_config=actual_config, configuration_verified=True,
                binding_verified=binding_verified,
                sql_success=sum(r["success"] for r in results), requests=args.requests, clients=results)
    return stat

def execute(args):
    binary = shutil.which(args.obclient)
    if binary is None:
        raise ValueError("existing obclient is required")
    args.report_dir.mkdir(parents=True, exist_ok=False)
    report = {"classification": CLASSIFICATION, "status": "failed", "phases": {},
              "service_started": False, "cleanup": [], "requests_per_phase": args.requests,
              "fixture_delay_ms": args.delay_ms,
              "peak_metric": "peak_active_processing: valid body accepted through fixed work done, before response flush",
              "script_sha256": sha(Path(__file__).read_bytes())}
    token = uuid.uuid4().hex[:16]
    database = "dev_urlcap_"+token
    models = ["dev_urlcap_"+token+"_m"+str(i) for i in range(2)]
    endpoints = [model+"_ep" for model in models]
    report.update(database=database, models=models, endpoints=endpoints)
    created = []; server = None; thread = None
    deadline = time.monotonic()+args.run_budget_seconds
    client = SqlClient(args, binary, deadline)
    try:
        audit = Audit(args.report_dir)
        server = ThreadingHTTPServer(("127.0.0.1", args.fixture_port), make_handler(audit,args.delay_ms/1000))
        server.daemon_threads = True
        if server.server_port in FORBIDDEN_PORTS:
            raise ValueError("selected forbidden fixture port")
        url = "http://127.0.0.1:"+str(server.server_port)+ROUTE
        report["url"] = url
        thread = threading.Thread(target=server.serve_forever, name="DEV_ONLY_HTTP", daemon=True)
        thread.start(); report["service_started"] = True
        client.query("CREATE DATABASE `"+database+"`"); created.append(("database",database))
        for model,endpoint in zip(models,endpoints):
            config = {"type":"completion", "model_name":"developer-generic-chat"}
            client.query("CALL DBMS_AI_SERVICE.CREATE_AI_MODEL("+literal(model)+","+literal(json.dumps(config))+")")
            created.append(("model",model))
            config = {"ai_model_name":model,"scope":"all","url":url,"provider":"openai",
                      "access_key":"dev-only-"+uuid.uuid4().hex,"max_concurrency":3}
            client.query("CALL DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT("+literal(endpoint)+","+literal(json.dumps(config))+")")
            created.append(("endpoint",endpoint))
        report["phases"]["control"] = run_phase("control",1024,models,endpoints,database,client,args,binary,deadline,audit)
        report["phases"]["target"] = run_phase("target",3,models,endpoints,database,client,args,binary,deadline,audit)
        report["status"],report["reason"] = verdict(report["phases"]["control"],report["phases"]["target"],args.requests)
    except Exception as error:
        report["error"] = error_summary(error)
    finally:
        cleanup_client = SqlClient(args,binary,timeout=15)
        # UUID-owned names are cleaned even when CREATE committed but its reply was lost.
        cleanup_names = ([("endpoint",x) for x in reversed(endpoints)]
                         +[("model",x) for x in reversed(models)]+[("database",database)])
        for kind,name in cleanup_names:
            sql = ("DROP DATABASE IF EXISTS `"+name+"`" if kind=="database" else
                   "CALL DBMS_AI_SERVICE.DROP_AI_MODEL"+("_ENDPOINT" if kind=="endpoint" else "")+"("+literal(name)+")")
            item = {"kind":kind,"name":name,"success":False}
            try:
                cleanup_client.query(sql); item["success"] = True
            except SqlError as error:
                if error.details["sql_error_code"] == (11112 if kind=="endpoint" else 11118 if kind=="model" else None):
                    item.update(success=True,absent=True)
                else:
                    item["error"] = error_summary(error)
            except Exception as error:
                item["error"] = error_summary(error)
            report["cleanup"].append(item)
        if server is not None:
            if thread is not None:
                server.shutdown(); thread.join(timeout=5)
            server.server_close()
        if any(not item["success"] for item in report["cleanup"]):
            report["status"] = "failed"; report["reason"] = "owned SQL cleanup failed"
        report["elapsed_seconds"] = round(args.run_budget_seconds-(deadline-time.monotonic()),3)
        (args.report_dir/"report.json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8",newline="\n")
    print(json.dumps(report,indent=2))
    return 0 if report["status"]=="passed" else (3 if report["status"]=="inconclusive" else 1)

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute",action="store_true")
    p.add_argument("--disposable",action="store_true")
    p.add_argument("--sql-host",default="127.0.0.1")
    p.add_argument("--sql-port",type=int)
    p.add_argument("--sql-user")
    p.add_argument("--obclient",default="obclient")
    p.add_argument("--password-env",default="OB_TEST_PASSWORD")
    p.add_argument("--report-dir",type=Path)
    p.add_argument("--fixture-port",type=int,default=0)
    p.add_argument("--requests",type=int,default=8)
    p.add_argument("--delay-ms",type=int,default=300)
    p.add_argument("--statement-timeout",type=int,default=30)
    p.add_argument("--run-budget-seconds",type=int,default=180)
    a = p.parse_args(argv)
    try:
        loopback = ipaddress.ip_address(a.sql_host).is_loopback
    except ValueError:
        loopback = False
    if not loopback or a.fixture_port in FORBIDDEN_PORTS or not 0<=a.fixture_port<=65535:
        p.error("numeric loopback SQL host and a separate developer fixture port are required")
    if not 6<=a.requests<=32 or not 100<=a.delay_ms<=2000 or not 1<=a.statement_timeout<=120 or not 30<=a.run_budget_seconds<=480:
        p.error("request/delay/timeout budget outside developer bounds")
    if not a.execute:
        print(json.dumps({"classification":CLASSIFICATION,"execute":False,"sql":False,"http":False,
                          "requests_per_phase":a.requests,"control_cap":1024,"target_cap":3,
                          "fixture_delay_ms":a.delay_ms,"fixture_bind":"127.0.0.1",
                          "fixture_port":a.fixture_port,"run_budget_seconds":a.run_budget_seconds},indent=2))
        return 0
    if not a.disposable or a.sql_port is None or not 1<=a.sql_port<=65535 or not a.sql_user or a.report_dir is None:
        p.error("--execute requires --disposable, explicit --sql-port/--sql-user and a new --report-dir")
    if a.report_dir.exists():
        p.error("--report-dir must not already exist")
    return execute(a)

if __name__=="__main__":
    raise SystemExit(main())
