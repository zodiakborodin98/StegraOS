#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Read selected metadata and ELF headers from the locked Btrfs image on Windows/Linux.

Does not mount the image, execute ARM programs, or certify bootability.
Requires requirements-inspect.txt. Output is an inventory, not a root filesystem.
"""
import argparse
from collections import deque
import hashlib
import importlib.util
import json
from pathlib import Path

from dissect.btrfs import Btrfs
from dissect.btrfs.exceptions import FileNotFoundError as BtrfsNotFound

PROJECT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('root_audit', PROJECT / 'scripts/audit-rootfs.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def resolve(fs, path):
    # Resolve against the image explicitly. dissect 1.10 can attach a Subvolume
    # as a relative symlink's parent when using get() for a multi-part path.
    pending, resolved, links = deque(path.split('/')), [], 0
    while pending:
        part = pending.popleft()
        if part in ('', '.'):
            continue
        if part == '..':
            if resolved:
                resolved.pop()
            continue
        node = fs.get('/' + '/'.join([*resolved, part]))
        if node.is_symlink():
            links += 1
            if links > 40:
                raise ValueError('Too many Btrfs symlinks')
            target = node.link
            if target.startswith('/'):
                resolved = []
            pending.extendleft(reversed(target.split('/')))
        else:
            resolved.append(part)
    return fs.get('/' + '/'.join(resolved))


def read_file(fs, path, limit):
    node = resolve(fs, path)
    if not node.is_file() or node.size > limit:
        raise ValueError(f'Not a bounded regular file: {path}')
    with node.open() as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('File exceeds the read limit')
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, default=PROJECT / 'downloads/steamos-rootfs.img')
    parser.add_argument('--output', type=Path, default=PROJECT / 'reports/image-inventory')
    args = parser.parse_args()
    lock = json.loads((PROJECT / 'sources.lock.json').read_text())
    with args.image.open('rb') as source:
        print('Checking image SHA256...', flush=True)
        image_hash = hashlib.file_digest(source, 'sha256').hexdigest()
        if image_hash != lock['steamos']['rootfs_sha256']:
            raise ValueError('Image does not match the pinned SteamOS rootfs')
        source.seek(0)
        fs = Btrfs(source)
        output = args.output.absolute()
        # Inventory artifacts must not overwrite previous results or be mistaken for a rootfs.
        output.mkdir(parents=True, exist_ok=False)
        samples = output / 'elf-samples'
        samples.mkdir()
        report = {'sha256': image_hash, 'filesystem': 'btrfs', 'label': fs.label,
                  'status': 'static inventory; not a boot or ISA test',
                  'os_release': {}, 'packages': [], 'elf': [], 'missing': []}
        for path in ('/usr/lib/os-release', '/etc/os-release'):
            try:
                report['os_release'][path] = read_file(fs, path, 65536).decode('utf-8', 'replace')
            except BtrfsNotFound:
                report['missing'].append(path)
        seen = set()
        for database in audit.PACMAN_DATABASES:
            try:
                directory = resolve(fs, database)
            except BtrfsNotFound:
                continue
            for name, node in directory.iterdir():
                if name in ('.', '..') or not node.is_dir():
                    continue
                logical = f'{database}/{name}/desc'
                try:
                    desc = audit.parse_desc(read_file(fs, logical, 1024 * 1024))
                except BtrfsNotFound:
                    continue
                key = (desc.get('NAME'), desc.get('VERSION'))
                if key in seen or not all(key):
                    continue
                seen.add(key)
                report['packages'].append({'name': key[0], 'version': key[1], 'architecture': desc.get('ARCH'), 'path': logical})
        for number, path in enumerate(audit.CORE_FILES):
            try:
                data = read_file(fs, path, 128 * 1024 * 1024)
            except BtrfsNotFound:
                report['missing'].append(path)
                continue
            sample = samples / f'{number:02d}-{Path(path).name}.bin'
            sample.write_bytes(data)
            report['elf'].append({'path': path, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), **audit.inspect_elf(sample)})
        report['packages'].sort(key=lambda item: item['name'])
        (output / 'inventory.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
        print(json.dumps(report['os_release'], indent=2))
        selected = {'systemd', 'glibc', 'gamescope', 'mesa', 'plasma-desktop', 'plasma-workspace', 'kwin', 'xorg-server'}
        print(json.dumps([package for package in report['packages'] if package['name'] in selected], indent=2))
        print(f'Inventory saved: {output}; {len(report["packages"])} packages; {len(report["elf"])} core files')


if __name__ == '__main__':
    main()
