"""Compare two SqlSession capture drivers using one synthetic database."""

import argparse
import hashlib
import json
import math
import platform
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

from public_samples import compare


def identity(path):
    data = path.read_bytes()
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def run(driver, directory, sql, capture):
    started = time.perf_counter()
    process = subprocess.run([str(driver), str(directory), str(sql), str(capture)],
                             capture_output=True, timeout=600)
    elapsed = time.perf_counter() - started
    if process.returncode:
        raise RuntimeError(process.stderr.decode("utf-8"))
    actual = json.loads(capture.read_text(encoding="utf-8"))
    if any("error" in result for result in actual):
        raise RuntimeError("SQL capture contains an execution error")
    return actual, elapsed


def vector_literal(units):
    return "'[" + ",".join(format(value / 4, ".2f") for value in units) + "]'"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    programs = {"baseline": args.baseline.resolve(strict=True), "candidate": args.candidate.resolve(strict=True)}
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report_path = args.report.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    row_count, dimensions, queries, topk = 10000, 128, 100, 10
    units = [[(residue * (dimension + 3) + dimension * dimension) % 1009 - 504
              for dimension in range(dimensions)] for residue in range(1009)]
    report = {"scope": "Synthetic exact Top10 development comparison through SqlSession file capture",
              "platform": platform.platform(), "rows": row_count, "dimensions": dimensions,
              "queries_per_group": queries, "topk": topk, "official_process_protocol_tested": False,
              "official_performance_score_measured": False, "physical_device_io_measured": False,
              "timing": "External wall time includes startup, SQL parsing, execution and file output",
              "cache_state": "No operating-system cache reset, balanced baseline-candidate-candidate-baseline order",
              "programs": {name: dict(path=str(path), **identity(path)) for name, path in programs.items()},
              "script": identity(Path(__file__)), "runs": [], "passed": False}

    def save():
        report_path.write_bytes(json.dumps(report, indent=2).encode("utf-8") + b"\n")

    save()
    setup = output / "setup.test"
    with setup.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("CREATE TABLE docs(id BIGINT PRIMARY KEY, tag VARCHAR(8), v VECTOR(128));\n")
        for start in range(0, row_count, 100):
            records = ["(" + str(key) + ", '" + str(key % 7) + "', " + vector_literal(units[key % 1009]) + ")"
                       for key in range(start, start + 100)]
            stream.write("INSERT INTO docs VALUES " + ",".join(records) + ";\n")

    expected = {"unfiltered": [], "filtered": []}
    statements = {group: [] for group in expected}
    for query in range(queries):
        key = (37 * query + 17) % 1009
        squared = [sum((left - right) ** 2 for left, right in zip(vector, units[key])) for vector in units]
        for group in expected:
            filtered = group == "filtered"
            candidates = range(3, row_count, 7) if filtered else range(row_count)
            chosen = sorted(candidates, key=lambda row: (squared[row % 1009], row))[:topk]
            expected[group].append({"columns": ["id", "distance"],
                                    "rows": [[row, math.sqrt(squared[row % 1009]) / 4] for row in chosen]})
            predicate = " WHERE tag = '3'" if filtered else ""
            statements[group].append("SELECT id, L2_DISTANCE(v, " + vector_literal(units[key]) +
                                     ") AS distance FROM docs" + predicate + " ORDER BY distance LIMIT 10;")
    for group in expected:
        (output / (group + ".test")).write_bytes(("\n".join(statements[group]) + "\n").encode("utf-8"))
        (output / (group + "-expected.json")).write_bytes(json.dumps(expected[group]).encode("utf-8") + b"\n")
    report["inputs"] = {path.name: identity(path) for path in sorted(output.iterdir()) if path.is_file()}
    save()

    with tempfile.TemporaryDirectory(prefix="owned-data-", dir=output) as temporary:
        directory = Path(temporary).resolve()
        if not directory.is_relative_to(output):
            raise RuntimeError("Comparison data outside owned output directory")
        actual, elapsed = run(programs["baseline"], directory, setup, output / "setup.json")
        if len(actual) != 101 or any(result.get("affected_rows") != 100 for result in actual[1:]):
            raise RuntimeError("Synthetic fixture creation differs")
        report["setup_seconds"] = elapsed
        original_data = identity(directory / "pages.bin")
        report["database"] = original_data
        save()
        for order, variant in enumerate(["baseline", "candidate", "candidate", "baseline"]):
            for group in expected:
                capture = output / (str(order) + "-" + variant + "-" + group + ".json")
                actual, elapsed = run(programs[variant], directory, output / (group + ".test"), capture)
                failures = []
                if len(actual) != queries:
                    failures.append({"reason": "Query count differs", "actual": len(actual)})
                for index, (wanted, observed) in enumerate(zip(expected[group], actual)):
                    errors = compare(wanted, observed)
                    if errors:
                        failures.append({"query": index, "errors": errors})
                record = {"order": order, "variant": variant, "group": group, "wall_seconds": elapsed,
                          "queries": queries, "rows_checked": queries * topk, "passed": not failures,
                          "failures": failures, "capture": identity(capture)}
                report["runs"].append(record)
                save()
                print(("PASS " if record["passed"] else "FAIL ") + variant + " " + group +
                      " seconds=" + format(elapsed, ".6f"), flush=True)
                if failures:
                    raise RuntimeError("Query results differ from independent integer-distance truth")
        if identity(directory / "pages.bin") != original_data:
            raise RuntimeError("Query runs modified the frozen database")
    report["summary"] = {}
    for group in expected:
        averages = {variant: statistics.mean(record["wall_seconds"] for record in report["runs"]
                                             if record["variant"] == variant and record["group"] == group)
                    for variant in programs}
        report["summary"][group] = {"baseline_mean_seconds": averages["baseline"],
                                    "candidate_mean_seconds": averages["candidate"],
                                    "speedup": averages["baseline"] / averages["candidate"],
                                    "candidate_elapsed_ratio": averages["candidate"] / averages["baseline"]}
    report["temporary_database_cleaned"] = True
    report["passed"] = True
    save()
    print(json.dumps(report["summary"]), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
