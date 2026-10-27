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

    wsl -d seekdb-ubuntu -u root -- bash wsl/get_telemetry_id.sh

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
    wsl -d seekdb-ubuntu -u root -- bash wsl/start_seekdb.sh
    wsl -d seekdb-ubuntu -u root -- bash wsl/stop_seekdb.sh
    wsl -d seekdb-ubuntu -u root -- bash wsl/get_telemetry_id.sh

    # logs
    wsl -d seekdb-ubuntu -u root -- tail -f /var/lib/seekdb/log/seekdb.log

## Note

The WSL distribution lives on `D:` and holds the whole deployment. If the
distribution is removed the UUID goes with it -- re-import and redeploy to
get a new one.
