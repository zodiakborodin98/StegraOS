#!/usr/bin/env python3
"""Check GPU firmware paths and the Vulkan/EGL load chain without a Tegra GPU.

This checks packaging and Cortex-A57 ELF startup, not GPU or display operation.
The negative control removes libvulkan from an extracted temporary copy only.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

from build_boot0 import (GPU_FIRMWARE_NAMES, GPU_PROFILE, validate_gpu_profile,
                        unpack_cpio, unpack_uboot, diagnostic_boot_script)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--initramfs", required=True, type=Path)
    ap.add_argument("--qemu", required=True, type=Path)
    ap.add_argument("--bsp", required=True, type=Path)
    ap.add_argument("--gpu-probe", required=True, type=Path)
    ap.add_argument("--framebuffer-probe", required=True, type=Path)
    ap.add_argument("--incompatible-bsp", type=Path,
                    help="Optional previous BSP: verify that the profile rejects it")
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    profile_check = validate_gpu_profile(args.bsp)
    rejected_previous = None
    if args.incompatible_bsp:
        try:
            validate_gpu_profile(args.incompatible_bsp)
        except ValueError as error:
            rejected_previous = str(error)
    if args.output.exists():
        ap.error("Output directory must not exist")
    root = args.output.resolve()
    root.mkdir(parents=True)
    blob = args.initramfs.read_bytes()
    _, compressed = unpack_uboot(blob)
    entries = unpack_cpio(gzip.decompress(compressed))
    for name, (fields, data) in entries.items():
        if name.startswith("frame/") and stat.S_ISREG(fields[1]):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(fields[1] & 0o777)
        elif name.startswith(("lib/firmware/gm20b/", "lib/firmware/tegra21x/")):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISREG(fields[1]):
                path.write_bytes(data)
            elif stat.S_ISLNK(fields[1]):
                path.symlink_to(data.decode())
                if not path.resolve().is_relative_to(root / "lib/firmware"):
                    raise ValueError("Firmware link escapes the extracted tree")
            elif stat.S_ISDIR(fields[1]):
                path.mkdir(exist_ok=True)
            else:
                raise ValueError("Unsupported firmware member")
    firmware = []
    for basename in GPU_FIRMWARE_NAMES:
        name = "lib/firmware/gm20b/" + basename
        actual = (root / name).read_bytes()
        expected = (args.bsp / name).read_bytes()
        firmware.append({"path": name, "bytes": len(actual),
                         "sha256": hashlib.sha256(actual).hexdigest(),
                         "matches_bsp": actual == expected and bool(actual),
                         "matches_profile": hashlib.sha256(actual).hexdigest() == GPU_PROFILE["bsp_files_sha256"][name]})
    lib = root / "frame/lib"
    bins = root / "frame/bin"
    # Relocate only the two manifests in the extracted test copy.
    for filename in ("nvidia_icd.json", "nvidia_egl.json"):
        manifest_path = root / "frame" / filename
        manifest = json.loads(manifest_path.read_text())
        target = manifest["ICD"]["library_path"]
        if not target.startswith("/frame/lib/"):
            raise ValueError("Unexpected manifest library path")
        manifest["ICD"]["library_path"] = str(root / target.lstrip("/"))
        manifest_path.write_text(json.dumps(manifest, indent=2))
    runtime = root / "runtime"
    runtime.mkdir(mode=0o700)
    env = os.environ.copy()
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "LD_LIBRARY_PATH", "LD_PRELOAD", "LD_DEBUG"):
        env.pop(key, None)
    env.update(VK_DRIVER_FILES=str(root / "frame/nvidia_icd.json"),
               VK_ICD_FILENAMES=str(root / "frame/nvidia_icd.json"),
               __EGL_VENDOR_LIBRARY_FILENAMES=str(root / "frame/nvidia_egl.json"),
               XDG_RUNTIME_DIR=str(runtime), XDG_CACHE_HOME=str(runtime / "cache"),
               XDG_CONFIG_HOME=str(runtime), XDG_DATA_HOME=str(runtime),
               VK_LOADER_DEBUG="all")
    base = [str(args.qemu.resolve()), "-cpu", "cortex-a57",
            "-E", "LD_LIBRARY_PATH=" + str(lib), "-E", "LD_DEBUG=libs",
            str(lib / "ld-linux-aarch64.so.1"), "--library-path", str(lib)]

    def run(name, program, arguments):
        completed = subprocess.run(base + [str(bins / program)] + arguments,
                                   capture_output=True, text=True, env=env, timeout=45)
        (root / (name + ".stdout")).write_text(completed.stdout)
        (root / (name + ".stderr")).write_text(completed.stderr)
        return completed

    missing = "Vulkan loader is not installed, not found, or failed to load"
    absent = lib / "libvulkan.so.1.disabled"
    (lib / "libvulkan.so.1").rename(absent)
    try:
        negative = run("negative-without-loader", "vulkaninfo", ["--summary"])
    finally:
        absent.rename(lib / "libvulkan.so.1")
    positive = run("vulkan-runtime-chain", "vulkaninfo", ["--summary"])
    gamescope = run("gamescope-version", "gamescope", ["--version"])
    compute = run("compute-without-tegra", "gpu-compute", [])
    framebuffer = run("framebuffer-self-test", "framebuffer-test", ["--self-test"])
    preview = run("framebuffer-preview", "framebuffer-test", ["--preview", str(root / "display-pattern.ppm")])
    no_fb = None
    if not Path('/dev/fb0').exists():
        no_fb = run("framebuffer-without-device", "framebuffer-test", [])
    strace = run("strace-version", "strace", ["--version"])
    sample = root / "phandle.bin"
    sample.write_bytes(bytes.fromhex("00001234"))
    od = run("od-phandle", "od", ["-An", "-tx1", str(sample)])
    checksum_file = root / "firmware-test.sha256"
    checksum_file.write_text((root / "frame/gpu-firmware.sha256").read_text().replace(
        "  /lib/firmware/", "  " + str(root / "lib/firmware") + "/"))
    checksum_test = run("firmware-checksums", "sha256sum", ["--check", str(checksum_file)])
    startup_libraries = ["libvulkan.so.1", "libGLX_nvidia.so.0", "libEGL_nvidia.so.0"]
    observed = {
        name: any("calling init:" in line and str(lib / name) in line
                  for line in positive.stderr.splitlines())
        for name in startup_libraries
    }
    expected_boot, boot_text = diagnostic_boot_script((args.bsp / "opt/switchroot/bootstack/boot.scr").read_bytes())
    panel_block = boot_text.split('# Get display panel handle.\n', 1)[1].split('# Set new active display panel handle.', 1)[0]
    # Exercise the U-Boot-compatible test/elif selection using shell stubs.
    # Distinguish the clone from real Sharp panels and retain unknown-ID diagnostics.
    panel_cases = []
    for display_id, sku, expected in [('f40', '2', 'panel-o-720p-6-2'),
                                     ('1040', '2', 'panel-s-720p-5-5'),
                                     ('1020', '2', 'panel-i-720p-5-5'),
                                     ('f30', '1', 'panel-a-720p-6-2'),
                                     ('f40', '1', 'Unknown Display ID:'),
                                     ('ffff', '2', 'Unknown Display ID:')]:
        script = ('setenv() { :; }; fdt() { echo "$*"; }; echoe() { echo "$*"; };\n' + panel_block)
        selection = subprocess.run(['/bin/sh', '-c', script], text=True, capture_output=True,
                                   env={**os.environ, 'display_id': display_id, 'sku': sku}, timeout=5)
        panel_cases.append({'display_id': display_id, 'sku': sku, 'expected': expected,
                            'passed': selection.returncode == 0 and expected in selection.stdout,
                            'stdout': selection.stdout, 'stderr': selection.stderr})
    checks = {
        "gpu_firmware_paths_open_and_match_bsp": all(record["matches_bsp"] for record in firmware),
        "gpu_firmware_matches_pinned_profile": all(record["matches_profile"] for record in firmware),
        "nvgpu_module_matches_profile": hashlib.sha256(entries[GPU_PROFILE["nvgpu_module_path"]][1]).hexdigest() == GPU_PROFILE["nvgpu_module_sha256"],
        "runtime_firmware_checksum_command": checksum_test.returncode == 0 and checksum_test.stdout.count(": OK") == len(firmware),
        "icd_api_matches_source_package": json.loads(entries["frame/nvidia_icd.json"][1])["ICD"]["api_version"] == GPU_PROFILE["icd_api_version"],
        "old_missing_loader_failure_reproduced": missing in negative.stderr and negative.returncode == 1,
        "new_loader_failure_absent": missing not in positive.stderr,
        "vulkan_loader_version_reported": "Vulkan Loader Version 1.4.309" in positive.stderr,
        "vulkan_and_nvidia_egl_libraries_initialized": all(observed.values()),
        "vulkan_process_exited_normally": positive.returncode in (0, 1),
        "gamescope_version": gamescope.returncode == 0 and "3.16.28" in gamescope.stderr,
        "compute_binary_matches_build": entries["frame/bin/gpu-compute"][1] == args.gpu_probe.read_bytes(),
        "compute_probe_rejects_missing_gpu": compute.returncode in (2, 3) and "COMPUTE_RESULT_OK" not in compute.stdout,
        "framebuffer_binary_matches_build": entries["frame/bin/framebuffer-test"][1] == args.framebuffer_probe.read_bytes(),
        "framebuffer_bounds_formats_rotation": framebuffer.returncode == 0 and 'FB_SELF_TEST_OK' in framebuffer.stdout,
        "framebuffer_renders_preview": preview.returncode == 0 and (root / 'display-pattern.ppm').read_bytes().startswith(b'P6\n1280 720\n255\n'),
        "boot_script_matches_verified_source": args.initramfs.with_name('boot.scr').read_bytes() == expected_boot,
        "panel_selection_cases": all(case['passed'] for case in panel_cases),
        "strace_version": strace.returncode == 0 and "6.16" in strace.stdout,
        "phandle_hex_reader": od.returncode == 0 and od.stdout.split() == ["00", "00", "12", "34"],
        "init_matches_source": entries["init"][1] == (Path(__file__).resolve().parents[1] / "boot0/init").read_bytes(),
    }
    if no_fb is not None:
        checks['framebuffer_rejects_missing_device'] = no_fb.returncode == 2 and 'FB_OPEN_FAILED' in no_fb.stderr
    if args.incompatible_bsp:
        checks["previous_incompatible_bsp_rejected"] = rejected_previous is not None
    report = {
        "revision": "r6", "scope": "packaging, ELF startup, framebuffer memory rendering and panel selection",
        "physical_boot_tested": False, "gpu_tested": False,
        "physical_display_tested": False, "panel_selection_cases": panel_cases,
        "framebuffer_preview_sha256": hashlib.sha256((root / 'display-pattern.ppm').read_bytes()).hexdigest(),
        "initramfs_sha256": hashlib.sha256(blob).hexdigest(),
        "checks": checks, "initialized_libraries": observed,
        "gpu_firmware": firmware,
        "gpu_profile": profile_check,
        "previous_incompatible_bsp_rejection": rejected_previous,
        "vulkan_exit_code": positive.returncode,
        "compute_without_gpu_exit_code": compute.returncode,
        "tegra_hardware_probe_observed": "NvRmGpuLibOpen failed" in positive.stderr,
        "note": "GPU initialization fails on this host without Tegra hardware; this is not a successful GPU test.",
        "passed": all(checks.values()),
    }
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
