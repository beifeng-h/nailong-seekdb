#!/bin/bash
# Reproduce the crash on a wide OR filter and capture a core dump.
set -u

cd /root/seekdb
BASE=/root/seekdb/build/crashdb
PORT=2888
CLIENT=/root/seekdb/deps/3rd/u01/obclient/bin/obclient

rm -rf "$BASE" /root/seekdb/build/core.*

ulimit -c unlimited
python3 tools/deploy/sdb.py start --binary build/seekdb-baseline \
  --base-dir "$BASE" --port "$PORT" \
  --parameter cpu_count=2 --parameter memory_limit=2G >/dev/null 2>&1

for _ in $(seq 1 90); do
  $CLIENT -h127.0.0.1 -P"$PORT" -uroot -A -e "SELECT 1" >/dev/null 2>&1 && break
  sleep 1
done

$CLIENT -h127.0.0.1 -P"$PORT" -uroot -A -e \
  "CREATE DATABASE IF NOT EXISTS ann_rewrite_repro" >/dev/null 2>&1
$CLIENT -h127.0.0.1 -P"$PORT" -uroot -A -e \
  "DROP TABLE IF EXISTS ann_rewrite_repro.t_sweep" >/dev/null 2>&1
$CLIENT -h127.0.0.1 -P"$PORT" -uroot -A -e \
  "CREATE TABLE ann_rewrite_repro.t_sweep (id BIGINT NOT NULL PRIMARY KEY, category INT NOT NULL,
   age INT NOT NULL, KEY idx_category (category), KEY idx_age (age)) ORGANIZATION HEAP" >/dev/null 2>&1
$CLIENT -h127.0.0.1 -P"$PORT" -uroot -A -e \
  "INSERT INTO ann_rewrite_repro.t_sweep SELECT 1,1,1 FROM dual" >/dev/null 2>&1

filter="(category = 1)"
for _ in $(seq 1 127); do
  filter="$filter OR (age > 1000)"
done

echo "running the crashing query..."
timeout 120 $CLIENT -h127.0.0.1 -P"$PORT" -uroot -A \
  -e "SELECT COUNT(*) FROM ann_rewrite_repro.t_sweep WHERE $filter" 2>&1 | head -3

sleep 3
ls -la /root/seekdb/build/core.* 2>/dev/null

CORE=$(ls -1 /root/seekdb/build/core.* 2>/dev/null | head -1)
if [ -n "$CORE" ]; then
  echo "=== backtrace ==="
  gdb -q -batch \
    -ex "set pagination off" \
    -ex "bt 40" \
    build/seekdb-baseline "$CORE" 2>&1 | tail -50
else
  echo "no core dump produced"
fi

