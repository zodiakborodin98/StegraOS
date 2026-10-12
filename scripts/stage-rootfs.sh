#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Prepare an inspectable native SteamOS tree. This does not create a bootable port.
set -Eeuo pipefail
umask 077

usage() {
    while IFS= read -r help_line; do printf '%s\n' "$help_line"; done <<'HELP'
Usage: sudo bash scripts/stage-rootfs.sh /absolute/new/linux/work-directory

Stage the locked, genuine SteamOS ARM rootfs and Switch BSP for port development.
The destination must not exist; its parent must exist on a native Linux filesystem.
Use a directory such as /home/your-user/steamos-switch-stage, not /mnt/c/...
Source downloads may reside on Windows storage. No source download is modified.

Prerequisites (Ubuntu/WSL): bash, python3, rsync, dpkg-deb, tar, util-linux,
                          btrfs-progs, coreutils; root and loop/Btrfs support.
Provision at least 20 GiB free space in the destination filesystem.
Run prepare-inputs.py first and reconstruct downloads/steamos-rootfs.img.
The source rootfs is mounted read-only with Btrfs log replay disabled.

Outputs: rootfs/, bsp/, nvidia/<package>/, sdcard/, audit.json, audit.txt, STATUS.txt.
NVIDIA archives remain separate for ABI analysis; their maintainer scripts never run.
No SD card is written, no image is declared bootable, and no target binary runs.
An audit blocker returns failure and retains the staged files and reports.
HELP
}
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
if [[ ${1:-} == --help || ${1:-} == -h ]]; then usage; exit 0; fi
[[ $# == 1 ]] || { usage >&2; exit 2; }
[[ $(uname -s) == Linux ]] || fail 'Run in Linux or WSL after its required reboot.'
[[ $EUID == 0 ]] || fail 'Root is required for the read-only loop mount and ownership-preserving copy.'
for tool in python3 rsync dpkg-deb tar findmnt mountpoint mount umount losetup realpath mkdir rmdir cp cat; do
    command -v "$tool" >/dev/null || fail "Missing prerequisite: $tool"
done

project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ -f $project/sources.lock.json && -f $project/scripts/audit-rootfs.py ]] || fail 'Project files are missing.'
workdir=$(python3 - "$1" <<'PY'
import os, pathlib, stat, sys
raw = sys.argv[1]
if not raw.startswith('/') or any(c in raw for c in '\n\r\t\0'):
    sys.exit('Destination must be an absolute Linux path without control characters.')
path = pathlib.Path(os.path.abspath(raw))
if path == pathlib.Path('/') or os.path.lexists(path):
    sys.exit('Refusing root or an existing destination, including a symlink.')
if not path.parent.is_dir():
    sys.exit('Destination parent must already exist.')
for part in (path.parent, *path.parent.parents):
    if stat.S_ISLNK(part.lstat().st_mode):
        sys.exit('Destination parents must not be symlinks.')
if str(path).startswith('/mnt/') and len(path.parts) > 2 and len(path.parts[2]) == 1:
    sys.exit('Use the Linux filesystem, not a Windows drive under /mnt/.')
print(path)
PY
)
filesystem=$(findmnt -n -o FSTYPE --target "$(dirname -- "$workdir")")
case "$filesystem" in
    ext2|ext3|ext4|btrfs|xfs|f2fs|zfs) ;;
    *) fail "Destination filesystem '$filesystem' is not supported; use native Linux storage, not drvfs/9p/NTFS." ;;
esac

printf 'Checking locked inputs (includes hashing the 10 GiB rootfs)...\n'
python3 - "$project" "$workdir" <<'PY'
import hashlib, json, os, pathlib, re, shutil, stat, sys
project, destination = map(pathlib.Path, sys.argv[1:])
lock = json.loads((project / 'sources.lock.json').read_text())
downloads = (project / 'downloads').resolve(strict=True)
def check(name, size, expected):
    if not re.fullmatch(r'[A-Za-z0-9_.+\-]+', name):
        raise ValueError('Invalid artifact filename in lock')
    path = downloads / name
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f'Input is not a regular, non-symlink file: {name}')
    if path.stat().st_size != size or not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ValueError(f'Input size or lock hash is invalid: {name}')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError(f'SHA-256 mismatch: {name}')
    print(f'  Verified {name}', flush=True)
try:
    if lock.get('schema') != 1:
        raise ValueError('Unsupported lock schema')
    check('steamos-rootfs.img', lock['steamos']['rootfs_size'], lock['steamos']['rootfs_sha256'])
    bsp = [a for a in lock['artifacts'] if a.get('role') == 'switch-bsp']
    gpu = [a for a in lock['artifacts'] if a.get('role') == 'nvidia-analysis']
    if len(bsp) != 1 or bsp[0]['name'] != 'switch-bsp_5.1.2-34+24.04_all.deb' or len(gpu) != 8:
        raise ValueError('Expected one pinned Switch BSP and eight NVIDIA packages')
    names = [a['name'] for a in lock['artifacts']]
    if len(names) != len(set(names)):
        raise ValueError('Duplicate artifact names')
    for artifact in lock['artifacts']:
        check(artifact['name'], artifact['size'], artifact['sha256'])
    if shutil.disk_usage(destination.parent).free < 20 * 1024**3:
        raise ValueError('At least 20 GiB free destination space is required')
except (ValueError, OSError, KeyError) as exc:
    sys.exit(f'Input validation failed: {exc}')
PY

# mkdir is deliberately non-recursive and fails if the destination appeared meanwhile.
mkdir -- "$workdir"
source_mount=$workdir/.source-mount
loop_device=''
cleanup() {
    local result=$? mounted_source=''
    trap - EXIT INT TERM
    if [[ -d $source_mount ]] && mountpoint -q -- "$source_mount"; then
        mounted_source=$(findmnt --nofsroot -n -o SOURCE --mountpoint "$source_mount") || true
        # A mount other than the loop device created below must never be unmounted.
        if [[ -n $loop_device && $mounted_source == "$loop_device" ]]; then
            if ! umount -- "$source_mount"; then
                printf 'Cleanup: unmount failed; retained %s and %s.\n' "$source_mount" "$loop_device" >&2
                exit 2
            fi
        else
            printf 'Cleanup: unexpected mount retained at %s.\n' "$source_mount" >&2
            exit 2
        fi
    fi
    if [[ -n $loop_device ]] && ! losetup --detach "$loop_device"; then
        printf 'Cleanup: could not detach owned loop device %s.\n' "$loop_device" >&2
        result=2
    fi
    if [[ -d $source_mount ]]; then rmdir -- "$source_mount" || true; fi
    if [[ $result != 0 ]]; then
        printf 'Staging incomplete; retained files and diagnostics: %s\n' "$workdir" >&2
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -- "$source_mount" "$workdir/rootfs" "$workdir/bsp" "$workdir/nvidia"
cp -- "$project/sources.lock.json" "$workdir/sources.lock.json"
cat > "$workdir/STATUS.txt" <<'STATUS'
STAGING IN PROGRESS — NOT A BOOTABLE SWITCH IMAGE
Source: genuine SteamOS ARM rootfs; Switchroot supplies hardware artifacts only.
Unfinished: NVIDIA/GLVND/Wayland ABI integration, systemd/kernel compatibility,
ARMv8.0 execution checks, input/audio/power services, Steam and Gamescope.
No target binaries, hardware boot, or games have been tested by this script.
STATUS

loop_device=$(losetup --find --show --read-only "$project/downloads/steamos-rootfs.img")
mount -t btrfs -o ro,rescue=nologreplay "$loop_device" "$source_mount"
printf 'Copying genuine SteamOS while preserving Linux metadata...\n'
rsync -aHAX --numeric-ids --filter='-x btrfs.*' -- "$source_mount/" "$workdir/rootfs/"

printf 'Extracting Switch BSP without running package scripts...\n'
dpkg-deb --extract "$project/downloads/switch-bsp_5.1.2-34+24.04_all.deb" "$workdir/bsp"
boot_source=$workdir/bsp/opt/switchroot/bootstack
boot_target=$workdir/sdcard/switchroot/steamos
mkdir -p -- "$boot_target" "$workdir/sdcard/bootloader/ini"
for artifact in bl31.bin bl33.bin boot.scr uImage initramfs nx-plat.dtimg README_CONFIG.txt; do
    [[ -f $boot_source/$artifact && ! -L $boot_source/$artifact && -s $boot_source/$artifact ]] || fail "Missing boot artifact: $artifact"
    cp -- "$boot_source/$artifact" "$boot_target/$artifact"
done

# Resolve logical Linux symlinks against the extracted root, never the host's /usr/lib.
# Validate archive links and existing destination paths before tar sees the archive.
module_destination=$(python3 - "$project" "$workdir" <<'PY'
import importlib.util, os, pathlib, posixpath, stat, sys, tarfile
project, work = map(pathlib.Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('root_audit', project / 'scripts/audit-rootfs.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
fs = audit.RootFS(work / 'rootfs')
destination = fs.resolve('/usr/lib')
if not destination.is_dir() or fs.resolve('/lib') != destination:
    sys.exit('Expected a merged /lib -> /usr/lib tree; inspect this SteamOS layout manually.')
archive = work / 'bsp/opt/switchroot/modules.tar.gz'
with tarfile.open(archive, 'r:gz') as source:
    members = source.getmembers()
    link_names = set()
    for member in members:
        name = posixpath.normpath(member.name)
        if name == '.' and member.isdir():
            continue
        if name.startswith('/') or '..' in name.split('/') or name.split('/')[0] not in ('modules', 'firmware'):
            sys.exit(f'Unexpected module archive path: {member.name}')
        if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
            sys.exit(f'Unsupported module archive member: {member.name}')
        if member.issym() or member.islnk():
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), member.linkname) if member.issym() else member.linkname)
            if member.linkname.startswith('/') or target == '..' or target.startswith('../'):
                sys.exit(f'Module archive link escapes destination: {member.name}')
            link_names.add(name)
        parts = pathlib.PurePosixPath(name).parts
        for count in range(1, len(parts) + 1):
            current = destination.joinpath(*parts[:count])
            if current.is_symlink():
                sys.exit(f'Existing symlink in module destination requires manual review: {current}')
    for member in members:
        parts = pathlib.PurePosixPath(posixpath.normpath(member.name)).parts
        if any('/'.join(parts[:count]) in link_names for count in range(1, len(parts))):
            sys.exit(f'Archive writes through its own link; manual review required: {member.name}')
print(destination)
PY
)
tar --extract --gzip --file "$workdir/bsp/opt/switchroot/modules.tar.gz" --directory "$module_destination" --no-overwrite-dir
python3 - "$project" "$workdir" "$source_mount" <<'PY'
import importlib.util, pathlib, sys
project, work, source = map(pathlib.Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('root_audit', project / 'scripts/audit-rootfs.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
fs = audit.RootFS(work / 'rootfs')
marker = fs.resolve('/etc') / 'switchroot_version.conf'
if marker.is_symlink() or marker.exists():
    sys.exit('Refusing to overwrite an existing Switchroot compatibility marker.')
marker.write_text('5.1.2\n')
marker.chmod(0o644)
original = audit.RootFS(source).read('/etc/os-release')
if fs.read('/etc/os-release') != original:
    sys.exit('SteamOS os-release changed during staging.')
(work / 'source-os-release').write_bytes(original)
PY

package_list=$(python3 - "$workdir/sources.lock.json" <<'PY'
import json, sys
for artifact in json.load(open(sys.argv[1]))['artifacts']:
    if artifact.get('role') == 'nvidia-analysis':
        print(artifact['name'])
PY
)
while IFS= read -r package; do
    [[ -n $package ]] || continue
    package_name=${package%%_*}
    printf 'Extracting %s separately for ABI review...\n' "$package_name"
    dpkg-deb --extract "$project/downloads/$package" "$workdir/nvidia/$package_name"
done <<< "$package_list"

cat > "$workdir/sdcard/bootloader/ini/SteamOS-experimental.ini.draft" <<'INI'
[SteamOS Switch experimental]
l4t=1
boot_prefixes=/switchroot/steamos/
id=SWR-STM
rootfstype=ext4
r2p_action=bootloader
emmc=0
fbconsole=0
bootargs_extra=SYSTEMD_CGROUP_ENABLE_LEGACY_FORCE=1
INI
cat > "$workdir/STATUS.txt" <<'STATUS'
STAGED SOURCE TREE — NOT A BOOTABLE SWITCH IMAGE
The rootfs is genuine SteamOS ARM. A Switchroot compatibility marker and matched
kernel modules/firmware were added; SteamOS os-release was preserved byte-for-byte.
NVIDIA .deb contents are isolated under nvidia/ and have NOT been integrated.
The Hekate INI is a .draft file and is not an active boot entry.
Stock boot.scr requests legacy cgroups; the draft INI explicitly permits them
for the pinned systemd 257 via SYSTEMD_CGROUP_ENABLE_LEGACY_FORCE=1.
That path still needs a runtime check. Stock initramfs performs fsck,
resize and other rootfs mutations on boot. Do not deploy this staging tree as-is.
Unfinished: NVIDIA/GLVND/Wayland ABI integration, systemd/kernel compatibility,
ARMv8.0 execution checks, input/audio/power services, Steam and Gamescope.
Inspect audit.json and audit.txt. An unverified audit result is not boot validation.
No target binaries, hardware boot, or games have been tested by this script.
STATUS

printf 'Running the mandatory offline compatibility audit...\n'
audit_result=0
python3 "$project/scripts/audit-rootfs.py" "$workdir/rootfs" --json > "$workdir/audit.json" 2> "$workdir/audit-errors.txt" || audit_result=$?
text_result=0
python3 "$project/scripts/audit-rootfs.py" "$workdir/rootfs" > "$workdir/audit.txt" 2>> "$workdir/audit-errors.txt" || text_result=$?
if [[ $audit_result != 0 || $text_result != 0 ]]; then
    printf '\nAUDIT FAILED: blockers or audit errors require resolution; see retained reports.\n' >> "$workdir/STATUS.txt"
    fail "Compatibility audit failed; staged tree and reports retained at $workdir"
fi
printf 'Staging complete, hardware integration unfinished: %s\n' "$workdir"
printf 'Review STATUS.txt and audit.txt. This is not a bootable image.\n'
