from pathlib import Path
import unittest
from unittest import mock

import fts_string_decoder_validation as validation


class StorageWorkTests(unittest.TestCase):
    def run_storage(self, corrupt=False):
        _, _, expected = validation.storage_input()
        calls = []

        def sql(call, out, label, query):
            calls.append(label)
            if label == 'decoder-major-version-before':
                return '100\t100\n'
            if label == 'decoder-major-version-after':
                return '200\t200\n'
            if label == 'decoder-storage-before':
                return expected
            if label == 'decoder-storage-after':
                return expected.replace('636F6D6D6F6E', '636F6D6D6F00', 1) if corrupt else expected
            return ''

        with mock.patch.object(validation.driver, 'sql', side_effect=sql):
            result = validation.storage_work(None, Path('unused'))
        return result, calls

    def test_storage_work_checks_expected_rows_before_and_after_major_compaction(self):
        result, calls = self.run_storage()
        self.assertEqual(result['rows'], 512)
        self.assertTrue(result['beforeFreezeMatches'] and result['afterFreezeMatches'])
        self.assertLess(calls.index('decoder-major-freeze'), calls.index('decoder-storage-after'))

    def test_storage_work_rejects_a_changed_decoded_byte(self):
        with self.assertRaisesRegex(RuntimeError, 'After-freeze storage bytes'):
            self.run_storage(corrupt=True)

    def test_generated_inputs_cover_null_nul_and_long_utf8(self):
        rows = validation.storage_rows()
        self.assertEqual(len(rows), 512)
        self.assertTrue(any(a is None or b is None or c is None for _, a, b, c in rows))
        self.assertTrue(any(c and b'\x00' in c for _, _, _, c in rows))
        self.assertTrue(any(c and len(c) > 128 for _, _, _, c in rows))


class FullMatchTests(unittest.TestCase):
    def test_document_result_guard_rejects_missing_ids(self):
        metrics = {name + '_hits': 2 for name in
                   ('query_cn', 'query_beng', 'query_mixed', 'query_limit')}
        with mock.patch.object(validation, 'ORIGINAL_BENCHMARK',
                               return_value=lambda call, out, pid: {'passed': True, 'metrics': metrics}), \
             mock.patch.object(validation.driver, 'sql', return_value='1\n'):
            work = validation.benchmark_callback(None, None, None, None, None)
            with self.assertRaisesRegex(RuntimeError, 'Incomplete or unordered'):
                work(None, Path('unused'), 1)


if __name__ == '__main__':
    unittest.main()
