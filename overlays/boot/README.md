# Diagnostic boot overlay

This is the first console stage of a native SteamOS port. It does not enable
KDE, Steam or Game Mode. The source remains Valve's SteamOS ARM rootfs,
including the original `/usr/lib/os-release` and `/etc/os-release`.

Run on a newly staged Linux tree:

```sh
sudo python3 scripts/apply-switch-overlay.py /root/steamos-switch-stage/rootfs --dry-run
sudo python3 scripts/apply-switch-overlay.py /root/steamos-switch-stage/rootfs
```

The root filesystem image must be ext4 labelled `SWR-STM`. Its `/etc`, `/var`
and `/home` are ordinary writable directories. The Frame image's `etc.mount`
expects `/var/lib/overlays/etc/{upper,work}`, which its own initrd normally
provides. Those directories do not exist in the pinned unbooted source, and
Switchroot's initramfs does not create them. This overlay therefore masks
`etc.mount`, the original separate `home.mount`, and SteamOS offload mounts.
It rejects an existing `/var/lib/overlays` tree to avoid silently discarding
configuration from an already booted system. It does not invent an upper layer.

The script masks Frame hardware/VR services, A/B update services, repartitioning,
U-Boot environment updates, the original graphics stack, Frame automount/rescan
rules and the duplicate zram generator. Vendor files stay in `/usr/lib`;
systemd and udev use masks in `/etc`. The manifest records original managed
files and desired contents. Reruns are idempotent and refuse unexpected edits.

No credentials are changed and no root autologin or SSH access is enabled.
The source account is `steamos`, UID/GID 1000, home `/home/steamos`; its login
credentials have not been established by this build. A boot-info service writes
local evidence to `/var/log/steamos-switch/` without needing an interactive login.

`SteamOS-console.ini.draft` must accompany this profile when image packaging is
ready. `fbconsole=0` selects the built-in display in this BSP. The stock boot.scr
already selects cgroup v1; systemd 257 additionally requires
`SYSTEMD_CGROUP_ENABLE_LEGACY_FORCE=1` **in the kernel command line**. The stock
initramfs passes `2` to init, so the draft explicitly sets
`systemd.unit=multi-user.target` as well as `default.target` in the rootfs.
This is not permission to flash an unvalidated image.

The stock initramfs requires `/etc/switchroot_version.conf`, uses target
`/usr/bin/xxd` for Bluetooth firmware, performs fsck and first-boot resize,
imports Joy-Con calibration, and initializes zram. It also changes firmware
and several sysfs settings. This overlay does not patch that initramfs or
claim those operations have been tested with SteamOS. Do not copy `.reinit`,
`.cleanup`, `modules.tar.gz` or `update.tar.gz` into its SD boot directory.

Optional `--fat-uuid XXXX-XXXX` adds a **manual**, read-only `/boot` mount.
Without it no systemd FAT mount is configured; the initramfs locates and
temporarily mounts the boot partition itself. `/etc/.resized` is deliberately
not created, so BSP's first-boot filesystem expansion remains available.

The later graphical phase must explicitly replace the diagnostic masks and
session profile after GPU/driver and client checks. Selecting graphical.target
alone does not enable the masked Frame display manager or Steam units.

Evidence: `reports/boot-source-*.json`, `reports/switchroot-init-source.txt`, and
the source URLs and hashes pinned in `sources.lock.json`.
