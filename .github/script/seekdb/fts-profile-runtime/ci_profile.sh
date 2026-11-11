#!/usr/bin/env bash
set -euo pipefail
umask 077

test "${GITHUB_ACTIONS:-}" = true
test "${GITHUB_REPOSITORY:-}" = beifeng-h/nailong-seekdb
test "${GITHUB_REF:-}" = refs/heads/perf/fts-profile-diagnostics-20261111
work=${PROFILE_WORK_ROOT:?The workflow must initialize a fresh work directory}
job_started=${PROFILE_JOB_STARTED_UTC:?The workflow must record its UTC start}
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
test -d "$work/evidence" && test ! -L "$work"
test "$(realpath -- "$work")" = "$work"
case "$work" in "$RUNNER_TEMP"/fts-profile-*) ;; *) exit 2 ;; esac

evidence="$work/evidence"
portable="$work/unpacked/fts-portable-original-benchmark-v2-20261111"
run_root="$work/run"
preflight_root="$work/preflight"
profiling="$work/profiling"
owner_uid=$(id -u)
owner_gid=$(id -g)
task_path=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
stage=preparation
active_pid=
step_started=$(date -u +%Y-%m-%dT%H:%M:%SZ)

root_python() {
  sudo -n env -i PATH="$task_path" PYTHONDONTWRITEBYTECODE=1 \
    LC_ALL=C LANG=C DEBUGINFOD_URLS= /usr/bin/python3 -B "$@"
}

finish() {
  result=$?
  trap - EXIT INT TERM
  set +e
  for role in preflight run; do
    owned_root="$work/$role"
    if sudo -n test -f "$owned_root/.fts-owned-run.json" && ! test -d "$evidence/$role"; then
      collection_cap=150s
      if test "$role" = preflight; then collection_cap=30s; fi
      timeout --signal=TERM --kill-after=15s "$collection_cap" \
        sudo -n env -i PATH="$task_path" PYTHONDONTWRITEBYTECODE=1 LC_ALL=C LANG=C \
        /usr/bin/python3 -B "$source_dir/run_profile.py" --portable-root "$portable" \
        --mode collect --run-root "$owned_root" --evidence-out "$evidence/$role" \
        > "$evidence/collection-$role.log" 2>&1
      collection_result=$?
      if test "$collection_result" -ne 0; then result=2; fi
    fi
  done
  if sudo -n test -f "$run_root/.fts-owned-run.json"; then
    if sudo -n test -d "$run_root/profiling" && ! test -f "$evidence/run/profiling-raw.tar.gz"; then
      sudo -n cp -a -- "$run_root/profiling" "$profiling"
      if test "$?" -ne 0; then result=2; fi
    fi
    if sudo -n test -f "$run_root/profiling-adapter-report.json"; then
      sudo -n cp -- "$run_root/profiling-adapter-report.json" "$evidence/profiling-adapter-report.json"
      if test "$?" -ne 0; then result=2; fi
    fi
  fi
  for output in "$evidence" "$profiling"; do
    if test -d "$output" && test ! -L "$output"; then
      sudo -n chown -R --no-dereference "$owner_uid:$owner_gid" -- "$output"
      if test "$?" -ne 0; then result=2; fi
      sudo -n chmod -R u+rwX,go-rwx -- "$output"
      if test "$?" -ne 0; then result=2; fi
    fi
  done
  python3 -B - "$evidence/ci-status.json" "$stage" "$result" <<'PY'
import datetime, json, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    "stage": sys.argv[2], "exitCode": int(sys.argv[3]),
    "finishedUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "formalContestRun": False,
    "physicalHostExclusivityVerified": False,
    "scope": "One CI diagnostic job with preserved runtime cleanup receipts"
}, indent=2) + "\n", encoding="utf-8")
PY
  exit "$result"
}

interrupted() {
  trap '' INT TERM
  if test -n "$active_pid"; then
    # This PID is the timeout monitor created immediately below for this run.
    kill -TERM "$active_pid" 2>/dev/null || true
    wait "$active_pid" || true
    active_pid=
  fi
  exit 143
}
trap finish EXIT
trap interrupted INT TERM

python3 -B - "$source_dir" "$work" <<'PY' > "$evidence/input-verification.json"
import hashlib, json, pathlib, sys, tarfile
source, work = map(pathlib.Path, sys.argv[1:])
pins = {
    "portable-original-v2.tar.gz": "b384f615bd0953dd3ca5602d002007d0fdebb16204a7d23f71b275625867eda5",
    "extract_official_obclient.py": "68cedcaf4ad26dc46619760c647a8b60800a6e01169d851d7b29eac55d2a7fa1",
}
receipt = {"sourceFiles": {}, "downloadedArchives": {}}
for name, expected in pins.items():
    path = source / name
    if path.is_symlink() or not path.is_file():
        raise SystemExit("Expected a regular pinned source file: " + name)
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise SystemExit("Source SHA256 mismatch: " + name)
    receipt["sourceFiles"][name] = actual
for role, expected in {
    "baseline": "02799ccf9fe5ff358cb6cb2403442ce1f3626dce80d211044636256c94140e96",
    "candidate": "76f1429ba1c2064d5421205f85f53eef0feac2055229103b92cf05f07dfbc0e5",
}.items():
    files = list((work / "downloads" / role).iterdir())
    if len(files) != 1 or files[0].is_symlink() or not files[0].is_file():
        raise SystemExit("Expected one exact original downloaded ZIP for " + role)
    with files[0].open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise SystemExit("Original artifact ZIP SHA256 mismatch: " + role)
    receipt["downloadedArchives"][role] = {"path": str(files[0]), "sha256": actual}
destination = work / "unpacked"
destination.mkdir(mode=0o700)
with tarfile.open(source / "portable-original-v2.tar.gz", "r:gz") as archive:
    members = archive.getmembers()
    paths = {}
    if len(members) > 256 or sum(member.size for member in members) > 16 * 1024**2:
        raise SystemExit("Portable archive exceeds its source-only bounds")
    for member in members:
        name = pathlib.PurePosixPath(member.name)
        if (not (member.isreg() or member.isdir()) or member.mode & 0o7000
                or name.is_absolute() or ".." in name.parts or "\\" in member.name
                or str(name) != member.name.rstrip("/")
                or name.parts[0] != "fts-portable-original-benchmark-v2-20261111"
                or str(name) in paths):
            raise SystemExit("Unsafe or duplicate portable member: " + member.name)
        paths[str(name)] = member
    for name in paths:
        for parent in pathlib.PurePosixPath(name).parents:
            if str(parent) in paths and not paths[str(parent)].isdir():
                raise SystemExit("Portable archive path crosses a regular file")
    archive.extractall(destination, members=members, filter="data")
receipt["portableExtraction"] = {"members": len(members), "linksCreated": 0, "filter": "data"}
print(json.dumps(receipt, indent=2))
PY

python3 -B "$source_dir/run_profile.py" --portable-root "$portable" --mode check-bundle \
  > "$evidence/bundle-check.log" 2>&1
python3 -B -m unittest discover -s "$source_dir" -p 'test_*.py' -v \
  > "$evidence/offline-tests.log" 2>&1
mkdir -m 0700 "$work/client-downloads"
(
  cd -- "$work/client-downloads"
  curl --fail --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 120 \
    --output obclient-2.2.3-1.el8.x86_64.rpm \
    https://mirrors.oceanbase.com/oceanbase/community/stable/el/8/x86_64/obclient-2.2.3-1.el8.x86_64.rpm
  curl --fail --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 120 \
    --output libobclient-2.2.3-1.el8.x86_64.rpm \
    https://mirrors.oceanbase.com/oceanbase/community/stable/el/8/x86_64/libobclient-2.2.3-1.el8.x86_64.rpm
  printf '%s\n' \
    '35e38b580996fc583b0eaa961bd78911feff08981624154851db51d7e5aa935d  obclient-2.2.3-1.el8.x86_64.rpm' \
    'e53cadb0d91c1a0d78369f0c9488be8d774f03ea09346e7b218799a5318a9a68  libobclient-2.2.3-1.el8.x86_64.rpm' | sha256sum --check -
  python3 -B "$source_dir/extract_official_obclient.py" --output official-client
) > "$evidence/client-preparation.log" 2>&1

perf_path=$(command -v perf || true)
if test -z "$perf_path" || ! timeout 5s "$perf_path" --version > "$evidence/perf-version.log" 2>&1; then
  perf_path=
  while IFS= read -r candidate; do
    if timeout 5s "$candidate" --version > "$evidence/perf-version.log" 2>&1; then
      perf_path=$candidate
      break
    fi
  done < <(find /usr/lib/linux-tools -maxdepth 3 -type f -name perf -print 2>/dev/null | sort -V -r)
fi
test -n "$perf_path"
task_path="$(dirname -- "$perf_path"):$task_path"
printf '%s\n' "$perf_path" > "$evidence/perf-selected-path.txt"
stage=perf-capability-probe
sudo -n timeout --signal=TERM --kill-after=5s 10s "$perf_path" stat -e cpu-clock:u -- sleep 0.05 \
  > "$evidence/perf-capability-probe.log" 2>&1

cpus=$(python3 -B - "$evidence/host-before.json" <<'PY'
import datetime, glob, json, os, pathlib, shutil, subprocess, sys
affinity = sorted(os.sched_getaffinity(0))
devices = sorted(set(glob.glob('/sys/class/drm/card[0-9]*')
                     + glob.glob('/dev/dri/renderD*') + glob.glob('/dev/nvidia[0-9]*')
                     + glob.glob('/dev/kfd')))
nvidia = shutil.which('nvidia-smi')
query = None
if nvidia:
    result = subprocess.run([nvidia, '--query-gpu=index,name', '--format=csv,noheader'],
                            capture_output=True, text=True, timeout=10)
    query = {"exitCode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
observation = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "allowedCpuSet": affinity, "selectedCpuSet": affinity[:4],
               "visibleGpuDevicePaths": devices, "nvidiaSmi": query,
               "physicalHostExclusivityVerified": False}
pathlib.Path(sys.argv[1]).write_text(json.dumps(observation, indent=2) + '\n')
if len(affinity) < 4:
    raise SystemExit('Four actual allowed CPUs are required')
print(','.join(map(str, affinity[:4])))
PY
)
baseline_zip=$(python3 -B -c 'import json,sys; print(json.load(open(sys.argv[1]))["downloadedArchives"]["baseline"]["path"])' "$evidence/input-verification.json")
candidate_zip=$(python3 -B -c 'import json,sys; print(json.load(open(sys.argv[1]))["downloadedArchives"]["candidate"]["path"])' "$evidence/input-verification.json")
python3 -B "$source_dir/run_profile.py" --portable-root "$portable" --mode extract \
  --run-root "$work/extraction" --baseline-zip "$baseline_zip" --candidate-zip "$candidate_zip" \
  > "$evidence/elf-extraction.log" 2>&1

runtime_args=(--portable-root "$portable" --baseline-elf "$work/extraction/runtime/baseline/seekdb"
  --candidate-elf "$work/extraction/runtime/candidate/seekdb"
  --obclient "$work/client-downloads/official-client/bin/obclient"
  --library-dir /lib/x86_64-linux-gnu --cpus "$cpus"
  --min-memory-gib 6 --min-disk-gib 12 --total-timeout-seconds 3900 --pair-timeout-seconds 3000)
stage=runtime-preflight
timeout --signal=TERM --kill-after=15s 120s \
  sudo -n env -i PATH="$task_path" PYTHONDONTWRITEBYTECODE=1 LC_ALL=C LANG=C \
  /usr/bin/python3 -B "$source_dir/run_profile.py" "${runtime_args[@]}" \
  --mode preflight --run-root "$preflight_root" > "$evidence/runtime-preflight.log" 2>&1
timeout --signal=TERM --kill-after=15s 30s \
  sudo -n env -i PATH="$task_path" PYTHONDONTWRITEBYTECODE=1 LC_ALL=C LANG=C \
  /usr/bin/python3 -B "$source_dir/run_profile.py" --portable-root "$portable" \
  --mode collect --run-root "$preflight_root" --evidence-out "$evidence/preflight" \
  > "$evidence/collection-preflight.log" 2>&1

attestations=$(root_python - "$preflight_root" "$evidence/attestations.json" "$job_started" "$step_started" <<'PY'
import datetime as dt, hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
report = json.loads((root / 'report.json').read_text())
idle = json.loads((root / 'global-processes-preflight.json').read_text())
gpu = json.loads((root / 'gpu-observation.json').read_text())
if report.get('status') != 'runtime_preflight_passed_no_database_started':
    raise SystemExit('Runtime preflight has not passed')
if idle.get('foreignActive') or idle.get('foreignRelated'):
    raise SystemExit('Visible workload prevents the task-window attestation')
if any(row['busyPercent'] != 0 for row in gpu['sysfs']) or gpu.get('nvidiaComputeRows', '').strip():
    raise SystemExit('GPU observations do not support an idle attestation')
now = dt.datetime.now(dt.timezone.utc)
instant = lambda value: dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
for value in [idle['utc'], gpu['utc']]:
    if not 0 <= (now - instant(value)).total_seconds() <= 120:
        raise SystemExit('Preflight observations are stale')
# Reserve time for the driver's 3900s limit, a 50s perf report, bounded
# interruption cleanup, final collection and output ownership transfer.
deadline = min(instant(sys.argv[3]) + dt.timedelta(seconds=5100),
               instant(sys.argv[4]) + dt.timedelta(seconds=4740))
if (deadline - now).total_seconds() < 4535:
    raise SystemExit('Remaining CI window cannot cover execution and cleanup caps')
record = {"idleAttestedAtUtc": idle['utc'], "gpuIdleAttestedAtUtc": gpu['utc'],
          "operatorStopDeadlineUtc": deadline.isoformat(), "physicalHostExclusivityVerified": False,
          "source": "Fresh passing original v2 preflight process and GPU observations",
          "preflightReportSha256": hashlib.sha256((root / 'report.json').read_bytes()).hexdigest(),
          "totalTimeoutSeconds": 3900, "pairTimeoutSeconds": 3000,
          "postCleanupPerfReportReserveSeconds": 50, "ciFinalizationReserveSeconds": 195}
pathlib.Path(sys.argv[2]).write_text(json.dumps(record, indent=2) + '\n')
print(idle['utc'], gpu['utc'], deadline.isoformat())
PY
)
IFS=' ' read -r idle_utc gpu_utc deadline_utc <<< "$attestations"
stage=profiled-original-pair
timeout --signal=TERM --kill-after=360s 3980s \
  sudo -n env -i PATH="$task_path" PYTHONDONTWRITEBYTECODE=1 LC_ALL=C LANG=C DEBUGINFOD_URLS= \
  /usr/bin/python3 -B "$source_dir/run_profile.py" "${runtime_args[@]}" \
  --mode run --run-root "$run_root" --stop-deadline-utc "$deadline_utc" \
  --confirm-exclusive-window --idle-attested-at-utc "$idle_utc" --gpu-idle-attested-at-utc "$gpu_utc" \
  --host-gpu-observation-note 'Visible devices are recorded in host-before.json and activity is checked by the original runtime preflight.' \
  > "$evidence/profile-driver.log" 2>&1 &
active_pid=$!
set +e
wait "$active_pid"
driver_result=$?
set -e
active_pid=
if test "$driver_result" -ne 0; then
  exit "$driver_result"
fi
stage=profile-completed
