# Reuse GPU query workspace per CAGRA handle

Status: implementation preparation; no GPU measurement or score claim.

## Scope and authority

The user delegated contest implementation and routine design choices while offline. The parent explicitly authorized isolated AMD preparation, a source-grounded hypothesis and reproducible experiment, then one minimal implementation or necessary benchmark. Seekdb AI tasks 1/3 keep formal-submission priority. This design uses that delegation rather than introducing another manual design approval. No cloud resources, credentials, private evaluation assets, submissions or existing worktrees are changed.

The independent checkout starts at `634888d9b037153fa143d2c913ba4b25503e1d62`, matching the public V5 branch and the peeled `amd-cagra-contest-baseline-v5` tag. The working branch is `codex/amd-query-workspace-reuse`. The root AGENTS.md naming guidance was read. Git author identity is not configured; local preparation commits use an explicit per-command `Codex <codex@localhost>` agent identity without changing user settings. These commits are local, with no push or formal submission. The optional writing-plans skill is not installed; the concrete plan is recorded below.

## Source facts and hypothesis

`tools/hipvs_bridge/seekdb_cuvs_bridge.c` currently allocates query, neighbor and distance device buffers at each search (baseline lines 58-60), then frees them (83-85). The same handle is used repeatedly by scalar SQL queries. The adaptor serializes both scalar and batch work under `g_ob_cuvs_mu` through worker join (`ob_vsag_adaptor.cpp` baseline lines 273-290 and 2078-2091), but the C ABI itself documents no concurrency guarantee.

Hypothesis H1: avoiding repeated RMM allocation/free for requests that fit an existing per-handle workspace may reduce warm-query overhead. RMM may already cache allocations, so the latency benefit is unknown. The first query still allocates and no CAGRA graph construction work is removed; this is not a claim to improve the main 14-point build component.

Two alternatives were considered: increasing the 64 KiB snapshot transfer chunks could reduce host I/O calls but lacks profiling evidence; changing graph iterations would require GPU quality measurements before choosing safe values. Neither is included in this patch.

## Minimal design

- Add three device pointers and exact allocated byte capacities to the private handle. Both build and deserialize initialize an empty workspace.
- Reserve only when a requested tensor exceeds its capacity. Allocate a replacement before releasing the old allocation; allocation failure must return an error without using undersized storage. Requests that fit perform no RMM allocations/frees.
- Retain the existing bridge's best-effort device deallocation policy and add diagnostics for failures. A successfully allocated replacement becomes the active pointer after release is attempted; this does not promise recovery or leak-free teardown if the external deallocator itself fails.
- Keep all existing graph and search parameters, data types, CUDA copies and the fixed cuVS search call. Continue creating/destroying search parameters per call to avoid assuming the library never mutates them.
- Add a per-handle pthread mutex around workspace use and search, including output copies. Concurrent searches on one handle serialize. As before, callers must quiesce handle users before freeing it; concurrent destruction is not introduced as a new guarantee.
- Free retained workspace before destroying its cuVS resources, then destroy the mutex. Add `-pthread` inside the permitted bridge build script; preserve the official outer command and C ABI.
- Check positive shapes and byte-size arithmetic before reserving. Invalid requests return nonzero and do not search using wrapped or undersized buffers.

Retained workspace is `4*nq*dim + 8*nq*topk` bytes at the largest allocated sizes (on the required FP32/uint32 target), plus any temporary replacement during growth. This must be measured against both VRAM limits, not declared safe from arithmetic alone.

## Implementation and validation plan

1. Prepare a small host harness that compiles the real bridge source against isolated test-only cuVS/CUDA/pthread doubles. Verify copying, capacity reuse, growth/shrink, allocation failures, teardown, deserialize and concurrent calls. A reuse assertion should fail against the frozen bridge and pass after the change. This checks host resource logic only; it is not a GPU, ROCm ABI, Recall or formal-result test.
2. Implement only the private handle workspace, its lifecycle, serialization of searches and bridge script linkage. Review the diff against the frozen base. Do not tune algorithms, SQL, graph format or fixed-library behavior.
3. When the existing GPU environment is controllable, first back up the earlier task's logs and inspect its state. Then use the official fixed offline build on a separate AMD source tree and validate actual fixed-library linkage. Do not reuse the AI-task toolchain or binary.
4. Compare frozen and candidate versions on identical public/development data. Record first query separately and repeated Top-10/Top-100 single-query SQL throughput, full ID/distances/Recall, peak VRAM and persistence reload. Apply the same DDL-to-first-query boundary, clean-index/cache start and paired order to any end-to-end experiment. No phase-specific implementation, hidden data or batch API substitution is allowed.
5. Only official platform results prove hidden-data gates and points. Do not submit AMD ahead of the frozen AI-task queue, and do not publish an unmeasured speedup.

The existing TRACE-dependent GPU smoke is a development-path check, not the formal OFF-trace benchmark. Its exit status and all expected cases must be inspected; a partial summary is insufficient. The current persistence test covers only a fixed-shape single query and cannot validate workspace lifetime by itself.

## Acceptance boundaries

Local acceptance: a reviewable small diff and meaningful host tests, exact baseline/source hashes, no retained-workspace leaks in the test model, no changed graph/search settings, and explicit untested GPU items.

Remaining acceptance: real fixed-environment compilation, actual cuVS GPU execution, same-round Recall and query gates, resource limits, CPU VSAG basic behavior, reproducible CPU/GPU report and correctly bound formal submission. None is inferred from host tests.

References: `artifacts/project-continuation-20261031/AMD_OFFICIAL_REQUIREMENTS_REVIEW.md` and the complete official-text copies in the parent workspace; public baseline `https://github.com/zihaomu/seekdb/tree/amd-oceanbase-official-contest-base-v5`.
