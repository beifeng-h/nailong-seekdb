#!/usr/bin/env python3
"""Decode verified saved TOKENIZE samples without starting a database."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import zipfile


RECORDING_SHA = '45f36c3169e9030de32349ccd1a7b6387500069f436c075ed076d8c7ffb86c46'
BINARY_ZIP_SHA = '486c4cfe5246b4e59295686e408ebcb2ca3c4299315a69acbae0398978a88555'
BINARY_SHA = '04fc8e7df52a9f6596d4e4666709972b5a8998684d248aa190bf25d63af5b3bd'
BINARY_BYTES = 1332426096
BUILD_ID = '356b93d1932ce02ef58ab2fe7a97de72db3b7e92'
BASE = '603075716540b4f14dff9e8650bbcb0d4d60d338'
HEADER = re.compile(r'^\s*(\d+)/(\d+)\s+(\d+)\.(\d{9}):\s*$')
FRAME = re.compile(r'^\s+[0-9a-fA-F]+\s+(.+)\s+\((.+)\)\s*$')
TARGETS = {
    'tcrw_write': 'TCRWLock::wrlock',
    'latch_write': 'ObLatch::wrlock',
    'latch_unlock': 'ObLatch::unlock',
    'array_hash_find': 'ObArrayHashMap::find',
    'dictionary_match': 'ObFTDATReader<void>::match_with_hit',
    'dictionary_range': 'ObFTRangeDict::find_first_char_range',
    'first_valid_char': 'ObCharset::first_valid_char',
    'classify_first_char': 'ObFTCharUtil::classify_first_char',
    'utf8_decode': 'ob_mb_wc_utf8mb4',
    'utf8_validate': 'ob_well_formed_len_utf8mb4',
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    with path.open('rb') as stream:
        value = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def parse_stacks(text):
    """Parse one header and its leaf-first frames for each perf sample."""
    samples = []
    for line in text.splitlines():
        header = HEADER.fullmatch(line)
        if header:
            stamp = int(header[3]) * 1_000_000_000 + int(header[4])
            samples.append(dict(pid=int(header[1]), tid=int(header[2]), stamp=stamp, frames=[]))
        elif line.strip():
            frame = FRAME.fullmatch(line)
            require(frame is not None and samples, 'Unparsed or orphan perf stack line')
            samples[-1]['frames'].append(dict(symbol=frame[1].strip(), dso=frame[2]))
    require(samples, 'Missing stack sample headers')
    return samples


def summarize_stacks(samples):
    result = {}
    for label, needle in TARGETS.items():
        leaves = [row for row in samples if row['frames'] and needle in row['frames'][0]['symbol']]
        inclusive = sum(any(needle in frame['symbol'] for frame in row['frames']) for row in samples)
        callers = Counter(row['frames'][1]['symbol'] if len(row['frames']) > 1 else '[no caller]'
                          for row in leaves)
        result[label] = dict(leafSamples=len(leaves), inclusiveSamples=inclusive,
                             leafCallers=[dict(symbol=s, samples=n) for s, n in callers.most_common(10)])
    two_frames = sum(len(row['frames']) >= 2 for row in samples)
    return dict(sampleCount=len(samples), samplesWithAnyFrame=sum(bool(row['frames']) for row in samples),
                samplesWithTwoFrames=two_frames,
                twoFrameFraction=two_frames / len(samples), targets=result)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def prepare(recording, binary_zip, work):
    require(digest(recording) == RECORDING_SHA and digest(binary_zip) == BINARY_ZIP_SHA,
            'Original artifact ZIP digest mismatch')
    with zipfile.ZipFile(recording) as archive:
        report = json.loads(archive.read('collected/report.json'))
        require(report['status'] == 'focused_tokenize_profile_completed' and not report['formalContestRun']
                and report['plan']['baselineCommit'] == BASE and report['plan']['proof']['originalInputCount'] == 75
                and report['builds']['baseline']['sha256'] == BINARY_SHA, 'Unexpected recorded program identity')
        profiles = {}
        original_exe = None
        for name in ('ik', 'beng'):
            prefix = 'run/profiling/' + name + '/'
            manifest = json.loads(archive.read(prefix + 'profile-manifest.json'))
            require(manifest['elfSha256'] == BINARY_SHA and manifest['sourceCommit'] == BASE
                    and manifest['fullBytes'], 'Unexpected recording manifest')
            entries = {row['path']: row for row in manifest['files']}
            for path in ('perf.data', 'capture-summary.json', 'capture-marks.jsonl'):
                data = archive.read(prefix + path)
                require(len(data) == entries[path]['bytes'] and hashlib.sha256(data).hexdigest() == entries[path]['sha256'],
                        'Recorded member does not match its manifest: ' + path)
                target = work / (name + '-' + path)
                target.write_bytes(data)
            summary = json.loads((work / (name + '-capture-summary.json')).read_text())
            marks = [json.loads(line) for line in (work / (name + '-capture-marks.jsonl')).read_text().splitlines()]
            require(not summary['errors'] and summary['perf_reaped'] and summary['stop_signals'] == [2]
                    and [m['label'] for m in marks] == ['sample_window_started', 'sample_window_finished'],
                    'Recording was not cleanly captured')
            exe = summary['scope']['exe']
            require(original_exe in (None, exe), 'Recorded executable path changed between phases')
            original_exe = exe
            entry = report['profileResults'][name]
            tids = [row['tid'] for row in entry['coverage']['threads'] if row['samples'] > 0]
            require(entry['coverage']['passed'] and tids and
                    sum(row['samples'] for row in entry['coverage']['threads']) == entry['coverage']['requestSamples'],
                    'Request-thread profile is not accepted')
            profiles[name] = dict(pid=summary['scope']['pid'], tids=tids,
                expectedSamples=entry['coverage']['requestSamples'],
                startNs=marks[0]['monotonic_ns'], stopNs=marks[1]['monotonic_ns'],
                perfData=str(work / (name + '-perf.data')))
    exe_path = PurePosixPath(original_exe)
    require(exe_path.is_absolute() and '..' not in exe_path.parts and '\\' not in original_exe
            and exe_path.name == 'seekdb', 'Invalid recorded executable path')
    symfs = work / 'symfs'
    elf = symfs.joinpath(*exe_path.parts[1:])
    elf.parent.mkdir(parents=True)
    require(shutil.disk_usage(work).free >= BINARY_BYTES + 1024**3, 'Insufficient temporary disk for ELF')
    with zipfile.ZipFile(binary_zip) as archive:
        entries = [entry for entry in archive.infolist() if entry.filename == 'candidate-elf.tar.gz']
        require(len(entries) == 1 and not entries[0].is_dir(), 'Expected one saved ELF archive')
        with archive.open(entries[0]) as raw, tarfile.open(fileobj=raw, mode='r|gz') as tar:
            seen = 0
            for member in tar:
                require(seen == 0 and member.name == 'candidate/seekdb' and member.isfile()
                        and member.size == BINARY_BYTES, 'Unexpected ELF archive content')
                with tar.extractfile(member) as source, elf.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                seen += 1
            require(seen == 1, 'Missing saved executable')
    require(elf.stat().st_size == BINARY_BYTES and digest(elf) == BINARY_SHA, 'Saved ELF digest mismatch')
    notes = subprocess.run(['readelf', '--notes', str(elf)], capture_output=True, text=True, timeout=30, check=True)
    require(re.findall(r'Build ID:\s*([0-9a-f]+)', notes.stdout) == [BUILD_ID], 'Saved ELF build ID mismatch')
    return dict(recordingSha256=RECORDING_SHA, binaryZipSha256=BINARY_ZIP_SHA,
                binarySha256=BINARY_SHA, buildId=BUILD_ID, sourceCommit=BASE,
                originalExecutable=original_exe, symfs=str(symfs), profiles=profiles,
                formalContestRun=False, databaseStarted=False)


def run_perf(perf, work, evidence, name, profile, symfs, inline):
    def seconds(value):
        return f'{value // 1_000_000_000}.{value % 1_000_000_000:09d}'
    window = seconds(profile['startNs']) + ',' + seconds(profile['stopNs'] - 1)
    label = name + ('-inline' if inline else '-stacks')
    output = evidence / (label + '.txt')
    errors = evidence / (label + '.stderr.txt')
    command = ['timeout', '--signal=TERM', '--kill-after=5s', '120s', perf, 'script', '--ns',
               '--max-stack=64', '--inline' if inline else '--no-inline', '--symfs', symfs,
               '--time', window, '--tid=' + ','.join(map(str, profile['tids'])),
               '-i', profile['perfData'], '-F', 'time,pid,tid,ip,sym,dso']
    with output.open('xb') as stdout, errors.open('xb') as stderr:
        result = subprocess.run(command, stdout=stdout, stderr=stderr, cwd=work, check=False)
    write_json(evidence / (label + '-command.json'), dict(command=command, exitCode=result.returncode,
               stdoutSha256=digest(output), stderrSha256=digest(errors)))
    require(result.returncode == 0, 'Saved stack decoder failed: ' + label)
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--recording', type=Path, required=True)
    parser.add_argument('--binary-zip', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    work = args.work.resolve(strict=True)
    evidence = work / 'evidence'
    require(evidence.is_dir() and not evidence.is_symlink(), 'Fresh evidence directory required')
    metadata = prepare(args.recording, args.binary_zip, work)
    write_json(evidence / 'input-verification.json', metadata)
    perf = shutil.which('perf')
    require(perf is not None, 'An installed perf executable is required')
    version = subprocess.run([perf, '--version'], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    write_json(evidence / 'perf-version.json', dict(version=version))
    output = {}
    limitations = ['Stacks are sampled diagnostic evidence, not score or wall-clock improvement',
                   'Frame and inline resolution depend on recorded unwind data and symbol availability',
                   'Target inclusive counts can overlap and must not be summed']
    for name, profile in metadata['profiles'].items():
        path = run_perf(perf, work, evidence, name, profile, metadata['symfs'], inline=False)
        samples = parse_stacks(path.read_text(encoding='utf-8', errors='replace'))
        require(len(samples) == profile['expectedSamples'] and
                all(row['pid'] == profile['pid'] and row['tid'] in profile['tids'] and
                    profile['startNs'] <= row['stamp'] < profile['stopNs'] for row in samples),
                'Decoded stack headers do not match the saved thread/time selection')
        result = summarize_stacks(samples)
        result['twoFrameCoverageGatePassed'] = result['twoFrameFraction'] >= 0.5
        output[name] = result
        write_json(evidence / 'analysis.json', dict(profiles=output, sourceCommit=BASE,
            formalContestRun=False, databaseStarted=False, limitations=limitations, complete=False))
        inline_path = run_perf(perf, work, evidence, name, profile, metadata['symfs'], inline=True)
        inline_samples = parse_stacks(inline_path.read_text(encoding='utf-8', errors='replace'))
        require([(x['pid'], x['tid'], x['stamp']) for x in inline_samples] ==
                [(x['pid'], x['tid'], x['stamp']) for x in samples], 'Inline output changed sample identities')
        result['inline'] = summarize_stacks(inline_samples)
    write_json(evidence / 'analysis.json', dict(profiles=output, sourceCommit=BASE,
        formalContestRun=False, databaseStarted=False, limitations=limitations, complete=True))
    print(json.dumps({name: {k: value[k] for k in ('sampleCount', 'twoFrameFraction')}
                      for name, value in output.items()}, sort_keys=True))


if __name__ == '__main__':
    main()
