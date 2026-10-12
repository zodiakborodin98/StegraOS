#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Verify/fetch locked Steam files and build a fresh, offline candidate home.

No target code, package installer, Steam updater or login is run. This does not
enable a session or claim the resulting client works on Nintendo Switch.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile

PROJECT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('pinned_inputs', PROJECT / 'scripts/prepare-inputs.py')
pinned_inputs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pinned_inputs)


def real_parents(path):
    """Reject symlink/junction output parents instead of silently resolving them."""
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
            raise ValueError(f'Symlink or junction path not allowed: {item}')


def verify(path, artifact):
    real_parents(path)
    if not path.is_file() or path.stat().st_size != artifact['size']:
        raise ValueError(f'Missing or wrong-sized input: {path}')
    sha256 = hashlib.sha256()
    blob = hashlib.sha1(f'blob {artifact["size"]}\0'.encode())
    with path.open('rb') as src:
        while chunk := src.read(8 * 1024 * 1024):
            sha256.update(chunk)
            blob.update(chunk)
    if sha256.hexdigest() != artifact['sha256']:
        raise ValueError(f'SHA256 mismatch: {path}')
    if artifact.get('git_blob_sha1') and blob.hexdigest() != artifact['git_blob_sha1']:
        raise ValueError(f'Pinned Git blob mismatch: {path}')


def relative_name(name):
    if '\\' in name or '\x00' in name or ':' in name:
        raise ValueError(f'Invalid archive path: {name!r}')
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError(f'Unsafe archive path: {name!r}')
    return path


def extract_tar(archive, destination, *, mode='r:*'):
    """Use Python's data filter, with strict names, size limit and no devices."""
    if not hasattr(tarfile, 'data_filter'):
        raise ValueError('Python with tarfile.data_filter is required (3.12+ recommended)')
    total = count = 0
    with tarfile.open(fileobj=archive, mode=mode) as tar:
        for member in tar:
            relative_name(member.name)
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError(f'Unexpected archive member: {member.name}')
            total += member.size
            count += 1
            if total > 4 * 1024**3 or count > 100000:
                raise ValueError('Archive exceeds the expected client size/entry limit')
            # data_filter rejects absolute/outside links, strips owner/setuid/write
            # permissions and resolves each extraction path against destination.
            tar.extract(member, destination, filter='data')


def extract_zip(path, destination, allowed):
    total = 0
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            # Valve's UI zip contains mixed slash conventions, including
            # duplicate `steamui\\css/` directory entries. Normalize before
            # applying the same traversal checks; never interpret backslashes
            # differently on Windows and Linux.
            relative = relative_name(member.filename.replace('\\', '/'))
            if not relative.parts or relative.parts[0] not in allowed:
                raise ValueError(f'Unexpected zip member: {member.filename}')
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f'Zip symlink not allowed: {member.filename}')
            target = destination.joinpath(*relative.parts)
            real_parents(target)
            total += member.file_size
            if total > 512 * 1024**2:
                raise ValueError('Zip exceeds the expected client size')
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as src, target.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
                target.chmod(0o755 if mode & 0o111 else 0o644)


@contextlib.contextmanager
def native_archive(cache):
    # The joined copy is temporary. Never trust an unverified previous concat.
    with tempfile.TemporaryFile() as joined:
        for suffix in ('aa', 'ab', 'ac'):
            with (cache / f'steamrtarm64.tar.gz.part{suffix}').open('rb') as src:
                shutil.copyfileobj(src, joined)
        joined.seek(0)
        yield joined


def native_overlay(cache, output):
    for folder in ('linuxarm64', 'steamrtarm64'):
        (output / folder).mkdir()
    with (cache / 'linuxarm64.tar.gz').open('rb') as src:
        extract_tar(src, output / 'linuxarm64')
    with native_archive(cache) as src:
        extract_tar(src, output / 'steamrtarm64')


@contextlib.contextmanager
def decompress_zstd(path):
    # zstd is a HOST utility. This never calls a program from the target rootfs.
    with path.open('rb') as source:
        process = subprocess.Popen(['zstd', '-dc'], stdin=source, stdout=subprocess.PIPE)
        try:
            yield process.stdout
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            process.stdout.close()
        if process.wait() != 0:
            raise ValueError('zstd failed while reading the stock Steam archive')


def build_candidate(stage, cache, lock):
    if sys.platform != 'linux':
        raise ValueError('Build the candidate home in Linux/WSL, on its native filesystem')
    stage = Path(os.path.abspath(stage))
    real_parents(stage)
    stage_lock = stage / 'sources.lock.json'
    if not (stage / 'STATUS.txt').is_file() or not stage_lock.is_file():
        raise ValueError('Expected a work directory created by stage-rootfs.sh')
    source = json.loads(stage_lock.read_text())
    if source['steamos']['rootfs_sha256'] != lock['stock_base']['source_rootfs_sha256']:
        raise ValueError('Staged source rootfs does not match the client lock')
    rootfs = stage / 'rootfs'
    archive = rootfs / lock['stock_base']['path'].lstrip('/')
    verify(archive, lock['stock_base'])
    output = stage / 'steam-client-candidate-home'
    real_parents(output)
    if output.exists():
        raise ValueError(f'Refusing to overwrite an existing candidate home: {output}')
    if shutil.disk_usage(stage).free < 5 * 1024**3:
        raise ValueError('At least 5 GiB free workspace space is required')
    output.mkdir(mode=0o700)
    # The output is a new isolated directory. It does not refer to $HOME or to
    # rootfs/home/steamos, and it is not automatically copied to either location.
    steam = output / '.local/share/Steam'
    steam.mkdir(parents=True)
    (output / 'CANDIDATE-INCOMPLETE.txt').write_text('Build in progress; do not install.\n')
    with decompress_zstd(archive) as src:
        extract_tar(src, steam, mode='r|')
    # Overlay only the pinned compatibility components, not installer scripts.
    with (cache / 'linuxarm64.tar.gz').open('rb') as src:
        extract_tar(src, steam / 'linuxarm64')
    with native_archive(cache) as src:
        extract_tar(src, steam / 'steamrtarm64')
    extract_zip(cache / 'linux_x86_64.zip', steam, {'linux32', 'linux64'})
    # Retain unrelated stock UI assets for now; this combination is a candidate.
    extract_zip(cache / 'steamui_websrc_all.zip', steam, {'steamui'})
    pv = steam / 'steamrtarm64/pv-runtime'
    pv.mkdir(exist_ok=True)
    with (cache / 'steam-runtime-steamrt-arm64.tar.xz').open('rb') as src:
        extract_tar(src, pv)
    shutil.copyfile(cache / 'steam-runtime-steamrt-arm64.sh', pv / 'steam-runtime-steamrt-arm64.sh')
    (pv / 'steam-runtime-steamrt-arm64.sh').chmod(0o755)
    # An explicit frozen client is essential: updating can reintroduce SIGILL.
    for target in (steam / 'steam.cfg', steam / 'steamrtarm64/steam.cfg'):
        real_parents(target)
        shutil.copyfile(cache / 'steam.cfg', target)
        target.chmod(0o644)
    (steam / 'package/beta').write_text('publicbeta\n')
    config = output / '.steam'
    config.mkdir()
    for name, target in {
        'steam': '../.local/share/Steam', 'root': '../.local/share/Steam',
        'sdk32': '../.local/share/Steam/linux32', 'sdk64': '../.local/share/Steam/linux64',
        'sdkarm64': '../.local/share/Steam/linuxarm64',
        'binarm64': '../.local/share/Steam/steamrtarm64',
        'bin32': '../.local/share/Steam/ubuntu12_32',
        'bin64': '../.local/share/Steam/ubuntu12_64',
    }.items():
        (config / name).symlink_to(target)
    # Fail rather than accidentally propagate any pre-existing account state.
    for bad in ('config/loginusers.vdf', 'config/config.vdf', 'userdata', 'registry.vdf'):
        if (steam / bad).exists():
            raise ValueError(f'Unexpected account state in source archive: {bad}')
    if list(steam.glob('ssfn*')):
        raise ValueError('Unexpected Steam authentication file in source archive')
    (output / 'steam-client.lock.json').write_text(json.dumps(lock, indent=2) + '\n')
    (output / 'CANDIDATE-INCOMPLETE.txt').unlink()
    (output / 'CANDIDATE-STATUS.txt').write_text(
        'OFFLINE STEAM CLIENT CANDIDATE — NOT RUN OR HARDWARE VALIDATED\n'
        'Source: locked genuine SteamOS archive + pinned Switchdeck downgrade + Valve ARM runtime.\n'
        'No accounts, target programs, installer scripts or updaters were used.\n'
        'Stock RUNSTEAM.sh is still Frame-specific; do not enable stock steam.service.\n'
        'No Gamescope integration, Proton, Box64, FEX, GPU or game validation is implied.\n'
        'The stock archive and old client assets may have incompatible residual files.\n')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, default=PROJECT / 'downloads/steam-compat')
    parser.add_argument('--fetch', action='store_true', help='download missing locked files; otherwise offline verification only')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--native-overlay', type=Path, help='extract native ARM candidates into a NEW directory (no execution)')
    actions.add_argument('--stage', type=Path, help='create a NEW steam-client-candidate-home beneath an existing staging workdir')
    args = parser.parse_args()
    lock = json.loads((PROJECT / 'steam-client.lock.json').read_text())
    cache = Path(os.path.abspath(args.cache))
    real_parents(cache)
    cache.mkdir(parents=True, exist_ok=True)
    for artifact in lock['artifacts']:
        if args.fetch:
            pinned_inputs.fetch(artifact, cache)
        verify(cache / artifact['name'], artifact)
    print(f'Verified {len(lock["artifacts"])} pinned client/runtime inputs', flush=True)
    if args.native_overlay:
        output = Path(os.path.abspath(args.native_overlay))
        real_parents(output)
        output.mkdir()  # Intentionally fails for any existing destination.
        native_overlay(cache, output)
        print(f'Native compatibility candidates extracted: {output}; no target programs executed')
    if args.stage:
        print(f'Candidate home: {build_candidate(args.stage, cache, lock)}')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        sys.exit(f'ERROR: {error}')
