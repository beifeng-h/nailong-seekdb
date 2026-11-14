"""Compare production stdio processes; these diagnostics are not contest scores."""

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from stdio_protocol_test import Client

BASELINE_REVISION = "7d07cdb7d148678f7a2a2db284da3b484575dbb9"


def identity(path):
    content = path.read_bytes()
    return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def process_metrics(process):
    if platform.system() != "Linux":
        return None
    root = Path("/proc") / str(process.pid)
    io = {key: int(value) for key, value in
          (line.split(":", 1) for line in (root / "io").read_text().splitlines())}
    fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
    ticks = os.sysconf("SC_CLK_TCK")
    status = {key: value.strip() for key, value in
              (line.split(":", 1) for line in (root / "status").read_text().splitlines())}
    return {"io": io, "user_seconds": int(fields[11]) / ticks,
            "kernel_seconds": int(fields[12]) / ticks,
            "rss_bytes": int(status["VmRSS"].split()[0]) * 1024,
            "lifetime_peak_rss_bytes": int(status["VmHWM"].split()[0]) * 1024}


def save(report, path):
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def check_response(response, expected):
    assert response.get("ok") is True, response
    if "rows" not in expected:
        assert response["affected_rows"] == expected["affected_rows"], response
        return
    assert response["columns"] == expected["columns"], response
    assert len(response["rows"]) == len(expected["rows"]), response
    for actual, wanted in zip(response["rows"], expected["rows"]):
        assert len(actual) == len(wanted), (actual, wanted)
        for value, truth in zip(actual, wanted):
            if isinstance(truth, float):
                assert math.isclose(value, truth, rel_tol=1e-12, abs_tol=1e-8), (actual, wanted)
            else:
                assert type(value) is type(truth) and value == truth, (actual, wanted)


def paired_requests(clients, statements, label, mode, round_index, report, report_path):
    names = list(clients)
    samples = {name: [] for name in names}
    captures = {name: hashlib.sha256() for name in names}
    request_digest = hashlib.sha256()
    before = {name: process_metrics(client.process) for name, client in clients.items()}
    for index, (sql, expected) in enumerate(statements):
        request = json.dumps({"id": index, "op": "execute", "sql": sql,
                              "search_mode": mode}, separators=(",", ":")).encode("utf-8")
        request_digest.update(request + b"\n")
        responses = []
        for name in (names if (index + round_index) % 2 == 0 else names[::-1]):
            started = time.perf_counter()
            response = clients[name].raw_request(request, index)
            samples[name].append(time.perf_counter() - started)
            check_response(response, expected)
            captures[name].update(json.dumps(response, sort_keys=True, allow_nan=False).encode() + b"\n")
            responses.append(response)
        assert responses[0] == responses[1], responses
    for name, client in clients.items():
        after = process_metrics(client.process)
        record = {"program": name, "group": label, "round": round_index,
                  "search_mode": mode, "requests": len(statements),
                  "seconds": sum(samples[name]), "samples": samples[name],
                  "requests_sha256": request_digest.hexdigest(),
                  "responses_sha256": captures[name].hexdigest(),
                  "metrics_before": before[name], "metrics_after": after}
        if after is not None:
            record["metrics_delta"] = {
                "user_seconds": after["user_seconds"] - before[name]["user_seconds"],
                "kernel_seconds": after["kernel_seconds"] - before[name]["kernel_seconds"],
                "io": {key: value - before[name]["io"][key] for key, value in after["io"].items()}}
        report["runs"].append(record)
        print(f"PASS {label} {round_index} {name} {record['seconds']:.6f}s", flush=True)
    save(report, report_path)


def durable_pair(directories):
    stores = {name: identity(directory / "pages.bin") for name, directory in directories.items()}
    assert stores["baseline"] == stores["candidate"], stores
    for directory in directories.values():
        assert (directory / "redo.wal").stat().st_size == 0
        assert sorted(path.name for path in directory.iterdir()) == ["pages.bin", "redo.wal"]
    return stores["baseline"]


def vector_literal(units):
    return "'[" + ",".join(format(value / 4, ".2f") for value in units) + "]'"


def make_fixture(rows, queries, points):
    units = [[(residue * (dimension + 3) + dimension * dimension) % 1009 - 504
              for dimension in range(128)] for residue in range(1009)]
    vectors = [vector_literal(vector) for vector in units]
    writes = [("CREATE TABLE docs(id BIGINT PRIMARY KEY, tag VARCHAR(8), v VECTOR(128));",
               {"affected_rows": 0})]
    for first in range(0, rows, 100):
        keys = range(first, min(first + 100, rows))
        records = [f"({key},'{key % 7}',{vectors[key % 1009]})" for key in keys]
        writes.append(("INSERT INTO docs VALUES" + ",".join(records) + ";",
                       {"affected_rows": len(records)}))
    groups = {"unfiltered": [], "filtered": [], "point": []}
    for number in range(queries):
        query = (37 * number + 17) % 1009
        squared = [sum((left - right) ** 2 for left, right in zip(vector, units[query]))
                   for vector in units]
        for group in ("unfiltered", "filtered"):
            filtered = group == "filtered"
            keys = range(3, rows, 7) if filtered else range(rows)
            chosen = sorted(keys, key=lambda key: (squared[key % 1009], key))[:10]
            predicate = " WHERE tag = '3'" if filtered else ""
            sql = ("SELECT id, L2_DISTANCE(v," + vectors[query] + ") AS distance FROM docs" +
                   predicate + " ORDER BY distance LIMIT 10;")
            groups[group].append((sql, {"columns": ["id", "distance"],
                "rows": [[key, math.sqrt(squared[key % 1009]) / 4] for key in chosen]}))
    for number in range(points):
        key = number % rows
        groups["point"].append((f"SELECT id,tag FROM docs WHERE id = {key};",
                                 {"columns": ["id", "tag"], "rows": [[key, str(key % 7)]]}))
    return writes, groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--candidate-revision", default="uncommitted")
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--queries", type=int, default=40)
    parser.add_argument("--point-queries", type=int, default=500)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if min(args.rows, args.queries, args.point_queries, args.rounds) < 1:
        parser.error("Row, query and round counts must be positive")
    programs = {"baseline": args.baseline.resolve(strict=True),
                "candidate": args.candidate.resolve(strict=True)}
    report_path = args.report.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {"scope": "Paired production stdio on deterministic synthetic data, retained OS cache",
              "official_score_measured": False, "official_container_T_B_M_measured": False,
              "metrics_scope": "Linux /proc process counters, including pipes and cache; VmHWM is lifetime RSS",
              "process_metrics_available": platform.system() == "Linux",
              "baseline_revision": BASELINE_REVISION, "candidate_revision": args.candidate_revision,
              "platform": platform.platform(), "rows": args.rows, "dimensions": 128,
              "queries_per_topk_group": args.queries, "point_queries": args.point_queries,
              "rounds": args.rounds, "harness": identity(Path(__file__)),
              "programs": {name: {"path": str(path), **identity(path)} for name, path in programs.items()},
              "started_at_utc": datetime.now(timezone.utc).isoformat(), "runs": [], "passed": False}
    save(report, report_path)
    clients = {}
    try:
        writes, groups = make_fixture(args.rows, args.queries, args.point_queries)
        with tempfile.TemporaryDirectory(prefix="minivectordb-paired-") as temporary:
            directories = {name: Path(temporary) / name for name in programs}
            try:
                for name in programs:
                    clients[name] = Client(programs[name], directories[name])
                paired_requests(clients, writes, "durable_insert", "exact", 0, report, report_path)
                for client in clients.values():
                    client.finish()
                report["database_without_index"] = durable_pair(directories)
                save(report, report_path)
            finally:
                for client in clients.values():
                    client.abort()
                clients.clear()
            for mode in ("exact", "ann"):
                try:
                    for name in programs:
                        clients[name] = Client(programs[name], directories[name])
                    boundary = sorted({key for key in (0, 99, 100, 1008, 1009, args.rows - 1)
                                       if key < args.rows})
                    for client in clients.values():
                        for key in boundary:
                            client.execute(f"SELECT id,tag FROM docs WHERE id = {key};",
                                           {"rows": [[key, str(key % 7)]]})
                    if mode == "ann":
                        paired_requests(clients, [("CREATE VECTOR INDEX docs_vec ON docs(v);",
                                        {"affected_rows": 0})], "create_index", "exact", 0, report, report_path)
                        for client in clients.values():
                            client.finish()
                        report["database_with_index"] = durable_pair(directories)
                        for client in clients.values():
                            client.abort()
                        clients.clear()
                        for name in programs:
                            clients[name] = Client(programs[name], directories[name])
                    for round_index in range(args.rounds):
                        for group, statements in groups.items():
                            paired_requests(clients, statements, mode + "_" + group, mode,
                                            round_index, report, report_path)
                    for client in clients.values():
                        client.finish()
                    expected_store = report["database_with_index" if mode == "ann" else "database_without_index"]
                    assert durable_pair(directories) == expected_store, "Read-only queries changed durable pages"
                finally:
                    for client in clients.values():
                        client.abort()
                    clients.clear()
        report["summary"] = {}
        for group in sorted({run["group"] for run in report["runs"]}):
            times = {name: statistics.median(run["seconds"] for run in report["runs"]
                                            if run["program"] == name and run["group"] == group)
                     for name in programs}
            report["summary"][group] = {**times, "speedup": times["baseline"] / times["candidate"]}
        report["passed"] = True
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save(report, report_path)
        print(json.dumps(report["summary"], indent=2), flush=True)
    except BaseException as error:
        report["failure"] = str(error)
        save(report, report_path)
        raise


if __name__ == "__main__":
    main()
