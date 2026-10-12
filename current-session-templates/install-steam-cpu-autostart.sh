#!/bin/bash
# CPU Steam autostart — additive persistent-session installer (root).
#
# Installs steam-cpu-session.service + private launcher and selects it as the
# graphical startup: disables the SDDM/display-manager autologin path (Alpha 1)
# after backing up the exact state that changes.  It does NOT start the session
# and does NOT reboot.  All Alpha files are left in place (recoverable).
#
# Test-only overrides (never set in production): M7G_CPU_TEST_ROOT (path prefix),
# SYSTEMCTL (command), M7G_CPU_ALLOW_NONROOT=1 (sandbox tests only).
set -eu

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
PACK_DIR="${M7G_CPU_PACK_DIR:-$SELF_DIR}"
ROOT="${M7G_CPU_TEST_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
ETC="$ROOT/etc/systemd/system"
LIB="$ROOT/usr/local/lib/steam-cpu"
VARLIB="$ROOT/var/lib/steam-cpu-autostart"
D="${M7G_CPU_DEPLOY:-/home/steamos/steamos-switch-dev/steam-gm1}"
SEED="${M7G_CPU_SEED:-/run/initramfs/mounts/data/steam-gm/m7g-seed-1/steam}"
ENTRY_SHA=92a68facaa3ee8105d88126567f5dd6d6dff48ae9025045d2ec13a2dba93317e
THIN_SHA=2e499917dc19bad3f062155e7ff9102c8dc39705ad7ba1176f96a3061e2ddd34
GS_SHA=b9f5847fe6378204126ba4beaa1b3c75b2b42831a4e1b91ebb3370d23723d3d8

fail(){ echo "INSTALL_FAIL: $*" >&2; exit 1; }
sha(){ sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }

[ "$(id -u)" = 0 ] || [ "${M7G_CPU_ALLOW_NONROOT:-0}" = 1 ] || fail "must run as root"

echo "== preconditions =="
SKIP_PINS="${M7G_CPU_SKIP_PINS:-0}"
[ -f "$D/run-steam-gm-entry-steam-mesa-a57-llvmpipe8-stretch1.sh" ] || fail "stretch1 entry missing"
[ -f "$D/run-steam-gm-entry-steam-mesa-a57-llvmpipe8-stretch1-nobind.sh" ] || fail "stretch1 thin missing"
[ -f "$D/m7g-o180sx1/gamescope-m7g-o180sx1-b9f5847f" ] || fail "accepted Gamescope ELF missing"
if [ "$SKIP_PINS" = 1 ]; then
    echo "TEST_ONLY: M7G_CPU_SKIP_PINS=1 (sha checks skipped)"
else
    [ "$(sha "$D/run-steam-gm-entry-steam-mesa-a57-llvmpipe8-stretch1.sh")" = "$ENTRY_SHA" ] || fail "stretch1 entry sha mismatch"
    [ "$(sha "$D/run-steam-gm-entry-steam-mesa-a57-llvmpipe8-stretch1-nobind.sh")" = "$THIN_SHA" ] || fail "stretch1 thin sha mismatch"
    [ "$(sha "$D/m7g-o180sx1/gamescope-m7g-o180sx1-b9f5847f")" = "$GS_SHA" ] || fail "accepted Gamescope ELF sha mismatch"
fi
[ -d "$D/mesa-a57-llvmpipe8-private/lib" ] || fail "llvmpipe8 private libdir missing"
[ -d "$D/mesa-a57-depthfix2-private/lib" ] || fail "depthfix2 private libdir missing"
[ -d "$SEED" ] || fail "Steam seed not accessible ($SEED)"
for f in steam-cpu-session.service launch-session.sh display-prep.sh; do
    [ -f "$PACK_DIR/$f" ] || fail "package file missing: $PACK_DIR/$f"
done
[ ! -e "$ETC/steam-cpu-session.service" ] || fail "steam-cpu-session.service already installed (use rollback first)"

echo "== backup of state that changes =="
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BK="$VARLIB/backup-$TS"
mkdir -p "$BK"
"$SYSTEMCTL" get-default > "$BK/default-target.txt" 2>/dev/null || echo unknown > "$BK/default-target.txt"
"$SYSTEMCTL" is-enabled sddm.service > "$BK/sddm-enabled.txt" 2>/dev/null || echo unknown > "$BK/sddm-enabled.txt"
if [ -L "$ETC/display-manager.service" ]; then
    readlink "$ETC/display-manager.service" > "$BK/display-manager.target.txt"
fi
[ -f "$ROOT/etc/sddm.conf.d/zz-switchdeck.conf" ] && cp -a "$ROOT/etc/sddm.conf.d/zz-switchdeck.conf" "$BK/" || true
[ -f "$ROOT/var/lib/sddm/state.conf" ] && cp -a "$ROOT/var/lib/sddm/state.conf" "$BK/" || true
ls -l "$ETC/graphical.target.wants" > "$BK/graphical-wants.txt" 2>/dev/null || true
cat > "$BK/restore-notes.txt" <<EOF
Alpha 1 startup state backup.
restore: run rollback-steam-cpu-autostart.sh (uses the newest backup dir),
then 'systemctl enable sddm.service' if sddm-enabled.txt says enabled.
EOF
echo "backup=$BK"

echo "== install files =="
mkdir -p "$ETC" "$LIB"
install -m 0644 "$PACK_DIR/steam-cpu-session.service" "$ETC/steam-cpu-session.service"
install -m 0755 "$PACK_DIR/launch-session.sh" "$LIB/launch-session.sh"
install -m 0755 "$PACK_DIR/display-prep.sh" "$LIB/display-prep.sh"
[ -f "$PACK_DIR/README.md" ] && install -m 0644 "$PACK_DIR/README.md" "$LIB/README.md" || true
"$SYSTEMCTL" daemon-reload

echo "== switch startup selection: disable display-manager autologin path =="
"$SYSTEMCTL" disable sddm.service >/dev/null 2>&1 || true
if [ -L "$ETC/display-manager.service" ]; then
    rm -f "$ETC/display-manager.service"
fi
[ -L "$ETC/display-manager.service" ] && fail "display-manager alias still present"
echo "display-manager disabled (Alpha autologin path off; files retained)"

echo "== enable persistent CPU session (no start) =="
"$SYSTEMCTL" enable steam-cpu-session.service
echo INSTALL_DONE backup="$BK"
echo "activation/start is a separate step; rollback: rollback-steam-cpu-autostart.sh"
