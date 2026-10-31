#!/usr/bin/env python3
"""Compare the tokenisation of a whole corpus between two instances.

Fetches the token stream of every row in chunks and compares a rolling
digest, so the check covers the complete corpus rather than a sample.
"""

import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mycli import Mysql


CHUNK = 2000


def digest_corpus(conn, table, limit):
    overall = hashlib.md5()
    row_count = 0
    for start in range(1, limit + 1, CHUNK):
        end = min(start + CHUNK - 1, limit)
        rows = conn.query("select id, tokenize(body, 'space') from %s "
                          "where id between %d and %d order by id"
                          % (table, start, end))
        for r in rows:
            overall.update(r[0])
            overall.update(r[1] or b"")
            overall.update(b"\x01")
            row_count += 1
    return overall.hexdigest(), row_count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="")
    ap.add_argument("--db", default="fts_bench")
    ap.add_argument("--table", default="bench_main")
    ap.add_argument("--rows", type=int, default=100000)
    ap.add_argument("--ports", type=int, nargs="+", required=True)
    ap.add_argument("--labels", nargs="+", required=True)
    args = ap.parse_args()

    results = {}
    for port, label in zip(args.ports, args.labels):
        conn = Mysql(args.host, port, args.user, args.password,
                     db=args.db, timeout=1800).connect()
        digest, count = digest_corpus(conn, args.table, args.rows)
        results[label] = (digest, count)
        print("%-16s rows=%d token_digest=%s" % (label, count, digest))
        conn.close()

    labels = list(results.keys())
    same = all(results[l] == results[labels[0]] for l in labels)
    print("tokenisation identical across all instances: %s" % same)
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
