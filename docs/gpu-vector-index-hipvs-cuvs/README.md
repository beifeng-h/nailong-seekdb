# hipVS/cuVS GPU vector backend

Status: **V5 source integration with a query-workspace reuse candidate** for AMD
`gfx1100` with ROCm 7.2.4. Candidate GPU compilation, persistence/reload acceptance
and paired performance measurements remain pending. This directory documents
the integration and its validation assets.

## Scope

The backend adds optional hipVS/cuVS acceleration behind seekdb's existing
vector adaptor. It does not replace VSAG globally.

- GPU support is compiled only with `OB_BUILD_CUVS=ON`; the default is `OFF`.
- A dense L2 HNSW/HGRAPH index opts in with `WITH (lib=cuvs)`.
- `lib=vsag` indexes keep their existing CPU behavior.
- Cosine and inner-product metrics are rejected for `lib=cuvs` at DDL time.
- Filters, deletes, stale GPU indexes, and unavailable GPUs fall back to VSAG.
- `dbms_vector.batch_knn` exposes one-call batched GPU ANN for explicit batch
  workloads.

The product integration lives in:

- [`ob_vsag_adaptor.cpp`](../../src/oblib/lib/vector/ob_vsag_adaptor.cpp):
  per-index marking, buffered vectors, CAGRA build/search, graph/ID/vector
  snapshot serialization and reload, freshness fallback, trace points, and
  batch entry points.
- [`ob_plugin_vector_index_adaptor.cpp`](../../src/observer/vector_index/ob_plugin_vector_index_adaptor.cpp):
  maps declarative `lib=cuvs` indexes to the adaptor handle.
- [`ob_vector_index_util.cpp`](../../src/observer/vector_index/ob_vector_index_util.cpp):
  validates the L2-only DDL contract.
- [`ob_dbms_vector_mysql.cpp`](../../src/pl/sys_package/ob_dbms_vector_mysql.cpp):
  implements `dbms_vector.batch_knn`.

## Bridge

seekdb links a small C ABI bridge instead of importing hipVS/cuVS C++ types into
the server. The maintained bridge source and build entry point are under
[`tools/hipvs_bridge`](../../tools/hipvs_bridge/).

The contest uses the fixed offline build below. `/work/bridge` replaces the
protocol's output-directory placeholder; keep the CMake options unchanged.
The frozen environment is ROCm 7.2.4, GCC 13.3 and CMake 3.31; the V5 source pins
Rust 1.97.1 and uses the organizer's frozen dependencies and offline vendor.

```bash
HIPVS_ROOT=/opt/hipvs ROCM_ROOT=/opt/rocm \
  tools/hipvs_bridge/build.sh /work/bridge/libseekdb_cuvs_bridge.so

cmake -S . -B build_release -G "Unix Makefiles" \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DOB_ENABLE_UNITY=ON \
  -DOB_USE_LLD=ON \
  -DOB_BUILD_CUVS=ON \
  -DOB_BUILD_CUVS_TRACE=OFF \
  -DCUVS_BRIDGE_LIB=/work/bridge/libseekdb_cuvs_bridge.so
cmake --build build_release --target seekdb
```

The bridge exports:

```text
seekdb_cuvs_build
seekdb_cuvs_search
seekdb_cuvs_free
seekdb_cuvs_serialize
seekdb_cuvs_deserialize
seekdb_cuvs_cagra_knn
```

For a CPU-only development build, leave the defaults or pass:

```bash
./build.sh release \
  -DOB_BUILD_CUVS=OFF \
  -DOB_BUILD_CUVS_TRACE=OFF \
  --make -j64
```

## SQL usage

Per-index GPU opt-in:

```sql
CREATE TABLE items(
  id BIGINT PRIMARY KEY,
  embedding VECTOR(128),
  VECTOR INDEX idx_embedding(embedding)
    WITH (distance=l2, type=hnsw, lib=cuvs)
);
```

Explicit batch ANN writes results to a caller-created output table:

```sql
CALL dbms_vector.batch_knn(
  "items",
  "query_vectors",
  10,
  "batch_results"
);
```

See [`examples/batch_knn.sql`](examples/batch_knn.sql) for the table contract.

## Validation

The self-contained development GPU smoke runner generates deterministic vectors
and ground truth; it does not use private `/work/datasets` or `/work/bench`
assets. It requires a trace build, unlike the formal `TRACE=OFF` build above:

```bash
LD_LIBRARY_PATH=/work/bridge:/opt/hipvs/lib:/opt/rocm/lib \
  docs/gpu-vector-index-hipvs-cuvs/validation/run_gpu_smoke.sh \
    --observer /path/to/trace-build/seekdb \
    --bridge /work/bridge/libseekdb_cuvs_bridge.so \
    --base-dir /tmp/seekdb-cuvs-smoke \
    --port 2981 \
    --render /dev/dri/renderD135 \
    --evidence /tmp/seekdb-cuvs-evidence
```

The runner validates:

- cuVS L2 build/serve and same-process VSAG isolation;
- non-L2 DDL rejection;
- filter, delete, and freshness fallback;
- threshold-triggered CAGRA rebuild;
- one-call `batch_knn`, row cardinality, and recall for Top-10 and Top-100;
- GPU device file descriptors and hidden-GPU fallback;
- bounded observer cleanup and closed ports.

The runner writes a machine-readable `summary.json` into its evidence directory.
Its smoke recall threshold and batched queries do not implement the formal
Top-10/Top-100 recall, single-query SQL, resource or paired timing protocol.
The separate [DDL persistence runner](validation/run_ddl_persistence_smoke.sh)
and [bridge persistence test](../../tools/hipvs_bridge/test_persistence.cpp)
exercise snapshot paths; their presence is not a passing candidate GPU result.
Results for a specific candidate belong in that pull request's description or a
validation comment rather than in the source tree.
The no-GPU DDL contract remains covered by
[`vector_index_cuvs_ddl.test`](../../tools/deploy/mysql_test/test_suite/vector_index/t/vector_index_cuvs_ddl.test).

## Current limitations

- Dense float32 L2 indexes are supported.
- V5 implements graph, ID and vector snapshots and GPU graph reload; candidate
  persistence/reload acceptance still needs the actual fixed GPU environment.
- Query workspaces are retained per handle under a mutex. This candidate does
  not optimize the first allocation or graph construction, and has no measured
  GPU speedup yet.
- The explicit batch API rebuilds its CAGRA index per call.
- GPU validation is manual because upstream CI has no compatible GPU runner.
- Multi-GPU and long-duration coverage remain pending. Host test doubles do not
  establish real snapshot-file, GPU stream or Linux ABI behavior.
