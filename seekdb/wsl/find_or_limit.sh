#!/bin/bash
# Find how many OR terms the exact path can take before the server dies.

set -u

PORT=2883
BASE=/root/seekdb/build/dev-instance
CLIENT=/root/seekdb/deps/3rd/u01/obclient/bin/obclient

ensure_server() {
  if $CLIENT -h127.0.0.1 -P$PORT -uroot -A -e "SELECT 1" >/dev/null 2>&1; then
    return 0
  fi
  echo "  (server down, restarting)"
  cd /root/seekdb
  python3 tools/deploy/sdb.py stop --base-dir "$BASE" >/dev/null 2>&1
  python3 tools/deploy/sdb.py start --binary build/bin/src/observer/seekdb \
    --base-dir "$BASE" --port $PORT --parameter cpu_count=4 --parameter memory_limit=4G >/dev/null 2>&1
  for _ in $(seq 1 60); do
    $CLIENT -h127.0.0.1 -P$PORT -uroot -A -e "SELECT 1" >/dev/null 2>&1 && break
    sleep 1
  done
}

ensure_server
$CLIENT -h127.0.0.1 -P$PORT -uroot -A -e "CREATE DATABASE IF NOT EXISTS ann_rewrite_repro" >/dev/null 2>&1
$CLIENT -h127.0.0.1 -P$PORT -uroot -A -e "USE ann_rewrite_repro" >/dev/null 2>&1
$CLIENT -h127.0.0.1 -P$PORT -uroot -A -e \
  "CREATE TABLE IF NOT EXISTS t (id BIGINT NOT NULL PRIMARY KEY, embedding VECTOR(128) NOT NULL,
   tenant INT NOT NULL, category INT NOT NULL, age INT NOT NULL,
   KEY idx_category (category), KEY idx_age (age)) ORGANIZATION HEAP" >/dev/null 2>&1
$CLIENT -h127.0.0.1 -P$PORT -uroot -A -e \
  "INSERT INTO t SELECT * FROM t LIMIT 0" >/dev/null 2>&1

for n in 8 16 24 32 48 64 96 128; do
  filter="(category = 1)"
  for _ in $(seq 1 $((n - 1))); do
    filter="$filter OR (age > 1000)"
  done

  ensure_server

  start=$(date +%s%N)
  if $CLIENT -h127.0.0.1 -P$PORT -uroot -Doceanbase -A -e \
      "SELECT COUNT(*) FROM t WHERE $filter" >/dev/null 2>&1; then
    end=$(date +%s%N)
    echo "n=$n OK ($(( (end - start) / 1000000 )) ms)"
  else
    if $CLIENT -h127.0.0.1 -P$PORT -uroot -A -e "SELECT 1" >/dev/null 2>&1; then
      echo "n=$n query error (server alive)"
    else
      echo "n=$n SERVER DIED"
    fi
  fi
done
