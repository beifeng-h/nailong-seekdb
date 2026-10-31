#!/usr/bin/env python3
"""Unified full-text index benchmark for seekdb (OceanBase seekdb 1.4.x).

The benchmark exercises the three stages the optimisation is judged on:

  build   CREATE FULLTEXT INDEX over an already populated table
          -> tokenisation + posting-list construction cost
  load    INSERT ... SELECT into a table that already carries the index
          -> online tokenisation + index write cost
  query   MATCH() retrieval workload
          -> word-table scan, relevance scoring and index back cost

Only the Python standard library is used (see mycli.py) so the harness runs
unmodified on the benchmark host.
"""

import argparse
import hashlib
import json
import os
import statistics
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import corpus
from mycli import Mysql


def now():
    return time.perf_counter()


def pct(values, p):
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * len(ordered) + 0.5)) - 1))
    return ordered[idx]


def summarize(latencies):
    if not latencies:
        return {}
    total = sum(latencies)
    return {
        "count": len(latencies),
        "total_s": round(total, 4),
        "qps": round(len(latencies) / total, 2) if total > 0 else 0.0,
        "mean_ms": round(total / len(latencies) * 1000.0, 3),
        "p50_ms": round(pct(latencies, 50) * 1000.0, 3),
        "p90_ms": round(pct(latencies, 90) * 1000.0, 3),
        "p99_ms": round(pct(latencies, 99) * 1000.0, 3),
        "max_ms": round(max(latencies) * 1000.0, 3),
        "min_ms": round(min(latencies) * 1000.0, 3),
    }


class Bench:
    def __init__(self, args):
        self.args = args
        self.conn = self.connect()
        self.results = {}

    def connect(self):
        return Mysql(self.args.host, self.args.port, self.args.user,
                     self.args.password, timeout=self.args.timeout).connect()

    # -------------------------------------------------------------- setup ---
    def setup(self):
        c = self.conn
        db = self.args.db
        # Bulk index builds and inserts of the benchmark corpus easily exceed
        # the default 10 second statement timeout.
        c.query("set ob_query_timeout = 7200000000")
        c.query("set ob_trx_timeout = 7200000000")
        c.query("drop database if exists `%s`" % db)
        c.query("create database `%s`" % db)
        c.query("use `%s`" % db)
        c.query("create table bench_stage (id int primary key, "
                "title varchar(512), body text) engine=innodb")
        c.query("create table bench_main (id int primary key, "
                "title varchar(512), body text) engine=innodb")
        c.query("create table bench_fts (id int primary key, "
                "title varchar(512), body text, "
                "fulltext index ft_txt(body)) engine=innodb")

    # --------------------------------------------------------------- load ---
    def load_stage(self):
        """Bulk load the raw corpus into the staging table (no index)."""
        c = self.conn
        batch = self.args.batch
        self._acc = 0.0
        wall = now()
        c.query("set autocommit=0")
        rows = corpus.gen_rows(self.args.rows, self.args.seed)
        pending = []
        count = 0
        for row in rows:
            pending.append(row)
            if len(pending) >= batch:
                count += self._flush(pending)
                pending = []
        if pending:
            count += self._flush(pending)
        t0 = now()
        c.query("commit")
        self._acc += now() - t0
        c.query("set autocommit=1")
        wall = now() - wall
        db_time = self._acc
        self.results["load_stage"] = {
            "rows": count,
            "db_s": round(db_time, 3),
            "wall_s": round(wall, 3),
            "rows_per_s": round(count / db_time, 1) if db_time > 0 else 0,
        }
        return count

    def _flush(self, pending):
        values = ",".join(
            "(%d,'%s','%s')" % (i, esc(t), esc(b)) for i, t, b in pending)
        sql = "insert into bench_stage (id,title,body) values " + values
        t0 = now()
        self.conn.query(sql)
        self._last_db = now() - t0
        self._acc += self._last_db
        return len(pending)

    _acc = 0.0

    # -------------------------------------------------------------- build ---
    def build_index(self):
        """CREATE FULLTEXT INDEX: pure tokenise + posting-list build."""
        c = self.conn
        t0 = now()
        c.query("insert into bench_main select id,title,body from bench_stage")
        copy_s = now() - t0
        t0 = now()
        c.query("create fulltext index ft_txt on bench_main (body)")
        build_s = now() - t0
        self.results["build"] = {
            "copy_s": round(copy_s, 3),
            "index_build_s": round(build_s, 3),
            "rows_per_s": round(self.args.rows / build_s, 1) if build_s > 0 else 0,
        }

    def load_fts(self):
        """INSERT into a table that already carries the index."""
        c = self.conn
        t0 = now()
        c.query("insert into bench_fts select id,title,body from bench_stage")
        load_s = now() - t0
        self.results["load_fts"] = {
            "load_s": round(load_s, 3),
            "rows_per_s": round(self.args.rows / load_s, 1) if load_s > 0 else 0,
        }

    # -------------------------------------------------------------- query ---
    def run_queries(self):
        queries = corpus.gen_queries(self.args.queries, self.args.seed + 1)
        table = self.args.query_table
        rendered = []
        for kind, sql in queries:
            rendered.append((kind, sql.replace("bench_main", table)))

        # warm up: page cache and tablet metadata
        warm = Mysql(self.args.host, self.args.port, self.args.user,
                     self.args.password, db=self.args.db,
                     timeout=self.args.timeout).connect()
        warm.query("set ob_query_timeout = 7200000000")
        for _, sql in rendered[:min(20, len(rendered))]:
            warm.query(sql)
        warm.close()

        latencies = []
        checks = []
        conn = self.conn
        if self.args.threads > 1:
            chunk = [rendered[i::self.args.threads] for i in range(self.args.threads)]
            results = [None] * self.args.threads

            def worker(idx):
                c = Mysql(self.args.host, self.args.port, self.args.user,
                          self.args.password, db=self.args.db,
                          timeout=self.args.timeout).connect()
                c.query("set ob_query_timeout = 7200000000")
                lat, chk = [], []
                for kind, sql in chunk[idx]:
                    t0 = now()
                    rows = c.query(sql)
                    lat.append(now() - t0)
                    chk.append((kind, sql, len(rows), row_digest(rows),
                                sorted_row_digest(rows)))
                c.close()
                results[idx] = (lat, chk)

            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(self.args.threads)]
            t0 = now()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            wall = now() - t0
            for item in results:
                latencies.extend(item[0])
                checks.extend(item[1])
        else:
            wall_start = now()
            for kind, sql in rendered:
                t0 = now()
                rows = conn.query(sql)
                latencies.append(now() - t0)
                checks.append((kind, sql, len(rows), row_digest(rows),
                               sorted_row_digest(rows)))
            wall = now() - wall_start

        self.results["query"] = summarize(latencies)
        self.results["query"]["wall_s"] = round(wall, 3)
        self.results["query"]["threads"] = self.args.threads
        self.results["query"]["matched_rows_total"] = sum(c[2] for c in checks)

        if self.args.regress:
            with open(self.args.regress, "w") as fh:
                for kind, sql, n, digest, sdigest in checks:
                    fh.write("%s\t%d\t%s\t%s\t%s\n"
                             % (kind, n, digest, sdigest, sql))
        return checks

    def run_topk_queries(self):
        """Top-k retrieval, the shape a real full text application issues."""
        queries = corpus.gen_queries(self.args.queries, self.args.seed + 2)
        table = self.args.query_table
        rendered = []
        for kind, sql in queries:
            term = sql.split("against('")[1].split("'")[0]
            rendered.append((kind, "select id, match(body) against('%s') as s "
                                  "from %s where match(body) against('%s') "
                                  "order by s desc limit 10" % (term, table, term)))

        conn = self.conn
        for _, sql in rendered[:min(20, len(rendered))]:
            conn.query(sql)

        latencies = []
        checks = []
        for kind, sql in rendered:
            t0 = now()
            rows = conn.query(sql)
            latencies.append(now() - t0)
            checks.append((kind, sql, len(rows), row_digest(rows),
                           sorted_row_digest(rows)))

        self.results["query_topk"] = summarize(latencies)
        self.results["query_topk"]["matched_rows_total"] = sum(c[2] for c in checks)
        if self.args.regress:
            with open(self.args.regress + ".topk", "w") as fh:
                for kind, sql, n, digest, sdigest in checks:
                    fh.write("%s\t%d\t%s\t%s\t%s\n"
                             % (kind, n, digest, sdigest, sql))
        return checks

    # --------------------------------------------------------------- main ---
    def run(self):
        phases = self.args.phases.split(",")
        if "setup" in phases:
            self.setup()
        self.conn.query("use `%s`" % self.args.db)
        if "load" in phases:
            self.load_stage()
        if "build" in phases:
            self.build_index()
        if "load_fts" in phases:
            self.load_fts()
        if "query" in phases:
            self.run_queries()
        if "query_topk" in phases:
            self.run_topk_queries()
        return self.results


def esc(text):
    return text.replace("\\", "\\\\").replace("'", "''")


def row_digest(rows):
    """Stable digest over a result set (column values are raw bytes)."""
    h = hashlib.md5()
    for row in rows:
        for value in row:
            if value is None:
                h.update(b"\x00NULL\x00")
            else:
                if isinstance(value, str):
                    value = value.encode("utf-8")
                h.update(value)
                h.update(b"\x00")
        h.update(b"\x01")
    return h.hexdigest()


def sorted_row_digest(rows):
    """Digest over the result set ignoring the order of the rows.

    MATCH returns the rows ordered by relevance.  Rows with an identical
    relevance score have no defined order, and that order depends on the
    physical layout of the instance, so two different instances legitimately
    return the same rows in a different order.  This digest compares the
    result sets themselves.
    """
    h = hashlib.md5()
    for row in sorted(rows, key=lambda r: tuple(b"" if v is None else v for v in r)):
        for value in row:
            if value is None:
                h.update(b"\x00NULL\x00")
            else:
                if isinstance(value, str):
                    value = value.encode("utf-8")
                h.update(value)
                h.update(b"\x00")
        h.update(b"\x01")
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=2881)
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="")
    ap.add_argument("--db", default="fts_bench")
    ap.add_argument("--rows", type=int, default=100000)
    ap.add_argument("--queries", type=int, default=200)
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--seed", type=int, default=20261031)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--phases", default="setup,load,build,load_fts,query,query_topk")
    ap.add_argument("--query-table", default="bench_main")
    ap.add_argument("--regress", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--tag", default="run")
    args = ap.parse_args()

    bench = Bench(args)
    try:
        results = bench.run()
    finally:
        bench.conn.close()

    payload = {"tag": args.tag, "config": vars(args), "results": results}
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    print(text)


if __name__ == "__main__":
    main()
