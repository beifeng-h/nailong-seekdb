#!/usr/bin/env python3
"""Compile one corrected B and reuse only the verified original A.

The historical hybrid and exact-B3 entry points remain byte-for-byte pinned to
those old kernels. This entry point loads the reviewed hybrid implementation
into a private module so its new identity cannot relabel any archived B3 ELF.
Original SQL, benchmark, scoring, strict TOKENIZE equivalence, and cleanup gates
are unchanged. Only the new fixed candidate is compiled, once.
"""
import importlib.util
from pathlib import Path
import sys

KERNEL = 'd0aee8d81f0d71e8b743ec75eae04a2339972d77'
KERNEL_TREE = 'f8ff4c56af96aa21d526faca8e4c9076b743bb1e'
CASE = 'fts_index.tokenize_last_word_representative_contract'
CASE_TEST_SHA256 = '223ad5654e95fd29e165c6f4efc98e910f58f8a352338a76e41ef37aa5c31bc0'
CASE_RESULT_SHA256 = 'c50004052d88662656182bb6a4f2cd6e730b3e3b6b3775e3728c1c2f2eb39da3'

_spec = importlib.util.spec_from_file_location('_fts_last_word_hybrid',
    Path(__file__).with_name('fts_source_hybrid_validation.py'))
if _spec is None or _spec.loader is None:
    raise RuntimeError('Reviewed source-hybrid implementation is unavailable')
hybrid = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hybrid)

# Private globals affect only this corrected-source entry point. The ordinary
# fts_source_hybrid_validation and fts_b3_reuse_validation modules keep their old
# source/ELF identities and their historical invalid verdicts.
hybrid.MODE = 'verified_original_A_last_word_fixed_source_B_v1'
hybrid.PRODUCTION_KERNEL = KERNEL
hybrid.PRODUCTION_KERNEL_TREE = KERNEL_TREE
hybrid.OLD_KERNELS = hybrid.OLD_KERNELS | frozenset((
    'c235713689325d2bb8882b86f8f89ee99ff9b43a',
    'b3eed52688fc1b178632a9dbb38aabb32f805c5d',
    'c146ba7e9039fa1779178e0ae3c89bc087bc9730'))
hybrid.NEW_CASES = hybrid.NEW_CASES + (CASE,)
hybrid.DRIVER_FILES = hybrid.DRIVER_FILES + ('fts_last_word_validation.py',)
hybrid.ALLOWED_DRIVER_PATHS = hybrid.ALLOWED_DRIVER_PATHS | frozenset((
    hybrid.PREFIX + 'fts_last_word_validation.py',
    hybrid.PREFIX + 'test_fts_last_word_validation.py',
    hybrid.PREFIX + 'test_fts_b3_live_workflow.py',
    hybrid.PREFIX + 'fixtures/fts-exact-b3-c146-validation.yml'))

_original_case_input_hashes = hybrid.case_input_hashes


def case_input_hashes(source):
    result = _original_case_input_hashes(source)
    if result[CASE] != dict(testSha256=CASE_TEST_SHA256,
                            expectedSha256=CASE_RESULT_SHA256):
        raise RuntimeError('Last-word representative regression differs from the fixed kernel')
    return result


hybrid.case_input_hashes = case_input_hashes

if __name__ == '__main__':
    raise SystemExit(hybrid.main())
