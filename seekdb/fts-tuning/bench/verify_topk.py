#!/usr/bin/env python3
"""Check whether a top-k MATCH result difference is a tie breaking artefact.

For every query the relevance scores of the returned rows are compared across
the instances.  Equal scores are ties: which of the tied rows survives the
LIMIT depends on the physical layout of the instance, so only the sequence of
scores is a semantic property.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mycli import Mysql


def fetch(conn, sql):
    rows = conn.query(sql)
    if not rows:
        return []
    out = []
    for r in rows:
        out.append((r[0].decode() if isinstance(r[0], bytes) else r[0],
                    round(float(r[1]), 9)))
    return out


def close_enough(a, b, tolerance=1e-4):
    """Relative comparison of two relevance score sequences.

    The BM25 relevance depends on the estimated number of documents in the
    retrieval domain.  That estimate is derived from per instance statistics
    and differs slightly between two instances that hold the very same rows,
    so the scores are compared with a relative tolerance.
    """
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if abs(x - y) > tolerance * max(1.0, abs(x)):
            return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="")
    ap.add_argument("--db", default="fts_bench")
    ap.add_argument("--ports", type=int, nargs="+", required=True)
    ap.add_argument("--labels", nargs="+", required=True)
    args = ap.parse_args()

    conns = {}
    for port, label in zip(args.ports, args.labels):
        conns[label] = Mysql(args.host, port, args.user, args.password,
                             db=args.db, timeout=600).connect()

    terms = ["performance", "index", "database", "query", "search", "token",
             "storage", "table", "vector", "memory", "oceanbase", "checkpoint",
             "well", "cluster", "optimized"]
    modes = ["", " in boolean mode"]

    score_mismatch = 0
    id_mismatch = 0
    checked = 0
    for term in terms:
        for mode in modes:
            if mode:
                sql = ("select id, match(body) against('%s' in boolean mode) as s "
                       "from bench_main where match(body) against('%s' in boolean mode) "
                       "order by s desc limit 10" % (term, term))
            else:
                sql = ("select id, match(body) against('%s') as s from bench_main "
                       "where match(body) against('%s') order by s desc limit 10"
                       % (term, term))
            results = {}
            for label, conn in conns.items():
                results[label] = fetch(conn, sql)
            checked += 1
            labels = list(conns.keys())
            base_label = labels[0]
            base = results[base_label]
            for label in labels[1:]:
                other = results[label]
                if not close_enough([s for _, s in base], [s for _, s in other]):
                    score_mismatch += 1
                    print("SCORE MISMATCH %s vs %s: %s" % (base_label, label, sql))
                    print("   %s" % [s for _, s in base])
                    print("   %s" % [s for _, s in other])
                    max_dev = 0.0
                    for x, y in zip([s for _, s in base], [s for _, s in other]):
                        if x != y:
                            max_dev = max(max_dev, abs(x - y) / max(1.0, abs(x)))
                    print("   max relative deviation: %.3g" % max_dev)
                if [i for i, _ in base] != [i for i, _ in other]:
                    tied = False
                    for idx in range(min(len(base), len(other))):
                        if base[idx][1] == other[idx][1]:
                            tied = True
                    id_mismatch += 1
                    print("id set/order differs (%s vs %s), equal score at a "
                          "differing position: %s" % (base_label, label, tied))

    for conn in conns.values():
        conn.close()

    print("checked=%d score_mismatches=%d id_order_differences=%d"
          % (checked, score_mismatch, id_mismatch))
    return 0 if score_mismatch == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
