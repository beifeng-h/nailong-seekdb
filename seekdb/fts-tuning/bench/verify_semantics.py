#!/usr/bin/env python3
"""Compare the observable full text behaviour of two seekdb instances.

The optimisation must not change tokenisation or retrieval semantics, so this
compares, byte for byte:

  * the token stream produced by TOKENIZE() for every probe text
  * the rows and the BM25 relevance scores returned by MATCH() queries

The probe corpus deliberately mixes ASCII, mixed case, every punctuation
character, stop words, tokens around the min/max token size boundaries and
multi byte UTF-8 (including characters that utf8mb4_general_ci folds onto
ASCII, such as U+00E9).
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mycli import Mysql


ASCII = "".join(chr(c) for c in range(32, 127))

PROBES = [
    "",
    " ",
    "hello world",
    "HELLO World",
    "Hello, World!  Mixed CASE; punctuation: " + ASCII,
    "the a an are as at be by com de en for from how i in is it la of on or that "
    "this to was what when where who will with und www",
    "The THE the tHe thE ThE",
    "ab abc abcd abcde abcdef abcdefg",
    "x xy xyz abcd efghijklmnopqrstuvwxyz0123456789",
    "index" * 40,
    "a" * 200,
    "\u00e9t\u00e9 caf\u00e9 na\u00efve",
    "\u00c9t\u00e9 CAF\u00c9",
    "\u4f60\u597d\u4e16\u754c",
    "mixed \u4e2d\u6587 and english \u00e9\u00e8\u00ea",
    "under_score __dunder__ snake_case word_1",
    "123 4567 89 0",
    "trailing spaces   ",
    "   leading spaces",
    "tabs\tand\nnewlines\r\nhere",
    "MiXeD_CaSe_WiTh_UnDeRsCoReS",
    "e\u0301 combining",
    "\uff21\uff22\uff23 fullwidth",
    "emoji \U0001f600 token",
    "word" + "x" * 83,
    "word" + "x" * 84,
    "word" + "x" * 85,
    "caf\u00e9_" + "y" * 40,
]


def connect(args, port):
    return Mysql(args.host, port, args.user, args.password, timeout=300).connect()


def rows_of(conn, sql):
    rows = conn.query(sql)
    if rows is None:
        return []
    out = []
    for row in rows:
        out.append(tuple(v.decode("utf-8", "replace") if isinstance(v, bytes) else v
                         for v in row))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="")
    ap.add_argument("--baseline-port", type=int, required=True)
    ap.add_argument("--optimized-port", type=int, required=True)
    ap.add_argument("--db", default="fts_verify")
    args = ap.parse_args()

    base = connect(args, args.baseline_port)
    opt = connect(args, args.optimized_port)

    failures = []
    checked = 0

    for idx, text in enumerate(PROBES):
        escaped = text.replace("\\", "\\\\").replace("'", "''")
        sql = "select tokenize('%s', 'space')" % escaped
        a = rows_of(base, sql)
        b = rows_of(opt, sql)
        checked += 1
        if a != b:
            failures.append({"kind": "tokenize", "probe": idx,
                             "text": text[:120], "baseline": a, "optimized": b})

    for conn in (base, opt):
        conn.query("drop database if exists `%s`" % args.db)
        conn.query("create database `%s`" % args.db)
        conn.query("use `%s`" % args.db)
        conn.query("create table ft (id int primary key, body text, "
                    "fulltext index ft_body(body)) engine=innodb")

    for idx, text in enumerate(PROBES):
        escaped = text.replace("\\", "\\\\").replace("'", "''")
        insert = "insert into ft values (%d, '%s')" % (idx, escaped)
        base.query(insert)
        opt.query(insert)

    terms = ["hello", "world", "index", "the", "word", "abc", "cafe",
             "caf\u00e9", "\u4e16\u754c", "mixed", "token", "xxxxx", "e",
             "a", "z", "aa"]
    modes = ["natural language mode", "boolean mode"]
    for term in terms:
        clean = term.replace("'", "")
        for mode in modes:
            if mode == "boolean mode":
                sql = ("select id, match(body) against('%s' in boolean mode) "
                       "from ft where match(body) against('%s' in boolean mode) "
                       "order by id" % (clean, clean))
            else:
                sql = ("select id, match(body) against('%s') from ft "
                       "where match(body) against('%s') order by id"
                       % (clean, clean))
            a = rows_of(base, sql)
            b = rows_of(opt, sql)
            checked += 1
            if a != b:
                failures.append({"kind": "match", "term": term, "mode": mode,
                                 "baseline": a, "optimized": b})

    a = rows_of(base, "select id, body from ft order by id")
    b = rows_of(opt, "select id, body from ft order by id")
    checked += 1
    if a != b:
        failures.append({"kind": "table_content", "baseline": a, "optimized": b})

    a = rows_of(base, "show create table ft")
    b = rows_of(opt, "show create table ft")
    checked += 1
    if a != b:
        failures.append({"kind": "ddl", "baseline": a, "optimized": b})

    base.close()
    opt.close()

    report = {
        "checks": checked,
        "failures": len(failures),
        "ok": len(failures) == 0,
        "details": failures[:20],
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
