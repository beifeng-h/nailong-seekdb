# Linux (WSL) deployment -- SeekDB v1.4.0

This is the deployment whose telemetry ID is a UUID, which is what the
competition submission form asks for. The Windows deployment next to it runs
v1.3.0, whose `content.id` is a 64 character hex string instead -- see
`../README.md` for why both exist.

## Where it lives

| | |
| --- | --- |
| WSL distribution | `seekdb-ubuntu` (WSL2, Ubuntu 22.04.5 LTS, glibc 2.35) |
| Install location | `D:\wsl\seekdb-ubuntu` |
| Observer binary | `/usr/bin/seekdb` (SeekDB v1.4.0.0) |
| Config | `/etc/seekdb/seekdb.cnf` |
| Base directory | `/var/lib/seekdb` |
| Telemetry file | `/var/lib/seekdb/run/telemetry.json` |
| Port | 2882 (2881 is taken by the Windows instance on the host) |

Reaching the instance from Windows works through WSL's localhost forwarding:

    python ..\tools\verify_sql.py 2882

## The deployment UUID

    1c461b9d-973e-8c7e-9a3c-29099836cf43

Read it back with:

    cd D:\数据库大赛
    wsl -d seekdb-ubuntu -u root -- bash seekdb/wsl/get_telemetry_id.sh

Unlike v1.3.0, v1.4.0 derives the UUID from the machine id and the base
directory instead of a timestamp, so **the UUID does not change when the
instance is restarted**.

## How it was built

1. WSL 2 was already present on the machine, only without a distribution.
   A minimal Ubuntu 22.04 rootfs was imported:

       wsl --import seekdb-ubuntu D:\wsl\seekdb-ubuntu ubuntu-22.04-minimal-cloudimg-amd64-root.tar.xz --version 2

2. The official v1.4.0 Ubuntu 22.04 package was downloaded and unpacked into
   its canonical paths (`dpkg-deb -x`, so the maintainer scripts and systemd
   unit stay out of the way):

       /usr/bin/seekdb, /usr/bin/obshell
       /usr/libexec/seekdb/scripts/...
       /usr/share/seekdb/{admin,srs,timezone}
       /etc/seekdb/{seekdb.cnf,default_parameter.json,...}

3. The minimal cloud image ships an empty `/etc/machine-id`, and v1.4.0
   derives the UUID from it, so a machine id was generated once and persisted:

       head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' > /etc/machine-id
       cp /etc/machine-id /var/lib/dbus/machine-id

4. `/etc/seekdb/seekdb.cnf` was written with the base, data and redo
   directories, the port and the resource limits, then the observer was
   started with `start_seekdb.sh`.

## Everyday commands

    # start / stop / read the UUID
    cd D:\数据库大赛
    wsl -d seekdb-ubuntu -u root -- bash seekdb/wsl/start_seekdb.sh
    wsl -d seekdb-ubuntu -u root -- bash seekdb/wsl/stop_seekdb.sh
    wsl -d seekdb-ubuntu -u root -- bash seekdb/wsl/get_telemetry_id.sh

    # logs
    wsl -d seekdb-ubuntu -u root -- tail -f /var/lib/seekdb/log/seekdb.log

## The upload -- this one bites

The competition platform does not just read the local file. It looks the UUID
up in **the report the instance uploaded to
`https://openwebapi.oceanbase.com/api/web/oceanbase/report`**. If the server
has no record for the UUID, the submission is rejected with
"暂未查询到该部署 UUID 的上报记录".

SeekDB v1.4.0 uploads **exactly once, while it bootstraps**
(`report_bootstrap_telemetry` in `src/observer/ob_server.cpp`). There is no
periodic retry. So if the network was unavailable at the moment the instance
bootstrapped, the UUID exists locally but never reaches the server, and no
amount of restarting helps -- a restart on an existing data directory does not
bootstrap again and therefore does not report again.

Check whether the upload happened:

    wsl -d seekdb-ubuntu -u root -- grep -cE "Failed to send telemetry" /var/lib/seekdb/log/seekdb.log

`0` means the upload succeeded. If it is not `0`, re-send the very same
payload the observer produced:

    wsl -d seekdb-ubuntu -u root -- bash seekdb/wsl/report_telemetry.sh

which posts `run/telemetry.json` to the endpoint and prints the server's
answer:

    {
      "code" : 200,
      "message" : "操作成功",
      "data" : "31218627920"
    }

If the base directory has to change, note that the UUID depends on it -- the
same instance under `/var/lib/seekdb` and under `/var/lib/seekdb.new` gets two
different UUIDs. The UUID is derived from the machine id and the base
directory, not from a timestamp, which is why it survives restarts but not a
moved data directory.

### Proving the upload happens

`verify_report_capture.sh` boots a throwaway observer with `https_proxy`
pointed at `proxy_capture.py`, a proxy that logs the target and then hangs
up. The proxy log shows the attempt:

    REQUEST CONNECT openwebapi.oceanbase.com:443 HTTP/1.1

and the observer logs `Failed to send telemetry`, which is the same warning
that would appear on a real network failure. That is how we know a bootstrap
without that warning really uploaded.

## Note

The WSL distribution lives on `D:` and holds the whole deployment. If the
distribution is removed the UUID goes with it -- re-import and redeploy to
get a new one.
