#!/usr/bin/env python3
"""Build ARM64 compute and framebuffer probes with the compiler from Steam Frame.

Runs the Frame compiler, assembler, linker and glslang explicitly through QEMU;
no chroot, binfmt registration, host package installation or device access.
"""
import argparse
import hashlib
import json
from pathlib import Path
import posixpath
import re
import struct
import subprocess
from dissect.btrfs import Btrfs


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--frame-partition', type=Path, required=True)
    ap.add_argument('--qemu', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    if args.output.exists():
        ap.error('Output directory must not exist')
    out = args.output.resolve()
    out.mkdir(parents=True)
    root = out / 'toolchain'
    source = Path(__file__).resolve().parents[1] / 'probes'
    gcc = '/usr/lib/gcc/aarch64-unknown-linux-gnu/15.1.1'
    records = {}
    with args.frame_partition.open('rb') as image:
        fs = Btrfs(image)

        def read(path):
            seen = set()
            while path not in seen:
                seen.add(path)
                inode = fs.get(path)
                if not inode.is_symlink():
                    return inode.open().read()
                path = posixpath.normpath(posixpath.join(posixpath.dirname(path), inode.link))
            raise ValueError('Frame source symlink loop')

        def save(path):
            dest = root / path.lstrip('/')
            if not dest.exists():
                data = read(path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                if data.startswith(b'\x7fELF'):
                    dest.chmod(0o755)
                records[path] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            return dest

        def tree(path):
            for name, node in fs.get(path).iterdir():
                if name in ('.', '..'):
                    continue
                child = path + '/' + name
                if node.is_dir():
                    tree(child)
                else:
                    save(child)

        # C, platform and Vulkan headers only; no Qt/C++ toolchain extraction.
        for name, node in fs.get('/usr/include').iterdir():
            if name.endswith('.h') and not node.is_dir():
                save('/usr/include/' + name)
        for name in ('bits', 'sys', 'gnu', 'asm', 'asm-generic', 'linux', 'vk_video'):
            tree('/usr/include/' + name)
        for name in ('vk_platform.h', 'vulkan_core.h'):
            save('/usr/include/vulkan/' + name)
        tree(gcc + '/include')
        tree(gcc + '/include-fixed')
        for path in ('/usr/lib/Scrt1.o', '/usr/lib/crti.o', '/usr/lib/crtn.o',
                     gcc + '/crtbeginS.o', gcc + '/crtendS.o', gcc + '/libgcc.a'):
            save(path)
        programs = {'cc1': gcc + '/cc1', 'as': '/usr/bin/as',
                    'ld': '/usr/bin/ld', 'glslang': '/usr/bin/glslang'}
        todo = list(programs.values()) + ['/usr/lib/ld-linux-aarch64.so.1', '/usr/lib/libvulkan.so.1']
        visited = set()
        while todo:
            path = todo.pop()
            if path in visited:
                continue
            visited.add(path)
            dest = save(path)
            elf = subprocess.run(['readelf', '-d', str(dest)], capture_output=True, text=True, check=True)
            for name in re.findall(r'\(NEEDED\).*?\[(.*?)\]', elf.stdout):
                todo.append('/usr/lib/' + name)

    lib = root / 'usr/lib'
    runner = [str(args.qemu.resolve()), '-cpu', 'cortex-a57', '-E', 'LD_LIBRARY_PATH=' + str(lib),
              str(lib / 'ld-linux-aarch64.so.1'), '--library-path', str(lib)]
    commands = []

    def run(name, arguments):
        command = runner + [str(root / programs[name].lstrip('/'))] + [str(x) for x in arguments]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        commands.append({'tool': name, 'returncode': result.returncode,
                         'stdout': result.stdout, 'stderr': result.stderr})
        (out / 'commands.json').write_text(json.dumps(commands, indent=2) + '\n')
        if result.returncode:
            raise RuntimeError(name + ': ' + result.stdout + result.stderr)

    spv = out / 'gpu_compute.spv'
    run('glslang', ['-V', '--target-env', 'vulkan1.0', '-o', spv, source / 'gpu_compute.comp'])
    data = spv.read_bytes()
    words = struct.unpack('<' + 'I' * (len(data) // 4), data)
    header = out / 'gpu_compute_spv.h'
    header.write_text('static const uint32_t gpu_compute_spv[] = {\n' +
                      ',\n'.join('0x%08xu' % word for word in words) + '\n};\n')
    def compile_probe(stem, name, extra_libraries):
        assembly = out / (stem + '.s')
        run('cc1', ['-quiet', '-O2', '-std=c11', '-fPIE', '-fstack-protector-strong',
                '-Wall', '-Wextra', '-Werror', '-nostdinc',
                '-isystem', root / (gcc + '/include').lstrip('/'),
                '-isystem', root / (gcc + '/include-fixed').lstrip('/'),
                '-isystem', root / 'usr/include', '-I', out,
                source / (stem + '.c'), '-o', assembly])
        obj = out / (stem + '.o')
        run('as', ['-o', obj, assembly])
        binary = out / name
        gcc_path = root / gcc.lstrip('/')
        run('ld', ['-pie', '-z', 'relro', '-z', 'now', '-dynamic-linker', '/frame/lib/ld-linux-aarch64.so.1',
               '-o', binary, lib / 'Scrt1.o', lib / 'crti.o', gcc_path / 'crtbeginS.o', obj,
               '-L', lib, '-rpath-link', lib, *[lib / p for p in extra_libraries], lib / 'libc.so.6',
               lib / 'ld-linux-aarch64.so.1',
               gcc_path / 'libgcc.a', gcc_path / 'crtendS.o', lib / 'crtn.o'])
        binary.chmod(0o755)
        return binary

    binary = compile_probe('gpu_compute', 'gpu-compute', ['libvulkan.so.1'])
    report = {'binary': 'gpu-compute', 'architecture': 'AArch64',
              'sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'shader_sha256': hashlib.sha256(data).hexdigest(),
              'source_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in (source / 'gpu_compute.c', source / 'gpu_compute.comp')},
              'toolchain_source': 'Pinned Steam Frame rootfs, GCC 15.1.1, executed with QEMU Cortex-A57',
              'hardware_tested': False, 'frame_files': records}
    (out / 'build-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'frame_files'}, indent=2))
    framebuffer = compile_probe('framebuffer', 'framebuffer-test', [])
    fb_report = {'binary': framebuffer.name, 'architecture': 'AArch64',
                 'sha256': hashlib.sha256(framebuffer.read_bytes()).hexdigest(),
                 'source_sha256': {'framebuffer.c': hashlib.sha256((source / 'framebuffer.c').read_bytes()).hexdigest()},
                 'toolchain_source': report['toolchain_source'], 'hardware_tested': False,
                 'frame_files': records}
    (out / 'framebuffer-build.json').write_text(json.dumps(fb_report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in fb_report.items() if k != 'frame_files'}, indent=2))


if __name__ == '__main__':
    main()
