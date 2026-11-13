import unittest

import fts_encoded_sort_validation as validation


def ddl_line(table, enabled):
    return ("generate_build_replica_sql execute sql(sql_string=INSERT /*+ "
            f"opt_param('enable_newsort', '{str(enabled).lower()}') */INTO `fts_large_bench`.`{table}`")


class BuildActivationTests(unittest.TestCase):
    def setUp(self):
        names = ('fti_all_ik', 'fti_all_ik_fts_doc_word', 'fti_content_ik',
                 'fti_content_ik_fts_doc_word', 'fti_en_beng', 'fti_en_beng_fts_doc_word')
        self.lines = [ddl_line('__idx_123_' + name, True) for name in names]
        self.other = ddl_line('__idx_123_fts_rowkey_doc', False)

    def test_requires_all_six_actual_build_sorts(self):
        proof = validation.build_hint_proof('\n'.join(self.lines + [self.other]), 'candidate')
        self.assertEqual(proof['enabledCount'], 6)
        with self.assertRaises(RuntimeError):
            validation.build_hint_proof('\n'.join(self.lines[:-1] + [self.other]), 'candidate')

    def test_generic_query_does_not_prove_ddl_activation(self):
        with self.assertRaises(RuntimeError):
            validation.build_hint_proof("SELECT /*+ opt_param('enable_newsort', 'true') */ 1;", 'candidate')

    def test_baseline_must_not_enable_sort(self):
        self.assertEqual(validation.build_hint_proof(self.other, 'baseline')['enabledCount'], 0)
        with self.assertRaises(RuntimeError):
            validation.build_hint_proof('\n'.join(self.lines + [self.other]), 'baseline')

    def test_retains_old_path_for_non_target_ddl(self):
        with self.assertRaises(RuntimeError):
            validation.build_hint_proof('\n'.join(self.lines), 'candidate')


if __name__ == '__main__':
    unittest.main()
