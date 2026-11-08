"""Offline strict byte-gate tests. Fixtures read original SQL/golden; no SQL runs."""
import copy
import difflib
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import fts_plan_compatibility as p

REPO = Path(__file__).resolve().parents[3]


class PlanMetadataCompatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Originals are read only. All adverse modifications below use owned
        # temporary copies, never the official test or golden.
        cls.original = (REPO / p.GOLDEN).read_bytes()
        cls.test = (REPO / p.TEST).read_bytes()
        if hashlib.sha256(cls.original).hexdigest() != p.GOLDEN_SHA256:
            raise AssertionError('Real golden fixture does not match immutable pin')
        if hashlib.sha256(cls.test).hexdigest() != p.TEST_SHA256:
            raise AssertionError('Real SQL fixture does not match immutable pin')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.out = self.root / 'owned-output'
        self.logs = self.out / 'mysqltest-log'
        self.logs.mkdir(parents=True)
        for relative, data in ((p.GOLDEN, self.original), (p.TEST, self.test)):
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.reject = self.logs / 'simple_query.reject'
        self.output = self.out / 'mysqltest-output.log'
        self.wrapper = self.out / 'original-run-case.log'
        lines = self.original.splitlines(keepends=True)
        for _, line, _, _, _ in p.SPECS:
            lines[line - 1] = lines[line - 1].replace(p.TRUE_TOKEN, p.FALSE_TOKEN, 1)
        self.compatible = b''.join(lines)
        self.row = dict(name=p.CASE, status='failed', cleanupPassed=True,
                        work=dict(passed=False, exitCode=1, invocations=1,
                                  inputsUnchanged=True, testSha256=p.TEST_SHA256,
                                  expectedSha256=p.GOLDEN_SHA256))
        self.arguments = dict(source=self.source, out=self.out, role='candidate',
                              case_name=p.CASE, row=self.row)
        self.write_actual(self.compatible)

    def write_actual(self, data):
        self.reject.write_bytes(data)
        mtime = b'2026-11-08 14:45:05.052470529 +0000'
        body = b''.join(difflib.diff_bytes(
            difflib.unified_diff, self.original.splitlines(keepends=True),
            data.splitlines(keepends=True), fromfile=str(self.source / p.GOLDEN).encode(),
            tofile=str(self.reject).encode(), fromfiledate=mtime, tofiledate=mtime))
        self.write_output(body + b'\n[2026-11-08 14:45:05] mysqltest: Result length mismatch\n')

    def write_output(self, data):
        self.output.write_bytes(data)
        self.wrapper.write_bytes(
            b'[ RUN      ] fts_index.simple_query\n' + data +
            (b'' if data.endswith(b'\n') else b'\n') +
            b'[  FAILED  ] fts_index.simple_query (0.746s, exit=1)\n')

    def classify(self, **changes):
        return p.classify_plan_metadata_compatibility(**dict(self.arguments, **changes))

    def assert_rejected(self, **changes):
        before = copy.deepcopy(self.row)
        proof = self.classify(**changes)
        self.assertFalse(proof['accepted'], proof)
        self.assertTrue(proof['reason'])
        self.assertEqual(before, self.row)
        self.assertFalse(proof['nativePassed'])
        self.assertFalse(proof['formalContestPass'])
        return proof

    def test_positive_real_pinned_fixture_complete_four_flag_proof(self):
        before = copy.deepcopy(self.row)
        inputs = {path: path.read_bytes() for path in
                  (self.source / p.GOLDEN, self.source / p.TEST,
                   self.reject, self.output, self.wrapper)}
        proof = self.classify()
        self.assertTrue(proof['accepted'], proof)
        self.assertEqual(proof['rawNativeStatus'], 'failed')
        self.assertFalse(proof['nativePassed'])
        self.assertFalse(proof['formalContestPass'])
        self.assertEqual(proof['approvedDifferenceCount'], 4)
        self.assertEqual([c['annotationLine'] for c in proof['changes']], [1206, 1563, 1633, 1657])
        self.assertEqual([c['scanId'] for c in proof['changes']], [1, 1, 0, 1])
        self.assertEqual([c['sqlStartLine'] for c in proof['changes']], [1191, 1541, 1621, 1642])
        self.assertEqual(proof['originalGolden']['bytes'], 184285)
        self.assertEqual(proof['originalGolden']['lines'], 2833)
        self.assertEqual(proof['actualReject']['bytes'], len(self.compatible))
        self.assertEqual(proof['actualReject']['sha256'], hashlib.sha256(self.compatible).hexdigest())
        self.assertEqual(proof['actualReject']['lines'], 2833)
        self.assertEqual(proof['adjacentScoredControl'], dict(annotationLine=1603, calcRelevance=True))
        self.assertTrue(proof['completeBytesCompared'] and proof['eofCompared'])
        self.assertEqual(before, self.row)
        self.assertEqual(inputs, {path: path.read_bytes() for path in inputs})

    def test_positive_gnu_diff_matches_observed_native_format(self):
        command = shutil.which('diff')
        self.assertIsNotNone(command, 'GNU diff required for independent native-format contract')
        result = subprocess.run([command, '-u', str(self.source / p.GOLDEN), str(self.reject)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.write_output(result.stdout + b'\n[2026-11-08 14:45:05] mysqltest: Result length mismatch\n')
        self.assertTrue(self.classify()['accepted'], self.classify())

    def test_all_four_changes_required_individually(self):
        for _, annotation, _, _, _ in p.SPECS:
            with self.subTest(annotation=annotation):
                lines = self.compatible.splitlines(keepends=True)
                lines[annotation - 1] = lines[annotation - 1].replace(p.FALSE_TOKEN, p.TRUE_TOKEN)
                self.write_actual(b''.join(lines))
                self.assert_rejected()

    def test_unchanged_native_reject_is_not_compatible(self):
        self.write_actual(self.original)
        self.assert_rejected()

    def test_extra_flag_change_in_scored_projection_rejected(self):
        lines = self.compatible.splitlines(keepends=True)
        lines[1602] = lines[1602].replace(p.TRUE_TOKEN, p.FALSE_TOKEN)
        self.write_actual(b''.join(lines))
        self.assert_rejected()

    def test_changes_to_rows_counts_scores_sql_errors_and_other_plan_fields_rejected(self):
        mutations = {
            'count': (b'|        3 |', b'|        4 |'),
            'row': (b'Leslie Lamport', b'Lester Lamport'),
            'score': (b'0.9973981429169073', b'0.9973981429169074'),
            'sql': (b'explain select count(1)', b'explain select count(2)'),
            'other_plan': (b'is_global_index=false', b'is_global_index=true'),
            'scan_id': ('|1 |└─TEXT RETRIEVAL SCAN'.encode(), '|2 |└─TEXT RETRIEVAL SCAN'.encode()),
            'error': (b'Query Plan\n', b'ERROR 9999: changed SQL result\n'),
            'flag_spacing': (p.FALSE_TOKEN, p.FALSE_TOKEN.replace(b'=', b' = ')),
        }
        for name, (old, new) in mutations.items():
            with self.subTest(name=name):
                self.assertIn(old, self.compatible)
                self.write_actual(self.compatible.replace(old, new, 1))
                self.assert_rejected()

    def test_whitespace_newlines_invalid_bytes_and_eof_rejected(self):
        mutations = dict(
            leading_space=b' ' + self.compatible,
            trailing_space=self.compatible.replace(b'\n', b' \n', 1),
            crlf=self.compatible.replace(b'\n', b'\r\n'),
            invalid_byte=self.compatible + b'\xff',
            missing_eof_newline=self.compatible[:-1],
            truncated_eof=self.compatible[:-100],
            extra_eof=self.compatible + b'\n',
            bare_cr=self.compatible.replace(b'\n', b'\r', 1))
        for name, data in mutations.items():
            with self.subTest(name=name):
                self.write_actual(data)
                self.assert_rejected()

    def test_original_golden_hash_cannot_be_replaced(self):
        (self.source / p.GOLDEN).write_bytes(self.compatible)
        self.assert_rejected()

    def test_original_sql_hash_cannot_be_replaced(self):
        (self.source / p.TEST).write_bytes(self.test + b'\n')
        self.assert_rejected()

    def test_recorded_native_hashes_must_match_both_pins(self):
        for key in ('testSha256', 'expectedSha256'):
            with self.subTest(key=key):
                original = self.row['work'][key]
                self.row['work'][key] = '0' * 64
                self.assert_rejected()
                self.row['work'][key] = original

    def test_wrong_role_case_name_and_raw_status_rejected(self):
        for changes in (dict(role='baseline'), dict(role='Candidate'),
                        dict(case_name='fts_index.basic_dml'), dict(case_name='simple_query')):
            with self.subTest(changes=changes):
                self.assert_rejected(**changes)
        for field, value in (('name', 'fts_index.basic_dml'), ('status', 'passed'),
                             ('status', 'infrastructure_error'), ('status', None)):
            with self.subTest(field=field, value=value):
                self.row[field] = value
                self.assert_rejected()
                self.row[field] = p.CASE if field == 'name' else 'failed'

    def test_work_requires_strict_boolean_and_integer_values(self):
        for field, values in dict(passed=(True, 0, None), exitCode=(0, 124, -11, 255, True, 1.0),
                                  invocations=(0, 2, True, 1.0),
                                  inputsUnchanged=(False, 1, None)).items():
            for value in values:
                with self.subTest(field=field, value=value):
                    previous = self.row['work'][field]
                    self.row['work'][field] = value
                    self.assert_rejected()
                    self.row['work'][field] = previous

    def test_missing_or_malformed_native_record_rejected(self):
        for value in (None, [], {}, 'failed'):
            with self.subTest(value=value):
                self.assert_rejected(row=value)
        previous = self.row.pop('work')
        self.assert_rejected()
        self.row['work'] = previous

    def test_any_abnormal_failure_diagnostic_in_row_or_work_rejected(self):
        for record in (self.row, self.row['work']):
            for key, value in dict(error='unexpected', traceback='trace', timedOut=True,
                                  signal=11, failureKind='infra', failureReason='fault').items():
                with self.subTest(record=record is self.row, key=key):
                    record[key] = value
                    self.assert_rejected()
                    del record[key]

    def test_failed_or_unproven_cleanup_rejected(self):
        for value in (False, 1, None):
            with self.subTest(value=value):
                self.row['cleanupPassed'] = value
                self.assert_rejected()
        self.row['cleanupPassed'] = True
        self.row['cleanupError'] = 'failed cleanup'
        self.assert_rejected()

    def test_remaining_database_directory_rejected(self):
        (self.out / 'database').mkdir()
        self.assert_rejected()

    def test_conflicting_recorded_role_rejected(self):
        self.row['role'] = 'baseline'
        self.assert_rejected()

    def test_excessive_native_log_directory_entries_rejected(self):
        for number in range(17):
            (self.logs / ('extra-%d.log' % number)).write_bytes(b'ordinary log')
        self.assert_rejected()

    def test_missing_and_duplicate_reject_rejected(self):
        self.reject.unlink()
        self.assert_rejected()
        self.reject.write_bytes(self.compatible)
        (self.logs / 'other.reject').write_bytes(self.compatible)
        self.assert_rejected()

    def test_nested_reject_directory_rejected(self):
        nested = self.logs / 'nested'
        nested.mkdir()
        (nested / 'simple_query.reject').write_bytes(self.compatible)
        self.assert_rejected()

    def test_wrong_reject_filename_rejected(self):
        self.reject.rename(self.logs / 'other.reject')
        self.assert_rejected()

    def test_evidence_size_limits_and_empty_reject_rejected(self):
        for data in (b'', b'x' * (p.MAX_BYTES + 1)):
            with self.subTest(size=len(data)):
                self.reject.write_bytes(data)
                self.assert_rejected()

    def test_symlink_reject_rejected(self):
        external = self.root / 'external.reject'
        external.write_bytes(self.compatible)
        self.reject.unlink()
        self.reject.symlink_to(external)
        self.assert_rejected()

    def test_symlink_native_log_directory_rejected(self):
        elsewhere = self.root / 'external-logs'
        self.logs.rename(elsewhere)
        self.logs.symlink_to(elsewhere, target_is_directory=True)
        self.assert_rejected()

    def test_symlink_output_ancestor_rejected(self):
        alias = self.root / 'output-alias'
        alias.symlink_to(self.out, target_is_directory=True)
        self.assert_rejected(out=alias)

    def test_symlink_original_golden_rejected(self):
        golden = self.source / p.GOLDEN
        external = self.root / 'external-golden'
        golden.rename(external)
        golden.symlink_to(external)
        self.assert_rejected()

    def test_symlink_diagnostics_rejected(self):
        for path in (self.output, self.wrapper):
            with self.subTest(path=path.name):
                content = path.read_bytes()
                external = self.root / ('external-' + path.name)
                external.write_bytes(content)
                path.unlink()
                path.symlink_to(external)
                self.assert_rejected()
                path.unlink()
                path.write_bytes(content)

    def test_hardlinked_reject_rejected(self):
        import os
        os.link(self.reject, self.root / 'other-hardlink')
        self.assert_rejected()

    def test_relative_path_traversal_and_source_overlap_rejected(self):
        for changes in (dict(out=Path('.')), dict(out=self.out / '..' / 'owned-output'),
                        dict(out=self.source), dict(source=self.out)):
            with self.subTest(changes=changes):
                self.assert_rejected(**changes)

    def test_missing_diagnostics_rejected(self):
        for path in (self.output, self.wrapper):
            before = path.read_bytes()
            path.unlink()
            self.assert_rejected()
            path.write_bytes(before)

    def test_extra_native_error_crash_timeout_or_signal_rejected(self):
        original = self.output.read_bytes()
        for diagnostic in (b'ERROR 2013: Lost connection\n', b'Segmentation fault\n',
                           b'3600 seconds timeout\n', b'Aborted (core dumped)\n',
                           b'failed to run mysqltest: missing executable\n',
                           b'[2026-11-08 14:45:06] mysqltest: Result length mismatch\n'):
            with self.subTest(diagnostic=diagnostic):
                self.write_output(original + diagnostic)
                self.assert_rejected()

    def test_unobserved_content_mismatch_diagnostic_rejected(self):
        self.write_output(self.output.read_bytes().replace(b'Result length mismatch', b'Result content mismatch'))
        self.assert_rejected()

    def test_missing_native_mismatch_diagnostic_rejected(self):
        self.write_output(self.output.read_bytes().split(b'\n[2026-', 1)[0] + b'\n')
        self.assert_rejected()

    def test_wrong_diff_paths_body_or_invalid_output_bytes_rejected(self):
        original = self.output.read_bytes()
        for data in (original.replace(str(self.reject).encode(), b'/unowned/reject', 1),
                     original.replace(str(self.source / p.GOLDEN).encode(), b'/other/expected', 1),
                     original.replace(b'@@ -1203,7 +1203,7 @@', b'@@ -1204,7 +1204,7 @@', 1),
                     original.replace(b'calc_relevance=false', b'calc_relevance=true', 1),
                     original + b'\xff'):
            with self.subTest(data=data[:90]):
                self.write_output(data)
                self.assert_rejected()

    def test_invalid_diagnostic_timestamp_rejected(self):
        self.write_output(self.output.read_bytes().replace(b'[2026-11-08', b'[2026-99-08', 1))
        self.assert_rejected()

    def test_wrapper_retry_other_case_or_exit_rejected(self):
        original = self.wrapper.read_bytes()
        for data in (original + b'[ RETRY   ] fts_index.simple_query (1/3)\n',
                     original.replace(b'exit=1', b'exit=124'),
                     original.replace(b'[ RUN      ] fts_index.simple_query', b'[ RUN      ] wrong_case'),
                     original.replace(b'(0.746s', b'(nan'),
                     original + b'warning: unexpected\n'):
            with self.subTest(data=data[-90:]):
                self.wrapper.write_bytes(data)
                self.assert_rejected()

    def test_internal_sql_operator_and_annotation_checks_fail_closed(self):
        # Even if a future programmer removes hashing, the statement/operator
        # binding itself does not accept the wrong SQL, scan, or flag.
        lines = self.original.splitlines(keepends=True)
        start, annotation, scan_id, _, sql = p.SPECS[0]
        for altered_sql, altered_scan, altered_annotation in (
                (sql + b' ', scan_id, annotation), (sql, 0, annotation),
                (sql, scan_id, annotation - 1)):
            with self.subTest(scan=altered_scan, annotation=altered_annotation):
                with self.assertRaises(ValueError):
                    p._statement_plan(lines, start, altered_annotation, altered_scan, altered_sql, True)


if __name__ == '__main__':
    unittest.main()
