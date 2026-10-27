"""Query a running SeekDB instance to prove it is alive and serving SQL.

    python verify_sql.py [port]
"""

import sys

import pymysql

port = int(sys.argv[1]) if len(sys.argv) > 1 else 2881

conn = pymysql.connect(host="127.0.0.1", port=port, user="root", password="",
                       connect_timeout=20)
cur = conn.cursor()

cur.execute("SELECT VERSION()")
print("VERSION()      ->", cur.fetchall())

cur.execute("SHOW DATABASES")
print("DATABASES      ->", [r[0] for r in cur.fetchall()])

cur.execute("CREATE DATABASE IF NOT EXISTS seekdb_demo")
cur.execute("USE seekdb_demo")
cur.execute("CREATE TABLE IF NOT EXISTS t (id INT PRIMARY KEY, name VARCHAR(64))")
cur.execute("INSERT INTO t VALUES (1,'hello'),(2,'seekdb') "
            "ON DUPLICATE KEY UPDATE name=name")
cur.execute("SELECT * FROM t ORDER BY id")
print("table t        ->", cur.fetchall())

conn.commit()
conn.close()
print(f"\nCONNECTED AND QUERIED OK (port {port})")
