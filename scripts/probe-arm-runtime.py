#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run bounded CLI smoke checks using QEMU's Cortex-A57 CPU model.

This tests startup/CPU instructions in selected commands only. The kernel is the
build host's kernel; it is NOT a Switch boot test or an L4T/GPU compatibility test.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import subprocess
import tempfile
import time

spec = importlib.util.spec_from_file_location('root_audit', Path(__file__).with_name('audit-rootfs.py'))
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

CHECKS = (
    ('libc', '/usr/lib/libc.so.6', []),
    ('bash', '/usr/bin/bash', ['--version']),
    ('true', '/usr/bin/true', []),
    ('systemd', '/usr/lib/systemd/systemd', ['--version']),
    ('udevadm', '/usr/bin/udevadm', ['--version']),
    ('pacman', '/usr/bin/pacman', ['--version']),
    ('python3', '/usr/bin/python3', ['--version']),
    ('xxd', '/usr/bin/xxd', ['-v']),
    ('gamescope', '/usr/bin/gamescope', ['--version']),
    ('kwin_x11', '/usr/bin/kwin_x11', ['-platform', 'offscreen', '--version']),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('rootfs', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--timeout', type=int, default=30)
    parser.add_argument('--checks', nargs='+', choices=[item[0] for item in CHECKS],
                        help='Run only the selected checks')
    args = parser.parse_args()
    if platform.system() != 'Linux':
        parser.error('This runtime check requires Linux with qemu-user-static.')
    qemu = shutil.which('qemu-aarch64-static')
    if not qemu:
        parser.error('qemu-aarch64-static is missing')
    if not 1 <= args.timeout <= 120:
        parser.error('timeout must be 1..120 seconds')
    if args.output.exists() or args.output.is_symlink():
        parser.error('Refusing to overwrite an existing report')
    fs = audit.RootFS(args.rootfs)
    if fs.root == Path('/'):
        parser.error('Rootfs cannot be the build host filesystem')
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    report = {'cpu_model': 'cortex-a57', 'host_kernel': platform.release(),
              'qemu': subprocess.check_output([qemu, '--version'], text=True).splitlines()[0],
              'limitation': 'User-mode emulation, host syscalls. No L4T kernel, Switch GPU, boot, or game tested.',
              'rootfs': str(fs.root), 'checks': []}
    with tempfile.TemporaryDirectory(prefix='steamos-a57-cli-') as temporary:
        env = {'PATH': '/usr/bin:/bin', 'HOME': temporary, 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
               'XDG_CACHE_HOME': temporary, 'QT_QPA_PLATFORM': 'offscreen',
               'QT_PLUGIN_PATH': str(fs.resolve('/usr/lib/qt6/plugins')),
               'QT_QPA_PLATFORM_PLUGIN_PATH': str(fs.resolve('/usr/lib/qt6/plugins/platforms'))}
        for name, logical, extra in CHECKS:
            if args.checks and name not in args.checks:
                continue
            entry = {'name': name, 'logical_path': logical, 'arguments': extra}
            try:
                executable = fs.resolve(logical)
                info = audit.inspect_elf(executable)
                if info.get('architecture') != 'aarch64':
                    raise ValueError('Expected an AArch64 ELF, not a script or foreign executable')
                command = [qemu, '-cpu', 'cortex-a57', '-L', str(fs.root), str(executable), *extra]
                started = time.monotonic()
                result = subprocess.run(command, cwd=temporary, env=env, capture_output=True,
                                        text=True, errors='replace', timeout=args.timeout)
                entry.update(returncode=result.returncode, seconds=round(time.monotonic() - started, 3),
                             stdout=result.stdout[-6000:], stderr=result.stderr[-6000:])
                entry['status'] = 'passed' if result.returncode == 0 else 'startup-failed'
                if result.returncode in (-4, 132) or 'uncaught target signal 4' in result.stderr:
                    entry['status'] = 'illegal-instruction'
            except subprocess.TimeoutExpired:
                entry['status'] = 'timeout'
            except (OSError, ValueError) as error:
                entry.update(status='not-run', error=str(error))
            report['checks'].append(entry)
            print(f'{name}: {entry["status"]}', flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Report: {args.output}')
    return int(any(check['status'] != 'passed' for check in report['checks']))


if __name__ == '__main__':
    raise SystemExit(main())
