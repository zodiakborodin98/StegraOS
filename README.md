# SteamOS Tegra / Nintendo Switch — source image builders

Community development scripts for assembling SteamOS ARM userland with the Switchroot
Linux/L4T platform and the Tegra graphics stack. **Alpha development source**, not an
official Valve/Switchroot distribution and not a newly validated ready-to-flash release.

## Included implementations

* `scripts/fetch-steamos.py`: manifest-pinned, resumable casync reconstruction with per-chunk
  bounds/hash checks and whole-image verification (GPL-2.0-only upstream-derived).
* Offline rootfs audit/staging, explicit overlay application and regular-file image packaging.
* `legacy-alpha1/source/`: original Alpha1 builder, init/boot/UI/hardware integration source.
  Its Xorg/Zenity path is historical; it is not the current Gamescope startup.
* `current-session-templates/`: actual CPU Steam/Gamescope autostart/display-prep/rollback
  source, without its deployment payload. Treat as a development recipe, not an installer
  ready to apply to an arbitrary system.
* Host tests, pinned input manifests and integration/reproducibility limitations.

## Build flow

Use a disposable Linux/WSL build environment. Read script help and `docs/STATUS.md` first.
For the offline console pipeline, obtain the manifest-pinned vendor inputs under their
applicable terms, reconstruct the rootfs, and prepare an **unused native Linux build directory**:
```sh
python3 -m pip install -r requirements.txt
python3 scripts/prepare-inputs.py --help
python3 scripts/fetch-steamos.py --help
bash scripts/stage-rootfs.sh --help
python3 scripts/apply-switch-overlay.py --help
python3 scripts/package-switch-image.py --help
python3 -m unittest discover -s tests -v
```
The historical Alpha1 source recipe is `legacy-alpha1/source/alpha/build.sh`.
That recipe downloads vendor packages locally, applies an offline overlay and builds
regular files; **no payload is redistributed by this repository**. Its manifests identify
community repack sources as such; they are not relabelled as official Steam ARM64 downloads.

The current-baseline image integration is a separate work item. A native rootfs built by
these scripts is not automatically the accepted current device, and no new installation,
partitioning, bootloader/kernel mutation or hardware run was performed during publication.

## Credits / licensing

* Valve / SteamOS and Gamescope: system userland and compositor.
* [Switchroot](https://wiki.switchroot.org/wiki/linux), CTCaer, theofficialgman,
  Gavin_Darkglider and the community: Switch kernel/BSP/boot/hardware platform.
* [theofficialgman](https://github.com/theofficialgman/vk_layer_tegra_dc_present): Tegra DC/GOB foundation.
* [SildurFX / Switchdeck](https://github.com/SildurFX/Switchdeck) and Switchdeck-Extras:
  Steam ARM64 and Proton/FEX/Box64/DXVK-Sarek/VKD3D compatibility recipes;
  Switchdeck itself credits Ivy for the original steam-arm64 downloader.
* [hashtagbasit/SteamOS-ARM-Handhelds](https://github.com/hashtagbasit/SteamOS-ARM-Handhelds):
  the upstream GPLv2 casync extraction code adapted by `fetch-steamos.py`.

Our new original materials use GPL-2.0-or-later; upstream-derived GPL-2.0-only files
and all other component terms remain as identified. The Switchdeck-derived
`legacy-alpha1/source/alpha/root/usr/local/bin/switchdeck-steam` retains **GPL-3.0-only**.
The preserved old Alpha1 MIT notice
documents its historical origin; it is not Switchdeck's GPLv3 script license. Closed
Valve/NVIDIA packages retain their own terms and are never covered by our GPL.

Sibling repositories: [graphics](https://github.com/zodiakborodin98/gamescope-tegra),
[LCD backend/probes](https://github.com/zodiakborodin98/tegra-lcd-backend),
[blockers and implementations](https://github.com/zodiakborodin98/steamos-tegra-research).
