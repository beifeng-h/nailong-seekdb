#!/usr/bin/env python3
"""Compare FTS DDL row caching with the preserved complete executable."""
import hashlib
import json

import fts_encoded_sort_validation as validation
import fts_expr_cache_targeted as targeted
import fts_ik_result_array_validation as previous
import fts_inverted_sort_validation as sort_validation
from fts_encoded_sort_revalidation import corrected_equality_work


driver, common = validation.driver, validation.common
PRODUCTS = ('src/sql/das/ob_das_domain_utils.cpp', 'src/sql/das/ob_das_domain_utils.h')
ORIGINAL_SOURCE_PROOF = validation.source_proof
ORIGINAL_BUILD_PROFILE = validation.build_profile
ORIGINAL_PREBUILD = validation.prebuild


def source_proof(args):
    return dict(ORIGINAL_SOURCE_PROOF(args, PRODUCTS),
                experiment='FTS_DDL_ROW_CACHE_VS_PRESERVED_BASELINE',
                archivedBinaryCommit=previous.ARCHIVED_BASE,
                archivedBinaryTree=previous.TREE)


def build_profile(args):
    settings, rust = ORIGINAL_BUILD_PROFILE(args, (PRODUCTS[0],))
    if args.phase == 'snapshot':
        path = args.root / 'evidence/candidate/affected-objects.json'
        records = json.loads(path.read_text())
        dependencies = '\n'.join(row['dependencyText'] for row in records)
        observed = [name for name in PRODUCTS if str(args.source / name) in dependencies]
        driver.require(set(observed) == set(PRODUCTS), 'Modified source or header was not compiled')
        for row in records:
            row.pop('dependencyText')
            row['observedModifiedDependencies'] = observed
        driver.dump(path, records)
    return settings, rust


def literal(text):
    return "_utf8mb4 X'" + text.encode('utf8').hex() + "'"


def distinct_words():
    return ['term' + chr(97 + i // 26) + chr(97 + i % 26) + 'zz' for i in range(300)]


def index_work(call, out, role):
    text_1100, text_150, text_65, text_502 = (literal('中 ' * 1100), literal('中 ' * 150),
                                              literal('华 ' * 65), literal('中 ' * 502))
    many_words = literal(' '.join(distinct_words()))
    tokenized = json.loads(driver.sql(call, out, 'ik-batch-many-tokenize',
        f'''SELECT TOKENIZE({many_words},'ik','[{{"output":"all"}}]');'''))
    tokens = tokenized['tokens']
    driver.require(isinstance(tokens, list) and len(tokens) >= 256 and
                   all(isinstance(item, dict) and len(item) == 1 for item in tokens),
                   'High-cardinality TOKENIZE fixture did not produce enough words')
    frequencies = {word: count for item in tokens for word, count in item.items()}
    driver.require(len(frequencies) == len(tokens) and
                   all(isinstance(count, int) and count > 0 for count in frequencies.values()) and
                   isinstance(tokenized['doc_len'], int) and tokenized['doc_len'] > 0,
                   'Invalid high-cardinality TOKENIZE result')
    setup = ('ALTER SYSTEM SET _enable_add_fulltext_index_to_existing_table=true;\n'
             'CREATE DATABASE ik_batch_probe;\n'
             'CREATE TABLE ik_batch_probe.docs(id BIGINT PRIMARY KEY, body TEXT) '
             'CHARSET=utf8mb4 COLLATE=utf8mb4_bin;\n'
             f'INSERT INTO ik_batch_probe.docs VALUES (1,{text_1100}),(2,{text_150}),'
             f'(3,{text_65}),(4,NULL),(5,{many_words});\n'
             'ALTER TABLE ik_batch_probe.docs ADD FULLTEXT INDEX ft(body) WITH PARSER ik;')
    driver.sql(call, out, 'ik-batch-index-setup', setup)
    table_id = driver.sql(call, out, 'ik-batch-table-id',
        "SELECT t.table_id FROM oceanbase.__all_table t JOIN oceanbase.__all_database d "
        "ON t.database_id=d.database_id WHERE d.database_name='ik_batch_probe' AND t.table_name='docs';").strip()
    driver.require(table_id.isdecimal(), 'Missing IK batch base table')
    catalog = driver.sql(call, out, 'ik-batch-catalog',
        f"SELECT 'aux',CAST(index_type AS CHAR),table_name FROM oceanbase.__all_table "
        f'WHERE data_table_id={table_id} AND index_type IN (13,14) UNION ALL '
        f"SELECT 'col','0',column_name FROM oceanbase.__all_column WHERE table_id={table_id} "
        "AND (column_name LIKE '__doc_id_%' OR column_name LIKE '__word_segment_%' "
        "OR column_name LIKE '__word_count_%' OR column_name LIKE '__doc_length_%');")
    catalog_rows = [row.split('\t') for row in catalog.splitlines()]
    driver.require(all(len(row) == 3 for row in catalog_rows), 'Malformed IK index catalog')
    aux = {row[1]: row[2] for row in catalog_rows if row[0] == 'aux'}
    driver.require(set(aux) == {'13', '14'} and sum(row[0] == 'aux' for row in catalog_rows) == 2,
                   'Missing IK index layouts')
    columns = [row[2] for row in catalog_rows if row[0] == 'col']
    selected = {}
    for prefix in ('__doc_id_', '__word_segment_', '__word_count_', '__doc_length_'):
        matches = [name for name in columns if name.startswith(prefix)]
        driver.require(len(matches) == 1, 'Missing or ambiguous IK generated column')
        selected[prefix] = targeted.ident(matches[0])
    doc, word = selected['__doc_id_'], selected['__word_segment_']
    count, length = selected['__word_count_'], selected['__doc_length_']
    results = {}
    for phase in ('built', 'mutated'):
        if phase == 'mutated':
            driver.sql(call, out, 'ik-batch-index-mutate',
                       f'UPDATE ik_batch_probe.docs SET body={text_502} WHERE id=1;\n'
                       'DELETE FROM ik_batch_probe.docs WHERE id=2;')
        statements = ["SET ob_enable_index_direct_select='ON';",
            f"SELECT {targeted.HINT} 'map',HEX({doc}),CAST(id AS CHAR) FROM ik_batch_probe.docs;"]
        for layout in ('13', '14'):
            statements.append(f"SELECT {targeted.HINT} '{layout}',{word},HEX({doc}),{count},{length} "
                              f'FROM ik_batch_probe.{targeted.ident(aux[layout])};')
        statements.append("SELECT 'hits',CAST(id AS CHAR) FROM ik_batch_probe.docs "
                          f"WHERE MATCH(body) AGAINST({literal('中')}) ORDER BY id;")
        raw = driver.sql(call, out, 'ik-batch-index-' + phase, '\n'.join(statements))
        groups = {name: [] for name in ('map', '13', '14', 'hits')}
        for row in targeted.rows(raw):
            driver.require(row[0] in groups, 'Unexpected IK index result')
            groups[row[0]].append(row[1:])
        mapping = dict(groups['map'])
        expected_ids = ['1', '2', '3', '4', '5'] if phase == 'built' else ['1', '3', '4', '5']
        driver.require(len(mapping) == len(groups['map']) == len(expected_ids)
                       and sorted(mapping.values()) == expected_ids, 'Wrong IK document mapping')
        long_count = (1100 if role == 'candidate' else 3300) if phase == 'built' else (
            502 if role == 'candidate' else 1004)
        expected = [['中', '1', long_count, long_count], ['华', '3', 65, 65]]
        expected.extend([word, '5', count, tokenized['doc_len']] for word, count in frequencies.items())
        if phase == 'built':
            expected.append(['中', '2', 150, 150])
        expected.sort()
        for layout in ('13', '14'):
            driver.require(targeted.canonical_postings(groups[layout], mapping) == expected,
                           'Incorrect IK index postings or document length')
        hits = [['1'], ['2']] if phase == 'built' else [['1']]
        driver.require(groups['hits'] == hits, 'Unexpected IK MATCH rows')
        results[phase] = dict(longFrequency=long_count, documentIds=expected_ids, matchRows=hits,
                              postingsPerLayout=len(expected))
    results['distinctWords'] = len(frequencies)
    return results


def equality_work(call, out):
    result = corrected_equality_work(call, out)
    matrix = previous.matrix_work(call, out)
    beng = targeted.run(call, out, None, driver.sql)
    ik = index_work(call, out, 'baseline')
    shared = dict(matrix=matrix['canonical'], beng=beng['canonical'], ik=ik)
    payload = bytes.fromhex(result['canonical']) + json.dumps(
        shared, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    result.update(canonical=payload.hex(), outputBytes=len(payload),
                  outputSha256=hashlib.sha256(payload).hexdigest(),
                  ikMatrixRows=matrix['rows'], ikIndex=ik,
                  bengTargetedStatements=beng['statements'])
    return result


def prebuild(args):
    ORIGINAL_PREBUILD(args)
    report = json.loads((args.root / 'report.json').read_text())
    identity = json.loads((args.root / 'runtime/baseline/identity.json').read_text())
    row = driver.run_owned(args, report, {'baseline': identity}, 'baseline', 'ddl-row-cache-prebuild',
                           lambda call, out, pid: dict(passed=True,
                               matrix=previous.matrix_work(call, out),
                               ikIndex=index_work(call, out, 'baseline')))
    common.require_instance(row)
    report['ddlRowCachePrebuild'] = row['work']
    driver.dump(args.root / 'report.json', report)


def build_hint_proof(log_text, role):
    result = sort_validation.build_hint_proof(log_text, 'baseline')
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
