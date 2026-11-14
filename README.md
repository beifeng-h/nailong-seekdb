# MiniVectorDB

This C++20 implementation provides disk-backed tables, BIGINT primary keys,
Unicode VARCHAR values, float32 vectors, exact filtered L2 TopK, persistent flat
vector indexes, transactions and redo recovery. SQL execution and query results use the same transaction
visibility as ordinary record access.

The public SQL fixtures in `mysqltest/` are unchanged from the official
`2026-competition-round2` commit
`f4d4e26653af823a7601668802f80e374ad6643c` (public v4, 20 groups and 95 SQL statements).
The same commit supplies the unchanged `STDIO_PROTOCOL.md` and `protocol_check/` driver.
[Official fixture description](https://github.com/oceanbase/seekdb/blob/f4d4e26653af823a7601668802f80e374ad6643c/mysqltest/README.md),
[official process contract](https://github.com/oceanbase/seekdb/blob/f4d4e26653af823a7601668802f80e374ad6643c/STDIO_PROTOCOL.md)

## Build and check

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel 2
ctest --test-dir build --output-on-failure
python3 protocol_check/run.py \
  --command '["build/minivectordb_server","--data","{data_dir}"]' \
  --output protocol-results/public
python3 tests/stdio_protocol_test.py --driver build/minivectordb_server --report stdio-native.json
python3 tests/public_samples.py build/sql_public_case_driver --report public-samples.json
python3 tests/legacy_schema.py build/sql_public_case_driver --report legacy-schema.json
python3 tests/legacy_schema.py build/sql_public_case_driver --vector-index --report legacy-index-schema.json
python3 tests/crash_recovery.py build/sql_fault_driver --sort-cleanup \
  --commit-returned-stage session.commit_returned --report sql-index-crash.json
python3 tests/index_build_recovery.py build/sql_fault_driver --report index-build-crash.json
python3 tests/development_protocol_test.py --driver build/minivectordb_development_server \
  --report development-pipes.json
python3 tests/public_protocol_check.py \
  --driver build/minivectordb_development_server \
  --report public-protocol-report.json
```

The public-sample runner waits for the capture process to exit at every
`--restart`, then opens the same database directory in a new process. It sends
`--mode ann` to the SQL session's explicit search-mode setter. SQL errors are
captured and subsequent statements continue. Until the official semantic error
mapping is frozen, expected-error assertions check a nonempty internal error
message and its category. Capture JSON is a development test artifact.

The public protocol check drives the executable over the published JSONL
contract and compares the responses with the unchanged public fixtures. It
uses the assertion semantics of `protocol_check/_assertions.py`. The
organizer's own `protocol_check/run.py` checks the same fixtures against a
built image on POSIX hosts.

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

## Existing judge protocol interface

`minivectordb_development_server` accepts
`--development-protocol mvdb-judge-json-v1 --data DIRECTORY` for existing callers.
Its session API, semantic error names and regression tests are retained.
The `development-checks` Docker target starts this interface.

After storage recovery the process writes one line and flushes it before it
reads any request:

```json
{"type":"ready"}
```

Each stdin line is one JSON request and produces exactly one stdout line.
The existing protocol tests cover integer and string identifiers and boolean `ok`.

| Request | Response |
| --- | --- |
| `{"id":N,"op":"execute","sql":"...","search_mode":"exact"}` on a query | `{"id":N,"ok":true,"columns":[...],"rows":[...]}` |
| `{"id":N,"op":"execute","sql":"...","search_mode":"exact"}` on INSERT, UPDATE or DELETE | `{"id":N,"ok":true,"affected_rows":N}` |
| `{"id":N,"op":"execute",...}` on table or transaction control | `{"id":N,"ok":true}` |
| `{"id":N,"op":"execute",...}` that fails | `{"id":N,"ok":false,"error":{"code":"...","message":"..."}}` |
| `{"id":N,"op":"shutdown"}` | `{"id":N,"ok":true}` and exit code 0 |

`search_mode` selects `exact` or `ann` for that request. `columns` keeps the
projection order, `rows` is a two-dimensional array and an empty result still
reports its column names. BIGINT cells are JSON integers, vector cells are
JSON arrays of finite float32 values, and the process never writes NaN or
Infinity. A failed statement answers with its error and never terminates the
process, so later requests are still served.

The framing follows the published rules: one complete JSON object per line,
terminated by a newline, flushed after every response, with all diagnostics on
stderr. After the shutdown response the process writes nothing more and exits
with code 0. The data directory is the `--data` argument, `/data` in the
container.

`src/judge_protocol.cpp` holds the framing, the restricted JSON reader for
the request envelope and the response writer. The SQL engine is unchanged:
the protocol classifies the statement with `parse_sql` and reuses the
existing sink, so the published statement semantics and the development
protocol stay identical.

## Development process protocol

The default container entry is described in
[Standard-input/output protocol](#standard-inputoutput-protocol).
The development framing in this section supports the team's original regressions.

The executable requires `--development-protocol mvdb-dev-jsonl-v1 --data DIRECTORY`.
This is an explicitly versioned development contract for the team's regression suite.

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
execution. These numbers describe the implementation's internal enum and are
not the contest error codes. If execution fails after some rows have
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

## Standard-input/output protocol

`minivectordb_server --data DIRECTORY` implements the official JSONL contract.
The default directory is `/data`. After recovery it actively sends the exact
`{"type":"ready"}` object. Each `execute` request supplies `id`, `sql`, and
`search_mode` and receives one response with the same JSON identifier and a
boolean `ok`. BIGINT values retain signed 64-bit precision. Query responses
contain ordered `columns` and `rows`, and modification responses contain
`affected_rows`.

The validated request identifier is echoed from its original JSON span,
including integer identifiers beyond uint64 and nested identifier values.
Duplicate object keys are rejected before SQL execution. Identifier extraction
uses the validated envelope and preserves JSON string escapes and number spelling.

Query rows are serialized into a bounded 64 KiB buffer. Larger responses spill
incrementally to the reserved `/data/.minivectordb-response.tmp` workspace.
Successful execution publishes the completed JSON response, using the same
buffer to copy spilled data. Failed execution discards the buffered rows and
returns an error response. The workspace is removed after the request and on
recovery. SQL parsing, transaction visibility, and numeric serialization reuse
the existing engine.

Exact vector scans reuse a record buffer and a decoded row throughout each
scan. Page files retain their opened operating-system handle and current
length. Writes update the length using the number of bytes transferred, and
successful truncations set the new length. Each file has a single owning
writer, and reopening reads its current length from the filesystem.

Record reads request a schema-bounded page prefix: eight bytes per BIGINT,
four bytes for a VARCHAR length plus at most four bytes per declared code point,
and four bytes per vector component, with a 16-byte page header. The prefix is
capped at the existing 4096-byte page size. Multi-page records retain full-page
reads, and a stored payload length cannot exceed the bytes fetched. Prefix reads
use the same transaction overlay as full-page reads; the unread suffix is zero.
Ordinary scans and flat-index scans reuse their row and record buffers.

The transaction dirty bitmap uses two 4096-byte windows. Windows write back on
eviction and before commit enumerates dirty pages. Savepoint rollback updates
the same windows, and transaction cleanup discards them. The cache has a fixed
8 KiB payload regardless of transaction size. Native positional writes avoid
an extra stream seek, complete short writes, retry POSIX EINTR, and track the
actual extent even when an error follows a partially completed write. The WAL
format, synchronization order and commit acknowledgement remain unchanged.

`shutdown` closes the database, rolls back uncommitted changes, acknowledges
the request, flushes, and exits with code zero. EOF also closes the database.
Expected SQL errors and invalid requests return nonempty string codes and
leave the process available for subsequent requests. The published
`error_codes` configuration is empty, so this adapter uses `syntax_error`,
`binding_error`, `execution_error`, and `invalid_request`. Error diagnostics
containing invalid UTF-8 bytes use replacement characters in the JSON response.

JSON parsing uses the vendored nlohmann/json 3.12.0 single header under its MIT
license in `third_party/nlohmann/`. The runtime uses local files and standard
streams. The official driver uses POSIX pipe selectors. Windows native checks
use reader threads and the unchanged official result assertions.

## Docker validation

```sh
docker build --platform linux/amd64 --target development-checks -t minivectordb-development .
mkdir -p demo-data
printf '%s\n' \
  '{"id":1,"op":"execute","sql":"CREATE TABLE docs(id BIGINT PRIMARY KEY, content VARCHAR(16));","search_mode":"exact"}' \
  '{"id":2,"op":"execute","sql":"INSERT INTO docs VALUES(1, '"'"'saved'"'"');","search_mode":"exact"}' \
  '{"id":3,"op":"execute","sql":"BEGIN;","search_mode":"exact"}' \
  '{"id":4,"op":"execute","sql":"DELETE FROM docs;","search_mode":"exact"}' \
  '{"id":5,"op":"execute","sql":"ROLLBACK;","search_mode":"exact"}' \
  '{"id":6,"op":"execute","sql":"SELECT * FROM docs;","search_mode":"exact"}' \
  '{"id":7,"op":"shutdown"}' \
  | docker run --rm -i --network none \
      --mount "type=bind,source=$PWD/demo-data,target=/data" minivectordb-development
printf '%s\n' \
  '{"id":1,"op":"execute","sql":"SELECT * FROM docs;","search_mode":"exact"}' \
  '{"id":2,"op":"shutdown"}' \
  | docker run --rm -i --network none \
      --mount "type=bind,source=$PWD/demo-data,target=/data" minivectordb-development
```

The organizer's own check runs against the same image:

```sh
python3 protocol_check/run.py --image minivectordb-development \
  --handshake-only --output "protocol-results/handshake-$(date +%Y%m%d-%H%M%S)"
python3 protocol_check/run.py --image minivectordb-development \
  --output "protocol-results/public-$(date +%Y%m%d-%H%M%S)"
```

The Dockerfile pins the Ubuntu 24.04 amd64 base-image digest. It bootstraps
`ca-certificates=20240203` through the signed Ubuntu noble release repository,
then installs the 173 exact binary-package versions recorded in
`ubuntu-packages.lock` from the official HTTPS Ubuntu snapshot dated
`20261109T000000Z`, also used by the passed d8 build. The snapshot preserves
versions that can leave the current archive index. APT retries transient fetch
failures three times. The lock file is a byte-for-byte copy of the complete package
list from the successful `029b884bd` Linux image. APT verifies archive
signatures and HTTPS certificates. After compiling, the image saves
`dpkg-query -W` output to `/build/installed-packages.tsv` and requires an exact
match with the lock file, including package names and versions.
The `development-checks` stage runs
the eight CTest programs, all 20 public fixture groups, the legacy schema
fixture and the public protocol check over the contest framing.

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

The default image starts `/build/minivectordb_server --data /data`. The
`development-checks` target retains `mvdb-judge-json-v1` and its existing checks.
The workflow builds the default image from a source whitelist, runs native
edge checks, and runs the unchanged official handshake and all public cases
against the default entrypoint. The official driver applies its published
2 CPU, 2 GiB and 128 PID limits, a read-only root filesystem, a writable
data volume, dropped capabilities, and disabled networking. Its response
limit is 16 MiB. The `formal-ready` label remains `false` pending formal admission.

The workflow checks `agent/conversation.json` from the checked-out commit before
building the image. It saves the material byte length and SHA256 in
`learning-material-input.json`. Run `python3 tests/learning_material_check.py`
to perform the same input check locally. The platform performs the Agent assessment.

Record decoding reserves the schema's column count before appending values.
Float32 vectors use bulk byte copies for encoding and decoding on little-endian
hosts, with explicit little-endian conversion on other hosts. WAL checksums use
a compile-time CRC-32 lookup table with the existing polynomial and incremental
checksum state. Record bytes, WAL framing and durable commit ordering follow
the existing format. The file I/O regression compares CRC results against the
previous bitwise algorithm, including incremental updates and page payloads.

The learning material includes selected authentic user requests, the user's
filter-order explanation and version-specific experiment results. Student
statements and assistant-executed validation retain their source attribution.
Material input checks establish file identity and parseability. Assessment of
the student's learning evidence uses the platform's independent evaluation.

All nine files under `agent/` reproduce the passed formal reference
`d8d894ff918c04751ecdf32f229454fa48b2f808`. The assessed JSON is exactly
7,686 bytes with SHA256
`60fd7d525cf72d78d14c09218c9ab5bfa43e3a285d6f16487667a8df5f7373b0`.
Its original student explanation and experiment result remain at
`/conversations/1/content` and `/experiments/0/output`. These files preserve
the reference's historical record. The four sections in `learning_record.md`
are its historical outline; the current detailed assessment rubric has five
dimensions and reads `agent/conversation.json`. This candidate's combination
of the original material and the default stdio entrypoint requires its own
formal evaluation.

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

The published process contract is implemented and the official public driver
is included unchanged. Formal 29-case admission, complete semantic error-code
mapping, performance scoring, and Agent scoring require platform validation.
