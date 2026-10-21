#!/bin/bash
# SPDX-License-Identifier: MIT
# Sourced by PID 1 after /dev, /proc, /sys and /run are available.
progress_dir=/run/stegra-boot
progress_persistent=''
progress_pid=''
progress_log_base='boot-runtime'
progress_start() {
    mkdir -p "$progress_dir"
    progress_stage 1 'WAITING FOR THE SD BOOT PARTITION'
    bb chvt 1 2>/dev/null || true
    /bin/stegra-boot-screen --watch "$progress_dir" 2>"$progress_dir/display.log" &
    progress_pid=$!
}
progress_record() {
    if [ -n "$progress_persistent" ]; then
        /bin/stegra-boot-screen --record "$progress_dir/boot.log" "$progress_persistent"
    else
        /bin/stegra-boot-screen --record "$progress_dir/boot.log"
    fi
}
progress_note() { printf '%s\n' "StegraOS: $*" | progress_record; }
progress_stage() {
    printf '%s\n%s\n' "$1" "$2" > "$progress_dir/state.tmp"
    bb mv "$progress_dir/state.tmp" "$progress_dir/state"
    progress_note "$2"
}
progress_attach_log() {
    local dir="$1/stegra-logs" n=1
    if mkdir -p "$dir"; then
        while [ -e "$dir/boot-$n.log" ]; do n=$((n+1)); done
        progress_persistent="$dir/boot-$n.log"
        progress_log_base="boot-$n"
        if ! (set -C; : > "$progress_persistent"); then progress_persistent=''; fi
    fi
    if [ -n "$progress_persistent" ]; then
        cat "$progress_dir/boot.log" > "$progress_persistent" || progress_persistent=''
        progress_note "EARLY BOOT LOG: stegra-logs/boot-$n.log"
    else
        progress_note 'WARNING: SD LOG UNAVAILABLE; LOG IS IN /run/stegra-boot'
    fi
}
progress_run() {
    "$@" 2>&1 | progress_record
    local results=("${PIPESTATUS[@]}")
    if [ "${results[1]}" -ne 0 ]; then
        progress_note "WARNING: LOG CAPTURE RC=${results[1]}"
    fi
    return "${results[0]}"
}
progress_error() {
    progress_stage -1 "$*"
    progress_note 'SETUP STOPPED. NO AUTOMATIC REPAIR OR REBOOT.'
    bb sync
}
progress_prepare_handoff() {
    # All additions are in tmpfs /run, never in the ext4 payload.
    # The wrapper is executed by stock switch_root as PID 1 in the real root.
    local unitdir=/run/systemd/system
    mkdir -p "$unitdir/sysinit.target.wants" || return 1
    for name in boot-screen init-systemd.sh capture-systemd.sh; do
        case "$name" in
            boot-screen) cp /bin/stegra-boot-screen "$progress_dir/$name" ;;
            *) cp "/$name" "$progress_dir/$name" ;;
        esac || return 1
        chmod 755 "$progress_dir/$name" || return 1
    done
    printf '%s\n' "$progress_log_base" > "$progress_dir/log-base" || return 1
    cp /stegra-boot-capture.service "$unitdir/stegra-boot-capture.service" || return 1
    bb ln -s ../stegra-boot-capture.service "$unitdir/sysinit.target.wants/stegra-boot-capture.service" || return 1
    progress_note 'POST-HANDOFF CAPTURE PREPARED IN /run (20 MIN LOGGING ONLY)'
}
progress_finish() {
    progress_note 'INITRAMFS PREPARATION COMPLETE; BEGINNING FINAL MOUNT MOVES'
    if [ -n "$progress_pid" ]; then
        kill "$progress_pid" 2>/dev/null || true
        wait "$progress_pid" 2>/dev/null || true
        progress_pid=''
    fi
}
