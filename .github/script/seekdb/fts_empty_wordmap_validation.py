#!/usr/bin/env python3
"""Validate skipping cleanup of an empty FTS word map against a preserved build."""

import json

import fts_ddl_row_cache_validation as fixtures


validation = fixtures.validation
driver = fixtures.driver
PRODUCT = 'src/storage/fts/ob_fts_parser_helper.cpp'


def source_proof(args):
    return dict(fixtures.ORIGINAL_SOURCE_PROOF(args, (PRODUCT,)),
                experiment='FTS_EMPTY_WORDMAP_VS_PRESERVED_BASELINE',
                archivedBinaryCommit=fixtures.previous.ARCHIVED_BASE,
                archivedBinaryTree=fixtures.previous.TREE)


def build_profile(args):
    settings, rust = fixtures.ORIGINAL_BUILD_PROFILE(args, (PRODUCT,))
    if args.phase == 'snapshot':
        path = args.root / 'evidence/candidate/affected-objects.json'
        records = json.loads(path.read_text())
        for row in records:
            driver.require(str(args.source / PRODUCT) in row['dependencyText'],
                           'Modified parser helper is absent from compiler dependencies')
            row.pop('dependencyText')
            row['observedModifiedDependencies'] = [PRODUCT]
        driver.dump(path, records)
    return settings, rust


def configure():
    for key, value in fixtures.previous.PIN.items():
        setattr(validation, key, value)
        setattr(driver, key, value)
    validation.source_proof = driver.source_proof = source_proof
    validation.build_profile = driver.build_profile = build_profile
    validation.prebuild = driver.prebuild = fixtures.prebuild
    validation.build_hint_proof = fixtures.build_hint_proof
    validation.equality_work = fixtures.equality_work


if __name__ == '__main__':
    configure()
    raise SystemExit(validation.main())
