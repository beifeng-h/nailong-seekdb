#!/usr/bin/env python3
"""Validate IK charset metadata caching against the exact complete b963 ELF."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex


_spec = importlib.util.spec_from_file_location(
    '_fts_ik_charset_parent', Path(__file__).with_name('fts_cs45_equality_validation.py'))
parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(parent)
driver = parent.driver
common = parent.common
profile = parent.profile

BASE = parent.BASE
BASE_TREE = parent.BASE_TREE
PRODUCTS = (
    parent.PRODUCT,
    'src/storage/fts/ik/ob_ik_char_util.h',
    'src/storage/fts/ik/ob_ik_processor.h',
    'src/storage/fts/ik/ob_ik_processor.cpp',
    'src/storage/fts/ik/ob_ik_arbitrator.cpp',
)
COMPILATION_SOURCES = (PRODUCTS[0], PRODUCTS[3], PRODUCTS[4])
SCOPE = 'SAME_HOST_IK_CHARSET_DEVELOPMENT_VALIDATION_NOT_FORMAL_CONTEST'
LONG_SEED_HEX = 'E4B8ADE58D8EE4BABAE6B091E585B1E5928CE59BBDE4BABAE6B091E5A4A7E4BC9AE5A08220'
LONG_REPETITIONS = 128
LONG_INPUT = bytes.fromhex(LONG_SEED_HEX) * LONG_REPETITIONS
LONG_UTF8_LITERAL = "_utf8mb4 X'" + LONG_INPUT.hex().upper() + "' COLLATE utf8mb4_general_ci"
SHORT_INPUT = bytes.fromhex(LONG_SEED_HEX) * 40
SHORT_UTF8_LITERAL = "_utf8mb4 X'" + SHORT_INPUT.hex().upper() + "' COLLATE utf8mb4_general_ci"
LONG_REPEAT = f"REPEAT(_utf8mb4 X'{LONG_SEED_HEX}', {LONG_REPETITIONS})"
SHORT_REPEAT = f"REPEAT(_utf8mb4 X'{LONG_SEED_HEX}', 40)"
CAST_LONG_REPEAT = f'CAST({LONG_REPEAT} AS CHAR(5000) CHARACTER SET utf8mb4) COLLATE utf8mb4_general_ci'
SMART_PROPERTIES = '[{"additional_args":[{"ik_mode":"smart"}]}]'
MAX_WORD_PROPERTIES = '[{"additional_args":[{"ik_mode":"max_word"}]}]'

LONG_INPUT_PROBES = (
    ('repeat-below-batch-smart', f"SELECT TOKENIZE({SHORT_REPEAT}, 'ik', '{SMART_PROPERTIES}');"),
    ('literal-below-batch-smart', f"SELECT TOKENIZE({SHORT_UTF8_LITERAL}, 'ik', '{SMART_PROPERTIES}');"),
    ('literal-long-smart', f"SELECT TOKENIZE({LONG_UTF8_LITERAL}, 'ik', '{SMART_PROPERTIES}');"),
    ('cast-repeat-long-smart', f"SELECT TOKENIZE({CAST_LONG_REPEAT}, 'ik', '{SMART_PROPERTIES}');"),
)
REPEAT_SQL_CASES = (
    ('repeat-long-smart', f"SELECT TOKENIZE({LONG_REPEAT}, 'ik', '{SMART_PROPERTIES}');"),
    ('repeat-long-max-word', f"SELECT TOKENIZE({LONG_REPEAT}, 'ik', '{MAX_WORD_PROPERTIES}');"),
)
METADATA_CASE_NAME = 'long-input-metadata'
METADATA_SQL = f"""SELECT 'ik-long-input-metadata',
CHAR_LENGTH({LONG_REPEAT}), OCTET_LENGTH({LONG_REPEAT}), COLLATION({LONG_REPEAT}),
HEX({LONG_REPEAT}) = '{LONG_INPUT.hex().upper()}',
CHAR_LENGTH({CAST_LONG_REPEAT}), OCTET_LENGTH({CAST_LONG_REPEAT}), COLLATION({CAST_LONG_REPEAT}),
HEX({CAST_LONG_REPEAT}) = '{LONG_INPUT.hex().upper()}';
"""

# The first TOKENIZE argument retains its collation in type deduction. Wide
# charset cases capture the actual SQL outcome, including unsupported charsets.
# Long input includes ASCII spaces so the parser can split batches.
IK_SQL = """SET NAMES utf8mb4 COLLATE utf8mb4_general_ci;
SELECT 'ik_typed_null', TOKENIZE(CAST(NULL AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_general_ci, 'ik');
SELECT 'ik_empty', TOKENIZE(_utf8mb4 '' COLLATE utf8mb4_general_ci, 'ik');
SELECT 'ik_utf8', TOKENIZE(_utf8mb4 X'E4B8ADE69687412D3132F09F9880' COLLATE utf8mb4_general_ci, 'ik');
SELECT 'ik_embedded_nul', TOKENIZE(_utf8mb4 X'E4B8AD00410031' COLLATE utf8mb4_bin, 'ik');
SELECT 'ik_smart', TOKENIZE(_utf8mb4 X'E4B8ADE58D8EE4BABAE6B091E585B1E5928CE59BBDE4BABAE6B091E5A4A7E4BC9AE5A082', 'ik', '[{"additional_args":[{"ik_mode":"smart"}]}]');
SELECT 'ik_max_word', TOKENIZE(_utf8mb4 X'E4B8ADE58D8EE4BABAE6B091E585B1E5928CE59BBDE4BABAE6B091E5A4A7E4BC9AE5A082', 'ik', '[{"additional_args":[{"ik_mode":"max_word"}]}]');
SELECT 'ik_long_smart', TOKENIZE(__LONG_UTF8_LITERAL__, 'ik', '[{"additional_args":[{"ik_mode":"smart"}]}]');
SELECT 'ik_long_max_word', TOKENIZE(__LONG_UTF8_LITERAL__, 'ik', '[{"additional_args":[{"ik_mode":"max_word"}]}]');
SET NAMES utf8mb4 COLLATE utf8mb4_bin;
SELECT 'ik_binary_session', TOKENIZE(_utf8mb4 X'E4B8ADE69687412D3132F09F9880', 'ik');
SET NAMES utf8mb4 COLLATE utf8mb4_general_ci;
SELECT 'ik_repeated_utf8', TOKENIZE(_utf8mb4 X'E4B8ADE69687412D3132F09F9880' COLLATE utf8mb4_general_ci, 'ik');
SELECT 'ik_repeated_smart', TOKENIZE(_utf8mb4 X'E4B8ADE58D8EE4BABAE6B091E585B1E5928CE59BBDE4BABAE6B091E5A4A7E4BC9AE5A082', 'ik', '[{"additional_args":[{"ik_mode":"smart"}]}]');
""".replace('__LONG_UTF8_LITERAL__', LONG_UTF8_LITERAL)
WIDE_SQL_CASES = (
    ('utf16-valid', "SELECT TOKENIZE(_utf16 X'4E2D65870041002D00310032D83DDE00' COLLATE utf16_general_ci, 'ik');"),
    ('utf16le-valid', "SELECT TOKENIZE(_utf16le X'2D4E876541002D00310032003DD800DE' COLLATE utf16le_general_ci, 'ik');"),
    ('utf16-bin-valid', "SELECT TOKENIZE(_utf16 X'4E2D65870041002D00310032' COLLATE utf16_bin, 'ik');"),
    ('utf16le-bin-valid', "SELECT TOKENIZE(_utf16le X'2D4E876541002D0031003200' COLLATE utf16le_bin, 'ik');"),
)
MALFORMED_SQL = (
    ('utf8-truncated', "SELECT TOKENIZE(_utf8mb4 X'E4B8', 'ik');"),
    ('utf8-illegal', "SELECT TOKENIZE(_utf8mb4 X'FF41', 'ik');"),
    ('utf16-odd', "SELECT TOKENIZE(_utf16 X'0041D8', 'ik');"),
    ('utf16-unpaired', "SELECT TOKENIZE(_utf16 X'D8000041', 'ik');"),
    ('utf16le-odd', "SELECT TOKENIZE(_utf16le X'410000', 'ik');"),
    ('utf16le-unpaired', "SELECT TOKENIZE(_utf16le X'00D84100', 'ik');"),
)
# Raw SQL NULL retains binary metadata during dictionary selection.
BOUNDARY_SQL_CASES = (
    ('null-binary-input', "SET NAMES utf8mb4 COLLATE utf8mb4_general_ci;\nSELECT TOKENIZE(NULL, 'ik');"),
)


def source_proof(args):
    source, commit = args.source, args.driver_sha
    driver.require(re.fullmatch('[0-9a-f]{40}', commit) is not None, 'Full source SHA required')
    identity = common.source_identity(source, commit)
    driver.require(common.git(source, 'rev-parse', BASE + '^{tree}').decode().strip() == BASE_TREE,
                   'Exact b963 source tree mismatch')
    changed = set(common.git(source, 'diff', '--name-only', BASE, commit, '--').decode().splitlines())
    product_changes = {path for path in changed if not path.startswith('.github/')}
    driver.require(product_changes == set(PRODUCTS), 'The five approved IK and charset products are required')
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
    patch = common.git(source, 'diff', *driver.CANONICAL_DIFF_FLAGS, BASE, commit, '--', *PRODUCTS)
    ci_files = {path: hashlib.sha256(common.git(source, 'show', commit + ':' + path)).hexdigest()
                for path in sorted(changed - product_changes)}
    driver.KERNEL, driver.KERNEL_TREE = commit, identity['tree']
    return dict(baselineCommit=BASE, baselineTree=BASE_TREE, kernelCommit=commit,
                kernelTree=identity['tree'], driverCommit=commit, driverTree=identity['tree'],
                productChanges=sorted(PRODUCTS), ciChanges=sorted(ci_files), ciBlobSha256=ci_files,
                canonicalFullIndexPatchSha256=hashlib.sha256(patch).hexdigest(),
                originalInputCount=75, originalInputInventorySha256=driver.INVENTORY,
                originalInputHashes=hashes, caseInputs=case_inputs,
                commitObjects={commit: driver.git_identity(source, commit)})


_prepare = parent.prepare


def prepare(args):
    _prepare(args)
    plan = json.loads((args.root / 'plan.json').read_text())
    plan.update(scope=SCOPE, candidateFeatures=['cs45_exact_byte_equality', 'ik_charset_metadata_cache'])
    driver.dump(args.root / 'plan.json', plan)
    report = json.loads((args.root / 'report.json').read_text())
    report.update(plan=plan, validationScope=SCOPE)
    driver.dump(args.root / 'report.json', report)


_build_profile = parent.build_profile


def dependency_record(path, row, source):
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
    headers = set()
    records = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        raw = profile.regular(candidate, profile.COMMAND_CAP)
        rules = []
        matched_text = []
        for line in raw.decode('utf-8').replace('\\\n', ' ').splitlines():
            if ': ' in line:
                target, inputs = line.split(': ', 1)
                if target.replace('\\ ', ' ') in targets:
                    rules.append(inputs)
                    matched_text.append(line)
        if rules:
            for inputs in rules:
                for name in shlex.split(inputs):
                    value = Path(name)
                    headers.add((value if value.is_absolute() else Path(row['directory']) / value).resolve())
            records.append(dict(path=str(candidate), bytes=len(raw),
                                sha256=hashlib.sha256(raw).hexdigest(), matchingRules=len(rules),
                                unfoldedMatchingRules=matched_text))
            if all((source / name).resolve() in headers for name in PRODUCTS[1:3]):
                break
    expected = (PRODUCTS[1], PRODUCTS[2])
    matched = [name for name in expected if (source / name).resolve() in headers]
    driver.require(records and len(matched) == len(expected),
                   'Actual IK compiler dependencies must include both modified headers')
    return dict(records=records, headers=matched)


def build_profile(args):
    settings, rust = _build_profile(args)
    build = args.source / 'build_release'
    directory = args.root / 'evidence/candidate' / ('profile-' + args.phase)
    original = json.loads((directory / 'profile-proof.json').read_text())
    compiler = original['signature']['compilers']['CXX']['COMPILER']
    commands = json.loads(profile.regular(build / 'compile_commands.json', profile.COMMAND_CAP))
    affected = {source: [] for source in COMPILATION_SOURCES}
    for row in commands:
        unit = Path(row['file'])
        if not unit.is_absolute():
            unit = Path(row['directory']) / unit
        included = {unit.resolve()}
        if 'Unity' in unit.parts:
            for name in re.findall(r'^#include "([^"\n]+)"', unit.read_text(encoding='utf-8'), re.M):
                path = Path(name)
                included.add((path if path.is_absolute() else unit.parent / path).resolve())
        for source in COMPILATION_SOURCES:
            if (args.source / source).resolve() in included:
                affected[source].append(row)
    driver.require(all(len(rows) == 1 for rows in affected.values()),
                   'Charset, IK processor and IK arbitrator each need one actual compilation unit')
    signature = dict(compiler=compiler, commands={source: rows[0] for source, rows in affected.items()},
                     productSourceSha256={source: common.digest(args.source / source) for source in PRODUCTS})
    prior = args.root / 'evidence/candidate/profile-prebuild/ik-compilation-proof.json'
    if args.phase == 'snapshot':
        driver.require(json.loads(prior.read_text())['signature'] == signature,
                       'An affected IK compilation command or source changed after prebuild')
    objects = {}
    source_objects = {}
    for source, rows in affected.items():
        row = rows[0]
        argv = row.get('arguments') or shlex.split(row['command'])
        driver.require(compiler in argv[:2] and '-o' in argv, 'Affected IK command uses an unexpected compiler')
        path = Path(argv[argv.index('-o') + 1])
        if not path.is_absolute():
            path = Path(row['directory']) / path
        driver.require(path.resolve().is_relative_to(build.resolve()), 'Affected object escapes the build root')
        if args.phase == 'snapshot':
            common.no_symlinks(path)
            driver.require(path.is_file() and path.stat().st_nlink == 1 and path.stat().st_size > 0,
                           'An affected IK object was not built')
            key = str(path.resolve())
            source_objects[source] = key
            if key in objects:
                driver.require(objects[key]['compileCommand'] == row, 'Conflicting commands share an object output')
                objects[key]['compilationSources'].append(source)
            else:
                objects[key] = dict(compilationSources=[source], path=str(path), bytes=path.stat().st_size,
                                    sha256=common.digest(path), compileCommand=row)
    driver.dump(directory / 'ik-compilation-proof.json', dict(passed=True, signature=signature))
    if args.phase == 'snapshot':
        driver.dump(args.root / 'evidence/candidate/affected-ik-objects.json',
                    dict(objects=list(objects.values()), sourceToObject=source_objects))
    return settings, rust


def verify_header_dependencies(args):
    snapshot = args.root / 'evidence/candidate/affected-ik-objects.json'
    affected = json.loads(snapshot.read_text())
    proof = dict(phase='validate', snapshotSha256=common.digest(snapshot), passed=False, objects=[])
    required = set(COMPILATION_SOURCES[1:])
    covered = set()
    try:
        for record in affected['objects']:
            sources = set(record['compilationSources']) & required
            if sources:
                proof['checkingObject'] = record['path']
                dependencies = dependency_record(Path(record['path']), record['compileCommand'], args.source)
                proof['objects'].append(dict(compilationSources=sorted(sources), path=record['path'],
                                             objectSha256=record['sha256'], headerDependencies=dependencies))
                covered.update(sources)
        driver.require(covered == required, 'Header dependency proof must cover both IK compilation sources')
        proof['passed'] = True
    finally:
        driver.dump(args.root / 'evidence/candidate/ik-header-dependencies.json', proof)


_equality_work = parent.equality_work


def equality_work(call, out):
    outcomes = []
    cases = (LONG_INPUT_PROBES + REPEAT_SQL_CASES + ((METADATA_CASE_NAME, METADATA_SQL),)
             + WIDE_SQL_CASES + MALFORMED_SQL + BOUNDARY_SQL_CASES)
    for name, query in cases:
        text = common.SESSION + query + '\n'
        label = 'ik-charset-case-' + name
        (out / (label + '.sql')).write_text(text, encoding='utf-8')
        path = out / (label + '.log')
        result = common.capture_group(
            [str(call.obclient), '-h127.0.0.1', '-P' + str(call.port), '-uroot', '-A', '-N', '-B',
             '--raw', '--default-character-set=utf8mb4', '-Doceanbase'],
            path, Path.cwd(), 900, env=os.environ.copy(), stdin=text.encode())
        driver.require(result['exitCode'] in (0, 1) and result['childrenCleaned'],
                       'Charset-case SQL client failed outside a completed SQL outcome')
        data = path.read_bytes()
        error_code, sql_state = None, None
        if result['exitCode']:
            match = re.search(rb'ERROR (\d+) \(([A-Z0-9]+)\) at line \d+:', data)
            driver.require(match is not None, 'Charset-case SQL lacks a complete server error diagnostic')
            error_code, sql_state = int(match[1]), match[2].decode('ascii')
            driver.require(error_code not in (2002, 2003, 2006, 2013, 2055),
                           'Charset-case SQL lost its client connection')
        outcomes.append(dict(name=name, exitCode=result['exitCode'], output=data.hex(),
                             serverErrorCode=error_code, sqlState=sql_state,
                             unsupportedMessage=bool(re.search(
                                 rb'not supported|unsupported|unknown character set|unknown collation', data, re.I)),
                             outputSha256=hashlib.sha256(data).hexdigest()))
    diagnostics = dict(outcomes=outcomes, metadataVerified=False, inputBytes=len(LONG_INPUT),
                       inputCharacters=len(LONG_INPUT.decode('utf-8')), repetitions=LONG_REPETITIONS)
    driver.dump(out / 'ik-input-diagnostics.json', diagnostics)
    metadata = next(row for row in outcomes if row['name'] == METADATA_CASE_NAME)
    driver.require(metadata['exitCode'] == 0, 'REPEAT metadata SQL must execute successfully')
    fields = bytes.fromhex(metadata['output']).decode('utf-8').strip().split('\t')
    driver.require(len(fields) == 9 and fields[0] == 'ik-long-input-metadata',
                   'Incomplete REPEAT metadata output')
    counts = [str(len(LONG_INPUT.decode('utf-8'))), str(len(LONG_INPUT)), '1']
    driver.require([fields[index] for index in (1, 2, 4, 5, 6, 8)] == counts + counts,
                   'REPEAT or CAST payload differs from the generated long literal')
    diagnostics['metadataVerified'] = True
    driver.dump(out / 'ik-input-diagnostics.json', diagnostics)
    collation = _equality_work(call, out)
    raw = driver.sql(call, out, 'ik-charset', IK_SQL)
    rows = [line.split('\t') for line in raw.splitlines()]
    labels = re.findall(r"^SELECT '([^']+)'", IK_SQL, re.M)
    driver.require(len(rows) == len(labels) and [row[0] for row in rows] == labels
                   and all(len(row) == 2 for row in rows), 'Incomplete IK charset parity output')
    output = (out / 'ik-charset.log').read_bytes()
    canonical = dict(collation=collation['canonical'], ikOutput=output.hex(), sqlOutcomes=outcomes)
    encoded = json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode()
    return dict(passed=True, canonical=encoded.hex(), outputSha256=hashlib.sha256(encoded).hexdigest(),
                cs45=collation, ikOutputBytes=len(output), ikOutputSha256=hashlib.sha256(output).hexdigest(),
                ikSqlSha256=hashlib.sha256(IK_SQL.encode()).hexdigest(), charsetOutcomes=outcomes,
                longInputMetadata=dict(characters=int(fields[1]), bytes=int(fields[2]), collation=fields[3],
                                       castCharacters=int(fields[5]), castBytes=int(fields[6]), castCollation=fields[7],
                                       repeatHexMatchesLiteral=True, castHexMatchesLiteral=True),
                repeatedCallsShareConnection=True, requiredSuccessInputCharset='utf8mb4',
                capturedInputCharsets=['utf8mb4', 'utf16', 'utf16le'],
                capturedBoundaryCases=[name for name, _ in BOUNDARY_SQL_CASES])


_prebuild = parent.prebuild


def prebuild(args):
    import fts_expr_cache_targeted as targeted
    original = targeted.run

    def run(call, out, args, sql):
        result = original(call, out, args, sql)
        result['ikCharsetPreflight'] = equality_work(call, out)
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
    preflight = report['targeted']['baseline']['ikCharsetPreflight']
    driver.require(preflight['canonical'] == report['equality']['baseline']['canonical'],
                   'Baseline IK prebuild SQL differs from the later fresh-instance output')
    report.update(validationScope=SCOPE, baselineIkCharsetPreflightPassed=True,
                  ikCharsetSqlParityPassed=True, candidateIncludesPriorCs45Change=True)
    driver.dump(args.root / 'report.json', report)


parent.source_proof = source_proof
parent.equality_work = equality_work
parent.validate = validate
driver.source_proof = source_proof
driver.prepare = prepare
driver.prebuild = prebuild
driver.build_profile = build_profile


def main(argv=None):
    return parent.main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
