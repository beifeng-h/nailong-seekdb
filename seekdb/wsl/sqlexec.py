#!/usr/bin/env python3
"""Run SQL statements against a seekdb instance.

    python3 sqlexec.py --port 2882 --file queries.sql
    python3 sqlexec.py --port 2882 --sql "select 1"
"""

import argparse
import sys

import pymysql


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=2882)
    parser.add_argument("--db", default=None)
    parser.add_argument("--sql", default=None)
    parser.add_argument("--file", default=None)
    args = parser.parse_args()

    conn = pymysql.connect(
        host="127.0.0.1",
        port=args.port,
        user="root",
        password="",
        autocommit=True,
        connect_timeout=30,
    )
    cur = conn.cursor()
    if args.db:
        cur.execute("USE " + args.db)

    statements = []
    if args.sql:
        statements.append(args.sql)
    if args.file:
        with open(args.file, "r", encoding="utf-8") as handle:
            for chunk in handle.read().split(";"):
                chunk = chunk.strip()
                if chunk:
                    statements.append(chunk)

    for statement in statements:
        print("### " + statement[:200])
        try:
            cur.execute(statement)
            rows = cur.fetchall()
            if rows:
                for row in rows:
                    print("\t".join("" if v is None else str(v) for v in row))
            else:
                print("(no rows)")
        except Exception as exc:  # noqa: BLE001
            print("ERROR: %s" % exc)
        print()

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
