import unittest

import fts_encoded_sort_cases as cases
import fts_encoded_sort_diagnostics as diagnostics


class SortEvidenceTests(unittest.TestCase):
    def test_detects_order_change_with_same_row_set(self):
        result = cases.compare_rows('1\n2\n3\n', '1\n3\n2\n', 3)
        self.assertFalse(result['passed'])
        self.assertTrue(result['completePermutation'])
        self.assertEqual(result['mismatchCount'], 2)

    def test_equal_but_incomplete_or_duplicated_output_fails(self):
        for output in ('1\n2\n', '1\n2\n2\n', ''):
            self.assertFalse(cases.compare_rows(output, output, 3)['passed'])

    def test_full_descending_result_passes(self):
        self.assertTrue(cases.compare_rows('3\n2\n1\n', '3\n2\n1\n', 3)['passed'])

    def test_changed_key_bytes_are_not_hidden_by_matching_ids(self):
        self.assertFalse(cases.compare_rows('1\t41\n', '1\t61\n', 1)['passed'])

    def test_reuses_single_exact_formal_baseline(self):
        self.assertEqual(set(diagnostics.PINS), {'candidate'})
        self.assertEqual(diagnostics.PINS['candidate']['commit'], diagnostics.CANDIDATE)
        self.assertEqual(diagnostics.PINS['candidate']['artifact'], 13941216422)
        self.assertEqual({role for name, role in diagnostics.SCHEDULE}, {'candidate'})


if __name__ == '__main__':
    unittest.main()
