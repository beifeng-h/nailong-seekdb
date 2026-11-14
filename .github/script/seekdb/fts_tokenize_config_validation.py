#!/usr/bin/env python3
"""Validate typed TOKENIZE configuration against the complete saved c3d7988 ELF."""
import hashlib
import json
import os
from pathlib import Path
import re

import fts_encoded_sort_validation as validation
import fts_inverted_sort_validation as previous
from fts_encoded_sort_revalidation import corrected_equality_work


driver, common = validation.driver, validation.common
PRODUCTS = (
    'src/sql/engine/expr/ob_expr_tokenize.cpp',
    'src/sql/engine/expr/ob_expr_tokenize.h',
    'src/storage/fts/ob_fts_parser_helper.cpp',
    'src/data_plane/api/data_plane/fts/ob_fts_parser_helper.h',
    'src/storage/fts/ob_fts_parser_property.cpp',
    'src/data_plane/api/data_plane/fts/ob_fts_parser_property.h',
)
COMPILE_UNITS = tuple(path for path in PRODUCTS if path.endswith('.cpp'))
ORIGINAL_SOURCE_PROOF = validation.source_proof
ORIGINAL_BUILD_PROFILE = validation.build_profile
ORIGINAL_PREBUILD = validation.prebuild


def source_proof(args):
    return dict(ORIGINAL_SOURCE_PROOF(args, PRODUCTS),
                experiment='TOKENIZE_TYPED_CONFIGURATION_VS_C3D7988',
                configurationValidationPreserved=True)


def build_profile(args):
    settings, rust = ORIGINAL_BUILD_PROFILE(args, COMPILE_UNITS)
    if args.phase == 'snapshot':
        path = args.root / 'evidence/candidate/affected-objects.json'
        records = json.loads(path.read_text())
        dependencies = '\n'.join(row['dependencyText'] for row in records)
        observed = [name for name in PRODUCTS if str(args.source / name) in dependencies]
        driver.require(set(observed) == set(PRODUCTS), 'A modified source/header is absent from compiler dependencies')
        for row in records:
            row.pop('dependencyText')
            row['observedModifiedDependencies'] = observed
        driver.dump(path, records)
    return settings, rust


def configuration_sql():
    statements = []
    long_text = "'" + 'database orchard ' * 64 + "'"
    texts = ("CAST(NULL AS CHAR CHARACTER SET utf8mb4)", "''", "'a A database DATABASE orchard'",
             "_utf8mb4 X'E695B0E68DAEE5BA93E6A380E7B4A2'",
             "_utf8mb4 X'6100622061'", long_text)
    for parser in ('ik', 'beng', 'space', 'ngram', 'ngram2'):
        for index, text in enumerate(texts):
            for mode in ('default', 'all'):
                label = f'{parser}_{index}_{mode}'
                statements.append(f"SELECT '{label}', TOKENIZE({text},'{parser}','[{{\"output\":\"{mode}\"}}]');")
    for parser in ('space', 'beng'):
        statements.append(f"SELECT '{parser}_bounds', TOKENIZE('a abc database extraordinary', '{parser}', "
            "'[{\"output\":\"all\"},{\"additional_args\":[{\"min_token_size\":4},{\"max_token_size\":16}]}]');")
    statements.append("SELECT 'ngram_size', TOKENIZE('database orchard','ngram', "
        "'[{\"output\":\"all\"},{\"additional_args\":[{\"ngram_token_size\":3}]}]');")
    statements.extend((
        "CREATE DATABASE tokenize_config_probe;",
        "CREATE TABLE tokenize_config_probe.docs(id INT PRIMARY KEY, body VARCHAR(2048), `parser` VARCHAR(16), `options` VARCHAR(1024));",
        "INSERT INTO tokenize_config_probe.docs VALUES (1,'a database orchard','beng','[{\"output\":\"all\"}]'),"
        "(2,'abc database','space','[{\"output\":\"default\"}]'),(3,NULL,'ngram','[{}]'),"
        "(4,'database','ngram2','[]');",
        "SELECT id,TOKENIZE(body,`parser`,`options`) FROM tokenize_config_probe.docs ORDER BY id;",
    ))
    return '\n'.join(statements)


def rejection_control(call, out, label, query, expected, pattern):
    control = common.SESSION + query + '\n'
    output = out / (label + '.log')
    (out / (label + '.sql')).write_text(control)
    receipt = common.capture_group([str(call.obclient), '-h127.0.0.1', '-P'+str(call.port),
        '-uroot', '-A', '-N', '-B', '--raw', '--default-character-set=utf8mb4', '-Doceanbase'],
        output, Path.cwd(), 30, env=os.environ.copy(), stdin=control.encode())
    rejected = output.read_text()
    driver.require(receipt['exitCode'] == 1 and receipt['childrenCleaned']
                   and re.fullmatch(pattern, rejected), 'Inherited rejection changed: ' + label)
    driver.dump(out / (label + '.json'), dict(expectedError=expected, **receipt))
    return rejected


def configuration_work(call, out):
    query = configuration_sql()
    text = common.SESSION + query + '\n'
    (out / 'typed-config-matrix.sql').write_text(text)
    output = out / 'typed-config-matrix.log'
    receipt = common.capture_group([str(call.obclient), '-h127.0.0.1', '-P'+str(call.port),
        '-uroot', '-A', '-N', '-B', '--raw', '--force', '--default-character-set=utf8mb4', '-Doceanbase'],
        output, Path.cwd(), 900, env=os.environ.copy(), stdin=text.encode())
    result = output.read_text()
    driver.require(receipt['exitCode'] == 0 and receipt['childrenCleaned']
                   and not re.search(r'^ERROR ', result, re.M), 'TOKENIZE configuration matrix failed')
    labels = re.findall(r"^SELECT '([^']+)'", query, re.M) + ['1', '2', '3', '4']
    rows = [line.split('\t') for line in result.splitlines()]
    driver.require(len(rows) == 67 and [row[0] for row in rows] == labels
                   and all(len(row) == 2 for row in rows), 'Incomplete TOKENIZE configuration matrix')
    rejected = rejection_control(call, out, 'binary-ik-null-control', "SELECT TOKENIZE(NULL,'ik');", 1235,
        r'ERROR 1235 \(0A000\) at line \d+: ik with the binary charset is not supported\n')
    rejected += rejection_control(call, out, 'repeat-ik-lob-control',
        "SELECT 'ik_5_default', TOKENIZE(REPEAT('database orchard ',64),'ik','[{\"output\":\"default\"}]');", 1210,
        r'ERROR 1210 \(HY000\) at line \d+: Invalid argument\n')
    return dict(canonical=result+rejected, sqlSha256=hashlib.sha256(query.encode()).hexdigest(),
                outputSha256=hashlib.sha256(result.encode()).hexdigest(), rows=67,
                parserCount=5, outputModes=2, inheritedBinaryIkNullError=1235,
                inheritedRepeatIkLobError=1210)


def equality_work(call, out):
    result = corrected_equality_work(call, out)
    matrix = configuration_work(call, out)
    payload = bytes.fromhex(result['canonical']) + matrix['canonical'].encode()
    result.update(canonical=payload.hex(), outputBytes=len(payload),
                  outputSha256=hashlib.sha256(payload).hexdigest())
    result['typedConfigurationMatrix'] = {key: value for key, value in matrix.items() if key != 'canonical'}
    return result


def prebuild(args):
    ORIGINAL_PREBUILD(args)
    report = json.loads((args.root / 'report.json').read_text())
    identity = json.loads((args.root / 'runtime/baseline/identity.json').read_text())
    row = driver.run_owned(args, report, {'baseline': identity}, 'baseline', 'config-baseline-prebuild',
                           lambda call, out, pid: dict(passed=True, **configuration_work(call, out)))
    common.require_instance(row)
    report['typedConfigPrebuild'] = row['work']
    driver.dump(args.root / 'report.json', report)


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
    validation.prebuild = driver.prebuild = prebuild
    validation.build_hint_proof = build_hint_proof
    validation.equality_work = equality_work


if __name__ == '__main__':
    configure()
    raise SystemExit(validation.main())
