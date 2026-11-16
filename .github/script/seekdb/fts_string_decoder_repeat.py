#!/usr/bin/env python3
"""Repeat the original FTS metrics using the preserved decoder executables."""
import fts_row_cache_repeat as repeat
import fts_string_decoder_validation as decoder


ORIGINAL_SUMMARIZE = repeat.summarize
BASE = '603075716540b4f14dff9e8650bbcb0d4d60d338'
CANDIDATE = '92b4c06d103ae3d4193e44e8971bf429e6c1909b'
CANDIDATE_TREE = '455586eef88b8bc28977dd42a7d99b589ac0abef'
DRIVER_FILES = frozenset((
    '.github/script/seekdb/fts_string_decoder_repeat.py',
    '.github/script/seekdb/test_fts_string_decoder_repeat.py',
    '.github/workflows/fts-profile-diagnostics.yml',
    '.github/workflows/fts-string-decoder-repeat.yml',
))

PINS = {
    'baseline': dict(commit=BASE, tree=decoder.TREE, artifact=14144705933, run=43667118747,
        zipBytes=329606064, zipSha256='486c4cfe5246b4e59295686e408ebcb2ca3c4299315a69acbae0398978a88555',
        tarBytes=329583055, tarSha256='4e9d55e3b38bdc04d0292e1fabf4d2990ff75bb829195901cc21048457b9a010',
        elfBytes=1332426096, elfSha256='04fc8e7df52a9f6596d4e4666709972b5a8998684d248aa190bf25d63af5b3bd',
        buildId='356b93d1932ce02ef58ab2fe7a97de72db3b7e92'),
    'candidate': dict(commit=CANDIDATE, tree=CANDIDATE_TREE, artifact=14160341412, run=43705406940,
        zipBytes=329604839, zipSha256='5188f9d48e07352735664021367bbef22e237fcc0962c1df34b4c5fd935d17c9',
        tarBytes=329581830, tarSha256='77a014cb2fead399905c29bbfe46275fce3161a3f16caccaf1eb47961c69a7fe',
        elfBytes=1332427024, elfSha256='d0ebcd7f16cec2a40d6bdb5e159057a2406360eae7e9aee5fdc57d021648cc3c',
        buildId='fd84e4c412b0810a4bb7837eb0b7d9392635d69a'),
}


def summarize(rounds, baseline, scorer):
    repeat.driver.require(bool(rounds), 'No repeated benchmark results')
    expected = rounds[0].get('fullMatchOutputs')
    repeat.driver.require(isinstance(expected, dict) and
                          set(expected) == {'query_cn', 'query_beng', 'query_mixed', 'query_limit'},
                          'Missing complete MATCH output summaries')
    repeat.driver.require(all(row.get('fullMatchOutputs') == expected for row in rounds),
                          'Full MATCH document outputs differ across repeated runs')
    result = ORIGINAL_SUMMARIZE(rounds, baseline, scorer)
    result['fullMatchOutputsAgree'] = True
    result['fullMatchOutputScope'] = 'Ordered document IDs after benchmark timing'
    return result


def configure():
    decoder.configure()
    repeat.BASE_SOURCE = BASE
    repeat.CANDIDATE = CANDIDATE
    repeat.CANDIDATE_TREE = CANDIDATE_TREE
    repeat.PRODUCTS = frozenset((decoder.PRODUCT,))
    repeat.ALLOWED_DRIVER_FILES = DRIVER_FILES
    repeat.archived.PINS = PINS
    repeat.archived.driver = repeat.driver
    repeat.row_cache.source_proof = decoder.source_proof
    repeat.driver.SCHEDULE = repeat.SCHEDULE
    repeat.summarize = summarize


def main():
    repeat.configure = configure
    return repeat.main()


if __name__ == '__main__':
    raise SystemExit(main())
