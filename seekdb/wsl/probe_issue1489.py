#!/usr/bin/env python3
"""Set up the issue #1489 regression table and dump the cached ANN plans.

Used to design the assertions of the mysqltest regression case: it shows what
the plan cache holds for the equivalent filters, before and after the fix.
"""

import argparse
import sys

import pymysql


TABLE = "t_issue1489_ann_rewrite"


def balanced_or(terms):
    if len(terms) == 1:
        return terms[0]
    middle = len(terms) // 2
    return "(%s) OR (%s)" % (balanced_or(terms[:middle]), balanced_or(terms[middle:]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=2882)
    parser.add_argument("--rows", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=32)
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
    cur.execute("CREATE DATABASE IF NOT EXISTS ann_rewrite_repro")
    cur.execute("USE ann_rewrite_repro")
    cur.execute("DROP TABLE IF EXISTS " + TABLE)
    cur.execute(
        """
        CREATE TABLE %s (
          id BIGINT NOT NULL,
          embedding VECTOR(3) NOT NULL,
          category INT NOT NULL,
          age INT NOT NULL,
          PRIMARY KEY (id),
          KEY idx_category (category),
          KEY idx_age (age),
          VECTOR KEY idx_embedding (embedding)
            WITH (distance=l2, type=hnsw, lib=vsag, m=16,
                  ef_construction=128, ef_search=64)
        )
        """ % TABLE
    )
    for row in range(args.rows):
        cur.execute(
            "INSERT INTO %s VALUES (%%s, '[%%s,0,0]', %%s, %%s)" % TABLE,
            (row, row, row, row),
        )
    cur.execute("ANALYZE TABLE " + TABLE)
    print("table %s created with %d rows" % (TABLE, args.rows))

    filters = {
        "A category = 1": "category = 1",
        "B category = 1 OR age > 1000": "(category = 1) OR (age > 1000)",
        "C A OR age>1000 x%d" % args.repeats: balanced_or(
            ["(category = 1)"] + ["(age > 1000)"] * args.repeats
        ),
    }

    cur.execute("ALTER SYSTEM FLUSH PLAN CACHE")
    for name, flt in filters.items():
        sql = (
            "SELECT id FROM %s WHERE %s "
            "ORDER BY l2_distance(embedding, '[0,0,0]') APPROXIMATE LIMIT 10" % (TABLE, flt)
        )
        cur.execute(sql)
        print("--- %s -> %s" % (name, cur.fetchall()))

    print()
    print("== cached plans ==")
    cur.execute(
        """
        SELECT s.statement, p.id, p.operator, p.object_name, p.object_alias,
               p.cardinality, p.access_predicates, p.filter_predicates
        FROM oceanbase.`V$OB_PLAN_CACHE_PLAN_STAT` s
        JOIN oceanbase.__all_virtual_sql_plan p ON p.plan_id = s.plan_id
        WHERE instr(s.statement, '%s') > 0
        ORDER BY length(s.statement), p.id
        """ % TABLE
    )
    for row in cur.fetchall():
        print("  stmt=%s" % row[0][:110])
        print(
            "    id=%s op=%s obj=%s alias=%s card=%s"
            % (row[1], row[2], row[3], row[4], row[5])
        )
        print("    access=%s" % (row[6] or "")[:200])
        print("    filter=%s" % (row[7] or "")[:200])

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
