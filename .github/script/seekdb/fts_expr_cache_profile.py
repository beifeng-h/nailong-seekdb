"""Read generated compiler records without executing CMake or changing its cache.

Only the original Linux release profile is accepted. Raw profile evidence is
whitelisted and small; the potentially large command database stays in place.
"""
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
from fts_paired_validation import no_symlinks


EXPECTED = dict(CMAKE_BUILD_TYPE='RelWithDebInfo', OB_ENABLE_UNITY='ON', OB_USE_LLD='ON')
CACHE_CAP = 2 * 1024**2
RECORD_CAP = 128 * 1024
COMMAND_CAP = 128 * 1024**2


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def cache_values(raw):
    result = {}
    for line in raw.decode('utf-8', errors='strict').splitlines():
        match = re.fullmatch(r'([^#/:][^:]*):[^=]+=(.*)', line)
        if match:
            require(match[1] not in result, 'Duplicate CMake cache key')
            result[match[1]] = match[2]
    return result


def compiler_values(raw, language):
    result = {}
    for key in ('COMPILER', 'COMPILER_ID', 'COMPILER_VERSION'):
        name = 'CMAKE_' + language + '_' + key
        matches = re.findall(r'^set\(' + re.escape(name) + r' "([^"\n]*)"\)\s*$',
                             raw.decode('utf-8', errors='strict'), re.M)
        require(len(matches) == 1, 'Missing or ambiguous generated compiler field: ' + name)
        require('\\' not in matches[0], 'Unexpected escape in generated compiler field')
        result[key] = matches[0]
    return result


def regular(path, cap):
    no_symlinks(path)
    require(not path.is_symlink() and path.is_file() and path.stat().st_nlink == 1,
            'Profile input must be a unique regular file: ' + str(path))
    require(path.stat().st_size <= cap, 'Profile input exceeds its explicit byte cap')
    return path.read_bytes()


def verify_build_profile(args):
    build = args.source / 'build_release'
    phase = getattr(args, 'phase', 'prebuild')
    require(phase in ('prebuild', 'snapshot'), 'Unexpected profile evidence phase')
    out = args.root / 'evidence/candidate' / ('profile-' + phase)
    out.mkdir(parents=True, exist_ok=True)
    cache_path = build / 'CMakeCache.txt'
    cache_raw = regular(cache_path, CACHE_CAP)
    (out / 'CMakeCache.txt').write_bytes(cache_raw)
    settings = cache_values(cache_raw)
    paths = {}
    records = {}
    raw_records = {}
    for language in ('C', 'CXX'):
        found = list((build / 'CMakeFiles').glob('*/CMake' + language + 'Compiler.cmake'))
        require(len(found) == 1, 'Exactly one generated compiler record required for ' + language)
        raw = regular(found[0], RECORD_CAP)
        (out / ('CMake' + language + 'Compiler.cmake')).write_bytes(raw)
        raw_records[language] = raw
        paths[language] = found[0]
    for language in ('C', 'CXX'):
        records[language] = compiler_values(raw_records[language], language)
    require(all(settings.get(k) == v for k, v in EXPECTED.items()), 'Original CMake settings changed')
    require(settings.get('CMAKE_GENERATOR') == 'Unix Makefiles'
            and settings.get('CMAKE_EXPORT_COMPILE_COMMANDS') == 'ON', 'Original generator/export profile changed')
    compilers = {}
    for language in ('C', 'CXX'):
        record = records[language]
        expected_path = args.source / 'deps/3rd/usr/local/oceanbase/devtools/bin' / (
            'clang-17' if language == 'C' else 'clang++-17')
        require(record['COMPILER'] == str(expected_path) and expected_path.is_file(),
                'Generated compiler is outside the original exact devtools path')
        require(record['COMPILER_ID'] == 'Clang' and record['COMPILER_VERSION'] == '17.0.6',
                'Generated compiler identity/version differs from original Clang 17.0.6')
        cached = settings.get('CMAKE_' + language + '_COMPILER')
        require(cached is None or cached == record['COMPILER'], 'Cache and generated compiler disagree')
        text = subprocess.check_output([record['COMPILER'], '--version'], timeout=30).decode('utf-8')
        require(re.search(r'\bclang version 17\.0\.6(?:\s|$)', text) is not None,
                'Actual compiler is not the original Clang 17.0.6')
        (out / (language + '-version.log')).write_text(text)
        compilers[language] = dict(path=record['COMPILER'], id=record['COMPILER_ID'],
            version=record['COMPILER_VERSION'], recordPath=str(paths[language]),
            recordSha256=hashlib.sha256((out / ('CMake' + language + 'Compiler.cmake')).read_bytes()).hexdigest())
    commands_path = build / 'compile_commands.json'
    commands_raw = regular(commands_path, COMMAND_CAP)
    commands = json.loads(commands_raw)
    require(isinstance(commands, list) and 0 < len(commands) <= 50000, 'Invalid command database')
    unity = list((build / 'src/sql/CMakeFiles/ob_sql.dir/Unity').glob('*.cxx'))
    affected = [p for p in unity if 'ob_table_scan_op.cpp' in p.read_text(encoding='utf-8')]
    require(len(affected) == 1, 'Exactly one affected generated Unity translation unit required')
    selected = []
    counts = dict(C=0, CXX=0)
    known = {record['path']: language for language, record in compilers.items()}
    launchers = {settings.get(k) for k in ('OB_CCACHE', 'OB_COMPILE_EXECUTABLE',
        'CMAKE_C_COMPILER_LAUNCHER', 'CMAKE_CXX_COMPILER_LAUNCHER')}
    launchers = {p for p in launchers if p and not p.endswith('-NOTFOUND')}
    for row in commands:
        require(isinstance(row, dict) and isinstance(row.get('directory'), str)
                and isinstance(row.get('file'), str), 'Malformed compiler command record')
        argv = row.get('arguments')
        if argv is None:
            require(isinstance(row.get('command'), str), 'Compiler command or arguments missing')
            argv = shlex.split(row['command'])
        require(isinstance(argv, list) and argv and all(isinstance(x, str) for x in argv), 'Invalid compiler arguments')
        index = 1 if argv[0] in launchers else 0
        require(len(argv) > index and argv[index] in known, 'Actual command uses an unverified compiler/launcher')
        language = known[argv[index]]
        counts[language] += 1
        file = Path(row['file'])
        if not file.is_absolute():
            file = Path(row['directory']) / file
        if file.resolve() == affected[0].resolve():
            require(language == 'CXX', 'Affected native SQL unit is not compiled by verified CXX compiler')
            selected.append(row)
    require(len(selected) == 1, 'Affected native SQL compile command missing or duplicated')
    command_proof = dict(path=str(commands_path), bytes=len(commands_raw), sha256=hashlib.sha256(commands_raw).hexdigest(),
        entryCount=len(commands), compilerCounts=counts, affectedUnity=str(affected[0]), selectedEntries=selected,
        wholeFileCopied=False, selectedEntriesComplete=True)
    encoded = json.dumps(command_proof, indent=2).encode()
    require(len(encoded) <= 1024**2, 'Selected compiler proof exceeds its explicit cap')
    (out / 'compile-commands-proof.json').write_bytes(encoded + b'\n')
    rust = subprocess.check_output(['rustc', '--version'], cwd=args.source / 'rust', timeout=30).decode().strip()
    require(rust.startswith('rustc 1.98.1 '), 'Original Rust 1.98.1 required')
    signature = dict(settings=EXPECTED, generator='Unix Makefiles', exportCompileCommands='ON',
        compilers=compilers, affectedCommand=selected[0], rustVersion=rust)
    encoded = json.dumps(signature, sort_keys=True, separators=(',', ':')).encode()
    proof = dict(signature=signature, signatureSha256=hashlib.sha256(encoded).hexdigest(),
        cachePath=str(cache_path), cacheBytes=len(cache_raw), cacheSha256=hashlib.sha256(cache_raw).hexdigest(),
        commandDatabaseSha256=command_proof['sha256'], phase=phase, passed=True)
    prior = args.root / 'evidence/candidate/profile-prebuild/profile-proof.json'
    if phase == 'snapshot':
        require(prior.is_file() and json.loads(prior.read_text())['signature'] == signature,
                'Active compiler/profile changed since prebuild')
    (out / 'profile-proof.json').write_text(json.dumps(proof, indent=2) + '\n')
    return EXPECTED.copy(), rust
