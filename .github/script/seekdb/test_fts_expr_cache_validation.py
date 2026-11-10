"""Local contract tests, not native SQL/build/performance results."""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile
import json
import unittest
from unittest.mock import patch
import fts_expr_cache_validation as driver
from fts_expr_cache_targeted import PureTests


class ValidationContractTests(unittest.TestCase):
    def test_exact_source_contract(self):
        source=Path(__file__).resolve().parents[3]
        head=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD']).decode().strip()
        proof=driver.source_proof(argparse.Namespace(source=source,driver_sha=head))
        self.assertEqual(proof['originalInputCount'],75)
        self.assertEqual(proof['patchSha256'],driver.PATCH_SHA)
        self.assertEqual(set(proof['productChanges']),driver.PRODUCT)
        self.assertEqual(proof['baselineCommit'],'b14755bed10d1399cf7d1dca6444cfa757727b21')
        self.assertEqual(len(proof['caseInputs']),9)

    def test_abba_only(self):
        self.assertEqual(driver.SCHEDULE,(('A1','baseline'),('B1','candidate'),('B2','candidate'),('A2','baseline')))
        self.assertEqual([x[0] for x in driver.DDL],['build_ik_all','build_ik_content','build_beng_en'])
        self.assertEqual(driver.DATA_SHA,'bc385b3c9586d40d667093947be20a8714ed65301eae68487af8fac22956d235')

    def test_query_shape_pins(self):
        source=Path(__file__).resolve().parents[3]
        q=driver.common.queries_from_benchmark(source/'tools/benchmark/fts_large_bench.sh')
        self.assertEqual(tuple(q),driver.common.QUERY_NAMES)
        self.assertEqual(len(driver.common.plan_queries(q)),7)
        self.assertEqual(tuple(name=='query_limit' or name in driver.common.PLAN_CONTROLS
            for name in driver.common.plan_queries(q)),(False,False,False,True,True,True,True))

    def test_wrong_commit_identity_rejected(self):
        wrong=b'author other <other@example.com> 0 +0000\ncommitter other <other@example.com> 0 +0000\n\nx\n'
        with patch.object(driver.common,'git',return_value=wrong):
            with self.assertRaisesRegex(RuntimeError,'Unverified user Git'):
                driver.git_identity(Path('.'),'a'*40)

    def test_no_historical_timing_or_scorer(self):
        text=Path(driver.__file__).read_text()
        self.assertNotIn('common.benchmark_work(',text)
        self.assertNotIn("globals()['BASELINE']",text)
        self.assertNotIn('--allow-config-mismatch',text)
        self.assertNotIn('3000',text)
        self.assertNotIn('query_rounds',text)
        self.assertLess(driver.BINARY_ARCHIVE_CAP,512*1024**2)
        self.assertLess(driver.LOG_ARCHIVE_CAP,512*1024**2)

    def test_personal_trigger_optout_only(self):
        source=Path(__file__).resolve().parents[3]
        old_text=(source/'.github/workflows/fts-contest-validation.yml').read_text()
        new_text=(source/'.github/workflows/fts-expr-cache-validation.yml').read_text()
        old_branches=re.search(r'^    branches: \[([^]]+)\]',old_text,re.M).group(1).split(',')
        new_branches=re.search(r'^    branches: \[([^]]+)\]',new_text,re.M).group(1).split(',')
        registered='perf/fts-official-contract-20261108'
        dedicated='perf/fts-expr-cache-validation-20261110'
        self.assertEqual(old_branches,[registered])
        self.assertEqual(new_branches,[dedicated])
        condition=next(line for line in old_text.splitlines() if line.lstrip().startswith('if:'))
        marker='[dedicated-native-validation]'
        self.assertIn("!contains(github.event.head_commit.message, '"+marker+"')",condition)
        self.assertNotIn(marker,new_text)
        def old_runs(branch,message):
            return branch in old_branches and marker not in message
        def new_runs(branch,message):
            return branch in new_branches
        self.assertTrue(old_runs(registered,'ordinary existing commit'))
        self.assertFalse(old_runs(dedicated,marker))
        self.assertTrue(new_runs(dedicated,marker))
        self.assertFalse(old_runs(registered,marker))
        self.assertFalse(new_runs(registered,marker))
        previous=subprocess.check_output(['git','-C',str(source),'show',driver.BASE+':.github/workflows/fts-contest-validation.yml']).decode()
        self.assertEqual(old_text,previous.replace(
            "if: ${{ github.repository == 'beifeng-h/nailong-seekdb' && github.event.repository.private == false }}",
            "if: ${{ github.repository == 'beifeng-h/nailong-seekdb' && github.event.repository.private == false && !contains(github.event.head_commit.message, '[dedicated-native-validation]') }}"))

    def test_bounded_collector_preserves_partial_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'root'; root.mkdir()
            (root/'evidence').mkdir()
            (root/'report.json').write_text('{"status":"failed_or_blocked"}\n')
            (root/'plan.json').write_text('{}\n')
            for name in ('one.log','two.log','three.log'):
                (root/'evidence'/name).write_text('x'*80)
            out=Path(tmp)/'out'
            with patch.object(driver,'LOG_PAYLOAD_CAP',140):
                driver.collect(argparse.Namespace(root=root,out=out))
            manifest=json.loads((out/'evidence-manifest.json').read_text())
            self.assertFalse(manifest['allSelectedEvidenceSaved'])
            self.assertEqual((out/'report.json').read_text(),(root/'report.json').read_text())
            self.assertTrue((out/'evidence.tar.gz').is_file())
            self.assertTrue(any(r.get('omitted') for r in manifest['files']))

    def test_identical_product_after_kernel(self):
        source=Path(__file__).resolve().parents[3]
        diff=subprocess.check_output(['git','-C',str(source),'diff','--name-only',driver.KERNEL,'HEAD','--','src'])
        self.assertFalse(diff)


if __name__=='__main__':
    unittest.main()
