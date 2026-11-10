#!/usr/bin/env python3
"""Independent SQL parity fixture; import and call run(call,out,args,sql_function).

sql_function must launch a NEW client per call, preserve raw SQL/output evidence,
raise on nonzero exit, and return headerless tab-delimited stdout as str/bytes.
Run once on each fresh isolated harness server. Compare result['canonical'].
No timings, server startup, native lifecycle proof, or source modifications here.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
import re
import unittest

DB = 'fts_expr_cache_targeted'
TABLES = ('bigint_pk', 'composite_pk', 'heap_rows')
HINT = "/*+opt_param('hidden_column_visible','true')*/"
UNPROVEN = [
    'Cache pointer clearing, allocator ownership, reset/destruction and error paths.',
    'Actual cache hits or plan reuse; PREPARE exercises SQL execution only.',
    'Guaranteed overlap of concurrent clients or shared execution contexts.',
    'Physical out-of-row LOB storage; fixture proves a 12000-byte TEXT input only.',
    'All native lifecycle paths, fault injection, memory safety, or performance.',
]


def ident(value):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', value):
        raise ValueError('Unexpected catalog identifier: ' + repr(value))
    return '`' + value + '`'


def rows(raw):
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='strict')
    if not isinstance(raw, str):
        raise TypeError('SQL callback must return raw str/bytes')
    return [line.split('\t') for line in raw.splitlines() if line]


def require(ok, message):
    if not ok:
        raise AssertionError(message)


def corpus(changed=False):
    data = {1: ('alpha alpha', 'bravo'), 2: ('bravo', 'charlie'),
            3: ('', None), 4: (None, ''), 5: (None, None),
            6: ('delta', 'alpha ' * 2000)}
    if changed:
        data[1] = ('charlie charlie', 'delta')
        del data[2]
    return data


def expected_postings(changed=False):
    result = []
    for key, parts in corpus(changed).items():
        words = ' '.join(x or '' for x in parts).split()
        for word, count in Counter(words).items():
            result.append([word, str(key), count, len(words)])
    return sorted(result)


def canonical_postings(raw, mapping):
    result = []
    for row in raw:
        require(len(row) == 4, 'Malformed posting: ' + repr(row))
        word, doc, count, length = row
        require(doc in mapping, 'Posting has unmapped doc ID')
        require(count.isdecimal() and length.isdecimal(), 'Invalid posting counters')
        result.append([word, mapping[doc], int(count), int(length)])
    require(len({(r[0], r[1]) for r in result}) == len(result), 'Duplicate posting')
    return sorted(result)


def score_rows(raw, expected_ids):
    result = []
    for row in raw:
        require(len(row) == 2, 'Malformed ranking')
        try:
            score = Decimal(row[1])
        except InvalidOperation as error:
            raise AssertionError('Invalid score') from error
        require(score.is_finite() and score >= 0, 'Invalid ranking score')
        result.append([row[0], format(score.quantize(Decimal('0.000001')), 'f')])
    require(sorted(r[0] for r in result) == sorted(expected_ids), 'Wrong match IDs')
    require(result == sorted(result, key=lambda r: (-Decimal(r[1]), int(r[0]))),
            'Unexpected ranking order')
    return result


def run(call, out, args, sql_function):
    """Raises on any mismatch; callback/parent retain failed SQL evidence."""
    evidence = []
    statements = 0

    def execute(label, sql):
        nonlocal statements
        statements += sql.count(';')
        require(statements < 100, 'Fixture exceeded statement budget')
        raw = sql_function(call, out, 'expr-cache-' + label, sql)
        evidence.append({'label': label, 'sql': sql,
                         'stdout': raw.decode('utf-8') if isinstance(raw, bytes) else raw})
        return rows(raw)

    setup = ["ALTER SYSTEM SET _enable_add_fulltext_index_to_existing_table = true",
             'CREATE DATABASE ' + DB, 'USE ' + DB]
    for table in TABLES:
        pk = {'bigint_pk': ', PRIMARY KEY(k)',
              'composite_pk': ', PRIMARY KEY(tag,k)', 'heap_rows': ''}[table]
        setup += [f'CREATE TABLE {table} (k BIGINT, tag VARCHAR(20), '
                  f'title VARCHAR(100), body TEXT{pk}) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin',
                  f"INSERT INTO {table} VALUES (1,'one','alpha alpha','bravo'),"
                  "(2,'two','bravo','charlie'),(3,'three','',NULL),(4,'four',NULL,''),"
                  "(5,'five',NULL,NULL),(6,'six','delta',REPEAT('alpha ',2000))",
                  f'ALTER TABLE {table} ADD FULLTEXT INDEX ctx(title,body) WITH PARSER beng']
    execute('setup', ';\n'.join(setup) + ';')
    table_rows = execute('table-ids',
        'SELECT t.table_name,t.table_id FROM oceanbase.__all_table t '
        'JOIN oceanbase.__all_database d ON t.database_id=d.database_id '
        f"WHERE d.database_name='{DB}' AND t.table_name IN ('bigint_pk','composite_pk','heap_rows');")
    require(len(table_rows) == 3 and all(len(r) == 2 for r in table_rows), 'Missing base tables')
    tids = dict(table_rows)
    require(set(tids) == set(TABLES) and all(x.isdecimal() for x in tids.values()), 'Invalid table IDs')
    metadata = {}
    catalog_sql = []
    for table in TABLES:
        tid = tids[table]
        catalog_sql.append(
            f"SELECT '{table}','aux',CAST(index_type AS CHAR),table_name FROM oceanbase.__all_table "
            f'WHERE data_table_id={tid} AND index_type IN (13,14) UNION ALL '
            f"SELECT '{table}','col','0',column_name FROM oceanbase.__all_column "
            f"WHERE table_id={tid} AND (column_name LIKE '__doc_id_%' OR "
            "column_name LIKE '__word_segment_%' OR column_name LIKE '__word_count_%' "
            "OR column_name LIKE '__doc_length_%')")
    catalog = execute('catalog', ';\n'.join(catalog_sql) + ';')
    require(all(len(r) == 4 and r[0] in TABLES for r in catalog), 'Malformed catalog')
    for table in TABLES:
        meta = [r[1:] for r in catalog if r[0] == table]
        require(all(len(r) == 3 for r in meta), 'Malformed metadata')
        aux = {r[1]: r[2] for r in meta if r[0] == 'aux'}
        require(set(aux) == {'13', '14'} and len([r for r in meta if r[0] == 'aux']) == 2,
                'Missing/duplicate aux layouts')
        cols = [r[2] for r in meta if r[0] == 'col']
        selected = {}
        for prefix in ('__word_segment_', '__word_count_', '__doc_length_'):
            matches = [x for x in cols if x.startswith(prefix)]
            require(len(matches) == 1, 'Ambiguous generated column: ' + prefix)
            selected[prefix] = ident(matches[0])
        docs = [x for x in cols if x.startswith('__doc_id_')]
        if table == 'heap_rows':
            require(not docs, 'Heap schema differs from established basic_dml contract')
            doc = '__pk_increment'
        else:
            require(len(docs) == 1, 'Missing/ambiguous doc ID')
            doc = docs[0]
        metadata[table] = (aux, selected, ident(doc))

    canonical = {}
    for changed, phase in ((False, 'built'), (True, 'mutated')):
        if changed:
            execute('mutate', 'USE ' + DB + ';\n' + '\n'.join(
                f"UPDATE {t} SET title='charlie charlie',body='delta' WHERE k=1;"
                f'DELETE FROM {t} WHERE k=2;' for t in TABLES))
        # Tagged result sets preserve every row without relying on column headers.
        commands = ['USE ' + DB, "SET ob_enable_index_direct_select='ON'"]
        for table in TABLES:
            aux, cols, doc = metadata[table]
            commands.append(f"SELECT {HINT} '{table}:map',HEX({doc}),CAST(k AS CHAR) FROM {table}")
            for layout in ('13', '14'):
                commands.append(f"SELECT {HINT} '{table}:{layout}',{cols['__word_segment_']},"
                    f"HEX({doc}),{cols['__word_count_']},{cols['__doc_length_']} FROM {ident(aux[layout])}")
            commands.append(f"SELECT '{table}:rank',CAST(k AS CHAR),"
                f"ROUND(MATCH(title,body) AGAINST('alpha'),6) AS score FROM {table} "
                "WHERE MATCH(title,body) AGAINST('alpha') ORDER BY score DESC,k")
            commands.append(f"SELECT '{table}:count',COUNT(*) FROM {table} "
                            "WHERE MATCH(title,body) AGAINST('alpha')")
        result = execute(phase, ';\n'.join(commands) + ';')
        grouped = {f'{t}:{kind}': [] for t in TABLES for kind in ('map', '13', '14', 'rank', 'count')}
        for row in result:
            require(row[0] in grouped, 'Unknown output tag')
            grouped[row[0]].append(row[1:])
        canonical[phase] = {}
        for table in TABLES:
            mapped = grouped[table + ':map']
            require(all(len(r) == 2 for r in mapped), 'Malformed doc mapping')
            mapping = dict(mapped)
            require(len(mapping) == len(mapped) == len(corpus(changed)), 'Doc mapping not bijective')
            require(sorted(mapping.values()) == sorted(map(str, corpus(changed))), 'Wrong mapped row keys')
            postings = {layout: canonical_postings(grouped[table + ':' + layout], mapping)
                        for layout in ('13', '14')}
            require(postings['13'] == postings['14'] == expected_postings(changed),
                    'Exact postings mismatch: ' + table + '/' + phase + ' ' + repr(postings))
            ids = ['6'] if changed else ['1', '6']
            ranking = score_rows(grouped[table + ':rank'], ids)
            count = grouped[table + ':count']
            require(count == [[str(len(ids))]], 'Wrong match count')
            canonical[phase][table] = dict(postings=postings, ranking=ranking, count=len(ids))

    # One callback keeps PREPARE, parameter changes and EXECUTE in one session.
    prepared = execute('prepared-join', f'USE {DB};\n'
        'PREPARE cache_stmt FROM "SELECT a.k FROM bigint_pk a JOIN composite_pk b '
        'ON a.k=b.k WHERE MATCH(a.title,a.body) AGAINST(?) ORDER BY a.k";\n'
        "SET @q='alpha'; EXECUTE cache_stmt USING @q;\n"
        "SET @q='charlie'; EXECUTE cache_stmt USING @q;\n"
        "SET @q='alpha'; EXECUTE cache_stmt USING @q;\n"
        'DEALLOCATE PREPARE cache_stmt;')
    require(prepared == [['6'], ['1'], ['6']], 'Prepared changed-parameter/join mismatch')
    canonical['preparedJoin'] = prepared
    # Distinct SQL callbacks => distinct native clients; no promise of overlap.
    # Keep execute/evidence bookkeeping on this thread rather than sharing it.
    requests = [(f'concurrent-{word}', f'USE {DB}; SELECT k FROM bigint_pk '
                 f"WHERE MATCH(title,body) AGAINST('{word}') ORDER BY k;")
                for word in ('alpha', 'charlie')]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(sql_function, call, out, 'expr-cache-' + label, sql)
                   for label, sql in requests]
        outputs = [future.result() for future in futures]
    canonical['independentClients'] = []
    for (label, sql), raw, expected in zip(requests, outputs, ([['6']], [['1']])):
        statements += sql.count(';')
        parsed = rows(raw)
        require(parsed == expected, 'Independent client mismatch')
        evidence.append(dict(label=label, sql=sql,
                             stdout=raw.decode('utf-8') if isinstance(raw, bytes) else raw))
        canonical['independentClients'].append(parsed)
    require(statements < 100, 'Fixture exceeded statement budget')
    return dict(passed=True, canonical=canonical, evidence=evidence,
                statements=statements, unproven=UNPROVEN,
                nativeSqlExecuted=True, requiresSeparateFreshServerPerRole=True)


class PureTests(unittest.TestCase):
    def test_exact_fixture(self):
        self.assertIn(['alpha', '6', 2000, 2001], expected_postings())
        self.assertIn(['charlie', '1', 2, 3], expected_postings(True))
        self.assertFalse(any(r[1] in ('3', '4', '5') for r in expected_postings()))

    def test_identity_independence(self):
        a = canonical_postings([['alpha', 'ABC', '2', '3']], {'ABC': '1'})
        b = canonical_postings([['alpha', 'FFFF', '2', '3']], {'FFFF': '1'})
        self.assertEqual(a, b)

    def test_reject_bad_postings(self):
        for value in ([['alpha', 'unknown', '1', '1']],
                      [['alpha', 'A', '-1', '1']], [['alpha', 'A', '1']],
                      [['alpha', 'A', '1', '1']] * 2):
            with self.assertRaises(AssertionError):
                canonical_postings(value, {'A': '1'})

    def test_scores(self):
        self.assertEqual(score_rows([['6', '0.200000']], ['6']), [['6', '0.200000']])
        for value in ([['6', 'NaN']], [['6', '-1']], [['1', '1']],
                      [['6', '1'], ['6', '1']]):
            with self.assertRaises(AssertionError):
                score_rows(value, ['6'])

    def test_identifiers_and_rows(self):
        self.assertEqual(rows(b'a\tb\n'), [['a', 'b']])
        self.assertEqual(ident('__idx_123_ctx'), '`__idx_123_ctx`')
        for value in ('', 'x`', 'x;DROP', 'a.b'):
            with self.assertRaises(ValueError):
                ident(value)

    def test_full_orchestration_mock_only(self):
        calls = []

        def fake(call, out, label, sql):
            calls.append((label, sql))
            result = []
            if label.endswith('table-ids'):
                result = [[t, str(i)] for i, t in enumerate(TABLES, 100)]
            elif label.endswith('catalog'):
                for t in TABLES:
                    result.extend([[t, 'aux', n, '__idx_' + t + '_' + n] for n in ('13', '14')])
                    names = ['__word_segment_20', '__word_count_21', '__doc_length_22']
                    if t != 'heap_rows':
                        names.append('__doc_id_19')
                    result.extend([[t, 'col', '0', n] for n in names])
            elif label.endswith(('built', 'mutated')):
                changed = label.endswith('mutated')
                for t in TABLES:
                    result.extend([[t + ':map', 'DOC' + str(k), str(k)] for k in corpus(changed)])
                    for layout in ('13', '14'):
                        result.extend([[t + ':' + layout, w, 'DOC' + k, str(c), str(n)]
                                       for w, k, c, n in expected_postings(changed)])
                    ids = ['6'] if changed else ['1', '6']
                    result.extend([[t + ':rank', k, '0.100000'] for k in ids])
                    result.append([t + ':count', str(len(ids))])
            elif label.endswith('prepared-join'):
                result = [['6'], ['1'], ['6']]
            elif label.endswith('concurrent-alpha'):
                result = [['6']]
            elif label.endswith('concurrent-charlie'):
                result = [['1']]
            return '\n'.join('\t'.join(r) for r in result) + ('\n' if result else '')

        result = run(None, None, None, fake)
        self.assertTrue(result['passed'])
        self.assertEqual(len(calls), 9)
        self.assertEqual(result['statements'], sum(sql.count(';') for _, sql in calls))
        self.assertLess(result['statements'] + 3 * len(calls), 100)
        setup = calls[0][1]
        for t in TABLES:
            self.assertLess(setup.index('INSERT INTO ' + t), setup.index('ALTER TABLE ' + t))


if __name__ == '__main__':
    unittest.main()
