import argparse
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import fts_encoded_sort_revalidation as resume


class RevalidationTests(unittest.TestCase):
    def test_successful_ddl_contains_only_demonstrated_charsets(self):
        sql = resume.supported_ddl_sql()
        self.assertNotIn('latin1', sql)
        self.assertIn('COLLATE=utf8mb4_general_ci', sql)
        self.assertIn('COLLATE=utf8mb4_bin', sql)
        self.assertEqual(sql.count('SELECT COUNT(*)'), 4)

    def check_rejection(self, exit_code, output, cleaned=True):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            def capture(command, log, *args, **kwargs):
                self.assertIn(b'CHARSET=latin1', kwargs['stdin'])
                log.write_text(output)
                return dict(exitCode=exit_code, childrenCleaned=cleaned)
            with mock.patch.object(resume.common, 'capture_group', side_effect=capture):
                return resume.unsupported_charset_control(argparse.Namespace(obclient=Path('client'), port=7500), out)

    def test_preserves_observed_1115_as_an_explicit_control(self):
        text = "ERROR 1115 (42000) at line 2: Unknown character set: 'latin1'\n"
        self.assertEqual(self.check_rejection(1, text), text)

    def test_does_not_accept_success_or_unrelated_sql_errors(self):
        for code, output in ((0, ''), (1, 'ERROR 1064 (42000): syntax error\n')):
            with self.assertRaises(RuntimeError):
                self.check_rejection(code, output)

    def test_rejected_sql_still_requires_child_cleanup(self):
        with self.assertRaises(RuntimeError):
            self.check_rejection(1, "ERROR 1115 (42000) at line 2: Unknown character set: 'latin1'\n", False)


if __name__ == '__main__':
    unittest.main()
