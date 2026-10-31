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
- Deserialize begins with empty workspace, and rejected snapshot dimensions clean up model resources.
- Invalid nonpositive shapes return before allocating or searching.
- A deterministic IDs-copyback gate proves a second same-handle search blocks on the mutex. Both output arrays are checked. The separate distance-copy tail is not independently gated.

The fixture returns an explicitly synthetic protocol, not nearest neighbors. Windows LLP64 has 32-bit `long`; the model deliberately uses `long*` tensor shapes to match the bridge arrays on this host. It does not validate real Linux DLPack layout or the Linux 64-bit `long` overflow boundary. The pthread double uses `std::mutex`, so this is not POSIX ABI validation. Concurrent destruction is outside the API contract; tests join callers before freeing.

Separate-handle concurrency, serialize/search concurrency, real snapshot files, actual GPU streams, external deallocator failures and pthread initialization failures remain untested. The test does not establish Recall, peak VRAM, formal gates or a latency improvement.

## Frozen comparison

The frozen V5 bridge at commit `634888d9b037153fa143d2c913ba4b25503e1d62` has SHA256 `296fafff4bd8d86ff2222840d44a23f56ba687acd1393879c8a0e922e7620b3e`. Compile that exact source with this fixture and run without arguments for common plumbing checks. `--require-reuse` must fail specifically because its second same-size query performs three further allocations; any build failure or unrelated runtime failure is not an accepted red result.

The candidate must compile and run the full `--require-reuse` suite with exit zero. Retain the exact source/header/fixture hashes, compiler output and full test log. GPU acceptance still requires the official fixed offline build, real fixed-library GPU execution, persistence/Recall/resource checks and paired SQL measurements described in the design document.
