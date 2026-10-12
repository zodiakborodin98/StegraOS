#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Install the reviewed offline Steam candidate into an unused staged user home.

Default is a read-only plan. --apply changes only rootfs/home/steamos and writes
an installation report alongside rootfs. It never starts Steam or a service.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys

PROJECT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('candidate_builder', PROJECT / 'scripts/prepare-steam-client.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

SHELL_FILES = ('.bashrc', '.bash_profile', '.bash_logout')
LAUNCHER = r'''#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Manual hardware test launcher for a frozen Switch-compatible client.
set -euo pipefail
if [[ $(id -u) != 1000 || $HOME != /home/steamos ]]; then
    printf 'Run as the SteamOS steamos user, with HOME=/home/steamos.\n' >&2
    exit 1
fi
if [[ -z ${DISPLAY:-} ]]; then
    printf 'Steam requires a working graphical session with X11/Xwayland DISPLAY.\n' >&2
    exit 1
fi
steamroot="$HOME/.local/share/Steam"
nvidia_run=/usr/lib/steamos-switch-nvidia/run
if [[ ! -x $nvidia_run ]]; then
    printf 'The Switch NVIDIA environment wrapper is missing.\n' >&2
    exit 1
fi
if [[ ! -x $steamroot/steamrtarm64/steam ]] ||
   ! grep -qx 'BootStrapperInhibitAll=enable' "$steamroot/steam.cfg"; then
    printf 'The frozen Steam client is missing or its update inhibition changed.\n' >&2
    exit 1
fi
export SYSTEM_PATH="$PATH"
export SYSTEM_LD_LIBRARY_PATH="${LD_LIBRARY_PATH-}"
export LD_LIBRARY_PATH="$steamroot/steamrtarm64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export STEAM_LAUNCH_WRAPPER_AFFINITY_LIST=0xf
export STEAM_DESKTOP_APP_AFFINITY_LIST=0xf
export PROTON_USE_WOW64=1
export WINEESYNC=0 PROTON_NO_ESYNC=1 PROTON_NO_FSYNC=1 PROTON_NO_NTSYNC=1
export STAGING_WRITECOPY=1 STAGING_SHARED_MEMORY=1
export __GL_THREADED_OPTIMIZATIONS=1
# No DXVK-Sarek/FEX/Box64 variable promises a tool that this image has not installed.
args=(-vrskip -vrdisable -noverifyfiles)
if [[ ${1:-} == --gamepad ]]; then
    shift
    args+=(-gamepadui -steamdeck -steamos3 -720p)
fi
cd "$steamroot"
for attempt in 1 2 3 4 5; do
    status=0
    "$nvidia_run" "$steamroot/steamrtarm64/steam" "${args[@]}" "$@" || status=$?
    [[ $status == 42 ]] || exit "$status"
done
printf 'Steam repeatedly requested a restart; stopping after five attempts.\n' >&2
exit 1
'''

WEBHELPER = r'''#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Keep CEF on the available Switch CPUs; do not impose Frame's taskset mask.
set -euo pipefail
if [[ -z ${DISPLAY:-} ]]; then
    printf 'steamwebhelper: no graphical DISPLAY is available.\n' >&2
    exit 1
fi
directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export LIBGL_KOPPER_DISABLE=true
cd "$directory"
exec "$directory/steamwebhelper" "$@"
'''

DESKTOP = '''[Desktop Entry]
Version=1.0
Type=Application
Name=Steam (Switch test)
Comment=Manually test the frozen ARM Steam client in a working graphical session
Exec=/home/steamos/.local/bin/steam-switch %U
Icon=steam
Terminal=false
StartupNotify=true
Categories=Game;
MimeType=x-scheme-handler/steam;x-scheme-handler/steamlink;
Actions=Gamepad;

[Desktop Action Gamepad]
Name=Gamepad UI (experimental)
Exec=/home/steamos/.local/bin/steam-switch --gamepad
'''


def sha256(path):
    with path.open('rb') as src:
        return hashlib.file_digest(src, 'sha256').hexdigest()


def validate_destination(destination, allow_empty):
    builder.real_parents(destination)
    if destination.exists():
        if not allow_empty:
            raise ValueError('Existing home refused; --allow-empty-stock-home is required for the empty stock directory')
        info = destination.stat()
        if not destination.is_dir() or (info.st_uid, info.st_gid) != (1000, 1000):
            raise ValueError('Existing destination must be a directory owned by 1000:1000')
        if any(destination.iterdir()):
            raise ValueError('Existing user home is not empty; it will not be modified')


def inventory(candidate):
    """Walk without following links; copied links must stay within candidate."""
    records = []
    for directory, dirs, files in os.walk(candidate, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            relative = path.relative_to(candidate).as_posix()
            metadata = path.lstat()
            if path.is_symlink():
                target = os.readlink(path)
                if Path(target).is_absolute() or not path.resolve().is_relative_to(candidate):
                    raise ValueError(f'Candidate link leaves its home: {relative}')
                record = {'path': relative, 'type': 'symlink', 'target': target}
            elif path.is_dir():
                record = {'path': relative, 'type': 'directory'}
            elif path.is_file():
                record = {'path': relative, 'type': 'file', 'size': metadata.st_size,
                          'sha256': sha256(path)}
            else:
                raise ValueError(f'Candidate contains a special file: {relative}')
            records.append(record)
    return sorted(records, key=lambda x: x['path'])


def validate(stage, allow_empty):
    if sys.platform != 'linux':
        raise ValueError('Run in the Linux/WSL build environment')
    stage = Path(os.path.abspath(stage))
    builder.real_parents(stage)
    if not (stage / 'STATUS.txt').is_file() or not (stage / 'sources.lock.json').is_file():
        raise ValueError('Expected a stage-rootfs.sh work directory')
    rootfs = stage / 'rootfs'
    builder.real_parents(rootfs)
    if rootfs == Path('/') or rootfs.resolve() == Path('/'):
        raise ValueError('Refusing the live host root')
    lock = json.loads((PROJECT / 'steam-client.lock.json').read_text())
    source = json.loads((stage / 'sources.lock.json').read_text())
    if source['steamos']['rootfs_sha256'] != lock['stock_base']['source_rootfs_sha256']:
        raise ValueError('Wrong source SteamOS build')
    # Read the offline account database directly, never getpwnam on the host.
    passwd = rootfs / 'etc/passwd'
    builder.real_parents(passwd)
    rows = [line.split(':') for line in passwd.read_text().splitlines() if line.startswith('steamos:')]
    if len(rows) != 1 or rows[0][2:4] != ['1000', '1000'] or rows[0][5:] != ['/home/steamos', '/bin/bash']:
        raise ValueError('Expected offline steamos account UID/GID 1000 and /home/steamos')
    group = rootfs / 'etc/group'
    builder.real_parents(group)
    if not any(line.split(':')[:3] == ['steamos', 'x', '1000'] for line in group.read_text().splitlines()):
        raise ValueError('Expected offline steamos group GID 1000')
    candidate = stage / 'steam-client-candidate-home'
    builder.real_parents(candidate)
    if (candidate / 'CANDIDATE-INCOMPLETE.txt').exists() or not (candidate / 'CANDIDATE-STATUS.txt').is_file():
        raise ValueError('Candidate build is missing or incomplete')
    if json.loads((candidate / 'steam-client.lock.json').read_text()) != lock:
        raise ValueError('Candidate input lock differs from reviewed project lock')
    steam = candidate / '.local/share/Steam'
    frozen = next(a for a in lock['artifacts'] if a['name'] == 'steam.cfg')
    builder.verify(steam / 'steam.cfg', frozen)
    builder.verify(steam / 'steamrtarm64/steam.cfg', frozen)
    for forbidden in ('.config', '.local/bin', 'Desktop', '.steam/registry.vdf',
                      '.local/share/Steam/config/loginusers.vdf', '.local/share/Steam/userdata'):
        if (candidate / forbidden).exists():
            raise ValueError(f'Unexpected mutable/user/session data in candidate: {forbidden}')
    if list(steam.glob('ssfn*')):
        raise ValueError('Steam authentication data found in candidate')
    for file in ('steamrtarm64/steam', 'steamrtarm64/steamui.so', 'steamrtarm64/steamwebhelper'):
        path = steam / file
        builder.real_parents(path)
        with path.open('rb') as src:
            if src.read(4) != b'\x7fELF':
                raise ValueError(f'Expected native ELF: {file}')
    destination = rootfs / 'home/steamos'
    validate_destination(destination, allow_empty)
    nvidia_run = rootfs / 'usr/lib/steamos-switch-nvidia/run'
    builder.real_parents(nvidia_run)
    if not nvidia_run.is_file() or not os.access(nvidia_run, os.X_OK):
        raise ValueError('Install the reviewed private Switch NVIDIA wrapper before this client')
    for name in SHELL_FILES:
        path = rootfs / 'etc/skel' / name
        builder.real_parents(path)
        if not path.is_file() or path.stat().st_size > 16384:
            raise ValueError(f'Unexpected stock shell skeleton: {name}')
    report = stage / 'steam-candidate-installed.json'
    if report.exists():
        raise ValueError('Existing installation report: refusing to repeat installation')
    return stage, rootfs, candidate, destination, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--allow-empty-stock-home', action='store_true')
    args = parser.parse_args()
    stage, rootfs, candidate, destination, report = validate(args.stage, args.allow_empty_stock_home)
    plan = {'status': 'review-plan', 'source': str(candidate), 'destination': str(destination),
            'user': 'steamos', 'uid': 1000, 'gid': 1000,
            'stock_skeleton_allowlist': list(SHELL_FILES),
            'manual_launcher': '/home/steamos/.local/bin/steam-switch',
            'graphics_wrapper': '/usr/lib/steamos-switch-nvidia/run',
            'desktop_entry': '/home/steamos/.local/share/applications/steam-switch.desktop',
            'services_enabled': [], 'autostart_entries': [],
            'changes_to_candidate': ['Replace only steamwebhelper.sh with a wrapper without Frame CPU affinity'],
            'limitations': ['Client has not been launched', 'No working graphics session is asserted',
                            'No Proton, Box64 or FEX installed', 'No automatic Steam startup']}
    if not args.apply:
        print(json.dumps(plan, indent=2))
        return
    if os.geteuid() != 0:
        raise ValueError('Root is required only for --apply to set offline UID/GID 1000')
    if shutil.disk_usage(destination.parent).free < 5 * 1024**3:
        raise ValueError('At least 5 GiB free staging storage is required')
    records = inventory(candidate)
    validate_destination(destination, args.allow_empty_stock_home)
    destination.mkdir(mode=0o700, exist_ok=args.allow_empty_stock_home)
    incomplete = destination / 'STEAM-INSTALL-INCOMPLETE.txt'
    incomplete.write_text('Offline install in progress. No Steam launch or services enabled.\n')
    # This is an unused home, not an update/copy onto a live Steam profile.
    for directory in ('.local', '.steam'):
        shutil.copytree(candidate / directory, destination / directory, symlinks=True)
    for name in SHELL_FILES:
        shutil.copyfile(rootfs / 'etc/skel' / name, destination / name)
        (destination / name).chmod(0o644)
    bindir = destination / '.local/bin'
    bindir.mkdir()
    launcher = bindir / 'steam-switch'
    launcher.write_text(LAUNCHER)
    launcher.chmod(0o755)
    webhelper = destination / '.local/share/Steam/steamrtarm64/steamwebhelper.sh'
    webhelper.write_text(WEBHELPER)
    webhelper.chmod(0o755)
    for folder in (destination / '.local/share/applications', destination / 'Desktop'):
        folder.mkdir(parents=True)
        shortcut = folder / 'steam-switch.desktop'
        shortcut.write_text(DESKTOP)
        shortcut.chmod(0o755 if folder.name == 'Desktop' else 0o644)
    notes = destination / '.local/share/steamos-switch'
    notes.mkdir()
    shutil.copyfile(candidate / 'steam-client.lock.json', notes / 'steam-client.lock.json')
    shutil.copyfile(candidate / 'CANDIDATE-STATUS.txt', notes / 'CANDIDATE-STATUS.txt')
    # lchown links themselves: never traverse .steam symlinks during ownership.
    os.chown(destination, 1000, 1000)
    destination.chmod(0o700)
    for directory, dirs, files in os.walk(destination, followlinks=False):
        for name in dirs + files:
            os.chown(Path(directory) / name, 1000, 1000, follow_symlinks=False)
    incomplete.unlink()
    plan.update(status='installed-offline-not-launched', source_inventory=records,
                launcher_sha256=sha256(launcher), webhelper_wrapper_sha256=sha256(webhelper))
    report.write_text(json.dumps(plan, indent=2) + '\n')
    print(f'Installed offline Steam candidate: {destination}; no services enabled or client launched')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError) as error:
        sys.exit(f'ERROR: {error}')
