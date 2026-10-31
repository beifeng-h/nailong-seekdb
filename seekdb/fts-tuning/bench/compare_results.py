#!/usr/bin/env python3
"""Compare two benchmark result files and print the deltas."""

import json
import sys


METRICS = [
    ("build", "index_build_s", "index build", "s", -1),
    ("build", "rows_per_s", "index build", "rows/s", 1),
    ("load_fts", "load_s", "insert with index", "s", -1),
    ("load_fts", "rows_per_s", "insert with index", "rows/s", 1),
    ("query", "p50_ms", "query", "ms", -1),
    ("query", "p90_ms", "query", "ms", -1),
    ("query", "p99_ms", "query", "ms", -1),
    ("query", "mean_ms", "query", "ms", -1),
    ("query", "qps", "query", "q/s", 1),
    ("query", "total_s", "query", "s", -1),
    ("query_topk", "p50_ms", "query top-10", "ms", -1),
    ("query_topk", "p99_ms", "query top-10", "ms", -1),
    ("query_topk", "mean_ms", "query top-10", "ms", -1),
    ("query_topk", "qps", "query top-10", "q/s", 1),
    ("query_topk", "total_s", "query top-10", "s", -1),
]


def main():
    if len(sys.argv) != 3:
        print("usage: compare_results.py <baseline.json> <optimized.json>")
        return 2
    with open(sys.argv[1]) as fh:
        base = json.load(fh)["results"]
    with open(sys.argv[2]) as fh:
        opt = json.load(fh)["results"]

    print("%-22s %-10s %14s %14s %10s" %
          ("phase", "metric", "baseline", "optimized", "change"))
    print("-" * 74)
    for section, key, phase, unit, better in METRICS:
        a = base.get(section, {}).get(key)
        b = opt.get(section, {}).get(key)
        if a is None or b is None:
            continue
        if a == 0:
            change = "n/a"
        else:
            ratio = (b - a) / a * 100.0
            change = "%+.1f%%" % ratio
            if better > 0 and ratio < -0.5:
                change = "%+.1f%% (faster)" % ratio
            elif better < 0 and ratio > 0.5:
                change = "%+.1f%% (faster)" % ratio
        print("%-22s %-10s %14s %14s %10s" %
              (phase, unit, a, b, change))
    print("-" * 74)
    for name in ("matched_rows_total",):
        a = base.get("query", {}).get(name)
        b = opt.get("query", {}).get(name)
        if a is not None and b is not None:
            same = "identical" if a == b else "DIFFERENT (%d vs %d)" % (a, b)
            print("matched rows returned: %s" % same)
    return 0


if __name__ == "__main__":
    sys.exit(main())
