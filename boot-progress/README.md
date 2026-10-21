# Visible first boot and post-handoff console — M11P4

Source-only update for the exact experimental Beta1 boot package. There are
no initramfs/kernel/bootloader/firmware binaries here and no card installer.
The base package must be obtained separately under its own distribution terms.

On a Linux cross-build host:
```sh
sh build-renderer.sh
python3 build_update.py --package /path/to/extracted-beta1 --output /path/to/new-output
```
The output directory must be new. Input SHA/CRC/CPIO checks fail closed; input
files are never overwritten. This is not a generic patcher for arbitrary images.

The early fbdev display shows phase, elapsed time, activity and recent console
lines. It uses real progress only when the underlying command supplies it,
rotates physical scanout180 degrees and restores KD_TEXT. The boot script routes
tty1/fbcon with rotation. The exact reference kernel supports VT/fbcon rotation.

Pinned FAT/console descriptors record BEFORE/AFTER/rc for final mount moves.
The real-root PID1 wrapper logs before exec; a runtime-only systemd capture
records actual /proc/1 identity and bounded system/warning/Steam streams.
128KiB per stream and1200s limit **logging only**, not boot, resize or game time.
No permanent framebuffer renderer covers Steam after handoff.

The prior v3 device display flashed inverted for about one second, then black.
Its log proved root mount/ldconfig, not actual systemd entry. The earlier root
had already expanded. v4 is installed on a separate test card but **hardware
orientation, console and successful boot remain unverified**. Do not promise
a10–15-minute completion time or assign the black screen to GPU/resize.

Read stegra-logs/boot-N*.log to locate the actual failure stage. A
BEFORE_SYSTEMD_EXEC marker alone is not proof that systemd executed.
