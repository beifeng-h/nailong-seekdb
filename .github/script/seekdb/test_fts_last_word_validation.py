"""Focused integration contracts for the fixed-source validation entry point.

Only inert local fixtures and Bash parsing. No download, product compilation,
old differential matrix, database, or benchmark runs are started here.
"""
import copy
import inspect
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import fts_last_word_validation as fixed
import fts_source_hybrid_validation as historical
import fts_b3_reuse_validation as archived_b3
from test_fts_b3_live_workflow import workflow_projection

REPO = Path(__file__).resolve().parents[3]
WORKFLOW = REPO / '.github/workflows/fts-contest-validation.yml'
D = 'a' * 40


class FixedSourceContracts(unittest.TestCase):
    def test_new_identity_never_relabels_the_archived_B3_modules(self):
        self.assertIsNot(fixed.hybrid, historical)
        self.assertEqual(historical.PRODUCTION_KERNEL, 'c235713689325d2bb8882b86f8f89ee99ff9b43a')
        self.assertEqual(archived_b3.PRODUCTION_KERNEL, historical.PRODUCTION_KERNEL)
        self.assertEqual(archived_b3.B3, 'b3eed52688fc1b178632a9dbb38aabb32f805c5d')
        self.assertNotEqual(fixed.KERNEL, historical.PRODUCTION_KERNEL)
        self.assertNotEqual(fixed.hybrid.MODE, historical.MODE)
        self.assertEqual(fixed.hybrid.PRODUCTION_KERNEL, fixed.KERNEL)
        self.assertEqual(fixed.hybrid.PRODUCTION_KERNEL_TREE, fixed.KERNEL_TREE)

    def test_fixed_native_contract_and_all_75_original_inputs_are_pinned(self):
        inputs = fixed.hybrid.case_input_hashes(REPO)
        self.assertEqual(inputs[fixed.CASE], dict(testSha256=fixed.CASE_TEST_SHA256,
            expectedSha256=fixed.CASE_RESULT_SHA256))
        self.assertEqual(len(fixed.hybrid.inventory(REPO)), 75)
        self.assertEqual(fixed.hybrid.inventory(REPO), historical.inventory(REPO))
        self.assertEqual(fixed.hybrid.selected_cases('baseline'), historical.selected_cases('baseline'))
        self.assertEqual(fixed.hybrid.selected_cases('candidate'),
            historical.selected_cases('candidate') + (fixed.CASE,))

    def test_new_native_contract_cannot_be_omitted_or_changed(self):
        actual = fixed.hybrid.case_input_hashes(REPO)
        for changed in ({key: value for key, value in actual.items() if key != fixed.CASE},
                dict(actual, **{fixed.CASE: dict(testSha256='0'*64,
                    expectedSha256=fixed.CASE_RESULT_SHA256)}),
                dict(actual, **{fixed.CASE: dict(testSha256=fixed.CASE_TEST_SHA256,
                    expectedSha256='0'*64)})):
            with patch.object(fixed, '_original_case_input_hashes', return_value=changed):
                with self.assertRaises((KeyError, RuntimeError)):
                    fixed.case_input_hashes(REPO)

    def test_old_source_or_elf_commit_is_not_a_new_driver(self):
        for driver in fixed.hybrid.OLD_KERNELS:
            with patch.object(fixed.hybrid, 'source_identity') as source:
                with self.assertRaises(RuntimeError):
                    fixed.hybrid.verify_driver(REPO, driver, fixed.KERNEL)
                source.assert_not_called()
        with self.assertRaises(RuntimeError):
            fixed.hybrid.verify_driver(REPO, D, historical.PRODUCTION_KERNEL)

    def test_new_driver_still_rejects_production_or_original_build_changes(self):
        for sequence in ((fixed.KERNEL_TREE.encode(), b'src/storage/fts/ob_fts_stop_word.cpp'),
                         (fixed.KERNEL_TREE.encode(), b'', b'build.sh')):
            with patch.object(fixed.hybrid, 'source_identity', return_value={'tree':'b'*40}), \
                    patch.object(fixed.hybrid, 'git', side_effect=sequence):
                with self.assertRaises(RuntimeError):
                    fixed.hybrid.verify_driver(REPO, D, fixed.KERNEL)

    def test_sql_semantic_comparison_and_all_final_gates_are_unchanged(self):
        self.assertEqual(fixed.hybrid.tokenize_sql(), historical.tokenize_sql())
        self.assertEqual(fixed.hybrid.tokenize_preflight(), historical.tokenize_preflight())
        for name in ('canonical_tokenize', 'tokenize_complete', 'validate', 'completed',
                     'historical_baseline', 'candidate_identity', 'accepted_compatibility'):
            self.assertEqual(inspect.getsource(getattr(fixed.hybrid,name)),
                inspect.getsource(getattr(historical,name)))
        self.assertEqual(fixed.hybrid.CONFIG, historical.CONFIG)
        self.assertEqual(fixed.hybrid.SCHEDULE, historical.SCHEDULE)
        self.assertEqual(fixed.hybrid.PAIRS, historical.PAIRS)
        self.assertEqual(fixed.hybrid.MARGIN, historical.MARGIN)

    def test_representative_difference_is_not_normalized_away(self):
        def corpus(last):
            rows=[]
            for parser in ('ik','beng'):
                for name,_ in historical.TOKENIZE_CORPUS:
                    value={'tokens':[], 'doc_len':0}
                    if parser == 'beng' and name == 'multibyte':
                        value={'tokens':[{last:2}], 'doc_len':2}
                    rows.append(parser+'/'+name+'\t'+json.dumps(value,ensure_ascii=False)+'\n')
            return ''.join(rows).encode()
        first=fixed.hybrid.canonical_tokenize(corpus('\U0001f642'))
        last=fixed.hybrid.canonical_tokenize(corpus('\U00020000'))
        self.assertNotEqual(first,last)
        self.assertEqual(first['beng/multibyte']['doc_len'],last['beng/multibyte']['doc_len'])


class FixedWorkflowContracts(unittest.TestCase):
    def workflow(self):
        return workflow_projection(WORKFLOW.read_text())

    def test_exact_target_read_only_permissions_and_one_original_A_download(self):
        workflow=self.workflow()
        self.assertEqual(workflow['on'],{'push':{'branches':['perf/fts-official-contract-20261108']}})
        self.assertEqual(workflow['permissions'],{'contents':'read'})
        job=workflow['jobs']['paired']
        self.assertEqual(job['permissions'],{'contents':'read','actions':'read'})
        self.assertEqual(job['runs-on'],'ubuntu-24.04')
        self.assertEqual(job['env']['HYBRID_KERNEL_SHA'],fixed.KERNEL)
        downloads=[step for step in job['steps'] if step.get('uses','').startswith('actions/download-artifact@')]
        self.assertEqual(len(downloads),1)
        self.assertEqual(downloads[0]['with']['artifact-ids'],'13621468966')
        self.assertEqual(downloads[0]['with']['run-id'],'42486120779')
        self.assertEqual(downloads[0]['with']['repository'],'beifeng-h/nailong-seekdb')
        self.assertEqual(downloads[0]['with']['digest-mismatch'],'error')
        self.assertIs(downloads[0]['with']['skip-decompress'],True)
        self.assertEqual(downloads[0]['uses'].split(' #',1)[0],'actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333')

    def test_new_B_is_compiled_once_and_preserved_before_strict_validation(self):
        text=WORKFLOW.read_text()
        runs='\n'.join(step.get('run','') for step in self.workflow()['jobs']['paired']['steps'])
        self.assertEqual(runs.count('--build build_release --target seekdb -j2'),1)
        self.assertEqual(runs.count('./build.sh release --init'),1)
        self.assertNotIn('--role baseline',runs)
        self.assertNotIn('13652898100',text)
        self.assertNotIn('fts_b3_reuse_validation.py',text)
        self.assertNotIn('fts_source_hybrid_validation.py',text)
        for phase in ('prepare-reuse','prebuild','snapshot','preserve-candidate','validate','finalize','collect-evidence','present'):
            self.assertIn('--phase '+phase,runs)
        order=[runs.index(token) for token in ('--phase prepare-reuse','./build.sh release --init',
            '--phase prebuild','--build build_release --target seekdb -j2',
            '--phase snapshot --role candidate','--phase preserve-candidate','--phase validate')]
        self.assertEqual(order,sorted(order))
        self.assertIn('fts_last_word_validation.py',runs)
        self.assertIn("-p 'test_fts_last_word_validation.py'",runs)
        self.assertNotIn("-p 'test_fts*.py'",runs)
        for forbidden in ('actions: write','secrets.','printenv','set -x','workflow_dispatch','pull_request'):
            self.assertNotIn(forbidden,text)

    def test_each_new_workflow_bash_step_parses_without_running(self):
        for step in self.workflow()['jobs']['paired']['steps']:
            if 'run' in step:
                parsed=subprocess.run(['bash','-n'],input=step['run'],text=True,capture_output=True)
                self.assertEqual(parsed.returncode,0,(step['name'],parsed.stderr))

    def test_old_B3_active_workflow_is_archived_byte_for_byte(self):
        archived=REPO/'.github/script/seekdb/fixtures/fts-exact-b3-c146-validation.yml'
        import hashlib
        self.assertEqual(hashlib.sha256(archived.read_bytes()).hexdigest(),'8d76b48df2c1137da4439d2de4adc08ca7d8c94cc9e3be8ebd2cdb02f0002cb0')
        text=(REPO/'.github/script/seekdb/test_fts_b3_live_workflow.py').read_text()
        self.assertIn('.github/script/seekdb/fixtures/fts-exact-b3-c146-validation.yml',text)


if __name__ == '__main__':
    unittest.main()
