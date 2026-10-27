#!/bin/bash
# Stop the SeekDB v1.4.0 observer inside the WSL distribution.

BASE_DIR=/var/lib/seekdb
PIDFILE="$BASE_DIR/run/seekdb.pid"

if [ ! -f "$PIDFILE" ]; then
    echo "[seekdb] no pid file at $PIDFILE"
    exit 0
fi

PID=$(cat "$PIDFILE")
if kill -0 "$PID" 2>/dev/null; then
    echo "[seekdb] stopping pid=$PID"
    kill "$PID"
    for _ in $(seq 1 30); do
        kill -0 "$PID" 2>/dev/null || break
        sleep 1
    done
    kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null
fi

echo "[seekdb] stopped"
