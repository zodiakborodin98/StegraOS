#!/usr/bin/env python3
"""Stage the pinned L4T graphics runtime in a private SteamOS directory.

No Debian maintainer scripts or target binaries run. Generic SteamOS GLVND,
Vulkan loader, DRM and GBM libraries remain authoritative. This is a diagnostic
runtime bundle, not a working gamescope backend or an enabled display session.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys

PREFIX = "/usr/lib/steamos-switch-nvidia"
PACKAGES = {"nvidia-l4t-3d-core", "nvidia-l4t-core", "nvidia-l4t-wayland"}
EXCLUDED = {"libdrm.so.2", "libnvgbm.so"}


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(4 * 1024**2), b""):
            h.update(chunk)
    return h.hexdigest()


def checked_source(base, relative):
    parts = PurePosixPath(relative).parts
    if not parts or relative.startswith("/") or ".." in parts:
        raise ValueError(f"Invalid source path: {relative}")
    item = base
    for part in parts:
        item /= part
        if item.is_symlink():
            raise ValueError(f"Source symlink requires review: {item}")
    if not item.is_file():
        raise ValueError(f"Missing regular source file: {item}")
    return item


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", type=Path, help="Linux work directory produced by stage-rootfs.sh")
    parser.add_argument("--plan", action="store_true", help="Verify inputs and print plan without writing")
    args = parser.parse_args()
    if os.name != "posix":
        raise ValueError("Run in Linux/WSL on the native Linux staging filesystem.")
    stage = args.stage.absolute()
    if stage == Path("/") or stage.is_symlink() or stage.resolve() != stage:
        raise ValueError("Stage must be an absolute directory without symlink parents.")
    project = Path(__file__).resolve().parent.parent
    lock_bytes = (project / "sources.lock.json").read_bytes()
    if (stage / "sources.lock.json").read_bytes() != lock_bytes:
        raise ValueError("Staged lock differs from the project lock.")
    lock = json.loads(lock_bytes)
    spec = importlib.util.spec_from_file_location("root_audit", project / "scripts/audit-rootfs.py")
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    if (stage / "rootfs").is_symlink():
        raise ValueError("The staged rootfs must not be a symlink.")
    fs = audit.RootFS(stage / "rootfs")
    if fs.read("/etc/os-release") != (stage / "source-os-release").read_bytes():
        raise ValueError("SteamOS identity differs from the preserved source.")
    release = fs.read("/etc/os-release").decode()
    if "ID=steamos" not in release and 'ID="steamos"' not in release:
        raise ValueError("The target does not identify as genuine SteamOS.")
    destination = fs.resolve("/usr/lib") / "steamos-switch-nvidia"
    if destination.exists() or destination.is_symlink():
        raise ValueError("Private runtime already exists; refusing overwrite.")
    if not destination.parent.is_dir():
        raise ValueError("SteamOS /usr/lib must already exist.")

    for artifact in lock["artifacts"]:
        package = artifact["name"].split("_", 1)[0]
        if package in PACKAGES:
            archive = checked_source(project / "downloads", artifact["name"])
            if archive.stat().st_size != artifact["size"] or digest(archive) != artifact["sha256"]:
                raise ValueError(f"Locked archive check failed: {artifact['name']}")
    report = json.loads((project / "downloads/nvidia-inspect/elf-report.json").read_text())
    selected = []
    provided = set()
    for entry in report:
        package = entry["package"].split("_", 1)[0]
        if package not in PACKAGES:
            continue
        rel = PurePosixPath(entry["path"])
        if rel.name in EXCLUDED:
            continue
        if "tegra" in rel.parts or "tegra-egl" in rel.parts:
            target = "lib/" + rel.name
        elif "xorg" in rel.parts:
            target = str(PurePosixPath("xorg/modules") / rel.parent.name / rel.name)
        else:
            continue
        source = checked_source(stage / "nvidia" / package, str(rel))
        if entry["machine"] != 183 or entry["bits"] != 64 or digest(source) != entry["sha256"]:
            raise ValueError(f"ELF architecture or pinned inspection hash mismatch: {source}")
        selected.append((entry, source, target))
        provided.update(entry["soname"])
        provided.add(rel.name)
    if not {"libGLX_nvidia.so.0", "libEGL_nvidia.so.0", "nvidia_drv.so"} <= provided:
        raise ValueError("Required NVIDIA graphics entry points were not selected.")
    dependencies = sorted({needed for entry, _, _ in selected for needed in entry["needed"]} - provided)
    missing = []
    for name in dependencies:
        try:
            found = fs.resolve("/usr/lib/" + name).is_file()
        except FileNotFoundError:
            found = False
        if not found:
            missing.append(name)
    manifest = {
        "schema": 1, "status": "private diagnostic runtime; no GPU or gamescope validation",
        "prefix": PREFIX, "generic_library_replacements": [],
        "excluded": sorted(EXCLUDED | {"libvulkan.so.1.2.141"}),
        "steam_os_dependencies": dependencies, "missing_steam_os_dependencies": missing,
        "libraries": [{"destination": target, "source_package": entry["package"],
                       "source_path": entry["path"], "sha256": entry["sha256"],
                       "needed": entry["needed"], "soname": entry["soname"]}
                      for entry, _, target in selected],
    }
    if args.plan:
        print(json.dumps(manifest, indent=2))
        return 1 if missing else 0
    if missing:
        raise ValueError("Resolve missing SteamOS SONAMEs before staging: " + ", ".join(missing))
    destination.mkdir(mode=0o755)
    for entry, source, relative in selected:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        with source.open("rb") as src, target.open("xb") as out:
            shutil.copyfileobj(src, out)
        target.chmod(0o755)
        if relative.startswith("lib/"):
            for soname in entry["soname"]:
                if "/" in soname or soname in (".", ".."):
                    raise ValueError("Unexpected SONAME")
                link = target.parent / soname
                if link != target:
                    link.symlink_to(target.name)
    config = destination / "config"
    config.mkdir(mode=0o755)
    def write(name, data, mode=0o644):
        path = destination / name
        path.write_text(data, encoding="utf-8")
        path.chmod(mode)
    write("config/nvidia_icd.json", json.dumps({"file_format_version": "1.0.0", "ICD": {
        "library_path": PREFIX + "/lib/libGLX_nvidia.so.0", "api_version": "1.2.131"}}, indent=2) + "\n")
    write("config/nvidia_egl.json", json.dumps({"file_format_version": "1.0.0", "ICD": {
        "library_path": PREFIX + "/lib/libEGL_nvidia.so.0"}}, indent=2) + "\n")
    write("run", """#!/bin/sh
set -eu
if [ "$#" -eq 0 ]; then printf '%s\\n' 'Usage: run COMMAND [ARGUMENT...]' >&2; exit 2; fi
prefix=/usr/lib/steamos-switch-nvidia
export LD_LIBRARY_PATH="$prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export VK_DRIVER_FILES="$prefix/config/nvidia_icd.json"
export VK_ICD_FILENAMES="$VK_DRIVER_FILES"
export __EGL_VENDOR_LIBRARY_FILENAMES="$prefix/config/nvidia_egl.json"
export __GLX_VENDOR_LIBRARY_NAME=nvidia
exec "$@"
""", 0o755)
    xorg = (stage / "bsp/etc/X11/xorg.conf").read_text()
    xorg += '\nSection "Files"\n    ModulePath "' + PREFIX + '/xorg/modules"\n    ModulePath "/usr/lib/xorg/modules"\nEndSection\n'
    write("config/xorg.conf", xorg)
    monitor = stage / "bsp/etc/X11/xorg.conf.d/10-monitor.conf"
    if monitor.is_file() and not monitor.is_symlink():
        write("config/10-monitor.conf.reference", monitor.read_text())
    for package in sorted(PACKAGES):
        docs = stage / "nvidia" / package / "usr/share/doc"
        if docs.is_dir():
            for source in docs.rglob("*"):
                if source.is_file() and not source.is_symlink():
                    relative = source.relative_to(docs)
                    source = checked_source(docs, str(relative))
                    target = destination / "licenses" / relative
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    if not target.exists():
                        shutil.copyfile(source, target)
                        target.chmod(0o644)
    write("manifest.json", json.dumps(manifest, indent=2) + "\n")
    print(f"Staged {len(selected)} verified ELF files at {destination}.")
    print("No display session enabled; no GPU validation; stock gamescope remains unverified.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as error:
        sys.exit(f"graphics-stage: {error}")
