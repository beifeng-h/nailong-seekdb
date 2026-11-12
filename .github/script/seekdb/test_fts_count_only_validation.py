"""Offline regressions for the COUNT-consumer validation entry point."""
import hashlib
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import fts_count_only_validation as count_only


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
    COUNT_OUTPUT = ('rows_remaining\n34\nnull_rows\n1\nempty_rows\n1\nplain_count\n32\n'
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
                with patch.object(count_only.driver, 'sql', side_effect=self.fixture_sql(role)):
                    results[role] = count_only.equality_work(None, out)
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
                          'equality': {'baseline': {'canonical': '00' if same else '01'}}}
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


if __name__ == '__main__':
    unittest.main()
