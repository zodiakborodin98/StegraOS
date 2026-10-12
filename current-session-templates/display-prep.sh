#!/bin/sh
# CPU Steam autostart — bounded display ready/restore helper (root via unit +).
# Contract extracted from the accepted visual300 wrapper:
#   on : fb0/blank=0 (bounded write; this Tegra target does NOT read it back)
#        + bl_power=0 with readback proof
#   off: bl_power=4 with readback proof + fb0/blank=4 (write-only on this target)
# Test-only override: M7G_CPU_SYSFS_ROOT (never set in production).
set -u
ROOT="${M7G_CPU_SYSFS_ROOT:-}"
FB="${ROOT}/sys/class/graphics/fb0/blank"
BL="${ROOT}/sys/class/backlight/backlight/bl_power"
mode="${1:-}"
write_bounded() { /usr/bin/timeout -k 2 5 sh -c 'printf "%s" "$2" > "$1"' sysfs_write "$1" "$2"; }
readback() { [ "$(/usr/bin/timeout -k 2 5 cat "$1" 2>/dev/null)" = "$2" ]; }
case "$mode" in
    on)
        [ -w "$FB" ] || { echo "DISPLAY_PREP_FAIL: fb0 blank not writable"; exit 1; }
        [ -w "$BL" ] || { echo "DISPLAY_PREP_FAIL: bl_power not writable"; exit 1; }
        write_bounded "$FB" 0 || { echo "DISPLAY_PREP_FAIL: fb0 unblank"; exit 1; }
        write_bounded "$BL" 0 || { echo "DISPLAY_PREP_FAIL: backlight on write"; exit 1; }
        readback "$BL" 0 || { echo "DISPLAY_PREP_FAIL: backlight on readback"; exit 1; }
        echo DISPLAY_PREP_OK on
        ;;
    off)
        [ -w "$BL" ] || { echo "DISPLAY_PREP_FAIL: bl_power not writable"; exit 1; }
        [ -w "$FB" ] || { echo "DISPLAY_PREP_FAIL: fb0 blank not writable"; exit 1; }
        write_bounded "$BL" 4 || { echo "DISPLAY_PREP_FAIL: backlight off write"; exit 1; }
        readback "$BL" 4 || { echo "DISPLAY_PREP_FAIL: backlight off readback"; exit 1; }
        write_bounded "$FB" 4 || { echo "DISPLAY_PREP_FAIL: fb0 blank"; exit 1; }
        echo DISPLAY_PREP_OK off
        ;;
    *)
        echo "usage: $0 on|off" >&2
        exit 2
        ;;
esac
