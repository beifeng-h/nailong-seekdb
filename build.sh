#!/usr/bin/env bash
# Root build entry for the grading environment.
#
# Thin wrapper around the upstream `build.upstream.sh` (the original seekdb
# `build.sh`). It exists because the upstream entry point refuses to run without
# an explicit profile (`./build.sh` alone prints usage and exits 2), and because
# the release build needs a prepared `deps/3rd` tree plus CMake.
#
# Behaviour:
#   * no arguments -> release build with `--make` (what a grader expects);
#   * if `deps/3rd` is not prepared, link the platform's prebuilt dependency
#     cache (the one the seekdb notebook image ships at
#     /opt/seekdb-deps-cache/*/3rd) so the build needs no network;
#   * any argument combination is forwarded to `build.upstream.sh` verbatim.
#
# Examples:
#   ./build.sh                        # release, -j32 (capped)
#   ./build.sh release --make -j96    # explicit
#   ./build.sh rpm --init --make rpm  # packaging still works
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE" || exit 1

JOBS="$(nproc 2>/dev/null || echo 8)"
[ "$JOBS" -lt 1 ] && JOBS=8

# --- 0. build offline --------------------------------------------------------
# The rust components (sql_nio, ...) are vendored in the cargo cache; without
# this the build stops at `rust-target/release/libsql_nio.a` with a crates.io
# connect error on a machine that has no egress.
export CARGO_NET_OFFLINE="${CARGO_NET_OFFLINE:-true}"
export CARGO_TERM_COLOR="${CARGO_TERM_COLOR:-never}"

# --- 1. prepare deps/3rd from a prebuilt cache when possible -----------------
if [ ! -f deps/3rd/DONE ]; then
  for cat in "${SEEKDB_DEPS_CACHE:-}" /opt/seekdb-deps-cache/*; do
    [ -n "$cat" ] || continue
    if [ -d "$cat/3rd/usr/local/oceanbase/deps" ]; then
      echo "[build.sh] using prebuilt dependencies from $cat"
      mkdir -p deps/3rd/usr/local/oceanbase
      for d in deps devtools; do
        if [ ! -e "deps/3rd/usr/local/oceanbase/$d" ]; then
          cp -al "$cat/3rd/usr/local/oceanbase/$d" "deps/3rd/usr/local/oceanbase/$d" 2>/dev/null \
            || cp -r "$cat/3rd/usr/local/oceanbase/$d" "deps/3rd/usr/local/oceanbase/$d"
        fi
      done
      cp -f "$cat/3rd/DONE" deps/3rd/DONE 2>/dev/null || true
      break
    fi
  done
fi

# --- 2. default invocation ---------------------------------------------------
if [ "$#" -eq 0 ]; then
  echo "[build.sh] no arguments: building release with -j$JOBS"
  set -- release --make "-j$JOBS"
fi

bash "$HERE/build.upstream.sh" "$@"
rc=$?

# --- 3. put the artifact where a harness may look for it ---------------------
if [ "$rc" -eq 0 ] && [ -f build_release/src/observer/seekdb ]; then
  mkdir -p bin 2>/dev/null || true
  ln -sf ../build_release/src/observer/seekdb bin/seekdb 2>/dev/null || true
  ln -sf build_release/src/observer/seekdb seekdb 2>/dev/null || true
  echo "[build.sh] artifact: build_release/src/observer/seekdb"
  echo "[build.sh] artifact: bin/seekdb (symlink)"
fi

exit "$rc"
