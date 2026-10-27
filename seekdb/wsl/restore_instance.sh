#!/bin/bash
# Bring the deployment back to a single observer on the canonical base dir.

set -u

BASE_DIR=/var/lib/seekdb

echo "=== observers before ==="
ps -eo pid,args | grep "[b]in/seekdb" | cut -c1-110

for pid in $(ps -eo pid,args | grep "[b]in/seekdb" | awk '{print $1}'); do
    echo "  killing $pid"
    kill "$pid" 2>/dev/null
done

for _ in $(seq 1 30); do
    ps -eo pid,args | grep -q "[b]in/seekdb" || break
    sleep 1
done
ps -eo pid,args | grep -q "[b]in/seekdb" && pkill -9 -f "[b]in/seekdb"

rm -rf /var/lib/seekdb.new

echo "=== starting on $BASE_DIR ==="
setsid nohup /usr/bin/seekdb --nodaemon \
    --base-dir="$BASE_DIR" --port=2882 \
    --data-dir="$BASE_DIR/store" --redo-dir="$BASE_DIR/store/redo" \
    --parameter cpu_count=4 --parameter memory_limit=2G \
    > /var/log/seekdb/observer.log 2>&1 < /dev/null &

for _ in $(seq 1 90); do
    [ -f "$BASE_DIR/run/telemetry.json" ] && break
    sleep 2
done
sleep 10

echo "=== observers after ==="
ps -eo pid,args | grep "[b]in/seekdb" | cut -c1-110
echo "=== telemetry ==="
cat "$BASE_DIR/run/telemetry.json" 2>/dev/null
echo
echo "=== upload warnings (expect 0) ==="
grep -cE "Failed to send telemetry|Failed to write telemetry" \
    "$BASE_DIR/log/seekdb.log" 2>/dev/null || echo 0
