#!/usr/bin/env python3
"""Compare the plan and the latency of two filters over the same table.

Usage:
    python3 compare_filters.py --port 2885 --filter-a "age > 10" \
        --filter-b "((age + 0) + 0) > 10"
"""

import argparse
import statistics
import sys
import time

import numpy as np
import pymysql


def median_ms(cur, sql, runs):
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        cur.execute(sql)
        cur.fetchall()
        times.append((time.perf_counter() - start) * 1000.0)
    return statistics.median(times)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--db", default="ann_issue1489")
    parser.add_argument("--table", default="t")
    parser.add_argument("--filter-a", required=True)
    parser.add_argument("--filter-b", required=True)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--exact", action="store_true")
    args = parser.parse_args()

    conn = pymysql.connect(host="127.0.0.1", port=args.port, user="root",
                           password="", autocommit=True, connect_timeout=30)
    cur = conn.cursor()
    cur.execute("USE %s" % args.db)

    rng = np.random.default_rng(20261003)
    dim = args.dim
    query_vector = rng.normal(size=dim)
    query_vector /= np.linalg.norm(query_vector)
    qvec = "[%s]" % ",".join("%.6f" % v for v in query_vector)

    for label, flt in (("A", args.filter_a), ("B", args.filter_b)):
        if args.exact:
            sql = ("SELECT id FROM %s WHERE %s ORDER BY id"
                   % (args.table, flt))
        else:
            sql = ("SELECT id FROM %s WHERE %s "
                   "ORDER BY l2_distance(embedding, '%s') APPROXIMATE LIMIT 10"
                   % (args.table, flt, qvec))

        cur.execute("SELECT COUNT(*) FROM %s WHERE %s" % (args.table, flt))
        eligible = cur.fetchone()[0]

        median_ms(cur, sql, 2)
        ms = median_ms(cur, sql, args.runs)
        print("=== %s: %s" % (label, flt))
        print("    eligible rows: %d, latency p50: %.3f ms" % (eligible, ms))
        cur.execute("EXPLAIN " + sql)
        for row in cur.fetchall():
            cells = ["" if c is None else str(c) for c in row]
            print("    | " + " | ".join(cells[:5]))
        print()

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

