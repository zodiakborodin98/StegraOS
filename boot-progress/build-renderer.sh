#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
set -eu
cd "$(dirname "$0")"
cc -std=c11 -O2 -Wall -Wextra -Werror boot-screen.c -o boot-screen-host
aarch64-linux-gnu-gcc -std=c11 -Os -static -Wall -Wextra -Werror boot-screen.c -o boot-screen-arm64
echo 'Built host and static ARM64 renderers; no device installation performed.'
