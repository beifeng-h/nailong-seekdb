"""Synthetic collector tests only: no SQL, network, compilation or runner data."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import fts_paired_collect as c


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'paired'
        self.out = Path(self.temp.name) / 'artifact'
        for name in ('runtime/baseline', 'runtime/candidate', 'evidence/baseline', 'evidence/candidate', 'validation/cases', 'validation/rounds'):
            (self.root / name).mkdir(parents=True)
        for role in ('baseline', 'candidate'):
            binary = self.root / 'runtime' / role / 'seekdb'
            binary.write_bytes(b'\x7fELF' + role.encode() * 100)
            identity = dict(role=role, sourceCommit=c.BASELINE if role == 'baseline' else 'a' * 40,
                            sourceTree='b' * 40, kernelOrigin=c.BASELINE if role == 'baseline' else c.KERNEL,
                            driverCommit='a' * 40, binary=str(binary),
                            bytes=binary.stat().st_size, sha256=c.digest(binary))
            binary.with_name('identity.json').write_text(json.dumps(identity))
        for name in ('plan.json', 'report.json'):
            (self.root / name).write_text('{}')
        (self.root / 'plan.json').write_text(json.dumps(dict(driverCommit='a' * 40, baselineCommit=c.BASELINE, kernelOrigin=c.KERNEL)))
        (self.root / 'validation/rounds/score.json').write_text('{"score":1}')
        (self.root / 'evidence/baseline/build.log').write_text('build passed\n')

    def run_collect(self):
        return c.collect(self.root, self.out)

    def change_identity(self, key, value, role='baseline'):
        path = self.root / 'runtime' / role / 'identity.json'
        data = json.loads(path.read_text())
        data[key] = value
        path.write_text(json.dumps(data))

    def test_full_bytes_independent_archives(self):
        result = self.run_collect()
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(len(result['archives']), 3)
        for role in ('baseline', 'candidate'):
            with tarfile.open(self.out / (role + '-elf.tar.gz')) as tar:
                self.assertEqual(tar.getnames(), [role + '/seekdb'])
                self.assertEqual(tar.extractfile(role + '/seekdb').read(),
                                 (self.root / 'runtime' / role / 'seekdb').read_bytes())
        self.assertEqual(json.loads((self.out / 'manifest.json').read_text())['ok'], True)

    def test_missing_binary_preserves_manifest_and_other_binary(self):
        (self.root / 'runtime/baseline/seekdb').unlink()
        result = self.run_collect()
        self.assertFalse(result['ok'])
        self.assertTrue((self.out / 'candidate-elf.tar.gz').exists())
        self.assertTrue((self.out / 'manifest.json').exists())

    def test_identity_mismatch(self):
        for key, value in [('bytes', 1), ('sha256', '0' * 64), ('sourceCommit', '0' * 40), ('sourceTree', 'bad'), ('role', 'candidate'), ('binary', '/wrong/seekdb'), ('driverCommit', '0' * 40), ('kernelOrigin', c.KERNEL)]:
            with self.subTest(key=key):
                original = (self.root / 'runtime/baseline/identity.json').read_text()
                self.change_identity(key, value)
                self.out = Path(self.temp.name) / ('artifact-' + key)
                self.assertFalse(self.run_collect()['ok'])
                (self.root / 'runtime/baseline/identity.json').write_text(original)

    def test_candidate_kernel_origin(self):
        self.change_identity('kernelOrigin', '0' * 40, 'candidate')
        self.assertFalse(self.run_collect()['ok'])

    def test_not_elf(self):
        (self.root / 'runtime/baseline/seekdb').write_bytes(b'not elf')
        self.assertFalse(self.run_collect()['ok'])

    def test_raw_and_compressed_caps(self):
        with patch.object(c, 'BINARY_RAW_CAP', 5):
            self.assertFalse(self.run_collect()['ok'])
        self.out = Path(self.temp.name) / 'compressed'
        with patch.object(c, 'BINARY_ARCHIVE_CAP', 5):
            self.assertFalse(self.run_collect()['ok'])
        self.assertFalse((self.out / 'baseline-elf.tar.gz').exists())

    def test_log_cap_and_tail(self):
        log = self.root / 'evidence/candidate/build.log'
        log.write_text('x' * 2048 + 'end')
        with patch.object(c, 'LOG_FILE_CAP', 1024):
            result = self.run_collect()
        self.assertTrue(result['ok'], result['errors'])
        item = next(f for f in result['files'] if f['path'] == 'evidence/candidate/build.log')
        self.assertTrue(item['truncated'])
        self.assertEqual(item['bytes'], 1024)
        with tarfile.open(self.out / 'evidence.tar.gz') as tar:
            self.assertTrue(tar.extractfile(item['path']).read().endswith(b'end'))

    def test_json_never_silently_truncated(self):
        with patch.object(c, 'LOG_TOTAL_CAP', 1):
            result = self.run_collect()
        self.assertFalse(result['ok'])
        self.assertTrue(any('Essential JSON' in s for s in result['errors']))

    def test_evidence_archive_cap(self):
        with patch.object(c, 'LOG_ARCHIVE_CAP', 5):
            result = self.run_collect()
        self.assertFalse(result['ok'])
        self.assertTrue((self.out / 'manifest.json').exists())
        self.assertFalse((self.out / 'evidence.tar.gz').exists())

    def test_forbidden_and_binary_evidence(self):
        for name, data in [('credentials.txt', b'secret'), ('core.bin', b'x'), ('innocent.log', b'\x7fELF\0payload')]:
            (self.root / 'evidence/candidate' / name).write_bytes(data)
        result = self.run_collect()
        self.assertFalse(result['ok'])
        with tarfile.open(self.out / 'evidence.tar.gz') as tar:
            self.assertFalse(any(name.endswith(('credentials.txt', 'core.bin', 'innocent.log')) for name in tar.getnames()))

    def test_unowned_paths_are_never_collected(self):
        (self.root / 'env').write_text('unrelated')
        (self.root / 'runtime/candidate/database').mkdir()
        (self.root / 'runtime/candidate/database/secret.txt').write_text('private')
        result = self.run_collect()
        self.assertTrue(result['ok'], result['errors'])
        with tarfile.open(self.out / 'evidence.tar.gz') as tar:
            self.assertFalse(any('database' in name or name == 'env' for name in tar.getnames()))

    def test_disjoint_and_fresh(self):
        for out in (self.root, self.root / 'artifact', self.root.parent):
            with self.subTest(out=out), self.assertRaises(ValueError):
                c.collect(self.root, out)
        self.out.mkdir()
        with self.assertRaises(FileExistsError):
            self.run_collect()

    def test_hardlink_rejected(self):
        source = self.root / 'runtime/baseline/seekdb'
        os.link(source, Path(self.temp.name) / 'alias')
        self.assertFalse(self.run_collect()['ok'])

    def test_symlink_evidence_rejected(self):
        link = self.root / 'evidence/candidate/linked.log'
        try:
            link.symlink_to(self.root / 'report.json')
        except OSError:
            self.skipTest('Symlink creation unavailable on this platform')
        self.assertFalse(self.run_collect()['ok'])

    def test_manifest_cap_fails_explicitly(self):
        with patch.object(c, 'MANIFEST_CAP', 2048):
            result = self.run_collect()
        self.assertFalse(result['ok'])
        self.assertLessEqual((self.out / 'manifest.json').stat().st_size, 2048)
        self.assertGreater(result['omittedFileRecords'], 0)

    def test_global_entry_cap(self):
        with patch.object(c, 'MAX_ENTRIES', 1):
            result = self.run_collect()
        self.assertFalse(result['ok'])

    def test_tailed_binary_head_rejected(self):
        (self.root / 'evidence/candidate/disguised.log').write_bytes(b'\x7fELF' + b'a' * 2048)
        with patch.object(c, 'LOG_FILE_CAP', 1024):
            self.assertFalse(self.run_collect()['ok'])

    def test_utf8_tail_boundary(self):
        (self.root / 'evidence/candidate/build.log').write_text('中' * 1000, encoding='utf-8')
        with patch.object(c, 'LOG_FILE_CAP', 1024):
            result = self.run_collect()
        self.assertTrue(result['ok'], result['errors'])

    def test_already_tailed_utf8_log(self):
        log = self.root / 'evidence/candidate/build.log'
        log.write_bytes(('中' * 10).encode('utf-8')[1:])
        result = self.run_collect()
        self.assertTrue(result['ok'], result['errors'])
        item = next(f for f in result['files'] if f['path'] == 'evidence/candidate/build.log')
        self.assertEqual(item['tailOffset'], 2)
        self.assertTrue(item['truncated'])

    def test_exact_helper_layout(self):
        case = self.root / 'validation/cases/baseline/case1'
        expected = {}
        for name in ('mysqltest-log/timer', 'db-log-tails/seekdb.log.wf',
                     'db-log-tails/seekdb.log.1', 'db-log-tails/seekdb.log.20261108153000',
                     'db-log-tails/seekdb.log.wf.20261108153000',
                     'db-log-tails/seekdb.log.20261108153000.wf'):
            path = case / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'123\n')
            expected[path.relative_to(self.root).as_posix()] = b'123\n'
        (case / 'manifest.json').write_text('{}')
        (case / 'result.json').write_text('{}')
        (case / 'tmp').mkdir()
        (case / 'tmp/native-temporary').write_bytes(b'\x00database')
        result = self.run_collect()
        self.assertTrue(result['ok'], result['errors'])
        with tarfile.open(self.out / 'evidence.tar.gz') as tar:
            for name, data in expected.items():
                self.assertEqual(tar.extractfile(name).read(), data)
            self.assertFalse(any('/tmp/' in name for name in tar.getnames()))

    def test_plan_pin_mismatch(self):
        (self.root / 'plan.json').write_text(json.dumps(dict(driverCommit='a' * 40, baselineCommit='0' * 40, kernelOrigin=c.KERNEL)))
        self.assertFalse(self.run_collect()['ok'])

    def test_candidate_source_mismatch(self):
        self.change_identity('sourceCommit', '0' * 40, 'candidate')
        self.assertFalse(self.run_collect()['ok'])

    def test_cli_failure_nonzero(self):
        (self.root / 'runtime/baseline/seekdb').unlink()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = c.main(['--root', str(self.root), '--out', str(self.out)])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output.getvalue())['ok'])


if __name__ == '__main__':
    unittest.main()
