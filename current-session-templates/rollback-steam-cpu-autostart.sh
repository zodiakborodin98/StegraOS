#!/bin/bash
# CPU Steam autostart — rollback (root). Restores the exact state recorded by
# the installer: stops/disables the CPU session, removes its additive files,
# restores the display-manager alias and sddm enabled state.
# Test-only overrides: M7G_CPU_TEST_ROOT, SYSTEMCTL, M7G_CPU_ALLOW_NONROOT=1.
set -eu
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="${M7G_CPU_TEST_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
ETC="$ROOT/etc/systemd/system"
LIB="$ROOT/usr/local/lib/steam-cpu"
VARLIB="$ROOT/var/lib/steam-cpu-autostart"
DM_TARGET=/usr/lib/systemd/system/sddm.service

[ "$(id -u)" = 0 ] || [ "${M7G_CPU_ALLOW_NONROOT:-0}" = 1 ] || { echo "ROLLBACK_FAIL: must run as root" >&2; exit 1; }

BK="$(ls -d "$VARLIB"/backup-* 2>/dev/null | sort | tail -1 || true)"
[ -n "$BK" ] || { echo "ROLLBACK_FAIL: no backup dir under $VARLIB" >&2; exit 1; }
echo "rollback_using_backup=$BK"

echo "== stop/disable CPU session (exact unit only) =="
"$SYSTEMCTL" disable --now steam-cpu-session.service >/dev/null 2>&1 || "$SYSTEMCTL" disable steam-cpu-session.service >/dev/null 2>&1 || true

echo "== remove additive files =="
rm -f "$ETC/steam-cpu-session.service"
rm -rf "$LIB"

echo "== restore display-manager alias/state =="
if [ -f "$BK/display-manager.target.txt" ]; then
    target="$(cat "$BK/display-manager.target.txt")"
    mkdir -p "$ETC"
    ln -sfn "$target" "$ETC/display-manager.service"
fi
if [ "$(cat "$BK/sddm-enabled.txt" 2>/dev/null || echo unknown)" = "enabled" ]; then
    "$SYSTEMCTL" enable sddm.service >/dev/null 2>&1 || true
fi
"$SYSTEMCTL" daemon-reload
echo "restored: display-manager alias + sddm enabled state (NOT started)"
echo ROLLBACK_DONE backup="$BK"
