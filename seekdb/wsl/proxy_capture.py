#!/usr/bin/env python3
"""A tiny CONNECT proxy that just logs what it is asked to tunnel.

Used to prove that the observer really tries to upload its telemetry
report. It accepts the CONNECT, records the target and then hangs up, so
the upload fails and the observer logs "Failed to send telemetry".
"""

import socket
import sys

LOG = "/var/log/seekdb/proxy.log"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8888


def log(line):
    with open(LOG, "a") as f:
        f.write(line + "\n")


server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", PORT))
server.listen(16)
log(f"proxy listening on 127.0.0.1:{PORT}")

while True:
    conn, _ = server.accept()
    try:
        conn.settimeout(10)
        data = conn.recv(4096).decode("latin-1")
        first = data.split("\r\n", 1)[0]
        log(f"REQUEST {first}")
        conn.sendall(b"HTTP/1.1 503 Service Unavailable\r\n\r\n")
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR {exc}")
    finally:
        conn.close()
