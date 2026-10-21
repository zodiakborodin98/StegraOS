<!-- publication-2026-10-21 -->
The newly added Stegra Control/input/boot sources retain their existing MIT or other per-file notices. The project GPL packaging license does not erase these grants. The hardware integration derives from the previously credited Switchdeck/Switchroot platform; no vendor binary is included.

# Attribution and licenses

* Valve/SteamOS/Gamescope: the external ARM userland/compositor platform.
* Switchroot, CTCaer, theofficialgman, Gavin_Darkglider and other Linux/boot/
  hardware contributors: Switch kernel/BSP/device platform. Kernel source
  identity of the running #149 build remains unknown; credited sources are not
  automatically that exact binary's corresponding source.
* theofficialgman/vk_layer_tegra_dc_present: Tegra DC/GOB foundation of the
  sibling graphics/LCD code. That implementation is not bundled as a binary here.
* SildurFX/Switchdeck (`be0578fbf00c5a14a988a830f914f2c34cdaa1a1`) and
  Switchdeck-Extras: ARM64 Steam and Proton/FEX/Box64/DXVK-Sarek/VKD3D recipes.
  Switchdeck credits Ivy for the original steam-arm64 download script.
* hashtagbasit/SteamOS-ARM-Handhelds: GPL-2.0-only extraction scripts adapted
  by `scripts/fetch-steamos.py`. Header/source URL and license remain intact.

Original packaging scripts/tests/docs use GPL-2.0-or-later unless a file carries
a different applicable notice. `fetch-steamos.py` and its derived test retain
GPL-2.0-only. The imported Alpha1 `switchdeck-steam` script is Switchdeck-derived;
its **GPL-3.0-only** grant is retained and the full GPLv3 text is in LICENSES/GPL-3.0.txt.
The older locally authored Alpha1 MIT notice is preserved for attribution in
`legacy-alpha1/HISTORICAL-LICENSE.txt`; it is not the upstream Switchdeck license.
No upstream script or package is silently relicensed as our GPLv2 work.

Copyright (c) 2026 SteamOS-Switch contributors for original work.
Source-only publication preparation, license/packaging changes and reference
documentation updated 2026-10-12; historical source inputs remain identifiable.

This repository includes no Valve Steam/SteamOS image, NVIDIA drivers/firmware,
kernel binary, proprietary runtime, games, account/configuration data, credentials,
raw evidence or private Git history. Download/build inputs retain their own terms;
a SHA pin identifies bytes, not a redistribution license or vendor endorsement.
The published manifests clearly distinguish official and community-repack inputs.

Current accepted device behavior is a historical evidence baseline, not proof that
a newly built image equals it. Read docs/STATUS.md. This is not an official release.
