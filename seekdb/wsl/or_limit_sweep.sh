#!/bin/bash
# Find how many OR terms a query can take before the server dies.
# Usage: or_limit_sweep.sh <port> <max_n>
set -u

PORT="${1:?port}"
MAXN="${2:-128}"
CLIENT=/root/seekdb/deps/3rd/u01/obclient/bin/obclient

$CLIENT -h127.0.0.1 -P"$PORT" -uroot -A -e "CREATE DATABASE IF NOT EXISTS ann_rewrite_repro" >/dev/null 2>&1
$CLIENT -h127.0.0.1 -P"$PORT" -uroot -A -e \
  "DROP TABLE IF EXISTS ann_rewrite_repro.t_sweep" >/dev/null 2>&1
$CLIENT -h127.0.0.1 -P"$PORT" -uroot -A -e \
  "CREATE TABLE ann_rewrite_repro.t_sweep (id BIGINT NOT NULL PRIMARY KEY, category INT NOT NULL, age INT NOT NULL,
   KEY idx_category (category), KEY idx_age (age)) ORGANIZATION HEAP" >/dev/null 2>&1
$CLIENT -h127.0.0.1 -P"$PORT" -uroot -A -e \
  "INSERT INTO ann_rewrite_repro.t_sweep SELECT 1,1,1 FROM dual UNION ALL SELECT 2,2,2 FROM dual" >/dev/null 2>&1

for n in 2 4 8 16 24 32 48 64 80 96 112 128; do
  [ "$n" -gt "$MAXN" ] && break
  filter="(category = 1)"
  for _ in $(seq 1 $((n - 1))); do
    filter="$filter OR (age > 1000)"
  done

  start=$(date +%s%N)
  if timeout 120 $CLIENT -h127.0.0.1 -P"$PORT" -uroot -A \
      -e "SELECT COUNT(*) FROM ann_rewrite_repro.t_sweep WHERE $filter" >/dev/null 2>&1; then
    end=$(date +%s%N)
    echo "n=$n OK ($(( (end - start) / 1000000 )) ms)"
  else
    if $CLIENT -h127.0.0.1 -P"$PORT" -uroot -A -e "SELECT 1" >/dev/null 2>&1; then
      echo "n=$n QUERY ERROR (server alive)"
    else
      echo "n=$n SERVER DIED"
      break
    fi
  fi
done

