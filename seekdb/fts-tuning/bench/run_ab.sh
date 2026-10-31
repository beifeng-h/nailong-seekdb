#!/usr/bin/env bash
# A/B driver: run the unified benchmark against two observers and compare.
#
#   bash run_ab.sh [rows] [queries] [threads] [baseline_port] [optimized_port]
#
# Defaults: 100000 rows, 300 queries, single thread, ports 2882 (baseline) and
# 2883 (optimised).
set -u

ROWS="${1:-100000}"
QUERIES="${2:-300}"
THREADS="${3:-1}"
BASE_PORT="${4:-2882}"
OPT_PORT="${5:-2883}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${HERE}/../results"
mkdir -p "$OUT"

run_one() {
  local tag="$1" port="$2"
  python3 "${HERE}/bench.py" \
    --host 127.0.0.1 --port "$port" --user root \
    --rows "$ROWS" --queries "$QUERIES" --threads "$THREADS" \
    --tag "$tag" --out "${OUT}/result_${tag}.json" \
    --regress "${OUT}/regress_${tag}.tsv"
}

echo "############ BASELINE (port ${BASE_PORT}) ############"
run_one baseline "$BASE_PORT" > "${OUT}/log_baseline.txt" 2>&1
echo "rc=$?  (log: ${OUT}/log_baseline.txt)"
tail -3 "${OUT}/log_baseline.txt"

echo
echo "############ OPTIMIZED (port ${OPT_PORT}) ############"
run_one optimized "$OPT_PORT" > "${OUT}/log_optimized.txt" 2>&1
echo "rc=$?  (log: ${OUT}/log_optimized.txt)"
tail -3 "${OUT}/log_optimized.txt"

echo
echo "############ COMPARISON ############"
python3 "${HERE}/compare_results.py" \
  "${OUT}/result_baseline.json" "${OUT}/result_optimized.json" | tee "${OUT}/summary.txt"

echo
echo "############ RESULT SET EQUIVALENCE ############"
for suffix in ".tsv" ".tsv.topk"; do
  b="${OUT}/regress_baseline${suffix}"
  o="${OUT}/regress_optimized${suffix}"
  if [ ! -f "$b" ] || [ ! -f "$o" ]; then
    echo "${suffix}: missing regression file"
    continue
  fi
  # column 1/2 = kind and row count, column 4 = the order insensitive digest
  if diff -q <(cut -f1,2,4 "$b") <(cut -f1,2,4 "$o") >/dev/null 2>&1; then
    echo "${suffix}: result SETS are identical across both builds"
  else
    echo "${suffix}: RESULT SETS DIFFER"
    diff <(cut -f1,2,4 "$b") <(cut -f1,2,4 "$o") | head -8
  fi
done
