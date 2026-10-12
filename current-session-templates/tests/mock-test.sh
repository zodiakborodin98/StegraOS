#!/usr/bin/env bash
# Local mock verification of the CPU-autostart package (no target).
# Contract coverage at the Gamescope exec boundary: parent-side Xwayland env
# (WLR_XWAYLAND/XWL_XWAYLAND_ELF/XWL_PRIVATE_LIBGBM_DIR), child env/argv/seed,
# integrity fail-closed paths, display-prep, unit contract, flat-upload
# installer (M7G_CPU_PACK_DIR) and rollback.
set -u
P=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P) || exit 1
W=$(mktemp -d "${TMPDIR:-/tmp}/cpu-autostart-test.XXXXXXXX") || exit 1
# Only this freshly created sandbox is disposable; never use a project path.
cleanup() {
    case "$W" in
        "${TMPDIR:-/tmp}"/cpu-autostart-test.*) rm -rf -- "$W" ;;
        *) echo "REFUSED unexpected sandbox path" >&2 ;;
    esac
}
trap cleanup EXIT
mkdir -p "$W/bin"
PASS=0; FAIL=0
ok(){ echo "PASS $1"; PASS=$((PASS+1)); }
no(){ echo "FAIL $1"; FAIL=$((FAIL+1)); }

# ---------- sandbox deploy with stubs ----------
D=$W/deploy; SEED=$W/seed
mkdir -p "$D/m7g-o180sx1" "$D/private-libgbm" "$D/mesa-a57-llvmpipe8-private/lib" \
         "$D/mesa-a57-depthfix2-private/lib" "$SEED/steamrtarm64"
cat > "$D/m7g-o180sx1/gamescope-m7g-o180sx1-b9f5847f" <<'EOS'
#!/bin/bash
echo "GS_ARGV $*"
echo "GS_PARENT_XWL WLR=${WLR_XWAYLAND-<unset>} ELF=${XWL_XWAYLAND_ELF-<unset>} LIBGBM=${XWL_PRIVATE_LIBGBM_DIR-<unset>}"
[ -x "${WLR_XWAYLAND:-/nonexistent}" ] || { echo "GS_FAIL: WLR_XWAYLAND not executable"; exit 66; }
echo "GS_PARENT_DISPLAY=${DISPLAY-<unset>} GS_PARENT_WD=${WAYLAND_DISPLAY-<unset>}"
export DISPLAY=:0 WAYLAND_DISPLAY=wayland-0   # emulate Gamescope publishing to the nested child
child=(); seen=0
for a in "$@"; do
    if [ "$seen" = 0 ] && [ "$a" = "--" ]; then seen=1; continue; fi
    [ "$seen" = 1 ] && child+=("$a")
done
exec "${child[@]}"
EOS
cat > "$SEED/steamrtarm64/steam" <<'EOS'
#!/bin/bash
echo "STEAM_ARGV $*"
echo "STEAM_ENV MESA=$GALLIUM_DRIVER SOFT=$LIBGL_ALWAYS_SOFTWARE GLX=$__GLX_VENDOR_LIBRARY_NAME CEF=${CEF_PATH-<unset>}"
echo "STEAM_ENV DISPLAY=${DISPLAY-<unset>} WD=${WAYLAND_DISPLAY-<unset>} MLDO=${MESA_LOADER_DRIVER_OVERRIDE-<unset>} GSWD=${GAMESCOPE_WAYLAND_DISPLAY-<unset>}"
echo "STEAM_ENV LD=$LD_LIBRARY_PATH"
exit 0
EOS
: > "$D/xwayland-eglstream-wrapper.sh"; : > "$D/Xwayland"
: > "$D/run-steam-gm-entry-steam-mesa-a57-llvmpipe8-stretch1.sh"
: > "$D/run-steam-gm-entry-steam-mesa-a57-llvmpipe8-stretch1-nobind.sh"
printf '#!/bin/sh\nexit 0\n' > "$D/deploy-preflight-check.sh"
: > "$D/deploy.sha256"
echo "STEAM_FLAGS='-steamdeck -gamepadui'" > "$D/steam-gm-flags.env"
echo "CEF_PATH=/sandbox/steamwebhelper.sh" >> "$D/steam-gm-flags.env"
( cd "$D" && sha256sum steam-gm-flags.env > package.sha256 )
chmod 755 "$D/m7g-o180sx1/gamescope-m7g-o180sx1-b9f5847f" "$SEED/steamrtarm64/steam" \
          "$D/xwayland-eglstream-wrapper.sh" "$D/Xwayland" "$D/deploy-preflight-check.sh"

run() { M7G_CPU_DEPLOY="$D" STEAM_GM_ROOT="$SEED" "$@"; }

echo "== launcher dry-run: exact geometry argv =="
OUT=$(M7G_CPU_DEPLOY="$D" STEAM_GM_ROOT="$SEED" M7G_CPU_DRY_RUN=1 bash "$P/launch-session.sh" 2>&1)
echo "$OUT" | grep -q -- '--backend tegra-l4t --expose-wayland -w 1280 -h 720 -W 720 -H 1280 -r 60 --scaler stretch --' && ok argv-geometry || no argv-geometry
echo "$OUT" | grep -q 'mesa-a57-depthfix2-private/lib:.*mesa-a57-llvmpipe8-private/lib' && ok argv-private-ld || no argv-private-ld

echo "== launcher real exec chain: Gamescope boundary + child env ==="
OUT2=$(env -u DISPLAY -u WAYLAND_DISPLAY M7G_CPU_DEPLOY="$D" STEAM_GM_ROOT="$SEED" bash "$P/launch-session.sh" 2>&1)
echo "$OUT2" | grep -q "GS_PARENT_XWL WLR=$D/xwayland-eglstream-wrapper.sh ELF=$D/Xwayland LIBGBM=$D/private-libgbm" && ok gs-boundary-xwayland-env || no gs-boundary-xwayland-env
[ -x "$D/xwayland-eglstream-wrapper.sh" ] && [ -x "$D/Xwayland" ] && [ -d "$D/private-libgbm" ] && ok xwayland-files-exist || no xwayland-files-exist
if ! echo "$OUT2" | grep -q '/root/steamos-switch-alpha2'; then ok no-buildtime-path; else no no-buildtime-path; fi
echo "$OUT2" | grep -q 'GS_PARENT_DISPLAY=<unset> GS_PARENT_WD=<unset>' && ok parent-display-unset || no parent-display-unset
echo "$OUT2" | grep -q 'STEAM_ENV MESA=llvmpipe SOFT=1 GLX=mesa CEF=/sandbox/steamwebhelper.sh' && ok child-env-llvmpipe-cef || no child-env-llvmpipe-cef
echo "$OUT2" | grep -q 'STEAM_ENV DISPLAY=:0 WD=wayland-0 MLDO=<unset> GSWD=<unset>' && ok child-env-compositor-and-unset || no child-env-compositor-and-unset
echo "$OUT2" | grep -q "STEAM_ENV LD=$D/mesa-a57-depthfix2-private/lib:$D/mesa-a57-llvmpipe8-private/lib" && ok child-env-ld-order || no child-env-ld-order
echo "$OUT2" | grep -q 'STEAM_ARGV .*-steamdeck -gamepadui' && ok child-steam-flags || no child-steam-flags

echo "== fail-closed setup paths =="
mv "$D/m7g-o180sx1/gamescope-m7g-o180sx1-b9f5847f" "$W/gs.bak"
run env M7G_CPU_DRY_RUN=1 bash "$P/launch-session.sh" >/dev/null 2>&1; [ $? = 10 ] && ok fail-gamescope-rc10 || no fail-gamescope-rc10
mv "$W/gs.bak" "$D/m7g-o180sx1/gamescope-m7g-o180sx1-b9f5847f"
mv "$D/xwayland-eglstream-wrapper.sh" "$W/xwl.bak"
run env M7G_CPU_DRY_RUN=1 bash "$P/launch-session.sh" >/dev/null 2>&1; [ $? = 10 ] && ok fail-wrapper-rc10 || no fail-wrapper-rc10
mv "$W/xwl.bak" "$D/xwayland-eglstream-wrapper.sh"
mv "$D/Xwayland" "$W/xwlelf.bak"
run env M7G_CPU_DRY_RUN=1 bash "$P/launch-session.sh" >/dev/null 2>&1; [ $? = 10 ] && ok fail-xwayland-elf-rc10 || no fail-xwayland-elf-rc10
mv "$W/xwlelf.bak" "$D/Xwayland"
cp "$D/package.sha256" "$W/package.bak"
echo "0000000000000000000000000000000000000000000000000000000000000000  steam-gm-flags.env" > "$D/package.sha256"
run env M7G_CPU_DRY_RUN=1 bash "$P/launch-session.sh" >/dev/null 2>&1; [ $? = 11 ] && ok fail-package-integrity-rc11 || no fail-package-integrity-rc11
cp "$W/package.bak" "$D/package.sha256"
mv "$D/deploy.sha256" "$W/deploy.bak"
run env M7G_CPU_DRY_RUN=1 bash "$P/launch-session.sh" >/dev/null 2>&1; [ $? = 11 ] && ok fail-deploy-manifest-rc11 || no fail-deploy-manifest-rc11
mv "$W/deploy.bak" "$D/deploy.sha256"
mv "$D/deploy-preflight-check.sh" "$W/preflight.bak"
run env M7G_CPU_DRY_RUN=1 bash "$P/launch-session.sh" >/dev/null 2>&1; [ $? = 12 ] && ok fail-preflight-helper-rc12 || no fail-preflight-helper-rc12
mv "$W/preflight.bak" "$D/deploy-preflight-check.sh"

echo "== display-prep on/off in sandbox sysfs =="
mkdir -p "$W/sys/class/graphics/fb0" "$W/sys/class/backlight/backlight"
echo 4 > "$W/sys/class/graphics/fb0/blank"; echo 4 > "$W/sys/class/backlight/backlight/bl_power"
M7G_CPU_SYSFS_ROOT="$W" sh "$P/display-prep.sh" on >/dev/null 2>&1
[ "$(cat "$W/sys/class/backlight/backlight/bl_power")" = 0 ] && [ "$(cat "$W/sys/class/graphics/fb0/blank")" = 0 ] && ok display-prep-on || no display-prep-on
M7G_CPU_SYSFS_ROOT="$W" sh "$P/display-prep.sh" off >/dev/null 2>&1
[ "$(cat "$W/sys/class/backlight/backlight/bl_power")" = 4 ] && [ "$(cat "$W/sys/class/graphics/fb0/blank")" = 4 ] && ok display-prep-off || no display-prep-off

echo "== unit contract (parent NVIDIA env, root display-prep, runtime dir) =="
U="$P/steam-cpu-session.service"
grep -q 'Environment=VK_ICD_FILENAMES=/etc/vulkan/icd.d/nvidia_icd.json' "$U" \
 && grep -q 'Environment=VK_DRIVER_FILES=/etc/vulkan/icd.d/nvidia_icd.json' "$U" \
 && grep -q 'Environment="VK_LOADER_LAYERS_DISABLE=\*"' "$U" \
 && grep -q 'Environment=GAMESCOPE_WSI_FORCE_BYPASS=1' "$U" \
 && ok unit-parent-env || no unit-parent-env
grep -q 'UnsetEnvironment=DISPLAY WAYLAND_DISPLAY LD_PRELOAD' "$U" \
 && grep -q 'ExecStartPre=+/usr/local/lib/steam-cpu/display-prep.sh on' "$U" \
 && grep -q 'ExecStopPost=+/usr/local/lib/steam-cpu/display-prep.sh off' "$U" \
 && grep -q 'RuntimeDirectory=steam-cpu-session' "$U" \
 && ok unit-display-runtime-contract || no unit-display-runtime-contract

# ---------- installer/rollback with systemctl shim, flat upload ----------
RT=$W/root; mkdir -p "$RT/etc/systemd/system" "$RT/etc/sddm.conf.d" "$RT/var/lib/sddm"
ln -s /usr/lib/systemd/system/sddm.service "$RT/etc/systemd/system/display-manager.service"
echo "[Autologin]" > "$RT/etc/sddm.conf.d/zz-switchdeck.conf"
cat > "$W/bin/systemctl" <<'EOS'
#!/bin/bash
RT="${M7G_CPU_TEST_ROOT:?}"; cmd="${1:-}"; shift || true
case "$cmd" in
  get-default) echo graphical.target ;;
  is-enabled) echo enabled ;;
  disable) for u in "$@"; do rm -f "$RT/etc/systemd/system/$u" "$RT/etc/systemd/system/display-manager.service"; done ;;
  enable) mkdir -p "$RT/etc/systemd/system/graphical.target.wants"; for u in "$@"; do ln -sfn "/usr/lib/systemd/system/$u" "$RT/etc/systemd/system/graphical.target.wants/$u"; done ;;
  daemon-reload) : ;;
  *) : ;;
esac
exit 0
EOS
chmod 755 "$W/bin/systemctl"
mkdir -p "$W/flat" "$W/stream"
cp "$P/steam-cpu-session.service" "$P/launch-session.sh" "$P/display-prep.sh" "$W/flat/"
cp "$P/install-steam-cpu-autostart.sh" "$W/stream/"

echo "== install from flat upload (M7G_CPU_PACK_DIR; installer streamed elsewhere) =="
M7G_CPU_TEST_ROOT="$RT" M7G_CPU_PACK_DIR="$W/flat" SYSTEMCTL="$W/bin/systemctl" \
    M7G_CPU_DEPLOY="$D" M7G_CPU_SEED="$SEED" M7G_CPU_SKIP_PINS=1 \
    bash "$W/stream/install-steam-cpu-autostart.sh" > "$W/install.out" 2>&1
RC=$?
[ $RC = 0 ] && ok install-rc || { no install-rc; tail -5 "$W/install.out"; }
grep -q INSTALL_DONE "$W/install.out" && ok install-done || no install-done
[ -f "$RT/etc/systemd/system/steam-cpu-session.service" ] && ok unit-installed || no unit-installed
[ -x "$RT/usr/local/lib/steam-cpu/launch-session.sh" ] && [ -x "$RT/usr/local/lib/steam-cpu/display-prep.sh" ] && ok lib-installed || no lib-installed
[ ! -e "$RT/etc/systemd/system/display-manager.service" ] && ok dm-alias-removed || no dm-alias-removed
[ -L "$RT/etc/systemd/system/graphical.target.wants/steam-cpu-session.service" ] && ok session-enabled || no session-enabled
[ -n "$(ls -d "$RT"/var/lib/steam-cpu-autostart/backup-* 2>/dev/null)" ] && ok backup-created || no backup-created

echo "== rollback (mock root) =="
M7G_CPU_TEST_ROOT="$RT" SYSTEMCTL="$W/bin/systemctl" bash "$P/rollback-steam-cpu-autostart.sh" > "$W/rollback.out" 2>&1
RC=$?
[ $RC = 0 ] && ok rollback-rc || { no rollback-rc; tail -5 "$W/rollback.out"; }
grep -q ROLLBACK_DONE "$W/rollback.out" && ok rollback-done || no rollback-done
[ ! -e "$RT/etc/systemd/system/steam-cpu-session.service" ] && ok rollback-unit-removed || no rollback-unit-removed
[ ! -d "$RT/usr/local/lib/steam-cpu" ] && ok rollback-lib-removed || no rollback-lib-removed
[ -L "$RT/etc/systemd/system/display-manager.service" ] && [ "$(readlink "$RT/etc/systemd/system/display-manager.service")" = /usr/lib/systemd/system/sddm.service ] && ok rollback-dm-restored || no rollback-dm-restored

echo "TESTS pass=$PASS fail=$FAIL"
[ "$FAIL" = 0 ] && echo CPU_AUTOSTART_TESTS_OK || echo CPU_AUTOSTART_TESTS_FAIL
test "$FAIL" = 0
