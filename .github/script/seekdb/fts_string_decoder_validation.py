#!/usr/bin/env python3
"""Validate a shared string decoder change against a preserved complete build."""
import hashlib
import json
import time

import fts_ddl_row_cache_validation as row_cache


validation, driver, common = row_cache.validation, row_cache.driver, row_cache.common
BASE = '603075716540b4f14dff9e8650bbcb0d4d60d338'
TREE = '28040eace1ed1439f7e28d2dd4fc4ac7e47fe901'
PRODUCT = 'src/storage/blocksstable/encoding/ob_string_diff_decoder.cpp'
PIN = dict(
    BASE=BASE, BASE_TREE=TREE, BASE_ARCHIVE_COMMIT=BASE, BASE_ARCHIVE_TREE=TREE,
    BASE_ZIP_BYTES=329606064, BASE_TAR_BYTES=329583055,
    BASE_ZIP_SHA='486c4cfe5246b4e59295686e408ebcb2ca3c4299315a69acbae0398978a88555',
    BASE_TAR_SHA='4e9d55e3b38bdc04d0292e1fabf4d2990ff75bb829195901cc21048457b9a010',
    BASE_ELF_BYTES=1332426096,
    BASE_ELF_SHA='04fc8e7df52a9f6596d4e4666709972b5a8998684d248aa190bf25d63af5b3bd',
    BASE_BUILD_ID='356b93d1932ce02ef58ab2fe7a97de72db3b7e92',
    BASE_RUN=43667118747, BASE_ARTIFACT=14144705933,
)
ORIGINAL_SOURCE_PROOF = row_cache.ORIGINAL_SOURCE_PROOF
ORIGINAL_BUILD_PROFILE = row_cache.ORIGINAL_BUILD_PROFILE
ORIGINAL_PREBUILD = row_cache.ORIGINAL_PREBUILD
ORIGINAL_VALIDATE = validation.validate
ORIGINAL_BENCHMARK = validation.benchmark_callback


def source_proof(args):
    return dict(ORIGINAL_SOURCE_PROOF(args, (PRODUCT,)),
                experiment='STRING_DECODER_VS_PRESERVED_ROW_CACHE_BASELINE')


def build_profile(args):
    settings, rust = ORIGINAL_BUILD_PROFILE(args, (PRODUCT,))
    if args.phase == 'snapshot':
        path = args.root / 'evidence/candidate/affected-objects.json'
        records = json.loads(path.read_text())
        driver.require(any(str(args.source / PRODUCT) in row['dependencyText'] for row in records),
                       'Modified decoder is missing from compiled dependencies')
        for row in records:
            row.pop('dependencyText')
        driver.dump(path, records)
    return settings, rust


def storage_rows():
    rows = []
    for i in range(512):
        a = None if i % 17 == 0 else ('commonXY%02xsuffix' % (i % 256)).encode()
        b = None if i % 19 == 0 else ('deadbeef%04xcafefeed' % i).encode()
        if i % 23 == 0:
            c = None
        elif i % 29 == 0:
            c = ('中' * 80 + '%04d' % i).encode('utf-8')
        else:
            c = ('前%04d末' % i).encode('utf-8') + (b'\x00' if i % 31 == 0 else b'')
        rows.append((i, a, b, c))
    return rows


def storage_input():
    def literal(value):
        return 'NULL' if value is None else "X'" + value.hex() + "'"

    rows = storage_rows()
    values = ',\n'.join('(' + ','.join([str(i)] + [literal(x) for x in data]) + ')'
                         for i, *data in rows)
    setup = ('CREATE DATABASE string_decode_probe;\n'
             'CREATE TABLE string_decode_probe.t ('
             'id INT PRIMARY KEY, a VARCHAR(300), b VARBINARY(80), c VARCHAR(300)) '
             'DEFAULT CHARSET=utf8mb4 ROW_FORMAT=COMPRESSED;\n'
             'INSERT INTO string_decode_probe.t VALUES\n' + values + ';')
    expected = ''.join('\t'.join([str(i)] + ['#NULL#' if x is None else x.hex().upper()
                                               for x in data]) + '\n'
                       for i, *data in rows)
    query = ("SELECT id, IF(a IS NULL,'#NULL#',HEX(a)), IF(b IS NULL,'#NULL#',HEX(b)), "
             "IF(c IS NULL,'#NULL#',HEX(c)) FROM string_decode_probe.t ORDER BY id;")
    return setup, query, expected


def storage_work(call, out):
    setup, query, expected = storage_input()
    driver.sql(call, out, 'decoder-storage-setup', setup)
    before = driver.sql(call, out, 'decoder-storage-before', query)
    driver.require(before == expected, 'Before-freeze storage bytes differ from independently generated rows')
    before_version = driver.sql(call, out, 'decoder-major-version-before',
        'SELECT frozen_scn,last_scn FROM oceanbase.DBA_OB_MAJOR_COMPACTION ORDER BY tenant_id;')
    driver.sql(call, out, 'decoder-major-freeze', 'ALTER SYSTEM MAJOR FREEZE;')
    deadline = time.monotonic() + 150
    attempts = 0
    while True:
        status = driver.sql(call, out, 'decoder-major-version-after',
            'SELECT frozen_scn,last_scn FROM oceanbase.DBA_OB_MAJOR_COMPACTION ORDER BY tenant_id;')
        values = [line.split('\t') for line in status.splitlines()]
        old = [line.split('\t') for line in before_version.splitlines()]
        complete = (len(values) == len(old) > 0 and
                    all(len(x) == len(y) == 2 and all(z.isdigit() for z in x + y) and
                        int(x[0]) > int(y[0]) and int(x[1]) >= int(x[0])
                        for x, y in zip(values, old)))
        attempts += 1
        if complete:
            break
        driver.require(time.monotonic() < deadline, 'Major compaction did not complete within 150 seconds')
        time.sleep(2)
    after = driver.sql(call, out, 'decoder-storage-after', query)
    driver.require(after == expected, 'After-freeze storage bytes differ from independently generated rows')
    return dict(rows=len(storage_rows()), inputSha256=hashlib.sha256(setup.encode()).hexdigest(),
                outputSha256=hashlib.sha256(expected.encode()).hexdigest(),
                beforeFreezeMatches=True, afterFreezeMatches=True, majorStatusBefore=before_version,
                majorStatusAfter=status, majorStatusPolls=attempts,
                scope='Compressed-table SQL parity; codec selection is not independently established')


def equality_work(call, out):
    result = row_cache.equality_work(call, out)
    storage = storage_work(call, out)
    payload = bytes.fromhex(result['canonical']) + json.dumps(
        {key: storage[key] for key in ('rows', 'inputSha256', 'outputSha256')},
        sort_keys=True, separators=(',', ':')).encode()
    result.update(canonical=payload.hex(), outputBytes=len(payload),
                  outputSha256=hashlib.sha256(payload).hexdigest(), stringDecoderStorage=storage)
    return result


def prebuild(args):
    ORIGINAL_PREBUILD(args)
    report = json.loads((args.root / 'report.json').read_text())
    identity = json.loads((args.root / 'runtime/baseline/identity.json').read_text())
    row = driver.run_owned(args, report, {'baseline': identity}, 'baseline', 'string-decoder-prebuild',
                           lambda call, out, pid: dict(passed=True, **storage_work(call, out)))
    common.require_instance(row)
    report['stringDecoderPrebuild'] = row['work']
    driver.dump(args.root / 'report.json', report)


def benchmark_callback(args, identities, role, baseline, scorer):
    inner = ORIGINAL_BENCHMARK(args, identities, role, baseline, scorer)

    def work(call, out, pid):
        result = inner(call, out, pid)
        if not result.get('passed'):
            return result
        queries = {
            'query_cn': "SELECT id FROM fts_large_bench.docs WHERE MATCH(title, content) AGAINST('全文索引 分词器') ORDER BY id",
            'query_beng': "SELECT id FROM fts_large_bench.docs WHERE MATCH(title_en, content_en) AGAINST('database performance audit') ORDER BY id",
            'query_mixed': "SELECT id FROM fts_large_bench.docs WHERE MATCH(title, content) AGAINST('OceanBase 稳定 database') ORDER BY id",
            'query_limit': "SELECT id FROM fts_large_bench.docs WHERE MATCH(content) AGAINST('倒排索引 tokenizer') ORDER BY id LIMIT 20",
        }
        outputs = {}
        for name, query in queries.items():
            raw = driver.sql(call, out, 'decoder-full-' + name, query + ';')
            ids = raw.splitlines()
            driver.require(len(ids) == int(result['metrics'][name + '_hits']) and
                           all(value.isdigit() for value in ids) and len(ids) == len(set(ids)) and
                           [int(value) for value in ids] == sorted(int(value) for value in ids),
                           'Incomplete or unordered full MATCH result: ' + name)
            outputs[name] = dict(rows=len(ids), sha256=hashlib.sha256(raw.encode()).hexdigest())
        result['fullMatchOutputs'] = outputs
        return result
    return work


def validate(args):
    ORIGINAL_VALIDATE(args)
    report_path = args.root / 'report.json'
    report = json.loads(report_path.read_text())
    report['status'] = 'checking_full_match_outputs'
    report['correctnessPassed'] = False
    driver.dump(report_path, report)
    results = [report['candidateSmoke']['fullMatchOutputs']] + [row['fullMatchOutputs'] for row in report['rounds']]
    driver.require(len(results) == 5 and all(row == results[0] for row in results),
                   'Full MATCH document outputs differ across same-input runs')
    report.update(status='native_storage_and_original_benchmark_abba_completed', correctnessPassed=True,
                  fullMatchOutputsAgree=True, fullMatchOutputScope='Ordered document IDs after benchmark timing')
    driver.dump(report_path, report)


def configure():
    row_cache.configure()
    for key, value in PIN.items():
        setattr(validation, key, value)
        setattr(driver, key, value)
    validation.source_proof = driver.source_proof = source_proof
    validation.build_profile = driver.build_profile = build_profile
    validation.prebuild = driver.prebuild = prebuild
    validation.equality_work = equality_work
    validation.benchmark_callback = benchmark_callback
    validation.validate = validate


if __name__ == '__main__':
    configure()
    raise SystemExit(validation.main())
