"""Offline regressions for the COUNT-consumer validation entry point."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import fts_count_only_validation as count_only


def setUpModule():
    global _diagnostic_mode
    _diagnostic_mode = patch.dict(os.environ, {'COUNT_BASELINE_DIAGNOSTICS_ONLY': '0'})
    _diagnostic_mode.start()


def tearDownModule():
    _diagnostic_mode.stop()


def retrieval_plan(relevance, sort_keys='nil', index_back='true'):
    return (
        '|0|SCALAR GROUP BY||1|1|\n'
        '|1|  TEXT RETRIEVAL SCAN|docs(fts)|32|1|\n'
        'Outputs & filters:\n'
        '  0 - output([T_FUN_COUNT(*)]), filter(nil)\n'
        '  1 - output([docs.id]), filter(nil)\n'
        f'      is_index_back={index_back}, is_global_index=false,\n'
        f'      calc_relevance={str(relevance).lower()}, match_expr(MATCH(docs.body)),\n'
        f'      sort_keys({sort_keys}), limit(2), offset(nil)\n'
    )


def accepted_matrix(role):
    return dict(strictCountAndPlanValidation=dict(
        passed=True, newDenseCountExpectedResultsPassed=True if role == 'candidate' else None,
        legacyScoredControlErrors=[], inheritedBaselineCountErrors=[],
        rawSqlCounts=dict(total=138, succeeded=138, failed=0), allRawSqlSucceeded=True))


class CountConsumerTests(unittest.TestCase):
    def test_candidate_count_plan_rejects_relevance_and_implicit_sort(self):
        count_only.check_count_plan(retrieval_plan(False), False, True)
        with self.assertRaises(RuntimeError):
            count_only.check_count_plan(retrieval_plan(True), False, True)
        with self.assertRaises(RuntimeError):
            count_only.check_count_plan(retrieval_plan(False, '[MATCH(docs.body), DESC]'), False, True)

    def test_index_back_is_recorded_without_requiring_elimination(self):
        for value in ('true', 'false'):
            proof = count_only.check_count_plan(retrieval_plan(False, index_back=value), False, True)
            self.assertEqual(proof['actualIndexBack'], value == 'true')

    def test_count_statements_have_standalone_labels_and_fixed_expected_counts(self):
        expected = dict(count_only.COUNT_CASES)
        self.assertEqual(expected['plain_count'][1], 32)
        self.assertEqual(expected['limit_2'][1], 2)
        self.assertEqual(expected['limit_32'][1], 32)
        self.assertEqual(expected['limit_40'][1], 32)
        self.assertEqual(expected['offset_30'][1], 2)
        self.assertEqual(expected['offset_33'][1], 0)
        self.assertEqual(expected['limit_0'][1], 0)
        self.assertEqual(expected['no_match'][1], 0)
        for label, (query, result) in count_only.COUNT_CASES:
            self.assertIn(f"SELECT '{label}';\n{query}", count_only.COUNT_SQL)
            if 'COUNT(' in query:
                self.assertNotIn("SELECT '", query)

    def test_missing_ambiguous_or_malformed_plan_evidence_is_rejected(self):
        plan = retrieval_plan(False)
        for text in ('false', plan + plan,
                     plan.replace('calc_relevance=false', 'calc_relevance=unknown'),
                     plan.replace('sort_keys(nil)', 'sort_keys([malformed')):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                count_only.check_count_plan(text, False, True)

    def test_sort_keys_must_belong_to_the_retrieval_operator(self):
        text = retrieval_plan(False).replace(
            '  0 - output([T_FUN_COUNT(*)]), filter(nil)',
            '  0 - output([T_FUN_COUNT(*)]), filter(nil)\n      sort_keys([other_column])')
        self.assertFalse(count_only.check_count_plan(text, False, True)['hasImplicitSortKeys'])


class SourceProofTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name)
        self.args = SimpleNamespace(source=self.source, driver_sha='c' * 40)
        for name in count_only.driver.CASES:
            suite, case = name.split('.')
            for directory, suffix in (('t', '.test'), ('r/mysql', '.result')):
                path = self.source / 'tools/deploy/mysql_test/test_suite' / suite / directory / (case + suffix)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'unchanged native input\n')

    def proof(self, changed):
        def git(source, *args):
            if args[0] == 'rev-parse':
                return count_only.BASE_TREE.encode()
            if args[0] == 'diff':
                return '\n'.join(changed).encode() if '--name-only' in args else b'actual cpp patch'
            if args[0] == 'show':
                commit, relative = args[1].split(':', 1)
                return b'unchanged native input\n' if commit == count_only.BASE else b'github tool blob'
            raise AssertionError(args)
        with patch.object(count_only.common, 'source_identity', return_value={'tree': 'd' * 40}), \
                patch.object(count_only.common, 'git', side_effect=git), \
                patch.object(count_only.driver, 'inventory', return_value={'original': 'hash'}), \
                patch.object(count_only.driver, 'git_identity', return_value='verified identity'):
            return count_only.source_proof(self.args)

    def test_exactly_two_rewrite_products_and_recorded_ci_history_are_allowed(self):
        ci = ['.github/workflows/fts-ik-charset-validation.yml',
              '.github/script/seekdb/fts_count_only_validation.py']
        result = self.proof(list(count_only.PRODUCTS) + ci)
        self.assertEqual(result['productChanges'], sorted(count_only.PRODUCTS))
        self.assertEqual(result['ciChanges'], sorted(ci))
        self.assertEqual(result['originalInputCount'], 75)
        self.assertEqual(result['simpleQueryExplainAudit']['countParentsWithDerivedLimit'], [])

    def test_unrelated_missing_or_historical_product_changes_are_rejected(self):
        for changed in (list(count_only.PRODUCTS) + ['src/extra.cpp'],
                        list(count_only.PRODUCTS)[:-1],
                        list(count_only.PRODUCTS) + ['src/oblib/lib/charset/ob_charset.cpp']):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                self.proof(changed)

    def test_original_native_input_change_is_rejected(self):
        case = count_only.driver.CASES[0].split('.')[1]
        path = self.source / 'tools/deploy/mysql_test/test_suite/fts_index/t' / (case + '.test')
        path.write_bytes(b'changed native input')
        with self.assertRaises(RuntimeError):
            self.proof(list(count_only.PRODUCTS))


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
        for name in count_only.PRODUCTS:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('source fixture\n', encoding='utf-8')
        self.obj = self.build / 'CMakeFiles/rewrite.dir/Unity/unit.cxx.o'
        self.obj.parent.mkdir(parents=True)
        self.obj.write_bytes(b'actual object fixture')
        self.row = dict(directory=str(self.build), file=str(self.source / count_only.REWRITE_CPP),
                        arguments=[self.compiler, '-o', str(self.obj), '-c', str(self.source / count_only.REWRITE_CPP)])
        self.rows = [self.row]
        self.dep = Path(str(self.obj) + '.d')
        self.dep.write_text(self.obj.as_posix() + ': ' + (self.source / count_only.REWRITE_HEADER).as_posix() + '\n',
                            encoding='utf-8')

    def original_profile(self, args):
        out = args.root / 'evidence/candidate' / ('profile-' + args.phase)
        out.mkdir(parents=True, exist_ok=True)
        count_only.driver.dump(out / 'profile-proof.json',
                               {'signature': {'compilers': {'CXX': {'COMPILER': self.compiler}}}})
        return count_only.profile.EXPECTED.copy(), 'rustc 1.98.1 fixture'

    def run_profile(self):
        (self.build / 'compile_commands.json').write_text(json.dumps(self.rows), encoding='utf-8')
        with patch.object(count_only, '_build_profile', side_effect=self.original_profile):
            count_only.build_profile(self.args)

    def test_direct_object_and_modified_header_are_bound_after_snapshot(self):
        self.run_profile()
        self.args.phase = 'snapshot'
        self.run_profile()
        proof_path = self.args.root / 'evidence/candidate/count-header-dependencies.json'
        self.assertFalse(proof_path.exists())
        count_only.verify_header_dependencies(self.args)
        proof = json.loads(proof_path.read_text())
        self.assertTrue(proof['passed'])
        self.assertTrue(proof['dependencies']['headerPresent'])
        self.assertEqual(proof['objectSha256'], hashlib.sha256(self.obj.read_bytes()).hexdigest())

    def test_unity_compilation_unit_is_recognized(self):
        unity = self.build / 'CMakeFiles/rewrite.dir/Unity/unit.cxx'
        unity.write_text('#include "' + (self.source / count_only.REWRITE_CPP).as_posix() + '"\n', encoding='utf-8')
        self.row['file'] = str(unity)
        self.run_profile()

    def test_missing_or_duplicate_compilation_unit_is_rejected(self):
        for rows in ([], [self.row, self.row]):
            self.rows = rows
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                self.run_profile()

    def test_changed_source_or_command_after_prebuild_is_rejected(self):
        self.run_profile()
        self.args.phase = 'snapshot'
        self.row['arguments'].insert(1, '-DCHANGED')
        with self.assertRaises(RuntimeError):
            self.run_profile()
        self.row['arguments'].remove('-DCHANGED')
        (self.source / count_only.REWRITE_HEADER).write_text('changed header', encoding='utf-8')
        with self.assertRaises(RuntimeError):
            self.run_profile()

    def test_wrong_compiler_or_object_outside_build_is_rejected(self):
        self.row['arguments'][0] = 'different-compiler'
        with self.assertRaises(RuntimeError):
            self.run_profile()
        self.row['arguments'][0] = self.compiler
        self.row['arguments'][2] = str(self.root / 'outside.o')
        with self.assertRaises(RuntimeError):
            self.run_profile()

    def test_header_proof_requires_exact_target_and_preserves_failed_dependency_evidence(self):
        self.run_profile()
        self.args.phase = 'snapshot'
        self.run_profile()
        self.dep.write_text('unrelated.o: ' + (self.source / count_only.REWRITE_HEADER).as_posix() + '\n', encoding='utf-8')
        with self.assertRaises(RuntimeError):
            count_only.verify_header_dependencies(self.args)
        proof = json.loads((self.args.root / 'evidence/candidate/count-header-dependencies.json').read_text())
        self.assertFalse(proof['passed'])
        self.assertEqual(proof['dependencies']['records'][0]['unfoldedMatchingRules'], [])
        saved = Path(proof['dependencies']['records'][0]['rawEvidencePath'])
        self.assertEqual(saved.read_bytes(), self.dep.read_bytes())

    def test_dependency_target_relative_to_build_root_is_accepted(self):
        self.dep.write_text(self.obj.relative_to(self.build).as_posix() + ': '
                            + (self.source / count_only.REWRITE_HEADER).as_posix() + '\n', encoding='utf-8')
        out = self.root / 'dependency-output'
        out.mkdir()
        proof = count_only.dependency_record(self.obj, self.row, self.source, out)
        self.assertTrue(proof['headerPresent'])

    def test_complete_object_dependencies_do_not_read_the_directory_fallback(self):
        fallback = self.obj.parent.parent / 'compiler_depend.make'
        fallback.write_text('unneeded directory-wide dependency data', encoding='utf-8')
        out = self.root / 'dependency-output'
        out.mkdir()
        original = count_only.profile.regular

        def regular(path, cap):
            if path == fallback:
                raise RuntimeError('Unneeded fallback must not block complete object dependencies')
            return original(path, cap)

        with patch.object(count_only.profile, 'regular', side_effect=regular):
            proof = count_only.dependency_record(self.obj, self.row, self.source, out)
        self.assertTrue(proof['headerPresent'])
        self.assertEqual(len(proof['records']), 1)

    def test_changed_object_is_rejected_before_header_acceptance(self):
        self.run_profile()
        self.args.phase = 'snapshot'
        self.run_profile()
        self.obj.write_bytes(b'changed object')
        with self.assertRaises(RuntimeError):
            count_only.verify_header_dependencies(self.args)


class CallbackTests(unittest.TestCase):
    COUNT_OUTPUT = ('rows_remaining\n69\nnull_rows\n1\nempty_rows\n1\nplain_count\n32\n'
                    'limit_2\n2\nlimit_32\n32\nlimit_40\n32\nlimit_0\n0\n'
                    'offset_30\n2\noffset_33\n0\nno_match\n0\ncount_column\n0\nparent_filter\n5\n')

    def fixture_sql(self, role, bad_count=False, bad_control=False):
        def sql(call, out, label, query):
            (out / (label + '.sql')).write_text(query, encoding='utf-8')
            if label == 'cs45-equality':
                labels = re.findall(r"^SELECT '([^']+)'", count_only.parent.REGRESSION_SQL, re.M)
                text = ''.join(name + '\t0\n' for name in labels)
            elif label == 'count-consumer':
                text = self.COUNT_OUTPUT.replace('plain_count\n32', 'plain_count\n31') if bad_count else self.COUNT_OUTPUT
            else:
                name = label.removeprefix('count-plan-')
                scored = name in count_only.SCORING_CONTROLS or role == 'baseline'
                if bad_control and name in count_only.SCORING_CONTROLS:
                    scored = False
                text = retrieval_plan(scored, '[MATCH(docs.body), DESC]' if scored else 'nil')
            (out / (label + '.log')).write_bytes(text.encode('utf-8'))
            return text
        return sql

    def test_full_count_stdout_is_compared_while_plan_changes_remain_explicit(self):
        results = {}
        with tempfile.TemporaryDirectory() as tmp:
            for role in ('baseline', 'candidate'):
                out = Path(tmp) / ('equality-' + role)
                out.mkdir()
                with patch.object(count_only.driver, 'sql', side_effect=self.fixture_sql(role)), \
                        patch.object(count_only, 'matrix_work', return_value=accepted_matrix(role)) as matrix:
                    results[role] = count_only.equality_work(None, out)
                matrix.assert_called_once_with(None, out, role, strict=True)
                self.assertEqual((out / 'count-consumer.log').read_text(), self.COUNT_OUTPUT)
                self.assertEqual(len(results[role]['countConsumerPlans']), 10)
        self.assertEqual(results['baseline']['canonical'], results['candidate']['canonical'])
        self.assertTrue(results['baseline']['countConsumerPlans']['limit_2']['actualCalcRelevance'])
        self.assertFalse(results['candidate']['countConsumerPlans']['limit_2']['actualCalcRelevance'])

    def test_wrong_count_and_scored_control_regression_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'equality-candidate'
            out.mkdir()
            for bad_count, bad_control in ((True, False), (False, True)):
                with self.subTest(bad_count=bad_count), \
                        patch.object(count_only.driver, 'sql', side_effect=self.fixture_sql('candidate', bad_count, bad_control)), \
                        self.assertRaises(RuntimeError):
                    count_only.equality_work(None, out)

    def test_original_query_limit_plan_changes_for_candidate_only(self):
        args = SimpleNamespace(source=Path('/candidate'), root=Path('/owned'), driver_sha='c' * 40)
        identities = {role: {'binary': '/runtime/' + role} for role in ('baseline', 'candidate')}
        queries = {'query_cn': 'original count SQL', 'query_limit': 'original derived-limit SQL',
                   'control_score_projection': 'original projection SQL'}
        for role in identities:
            def sql(call, out, label, query):
                expected = label.startswith('control_') or (label == 'query_limit-explain' and role == 'baseline')
                return retrieval_plan(expected, '[MATCH(docs.body), DESC]' if expected else 'nil')
            with patch.object(count_only.common, 'benchmark_work', return_value=lambda call, out:
                              {'passed': True, 'metrics': {'original': 1}}) as factory, \
                    patch.object(count_only.common, 'queries_from_benchmark', return_value={}), \
                    patch.object(count_only.common, 'plan_queries', return_value=queries), \
                    patch.object(count_only.driver, 'sql', side_effect=sql):
                result = count_only.benchmark_callback(args, identities, role, {}, None)(None, Path('/out'), 42)
            self.assertFalse(factory.call_args.args[3])
            self.assertEqual(result['roleConsistentPlans']['query_limit']['actualCalcRelevance'], role == 'baseline')

    def test_failed_benchmark_does_not_execute_plans(self):
        args = SimpleNamespace(source=Path('/candidate'), root=Path('/owned'), driver_sha='c' * 40)
        with patch.object(count_only.common, 'benchmark_work', return_value=lambda call, out: {'passed': False}), \
                patch.object(count_only.driver, 'sql') as sql:
            result = count_only.benchmark_callback(args, {'candidate': {'binary': '/elf'}}, 'candidate', {}, None)(None, None, 42)
        self.assertFalse(result['passed'])
        sql.assert_not_called()

    def test_prebuild_includes_count_fixture_and_restores_original_targeted_entry(self):
        import fts_expr_cache_targeted as targeted
        original = lambda *args: {'passed': True, 'canonical': {}}
        with patch.object(targeted, 'run', original), \
                patch.object(count_only, 'equality_work', return_value={'passed': True}) as equality:
            def prebuild(args):
                result = targeted.run(None, Path('/targeted-baseline'), args, None)
                self.assertEqual(result['countOnlyPreflight'], {'passed': True})
            with patch.object(count_only, '_prebuild', side_effect=prebuild):
                count_only.prebuild(None)
            self.assertIs(targeted.run, original)
        equality.assert_called_once()

    def test_prebuild_failure_restores_original_targeted_entry(self):
        import fts_expr_cache_targeted as targeted
        original = targeted.run
        with patch.object(count_only, '_prebuild', side_effect=RuntimeError('baseline SQL failed')):
            with self.assertRaises(RuntimeError):
                count_only.prebuild(None)
        self.assertIs(targeted.run, original)

    def test_header_failure_prevents_parent_validation(self):
        with patch.object(count_only, 'verify_header_dependencies', side_effect=RuntimeError('missing header')), \
                patch.object(count_only, '_validate') as parent:
            with self.assertRaises(RuntimeError):
                count_only.validate(None)
        parent.assert_not_called()

    def test_baseline_preflight_must_match_later_fresh_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = SimpleNamespace(root=root)
            for same in (False, True):
                report = {'targeted': {'baseline': {'countOnlyPreflight': {'canonical': '00'}}},
                          'equality': {'baseline': {'canonical': '00' if same else '01', 'countMatrix': accepted_matrix('baseline')},
                                       'candidate': {'countMatrix': accepted_matrix('candidate')}}}
                count_only.driver.dump(root / 'report.json', report)
                with patch.object(count_only, 'verify_header_dependencies'), patch.object(count_only, '_validate'):
                    if same:
                        count_only.validate(args)
                        self.assertTrue(json.loads((root / 'report.json').read_text())['countOnlySqlParityPassed'])
                    else:
                        with self.assertRaises(RuntimeError):
                            count_only.validate(args)

    def test_private_module_hooks_keep_existing_bounded_lifecycle(self):
        self.assertIs(count_only.parent.validate, count_only.validate)
        self.assertIs(count_only.driver.validate, count_only.parent.bounded_validate)
        self.assertIs(count_only.driver.prebuild, count_only.prebuild)
        self.assertIs(count_only.driver.build_profile, count_only.build_profile)
        self.assertIs(count_only.driver.source_proof, count_only.source_proof)


class WorkflowTests(unittest.TestCase):
    def test_baseline_prebuild_and_full_elf_upload_precede_validation(self):
        path = Path(__file__).resolve().parents[2] / 'workflows/fts-count-only-validation.yml'
        text = path.read_text(encoding='utf-8')
        markers = ('--phase prebuild', '--build build_release --target seekdb -j2',
                   '--phase snapshot', '--phase preserve', 'name: fts-count-only-candidate-', '--phase validate')
        positions = [text.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("artifact-ids: '13828533821'", text)
        self.assertIn("run-id: '42953505169'", text)
        self.assertIn("cache: 'false'", text)
        self.assertIn('CARGO_BUILD_JOBS: \'2\'', text)
        self.assertNotIn('ob_charset.cpp', text.split('permissions:', 1)[0])

    def test_explicit_diagnostic_mode_skips_all_three_candidate_steps(self):
        path = Path(__file__).resolve().parents[2] / 'workflows/fts-count-only-validation.yml'
        text = path.read_text(encoding='utf-8')
        self.assertIn("COUNT_BASELINE_DIAGNOSTICS_ONLY: '0'", text)
        condition = "if: ${{ env.COUNT_BASELINE_DIAGNOSTICS_ONLY != '1' }}"
        self.assertEqual(text.count(condition), 3)
        for name in ('Build the complete candidate once with the original two-job profile',
                     'Preserve the full candidate before SQL validation',
                     'Validate native SQL and original nine-metric ABBA'):
            self.assertIn('      - name: ' + name + '\n        ' + condition, text)


class DiagnosticTests(unittest.TestCase):
    def test_matrix_preserves_original_corpus_and_varies_one_named_factor(self):
        matrices = {row['name']: count_only.diagnostic_fixture(row) for row in count_only.DIAGNOSTIC_FIXTURES}
        dense = matrices['beng_deleted_dense']
        expected_setup = count_only.DENSE_COUNT_SETUP.replace('CREATE DATABASE fts_count_consumer;\n', '').replace(
            count_only.TABLE, dense['table'])
        self.assertEqual(dense['setup'], expected_setup)
        self.assertEqual(dense['positiveRows'], 32)
        self.assertEqual(dense['totalRows'], 34)
        no_delete = matrices['beng_dense_no_delete']
        self.assertEqual(no_delete['positiveRows'], 35)
        self.assertEqual(no_delete['totalRows'], 37)
        self.assertNotIn('DELETE FROM', no_delete['setup'])
        ik = matrices['ik_deleted_dense']
        self.assertEqual(ik['setup'], dense['setup'].replace(dense['table'], ik['table']).replace('PARSER beng', 'PARSER ik'))
        mixed = matrices['beng_deleted_mixed']
        self.assertEqual(mixed['totalRows'], 69)
        self.assertEqual(mixed['setup'].count("'unrelated zebra orchard'"), 35)
        self.assertEqual(count_only.COUNT_SETUP.count("'unrelated zebra orchard'"), 35)
        self.assertIn('DELETE FROM', count_only.COUNT_SETUP)
        self.assertEqual(dict(count_only.COUNT_CASES)['rows_remaining'][1], 69)
        for matrix in matrices.values():
            self.assertEqual(matrix['setup'].count("'database performance audit'"), 35)
            self.assertEqual(len(matrix['cases']), 15)
            self.assertEqual(len(matrix['plans']), 18)
            cases = {name: (query, value) for name, query, value in matrix['cases']}
            self.assertEqual(cases['limit_20'][1], 20)
            self.assertIn('SELECT 1 FROM ', cases['projection_constant_limit_2'][0])
            self.assertEqual(cases['projection_constant_limit_2'][1], 2)
            self.assertEqual(cases['offset_30'][1], 5 if matrix['name'] == 'beng_dense_no_delete' else 2)
            self.assertEqual(cases['offset_33'][1], 2 if matrix['name'] == 'beng_dense_no_delete' else 0)

    def owned(self, root, raw=b'[2026-11-12 00:00:00.000001] WARN original root-cause detail\n', role='baseline'):
        out = root / 'validation' / ('targeted-baseline' if role == 'baseline' else 'equality-candidate')
        log = out / 'database/log/seekdb.log'
        log.parent.mkdir(parents=True)
        log.write_bytes(raw)
        identity = dict(role=role, sourceCommit=count_only.BASE if role == 'baseline' else 'c' * 40,
                        sourceTree=count_only.BASE_TREE if role == 'baseline' else 'd' * 40,
                        sha256=count_only.parent.BASE_ELF_SHA if role == 'baseline' else 'e' * 64,
                        buildId=count_only.parent.BASE_BUILD_ID if role == 'baseline' else 'f' * 40,
                        binary=str(root / 'runtime' / role / 'seekdb'))
        identity_path = root / 'runtime' / role / 'identity.json'
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        count_only.driver.dump(identity_path, identity)
        count_only.driver.dump(root / 'plan.json', {'proof': {'driverCommit': 'c' * 40, 'driverTree': 'd' * 40}})
        count_only.driver.dump(out / 'owned-process.json', dict(role=role, binary=identity['binary'], sha256=identity['sha256']))
        call = SimpleNamespace(base_dir=out / 'database', obclient=Path('/client'), port=7500)
        return call, out, log

    def capture(self, failure_label=None, connection_code=None):
        calls = []

        def run(command, path, cwd, timeout, **kwargs):
            calls.append(path.stem)
            failed = path.stem == failure_label
            code = connection_code if failed and connection_code else 4016
            if failed:
                raw = f'ERROR {code} (HY000) at line 3: diagnostic server outcome\n'.encode()
            elif '-explain-' in path.stem:
                raw = retrieval_plan(True, '[MATCH(docs.body), DESC]').encode()
            else:
                raw = b'1\n'
            path.write_bytes(raw)
            return dict(exitCode=1 if failed else 0, childrenCleaned=True)
        return run, calls

    def test_first_sql_error_does_not_truncate_matrix_or_claim_correctness(self):
        with tempfile.TemporaryDirectory() as tmp:
            call, out, log = self.owned(Path(tmp))
            capture, calls = self.capture('diag-beng_deleted_dense-count-limit_2')
            with patch.object(count_only.common, 'capture_group', side_effect=capture):
                result = count_only.diagnostic_work(call, out, None, None)
            data = json.loads((out / 'count-matrix-diagnostics.json').read_text())
            self.assertEqual(len(calls), 138)
            self.assertEqual(len(data['outcomes']), 138)
            error = next(row for row in data['outcomes'] if row['label'] == 'diag-beng_deleted_dense-count-limit_2')
            self.assertEqual(error['serverErrorCode'], 4016)
            self.assertIn(b'ERROR 4016', bytes.fromhex(error['outputHex']))
            first = data['tables'][0]['firstErrorServerLog']
            self.assertTrue(Path(first['path']).is_file())
            self.assertEqual(data['tables'][0]['firstErrorSqlLabel'], error['label'])
            self.assertLessEqual(json.loads(Path(first['path']).read_text())['savedRawBytes'], 256 * 1024)
            self.assertTrue(result['passed'])
            self.assertTrue(result['diagnosticOnly'])
            self.assertFalse(result['correctnessPassed'])
            self.assertFalse(result['candidateBuildAttempted'])
            self.assertTrue(log.exists())
            self.assertFalse((out / 'own-stop.log').exists())

    def test_connection_failures_are_checked_after_complete_json_is_saved(self):
        for code in count_only.CONNECTION_ERRORS:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                call, out, log = self.owned(Path(tmp))
                capture, calls = self.capture('diag-beng_deleted_dense-count-limit_2', code)
                with patch.object(count_only.common, 'capture_group', side_effect=capture), self.assertRaises(RuntimeError):
                    count_only.diagnostic_work(call, out, None, None)
                data = json.loads((out / 'count-matrix-diagnostics.json').read_text())
                self.assertEqual(len(data['outcomes']), 138)
                self.assertEqual(len(calls), 138)
                self.assertTrue(any(row['connectionLost'] for row in data['outcomes']))
                self.assertTrue((out / 'count-baseline-live-log.json').is_file())

    def test_final_connection_failure_also_preserves_diagnostic_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            call, out, log = self.owned(Path(tmp))
            capture, calls = self.capture('diag-final-connection', 2006)
            with patch.object(count_only.common, 'capture_group', side_effect=capture), self.assertRaises(RuntimeError):
                count_only.diagnostic_work(call, out, None, None)
            data = json.loads((out / 'count-matrix-diagnostics.json').read_text())
            self.assertEqual(data['outcomes'][-1]['serverErrorCode'], 2006)

    def matrix_capture(self, role, wrong_count=False):
        records = {row['label']: row for row in MatrixPolicyTests().outcomes(role)}
        calls = []

        def capture(command, path, cwd, timeout, **kwargs):
            calls.append(path.stem)
            record = records[path.stem]
            raw = bytes.fromhex(record['outputHex'])
            if wrong_count and path.stem == 'diag-beng_deleted_dense-count-limit_2':
                raw = b'0\n'
            path.write_bytes(raw)
            return dict(exitCode=record['exitCode'], childrenCleaned=True)

        return capture, calls

    def test_strict_matrix_captures_all_sql_and_binds_each_actual_role(self):
        for role in ('baseline', 'candidate'):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                call, out, log = self.owned(Path(tmp), role=role)
                capture, calls = self.matrix_capture(role)
                with patch.object(count_only.common, 'capture_group', side_effect=capture):
                    result = count_only.matrix_work(call, out, role, strict=True)
                data = json.loads((out / 'count-matrix-diagnostics.json').read_text())
                self.assertEqual(len(calls), 138)
                self.assertEqual(len(data['outcomes']), 138)
                self.assertTrue(result['strictCountAndPlanValidation']['passed'])
                self.assertFalse(result['diagnosticOnly'])
                self.assertEqual(result['roleIdentity']['role'], role)
                self.assertEqual(result['candidateBuildAttempted'], role == 'candidate')
                self.assertEqual(len(data['strictCountAndPlanValidation']['legacyScoredControlErrors']), 2)
                self.assertEqual(sum(row['exitCode'] == 1 for row in data['outcomes']), 8 if role == 'baseline' else 2)

    def test_strict_matrix_saves_complete_evidence_before_rejecting_wrong_candidate_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            call, out, log = self.owned(Path(tmp), role='candidate')
            capture, calls = self.matrix_capture('candidate', wrong_count=True)
            with patch.object(count_only.common, 'capture_group', side_effect=capture), self.assertRaisesRegex(
                    RuntimeError, 'COUNT matrix failed'):
                count_only.matrix_work(call, out, 'candidate', strict=True)
            data = json.loads((out / 'count-matrix-diagnostics.json').read_text())
            self.assertEqual(len(calls), 138)
            self.assertEqual(len(data['outcomes']), 138)
            self.assertFalse(data['strictCountAndPlanValidation']['passed'])
            self.assertFalse(data['strictCountAndPlanValidation']['newDenseCountExpectedResultsPassed'])
            rejected = next(row for row in data['outcomes'] if row['label'] == 'diag-beng_deleted_dense-count-limit_2')
            self.assertEqual(rejected['observedCount'], 0)
            self.assertEqual(rejected['expectedCount'], 2)
            self.assertEqual(bytes.fromhex(rejected['outputHex']), b'0\n')
            self.assertTrue((out / 'count-baseline-live-log.json').is_file())

    def test_live_log_keeps_info_error_propagation_and_exact_offsets(self):
        raw = (b'[2026-11-12 00:00:00.000001] INFO context before\n'
               b'[2026-11-12 00:00:00.000002] INFO exec_ctx.errcode()=-4016\n'
               b'[2026-11-12 00:00:00.000003] INFO send_error_packet\n'
               b'[2026-11-12 00:00:00.000004] WDIAG detail\n'
               b'[2026-11-12 00:00:00.000005] EDIAG detail\n'
               b'[2026-11-12 00:00:00.000006] INFO context after\n')
        with tempfile.TemporaryDirectory() as tmp:
            call, out, log = self.owned(Path(tmp), raw)
            row = {'label': 'failed-query', 'serverLogOffsetBefore': 0, 'serverLogOffsetAfter': len(raw)}
            receipt = count_only.capture_live_diagnostic_log(call, out, [row])
            data = json.loads(Path(receipt['path']).read_text())
            self.assertEqual(data['initialSizePrefixSha256'], hashlib.sha256(raw).hexdigest())
            patterns = set()
            for record in data['records']:
                original = raw[record['offset']:record['offset'] + record['bytes']]
                self.assertEqual(bytes.fromhex(record['rawHex']), original)
                self.assertEqual(record['sha256'], hashlib.sha256(original).hexdigest())
                self.assertEqual(record['sqlAssociations'], ['failed-query'])
                patterns.update(record['matchedPatterns'])
            self.assertTrue({'error_4016', 'send_error_packet', 'wdiag', 'ediag'} <= patterns)
            self.assertTrue(data['capturedBeforeOwnedStop'])

    def test_oversized_lines_keep_hash_and_explicit_omission(self):
        raw = b'WARN ' + b'x' * 200 + b' ret=-4016\n'
        with tempfile.TemporaryDirectory() as tmp:
            call, out, log = self.owned(Path(tmp), raw)
            with patch.object(count_only, 'LIVE_LOG_LINE_CAP', 64):
                receipt = count_only.capture_live_diagnostic_log(call, out, [])
            data = json.loads(Path(receipt['path']).read_text())
            record = data['records'][0]
            self.assertEqual(record['bytes'], len(raw))
            self.assertEqual(record['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertNotIn('rawHex', record)
            self.assertIn('omittedReason', record)
            self.assertEqual(data['unsavedByteRanges'], [{'offset': 0, 'bytes': len(raw)}])

    def test_recent_record_eviction_is_bounded_and_accounted(self):
        raw = b''.join(b'WARN error detail ' + str(i).encode() + b'\n' for i in range(10))
        with tempfile.TemporaryDirectory() as tmp:
            call, out, log = self.owned(Path(tmp), raw)
            with patch.object(count_only, 'LIVE_LOG_RECORD_CAP', 3), patch.object(count_only, 'LIVE_LOG_BYTE_CAP', 60):
                receipt = count_only.capture_live_diagnostic_log(call, out, [])
            data = json.loads(Path(receipt['path']).read_text())
            self.assertLessEqual(len(data['records']), 3)
            self.assertLessEqual(data['savedRawBytes'], 60)
            self.assertGreater(data['evictedOlderSelectedRecords'], 0)
            self.assertEqual(data['matchedLineCount'], 10)
            self.assertTrue(data['unsavedByteRanges'])

    def test_live_log_snapshot_excludes_bytes_appended_after_initial_size(self):
        initial = b'WARN original error\nINFO original context\n'
        appended = b'ERROR appended after snapshot began\n'
        with tempfile.TemporaryDirectory() as tmp:
            call, out, log = self.owned(Path(tmp), initial)
            original_open = Path.open

            class GrowingReader:
                def __init__(self, stream):
                    self.stream, self.appended = stream, False

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.stream.close()

                def fileno(self):
                    return self.stream.fileno()

                def readline(self, size):
                    raw = self.stream.readline(size)
                    if not self.appended:
                        with original_open(log, 'ab') as writer:
                            writer.write(appended)
                        self.appended = True
                    return raw

            def open_path(path, mode='r', *args, **kwargs):
                stream = original_open(path, mode, *args, **kwargs)
                return GrowingReader(stream) if path == log and mode == 'rb' else stream

            with patch.object(Path, 'open', open_path):
                receipt = count_only.capture_live_diagnostic_log(call, out, [])
            data = json.loads(Path(receipt['path']).read_text())
            self.assertEqual(data['initialSizeBytes'], len(initial))
            self.assertEqual(data['finalSizeBytes'], len(initial) + len(appended))
            self.assertEqual(data['initialSizePrefixSha256'], hashlib.sha256(initial).hexdigest())
            saved = b''.join(bytes.fromhex(row['rawHex']) for row in data['records'])
            self.assertEqual(saved, initial)

    def test_diagnostic_prebuild_reuses_generic_status_receipt_and_cleanup_path(self):
        for code in (None, 2006):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / 'source'
                source.mkdir()
                helper = source / '.github/script/seekdb/fts_expr_cache_targeted.py'
                helper.parent.mkdir(parents=True)
                helper.write_bytes(b'original helper fixture\n')
                (root / 'evidence').mkdir()
                baseline_dir = root / 'runtime/baseline'
                baseline_dir.mkdir(parents=True)
                count_only.driver.dump(baseline_dir / 'identity.json', {'binary': str(baseline_dir / 'seekdb')})
                count_only.driver.dump(root / 'report.json', {'status': 'prepared', 'targeted': {}, 'instances': []})
                args = SimpleNamespace(root=root, source=source, driver_sha='c' * 40, phase='prebuild')
                capture, calls = self.capture('diag-beng_deleted_dense-count-limit_2', code)
                original_digest = count_only.common.digest
                events = []

                def digest(path):
                    return count_only.common.CLIENTS[path.name] if path.name in count_only.common.CLIENTS else original_digest(path)

                def owned(args, report, identities, role, name, work, **kwargs):
                    call, out, log = self.owned(root)
                    row = {'name': name, 'role': role, 'status': 'running'}
                    try:
                        row['work'] = work(call, out, 42)
                        row['status'] = 'passed'
                    except RuntimeError as error:
                        row.update(status='infrastructure_error', error=str(error))
                    finally:
                        self.assertTrue((out / 'count-baseline-live-log.json').exists())
                        self.assertTrue((out / 'count-matrix-diagnostics.json').exists())
                        events.append('owned-stop-and-cleanup')
                        row['cleanupPassed'] = True
                        count_only.driver.dump(out / 'result.json', row)
                        report['instances'].append(row)
                        count_only.driver.dump(root / 'report.json', report)
                    return row

                with patch.dict(os.environ, {'COUNT_BASELINE_DIAGNOSTICS_ONLY': '1'}), \
                        patch.object(count_only.parent, 'prepare_nofile'), \
                        patch.object(count_only.driver, 'load_plan'), \
                        patch.object(count_only.driver, 'resource_record'), \
                        patch.object(count_only.driver, 'build_profile'), \
                        patch.object(count_only.common, 'digest', side_effect=digest), \
                        patch.object(count_only.common, 'verify_loader'), \
                        patch.object(count_only.driver, 'verify_version'), \
                        patch.object(count_only.driver, 'run_owned', side_effect=owned), \
                        patch.object(count_only.common, 'capture_group', side_effect=capture):
                    if code is None:
                        count_only.prebuild(args)
                    else:
                        with self.assertRaises(RuntimeError):
                            count_only.prebuild(args)
                report = json.loads((root / 'report.json').read_text())
                self.assertEqual(events, ['owned-stop-and-cleanup'])
                self.assertTrue(report['instances'][0]['cleanupPassed'])
                self.assertTrue(report['diagnosticOnly'])
                self.assertFalse(report['candidateBuildAttempted'])
                self.assertFalse(report['correctnessPassed'])
                self.assertEqual(len(report['baselineDiagnostics']['outcomes']), 138)
                if code is None:
                    self.assertEqual(report['status'], 'baseline_diagnostics_completed')
                    self.assertIn('baselineTargetedPreflight', report)
                else:
                    self.assertNotEqual(report['status'], 'baseline_diagnostics_completed')

    def test_diagnostic_mode_refuses_candidate_validation(self):
        with patch.dict(os.environ, {'COUNT_BASELINE_DIAGNOSTICS_ONLY': '1'}), \
                patch.object(count_only, 'verify_header_dependencies') as headers, \
                patch.object(count_only, '_validate') as validate:
            with self.assertRaises(RuntimeError):
                count_only.validate(None)
        headers.assert_not_called()
        validate.assert_not_called()

    def test_capture_only_candidate_uses_its_actual_build_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            call, out, log = self.owned(Path(tmp), role='candidate')
            capture, calls = self.capture()
            with patch.object(count_only.common, 'capture_group', side_effect=capture):
                result = count_only.diagnostic_work(call, out, None, None)
            data = json.loads((out / 'count-matrix-diagnostics.json').read_text())
            self.assertEqual(data['roleIdentity']['role'], 'candidate')
            self.assertEqual(data['roleIdentity']['sourceCommit'], 'c' * 40)
            self.assertEqual(data['roleIdentity']['elfSha256'], 'e' * 64)
            self.assertTrue(data['candidateBuildAttempted'])
            self.assertTrue(result['candidateBuildAttempted'])
            self.assertFalse(data['correctnessPassed'])
            self.assertTrue(data['diagnosticOnly'])
            self.assertIsNone(data['strictCountAndPlanValidation'])

    def test_matrix_identity_rejects_relabelled_elf_or_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            call, out, log = self.owned(Path(tmp))
            root = out.parent.parent
            identity_path = root / 'runtime/baseline/identity.json'
            identity = json.loads(identity_path.read_text())
            identity['sourceCommit'] = 'c' * 40
            count_only.driver.dump(identity_path, identity)
            with self.assertRaises(RuntimeError):
                count_only.matrix_identity(out, 'baseline')


class MatrixPolicyTests(unittest.TestCase):
    CASES = ('rows_remaining', 'null_rows', 'empty_rows', 'plain_count', 'limit_2', 'limit_32',
             'limit_40', 'limit_0', 'offset_30', 'offset_33', 'no_match', 'count_column',
             'parent_filter', 'limit_20', 'projection_constant_limit_2')
    EXPECTED = {
        'beng_deleted_dense': (34, 1, 1, 32, 2, 32, 32, 0, 2, 0, 0, 0, 5, 20, 2),
        'beng_dense_no_delete': (37, 1, 1, 35, 2, 32, 35, 0, 5, 2, 0, 0, 5, 20, 2),
        'ik_deleted_dense': (34, 1, 1, 32, 2, 32, 32, 0, 2, 0, 0, 0, 5, 20, 2),
        'beng_deleted_mixed': (69, 1, 1, 32, 2, 32, 32, 0, 2, 0, 0, 0, 5, 20, 2),
    }

    def outcomes(self, role, inherited=True):
        def row(label, kind, table=None, case=None, expected=None, sql='SELECT 1;', raw=b'', error=None):
            return dict(label=label, kind=kind, table=table, caseName=case, sql=sql,
                        expectedCount=expected, observedCount=None if error else expected,
                        exitCode=1 if error else 0, serverErrorCode=error, childrenCleaned=True,
                        connectionLost=False, outputHex=raw.hex(), outputSha256=hashlib.sha256(raw).hexdigest())

        rows = [row('diag-schema', 'setup')]
        for fixture in count_only.DIAGNOSTIC_FIXTURES:
            matrix = count_only.diagnostic_fixture(fixture)
            table = matrix['table']
            rows.append(row('diag-' + fixture['name'] + '-setup', 'setup', table))
            expected = dict(zip(self.CASES, self.EXPECTED[fixture['name']]))
            queries = {name: query for name, query, value in matrix['cases']}
            for name in self.CASES:
                allowed_error = inherited and fixture['name'] in count_only.DENSE_TABLES and (
                    name == 'count_column' or role == 'baseline' and name in ('limit_2', 'limit_20', 'projection_constant_limit_2'))
                raw = b'ERROR 4016 (HY000): Internal error\n' if allowed_error else (str(expected[name]) + '\n').encode()
                rows.append(row('diag-' + fixture['name'] + '-count-' + name, 'count', table, name,
                                expected[name], queries[name], raw, 4016 if allowed_error else None))
            for name, query in matrix['plans'].items():
                scored = name in ('count_column', 'parent_filter', 'plain_select_limit', 'score_projection', 'score_order')
                scored = scored or role == 'baseline' and name != 'plain_count'
                raw = retrieval_plan(scored, '[MATCH(docs.body), DESC]' if scored else 'nil').encode() if 'MATCH(' in query else b'|0|TABLE SCAN|docs|\n'
                rows.append(row('diag-' + fixture['name'] + '-explain-' + name, 'explain', table, name,
                                sql='EXPLAIN ' + query, raw=raw))
        rows.append(row('diag-final-connection', 'connection', raw=b'1\n'))
        return rows

    def count_row(self, rows, table, case):
        return next(row for row in rows if row['kind'] == 'count' and row['table'].endswith('.' + table)
                    and row['caseName'] == case)

    def test_baseline_accepts_only_observed_dense_4016_and_preserves_raw_errors(self):
        rows = self.outcomes('baseline')
        verdict = count_only.matrix_verdict(rows, 'baseline')
        self.assertTrue(verdict['passed'])
        self.assertEqual(len(verdict['inheritedBaselineCountErrors']), 6)
        self.assertEqual(len(verdict['legacyScoredControlErrors']), 2)
        self.assertEqual(verdict['rawSqlCounts'], {'total': 138, 'succeeded': 130, 'failed': 8})
        self.assertFalse(verdict['allRawSqlSucceeded'])
        self.assertIsNone(verdict['newDenseCountExpectedResultsPassed'])
        self.assertEqual(sum(row['exitCode'] == 1 for row in rows), 8)

    def test_candidate_requires_all_new_dense_counts_and_keeps_only_scored_control_exception(self):
        rows = self.outcomes('candidate')
        verdict = count_only.matrix_verdict(rows, 'candidate')
        self.assertTrue(verdict['passed'])
        self.assertTrue(verdict['newDenseCountExpectedResultsPassed'])
        self.assertEqual(len(verdict['legacyScoredControlErrors']), 2)
        self.assertEqual(verdict['inheritedBaselineCountErrors'], [])
        self.assertEqual(verdict['rawSqlCounts']['failed'], 2)
        for case in ('limit_2', 'limit_20', 'projection_constant_limit_2'):
            rows = self.outcomes('candidate')
            item = self.count_row(rows, 'beng_deleted_dense', case)
            item.update(exitCode=1, serverErrorCode=4016, observedCount=None)
            self.assertFalse(count_only.matrix_verdict(rows, 'candidate')['passed'])

    def test_correct_success_is_accepted_for_previously_failing_baseline_or_control(self):
        for role in ('baseline', 'candidate'):
            verdict = count_only.matrix_verdict(self.outcomes(role, inherited=False), role)
            self.assertTrue(verdict['passed'])
            self.assertTrue(verdict['allRawSqlSucceeded'])
            self.assertEqual(verdict['legacyScoredControlErrors'], [])

    def test_wrong_success_and_tampered_expected_metadata_are_rejected(self):
        for role in ('baseline', 'candidate'):
            rows = self.outcomes(role)
            item = self.count_row(rows, 'beng_deleted_mixed', 'limit_2')
            item.update(observedCount=0, expectedCount=0)
            self.assertFalse(count_only.matrix_verdict(rows, role)['passed'])

    def test_other_tables_cases_error_codes_and_explain_errors_are_rejected(self):
        for table, case, code in (('beng_deleted_mixed', 'limit_2', 4016),
                                  ('beng_dense_no_delete', 'count_column', 4016),
                                  ('beng_deleted_dense', 'limit_32', 4016),
                                  ('beng_deleted_dense', 'limit_2', 4008)):
            rows = self.outcomes('baseline')
            self.count_row(rows, table, case).update(exitCode=1, serverErrorCode=code, observedCount=None)
            self.assertFalse(count_only.matrix_verdict(rows, 'baseline')['passed'])
        rows = self.outcomes('baseline')
        next(row for row in rows if row['kind'] == 'explain').update(exitCode=1, serverErrorCode=4016)
        self.assertFalse(count_only.matrix_verdict(rows, 'baseline')['passed'])

    def test_consumer_plan_checks_reject_relevance_or_sorting_regression(self):
        for text in (retrieval_plan(True, '[MATCH(docs.body), DESC]'),
                     retrieval_plan(False, '[MATCH(docs.body), DESC]')):
            rows = self.outcomes('candidate')
            item = next(row for row in rows if row['kind'] == 'explain' and row['caseName'] == 'limit_2')
            item['outputHex'] = text.encode().hex()
            self.assertFalse(count_only.matrix_verdict(rows, 'candidate')['passed'])
        rows = self.outcomes('candidate')
        item = next(row for row in rows if row['kind'] == 'explain' and row['caseName'] == 'count_column')
        item['outputHex'] = retrieval_plan(False).encode().hex()
        self.assertFalse(count_only.matrix_verdict(rows, 'candidate')['passed'])

    def test_missing_duplicate_or_client_failed_outcome_is_rejected(self):
        rows = self.outcomes('baseline')
        self.assertFalse(count_only.matrix_verdict(rows[:-1], 'baseline')['passed'])
        self.assertFalse(count_only.matrix_verdict(rows + [rows[0]], 'baseline')['passed'])
        rows = self.outcomes('baseline')
        rows[0]['childrenCleaned'] = False
        self.assertFalse(count_only.matrix_verdict(rows, 'baseline')['passed'])


if __name__ == '__main__':
    unittest.main()
