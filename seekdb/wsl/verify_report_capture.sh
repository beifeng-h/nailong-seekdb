#!/bin/bash
# Capture the observer's telemetry upload with a local CONNECT proxy.
#
# Boots a throwaway observer with https_proxy pointed at a proxy that logs
# the target and then refuses, which proves two things at once:
#
#   * the observer really tries to upload its telemetry report, and
#   * a failed upload is what produces the "Failed to send telemetry" line
#     in the log -- so a bootstrap without that line means the upload
#     succeeded.

set -u

PROBE=/var/lib/seekdb_probe
PROXY_LOG=/var/log/seekdb/proxy.log

rm -f "$PROXY_LOG"

echo "### starting the capture proxy"
python3 /mnt/d/数据库大赛/seekdb/wsl/proxy_capture.py 8888 &
PROXY_PID=$!
sleep 2

echo "### booting a throwaway observer through the proxy"
mkdir -p "$PROBE"
https_proxy=http://127.0.0.1:8888 \
  setsid nohup /usr/bin/seekdb --nodaemon \
    --base-dir="$PROBE" --port=2883 \
    --data-dir="$PROBE/store" --redo-dir="$PROBE/store/redo" \
    --parameter cpu_count=4 --parameter memory_limit=2G \
    > /var/log/seekdb/observer-probe.log 2>&1 < /dev/null &

for _ in $(seq 1 90); do
    [ -f "$PROBE/run/telemetry.json" ] && break
    sleep 2
done
sleep 20

echo "### proxy log (expect a CONNECT to the telemetry endpoint)"
cat "$PROXY_LOG" 2>/dev/null

echo "### observer upload warnings (expect >= 1)"
grep -cE "Failed to send telemetry|Failed to write telemetry" \
    "$PROBE/log/seekdb.log" 2>/dev/null || echo 0

echo "### cleaning up"
for pid in $(ps -eo pid,args | grep "[b]in/seekdb" | grep "seekdb_probe" | awk '{print $1}'); do
    kill "$pid" 2>/dev/null
done
sleep 6
rm -rf "$PROBE"
kill "$PROXY_PID" 2>/dev/null

echo "### done"
