# Persistent480p Zink session source recipe

This is an adaptable source template of the measured development profile, not
a ready-to-run installer or a fresh image qualification. No GPU/client payload
is supplied. The parent CPU session templates remain the rollback reference.

Assembly prerequisites: a qualified compositor/Xwayland/GBM deployment with its
own manifests, an authorized local Steam ARM64 client, pinned Mesa 24.2.8 T3 GLX/
Gallium/glapi, the built early-ICD shim, and the exact private CEF copy. Sources
and build helpers are in gamescope-tegra's graphics-stack/steam-zink. Use an
owned staging directory; never replace global Mesa/NVIDIA or rewrite Steam data.

Templates use /opt/steam-tegra paths, which must be adapted as a consistent
closure. Set GAMESCOPE_TEGRA_SHA256 for your own qualified build; do not assume
a historical ELF hash qualifies a newly built compositor. The launcher still
expects deploy/package manifests and compatible fallback directories. The CEF
directory contains the private helper and access to your own matching runtime
resources; these are not included. User steamos/systemd/owned0700 runtime and the
existing display/audio/session setup are prerequisites. Keep CPU rollback files.

The drop-in deliberately sorts after older orientation ExecStart overrides.
Inspect the effective systemd ExecStart before starting. The service retains
its historical name steam-cpu-session.service, now with infinity lifetime and
no automatic test rollback. Restart remains:
`sudo systemctl restart steam-cpu-session.service`.

To roll back an installation, restore the backed-up source/drop-in or remove
only your own qualified Zink override, daemon-reload, then restart the original
CPU session. This example does not supply destructive cleanup or enable itself.

The user confirmed480p Zink with diagnostic sync flags and used it for about an
hour without noticed UI bugs. Optional diagnostics are now off by request;
that change's visual/performance acceptance remains pending. Functional TFP,
child upload, depth layout, allocation, resize and early ICD fixes are retained.
Physical displayFPS, cold boot, long soak and arbitrary game compatibility are
not qualified. See the sibling gamescope-tegra/docs/ZINK-STEAM-UI.md.
