"""Regressions for IK source scope, actual compilation coverage and SQL parity."""
import argparse
import copy
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import fts_ik_charset_validation as ik


class SourceProofTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name)
        self.args = argparse.Namespace(source=self.source, driver_sha='c' * 40)
        for name in ik.driver.CASES:
            suite, case = name.split('.')
            for directory, suffix in (('t', '.test'), ('r/mysql', '.result')):
                path = self.source / 'tools/deploy/mysql_test/test_suite' / suite / directory / (case + suffix)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'original input\n')

    def proof(self, changed):
        def git(source, *args):
            if args[0] == 'rev-parse':
                return ik.BASE_TREE.encode()
            if args[0] == 'diff':
                return '\n'.join(changed).encode() if '--name-only' in args else b'canonical product patch'
            if args[0] == 'show':
                return b'original input\n' if args[1].startswith(ik.BASE + ':') else b'CI blob'
            raise AssertionError(args)
        with patch.object(ik.common, 'source_identity', return_value={'tree': 'd' * 40}), \
                patch.object(ik.common, 'git', side_effect=git), \
                patch.object(ik.driver, 'inventory', return_value={'input': 'sha'}), \
                patch.object(ik.driver, 'git_identity', return_value='user identity'):
            return ik.source_proof(self.args)

    def test_exact_five_products_and_existing_ci_tools_are_allowed(self):
        ci = ['.github/workflows/fts-profile-diagnostics.yml',
              '.github/script/seekdb/fts_ik_charset_validation.py']
        proof = self.proof(list(ik.PRODUCTS) + ci)
        self.assertEqual(proof['productChanges'], sorted(ik.PRODUCTS))
        self.assertEqual(proof['ciChanges'], sorted(ci))
        self.assertEqual(proof['baselineCommit'], ik.BASE)
        self.assertEqual(proof['originalInputCount'], 75)
        self.assertEqual(proof['kernelCommit'], self.args.driver_sha)

    def test_extra_or_missing_product_is_rejected(self):
        for paths in (list(ik.PRODUCTS) + ['src/sql/extra.cpp'], list(ik.PRODUCTS)[:-1]):
            with self.subTest(paths=paths), self.assertRaises(RuntimeError):
                self.proof(paths)

    def test_native_input_changes_are_rejected(self):
        case = ik.driver.CASES[0].split('.')[1]
        path = self.source / 'tools/deploy/mysql_test/test_suite/fts_index/t' / (case + '.test')
        path.write_bytes(b'changed')
        with self.assertRaises(RuntimeError):
            self.proof(list(ik.PRODUCTS))


class CompilationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.build = self.source / 'build_release'
        self.build.mkdir(parents=True)
        self.args = SimpleNamespace(source=self.source, root=self.root / 'run', phase='prebuild')
        self.compiler = str(self.source / 'deps/3rd/usr/local/oceanbase/devtools/bin/clang++-17')
        for name in ik.PRODUCTS:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('source fixture\n', encoding='utf-8')
        self.rows = []
        for index, name in enumerate(ik.COMPILATION_SOURCES):
            output = self.build / 'CMakeFiles/fixture.dir' / (str(index) + '.o')
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b'object bytes')
            dependencies = ' '.join((self.source / p).as_posix() for p in ik.PRODUCTS[1:3])
            Path(str(output) + '.d').write_text(output.as_posix() + ': ' + dependencies + '\n')
            self.rows.append(dict(directory=str(self.build), file=str(self.source / name),
                arguments=[self.compiler, '-o', str(output), '-c', str(self.source / name)]))

    def original_profile(self, args):
        path = args.root / 'evidence/candidate' / ('profile-' + args.phase) / 'profile-proof.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        ik.driver.dump(path, {'signature': {'compilers': {'CXX': {'COMPILER': self.compiler}}}})
        return ik.profile.EXPECTED.copy(), 'rustc 1.98.1 fixture'

    def run_profile(self):
        (self.build / 'compile_commands.json').write_text(json.dumps(self.rows), encoding='utf-8')
        with patch.object(ik, '_build_profile', side_effect=self.original_profile):
            return ik.build_profile(self.args)

    def test_snapshot_and_validation_cover_three_compilation_sources_and_two_headers(self):
        self.run_profile()
        self.args.phase = 'snapshot'
        self.run_profile()
        proof = json.loads((self.args.root / 'evidence/candidate/affected-ik-objects.json').read_text())
        self.assertEqual(set(proof['sourceToObject']), set(ik.COMPILATION_SOURCES))
        self.assertEqual(len(proof['objects']), 3)
        self.assertFalse((self.args.root / 'evidence/candidate/ik-header-dependencies.json').exists())
        ik.verify_header_dependencies(self.args)
        headers = json.loads((self.args.root / 'evidence/candidate/ik-header-dependencies.json').read_text())
        self.assertTrue(headers['passed'])
        self.assertEqual(headers['phase'], 'validate')
        for obj in headers['objects']:
            self.assertEqual(obj['headerDependencies']['headers'], list(ik.PRODUCTS[1:3]))

    def test_dependency_format_failure_occurs_after_snapshot_and_before_parent_validation(self):
        self.run_profile()
        self.args.phase = 'snapshot'
        path = Path(self.rows[1]['arguments'][2])
        Path(str(path) + '.d').write_text('unrelated.o: unrelated-header.h\n')
        self.run_profile()
        with patch.object(ik, '_validate') as parent:
            with self.assertRaisesRegex(RuntimeError, 'compiler dependencies'):
                ik.validate(self.args)
        parent.assert_not_called()
        proof = json.loads((self.args.root / 'evidence/candidate/ik-header-dependencies.json').read_text())
        self.assertFalse(proof['passed'])
        self.assertEqual(proof['checkingObject'], str(path))

    def test_shared_unity_object_is_deduplicated_and_maps_both_ik_sources(self):
        unity = self.build / 'CMakeFiles/fixture.dir/Unity/combined.cxx'
        unity.parent.mkdir()
        unity.write_text(''.join('#include "' + (self.source / p).as_posix() + '"\n'
                                 for p in ik.COMPILATION_SOURCES[1:]))
        self.rows[1]['file'] = str(unity)
        self.rows.pop()
        self.run_profile()
        self.args.phase = 'snapshot'
        self.run_profile()
        proof = json.loads((self.args.root / 'evidence/candidate/affected-ik-objects.json').read_text())
        self.assertEqual(len(proof['objects']), 2)
        self.assertEqual(proof['sourceToObject'][ik.PRODUCTS[3]], proof['sourceToObject'][ik.PRODUCTS[4]])

    def test_missing_or_duplicate_actual_unit_is_rejected(self):
        original = copy.deepcopy(self.rows)
        for rows in (original[:-1], original + [original[1]]):
            self.rows = rows
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                self.run_profile()

    def test_snapshot_rejects_changed_source_or_compile_command(self):
        self.run_profile()
        self.args.phase = 'snapshot'
        self.rows[1]['arguments'].insert(1, '-DCHANGED')
        with self.assertRaisesRegex(RuntimeError, 'changed after prebuild'):
            self.run_profile()
        self.rows[1]['arguments'].remove('-DCHANGED')
        (self.source / ik.PRODUCTS[1]).write_text('changed header')
        with self.assertRaisesRegex(RuntimeError, 'changed after prebuild'):
            self.run_profile()

    def test_actual_compiler_and_object_location_are_checked(self):
        self.rows[1]['arguments'][0] = 'unexpected-clang'
        with self.assertRaisesRegex(RuntimeError, 'unexpected compiler'):
            self.run_profile()
        self.rows[1]['arguments'][0] = self.compiler
        self.rows[1]['arguments'][2] = str(self.root / 'outside.o')
        with self.assertRaisesRegex(RuntimeError, 'escapes'):
            self.run_profile()

    def test_header_dependency_uses_the_matching_make_target(self):
        row = self.rows[1]
        path = Path(row['arguments'][2])
        Path(str(path) + '.d').unlink()
        make = path.parent / 'compiler_depend.make'
        dependencies = ' '.join((self.source / p).as_posix() for p in ik.PRODUCTS[1:3])
        make.write_text('unrelated.o: ' + dependencies + '\n')
        with self.assertRaises(RuntimeError):
            ik.dependency_record(path, row, self.source)
        make.write_text(path.relative_to(self.build).as_posix() + ': ' + dependencies + '\n')
        result = ik.dependency_record(path, row, self.source)
        self.assertEqual(result['headers'], list(ik.PRODUCTS[1:3]))

    def test_dependency_target_relative_to_build_root_matches_nested_command_directory(self):
        directory = self.build / 'src/storage/fts'
        path = directory / 'CMakeFiles/fts.dir/Unity/combined.cxx.o'
        path.parent.mkdir(parents=True)
        dependencies = ' '.join((self.source / p).as_posix() for p in ik.PRODUCTS[1:3])
        target = path.relative_to(self.build).as_posix()
        Path(str(path) + '.d').write_text(target + ': ' + dependencies + '\n')
        result = ik.dependency_record(path, {'directory': str(directory)}, self.source)
        self.assertEqual(result['headers'], list(ik.PRODUCTS[1:3]))
        Path(str(path) + '.d').write_text('other/' + target + ': ' + dependencies + '\n')
        with self.assertRaises(RuntimeError):
            ik.dependency_record(path, {'directory': str(directory)}, self.source)


class CallbackTests(unittest.TestCase):
    def sql(self, call, out, label, text):
        labels = re.findall(r"^SELECT '([^']+)'", text, re.M)
        raw = ''.join(name + '\t[]\n' for name in labels)
        (out / (label + '.log')).write_bytes(raw.encode())
        return raw

    def test_canonical_contains_all_real_outputs_and_malformed_exit_codes(self):
        def capture(command, path, cwd, timeout, **kwargs):
            path.write_bytes(b'ERROR 1366 (HY000) at line 2: Invalid text\n')
            return {'exitCode': 1, 'childrenCleaned': True}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(ik, '_equality_work', return_value={'canonical': 'abcd'}), \
                patch.object(ik.driver, 'sql', side_effect=self.sql), \
                patch.object(ik.common, 'capture_group', side_effect=capture):
            result = ik.equality_work(SimpleNamespace(obclient='/client', port=1234), Path(tmp))
        decoded = json.loads(bytes.fromhex(result['canonical']))
        self.assertEqual(decoded['collation'], 'abcd')
        self.assertIn('ik_utf8\t[]', bytes.fromhex(decoded['ikOutput']).decode())
        self.assertEqual([row['exitCode'] for row in decoded['sqlOutcomes']], [1] * 11)
        self.assertTrue(all(row['serverErrorCode'] == 1366 for row in decoded['sqlOutcomes']))
        self.assertTrue(result['repeatedCallsShareConnection'])

    def test_malformed_text_may_complete_successfully_and_preserves_that_status(self):
        def capture(command, path, cwd, timeout, **kwargs):
            path.write_bytes(b'[]\n')
            return {'exitCode': 0, 'childrenCleaned': True}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(ik, '_equality_work', return_value={'canonical': 'abcd'}), \
                patch.object(ik.driver, 'sql', side_effect=self.sql), \
                patch.object(ik.common, 'capture_group', side_effect=capture):
            result = ik.equality_work(SimpleNamespace(obclient='/client', port=1234), Path(tmp))
        self.assertTrue(all(row['exitCode'] == 0 for row in result['charsetOutcomes']))

    def test_wide_charset_rejections_are_compared_without_aborting_the_valid_suite(self):
        def capture(command, path, cwd, timeout, **kwargs):
            path.write_bytes(b'ERROR 1235 (42000) at line 2: Charset is not supported\n')
            return {'exitCode': 1, 'childrenCleaned': True}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(ik, '_equality_work', return_value={'canonical': 'abcd'}), \
                patch.object(ik.driver, 'sql', side_effect=self.sql), \
                patch.object(ik.common, 'capture_group', side_effect=capture):
            result = ik.equality_work(SimpleNamespace(obclient='/client', port=1234), Path(tmp))
        wide = result['charsetOutcomes'][:len(ik.WIDE_SQL_CASES)]
        self.assertEqual([row['name'] for row in wide], [row[0] for row in ik.WIDE_SQL_CASES])
        self.assertTrue(all(row['exitCode'] == 1 and row['serverErrorCode'] == 1235
                            and row['unsupportedMessage'] for row in wide))
        self.assertNotIn('_utf16 ', ik.IK_SQL)
        self.assertNotIn('_utf16le ', ik.IK_SQL)

    def test_null_binary_boundary_retains_exact_server_error_and_skips_required_success(self):
        raw = b'ERROR 1235 (0A000) at line 3: ik with the binary charset is not supported\n'
        submitted = []
        def capture(command, path, cwd, timeout, **kwargs):
            submitted.append(kwargs['stdin'])
            path.write_bytes(raw)
            return {'exitCode': 1, 'childrenCleaned': True}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(ik, '_equality_work', return_value={'canonical': 'abcd'}), \
                patch.object(ik.driver, 'sql', side_effect=self.sql), \
                patch.object(ik.common, 'capture_group', side_effect=capture):
            out = Path(tmp)
            result = ik.equality_work(SimpleNamespace(obclient='/client', port=1234), out)
            path = out / 'ik-charset-case-null-binary-input.log'
            self.assertEqual(path.read_bytes(), raw)
        self.assertIsNone(re.search(r'TOKENIZE\s*\(\s*NULL', ik.IK_SQL, re.I))
        boundary = next(row for row in result['charsetOutcomes'] if row['name'] == 'null-binary-input')
        self.assertEqual(boundary['exitCode'], 1)
        self.assertEqual(boundary['serverErrorCode'], 1235)
        self.assertEqual(boundary['sqlState'], '0A000')
        self.assertEqual(bytes.fromhex(boundary['output']), raw)
        self.assertTrue(boundary['unsupportedMessage'])
        self.assertEqual(submitted[-1].decode().splitlines()[2], "SELECT TOKENIZE(NULL, 'ik');")
        decoded = json.loads(bytes.fromhex(result['canonical']))
        self.assertIn(boundary, decoded['sqlOutcomes'])

    def test_required_null_case_declares_the_supported_utf8_string_type(self):
        self.assertIn("SELECT 'ik_typed_null', TOKENIZE(CAST(NULL AS CHAR CHARACTER SET utf8mb4) "
                      "COLLATE utf8mb4_general_ci, 'ik');", ik.IK_SQL)
        self.assertIn("SELECT 'ik_empty', TOKENIZE(_utf8mb4 '' COLLATE utf8mb4_general_ci, 'ik');", ik.IK_SQL)

    def test_same_connection_suite_covers_both_modes_and_the_real_batch_boundary(self):
        repeated = re.findall(r"REPEAT\(_utf8mb4 X'([0-9A-F]+)', (\d+)\)", ik.IK_SQL)
        self.assertEqual(len(repeated), 2)
        header = Path(__file__).resolve().parents[3] / 'src/storage/fts/ob_ik_ft_parser.h'
        limit = int(re.search(r'SEGMENT_LIMIT\s*=\s*(\d+)', header.read_text())[1])
        for value, count in repeated:
            expanded = bytes.fromhex(value).decode('utf-8') * int(count)
            self.assertGreater(len(expanded), limit)
            self.assertTrue(any(index > limit for index, char in enumerate(expanded) if char == ' '))
        for label in ('ik_long_smart', 'ik_long_max_word', 'ik_binary_session', 'ik_repeated_smart'):
            self.assertIn("SELECT '" + label + "'", ik.IK_SQL)

    def test_incomplete_valid_output_and_unexpected_client_failures_are_rejected(self):
        for mode in ('missing-output', 'timeout', 'connection-failure'):
            def capture(command, path, cwd, timeout, **kwargs):
                path.write_bytes(b'ERROR 2002 (HY000): Connection failed\n')
                return {'exitCode': 124 if mode == 'timeout' else 1, 'childrenCleaned': True}
            sql = (lambda *args: '') if mode == 'missing-output' else self.sql
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(ik, '_equality_work', return_value={'canonical': 'abcd'}), \
                    patch.object(ik.driver, 'sql', side_effect=sql), \
                    patch.object(ik.common, 'capture_group', side_effect=capture), self.assertRaises(RuntimeError):
                ik.equality_work(SimpleNamespace(obclient='/client', port=1234), Path(tmp))

    def test_lost_connection_with_a_line_number_is_rejected_and_raw_log_retained(self):
        raw = b'ERROR 2013 (HY000) at line 2: Lost connection to server during query\n'
        def capture(command, path, cwd, timeout, **kwargs):
            path.write_bytes(raw)
            return {'exitCode': 1, 'childrenCleaned': True}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(ik, '_equality_work', return_value={'canonical': 'abcd'}), \
                patch.object(ik.driver, 'sql', side_effect=self.sql), \
                patch.object(ik.common, 'capture_group', side_effect=capture):
            out = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, 'lost its client connection'):
                ik.equality_work(SimpleNamespace(obclient='/client', port=1234), out)
            path = out / ('ik-charset-case-' + ik.WIDE_SQL_CASES[0][0] + '.log')
            self.assertEqual(path.read_bytes(), raw)

    def test_prebuild_wraps_existing_baseline_instance_and_restores_callback(self):
        import fts_expr_cache_targeted as targeted
        original = targeted.run
        args = SimpleNamespace(root=Path('/run'))
        def parent_prebuild(args):
            return targeted.run(None, None, args, None)
        with patch.object(ik, 'equality_work', return_value={'canonical': 'ik'}), \
                patch.object(ik, '_prebuild') as outer:
            outer.side_effect = parent_prebuild
            with patch.object(targeted, 'run', return_value={'canonical': 'original'}) as mocked:
                result = ik.prebuild(args)
                self.assertIs(targeted.run, mocked)
        self.assertEqual(result['ikCharsetPreflight']['canonical'], 'ik')
        self.assertIs(targeted.run, original)

    def test_prebuild_restores_original_callback_after_a_failure(self):
        import fts_expr_cache_targeted as targeted
        original = targeted.run
        with patch.object(ik, '_prebuild', side_effect=RuntimeError('preflight failed')):
            with self.assertRaisesRegex(RuntimeError, 'preflight failed'):
                ik.prebuild(SimpleNamespace())
        self.assertIs(targeted.run, original)

    def test_prepare_records_the_new_scope_in_both_plan_views(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(root=Path(tmp))
            def prepare(args):
                ik.driver.dump(args.root / 'plan.json', {'scope': 'old', 'proof': {'input': 'original'}})
                ik.driver.dump(args.root / 'report.json', {'status': 'prepared'})
            with patch.object(ik, '_prepare', side_effect=prepare):
                ik.prepare(args)
            plan = json.loads((args.root / 'plan.json').read_text())
            report = json.loads((args.root / 'report.json').read_text())
        self.assertEqual(plan['scope'], ik.SCOPE)
        self.assertEqual(report['validationScope'], ik.SCOPE)
        self.assertEqual(report['plan'], plan)
        self.assertEqual(plan['proof'], {'input': 'original'})

    def test_final_validation_checks_the_prebuild_ik_sql_receipt(self):
        for canonical in ('same', 'changed'):
            with self.subTest(canonical=canonical), tempfile.TemporaryDirectory() as tmp:
                args = SimpleNamespace(root=Path(tmp))
                report = {'targeted': {'baseline': {'ikCharsetPreflight': {'canonical': 'same'}}},
                          'equality': {'baseline': {'canonical': canonical}}}
                ik.driver.dump(args.root / 'report.json', report)
                with patch.object(ik, '_validate'), patch.object(ik, 'verify_header_dependencies'):
                    if canonical == 'same':
                        ik.validate(args)
                        result = json.loads((args.root / 'report.json').read_text())
                        self.assertTrue(result['baselineIkCharsetPreflightPassed'])
                        self.assertEqual(result['validationScope'], ik.SCOPE)
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'later fresh-instance'):
                            ik.validate(args)

    def test_private_parent_routes_all_required_hooks_to_the_new_scope(self):
        self.assertIs(ik.driver.source_proof, ik.source_proof)
        self.assertIs(ik.parent.source_proof, ik.source_proof)
        self.assertIs(ik.parent.equality_work, ik.equality_work)
        self.assertIs(ik.parent.validate, ik.validate)
        self.assertIs(ik.driver.build_profile, ik.build_profile)
        self.assertIs(ik.driver.prepare, ik.prepare)
        self.assertIs(ik.driver.prebuild, ik.prebuild)


class WorkflowTests(unittest.TestCase):
    def test_push_filters_and_preservation_order_match_this_candidate(self):
        directory = Path(__file__).resolve().parents[2] / 'workflows'
        text = (directory / 'fts-ik-charset-validation.yml').read_text()
        header = text.split('permissions:', 1)[0]
        expected = set(ik.PRODUCTS[1:]) | {
            '.github/script/seekdb/fts_ik_charset_validation.py',
            '.github/script/seekdb/test_fts_ik_charset_validation.py',
            '.github/workflows/fts-ik-charset-validation.yml',
        }
        self.assertEqual(set(re.findall(r'^      - (.+)$', header, re.M)), expected)
        self.assertIn('branches: [perf/fts-profile-diagnostics-20261111]', header)
        self.assertLess(text.index('name: Preserve the full candidate before SQL validation'),
                        text.index('name: Validate native SQL and original nine-metric ABBA'))
        self.assertIn('--target seekdb -j2', text)
        old = (directory / 'fts-profile-diagnostics.yml').read_text().split('permissions:', 1)[0]
        self.assertTrue(expected <= set(re.findall(r'^      - (.+)$', old, re.M)))


if __name__ == '__main__':
    unittest.main()
