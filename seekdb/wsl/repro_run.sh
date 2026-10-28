#!/bin/bash
# Run the issue #1489 reproducer against a running instance.
# Usage: repro_run.sh <port> [rows] [repeats] [runs]
set -u

PORT="${1:?port}"
ROWS="${2:-20000}"
REPEATS="${3:-128}"
RUNS="${4:-5}"

timeout 1800 python3 "/mnt/d/数据库大赛/seekdb/wsl/repro_ann_rewrite.py" \
  --port "$PORT" --rows "$ROWS" --repeats "$REPEATS" --runs "$RUNS"

