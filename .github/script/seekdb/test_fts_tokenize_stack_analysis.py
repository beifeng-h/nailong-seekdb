import unittest

import fts_tokenize_stack_analysis as analysis


class StackAnalysisTests(unittest.TestCase):
    def test_parser_keeps_thread_time_and_leaf_first_callers(self):
        text = '''  42/101 151.123456789:
            abc oceanbase::common::TCRWLock::wrlock(long) (seekdb)
            def oceanbase::share::SomeCaller::run() (seekdb)

          42/101 151.223456789:
            aaa oceanbase::storage::ObArrayHashMap::find(int) (seekdb)
            bbb oceanbase::storage::ObIKProcessor::process() (seekdb)
        '''
        samples = analysis.parse_stacks(text)
        self.assertEqual([(x['pid'], x['tid'], x['stamp']) for x in samples],
                         [(42, 101, 151123456789), (42, 101, 151223456789)])
        summary = analysis.summarize_stacks(samples)
        self.assertEqual(summary['twoFrameFraction'], 1.0)
        self.assertEqual(summary['targets']['tcrw_write']['leafSamples'], 1)
        self.assertEqual(summary['targets']['array_hash_find']['leafSamples'], 1)
        self.assertIn('SomeCaller', summary['targets']['tcrw_write']['leafCallers'][0]['symbol'])

    def test_inclusive_counts_do_not_add_overlapping_targets(self):
        text = '''  42/101 151.123456789:
            abc oceanbase::common::ObLatch::wrlock() (seekdb)
            def oceanbase::storage::ObArrayHashMap::find() (seekdb)
        '''
        summary = analysis.summarize_stacks(analysis.parse_stacks(text))
        self.assertEqual(summary['sampleCount'], 1)
        self.assertEqual(summary['targets']['latch_write']['inclusiveSamples'], 1)
        self.assertEqual(summary['targets']['array_hash_find']['inclusiveSamples'], 1)

    def test_unparsed_lines_fail_and_frameless_samples_remain_visible(self):
        with self.assertRaisesRegex(RuntimeError, 'orphan'):
            analysis.parse_stacks('      abc orphan (seekdb)\n')
        sample = analysis.summarize_stacks(analysis.parse_stacks('  42/101 151.123456789:\n'))
        self.assertEqual(sample['sampleCount'], 1)
        self.assertEqual(sample['samplesWithAnyFrame'], 0)
        self.assertEqual(sample['twoFrameFraction'], 0)


if __name__ == '__main__':
    unittest.main()
