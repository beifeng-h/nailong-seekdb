# MiniVectorDB development build

This C++20 implementation provides disk-backed tables, BIGINT primary keys,
Unicode VARCHAR values, float32 vectors, exact filtered L2 TopK, transactions
and redo recovery. SQL execution and query results use the same transaction
visibility as ordinary record access.

The public SQL fixtures in `mysqltest/` are unchanged from the official
`2026-competition-round2` commit
`a3dba559c1e7ebdc48bc990ffb1a4c75d1c1075c`.
[Official fixture description](https://github.com/oceanbase/seekdb/blob/a3dba559c1e7ebdc48bc990ffb1a4c75d1c1075c/mysqltest/README.md)

## Build and check

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel 2
ctest --test-dir build --output-on-failure
python3 tests/public_samples.py build/sql_public_case_driver --report public-samples.json
python3 tests/legacy_schema.py build/sql_public_case_driver --report legacy-schema.json
```

## Docker validation

```sh
docker build --platform linux/amd64 --target development-checks -t minivectordb-development .
```

The Dockerfile pins the Ubuntu 24.04 amd64 base-image digest. It bootstraps
`ca-certificates=20240203` through the signed Ubuntu noble release repository,
then installs build tools from the HTTPS `20261109T000000Z` Ubuntu package
snapshot. APT archive signatures and HTTPS certificate verification remain
enabled. The `development-checks` stage runs
the five CTest programs, all five public fixture groups and the legacy schema
fixture. Installed package versions are saved inside the image at
`/build/installed-packages.tsv`.

The independent Actions workflow builds from an explicit Git archive of source,
tests and build files. It then runs process-kill recovery checks in a
network-disabled container. The image exists on the temporary runner during
validation.

File reads use native positional operations. The file I/O tests cover short
reads, errors, immediate reads after unbuffered writes, offsets beyond 4 GiB
and interrupted reads on Linux. The positional-read workflow also compares
the candidate with the frozen read implementation on one synthetic
10,000-row, 128-dimensional database. Both variants execute 100 exact Top10
queries per group in balanced order and check every returned row against
independent integer-distance calculations. Its elapsed times include process
startup, SQL parsing and result capture. The operating-system cache is retained
between runs.

## Integration status

The contest stdin/stdout process contract and full driver remain pending.
The current Docker stages validate the development implementation. A contest
database startup entry and official acceptance still require the frozen
protocol and container requirements.
