"""Validate execution-owned word tables, character fusion and batch decoding."""
import hashlib
import json

import fts_ddl_row_cache_validation as fixtures


validation, driver = fixtures.validation, fixtures.driver
PRODUCTS = (
    'src/data_plane/api/data_plane/fts/ft_local_word_map.h',
    'src/data_plane/api/data_plane/fts/ob_fts_parser.h',
    'src/data_plane/api/data_plane/fts/ob_fts_parser_helper.h',
    'src/share/text_analysis/ob_token_stream.cpp',
    'src/share/text_analysis/ob_token_stream.h',
    'src/sql/das/ob_das_domain_utils.cpp',
    'src/sql/das/ob_das_domain_utils.h',
    'src/sql/engine/expr/ob_expr_tokenize.cpp',
    'src/sql/engine/expr/ob_expr_tokenize.h',
    'src/observer/composition/retrieval/ob_das_legacy_tr_merge_iter.cpp',
    'src/storage/retrieval/ob_text_daat_iter.cpp',
    'src/storage/retrieval/ob_text_daat_iter.h',
    'src/storage/retrieval/ob_text_retrieval_token_iter.cpp',
    'src/storage/retrieval/ob_text_retrieval_token_iter.h',
    'src/storage/blocksstable/encoding/ob_string_diff_decoder.cpp',
    'src/storage/fts/ik/ob_ik_char_util.h',
    'src/storage/fts/ik/ob_ik_processor.cpp',
    'src/storage/fts/ik/ob_ik_processor.h',
    'src/storage/fts/ob_beng_ft_parser.cpp',
    'src/storage/fts/ob_beng_ft_parser.h',
    'src/storage/fts/ob_fts_parser_helper.cpp',
    'src/storage/fts/ob_fts_stop_word.cpp',
    'src/storage/fts/ob_fts_stop_word.h',
)
COMPILE_UNITS = tuple(name for name in PRODUCTS if name.endswith('.cpp'))


def source_proof(args):
    return dict(fixtures.ORIGINAL_SOURCE_PROOF(args, PRODUCTS),
                experiment='FTS_EXECUTION_WORD_TABLE_AND_CHARACTER_FUSION',
                archivedBinaryCommit=fixtures.previous.ARCHIVED_BASE,
                archivedBinaryTree=fixtures.previous.TREE)


def build_profile(args):
    settings, rust = fixtures.ORIGINAL_BUILD_PROFILE(args, COMPILE_UNITS)
    if args.phase == 'snapshot':
        path = args.root / 'evidence/candidate/affected-objects.json'
        records = json.loads(path.read_text())
        dependencies = '\n'.join(row['dependencyText'] for row in records)
        observed = [name for name in PRODUCTS if str(args.source / name) in dependencies]
        driver.require(set(observed) == set(PRODUCTS), 'A changed product file was not compiled')
        for row in records:
            row.pop('dependencyText')
            row['observedModifiedDependencies'] = observed
        driver.dump(path, records)
    return settings, rust


def context_sql():
    statements = [
        'CREATE DATABASE ft_context_probe;',
        'CREATE TABLE ft_context_probe.docs(id INT PRIMARY KEY, body VARCHAR(16000), '
        'parser_name VARCHAR(16), options JSON) CHARSET=utf8mb4;',
    ]
    words = ' '.join(fixtures.distinct_words())
    samples = ['', 'Alpha ALPHA alpha', 'Database DATA database', 'the AND an',
               'é e É', '数据库 Database 123.45 日本語 한국어 😀', words,
               'middle ' * 500, 'Alpha beta ALPHA gamma BETA alpha']
    values = []
    for index in range(90):
        parser = ('beng', 'ik', 'space', 'ngram', 'ngram2')[index % 5]
        options = [{'output': 'all' if index % 2 else 'default'}]
        if parser in ('beng', 'space'):
            options.append({'additional_args': [{'min_token_size': 2 + index % 4}]})
        elif parser == 'ik':
            options.append({'additional_args': [{'ik_mode': 'smart' if index % 2 else 'max_word'}]})
        options_sql = json.dumps(options, separators=(',', ':'))
        sample = samples[(index // 5) % len(samples)]
        values.append(f"({index},{fixtures.literal(sample)},'{parser}','{options_sql}')")
    statements.append('INSERT INTO ft_context_probe.docs VALUES ' + ','.join(values) + ';')
    for collation in ('utf8mb4_general_ci', 'utf8mb4_bin'):
        statements.append("SELECT id,TOKENIZE(body COLLATE " + collation +
                          ',parser_name,options) FROM ft_context_probe.docs ORDER BY id;')
    return '\n'.join(statements)


def equality_work(call, out):
    result = fixtures.equality_work(call, out)
    raw = driver.sql(call, out, 'execution-context-switching', context_sql())
    driver.require(len(raw.splitlines()) == 180, 'Incomplete context-switching result stream')
    payload = bytes.fromhex(result['canonical']) + raw.encode('utf-8')
    result.update(canonical=payload.hex(), outputBytes=len(payload),
                  outputSha256=hashlib.sha256(payload).hexdigest(), contextRows=180)
    return result


def configure():
    fixtures.configure()
    validation.source_proof = driver.source_proof = source_proof
    validation.build_profile = driver.build_profile = build_profile
    validation.equality_work = equality_work
    validation.SCHEDULE = (('A1', 'baseline'), ('B1', 'candidate'), ('B2', 'candidate'),
                           ('A2', 'baseline'), ('A3', 'baseline'), ('B3', 'candidate'),
                           ('B4', 'candidate'), ('A4', 'baseline'))
    validation.PAIRS = tuple(('A' + str(i), 'B' + str(i)) for i in range(1, 5))
    driver.SCHEDULE = validation.SCHEDULE


if __name__ == '__main__':
    configure()
    raise SystemExit(validation.main())
