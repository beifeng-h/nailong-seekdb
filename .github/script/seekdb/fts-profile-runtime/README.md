# FTS profiling diagnostics

This runtime captures a function-level profile of the exact `b9637d5e792cce0e461a06eee2df91036a3ff37a` candidate. It reuses the original portable v2 driver, the pinned b147 baseline, the official benchmark inputs and the existing database lifecycle.

Candidate measurements include perf and process-sampling overhead. The adapter labels the run as diagnostic and records function sample counts, symbol resolution and phase boundaries. The original metrics and scoring outputs retain their provenance.

## Execution

The dedicated workflow uses a standard Ubuntu 24.04 runner in this public repository. It downloads the two pinned existing artifacts, verifies their ZIP/TAR/ELF identities, extracts the verified official client and checks the runtime before starting databases.

The workflow first checks the existing interpreter, binary tools, libaio and perf software-event capability. Passing installed prerequisites are reused. The probe output is preserved with the diagnostic evidence.

The original runtime budget is 3900 seconds for the session and 3000 seconds for the pair. The capture adds a 20-second cleanup reserve. Post-cleanup perf reporting has a 30-second command limit and a 50-second total reserve. The workflow has a 90-minute job limit and records a fresh operator-selected UTC stop deadline.

Before `preflight` or `run`, the adapter satisfies the original driver's soft file-descriptor limit of 8192 for its own process and future descendants. It preserves the hard limit and records both observed limits. An infinite soft limit is set to 8192 for compatibility with the original numeric gate.

Transient external CPU activity can trigger fresh original process observations within a shared 30-second additional budget per adapter process. The original guard evaluates the returned observation. The adapter records every observation and checks the existing UTC reserve before accepting a recovered idle result.

`profile_capture.py` attaches to the driver-verified candidate PID. It records 99 Hz user CPU samples with DWARF call stacks and collects process CPU, I/O and RSS every 250 ms. Benchmark stage markers carry polling and scheduling uncertainty. The capture signals and reaps its own perf process group.

`run_profile.py` verifies the exact original runner, manifest and all 75 frozen inputs. It adds sampling to the candidate benchmark and runs flat perf reporting after the original driver has completed database cleanup. Positive integer sample rows and unknown-symbol fractions are recorded in the diagnostic report.

## Offline checks

```sh
python3 -B -m unittest discover -s .github/script/seekdb/fts-profile-runtime -p 'test_*.py' -v
python3 -B run_profile.py --portable-root /absolute/path/to/original-bundle --mode check-bundle
```

The runtime also forwards the original `extract`, `preflight`, `run` and `collect` modes on Linux. Collection includes raw `perf.data`, process samples, phase marks and identity records in a verified `profiling-raw.tar.gz` supplement. A failed collection retains its partial output and source data.

The portable archive SHA256 is `b384f615bd0953dd3ca5602d002007d0fdebb16204a7d23f71b275625867eda5`. Its original input inventory SHA256 is `f36e2d1c8aaabd3e9ffc4f8e3635defca6dc3046e3d575e6e73165fa8d80a135`.

Perf options follow the upstream project's manuals. Standard public-runner compute follows GitHub's documented billing rules. [perf record](https://man7.org/linux/man-pages/man1/perf-record.1.html) [perf report](https://man7.org/linux/man-pages/man1/perf-report.1.html) [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
