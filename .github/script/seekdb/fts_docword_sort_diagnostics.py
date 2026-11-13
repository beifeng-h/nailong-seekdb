#!/usr/bin/env python3
"""Isolate sorting of original FTS postings using the preserved bd5d program."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

import fts_encoded_sort_diagnostics as runtime

driver, common = runtime.driver, runtime.common
ANCHOR = '2795c7c73e53303b08c2dc18c02db10ad4689f87'
ALLOWED = {
    '.github/script/seekdb/fts_docword_sort_diagnostics.py',
    '.github/script/seekdb/test_fts_docword_sort_diagnostics.py',
    '.github/workflows/fts-docword-sort-diagnostics.yml',
    '.github/workflows/fts-profile-diagnostics.yml',
}
AREAS = ('2M', '256M')
ORDERS = {'doc_word': 'doc_id, word', 'inverted': 'word, doc_id'}
STAT_COLUMNS = ('PLAN_ID', 'SQL_ID', 'EXECUTIONS', 'ELAPSED_TIME', 'CPU_TIME',
                'DISK_READS', 'DIRECT_WRITES', 'BUFFER_GETS', 'ROWS_PROCESSED',
                'APPLICATION_WAIT_TIME', 'CONCURRENCY_WAIT_TIME', 'USER_IO_WAIT_TIME')
WORKAREA_COLUMNS = ('SQL_ID', 'OPERATION_ID', 'LAST_MEMORY_USED', 'LAST_EXECUTION',
                    'OPTIMAL_EXECUTIONS', 'ONEPASS_EXECUTIONS', 'MULTIPASSES_EXECUTIONS',
                    'MAX_TEMPSEG_SIZE', 'LAST_TEMPSEG_SIZE')


def workarea_query(sql_id, plan_id):
    return ('SELECT ' + ','.join(WORKAREA_COLUMNS) + ' FROM oceanbase.V$SQL_WORKAREA '
            f"WHERE SQL_ID='{sql_id}' AND CHILD_NUMBER={plan_id} AND OPERATION_TYPE='PHY_SORT';")


def source_proof(source, commit):
    identity = common.source_identity(source, commit)
    changed = set(common.git(source, 'diff', '--name-only', ANCHOR, commit, '--').decode().splitlines())
    driver.require(changed <= ALLOWED, 'Diagnostic edits exceed the frozen driver scope')
    inputs = driver.inventory(source)
    driver.require(common.git(source, 'rev-parse', runtime.CANDIDATE + '^{tree}').decode().strip()
                   == runtime.CANDIDATE_TREE, 'Preserved ELF source tree differs')
    return dict(driverCommit=commit, driverTree=identity['tree'], driverAnchor=ANCHOR,
                elfSourceCommit=runtime.CANDIDATE, elfSourceTree=runtime.CANDIDATE_TREE,
                diagnosticChanges=sorted(changed), originalInputCount=len(inputs),
                originalInputHashes=inputs, originalInputInventorySha256=driver.INVENTORY,
                newBuilds=0, productChangeInThisExperiment=False)


def ident(value):
    driver.require(re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', value) is not None,
                   'Unexpected catalog identifier')
    return '`' + value + '`'


def parse_stats(raw):
    result = {}
    for line in raw.splitlines():
        fields = line.split('\t')
        driver.require(len(fields) == len(STAT_COLUMNS), 'Incomplete plan statistics')
        row = dict(zip(STAT_COLUMNS, fields))
        for key in STAT_COLUMNS:
            if key != 'SQL_ID':
                row[key] = int(row[key])
        driver.require(re.fullmatch('[0-9A-Fa-f]{32}', row['SQL_ID']) is not None
                       and row['PLAN_ID'] not in result, 'Invalid or duplicate plan identity')
        result[row['PLAN_ID']] = row
    return result


def stats_delta(before, after):
    changed = []
    for plan_id, row in after.items():
        previous = before.get(plan_id, {})
        delta = {key: row[key] - previous.get(key, 0) for key in STAT_COLUMNS
                 if key not in ('PLAN_ID', 'SQL_ID')}
        if delta['EXECUTIONS']:
            driver.require(not previous or previous['SQL_ID'] == row['SQL_ID'], 'Plan identity changed')
            changed.append(dict(PLAN_ID=plan_id, SQL_ID=row['SQL_ID'], **delta))
    driver.require(len(changed) == 1 and changed[0]['EXECUTIONS'] == 1
                   and changed[0]['ELAPSED_TIME'] > 0 and changed[0]['CPU_TIME'] > 0,
                   'Expected exactly one measured plan execution delta')
    return changed[0]


def snapshot(call, out, label):
    raw = driver.sql(call, out, label, 'SELECT ' + ','.join(STAT_COLUMNS) +
        ' FROM oceanbase.V$OB_PLAN_CACHE_PLAN_STAT '
        "WHERE QUERY_SQL LIKE 'SELECT /*+%FROM fts_docword_probe.%';")
    return parse_stats(raw)


def parse_workarea(raw, sql_id):
    rows = [line.split('\t') for line in raw.splitlines()]
    driver.require(len(rows) == 1 and len(rows[0]) == len(WORKAREA_COLUMNS),
                   'Expected one SORT workarea record')
    result = dict(zip(WORKAREA_COLUMNS, rows[0]))
    driver.require(result['SQL_ID'] == sql_id and result['LAST_EXECUTION'] in
                   ('OPTIMAL', 'ONE PASS', 'MULTI-PASS'), 'Workarea identity or execution class differs')
    for key in WORKAREA_COLUMNS:
        if key not in ('SQL_ID', 'LAST_EXECUTION'):
            result[key] = None if result[key] == 'NULL' else int(result[key])
    return result


def measure(call, out, pid, table, order, enabled, tag, rows):
    flag = str(enabled).lower()
    query = (f"SELECT /*+ NO_REWRITE FULL(t) PARALLEL(1) "
             f"OPT_PARAM('enable_newsort','{flag}') OPT_PARAM('ROWSETS_MAX_ROWS',256) */ /*{tag}*/ "
             'doc_id, word, word_count, doc_length '
             f'FROM fts_docword_probe.{ident(table)} t ORDER BY {ORDERS[order]};')
    # The regular client escapes binary doc IDs, so every returned row has one newline.
    sql_text = common.SESSION + query + '\n'
    (out / (tag + '.sql')).write_text(sql_text, encoding='utf-8')
    if tag.split('_')[-2] in ('0', '1'):
        plan = driver.sql(call, out, tag + '-plan', 'EXPLAIN EXTENDED ' + query)
        driver.require('SORT' in plan, 'Explicit full sort operator missing: ' + tag)
    previous = snapshot(call, out, tag + '-before')
    output = out / (tag + '.rows')
    before_cpu, before = driver.cpu_ticks(pid), time.monotonic_ns()
    receipt = common.capture_group([str(call.obclient), '-h127.0.0.1', '-P' + str(call.port),
        '-uroot', '-A', '-N', '-B', '--default-character-set=utf8mb4', '-Doceanbase'],
        output, Path.cwd(), 120, env=os.environ.copy(), stdin=sql_text.encode())
    wall_ms = (time.monotonic_ns() - before) / 1e6
    cpu_ms = (driver.cpu_ticks(pid) - before_cpu) * 1000 / os.sysconf('SC_CLK_TCK')
    if receipt['exitCode'] != 0:
        shutil.copyfile(output, out / (tag + '-failed-output.log'))
    driver.require(receipt['exitCode'] == 0 and receipt['childrenCleaned'], 'Sort query failed: ' + tag)
    raw = output.read_bytes()
    driver.require(raw.count(b'\n') == rows, 'Incomplete sorted row stream: ' + tag)
    measured = stats_delta(previous, snapshot(call, out, tag + '-after'))
    workarea_raw = driver.sql(call, out, tag + '-workarea',
                             workarea_query(measured['SQL_ID'], measured['PLAN_ID']))
    workarea = parse_workarea(workarea_raw, measured['SQL_ID'])
    return dict(tag=tag, enabled=enabled, wallMsIncludingClient=wall_ms,
                processCpuMsIncludingBackground=cpu_ms, planStats=measured, workarea=workarea,
                outputBytes=len(raw), outputSha256=hashlib.sha256(raw).hexdigest(),
                rows=rows, query=query, receipt=receipt), output


def setup(runtime_args, call, out):
    source = runtime_args.source
    # Three additional posting copies plus sustained external sorts exceed the original 2G data cap.
    driver.sql(call, out, 'diagnostic-storage-capacity',
        "ALTER SYSTEM SET datafile_maxsize='6G';\n"
        "SHOW PARAMETERS LIKE 'datafile%';\nSHOW PARAMETERS LIKE 'enable_perf_event';")
    driver.sql(call, out, 'setup', (source / 'tools/benchmark/fts_large_bench_setup.sql').read_text())
    data = subprocess.check_output([os.sys.executable, str(source / 'tools/benchmark/fts_large_bench_gen.py'),
                                   '--rows', '20000', '--batch', '500', '--seed', '42'], timeout=60)
    driver.require(hashlib.sha256(data).hexdigest() == driver.DATA_SHA, 'Original 20K input differs')
    driver.sql(call, out, 'load', data.decode())
    driver.sql(call, out, 'enable-fts', 'ALTER SYSTEM SET _enable_add_fulltext_index_to_existing_table=true;')
    for label, ddl in driver.DDL:
        driver.sql(call, out, label, 'USE fts_large_bench; ' + ddl)
    driver.sql(call, out, 'probe-db', 'CREATE DATABASE fts_docword_probe;')
    metadata = driver.sql(call, out, 'catalog',
        'SELECT t.table_name,c.column_name FROM oceanbase.__all_table t '
        'JOIN oceanbase.__all_database d ON t.database_id=d.database_id '
        'JOIN oceanbase.__all_column c ON c.table_id=t.table_id '
        "WHERE d.database_name='fts_large_bench' AND t.index_type=14 ORDER BY t.table_name,c.column_id;")
    tables = {}
    for line in metadata.splitlines():
        table, column = line.split('\t')
        tables.setdefault(table, []).append(column)
    driver.require(len(tables) == 3, 'Expected three original doc_word auxiliary tables')
    result = []
    for table, columns in sorted(tables.items()):
        fields = []
        for prefix in ('__doc_id_', '__word_segment_', '__word_count_', '__doc_length_'):
            matches = [column for column in columns if column.startswith(prefix)]
            driver.require(len(matches) == 1, 'Missing or ambiguous auxiliary column')
            fields.append(ident(matches[0]))
        name = next((key for key in ('fti_all_ik', 'fti_content_ik', 'fti_en_beng')
                     if table.endswith(key + '_fts_doc_word')), None)
        driver.require(name is not None, 'Unexpected auxiliary table')
        target = 'fts_docword_probe.' + ident(name)
        original_count = int(driver.sql(call, out, name + '-original-count',
            "SET ob_enable_index_direct_select='ON';\n"
            f'SELECT COUNT(*) FROM fts_large_bench.{ident(table)};').strip())
        driver.sql(call, out, name + '-materialize',
            f'CREATE TABLE {target} (id BIGINT AUTO_INCREMENT PRIMARY KEY, '
            'doc_id VARCHAR(16) CHARACTER SET binary NOT NULL, '
            'word VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL, '
            'word_count BIGINT, doc_length BIGINT);\n'
            "SET ob_enable_index_direct_select='ON';\n"
            f'INSERT INTO {target}(doc_id,word,word_count,doc_length) '
            "SELECT /*+ OPT_PARAM('hidden_column_visible','true') */ " + ','.join(fields) +
            f' FROM fts_large_bench.{ident(table)} ORDER BY {fields[0]}, {fields[1]};')
        stats = driver.sql(call, out, name + '-counts',
            f'SELECT COUNT(*),COUNT(DISTINCT doc_id),MIN(OCTET_LENGTH(doc_id)),MAX(OCTET_LENGTH(doc_id)), '
            f'MAX(OCTET_LENGTH(word)) FROM {target};').strip().split('\t')
        driver.require(len(stats) == 5 and int(stats[0]) == original_count > 20000
                       and stats[1:4] == ['20000','16','16'],
                       'Unexpected original postings shape')
        result.append(dict(table=name, rows=int(stats[0]), sourceTable=table,
                           documents=20000, docIdBytes=16, maximumWordBytes=int(stats[4])))
    return result


def work_factory(runtime_args, identities, role, position, baseline, scorer):
    def work(call, out, pid):
        result = dict(passed=False, position=position, measurements=[], inputTables=[],
                      formalContestRun=False, originalNineMetricResult=False, newBuilds=0,
                      scope='Materialized original postings; full scan and sort, excludes tokenization and DDL writes',
                      cpuMetric='Plan CPU_TIME is an elapsed-minus-recorded-waits estimate, not hardware CPU time',
                      diagnosticDatafileMaxsize='6G')
        try:
            result['inputTables'] = setup(runtime_args, call, out)
            driver.sql(call, out, 'statistics-schema', 'DESCRIBE oceanbase.V$OB_PLAN_CACHE_PLAN_STAT;')
            driver.sql(call, out, 'manual-workarea', "ALTER SYSTEM SET workarea_size_policy='MANUAL';")
            canonical = {}
            for area in (AREAS if position == 0 else tuple(reversed(AREAS))):
                driver.sql(call, out, 'area-' + area,
                    f"ALTER SYSTEM SET _sort_area_size='{area}';\nSHOW PARAMETERS LIKE '_sort_area_size';")
                for corpus in (result['inputTables'] if position == 0 else list(reversed(result['inputTables']))):
                    table, rows = corpus['table'], corpus['rows']
                    for order in (tuple(ORDERS) if position == 0 else tuple(reversed(ORDERS))):
                        sequence = (False, True, False, True, True, False)
                        if position:
                            sequence = tuple(not item for item in sequence)
                        for sample, enabled in enumerate(sequence):
                            tag = f'dw{position}_{table}_{order}_{area}_{sample}_{int(enabled)}'
                            record, output = measure(call, out, pid, table, order, enabled, tag, rows)
                            record.update(table=table, order=order, area=area, sample=sample,
                                          warmup=sample < 2)
                            result['measurements'].append(record)
                            key = (table, order)
                            if key not in canonical:
                                canonical[key] = record['outputSha256']
                                saved = out / (table + '-' + order + '-canonical.rows.gz')
                                with output.open('rb') as incoming, gzip.open(saved, 'wb') as outgoing:
                                    shutil.copyfileobj(incoming, outgoing)
                            driver.require(record['outputSha256'] == canonical[key],
                                           'Ordered output differs across sort paths or memory limits: ' + tag)
                            output.unlink()
                            driver.dump(out / 'docword-sort-diagnostics.json', result)
            driver.require(len(result['measurements']) == 72, 'Incomplete diagnostic schedule')
            result.update(passed=True, allRowOrdersEqual=True, significanceClaimed=False)
            return result
        finally:
            driver.dump(out / 'docword-sort-diagnostics.json', result)
    return work


ORIGINAL_PREPARE = runtime.prepare
ORIGINAL_COLLECT = driver.collect


def prepare(args, report):
    prepared = ORIGINAL_PREPARE(args, report)
    plan = json.loads((args.root / 'plan.json').read_text())
    plan['diagnosticConfig'] = dict(originalDocuments=20000, areas=list(AREAS),
        sortKeyOrders=ORDERS, measuredSamplesPerPathPerInstance=2, instances=2,
        warmupPerPathPerCell=1, newBuilds=0)
    driver.dump(args.root / 'plan.json', plan)
    return prepared


def collect(args):
    ORIGINAL_COLLECT(args)
    records = []
    for source in sorted((args.root / 'validation').glob('*/*-canonical.rows.gz')):
        destination = args.out / 'canonical-rows' / source.parent.name / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        driver.require(source.stat().st_size <= 32 * 1024**2, 'Canonical compressed rows exceed cap')
        shutil.copyfile(source, destination)
        records.append(dict(path=destination.relative_to(args.out).as_posix(),
                            bytes=source.stat().st_size, sha256=common.digest(source)))
    driver.dump(args.out / 'canonical-rows-manifest.json', records)


def main():
    runtime.SCOPE = 'PRESERVED_ELF_ORIGINAL_POSTINGS_DOCWORD_SORT_DIAGNOSTICS'
    runtime.diagnostic_source_proof = source_proof
    runtime.diagnostic_work = work_factory
    runtime.prepare = prepare
    driver.collect = collect
    runtime.main()


if __name__ == '__main__':
    main()
