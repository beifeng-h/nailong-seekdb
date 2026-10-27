#!/bin/bash
# (Re-)send the telemetry report of this deployment.
#
# SeekDB v1.4.0 uploads its telemetry exactly once, while it bootstraps. If
# the network was unavailable at that moment there is no retry and the
# competition platform cannot find the deployment UUID -- this script posts
# the very same payload the observer produced, so the record exists.
#
#   bash report_telemetry.sh

set -u

FILE=/var/lib/seekdb/run/telemetry.json
URL=https://openwebapi.oceanbase.com/api/web/oceanbase/report

if [ ! -f "$FILE" ]; then
    echo "no telemetry file at $FILE - is seekdb running?" >&2
    exit 1
fi

echo "payload:"
cat "$FILE"
echo

echo "posting to $URL"
curl -s -w "\nhttp=%{http_code}\n" --max-time 30 \
    -X POST -H "Content-Type: application/json" \
    --data-binary "@$FILE" "$URL"
