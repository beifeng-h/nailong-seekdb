import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import fts_ik_result_array_validation as experiment


class StopAfterIdentity(Exception):
    pass


class BaselineIdentityTests(unittest.TestCase):
    def test_archived_tree_mismatch_is_rejected_before_creating_output(self):
        args = argparse.Namespace(root=Path('unused-output'))
        with mock.patch.object(experiment.validation, 'BASE_TREE', 'source-tree'), \
             mock.patch.object(experiment.validation, 'BASE_ARCHIVE_TREE', 'different-tree'):
            with self.assertRaisesRegex(RuntimeError, 'trees must match'):
                experiment.validation.prepare(args)

    def test_prebuild_checks_the_archived_binary_version(self):
        driver = experiment.driver
        args = argparse.Namespace(source=Path('unused-source'), root=Path('unused-output'))
        with mock.patch.object(driver, 'BASE', 'rewritten-source'), \
             mock.patch.object(driver, 'BASE_ARCHIVE_COMMIT', 'archived-binary'), \
             mock.patch.object(driver, 'load_plan'), \
             mock.patch.object(driver, 'resource_record'), \
             mock.patch.object(driver, 'build_profile'), \
             mock.patch.dict(driver.common.CLIENTS, {}, clear=True), \
             mock.patch.object(driver.common, 'verify_loader'), \
             mock.patch.object(driver, 'verify_version', side_effect=StopAfterIdentity) as check:
            with self.assertRaises(StopAfterIdentity):
                experiment.validation._parent_prebuild(args)
            self.assertEqual(check.call_args.args[-1], 'archived-binary')

    def test_validation_checks_the_archived_binary_identity(self):
        validation, driver = experiment.validation, experiment.driver
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'report.json').write_text('{"status":"prepared"}')
            for role in ('baseline', 'candidate'):
                directory = root / 'runtime' / role
                directory.mkdir(parents=True)
                (directory / 'identity.json').write_text(json.dumps(dict(
                    binary='unused', sha256='digest', bytes=1, buildId='build')))
            args = argparse.Namespace(root=root, driver_sha='candidate')
            with mock.patch.object(validation, 'BASE', 'rewritten-source'), \
                 mock.patch.object(validation, 'BASE_ARCHIVE_COMMIT', 'archived-binary'), \
                 mock.patch.object(driver, 'load_plan'), \
                 mock.patch.object(driver, 'safe_elf', side_effect=StopAfterIdentity) as check:
                with self.assertRaises(StopAfterIdentity):
                    validation.validate(args)
                self.assertEqual(check.call_args.args[-1], 'archived-binary')


if __name__ == '__main__':
    unittest.main()
