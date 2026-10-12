import importlib.util
import io
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/prepare-steam-client.py'
spec = importlib.util.spec_from_file_location('steam_candidate', SCRIPT)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class ArchiveBoundaryTests(unittest.TestCase):
    def tar_bytes(self, name, *, link=None):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            member = tarfile.TarInfo(name)
            if link is not None:
                member.type = tarfile.SYMTYPE
                member.linkname = link
                archive.addfile(member)
            else:
                member.size = 4
                archive.addfile(member, io.BytesIO(b'test'))
        stream.seek(0)
        return stream

    def test_tar_rejects_traversal_and_absolute_paths(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / 'candidate'
            output.mkdir()
            for name in ('../escaped', '/escaped', 'C:/escaped', 'dir\\escaped'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    client.extract_tar(self.tar_bytes(name), output)
            self.assertFalse((Path(td) / 'escaped').exists())
            self.assertEqual(list(output.iterdir()), [])

    def test_tar_rejects_link_outside_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / 'candidate'
            output.mkdir()
            with self.assertRaises(tarfile.FilterError):
                client.extract_tar(self.tar_bytes('link', link='../escaped'), output)
            self.assertFalse((output / 'link').exists())

    def test_zip_does_not_follow_preexisting_link(self):
        if not hasattr(Path, 'symlink_to'):
            self.skipTest('symlinks unavailable')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / 'candidate'
            output.mkdir()
            outside = root / 'outside'
            outside.mkdir()
            try:
                (output / 'steamui').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('symlinks unavailable to this account')
            archive = root / 'test.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('steamui/example.js', 'test')
            with self.assertRaises(ValueError):
                client.extract_zip(archive, output, {'steamui'})
            self.assertEqual(list(outside.iterdir()), [])

    def test_zip_rejects_links_and_unexpected_folders(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / 'candidate'
            output.mkdir()
            for name, mode in [('config/loginusers.vdf', 0o644), ('steamui/link', stat.S_IFLNK | 0o777)]:
                archive = root / 'test.zip'
                with zipfile.ZipFile(archive, 'w') as z:
                    entry = zipfile.ZipInfo(name)
                    entry.create_system = 3
                    entry.external_attr = mode << 16
                    z.writestr(entry, '../outside')
                with self.subTest(name=name), self.assertRaises(ValueError):
                    client.extract_zip(archive, output, {'steamui'})
            self.assertEqual(list(output.iterdir()), [])

    def test_zip_normalizes_valve_slashes_without_accepting_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / 'candidate'
            output.mkdir()
            archive = root / 'test.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('steamui\\css/', '')
                z.writestr('steamui\\css/example.css', 'test')
            client.extract_zip(archive, output, {'steamui'})
            self.assertEqual((output / 'steamui/css/example.css').read_text(), 'test')
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('steamui\\..\\..\\escaped', 'bad')
            with self.assertRaises(ValueError):
                client.extract_zip(archive, output, {'steamui'})
            self.assertFalse((root / 'escaped').exists())


if __name__ == '__main__':
    unittest.main()
