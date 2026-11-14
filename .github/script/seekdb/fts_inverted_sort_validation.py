#!/usr/bin/env python3
"""Build an inverted-only sort candidate and compare it with the saved c3d7988 ELF."""
import re

import fts_encoded_sort_validation as validation
from fts_encoded_sort_revalidation import corrected_equality_work


driver, common = validation.driver, validation.common
BASE = 'c3d798873b700987b6983043d2abf0defd8e2500'
PIN = dict(
    BASE=BASE, BASE_TREE='1377b9b22a2f5c3301579647b84507c076bed98e',
    BASE_ZIP_BYTES=329610377, BASE_TAR_BYTES=329587368,
    BASE_ZIP_SHA='346dc72b8784606d2c7d8c2ff9d5e0a15addbf4626461d98b601d8fcb7509480',
    BASE_TAR_SHA='50e0ddb773d609515b225918062ca670e937375f475e53d1822eb5e88dc599a1',
    BASE_ELF_BYTES=1332420904,
    BASE_ELF_SHA='61720b07ee1c34fdac0c4a8a67172e5729ad2ad08eacdf74c3014e5a96584c9f',
    BASE_BUILD_ID='2161c86c66582589b65323bd3a53ece1282e21b4',
    BASE_RUN=43350361661, BASE_ARTIFACT=14006965532)
OLD_GUARD = b'(table_schema.is_fts_index_aux() || table_schema.is_fts_doc_word_aux())'
NEW_GUARD = b'table_schema.is_fts_index_aux()'
INVERTED = ('fti_all_ik', 'fti_content_ik', 'fti_en_beng')
DOC_WORD = tuple(name + '_fts_doc_word' for name in INVERTED)
OTHER = ('fts_rowkey_doc', 'fts_doc_rowkey')
ORIGINAL_SOURCE_PROOF = validation.source_proof


def source_proof(args):
    proof = ORIGINAL_SOURCE_PROOF(args)
    original = common.git(args.source, 'show', BASE + ':' + validation.PRODUCT)
    current = common.git(args.source, 'show', args.driver_sha + ':' + validation.PRODUCT)
    driver.require(original.count(OLD_GUARD) == 1
                   and current == original.replace(OLD_GUARD, NEW_GUARD, 1),
                   'The candidate must only narrow the existing FTS sort guard')
    return dict(proof, experiment='INVERTED_ONLY_VS_C3D7988',
                productGuardChangeOnly=True, expectedEnabledAuxiliaryTables=3)


def build_hint_proof(log_text, role):
    driver.require(role in ('baseline', 'candidate'), 'Unknown sort validation role')
    expected = {name: True for name in INVERTED}
    expected.update({name: role == 'baseline' for name in DOC_WORD})
    expected.update({name: False for name in OTHER})
    seen = {}
    enabled_lines = []
    for line in log_text.splitlines():
        if 'generate_build_replica_sql' not in line or 'INSERT /*+' not in line:
            continue
        table = re.search(r'INTO `fts_large_bench`\.`__idx_\d+_([^`]+)`', line)
        hint = re.search(r"opt_param\('enable_newsort', '(true|false)'\)", line)
        driver.require(table is not None and hint is not None, 'Incomplete build-sort SQL evidence')
        name, enabled = table.group(1), hint.group(1) == 'true'
        driver.require(name in expected and name not in seen and enabled == expected[name],
                       'Unexpected auxiliary table or sort hint: ' + name)
        seen[name] = enabled
        if enabled:
            enabled_lines.append(line)
    driver.require(seen == expected, 'Original eight auxiliary build statements are incomplete')
    return dict(role=role, enabledCount=len(enabled_lines), disabledCount=len(seen)-len(enabled_lines),
                enabledSqlLines=enabled_lines, auxiliaryHints=seen)


def configure():
    for key, value in PIN.items():
        setattr(validation, key, value)
        setattr(driver, key, value)
    validation.source_proof = driver.source_proof = source_proof
    validation.build_hint_proof = build_hint_proof
    validation.equality_work = corrected_equality_work


def main():
    configure()
    return validation.main()


if __name__ == '__main__':
    raise SystemExit(main())
