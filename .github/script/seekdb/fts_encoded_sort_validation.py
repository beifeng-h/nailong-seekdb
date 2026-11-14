#!/usr/bin/env python3
"""Build one FTS sort candidate, reuse exact bd5d, and collect native/ABBA evidence."""
import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tarfile
import zipfile

import fts_expr_cache_profile as profile

_spec = importlib.util.spec_from_file_location(
    '_fts_encoded_sort_parent', Path(__file__).with_name('fts_expr_cache_validation.py'))
driver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(driver)
common = driver.common

BASE = 'bd5d28fda9552daca40ab81a2ef32b8ca663d426'
BASE_TREE = 'c82876324f044cd66f5d707815c1a3a4dbd3f448'
PRODUCT = 'src/rootserver/ddl_task/ob_ddl_common_rs_impl.cpp'
BASE_ZIP_SHA = 'aa61f704b2af0cfb070473123f984f457b9145050ac3913e90f6430cb67d8ea2'
BASE_TAR_SHA = '33df822f7840d6829b739bb2e0c568c1c492268496dfb99ced8915d107430c21'
BASE_ELF_SHA = '8b83cb8fc4b2f99196437e63b8d6a6c4c3d08443c3030942552c52d2ee0b63e3'
BASE_BUILD_ID = '66082815f1b9cf9fc5261081d5c3535471990b9a'
BASE_ELF_BYTES = 1332384520
BASE_ZIP_BYTES = 329590277
BASE_TAR_BYTES = 329567268
BASE_RUN = 43204498012
BASE_ARTIFACT = 13941216422
SCHEDULE = (('A1', 'baseline'), ('B1', 'candidate'), ('B2', 'candidate'), ('A2', 'baseline'))
PAIRS = (('A1', 'B1'), ('A2', 'B2'))
REGRESSION_SQL = """SET NAMES utf8mb4 COLLATE utf8mb4_general_ci;
SELECT 'null_null', SIGN(STRCMP(NULL, NULL));
SELECT 'null_word', SIGN(STRCMP(NULL, 'word' COLLATE utf8mb4_general_ci));
SELECT 'word_null', SIGN(STRCMP('word' COLLATE utf8mb4_general_ci, NULL));
SELECT 'empty_empty', SIGN(STRCMP('' COLLATE utf8mb4_general_ci, '' COLLATE utf8mb4_general_ci));
SELECT 'empty_word', SIGN(STRCMP('' COLLATE utf8mb4_general_ci, 'word' COLLATE utf8mb4_general_ci));
SELECT 'same_ascii', SIGN(STRCMP('word' COLLATE utf8mb4_general_ci, 'word' COLLATE utf8mb4_general_ci));
SELECT 'same_chinese', SIGN(STRCMP(_utf8mb4 X'E695B0E68DAEE5BA93' COLLATE utf8mb4_general_ci, _utf8mb4 X'E695B0E68DAEE5BA93' COLLATE utf8mb4_general_ci));
SELECT 'same_embedded_nul', SIGN(STRCMP(_utf8mb4 X'610062' COLLATE utf8mb4_general_ci, _utf8mb4 X'610062' COLLATE utf8mb4_general_ci));
SELECT 'different_embedded_nul', SIGN(STRCMP(_utf8mb4 X'610062' COLLATE utf8mb4_general_ci, _utf8mb4 X'610063' COLLATE utf8mb4_general_ci));
SELECT 'same_padding', SIGN(STRCMP('word ' COLLATE utf8mb4_general_ci, 'word ' COLLATE utf8mb4_general_ci));
SELECT 'padding_only_difference', SIGN(STRCMP('word' COLLATE utf8mb4_general_ci, 'word ' COLLATE utf8mb4_general_ci));
SELECT 'case_equal_different_bytes', SIGN(STRCMP('A' COLLATE utf8mb4_general_ci, 'a' COLLATE utf8mb4_general_ci));
SELECT 'accent_equal_different_bytes', SIGN(STRCMP(_utf8mb4 X'C3A9' COLLATE utf8mb4_general_ci, 'e' COLLATE utf8mb4_general_ci));
SELECT 'long_equal', SIGN(STRCMP(REPEAT('word', 16384) COLLATE utf8mb4_general_ci, REPEAT('word', 16384) COLLATE utf8mb4_general_ci));
SELECT 'long_different', SIGN(STRCMP(CONCAT(REPEAT('word', 16384), 'a') COLLATE utf8mb4_general_ci, CONCAT(REPEAT('word', 16384), 'b') COLLATE utf8mb4_general_ci));
SELECT 'ordered', GROUP_CONCAT(CONCAT(id, ':', HEX(word)) ORDER BY word COLLATE utf8mb4_general_ci ASC, id ASC SEPARATOR '|') FROM (
  SELECT 1 id, 'word' word UNION ALL SELECT 2, 'WORD' UNION ALL
  SELECT 3, '' UNION ALL SELECT 4, 'word ' UNION ALL
  SELECT 5, _utf8mb4 X'C3A9' UNION ALL SELECT 6, 'e' UNION ALL
  SELECT 7, _utf8mb4 X'610062' UNION ALL SELECT 8, 'word'
) samples;
"""


def source_proof(args, products=None):
    source, commit = args.source, args.driver_sha
    driver.require(re.fullmatch('[0-9a-f]{40}', commit) is not None, 'Full source SHA required')
    identity = common.source_identity(source, commit)
    driver.require(common.git(source, 'rev-parse', BASE + '^{tree}').decode().strip() == BASE_TREE,
                   'Exact bd5d source tree mismatch')
    changed = set(common.git(source, 'diff', '--name-only', BASE, commit, '--').decode().splitlines())
    product_changes = {path for path in changed if not path.startswith('.github/')}
    expected_products = {PRODUCT} if products is None else set(products)
    driver.require(product_changes == expected_products, 'Product changes exceed the selected experiment')
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
    patch = common.git(source, 'diff', *driver.CANONICAL_DIFF_FLAGS, BASE, commit, '--', *sorted(expected_products))
    ci_files = {}
    for path in sorted(changed - product_changes):
        ci_files[path] = hashlib.sha256(common.git(source, 'show', commit + ':' + path)).hexdigest()
    driver.KERNEL, driver.KERNEL_TREE = commit, identity['tree']
    driver.KERNEL, driver.KERNEL_TREE = commit, identity['tree']
    return dict(baselineCommit=BASE, baselineTree=BASE_TREE, kernelCommit=commit,
                kernelTree=identity['tree'], driverCommit=commit, driverTree=identity['tree'],
                productChanges=sorted(expected_products), ciChanges=sorted(ci_files), ciBlobSha256=ci_files,
                canonicalFullIndexPatchSha256=hashlib.sha256(patch).hexdigest(),
                originalInputCount=75, originalInputInventorySha256=driver.INVENTORY,
                originalInputHashes=hashes, caseInputs=case_inputs,
                commitObjects={commit: driver.git_identity(source, commit)})


def prepare(args):
    root = args.root
    common.no_symlinks(root)
    driver.require(not root.exists() and args.source != root and args.source not in root.parents
                   and root not in args.source.parents, 'Fresh disjoint validation root required')
    proof = source_proof(args)
    driver.require(args.artifact_zip is not None and args.artifact_zip.stat().st_size == BASE_ZIP_BYTES
                   and common.digest(args.artifact_zip) == BASE_ZIP_SHA, 'Exact bd5d ZIP mismatch')
    root.mkdir()
    for name in ('runtime/baseline', 'runtime/candidate', 'evidence/baseline',
                 'evidence/candidate', 'validation', 'sources'):
        (root / name).mkdir(parents=True)
    driver.resource_record(root / 'evidence/resources-before.json', 16 * 1024**3)
    common.git(args.source, 'worktree', 'add', '--detach', str(root / 'sources/baseline'), BASE)
    with zipfile.ZipFile(args.artifact_zip) as archive:
        driver.require(set(archive.namelist()) == {'candidate-artifact-manifest.json',
                       'candidate-identity.json', 'candidate-elf.tar.gz'}
                       and len(archive.infolist()) == 3, 'Unexpected exact bd5d ZIP inventory')
        identity_raw = archive.read('candidate-identity.json')
        original = json.loads(identity_raw)
        manifest = json.loads(archive.read('candidate-artifact-manifest.json'))
        driver.require(original['sourceCommit'] == BASE and original['sourceTree'] == BASE_TREE
                       and original['sha256'] == BASE_ELF_SHA and original['bytes'] == BASE_ELF_BYTES
                       and original['buildId'] == BASE_BUILD_ID
                       and original['officialInputHashes'] == proof['originalInputHashes']
                       and manifest['identity'] == original
                       and manifest['fullBytes'] is True and manifest['stripped'] is False
                       and manifest['truncated'] is False, 'Archived bd5d identity mismatch')
        payload = archive.read('candidate-elf.tar.gz')
        driver.require(len(payload) == BASE_TAR_BYTES and hashlib.sha256(payload).hexdigest() == BASE_TAR_SHA,
                       'Exact bd5d tar identity mismatch')
    baseline = root / 'runtime/baseline/seekdb'
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
        members = archive.getmembers()
        driver.require(len(members) == 1 and members[0].name == 'candidate/seekdb'
                       and members[0].isreg() and members[0].size == BASE_ELF_BYTES,
                       'Expected one regular exact bd5d ELF member')
        with archive.extractfile(members[0]) as source, baseline.open('xb') as output:
            shutil.copyfileobj(source, output)
    baseline.chmod(0o555)
    driver.safe_elf(baseline, BASE_ELF_SHA, BASE_ELF_BYTES, BASE_BUILD_ID, BASE)
    (root / 'evidence/baseline/archived-identity.json').write_bytes(identity_raw)
    driver.dump(root / 'runtime/baseline/identity.json', dict(
        original, role='baseline', binary=str(baseline), archiveRole='candidate',
        archiveRun=BASE_RUN, archiveId=BASE_ARTIFACT, archiveZipSha256=BASE_ZIP_SHA))
    plan = dict(scope='SAME_HOST_ENCODED_SORT_DEVELOPMENT_VALIDATION_NOT_FORMAL_CONTEST', proof=proof,
                baselineCommit=BASE, baselineRebuilt=False, candidateBuildsRequired=1,
                formalContestRun=False, formalSubmissionsConsumedBeforeThisRun=11,
                schedule=[dict(name=name, role=role) for name, role in SCHEDULE],
                benchmarkConfig=common.CONFIG, validationWorkLimitSeconds=7200,
                baselineArtifact=dict(run=BASE_RUN, id=BASE_ARTIFACT, zipSha256=BASE_ZIP_SHA))
    driver.dump(root / 'plan.json', plan)
    driver.dump(root / 'report.json', dict(status='prepared', formalContestRun=False,
                baselineRebuilt=False, instances=[], nativeCases=[], targeted={}, rounds=[], plan=plan))


def build_profile(args, products=None):
    build = args.source / 'build_release'
    out = args.root / 'evidence/candidate' / ('profile-' + args.phase)
    out.mkdir(parents=True, exist_ok=True)
    cache_raw = profile.regular(build / 'CMakeCache.txt', profile.CACHE_CAP)
    cache = profile.cache_values(cache_raw)
    driver.require(all(cache.get(key) == value for key, value in profile.EXPECTED.items())
                   and cache.get('CMAKE_GENERATOR') == 'Unix Makefiles'
                   and cache.get('CMAKE_EXPORT_COMPILE_COMMANDS') == 'ON', 'Original build profile changed')
    (out / 'CMakeCache.txt').write_bytes(cache_raw)
    compilers = {}
    for language, executable in (('C', 'clang-17'), ('CXX', 'clang++-17')):
        found = list((build / 'CMakeFiles').glob('*/CMake' + language + 'Compiler.cmake'))
        driver.require(len(found) == 1, 'One generated compiler record required')
        raw = profile.regular(found[0], profile.RECORD_CAP)
        record = profile.compiler_values(raw, language)
        compiler = args.source / 'deps/3rd/usr/local/oceanbase/devtools/bin' / executable
        driver.require(record == dict(COMPILER=str(compiler), COMPILER_ID='Clang',
                       COMPILER_VERSION='17.0.6'), 'Original generated Clang identity changed')
        version = subprocess.check_output([str(compiler), '--version'], timeout=30).decode()
        driver.require(re.search(r'\bclang version 17\.0\.6(?:\s|$)', version), 'Actual Clang version mismatch')
        (out / ('CMake' + language + 'Compiler.cmake')).write_bytes(raw)
        (out / (language + '-version.log')).write_text(version)
        compilers[language] = record
    commands_raw = profile.regular(build / 'compile_commands.json', profile.COMMAND_CAP)
    commands = json.loads(commands_raw)
    driver.require(isinstance(commands, list) and 0 < len(commands) <= 50000, 'Invalid command database')
    targets = (PRODUCT,) if products is None else tuple(products)
    affected = {name: [] for name in targets}
    for row in commands:
        unit = Path(row['file'])
        if not unit.is_absolute():
            unit = Path(row['directory']) / unit
        includes = []
        if 'Unity' in unit.parts:
            includes = re.findall(r'^#include "([^"\n]+)"', unit.read_text(encoding='utf-8'), re.M)
        for name in targets:
            if unit.resolve() == (args.source / name).resolve() or str(args.source / name) in includes:
                affected[name].append(row)
    driver.require(all(len(rows) == 1 for rows in affected.values()),
                   'One actual compilation unit required for each modified source')
    selected = {name: rows[0] for name, rows in affected.items()}
    rust = subprocess.check_output(['rustc', '--version'], cwd=args.source / 'rust', timeout=30).decode().strip()
    driver.require(rust.startswith('rustc 1.98.1 '), 'Original Rust 1.98.1 required')
    signature = dict(settings=profile.EXPECTED, compilers=compilers, rustVersion=rust)
    if products is None:
        signature['affectedCommand'] = selected[PRODUCT]
    else:
        signature['affectedCommands'] = selected
    prior = args.root / 'evidence/candidate/profile-prebuild/profile-proof.json'
    if args.phase == 'snapshot':
        driver.require(json.loads(prior.read_text())['signature'] == signature, 'Compiler/profile changed after prebuild')
        objects = []
        for name, row in selected.items():
            argv = row.get('arguments') or shlex.split(row['command'])
            driver.require(compilers['CXX']['COMPILER'] in argv[:2] and '-o' in argv,
                           'Affected command uses an unexpected compiler')
            object_path = Path(argv[argv.index('-o') + 1])
            if not object_path.is_absolute():
                object_path = Path(row['directory']) / object_path
            driver.require(object_path.resolve().is_relative_to(build.resolve()), 'Object escapes build root')
            common.no_symlinks(object_path)
            driver.require(object_path.is_file() and object_path.stat().st_size > 0, 'Affected object was not built')
            record = dict(product=name, path=str(object_path), bytes=object_path.stat().st_size,
                          sha256=common.digest(object_path), compileCommand=row)
            if products is not None:
                dependency = Path(argv[argv.index('-MF') + 1]) if '-MF' in argv else Path(str(object_path)+'.d')
                if not dependency.is_absolute():
                    dependency = Path(row['directory']) / dependency
                raw = profile.regular(dependency, 8 * 1024**2)
                record['dependency'] = dict(path=str(dependency), bytes=len(raw),
                    sha256=hashlib.sha256(raw).hexdigest(), sourceFiles=[target for target in targets
                    if str(args.source / target) in raw.decode()], wholeFileCopied=False)
                record['dependencyText'] = raw.decode()
            objects.append(record)
        if products is None:
            driver.dump(args.root / 'evidence/candidate/affected-object.json', objects[0])
        else:
            driver.dump(args.root / 'evidence/candidate/affected-objects.json', objects)
    driver.dump(out / 'profile-proof.json', dict(passed=True, signature=signature,
                commandDatabaseSha256=hashlib.sha256(commands_raw).hexdigest()))
    return profile.EXPECTED.copy(), rust


def equality_work(call, out):
    raw = driver.sql(call, out, 'encoded_sort-equality', REGRESSION_SQL)
    rows = [line.split('\t') for line in raw.splitlines()]
    expected_labels = re.findall(r"^SELECT '([^']+)'", REGRESSION_SQL, re.M)
    driver.require(len(rows) == len(expected_labels) and [row[0] for row in rows] == expected_labels
                   and all(len(row) == 2 for row in rows), 'Incomplete collation SQL parity output')
    ddl_sql = ["ALTER SYSTEM SET _enable_add_fulltext_index_to_existing_table=true;",
               'CREATE DATABASE fts_sort_ddl;']
    for suffix, charset, collation in (
            ('ci', 'utf8mb4', 'utf8mb4_general_ci'),
            ('bin', 'utf8mb4', 'utf8mb4_bin'),
            ('latin1', 'latin1', 'latin1_swedish_ci')):
        table = 'fts_sort_ddl.docs_' + suffix
        ddl_sql.append(f'CREATE TABLE {table} (id BIGINT PRIMARY KEY, body TEXT) '
                       f'CHARSET={charset} COLLATE={collation};')
        values = ','.join(f"({i},'{('database audit' if i % 2 == 0 else 'orchard zebra')}')"
                          for i in range(1, 65))
        ddl_sql.extend((f'INSERT INTO {table} VALUES {values};',
            f'ALTER TABLE {table} ADD FULLTEXT INDEX ft_{suffix}(body) WITH PARSER beng;',
            f"SELECT COUNT(*) FROM {table} WHERE MATCH(body) AGAINST('database');",
            f'DELETE FROM {table} WHERE id=2;',
            f"UPDATE {table} SET body='orchard zebra' WHERE id=4;",
            f"INSERT INTO {table} VALUES (101,'database audit');",
            f"SELECT COUNT(*) FROM {table} WHERE MATCH(body) AGAINST('database');"))
    ddl_output = driver.sql(call, out, 'fts-sort-ddl', '\n'.join(ddl_sql))
    driver.require(ddl_output.splitlines() == ['32', '31'] * 3, 'FTS build and DML count mismatch')
    output = (out / 'encoded_sort-equality.log').read_bytes() + ddl_output.encode()
    return dict(passed=True, canonical=output.hex(), outputBytes=len(output),
                outputSha256=hashlib.sha256(output).hexdigest(),
                sqlSha256=hashlib.sha256(REGRESSION_SQL.encode()).hexdigest(),
                executedSqlSha256=common.digest(out / 'encoded_sort-equality.sql'))


def build_hint_proof(log_text, role):
    lines = [line for line in log_text.splitlines()
             if 'generate_build_replica_sql' in line and 'INSERT /*+' in line]
    enabled = [line for line in lines if "opt_param('enable_newsort', 'true')" in line]
    disabled = [line for line in lines if "opt_param('enable_newsort', 'false')" in line]
    expected_suffixes = ('fti_all_ik', 'fti_all_ik_fts_doc_word', 'fti_content_ik',
                         'fti_content_ik_fts_doc_word', 'fti_en_beng', 'fti_en_beng_fts_doc_word')
    driver.require(bool(enabled) == (role == 'candidate'), 'FTS build sorting hint activation differs')
    if role == 'candidate':
        driver.require(all(any(suffix + '`' in line for line in enabled) for suffix in expected_suffixes),
                       'An original benchmark FTS auxiliary sort was not enabled')
    driver.require(bool(disabled), 'Other DDL sorts must retain the old comparison path')
    return dict(role=role, enabledCount=len(enabled), disabledCount=len(disabled), enabledSqlLines=enabled)


def native_verdict(role, case, row, source, out):
    if row['status'] == 'passed' or case != 'fts_index.simple_query':
        common.require_instance(row)
        return None
    if role == 'baseline':
        reject = out / 'mysqltest-log/simple_query.reject'
        driver.require(row['cleanupPassed'] and row['work'].get('inputsUnchanged') is True
                       and row['work'].get('exitCode') == 1
                       and common.digest(reject) == '737c4257fabf57dbd5365fdd04cfe2573206129d19991a8a1616e7e3db1e4896',
                       'Baseline native failure differs from the exact archived bd5d reject')
        proof = dict(accepted=True, scope='Exact inherited bd5d native reject bytes',
                     rejectBytes=reject.stat().st_size, rejectSha256=common.digest(reject))
    else:
        proof = driver.classify_plan_metadata_compatibility(
            source=source, out=out, role=role, case_name=case, row=row)
        driver.require(proof['accepted'] and proof['completeBytesCompared'] and row['cleanupPassed'],
                       'Native failure exceeds the inherited plan annotations')
    driver.dump(out / 'supplemental-plan-compatibility.json', proof)
    return proof


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
            raw = driver.sql(call, out, name + '-explain', 'EXPLAIN ' + query)
            parsed = common.plan_relevance(raw)
            expected = name in common.PLAN_CONTROLS
            driver.require(parsed['actualCalcRelevance'] == expected, 'Role-consistent scoring plan mismatch')
            plans[name] = dict(expectedCalcRelevance=expected, **parsed)
        log_path = out / 'database/log/seekdb.log'
        log_text = log_path.read_text(encoding='utf-8', errors='replace')
        hint_proof = build_hint_proof(log_text, role)
        driver.dump(out / 'fts-build-sort-hints.json', dict(**hint_proof,
            completeServerLogSha256AtObservation=common.digest(log_path)))
        result['ftsBuildSortHintProof'] = {key: hint_proof[key] for key in ('enabledCount', 'disabledCount')}
        result['roleConsistentPlans'] = plans
        return result
    return work


def pair_summary(rounds, baseline, scorer):
    metrics = {row['name']: row['metrics'] for row in rounds}
    pairs = []
    for a, b in PAIRS:
        reference = dict(baseline, baseline_metrics=metrics[a])
        scorer.check_config(metrics[a], reference, False)
        scorer.check_config(metrics[b], reference, False)
        score = scorer.calculate_score(metrics[b], reference)
        pairs.append(dict(baseline=a, candidate=b, unboundedImprovement=score['mean_improvement'],
                          categories=score['categories']))
    return dict(pairs=pairs, meanUnboundedImprovement=statistics.mean(
                row['unboundedImprovement'] for row in pairs), samplesPerRole=2,
                statisticalSignificanceClaimed=False, formalContestScore=False)


def validate(args):
    driver.load_plan(args)
    report = json.loads((args.root / 'report.json').read_text())
    driver.require(report['status'] == 'prepared', 'Validation may execute once per fresh root')
    identities = {role: json.loads((args.root / 'runtime' / role / 'identity.json').read_text())
                  for role in ('baseline', 'candidate')}
    for role, identity in identities.items():
        driver.safe_elf(Path(identity['binary']), identity['sha256'], identity['bytes'],
                        identity['buildId'], BASE if role == 'baseline' else args.driver_sha)
    report.update(status='running', builds=identities)
    driver.dump(args.root / 'report.json', report)
    driver.check_baseline_targeted(args, report)
    scorer = common.load_module('encoded_sort_original_scorer', args.source / 'tools/benchmark/fts_large_bench_score.py')
    baseline = json.loads((args.source / 'tools/benchmark/fts_large_bench_baseline.json').read_text())
    for role in ('baseline', 'candidate'):
        source = args.root / 'sources/baseline' if role == 'baseline' else args.source
        native = driver.native_harness.load_native(source)
        available = {case.name: case for case in native.discover_cases(source)}
        for case in driver.CASES:
            inner = driver.native_harness.original_case(argparse.Namespace(source=source), native, available[case])
            name = case if role == 'candidate' else 'native-baseline-' + case
            row = driver.run_owned(args, report, identities, role, name,
                lambda call, out, pid, inner=inner: inner(call, out), initialize=True, native=True)
            proof = native_verdict(role, case, row, source, args.root / 'validation' / name)
            report['nativeCases'].append(dict(role=role, name=case, rawNativeStatus=row['status'],
                                             cleanupPassed=row['cleanupPassed'], compatibility=proof))
            driver.dump(args.root / 'report.json', report)
        row = driver.run_owned(args, report, identities, role, 'equality-' + role,
                               lambda call, out, pid: equality_work(call, out))
        common.require_instance(row)
        report.setdefault('equality', {})[role] = row['work']
    driver.require(report['equality']['baseline']['canonical'] == report['equality']['candidate']['canonical'],
                   'A/B collation SQL parity mismatch')
    smoke = driver.run_owned(args, report, identities, 'candidate', 'candidate-smoke',
                            benchmark_callback(args, identities, 'candidate', baseline, scorer))
    common.require_instance(smoke)
    report['candidateSmoke'] = smoke['work']
    for name, role in SCHEDULE:
        row = driver.run_owned(args, report, identities, role, name,
                               benchmark_callback(args, identities, role, baseline, scorer))
        common.require_instance(row)
        report['rounds'].append(dict(name=name, role=role, **row['work']))
        driver.dump(args.root / 'report.json', report)
    logs = [row.get('dbLogs', {}).get('fullServerLogs', {}) for row in report['instances']
            if row['name'] in dict(SCHEDULE)]
    driver.require(len(logs) == 4 and all(row.get('fullBytes') and not row.get('truncated') for row in logs)
                   and sum(row.get('fileCount', 0) for row in logs) >= 8,
                   'Complete raw ABBA server logs are required')
    report.update(status='native_correctness_and_original_benchmark_abba_completed',
                  correctnessPassed=True, sameHostPerformance=pair_summary(report['rounds'], baseline, scorer),
                  originalBenchmarkScorerRun=True, allOfficialInputsUnchanged=True,
                  formalContestRun=False, formalSubmissionsConsumedBeforeThisRun=11,
                  baselineRebuilt=False, performanceSignificanceClaimed=False)
    source_proof(args)
    driver.dump(args.root / 'report.json', report)


def bounded_validate(args):
    def expired(signum, frame):
        raise TimeoutError('The 120-minute validation work limit expired; clean up the owned database')
    prepare_nofile(args)
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 7200)
    try:
        return validate(args)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def prepare_nofile(args):
    import resource
    before = resource.getrlimit(resource.RLIMIT_NOFILE)
    soft, hard = before
    if soft != resource.RLIM_INFINITY and soft < 8192:
        driver.require(hard == resource.RLIM_INFINITY or hard >= 8192,
                       'The hard file-descriptor limit cannot support the owned database')
        resource.setrlimit(resource.RLIMIT_NOFILE, (8192, hard))
    after = resource.getrlimit(resource.RLIMIT_NOFILE)
    driver.dump(args.root / 'evidence' / (args.phase + '-nofile.json'),
                dict(before=list(before), after=list(after), requiredSoft=8192))
    driver.require((after[0] == resource.RLIM_INFINITY or after[0] >= 8192) and after[1] == hard,
                   'Process-local file-descriptor adjustment failed')


_parent_prebuild = driver.prebuild


def prebuild(args):
    prepare_nofile(args)
    return _parent_prebuild(args)


driver.BASE, driver.BASE_TREE = BASE, BASE_TREE
driver.BASE_ELF_SHA, driver.BASE_BUILD_ID = BASE_ELF_SHA, BASE_BUILD_ID
driver.BASE_ZIP_SHA, driver.BASE_TAR_SHA = BASE_ZIP_SHA, BASE_TAR_SHA
driver.SCHEDULE = SCHEDULE
driver.source_proof = source_proof
driver.prepare = prepare
driver.prebuild = prebuild
driver.build_profile = build_profile
driver.validate = bounded_validate


def main(argv=None):
    if sys.platform == 'linux':
        common.install_termination_handlers()
    return driver.main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
