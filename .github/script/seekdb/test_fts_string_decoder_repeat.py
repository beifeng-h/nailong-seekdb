"""Check that repeated measurements use the intended preserved executables."""
from pathlib import Path
import unittest

import fts_string_decoder_repeat as experiment


ROOT = Path(__file__).resolve().parents[3]


class DecoderRepeatTests(unittest.TestCase):
    def test_configuration_binds_frozen_sources_and_decoder_checks(self):
        experiment.configure()
        repeat = experiment.repeat
        decoder = experiment.decoder
        self.assertEqual(repeat.BASE_SOURCE, decoder.BASE)
        self.assertEqual(repeat.driver.BASE, experiment.BASE)
        self.assertEqual(repeat.CANDIDATE, experiment.CANDIDATE)
        self.assertEqual(repeat.CANDIDATE_TREE, experiment.CANDIDATE_TREE)
        self.assertEqual(repeat.PRODUCTS, {decoder.PRODUCT})
        self.assertIs(repeat.row_cache.source_proof, decoder.source_proof)
        self.assertIs(repeat.driver.source_proof, decoder.source_proof)
        self.assertIs(repeat.row_cache.validation.benchmark_callback, decoder.benchmark_callback)
        self.assertEqual(repeat.archived.PINS, experiment.PINS)
        self.assertIs(repeat.summarize, experiment.summarize)
        self.assertEqual(len(repeat.SCHEDULE), 8)
        self.assertEqual(len(repeat.PAIRS), 4)

    def test_rejects_missing_or_different_full_match_outputs(self):
        outputs = {key: {'rows': 1, 'sha256': 'a'} for key in
                   ('query_cn', 'query_beng', 'query_mixed', 'query_limit')}
        with self.assertRaises(Exception):
            experiment.summarize([{'fullMatchOutputs': outputs}, {'fullMatchOutputs': {}}], {}, None)
        with self.assertRaises(Exception):
            experiment.summarize([{}], {}, None)

    def test_workflow_fetches_all_required_objects_and_exact_artifacts(self):
        path = ROOT / '.github/workflows/fts-string-decoder-repeat.yml'
        workflow = path.read_text(encoding='utf8')
        for commit in (experiment.BASE, experiment.CANDIDATE, experiment.repeat.driver.native_harness.REFERENCE):
            self.assertIn(commit, workflow)
        for pin in experiment.PINS.values():
            self.assertIn("artifact-ids: '" + str(pin['artifact']) + "'", workflow)
            self.assertIn("run-id: '" + str(pin['run']) + "'", workflow)
        self.assertIn('cat-file -e "$sha^{commit}"', workflow)
        self.assertIn('fts_string_decoder_repeat.py --phase run', workflow)

    def test_generic_profiler_does_not_start_for_repeat_driver(self):
        trigger = (ROOT / '.github/workflows/fts-profile-diagnostics.yml').read_text(encoding='utf8').split(
            '\npermissions:', 1)[0]
        for path in experiment.DRIVER_FILES:
            self.assertIn('- ' + path, trigger)


if __name__ == '__main__':
    unittest.main()
