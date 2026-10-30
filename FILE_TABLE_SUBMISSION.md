# seekdb Local File Table

Implementation of `read_csv`, `read_json`, `read_parquet`, and `glob` inside the
seekdb SQL engine.

Validation results:

- File engine unit tests: 118/118 passed
- Parquet reader tests: 62/62 passed
- End-to-end SQL tests: 25/25 passed
- seekdb build: successful

Build entry point:

```bash
./build.py build -o /root/seekdb-build -j32 --spawn_strategy=local seekdb
```