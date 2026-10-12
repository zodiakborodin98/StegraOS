# SPDX-License-Identifier: GPL-2.0-or-later
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("switch_overlay", PROJECT / "scripts/apply-switch-overlay.py")
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)


@unittest.skipUnless(os.name == "posix", "dir-fd confinement must be exercised on Linux")
class SwitchOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "rootfs"
        for name in ("etc/systemd/system", "usr/lib/systemd/system", "usr/lib/systemd/user", "var/lib"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.release = b'ID=steamos\nVERSION_ID=0.5.0\nVARIANT_ID=vr\n'
        (self.root / "usr/lib/os-release").write_bytes(self.release)
        (self.root / "etc/os-release").symlink_to("../usr/lib/os-release")
        (self.root / "etc/switchroot_version.conf").write_text("5.1.2\n")
        (self.root / "etc/fstab").write_text("/dev/disk/by-partsets/shared/home /home ext4 defaults 0 2\n")
        (self.root / "etc/passwd").write_text("steamos:x:1000:1000::/home/steamos:/bin/bash\n")
        (self.root / "usr/lib/systemd/system/deckard-fpga.service").write_text("[Service]\nExecStart=/usr/bin/false\n")
        (self.root / "usr/lib/systemd/user/steamvr.service").write_text("[Service]\nExecStart=/usr/bin/false\n")

    def test_console_profile_output_and_preserved_identity(self):
        original_passwd = (self.root / "etc/passwd").read_bytes()
        result = overlay.apply(self.root)
        self.assertEqual(result["phase"], "diagnostic-console")
        self.assertIn("LABEL=SWR-STM / ext4", (self.root / "etc/fstab").read_text())
        self.assertNotIn("by-partsets", (self.root / "etc/fstab").read_text())
        self.assertEqual((self.root / "usr/lib/os-release").read_bytes(), self.release)
        self.assertEqual(os.readlink(self.root / "etc/os-release"), "../usr/lib/os-release")
        self.assertEqual((self.root / "etc/passwd").read_bytes(), original_passwd)
        self.assertFalse((self.root / "etc/.resized").exists())
        self.assertFalse((self.root / "var/lib/overlays").exists())
        for name in ("etc.mount", "home.mount", "systemd-repart.service", "setubootenv.service",
                     "atomupd.service", "rauc.service", "deckard-fpga.service", "sddm.service"):
            self.assertEqual(os.readlink(self.root / "etc/systemd/system" / name), "/dev/null")
        self.assertEqual(os.readlink(self.root / "etc/systemd/user/steamvr.service"), "/dev/null")
        self.assertEqual(os.readlink(self.root / "etc/systemd/user/steam.service"), "/dev/null")
        self.assertIn("systemd.unit=multi-user.target", result["required_bootargs"])
        self.assertTrue((self.root / "etc/systemd/system/multi-user.target.wants/steamos-switch-boot-info.service").is_symlink())
        self.assertEqual((self.root / "usr/lib/steamos-switch/collect-boot-info").stat().st_mode & 0o777, 0o755)

    def test_idempotent_with_preserved_originals(self):
        fstab = (self.root / "etc/fstab").read_bytes()
        overlay.apply(self.root)
        first = (self.root / overlay.MANIFEST).read_bytes()
        overlay.apply(self.root)
        self.assertEqual((self.root / overlay.MANIFEST).read_bytes(), first)
        manifest = json.loads(first)
        self.assertEqual(manifest["originals"]["etc/fstab"], overlay.file_value(fstab))

    def test_dry_run_does_not_create_any_managed_files(self):
        before = set(self.root.rglob("*"))
        result = overlay.apply(self.root, dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        self.assertEqual(set(self.root.rglob("*")), before)

    def test_reject_modified_managed_file_before_any_changes(self):
        overlay.apply(self.root)
        manifest_before = (self.root / overlay.MANIFEST).read_bytes()
        (self.root / "etc/fstab").write_text("user edit\n")
        with self.assertRaisesRegex(overlay.OverlayError, "changed outside"):
            overlay.apply(self.root)
        self.assertEqual((self.root / overlay.MANIFEST).read_bytes(), manifest_before)

    def test_reject_unknown_os_and_bsp(self):
        (self.root / "usr/lib/os-release").write_text("ID=ubuntu\n")
        with self.assertRaisesRegex(overlay.OverlayError, "SteamOS"):
            overlay.apply(self.root)
        (self.root / "usr/lib/os-release").write_bytes(self.release)
        (self.root / "etc/switchroot_version.conf").write_text("unknown\n")
        with self.assertRaisesRegex(overlay.OverlayError, "staging"):
            overlay.apply(self.root)

    def test_reject_prior_overlay_layout(self):
        (self.root / "var/lib/overlays/etc/upper").mkdir(parents=True)
        with self.assertRaisesRegex(overlay.OverlayError, "migration"):
            overlay.apply(self.root)

    def test_reject_rootfs_symlink(self):
        linked = self.base / "linked-root"
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(overlay.OverlayError):
            overlay.apply(linked)

    def test_reject_write_parent_symlink_before_fstab_changes(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.root / "etc/udev").symlink_to(outside, target_is_directory=True)
        original = (self.root / "etc/fstab").read_bytes()
        with self.assertRaises(OSError):
            overlay.apply(self.root)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual((self.root / "etc/fstab").read_bytes(), original)
        self.assertFalse((self.root / overlay.MANIFEST).exists())

    def test_existing_mask_symlink_replaced_without_following(self):
        outside = self.base / "outside-unit"
        outside.write_text("do not alter\n")
        (self.root / "etc/systemd/system/sddm.service").symlink_to(outside)
        overlay.apply(self.root)
        self.assertEqual(outside.read_text(), "do not alter\n")
        self.assertEqual(os.readlink(self.root / "etc/systemd/system/sddm.service"), "/dev/null")

    def test_fstab_symlink_rejected(self):
        (self.root / "etc/fstab").unlink()
        outside = self.base / "outside-fstab"
        outside.write_text("outside\n")
        (self.root / "etc/fstab").symlink_to(outside)
        with self.assertRaisesRegex(overlay.OverlayError, "fstab is a symlink"):
            overlay.apply(self.root)
        self.assertEqual(outside.read_text(), "outside\n")

    def test_hardlink_replacement_does_not_truncate_other_file(self):
        outside = self.base / "outside-fstab"
        os.link(self.root / "etc/fstab", outside)
        before = outside.read_bytes()
        overlay.apply(self.root)
        self.assertEqual(outside.read_bytes(), before)
        self.assertNotEqual((self.root / "etc/fstab").stat().st_ino, outside.stat().st_ino)

    def test_manifest_symlink_rejected(self):
        path = self.root / overlay.MANIFEST
        path.parent.mkdir(parents=True)
        outside = self.base / "outside-manifest"
        outside.write_text("{}\n")
        path.symlink_to(outside)
        with self.assertRaisesRegex(overlay.OverlayError, "manifest must not"):
            overlay.apply(self.root)
        self.assertEqual(outside.read_text(), "{}\n")

    def test_optional_fat_mount_never_mounts_automatically(self):
        overlay.apply(self.root, fat_uuid="abcd-1234")
        fstab = (self.root / "etc/fstab").read_text()
        self.assertIn("UUID=ABCD-1234 /boot vfat noauto,nofail,ro", fstab)

    def test_fat_uuid_cannot_inject_fstab_entry(self):
        for value in ("bad", "ABCD-1234\nLABEL=X /x ext4 defaults 0 0", "../../etc"):
            with self.subTest(value=value), self.assertRaises(overlay.OverlayError):
                overlay.apply(self.root, fat_uuid=value)

    def test_paths_cannot_escape_tree(self):
        tree = overlay.OfflineTree(self.root)
        self.addCleanup(tree.close)
        for relative in ("../outside", "/etc/passwd", "etc/../../outside", "etc//fstab", "etc/./fstab"):
            with self.subTest(relative=relative), self.assertRaises(overlay.OverlayError):
                tree.write(relative, overlay.file_value("bad"))


if __name__ == "__main__":
    unittest.main()
