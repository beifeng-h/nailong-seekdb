#!/usr/bin/env bash
set -euo pipefail
umask 077

test "${GITHUB_ACTIONS:-}" = true
test "${GITHUB_REPOSITORY:-}" = beifeng-h/nailong-seekdb
test "${GITHUB_REF:-}" = refs/heads/perf/fts-profile-diagnostics-20261111
work=${PROFILE_ANALYSIS_WORK_ROOT:?The workflow must initialize a fresh work directory}
test -d "$work/evidence" && test ! -L "$work"
test "$(realpath -- "$work")" = "$work"
case "$work" in "$RUNNER_TEMP"/fts-recording-analysis-*) ;; *) exit 2 ;; esac
export DEBUGINFOD_URLS=
evidence="$work/evidence"
stage=input-preparation

finish() {
  result=$?
  trap - EXIT
  python3 -B - "$evidence" "$stage" "$result" <<'PY'
import datetime
import hashlib
import json
import pathlib
import sys

evidence = pathlib.Path(sys.argv[1])
(evidence / "ci-status.json").write_text(json.dumps({
    "stage": sys.argv[2],
    "exitCode": int(sys.argv[3]),
    "finishedUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "scope": "Offline decoding of a saved candidate perf recording",
    "formalContestRun": False,
}, indent=2) + "\n", encoding="utf-8")
files = []
for path in sorted(evidence.rglob("*")):
    if path.is_file() and path.name != "output-file-manifest.json":
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files.append({"path": path.relative_to(evidence).as_posix(),
                      "bytes": path.stat().st_size, "sha256": digest})
(evidence / "output-file-manifest.json").write_text(
    json.dumps({"files": files}, indent=2) + "\n", encoding="utf-8")
PY
  exit "$result"
}
trap finish EXIT

python3 -B - "$work" <<'PY' > "$evidence/input-preparation.log" 2>&1
import hashlib
import json
import pathlib
import re
import shutil
import sys
import tarfile
import zipfile

work = pathlib.Path(sys.argv[1])
evidence = work / "evidence"
elf_sha256 = "9f2cef74d9bcee62ef155531fc25eec0292eb94c25ce531bc31750b0df8dcf0b"
elf_bytes = 1332371664
build_id = "29197aa40e7b172beb280c49dfca1aaa8a05bc01"
receipt = {"archives": {}, "supplementalFiles": []}


def safe_name(name):
    path = pathlib.PurePosixPath(name)
    if (not path.parts or path.is_absolute() or ".." in path.parts
            or "\\" in name or str(path) != name):
        raise ValueError("Unsafe archive path: " + name)
    return path


def copy_verified(source, target, expected_bytes, expected_sha256):
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256()
    count = 0
    with target.open("xb") as output:
        while chunk := source.read(1024 * 1024):
            count += len(chunk)
            if count > expected_bytes:
                raise ValueError("Archive member exceeds its pinned size: " + str(target))
            digest.update(chunk)
            output.write(chunk)
    if count != expected_bytes or digest.hexdigest() != expected_sha256:
        raise ValueError("Archive member size or SHA256 mismatch: " + str(target))


archives = {}
for role, expected_sha256 in {
    "recording": "82d90114091591ca0f00150c96a96445affe8f2b596df703fd2ba09e58fe08b0",
    "candidate": "76f1429ba1c2064d5421205f85f53eef0feac2055229103b92cf05f07dfbc0e5",
}.items():
    files = list((work / "downloads" / role).iterdir())
    if len(files) != 1 or files[0].is_symlink() or not files[0].is_file():
        raise ValueError("Expected one original artifact ZIP for " + role)
    with files[0].open("rb") as stream:
        actual_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError("Original artifact ZIP SHA256 mismatch for " + role)
    archives[role] = files[0]
    receipt["archives"][role] = {"bytes": files[0].stat().st_size,
                                 "sha256": actual_sha256}

unpacked = work / "unpacked"
unpacked.mkdir(mode=0o700)
supplement = work / "profiling-raw.tar.gz"
with zipfile.ZipFile(archives["recording"]) as archive:
    matches = [entry for entry in archive.infolist()
               if entry.filename == "evidence/run/profiling-raw.tar.gz"]
    if len(matches) != 1 or matches[0].is_dir() or matches[0].file_size != 8075807:
        raise ValueError("Expected the unique pinned profiling supplemental archive")
    with archive.open(matches[0]) as source:
        copy_verified(source, supplement, 8075807,
                      "b2a8bc6a85a6a7b6c0c17217d4b8888ee87c5d359250e3b5c76b76df5f69b1d1")
receipt["supplementalArchive"] = {
    "path": "evidence/run/profiling-raw.tar.gz", "bytes": 8075807,
    "sha256": "b2a8bc6a85a6a7b6c0c17217d4b8888ee87c5d359250e3b5c76b76df5f69b1d1",
}
with tarfile.open(supplement, "r:gz") as archive:
    members = archive.getmembers()
    if len(members) > 128 or sum(member.size for member in members) > 128 * 1024**2:
        raise ValueError("Profiling supplemental archive exceeds its bounds")
    by_name = {}
    for member in members:
        safe_name(member.name)
        if not member.isreg() or member.mode & 0o7000 or member.name in by_name:
            raise ValueError("Nonregular or duplicate supplemental member: " + member.name)
        by_name[member.name] = member
    manifest_member = by_name["profiling-payload-manifest.json"]
    if manifest_member.size > 1024**2:
        raise ValueError("Profiling manifest exceeds its size bound")
    with archive.extractfile(manifest_member) as source:
        manifest = json.load(source)
    entries = {}
    for entry in manifest["files"]:
        name = entry["path"]
        safe_name(name)
        if (name in entries or type(entry["bytes"]) is not int or entry["bytes"] < 0
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise ValueError("Invalid or duplicate profiling manifest entry: " + name)
        entries[name] = entry
    if set(entries) != set(by_name) - {"profiling-payload-manifest.json"}:
        raise ValueError("Profiling manifest does not cover the exact archive inventory")
    for name, entry in entries.items():
        member = by_name[name]
        if member.size != entry["bytes"]:
            raise ValueError("Profiling member size differs from its manifest: " + name)
        target = unpacked.joinpath(*safe_name(name).parts)
        if not target.resolve().is_relative_to(unpacked.resolve()):
            raise ValueError("Profiling member escapes its extraction root")
        with archive.extractfile(member) as source:
            copy_verified(source, target, entry["bytes"], entry["sha256"])
        receipt["supplementalFiles"].append(entry)
    with archive.extractfile(manifest_member) as source:
        (evidence / "profiling-payload-manifest.json").write_bytes(source.read())

owned = json.loads((unpacked / "candidate/owned-process.json").read_text(encoding="utf-8"))
runtime = json.loads((unpacked / "runtime-verification.json").read_text(encoding="utf-8"))
original_exe = owned["exe"]
exe_path = pathlib.PurePosixPath(original_exe)
if (not exe_path.is_absolute() or ".." in exe_path.parts or "\\" in original_exe
        or str(exe_path) != original_exe or exe_path.name != "seekdb"):
    raise ValueError("Invalid original absolute executable path")
candidate = runtime["candidate"]
if (candidate["path"] != original_exe or candidate["sha256"] != elf_sha256
        or candidate["bytes"] != elf_bytes or candidate["buildId"] != build_id
        or candidate["actualTargetHashVerified"] is not True):
    raise ValueError("Recorded runtime identity differs from the pinned candidate ELF")
symfs = work / "symfs"
symfs.mkdir(mode=0o700)
elf = symfs.joinpath(*exe_path.parts[1:])
if not elf.resolve().is_relative_to(symfs.resolve()):
    raise ValueError("Executable symbol path escapes symfs")
with zipfile.ZipFile(archives["candidate"]) as archive:
    matches = [entry for entry in archive.infolist() if entry.filename == "candidate-elf.tar.gz"]
    if len(matches) != 1 or matches[0].is_dir():
        raise ValueError("Expected one candidate ELF archive")
    extracted = False
    with archive.open(matches[0]) as source, tarfile.open(fileobj=source, mode="r|gz") as tar:
        for member in tar:
            safe_name(member.name)
            if (extracted or member.name != "candidate/seekdb" or not member.isreg()
                    or member.mode & 0o7000 or member.size != elf_bytes):
                raise ValueError("Unexpected candidate ELF archive member: " + member.name)
            with tar.extractfile(member) as payload:
                copy_verified(payload, elf, elf_bytes, elf_sha256)
            elf.chmod(0o400)
            extracted = True
    if not extracted:
        raise ValueError("Candidate ELF archive is empty")
for name in ("candidate/owned-process.json", "runtime-verification.json"):
    shutil.copyfile(unpacked / name, evidence / pathlib.PurePosixPath(name).name)
receipt["candidateElf"] = {"originalExe": original_exe, "symfsPath": str(elf),
                           "bytes": elf_bytes, "sha256": elf_sha256, "buildId": build_id}
receipt["analysisPaths"] = {"symfs": str(symfs), "elf": str(elf),
                            "perfData": str(unpacked / "profiling/candidate-benchmark/perf.data")}
(evidence / "input-verification.json").write_text(
    json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
print("Verified both original ZIPs, supplemental manifest, recording and exact candidate ELF.")
PY

stage=perf-selection
perf_path=
while IFS= read -r candidate; do
  test -x "$candidate" || continue
  printf 'Probing installed perf: %s\n' "$candidate" >> "$evidence/perf-probe.log"
  if timeout --signal=TERM --kill-after=2s 5s "$candidate" --version \
    > "$evidence/perf-version.txt" 2>> "$evidence/perf-probe.log" && \
    test "$(cat "$evidence/perf-version.txt")" = 'perf version 7.0.14'; then
    perf_path=$candidate
    break
  fi
done < <(
  command -v perf || true
  find /usr/lib -maxdepth 4 -type f -name perf -print 2>/dev/null | sort -V -r
)
test -n "$perf_path"
printf '%s\n' "$perf_path" > "$evidence/perf-selected-path.txt"

stage=recording-analysis
python3 -B - "$evidence" "$perf_path" <<'PY' > "$evidence/analysis.log" 2>&1
import datetime
import hashlib
import json
import pathlib
import re
import subprocess
import sys

evidence = pathlib.Path(sys.argv[1])
perf = sys.argv[2]
verification = json.loads((evidence / "input-verification.json").read_text(encoding="utf-8"))
paths = verification["analysisPaths"]


def run_command(name, command):
    stdout = evidence / (name + ".txt")
    stderr = evidence / (name + ".stderr.txt")
    receipt = {"command": command,
               "startedUtc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    with stdout.open("xb") as output, stderr.open("xb") as errors:
        result = subprocess.run(command, stdout=output, stderr=errors, check=False)
    receipt["exitCode"] = result.returncode
    receipt["finishedUtc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    receipt["outputs"] = []
    for path in (stdout, stderr):
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        receipt["outputs"].append({"path": path.name, "bytes": path.stat().st_size,
                                   "sha256": digest})
    (evidence / (name + ".receipt.json")).write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return result.returncode


def stack_coverage(name):
    header_pattern = re.compile(r"^\s*(\d+)/(\d+)\s+(\d+\.\d{9}):\s*$")
    frame_pattern = re.compile(r"^\s+[0-9a-fA-F]+\s+.+\s+\(.+\)\s*$")
    headers = []
    frame_counts = []
    unparsed_lines = 0
    with (evidence / (name + ".txt")).open(encoding="utf-8", errors="replace") as source:
        for line in source:
            header = header_pattern.fullmatch(line)
            if header:
                headers.append((int(header[1]), int(header[2]), header[3]))
                frame_counts.append(0)
            elif line.strip():
                if headers and frame_pattern.fullmatch(line):
                    frame_counts[-1] += 1
                else:
                    unparsed_lines += 1
    two_frame_samples = sum(count >= 2 for count in frame_counts)
    return {
        "path": name + ".txt",
        "headerCount": len(headers),
        "samplesWithAnyFrame": sum(count >= 1 for count in frame_counts),
        "samplesWithAtLeastTwoFrames": two_frame_samples,
        "twoFrameCoverageFraction": two_frame_samples / len(headers) if headers else 0,
        "observedTids": sorted({header[1] for header in headers}),
        "unparsedNonemptyLineCount": unparsed_lines,
    }, headers


notes_status = run_command("candidate-readelf-notes", [
    "timeout", "--signal=TERM", "--kill-after=5s", "15s", "readelf", "--notes", paths["elf"],
])
notes = (evidence / "candidate-readelf-notes.txt").read_text(encoding="utf-8")
build_ids = re.findall(r"Build ID:\s*([0-9a-f]+)", notes)
if notes_status != 0 or build_ids != [verification["candidateElf"]["buildId"]]:
    raise SystemExit("Actual candidate ELF Build ID verification failed")
script_status = run_command("stack-samples", [
    "timeout", "--signal=TERM", "--kill-after=5s", "120s", perf, "script",
    "--ns", "--max-stack=64", "--no-inline", "--symfs", paths["symfs"],
    "-i", paths["perfData"], "-F", "time,pid,tid,ip,sym,dso",
])
report_status = run_command("caller-all", [
    "timeout", "--signal=TERM", "--kill-after=5s", "90s", perf, "report",
    "--stdio", "--show-nr-samples", "--children", "--call-graph=graph,0.5,caller",
    "--max-stack=64", "--percent-limit=0.25", "--symfs", paths["symfs"],
    "-i", paths["perfData"],
])
thread_statuses = {}
for tid in (2993, 2994, 3055, 3056, 3090, 2974):
    thread_statuses[tid] = run_command("stack-samples-tid-" + str(tid), [
        "timeout", "--signal=TERM", "--kill-after=5s", "60s", perf, "script",
        "--ns", "--max-stack=64", "--no-inline", "--symfs", paths["symfs"],
        "-i", paths["perfData"], "-F", "time,pid,tid,ip,sym,dso", "--tid=" + str(tid),
    ])
all_coverage, input_headers = stack_coverage("stack-samples")
coverage = {
    "scope": "Saved recording call-chain coverage with independent single-TID decoders",
    "expectedRecordingHeaderCount": 4355,
    "recordingHeaderCountMatches": all_coverage["headerCount"] == 4355,
    "allSamples": all_coverage,
    "threadSamples": [],
    "commandExitCodes": {"stack-samples": script_status, "caller-all": report_status},
}
for tid, status in thread_statuses.items():
    name = "stack-samples-tid-" + str(tid)
    thread_coverage, headers = stack_coverage(name)
    filtered_headers = [header for header in input_headers if header[1] == tid]
    thread_coverage.update({
        "requestedTid": tid,
        "expectedInputHeaderCount": len(filtered_headers),
        "tidFilterMatches": bool(headers) and thread_coverage["observedTids"] == [tid],
        "inputFilteredHeadersMatch": bool(filtered_headers) and headers == filtered_headers,
    })
    coverage["threadSamples"].append(thread_coverage)
    coverage["commandExitCodes"][name] = status
coverage["passed"] = (
    coverage["recordingHeaderCountMatches"]
    and all_coverage["unparsedNonemptyLineCount"] == 0
    and (evidence / "caller-all.txt").stat().st_size > 0
    and all(status == 0 for status in coverage["commandExitCodes"].values())
    and all(row["tidFilterMatches"] and row["inputFilteredHeadersMatch"]
            and row["samplesWithAtLeastTwoFrames"] > 0
            and row["unparsedNonemptyLineCount"] == 0
            for row in coverage["threadSamples"])
)
(evidence / "coverage.json").write_text(
    json.dumps(coverage, indent=2) + "\n", encoding="utf-8")
if not coverage["passed"]:
    raise SystemExit("Saved-recording commands or call-chain coverage checks failed")
print("Saved-recording reports and six independent single-TID coverage checks completed.")
PY
stage=complete
