#!/bin/bash
# Build environment bootstrap for the seekdb source build used by competition
# problem 2 (fix a 2026-competition issue and submit a PR).
#
# Run inside WSL:
#   wsl -d seekdb-ubuntu -u root -- bash /mnt/d/数据库大赛/seekdb/wsl/build_seekdb.sh
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive

if ! command -v g++ >/dev/null 2>&1 || ! command -v rpmbuild >/dev/null 2>&1; then
  echo "=== host packages ==="
  apt-get update
  apt-get install -y \
    git wget curl rpm rpm2cpio cpio make build-essential binutils m4 file \
    python3 python3-pip python3-venv libaio-dev
fi

echo "=== versions ==="
git --version
g++ --version | head -1
python3 --version

echo "=== source tree ==="
SRC=/root/seekdb
if [ ! -d "$SRC/.git" ]; then
  git clone --progress https://github.com/oceanbase/seekdb.git "$SRC"
fi
cd "$SRC"
git remote set-url origin https://github.com/oceanbase/seekdb.git
git remote remove upstream 2>/dev/null || true
git remote add upstream https://github.com/oceanbase/seekdb.git
git fetch upstream
git switch -C fix/issue-1489 upstream/master
git log --oneline -1

echo "=== dependencies (long) ==="
./build.py init

echo "=== compile (long) ==="
./build.py build -j8 seekdb

ls -l build/bin/src/observer/seekdb
echo "BOOTSTRAP_DONE"
