"""Compile the production container body with explicit substituted dependencies."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


def run(source, out):
    out.mkdir(parents=True, exist_ok=False)
    header = source / 'src/data_plane/api/data_plane/fts/ft_local_word_map.h'
    raw = header.read_bytes()
    body = re.sub(r'^#include[^\n]*\n', '', raw.decode(), flags=re.M)
    body = body.replace('private:', 'public:')
    (out / 'map-under-test.h').write_text(body, encoding='utf-8')
    probe = Path(__file__).with_suffix('.cpp')
    shutil.copyfile(probe, out / 'probe.cpp')
    env = os.environ.copy()
    if os.name == 'nt':
        vc_root = Path(r'C:\Program Files\Microsoft Visual Studio\18\Community\VC\Tools\MSVC')
        sdk = Path(r'C:\Program Files (x86)\Windows Kits\10')
        vc = sorted(vc_root.iterdir(), reverse=True)[0]
        version = sorted((sdk / 'Include').iterdir(), reverse=True)[0].name
        binary = vc / 'bin/Hostx64/x64'
        env['PATH'] = str(binary) + os.pathsep + env['PATH']
        env['INCLUDE'] = os.pathsep.join(str(p) for p in (vc / 'include', sdk / 'Include' / version / 'ucrt',
            sdk / 'Include' / version / 'shared', sdk / 'Include' / version / 'um'))
        env['LIB'] = os.pathsep.join(str(p) for p in (vc / 'lib/x64', sdk / 'Lib' / version / 'ucrt/x64',
            sdk / 'Lib' / version / 'um/x64'))
        command = [str(binary / 'cl.exe'), '/nologo', '/O2', '/EHsc', '/std:c++17',
                   '/Fe:probe.exe', 'probe.cpp']
        executable = out / 'probe.exe'
    else:
        command = ['g++', '-std=c++17', '-O1', '-g', '-fsanitize=address,undefined',
                   '-fno-omit-frame-pointer', 'probe.cpp', '-o', 'probe']
        executable = out / 'probe'
    built = subprocess.run(command, cwd=out, env=env, capture_output=True, text=True)
    (out / 'compile.log').write_text(built.stdout + built.stderr, encoding='utf-8')
    if built.returncode:
        raise RuntimeError(built.stdout + built.stderr)
    result = subprocess.run([str(executable.resolve())], cwd=out, env=env,
                            capture_output=True, text=True, check=True)
    report = dict(status='passed', stdout=result.stdout,
                  productionHeaderSha256=hashlib.sha256(raw).hexdigest(),
                  substitutedDependencies=['word hash and equality', 'array storage', 'allocator'],
                  realCollationAndDatabaseValidated=False)
    (out / 'result.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.source.resolve(), args.out.resolve())))
