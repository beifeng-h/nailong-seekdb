import pymysql

conn = pymysql.connect(host="127.0.0.1", port=2881, user="root", password="",
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

cur.execute("SELECT COUNT(*) FROM oceanbase.__all_virtual_table")
print("internal tabs  ->", cur.fetchall())

conn.commit()
conn.close()
print("\nCONNECTED AND QUERIED OK")
