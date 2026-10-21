# Included at the end of init, after ldconfig. Still PID 1.
progress_prepare_handoff || stop 'Cannot prepare post-handoff diagnostics'
progress_stage 5 'HANDING OVER TO THE LINUX TEXT CONSOLE'
progress_finish
bb sync
# Pin both the FAT log and console before moving /run and /dev.
if [ -n "$progress_persistent" ]; then
    exec 3>> "$progress_persistent" || stop 'Cannot open final handoff log'
else
    exec 3>> "$progress_dir/handoff.log" || stop 'Cannot open runtime handoff log'
fi
exec 4>/dev/console || exec 4>&3
exec 1>&3 2>&1
handoff_note() { printf '%s\n' "$*" >&3; printf '%s\n' "$*" >&4; }
handoff_stop() {
    handoff_note "HANDOFF FAILED: $*"
    /bin/busybox sync
    while :; do /bin/busybox sleep 60; done
}
for tree in run dev sys proc; do
    handoff_note "BEFORE_MOVE_$tree"
    if /bin/mount --move "/$tree" "/newroot/$tree"; then
        handoff_note "AFTER_MOVE_$tree rc=0"
    else
        move_rc=$?
        handoff_stop "mount --move /$tree rc=$move_rc"
    fi
done
handoff_note 'BEFORE_SWITCH_ROOT target=/run/stegra-boot/init-systemd.sh'
shopt -s execfail
exec /usr/sbin/switch_root /newroot /run/stegra-boot/init-systemd.sh
switch_rc=$?
handoff_stop "Cannot exec switch_root rc=$switch_rc"
