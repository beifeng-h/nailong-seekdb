#!/bin/bash
# Prove that the observer really uploads its telemetry report.
#
# SeekDB v1.4.0 reports telemetry exactly once, while it bootstraps
# (report_bootstrap_telemetry). There is no periodic retry, so if the
# network is unavailable at that moment the platform never sees the UUID.
#
# This script checks the other direction: point the telemetry host at a
# black hole and confirm the observer complains. Combined with a bootstrap
# that produces no complaint, that shows the upload really happens.
#
#   bash verify_telemetry_report.sh

set -u

BASE_DIR=/var/lib/seekdb
LOG=$BASE_DIR/log/seekdb.log
HOSTS=/etc/hosts
ENDPOINT=openwebapi.oceanbase.com

rebootstrap() {
    if [ -f "$BASE_DIR/run/seekdb.pid" ]; then
        kill "$(cat "$BASE_DIR/run/seekdb.pid")" 2>/dev/null
        sleep 8
    fi
    rm -rf "$BASE_DIR.new"
    mkdir -p "$BASE_DIR.new"
    setsid nohup /usr/bin/seekdb --nodaemon \
        --base-dir="$BASE_DIR.new" --port=2882 \
        --data-dir="$BASE_DIR.new/store" --redo-dir="$BASE_DIR.new/store/redo" \
        --parameter cpu_count=4 --parameter memory_limit=2G \
        > /var/log/seekdb/observer-verify.log 2>&1 < /dev/null &
    for _ in $(seq 1 90); do
        [ -f "$BASE_DIR.new/run/telemetry.json" ] && break
        sleep 2
    done
    sleep 15
}

warnings() {
    grep -cE "Failed to send telemetry|Failed to write telemetry|Failed to generate telemetry" \
        "$BASE_DIR.new/log/seekdb.log" 2>/dev/null || echo 0
}

echo "### 1. black hole the telemetry endpoint"
cp "$HOSTS" "$HOSTS.bak"
grep -v "$ENDPOINT" "$HOSTS" > "$HOSTS.tmp" && mv "$HOSTS.tmp" "$HOSTS"
echo "127.0.0.1 $ENDPOINT" >> "$HOSTS"

rebootstrap
echo "    uuid   : $(grep -o '"id"[^,]*' "$BASE_DIR.new/run/telemetry.json" 2>/dev/null)"
echo "    warnings: $(warnings)   (expected: >= 1)"

echo "### 2. restore the endpoint and bootstrap again"
mv "$HOSTS.bak" "$HOSTS"
rebootstrap
echo "    uuid   : $(grep -o '"id"[^,]*' "$BASE_DIR.new/run/telemetry.json" 2>/dev/null)"
echo "    warnings: $(warnings)   (expected: 0)"

echo "### done"
