# Workspace host-logic tests

This Windows/MSVC-only model compiles the real bridge source as C++17 against test-only cuVS, CUDA and pthread doubles. It checks host resource control, not ROCm compatibility, actual GPU behavior, ANN quality or speed. None of this include directory is used by the production build script.

Run from the repository root (the third argument optionally supplies another installed Visual Studio `VsDevCmd.bat`):

```powershell
$out = Join-Path $env:TEMP 'seekdb-cuvs-host-candidate'
& tools/hipvs_bridge/tests/host/build-host.cmd tools/hipvs_bridge/seekdb_cuvs_bridge.c $out
if ($LASTEXITCODE -ne 0) { throw 'host build failed' }
& "$out/host_test.exe" --require-reuse
if ($LASTEXITCODE -ne 0) { throw 'host tests failed' }
```

Use a fresh output directory for each source version. `build-host.cmd` creates a unique Temp directory when its second argument is omitted. No binary or object belongs in this source directory.

## What is checked

- Repeated requests that fit an existing workspace perform zero further RMM allocations; growth and shrink preserve query/ID/distance copying, including null distance output.
- Graph and search parameter values remain those of the frozen source in this model.
- Allocation failures at each of the three growth positions return failure, leave the handle usable and balance all model allocations at teardown.
- An injected distance-copy failure returns an error after the ID copy, and the handle remains usable for a later successful search.
- Workspace-release failures before and after model deallocation are tested at query, neighbor and distance growth in 128 and 256 dimensions. They return failure before CAGRA, disable further search/serialization, and never retry the ambiguous pointer. Other known-owned buffers are each freed once. Model allocations left live by a failure before deallocation remain explicitly unknown; they are not counted as recovered GPU memory.
- Deserialize begins with empty workspace, and rejected snapshot dimensions clean up model resources.
- Invalid nonpositive shapes return before allocating or searching.
- A deterministic IDs-copyback gate proves a second same-handle search blocks on the mutex. Both output arrays are checked. The separate distance-copy tail is not independently gated.
- Build/load lifecycle diagnostics model the fixed SDK behavior in which successful lifecycle calls and cleanup clear thread-local error text. Ten failure points are each injected with a marker, null text and empty text. Captured stderr must retain the first stage-specific diagnostic before cleanup; all cases check a null handle, exact cleanup order and balanced host resources. Snapshot size/dimension API failures must not be labelled shape mismatches. Two genuine mismatch controls retain the original rejection behavior. These are host doubles, not real snapshot/GPU tests.

The fixture returns an explicitly synthetic protocol, not nearest neighbors. Windows LLP64 has 32-bit `long`; the model deliberately uses `long*` tensor shapes to match the bridge arrays on this host. It does not validate real Linux DLPack layout or the Linux 64-bit `long` overflow boundary. The pthread double uses `std::mutex`, so this is not POSIX ABI validation. Concurrent destruction is outside the API contract; tests join callers before freeing.

Separate-handle concurrency, serialize/search concurrency, real snapshot files, actual GPU streams, real deallocator failure semantics and pthread initialization failures remain untested. The test does not establish Recall, peak VRAM, formal gates or a latency improvement.

The isolated `--release-failure-before` and `--release-failure-after` modes run only the new release-failure cases. The full `--require-reuse` suite includes both. Build the fixture against the previous bridge and these isolated modes fail because a release error returns success; the full frozen-comparison mode without arguments still runs the common plumbing checks.

`--lifecycle-diagnostics` runs only the 30 build/load diagnostic cases and two shape-mismatch controls; the full `--require-reuse` suite also includes them. The test uses an owned temporary file to capture stderr and restores the original stream before assertions. Empty or null SDK text requires the bridge's explicit unavailable-text message. This models diagnostic lifetime and cleanup behavior, not installed-SDK provenance or the formal worker.

## Frozen comparison

The frozen V5 bridge at commit `634888d9b037153fa143d2c913ba4b25503e1d62` has SHA256 `296fafff4bd8d86ff2222840d44a23f56ba687acd1393879c8a0e922e7620b3e`. Compile that exact source with this fixture and run without arguments for common plumbing checks. `--require-reuse` must fail specifically because its second same-size query performs three further allocations; any build failure or unrelated runtime failure is not an accepted red result.

The candidate must compile and run the full `--require-reuse` suite with exit zero. Retain the exact source/header/fixture hashes, compiler output and full test log. GPU acceptance still requires the official fixed offline build, real fixed-library GPU execution, persistence/Recall/resource checks and paired SQL measurements described in the design document.
