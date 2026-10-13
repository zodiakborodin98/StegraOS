#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# CPU Steam persistent session launcher (stretch1 d7114ae functional chain).
#
# This is the accepted llvmpipe8+depthfix2 Steam route WITHOUT test scaffolding:
# no thin, no handoff, no samplers, no identity observer, no trace preload, no
# experiment duration/teardown.  The parent launch environment that the accepted
# wrapper supplied (ICD selection, loader-layer isolation, WSI force-bypass,
# HOME) is provided by the unit; the child env/argv/seed are reproduced verbatim
# from the accepted stretch1 entry (92a68fac...).
#
# Production: ExecStart of steam-cpu-session.service as User=steamos.
# Test-only overrides (never set in production): M7G_CPU_DEPLOY,
# M7G_CPU_DRY_RUN=1 (print final argv instead of exec).
set -eu
# Persistent 480p Zink: no test runner/timer or frame dumps.
[ -d '/run/steam-zink-baseline-480p' ] || { echo ZINK_RUNTIME_DIRECTORY_MISSING; exit 13; }
umask 077
python3 - <<'BASELINE_INIT'
from pathlib import Path
import secrets
r=Path('/run/steam-zink-baseline-480p')
(r/'token').write_text(secrets.token_hex(16));(r/'token').chmod(0o600)
(r/'early.log').write_text('');(r/'early.log').chmod(0o600)
BASELINE_INIT

DEPLOY="${M7G_CPU_DEPLOY:-/opt/steam-tegra/deploy}"
STEAMROOT="${STEAM_GM_ROOT:-/opt/steam-tegra/steam}"
GS_BIN="/opt/steam-tegra/gamescope"
[ -f "$GS_BIN" ] && [ ! -L "$GS_BIN" ] || { echo "M9N_CANDIDATE_NOT_REGULAR"; exit 14; }
printf '%s  %s\n' "${GAMESCOPE_TEGRA_SHA256:?Set the hash of your qualified compositor build}" "$GS_BIN" | sha256sum -c - || { echo "M9N_CANDIDATE_HASH_FAIL"; exit 14; }
export TEGRA_L4T_DXVK_YFLIP_APPID='*'
echo 'M9N_ORIENTATION_CLASS=DXVK_FROG_NONZERO_APPID' 
STEAM_BIN="$STEAMROOT/steamrtarm64/steam"

# --- parent-side internal-Xwayland contract (accepted entry lines 153-156) --
# These MUST be in the Gamescope process environment: wlroots execs
# WLR_XWAYLAND for the internal Xwayland, and the wrapper consumes
# XWL_XWAYLAND_ELF / XWL_PRIVATE_LIBGBM_DIR.  They are parent-side, not
# child-only (the Steam child inherits them unchanged, as in the accepted run).
WLR_XWAYLAND="$DEPLOY/xwayland-eglstream-wrapper.sh"
XWL_XWAYLAND_ELF="$DEPLOY/m8t-o180sx1/Xwayland-m8t-4dd0577f"
XWL_PRIVATE_LIBGBM_DIR="$DEPLOY/private-libgbm"
export WLR_XWAYLAND XWL_XWAYLAND_ELF XWL_PRIVATE_LIBGBM_DIR
export XWL_SCREEN_SIZE_OVERRIDE=854x480

echo "CPU_SESSION_START utc=$(date -u +%Y-%m-%dT%H:%M:%SZ) uid=$(id -u) deploy=$DEPLOY seed=$STEAMROOT"
echo "XWL_CONTRACT WLR_XWAYLAND=$WLR_XWAYLAND XWL_XWAYLAND_ELF=$XWL_XWAYLAND_ELF XWL_PRIVATE_LIBGBM_DIR=$XWL_PRIVATE_LIBGBM_DIR"

case "$DEPLOY" in /*) ;; *) echo "SETUP_FAIL: deploy not absolute"; exit 10 ;; esac

# --- Steam seed/flags (authoritative; same source/fallback as accepted entry)
if [ -f "$DEPLOY/steam-gm-flags.env" ]; then
    . "$DEPLOY/steam-gm-flags.env"
fi
if [ -z "${STEAM_FLAGS:-}" ]; then
    STEAM_FLAGS="-vrskip -vrdisable -fasthtml -noverifyfiles -nocrashmonitor -no-cef-sandbox -cef-disable-sandbox -cef-single-process -cef-in-process-gpu -cef-disable-breakpad -cef-disable-js-logging -cef-disable-seccomp-sandbox -720p -steampal -gamepadui -steamdeck"
    CEF_PATH="$STEAMROOT/steamrtarm64/steamwebhelper.sh"
    _rtarm_root="$STEAMROOT/steamrtarm64/pv-runtime/steam-runtime-steamrt-arm64"
    _rtarm="$(ls -d "$_rtarm_root"/steamrt3c_platform_*/files 2>/dev/null | head -n1)"
    _rtx64_root="$STEAMROOT/steamrt64/pv-runtime/steam-runtime-steamrt"
    _rtx64="$(ls -d "$_rtx64_root"/steamrt3c_platform_*/files 2>/dev/null | head -n1)"
    LD_LIBRARY_PATH="$STEAMROOT/steamrtarm64${_rtarm:+:$_rtarm/lib/aarch64-linux-gnu:$_rtarm/lib}:$STEAMROOT/steamrt64${_rtx64:+:$_rtx64/lib/x86_64-linux-gnu:$_rtx64/lib}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export __GL_THREADED_OPTIMIZATIONS=1
fi
export STEAM_FLAGS CEF_PATH LD_LIBRARY_PATH
CEF_PATH="/opt/steam-tegra/zink-480p/cef-zink.sh"
export CEF_PATH

# --- M8i: browser composer selection (System default; rollback lever below)
# Rollback to the previous OpenGL/default composer:  touch /usr/local/lib/steam-cpu/composer-opengl
# Re-enable the System composer:                     rm -f /usr/local/lib/steam-cpu/composer-opengl
M8I_COMPOSER_ARGS="-composer-mode=2"

# --- private runtime dir (unit provides XDG_RUNTIME_DIR; validate it)
if [ -n "${XDG_RUNTIME_DIR:-}" ] && [ -d "$XDG_RUNTIME_DIR" ] \
   && [ "$(stat -c %u "$XDG_RUNTIME_DIR")" = "$(id -u)" ] \
   && [ "$(stat -c %a "$XDG_RUNTIME_DIR")" = "700" ]; then
    :
else
    XDG_RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/steam-cpu-session.XXXXXX")" || exit 13
    chmod 700 "$XDG_RUNTIME_DIR" || exit 13
fi
export XDG_RUNTIME_DIR
echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR uid=$(id -u) mode=$(stat -c %a "$XDG_RUNTIME_DIR")"

cd "$DEPLOY"

# --- fail-closed setup checks (same set as accepted entry)
[ -x "$GS_BIN" ]  || { echo "SETUP_FAIL: Gamescope ELF missing/not executable"; exit 10; }
[ -x "$STEAM_BIN" ]  || { echo "SETUP_FAIL: Steam launcher missing/not executable"; exit 10; }
[ -x "$WLR_XWAYLAND" ] || { echo "SETUP_FAIL: Xwayland wrapper missing"; exit 10; }
[ -x "$XWL_XWAYLAND_ELF" ] || { echo "SETUP_FAIL: pinned Xwayland missing"; exit 10; }
[ -d "$XWL_PRIVATE_LIBGBM_DIR" ] || { echo "SETUP_FAIL: private-libgbm dir missing"; exit 10; }
[ -d "$DEPLOY/mesa-a57-llvmpipe8-private/lib" ] || { echo "SETUP_FAIL: private mesa libdir missing"; exit 10; }
[ -d "$DEPLOY/mesa-a57-depthfix2-private/lib" ] || { echo "SETUP_FAIL: depthfix2 mesa libdir missing"; exit 10; }
case "$WLR_XWAYLAND$XWL_XWAYLAND_ELF$XWL_PRIVATE_LIBGBM_DIR" in
    *"/root/steamos-switch-alpha2"*) echo "SETUP_FAIL: build-time Xwayland path leaked"; exit 10 ;;
esac
[ -f "$DEPLOY/deploy.sha256" ] || { echo "SETUP_FAIL: deploy manifest missing"; exit 11; }
sha256sum -c package.sha256 || { echo "SETUP_FAIL: package integrity failed"; exit 11; }
[ -x "$DEPLOY/deploy-preflight-check.sh" ] || { echo "SETUP_FAIL: preflight helper missing"; exit 12; }
"$DEPLOY/deploy-preflight-check.sh" "$DEPLOY/deploy.sha256" || { echo "SETUP_FAIL: preflight failed"; exit 12; }

# --- M9R full Steam/CEF Zink sync capture (parent compositor unchanged)
M9R_ZINK_LIBS=""
IFS=: read -r -a M9R_PATHS <<< "${LD_LIBRARY_PATH:-}"
for M9R_PATH in "${M9R_PATHS[@]}"; do
 case "$M9R_PATH" in
  /opt/steam-tegra/deploy/mesa-a57-depthfix2-private/lib|/opt/steam-tegra/deploy/mesa-a57-llvmpipe8-private/lib) ;;
  *) M9R_ZINK_LIBS="${M9R_ZINK_LIBS:+$M9R_ZINK_LIBS:}$M9R_PATH" ;;
 esac
done
STEAM_FLAGS="${STEAM_FLAGS//-cef-single-process/}"
STEAM_FLAGS="${STEAM_FLAGS//-cef-in-process-gpu/}"
STEAM_FLAGS="$STEAM_FLAGS -cef-force-gpu"
set -- "$GS_BIN" --backend tegra-l4t --expose-wayland \
      -w 1280 -h 720 -W 720 -H 1280 -r 60 --scaler stretch -- \
      /usr/bin/env -u GAMESCOPE_WAYLAND_DISPLAY -u LIBGL_ALWAYS_INDIRECT -u LIBGL_ALWAYS_SOFTWARE -u LIBGL_KOPPER_DISABLE -u ZINK_DEBUG \
      __GLX_VENDOR_LIBRARY_NAME=mesa MESA_LOADER_DRIVER_OVERRIDE=zink GALLIUM_DRIVER=zink M9R_TFP_READBACK=1 M9R_CEF_CHILD_UPLOAD=1 M9R_IMAGE_FORCE_DEDICATED=1 LIBGL_DRIVERS_PATH=/opt/steam-tegra/zink-480p/provider/lib LIBGL_KOPPER_DRI2=1 M7G_T3_ADAPTER=1 \
      "LD_LIBRARY_PATH=/opt/steam-tegra/zink-480p/provider/lib${M9R_ZINK_LIBS:+:$M9R_ZINK_LIBS}" \
      "LD_PRELOAD=/opt/steam-tegra/zink-480p/libsteam-zink-forkexec.so" \
      "STEAM_ZINK_TOKEN=$(cat /run/steam-zink-baseline-480p/token)" STEAM_ZINK_EXEC_TOPOLOGY=direct-v1 \
      "STEAM_ZINK_STEAM_EXE=$STEAM_BIN" STEAM_ZINK_CEF_EXE=/opt/steam-tegra/zink-480p/cef/steamwebhelper \
      "STEAM_ZINK_CEF_ALT_EXE=$STEAMROOT/steamrtarm64/steamwebhelper" STEAM_ZINK_BOOT_LOG=/dev/null \
      M7G_ZINK_T3_WRAPPER_RUN_ID=steam-zink-baseline-480p \
      "$STEAM_BIN" $STEAM_FLAGS $M8I_COMPOSER_ARGS

if [ "${M7G_CPU_DRY_RUN:-0}" = "1" ]; then
    printf 'EXEC'
    for a in "$@"; do printf ' %q' "$a"; done
    printf '\n'
    exit 0
fi
exec "$@"
