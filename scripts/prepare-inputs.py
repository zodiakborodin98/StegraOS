#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Fetch only the pinned input packages. Never installs or executes their contents."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlparse
from urllib.request import urlopen

PROJECT = Path(__file__).resolve().parent.parent


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def fetch(artifact, directory):
    name = artifact['name']
    if Path(name).name != name or '/' in name or '\\' in name:
        raise ValueError('Artifact name must be a filename')
    target = directory / name
    if target.is_symlink():
        raise ValueError(f'Refusing symlink: {target}')
    if target.exists():
        if target.is_file() and target.stat().st_size == artifact['size'] and digest(target) == artifact['sha256']:
            print(f'Verified existing {name}', flush=True)
            return
        raise ValueError(f'Existing file does not match lock: {target}')
    if urlparse(artifact['url']).scheme != 'https':
        raise ValueError('Only HTTPS input URLs are supported')
    descriptor, temporary = tempfile.mkstemp(prefix=name + '.', suffix='.partial', dir=directory)
    try:
        print(f'Downloading {name}', flush=True)
        with os.fdopen(descriptor, 'wb') as output, urlopen(artifact['url'], timeout=60) as response:
            if urlparse(response.geturl()).scheme != 'https':
                raise ValueError('Non-HTTPS redirect')
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > artifact['size']:
                    raise ValueError('Download exceeds locked size')
                output.write(chunk)
        if count != artifact['size'] or digest(Path(temporary)) != artifact['sha256']:
            raise ValueError(f'Input hash/size mismatch: {name}')
        # Refuse replacing a file that appeared while downloading.
        if target.exists():
            raise FileExistsError(target)
        os.replace(temporary, target)
        print(f'Verified {name}', flush=True)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download-dir', type=Path, default=PROJECT / 'downloads')
    parser.add_argument('--rootfs', action='store_true', help='also reconstruct the official 10 GiB rootfs; requires extracted RAUC index')
    parser.add_argument('--index', type=Path, default=PROJECT / 'downloads/deckard-bundle/rootfs.img.caibx')
    parser.add_argument('--workers', type=int, default=24)
    args = parser.parse_args()
    manifest = json.loads((PROJECT / 'sources.lock.json').read_text(encoding='utf-8'))
    directory = args.download_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    for artifact in manifest['artifacts']:
        fetch(artifact, directory)
    if args.rootfs:
        if not args.index.is_file():
            parser.error('Extract the locked .raucb with unsquashfs or 7-Zip first; see README.md')
        command = [sys.executable, str(PROJECT / 'scripts/fetch-steamos.py'), '--index', str(args.index),
                   '--output', str(directory / 'steamos-rootfs.img'), '--cache', str(directory / 'casync-cache'),
                   '--sha256', manifest['steamos']['rootfs_sha256'], '--workers', str(args.workers)]
        for store in manifest['steamos']['stores']:
            command.extend(['--store', store])
        subprocess.run(command, check=True)
    print('Inputs verified. These are source materials, not a Switch installation image.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f'ERROR: {error}')
