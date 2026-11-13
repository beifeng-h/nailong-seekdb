import unittest

import fts_docword_sort_diagnostics as diagnostic


class EvidenceTests(unittest.TestCase):
    def audit(self, rows='100', status='0'):
        return '\t'.join(('7', 'A' * 32, '4', status, rows, '2000', '1200', '200', '300', '0', '1')) + '\n'

    def test_exact_query_audit_and_cpu_proxy(self):
        result = diagnostic.parse_audit(self.audit(), 100)
        self.assertEqual(result['executeMinusWaitUs'], 1000)

    def test_rejects_incomplete_wrong_or_failed_audit(self):
        for raw in ('', self.audit() + self.audit(), self.audit(rows='99'), self.audit(status='-1')):
            with self.assertRaises(Exception):
                diagnostic.parse_audit(raw, 100)

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
