#!/usr/bin/env python3
"""Build an experimental Hekate hardware probe, not a complete SteamOS port.

Uses the Switchroot boot stack with diagnostic boot arguments, a replaced init,
selected official Frame ELF files, and matching L4T modules/NVIDIA userspace.
Requires dissect.btrfs, backports.zstd and readelf. No elevated privileges.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import posixpath
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import zlib
from dissect.btrfs import Btrfs

# These are opened at runtime and do not appear in the DT_NEEDED graph.
DLOPEN_LIBRARIES = ("libvulkan.so.1", "libEGL.so.1")
GPU_PROFILE = json.loads((Path(__file__).resolve().parents[1] / "config/gpu-profile.json").read_text())
GPU_FIRMWARE_NAMES = tuple(record["name"] for record in GPU_PROFILE["firmware"])


def validate_gpu_profile(bsp):
    """Reject a mixed or changed NVIDIA stack before building the RAM disk."""
    names = {path.name for path in (bsp / "lib/firmware/gm20b").iterdir()}
    if names != set(GPU_FIRMWARE_NAMES):
        raise ValueError("GM20B firmware names differ from the audited profile; use a fresh BSP directory")
    for folder in ("tegra", "tegra-egl"):
        for path in (bsp / "usr/lib/aarch64-linux-gnu" / folder).iterdir():
            if ".so" in path.name and path.is_file():
                with path.open("rb") as stream:
                    is_elf = stream.read(4) == b"\x7fELF"
                if is_elf and path.relative_to(bsp).as_posix() not in GPU_PROFILE["bsp_files_sha256"]:
                    raise ValueError(f"Unpinned NVIDIA library; use a fresh BSP directory: {path.name}")
    for name, expected in GPU_PROFILE["bsp_files_sha256"].items():
        path = bsp / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"BSP differs from the audited NVIDIA 32.3.1 profile: {name}")
    module_path = GPU_PROFILE["nvgpu_module_path"].removeprefix("lib/")
    with tarfile.open(bsp / "opt/switchroot/modules.tar.gz") as tar:
        member = next(m for m in tar if m.name.removeprefix("./") == module_path)
        digest = hashlib.sha256(tar.extractfile(member).read()).hexdigest()
    if digest != GPU_PROFILE["nvgpu_module_sha256"]:
        raise ValueError("nvgpu module changed; re-audit firmware requirements")
    return {"name": GPU_PROFILE["name"], "nvgpu_module_sha256": digest,
            "verified_bsp_files": len(GPU_PROFILE["bsp_files_sha256"])}


def gpu_firmware_manifest(entries):
    """Resolve the GM20B request paths inside the serialized initramfs tree."""
    records = []
    for basename in GPU_FIRMWARE_NAMES:
        request_path = "lib/firmware/gm20b/" + basename
        path = request_path
        seen = set()
        while True:
            if path in seen:
                raise ValueError(f"Firmware symlink cycle: {request_path}")
            seen.add(path)
            if path not in entries:
                raise ValueError(f"Missing GPU firmware path: {path}")
            fields, data = entries[path]
            if stat.S_ISLNK(fields[1]):
                target = data.decode()
                path = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
                if not path.startswith("lib/firmware/"):
                    raise ValueError(f"Firmware link escapes its tree: {request_path}")
                continue
            if not stat.S_ISREG(fields[1]) or not data:
                raise ValueError(f"GPU firmware target is not a nonempty file: {path}")
            records.append({"request_path": request_path, "resolved_path": path,
                            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            break
    return records


def unpack_uboot(blob):
    if len(blob) < 64 or blob[:4] != bytes.fromhex("27051956"):
        raise ValueError("Invalid U-Boot legacy image")
    fields = struct.unpack(">7I4B32s", blob[:64])
    header = bytearray(blob[:64])
    header[4:8] = bytes(4)
    if zlib.crc32(header) != fields[1]:
        raise ValueError("U-Boot header CRC mismatch")
    payload = blob[64:64 + fields[3]]
    if len(payload) != fields[3] or zlib.crc32(payload) != fields[6]:
        raise ValueError("U-Boot payload CRC/length mismatch")
    return list(fields), payload


def unpack_cpio(blob):
    entries = {}
    offset = 0
    while blob[offset:offset + 6] == b"070701":
        fields = [int(blob[offset + 6 + i * 8:offset + 14 + i * 8], 16) for i in range(13)]
        name = blob[offset + 110:offset + 110 + fields[11] - 1].decode()
        start = (offset + 110 + fields[11] + 3) & ~3
        data = blob[start:start + fields[6]]
        if len(data) != fields[6]:
            raise ValueError("Truncated cpio entry")
        offset = (start + fields[6] + 3) & ~3
        if name == "TRAILER!!!":
            return entries
        if name.startswith("/") or ".." in Path(name).parts:
            raise ValueError("Unexpected cpio path")
        entries[name] = (fields, data)
    raise ValueError("Invalid/truncated cpio archive")


def encode_cpio(entries):
    output = io.BytesIO()
    items = list(entries.items()) + [("TRAILER!!!", ([0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 11, 0], b""))]
    for name, (original, data) in items:
        fields = original.copy()
        encoded = name.encode() + b"\0"
        fields[6] = len(data)
        fields[11] = len(encoded)
        output.write(b"070701" + b"".join(f"{n:08x}".encode() for n in fields))
        output.write(encoded)
        output.write(bytes((-output.tell()) % 4))
        output.write(data)
        output.write(bytes((-output.tell()) % 4))
    output.write(bytes((-output.tell()) % 512))
    return output.getvalue()


def diagnostic_boot_script(blob):
    """Keep generic timing for the 0F40 Lite clone; log the panel strategy."""
    header, payload = unpack_uboot(blob)
    if header[9:11] != [6, 0]:  # script, uncompressed
        raise ValueError("Unexpected boot script type/compression")
    size, terminator = struct.unpack_from(">II", payload)
    if terminator != 0 or len(payload) != size + 8:
        raise ValueError("Unexpected boot script component table")
    script = payload[8:].decode()
    panel_marker = '# Get display panel handle.\n'
    clone_marker = 'elif test ${display_id} = 2050; then echo Display is SAM 7.0\n'
    if script.count(panel_marker) != 1 or script.count(clone_marker) != 1:
        raise ValueError('Boot script panel selection changed')
    script = script.replace(panel_marker, panel_marker + 'setenv switchdeck_panel_strategy upstream\n')
    script = script.replace(clone_marker,
        'elif test "${display_id}" = f40 -a "${sku}" = 2; then\n'
        '    echo Display 0F40: replacement 5.5 panel - generic profile - output unverified\n'
        '    fdt get value DHANDLE /host1x/dsi/panel-o-720p-6-2 phandle\n'
        '    setenv switchdeck_panel_strategy clone-0f40-generic-test\n' + clone_marker)
    marker = "# Set kernel cmdline.\n"
    if script.count(marker) != 1:
        raise ValueError("Boot script diagnostic insertion point changed")
    insertion = (
        "# Switchdeck r6: panel selection and diagnostic metadata.\n"
        'setenv bootargs_extra ${bootargs_extra} "switchdeck_display_id=${display_id} '
        'switchdeck_sku=${sku} switchdeck_loader_rev=${loader_rev} '
        'switchdeck_panel_strategy=${switchdeck_panel_strategy}"\n\n'
    )
    script = script.replace(marker, insertion + marker)
    encoded = script.encode()
    payload = struct.pack(">II", len(encoded), 0) + encoded
    header[1] = 0
    header[2] = 0
    header[3] = len(payload)
    header[6] = zlib.crc32(payload)
    header[1] = zlib.crc32(struct.pack(">7I4B32s", *header))
    result = struct.pack(">7I4B32s", *header) + payload
    unpack_uboot(result)
    return result, script


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frame-partition", type=Path, required=True)
    ap.add_argument("--bsp", type=Path, required=True)
    ap.add_argument("--gpu-probe", type=Path, required=True,
                    help="gpu-compute built by build_gpu_probe.py")
    ap.add_argument("--framebuffer-probe", type=Path, required=True,
                    help="framebuffer-test built by build_gpu_probe.py")
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    if args.output.exists():
        ap.error("Output directory must not exist")
    profile_check = validate_gpu_profile(args.bsp)
    probe_report = json.loads((args.gpu_probe.parent / "build-report.json").read_text())
    if hashlib.sha256(args.gpu_probe.read_bytes()).hexdigest() != probe_report["sha256"]:
        raise ValueError("GPU compute binary differs from its build report")
    project = Path(__file__).resolve().parents[1]
    for name, expected in probe_report["source_sha256"].items():
        if hashlib.sha256((project / "probes" / name).read_bytes()).hexdigest() != expected:
            raise ValueError("GPU compute source changed; rebuild its binary")
    fb_report = json.loads((args.framebuffer_probe.parent / "framebuffer-build.json").read_text())
    if hashlib.sha256(args.framebuffer_probe.read_bytes()).hexdigest() != fb_report["sha256"]:
        raise ValueError("Framebuffer binary differs from its build report")
    for name, expected in fb_report["source_sha256"].items():
        if hashlib.sha256((project / "probes" / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Framebuffer source changed; rebuild its binary")
    out = args.output.resolve()
    ramfiles = out / "build-files"
    ramfiles.mkdir(parents=True)
    boot = args.bsp / "opt/switchroot/bootstack"
    original_initramfs = (boot / "initramfs").read_bytes()
    header, compressed = unpack_uboot(original_initramfs)
    if header[8:11] != [22, 3, 1]:  # ARM64, RAMDisk, gzip
        raise ValueError("Unexpected original RAM disk type/architecture/compression")
    entries = unpack_cpio(gzip.decompress(compressed))
    source_records = []
    used_inode = max(fields[0] for fields, data in entries.values()) + 1

    def add(name, data=b"", mode=0o100644):
        nonlocal used_inode
        for parent in reversed(Path(name).parents):
            p = str(parent)
            if p != "." and p not in entries:
                add(p, b"", stat.S_IFDIR | 0o755)
        fields = [used_inode, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name) + 1, 0]
        used_inode += 1
        entries[name] = (fields, data)

    # Keep the original kernel, firmware and DT; replace init and add log metadata.
    project = Path(__file__).resolve().parents[1]
    add("init", (project / "boot0/init").read_bytes(), 0o100755)
    add("frame", b"", 0o40755)
    add("frame/bin", b"", 0o40755)
    add("frame/lib", b"", 0o40755)
    queue = []
    added_libs = set()

    with args.frame_partition.open("rb") as image:
        fs = Btrfs(image)

        def frame_read(path):
            seen = set()
            while path not in seen:
                seen.add(path)
                inode = fs.get(path)
                if not inode.is_symlink():
                    data = inode.open().read()
                    if len(data) != inode.size:
                        raise ValueError(f"Short Frame read: {path}")
                    return data
                path = posixpath.normpath(posixpath.join(posixpath.dirname(path), inode.link))
            raise ValueError("Symlink loop")

        libraries = {}
        for directory in ("/usr/lib", "/usr/lib/systemd"):
            for name, inode in fs.get(directory).iterdir():
                if ".so" in name:
                    libraries.setdefault(name, ("frame", directory + "/" + name))
        # Keep the Frame versions of common libraries (especially libdrm).
        # NVIDIA's BSP carries an older libdrm lacking symbols Gamescope needs.
        # Add NVIDIA-specific libraries while preserving the common userspace ABI.
        nv_libraries = {}
        for directory in ("usr/lib/aarch64-linux-gnu/tegra", "usr/lib/aarch64-linux-gnu/tegra-egl"):
            for path in (args.bsp / directory).iterdir():
                if ".so" in path.name and path.is_file():
                    with path.open("rb") as library_file:
                        if library_file.read(4) == b"\x7fELF":
                            nv_libraries[path.name] = ("bsp", path)
                            info = subprocess.run(["readelf", "-d", str(path)],
                                                  capture_output=True, text=True, check=True)
                            soname = re.search(r"\(SONAME\).*?\[(.*?)\]", info.stdout)
                            if soname:
                                nv_libraries.setdefault(soname[1], ("bsp", path))
        for name, source in nv_libraries.items():
            libraries.setdefault(name, source)

        def elf_add(name, source, mode=0o100755):
            kind, path = source
            data = frame_read(path) if kind == "frame" else Path(path).read_bytes()
            if data[:4] != b"\x7fELF":
                raise ValueError(f"Not an ELF: {path}")
            if struct.unpack_from("<H", data, 18)[0] != 183:
                raise ValueError(f"Not AArch64: {path}")
            add(name, data, mode)
            temp = ramfiles / name.replace("/", "_")
            temp.write_bytes(data)
            queue.append(temp)
            source_records.append({"destination": name, "source_kind": kind,
                "source_path": str(path) if kind == "frame" else (
                    Path(path).name if kind == "probe" else str(Path(path).relative_to(args.bsp))),
                "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})

        programs = {"bash": "/usr/bin/bash", "true": "/usr/bin/true",
                    "systemd": "/usr/lib/systemd/systemd", "gamescope": "/usr/bin/gamescope",
                    "vulkaninfo": "/usr/bin/vulkaninfo", "modprobe": "/usr/bin/kmod",
                    "timeout": "/usr/bin/timeout", "sync": "/usr/bin/sync",
                    "od": "/usr/bin/od", "sha256sum": "/usr/bin/sha256sum",
                    "strace": "/usr/bin/strace"}
        for name, path in programs.items():
            elf_add("frame/bin/" + name, ("frame", path))
        elf_add("frame/bin/gpu-compute", ("probe", args.gpu_probe))
        elf_add("frame/bin/framebuffer-test", ("probe", args.framebuffer_probe))
        for name, source in nv_libraries.items():
            elf_add("frame/lib/" + name, libraries[name])
            added_libs.add(name)
        for name in DLOPEN_LIBRARIES:
            if name not in libraries:
                raise ValueError(f"Missing runtime-loaded library: {name}")
            if name not in added_libs:
                elf_add("frame/lib/" + name, libraries[name])
                added_libs.add(name)
        elf_add("frame/lib/ld-linux-aarch64.so.1", ("frame", "/usr/lib/ld-linux-aarch64.so.1"))
        added_libs.add("ld-linux-aarch64.so.1")
        while queue:
            binary = queue.pop()
            result = subprocess.run(["readelf", "-d", str(binary)], capture_output=True, text=True, check=True)
            for name in re.findall(r"\(NEEDED\).*?\[(.*?)\]", result.stdout):
                if name in added_libs:
                    continue
                if name not in libraries:
                    raise ValueError(f"Unresolved ELF dependency: {name} in {binary.name}")
                elf_add("frame/lib/" + name, libraries[name])
                added_libs.add(name)
        add("frame/os-release", frame_read("/usr/lib/os-release"))

    icd = json.loads((args.bsp / "usr/lib/aarch64-linux-gnu/tegra/nvidia_icd.json").read_text())
    if icd["ICD"]["library_path"] != "libGLX_nvidia.so.0":
        raise ValueError("Unexpected NVIDIA Vulkan ICD library")
    icd["ICD"]["library_path"] = "/frame/lib/libGLX_nvidia.so.0"
    add("frame/nvidia_icd.json", json.dumps(icd, indent=2).encode())
    add("frame/nvidia_egl.json", json.dumps({"file_format_version": "1.0.0", "ICD": {
        "library_path": "/frame/lib/libEGL_nvidia.so.0"}}, indent=2).encode())
    with tarfile.open(args.bsp / "opt/switchroot/modules.tar.gz") as tar:
        for member in tar:
            name = member.name.removeprefix("./")
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError("Unexpected module archive path")
            name = "lib/" + name
            if member.isdir():
                add(name, b"", stat.S_IFDIR | member.mode)
            elif member.isfile():
                add(name, tar.extractfile(member).read(), stat.S_IFREG | member.mode)
            elif member.issym():
                add(name, member.linkname.encode(), stat.S_IFLNK | 0o777)
            else:
                raise ValueError(f"Unsupported module archive entry: {member.name}")
    # The driver requests gm20b/* first. NVIDIA supplies those names as links
    # into tegra21x/*. Omitting the aliases stalls the kernel's firmware loader.
    firmware_root = args.bsp / "lib/firmware"
    firmware_trees = sorted(firmware_root.glob("tegra*")) + [firmware_root / "gm20b"]
    for directory in firmware_trees:
        if not directory.exists():
            raise ValueError(f"Missing GPU firmware source tree: {directory}")
        for path in sorted(directory.rglob("*")) if directory.is_dir() else [directory]:
            name = path.relative_to(args.bsp).as_posix()
            if path.is_symlink():
                add(name, path.readlink().as_posix().encode(), stat.S_IFLNK | 0o777)
            elif path.is_dir():
                add(name, b"", stat.S_IFDIR | 0o755)
            elif path.is_file():
                add(name, path.read_bytes())
            else:
                raise ValueError(f"Unsupported firmware entry: {path}")

    add("frame/gpu-profile.txt", (GPU_PROFILE["name"] + "\n").encode())
    add("frame/gpu-firmware-paths.txt", "".join(
        "/" + record["path"] + "\n" for record in GPU_PROFILE["firmware"]).encode())
    add("frame/gpu-firmware.sha256", "".join(
        record["sha256"] + "  /" + record["path"] + "\n"
        for record in GPU_PROFILE["firmware"]).encode())

    cpio = encode_cpio(entries)
    # Exact round-trip validation of every initramfs member before distribution.
    decoded = unpack_cpio(cpio)
    if set(decoded) != set(entries) or any(decoded[n][1] != v[1] for n, v in entries.items()):
        raise ValueError("CPIO round-trip validation failed")
    firmware_records = gpu_firmware_manifest(decoded)
    if hashlib.sha256(decoded[GPU_PROFILE["nvgpu_module_path"]][1]).hexdigest() != GPU_PROFILE["nvgpu_module_sha256"]:
        raise ValueError("Serialized nvgpu module differs from the audited module")
    for record in firmware_records:
        original = args.bsp / record["request_path"]
        if hashlib.sha256(original.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError(f"Firmware content differs from NVIDIA BSP: {original}")
    payload = gzip.compress(cpio, compresslevel=6, mtime=0)
    # Hekate's stock initrd load address 0x92000000 precedes kernel 0xA0000000.
    if len(payload) + 64 >= 0x0E000000:
        raise ValueError("RAM disk overlaps the stock kernel load address")
    header[1] = 0
    header[2] = 0
    header[3] = len(payload)
    header[6] = zlib.crc32(payload)
    header[11] = b"Switchdeck Boot0 r6".ljust(32, b"\0")
    header[1] = zlib.crc32(struct.pack(">7I4B32s", *header))
    ramdisk = struct.pack(">7I4B32s", *header) + payload
    unpack_uboot(ramdisk)
    sd = out / "sd-card"
    destination = sd / "switchroot/switchdeck-boot0"
    destination.mkdir(parents=True)
    for filename in ("bl31.bin", "bl33.bin", "uImage", "nx-plat.dtimg"):
        shutil.copyfile(boot / filename, destination / filename)
    boot_script, boot_text = diagnostic_boot_script((boot / "boot.scr").read_bytes())
    (destination / "boot.scr").write_bytes(boot_script)
    (out / "boot.txt").write_text(boot_text)
    (destination / "initramfs").write_bytes(ramdisk)
    configs = sd / "bootloader/ini"
    configs.mkdir(parents=True)
    (configs / "SWITCHDECK-BOOT0.ini").write_text(
        "[Switchdeck Boot0 r6 - Display test]\n"
        "l4t=1\nboot_prefixes=/switchroot/switchdeck-boot0/\n"
        "id=SWD-BOOT0\nemmc=0\nr2p_action=bootloader\nfbconsole=0\n")
    report = {"artifact_type": "experimental hardware probe", "revision": "r6", "complete_steamos_port": False,
              "hardware_boot_tested": False, "kernel": "4.9.140-l4t",
              "ramdisk_compressed_bytes": len(ramdisk), "ramdisk_uncompressed_bytes": len(cpio),
              "source_initramfs_sha256": hashlib.sha256(original_initramfs).hexdigest(),
              "source_boot_script_sha256": hashlib.sha256((boot / "boot.scr").read_bytes()).hexdigest(),
              "boot_script_change": "explicit 0F40 Lite clone generic profile branch; report strategy; no timing changes",
              "runtime_loaded_libraries": list(DLOPEN_LIBRARIES),
              "gpu_firmware": firmware_records,
              "gpu_profile": profile_check,
              "gpu_compute_probe": {k: v for k, v in probe_report.items() if k != "frame_files"},
              "framebuffer_probe": {k: v for k, v in fb_report.items() if k != "frame_files"},
              "vulkan_icd_api_version": icd["ICD"]["api_version"],
              "entry_count": len(entries), "files": source_records,
              "checks": ["U-Boot header and data CRC", "CPIO complete round-trip",
                         "AArch64 ELF machine", "all DT_NEEDED dependencies resolved",
                         "explicit Vulkan/EGL dlopen dependencies and vendor manifests included",
                         "nvgpu module and NVIDIA firmware/userspace match the audited profile",
                         "all GM20B firmware paths, including acr_ucode.bin, match pinned hashes",
                         "compressed initrd does not overlap stock kernel load address"]}
    (out / "build-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "files"}, indent=2))


if __name__ == "__main__":
    main()
