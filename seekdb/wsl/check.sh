#!/bin/bash
# Helper: run a command against a seekdb instance with a bounded wait.
# Usage: check.sh <port> <sql>
set -u

PORT="${1:?port}"
SQL="${2:?sql}"
CLIENT=/root/seekdb/deps/3rd/u01/obclient/bin/obclient

for _ in $(seq 1 60); do
  if $CLIENT -h127.0.0.1 -P"$PORT" -uroot -A -e "SELECT 1" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

timeout 300 $CLIENT -h127.0.0.1 -P"$PORT" -uroot -A -e "$SQL"

