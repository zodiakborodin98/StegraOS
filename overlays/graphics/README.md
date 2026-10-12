# First-boot graphics diagnostics

This overlay runs a bounded diagnostic session inside the genuine SteamOS ARM
rootfs on the Switch. It does not launch Steam, a desktop login, or an installer.
KDE Plasma X11, Openbox and the diagnostic tools are present in the pinned rootfs;
their presence does not certify the NVIDIA Xorg ABI or Gaming Mode.

Prepare from Linux after staging, graphics-stage.py and the boot overlay:

```sh
sudo python3 scripts/prepare-graphics-probe.py /root/steamos-switch-native
sudo python3 scripts/prepare-graphics-probe.py /root/steamos-switch-native --apply
```

The first command is a read-only plan. The second adds precisely 15 `tegra21x`
firmware files from the SHA-256-pinned NVIDIA package, licenses, the probe and
its enabled systemd unit. Every firmware payload is compared with the pinned
archive before writing. Identical existing files are accepted; differing ones
are refused. Existing TSEC firmware and Switch-specific Wi-Fi files are untouched.

At boot `steamos-switch-graphics-probe.service` creates
`/var/log/steamos-switch/graphics-<boot-id>/`, enumerates the NVIDIA ICD, then
starts an authenticated local Xorg display if no display session is active.
TCP listening is disabled. It collects Xorg, GLX, EGL and full Vulkan logs, then
tries the genuine gamescope SDL backend with a harmless two-second sleep child.
No login or password is required. The X server is stopped at completion; only
the timeout process created by this probe is signalled. Individual commands and
the complete service are bounded (service limit 250 seconds, Xorg 190 seconds,
gamescope 45 seconds). A kernel driver stuck in an uninterruptible state cannot
be guaranteed to obey a userspace timeout.

Xorg may temporarily replace the console with its display on VT8. Its normal VT
restoration stays enabled; the cleanup handler also runs a bounded `chvt` back
to the console recorded before startup (fallback VT1). The rootfs Xorg manpage
describes `-novtswitch` as disabling restoration on reset/shutdown, so that flag
is deliberately absent. Device initialization is allowed a separate bounded
15-second `udevadm settle` after the standard udev trigger/daemon. Timeout is
recorded by systemd but does not suppress useful diagnostic failure logs.

`capabilities.json` marks an extension present/absent only after successful,
recognizable NVIDIA device enumeration. Driver loading failure, another vendor,
or an unfamiliar log format returns `unknown`; full logs retain the evidence.
An enumerated extension is not a successful dma-buf interchange or presentation
test. Gamescope exit status and its full log record the actual initialization
outcome. A timeout does not prove a successful graphics session.

The FAT log exporter must order itself **after**
`steamos-switch-graphics-probe.service` and copy this boot-specific directory.
The probe does not mount or write the SD card FAT partition itself.

No software Vulkan driver is installed in the pinned rootfs: its original ICD
directory contains only Freedreno. Installing a CPU compositor would still
require proven interchange with NVIDIA-rendered client buffers; gamescope's
existing Vulkan importer uses dma-buf. Choosing a software Vulkan device alone
does not provide that interoperability. This option is not represented as a
working substitute for the missing NVIDIA Vulkan features.

Validation performed off-device: Python syntax, shell syntax, the real staged
firmware/archive plan, and five capability-parser cases covering success,
failure, foreign vendor, malformed vendor and missing device information.
No hardware graphics execution has yet been performed.
