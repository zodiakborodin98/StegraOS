#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Build-host dependencies only; this never installs Ubuntu into the Switch image.
set -euo pipefail
[[ $(uname -s) == Linux && $EUID == 0 ]] || { echo 'Run as root in the Linux build environment.' >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  rsync btrfs-progs e2fsprogs dosfstools squashfs-tools util-linux \
  python3 python3-venv python3-pip zstd file ca-certificates curl \
  qemu-user-static binfmt-support binutils-aarch64-linux-gnu \
  u-boot-tools cpio xz-utils zlib1g-dev build-essential pkg-config
modprobe btrfs
grep -w btrfs /proc/filesystems
qemu-aarch64-static --version | head -n 1
