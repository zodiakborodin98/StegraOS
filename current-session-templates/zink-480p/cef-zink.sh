#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
set -eu
unset ZINK_DEBUG VK_INSTANCE_LAYERS VK_LAYER_PATH VK_LAYER_SETTINGS_PATH VK_LOADER_DEBUG M9R_NV_IO_TRACE_DIR M9R_T3_FRAME_DIR M9R_LAYOUT_TRACE M9R_IMAGE_MEMORY_TRACE STEAM_ZINK_GL_TRACE_EXE STEAM_ZINK_GL_TRACE_DIR STEAM_ZINK_GL_TRACE_ANGLE
P=/opt/steam-tegra/zink-480p
R=/run/steam-zink-baseline-480p
cd "/opt/steam-tegra/zink-480p/cef"
unset LIBGL_ALWAYS_SOFTWARE GALLIUM_DRIVER LIBGL_KOPPER_DISABLE LIBGL_ALWAYS_INDIRECT GAMESCOPE_WAYLAND_DISPLAY
ld=""
IFS=: read -r -a paths <<< "${LD_LIBRARY_PATH:-}"
for p in "${paths[@]}"; do
 case "$p" in
  /opt/steam-tegra/deploy/mesa-a57-depthfix2-private/lib|/opt/steam-tegra/deploy/mesa-a57-llvmpipe8-private/lib) ;;
  *) ld="${ld:+$ld:}$p" ;;
 esac
done
export LD_LIBRARY_PATH="$P/validation/lib:/opt/steam-tegra/zink-480p/provider/lib:$ld"
export LD_PRELOAD="/opt/steam-tegra/zink-480p/libsteam-zink-forkexec.so"
export __GLX_VENDOR_LIBRARY_NAME=mesa MESA_LOADER_DRIVER_OVERRIDE=zink LIBGL_KOPPER_DRI2=1 M7G_T3_ADAPTER=1
export GALLIUM_DRIVER=zink M9R_TFP_READBACK=1 M9R_CEF_CHILD_UPLOAD=1
unset M9R_NV_IO_TRACE_DIR
export STEAM_ZINK_GL_PAYLOAD=0
unset M9R_T3_FRAME_DIR
export LIBGL_DRIVERS_PATH=/opt/steam-tegra/zink-480p/provider/lib
export DISABLE_GAMESCOPE_WSI=1 M7G_ZINK_T3_WRAPPER_RUN_ID=m9r-zink-ui46
export STEAM_ZINK_TOKEN="$(cat "$R/token")" STEAM_ZINK_BOOT_LOG=/dev/null STEAM_ZINK_CEF_EXE="/opt/steam-tegra/zink-480p/cef/steamwebhelper"
export STEAM_ZINK_EXEC_TOPOLOGY=direct-v1 STEAM_ZINK_CEF_ALT_EXE=/opt/steam-tegra/steam/steamrtarm64/steamwebhelper
unset STEAM_ZINK_STEAM_EXE STEAM_ZINK_PROBE_EXE
export VK_LOADER_LAYERS_DISABLE='*'
unset VK_LOADER_DEBUG M9R_VK_ABORT_ON_ERROR
unset M9R_LAYOUT_TRACE
export M9R_ZS_RP_LAYOUT_FIX=1
unset M9R_IMAGE_MEMORY_TRACE
export M9R_IMAGE_FORCE_DEDICATED=1
args=()
for a in "$@"; do
 case "$a" in --enable-gpu-debugging|--enable-gpu-command-logging|--enable-gpu-service-logging|--enable-logging|--v=*|--vmodule=*|--log-file=*) ;; -composer-mode=*|--composer-mode=*) args+=(-composer-mode=2) ;; *) args+=("$a") ;; esac
done
exec taskset 0x7e "/opt/steam-tegra/zink-480p/cef/steamwebhelper" --use-gl=angle --use-angle=gl "${args[@]}" -composer-mode=2 --no-zygote --log-file=/dev/null >/dev/null 2>&1
