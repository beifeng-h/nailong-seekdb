import unittest

import fts_docword_sort_diagnostics as diagnostic


class EvidenceTests(unittest.TestCase):
    def stats(self, executions=1, elapsed=2000, cpu=1200, plan=7):
        return '\t'.join(map(str, (plan, 'A' * 32, executions, elapsed, cpu, 0, 0, 100, 100, 0, 0, 200))) + '\n'

    def test_new_plan_delta(self):
        result = diagnostic.stats_delta({}, diagnostic.parse_stats(self.stats()))
        self.assertEqual(result['CPU_TIME'], 1200)

    def test_cached_plan_uses_single_execution_difference(self):
        result = diagnostic.stats_delta(diagnostic.parse_stats(self.stats()),
            diagnostic.parse_stats(self.stats(executions=2, elapsed=4000, cpu=2400)))
        self.assertEqual(result['CPU_TIME'], 1200)
        self.assertEqual(result['EXECUTIONS'], 1)

    def test_rejects_missing_duplicate_and_multi_execution_deltas(self):
        for raw in ('', self.stats(executions=2), self.stats() + self.stats(plan=8)):
            with self.assertRaises(Exception):
                diagnostic.stats_delta({}, diagnostic.parse_stats(raw))
        with self.assertRaises(Exception):
            diagnostic.parse_stats(self.stats() + self.stats())

    def test_workarea_distinguishes_optimal_and_external_execution(self):
        for mode, temp in (('OPTIMAL', 'NULL'), ('ONE PASS', '8000000'), ('MULTI-PASS', '9000000')):
            result = diagnostic.parse_workarea('\t'.join(('A' * 32, '1', '2097152', mode,
                                                         '0', '1', '0', temp)) + '\n', 'A' * 32)
            self.assertEqual(result['LAST_EXECUTION'], mode)

    def test_rejects_wrong_query_workarea(self):
        with self.assertRaises(Exception):
            diagnostic.parse_workarea('\t'.join(('B' * 32, '1', '123', 'OPTIMAL', '1', '0', '0', 'NULL')), 'A' * 32)


if __name__ == '__main__':
    unittest.main()
