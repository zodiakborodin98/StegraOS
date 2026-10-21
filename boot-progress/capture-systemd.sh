#!/bin/bash
# SPDX-License-Identifier: MIT
# Root runtime service, bounded by systemd RuntimeMaxSec. No GPU/display calls.
# Keep stdout out of journald: following the journal must not log back into it.
set -u
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C LANG=C
dir=/run/stegra-boot
base=boot-runtime
IFS= read -r base < "$dir/log-base" || true
case "$base" in boot-[0-9]*|boot-runtime) ;; *) base=boot-runtime ;; esac
destination=/boot/stegra-logs
if [ ! -d "$destination" ] || [ ! -w "$destination" ]; then destination="$dir"; fi
meta="$destination/$base-systemd-started.log"
{
    printf 'SYSTEMD_CAPTURE_STARTED\n'
    printf 'PID1_COMM='; cat /proc/1/comm
    printf 'PID1_EXE='; readlink /proc/1/exe
    printf 'UPTIME='; cat /proc/uptime
    printf 'KERNEL='; uname -r
    printf 'LOGGING_ONLY_LIMIT_SECONDS=1200\n'
} > "$meta" 2>&1
if [ -f "$dir/display.log" ]; then cp "$dir/display.log" "$destination/$base-display.log"; fi
children=()
stop_children() {
    trap - TERM INT
    for pid in "${children[@]}"; do kill "$pid" 2>/dev/null || true; done
    for pid in "${children[@]}"; do wait "$pid" 2>/dev/null || true; done
    # Do not delay shutdown with a global sync on a failing SD.
    exit 0
}
trap stop_children TERM INT
stream() {
    local suffix=$1; shift
    journalctl --boot --follow --no-pager --output=short-monotonic "$@" 2>&1 |
        "$dir/boot-screen" --record "$destination/$base-$suffix.log" >/dev/null
    local results=("${PIPESTATUS[@]}")
    printf 'STREAM_%s_EXIT journalctl=%s capture=%s\n' "$suffix" "${results[0]}" "${results[1]}" >> "$meta"
}
stream system & children+=("$!")
stream warnings --priority=warning & children+=("$!")
stream steam --unit=steam-cpu-session.service & children+=("$!")
for pid in "${children[@]}"; do wait "$pid" || true; done
printf 'SYSTEMD_CAPTURE_ENDED\n' >> "$meta"
