# MiniVectorDB development build

This C++20 implementation provides disk-backed tables, BIGINT primary keys,
Unicode VARCHAR values, float32 vectors, exact filtered L2 TopK, persistent flat
vector indexes, transactions and redo recovery. SQL execution and query results use the same transaction
visibility as ordinary record access.

The public SQL fixtures in `mysqltest/` are unchanged from the official
`2026-competition-round2` commit
`9f6df2ba5acf871376e22b64a3d0724e4b575044` (public v4, 20 groups and 95 SQL statements).
[Official fixture description](https://github.com/oceanbase/seekdb/blob/9f6df2ba5acf871376e22b64a3d0724e4b575044/mysqltest/README.md)

## Build and check

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel 2
ctest --test-dir build --output-on-failure
python3 tests/public_samples.py build/sql_public_case_driver --report public-samples.json
python3 tests/legacy_schema.py build/sql_public_case_driver --report legacy-schema.json
python3 tests/legacy_schema.py build/sql_public_case_driver --vector-index --report legacy-index-schema.json
python3 tests/crash_recovery.py build/sql_fault_driver --sort-cleanup \
  --commit-returned-stage session.commit_returned --report sql-index-crash.json
python3 tests/index_build_recovery.py build/sql_fault_driver --report index-build-crash.json
```

The public-sample runner waits for the capture process to exit at every
`--restart`, then opens the same database directory in a new process. It sends
`--mode ann` to the SQL session's explicit search-mode setter. SQL errors are
captured and subsequent statements continue. Until the official semantic error
mapping is frozen, expected-error assertions check a nonempty internal error
message and its category. Capture JSON is a development test artifact.

## Vector index

`CREATE VECTOR INDEX docs_vec ON docs(embedding)` creates an auxiliary disk
table with one live primary-key, record-pointer and float32-vector entry per
record. The owner catalog stores its persistent pointer in the previously
unused table-header field at byte 56. Older tables read this field as zero.
Index pages, row pages and catalog changes share the existing page transaction,
statement savepoint, free-page list and redo WAL. Insert, update and delete
maintain the index immediately, including changes to ordinary columns that
replace a record page. The auxiliary table stays outside the user catalog.
Each table supports one index on its VECTOR column.

`SqlSession::set_search_mode(SearchMode::ann)` selects exhaustive search of the
disk flat index for distance-ordered queries. Distance scans in this mode
require an index. Primary-key equality uses direct record lookup in either
mode. This implementation computes exact
distances over all eligible entries. Approximate candidate pruning and
sublinear ANN acceleration remain unimplemented. `exact` mode scans the owner
table. Both paths filter candidates before TopK, break ties by ascending
primary key, retain a bounded candidate heap, and use external sorting for K
above the working budget or descending distance queries.

The index regressions compare IDs, full rows and distances against independent
expected rows, including filters, ties, cancellation, small working budgets,
failed statements, rollback, restart, chained vectors, directory growth and
page reuse. The SQL crash driver checks both exact and index queries after
forced termination. The index-build crash test also checks the atomic
publication of a newly built index and record changes.

## Docker validation

```sh
docker build --platform linux/amd64 --target development-checks -t minivectordb-development .
```

The Dockerfile pins the Ubuntu 24.04 amd64 base-image digest. It bootstraps
`ca-certificates=20240203` through the signed Ubuntu noble release repository,
then installs build tools from the HTTPS `20261109T000000Z` Ubuntu package
snapshot. APT archive signatures and HTTPS certificate verification remain
enabled. The `development-checks` stage runs
the six CTest programs, all 20 public fixture groups and the legacy schema
fixture. Installed package versions are saved inside the image at
`/build/installed-packages.tsv`.

The independent Actions workflow builds from an explicit Git archive of source,
tests and build files. It then runs process-kill recovery checks in a
network-disabled container. The image exists on the temporary runner during
validation.

File reads use native positional operations. The file I/O tests cover short
reads, errors, immediate reads after unbuffered writes, offsets beyond 4 GiB
and interrupted reads on Linux. The v4 workflow also writes 100,000 indexed
128-dimensional records, reopens them and checks two Top10 groups against
independent periodic-key truth, followed by a streamed full-K query. Each
database process has a 32 MiB RLIMIT_AS cap, below the logical record size.
This cap excludes filesystem page-cache memory. The enclosing development
container has a separate 2 GiB memory limit. Formal resource scoring remains
unverified.

The workflow also builds the default image without `--target` using its local
job cache, prints the final image's entrypoint, command, working directory and
labels, then records a closed-input startup and exit. Both Docker builds select
the development-checks stage. The Dockerfile currently supplies no database
`ENTRYPOINT` or `CMD`. The default image therefore inherits the base image's
command. The startup check records that development configuration.

## Integration status

The contest stdin/stdout process contract and full driver remain pending.
The current Docker stages validate the development implementation. A contest
database startup entry and official acceptance still require the frozen
protocol and container requirements.
