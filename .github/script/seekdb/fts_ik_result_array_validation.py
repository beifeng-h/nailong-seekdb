#!/usr/bin/env python3
"""Compare the IK result container against the preserved complete baseline."""
import hashlib
import json

import fts_encoded_sort_validation as validation
import fts_inverted_sort_validation as sort_validation
from fts_encoded_sort_revalidation import corrected_equality_work


driver, common = validation.driver, validation.common
BASE = '2abe2633f85f41c1825fb144b29e2d3496e614df'
ARCHIVED_BASE = '9eaede23451fa9bfeae878c73aa56a7e0a04785a'
TREE = '7d7ca86448201fc8c39e99c45d8c3036f96a66e2'
PRODUCTS = (
    'src/storage/fts/ik/ob_ik_arbitrator.cpp',
    'src/storage/fts/ik/ob_ik_processor.cpp',
    'src/storage/fts/ik/ob_ik_processor.h',
    'src/storage/fts/ob_ik_ft_parser.cpp',
)
COMPILE_UNITS = tuple(path for path in PRODUCTS if path.endswith('.cpp'))
PIN = dict(
    BASE=BASE, BASE_TREE=TREE, BASE_ARCHIVE_COMMIT=ARCHIVED_BASE,
    BASE_ARCHIVE_TREE=TREE,
    BASE_ZIP_BYTES=329599861, BASE_TAR_BYTES=329576852,
    BASE_ZIP_SHA='18f35d2509c2e4dab0cf28e70c9bb06f9d6dc7b71c2105923081646cbb22f84e',
    BASE_TAR_SHA='3eb6fb87c38ee3cc5ad9dc32fb76a9667a40fc9f4975ce1c3bd03cb3a7887029',
    BASE_ELF_BYTES=1332421056,
    BASE_ELF_SHA='44e7d8d1054acf40811f4925ccc914331f45b793002dd8b54784d6b9385153b4',
    BASE_BUILD_ID='41599745ef85ddee1ef586763c3da42b34f8561f',
    BASE_RUN=43596282503, BASE_ARTIFACT=14115653708,
)
ORIGINAL_SOURCE_PROOF = validation.source_proof
ORIGINAL_BUILD_PROFILE = validation.build_profile
ORIGINAL_PREBUILD = validation.prebuild


def source_proof(args):
    return dict(ORIGINAL_SOURCE_PROOF(args, PRODUCTS),
                experiment='IK_RESULT_ARRAY_VS_PRESERVED_BASELINE',
                archivedBinaryCommit=ARCHIVED_BASE, archivedBinaryTree=TREE)


def build_profile(args):
    settings, rust = ORIGINAL_BUILD_PROFILE(args, COMPILE_UNITS)
    if args.phase == 'snapshot':
        path = args.root / 'evidence/candidate/affected-objects.json'
        records = json.loads(path.read_text())
        dependencies = '\n'.join(row['dependencyText'] for row in records)
        observed = [name for name in PRODUCTS if str(args.source / name) in dependencies]
        driver.require(set(observed) == set(PRODUCTS),
                       'A modified source or header is absent from compiler dependencies')
        for row in records:
            row.pop('dependencyText')
            row['observedModifiedDependencies'] = observed
        driver.dump(path, records)
    return settings, rust


def matrix_sql():
    samples = (
        ('empty', ''), ('ascii', 'one two three'), ('chinese', '人际关系'),
        ('count_end', '12个'), ('count_chain', '12三百公斤'),
        ('count_mixed', '5百 5个 32公斤 12三百公斤'),
        ('mixed', '数据库 Database 123.45 日本語 한국어 😀'),
        ('dictionary', '中华人民共和国人民大会堂'),
        ('short', '中 ' * 4), ('near_63', '中 ' * 63),
        ('near_64', '中 ' * 64), ('near_65', '中 ' * 65),
        ('grow', '中 ' * 150), ('multiple_batches', '中 ' * 1100),
    )
    statements, labels = [], []
    for mode in ('smart', 'max_word'):
        for name, sample in samples:
            label = mode + '_' + name
            literal = "_utf8mb4 X'" + sample.encode('utf-8').hex() + "'" if sample else "''"
            options = '[{"output":"all"},{"additional_args":[{"ik_mode":"' + mode + '"}]}]'
            statements.append(f"SELECT '{label}', TOKENIZE({literal},'ik','{options}');")
            labels.append(label)
    return '\n'.join(statements), labels


def matrix_work(call, out):
    query, labels = matrix_sql()
    raw = driver.sql(call, out, 'ik-result-container-matrix', query)
    rows = [line.split('\t') for line in raw.splitlines()]
    driver.require(len(rows) == len(labels) and [row[0] for row in rows] == labels
                   and all(len(row) == 2 for row in rows),
                   'Incomplete IK result-container matrix')
    return dict(canonical=raw, rows=len(rows), modes=2,
                sqlSha256=hashlib.sha256(query.encode()).hexdigest(),
                outputSha256=hashlib.sha256(raw.encode()).hexdigest())


def equality_work(call, out):
    result = corrected_equality_work(call, out)
    matrix = matrix_work(call, out)
    payload = bytes.fromhex(result['canonical']) + matrix['canonical'].encode()
    result.update(canonical=payload.hex(), outputBytes=len(payload),
                  outputSha256=hashlib.sha256(payload).hexdigest())
    result['ikResultMatrix'] = {key: value for key, value in matrix.items() if key != 'canonical'}
    return result


def prebuild(args):
    ORIGINAL_PREBUILD(args)
    report = json.loads((args.root / 'report.json').read_text())
    identity = json.loads((args.root / 'runtime/baseline/identity.json').read_text())
    row = driver.run_owned(args, report, {'baseline': identity}, 'baseline', 'ik-matrix-prebuild',
                           lambda call, out, pid: dict(passed=True, **matrix_work(call, out)))
    common.require_instance(row)
    report['ikMatrixPrebuild'] = {key: value for key, value in row['work'].items() if key != 'canonical'}
    driver.dump(args.root / 'report.json', report)


def build_hint_proof(log_text, role):
    result = sort_validation.build_hint_proof(log_text, 'baseline')
    result['role'] = role
    return result


def configure():
    for key, value in PIN.items():
        setattr(validation, key, value)
        setattr(driver, key, value)
    validation.source_proof = driver.source_proof = source_proof
    validation.build_profile = driver.build_profile = build_profile
    validation.prebuild = driver.prebuild = prebuild
    validation.build_hint_proof = build_hint_proof
    validation.equality_work = equality_work


if __name__ == '__main__':
    configure()
    raise SystemExit(validation.main())
