#!/usr/bin/env python3
"""Compare match-only filter elision with the saved complete c3d7988 executable."""
import json

import fts_encoded_sort_validation as validation
import fts_inverted_sort_validation as previous
from fts_encoded_sort_revalidation import corrected_equality_work


driver, common = validation.driver, validation.common
PRODUCT = 'src/observer/composition/retrieval/ob_das_legacy_tr_merge_iter.cpp'
OLD_FILTER = b'  sr_iter_param_.filter_expr_ = ir_ctdef_->match_filter_;'
NEW_FILTER = (b'  sr_iter_param_.filter_expr_ = ir_ctdef_->need_calc_relevance() '
              b'? ir_ctdef_->match_filter_ : nullptr;')
ORIGINAL_SOURCE_PROOF = validation.source_proof
ORIGINAL_BUILD_PROFILE = validation.build_profile


def verify_product(original, current):
    driver.require(original.count(OLD_FILTER) == 1
                   and current == original.replace(OLD_FILTER, NEW_FILTER, 1),
                   'Only the match-only score filter may be omitted')


def source_proof(args):
    proof = ORIGINAL_SOURCE_PROOF(args, (PRODUCT,))
    original = common.git(args.source, 'show', previous.BASE + ':' + PRODUCT)
    current = common.git(args.source, 'show', args.driver_sha + ':' + PRODUCT)
    verify_product(original, current)
    return dict(proof, experiment='MATCH_ONLY_FILTER_VS_C3D7988',
                relevanceProjectionPreserved=True, scoredFilterPreserved=True)


def build_profile(args):
    settings, rust = ORIGINAL_BUILD_PROFILE(args, (PRODUCT,))
    if args.phase == 'snapshot':
        path = args.root / 'evidence/candidate/affected-objects.json'
        records = json.loads(path.read_text())
        for row in records:
            driver.require(str(args.source / PRODUCT) in row.pop('dependencyText'),
                           'The modified retrieval source is absent from compiler dependencies')
        driver.dump(path, records)
    return settings, rust


def build_hint_proof(log_text, role):
    result = previous.build_hint_proof(log_text, 'baseline')
    result['role'] = role
    return result


def configure():
    for key, value in previous.PIN.items():
        setattr(validation, key, value)
        setattr(driver, key, value)
    validation.source_proof = driver.source_proof = source_proof
    validation.build_profile = driver.build_profile = build_profile
    validation.build_hint_proof = build_hint_proof
    validation.equality_work = corrected_equality_work


if __name__ == '__main__':
    configure()
    raise SystemExit(validation.main())
