#!/bin/bash
# Copy the edited sources from the Windows working copy into the WSL git tree.
#
#   wsl -d seekdb-ubuntu -u root -- bash /mnt/d/数据库大赛/seekdb/wsl/sync_to_wsl.sh

set -euo pipefail

SRC="/mnt/d/数据库大赛/seekdb-master"
DST="/root/seekdb"

for file in \
  src/sql/rewrite/ob_transformer_impl.cpp \
  tools/deploy/mysql_test/test_suite/vector_index/t/vector_index_issue1489.test \
  tools/deploy/mysql_test/test_suite/vector_index/r/mysql/vector_index_issue1489.result
do
  if [ -f "$SRC/$file" ]; then
    mkdir -p "$DST/$(dirname "$file")"
    cp "$SRC/$file" "$DST/$file"
    echo "synced $file"
  else
    echo "skip (absent) $file"
  fi
done

cd "$DST"
git status --short
