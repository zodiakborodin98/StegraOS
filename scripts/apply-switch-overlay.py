#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Apply the diagnostic boot layer to an OFFLINE, staged SteamOS Switch rootfs.

Run on Linux after stage-rootfs.sh. No target binary, mount, disk, password or
network command runs. This is a console bring-up layer, not a Game Mode port.
The tree must not be modified concurrently by another build process.
"""

import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import uuid


PROJECT = Path(__file__).resolve().parent.parent
MANIFEST = "usr/lib/steamos-switch/boot-overlay.json"
REQUIRED_BOOTARGS = (
    "SYSTEMD_CGROUP_ENABLE_LEGACY_FORCE=1",
    "systemd.unit=multi-user.target",
    "systemd.gpt_auto=0",
)
# Verified against the pinned Frame image. Masking leaves vendor files intact.
SYSTEM_MASKS = (
    "atomupd.service", "rauc.service", "steamos-boot.service",
    "steamos-post-update.service", "holo-post-update-shutdown.service",
    "systemd-repart.service", "systemd-boot-update.service",
    "setubootenv.service", "steamos-manager.service",
    "steamos-offload.target", "etc.mount", "home.mount", "efi.mount", "esp.mount",
    "root.mount", "srv.mount", "var-cache-pacman.mount", "var-lib-containers.mount",
    "var-lib-docker.mount", "var-lib-flatpak.mount", "var-lib-systemd-coredump.mount",
    "var-lib-steamos\\x2dlog\\x2dsubmitter.mount", "var-log.mount", "var-tmp.mount",
    "adbd.service", "adbd-pre.service", "adbd-post.service", "usb-gadget.service",
    "usb-gadget.target", "usb-ncm-gadget@.service", "usb-ncm-dnsmasq@.service",
    "dsp_service.service", "iris-driver-rebind.service", "gpu-trace.service",
    "set-wifi-mac-address.service", "set-bluetooth-mac-address.service",
    "softapmanager.service", "steam-video-codec-setup.service",
    "steam-web-debug-portforward.service", "steamvr-web-debug-portforward.service",
    # Diagnostic first boot must not enter the Frame display/session stack.
    "sddm.service", "display-manager.service",
)
USER_MASKS = (
    "steam.service", "gamescope-session.service", "gamescope-session.target",
    "steamos-powerbuttond.service", "steamos-manager.service",
    "steamos-manager-configure-cecd.service", "steamos-scheduler.service",
)
RULE_MASKS = (
    "90-mac-addresses.rules", "99-sdcard-rescan.rules", "99-steamos-automount.rules",
    "99-eeprom.rules", "99-steamvr.rules", "typec-plug-event.rules",
)


class OverlayError(ValueError):
    pass


class OfflineTree:
    """Linux dir-fd operations; never follows symlinks in write paths."""

    def __init__(self, path):
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise OverlayError("Run on native Linux storage, preferably inside WSL.")
        self.root = Path(path)
        if not self.root.is_absolute() or ".." in self.root.parts or self.root == Path("/"):
            raise OverlayError("Rootfs must be an absolute non-root path without '..'.")
        for part in reversed((self.root, *self.root.parents)):
            if not stat.S_ISDIR(part.lstat().st_mode):
                raise OverlayError(f"Rootfs or an ancestor is not a real directory: {part}")
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def close(self):
        os.close(self.fd)

    @staticmethod
    def parts(relative):
        p = PurePosixPath(relative)
        if (not relative or p.is_absolute() or any(v in ("", ".", "..") for v in relative.split("/"))
                or any(ord(c) < 32 for c in relative)):
            raise OverlayError(f"Unsafe relative target: {relative!r}")
        return p.parts

    @contextlib.contextmanager
    def parent(self, relative, create=False):
        parts = self.parts(relative)
        fd = os.dup(self.fd)
        try:
            for name in parts[:-1]:
                if create:
                    try:
                        os.mkdir(name, 0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def check_parents(self, relative):
        # Missing parents are permitted; symlinks and non-directories are not.
        fd = os.dup(self.fd)
        try:
            for name in self.parts(relative)[:-1]:
                try:
                    next_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    return
                os.close(fd)
                fd = next_fd
        finally:
            os.close(fd)

    def snapshot(self, relative):
        self.check_parents(relative)
        try:
            with self.parent(relative) as (fd, name):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    return {"kind": "symlink", "target": os.readlink(name, dir_fd=fd)}
                if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                    raise OverlayError(f"Expected a bounded regular file or symlink: {relative}")
                handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                with os.fdopen(handle, "rb") as source:
                    data = source.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise OverlayError(f"File too large: {relative}")
                return {"kind": "file", "mode": stat.S_IMODE(info.st_mode),
                        "data": base64.b64encode(data).decode("ascii")}
        except FileNotFoundError:
            return {"kind": "absent"}

    def read(self, relative):
        value = self.snapshot(relative)
        if value["kind"] != "file":
            raise OverlayError(f"Expected a regular metadata file: {relative}")
        return base64.b64decode(value["data"], validate=True)

    def write(self, relative, value):
        # Atomic replacement avoids truncating a symlink target or a hardlink.
        with self.parent(relative, create=True) as (fd, name):
            temporary = f".switch-overlay-{uuid.uuid4().hex}"
            try:
                if value["kind"] == "symlink":
                    os.symlink(value["target"], temporary, dir_fd=fd)
                else:
                    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     value["mode"], dir_fd=fd)
                    with os.fdopen(handle, "wb") as target:
                        target.write(base64.b64decode(value["data"], validate=True))
                        os.fchmod(target.fileno(), value["mode"])
                        os.fsync(target.fileno())
                os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass


def file_value(text, mode=0o644):
    data = text.encode() if isinstance(text, str) else text
    return {"kind": "file", "mode": mode, "data": base64.b64encode(data).decode("ascii")}


def link_value(target="/dev/null"):
    return {"kind": "symlink", "target": target}


def plan(tree, fat_uuid=None):
    if fat_uuid is not None and not re.fullmatch(r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}", fat_uuid):
        raise OverlayError("FAT UUID must use XXXX-XXXX hexadecimal format.")
    # The pinned source has no overlay upper. Do not silently ignore user data
    # from a previously booted SteamOS tree or install a competing /etc layer.
    upper = tree.root / "var/lib/overlays"
    if os.path.lexists(upper):
        raise OverlayError("Existing /var/lib/overlays needs explicit migration; use a freshly staged tree.")
    tree.check_parents("var/lib/overlays/placeholder")
    fstab = ("# Managed by SteamOS Switch diagnostic overlay; one writable ext4 root.\n"
             "# /etc, /var and /home remain directories on this root filesystem.\n"
             "LABEL=SWR-STM / ext4 defaults,noatime 0 1\n")
    if fat_uuid:
        fstab += (f"UUID={fat_uuid.upper()} /boot vfat noauto,nofail,ro,umask=0077,"
                  "x-systemd.device-timeout=5s 0 0\n")
    else:
        fstab += "# FAT boot partition is not mounted by systemd; Switchroot initramfs handles it.\n"
    items = {"etc/fstab": file_value(fstab),
             "etc/switchroot_version.conf": file_value("5.1.2\n"),
             "etc/systemd/system/default.target": link_value("/usr/lib/systemd/system/multi-user.target"),
             # BSP already initializes zram; prevent a second configurator.
             "etc/systemd/system-generators/zram-generator": link_value(),
             "etc/systemd/system-generators/systemd-gpt-auto-generator": link_value(),
             "etc/tmpfiles.d/steamos-offload.conf": link_value(),
             "etc/systemd/system/multi-user.target.wants/steamos-switch-boot-info.service":
                 link_value("/etc/systemd/system/steamos-switch-boot-info.service"),
             "etc/systemd/system/getty.target.wants/getty@tty1.service":
                 link_value("/usr/lib/systemd/system/getty@.service")}
    masks = set(SYSTEM_MASKS)
    user_masks = set(USER_MASKS)
    # Include all actual Frame hardware and VR units, including future names in
    # this pinned package set. Only unit basenames under known dirs are used.
    for scope, collected in (("system", masks), ("user", user_masks)):
        directory = f"usr/lib/systemd/{scope}"
        tree.check_parents(f"{directory}/placeholder")
        for entry in os.listdir(tree.root / directory):
            if entry.startswith(("deckard-", "steamvr-", "steamvr.")) and entry.endswith((".service", ".target", ".socket", ".timer")):
                collected.add(entry)
        for name in sorted(collected):
            items[f"etc/systemd/{scope}/{name}"] = link_value()
    for name in RULE_MASKS:
        items[f"etc/udev/rules.d/{name}"] = link_value()
    for source in sorted((PROJECT / "overlays/boot/rootfs").rglob("*")):
        if source.is_symlink():
            raise OverlayError(f"Source overlay may not contain symlinks: {source}")
        if source.is_file():
            relative = source.relative_to(PROJECT / "overlays/boot/rootfs").as_posix()
            if relative in items:
                raise OverlayError(f"Duplicate managed target: {relative}")
            items[relative] = file_value(source.read_bytes(), 0o755 if relative.endswith("/collect-boot-info") else 0o644)
    return dict(sorted(items.items()))


def apply(root, fat_uuid=None, dry_run=False):
    tree = OfflineTree(root)
    try:
        release = tree.read("usr/lib/os-release")
        if not re.search(rb"(?m)^ID=[\"']?steamos[\"']?$", release):
            raise OverlayError("Expected genuine SteamOS os-release; refusing another OS.")
        if tree.read("etc/switchroot_version.conf").strip() != b"5.1.2":
            raise OverlayError("Run pinned Switchroot BSP staging first.")
        os_release_entry = tree.snapshot("etc/os-release")
        items = plan(tree, fat_uuid)
        for relative in (*items, MANIFEST):
            tree.check_parents(relative)
        existing = tree.snapshot(MANIFEST)
        current = {relative: tree.snapshot(relative) for relative in items}
        if existing["kind"] == "absent":
            manifest = {"schema": 1, "status": "applying", "phase": "diagnostic-console",
                        "layout": "single-ext4-mutable", "os_release_sha256": hashlib.sha256(release).hexdigest(),
                        "required_bootargs": list(REQUIRED_BOOTARGS), "fat_uuid": fat_uuid,
                        "originals": current, "desired": items}
        else:
            if existing["kind"] != "file":
                raise OverlayError("Overlay manifest must not be a symlink.")
            manifest = json.loads(tree.read(MANIFEST))
            if (manifest.get("schema") != 1 or manifest.get("desired") != items
                    or manifest.get("os_release_sha256") != hashlib.sha256(release).hexdigest()
                    or set(manifest.get("originals", {})) != set(items)):
                raise OverlayError("Overlay configuration changed; restage before changing this diagnostic profile.")
            for relative, value in current.items():
                if value not in (items[relative], manifest["originals"][relative]):
                    raise OverlayError(f"Managed target changed outside this build: {relative}")
        # Existing final links are replaced, never followed. But replacing fstab
        # through an unexplained link is a layout change requiring review.
        if manifest["originals"]["etc/fstab"]["kind"] == "symlink":
            raise OverlayError("Original /etc/fstab is a symlink; inspect this layout before editing.")
        if dry_run:
            return {"status": "dry-run", "managed_paths": list(items), "required_bootargs": list(REQUIRED_BOOTARGS)}
        manifest["status"] = "applying"
        tree.write(MANIFEST, file_value(json.dumps(manifest, indent=2, sort_keys=True) + "\n"))
        for relative, value in items.items():
            if current[relative] != value:
                tree.write(relative, value)
        if tree.read("usr/lib/os-release") != release or tree.snapshot("etc/os-release") != os_release_entry:
            raise OverlayError("SteamOS os-release unexpectedly changed.")
        manifest["status"] = "applied"
        tree.write(MANIFEST, file_value(json.dumps(manifest, indent=2, sort_keys=True) + "\n"))
        return {"status": "applied", "phase": "diagnostic-console", "managed_paths": len(items),
                "required_bootargs": list(REQUIRED_BOOTARGS),
                "warning": "No hardware boot, login credentials, graphics or Game Mode validated."}
    finally:
        tree.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rootfs", type=Path)
    parser.add_argument("--fat-uuid", help="Optional FAT UUID; configure a read-only manual /boot mount.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(apply(args.rootfs, args.fat_uuid, args.dry_run), indent=2))
    except (OSError, ValueError) as exc:
        print(f"Overlay failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
