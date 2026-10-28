#!/usr/bin/env python3
"""Reproduce seekdb issue #1489.

Two semantically equivalent filters select the same rows, but the ANN
(APPROXIMATE) path is orders of magnitude slower for one of them.

    F_base = category = 1
    F_or1  = (category = 1) OR (age > 1000)      -- age <= 1000, so identical
    F_orN  = (category = 1) OR (age > 1000) ...  -- N copies of the same term

The exact path rewrites the OR into range scans, the ANN path does not.

Usage:
    python3 repro_ann_rewrite.py --port 2882 [--rows 20000] [--repeats 128]
"""

import argparse
import statistics
import sys
import time

import numpy as np
import pymysql


def balanced(op, expressions):
    """Build a balanced boolean tree, the way the issue reports do."""
    if len(expressions) == 1:
        return expressions[0]
    middle = len(expressions) // 2
    return "(%s) %s (%s)" % (
        balanced(op, expressions[:middle]),
        op,
        balanced(op, expressions[middle:]),
    )


def connect(port):
    return pymysql.connect(
        host="127.0.0.1",
        port=port,
        user="root",
        password="",
        autocommit=True,
        connect_timeout=30,
    )


def run(cur, sql):
    cur.execute(sql)
    return cur.fetchall()


def median_time(cur, sql, runs):
    """Median wall clock of `runs` executions, in milliseconds."""
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        cur.execute(sql)
        cur.fetchall()
        times.append((time.perf_counter() - start) * 1000.0)
    return statistics.median(times), min(times), max(times)


def explain(cur, sql):
    cur.execute("EXPLAIN " + sql)
    return "\n".join("\t".join("" if c is None else str(c) for c in row) for row in cur.fetchall())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=2882)
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--repeats", type=int, default=128)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--keep", action="store_true", help="do not drop the database first")
    parser.add_argument("--explain-only", action="store_true", help="skip the timing loop")
    args = parser.parse_args()

    conn = connect(args.port)
    cur = conn.cursor()

    if not args.keep:
        cur.execute("DROP DATABASE IF EXISTS ann_rewrite_repro")
        cur.execute("CREATE DATABASE ann_rewrite_repro")
    cur.execute("USE ann_rewrite_repro")

    dim = args.dim
    rng = np.random.default_rng(20261002)

    print("== setup ==")
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
        """ % dim
    )

    rows = args.rows
    vectors = rng.normal(size=(rows, dim))
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    tenant = rng.integers(0, 100, size=rows)
    category = tenant + 1
    age = rng.integers(1, 1001, size=rows)

    batch = []
    for i in range(rows):
        vec = ",".join("%.6f" % v for v in vectors[i])
        batch.append((i, "[%s]" % vec, int(tenant[i]), int(category[i]), int(age[i])))
        if len(batch) >= 500:
            cur.executemany(
                "INSERT INTO t (id, embedding, tenant, category, age) VALUES (%s,%s,%s,%s,%s)",
                batch,
            )
            batch = []
    if batch:
        cur.executemany(
            "INSERT INTO t (id, embedding, tenant, category, age) VALUES (%s,%s,%s,%s,%s)",
            batch,
        )
    cur.execute("ANALYZE TABLE t")
    print("   inserted %d rows, analyze done" % rows)

    cur.execute(
        """
        CREATE VECTOR INDEX t_hnsw ON t(embedding)
        WITH (distance=l2, type=hnsw, lib=vsag, m=16,
              ef_construction=128, sync_mode=immediate)
        """
    )
    print("   vector index created")

    query_vector = rng.normal(size=dim)
    query_vector /= np.linalg.norm(query_vector)
    qvec = "[%s]" % ",".join("%.6f" % v for v in query_vector)

    filters = {
        "base  (category = 1)": "category = 1",
        "or1   (category = 1 OR age > 1000)": "(category = 1) OR (age > 1000)",
        "or%d   (category = 1 OR age > 1000 ...)" % args.repeats: balanced(
            "OR", ["(category = 1)"] + ["(age > 1000)"] * args.repeats
        ),
    }

    print()
    print("== eligibility check (exact path) ==")
    reference = None
    for name, flt in filters.items():
        rows_out = run(cur, "SELECT id FROM t WHERE %s ORDER BY id" % flt)
        ids = set(r[0] for r in rows_out)
        if reference is None:
            reference = ids
            print("   %-45s -> %d rows (reference)" % (name, len(ids)))
        else:
            print(
                "   %-45s -> %d rows, identical=%s"
                % (name, len(ids), ids == reference)
            )

    print()
    print("== plans ==")
    for name, flt in filters.items():
        print()
        print("--- %s" % name)
        exact_sql = "SELECT id FROM t WHERE %s ORDER BY id" % flt
        ann_sql = (
            "SELECT id FROM t WHERE %s ORDER BY l2_distance(embedding, '%s') APPROXIMATE LIMIT 10"
            % (flt, qvec)
        )
        print("   [exact]")
        for line in explain(cur, exact_sql).splitlines():
            print("     " + line)
        print("   [ann]")
        for line in explain(cur, ann_sql).splitlines():
            print("     " + line)

    if args.explain_only:
        cur.close()
        conn.close()
        return 0

    print()
    print("== latency (median / min / max ms) ==")
    for name, flt in filters.items():
        exact_sql = "SELECT id FROM t WHERE %s ORDER BY id" % flt
        ann_sql = (
            "SELECT id FROM t WHERE %s ORDER BY l2_distance(embedding, '%s') APPROXIMATE LIMIT 10"
            % (flt, qvec)
        )
        # warmup
        median_time(cur, exact_sql, 1)
        median_time(cur, ann_sql, 2)

        e = median_time(cur, exact_sql, args.runs)
        a = median_time(cur, ann_sql, args.runs)
        print("   %-45s exact %9.3f / %9.3f / %9.3f   ann %9.3f / %9.3f / %9.3f"
              % (name, e[0], e[1], e[2], a[0], a[1], a[2]))

    print()
    print("== NO_REWRITE control on the exact path ==")
    flt = filters["or%d   (category = 1 OR age > 1000 ...)" % args.repeats]
    for hint in ("", "/*+ NO_REWRITE */ "):
        sql = "SELECT id FROM t WHERE %s%s ORDER BY id" % (hint, flt)
        median_time(cur, sql, 1)
        t = median_time(cur, sql, args.runs)
        print("   hint=%-20s exact median %9.3f ms (min %9.3f)" % (repr(hint), t[0], t[1]))

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
