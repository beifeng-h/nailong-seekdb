"""Synthetic paired-contract tests. Never builds or starts SQL; one Linux test owns synthetic subprocesses."""
import argparse
import ast
from contextlib import ExitStack, redirect_stdout
from collections import namedtuple
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import time
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import fts_paired_validation as p

REPO = Path(__file__).resolve().parents[3]
DRIVER = 'a' * 40
TREE = 'b' * 40
BASELINE = json.loads((REPO / (p.BENCH + '_baseline.json')).read_text(encoding='utf-8'))
SCORER = p.load_module('test_original_scorer', REPO / (p.BENCH + '_score.py'))

# Exact UTF-8 outputs recovered from the completed Actions job below. Both A4
# and B4 had these identical outputs before the new match-only optimization.
# Each hash is the corresponding original PAIRED_EXPLAIN.outputSha256, not a
# hash invented from this fixture. JSON preserves original trailing spaces/LF.
ACTUAL_ACTIONS_EXPLAINS = json.loads(r'''{
  "sourceJobUrl": "https://github.com/beifeng-h/nailong-seekdb/actions/runs/42470225355/job/128699028769",
  "fixtures": [
    {"query":"query_cn","sha256":"aa89f88510c6213065504f9a9b29af4c0318b13b7b03945f44d54913fb20fa09","output":"Query Plan\n=================================================================\n|ID|OPERATOR             |NAME            |EST.ROWS|EST.TIME(us)|\n-----------------------------------------------------------------\n|0 |SCALAR GROUP BY      |                |1       |587536      |\n|1 |└─TEXT RETRIEVAL SCAN|docs(fti_all_ik)|12669   |587307      |\n=================================================================\nOutputs & filters:\n-------------------------------------\n  0 - output([T_FUN_COUNT(*)]), filter(nil), rowset=256\n      group(nil), agg_func([T_FUN_COUNT(*)])\n  1 - output(nil), filter(nil), rowset=256\n      access(nil), partitions(p0)\n      is_index_back=false, is_global_index=false, \n      calc_relevance=true, match_expr(MATCH(docs.title, docs.content) AGAINST('全文索引 分词器')), \n      pushdown_match_filter(MATCH(docs.title, docs.content) AGAINST('全文索引 分词器'))\n"},
    {"query":"query_beng","sha256":"af942995681f75327feca190967715496461e9a3dbdb566c481f038c1324d933","output":"Query Plan\n==================================================================\n|ID|OPERATOR             |NAME             |EST.ROWS|EST.TIME(us)|\n------------------------------------------------------------------\n|0 |SCALAR GROUP BY      |                 |1       |834767      |\n|1 |└─TEXT RETRIEVAL SCAN|docs(fti_en_beng)|18000   |834441      |\n==================================================================\nOutputs & filters:\n-------------------------------------\n  0 - output([T_FUN_COUNT(*)]), filter(nil), rowset=256\n      group(nil), agg_func([T_FUN_COUNT(*)])\n  1 - output(nil), filter(nil), rowset=256\n      access(nil), partitions(p0)\n      is_index_back=false, is_global_index=false, \n      calc_relevance=true, match_expr(MATCH(docs.title_en, docs.content_en) AGAINST('database performance audit')), \n      pushdown_match_filter(MATCH(docs.title_en, docs.content_en) AGAINST('database performance audit'))\n"},
    {"query":"query_mixed","sha256":"a48fb77c249e17ddb7ccf5e2f7162c471fca643c5e290b9abc865768a9646427","output":"Query Plan\n=================================================================\n|ID|OPERATOR             |NAME            |EST.ROWS|EST.TIME(us)|\n-----------------------------------------------------------------\n|0 |SCALAR GROUP BY      |                |1       |587359      |\n|1 |└─TEXT RETRIEVAL SCAN|docs(fti_all_ik)|12665   |587130      |\n=================================================================\nOutputs & filters:\n-------------------------------------\n  0 - output([T_FUN_COUNT(*)]), filter(nil), rowset=256\n      group(nil), agg_func([T_FUN_COUNT(*)])\n  1 - output(nil), filter(nil), rowset=256\n      access(nil), partitions(p0)\n      is_index_back=false, is_global_index=false, \n      calc_relevance=true, match_expr(MATCH(docs.title, docs.content) AGAINST('OceanBase 稳定 database')), \n      pushdown_match_filter(MATCH(docs.title, docs.content) AGAINST('OceanBase 稳定 database'))\n"},
    {"query":"query_limit","sha256":"df556ca4105cd0eceebd49236aff90161852ca1d45d8cf9a81122a4c5bdafb90","output":"Query Plan\n=======================================================================\n|ID|OPERATOR               |NAME                |EST.ROWS|EST.TIME(us)|\n-----------------------------------------------------------------------\n|0 |SCALAR GROUP BY        |                    |1       |406515      |\n|1 |└─SUBPLAN SCAN         |t                   |14000   |406262      |\n|2 |  └─TEXT RETRIEVAL SCAN|docs(fti_content_ik)|14000   |406225      |\n=======================================================================\nOutputs & filters:\n-------------------------------------\n  0 - output([T_FUN_COUNT(*)]), filter(nil), rowset=256\n      group(nil), agg_func([T_FUN_COUNT(*)])\n  1 - output(nil), filter(nil), rowset=256\n      access(nil)\n  2 - output([docs.id]), filter(nil), rowset=256\n      access([docs.id]), partitions(p0)\n      is_index_back=true, is_global_index=false, \n      calc_relevance=true, match_expr(MATCH(docs.content) AGAINST('倒排索引 tokenizer')), \n      pushdown_match_filter(MATCH(docs.content) AGAINST('倒排索引 tokenizer')), \n      sort_keys([MATCH(docs.content) AGAINST('倒排索引 tokenizer'), DESC]), limit(20), offset(nil), with_ties(false)\n"}
  ]
}''')


class Contracts(unittest.TestCase):
    def assert_workflow_expression_scope(self, text):
        before_steps, steps = text.split('    steps:', 1)
        self.assertNotRegex(before_steps, r'\$\{\{\s*runner(?:\.|\[)')
        self.assertNotIn('      PAIRED_ROOT:', before_steps)
        first_step = steps.split('      - name:')[1]
        self.assertIn('printf \'PAIRED_ROOT=%s/seekdb-paired\\n\' "$RUNNER_TEMP" >> "$GITHUB_ENV"', first_step)
        self.assertIn('path: ${{ runner.temp }}/seekdb-paired-artifact/', steps)

    def test_workflow_runner_context_is_step_scoped_and_root_exported_early(self):
        text = (REPO / '.github/workflows/fts-contest-validation.yml').read_text(encoding='utf-8')
        self.assert_workflow_expression_scope(text)

    def test_workflow_rejects_runner_temp_in_job_env_regression(self):
        text = (REPO / '.github/workflows/fts-contest-validation.yml').read_text(encoding='utf-8')
        broken = text.replace('    steps:', '      PAIRED_ROOT: ${{ runner.temp }}/seekdb-paired\n    steps:', 1)
        with self.assertRaises(AssertionError):
            self.assert_workflow_expression_scope(broken)

    def setUp(self):
        signals = patch.object(p.signal, 'signal')
        signals.start()
        self.addCleanup(signals.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'owned'
        self.source = Path(self.tmp.name) / 'source'
        self.source.mkdir()
        self.root.mkdir()
        self.args = argparse.Namespace(root=self.root, source=self.source, driver_sha=DRIVER, role='candidate')
        self.quiet = redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def identity(self, role='candidate'):
        binary = self.root / 'runtime' / role / 'seekdb'
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b'\x7fELF' + role.encode() + (p.BASELINE if role == 'baseline' else DRIVER).encode())
        return dict(role=role, sourceCommit=p.BASELINE if role == 'baseline' else DRIVER,
                    sourceTree=TREE, kernelOrigin=p.BASELINE if role == 'baseline' else p.KERNEL,
                    driverCommit=DRIVER, binary=str(binary), bytes=binary.stat().st_size,
                    sha256=p.digest(binary), buildId='abcdef', officialInputHashes={'official': 'fixed'})

    def metrics(self, factor=1):
        metrics = dict(BASELINE['baseline_metrics'], **p.CONFIG, git_dirty=0)
        for names in BASELINE['score_categories'].values():
            for name in names:
                metrics[name] *= factor
        return metrics

    def report_file(self, fields=None):
        data = dict(label='candidate-' + DRIVER[:12], git_head=DRIVER, **self.metrics())
        if fields:
            data.update(fields)
        path = self.root / 'metrics.txt'
        path.write_text(''.join('%s: %s\n' % item for item in data.items()), encoding='utf-8')
        return path

    def saved_plan(self):
        plan = p.plan_for(self.source, self.root, DRIVER)
        plan['driverFiles'] = {}
        for name in p.DRIVER_FILES:
            path = self.root / 'driver' / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('synthetic ' + name)
            plan['driverFiles'][name] = p.digest(path)
        p.write_json(self.root / 'plan.json', plan)
        return plan

    def presentation_report(self, factors=(.9, .9, .9, .9)):
        cases = [dict(role=role, name=name, status='passed', cleanupPassed=True)
                 for role in ('baseline', 'candidate')
                 for name in p.selected_cases(role)]
        rounds = [dict(role=role, name=name, status='passed', cleanupPassed=True)
                  for name, role in p.SCHEDULE]
        return dict(status='valid_calibration_completed', driverCommit=DRIVER,
            kernelOrigin=p.KERNEL, baselineCommit=p.BASELINE, allOfficialInputsUnchanged=True,
            builds=dict(baseline=dict(sourceCommit=p.BASELINE), candidate=dict(sourceCommit=DRIVER)),
            caseVerdicts=cases, roundVerdicts=rounds,
            sameHostPerformance=p.performance(self.pairs(factors), BASELINE, SCORER),
            planAssertions={name: dict(role=role, queries=self.plan_rows(role))
                            for name, role in (('A4', 'baseline'), ('B4', 'candidate'))},
            instances=[dict(name=name, work=dict(informativeFrozenBaselineScore=dict(score=100)))
                       for name, _ in p.SCHEDULE])

    @staticmethod
    def plan_text(scored=False):
        return ('Query Plan\n===================================================\n'
                '|ID|OPERATOR             |NAME          |EST.ROWS|\n'
                '|0 |SCALAR GROUP BY      |              |1       |\n'
                '|1 |  TEXT RETRIEVAL SCAN|docs(fti_all) |8001    |\n'
                '===================================================\n'
                'Outputs & filters:\n-------------------------------------\n'
                '  0 - output([T_FUN_COUNT(*)]), filter(nil), rowset=16\n'
                '  1 - output([docs.id]), filter(nil), rowset=16\n'
                '      access([docs.id]), partitions(p0)\n'
                '      calc_relevance=%s, match_expr(MATCH(docs.title, docs.content) AGAINST(\'text\')),\n'
                '      pushdown_match_filter(MATCH(docs.title, docs.content) AGAINST(\'text\'))\n') % ('true' if scored else 'false')

    def plan_rows(self, role):
        rows = {}
        for name in p.QUERY_NAMES + p.PLAN_CONTROLS:
            scored = p.expected_plan_relevance(role, name)
            rows[name] = dict(role=role, kind='official' if name in p.QUERY_NAMES else 'scored_control',
                passed=True, exitCode=0, childrenCleaned=True, expectedCalcRelevance=scored,
                sqlSha256='c' * 64, outputSha256='d' * 64, **p.plan_relevance(self.plan_text(scored)))
        return rows

    def test_presentation_complete_all_pairs_categories_metrics_and_identities(self):
        report = self.presentation_report()
        original = copy.deepcopy(report)
        lines = p.presentation_lines(report)
        text = '\n'.join(lines)
        self.assertIn('Paired development classification: improvement_established', text)
        self.assertIn('Unbounded mean: +10%', text)
        self.assertIn('predeclared margin: +2%', text)
        self.assertIn('B actual build source: ' + DRIVER, text)
        self.assertIn('B kernel origin: ' + p.KERNEL, text)
        self.assertIn('Correctness baseline: 5/5', text)
        self.assertIn('Correctness candidate: 7/7', text)
        self.assertIn('Owned DB cleanup: 20/20', text)
        self.assertIn('(20 expected)', text)
        self.assertIn('Untimed actual-plan assertions: passed for A4/B4', text)
        self.assertIn('three candidate COUNT queries unscored; protected implicit-ranked LIMIT', text)
        self.assertEqual(sum(' category ' in line for line in lines), 12)
        self.assertEqual(sum(' metric ' in line for line in lines), 36)
        self.assertEqual(sum(line.startswith('Frozen reference ') for line in lines), 8)
        for a, b in p.PAIRS:
            self.assertTrue(any(a + '/' + b + ': unbounded improvement=' in line for line in lines))
            for names in BASELINE['score_categories'].values():
                for metric in names:
                    self.assertTrue(any('Pair %s/%s metric %s: A=' % (a, b, metric) in line
                                        and ' B=' in line and ' improvement=' in line for line in lines))
        self.assertTrue(all(len('FTS_RESULT ' + line) < 512 for line in lines))
        self.assertIn('No formal contest score', text)
        self.assertIn('different-host reference only, separate from paired development', text)
        self.assertEqual(report, original)

    def test_presentation_regression_and_inconclusive_not_clamped(self):
        for factor, classification, mean in ((1.1, 'regression', '-10%'), (1, 'inconclusive', '+0%')):
            with self.subTest(classification=classification):
                text = '\n'.join(p.presentation_lines(self.presentation_report((factor,) * 4)))
                self.assertIn('Paired development classification: ' + classification, text)
                self.assertIn('Unbounded mean: ' + mean, text)
                self.assertNotIn('classification: improvement_established', text)

    def test_presentation_counts_derive_from_cases_and_schedule(self):
        with patch.object(p, 'REGRESSIONS', p.REGRESSIONS + ('fts_index.future_contract',)):
            text = '\n'.join(p.presentation_lines(self.presentation_report()))
        self.assertIn('Correctness candidate: 8/8', text)
        self.assertIn('Owned DB cleanup: 21/21', text)
        self.assertIn('(21 expected)', text)

    def test_plan_assertion_report_rejects_missing_wrong_role_or_inconsistent_proof(self):
        original = self.presentation_report()
        self.assertTrue(p.accepted_plan_assertions(original['planAssertions']))
        variants = [None, [], {}, {'A4': original['planAssertions']['A4']}]
        for field, value in (('role', 'baseline'), ('kind', 'scored_control'), ('passed', False),
                ('exitCode', False), ('exitCode', 1), ('childrenCleaned', 1),
                ('expectedCalcRelevance', True), ('actualCalcRelevance', True),
                ('actualCalcRelevance', 0), ('scanId', -1), ('scanId', True),
                ('sqlSha256', 'bad'), ('outputSha256', None), ('calcRelevanceEvidence', []),
                ('calcRelevanceEvidence', [None]),
                ('calcRelevanceEvidence', ['calc_relevance=false']),
                ('calcRelevanceEvidence', [self.plan_text(True)])):
            value_report = copy.deepcopy(original['planAssertions'])
            value_report['B4']['queries']['query_cn'][field] = value
            variants.append(value_report)
        for change in ('record_role', 'missing_control', 'extra_round', 'control_false', 'limit_false'):
            value = copy.deepcopy(original['planAssertions'])
            if change == 'record_role':
                value['B4']['role'] = 'baseline'
            elif change == 'missing_control':
                value['B4']['queries'].pop(p.PLAN_CONTROLS[0])
            elif change == 'extra_round':
                value['B3'] = value['B4']
            elif change == 'control_false':
                value['B4']['queries'][p.PLAN_CONTROLS[0]]['actualCalcRelevance'] = False
            else:
                value['B4']['queries']['query_limit']['actualCalcRelevance'] = False
            variants.append(value)
        for index, value in enumerate(variants):
            with self.subTest(index=index):
                self.assertFalse(p.accepted_plan_assertions(value))
                report = dict(original, planAssertions=value)
                text = '\n'.join(p.presentation_lines(report))
                self.assertIn('Paired development classification: unavailable', text)
                self.assertIn('No improvement claim.', text)
        report = copy.deepcopy(original)
        row = report['planAssertions']['B4']['queries']['query_cn']
        row.update(passed=False, failureReason='Actual calc_relevance differs')
        self.assertIn('Untimed plan failure B4/candidate/query_cn: Actual calc_relevance differs',
            '\n'.join(p.presentation_lines(report)))

    def test_presentation_retains_each_pair_and_outlier(self):
        lines = p.presentation_lines(self.presentation_report((.9, 1.2, .95, 1)))
        self.assertTrue(any('Pair 2 A2/B2: unbounded improvement=-20%' in line for line in lines))
        self.assertEqual(sum('Pair A2/B2 metric ' in line and 'improvement=-20%' in line for line in lines), 9)
        self.assertEqual(sum(' metric ' in line for line in lines), 36)

    def test_presentation_invalid_or_incomplete_never_claims_improvement(self):
        reports = [{}, dict(status='phase_completed')]
        for mutation in ('status', 'round', 'pair', 'metric', 'margin', 'classification', 'source'):
            report = self.presentation_report()
            if mutation == 'status':
                report['status'] = 'invalid_calibration'
                report['failure'] = dict(phase='validate', reason='Owned DB cleanup incomplete: B2')
            elif mutation == 'round':
                report['roundVerdicts'][-1]['cleanupPassed'] = False
            elif mutation == 'pair':
                report['sameHostPerformance']['pairs'].pop()
            elif mutation == 'metric':
                report['sameHostPerformance']['pairs'][0]['categories']['query']['metrics'].pop()
            elif mutation == 'margin':
                report['sameHostPerformance']['margin'] = .5
            elif mutation == 'classification':
                report['sameHostPerformance']['classification'] = 'regression'
            else:
                report['builds']['candidate']['sourceCommit'] = p.KERNEL
            reports.append(report)
        for report in reports:
            with self.subTest(report=report.get('status')):
                text = '\n'.join(p.presentation_lines(report))
                self.assertIn('Paired development classification: unavailable', text)
                self.assertIn('No improvement claim.', text)
                self.assertNotIn('classification: improvement_established', text)
        invalid = reports[2]
        text = '\n'.join(p.presentation_lines(invalid))
        self.assertIn('Failure phase: validate', text)
        self.assertIn('Failure reason: Owned DB cleanup incomplete: B2', text)

    def test_presentation_nonfinite_numbers_are_unavailable_not_nan(self):
        for value in (float('nan'), float('inf'), float('-inf'), 10 ** 1000, True, 'not a number'):
            with self.subTest(value_type=type(value).__name__):
                report = self.presentation_report()
                perf = report['sameHostPerformance']
                perf['meanUnboundedImprovement'] = value
                perf['pairs'][0]['categories']['build']['metrics'][0]['current'] = value
                text = '\n'.join(p.presentation_lines(report))
                self.assertIn('Paired development classification: unavailable', text)
                self.assertNotIn('NaN', text)
                self.assertNotIn('nan', text)
                self.assertNotIn('Infinity', text)
                self.assertNotIn('=inf', text)
                self.assertTrue(all(len('FTS_RESULT ' + line) < 512 for line in text.splitlines()))

    def test_presentation_long_failure_is_bounded_and_not_truncated(self):
        reason = 'Owned cleanup failed: ' + 'precise-detail ' * 140 + '\nlast detail'
        report = dict(status='invalid_calibration', failure=dict(phase='validate', reason=reason))
        lines = p.presentation_lines(report)
        chunks = [line.split(': ', 1)[1] for line in lines if line.startswith('Continuation ')]
        self.assertEqual(''.join(chunks), json.dumps('Failure reason: ' + reason, ensure_ascii=True)[1:-1])
        self.assertTrue(all(len('FTS_RESULT ' + line) < 512 for line in lines))

    def test_presentation_summary_append_and_stdout_are_readable(self):
        summary = self.root / 'summary.txt'
        summary.write_text('Existing summary\n', encoding='utf-8')
        output = io.StringIO()
        with redirect_stdout(output):
            p.present_report(self.presentation_report(), summary)
        text = summary.read_text(encoding='utf-8')
        self.assertTrue(text.startswith('Existing summary\n'))
        self.assertIn('## FTS same-host result', text)
        self.assertIn('Paired development classification: improvement_established', text)
        self.assertIn('Pair A4/B4 metric query_limit_avg_ms:', text)
        self.assertTrue(all(line.startswith('FTS_RESULT ') and len(line) < 512 for line in output.getvalue().splitlines()))

    def test_present_phase_is_read_only_and_tolerates_missing_invalid_report(self):
        summary = self.root / 'summary.txt'
        path = self.root / 'report.json'
        arguments = ['--phase', 'present', '--source', str(self.source), '--root', str(self.root),
                     '--driver-sha', DRIVER, '--summary', str(summary)]
        for contents in (json.dumps(self.presentation_report()), '{invalid json', None):
            with self.subTest(contents=contents is None):
                if contents is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(contents, encoding='utf-8')
                with patch.object(p, 'git', side_effect=AssertionError('Presentation must not run Git')), \
                        patch.object(p, 'load_plan', side_effect=AssertionError('Presentation must not load/change source plan')):
                    self.assertEqual(p.main(arguments), 0)
                if contents is None:
                    self.assertFalse(path.exists())
                else:
                    self.assertEqual(path.read_text(encoding='utf-8'), contents)
        self.assertIn('Failure phase: presentation', summary.read_text(encoding='utf-8'))

    def test_workflow_publishes_readable_summary_and_retains_json(self):
        text = (REPO / '.github/workflows/fts-contest-validation.yml').read_text(encoding='utf-8')
        self.assertIn('--phase present', text)
        self.assertIn('--summary "$GITHUB_STEP_SUMMARY"', text)
        self.assertIn('PAIRED_VISIBLE_RESULT', text)
        self.assertLess(text.index('--phase present'), text.index('PAIRED_VISIBLE_RESULT'))

    def test_visible_json_and_small_report_retain_strict_presentation_evidence(self):
        text = (REPO / '.github/workflows/fts-contest-validation.yml').read_text(encoding='utf-8')
        visible = text.split('print("PAIRED_VISIBLE_RESULT",', 1)[1].split('if k in r}', 1)[0]
        for key in ('allOfficialInputsUnchanged', 'planAssertions', 'formalContestRun'):
            with self.subTest(key=key):
                self.assertIn('"' + key + '"', visible)
        report = dict(self.presentation_report(), formalContestRun=False)
        compact = p.small_report(report)
        for key in ('allOfficialInputsUnchanged', 'planAssertions', 'formalContestRun'):
            self.assertIn(key, compact)
            self.assertEqual(compact[key], report[key])
        self.assertTrue(p.accepted_plan_assertions(compact['planAssertions']))

    def test_source_exact_clean_and_tree(self):
        with patch.object(p, 'git', side_effect=[DRIVER.encode(), b'', TREE.encode()]) as git:
            self.assertEqual(p.source_identity(self.source, DRIVER), dict(commit=DRIVER, tree=TREE))
            self.assertIn('--untracked-files=all', git.call_args_list[1].args)

    def test_source_rejects_wrong_commit_dirty_staged_untracked(self):
        for head, status in [(p.BASELINE, b''), (DRIVER, b' M src/a'), (DRIVER, b'M  src/a'), (DRIVER, b'?? new')]:
            with self.subTest(head=head, status=status), patch.object(p, 'git', side_effect=[head.encode(), status]):
                with self.assertRaises(RuntimeError):
                    p.source_identity(self.source, DRIVER)

    def test_driver_allows_only_exact_ci_paths(self):
        with patch.object(p, 'git', side_effect=['\n'.join(p.ALLOWED_DRIVER_PATHS).encode(),
                (REPO / p.DIAGNOSTIC_TEST).read_bytes(), b'', b'']):
            self.assertEqual(p.verify_driver(self.source, DRIVER)['kernelOrigin'], p.KERNEL)
        for forbidden in ['src/sql/executor.cpp', p.PREFIX + 'fts_contest_validation.py', p.PREFIX + 'another_test.py', 'tools/benchmark/fts_large_bench.sh']:
            with self.subTest(path=forbidden), patch.object(p, 'git', return_value=forbidden.encode()):
                with self.assertRaises(RuntimeError):
                    p.verify_driver(self.source, DRIVER)
        with patch.object(p, 'git', side_effect=[b'', b'build.sh']):
            with self.assertRaises(RuntimeError):
                p.verify_driver(self.source, DRIVER)
        with self.assertRaises(RuntimeError):
            p.verify_driver(self.source, '8db524b')

    def test_source_switch_verifies_before_and_after(self):
        with patch.object(p, 'source_identity', side_effect=[{'commit': DRIVER}, {'commit': p.BASELINE}]) as identity, patch.object(p, 'git') as git:
            self.assertEqual(p.switch_source(self.source, DRIVER, p.BASELINE)['commit'], p.BASELINE)
            self.assertEqual(identity.call_args_list[0].args, (self.source, DRIVER))
            self.assertEqual(identity.call_args_list[1].args, (self.source, p.BASELINE))
            git.assert_called_once_with(self.source, 'checkout', '--detach', p.BASELINE)
        with patch.object(p, 'source_identity', side_effect=RuntimeError('dirty')), patch.object(p, 'git') as git:
            with self.assertRaises(RuntimeError):
                p.switch_source(self.source, DRIVER, p.BASELINE)
            git.assert_not_called()
        with patch.object(p, 'source_identity', side_effect=[{}, RuntimeError('wrong target')]), patch.object(p, 'git'):
            with self.assertRaises(RuntimeError):
                p.switch_source(self.source, DRIVER, p.BASELINE)

    def test_identity_binds_role_commit_kernel_tree_bytes_hash_build_id(self):
        for role in ('baseline', 'candidate'):
            identity = self.identity(role)
            commit = identity['sourceCommit']
            self.assertEqual(p.validate_identity(identity, role, commit, self.root), Path(identity['binary']))
            for key, value in [('role', 'other'), ('sourceCommit', 'c' * 40), ('kernelOrigin', 'd' * 40), ('sourceTree', 'bad'), ('bytes', 0), ('sha256', '0' * 64), ('buildId', '')]:
                with self.subTest(role=role, key=key), self.assertRaises(RuntimeError):
                    p.validate_identity(dict(identity, **{key: value}), role, commit, self.root)

    def test_baseline_candidate_confusion_and_wrong_path(self):
        baseline = self.identity('baseline')
        candidate = self.identity('candidate')
        with self.assertRaises(RuntimeError):
            p.validate_identity(baseline, 'candidate', DRIVER, self.root)
        for binary in [baseline['binary'], 'runtime/candidate/seekdb', str(self.root / 'missing')]:
            with self.subTest(binary=binary), self.assertRaises(RuntimeError):
                p.validate_identity(dict(candidate, binary=binary), 'candidate', DRIVER, self.root)
        Path(candidate['binary']).write_bytes(b'not ELF')
        with self.assertRaises(RuntimeError):
            p.validate_identity(candidate, 'candidate', DRIVER, self.root)

    def test_identity_detects_post_snapshot_mutation(self):
        identity = self.identity()
        with Path(identity['binary']).open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaises(RuntimeError):
            p.validate_identity(identity, 'candidate', DRIVER, self.root)

    def test_identity_rejects_hardlink(self):
        identity = self.identity()
        os.link(identity['binary'], self.root / 'alias')
        with self.assertRaises(RuntimeError):
            p.validate_identity(identity, 'candidate', DRIVER, self.root)

    def test_loader_success_missing_dependency_nonzero_and_unclean(self):
        log = self.root / 'ldd.log'
        for text, code, clean, valid in [('libc => found', 0, True, True), ('libbad => not found', 0, True, False), ('error', 1, True, False), ('found', 0, False, False)]:
            log.write_text(text)
            with self.subTest(text=text, code=code, clean=clean), patch.object(p, 'capture_group', return_value=dict(exitCode=code, childrenCleaned=clean)):
                if valid:
                    self.assertEqual(p.verify_loader('seekdb', log, self.source, {})['exitCode'], 0)
                else:
                    with self.assertRaises(RuntimeError):
                        p.verify_loader('seekdb', log, self.source, {})
        with patch.object(p, 'capture_group', side_effect=FileNotFoundError('ldd missing')):
            with self.assertRaises(FileNotFoundError):
                p.verify_loader('seekdb', log, self.source, {})

    def test_capture_group_preserves_nonzero_and_error_log(self):
        process = Mock(pid=123, returncode=1)
        with patch.object(p.subprocess, 'Popen', return_value=process) as launch, patch.object(p.os, 'killpg', side_effect=ProcessLookupError, create=True):
            row = p.capture_group(['fake'], self.root / 'error.log', self.source, 3)
        self.assertEqual(row['exitCode'], 1)
        self.assertTrue(row['childrenCleaned'])
        self.assertTrue(launch.call_args.kwargs['start_new_session'])
        self.assertEqual(json.loads((self.root / 'error.log.json').read_text())['exitCode'], 1)

    def test_capture_group_timeout_not_pass_and_kills_owned_group(self):
        process = Mock(pid=123, returncode=-9)
        process.communicate.side_effect = [subprocess.TimeoutExpired('fake', 1), None]
        with patch.object(p.subprocess, 'Popen', return_value=process), patch.object(p.os, 'killpg', create=True) as kill, patch.object(p.signal, 'SIGKILL', 9, create=True):
            row = p.capture_group(['fake'], self.root / 'timeout.log', self.source, 1)
        self.assertEqual(row['exitCode'], 124)
        self.assertFalse(row['childrenCleaned'])
        self.assertTrue(row['timedOut'])
        self.assertEqual([call.args[1] for call in kill.call_args_list], [0, p.signal.SIGTERM, 9])

    def test_strict_report_positive(self):
        result = p.strict_report(self.report_file(), 'candidate', DRIVER, BASELINE)
        self.assertEqual(result['query_cn_hits'], 8001)

    def test_strict_report_rejects_missing_and_duplicate_metrics(self):
        path = self.report_file()
        original = path.read_text()
        path.write_text('\n'.join(line for line in original.splitlines() if not line.startswith('query_cn_avg_ms:')))
        with self.assertRaises(RuntimeError):
            p.strict_report(path, 'candidate', DRIVER, BASELINE)
        path.write_text(original + 'query_cn_hits: 8001\n')
        with self.assertRaises(RuntimeError):
            p.strict_report(path, 'candidate', DRIVER, BASELINE)

    def test_strict_report_rejects_invalid_metrics_hits_config_role_and_commit(self):
        bad = [('query_cn_avg_ms', value) for value in ['NaN', 'inf', '-inf', -1, 0, 'N/A']]
        bad += [('raw_load_sec', 0), ('raw_load_rows_per_sec', 0), ('select1_avg_ms', 0), ('query_cn_hits', 8000), ('query_limit_hits', 20.5), ('rows', 1), ('skip_load', 1), ('git_dirty', 1), ('label', 'baseline-' + DRIVER[:12]), ('git_head', p.BASELINE), ('git_head', DRIVER[:6])]
        for key, value in bad:
            with self.subTest(key=key, value=value), self.assertRaises(RuntimeError):
                p.strict_report(self.report_file({key: value}), 'candidate', DRIVER, BASELINE)

    def test_error_log_cannot_be_report(self):
        path = self.root / 'error.txt'
        path.write_text('ERROR: connection failed\nquery_cn_hits: 8001\nscore: 100\n')
        with self.assertRaises(RuntimeError):
            p.strict_report(path, 'candidate', DRIVER, BASELINE)

    def test_exact_four_original_query_literals(self):
        benchmark = REPO / (p.BENCH + '.sh')
        before = p.digest(benchmark)
        queries = p.queries_from_benchmark(benchmark)
        expected = {
            'query_cn': "SELECT COUNT(*) FROM fts_large_bench.docs WHERE MATCH(title, content) AGAINST('\u5168\u6587\u7d22\u5f15 分词器');",
            'query_beng': "SELECT COUNT(*) FROM fts_large_bench.docs WHERE MATCH(title_en, content_en) AGAINST('database performance audit');",
            'query_mixed': "SELECT COUNT(*) FROM fts_large_bench.docs WHERE MATCH(title, content) AGAINST('OceanBase 稳定 database');",
            'query_limit': "SELECT COUNT(*) FROM (SELECT id FROM fts_large_bench.docs WHERE MATCH(content) AGAINST('\u5012\u6392\u7d22\u5f15 tokenizer') LIMIT 20) t;",
        }
        self.assertEqual(queries, expected)
        self.assertEqual(p.digest(benchmark), before)

    def test_query_extraction_rejects_missing_duplicate_nonliteral(self):
        text = (REPO / (p.BENCH + '.sh')).read_text(encoding='utf-8')
        for altered in [text.replace('run_query_bench "query_cn"', 'run_query_bench "wrong"'), text + '\n' + text, text.replace("AGAINST('database performance audit')", "AGAINST('$variable')")]:
            path = self.root / 'bench.sh'
            path.write_text(altered, encoding='utf-8')
            with self.assertRaises(RuntimeError):
                p.queries_from_benchmark(path)

    def pairs(self, factors):
        return [dict(baseline=a, candidate=b, baselineMetrics=self.metrics(), candidateMetrics=self.metrics(factor)) for (a, b), factor in zip(p.PAIRS, factors)]

    def test_performance_positive_negative_and_inconclusive(self):
        for factor, expected in [(0.9, 'improvement_established'), (1.1, 'regression'), (1, 'inconclusive'), (0.981, 'inconclusive'), (1.019, 'inconclusive'), (3, 'regression')]:
            with self.subTest(factor=factor):
                result = p.performance(self.pairs([factor] * 4), BASELINE, SCORER)
                self.assertEqual(result['classification'], expected)
                self.assertAlmostEqual(result['meanUnboundedImprovement'], 1 - factor)
                self.assertFalse(result['formalContestScore'])
                if factor == 3:
                    self.assertEqual(result['pairs'][0]['diagnosticClampedScore'], 0)
                    self.assertEqual(result['meanUnboundedImprovement'], -2)

    def test_performance_variability_and_invalid_pair_count(self):
        self.assertEqual(p.performance(self.pairs([0.6, 1.2, 0.7, 1.1]), BASELINE, SCORER)['classification'], 'inconclusive')
        with self.assertRaises(RuntimeError):
            p.performance(self.pairs([1, 1, 1]), BASELINE, SCORER)
        fake = Mock()
        fake.calculate_score.return_value = dict(mean_improvement=float('nan'), categories={}, score=0)
        with self.assertRaises(RuntimeError):
            p.performance(self.pairs([1] * 4), BASELINE, fake)

    def test_require_instance_never_accepts_cleanup_failure_or_error(self):
        p.require_instance(dict(status='passed', cleanupPassed=True))
        for row in [{}, dict(status='failed', cleanupPassed=True), dict(status='infrastructure_error', cleanupPassed=True), dict(status='passed', cleanupPassed=False), dict(status='passed', cleanupPassed=1)]:
            with self.subTest(row=row), self.assertRaises(RuntimeError):
                p.require_instance(row)

    def test_plan_has_complete_eight_run_abba_baab_and_adjacent_pairs(self):
        plan = p.plan_for(self.source, self.root, DRIVER)
        self.assertEqual([(row['name'], row['role']) for row in plan['schedule']], [('A1', 'baseline'), ('B1', 'candidate'), ('B2', 'candidate'), ('A2', 'baseline'), ('B3', 'candidate'), ('A3', 'baseline'), ('A4', 'baseline'), ('B4', 'candidate')])
        self.assertEqual(plan['pairs'], [dict(baseline='A%d' % n, candidate='B%d' % n) for n in range(1, 5)])
        self.assertEqual(plan['performanceMargin'], .02)
        self.assertEqual(plan['caseRetries'], 0)

    def test_saved_plan_checks_identity_and_driver_hashes(self):
        plan = self.saved_plan()
        self.assertEqual(p.load_plan(self.args), plan)
        for key, bad in [('driverCommit', p.BASELINE), ('workspace', 'wrong'), ('root', 'wrong'), ('kernelOrigin', DRIVER), ('baselineCommit', DRIVER)]:
            broken = dict(plan, **{key: bad})
            p.write_json(self.root / 'plan.json', broken)
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                p.load_plan(self.args)
        p.write_json(self.root / 'plan.json', plan)
        (self.root / 'driver' / p.DRIVER_FILES[0]).write_text('tampered')
        with self.assertRaises(RuntimeError):
            p.load_plan(self.args)

    def test_saved_plan_rejects_missing_driver_file_entry(self):
        plan = self.saved_plan()
        plan['driverFiles'].pop(p.DRIVER_FILES[0])
        p.write_json(self.root / 'plan.json', plan)
        with self.assertRaises(RuntimeError):
            p.load_plan(self.args)

    def test_main_default_is_plan_only_no_execution(self):
        with patch.object(p, 'prepare') as prepare, patch.object(p, 'validate') as validate:
            self.assertEqual(p.main(['--source', str(self.source), '--root', str(self.root), '--driver-sha', DRIVER]), 0)
        prepare.assert_not_called()
        validate.assert_not_called()

    def test_main_exception_retains_partial_report(self):
        self.saved_plan()
        prior = dict(builds={'baseline': {'sha256': 'preserved'}}, instances=[{'name': 'A1'}], roundVerdicts=[{'name': 'A1'}], status='running')
        p.write_json(self.root / 'report.json', prior)
        def fail(args, plan, report):
            report['instances'].append({'name': 'B1', 'status': 'failed'})
            raise RuntimeError('synthetic worker failure')
        with patch.object(p.sys, 'platform', 'linux'), patch.object(p, 'validate', side_effect=fail):
            result = p.main(['--source', str(self.source), '--root', str(self.root), '--driver-sha', DRIVER, '--phase', 'validate', '--execute'])
        saved = json.loads((self.root / 'report.json').read_text())
        self.assertEqual(result, 1)
        self.assertEqual(saved['status'], 'invalid_calibration')
        self.assertEqual(saved['builds'], prior['builds'])
        self.assertEqual(len(saved['instances']), 2)
        self.assertIn('synthetic worker failure', saved['failure']['reason'])

    def fake_validation(self, fail_at=None, inputs=None, cleanup=True, factor=1, invoke_work=False, process_matches=True, parameter_drift=False):
        plan = self.saved_plan()
        p.write_json(self.source / (p.BENCH + '_baseline.json'), BASELINE)
        report = dict(scope='synthetic', status='running', builds={}, instances=[], caseVerdicts=[], roundVerdicts=[], roundMetrics={})
        for role in ('baseline', 'candidate'):
            identity = self.identity(role)
            p.write_json(self.root / 'runtime' / role / 'identity.json', identity)
        calls = []
        self.ports = []
        Case = namedtuple('MysqltestCase', ('name', 'test_file', 'result_file'))
        native = SimpleNamespace(discover_cases=lambda source: [Case(n,
            source / ('tools/deploy/mysql_test/test_suite/' + n.replace('.', '/t/') + '.test'),
            source / ('tools/deploy/mysql_test/test_suite/' + n.replace('.', '/r/mysql/') + '.result'))
            for n in p.selected_cases('candidate')])
        harness = Mock()
        self._fake_harness = harness
        harness.original_inputs.side_effect = inputs or (lambda source: {'official': 'fixed'})
        harness.load_native.return_value = native
        harness.original_case.return_value = lambda call, out: dict(passed=True)
        def run(args, native, name, port, work, initialize=False):
            calls.append((name, args.role, initialize))
            self.ports.append(port)
            row = dict(name=name, status='passed', cleanupPassed=True, work={})
            if not initialize:
                row['work']['metrics'] = self.metrics(factor if args.role == 'candidate' else 1)
                if name in ('A4', 'B4'):
                    row['work']['explain'] = self.plan_rows(args.role)
            if invoke_work:
                self._running_binary = args.binary
                out = args.out / name
                out.mkdir(parents=True, exist_ok=True)
                text = self.parameter_text()
                if parameter_drift and args.role == 'candidate':
                    text = text.replace('INFO', 'WARN')
                (out / 'actual-parameters.log').write_text(text)
                try:
                    row['work'] = work(SimpleNamespace(base_dir=out / 'database', port=port), out)
                except Exception as error:
                    row.update(status='infrastructure_error', error=str(error))
            if name == fail_at:
                row.update(status='passed' if not cleanup else 'infrastructure_error', cleanupPassed=cleanup)
            return row
        harness.run_instance.side_effect = run
        original_digest = p.digest
        def digest(path):
            return p.CLIENTS[Path(path).name] if Path(path).name in p.CLIENTS else original_digest(path)
        stack = ExitStack()
        self.addCleanup(stack.close)
        def benchmark(args, baseline, scorer, final):
            def execute(call, out):
                result = dict(passed=True, metrics=self.metrics(factor if args.role == 'candidate' else 1))
                if final:
                    result['explain'] = self.plan_rows(args.role)
                return result
            return execute
        for target, value in [('load_module', lambda name, path: harness if 'harness' in name else SimpleNamespace(inspect_instance_process=lambda *a: 123) if 'sdb' in name else SCORER), ('source_identity', lambda source, commit: dict(commit=commit, tree=TREE)), ('verify_driver', lambda *a: {}), ('verify_loader', lambda *a: {}), ('digest', digest), ('benchmark_work', benchmark)]:
            stack.enter_context(patch.object(p, target, side_effect=value))
        stack.enter_context(patch.object(p.time, 'sleep'))
        if invoke_work:
            original_resolve = Path.resolve
            def resolve(path, *a, **kw):
                if path.name == 'exe' and path.parent.name == '123':
                    return self._running_binary if process_matches else self.root / 'wrong-elf'
                return original_resolve(path, *a, **kw)
            stack.enter_context(patch.object(Path, 'resolve', resolve))
        return plan, report, calls

    def test_validate_runs_all_cases_and_exact_schedule(self):
        plan, report, calls = self.fake_validation(factor=1.1)
        p.validate(self.args, plan, report)
        self.assertEqual(calls[:5], [(n, 'baseline', True) for n in p.CASES])
        self.assertEqual(calls[5:12], [(n, 'candidate', True) for n in p.selected_cases('candidate')])
        self.assertEqual(calls[12:], [(n, r, False) for n, r in p.SCHEDULE])
        self.assertEqual(report['status'], 'valid_calibration_completed')
        self.assertEqual(report['sameHostPerformance']['classification'], 'regression')
        self.assertEqual(len(report['instances']), 20)
        self.assertEqual(self.ports, list(range(7300, 7320)))
        self.assertEqual(len(set(self.ports)), 20)
        self.assertEqual(report['planAssertions'], {name: dict(role=role, queries=self.plan_rows(role))
            for name, role in (('A4', 'baseline'), ('B4', 'candidate'))})
        self.assertEqual([(call.args[0].role, call.args[-1]) for call in p.benchmark_work.call_args_list],
                         [(role, name in ('A4', 'B4')) for name, role in p.SCHEDULE])
        self.assertTrue(report['allOfficialInputsUnchanged'])

    def test_validate_stops_after_failed_or_unclean_round_preserves_evidence(self):
        for cleanup in (False, True):
            with self.subTest(cleanup=cleanup):
                plan, report, calls = self.fake_validation(fail_at='B1', cleanup=cleanup)
                with self.assertRaises(RuntimeError):
                    p.validate(self.args, plan, report)
                self.assertEqual([row[0] for row in calls[12:]], ['A1', 'B1'])
                saved = json.loads((self.root / 'report.json').read_text())
                self.assertEqual(saved['roundVerdicts'][-1]['name'], 'B1')
                self.assertNotIn('sameHostPerformance', saved)

    def test_validate_plan_failure_preserves_partial_proof_and_cleanup_before_raise(self):
        plan, report, calls = self.fake_validation()
        original_run = self._fake_harness.run_instance.side_effect
        def failed_plan(*arguments, **kwargs):
            row = original_run(*arguments, **kwargs)
            if arguments[2] == 'B4':
                row['status'] = 'failed'
                row['work'].update(passed=False, failureKind='untimed_plan_assertion_failure')
                row['work']['explain']['control_score_projection'].update(
                    passed=False, actualCalcRelevance=False, failureReason='scored control did not calculate relevance')
            return row
        self._fake_harness.run_instance.side_effect = failed_plan
        with self.assertRaisesRegex(RuntimeError, 'Owned instance failed'):
            p.validate(self.args, plan, report)
        saved = json.loads((self.root / 'report.json').read_text())
        self.assertEqual(len(saved['instances']), 20)
        self.assertTrue(saved['instances'][-1]['cleanupPassed'])
        self.assertEqual(saved['roundVerdicts'][-1]['status'], 'failed')
        self.assertFalse(saved['planAssertions']['B4']['queries']['control_score_projection']['passed'])
        self.assertIn('did not calculate', saved['planAssertions']['B4']['queries']['control_score_projection']['failureReason'])
        self.assertIn('B4', saved['roundMetrics'])
        self.assertNotIn('sameHostPerformance', saved)

    def test_validate_requires_complete_final_role_plan_proof(self):
        plan, report, calls = self.fake_validation()
        original_run = self._fake_harness.run_instance.side_effect
        def missing_plan(*arguments, **kwargs):
            row = original_run(*arguments, **kwargs)
            row['work'].pop('explain', None)
            return row
        self._fake_harness.run_instance.side_effect = missing_plan
        with self.assertRaisesRegex(RuntimeError, 'actual-plan assertions are required'):
            p.validate(self.args, plan, report)
        self.assertEqual(len(calls), 20)
        self.assertNotIn('sameHostPerformance', report)

    def test_validate_rejects_input_hash_mismatch_before_any_case(self):
        plan, report, calls = self.fake_validation(inputs=lambda source: {'official': 'changed'})
        with self.assertRaisesRegex(RuntimeError, 'hashes differ'):
            p.validate(self.args, plan, report)
        self.assertEqual(calls, [])

    def test_validate_rejects_midrun_input_mutation(self):
        count = 0
        def inputs(source):
            nonlocal count
            count += 1
            return {'official': 'fixed' if count <= 3 else 'changed'}
        plan, report, calls = self.fake_validation(inputs=inputs)
        with self.assertRaisesRegex(RuntimeError, 'inputs changed'):
            p.validate(self.args, plan, report)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(report['instances']), 1)

    def test_original_benchmark_native_runner_and_candidate_harness_are_pinned(self):
        protected = {
            'tools/benchmark/fts_large_bench.sh': '596ea5d1c0038e666b78aa94bef22d2dfb87ffc9e84dbc7e2a1da1f746424462',
            'tools/benchmark/fts_large_bench_gen.py': 'b0c816dd8211ac0471f60b72a37a3fb5c62580357a5b6d7ac95f5a8c2e3f143d',
            'tools/benchmark/fts_large_bench_score.py': 'bea52eb1c68d05693bb3ac5b910ab0e2fa0e70e01582f40466898ee1e2e3ef55',
            'tools/benchmark/fts_large_bench_setup.sql': '311826751df7f51ac45156fb4284aeca9bd9197370aceb88f4fb92832b458108',
            'tools/benchmark/fts_large_bench_baseline.json': 'f4e97eddb2e49c982b967fc8047f7cf37177eebe53d648f6746d85aacda91f0c',
            p.PREFIX + 'fts_contest_validation.py': '93098702921142f731624c5133ab05f9236589dea90e35f54e8a3586e4159583',
            p.PREFIX + 'mysqltest_for_seekdb.py': 'fc7bb1cb8e10ab0c3fd68bade220caa69b7cda94f459ef7d9cdef2e431629b22',
            p.PREFIX + 'sdb.py': '53d88d4372e5534fbd37e17be3f418a2353efea648395f0ffb859926ae9eecd6',
        }
        for relative, expected in protected.items():
            with self.subTest(path=relative):
                # Git checkouts may use CRLF on Windows. Normalize only CRLF,
                # keeping all other bytes protected by the canonical LF hash.
                canonical = (REPO / relative).read_bytes().replace(b'\r\n', b'\n')
                self.assertEqual(hashlib.sha256(canonical).hexdigest(), expected)
                crlf = canonical.replace(b'\n', b'\r\n')
                self.assertEqual(hashlib.sha256(crlf.replace(b'\r\n', b'\n')).hexdigest(), expected)

    def test_original_inputs_still_protects_every_original_official_file(self):
        path = REPO / p.PREFIX / 'fts_contest_validation.py'
        text = path.read_text(encoding='utf-8')
        function = next(node for node in ast.parse(text).body
                        if isinstance(node, ast.FunctionDef) and node.name == 'original_inputs')
        original = ast.get_source_segment(text, function)
        self.assertEqual(hashlib.sha256(original.encode()).hexdigest(),
            '6bed5afc79e4d41afd206c3ba0025ecec1b2fc1bcfc0263ee655c88a2ca62d27')
        harness = p.load_module('test_candidate_harness', path)
        self.assertEqual(harness.CASES, p.selected_cases('candidate'))
        self.assertEqual(p.selected_cases('baseline'), p.CASES)
        self.assertEqual(p.REGRESSIONS, ('fts_index.tokenize_casefold_contract', 'fts_index.match_filter_only_contract'))


    def snapshot_fixture(self):
        built = self.source / 'build_release/src/observer/seekdb'
        built.parent.mkdir(parents=True)
        built.write_bytes(b'\x7fELFsynthetic-candidate' + DRIVER.encode())
        (built.parent / 'ob_version.cpp').write_text('version ' + DRIVER)
        (self.source / 'build_release/CMakeCache.txt').write_text('CMAKE_BUILD_TYPE:STRING=RelWithDebInfo\nOB_ENABLE_UNITY:BOOL=ON\nOB_USE_LLD:BOOL=ON\n')
        (self.source / 'rust').mkdir()
        (self.root / 'runtime/candidate').mkdir(parents=True)
        (self.root / 'evidence/candidate').mkdir(parents=True)
        harness = Mock()
        harness.original_inputs.return_value = {'official': 'fixed'}
        def capture(command, output, *args, **kwargs):
            Path(output).write_text('Build ID: abcdef\n')
            return dict(exitCode=0, childrenCleaned=True)
        original_digest = p.digest
        def digest(path):
            return p.CLIENTS[Path(path).name] if Path(path).name in p.CLIENTS else original_digest(path)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(p, 'source_identity', return_value=dict(commit=DRIVER, tree=TREE)))
        stack.enter_context(patch.object(p, 'verify_driver', return_value={}))
        stack.enter_context(patch.object(p, 'load_module', return_value=harness))
        stack.enter_context(patch.object(p, 'capture_group', side_effect=capture))
        def loader(binary, log, *args):
            Path(log).write_text('synthetic loader resolved dependencies\n')
            return dict(exitCode=0, childrenCleaned=True)
        stack.enter_context(patch.object(p, 'verify_loader', side_effect=loader))
        stack.enter_context(patch.object(p, 'digest', side_effect=digest))
        stack.enter_context(patch.object(p.subprocess, 'check_output', return_value=b'rustc 1.98.1 (synthetic)'))
        return built

    def test_snapshot_binds_original_build_artifact_and_version(self):
        built = self.snapshot_fixture()
        record = p.snapshot(self.args, {})
        self.assertEqual(record['sourceCommit'], DRIVER)
        self.assertEqual(record['sourceTree'], TREE)
        self.assertEqual(record['kernelOrigin'], p.KERNEL)
        self.assertEqual(record['sha256'], p.digest(built))
        self.assertEqual(record['buildId'], 'abcdef')
        self.assertEqual(record['officialInputHashes'], {'official': 'fixed'})
        self.assertEqual(Path(record['binary']).read_bytes(), built.read_bytes())
        self.assertEqual(json.loads((self.root / 'runtime/candidate/identity.json').read_text())['sha256'], record['sha256'])

    def test_snapshot_rejects_stale_generated_version(self):
        built = self.snapshot_fixture()
        (built.parent / 'ob_version.cpp').write_text('old version ' + p.BASELINE)
        with self.assertRaisesRegex(RuntimeError, 'version'):
            p.snapshot(self.args, {})
        self.assertFalse((self.root / 'runtime/candidate/identity.json').exists())

    def test_snapshot_rejects_changed_build_settings(self):
        self.snapshot_fixture()
        (self.source / 'build_release/CMakeCache.txt').write_text('CMAKE_BUILD_TYPE:STRING=Debug\n')
        with self.assertRaisesRegex(RuntimeError, 'build setting'):
            p.snapshot(self.args, {})

    def test_snapshot_rejects_wrong_rust(self):
        self.snapshot_fixture()
        with patch.object(p.subprocess, 'check_output', return_value=b'rustc 1.99.0 synthetic'):
            with self.assertRaisesRegex(RuntimeError, 'Rust'):
                p.snapshot(self.args, {})

    def test_snapshot_rejects_invalid_build_id(self):
        self.snapshot_fixture()
        def capture(command, output, *args, **kwargs):
            Path(output).write_text('error: no ELF notes')
            return dict(exitCode=0, childrenCleaned=True)
        with patch.object(p, 'capture_group', side_effect=capture):
            with self.assertRaisesRegex(RuntimeError, 'BuildID'):
                p.snapshot(self.args, {})

    def test_snapshot_refuses_replacing_preserved_elf(self):
        self.snapshot_fixture()
        destination = self.root / 'runtime/candidate/seekdb'
        destination.write_bytes(b'preserve me')
        with self.assertRaisesRegex(RuntimeError, 'replace'):
            p.snapshot(self.args, {})
        self.assertEqual(destination.read_bytes(), b'preserve me')

    def test_prepare_preserves_driver_and_checks_exact_source_switch(self):
        self.root.rmdir()
        for name in p.DRIVER_FILES:
            path = self.source / p.PREFIX / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic ' + name)
        def git(source, *arguments):
            if arguments[0] == 'show':
                return (self.source / arguments[1].split(':', 1)[1]).read_bytes()
            return b''
        with patch.object(p, 'source_identity', return_value=dict(commit=DRIVER, tree=TREE)), patch.object(p, 'verify_driver', return_value={}), patch.object(p, 'git', side_effect=git) as mocked_git, patch.object(p, 'switch_source') as switch:
            plan = p.prepare(self.args)
        switch.assert_called_once_with(self.source, DRIVER, p.BASELINE)
        self.assertEqual(set(plan['driverFiles']), set(p.DRIVER_FILES))
        self.assertTrue((self.root / 'sources').is_dir())
        self.assertIn(('worktree', 'add', '--detach', str(self.root / 'sources/baseline'), p.BASELINE), [call.args[1:] for call in mocked_git.call_args_list])
        for name in p.DRIVER_FILES:
            self.assertEqual((self.root / 'driver' / name).read_bytes(), (self.source / p.PREFIX / name).read_bytes())

    def test_prepare_rejects_existing_or_nested_root_before_git(self):
        with patch.object(p, 'git') as git:
            with self.assertRaises(RuntimeError):
                p.prepare(self.args)
            self.args.root = self.source / 'inside'
            with self.assertRaises(RuntimeError):
                p.prepare(self.args)
        git.assert_not_called()

    @staticmethod
    def parameter_text():
        return 'memory_budget\t4G\nvector_memory_limit\t1GB\ndatafile_maxsize\t2GiB\nlog_disk_size\t2147483648\ndatafile_size\t1G\nsyslog_level\tINFO\n'

    def test_effective_parameters_normalizes_units(self):
        path = self.root / 'parameters.log'
        path.write_text(self.parameter_text())
        observed = p.effective_parameters(path)
        self.assertEqual(observed['memory_budget'], 4 * 1024 ** 3)
        self.assertEqual(observed['vector_memory_limit'], 1024 ** 3)
        self.assertEqual(observed['syslog_level'], 'INFO')

    def test_effective_parameters_rejects_missing_malformed_conflicting_and_wrong(self):
        text = self.parameter_text()
        for bad in [text.replace('4G', '2G'), text.replace('4G', 'unlimited'), text + 'memory_budget\t2G\n', text.replace('syslog_level\tINFO\n', ''), text.replace('datafile_size\t1G\n', ''), 'ERROR 123\n']:
            path = self.root / 'parameters.log'
            path.write_text(bad)
            with self.subTest(text=bad), self.assertRaises(RuntimeError):
                p.effective_parameters(path)

    def test_owned_callback_checks_live_executable_and_effective_parameters(self):
        plan, report, calls = self.fake_validation(invoke_work=True)
        p.validate(self.args, plan, report)
        self.assertEqual(report['status'], 'valid_calibration_completed')
        self.assertEqual(len(calls), 20)
        self.assertEqual(set(report['observedParameters']), {'cases', 'rounds'})
        for name, role in p.SCHEDULE:
            proof = json.loads((self.root / 'validation/rounds' / name / 'owned-process.json').read_text())
            self.assertEqual(proof['binary'], str(self.root / 'runtime' / role / 'seekdb'))
            self.assertEqual(proof['pid'], 123)

    def test_owned_callback_rejects_wrong_live_executable(self):
        plan, report, calls = self.fake_validation(invoke_work=True, process_matches=False)
        with self.assertRaises(RuntimeError):
            p.validate(self.args, plan, report)
        self.assertEqual(len(calls), 1)
        self.assertIn('exact role ELF', report['instances'][0]['error'])
        self.assertEqual(report['instances'][0]['status'], 'infrastructure_error')

    def test_owned_callback_rejects_role_parameter_drift(self):
        plan, report, calls = self.fake_validation(invoke_work=True, parameter_drift=True)
        with self.assertRaises(RuntimeError):
            p.validate(self.args, plan, report)
        self.assertEqual(len(calls), 6)
        self.assertIn('settings differ', report['instances'][-1]['error'])
        self.assertEqual(report['roundVerdicts'], [])

    def test_finalize_marks_incomplete_invalid_without_destroying_evidence(self):
        self.saved_plan()
        for status, expected in [('running', 1), ('phase_completed', 1), ('invalid_calibration', 1), ('valid_calibration_completed', 0)]:
            p.write_json(self.root / 'report.json', dict(status=status, builds={'baseline': 'kept'}, failure={'reason': 'original failure'}))
            with self.subTest(status=status), patch.object(p.sys, 'platform', 'linux'):
                code = p.main(['--source', str(self.source), '--root', str(self.root), '--driver-sha', DRIVER, '--phase', 'finalize', '--execute'])
            self.assertEqual(code, expected)
            saved = json.loads((self.root / 'report.json').read_text())
            self.assertEqual(saved['builds'], {'baseline': 'kept'})
            self.assertEqual(saved['failure']['reason'], 'original failure')
            self.assertEqual(saved['status'], 'invalid_calibration' if expected else 'valid_calibration_completed')

    def test_finalize_missing_report_is_invalid(self):
        self.saved_plan()
        with patch.object(p.sys, 'platform', 'linux'):
            code = p.main(['--source', str(self.source), '--root', str(self.root), '--driver-sha', DRIVER, '--phase', 'finalize', '--execute'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads((self.root / 'report.json').read_text())['status'], 'invalid_calibration')

    def test_exact_two_percent_boundary_is_inconclusive(self):
        scorer = Mock()
        for value in (.02, -.02):
            scorer.calculate_score.return_value = dict(mean_improvement=value, categories={}, score=0)
            self.assertEqual(p.performance(self.pairs([1] * 4), BASELINE, scorer)['classification'], 'inconclusive')

    def test_elf_requires_embedded_exact_source_revision(self):
        identity = self.identity()
        path = Path(identity['binary'])
        path.write_bytes(b'\x7fELF' + p.BASELINE.encode())
        identity.update(bytes=path.stat().st_size, sha256=p.digest(path))
        with self.assertRaisesRegex(RuntimeError, 'exact build-source'):
            p.validate_identity(identity, 'candidate', DRIVER, self.root)

    def test_benchmark_failure_does_not_parse_or_score(self):
        args = SimpleNamespace(workspace=self.source, binary=self.root / 'seekdb', obclient='/client', source=self.source, role='candidate', expected_sha=DRIVER)
        for result in [dict(exitCode=1, childrenCleaned=True), dict(exitCode=0, childrenCleaned=False)]:
            with self.subTest(result=result), patch.object(p.time, 'sleep'), patch.object(p, 'capture_group', return_value=result), patch.object(p, 'strict_report') as parse:
                row = p.benchmark_work(args, BASELINE, SCORER, False)(SimpleNamespace(port=7300), self.root)
            self.assertFalse(row['passed'])
            self.assertEqual(row['failureKind'], 'benchmark_process_failure')
            parse.assert_not_called()

    def test_benchmark_positive_uses_original_config_no_override(self):
        args = SimpleNamespace(workspace=self.source, binary=self.root / 'seekdb', obclient='/client', source=self.source, role='candidate', expected_sha=DRIVER)
        path = self.report_file()
        path.rename(self.root / 'report.txt')
        captured = []
        events = []
        def capture(command, output, *a, **kw):
            captured.append((command, kw))
            events.append(Path(output).name)
            Path(output).write_text('{}' if Path(output).name == 'frozen-score.json' else 'benchmark completed')
            return dict(exitCode=0, childrenCleaned=True)
        def plan(*arguments):
            events.append('untimed_explain')
            return {'exact': True}
        with patch.object(p.time, 'sleep'), patch.object(p, 'capture_group', side_effect=capture), patch.object(p, 'explain_queries', side_effect=plan) as explain:
            row = p.benchmark_work(args, BASELINE, SCORER, True)(SimpleNamespace(port=7300), self.root)
        self.assertTrue(row['passed'])
        self.assertEqual(captured[0][0], ['bash', str(self.source / (p.BENCH + '.sh'))])
        self.assertEqual({key: captured[0][1]['env'][key.upper()] for key in p.CONFIG}, {key: str(value) for key, value in p.CONFIG.items()})
        self.assertNotIn('--allow-config-mismatch', captured[1][0])
        self.assertNotIn('--baseline', captured[1][0])
        explain.assert_called_once()
        self.assertEqual(events, ['benchmark.log', 'frozen-score.json', 'untimed_explain'])

    def test_nonfinal_benchmark_never_runs_plan_controls(self):
        args = SimpleNamespace(workspace=self.source, binary=self.root / 'seekdb', obclient='/client', source=self.source, role='candidate', expected_sha=DRIVER)
        self.report_file().rename(self.root / 'report.txt')
        def capture(command, output, *a, **kw):
            Path(output).write_text('{}')
            return dict(exitCode=0, childrenCleaned=True)
        with patch.object(p.time, 'sleep'), patch.object(p, 'capture_group', side_effect=capture), patch.object(p, 'explain_queries') as explain:
            result = p.benchmark_work(args, BASELINE, SCORER, False)(SimpleNamespace(port=7300), self.root)
        self.assertTrue(result['passed'])
        self.assertNotIn('explain', result)
        explain.assert_not_called()

    def test_plan_parser_requires_actual_single_operator_and_annotation(self):
        for scored in (False, True):
            for tree in ('  ', '\u2514\u2500'):
                with self.subTest(scored=scored, tree=tree):
                    text = self.plan_text(scored).replace('|1 |  TEXT', '|1 |' + tree + 'TEXT')
                    result = p.plan_relevance(text)
                    self.assertIs(result['actualCalcRelevance'], scored)
                    self.assertEqual(result['scanId'], 1)
                    self.assertEqual(len(result['calcRelevanceEvidence']), 1)

    def test_plan_relevance_contract_preserves_implicit_ranked_limit(self):
        for role in ('baseline', 'candidate'):
            for name in p.QUERY_NAMES + p.PLAN_CONTROLS:
                with self.subTest(role=role, query=name):
                    self.assertIs(p.expected_plan_relevance(role, name),
                        False if role == 'candidate' and name in ('query_cn', 'query_beng', 'query_mixed') else True)
        with self.assertRaisesRegex(RuntimeError, 'role confusion'):
            p.expected_plan_relevance('other', 'query_cn')
        with self.assertRaisesRegex(RuntimeError, 'Unknown EXPLAIN query'):
            p.expected_plan_relevance('candidate', 'unknown')

    def test_plan_parser_accepts_existing_official_result_plan_format(self):
        # Complete real repository plans, including Unicode tree glyphs and
        # SORT/retrieval output ownership. These are not Actions-log fixtures.
        path = REPO / 'tools/deploy/mysql_test/test_suite/fts_index/r/mysql/create_fts_index_afterward.result'
        original = path.read_bytes()
        sections = path.read_text(encoding='utf-8').split('EXPLAIN SELECT')[1:]
        self.assertEqual(len(sections), 2)
        for section, columns in zip(sections, ('b, fts_col_orders.c', 'c, fts_col_orders.b')):
            with self.subTest(query=section.splitlines()[0]):
                self.assertIn('TEXT RETRIEVAL SCAN', section)
                self.assertIn('Outputs & filters:', section)
                self.assertIn('  0 - output(', section)
                parsed = p.plan_relevance(section)
                self.assertEqual(parsed['scanId'], 1)
                self.assertIs(parsed['actualCalcRelevance'], True)
                self.assertEqual(parsed['calcRelevanceEvidence'], [
                    "      calc_relevance=true, match_expr(MATCH(fts_col_orders.%s) AGAINST('aa')), " % columns])
        self.assertEqual(path.read_bytes(), original)

    def test_plan_parser_accepts_hash_verified_actual_actions_outputs(self):
        fixtures = ACTUAL_ACTIONS_EXPLAINS['fixtures']
        self.assertEqual([row['query'] for row in fixtures], list(p.QUERY_NAMES))
        for row in fixtures:
            with self.subTest(query=row['query']):
                # The same original hash was recorded for A4 and B4.
                self.assertEqual(hashlib.sha256(row['output'].encode('utf-8')).hexdigest(), row['sha256'])
                parsed = p.plan_relevance(row['output'])
                self.assertEqual(parsed['scanId'], 2 if row['query'] == 'query_limit' else 1)
                self.assertIs(parsed['actualCalcRelevance'], True)
                self.assertEqual(parsed['calcRelevanceEvidence'], [line for line in row['output'].splitlines()
                                                                     if 'calc_relevance' in line])
        limit = fixtures[-1]['output']
        self.assertIn('|1 |└─SUBPLAN SCAN', limit)
        self.assertIn('sort_keys([MATCH(docs.content)', limit)
        # The real LIMIT parent is a SUBPLAN. Moving evidence onto that node
        # must fail even though the table still names a retrieval scan.
        misplaced = limit.replace('  2 - output([docs.id])', '  3 - output([docs.id])')
        with self.assertRaises(RuntimeError):
            p.plan_relevance(misplaced)

    def test_plan_parser_rejects_missing_malformed_ambiguous_conflicting_evidence(self):
        text = self.plan_text()
        bad = ['', 'calc_relevance=false\n', text.replace('TEXT RETRIEVAL SCAN', 'TABLE SCAN'),
            text.replace('TEXT RETRIEVAL SCAN', 'TABLE FULL SCAN'),
            text.replace('|0 |SCALAR GROUP BY', '|0 |TEXT RETRIEVAL SCAN'),
            text.replace('Outputs & filters:', 'Unknown:'), text + 'Outputs & filters:\n',
            text.replace('calc_relevance=false', 'calc_relevance=unknown'),
            text.replace('calc_relevance=false', 'calc_relevance=falseish'),
            text.replace('calc_relevance=false', 'calc_relevance=0'),
            text.replace('match_expr(MATCH(', 'other_expr(MATCH('),
            text.replace('  1 - output(', '  2 - output('),
            text.replace('|1 |  TEXT', '|2 |  TEXT'),
            text.replace('      calc_relevance', '  2 - output([docs.id]),\n      calc_relevance'),
            text + 'calc_relevance=true\n', text + 'calc_relevance=false\n',
            text + '  1 - output([docs.id]), filter(nil)\n',
            text.replace('calc_relevance=false', 'calc_relevance=false, calc_relevance=true')]
        for index, value in enumerate(bad):
            with self.subTest(index=index), self.assertRaises(RuntimeError):
                p.plan_relevance(value)

    def test_scored_controls_derive_exact_original_literal_and_schema(self):
        path = REPO / (p.BENCH + '.sh')
        before = p.digest(path)
        original = p.queries_from_benchmark(path)
        queries = p.plan_queries(original)
        self.assertEqual(list(queries), list(p.QUERY_NAMES + p.PLAN_CONTROLS))
        self.assertEqual({name: queries[name] for name in p.QUERY_NAMES}, original)
        expression = original['query_cn'].split(' WHERE ', 1)[1][:-1]
        self.assertEqual(queries['control_score_projection'],
            'SELECT id, %s AS relevance FROM fts_large_bench.docs WHERE %s;' % (expression, expression))
        self.assertEqual(queries['control_score_order'],
            'SELECT id FROM fts_large_bench.docs WHERE %s ORDER BY %s DESC, id LIMIT 20;' % (expression, expression))
        self.assertEqual(queries['control_nonzero_threshold'],
            'SELECT COUNT(*) FROM fts_large_bench.docs WHERE %s AND %s > 0.5;' % (expression, expression))
        self.assertEqual(p.digest(path), before)
        for sql in (original['query_cn'].replace('fts_large_bench.docs', 'other.docs'),
                    original['query_cn'].replace('COUNT(*)', 'id')):
            with self.subTest(sql=sql), self.assertRaises(RuntimeError):
                p.plan_queries(dict(original, query_cn=sql))

    def test_nonzero_control_keeps_identical_indexable_match_and_numeric_consumer(self):
        original = p.queries_from_benchmark(REPO / (p.BENCH + '.sh'))
        queries = p.plan_queries(original)
        expression = original['query_cn'].split(' WHERE ', 1)[1][:-1]
        control = queries['control_nonzero_threshold']
        prefix, predicates = control.split(' WHERE ', 1)
        self.assertEqual(prefix, 'SELECT COUNT(*) FROM fts_large_bench.docs')
        # The positive conjunct exposes fulltext access. Its identical numeric
        # consumer still requires actual relevance, without changing the rows.
        self.assertEqual(predicates.split(' AND '), [expression, expression + ' > 0.5;'])
        self.assertEqual(control.count(expression), 2)
        self.assertEqual({name: queries[name] for name in p.QUERY_NAMES}, original)
        for role in ('baseline', 'candidate'):
            self.assertIs(p.expected_plan_relevance(role, 'control_nonzero_threshold'), True)

    def test_explain_exact_official_queries_plus_controls_role_flags_and_hashes(self):
        queries = p.plan_queries(p.queries_from_benchmark(REPO / (p.BENCH + '.sh')))
        for role in ('baseline', 'candidate'):
            args = SimpleNamespace(source=REPO, role=role, obclient='/client')
            captured = []
            def capture(command, output, *a, **kw):
                captured.append((command, kw['stdin']))
                scored = p.expected_plan_relevance(role, Path(output).name.removesuffix('-explain.log'))
                Path(output).write_text(self.plan_text(scored), encoding='utf-8')
                return dict(exitCode=0, childrenCleaned=True)
            with self.subTest(role=role), patch.object(p, 'capture_group', side_effect=capture):
                result = p.explain_queries(args, SimpleNamespace(port=7300), self.root, {})
                self.assertEqual([value for _, value in captured],
                    [(p.SESSION + 'EXPLAIN ' + sql + '\n').encode('utf-8') for sql in queries.values()])
                self.assertEqual(list(result), list(queries))
                for command, _ in captured:
                    self.assertEqual(command[-4:-1], ['-N', '-B', '--raw'])
                for name, row in result.items():
                    expected = p.expected_plan_relevance(role, name)
                    self.assertIs(row['actualCalcRelevance'], expected)
                    self.assertIs(row['expectedCalcRelevance'], expected)
                    self.assertTrue(row['passed'])
                    self.assertEqual(row['role'], role)
                    self.assertEqual(row['sqlSha256'], p.digest(self.root / (name + '-explain.sql')))
                    self.assertEqual(row['outputSha256'], p.digest(self.root / (name + '-explain.log')))
                self.assertEqual(json.loads((self.root / 'explain-result.json').read_text()), result)

    def test_explain_role_confusion_never_starts_process(self):
        with patch.object(p, 'capture_group') as capture:
            with self.assertRaisesRegex(RuntimeError, 'role confusion'):
                p.explain_queries(SimpleNamespace(role='other'), None, self.root, {})
        capture.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'role confusion'):
            p.selected_cases('other')

    def test_explain_rejects_actual_role_mismatch_and_unscored_controls(self):
        for role, failed_query, bad_scored in (('baseline', 'query_cn', False), ('candidate', 'query_cn', True),
                ('candidate', 'query_limit', False), ('candidate', 'control_score_projection', False),
                ('candidate', 'control_score_order', False), ('candidate', 'control_nonzero_threshold', False)):
            args = SimpleNamespace(source=REPO, role=role, obclient='/client')
            def capture(command, output, *a, **kw):
                name = Path(output).name.removesuffix('-explain.log')
                scored = bad_scored if name == failed_query else p.expected_plan_relevance(role, name)
                Path(output).write_text(self.plan_text(scored))
                return dict(exitCode=0, childrenCleaned=True)
            with self.subTest(role=role, failed_query=failed_query), patch.object(p, 'capture_group', side_effect=capture):
                with self.assertRaises(p.PlanEvidenceError) as raised:
                    p.explain_queries(args, SimpleNamespace(port=7300), self.root, {})
                results = raised.exception.results
                self.assertEqual(list(results)[-1], failed_query)
                self.assertFalse(results[failed_query]['passed'])
                self.assertIn('differs from required', results[failed_query]['failureReason'])
                self.assertEqual(json.loads((self.root / 'explain-result.json').read_text()), results)

    def test_explain_failure_preserves_partial_output_hash_and_reason_before_exception(self):
        args = SimpleNamespace(source=REPO, role='candidate', obclient='/client')
        for process, output in ((dict(exitCode=1, childrenCleaned=True), 'ERROR 1000'),
                (dict(exitCode=124, childrenCleaned=False, timedOut=True), self.plan_text()),
                (dict(exitCode=0, childrenCleaned=False), self.plan_text()),
                (dict(exitCode=0, childrenCleaned=1), self.plan_text()),
                (dict(exitCode=False, childrenCleaned=True), self.plan_text()),
                (dict(exitCode=0, childrenCleaned=True), 'calc_relevance=false\n'),
                (dict(exitCode=0, childrenCleaned=True), self.plan_text() + 'calc_relevance=true\n')):
            def capture(command, path, *a, **kw):
                Path(path).write_text(output)
                return dict(process)
            with self.subTest(process=process, output=output[:30]), patch.object(p, 'capture_group', side_effect=capture) as capture_mock:
                with self.assertRaises(p.PlanEvidenceError) as raised:
                    p.explain_queries(args, SimpleNamespace(port=7300), self.root, {})
                capture_mock.assert_called_once()
                partial = json.loads((self.root / 'explain-result.json').read_text())
                self.assertEqual(partial, raised.exception.results)
                self.assertEqual(list(partial), ['query_cn'])
                row = partial['query_cn']
                self.assertFalse(row['passed'])
                self.assertTrue(row['failureReason'])
                self.assertEqual(row['outputSha256'], p.digest(self.root / 'query_cn-explain.log'))
                self.assertEqual(row['sqlSha256'], p.digest(self.root / 'query_cn-explain.sql'))
                self.assertEqual(row['calcRelevanceEvidence'], [line for line in output.splitlines() if 'calc_relevance' in line])

    def test_explain_launch_error_preserves_failure(self):
        args = SimpleNamespace(source=REPO, role='candidate', obclient='/client')
        with patch.object(p, 'capture_group', side_effect=FileNotFoundError('client missing')):
            with self.assertRaises(p.PlanEvidenceError) as raised:
                p.explain_queries(args, SimpleNamespace(port=7300), self.root, {})
        row = raised.exception.results['query_cn']
        self.assertIn('client missing', row['failureReason'])
        self.assertFalse(row['passed'])
        self.assertIsNone(row['outputSha256'])

    def test_benchmark_plan_failure_retains_metrics_and_evidence_for_owned_cleanup(self):
        args = SimpleNamespace(workspace=self.source, binary=self.root / 'seekdb', obclient='/client', source=self.source, role='candidate', expected_sha=DRIVER)
        self.report_file().rename(self.root / 'report.txt')
        results = {'query_cn': dict(passed=False, failureReason='missing actual plan')}
        def capture(command, output, *a, **kw):
            Path(output).write_text('{}')
            return dict(exitCode=0, childrenCleaned=True)
        with patch.object(p.time, 'sleep'), patch.object(p, 'capture_group', side_effect=capture), \
                patch.object(p, 'explain_queries', side_effect=p.PlanEvidenceError('plan assertion failed', results)):
            row = p.benchmark_work(args, BASELINE, SCORER, True)(SimpleNamespace(port=7300), self.root)
        self.assertFalse(row['passed'])
        self.assertEqual(row['failureKind'], 'untimed_plan_assertion_failure')
        self.assertEqual(row['explain'], results)
        self.assertEqual(row['metrics'], self.metrics())

    @unittest.skipUnless(sys.platform == 'linux', 'Real SIGTERM lifecycle requires Linux')
    def test_real_sigterm_cleans_owned_child_and_prevents_next_work(self):
        pid_path = self.root / 'synthetic-child.pid'
        interrupted = self.root / 'interrupted.txt'
        subsequent = self.root / 'subsequent-work.txt'
        child_code = 'import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)'
        wrapper_code = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import fts_paired_validation as p
p.install_termination_handlers()
root = Path(sys.argv[2])
try:
    p.capture_group([sys.executable, '-c', sys.argv[3], str(root / 'synthetic-child.pid')], root / 'child.log', root, 70)
    (root / 'subsequent-work.txt').write_text('must not execute')
except InterruptedError:
    (root / 'interrupted.txt').write_text('caught')
'''
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        wrapper = subprocess.Popen([sys.executable, '-c', wrapper_code, str(Path(p.__file__).parent), str(self.root), child_code],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
        child_pid = None
        def gone(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and wrapper.poll() is None:
                if pid_path.is_file() and pid_path.read_text().strip():
                    child_pid = int(pid_path.read_text())
                    break
                time.sleep(.05)
            self.assertIsNotNone(child_pid, 'Synthetic child never published its PID')
            os.kill(wrapper.pid, signal.SIGTERM)
            stdout, stderr = wrapper.communicate(timeout=20)
            self.assertEqual(wrapper.returncode, 0, (stdout + stderr).decode('utf-8', 'replace'))
            self.assertEqual(interrupted.read_text(), 'caught')
            self.assertFalse(subsequent.exists(), 'Work ran after termination')
            deadline = time.monotonic() + 3
            while not gone(child_pid) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(gone(child_pid), 'Owned child survived wrapper SIGTERM')
        finally:
            # Only these two freshly created, positively identified process groups
            # may be signalled. Never enumerate or kill unrelated processes.
            if wrapper.poll() is None:
                os.killpg(wrapper.pid, signal.SIGKILL)
                wrapper.communicate(timeout=10)
            if child_pid is None and pid_path.is_file() and pid_path.read_text().strip():
                child_pid = int(pid_path.read_text())
            if child_pid is not None and not gone(child_pid):
                cmdline = Path('/proc') / str(child_pid) / 'cmdline'
                try:
                    owned = os.fsencode(str(pid_path)) in cmdline.read_bytes().split(b'\0')
                except FileNotFoundError:
                    owned = False
                if owned:
                    try:
                        os.killpg(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if wrapper.stdout:
                wrapper.stdout.close()
            if wrapper.stderr:
                wrapper.stderr.close()

    @staticmethod
    def compatibility_record():
        import fts_plan_compatibility as c
        row = dict(role='candidate', name=c.CASE, status='failed', cleanupPassed=True,
            work=dict(passed=False, exitCode=1, invocations=1, inputsUnchanged=True,
                      testSha256=c.TEST_SHA256, expectedSha256=c.GOLDEN_SHA256))
        def info(name, sha='c' * 64, count=99, lines=3):
            return dict(path='/synthetic/' + name, sha256=sha, bytes=count,
                        lines=lines, endsWithNewline=True)
        proof = dict(scheme=c.SCHEME, accepted=True, role='candidate', caseName=c.CASE,
            rawNativeStatus='failed', nativePassed=False, formalContestPass=False,
            approvedDifferenceCount=4, completeBytesCompared=True, eofCompared=True,
            inputsUnchanged=True, ownedCleanupPassed=True,
            originalGolden=info('golden', c.GOLDEN_SHA256, 184285, 2833),
            actualReject=info('reject', '737c4257fabf57dbd5365fdd04cfe2573206129d19991a8a1616e7e3db1e4896', 184289, 2833),
            originalTest=info('test', c.TEST_SHA256), nativeOutput=info('output'), nativeWrapper=info('wrapper'),
            nativeMismatchTimestamp='2026-11-08 12:34:56',
            adjacentScoredControl=dict(annotationLine=1603, calcRelevance=True),
            changes=[dict(sqlStartLine=start, annotationLine=line, scanId=scan,
                          statement=sql.decode().rstrip('\n'), purpose=purpose,
                          expectedCalcRelevance=True, actualCalcRelevance=False)
                     for start, line, scan, purpose, sql in c.SPECS])
        context = dict(sourceCommit=p.CANDIDATE, validationDriverCommit=DRIVER,
            source='/synthetic/source', out='/synthetic/out',
            nativeResult=dict(path='/synthetic/out/result.json', sha256='e' * 64, bytes=555))
        for key, path in dict(originalGolden=Path(context['source']) / c.GOLDEN,
                originalTest=Path(context['source']) / c.TEST,
                actualReject=Path(context['out']) / 'mysqltest-log/simple_query.reject',
                nativeOutput=Path(context['out']) / 'mysqltest-output.log',
                nativeWrapper=Path(context['out']) / 'original-run-case.log').items():
            proof[key]['path'] = str(path)
        proof['validationContext'] = context
        row['validationContext'] = context
        return proof, row

    def diagnostic_fixture(self):
        source = self.root / 'sources/candidate'
        corrected = (REPO / p.DIAGNOSTIC_TEST).read_bytes()
        original = corrected.replace(p.DIAGNOSTIC_LITERAL, p.DIAGNOSTIC_TOKEN)
        for path, data in ((source / p.DIAGNOSTIC_TEST, original),
                (source / p.DIAGNOSTIC_RESULT, (REPO / p.DIAGNOSTIC_RESULT).read_bytes()),
                (self.source / p.DIAGNOSTIC_TEST, corrected)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        Case = namedtuple('MysqltestCase', ('name', 'test_file', 'result_file'))
        case = Case(p.DIAGNOSTIC_CASE, source / p.DIAGNOSTIC_TEST, source / p.DIAGNOSTIC_RESULT)
        call_args = SimpleNamespace(role='candidate', source=source, expected_sha=p.CANDIDATE)
        return case, call_args

    def diagnostic_row(self):
        proof = p.diagnostic_provenance(self.root, self.root / 'sources/candidate', self.source, DRIVER)
        return dict(role='candidate', name=p.DIAGNOSTIC_CASE, status='passed', cleanupPassed=True,
            work=dict(passed=True, exitCode=0, invocations=1, inputsUnchanged=True,
                testSha256=p.DIAGNOSTIC_TEST_SHA, expectedSha256=p.DIAGNOSTIC_RESULT_SHA,
                diagnosticInput=proof, diagnosticInputVerified=True))

    def test_added_diagnostic_public_test_is_exactly_two_literal_substitutions(self):
        case, _ = self.diagnostic_fixture()
        raw, corrected = case.test_file.read_bytes(), (REPO / p.DIAGNOSTIC_TEST).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), p.DIAGNOSTIC_ORIGINAL_SHA)
        self.assertEqual(hashlib.sha256(corrected).hexdigest(), p.DIAGNOSTIC_TEST_SHA)
        self.assertEqual(raw.count(p.DIAGNOSTIC_TOKEN), 2)
        self.assertEqual(corrected, raw.replace(p.DIAGNOSTIC_TOKEN, p.DIAGNOSTIC_LITERAL))
        self.assertEqual(corrected.replace(p.DIAGNOSTIC_LITERAL, p.DIAGNOSTIC_TOKEN), raw)
        self.assertEqual(raw.count(b'\n'), corrected.count(b'\n'))
        self.assertEqual(p.digest(REPO / p.DIAGNOSTIC_RESULT), p.DIAGNOSTIC_RESULT_SHA)
        self.assertEqual(len(p.DIAGNOSTIC_QUERY.split()), 302)
        self.assertEqual(p.DIAGNOSTIC_QUERY.split(), [b'alpha', b'beta'] +
                         [b'absent' + str(i).encode() for i in range(1, 301)])

    def test_added_diagnostic_override_passes_owned_copy_to_unchanged_harness(self):
        case, call_args = self.diagnostic_fixture()
        native, harness = object(), Mock()
        before = {path: path.read_bytes() for path in (case.test_file, case.result_file, self.source / p.DIAGNOSTIC_TEST)}
        harness.original_case.return_value = lambda call, out: self.diagnostic_row()['work']
        execute = p.diagnostic_case(self.args, call_args, native, case, harness)
        used_args, used_native, used_case = harness.original_case.call_args.args
        self.assertIs(used_args, call_args)
        self.assertIs(used_native, native)
        self.assertEqual(used_case, case._replace(test_file=self.root / p.DIAGNOSTIC_OWNED_TEST))
        out = self.root / 'validation/cases/candidate' / p.DIAGNOSTIC_CASE
        out.mkdir(parents=True)
        row = self.diagnostic_row()
        row['work'] = execute(object(), out)
        self.assertTrue(p.accepted_diagnostic_input(row, DRIVER))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        proof = json.loads((self.root / p.DIAGNOSTIC_PROVENANCE).read_bytes())
        self.assertEqual(proof, row['work']['diagnosticInput'])
        self.assertEqual(proof['historicalFailure']['successfulAssertions'], 60)
        self.assertIs(proof['historicalFailure']['wideAssertionRan'], False)
        self.assertEqual(proof['actualCandidateBuildCommit'], p.CANDIDATE)
        with self.assertRaises(FileExistsError):
            p.diagnostic_case(self.args, call_args, native, case, harness)

    def test_added_diagnostic_override_rejects_wrong_case_role_source_or_expected(self):
        case, call_args = self.diagnostic_fixture()
        for bad_case, bad_args in ((case._replace(name=p.CASES[-1]), call_args),
                (case._replace(test_file=self.source / p.DIAGNOSTIC_TEST), call_args),
                (case._replace(result_file=self.source / p.DIAGNOSTIC_RESULT), call_args),
                (case, SimpleNamespace(**dict(vars(call_args), role='baseline'))),
                (case, SimpleNamespace(**dict(vars(call_args), expected_sha=DRIVER)))):
            with self.subTest(case=bad_case, args=bad_args), self.assertRaisesRegex(RuntimeError, 'only permits'):
                p.diagnostic_case(self.args, bad_args, object(), bad_case, Mock())
        for path in (self.root.parent / 'elsewhere', self.root / 'sources/candidate/..'):
            with self.assertRaisesRegex(RuntimeError, 'bounds'):
                p.diagnostic_provenance(self.root, path, self.source, DRIVER)

    def test_added_diagnostic_override_rejects_any_original_or_corrected_or_golden_byte_change(self):
        case, call_args = self.diagnostic_fixture()
        for path in (case.test_file, case.result_file, self.source / p.DIAGNOSTIC_TEST):
            before = path.read_bytes()
            for after in (before + b'\n', before.replace(b'\n', b'\r\n'), before[:-1]):
                path.write_bytes(after)
                with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, 'size/hash'):
                    p.diagnostic_case(self.args, call_args, object(), case, Mock())
            path.write_bytes(before)
        self.assertFalse((self.root / 'diagnostic-inputs').exists())

    def test_added_diagnostic_override_rejects_links_and_preexisting_owned_directory(self):
        case, call_args = self.diagnostic_fixture()
        for path in (case.test_file, case.result_file, self.source / p.DIAGNOSTIC_TEST):
            alias = path.with_name('alias')
            os.link(path, alias)
            with self.assertRaisesRegex(RuntimeError, 'type/link'):
                p.diagnostic_case(self.args, call_args, object(), case, Mock())
            alias.unlink()
            path.rename(alias)
            path.symlink_to(alias)
            with self.assertRaisesRegex(RuntimeError, 'Symlink'):
                p.diagnostic_case(self.args, call_args, object(), case, Mock())
            path.unlink()
            alias.rename(path)
        (self.root / 'diagnostic-inputs').symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Symlink'):
            p.diagnostic_case(self.args, call_args, object(), case, Mock())
        (self.root / 'diagnostic-inputs').unlink()
        (self.root / 'diagnostic-inputs').mkdir()
        with self.assertRaises(FileExistsError):
            p.diagnostic_case(self.args, call_args, object(), case, Mock())

    def test_added_diagnostic_guard_rechecks_all_inputs_provenance_and_owned_out(self):
        for target in ('originalTest', 'correctedDriverTest', 'derivedTest', 'expectedResult', 'provenancePath', 'out'):
            with self.subTest(target=target):
                self.setUp()
                case, call_args = self.diagnostic_fixture()
                harness = Mock()
                proof = self.diagnostic_row()['work']['diagnosticInput']
                def mutate(call, out):
                    path = Path(proof[target] if target == 'provenancePath' else proof[target]['path'])
                    path.write_bytes(path.read_bytes() + b' ')
                    return self.diagnostic_row()['work']
                harness.original_case.return_value = mutate
                execute = p.diagnostic_case(self.args, call_args, object(), case, harness)
                out = self.root / 'validation/cases/candidate' / p.DIAGNOSTIC_CASE
                out.mkdir(parents=True)
                with self.assertRaises((RuntimeError, json.JSONDecodeError)):
                    execute(object(), self.root if target == 'out' else out)

    def test_added_diagnostic_provenance_gate_rejects_arbitrary_pass_and_waivers(self):
        row = self.diagnostic_row()
        self.assertTrue(p.accepted_diagnostic_input(row, DRIVER))
        for key, value in (('status', 'failed'), ('cleanupPassed', False), ('role', 'baseline'), ('error', 'bad')):
            self.assertFalse(p.accepted_diagnostic_input(dict(row, **{key: value}), DRIVER))
        for key, value in (('passed', False), ('exitCode', False), ('exitCode', 1), ('invocations', 2),
                ('inputsUnchanged', False), ('diagnosticInputVerified', False), ('testSha256', p.DIAGNOSTIC_ORIGINAL_SHA),
                ('expectedSha256', 'a' * 64), ('diagnosticInput', {'accepted': True}), ('timedOut', True)):
            self.assertFalse(p.accepted_diagnostic_input(dict(row, work=dict(row['work'], **{key: value})), DRIVER))
        for key in row['work']['diagnosticInput']:
            changed = copy.deepcopy(row)
            changed['work']['diagnosticInput'].pop(key)
            self.assertFalse(p.accepted_diagnostic_input(changed, DRIVER), key)
        for key, value in (('actualCandidateBuildCommit', DRIVER), ('validationDriverCommit', p.CANDIDATE),
                ('source', str(self.source)), ('assertionCount', 60), ('substitutions', {'count': 1}),
                ('queryLiteral', {'tokens': 301}), ('expectedResultUnchanged', False),
                ('historicalFailure', {'nativeStatus': 'passed'})):
            changed = copy.deepcopy(row)
            changed['work']['diagnosticInput'][key] = value
            self.assertFalse(p.accepted_diagnostic_input(changed, DRIVER), key)
        rows = self.presentation_report()['caseVerdicts']
        self.assertFalse(p.case_evidence_complete(rows, validation_driver=DRIVER, require_diagnostic=True))
        rows[-1] = row
        self.assertTrue(p.case_evidence_complete(rows, validation_driver=DRIVER, require_diagnostic=True))

    def test_added_diagnostic_collector_only_selects_two_exact_owned_filenames(self):
        case, call_args = self.diagnostic_fixture()
        p.diagnostic_case(self.args, call_args, object(), case, Mock())
        (self.root / 'diagnostic-inputs/unrelated.test').write_bytes(b'DO NOT COLLECT')
        manifest = p.collect_continuation_evidence(self.root, self.root.parent / 'diagnostic-evidence')
        self.assertEqual({row['path'] for row in manifest['files']},
                         {p.DIAGNOSTIC_OWNED_TEST, p.DIAGNOSTIC_PROVENANCE})
        self.assertEqual(manifest['bytes'], sum((self.root / row['path']).stat().st_size for row in manifest['files']))

    def test_added_diagnostic_allowlist_requires_exact_corrected_hash_and_unchanged_result(self):
        corrected = (REPO / p.DIAGNOSTIC_TEST).read_bytes()
        with patch.object(p, 'git', side_effect=[p.DIAGNOSTIC_TEST.encode(), corrected, b'', b'']):
            self.assertIn(p.DIAGNOSTIC_TEST, p.verify_driver(self.source, DRIVER)['allowedDriverChanges'])
        for altered in (corrected + b'\n', corrected.replace(p.DIAGNOSTIC_LITERAL, p.DIAGNOSTIC_TOKEN, 1)):
            with patch.object(p, 'git', side_effect=[p.DIAGNOSTIC_TEST.encode(), altered]):
                with self.assertRaisesRegex(RuntimeError, 'exact two-literal'):
                    p.verify_driver(self.source, DRIVER)
        with patch.object(p, 'git', side_effect=[p.DIAGNOSTIC_TEST.encode(), corrected, p.DIAGNOSTIC_RESULT.encode()]):
            with self.assertRaisesRegex(RuntimeError, 'expected result changed'):
                p.verify_driver(self.source, DRIVER)
        for forbidden in (p.DIAGNOSTIC_RESULT, p.DIAGNOSTIC_TEST.replace('match_filter_only_contract', 'simple_query'),
                          p.DIAGNOSTIC_TEST.replace('match_filter_only_contract', 'tokenize_casefold_contract')):
            self.assertNotIn(forbidden, p.ALLOWED_DRIVER_PATHS)
            with patch.object(p, 'git', return_value=forbidden.encode()), self.assertRaises(RuntimeError):
                p.verify_driver(self.source, DRIVER)

    def test_continuation_strong_proof_never_accepts_boolean_only_or_malformed_fields(self):
        proof, row = self.compatibility_record()
        self.assertTrue(p.accepted_compatibility(proof, row))
        for key, bad in [('scheme', 'other'), ('approvedDifferenceCount', True),
                ('completeBytesCompared', False), ('eofCompared', False), ('nativePassed', True),
                ('formalContestPass', True), ('ownedCleanupPassed', False), ('rawNativeStatus', 'passed'),
                ('changes', proof['changes'][:-1]), ('adjacentScoredControl', {}), ('originalGolden', {}),
                ('actualReject', dict(proof['actualReject'], sha256='d' * 64)),
                ('nativeOutput', dict(proof['nativeOutput'], bytes=2 ** 20 + 1)),
                ('nativeWrapper', dict(proof['nativeWrapper'], path='relative'))]:
            with self.subTest(key=key):
                self.assertFalse(p.accepted_compatibility(dict(proof, **{key: bad}), row))
        self.assertFalse(p.accepted_compatibility({'accepted': True}, row))
        for key, bad in [('role', 'baseline'), ('name', p.CASES[0]), ('status', 'passed'),
                         ('cleanupPassed', False), ('timedOut', True), ('error', 'failure')]:
            with self.subTest(raw=key):
                self.assertFalse(p.accepted_compatibility(proof, dict(row, **{key: bad})))
        for key, bad in [('passed', True), ('exitCode', False), ('exitCode', 2),
                         ('invocations', 2), ('inputsUnchanged', False), ('failureKind', 'timeout')]:
            with self.subTest(work=key):
                self.assertFalse(p.accepted_compatibility(proof, dict(row, work=dict(row['work'], **{key: bad}))))

    def continuation_presentation(self):
        report = self.presentation_report()
        proof, raw = self.compatibility_record()
        report.update(mode=p.REUSE_MODE, validationDriverCommit=DRIVER, candidateBuildCommit=p.CANDIDATE,
                      supplementalCompatibility={'candidate/' + p.CASES[-1]: proof})
        report['builds']['candidate']['sourceCommit'] = p.CANDIDATE
        report['caseVerdicts'][-1] = self.diagnostic_row()
        report['caseVerdicts'] = [raw if row['role'] == 'candidate' and row['name'] == p.CASES[-1] else row
                                 for row in report['caseVerdicts']]
        return report

    def test_continuation_presentation_keeps_actual_source_and_native_failure_visible(self):
        report = self.continuation_presentation()
        before = copy.deepcopy(report)
        text = '\n'.join(p.presentation_lines(report))
        self.assertIn('classification: improvement_established', text)
        self.assertIn('B actual build source: ' + p.CANDIDATE, text)
        self.assertIn('driver source: ' + DRIVER, text)
        self.assertIn('originals raw: 4/5 native passes; plan-compatible: 1/1; extra regressions: 2/2', text)
        self.assertIn('61 new assertions', text)
        self.assertIn('raw native status: failed', text)
        self.assertIn('candidate/fts_index.simple_query status=failed cleanup=True', text)
        self.assertNotIn('Correctness candidate: 7/7', text)
        self.assertEqual(report, before)
        compact = p.small_report(report)
        for key in ('mode', 'validationDriverCommit', 'candidateBuildCommit', 'supplementalCompatibility'):
            self.assertEqual(compact[key], report[key])

    def test_continuation_presentation_and_cases_require_full_proof_not_accepted_flag(self):
        report = self.continuation_presentation()
        self.assertTrue(p.case_evidence_complete(report['caseVerdicts'], report['supplementalCompatibility']))
        for bad in ({}, {'candidate/' + p.CASES[-1]: {'accepted': True}},
                    {'candidate/' + p.CASES[0]: next(iter(report['supplementalCompatibility'].values()))}):
            text = '\n'.join(p.presentation_lines(dict(report, supplementalCompatibility=bad)))
            self.assertIn('classification: unavailable', text)
            self.assertIn('No improvement claim.', text)
        for key, value in (('candidateBuildCommit', DRIVER), ('validationDriverCommit', p.CANDIDATE)):
            self.assertIn('classification: unavailable', '\n'.join(p.presentation_lines(dict(report, **{key: value}))))
        report['builds']['candidate']['sourceCommit'] = DRIVER
        self.assertIn('classification: unavailable', '\n'.join(p.presentation_lines(report)))

    def test_reuse_driver_checks_actual_B_to_driver_diff_as_well_as_original_protection(self):
        with patch.object(p, 'source_identity'), patch.object(p, 'verify_driver', return_value={'kernelOrigin': p.KERNEL}), \
                patch.object(p, 'git', return_value=(p.PREFIX + 'fts_plan_compatibility.py\n').encode()) as git:
            proof = p.verify_reuse_driver(self.source, DRIVER)
        self.assertEqual(proof['actualCandidateBuildCommit'], p.CANDIDATE)
        self.assertEqual(git.call_args.args[3:5], (p.CANDIDATE, DRIVER))
        for path in ('src/sql/optimizer/a.cpp', 'build.sh', 'rust/a.rs', 'deps/a',
                     'tools/benchmark/fts_large_bench_score.py',
                     'tools/deploy/mysql_test/test_suite/fts_index/r/mysql/simple_query.result',
                     p.PREFIX + 'fts_contest_validation.py'):
            with self.subTest(path=path), patch.object(p, 'source_identity'), \
                    patch.object(p, 'verify_driver', return_value={}), patch.object(p, 'git', return_value=path.encode()):
                with self.assertRaisesRegex(RuntimeError, 'outside explicit diagnostic allowlist'):
                    p.verify_reuse_driver(self.source, DRIVER)

    def relocated_fixture(self, role='candidate'):
        commit = p.BASELINE if role == 'baseline' else p.CANDIDATE
        binary = self.root / 'runtime' / role / 'seekdb'
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b'\x7fELFsynthetic-' + commit.encode())
        identity = dict(role=role, sourceCommit=commit, sourceTree=p.SOURCE_TREES[role],
                        kernelOrigin=p.BASELINE if role == 'baseline' else p.KERNEL,
                        driverCommit=p.CANDIDATE, binary='/historical/runtime/' + role + '/seekdb',
                        bytes=binary.stat().st_size, sha256=p.digest(binary), buildId='abcd',
                        generatedVersionSha256='a' * 64)
        original = binary.parent / 'identity.original.json'
        original.write_bytes((json.dumps(identity, separators=(', ', ': ')) + '\n\n').encode())
        provenance = dict(identity, originalBinaryPath=identity['binary'], originalIdentitySha256=p.digest(original),
                          runtimeBinaryPath=str(binary), staticIdentityVerified=True, previousValidationAccepted=False,
                          actualValidationDriver='UNASSIGNED', buildDriverCommit=p.CANDIDATE,
                          artifactId=13621468966, runId=42486120779, runAttempt=1,
                          artifactZipSha256='23f8b2f8f6322edcd5dd0ed2b485c0d3271b3992d24417866de9eb90f77d8f75')
        provenance.pop('binary')
        provenance.pop('driverCommit')
        p.write_json(binary.parent / 'relocation.json', provenance)
        return identity, provenance, original

    def test_relocated_identity_preserves_original_JSON_bytes_and_actual_B_commit(self):
        for role in ('baseline', 'candidate'):
            identity, provenance, original = self.relocated_fixture(role)
            before = original.read_bytes()
            self.assertEqual(p.validate_relocated_identity(identity, role, self.root, provenance), original.parent / 'seekdb')
            self.assertEqual(original.read_bytes(), before)
            self.assertTrue(identity['binary'].startswith('/historical/'))
            self.assertEqual(identity['driverCommit'], p.CANDIDATE)

    def test_relocated_identity_rejects_relabel_or_metadata_byte_drift(self):
        identity, provenance, original = self.relocated_fixture()
        for key, value in [('sourceCommit', DRIVER), ('sourceTree', TREE), ('kernelOrigin', p.CANDIDATE),
                           ('buildDriverCommit', DRIVER), ('runtimeBinaryPath', '/wrong/seekdb'),
                           ('originalIdentitySha256', 'd' * 64), ('sha256', 'd' * 64), ('staticIdentityVerified', False)]:
            changed = dict(provenance, **{key: value})
            p.write_json(original.parent / 'relocation.json', changed)
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                p.validate_relocated_identity(identity, 'candidate', self.root, changed)
        p.write_json(original.parent / 'relocation.json', provenance)
        original.write_bytes(original.read_bytes() + b' ')
        with self.assertRaisesRegex(RuntimeError, 'integrity mismatch'):
            p.validate_relocated_identity(identity, 'candidate', self.root, provenance)

    def test_prepare_reuse_keeps_driver_checkout_and_creates_two_real_source_worktrees(self):
        self.root.rmdir()
        self.args.artifact_zip = self.source / 'original.zip'
        for name in p.DRIVER_FILES:
            path = self.source / p.PREFIX / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic ' + name)
        def git(source, *args):
            if args[0] == 'show':
                return (self.source / args[1].split(':', 1)[1]).read_bytes()
            return b''
        def identity(source, commit):
            role = 'baseline' if commit == p.BASELINE else 'candidate'
            return dict(commit=commit, tree=p.SOURCE_TREES[role] if source != self.source else TREE)
        loader = Mock()
        def verified(archive, root):
            self.assertEqual(archive, self.args.artifact_zip)
            self.assertFalse(root.exists())
            p.write_json(root / 'verification.json', {'synthetic': True})
            return {'artifact': {'artifactId': 13621468966}}
        loader.prepare_verified_runtime.side_effect = verified
        with patch.object(p, 'git', side_effect=git) as commands, patch.object(p, 'source_identity', side_effect=identity), \
                patch.object(p, 'verify_reuse_driver', return_value={}), patch.object(p, 'load_module', return_value=loader), \
                patch.object(p, 'switch_source') as switch:
            plan = p.prepare_reuse(self.args)
        switch.assert_not_called()
        self.assertEqual(plan['validationDriverCommit'], DRIVER)
        self.assertEqual(plan['candidateBuildCommit'], p.CANDIDATE)
        self.assertEqual(plan['candidateSource'], str(self.root / 'sources/candidate'))
        worktrees = [c.args[1:] for c in commands.call_args_list if c.args[1] == 'worktree']
        self.assertEqual(worktrees, [('worktree', 'add', '--detach', str(self.root / 'sources' / role), commit)
                                     for role, commit in (('baseline', p.BASELINE), ('candidate', p.CANDIDATE))])
        self.assertEqual(p.load_plan(self.args), plan)
        (self.root / 'runtime/verification.json').write_text('changed')
        with self.assertRaisesRegex(RuntimeError, 'verification plan mismatch'):
            p.load_plan(self.args)

    def fake_reuse_validation(self, accepted=True, cleanup=True):
        import fts_plan_compatibility as c
        plan, report, calls = self.fake_validation()
        self.diagnostic_fixture()
        plan['candidateSource'] = str(self.root / 'sources/candidate')
        p.write_json(self.root / 'runtime/verification.json', {'synthetic': True})
        plan.update(mode=p.REUSE_MODE, candidateBuildCommit=p.CANDIDATE,
                    artifact={'artifactId': 13621468966},
                    runtimeVerificationSha256=p.digest(self.root / 'runtime/verification.json'))
        for role in ('baseline', 'candidate'):
            identity = json.loads((self.root / 'runtime' / role / 'identity.json').read_text())
            identity['sourceCommit'] = p.BASELINE if role == 'baseline' else p.CANDIDATE
            p.write_json(self.root / 'runtime' / role / 'identity.original.json', identity)
            p.write_json(self.root / 'runtime' / role / 'relocation.json', {'synthetic': True})
        verification = dict(status='verified_runtime_prepared', runtimeRoot=str(self.root / 'runtime'),
            verificationPath=str(self.root / 'runtime/verification.json'), oldValidationAccepted=False,
            currentValidationPerformed=False, artifact=plan['artifact'], roles={})
        for role in ('baseline', 'candidate'):
            directory = self.root / 'runtime' / role
            verification['roles'][role] = dict(binary=str(directory / 'seekdb'),
                identityOriginalPath=str(directory / 'identity.original.json'),
                relocationPath=str(directory / 'relocation.json'),
                identity=json.loads((directory / 'identity.original.json').read_text()), provenance={'synthetic': True})
        p.write_json(self.root / 'runtime/verification.json', verification)
        plan['runtimeVerificationSha256'] = p.digest(self.root / 'runtime/verification.json')
        proof, raw = self.compatibility_record()
        if not accepted:
            proof = {'accepted': True}
        original = self._fake_harness.run_instance.side_effect
        def run(*args, **kwargs):
            row = original(*args, **kwargs)
            if args[0].role == 'candidate' and args[2] == p.DIAGNOSTIC_CASE:
                row['work'] = self.diagnostic_row()['work']
            if args[0].role == 'candidate' and args[2] == p.CASES[-1]:
                row = copy.deepcopy(raw)
                row.pop('role')
                row.pop('validationContext')
                row['cleanupPassed'] = cleanup
                row['parameters'] = ('memory_budget=4G', 'vector_memory_limit=1G')
                out = args[0].out / args[2]
                p.write_json(out / 'result.json', row)
                (out / 'original-run-case.log').write_bytes(b'UNCHANGED NATIVE FAIL\n')
            return row
        self._fake_harness.run_instance.side_effect = run
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(p, 'verify_reuse_driver', return_value={}))
        stack.enter_context(patch.object(p, 'validate_relocated_identity', side_effect=lambda identity, role, root, provenance: root / 'runtime' / role / 'seekdb'))
        def classify(**kwargs):
            current = copy.deepcopy(proof)
            for key, path in dict(originalGolden=kwargs['source'] / c.GOLDEN,
                    originalTest=kwargs['source'] / c.TEST,
                    actualReject=kwargs['out'] / 'mysqltest-log/simple_query.reject',
                    nativeOutput=kwargs['out'] / 'mysqltest-output.log',
                    nativeWrapper=kwargs['out'] / 'original-run-case.log').items():
                if key in current:
                    current[key]['path'] = str(path)
            return current
        classifier = stack.enter_context(patch.object(c, 'classify_plan_metadata_compatibility', side_effect=classify))
        return plan, report, calls, classifier

    def test_continuation_bypasses_only_verified_failure_after_cleanup_preserves_raw_and_schedule(self):
        plan, report, calls, classifier = self.fake_reuse_validation()
        p.validate(self.args, plan, report)
        self.assertEqual(len(calls), 20)
        self.assertEqual(calls[12:], [(name, role, False) for name, role in p.SCHEDULE])
        raw_path = self.root / 'validation/cases/candidate' / p.CASES[-1] / 'result.json'
        raw_bytes = raw_path.read_bytes()
        raw = json.loads(raw_bytes)
        self.assertEqual(raw['status'], 'failed')
        self.assertIs(raw['work']['passed'], False)
        self.assertEqual(raw['work']['exitCode'], 1)
        self.assertEqual(json.loads(json.dumps(classifier.call_args.kwargs['row'])), raw)
        self.assertEqual(classifier.call_args.kwargs['row']['cleanupPassed'], True)
        self.assertEqual((raw_path.parent / 'original-run-case.log').read_bytes(), b'UNCHANGED NATIVE FAIL\n')
        self.assertEqual(report['caseVerdicts'][9]['status'], 'failed')
        self.assertEqual(report['instances'][9]['status'], 'failed')
        self.assertTrue((raw_path.parent / 'supplemental-plan-compatibility.json').is_file())
        self.assertEqual(report['builds']['candidate']['sourceCommit'], p.CANDIDATE)
        self.assertEqual(report['validationDriverCommit'], DRIVER)
        self.assertTrue(p.case_evidence_complete(report['caseVerdicts'], report['supplementalCompatibility']))

    def test_continuation_fails_closed_before_regressions_or_timing_on_weak_proof_or_bad_cleanup(self):
        for accepted, cleanup in ((False, True), (True, False)):
            with self.subTest(accepted=accepted, cleanup=cleanup):
                plan, report, calls, classifier = self.fake_reuse_validation(accepted, cleanup)
                with self.assertRaisesRegex(RuntimeError, 'Owned instance failed'):
                    p.validate(self.args, plan, report)
                self.assertEqual(len(calls), 10)
                self.assertEqual(report['roundVerdicts'], [])
                self.assertEqual(report['caseVerdicts'][-1]['status'], 'failed')

    def test_continuation_evidence_preserves_raw_files_excludes_ELFs_DB_env_and_logtails(self):
        evidence = self.root / 'validation/cases/candidate' / p.CASES[-1]
        p.write_json(evidence / 'result.json', {'status': 'failed'})
        (evidence / 'mysqltest-log').mkdir()
        (evidence / 'mysqltest-log/simple_query.reject').write_bytes(b'raw reject\x00\n')
        (evidence / 'mysqltest-log/timer').write_bytes(b'123\n')
        (evidence / 'mysqltest-log/simple_query.log').write_bytes(b'raw native log\n')
        for relative in ('runtime/candidate/seekdb', 'validation/cases/candidate/' + p.CASES[-1] + '/database/private',
                         'validation/cases/candidate/' + p.CASES[-1] + '/db-log-tails/seekdb.log', 'environment.txt', 'core'):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'NEVER UPLOAD')
        out = self.root.parent / 'new-evidence'
        manifest = p.collect_continuation_evidence(self.root, out)
        self.assertEqual((out / 'validation/cases/candidate' / p.CASES[-1] / 'mysqltest-log/simple_query.reject').read_bytes(), b'raw reject\x00\n')
        self.assertFalse(manifest['unchangedElfsReuploaded'])
        self.assertFalse(manifest['historicalCollectorPassed'])
        paths = [entry['path'] for entry in manifest['files']]
        self.assertEqual(len(paths), 4)
        self.assertIn('validation/cases/candidate/' + p.CASES[-1] + '/mysqltest-log/timer', paths)
        self.assertNotIn('runtime/candidate/seekdb', paths)
        self.assertTrue((out / 'continuation-evidence-manifest.json').is_file())

    def test_continuation_evidence_rejects_oversize_or_linked_or_nested_outputs(self):
        report = self.root / 'report.json'
        report.write_bytes(b'x' * (8 * 1024 ** 2 + 1))
        with self.assertRaisesRegex(RuntimeError, 'size violation'):
            p.collect_continuation_evidence(self.root, self.root.parent / 'oversize')
        report.write_bytes(b'{}')
        os.link(report, self.root / 'alias')
        with self.assertRaisesRegex(RuntimeError, 'type/link/size'):
            p.collect_continuation_evidence(self.root, self.root.parent / 'linked')
        with self.assertRaisesRegex(RuntimeError, 'fresh and disjoint'):
            p.collect_continuation_evidence(self.root, self.root / 'nested')

    def test_continuation_workflow_is_single_pinned_readonly_download_no_compile_no_ELF_upload(self):
        text = (REPO / '.github/workflows/fts-contest-validation.yml').read_text()
        self.assertEqual(text.count('    runs-on:'), 1)
        self.assertIn('actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333', text)
        for snippet in ("artifact-ids: '13621468966'", "run-id: '42486120779'", 'repository: beifeng-h/nailong-seekdb',
                        'github-token: ${{ github.token }}', 'skip-decompress: true', 'digest-mismatch: error',
                        'actions: read', '--phase prepare-reuse', '--phase collect-evidence', './build.sh release --init',
                        'test ! -e "$RUNNER_TEMP/seekdb-original-artifact"'):
            self.assertIn(snippet, text)
        for forbidden in ('--build ', '--target ', '--make ', 'fts_paired_collect.py', 'secrets.', 'printenv',
                          'actions/download-artifact@v4', '13608812363', 'persist-credentials', 'set -x', 'actions: write'):
            self.assertNotIn(forbidden, text)
        self.assertLess(text.index('admit download'), text.index('actions/download-artifact@'))
        self.assertIn("-mindepth 1 -maxdepth 1 -print0", text)
        self.assertIn('test -f "${archives[0]}" && test ! -L "${archives[0]}"', text)
        self.assertNotIn("-name '*.zip'", text)
        self.assertIn('https://mirrors.oceanbase.com/', text)
        self.assertIn('exec /usr/bin/wget --https-only --timeout=30 --tries=1', text)


    def test_continuation_proof_rejects_stale_driver_paths_or_raw_result_binding(self):
        proof, row = self.compatibility_record()
        for key, bad in (('sourceCommit', DRIVER), ('validationDriverCommit', p.CANDIDATE),
                         ('source', '/stale/source'), ('out', '/stale/out')):
            changed = copy.deepcopy(row)
            changed['validationContext'][key] = bad
            with self.subTest(key=key):
                self.assertFalse(p.accepted_compatibility(proof, changed, DRIVER))
        changed = copy.deepcopy(proof)
        changed['nativeOutput']['path'] = '/stale/out/mysqltest-output.log'
        self.assertFalse(p.accepted_compatibility(changed, row, DRIVER))
        changed = copy.deepcopy(proof)
        changed['validationContext']['nativeResult']['sha256'] = 'f' * 64
        self.assertFalse(p.accepted_compatibility(changed, row, DRIVER))
        self.assertFalse(p.accepted_compatibility(proof, row, p.CANDIDATE))

    def test_continuation_rejects_initial_identity_drift_from_verified_runtime_record(self):
        plan, report, calls, _ = self.fake_reuse_validation()
        path = self.root / 'runtime/candidate/identity.original.json'
        identity = json.loads(path.read_text())
        identity['sha256'] = 'd' * 64
        p.write_json(path, identity)
        with self.assertRaisesRegex(RuntimeError, 'differs from verified runtime record'):
            p.validate(self.args, plan, report)
        self.assertEqual(calls, [])

    def test_continuation_evidence_keeps_exact_timer_but_rejects_unrelated_native_filename(self):
        logs = self.root / 'validation/cases/candidate' / p.CASES[-1] / 'mysqltest-log'
        logs.mkdir(parents=True)
        (logs / 'timer').write_text('123\n')
        (logs / 'environment.txt').write_text('unexpected')
        with self.assertRaisesRegex(RuntimeError, 'Unexpected native evidence filename'):
            p.collect_continuation_evidence(self.root, self.root.parent / 'unexpected-native')


CHECKOUT_COMMITS = (
    'a' * 40,
    '5bce9e06d1281347ad61344e4b01fab9158c58a2',
    'bc4dde62eee39acb630ffe54741aecebe4ab0a2a',
    'f1f4d0d4b225ec528dc3802dbfd93d9d4870aa65',
)


class CheckoutContracts(unittest.TestCase):
    def run_checkout(self, **overrides):
        workflow = (REPO / '.github/workflows/fts-contest-validation.yml').read_text(encoding='utf-8')
        first_step = workflow.split('    steps:\n', 1)[1].split('      - name:', 2)[1]
        script = textwrap.dedent(first_step.split('        run: |\n', 1)[1])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stub = root / 'git'
            stub.write_text(textwrap.dedent('''\
                #!/usr/bin/env python3
                import json, os, sys
                args = sys.argv[1:]
                with open(os.environ['GIT_TRACE_FILE'], 'a', encoding='utf-8') as out:
                    out.write(json.dumps(args) + '\\n')
                while args and args[0] in ('-c', '-C'):
                    args = args[2:]
                command = args[0]
                if command == 'fetch':
                    sys.exit(int(os.environ.get('FETCH_EXIT', '0')))
                if command == 'cat-file' and args[-1] == os.environ.get('MISSING_COMMIT', '') + '^{commit}':
                    sys.exit(72)
                if command == 'rev-parse':
                    print(os.environ.get('CHECKOUT_HEAD', os.environ['GITHUB_SHA']))
                if command == 'status':
                    print(os.environ.get('CHECKOUT_STATUS', ''), end='')
            '''), encoding='utf-8')
            stub.chmod(0o755)
            trace = root / 'git-calls.jsonl'
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                       GIT_TRACE_FILE=str(trace), GITHUB_WORKSPACE=str(root / 'workspace'),
                       RUNNER_TEMP=str(root), GITHUB_ENV=str(root / 'github-env'),
                       GITHUB_SERVER_URL='https://github.com',
                       GITHUB_REPOSITORY='beifeng-h/nailong-seekdb', GITHUB_SHA=CHECKOUT_COMMITS[0])
            env.update(overrides)
            result = subprocess.run(['bash', '--noprofile', '--norc', '-c', script],
                                    env=env, cwd=root, text=True, capture_output=True, timeout=10)
            calls = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()]
            return result, calls, env['GITHUB_WORKSPACE']

    def fetch_call(self, workspace):
        return ['-c', 'maintenance.auto=false', '-c', 'gc.auto=0', '-C', workspace,
                'fetch', '--atomic', '--no-auto-maintenance', '--no-tags', '--depth=1',
                'origin', *CHECKOUT_COMMITS]

    def test_single_atomic_fetch_precedes_all_commit_checks_and_driver_checkout(self):
        result, calls, workspace = self.run_checkout()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [
            ['init', workspace],
            ['-C', workspace, 'remote', 'add', 'origin', 'https://github.com/beifeng-h/nailong-seekdb.git'],
            self.fetch_call(workspace),
            *[['-C', workspace, 'cat-file', '-e', sha + '^{commit}'] for sha in CHECKOUT_COMMITS],
            ['-C', workspace, 'checkout', '--detach', CHECKOUT_COMMITS[0]],
            ['rev-parse', 'HEAD'],
            ['status', '--porcelain=v1', '--untracked-files=all'],
        ])

    def test_fetch_failure_stops_without_retry_verification_or_checkout(self):
        result, calls, workspace = self.run_checkout(FETCH_EXIT='71')
        self.assertEqual(result.returncode, 71)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1], self.fetch_call(workspace))

    def test_each_missing_commit_stops_before_checkout(self):
        for index, sha in enumerate(CHECKOUT_COMMITS):
            with self.subTest(commit=sha):
                result, calls, workspace = self.run_checkout(MISSING_COMMIT=sha)
                self.assertEqual(result.returncode, 72)
                self.assertEqual(calls[2], self.fetch_call(workspace))
                self.assertEqual(calls[3:], [
                    ['-C', workspace, 'cat-file', '-e', value + '^{commit}']
                    for value in CHECKOUT_COMMITS[:index + 1]
                ])

    def test_driver_head_mismatch_fails_before_cleanliness_check(self):
        result, calls, _ = self.run_checkout(CHECKOUT_HEAD='b' * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls[-1], ['rev-parse', 'HEAD'])

    def test_dirty_driver_checkout_fails(self):
        result, calls, _ = self.run_checkout(CHECKOUT_STATUS='?? unexpected\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls[-1], ['status', '--porcelain=v1', '--untracked-files=all'])


if __name__ == '__main__':
    unittest.main()
