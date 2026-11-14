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
- Graph parameters use 4 NN-Descent iterations, intermediate degree 256 and output degree 128. Search parameters explicitly select `MULTI_CTA`, width 4 and `max(1536, 4 * topk)` intermediate results.
- Allocation failures at each of the three growth positions return failure, leave the handle usable and balance all model allocations at teardown.
- An injected distance-copy failure returns an error after the ID copy, and the handle remains usable for a later successful search.
- Workspace-release failures before and after model deallocation are tested at query, neighbor and distance growth in 128 and 256 dimensions. They return failure before CAGRA, disable further search/serialization, and never retry the ambiguous pointer. Other known-owned buffers are each freed once. Model allocations left live by a failure before deallocation remain explicitly unknown; they are not counted as recovered GPU memory.
- Deserialize begins with empty workspace, and rejected snapshot dimensions clean up model resources.
- Invalid nonpositive shapes return before allocating or searching.
- A deterministic IDs-copyback gate proves a second same-handle search blocks on the mutex. Both output arrays are checked. The separate distance-copy tail is not independently gated.
- Build/load lifecycle diagnostics model the fixed SDK behavior in which successful lifecycle calls and cleanup clear thread-local error text. Ten failure points are each injected with a marker, null text and empty text. Captured stderr must retain the first stage-specific diagnostic before cleanup; all cases check a null handle, exact cleanup order and balanced host resources. Snapshot size/dimension API failures must not be labelled shape mismatches. Two genuine mismatch controls retain the original rejection behavior. These are host doubles, not real snapshot/GPU tests.
- Query H2D, IDs D2H and distances D2H copy errors retain their operation and actual injected runtime code/detail, including absent/empty detail. The bridge codes stay 13/16/18, completed-copy order stays unchanged, and later successful searches reuse the handle. The model injects failure before the failing copy writes; it does not guarantee real HIP failure leaves outputs untouched. Serialize likewise tests marker/null/empty SDK text, return 1, successful later use and normal teardown. Existing workspace-failure cases still reject a disabled handle.

The fixture returns an explicitly synthetic protocol, not nearest neighbors. Windows LLP64 has 32-bit `long`; the model deliberately uses `long*` tensor shapes to match the bridge arrays on this host. It does not validate real Linux DLPack layout or the Linux 64-bit `long` overflow boundary. The pthread double uses `std::mutex`, so this is not POSIX ABI validation. Concurrent destruction is outside the API contract; tests join callers before freeing.

Separate-handle concurrency, serialize/search concurrency, real snapshot files, actual GPU streams, real deallocator failure semantics and pthread initialization failures remain untested. The test does not establish Recall, peak VRAM, formal gates or a latency improvement.

The isolated `--release-failure-before` and `--release-failure-after` modes run only the new release-failure cases. The full `--require-reuse` suite includes both. Build the fixture against the previous bridge and these isolated modes fail because a release error returns success; the full frozen-comparison mode without arguments still runs the common plumbing checks.

`--lifecycle-diagnostics` runs only the 30 build/load diagnostic cases and two shape-mismatch controls; the full `--require-reuse` suite also includes them. The test uses an owned temporary file to capture stderr and restores the original stream before assertions. Empty or null SDK text requires the bridge's explicit unavailable-text message. This models diagnostic lifetime and cleanup behavior, not installed-SDK provenance or the formal worker.

`--copy-serialize-diagnostics` isolates nine runtime-copy diagnostic cases and three serialize cases. The full suite includes them. The production runtime helper uses `cudaError_t` and `cudaGetErrorString`, mapped by the fixed hipVS compatibility header to `hipError_t` and `hipGetErrorString`; the host declarations are test doubles only.

`--stream-ordering` runs the resource-stream regression. The fake asynchronous copy API only appends work to a per-resource FIFO. Search also appends work after asynchronous copying has selected that resource's stream. The fixture executes those copies and search operations when `cuvsStreamSync` drains the FIFO. A direct control checks that host and device buffers retain their old contents before Sync and receive the expected values after Sync.

The stream cases check nq 1/64/1 with Top10/100, growth and retained allocations, cold/warm StreamGet failure with return 20, Sync failure with return 21 and a disabled handle, preservation of earlier returns 10/13/14/15/16/18 when Sync also fails, search-parameter lifetime, and mutex coverage through pending IDs and distance copies. Teardown checks a pre-cleanup Sync, queued model frees, index/parameter destruction, a final Sync, and resource destruction. The full `--require-reuse` suite includes these cases.

Failed Sync controls record the queued work as unknown and discard the fixture's callbacks. This bookkeeping does not establish cancellation or completion of real device work. Failed teardown synchronization is tested as best effort. The post-cleanup failure case reports three unresolved model allocations before disposing fixture-owned backing storage. Successful host cleanup in other cases only describes this model.

For a red control, compile the fixture against the exact previous bridge at `441750e0fa911ba6a8dbc81553614c168331308c` and run `--stream-ordering`. The queue control must pass. The bridge contract check must then fail with resource StreamGet, Sync and asynchronous-copy counts all zero. Retain this failure log separately from the candidate log. The synchronous `cudaMemcpy` double remains available for that previous source and frozen V5 comparisons.

## Frozen comparison

The frozen V5 bridge at commit `634888d9b037153fa143d2c913ba4b25503e1d62` has SHA256 `296fafff4bd8d86ff2222840d44a23f56ba687acd1393879c8a0e922e7620b3e`. Supply `--frozen-parameters` as the fourth argument to `build-host.cmd` when compiling that source or a previous 100-iteration, `SINGLE_CTA` source. Use an empty third argument for the default Visual Studio path. This selects the frozen parameter expectations in the fixture. Run without arguments for common plumbing checks. `--require-reuse` must fail specifically because its second same-size query performs three further allocations. Retain the actual failure output.

The candidate must compile and run the full `--require-reuse` suite with exit zero. Retain the exact source/header/fixture hashes, compiler output and full test log. GPU acceptance still requires the official fixed offline build, real fixed-library GPU execution, persistence/Recall/resource checks and paired SQL measurements described in the design document.
