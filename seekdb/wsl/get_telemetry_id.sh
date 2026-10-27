#!/bin/bash
# Print the deployment UUID (content.id) of the SeekDB v1.4.0 instance.

FILE=/var/lib/seekdb/run/telemetry.json

if [ ! -f "$FILE" ]; then
    echo "telemetry.json not found at $FILE - is seekdb running?" >&2
    exit 1
fi

if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["content"]["id"])' "$FILE"
else
    grep -o '"id"[[:space:]]*:[[:space:]]*"[^"]*"' "$FILE" | head -1 | cut -d'"' -f4
fi
