"""Independent offline contracts for the active, zero-build exact-B3 continuation.

Only standard-library fixtures, inert files, stub commands and ``bash -n`` are
used. No artifact is fetched and no preserved ELF, client or server is executed.
Historical workflow/loader test classes are deliberately not imported.
"""
import argparse
import copy
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import patch

import fts_b3_reuse_validation as reuse


REPO = Path(__file__).resolve().parents[3]
WORKFLOW = REPO / '.github/script/seekdb/fixtures/fts-exact-b3-c146-validation.yml'
D = 'a' * 40
B3 = 'b3eed52688fc1b178632a9dbb38aabb32f805c5d'
C235 = 'c235713689325d2bb8882b86f8f89ee99ff9b43a'
A = '5bce9e06d1281347ad61344e4b01fab9158c58a2'
FE = 'fe876f8f805969970621a18a1cf812ff322fc78c'
REPOSITORY = 'beifeng-h/nailong-seekdb'
DOWNLOAD = 'actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333'
UPLOAD = 'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02'
CLIENTS = {
    'obclient': '798aa952d536563ee0291e69251a6d0cd0d85e0c470d5499768fa0731ff3c1be',
    'mysqltest': '64e0323b385af2831c5b166a375e8320ec40e3ead0bd925295de1b8f5a2196c6',
}


def workflow_projection(text):
    """Strict indentation/schema projection, not a general-purpose YAML parser.

    Unlike a regex that invents the expected trigger, this reads every actual
    mapping key and rejects duplicates/unsupported syntax. New YAML features
    must be reviewed explicitly; no PyYAML install is needed on a fresh runner.
    """
    lines = text.splitlines()

    def scalar(raw):
        raw = raw.strip()
        if raw.startswith(('\"', "'")):
            if raw[-1:] != raw[:1]:
                raise AssertionError('Unterminated quoted scalar')
            return raw[1:-1]
        if raw in ('true', 'false'):
            return raw == 'true'
        if raw.startswith('[') and raw.endswith(']'):
            return [scalar(value) for value in raw[1:-1].split(',') if value.strip()]
        if raw.startswith(('&', '*', '!', '{', '>')):
            raise AssertionError('Unsupported YAML scalar')
        return raw

    def skip(index):
        while index < len(lines) and (not lines[index].strip() or lines[index].lstrip().startswith('#')):
            index += 1
        return index

    def mapping(index, indent):
        result = {}
        while (index := skip(index)) < len(lines):
            line = lines[index]
            width = len(line) - len(line.lstrip(' '))
            if width < indent:
                break
            match = re.fullmatch(' ' * indent + r'([A-Za-z_][A-Za-z0-9_-]*):(?: (.*))?', line)
            if not match:
                raise AssertionError('Unsupported workflow mapping: ' + line)
            key, raw = match.groups()
            if key in result:
                raise AssertionError('Duplicate workflow field: ' + key)
            index += 1
            if raw == '|':
                start = index
                while index < len(lines) and (not lines[index].strip() or lines[index].startswith(' ' * (indent + 2))):
                    index += 1
                result[key] = textwrap.dedent('\n'.join(lines[start:index])) + '\n'
            elif raw:
                result[key] = scalar(raw)
            else:
                index = skip(index)
                if index == len(lines) or len(lines[index]) - len(lines[index].lstrip(' ')) <= indent:
                    result[key] = {}
                elif lines[index].startswith(' ' * (indent + 2) + '- '):
                    items = []
                    while index < len(lines) and lines[index].startswith(' ' * (indent + 2) + '- '):
                        # Turn just this sequence marker into the mapping indent.
                        lines[index] = ' ' * (indent + 4) + lines[index][indent + 4:]
                        item, index = mapping(index, indent + 4)
                        items.append(item)
                        index = skip(index)
                    result[key] = items
                else:
                    result[key], index = mapping(index, indent + 2)
        return result, index

    value, end = mapping(0, 0)
    if skip(end) != len(lines):
        raise AssertionError('Unparsed workflow content')
    return value


class LiveB3Workflow(unittest.TestCase):
    def workflow(self, text=None):
        return workflow_projection(WORKFLOW.read_text() if text is None else text)

    def assert_contract(self, text):
        workflow = self.workflow(text)
        self.assertEqual(set(workflow), {'name', 'on', 'permissions', 'concurrency', 'jobs'})
        self.assertEqual(workflow['on'], {'push': {'branches': ['perf/fts-official-contract-20261108']}})
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        self.assertEqual(workflow['concurrency'], {'group': 'seekdb-fts-free-linux-validation', 'cancel-in-progress': False})
        self.assertEqual(set(workflow['jobs']), {'paired'})
        job = workflow['jobs']['paired']
        self.assertEqual(set(job), {'if', 'runs-on', 'timeout-minutes', 'permissions', 'env', 'steps'})
        self.assertEqual(job['if'], "${{ github.repository == 'beifeng-h/nailong-seekdb' && github.event.repository.private == false }}")
        self.assertEqual(job['runs-on'], 'ubuntu-24.04')
        self.assertEqual(job['permissions'], {'contents': 'read', 'actions': 'read'})
        self.assertEqual(job['env'], {'HYBRID_KERNEL_SHA': C235, 'B3_SHA': B3,
            'GIT_TERMINAL_PROMPT': '0', 'PYTHONDONTWRITEBYTECODE': '1'})
        steps = job['steps']
        self.assertEqual(len({step['name'] for step in steps}), len(steps))
        for step in steps:
            self.assertTrue(set(step) <= {'name', 'shell', 'run', 'uses', 'with', 'if'})
            self.assertNotEqual('run' in step, 'uses' in step)
            if 'run' in step:
                self.assertEqual(step['shell'], 'bash')
                self.assertTrue(step['run'].startswith('set -euo pipefail\n'))
        downloads = [(index, step) for index, step in enumerate(steps) if step.get('uses', '').startswith('actions/download-artifact@')]
        self.assertEqual(len(downloads), 2)
        for (_, step), artifact, run, path in zip(downloads,
                ('13621468966', '13652898100'), ('42486120779', '42565036644'),
                ('seekdb-original-artifact', 'seekdb-b3-artifact')):
            self.assertEqual(step['uses'], DOWNLOAD)
            self.assertEqual(step['with'], {'artifact-ids': artifact, 'run-id': run,
                'repository': REPOSITORY, 'github-token': '${{ github.token }}',
                'path': '${{ runner.temp }}/' + path, 'skip-decompress': True, 'digest-mismatch': 'error'})
            self.assertNotIn('if', step)
        self.assertEqual([step['uses'] for step in steps if 'uses' in step],
            [DOWNLOAD, DOWNLOAD, './.github/actions/setup-rust', UPLOAD])
        rust = next(step for step in steps if step.get('uses') == './.github/actions/setup-rust')
        self.assertEqual(rust['with'], {'cache': 'false', 'crates-mirror': 'false'})
        cleanups = [step['run'] for step in steps if 'sudo rm ' in step.get('run', '')]
        self.assertEqual(len(cleanups), 1)
        cleanup = cleanups[0].split('uname -srmo;', 1)[0]
        self.assertEqual(cleanup, textwrap.dedent('''\
            set -euo pipefail
            for image_tool in /usr/local/lib/android /usr/share/dotnet /opt/ghc; do
              if test -e "$image_tool"; then
                resolved=$(realpath -- "$image_tool")
                test "$resolved" = "$image_tool"
                case "$resolved" in
                  /usr/local/lib/android|/usr/share/dotnet|/opt/ghc) sudo rm -rf -- "$resolved" ;;
                  *) exit 2 ;;
                esac
              fi
            done
            '''))
        self.assertEqual(text.count('${{ github.token }}'), 2)
        for forbidden in ('secrets.', 'secrets[', 'printenv', 'set -x', 'actions: write',
                'id-token:', 'workflow_dispatch', 'pull_request', 'workflow_run',
                '--phase snapshot', '--phase preserve-candidate', '--phase prebuild',
                '--build build_release', '--target seekdb', 'ninja ', 'cmake ',
                'curl api.github.com', 'gh run download'):
            self.assertNotIn(forbidden, text)

        runs = '\n'.join(step.get('run', '') for step in steps)
        phase_steps = {}
        for phase in ('verify-source', 'prepare-reuse', 'validate', 'finalize', 'collect-evidence', 'present'):
            found = [index for index, step in enumerate(steps) if '--phase ' + phase in step.get('run', '')]
            self.assertEqual(len(found), 1, phase)
            phase_steps[phase] = found[0]
        gate = next(index for index, step in enumerate(steps) if '-m unittest discover' in step.get('run', ''))
        self.assertEqual(gate, phase_steps['verify-source'])
        self.assertIn("python3 -B -m unittest discover -s .github/script/seekdb -p 'test_fts*.py' -v", steps[gate]['run'])
        self.assertNotIn('if', steps[gate])
        self.assertLess(steps[gate]['run'].index('-m unittest discover'), steps[gate]['run'].index('--phase verify-source'))
        self.assertLess(gate, downloads[0][0])
        self.assertLess(downloads[1][0], phase_steps['prepare-reuse'])
        initialize = [index for index, step in enumerate(steps) if './build.sh' in step.get('run', '')]
        self.assertEqual(len(initialize), 1)
        self.assertEqual(runs.count('./build.sh'), 1)
        init = steps[initialize[0]]['run']
        for required in ('cd "$PAIRED_ROOT/sources/candidate"', './build.sh release --init',
                'test "$(git rev-parse HEAD)" = "$B3_SHA"',
                'test -z "$(git status --porcelain=v1 --untracked-files=all)"',
                'test ! -e build_release/src/observer/seekdb', 'sha256sum -c -'):
            self.assertIn(required, init)
        self.assertLess(init.index('cd "$PAIRED_ROOT/sources/candidate"'), init.index('./build.sh'))
        for name, sha in CLIENTS.items():
            self.assertIn(sha + '  deps/3rd/u01/obclient/bin/' + name, init)
        self.assertLess(phase_steps['prepare-reuse'], initialize[0])
        self.assertLess(initialize[0], phase_steps['validate'])
        self.assertEqual(list(phase_steps.values()), sorted(phase_steps.values()))
        for phase in ('finalize', 'collect-evidence', 'present'):
            self.assertEqual(steps[phase_steps[phase]]['if'], 'always()')
        prepare = steps[phase_steps['prepare-reuse']]['run']
        for required in ('--artifact-zip "$original"', '--candidate-artifact-zip "$candidate"',
                'test "${#files[@]}" -eq 1', 'test ! -L "${files[0]}"', "stat -c '%h'", '--execute'):
            self.assertIn(required, prepare)
        self.assertIn('"$PAIRED_ROOT/driver/fts_b3_reuse_validation.py"', steps[phase_steps['validate']]['run'])
        checkout = steps[0]['run']
        self.assertEqual(checkout.count('fetch --atomic --no-auto-maintenance --no-tags --depth=1 origin'), 1)
        self.assertIn('"$GITHUB_SHA" "$B3_SHA" "$HYBRID_KERNEL_SHA"', checkout)
        self.assertEqual(checkout.count(A + ' ' + FE), 2)
        self.assertLess(checkout.index('cat-file -e'), checkout.index('checkout --detach'))
        self.assertIn('test "$(git rev-parse HEAD)" = "$GITHUB_SHA"', checkout)
        self.assertIn('test -z "$(git status --porcelain=v1 --untracked-files=all)"', checkout)

    def test_active_workflow_positive_contract(self):
        self.assert_contract(WORKFLOW.read_text())

    def reject_changes(self, changes):
        original = WORKFLOW.read_text()
        for old, new in changes:
            with self.subTest(old=old, new=new):
                self.assertIn(old, original)
                altered = original.replace(old, new, 1)
                with self.assertRaises(AssertionError):
                    self.assert_contract(altered)

    def test_reject_expanded_trigger_repo_concurrency_permissions_and_secrets(self):
        self.reject_changes([
            ('  push:\n', '  workflow_dispatch:\n  push:\n'),
            ('branches: [perf/fts-official-contract-20261108]', 'branches: [perf/fts-official-contract-20261108, main]'),
            ('github.event.repository.private == false', 'github.event.repository.private == true'),
            ('cancel-in-progress: false', 'cancel-in-progress: true'),
            ('group: seekdb-fts-free-linux-validation', 'group: another-group'),
            ('  contents: read\n', '  contents: write\n'),
            ('      actions: read\n', '      actions: read\n      issues: write\n'),
            ('${{ github.token }}', '${{ secrets.GITHUB_TOKEN }}'),
            ("      B3_SHA: '" + B3 + "'", "      B3_SHA: '" + D + "'"),
            ("      HYBRID_KERNEL_SHA: '" + C235 + "'", "      HYBRID_KERNEL_SHA: '" + FE + "'"),
        ])

    def test_reject_wrong_duplicate_mutable_or_loosened_artifact_downloads(self):
        self.reject_changes([
            ('13621468966', '13608812363'), ('13652898100', '13621468966'),
            ('42486120779', '42565036644'), ('42565036644', '42486120779'),
            (DOWNLOAD, 'actions/download-artifact@v4'),
            ('skip-decompress: true', 'skip-decompress: false'),
            ('digest-mismatch: error', 'digest-mismatch: warn'),
            ('repository: ' + REPOSITORY, 'repository: other/repository'),
        ])
        text = WORKFLOW.read_text()
        start = text.index('      - name: Download immutable original')
        end = text.index('      - name: Download exact immutable', start)
        with self.assertRaises(AssertionError):
            self.assert_contract(text[:end] + text[start:end] + text[end:])

    def test_reject_skipped_gate_compile_wrong_source_client_or_missing_FE(self):
        self.reject_changes([
            ("-p 'test_fts*.py'", "-p 'test_fts_b3*.py'"),
            ('--phase verify-source', '--phase plan'),
            ('--candidate-artifact-zip "$candidate"', '--candidate-artifact-zip "$original"'),
            ('cd "$PAIRED_ROOT/sources/candidate"', 'cd "$GITHUB_WORKSPACE"'),
            ('./build.sh release --init', './build.sh release --init; cmake --build build_release'),
            ('test ! -e build_release/src/observer/seekdb', 'true'),
            (CLIENTS['obclient'], '0' * 64),
            ('--phase finalize', '--phase snapshot'),
            (' ' + FE, ''),
            ('fetch --atomic', 'fetch'),
            ('test "$(git rev-parse HEAD)" = "$GITHUB_SHA"', 'true'),
            ('/usr/local/lib/android /usr/share/dotnet /opt/ghc;', '/usr/local/lib/android /usr/share/dotnet /opt/ghc /home;'),
            ('test "$resolved" = "$image_tool"', 'true'),
            ('sudo rm -rf -- "$resolved"', 'sudo rm -rf /'),
        ])

    def test_projection_rejects_duplicate_keys_instead_of_overwriting(self):
        for old, new in [('  contents: read', '  contents: read\n  contents: write'),
                ('  push:', '  push:\n  push:'),
                ("          artifact-ids: '13652898100'", "          artifact-ids: '13652898100'\n          artifact-ids: '13621468966'")]:
            with self.subTest(old=old), self.assertRaises(AssertionError):
                self.workflow(WORKFLOW.read_text().replace(old, new, 1))

    def test_every_active_shell_step_parses_without_execution(self):
        for step in self.workflow()['jobs']['paired']['steps']:
            if 'run' in step:
                result = subprocess.run(['bash', '-n'], input=step['run'], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, (step['name'], result.stderr))

    def run_checkout(self, script=None, **overrides):
        script = script or self.workflow()['jobs']['paired']['steps'][0]['run']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'git').write_text(textwrap.dedent('''\
                #!/usr/bin/env python3
                import json, os, sys
                args = sys.argv[1:]
                with open(os.environ['TRACE'], 'a') as out:
                    out.write(json.dumps(args) + '\\n')
                while args and args[0] in ('-C', '-c'):
                    args = args[2:]
                command = args[0]
                if command == 'fetch':
                    sys.exit(int(os.environ.get('FETCH_EXIT', '0')))
                if command == 'cat-file' and args[-1] == os.environ.get('MISSING', '') + '^{commit}':
                    sys.exit(72)
                if command == 'rev-parse':
                    print(os.environ.get('TEST_HEAD', os.environ['GITHUB_SHA']))
                if command == 'status':
                    print(os.environ.get('DIRTY', ''), end='')
                if command not in ('init', 'remote', 'fetch', 'cat-file', 'checkout', 'rev-parse', 'status'):
                    sys.exit(99)
                '''))
            (root / 'git').chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'], TRACE=str(root / 'trace'),
                GITHUB_WORKSPACE=str(root / 'source'), RUNNER_TEMP=str(root), GITHUB_ENV=str(root / 'env'),
                GITHUB_SHA=D, B3_SHA=B3, HYBRID_KERNEL_SHA=C235, GITHUB_SERVER_URL='https://github.com',
                GITHUB_REPOSITORY=REPOSITORY)
            env.update(overrides)
            result = subprocess.run(['bash', '-c', script], env=env, text=True, capture_output=True)
            calls = [json.loads(line) for line in (root / 'trace').read_text().splitlines()] if (root / 'trace').exists() else []
            return result, calls

    def test_one_atomic_fetch_contains_all_five_objects_before_checkout(self):
        result, calls = self.run_checkout()
        self.assertEqual(result.returncode, 0, result.stderr)
        fetches = [call for call in calls if 'fetch' in call]
        self.assertEqual(len(fetches), 1)
        self.assertEqual(fetches[0][-5:], [D, B3, C235, A, FE])
        for flag in ('--atomic', '--no-auto-maintenance', '--no-tags', '--depth=1', 'maintenance.auto=false', 'gc.auto=0'):
            self.assertIn(flag, fetches[0])
        checked = [(index, call[-1]) for index, call in enumerate(calls) if 'cat-file' in call]
        self.assertEqual([sha for _, sha in checked], [sha + '^{commit}' for sha in (D, B3, C235, A, FE)])
        checkout = [(index, call) for index, call in enumerate(calls) if 'checkout' in call]
        self.assertEqual(len(checkout), 1)
        self.assertEqual(checkout[0][1][-3:], ['checkout', '--detach', D])
        self.assertGreater(checkout[0][0], max(index for index, _ in checked))

    def test_fetch_failure_or_each_missing_object_never_reaches_checkout(self):
        for overrides in [dict(FETCH_EXIT='71')] + [dict(MISSING=sha) for sha in (D, B3, C235, A, FE)]:
            with self.subTest(overrides=overrides):
                result, calls = self.run_checkout(**overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(sum('fetch' in call for call in calls), 1)
                self.assertFalse(any('checkout' in call for call in calls))

    def test_missing_FE_regression_is_both_statically_and_dynamically_rejected(self):
        script = self.workflow()['jobs']['paired']['steps'][0]['run']
        broken = script.replace(' ' + FE, '', 1)
        # The validation loop must still refuse checkout if fetch omitted FE.
        result, calls = self.run_checkout(broken, MISSING=FE)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any('checkout' in call for call in calls))
        self.assertNotIn(FE, next(call for call in calls if 'fetch' in call))

    def test_wrong_environment_pin_head_or_dirty_checkout_fails_closed(self):
        for overrides in (dict(B3_SHA=D), dict(HYBRID_KERNEL_SHA=FE), dict(TEST_HEAD='f' * 40), dict(DIRTY='?? unexpected')):
            with self.subTest(overrides=overrides):
                result, calls = self.run_checkout(**overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertLessEqual(sum('fetch' in call for call in calls), 1)
                if set(overrides) & {'B3_SHA', 'HYBRID_KERNEL_SHA'}:
                    self.assertEqual(calls, [])

    def test_offline_test_or_source_gate_failure_never_reaches_download_boundary(self):
        gate = next(step['run'] for step in self.workflow()['jobs']['paired']['steps']
            if '-m unittest discover' in step.get('run', ''))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stub = root / 'python3'
            stub.write_text(textwrap.dedent('''\
                #!/bin/bash
                printf '%s\\n' "$*" >> "$TRACE"
                if [[ "$*" = *unittest* ]]; then
                  exit "$TEST_EXIT"
                fi
                if [[ "$*" = *verify-source* ]]; then
                  exit "$SOURCE_EXIT"
                fi
                exit 99
                '''))
            stub.chmod(0o755)
            for first, second in ((1, 0), (0, 1), (0, 0)):
                trace = root / ('trace-%s-%s' % (first, second))
                env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                    TRACE=str(trace), TEST_EXIT=str(first), SOURCE_EXIT=str(second),
                    GITHUB_WORKSPACE=str(root), PAIRED_ROOT=str(root / 'run'), GITHUB_SHA=D, HYBRID_KERNEL_SHA=C235)
                result = subprocess.run(['bash', '-c', gate + "printf 'DOWNLOAD_BOUNDARY\\n' >> \"$TRACE\"\n"],
                    env=env, text=True, capture_output=True)
                calls = trace.read_text().splitlines()
                with self.subTest(test_exit=first, source_exit=second):
                    self.assertEqual(result.returncode == 0, first == second == 0)
                    self.assertEqual('DOWNLOAD_BOUNDARY' in calls, first == second == 0)
                    self.assertEqual(sum('verify-source' in call for call in calls), int(first == 0))

    def initialization_tail_script(self):
        collector = next(step for step in self.workflow()['jobs']['paired']['steps']
            if '--phase collect-evidence' in step.get('run', ''))
        self.assertEqual(collector['if'], 'always()')
        self.assertEqual(collector['run'].count("<<'PYTAIL'\n"), 1)
        script = collector['run'].split("<<'PYTAIL'\n", 1)[1].split('\nPYTAIL\n', 1)[0]
        self.assertNotIn('--phase collect-evidence', script)
        return script

    def test_initialize_tail_is_bounded_hash_bound_and_cannot_rewrite_verdict(self):
        script = self.initialization_tail_script()
        cap = 128 * 1024
        for data in (b'small initialize output\n\x00\xff', b'prefix-never-copied\n' + b'\xff\x00' * cap):
            with self.subTest(size=len(data)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                log, out = root / 'initialize.log', root / 'evidence'
                out.mkdir()
                verdict = root / 'report.json'
                before = b'{"status":"invalid_calibration","failure":"initialize failed"}\n'
                verdict.write_bytes(before)
                log.write_bytes(data)
                # Execute ONLY extracted standard-library PYTAIL, never the shell
                # collector, initialization command, downloader or any ELF.
                result = subprocess.run([sys.executable, '-B', '-', str(log), str(out)],
                    input=script, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                selected = data[-cap:]
                tail = (out / 'initialize-tail.log').read_text()
                self.assertEqual(tail, selected.decode('utf-8', 'backslashreplace').replace('\x00', '\\x00'))
                self.assertLessEqual((out / 'initialize-tail.log').stat().st_size, 4 * cap)
                self.assertNotIn('\x00', tail)
                self.assertEqual(json.loads((out / 'initialize-tail.json').read_text()), dict(
                    originalBytes=len(data), selectedBytes=len(selected), tailOffset=len(data) - len(selected),
                    selectedRawSha256=hashlib.sha256(selected).hexdigest(),
                    utf8Representation='backslashreplace', maxSelectedRawBytes=cap))
                self.assertEqual(result.stdout, 'B3_INITIALIZE_BOUNDED_TAIL\n' + tail + '\n')
                self.assertEqual(verdict.read_bytes(), before)
                self.assertEqual(log.read_bytes(), data)

    def test_initialize_tail_refuses_symlink_or_shared_log_without_output(self):
        script = self.initialization_tail_script()
        for kind in ('symlink', 'hardlink'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                original, log, out = root / 'original.log', root / 'log', root / 'evidence'
                original.write_bytes(b'synthetic log')
                out.mkdir()
                if kind == 'symlink':
                    log.symlink_to(original)
                else:
                    os.link(original, log)
                result = subprocess.run([sys.executable, '-B', '-', str(log), str(out)],
                    input=script, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('owned regular file', result.stderr)
                self.assertEqual(list(out.iterdir()), [])


class LiveB3ExecutionGates(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source, self.root = self.base / 'D', self.base / 'run'
        self.source.mkdir()
        self.args = argparse.Namespace(source=self.source, root=self.root, driver_sha=D, kernel_sha=C235)

    def argv(self, phase):
        return ['--phase', phase, '--source', str(self.source), '--root', str(self.root),
            '--driver-sha', D, '--kernel-sha', C235]

    def test_read_only_source_phase_does_not_plan_prepare_load_or_execute(self):
        with ExitStack() as stack:
            verify = stack.enter_context(patch.object(reuse, 'verify_driver', return_value={'driverCommit': D}))
            blocked = [stack.enter_context(patch.object(reuse, name, side_effect=AssertionError(name)))
                for name in ('plan_for', 'prepare', 'load_plan', 'validate', 'prepare_candidate', 'install_termination_handlers')]
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(reuse.main(self.argv('verify-source')), 0)
            verify.assert_called_once_with(self.source, D, C235)
            self.assertEqual(json.loads(output.getvalue()), {'driverCommit': D})
            for call in blocked:
                call.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_source_equivalence_failure_propagates_without_preparation(self):
        with patch.object(reuse, 'verify_driver', side_effect=RuntimeError('source profile changed')), \
                patch.object(reuse, 'prepare') as prepare, self.assertRaisesRegex(RuntimeError, 'source profile changed'):
            reuse.main(self.argv('verify-source'))
        prepare.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_live_test_is_explicitly_allowed_but_build_SQL_and_proof_are_not(self):
        self.assertIn(reuse.PREFIX + Path(__file__).name, reuse.ALLOWED_CHANGES)
        for changed in ('build.sh', 'deps/3rd/obclient.txt', 'src/sql/evil.cpp',
                reuse.PREFIX + 'fts_plan_compatibility.py', 'tools/deploy/mysql_test/test_suite/fts_index/t/mysql/simple_query.test'):
            with self.subTest(changed=changed), patch.object(reuse, 'source_identity', return_value={'tree': 'd' * 40}), \
                    patch.object(reuse, 'git', side_effect=[reuse.B3_TREE.encode(), reuse.PRODUCTION_KERNEL_TREE.encode(), changed.encode()]), \
                    self.assertRaisesRegex(RuntimeError, 'outside explicit CI allowlist'):
                reuse.verify_driver(self.source, D, C235)

    def fresh_stages(self):
        for stage in ('cases/baseline', 'cases/candidate', 'untimed', 'rounds'):
            (self.root / 'validation' / stage).mkdir(parents=True, exist_ok=True)

    def test_missing_or_nonempty_fresh_parent_stops_before_any_module_or_loader(self):
        stages = ('cases/baseline', 'cases/candidate', 'untimed', 'rounds')
        self.fresh_stages()
        with patch.object(reuse, 'load_module') as module, patch.object(reuse, 'verify_loader') as loader:
            for stage in stages:
                directory = self.root / 'validation' / stage
                directory.rmdir()
                with self.subTest(stage=stage, mode='missing'), self.assertRaisesRegex(RuntimeError, 'exist and be empty'):
                    reuse.validate(self.args, {}, {})
                directory.mkdir()
                (directory / 'old-result.json').write_text('{}')
                with self.subTest(stage=stage, mode='stale'), self.assertRaisesRegex(RuntimeError, 'exist and be empty'):
                    reuse.validate(self.args, {}, {})
                (directory / 'old-result.json').unlink()
            module.assert_not_called()
            loader.assert_not_called()

    def test_each_old_result_field_is_rejected_even_with_fresh_directories(self):
        self.fresh_stages()
        with patch.object(reuse, 'load_module') as module:
            for key in ('instances', 'caseVerdicts', 'untimedVerdicts', 'roundVerdicts', 'roundMetrics',
                    'supplementalCompatibility', 'sameHostPerformance', 'planAssertions'):
                with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'cannot resume/reuse'):
                    reuse.validate(self.args, {}, {key: [{'old': True}]})
            module.assert_not_called()

    def runtime_gate(self, bad_client=None, reject_identity=None):
        """Stop before native case discovery; every process boundary is mocked."""
        self.fresh_stages()
        sources = {role: self.root / 'sources' / role for role in ('baseline', 'candidate')}
        for source in sources.values():
            source.mkdir(parents=True, exist_ok=True)
        events = []
        identities = {role: dict(sourceCommit=A if role == 'baseline' else B3, sourceTree='b' * 40,
            kernelOrigin=A if role == 'baseline' else C235, driverCommit=FE if role == 'baseline' else B3,
            sha256='f' * 64, bytes=1, buildId='fixture', officialInputHashes={}) for role in sources}
        reuse.write_json(self.root / 'runtime/candidate/identity.original.json', identities['candidate'])
        binaries = {role: self.root / 'runtime' / role / 'seekdb' for role in sources}
        def identity(role):
            events.append(('identity', role))
            if role == reject_identity:
                raise RuntimeError('full ELF hash rejected')
            return (binaries[role], identities[role]) if role == 'baseline' else binaries[role]
        def digest(path):
            events.append(('hash', Path(path).name))
            return 'wrong' if Path(path).name == bad_client else CLIENTS[Path(path).name]
        def loader(binary, log, source, env):
            events.append(('loader', Path(binary).name, source, dict(env)))
            return {}
        native = types.SimpleNamespace(discover_cases=lambda source: (_ for _ in ()).throw(RuntimeError('stop before native cases')))
        harness = types.SimpleNamespace(original_inputs=lambda source: {}, load_native=lambda source: native)
        scorer = types.SimpleNamespace(load_baseline=lambda path: {})
        plan = dict(baselineSource=str(sources['baseline']), candidateSource=str(sources['candidate']),
            artifact={}, candidateRelocation={}, runtimeVerificationSha256='f' * 64)
        with ExitStack() as stack:
            replacements = dict(load_module=lambda name, path: harness if name == 'paired_original_harness' else scorer,
                verify_driver=lambda *args: {}, source_identity=lambda *args: {'tree': 'b' * 40},
                historical_baseline=lambda *args: identity('baseline'), candidate_identity=lambda *args: identity('candidate'),
                verify_loader=loader, digest=digest, persist_report=lambda *args: None)
            for name, value in replacements.items():
                stack.enter_context(patch.object(reuse, name, value))
            with self.assertRaises(RuntimeError) as error:
                reuse.validate(self.args, plan, {'builds': {}})
        return events, str(error.exception), sources, binaries

    def test_A_B3_and_clients_use_actual_B3_library_path_after_identity_and_hashes(self):
        events, error, sources, binaries = self.runtime_gate()
        self.assertEqual(error, 'stop before native cases')
        loaders = [event for event in events if event[0] == 'loader']
        self.assertEqual([event[1] for event in loaders], ['seekdb', 'obclient', 'mysqltest'] * 2)
        for offset, role in enumerate(('baseline', 'candidate')):
            loader = loaders[offset * 3]
            self.assertLess(events.index(('identity', role)), events.index(loader))
            for event in loaders[offset * 3:offset * 3 + 3]:
                self.assertEqual(event[2], sources[role])
                expected = [str(binaries[role].parent)] + [str(sources['candidate'] / path) for path in (
                    'deps/3rd/u01/obclient/lib', 'deps/3rd/u01/obclient/lib64',
                    'deps/3rd/usr/local/oceanbase/deps/devel/lib', 'deps/3rd/usr/local/oceanbase/deps/devel/lib64')]
                self.assertEqual(event[3]['LD_LIBRARY_PATH'].split(':'), expected)
                if event[1] in CLIENTS:
                    self.assertEqual(events[events.index(event) - 1], ('hash', event[1]))

    def test_bad_full_identity_or_client_hash_never_reaches_its_loader(self):
        for role in ('baseline', 'candidate'):
            events, error, _, _ = self.runtime_gate(reject_identity=role)
            self.assertEqual(error, 'full ELF hash rejected')
            self.assertEqual(sum(event[0] == 'loader' and event[1] == 'seekdb' for event in events), int(role == 'candidate'))
        for client in CLIENTS:
            events, error, _, _ = self.runtime_gate(bad_client=client)
            self.assertIn('Official native client changed', error)
            self.assertFalse(any(event[:2] == ('loader', client) for event in events))

    def test_zero_build_plan_demands_23_new_owned_cleanups(self):
        plan = reuse.plan_for(self.source, self.root, D, C235)
        self.assertEqual([plan[key] for key in ('candidateBuildsRequired', 'freshNativeCasesRequired',
            'freshUntimedInstancesRequired', 'freshTimedRoundsRequired', 'ownedCleanupsRequired')], [0, 13, 2, 8, 23])
        self.assertEqual(plan['candidateBuildCommit'], B3)
        self.assertEqual(plan['candidateBuildDriverCommit'], B3)
        self.assertEqual(plan['validationDriverCommit'], D)
        self.assertFalse(plan['historicalB3ValidationAccepted'])

    def test_completion_requires_each_of_23_ordered_owned_cleanups_and_exact_build_roles(self):
        expected = [(role, name) for role in ('baseline', 'candidate') for name in reuse.selected_cases(role)]
        expected += [('baseline', 'tokenize-baseline'), ('candidate', 'tokenize-candidate')]
        expected += [(role, name) for name, role in reuse.SCHEDULE]
        self.assertEqual(len(expected), 23)
        instances = [dict(role=role, name=name, status='passed', cleanupPassed=True) for role, name in expected]
        report = dict(status='valid_calibration_completed', mode=reuse.MODE, driverCommit=D,
            candidateBuildCommit=B3, candidateBuildDriverCommit=B3, validationDriverCommit=D,
            candidateRebuilt=False, historicalB3ValidationAccepted=False, kernelOrigin=C235, baselineCommit=A,
            allOfficialInputsUnchanged=True, formalContestRun=False, historicalCandidateExecuted=False,
            baselineRebuilt=False, instances=instances, caseVerdicts=copy.deepcopy(instances[:13]),
            untimedVerdicts=copy.deepcopy(instances[13:15]), roundVerdicts=copy.deepcopy(instances[15:]),
            builds={'baseline': {'sourceCommit': A, 'driverCommit': reuse.legacy.CANDIDATE},
                'candidate': {'sourceCommit': B3, 'kernelOrigin': C235, 'driverCommit': B3}},
            roundMetrics={name: {} for name, _ in reuse.SCHEDULE}, sameHostPerformance={'fixture': True})
        reuse.write_json(self.root / 'runtime/candidate/identity.original.json', {})
        plan = dict(candidateSource=str(self.root / 'sources/candidate'), equivalence={'caseInputs': {}})
        scorer = types.SimpleNamespace(load_baseline=lambda path: {})
        with ExitStack() as stack:
            for name, value in dict(case_evidence_complete=lambda *args: True,
                    accepted_plan_assertions=lambda *args: True, tokenize_complete=lambda *args: True,
                    load_plan=lambda *args: plan, candidate_identity=lambda *args: None,
                    historical_baseline=lambda *args: None, load_module=lambda *args: scorer,
                    performance=lambda *args: {'fixture': True}).items():
                stack.enter_context(patch.object(reuse, name, value))
            self.assertTrue(reuse.completed(report, self.args, plan))
            for index in range(23):
                for defect in ('missing', 'failed', 'reordered'):
                    changed = copy.deepcopy(report)
                    if defect == 'missing':
                        changed['instances'].pop(index)
                    elif defect == 'failed':
                        changed['instances'][index]['cleanupPassed'] = False
                    else:
                        changed['instances'][index]['name'] = 'old-round'
                    with self.subTest(index=index, defect=defect):
                        self.assertFalse(reuse.completed(changed, self.args, plan))
            for key, value in (('candidateBuildCommit', D), ('candidateBuildDriverCommit', D),
                    ('historicalB3ValidationAccepted', True), ('candidateRebuilt', True)):
                with self.subTest(key=key):
                    self.assertFalse(reuse.completed(dict(report, **{key: value}), self.args, plan))

    def test_present_logs_full_unmodified_report_without_claiming_partial_success(self):
        report = dict(status='invalid_calibration', failure={'phase': 'validate', 'reason': 'fixture failure'},
            sameHostPerformance={'classification': 'improvement'}, caseVerdicts=[{'status': 'failed'}])
        original = copy.deepcopy(report)
        self.args.summary = None
        output = io.StringIO()
        with redirect_stdout(output):
            reuse.present(report, self.args, None)
        visible_line, remainder = output.getvalue().split('\n', 1)
        visible = json.loads(visible_line.removeprefix('B3_REUSE_VISIBLE_RESULT '))
        self.assertEqual(visible['result'], 'invalid/incomplete; no performance claim')
        self.assertFalse(visible['originalB3ValidationAccepted'])
        self.assertTrue(remainder.startswith('B3_REUSE_FULL_REPORT\n'))
        self.assertEqual(json.loads(remainder.removeprefix('B3_REUSE_FULL_REPORT\n')), original)
        self.assertEqual(report, original)

    def test_collector_text_failure_preserves_current_and_historical_verdict_bytes(self):
        self.root.mkdir()
        current = b'{"status":"valid_calibration_completed","fresh":true}\n'
        historical = b'{"status":"invalid_calibration","failure":"missing parent"}\n'
        (self.root / 'report.json').write_bytes(current)
        old = self.root / 'runtime/candidate/original/evidence/report.json'
        old.parent.mkdir(parents=True)
        old.write_bytes(historical)
        bad = self.root / 'runtime/candidate/relocation.json'
        for content in (b'not JSON', b'\xff', b'\x7fELF'):
            bad.write_bytes(content)
            with tempfile.TemporaryDirectory(dir=self.base) as directory:
                out = Path(directory) / 'evidence'
                with self.subTest(content=content), self.assertRaises((ValueError, UnicodeError, RuntimeError)):
                    reuse.main(self.argv('collect-evidence') + ['--out', str(out)])
            self.assertEqual((self.root / 'report.json').read_bytes(), current)
            self.assertEqual(old.read_bytes(), historical)


if __name__ == '__main__':
    unittest.main()
