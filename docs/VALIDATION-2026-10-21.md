# Runtime/boot source validation — 21 October 2026

The exported source was checked on the Linux build host without device access:

* 13 input/rescue/discovery fixture tests passed. The initial exported test
  referenced the private candidate subdirectory; its public relative path was
  corrected and the complete group rerun. Restart actions are mocked.
* 8 ZRAM policy fixtures passed with fake sysfs and mocked swap commands.
* Python modules passed syntax compilation. Shell sources retain their original
  runtime contracts; this publication does not enable their services.
* The boot renderer compiled for host and static ARM64. The portable builder
  rebuilt the update against exact user-supplied Beta1 boot inputs, checked
  uImage CRCs, preserved original CPIO entries other than init and produced
  separate output/receipt. No card, root image, kernel or original archive changed.

All code is source-only; compiled local outputs are excluded. The publication
does not install Steam, vendor drivers, firmware or compatibility-tool payloads.
GUI/hotkey/game acceptance cited elsewhere belongs to the existing development
card. Changed-size ZRAM cold boot and new Beta1 startup-v4 hardware boot remain
unqualified. FEX lowmem exit restoration is FAIL/RECOVERED, not clean lifecycle.
