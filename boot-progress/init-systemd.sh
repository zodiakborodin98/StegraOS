#!/bin/bash
# SPDX-License-Identifier: MIT
# Reached only after stock switch_root. Must retain PID 1 for systemd.
set -u
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C LANG=C
dir=/run/stegra-boot
base=boot-runtime
IFS= read -r base < "$dir/log-base" || true
case "$base" in boot-[0-9]*|boot-runtime) ;; *) base=boot-runtime ;; esac
log="/boot/stegra-logs/$base-handoff.log"
if ! { exec 3>> "$log"; }; then exec 3>> "$dir/handoff.log"; fi
note() { printf '%s\n' "$*" >&3; printf '%s\n' "$*" >/dev/console 2>/dev/null || true; }
fail() {
    note "SYSTEMD_EXEC_FAILED: $*"
    printf '%s\n%s\n' -1 "$*" > "$dir/state.tmp"
    mv "$dir/state.tmp" "$dir/state"
    "$dir/boot-screen" --watch "$dir" 2>> "$dir/display.log" &
    sync
    while :; do sleep 60; done
}
note "AFTER_SWITCH_ROOT wrapper_pid=$$"
[ "$$" = 1 ] || { note 'REFUSED: wrapper is not PID 1'; exit 2; }
note "ROOT_KERNEL: $(uname -r)"
while IFS= read -r line; do
    case "$line" in *' / '*|*' /boot '*|*' /run '*) note "ROOT_MOUNT: $line" ;; esac
done < /proc/mounts
[ -x /usr/lib/systemd/systemd ] || fail 'Missing executable /usr/lib/systemd/systemd'
note 'BEFORE_SYSTEMD_EXEC path=/usr/lib/systemd/systemd'
shopt -s execfail
exec /usr/lib/systemd/systemd --show-status=yes --log-target=journal-or-kmsg
rc=$?
fail "exec systemd rc=$rc"
