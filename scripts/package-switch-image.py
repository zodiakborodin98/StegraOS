#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Package an experimental SteamOS Switch CONSOLE image; never flash a device.

Requires Linux root, rsync, util-linux and e2fsprogs. STAGE is read-only input.
WORK and OUTPUT must be new directories with existing, non-symlink parents.
WORK must be on native Linux storage; OUTPUT may be Windows storage in WSL.
Only a newly created regular image file is formatted and loop-mounted.
"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

PROJECT = Path(__file__).resolve().parent.parent
ALIGNMENT = 4 * 1024**2
PART_SIZE = 4092 * 1024**2
GIB = 1024**3
FEATURES = "has_journal,ext_attr,resize_inode,dir_index,filetype,extent,sparse_super,large_file,huge_file,uninit_bg,dir_nlink,extra_isize"
BOOT_FILES = ("bl31.bin", "bl33.bin", "boot.scr", "uImage", "initramfs", "nx-plat.dtimg", "README_CONFIG.txt")
SENTINEL = b"SteamOS-Switch-Mariko-console-diagnostics-v1\n"


class PackageError(ValueError):
    pass


def run(args, **kwargs):
    return subprocess.run([str(v) for v in args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs).stdout


def checked_path(raw, new=False):
    path = Path(raw)
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise PackageError("Paths must be absolute and may not contain '..' or name '/'.")
    if any(path == Path(prefix) or Path(prefix) in path.parents for prefix in ("/dev", "/proc", "/sys", "/run")):
        raise PackageError("Device and runtime namespaces cannot contain build inputs/outputs.")
    if new and os.path.lexists(path):
        raise PackageError(f"Refusing an existing destination: {path}")
    for current in reversed(((path.parent if new else path), *(path.parent if new else path).parents)):
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise PackageError(f"Path ancestor is not a real directory: {current}")
    return path


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def image_size(apparent_bytes, requested_gib=None):
    minimum = max(12, math.ceil((apparent_bytes * 1.12 + GIB) / GIB))
    if requested_gib is not None and not minimum <= requested_gib <= 256:
        raise PackageError(f"Image needs at least {minimum} GiB; supported maximum is 256 GiB.")
    return (requested_gib or minimum) * GIB


def split_image(image, directory, part_size=PART_SIZE):
    if part_size <= 0 or part_size % ALIGNMENT or part_size > PART_SIZE:
        raise PackageError("Parts must be 4 MiB aligned and fit FAT32.")
    size = image.stat().st_size
    if not stat.S_ISREG(image.lstat().st_mode) or size == 0 or size % ALIGNMENT:
        raise PackageError("Only an aligned regular image file can be split.")
    directory.mkdir(parents=True, exist_ok=False)
    entries, aggregate = [], hashlib.sha256()
    with image.open("rb") as source:
        for number in range(math.ceil(size / part_size)):
            name = f"l4t.{number:02d}"
            path = directory / name
            remaining = min(part_size, size - number * part_size)
            part_hash = hashlib.sha256()
            with path.open("xb") as target:
                while remaining:
                    block = source.read(min(8 * 1024**2, remaining))
                    if not block:
                        raise PackageError("Image changed or was truncated during splitting.")
                    target.write(block)
                    part_hash.update(block)
                    aggregate.update(block)
                    remaining -= len(block)
                target.flush()
                os.fsync(target.fileno())
            entries.append({"path": f"switchroot/install/{name}", "size": path.stat().st_size,
                            "sha256": part_hash.hexdigest()})
    return entries, aggregate.hexdigest()


def validate_stage(stage):
    root = checked_path(stage / "rootfs")
    spec = importlib.util.spec_from_file_location("switch_boot", PROJECT / "scripts/apply-switch-overlay.py")
    boot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boot)
    tree = boot.OfflineTree(root)
    try:
        manifest = json.loads(tree.read(boot.MANIFEST))
        if manifest.get("status") != "applied" or manifest.get("phase") != "diagnostic-console":
            raise PackageError("Apply the diagnostic boot overlay before packaging.")
        for relative, expected in manifest["desired"].items():
            if tree.snapshot(relative) != expected:
                raise PackageError(f"Managed boot target differs from manifest: {relative}")
        if tree.read("usr/lib/os-release") != (stage / "source-os-release").read_bytes():
            raise PackageError("SteamOS os-release differs from the staged original.")
        if tree.read("etc/switchroot_version.conf").strip() != b"5.1.2":
            raise PackageError("Unexpected Switchroot compatibility marker.")
    finally:
        tree.close()
    for special in ("dev", "proc", "sys", "run"):
        location = root / special
        if location.is_symlink() or not location.is_dir() or any(location.iterdir()):
            raise PackageError(f"Offline rootfs /{special} must be an empty real directory.")
    mounts = json.loads(run(["findmnt", "--json", "--list", "-o", "TARGET"]))["filesystems"]
    for mount in mounts:
        target = Path(mount["target"])
        if target == root or root in target.parents:
            raise PackageError(f"Unmount staged runtime mounts before packaging: {target}")
    source_lock = json.loads((stage / "sources.lock.json").read_text())
    if source_lock != json.loads((PROJECT / "sources.lock.json").read_text()):
        raise PackageError("Stage source lock differs from project source lock.")
    for name in BOOT_FILES:
        path = stage / "sdcard/switchroot/steamos" / name
        if not stat.S_ISREG(path.lstat().st_mode) or not path.stat().st_size:
            raise PackageError(f"Invalid bootstack file: {name}")
        original = stage / "bsp/opt/switchroot/bootstack" / name
        if sha(path) != sha(original):
            raise PackageError(f"Bootstack differs from the pinned BSP: {name}")
    return root, source_lock


def install_packaging_overlay(mountpoint):
    spec = importlib.util.spec_from_file_location("switch_boot", PROJECT / "scripts/apply-switch-overlay.py")
    boot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boot)
    tree = boot.OfflineTree(mountpoint)
    try:
        source_root = PROJECT / "overlays/packaging/rootfs"
        for source in sorted(source_root.rglob("*")):
            if source.is_symlink():
                raise PackageError("Packaging source may not contain symlinks.")
            if source.is_file():
                relative = source.relative_to(source_root).as_posix()
                if tree.snapshot(relative)["kind"] != "absent":
                    raise PackageError(f"Packaging overlay conflicts with staged source: {relative}")
                tree.write(relative, boot.file_value(source.read_bytes(), 0o755 if relative.endswith("/export-boot-logs") else 0o644))
        tree.write("etc/systemd/system/timers.target.wants/steamos-switch-export-logs.timer",
                   boot.link_value("/etc/systemd/system/steamos-switch-export-logs.timer"))
    finally:
        tree.close()


def build(stage, work, output, size_gib=None):
    if os.name != "posix" or os.geteuid() != 0:
        raise PackageError("Run as root on Linux/WSL.")
    for command in ("rsync", "losetup", "mount", "umount", "findmnt", "mkfs.ext4", "e2fsck", "dumpe2fs", "du"):
        if shutil.which(command) is None:
            raise PackageError(f"Missing prerequisite: {command}")
    stage, work, output = checked_path(stage), checked_path(work, True), checked_path(output, True)
    for left, right in ((stage, work), (stage, output), (work, output)):
        if left == right or left in right.parents or right in left.parents:
            raise PackageError("Stage, work and output directories must be disjoint.")
    filesystem = run(["findmnt", "-n", "-o", "FSTYPE", "--target", work.parent]).strip()
    if filesystem not in ("ext4", "btrfs", "xfs", "f2fs", "zfs"):
        raise PackageError("WORK must reside on native Linux storage.")
    root, lock = validate_stage(stage)
    apparent = int(run(["du", "--apparent-size", "-sx", "-B1", root]).split()[0])
    size = image_size(apparent, size_gib)
    required_work = size + GIB
    required_output = size + 128 * 1024**2
    if work.parent.stat().st_dev == output.parent.stat().st_dev:
        required_work += required_output
    if shutil.disk_usage(work.parent).free < required_work or shutil.disk_usage(output.parent).free < required_output:
        raise PackageError(f"Insufficient free storage: need image {size / GIB:.0f} GiB plus chunks and headroom.")
    work.mkdir(mode=0o700)
    image = work / "SteamOS-Switch-Mariko-console.ext4.img"
    with image.open("xb") as stream:
        stream.truncate(size)
    mountpoint = work / "image-root"
    mountpoint.mkdir()
    loop = None
    mounted = False
    try:
        print(f"Creating {size / GIB:.0f} GiB experimental console image...", flush=True)
        mkfs = run(["mkfs.ext4", "-F", "-b", "4096", "-I", "256", "-m", "1", "-L", "SWR-STM",
                    "-O", "none," + FEATURES, "-E", "lazy_itable_init=0,lazy_journal_init=0", image])
        (work / "mkfs.txt").write_text(mkfs)
        loop = run(["losetup", "--find", "--show", image]).strip()
        if not re.fullmatch(r"/dev/loop[0-9]+", loop):
            raise PackageError("Unexpected loop device response.")
        run(["mount", "-t", "ext4", "-o", "nosuid,nodev", loop, mountpoint])
        mounted = True
        print("Copying staged rootfs and verifying contents and metadata...", flush=True)
        run(["rsync", "-aHAX", "--numeric-ids", "--", str(root) + "/", str(mountpoint) + "/"])
        differences = run(["rsync", "-aHAXnic", "--numeric-ids", "--", str(root) + "/", str(mountpoint) + "/"])
        (work / "rootfs-copy-check.txt").write_text(differences)
        if differences.strip():
            raise PackageError("Copied rootfs differs from stage; inspect rootfs-copy-check.txt.")
        install_packaging_overlay(mountpoint)
        os.sync()
        run(["umount", mountpoint])
        mounted = False
        run(["losetup", "--detach", loop])
        loop = None
        print("Checking ext4 filesystem before splitting...", flush=True)
        (work / "fsck.txt").write_text(run(["e2fsck", "-fn", image]))
        superblock = run(["dumpe2fs", "-h", image])
        (work / "ext4-superblock.txt").write_text(superblock)
        actual_features = re.search(r"^Filesystem features:\s*(.*)$", superblock, re.M).group(1).split()
        if set(actual_features) != set(FEATURES.split(",")):
            raise PackageError(f"Unexpected ext4 features: {actual_features}")
        output.mkdir(mode=0o755)
        boot = output / "switchroot/steamos"
        boot.mkdir(parents=True)
        for name in BOOT_FILES:
            shutil.copyfile(stage / "sdcard/switchroot/steamos" / name, boot / name)
        (boot / "diagnostic-log-target").write_bytes(SENTINEL)
        ini = output / "bootloader/ini/SteamOS-console.ini"
        ini.parent.mkdir(parents=True)
        shutil.copyfile(PROJECT / "overlays/boot/SteamOS-console.ini.draft", ini)
        print("Writing FAT32-compatible Hekate installation parts...", flush=True)
        parts, image_hash = split_image(image, output / "switchroot/install")
        report = {"schema": 1, "status": "experimental-console-hardware-unverified", "image_size": size,
                  "image_sha256": image_hash, "rootfs_label": "SWR-STM", "ext4_features": actual_features,
                  "rootfs_source_sha256": lock["steamos"]["rootfs_sha256"], "parts": parts,
                  "stage": str(stage), "work": str(work), "output": str(output),
                  "rootfs_apparent_bytes": apparent, "added_overlay": "overlays/packaging/rootfs",
                  "tested": ["rootfs copy contents/metadata", "e2fsck -fn", "explicit ext4 feature list", "part SHA256"],
                  "not_tested": ["Switch boot", "NVIDIA output", "Steam/Game Mode", "games", "login credentials"]}
        (output / "package-manifest.json").write_text(json.dumps(report, indent=2) + "\n")
        shutil.copyfile(PROJECT / "docs/console-image-install.md", output / "READ-ME-FIRST.md")
        for name in ("fsck.txt", "ext4-superblock.txt", "rootfs-copy-check.txt"):
            shutil.copyfile(work / name, output / name)
        checksums = []
        for file in sorted(output.rglob("*")):
            if file.is_file():
                digest = next((p["sha256"] for p in parts if p["path"] == file.relative_to(output).as_posix()), None)
                checksums.append(f"{digest or sha(file)}  {file.relative_to(output).as_posix()}")
        (output / "SHA256SUMS").write_text("\n".join(checksums) + "\n")
        print(json.dumps(report, indent=2), flush=True)
        return report
    finally:
        if mounted:
            source = run(["findmnt", "--nofsroot", "-n", "-o", "SOURCE", "--mountpoint", mountpoint]).strip()
            if source != loop:
                raise PackageError(f"Unexpected mount retained at {mountpoint}; refusing cleanup.")
            run(["umount", mountpoint])
        if loop:
            run(["losetup", "--detach", loop])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--size-gib", type=int, help="Default: at least 12 GiB, sized from the staged contents.")
    args = parser.parse_args()
    try:
        build(args.stage, args.work, args.output, args.size_gib)
    except subprocess.CalledProcessError as exc:
        print(exc.stdout or str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"Packaging failed; partial files retained: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
