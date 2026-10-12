#!/bin/bash
# Reproducible, unprivileged build on x86_64 Ubuntu 24.04 with Python, dpkg-deb,
# zstd, binutils and e2fsprogs. Use an unsynchronised temporary build directory.
set -euo pipefail
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
build_dir=${1:?Usage: build.sh /absolute/new/build-directory}
[[ "$build_dir" = /* && ! -e "$build_dir" ]] || { echo 'Build directory must be absolute and new.' >&2; exit 1; }
mkdir -p "$build_dir"
python3 -m venv "$build_dir/venv"
py="$build_dir/venv/bin/python"
"$py" -m pip install -r "$source_dir/requirements.txt"
"$py" "$source_dir/tools/download_alpha.py" --output "$build_dir/downloads"
"$py" "$source_dir/tools/extract_frame.py" "$build_dir/downloads/steamframe-0.3.0.img.zip" --output "$build_dir/parts" --partitions rootfs-A
dpkg-deb -x "$build_dir/downloads/switch-bsp.deb" "$build_dir/bsp"
dpkg-deb -x "$build_dir/downloads/host-squashfs.deb" "$build_dir/host"
dpkg-deb -x "$build_dir/downloads/host-lzo.deb" "$build_dir/host"
dpkg-deb -x "$build_dir/downloads/qemu.deb" "$build_dir/qemu"
dpkg-deb -x "$build_dir/downloads/busybox-static.deb" "$build_dir/busybox"
"$py" "$source_dir/tools/stage_alpha.py" --frame "$build_dir/parts/partition-03.img" --downloads "$build_dir/downloads" --bsp "$build_dir/bsp" --output "$build_dir/root-overlay"
"$py" "$source_dir/tools/validate_alpha.py" --frame "$build_dir/parts/partition-03.img" --overlay "$build_dir/root-overlay" --qemu "$build_dir/qemu/usr/bin/qemu-aarch64-static" --output "$build_dir/validation"
"$py" "$source_dir/tools/build_alpha_rootfs.py" --frame "$build_dir/parts/partition-03.img" --overlay "$build_dir/root-overlay" --mksquashfs "$build_dir/host/usr/bin/mksquashfs" --host-lib "$build_dir/host/lib/x86_64-linux-gnu" --output "$build_dir/rootfs.squashfs"
"$py" "$source_dir/tools/build_alpha_boot.py" --bsp "$build_dir/bsp" --busybox "$build_dir/busybox/usr/bin/busybox" --rootfs "$build_dir/rootfs.squashfs" --output "$build_dir/boot"
"$py" "$source_dir/tools/package_alpha.py" --rootfs "$build_dir/rootfs.squashfs" --boot "$build_dir/boot" --output "$build_dir/Switchdeck-SteamOS-Alpha1.zip"
echo "Built $build_dir/Switchdeck-SteamOS-Alpha1.zip. Hardware boot remains unvalidated."
