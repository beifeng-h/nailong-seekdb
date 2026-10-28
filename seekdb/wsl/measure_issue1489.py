#!/usr/bin/env python3
"""Measure the ANN latency of equivalent filters, as issue #1489 reports.

The equivalent filter of the issue is

    category = 1 OR (age > 1000) OR (age > 1000) ...

where `age` never exceeds 1000, so the extra terms are always false and the
filter selects the same rows as `category = 1` alone.

The exact path is only used to prove the equivalence with a term count that
the server still survives; the interesting measurement is the ANN path.

Usage:
    python3 measure_issue1489.py --port 2884 [--rows 20000] [--repeats 128]
"""

import argparse
import statistics
import sys
import time

import numpy as np
import pymysql


def connect(port):
    return pymysql.connect(
        host="127.0.0.1",
        port=port,
        user="root",
        password="",
        autocommit=True,
        connect_timeout=30,
    )


def or_filter(repeats):
    terms = ["(category = 1)"] + ["(age > 1000)"] * repeats
    return " OR ".join(terms)


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
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--repeats", type=int, default=128)
    parser.add_argument("--runs", type=int, default=9)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    conn = connect(args.port)
    cur = conn.cursor()

    if not args.keep:
        cur.execute("DROP DATABASE IF EXISTS ann_issue1489")
        cur.execute("CREATE DATABASE ann_issue1489")
    cur.execute("USE ann_issue1489")

    rng = np.random.default_rng(20261002)
    dim = args.dim
    rows = args.rows

    cur.execute("DROP TABLE IF EXISTS t")
    cur.execute(
        """
        CREATE TABLE t (
          id BIGINT NOT NULL PRIMARY KEY,
          embedding VECTOR(%d) NOT NULL,
          tenant INT NOT NULL,
          category INT NOT NULL,
          age INT NOT NULL,
          KEY idx_category (category),
          KEY idx_age (age)
        ) ORGANIZATION HEAP
        """
        % dim
    )
    vectors = rng.normal(size=(rows, dim))
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    tenant = rng.integers(0, 100, size=rows)
    age = rng.integers(1, 1001, size=rows)
    batch = []
    for i in range(rows):
        vec = ",".join("%.6f" % v for v in vectors[i])
        batch.append((i, "[%s]" % vec, int(tenant[i]), int(tenant[i]) + 1,
                      int(age[i])))
        if len(batch) >= 500:
            cur.executemany(
                "INSERT INTO t (id, embedding, tenant, category, age) "
                "VALUES (%s,%s,%s,%s,%s)",
                batch,
            )
            batch = []
    if batch:
        cur.executemany(
            "INSERT INTO t (id, embedding, tenant, category, age) "
            "VALUES (%s,%s,%s,%s,%s)",
            batch,
        )
    cur.execute("ANALYZE TABLE t")
    cur.execute(
        """
        CREATE VECTOR INDEX t_hnsw ON t(embedding)
        WITH (distance=l2, type=hnsw, lib=vsag, m=16,
              ef_construction=128, sync_mode=immediate)
        """
    )
    print("table ready: %d rows, %d OR terms" % (rows, args.repeats + 1))

    query_vector = rng.normal(size=dim)
    query_vector /= np.linalg.norm(query_vector)
    qvec = "[%s]" % ",".join("%.6f" % v for v in query_vector)

    base = "category = 1"
    equivalent = or_filter(args.repeats)

    small = or_filter(32)
    cur.execute("SELECT id FROM t WHERE %s ORDER BY id" % base)
    ref = set(r[0] for r in cur.fetchall())
    cur.execute("SELECT id FROM t WHERE %s ORDER BY id" % small)
    got = set(r[0] for r in cur.fetchall())
    print("exact path: base=%d rows, or33=%d rows, identical=%s"
          % (len(ref), len(got), ref == got))

    def ann_sql(flt):
        return (
            "SELECT id FROM t WHERE %s "
            "ORDER BY l2_distance(embedding, '%s') APPROXIMATE LIMIT 10"
            % (flt, qvec)
        )

    cur.execute("SET ob_hnsw_ef_search = 64")
    cur.execute("ALTER SYSTEM FLUSH PLAN CACHE")

    timings = {}
    for label, flt in (("base", base), ("equivalent", equivalent)):
        sql = ann_sql(flt)
        median_ms(cur, sql, 2)
        timings[label] = median_ms(cur, sql, args.runs)
    print()
    print("%-12s %12s %14s %10s"
          % ("filter", "base ms", "equivalent ms", "ratio"))
    print("%-12s %12.3f %14.3f %9.2fx"
          % ("ann", timings["base"], timings["equivalent"],
             timings["equivalent"] / timings["base"]))

    print()
    print("plans:")
    for label, flt in (("base", base), ("equivalent", equivalent)):
        cur.execute("EXPLAIN " + ann_sql(flt))
        print("  --- %s" % label)
        for row in cur.fetchall():
            print("    " + "\t".join("" if c is None else str(c) for c in row))

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

