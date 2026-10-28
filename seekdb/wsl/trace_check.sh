#!/bin/bash
# Run a statement with the optimizer trace enabled and print the trace lines
# that the rewrite rules produced.
set -u

PORT="${1:?port}"
SQL="${2:?sql}"
CLIENT=/root/seekdb/deps/3rd/u01/obclient/bin/obclient
LOGDIR=${3:-/root/seekdb/build/devC/log}

rm -rf "$LOGDIR/obtrace"
$CLIENT -h127.0.0.1 -P"$PORT" -uroot -A -e \
  "SET optimizer_trace_enable=on; SET optimizer_trace_max_trace_size=10485760;" >/dev/null 2>&1
$CLIENT -h127.0.0.1 -P"$PORT" -uroot -A -Doceanbase -e "$SQL" >/dev/null 2>&1
$CLIENT -h127.0.0.1 -P"$PORT" -uroot -A -e "SET optimizer_trace_enable=off;" >/dev/null 2>&1

sleep 1
for f in "$LOGDIR"/obtrace/*.trace; do
  [ -f "$f" ] || continue
  echo "=== $f"
  grep -a -n -i "identity arith\|canonicalize\|remove dummy\|simplify\|SIMPLIFY_EXPR\|rewrite" "$f" | head -30
done

