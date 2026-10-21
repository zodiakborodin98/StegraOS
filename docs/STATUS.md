<!-- publication-2026-10-21 -->
See [21 October status](UPDATE-2026-10-21.md) for the current working-card/Beta1 distinction. Validation below describes its dated source snapshot.

# Scope and current reproducibility limits

**Update 13 October 2026:** current device UI is480p Zink/NVIDIA, not the
CPU profile described below. Source templates for that route are in
`current-session-templates/zink-480p/`; they preserve the functional fixes and
have optional diagnostics disabled. Fresh image/boot/game compatibility remains
unqualified. The rest of this document describes historical image-building limits.

The project has a working physical-Switch Gamescope+CPU-rendered Steam baseline.
Native FEZ/SMB were visually accepted. This repository is source packaging, not a new
qualified image release and not proof that a freshly built image equals the current device.

Two historical build pipelines are included: regular-file diagnostic-console packaging
(`scripts/`) and the Alpha1 SquashFS/boot builder (`legacy-alpha1/source/alpha/build.sh`).
Alpha1 used Xorg/Openbox/Zenity; the current working baseline uses Gamescope+Steam.
Do not mistake the historical Alpha1 startup for the desired current session.

`current-session-templates/` contains the real accepted CPU-autostart source recipe and
rollback contracts. Its deployment payloads are deliberately absent. The recorded
MANIFEST was omitted because copied source has new publication license notices; users
must generate a manifest for their actual qualified assembled payload.

Still required for a current-baseline image: exact accepted graphics source/payload closure,
modern effective 240p UI/720p game launcher, audio/input/power/lifecycle configuration,
Steam ARM64 compatible client inputs, source-to-binary provenance and a physical boot/UI test.
Proton/FEX/Box64 payloads are external; ordinary FEX game compatibility remains open.

Closed binaries come only from explicit user/vendor inputs at recorded hashes/terms.
No images, Steam/NVIDIA payloads, rootfs/sysroot, saved accounts, credentials, keys, games,
captures or device evidence are in this source repository. No flash/eMMC operation is performed
by publication or by the regular-file packager. The existing device baseline was not changed.
