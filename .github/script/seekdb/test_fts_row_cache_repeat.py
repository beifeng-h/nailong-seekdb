"""Check the repeat schedule and scoring against the unchanged scorer."""
import json
from pathlib import Path
import unittest

import fts_row_cache_repeat as repeat


ROOT = Path(__file__).resolve().parents[3]
SCORER_PATH = ROOT / 'tools/benchmark/fts_large_bench_score.py'
BASELINE_PATH = ROOT / 'tools/benchmark/fts_large_bench_baseline.json'
SCORER = repeat.common.load_module('repeat_test_original_scorer', SCORER_PATH)
BASELINE = json.loads(BASELINE_PATH.read_text(encoding='utf8'))


def make_rounds():
    base = dict(BASELINE['baseline_metrics'], **BASELINE['benchmark_config'])
    factors = {'B1': 0.8, 'B2': 1.1, 'B3': 1.0, 'B4': 0.95}
    keys = [key for group in BASELINE['score_categories'].values() for key in group]
    result = []
    for name, role in repeat.SCHEDULE:
        values = dict(base)
        if role == 'candidate':
            for key in keys:
                values[key] *= factors[name]
        result.append(dict(name=name, role=role, metrics=values))
    return result


class RepeatScoringTests(unittest.TestCase):
    def test_four_paired_results_preserve_negative_improvements(self):
        result = repeat.summarize(make_rounds(), BASELINE, SCORER)
        self.assertEqual([(x['baseline'], x['candidate']) for x in result['pairs']], list(repeat.PAIRS))
        for row, expected in zip(result['pairs'], (0.2, -0.1, 0.0, 0.05)):
            self.assertAlmostEqual(row['improvement'], expected)
        self.assertAlmostEqual(result['meanImprovement'], 0.0375)
        self.assertFalse(result['formalContestScore'])

    def test_missing_or_duplicate_round_is_rejected(self):
        rows = make_rounds()
        with self.assertRaises(Exception):
            repeat.summarize(rows[:-1], BASELINE, SCORER)
        rows[-1] = rows[0]
        with self.assertRaises(Exception):
            repeat.summarize(rows, BASELINE, SCORER)

    def test_changed_original_configuration_is_rejected(self):
        rows = make_rounds()
        rows[1]['metrics']['rows'] += 1
        with self.assertRaises(SystemExit):
            repeat.summarize(rows, BASELINE, SCORER)

    def test_missing_scored_metric_is_rejected(self):
        rows = make_rounds()
        del rows[1]['metrics']['query_cn_avg_ms']
        with self.assertRaises(SystemExit):
            repeat.summarize(rows, BASELINE, SCORER)

    def test_existing_profiler_ignores_repeat_files(self):
        trigger = (ROOT / '.github/workflows/fts-profile-diagnostics.yml').read_text(encoding='utf8').split(
            '\npermissions:', 1)[0]
        for path in (
            '.github/script/seekdb/fts_row_cache_repeat.py',
            '.github/script/seekdb/test_fts_row_cache_repeat.py',
            '.github/workflows/fts-row-cache-repeat.yml',
        ):
            self.assertIn('- ' + path, trigger)


if __name__ == '__main__':
    unittest.main()
