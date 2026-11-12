#!/usr/bin/env python3
"""Validate COUNT-consumer rewriting against the exact complete b963 ELF."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shlex


_spec = importlib.util.spec_from_file_location(
    '_fts_count_only_parent', Path(__file__).with_name('fts_cs45_equality_validation.py'))
parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(parent)
driver = parent.driver
common = parent.common
profile = parent.profile
BASE, BASE_TREE = parent.BASE, parent.BASE_TREE
REWRITE_CPP = 'src/sql/rewrite/ob_transform_pre_process.cpp'
REWRITE_HEADER = 'src/sql/rewrite/ob_transform_pre_process.h'
PRODUCTS = (REWRITE_CPP, REWRITE_HEADER)
SCOPE = 'SAME_HOST_COUNT_CONSUMER_DEVELOPMENT_VALIDATION_NOT_FORMAL_CONTEST'
TABLE = 'fts_count_consumer.docs'
MATCH = "MATCH(body) AGAINST('database performance audit')"


def limited_count(limit, offset=0, aggregate='COUNT(*)', parent_filter='', projection='id'):
    query = f'SELECT {projection} FROM {TABLE} WHERE {MATCH} LIMIT {limit}'
    if offset:
        query += f' OFFSET {offset}'
    return f'SELECT {aggregate} FROM ({query}) counted{parent_filter};'


COUNT_CASES = (
    ('rows_remaining', (f'SELECT COUNT(*) FROM {TABLE};', 34)),
    ('null_rows', (f'SELECT COUNT(*) FROM {TABLE} WHERE body IS NULL;', 1)),
    ('empty_rows', (f"SELECT COUNT(*) FROM {TABLE} WHERE body = '';", 1)),
    ('plain_count', (f'SELECT COUNT(*) FROM {TABLE} WHERE {MATCH};', 32)),
    ('limit_2', (limited_count(2), 2)),
    ('limit_32', (limited_count(32), 32)),
    ('limit_40', (limited_count(40), 32)),
    ('limit_0', (limited_count(0), 0)),
    ('offset_30', (limited_count(40, 30), 2)),
    ('offset_33', (limited_count(40, 33), 0)),
    ('no_match', (f'SELECT COUNT(*) FROM (SELECT id FROM {TABLE} '
                  "WHERE MATCH(body) AGAINST('absentnomatchtoken') LIMIT 40) counted;", 0)),
    ('count_column', (limited_count(2, aggregate='COUNT(nullable_value)', projection='id, nullable_value'), 0)),
    ('parent_filter', (limited_count(40, parent_filter=' WHERE counted.id > 30'), 5)),
)
_values = ',\n'.join(f"({index}, 'database performance audit', NULL)" for index in range(1, 36))
COUNT_SETUP = f"""ALTER SYSTEM SET _enable_add_fulltext_index_to_existing_table = true;
CREATE DATABASE fts_count_consumer;
CREATE TABLE {TABLE} (id BIGINT PRIMARY KEY, body TEXT, nullable_value BIGINT)
DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
INSERT INTO {TABLE} VALUES
{_values},
(101, NULL, 7), (102, '', 8);
ALTER TABLE {TABLE} ADD FULLTEXT INDEX body_fts(body) WITH PARSER beng;
DELETE FROM {TABLE} WHERE id IN (1, 2, 3);
"""
COUNT_SQL = COUNT_SETUP + ''.join(
    f"SELECT '{name}';\n{query}\n" for name, (query, expected) in COUNT_CASES)
COUNT_PLAN_CASES = ('limit_2', 'limit_32', 'limit_40', 'offset_30', 'offset_33')
SCORING_CONTROLS = {
    'plain_select_limit': f'SELECT id FROM {TABLE} WHERE {MATCH} LIMIT 2;',
    'score_projection': f'SELECT id, {MATCH} AS relevance FROM {TABLE} WHERE {MATCH} LIMIT 2;',
    'score_order': f'SELECT id FROM {TABLE} WHERE {MATCH} ORDER BY {MATCH} DESC, id LIMIT 2;',
    'count_column': limited_count(2, aggregate='COUNT(nullable_value)', projection='id, nullable_value'),
    'parent_filter': limited_count(40, parent_filter=' WHERE counted.id > 30'),
}


def source_proof(args):
    source, commit = args.source, args.driver_sha
    driver.require(re.fullmatch('[0-9a-f]{40}', commit) is not None, 'Full source SHA required')
    identity = common.source_identity(source, commit)
    driver.require(common.git(source, 'rev-parse', BASE + '^{tree}').decode().strip() == BASE_TREE,
                   'Exact b963 source tree mismatch')
    changed = set(common.git(source, 'diff', '--name-only', BASE, commit, '--').decode().splitlines())
    products = {path for path in changed if not path.startswith('.github/')}
    driver.require(products == set(PRODUCTS), 'Exactly the two COUNT-consumer rewrite products are required')
    hashes = driver.inventory(source)
    case_inputs = {}
    for name in driver.CASES:
        suite, case = name.split('.')
        directory = source / 'tools/deploy/mysql_test/test_suite' / suite
        paths = (directory / 't' / (case + '.test'), directory / 'r/mysql' / (case + '.result'))
        for path in paths:
            relative = path.relative_to(source).as_posix()
            driver.require(common.git(source, 'show', BASE + ':' + relative) == path.read_bytes(),
                           'Existing selected native input changed: ' + relative)
        case_inputs[name] = dict(testSha256=common.digest(paths[0]), expectedSha256=common.digest(paths[1]))
    test = source / 'tools/deploy/mysql_test/test_suite/fts_index/t/simple_query.test'
    test_text = test.read_text(encoding='utf-8')
    explains = list(re.finditer(r'^\s*explain\b.*?;', test_text, re.I | re.M | re.S))
    count_parents = [dict(line=test_text.count('\n', 0, match.start()) + 1,
                          sql=match.group().strip()) for match in explains
                     if re.search(r'\bcount\s*\(', match.group(), re.I)
                     and re.search(r'\bfrom\s*\(\s*select\b', match.group(), re.I)
                     and re.search(r'\blimit\b', match.group(), re.I)]
    patch = common.git(source, 'diff', *driver.CANONICAL_DIFF_FLAGS, BASE, commit, '--', *PRODUCTS)
    ci_files = {path: hashlib.sha256(common.git(source, 'show', commit + ':' + path)).hexdigest()
                for path in sorted(changed - products)}
    driver.KERNEL, driver.KERNEL_TREE = commit, identity['tree']
    return dict(baselineCommit=BASE, baselineTree=BASE_TREE, kernelCommit=commit,
                kernelTree=identity['tree'], driverCommit=commit, driverTree=identity['tree'],
                productChanges=sorted(PRODUCTS), ciChanges=sorted(ci_files), ciBlobSha256=ci_files,
                canonicalFullIndexPatchSha256=hashlib.sha256(patch).hexdigest(),
                originalInputCount=75, originalInputInventorySha256=driver.INVENTORY,
                originalInputHashes=hashes, caseInputs=case_inputs,
                simpleQueryExplainAudit=dict(testSha256=common.digest(test), explainStatements=len(explains),
                                             countParentsWithDerivedLimit=count_parents),
                commitObjects={commit: driver.git_identity(source, commit)})


_prepare = parent.prepare


def prepare(args):
    _prepare(args)
    plan = json.loads((args.root / 'plan.json').read_text())
    plan.update(scope=SCOPE, candidateFeatures=['count_consumer_filter_only_limit'])
    driver.dump(args.root / 'plan.json', plan)
    report = json.loads((args.root / 'report.json').read_text())
    report.update(plan=plan, validationScope=SCOPE)
    driver.dump(args.root / 'report.json', report)


_build_profile = parent.build_profile


def build_profile(args):
    settings, rust = _build_profile(args)
    build = args.source / 'build_release'
    out = args.root / 'evidence/candidate' / ('profile-' + args.phase)
    original = json.loads((out / 'profile-proof.json').read_text())
    compiler = original['signature']['compilers']['CXX']['COMPILER']
    commands = json.loads(profile.regular(build / 'compile_commands.json', profile.COMMAND_CAP))
    affected = []
    for row in commands:
        unit = Path(row['file'])
        if not unit.is_absolute():
            unit = Path(row['directory']) / unit
        included = {unit.resolve()}
        if 'Unity' in unit.parts:
            for name in re.findall(r'^#include "([^"\n]+)"', unit.read_text(encoding='utf-8'), re.M):
                path = Path(name)
                included.add((path if path.is_absolute() else unit.parent / path).resolve())
        if (args.source / REWRITE_CPP).resolve() in included:
            affected.append(row)
    driver.require(len(affected) == 1, 'One actual preprocess compilation unit required')
    row = affected[0]
    argv = row.get('arguments') or shlex.split(row['command'])
    driver.require(compiler in argv[:2] and '-o' in argv, 'Preprocess command uses an unexpected compiler')
    obj = Path(argv[argv.index('-o') + 1])
    if not obj.is_absolute():
        obj = Path(row['directory']) / obj
    driver.require(obj.resolve().is_relative_to(build.resolve()), 'Preprocess object escapes the build root')
    signature = dict(compiler=compiler, compileCommand=row,
                     productSourceSha256={name: common.digest(args.source / name) for name in PRODUCTS})
    if args.phase == 'snapshot':
        prior = args.root / 'evidence/candidate/profile-prebuild/count-compilation-proof.json'
        driver.require(json.loads(prior.read_text())['signature'] == signature,
                       'Preprocess source or compilation command changed after prebuild')
        common.no_symlinks(obj)
        driver.require(obj.is_file() and obj.stat().st_nlink == 1 and obj.stat().st_size > 0,
                       'The actual unique preprocess object was not built')
        driver.dump(args.root / 'evidence/candidate/affected-count-object.json',
                    dict(source=REWRITE_CPP, path=str(obj), bytes=obj.stat().st_size,
                         sha256=common.digest(obj), compileCommand=row))
    driver.dump(out / 'count-compilation-proof.json', dict(passed=True, signature=signature))
    return settings, rust


def dependency_record(path, row, source, out):
    candidates = [Path(str(path) + '.d')]
    for directory in path.parents:
        if directory.name.endswith('.dir'):
            candidates.append(directory / 'compiler_depend.make')
            break
    resolved = path.resolve()
    targets = {resolved.as_posix()}
    for directory in (Path(row['directory']).resolve(), (source / 'build_release').resolve()):
        if resolved.is_relative_to(directory):
            targets.add(resolved.relative_to(directory).as_posix())
    headers, records = set(), []
    for index, candidate in enumerate(candidates):
        if not candidate.is_file():
            records.append(dict(path=str(candidate), exists=False))
            continue
        raw = profile.regular(candidate, profile.COMMAND_CAP)
        saved = out / ('preprocess-dependency-' + str(index) + '.txt')
        saved.write_bytes(raw)
        matching = []
        for line in re.sub(r'\\\r?\n', ' ', raw.decode('utf-8')).splitlines():
            if ': ' in line:
                target, inputs = line.split(': ', 1)
                if target.replace('\\ ', ' ') in targets:
                    matching.append(line)
                    for name in shlex.split(inputs):
                        value = Path(name)
                        headers.add((value if value.is_absolute() else Path(row['directory']) / value).resolve())
        records.append(dict(path=str(candidate), exists=True, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                            rawEvidencePath=str(saved), unfoldedMatchingRules=matching))
        if (source / REWRITE_HEADER).resolve() in headers:
            break
    return dict(records=records, header=REWRITE_HEADER,
                headerPresent=(source / REWRITE_HEADER).resolve() in headers)


def verify_header_dependencies(args):
    snapshot = args.root / 'evidence/candidate/affected-count-object.json'
    record = json.loads(snapshot.read_text())
    proof = dict(phase='validate', snapshotSha256=common.digest(snapshot), passed=False,
                 compilationSource=REWRITE_CPP, checkingObject=record['path'])
    try:
        obj = Path(record['path'])
        common.no_symlinks(obj)
        driver.require(obj.is_file() and obj.stat().st_nlink == 1 and obj.stat().st_size == record['bytes']
                       and common.digest(obj) == record['sha256'], 'Saved preprocess object changed')
        proof['objectSha256'] = record['sha256']
        proof['dependencies'] = dependency_record(obj, record['compileCommand'], args.source,
                                                  args.root / 'evidence/candidate')
        driver.require(proof['dependencies']['headerPresent'],
                       'Actual preprocess compiler dependencies must include its modified header')
        proof['passed'] = True
    finally:
        driver.dump(args.root / 'evidence/candidate/count-header-dependencies.json', proof)


def check_count_plan(text, expected_relevance, require_unordered):
    parsed = common.plan_relevance(text)
    annotations = text.split('Outputs & filters:', 1)[1]
    blocks = list(re.finditer(r'^\s*(\d+)\s+-\s+output\(', annotations, re.M))
    owned = next(annotations[block.start():blocks[index + 1].start() if index + 1 < len(blocks) else len(annotations)]
                 for index, block in enumerate(blocks) if int(block[1]) == parsed['scanId'])
    sort_keys = re.findall(r'\bsort_keys\s*\((nil|\[[^\n]*\])\)', owned)
    driver.require(len(sort_keys) == len(re.findall(r'\bsort_keys\s*\(', owned)),
                   'Malformed implicit sort-key annotation')
    index_back = re.findall(r'\bis_index_back\s*=\s*(true|false)', owned)
    driver.require(len(index_back) <= 1, 'Ambiguous retrieval index-back annotation')
    parsed.update(expectedCalcRelevance=expected_relevance, implicitSortKeys=sort_keys,
                  hasImplicitSortKeys=any(value != 'nil' for value in sort_keys),
                  actualIndexBack=None if not index_back else index_back[0] == 'true',
                  indexBackAnnotationPresent=bool(index_back), retrievalAnnotation=owned)
    driver.require(parsed['actualCalcRelevance'] == expected_relevance, 'COUNT-consumer relevance plan mismatch')
    driver.require(not require_unordered or not parsed['hasImplicitSortKeys'],
                   'COUNT-consumer plan retains implicit relevance sorting')
    return parsed


_equality_work = parent.equality_work


def equality_work(call, out):
    role = {'targeted-baseline': 'baseline', 'equality-baseline': 'baseline',
            'equality-candidate': 'candidate'}.get(out.name)
    driver.require(role is not None, 'COUNT parity must bind a named validation role')
    collation = _equality_work(call, out)
    raw = driver.sql(call, out, 'count-consumer', COUNT_SQL)
    expected = []
    for name, (query, value) in COUNT_CASES:
        expected.extend((name, str(value)))
    driver.require(raw.splitlines() == expected, 'COUNT-consumer results differ from the fixed fixture')
    plans = {}
    cases = dict(COUNT_CASES)
    queries = {name: cases[name][0] for name in COUNT_PLAN_CASES}
    queries.update(SCORING_CONTROLS)
    for name, query in queries.items():
        label = 'count-plan-' + name
        text = driver.sql(call, out, label, 'EXPLAIN ' + query)
        expected_relevance = name in SCORING_CONTROLS or role == 'baseline'
        proof = check_count_plan(text, expected_relevance, not expected_relevance)
        proof.update(sql=query, sqlSha256=hashlib.sha256(query.encode()).hexdigest(),
                     outputSha256=common.digest(out / (label + '.log')))
        plans[name] = proof
    driver.dump(out / 'count-consumer-plans.json', dict(role=role, passed=True, queries=plans))
    output = (out / 'count-consumer.log').read_bytes()
    canonical = json.dumps(dict(collation=collation['canonical'], counts=output.hex()),
                           sort_keys=True, separators=(',', ':')).encode()
    return dict(passed=True, canonical=canonical.hex(), cs45=collation,
                countOutputBytes=len(output), countOutputSha256=hashlib.sha256(output).hexdigest(),
                countSqlSha256=hashlib.sha256(COUNT_SQL.encode()).hexdigest(),
                expectedCounts={name: value for name, (query, value) in COUNT_CASES},
                countConsumerPlans=plans)


def benchmark_callback(args, identities, role, baseline, scorer):
    source = args.root / 'sources/baseline' if role == 'baseline' else args.source
    role_args = argparse.Namespace(source=source, workspace=args.source, role=role,
        expected_sha=BASE if role == 'baseline' else args.driver_sha,
        binary=Path(identities[role]['binary']), obclient=args.source / 'deps/3rd/u01/obclient/bin/obclient')
    benchmark = common.benchmark_work(role_args, baseline, scorer, False)

    def work(call, out, pid):
        result = benchmark(call, out)
        if not result.get('passed'):
            return result
        queries = common.queries_from_benchmark(source / 'tools/benchmark/fts_large_bench.sh')
        plans = {}
        for name, query in common.plan_queries(queries).items():
            text = driver.sql(call, out, name + '-explain', 'EXPLAIN ' + query)
            expected = name in common.PLAN_CONTROLS or (name == 'query_limit' and role == 'baseline')
            plans[name] = check_count_plan(text, expected, not expected)
        result['roleConsistentPlans'] = plans
        return result
    return work


_prebuild = parent.prebuild


def prebuild(args):
    import fts_expr_cache_targeted as targeted
    original = targeted.run

    def run(call, out, args, sql):
        result = original(call, out, args, sql)
        result['countOnlyPreflight'] = equality_work(call, out)
        return result

    targeted.run = run
    try:
        return _prebuild(args)
    finally:
        targeted.run = original


_validate = parent.validate


def validate(args):
    verify_header_dependencies(args)
    _validate(args)
    report = json.loads((args.root / 'report.json').read_text())
    preflight = report['targeted']['baseline']['countOnlyPreflight']
    driver.require(preflight['canonical'] == report['equality']['baseline']['canonical'],
                   'Baseline COUNT prebuild SQL differs from the later fresh-instance output')
    report.update(validationScope=SCOPE, baselineCountPreflightPassed=True,
                  countOnlySqlParityPassed=True, countOnlyPlanProofPassed=True)
    driver.dump(args.root / 'report.json', report)


parent.source_proof = source_proof
parent.equality_work = equality_work
parent.benchmark_callback = benchmark_callback
parent.validate = validate
driver.source_proof = source_proof
driver.prepare = prepare
driver.prebuild = prebuild
driver.build_profile = build_profile


def main(argv=None):
    return parent.main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
