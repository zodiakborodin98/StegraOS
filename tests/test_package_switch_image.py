# SPDX-License-Identifier: GPL-2.0-or-later
import gzip
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("switch_package", PROJECT / "scripts/package-switch-image.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def test_size_reserves_metadata_and_free_space(self):
        self.assertEqual(package.image_size(8 * package.GIB), 12 * package.GIB)
        self.assertGreaterEqual(package.image_size(12 * package.GIB), 15 * package.GIB)
        with self.assertRaises(package.PackageError):
            package.image_size(12 * package.GIB, 12)

    def test_parts_reconstruct_exact_image_with_hashes(self):
        image = self.base / "fixture.img"
        image.write_bytes(b"a" * package.ALIGNMENT + b"b" * package.ALIGNMENT + b"c" * package.ALIGNMENT)
        parts, digest = package.split_image(image, self.base / "parts", package.ALIGNMENT)
        self.assertEqual([p["path"] for p in parts], [f"switchroot/install/l4t.{n:02d}" for n in range(3)])
        self.assertEqual(digest, package.sha(image))
        combined = hashlib.sha256()
        for part in parts:
            path = self.base / "parts" / Path(part["path"]).name
            self.assertEqual(package.sha(path), part["sha256"])
            self.assertEqual(path.stat().st_size % package.ALIGNMENT, 0)
            combined.update(path.read_bytes())
        self.assertEqual(combined.hexdigest(), digest)

    def test_split_rejects_unaligned_or_existing_destination(self):
        image = self.base / "bad.img"
        image.write_bytes(b"bad")
        with self.assertRaises(package.PackageError):
            package.split_image(image, self.base / "parts")
        with image.open("wb") as stream:
            stream.truncate(package.ALIGNMENT)
        (self.base / "parts").mkdir()
        with self.assertRaises(FileExistsError):
            package.split_image(image, self.base / "parts")
        with self.assertRaises(package.PackageError):
            package.split_image(image, self.base / "new-parts", 4294967296)

    @unittest.skipUnless(os.name == "posix", "Linux path checks")
    def test_device_existing_and_symlink_destinations_rejected(self):
        for target in ("/", "/dev/new-file", "/proc/new-file", "/sys/new-file", str(self.base)):
            with self.subTest(target=target), self.assertRaises(package.PackageError):
                package.checked_path(target, new=True)
        link = self.base / "link"
        link.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(package.PackageError):
            package.checked_path(link / "new", new=True)

    @unittest.skipUnless(os.name == "posix", "Linux ext4 tools and QEMU integration")
    def test_legacy_ext4_features_with_bsp_fsck(self):
        required = ("mkfs.ext4", "dumpe2fs", "e2fsck", "qemu-aarch64-static")
        if not all(shutil.which(tool) for tool in required):
            self.skipTest("Needs Linux e2fsprogs and qemu-aarch64-static")
        initramfs = PROJECT / "downloads/switch-bsp-inspect/opt/switchroot/bootstack/initramfs"
        if not initramfs.exists():
            self.skipTest("Pinned BSP initramfs has not been downloaded")
        raw = initramfs.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), "9af310cb9eccfaeadebe697f4cba393e398d0638e8b3d2a4afe670e820cc4b79")
        blob = gzip.decompress(raw[64:])
        offset, executable = 0, None
        runtime = self.base / "bsp-runtime"
        (runtime / "lib/aarch64-linux-gnu").mkdir(parents=True)
        while offset < len(blob):
            header = blob[offset:offset + 110]
            self.assertIn(header[:6], (b"070701", b"070702"))
            fields = [int(header[i:i + 8], 16) for i in range(6, 110, 8)]
            size, name_size = fields[6], fields[11]
            name = blob[offset + 110:offset + 110 + name_size - 1].decode()
            offset = (offset + 110 + name_size + 3) & ~3
            data = blob[offset:offset + size]
            offset = (offset + size + 3) & ~3
            if name in ("sbin/fsck.ext3", "./sbin/fsck.ext3") and data.startswith(b"\x7fELF"):
                executable = self.base / "bsp-fsck"
                executable.write_bytes(data)
                executable.chmod(0o755)
            if (name == "lib/ld-linux-aarch64.so.1" or
                    re.fullmatch(r"lib/aarch64-linux-gnu/[A-Za-z0-9_.+-]+", name)):
                target = runtime / name
                if stat.S_ISREG(fields[1]):
                    target.write_bytes(data)
                    target.chmod(0o755)
                elif stat.S_ISLNK(fields[1]):
                    link = data.decode()
                    self.assertNotIn("..", link)
                    self.assertFalse(link.startswith("/"))
                    target.symlink_to(link)
            if name == "TRAILER!!!":
                break
        self.assertIsNotNone(executable, "Expected static fsck.ext3 inside pinned BSP initramfs")
        fixture = self.base / "fixture.ext4"
        with fixture.open("xb") as target:
            target.truncate(96 * 1024**2)
        package.run(["mkfs.ext4", "-F", "-b", "4096", "-I", "256", "-m", "1", "-L", "SWR-STM",
                     "-O", "none," + package.FEATURES, "-E", "lazy_itable_init=0,lazy_journal_init=0", fixture])
        superblock = package.run(["dumpe2fs", "-h", fixture])
        actual = set(re.search(r"^Filesystem features:\s*(.*)$", superblock, re.M).group(1).split())
        self.assertEqual(actual, set(package.FEATURES.split(",")))
        package.run(["e2fsck", "-fn", fixture])
        before = package.sha(fixture)
        try:
            old_check = package.run(["qemu-aarch64-static", "-cpu", "cortex-a57", "-L", runtime, executable, "-fn", fixture])
        except subprocess.CalledProcessError as exc:
            self.fail(exc.stdout)
        self.assertIn("1.45.6", old_check)
        self.assertIn("Pass 5", old_check)
        self.assertEqual(package.sha(fixture), before, "BSP fsck -fn must not change the fixture")


if __name__ == "__main__":
    unittest.main()
