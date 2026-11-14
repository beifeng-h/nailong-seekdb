import unittest

import fts_inverted_sort_validation as validation


def build_lines(role):
    names = validation.INVERTED + validation.DOC_WORD + validation.OTHER
    lines = []
    for name in names:
        enabled = name in validation.INVERTED or (role == 'baseline' and name in validation.DOC_WORD)
        lines.append("generate_build_replica_sql execute sql(sql_string=INSERT /*+ "
                     f"opt_param('enable_newsort', '{str(enabled).lower()}') "
                     f"*/INTO `fts_large_bench`.`__idx_500003_{name}`")
    return lines


class BuildActivationTests(unittest.TestCase):
    def test_exact_baseline_and_candidate_activation(self):
        for role, counts in (('baseline', (6, 2)), ('candidate', (3, 5))):
            proof = validation.build_hint_proof('\n'.join(build_lines(role)), role)
            self.assertEqual((proof['enabledCount'], proof['disabledCount']), counts)

    def test_wrong_role_cannot_pass(self):
        for role, other in (('baseline', 'candidate'), ('candidate', 'baseline')):
            with self.assertRaises(RuntimeError):
                validation.build_hint_proof('\n'.join(build_lines(other)), role)

    def test_missing_or_duplicate_ddl_cannot_pass(self):
        lines = build_lines('candidate')
        for changed in (lines[:-1], lines + [lines[0]]):
            with self.assertRaises(RuntimeError):
                validation.build_hint_proof('\n'.join(changed), 'candidate')

    def test_plain_query_cannot_prove_activation(self):
        with self.assertRaises(RuntimeError):
            validation.build_hint_proof("SELECT /*+ opt_param('enable_newsort', 'true') */ 1", 'candidate')


if __name__ == '__main__':
    unittest.main()
