#!/bin/bash
# Probe which hosts the build and the PR workflow need.

for host in \
  https://api.github.com \
  https://github.com \
  https://codeload.github.com \
  https://objects.githubusercontent.com \
  https://raw.githubusercontent.com \
  https://mirrors.oceanbase.com \
  https://rsproxy.cn \
  https://static.rust-lang.org
do
  printf '%-42s ' "$host"
  timeout 12 curl -4 -sS -o /dev/null -w '%{http_code} %{time_total}s\n' "$host" 2>&1 | tail -1
done

echo
echo "== git protocol test (15s) =="
timeout 15 git ls-remote https://github.com/oceanbase/seekdb.git HEAD 2>&1 | head -2
echo "rc=$?"
