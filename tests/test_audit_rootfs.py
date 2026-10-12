"""Offline fixtures test ELF parsing, metadata decisions and root confinement."""

import importlib.util
import os
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("audit_rootfs", Path(__file__).resolve().parents[1] / "scripts" / "audit-rootfs.py")
audit_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_module)


def elf64(machine=183, kernel=(3, 7, 0), endian="<"):
    note = struct.pack(endian + "III", 4, 16, 1) + b"GNU\0" + struct.pack(endian + "IIII", 0, *kernel)
    ident = b"\x7fELF" + bytes((2, 1 if endian == "<" else 2, 1)) + bytes(9)
    header = struct.pack(endian + "HHIQQQIHHHHHH", 3, machine, 1, 0, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    program = struct.pack(endian + "IIQQQQQQ", 4, 0, 120, 0, 0, len(note), len(note), 4)
    return ident + header + program + note


class RootAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, logical, data):
        path = self.root / logical.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path

    def package(self, db, version):
        self.write(db + "/systemd-fixture/desc", f"%NAME%\nsystemd\n\n%VERSION%\n{version}\n\n%ARCH%\naarch64\n")

    def symlink(self, logical, target, directory=False):
        path = self.root / logical.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            self.skipTest("This platform/user cannot create symlink fixtures: " + str(exc))

    def virtual_links(self, targets):
        """Exercise the resolver on Windows without requiring symlink privilege.

        Only lstat/readlink of the specified links are simulated. Real target
        directories/files and the complete RootFS resolution logic are used.
        Native symlinks are covered separately when the platform permits them.
        """
        links = {self.root / name.lstrip("/"): target for name, target in targets.items()}
        original_lstat = Path.lstat
        original_readlink = os.readlink

        def fixture_lstat(path, *args, **kwargs):
            if path in links:
                return os.stat_result((stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            return original_lstat(path, *args, **kwargs)

        def fixture_readlink(path, *args, **kwargs):
            if Path(path) in links:
                return links[Path(path)]
            return original_readlink(path, *args, **kwargs)

        lstat_patch = patch.object(Path, "lstat", fixture_lstat)
        readlink_patch = patch.object(audit_module.os, "readlink", fixture_readlink)
        lstat_patch.start()
        self.addCleanup(lstat_patch.stop)
        readlink_patch.start()
        self.addCleanup(readlink_patch.stop)

    def test_both_package_databases_are_inspected(self):
        self.package("/usr/lib/holo/pacmandb/local", "257.8-1")
        self.package("/var/lib/pacman/local", "2:258.1-1")
        report = audit_module.audit(self.root)
        self.assertEqual(report["package_count"], 2)
        self.assertEqual(report["status"], "blocked")
        codes = {item["code"] for item in report["findings"]}
        self.assertIn("conflicting-systemd-metadata", codes)
        self.assertIn("systemd-kernel-baseline", codes)

    def test_direct_holo_database_layout(self):
        self.package("/usr/lib/holo/pacmandb", "256-3")
        report = audit_module.audit(self.root)
        self.assertEqual(report["package_count"], 1)
        self.assertEqual(report["status"], "unverified")

    @unittest.skipUnless(os.name == "posix", "Epoch filenames require POSIX storage")
    def test_pacman_epoch_directory_is_counted_on_linux(self):
        logical = "/usr/lib/holo/pacmandb/local/fixture-2:1.0-1/desc"
        self.write(logical, "%NAME%\nfixture\n\n%VERSION%\n2:1.0-1\n\n%ARCH%\naarch64\n")
        self.assertEqual(audit_module.RootFS(self.root).read(logical).splitlines()[1], b"fixture")
        report = audit_module.audit(self.root)
        self.assertEqual(report["package_count"], 1)
        self.assertFalse(any(item["code"] == "package-metadata-unreadable" for item in report["findings"]))

    def test_windows_alternate_data_streams_still_rejected(self):
        with patch.object(audit_module.os, "name", "nt"):
            for path in ("/etc/passwd:secret", "/db/fixture-2:1.0-1/desc", "C:/Windows"):
                with self.subTest(path=path), self.assertRaises(audit_module.AuditError):
                    audit_module.RootFS.parts(path)

    def test_older_systemd_is_not_a_blanket_blocker(self):
        self.package("/var/lib/pacman/local", "257.1-1")
        report = audit_module.audit(self.root)
        self.assertFalse(any(item["level"] == "blocker" for item in report["findings"]))
        self.assertIn("legacy-cgroup-boot-flags", {item["code"] for item in report["findings"]})
        self.assertEqual(report["status"], "unverified")

    def test_systemd_baseline_is_not_triggered_for_kernel54(self):
        self.package("/var/lib/pacman/local", "258-1")
        report = audit_module.audit(self.root, "5.4.0")
        self.assertNotIn("systemd-kernel-baseline", {item["code"] for item in report["findings"]})
        self.assertIn("systemd-cgroup-v1-removed", {item["code"] for item in report["findings"]})

    def test_arm64_elf_and_gnu_abi_are_read_in_both_byte_orders(self):
        for endian in ("<", ">"):
            path = self.write("/test-elf", elf64(endian=endian))
            result = audit_module.inspect_elf(path)
            self.assertEqual(result["architecture"], "aarch64")
            self.assertEqual(result["bits"], 64)
            self.assertEqual(result["gnu_abi_tags"], [{"os": "Linux", "minimum_kernel": "3.7.0"}])

    def test_newer_gnu_abi_tag_blocks_old_target(self):
        self.write("/usr/lib/libc.so.6", elf64(kernel=(5, 4, 0)))
        report = audit_module.audit(self.root)
        self.assertIn("elf-kernel-abi", {item["code"] for item in report["findings"]})
        self.assertEqual(report["status"], "blocked")

    def test_wrong_core_architecture_is_identified(self):
        self.write("/usr/bin/gamescope", elf64(machine=62))
        report = audit_module.audit(self.root)
        self.assertIn("core-elf-architecture", {item["code"] for item in report["findings"]})

    def test_truncated_elf_reports_unknown_and_continues(self):
        self.write("/usr/lib/systemd/systemd", elf64()[:-3])
        self.write("/usr/bin/gamescope", elf64())
        report = audit_module.audit(self.root)
        binary_by_path = {item["path"]: item for item in report["binaries"]}
        self.assertEqual(binary_by_path["/usr/lib/systemd/systemd"]["kind"], "unreadable")
        self.assertEqual(binary_by_path["/usr/bin/gamescope"]["kind"], "ELF")
        self.assertEqual(report["status"], "unverified")

    def test_script_is_not_executed(self):
        self.write("/usr/bin/steam", "#!/bin/sh\ntouch forbidden\n")
        report = audit_module.audit(self.root)
        steam = next(item for item in report["binaries"] if item["path"] == "/usr/bin/steam")
        self.assertEqual(steam["kind"], "script")
        self.assertFalse((self.root / "forbidden").exists())

    def test_absolute_symlink_stays_inside_root(self):
        expected = self.write("/usr/lib/os-release", 'ID=steamos\n')
        self.symlink("/etc/os-release", "/usr/lib/os-release")
        fs = audit_module.RootFS(self.root)
        self.assertEqual(fs.resolve("/etc/os-release"), expected)
        self.assertEqual(fs.read("/etc/os-release"), b"ID=steamos\n")

    def test_virtual_absolute_symlink_cannot_read_host_file(self):
        expected = self.write("/usr/lib/os-release", "ID=fixture\n")
        (self.root / "etc").mkdir()
        self.virtual_links({"/etc/os-release": "/usr/lib/os-release"})
        fs = audit_module.RootFS(self.root)
        self.assertEqual(fs.resolve("/etc/os-release"), expected)
        self.assertEqual(fs.read("/etc/os-release"), b"ID=fixture\n")

    def test_virtual_relative_links_and_directory_alias(self):
        expected = self.write("/usr/lib/fixture", "inside")
        (self.root / "etc").mkdir()
        self.virtual_links({"/lib": "usr/lib", "/etc/fixture": "../lib/fixture"})
        self.assertEqual(audit_module.RootFS(self.root).resolve("/etc/fixture"), expected)

    def test_virtual_escape_and_cycle_are_rejected(self):
        self.virtual_links({"/escape": "../../outside", "/loop1": "loop2", "/loop2": "loop1"})
        fs = audit_module.RootFS(self.root)
        for logical in ("/escape", "/loop1", "/../outside", "C:/Windows", r"/etc\host"):
            with self.subTest(path=logical), self.assertRaises(audit_module.AuditError):
                fs.resolve(logical)

    def test_relative_symlink_and_directory_alias(self):
        expected = self.write("/usr/lib/fixture", "inside")
        self.symlink("/lib", "usr/lib", directory=True)
        self.symlink("/etc/fixture", "../lib/fixture")
        self.assertEqual(audit_module.RootFS(self.root).resolve("/etc/fixture"), expected)

    def test_escape_and_symlink_cycle_are_rejected(self):
        self.symlink("/escape", "../../outside")
        self.symlink("/loop1", "loop2")
        self.symlink("/loop2", "loop1")
        fs = audit_module.RootFS(self.root)
        for logical in ("/escape", "/loop1", "/../outside", "C:/Windows", r"/etc\host"):
            with self.subTest(path=logical), self.assertRaises(audit_module.AuditError):
                fs.resolve(logical)

    def test_empty_root_never_reports_compatible(self):
        report = audit_module.audit(self.root)
        self.assertEqual(report["status"], "unverified")
        self.assertIn("no-package-metadata", {item["code"] for item in report["findings"]})
        self.assertTrue(any("ARMv8.0" in item for item in report["limitations"]))

    def test_version_parsing(self):
        self.assertEqual(audit_module.package_major("2:258.1-1"), 258)
        self.assertIsNone(audit_module.package_major("unknown"))
        self.assertEqual(audit_module.kernel_version("4.9.140-switchroot"), (4, 9, 140))
        with self.assertRaises(audit_module.AuditError):
            audit_module.kernel_version("unknown")


if __name__ == "__main__":
    unittest.main()
