"""SQL differential cases for preserved-ELF FTS sort diagnostics."""
import hashlib
import json
import time

WORDS = (
    None, '', '\0', 'a', 'A', 'a ', 'a  ', ' a', 'a b', 'a  b',
    'a\0', 'a\0b', 'a\x01b', 'a\x1fb', 'a!b', 'e', 'E', 'é', 'É', 'è',
    'ß', 'ss', '中', '中国', '中文', '数据库', '倒排索引', 'tokenizer',
    '😀', '😁', '𠀀', ' ', '  ', '\t', '\n', 'z', 'Z', 'z ' * 127,
    'a' * 1024, 'a' * 1023 + 'b', '中' * 256,
)
DOC_IDS = (bytes(16), bytes([255]) * 16, bytes(range(16)),
           bytes([32]) * 16, bytes([65]) * 16, bytes([0, 255]) * 8)


def setup_sql():
    statements = [
        'CREATE DATABASE fts_sort_probe;',
        'CREATE TABLE fts_sort_probe.keys_ci (id BIGINT PRIMARY KEY, '
        'word VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci, '
        'doc_id VARCHAR(16) CHARACTER SET binary NOT NULL);',
    ]
    values = []
    for word in WORDS:
        word_sql = 'NULL' if word is None else "CONVERT(X'" + word.encode().hex() + "' USING utf8mb4)"
        for doc in DOC_IDS:
            values.append(f"({len(values) + 1},{word_sql},X'{doc.hex()}')")
    statements.append('INSERT INTO fts_sort_probe.keys_ci VALUES ' + ','.join(values) + ';')
    statements.extend((
        'CREATE TABLE fts_sort_probe.keys_bin LIKE fts_sort_probe.keys_ci;',
        'ALTER TABLE fts_sort_probe.keys_bin MODIFY word VARCHAR(1024) '
        'CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;',
        'INSERT INTO fts_sort_probe.keys_bin SELECT * FROM fts_sort_probe.keys_ci;',
    ))
    return '\n'.join(statements)


def query(table, order, enabled, batch, wide=False):
    flag = 'true' if enabled else 'false'
    projection = 'id, HEX(word), HEX(doc_id)' if wide else 'id'
    return (f"SELECT /*+ NO_REWRITE FULL(t) OPT_PARAM('enable_newsort','{flag}') "
            f"OPT_PARAM('ROWSETS_MAX_ROWS',{256 if batch else 0}) */ {projection} "
            f'FROM fts_sort_probe.{table} t ORDER BY {order};')


def compare_rows(old, new, expected):
    old_rows, new_rows = old.splitlines(), new.splitlines()
    old_ids = [int(row.split('\t', 1)[0]) for row in old_rows]
    new_ids = [int(row.split('\t', 1)[0]) for row in new_rows]
    complete = (len(old_rows) == len(new_rows) == expected
                and sorted(old_ids) == sorted(new_ids) == list(range(1, expected + 1)))
    differences = [(i, a, b) for i, (a, b) in enumerate(zip(old_rows, new_rows)) if a != b]
    return dict(passed=complete and not differences, completePermutation=complete,
                rows=expected, oldSha256=hashlib.sha256(old.encode()).hexdigest(),
                newSha256=hashlib.sha256(new.encode()).hexdigest(),
                mismatchCount=len(differences), firstMismatches=differences[:12])


def work(driver, common, call, out, pid, position):
    result = dict(passed=False, ownedPid=pid, cases=[], executionOrder=[],
                  originalNineMetricResult=False, formalContestRun=False,
                  activationProof='Hint and full extended plans retained; inspect encoded expression and rowset evidence',
                  newBuilds=0)
    driver.sql(call, out, 'setup', setup_sql())
    driver.sql(call, out, 'cpu-parameters', "SHOW PARAMETERS LIKE '_rowsets_max_rows';\n"
               "SHOW PARAMETERS LIKE '_sort_area_size';")
    size = len(WORDS) * len(DOC_IDS)
    orders = ('word ASC, doc_id ASC, id ASC', 'doc_id ASC, word ASC, id ASC',
              'word DESC, doc_id DESC, id DESC', 'doc_id DESC, word DESC, id DESC')
    try:
        for table in ('keys_ci', 'keys_bin'):
            for batch in (False, True):
                for index, order in enumerate(orders):
                    name = f'{table}-batch{int(batch)}-order{index}'
                    outputs = {}
                    plans = {}
                    for enabled in ((False, True) if position == 0 else (True, False)):
                        label = name + '-new' + str(int(enabled))
                        sql = query(table, order, enabled, batch, wide=True)
                        plans[str(enabled)] = driver.sql(call, out, label + '-plan', 'EXPLAIN EXTENDED ' + sql)
                        driver.require('SORT' in plans[str(enabled)], 'Full SORT operator missing: ' + label)
                        outputs[enabled] = driver.sql(call, out, label, sql)
                        result['executionOrder'].append(label)
                    check = compare_rows(outputs[False], outputs[True], size)
                    result['cases'].append(dict(name=name, **check,
                        oldPlanHasEncodedExpression='encode_sortkey' in plans['False'].lower(),
                        newPlanHasEncodedExpression='encode_sortkey' in plans['True'].lower()))
                    driver.dump(out / 'sort-diagnostics.json', result)

        # Preserve every small semantic counterexample before trying the disk path.
        if not all(row['passed'] for row in result['cases']):
            result.update(status='semantic_mismatch', spillAttempted=False)
            return result

        current = size
        expand = []
        for _ in range(7):
            expand.append('INSERT INTO fts_sort_probe.keys_ci '
                          f'SELECT id+{current}, word, doc_id FROM fts_sort_probe.keys_ci;')
            current *= 2
        driver.sql(call, out, 'expand', '\n'.join(expand))
        driver.sql(call, out, 'spill-setting', "ALTER SYSTEM SET _sort_area_size='2M';")
        result['spillAttempted'] = True
        for index, order in enumerate(orders[:2]):
            outputs = {}
            times = {}
            for enabled in ((False, True) if position == 0 else (True, False)):
                label = f'spill-order{index}-new{int(enabled)}'
                sql = "SET workarea_size_policy='manual';\n" + query('keys_ci', order, enabled, True)
                started = time.monotonic_ns()
                outputs[enabled] = driver.sql(call, out, label, sql)
                times[str(enabled)] = (time.monotonic_ns() - started) / 1e6
                driver.sql(call, out, label + '-workarea',
                    'SELECT SQL_ID, OPERATION_TYPE, LAST_EXECUTION, '
                    'ONEPASS_EXECUTIONS, MULTIPASSES_EXECUTIONS, MAX_TEMPSEG_SIZE '
                    "FROM oceanbase.V$SQL_WORKAREA WHERE OPERATION_TYPE LIKE '%SORT%';")
            check = compare_rows(outputs[False], outputs[True], current)
            result['cases'].append(dict(name=f'spill-order{index}', **check,
                                       elapsedMsIncludingClient=times))
        result.update(passed=all(row['passed'] for row in result['cases']),
                      status='order_comparison_completed',
                      spillVerified=False,
                      spillVerification='Inspect saved workarea records; memory setting alone is not proof')
        return result
    finally:
        driver.dump(out / 'sort-diagnostics.json', result)
