"""Synthetic offline tests. Never run a preserved ELF, ldd, or a real artifact."""
import copy
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

import fts_paired_collect as collector
import fts_verified_elf_reuse as reuse


def digest(data):
    return hashlib.sha256(data).hexdigest()


def tar_bytes(items):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz', format=tarfile.USTAR_FORMAT) as archive:
        for name, data, kind in items:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(data) if kind in (tarfile.REGTYPE, tarfile.AREGTYPE) else 0
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = '/outside/never-followed'
            archive.addfile(info, io.BytesIO(data) if info.size else None)
    return output.getvalue()


class VerifiedReuseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.producer = self.base / 'producer'
        self.collected = self.base / 'collected'
        self.sequence = 0
        for name in ('runtime/baseline', 'runtime/candidate', 'evidence/baseline',
                     'evidence/candidate', 'validation/cases', 'validation/rounds'):
            (self.producer / name).mkdir(parents=True, exist_ok=True)
        self.inventory = {'build.sh': '1' * 64, 'tools/benchmark/fts_large_bench.sh': '2' * 64}
        self.identities, self.identity_raw, self.binary_bytes, pins = {}, {}, {}, []
        for production_pin in reuse._PRODUCTION_PINS.roles:
            role = production_pin.role
            binary = self.producer / 'runtime' / role / 'seekdb'
            data = b'\x7fELF' + role.encode() + b'-' * 31 + production_pin.source_commit.encode() + b'-preserved-complete-end'
            binary.write_bytes(data)
            self.binary_bytes[role] = data
            pin = replace(production_pin, byte_count=len(data), sha256=digest(data))
            pins.append(pin)
            identity = dict(role=role, sourceCommit=pin.source_commit, sourceTree=pin.source_tree,
                kernelOrigin=pin.kernel_origin, driverCommit=reuse._PRODUCTION_PINS.driver_commit,
                binary=str(binary), bytes=len(data), sha256=digest(data), buildId=pin.build_id,
                generatedVersionSha256=pin.generated_version_sha256,
                officialInputHashes=self.inventory, buildSettings=dict(reuse.BUILD_SETTINGS),
                rustVersion=reuse.RUST_VERSION, loader=dict(command=['ldd', str(binary)], timeoutSeconds=30,
                    exitCode=0, childrenCleaned=True,
                    output=str(self.producer / 'evidence' / role / 'seekdb-ldd.log'), outputSha256='3' * 64))
            self.identities[role] = identity
            # Deliberately noncanonical formatting must survive relocation byte-for-byte.
            raw = (json.dumps(identity, indent=3) + '\n\n').encode()
            self.identity_raw[role] = raw
            binary.with_name('identity.json').write_bytes(raw)
        self.pins = replace(reuse._PRODUCTION_PINS, roles=tuple(pins), inventory_count=len(self.inventory),
            inventory_sha256=digest(json.dumps(self.inventory, sort_keys=True, separators=(',', ':')).encode()))
        self.plan = dict(scope='SAME_HOST_DEVELOPMENT_CALIBRATION_NOT_FORMAL_CONTEST',
            driverCommit=self.pins.driver_commit, baselineCommit=pins[0].source_commit,
            kernelOrigin=pins[1].kernel_origin, workspace='/fixture/source', root=str(self.producer),
            baselineSource=str(self.producer / 'sources/baseline'), candidateSource='/fixture/source',
            schedule=[], pairs=[], benchmarkConfig={}, performanceMargin=0.02, profiler=False, caseRetries=0,
            driverTree=pins[1].source_tree, equivalence={'kernelOrigin': pins[1].kernel_origin},
            driverFiles=dict(reuse._HISTORICAL_DRIVER_FILES))
        (self.producer / 'plan.json').write_text(json.dumps(self.plan))
        # Historical native failure remains independent from ELF reuse approval.
        (self.producer / 'report.json').write_text(json.dumps(dict(status='invalid_calibration',
            failure={'phase': 'validate', 'reason': 'original native cn_word failed'})))
        self.manifest = collector.collect(self.producer, self.collected)
        self.assertTrue(self.manifest['ok'], self.manifest['errors'])
        self.archives = {name: (self.collected / name).read_bytes() for name in reuse.ARCHIVES}

    def package(self, archives=None, manifest=None, zip_items=None, raw_manifest=None):
        self.sequence += 1
        archives = self.archives if archives is None else archives
        manifest = copy.deepcopy(self.manifest if manifest is None else manifest)
        for record in manifest['archives']:
            if record['path'] in archives:
                data = archives[record['path']]
                record.update(bytes=len(data), sha256=digest(data))
        manifest_raw = json.dumps(manifest).encode() if raw_manifest is None else raw_manifest
        items = [('manifest.json', manifest_raw)] + list(archives.items())
        if zip_items is not None:
            items = zip_items(items)
        path = self.base / ('artifact-%d.zip' % self.sequence)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as archive:
                for entry in items:
                    if len(entry) == 3:
                        name, data, mode = entry
                    else:
                        name, data = entry
                        mode = stat.S_IFREG | 0o644
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = mode << 16
                    archive.writestr(info, data)
        raw = path.read_bytes()
        return path, replace(self.pins, zip_bytes=len(raw), zip_sha256=digest(raw))

    def run_prepare(self, path, pins, name=None):
        root = self.base / (name or ('runtime-%d' % self.sequence))

        def notes(binary, output):
            pin = next(pin for pin in pins.roles if pin.role == binary.parent.name)
            output.write_text('Build ID: ' + pin.build_id + '\n')
            return pin.build_id

        with patch.object(reuse, '_read_build_id', side_effect=notes):
            return reuse._prepare_verified_runtime(path, root, pins)

    def rejected(self, path, pins, name=None):
        root = self.base / (name or ('runtime-%d' % self.sequence))
        with self.assertRaises(reuse.VerificationError):
            self.run_prepare(path, pins, root.name)
        self.assertFalse((root / 'verification.json').exists())
        if root.exists():
            self.assertTrue((root / 'verification.failed.json').is_file())
            for binary in root.glob('*/seekdb'):
                self.assertFalse(binary.stat().st_mode & 0o111)
        return root

    def change_evidence(self, name, data, manifest=None, extra=()):
        manifest = copy.deepcopy(self.manifest if manifest is None else manifest)
        output = []
        with tarfile.open(fileobj=io.BytesIO(self.archives['evidence.tar.gz']), mode='r:gz') as archive:
            for info in archive:
                raw = archive.extractfile(info).read()
                output.append((info.name, data if info.name == name else raw, tarfile.REGTYPE))
        output += list(extra)
        for record in manifest['files']:
            if record['path'] == name:
                manifest['logPayloadBytes'] += len(data) - record['bytes']
                record.update(originalBytes=len(data), bytes=len(data), sha256=digest(data))
        archives = dict(self.archives, **{'evidence.tar.gz': tar_bytes(output)})
        return self.package(archives=archives, manifest=manifest)

    def test_real_collector_interoperability_and_original_bytes_preserved(self):
        path, pins = self.package()
        # This also proves source SHA detection works across read chunk boundaries.
        with patch.object(reuse, 'CHUNK', 32):
            result = self.run_prepare(path, pins)
        self.assertEqual(result['status'], 'verified_runtime_prepared')
        self.assertFalse(result['oldValidationAccepted'])
        self.assertFalse(result['currentValidationPerformed'])
        for role, record in result['roles'].items():
            binary = Path(record['binary'])
            self.assertEqual(binary.read_bytes(), self.binary_bytes[role])
            self.assertEqual(binary.stat().st_mode & 0o777, 0o555)
            self.assertEqual(Path(record['identityOriginalPath']).read_bytes(), self.identity_raw[role])
            self.assertEqual(record['identity'], self.identities[role])
            self.assertNotEqual(record['identity']['binary'], record['binary'])
            provenance = record['provenance']
            self.assertEqual(provenance['originalIdentitySha256'], digest(self.identity_raw[role]))
            self.assertEqual(provenance['originalBinaryPath'], self.identities[role]['binary'])
            self.assertEqual(provenance['runtimeBinaryPath'], record['binary'])
            self.assertEqual(provenance['sourceCommit'], self.identities[role]['sourceCommit'])
            self.assertEqual(provenance['actualValidationDriver'], 'UNASSIGNED')
        self.assertEqual((self.producer / 'report.json').read_text(), json.dumps(dict(status='invalid_calibration',
            failure={'phase': 'validate', 'reason': 'original native cn_word failed'})))

    def test_real_collector_known_optional_nontext_omission_preserves_error(self):
        log = self.producer / 'validation/cases/baseline/fts_index.cn_word/db-log-tails/seekdb.log'
        log.parent.mkdir(parents=True)
        log.write_bytes((b'text-\xff' * 1000))
        output = self.base / 'collected-with-omission'
        manifest = collector.collect(self.producer, output)
        self.assertFalse(manifest['ok'])
        self.assertEqual(manifest['errors'], [log.relative_to(self.producer).as_posix() + ': ' + reuse._OPTIONAL_ERROR])
        archives = {name: (output / name).read_bytes() for name in reuse.ARCHIVES}
        path, pins = self.package(archives=archives, manifest=manifest)
        result = self.run_prepare(path, pins)
        self.assertFalse(result['originalManifest']['ok'])
        self.assertEqual(result['originalManifest']['errors'], manifest['errors'])
        self.assertFalse(result['oldValidationAccepted'])

    def test_real_collector_sparse_diagnostic_conversion_is_accounted_not_parsed(self):
        log = self.producer / 'validation/cases/baseline/fts_index.cn_word/db-log-tails/seekdb.log'
        log.parent.mkdir(parents=True)
        log.write_bytes(b'ordinary SQL diagnostic\\path\n' * 100 + b'invalid=\xff\n')
        output = self.base / 'collected-with-diagnostic'
        manifest = collector.collect(self.producer, output)
        self.assertTrue(manifest['ok'], manifest['errors'])
        record = next(record for record in manifest['files'] if record['path'] == log.relative_to(self.producer).as_posix())
        self.assertTrue(record['converted'])
        archives = {name: (output / name).read_bytes() for name in reuse.ARCHIVES}
        path, pins = self.package(archives=archives, manifest=manifest)
        result = self.run_prepare(path, pins)
        self.assertEqual(result['originalManifest']['files'], manifest['files'])
        self.assertFalse(any(Path(result['runtimeRoot']).rglob('seekdb.log')))

    def test_optional_collector_error_for_other_case_role_or_failure_is_fatal(self):
        for name, reason in (
            ('validation/cases/candidate/fts_index.cn_word/db-log-tails/seekdb.log', reuse._OPTIONAL_ERROR),
            ('validation/cases/baseline/fts_index.basic_dml/db-log-tails/seekdb.log', reuse._OPTIONAL_ERROR),
            ('validation/cases/baseline/fts_index.cn_word/db-log-tails/seekdb.log', 'Unexpected binary evidence')):
            manifest = copy.deepcopy(self.manifest)
            manifest['files'].append(dict(path=name, originalBytes=100, bytes=0, truncated=False, error=reason))
            manifest.update(ok=False, errors=[name + ': ' + reason])
            path, pins = self.package(manifest=manifest)
            self.rejected(path, pins)

    def test_public_api_has_no_identity_override_and_uses_production_pins(self):
        path, _ = self.package()
        with patch.object(reuse, '_read_build_id') as readelf:
            with self.assertRaises(reuse.VerificationError):
                reuse.prepare_verified_runtime(path, self.base / 'production')
            readelf.assert_not_called()
        self.assertFalse((self.base / 'production').exists())
        with self.assertRaises(TypeError):
            reuse.prepare_verified_runtime(path, self.base / 'production', self.pins)

    def test_outer_full_digest_size_checked_before_zip_parser(self):
        path, pins = self.package()
        for wrong in (replace(pins, zip_bytes=pins.zip_bytes + 1), replace(pins, zip_bytes=pins.zip_bytes - 1),
                      replace(pins, zip_sha256='0' * 64)):
            with self.subTest(pins=wrong), patch.object(reuse.zipfile, 'ZipFile') as parser:
                self.rejected(path, wrong, 'wrong-' + str(wrong.zip_bytes) + wrong.zip_sha256[:2])
                parser.assert_not_called()

    def test_archive_full_digest_mismatch_before_runtime_write(self):
        manifest = copy.deepcopy(self.manifest)
        raw = json.dumps(manifest).replace(manifest['archives'][0]['sha256'], '0' * 64).encode()
        path, pins = self.package(raw_manifest=raw)
        self.rejected(path, pins)
        self.assertFalse((self.base / ('runtime-%d' % self.sequence)).exists())

    def test_archive_claim_size_mismatch(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['archives'][0]['bytes'] += 1
        path, pins = self.package(raw_manifest=json.dumps(manifest).encode())
        self.rejected(path, pins)

    def test_zip_path_traversal_absolute_backslash_duplicates_and_extra_members(self):
        for name in ('../manifest.json', '/manifest.json', 'x\\manifest.json', 'x/../manifest.json',
                     'x//manifest.json', 'C:/manifest.json'):
            with self.subTest(name=name):
                path, pins = self.package(zip_items=lambda items: [(name, items[0][1])] + items[1:])
                self.rejected(path, pins)
        for transform in (lambda items: items + [items[0]], lambda items: items[:-1],
                          lambda items: items + [('extra', b'no')]):
            path, pins = self.package(zip_items=transform)
            self.rejected(path, pins)

    def test_zip_hidden_nul_name_rejected(self):
        # ZipInfo's writer strips NULs, so inject this malformed raw name into
        # both existing header copies without changing offsets or payload CRCs.
        path, pins = self.package(zip_items=lambda items: [('manifest.jsonXhidden', items[0][1])] + items[1:])
        raw = path.read_bytes().replace(b'manifest.jsonXhidden', b'manifest.json\x00hidden')
        path.write_bytes(raw)
        self.rejected(path, replace(pins, zip_bytes=len(raw), zip_sha256=digest(raw)))

    def test_zip_symlink_special_and_directory_rejected(self):
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600, stat.S_IFCHR | 0o600, stat.S_IFDIR | 0o755):
            with self.subTest(mode=mode):
                path, pins = self.package(zip_items=lambda items: [(items[0][0], items[0][1], mode)] + items[1:])
                self.rejected(path, pins)

    def test_zip_member_size_bound(self):
        path, pins = self.package()
        with patch.object(reuse, 'MANIFEST_CAP', 10):
            self.rejected(path, pins)

    def test_manifest_schema_duplicate_nonfinite_malformed_and_truncated_json(self):
        good = json.dumps(self.manifest)
        for raw in (b'{}', b'[]', b'not json', b'\xff', good[:-1].encode(),
                    ('{"scope":"duplicate",' + good[1:]).encode(),
                    good.replace('"logPayloadBytes":', '"bad": NaN,"logPayloadBytes":').encode(),
                    good.replace('"logPayloadBytes":', '"bad": 1e999,"logPayloadBytes":').encode()):
            with self.subTest(raw=raw[:80]):
                path, pins = self.package(raw_manifest=raw)
                self.rejected(path, pins)

    def test_manifest_fatal_error_is_never_suppressed(self):
        manifest = copy.deepcopy(self.manifest)
        manifest.update(ok=False, errors=['baseline: Identity bytes/SHA256 mismatch'])
        path, pins = self.package(manifest=manifest)
        self.rejected(path, pins)

    def test_manifest_file_digest_and_duplicate_inventory_errors(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['files'].append(copy.deepcopy(manifest['files'][0]))
        path, pins = self.package(manifest=manifest)
        self.rejected(path, pins)
        manifest = copy.deepcopy(self.manifest)
        next(record for record in manifest['files'] if record['path'] == 'plan.json')['sha256'] = '0' * 64
        path, pins = self.package(manifest=manifest)
        self.rejected(path, pins)

    def test_manifest_role_source_build_id_and_binary_size_pin_mismatch(self):
        for key, value in (('role', 'candidate'), ('sourceCommit', '0' * 40), ('buildId', '0' * 40),
                           ('bytes', 1), ('sha256', '0' * 64), ('truncated', True)):
            manifest = copy.deepcopy(self.manifest)
            manifest['files'][0][key] = value
            with self.subTest(key=key):
                path, pins = self.package(manifest=manifest)
                self.rejected(path, pins)

    def test_identity_all_immutable_metadata_mismatches(self):
        changes = [('role', 'baseline'), ('sourceCommit', '0' * 40), ('sourceTree', '0' * 40),
                   ('kernelOrigin', '0' * 40), ('driverCommit', '0' * 40), ('bytes', True),
                   ('sha256', '0' * 64), ('buildId', '0' * 40), ('generatedVersionSha256', '0' * 64),
                   ('buildSettings', dict(reuse.BUILD_SETTINGS, OB_ENABLE_UNITY='OFF')),
                   ('rustVersion', 'rustc different'), ('officialInputHashes', {}),
                   ('officialInputHashes', dict(self.inventory, **{'build.sh': '0' * 64})),
                   ('binary', '/other/runtime/candidate/seekdb')]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                identity = dict(self.identities['candidate'], **{key: value})
                path, pins = self.change_evidence('runtime/candidate/identity.json', json.dumps(identity).encode())
                self.rejected(path, pins)

    def test_identity_json_rejects_duplicate_keys_nonfinite_invalid_or_extra_schema(self):
        raw = self.identity_raw['candidate']
        for data in (raw[:-3], b'{"role": NaN}', b'{"role": "candidate", "role": "candidate"}',
                     json.dumps(dict(self.identities['candidate'], unexpected=True)).encode()):
            path, pins = self.change_evidence('runtime/candidate/identity.json', data)
            self.rejected(path, pins)

    def test_plan_identity_and_original_role_path_mismatch(self):
        for key, value in (('driverCommit', '0' * 40), ('driverTree', '0' * 40),
                           ('kernelOrigin', '0' * 40), ('baselineCommit', '0' * 40),
                           ('root', '/different/root'), ('workspace', '../relative')):
            plan = dict(self.plan, **{key: value})
            path, pins = self.change_evidence('plan.json', json.dumps(plan).encode())
            self.rejected(path, pins)
        plan = dict(self.plan, driverFiles=dict(self.plan['driverFiles'], **{'fts_paired_collect.py': '0' * 64}))
        path, pins = self.change_evidence('plan.json', json.dumps(plan).encode())
        self.rejected(path, pins)

    def test_evidence_tar_escape_duplicate_link_special_and_unlisted_files(self):
        for name, kind in (('../escape', tarfile.REGTYPE), ('/escape', tarfile.REGTYPE),
                           ('x\\escape', tarfile.REGTYPE), ('plan.json', tarfile.REGTYPE),
                           ('link', tarfile.SYMTYPE), ('hardlink', tarfile.LNKTYPE),
                           ('fifo', tarfile.FIFOTYPE), ('character', tarfile.CHRTYPE),
                           ('directory', tarfile.DIRTYPE), ('unlisted.json', tarfile.REGTYPE)):
            with self.subTest(name=name, kind=kind):
                path, pins = self.change_evidence('plan.json', json.dumps(self.plan).encode(),
                                                  extra=((name, b'bad', kind),))
                self.rejected(path, pins)

    def test_tar_extensions_rejected_without_following_metadata(self):
        for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK,
                     tarfile.GNUTYPE_SPARSE):
            with self.subTest(kind=kind):
                archives = dict(self.archives)
                archives['candidate-elf.tar.gz'] = tar_bytes([('candidate/seekdb', b'', kind)])
                path, pins = self.package(archives=archives)
                self.rejected(path, pins)

    def test_binary_tar_escape_wrong_role_duplicate_missing_and_oversize(self):
        for items in ([('../candidate/seekdb', self.binary_bytes['candidate'], tarfile.REGTYPE)],
                      [('baseline/seekdb', self.binary_bytes['candidate'], tarfile.REGTYPE)],
                      [('candidate/seekdb', self.binary_bytes['candidate'], tarfile.REGTYPE)] * 2,
                      [], [('candidate/seekdb', self.binary_bytes['candidate'] + b'x', tarfile.REGTYPE)]):
            archives = dict(self.archives)
            archives['candidate-elf.tar.gz'] = tar_bytes(items)
            path, pins = self.package(archives=archives)
            self.rejected(path, pins)

    def test_tar_truncation_bad_header_nonzero_padding_and_trailing_data(self):
        import gzip
        original = gzip.decompress(self.archives['candidate-elf.tar.gz'])
        data_size = len(self.binary_bytes['candidate'])
        variants = [original[:600], b'bad' + original[3:], original[:512 + data_size] + b'X' + original[513 + data_size:],
                    original + b'nonzero-trailing-data']
        for raw in variants:
            archives = dict(self.archives)
            archives['candidate-elf.tar.gz'] = gzip.compress(raw)
            path, pins = self.package(archives=archives)
            self.rejected(path, pins)
        archives = dict(self.archives)
        archives['candidate-elf.tar.gz'] = archives['candidate-elf.tar.gz'][:-4]
        path, pins = self.package(archives=archives)
        self.rejected(path, pins)

    def test_tar_decompressed_zero_padding_bomb_is_bounded(self):
        import gzip
        archives = dict(self.archives)
        original = gzip.decompress(archives['candidate-elf.tar.gz'])
        archives['candidate-elf.tar.gz'] = gzip.compress(original + bytes(20000))
        path, pins = self.package(archives=archives)
        self.rejected(path, pins)

    def test_manifest_evidence_accounting_and_missing_inventory_fail(self):
        for alter in (
            lambda manifest: manifest.update(logPayloadBytes=manifest['logPayloadBytes'] + 1),
            lambda manifest: manifest['files'].remove(next(record for record in manifest['files'] if record['path'] == 'plan.json')),
            lambda manifest: next(record for record in manifest['files'] if record['path'] == 'plan.json').update(truncated=True),
            lambda manifest: next(record for record in manifest['files'] if record['path'] == 'plan.json').update(originalBytes=True)):
            manifest = copy.deepcopy(self.manifest)
            alter(manifest)
            path, pins = self.package(manifest=manifest)
            self.rejected(path, pins)

    def test_full_elf_hash_magic_and_embedded_source_are_independent_proofs(self):
        original = self.binary_bytes['candidate']
        for replacement in (original[:-1] + b'X', b'NOPE' + original[4:],
                            original.replace(self.pins.roles[1].source_commit.encode(), b'0' * 40)):
            archives = dict(self.archives)
            archives['candidate-elf.tar.gz'] = tar_bytes([('candidate/seekdb', replacement, tarfile.REGTYPE)])
            path, pins = self.package(archives=archives)
            if replacement == original[:-1] + b'X':
                # Existing pins catch different full bytes even with updated archive claims.
                self.rejected(path, pins)
            else:
                # A synthetic private fixture digest can agree while magic or embedded
                # source still disagrees. No production API accepts these altered pins.
                changed = replace(pins.roles[1], sha256=digest(replacement))
                manifest = copy.deepcopy(self.manifest)
                next(record for record in manifest['files'] if record.get('role') == 'candidate')['sha256'] = changed.sha256
                identity = dict(self.identities['candidate'], sha256=changed.sha256)
                self.archives, saved = archives, self.archives
                try:
                    path, pins2 = self.change_evidence('runtime/candidate/identity.json', json.dumps(identity).encode(), manifest=manifest)
                finally:
                    self.archives = saved
                self.rejected(path, replace(pins2, roles=(pins.roles[0], changed)))

    def test_actual_static_build_id_mismatch_has_no_executable_approval(self):
        path, pins = self.package()
        root = self.base / 'wrong-build-id'
        with patch.object(reuse, '_read_build_id', return_value='0' * 40), self.assertRaises(reuse.VerificationError):
            reuse._prepare_verified_runtime(path, root, pins)
        self.assertFalse((root / 'verification.json').exists())
        self.assertTrue((root / 'verification.failed.json').exists())
        self.assertTrue(all(not binary.stat().st_mode & 0o111 for binary in root.glob('*/seekdb')))

    def test_no_overwrite_existing_directory_file_source_or_output(self):
        path, pins = self.package()
        existing = self.base / 'existing'
        existing.mkdir()
        sentinel = existing / 'keep'
        sentinel.write_bytes(b'unchanged')
        for target in (existing, self.producer, path):
            with self.subTest(target=target), self.assertRaises(reuse.VerificationError):
                reuse._prepare_verified_runtime(path, target, pins)
        self.assertEqual(sentinel.read_bytes(), b'unchanged')
        result = self.run_prepare(path, pins)
        with self.assertRaises(reuse.VerificationError):
            reuse._prepare_verified_runtime(path, Path(result['runtimeRoot']), pins)

    def test_reject_symlink_components_and_hardlinked_input(self):
        path, pins = self.package()
        symlink = self.base / 'linked.zip'
        symlink.symlink_to(path)
        with self.assertRaises(reuse.VerificationError):
            reuse._prepare_verified_runtime(symlink, self.base / 'symlink-out', pins)
        directory_link = self.base / 'directory-link'
        directory_link.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(reuse.VerificationError):
            reuse._prepare_verified_runtime(path, directory_link / 'symlink-out', pins)
        os.link(path, self.base / 'hardlinked.zip')
        self.rejected(path, pins)

    def test_static_readelf_command_never_executes_elf_or_ldd(self):
        binary = self.base / 'seekdb'
        binary.write_bytes(b'not-executed')
        notes = self.base / 'notes.log'

        def run(command, **kwargs):
            self.assertEqual(command, ['/usr/bin/readelf', '-n', '--', str(binary)])
            self.assertEqual(kwargs['timeout'], 30)
            self.assertEqual(kwargs['stdin'], reuse.subprocess.DEVNULL)
            kwargs['stdout'].write(b'Build ID: ' + b'a' * 40 + b'\n')
            return type('Result', (), {'returncode': 0})()

        with patch.object(reuse.subprocess, 'run', side_effect=run):
            self.assertEqual(reuse._read_build_id(binary, notes), 'a' * 40)

    def test_static_readelf_missing_duplicate_failure_and_cap(self):
        for data, code in ((b'no build id', 0), (b'Build ID: aa\nBuild ID: bb\n', 0),
                           (b'Build ID: aa\n', 1), (b'X' * (reuse.NOTE_CAP + 1), 0)):
            self.sequence += 1
            notes = self.base / ('notes-%d' % self.sequence)

            def run(command, **kwargs):
                kwargs['stdout'].write(data)
                return type('Result', (), {'returncode': code})()

            with patch.object(reuse.subprocess, 'run', side_effect=run), self.assertRaises(reuse.VerificationError):
                reuse._read_build_id(self.base / 'not-executed', notes)


if __name__ == '__main__':
    unittest.main()
