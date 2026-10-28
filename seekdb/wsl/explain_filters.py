#!/usr/bin/env python3
"""Show the plan of the exact and the approximate path for the equivalent
filters of issue #1489.

Usage:
    python3 explain_filters.py --port 2884 [--repeats 128] [--rows 20000]
"""

import argparse
import sys

import numpy as np
import pymysql


def or_filter(repeats):
    terms = ["(category = 1)"] + ["(age > 1000)"] * repeats
    return " OR ".join(terms)


def show(cur, title, sql):
    cur.execute("EXPLAIN " + sql)
    print("--- %s" % title)
    for row in cur.fetchall():
        print("    " + "\t".join("" if c is None else str(c) for c in row))
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--repeats", type=int, default=128)
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

    base = "category = 1"
    equivalent = or_filter(args.repeats)

    print("=== EXACT path ===")
    show(cur, "base", "SELECT id FROM t WHERE %s ORDER BY id" % base)
    show(cur, "equivalent", "SELECT id FROM t WHERE %s ORDER BY id" % equivalent)

    print("=== ANN path ===")
    show(cur, "base",
         "SELECT id FROM t WHERE %s "
         "ORDER BY l2_distance(embedding, '%s') APPROXIMATE LIMIT 10"
         % (base, qvec))
    show(cur, "equivalent",
         "SELECT id FROM t WHERE %s "
         "ORDER BY l2_distance(embedding, '%s') APPROXIMATE LIMIT 10"
         % (equivalent, qvec))

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

