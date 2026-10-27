#!/bin/bash
# Start the SeekDB v1.4.0 observer inside the WSL distribution.
#
#   wsl -d seekdb-ubuntu -u root -- bash /mnt/d/数据库大赛/seekdb/wsl/start_seekdb.sh

set -e

BASE_DIR=/var/lib/seekdb
CNF=/etc/seekdb/seekdb.cnf
LOG=/var/log/seekdb/observer.log

PORT=$(grep -E '^port=' "$CNF" | cut -d= -f2)
CPU=$(grep -E '^cpu_count=' "$CNF" | cut -d= -f2)
MEM=$(grep -E '^memory_limit=' "$CNF" | cut -d= -f2)

mkdir -p "$BASE_DIR" "$(dirname "$LOG")"

if [ -f "$BASE_DIR/run/seekdb.pid" ] && kill -0 "$(cat "$BASE_DIR/run/seekdb.pid")" 2>/dev/null; then
    echo "[seekdb] already running, pid $(cat "$BASE_DIR/run/seekdb.pid")"
    exit 0
fi

echo "[seekdb] starting: port=$PORT cpu_count=$CPU memory_limit=$MEM"
setsid nohup /usr/bin/seekdb --nodaemon \
    --base-dir="$BASE_DIR" \
    --port="$PORT" \
    --data-dir="$BASE_DIR/store" \
    --redo-dir="$BASE_DIR/store/redo" \
    --parameter "cpu_count=$CPU" \
    --parameter "memory_limit=$MEM" \
    > "$LOG" 2>&1 < /dev/null &

for _ in $(seq 1 60); do
    if [ -f "$BASE_DIR/run/telemetry.json" ]; then
        sleep 5
        echo "[seekdb] started, pid $(cat "$BASE_DIR/run/seekdb.pid" 2>/dev/null)"
        exit 0
    fi
    sleep 2
done

echo "[seekdb] did not report a telemetry file; check $LOG" >&2
exit 1
