#!/usr/bin/env python3
"""Install pinned Tegra21x firmware and bounded first-boot GPU diagnostics.

Default is read-only plan. --apply writes only to the supplied native staging
rootfs; no target program runs and no SD card is accessed.
"""
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile

FIRMWARE = {
    "nvhost_nvjpg010.fw", "nv_acr_ucode_prod.bin", "gpmu_ucode_desc.bin",
    "pmu_sig.bin", "vic04_ucode.bin", "gpu2cde.bin", "gpccs.bin", "fecs.bin",
    "pmu_bl.bin", "nvhost_nvdec020_ns.fw", "gpmu_ucode_image.bin",
    "fecs_sig.bin", "gpmu_ucode.bin", "NETB_img.bin", "nvhost_nvenc050.fw",
}
SERVICE = "steamos-switch-graphics-probe.service"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if os.name != "posix":
        raise ValueError("Use WSL/Linux on the native staging filesystem.")
    stage = args.stage.absolute()
    if stage == Path("/") or stage.resolve() != stage or (stage / "rootfs").is_symlink():
        raise ValueError("Use an explicit native staging directory without symlink parents.")
    project = Path(__file__).resolve().parent.parent
    lock_data = (project / "sources.lock.json").read_bytes()
    if (stage / "sources.lock.json").read_bytes() != lock_data:
        raise ValueError("Staged source lock does not match the project.")
    spec = importlib.util.spec_from_file_location("root_audit", project / "scripts/audit-rootfs.py")
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    fs = audit.RootFS(stage / "rootfs")
    if fs.read("/etc/os-release") != (stage / "source-os-release").read_bytes():
        raise ValueError("SteamOS identity differs from the source.")
    for required in ("/usr/lib/steamos-switch-nvidia/run", "/usr/lib/Xorg", "/usr/bin/python3",
                     "/usr/bin/glxinfo", "/usr/bin/eglinfo", "/usr/bin/vulkaninfo", "/usr/bin/timeout",
                     "/usr/bin/xauth", "/usr/bin/xdpyinfo", "/usr/bin/pgrep", "/usr/bin/gamescope",
                     "/usr/bin/chvt", "/usr/bin/udevadm"):
        fs.resolve(required)
    # Packages are read as an archive. Their maintainer scripts are never run.
    artifacts = [a for a in json.loads(lock_data)["artifacts"]
                 if a["name"].startswith("nvidia-l4t-firmware_")]
    if len(artifacts) != 1:
        raise ValueError("Expected exactly one pinned NVIDIA firmware package.")
    artifact = artifacts[0]
    archive = project / "downloads" / artifact["name"]
    if archive.is_symlink() or archive.stat().st_size != artifact["size"] or sha(archive.read_bytes()) != artifact["sha256"]:
        raise ValueError("Pinned firmware archive hash mismatch.")
    container = subprocess.run(["dpkg-deb", "--fsys-tarfile", str(archive)],
                               check=True, stdout=subprocess.PIPE).stdout
    plans = []
    found = set()
    licenses = 0
    with tarfile.open(fileobj=io.BytesIO(container), mode="r:") as tar:
        for entry in tar:
            path = PurePosixPath(entry.name)
            if str(path).startswith("lib/firmware/tegra21x/"):
                if path.name not in FIRMWARE or not entry.isfile() or len(path.parts) != 4:
                    raise ValueError(f"Unexpected Tegra firmware entry: {entry.name}")
                if path.name in found:
                    raise ValueError("Duplicate firmware member")
                found.add(path.name)
                payload = tar.extractfile(entry).read()
                source = stage / "nvidia/nvidia-l4t-firmware" / path
                if source.is_symlink() or source.read_bytes() != payload:
                    raise ValueError(f"Staged firmware differs from pinned archive: {path.name}")
                plans.append(("/usr/lib/firmware/tegra21x/" + path.name, payload, 0o644, "firmware"))
            elif str(path).startswith("usr/share/doc/nvidia-l4t-firmware/") and entry.isfile():
                if len(path.parts) != 5 or ".." in path.parts:
                    raise ValueError("Unexpected firmware license path")
                payload = tar.extractfile(entry).read()
                plans.append(("/usr/share/doc/steamos-switch-firmware/" + path.name, payload, 0o644, "license"))
                licenses += 1
    if found != FIRMWARE or licenses == 0:
        raise ValueError("Pinned firmware or license list is incomplete.")
    overlay = project / "overlays/graphics/rootfs"
    overlay_files = (
        "etc/systemd/system/" + SERVICE,
        "usr/lib/steamos-switch/collect-graphics-info",
        "usr/lib/steamos-switch/summarize-graphics-info",
    )
    for relative in overlay_files:
        source = overlay / relative
        if source.is_symlink():
            raise ValueError("Overlay source symlinks are unsupported.")
        if not source.is_file():
            raise ValueError(f"Missing probe asset: {relative}")
        mode = 0o755 if relative.startswith("usr/lib/steamos-switch/") else 0o644
        plans.append(("/" + relative, source.read_bytes(), mode, "probe"))

    def destination(logical):
        # Resolve existing Linux symlinks against rootfs, then create new children
        # inside that resolved parent. Never follow a new target-name symlink.
        parts = PurePosixPath(logical).parts
        for count in range(len(parts) - 1, 0, -1):
            parent_logical = str(PurePosixPath(*parts[:count]))
            try:
                parent = fs.resolve(parent_logical)
            except FileNotFoundError:
                continue
            if not parent.is_dir():
                raise ValueError(f"Not a destination directory: {parent_logical}")
            target = parent.joinpath(*parts[count:])
            current = parent
            for part in parts[count:]:
                current /= part
                if current.is_symlink():
                    raise ValueError(f"Refusing destination symlink: {current}")
            return target
        raise ValueError("Could not resolve rootfs destination")

    report = {"schema": 1, "mode": "apply" if args.apply else "plan", "files": []}
    resolved = []
    for logical, payload, mode, role in plans:
        target = destination(logical)
        if target.exists() and (not target.is_file() or target.read_bytes() != payload):
            raise ValueError(f"Refusing different pre-existing file: {logical}")
        resolved.append((target, payload, mode))
        report["files"].append({"path": logical, "role": role, "sha256": sha(payload),
                                "existing_identical": target.exists()})
    wants = fs.resolve("/etc/systemd/system/multi-user.target.wants") / SERVICE
    if wants.is_symlink():
        if os.readlink(wants) != "../" + SERVICE:
            raise ValueError("Different service enablement already exists.")
    elif wants.exists():
        raise ValueError("Service enablement name is occupied.")
    if args.apply:
        if os.geteuid() != 0:
            raise ValueError("Apply as root to preserve target ownership.")
        for target, payload, mode in resolved:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            if not target.exists():
                with target.open("xb") as stream:
                    stream.write(payload)
            target.chmod(mode)
        wants.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        if not wants.is_symlink():
            wants.symlink_to("../" + SERVICE)
    report["note"] = "Only 15 Tegra21x firmware files; existing TSEC and Switch Wi-Fi firmware untouched. GPU execution occurs only at target boot."
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        sys.exit(f"prepare-graphics-probe: {exc}")
