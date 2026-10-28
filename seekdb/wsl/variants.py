#!/usr/bin/env python3
"""Compare the ANN plan and latency of filters with a growing number of OR
terms, to see where the vector index scan loses the index range.

Usage:
    python3 variants.py --port 2885 [--rows 20000]
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
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()

    conn = pymysql.connect(host="127.0.0.1", port=args.port, user="root",
                           password="", autocommit=True, connect_timeout=30)
    cur = conn.cursor()
    cur.execute("USE ann_issue1489")

    rng = np.random.default_rng(20261003)
    dim = args.dim
    query_vector = rng.normal(size=dim)
    query_vector /= np.linalg.norm(query_vector)
    qvec = "[%s]" % ",".join("%.6f" % v for v in query_vector)

    variants = [
        ("base", "category = 1"),
        ("or2", "category = 1 OR age > 1000"),
        ("or3", "category = 1 OR age > 1000 OR age > 2000"),
        ("or8", "category = 1 OR " + " OR ".join(["age > 1000"] * 7)),
        ("or33", "category = 1 OR " + " OR ".join(["age > 1000"] * 32)),
    ]

    cur.execute("SET ob_hnsw_ef_search = 64")
    for label, flt in variants:
        sql = ("SELECT id FROM t WHERE %s "
               "ORDER BY l2_distance(embedding, '%s') APPROXIMATE LIMIT 10"
               % (flt, qvec))
        median_ms(cur, sql, 2)
        ms = median_ms(cur, sql, args.runs)
        cur.execute("EXPLAIN " + sql)
        rows = cur.fetchall()
        operator = rows[0][1] if rows else ""
        name = rows[0][2] if rows else ""
        access = ""
        for row in cur.fetchall():
            pass
        print("%-6s %9.3f ms  op=%-42s name=%s"
              % (label, ms, operator, name))
        cur.execute("EXPLAIN " + sql)
        detail = cur.fetchall()
        for row in detail:
            for col in row:
                text = "" if col is None else str(col)
                if "range_key" in text or "range(" in text:
                    print("        %s" % text.strip()[:160])

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

