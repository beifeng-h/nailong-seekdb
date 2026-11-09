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
python3 tests/development_protocol_test.py --driver build/minivectordb_development_server \
  --report development-pipes.json
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

## Development process protocol

The executable requires `--development-protocol mvdb-dev-jsonl-v1 --data DIRECTORY`.
This is an explicitly versioned development contract. Official process
compatibility remains pending the organizer's driver.

Input is UTF-8 with one complete SQL statement per line. Empty lines are
ignored. `@mode exact` and `@mode ann` change the session mode. EOF and `@quit`
perform normal shutdown. The caller waits for process exit before reopening
the same directory. Destruction rolls back any uncommitted transaction.

Every stdout line is one JSON object containing `protocol: "mvdb-dev-jsonl-v1"`
and an `event` field. Output is flushed after every frame. Diagnostics go to
stderr. The process opens no network listeners.

| Event | Fields and meaning |
| --- | --- |
| ready | `formal_ready: false`, emitted after storage recovery completes |
| columns | Ordered `columns` array before query rows |
| row | One `values` array, streamed from the execution sink |
| complete | `is_query`, `affected_rows`, `emitted_rows`, terminates one successful SQL request |
| error | `kind: "sql"`, `message` and internal `category`, terminates one failed SQL request |
| mode | `mode`, acknowledges a valid development mode command |
| error for a command | `kind: "command"` and `message`, leaves the process running |

The development SQL categories are 0 for syntax, 1 for binding and 2 for
execution. These numbers describe the implementation's internal enum. Official
error-code mapping remains pending. If execution fails after some rows have
been emitted, the client discards those frames when it receives `error`.
The executable retains the current SQL input line and serializes each output
row directly. It does not accumulate the query result set. JSON integers
preserve BIGINT values and vector cells contain the stored float32 values.

Floating cells use `std::to_chars` with the callers' scientific precision of 17
fractional digits and a fixed 64-byte formatting buffer. The byte regression
checks finite double and float32 samples, signed zero, subnormals, numeric
extremes and BIGINT bounds against the previous classic-locale formatting.

The transport and command handling are isolated in `development_protocol.cpp`.
The SQL engine exposes its existing sink interface and explicit mode setter.
The file-capture test driver shares the same scalar and vector serialization.
Windows uses wide command-line arguments and binary standard streams for
Unicode paths and UTF-8 pipe data.

## Docker validation

```sh
docker build --platform linux/amd64 --target development-checks -t minivectordb-development .
mkdir -p demo-data
printf '%s\n' \
  'CREATE TABLE docs(id BIGINT PRIMARY KEY, content VARCHAR(16));' \
  "INSERT INTO docs VALUES(1, 'saved');" \
  'BEGIN;' 'DELETE FROM docs;' 'ROLLBACK;' \
  'SELECT * FROM docs;' '@quit' \
  | docker run --rm -i --network none \
      --mount "type=bind,source=$PWD/demo-data,target=/data" minivectordb-development
printf '%s\n' 'SELECT * FROM docs;' \
  | docker run --rm -i --network none \
      --mount "type=bind,source=$PWD/demo-data,target=/data" minivectordb-development
```

The Dockerfile pins the Ubuntu 24.04 amd64 base-image digest. It bootstraps
`ca-certificates=20240203` through the signed Ubuntu noble release repository,
then installs build tools from the HTTPS `20261109T000000Z` Ubuntu package
snapshot. APT archive signatures and HTTPS certificate verification remain
enabled. The `development-checks` stage runs
the seven CTest programs, all 20 public fixture groups and the legacy schema
fixture. Installed package versions are saved inside the image at
`/build/installed-packages.tsv`.

The frozen M9 Actions workflow builds the default image from an explicit Git
archive of source, tests and build files. It then runs the 95 public SQL
statements through the default database entry with real container pipes,
including process restarts and mode changes. It checks 1200 streamed ordered
rows, both shutdown paths, unknown protocol selection, a read-only `/data`
mount, the PID 1 executable, open file locations and an empty socket table in
the container's `none` network namespace. The image exists on the temporary
runner during validation.

File reads use native positional operations. The file I/O tests cover short
reads, errors, immediate reads after unbuffered writes, offsets beyond 4 GiB
and interrupted reads on Linux. The frozen M8 v4 workflow writes 100,000 indexed
128-dimensional records, reopens them and checks two Top10 groups against
independent periodic-key truth, followed by a streamed full-K query. Each
database process has a 32 MiB RLIMIT_AS cap, below the logical record size.
This cap excludes filesystem page-cache memory. The enclosing development
container has a separate 2 GiB memory limit. Formal resource scoring remains
unverified.

The default image starts `/build/minivectordb_development_server` with the
explicit development protocol and `/data`. Its `formal-ready` label remains
`false`. That M9 workflow builds without `--target`, inspects the actual final
configuration, and exercises default startup without overriding the entrypoint.

## Numeric serialization comparison

The M10 workflow uses the runner's installed CMake and C++ compiler to build
frozen M9 and the candidate with matching Release options. It runs the focused
byte regression and process-contract test, followed by a same-program A/A
control and three A/B/B/A blocks. This workflow uses direct processes and the
existing development protocol.

`tests/serialization_comparison.py` creates one fixed 10,000-row, 128-dimensional
database, then separately measures `SELECT id` and `SELECT id, v`. It records
external process wall time with stdout redirected to a file. Each result is
checked outside the timing window against independently generated quarter-integer
vectors, and all output bytes must match the frozen baseline. It also checks
that query runs preserve the database file. Operating-system cache is retained.
Results apply to these projection workloads and the measured environment.
Filtered TopK scoring, physical device I/O and formal resource acceptance
require their own measurements.

## Integration status

The contest stdin/stdout process contract and full driver remain pending.
The current Docker stages and process tests validate the development contract.
Official framing, results, readiness, mode commands, shutdown, error codes and
resource acceptance still require the frozen protocol and container requirements.
