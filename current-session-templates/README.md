# CPU Steam autostart package (persistent Gamescope + Steam)

Additive replacement of the Alpha 1 Xorg/Openbox/Zenity startup with the latest
demonstrated CPU Steam route (stretch1 d7114ae: llvmpipe8 + depthfix2, o180sx1,
`-w 1280 -h 720 -W 720 -H 1280 -r 60 --scaler stretch`).  No accepted artifact
is modified; all Alpha files remain in place and recoverable.

## Files

* `steam-cpu-session.service` — system service, `Type=exec`, `User=steamos`,
  `Slice=system.slice`, `KillMode=control-group`, `Restart=no` (no restart
  storm), parent environment reproduced from the accepted wrapper probe unit
  (`HOME`, nvidia `VK_ICD_FILENAMES`/`VK_DRIVER_FILES`, `VK_LOADER_LAYERS_DISABLE=*`,
  `DISABLE_MANGOHUD=1`, `GAMESCOPE_WSI_FORCE_BYPASS=1`, `DISABLE_FOSSILIZE=1`,
  `UnsetEnvironment=DISPLAY WAYLAND_DISPLAY LD_PRELOAD`), private
  `RuntimeDirectory=steam-cpu-session` (0700), and root display prep/restore via
  `ExecStartPre=+… on` / `ExecStopPost=+… off`.  Logs go to the journal
  (bounded by journald); no per-frame file grows on disk.
* `launch-session.sh` — the accepted stretch1 functional chain without test
  scaffolding (no thin/handoff/samplers/observer/tracer/duration).  Sources
  `steam-gm-flags.env`, validates seed/dirs, verifies `package.sha256` +
  deploy preflight when present, then executes Gamescope with the accepted
  geometry and the child-only private llvmpipe env.
* `display-prep.sh` — accepted bounded sysfs contract: on = fb0/blank=0 +
  bl_power=0 (readback); off = bl_power=4 (readback) + fb0/blank=4.
* `install-steam-cpu-autostart.sh` — root installer: pin checks, timestamped
  backup of every state it changes, installs files, disables the
  SDDM/display-manager autologin path, enables the new unit.  No start.
* `rollback-steam-cpu-autostart.sh` — restores the backed-up display-manager
  alias/sddm state, removes the additive files, disables the new unit.
* `tests/mock-test.sh` — local-only mock verification (21 checks).

## Audit summary (live, 2026-10-05, read-only)

* `graphical.target` → `display-manager.service` alias → `sddm.service`
  (enabled; currently inactive/dead).  `/etc/sddm.conf.d/zz-switchdeck.conf`:
  X11/Xorg, autologin `User=steamos Session=switchdeck.desktop Relogin=false`.
* `switchdeck.desktop` → `/usr/local/bin/switchdeck-session` (Openbox + nvidia
  env) → `/usr/local/bin/switchdeck-steam` then `switchdeck-menu` (Zenity loop).
  `switchdeck-hardware.service` runs before display-manager.
* No boot-time recreation of `/run/steamos-switch-tools-m7c` was found; the
  root-only helpers there are dev-time artifacts.  The package deliberately
  does not depend on them.
* Live sessions at audit time: SSH user session + manager only; no Alpha
  Xorg/Zenity scope; display resting (bl_power=4).

## Install / single activation (later execution prompt; not run here)

```
# install + select startup (no start, no reboot)
sudo bash install-steam-cpu-autostart.sh
# single first activation (after confirming no Alpha scope is active; if a
# switchdeck-session scope exists, stop ONLY that exact session-*.scope)
systemctl start steam-cpu-session.service
# verify: switchdeck session/Steam identity, private llvmpipe provenance,
# accepted geometry, real Steam content; user UI verdict separate.
# rollback
sudo bash rollback-steam-cpu-autostart.sh
```

## Proven locally vs target UNKNOWN

Locally proven (mock): exact geometry argv, child env (private LD order,
llvmpipe/GALLIUM/GLX vars, unsets), fail-closed setup, display prep on/off,
install backups + startup-selection switch, rollback restore.  Target UNKNOWN:
boot outcome (enable symlink alone is not proof), first-boot timing against
`switchdeck-hardware.service`, real Steam/CEF identity/provenance, geometry,
UI verdict — all for the later activation/reboot review.
