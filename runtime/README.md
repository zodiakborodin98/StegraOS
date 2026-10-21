# Current runtime integration sources — 21 October 2026

This is source for an existing experimental SteamOS/Switch installation, **not
an automatic installer or a boot-qualified full image**. Preserve per-file
licenses. Existing vendor/runtime payloads are user supplied. Review destination
paths and service ordering before integrating into a new image.

| Source directory | Intended destination / responsibility |
|---|---|
| steam-perf-panel | /usr/local/lib/steam-perf-panel; CPU/GPU ceilings, English overlay, global CEF lease manager, ZRAM policy |
| steam-input/switch_devices.py | /usr/local/lib/steam-input; physical Lite/Joy-Con roles, excluding Steam virtual Xbox |
| steam-input/session-rescue.py | /usr/local/lib/steam-cpu/session-rescue.py; hold Capture3s to restart the Steam session |
| steam-input/*.c | touch and USB/libinput→existing EIS bridge sources; build with target libinput/libei dependencies |
| steam-lowmem | helper at /usr/local/lib/steam-cpu/steam-lowmem.py, /usr/local/bin/steam-lowmem wrapper, plus the CEF gate script at the configured CEF_PATH |
| compat-policy | copy into each user-supplied FEX/Box64 compatibility tool and bind its payload manifest |
| system-integration | hardware helper and systemd unit source; inspect existing owners before enabling |

Minus opens the side panel. Up/down selects vertical tabs; right/A enters,
left/B returns. Filled radio indicates the saved choice, not keyboard focus.
Saved target, actual ceiling and current DVFS clock are distinct.

The backend is wanted by graphical.target after switch-perf.service; the old
multi-user ordering created a boot cycle. Existing device permissions are
required; this export does not grant broad input/root access. The current OLED
user handler reads Combined Joy-Cons; root Capture uses physical Left.
Backend and rescue units are system services; panel hotkey and global-lowmem
units are user services. Enabling them in the wrong manager is not supported.
Place switch_devices.py at the shared steam-input path even though the rescue
entrypoint itself remains in the historical steam-cpu directory.

Global CEF unload defaults OFF. One lease per game unloads webhelper while
retaining Steam core/API/input. The old per-game wrapper is passthrough under
the global manager; it must not create a second lease. Native SMB6→0→6 passed.
HL2DM/FEX gameplay with Steam Input and no CEF passed on a keyboard/mouse
layout, but CEF restoration on exit had consumer0x321c and required recovery.
Opening/closing the panel in Windows games also remains in development.

The catalog is **not a hardware allowlist**. CPU2091 was clamped to1963.5MHz;
RAM2133 was read back on one OC boot. Arbitrary table entries up to3200MHz
are not implemented just because they are listed. Keep voltage/thermal guards.

ZRAM choices System(3GiB)/Off/1/2/3/4GiB save for the next **OS boot**, not
Steam restart. Runtime never swapoff/resets active swap. The existing hardware
helper remains the single owner, with zram-generator masked on the reference
system. UI/API and active-swap preservation passed; a changed-size cold boot
has not been qualified. Do not run --boot manually as a live resizing command.
