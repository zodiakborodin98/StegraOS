import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/install-steam-candidate.py'
spec = importlib.util.spec_from_file_location('steam_install', SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallBoundaryTests(unittest.TestCase):
    def test_existing_home_refused_without_explicit_empty_flag(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / 'home'
            home.mkdir()
            with self.assertRaises(ValueError):
                installer.validate_destination(home, False)
            self.assertEqual(list(home.iterdir()), [])

    def test_populated_home_is_never_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / 'home'
            home.mkdir()
            (home / 'user-file').write_text('keep me')
            with self.assertRaises(ValueError):
                installer.validate_destination(home, True)
            self.assertEqual((home / 'user-file').read_text(), 'keep me')

    def test_inventory_rejects_external_links(self):
        with tempfile.TemporaryDirectory() as td:
            candidate = Path(td) / 'candidate'
            candidate.mkdir()
            try:
                (candidate / 'outside').symlink_to('../outside')
            except OSError:
                self.skipTest('symlinks unavailable to this account')
            with self.assertRaises(ValueError):
                installer.inventory(candidate)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux launcher syntax check')
    def test_shell_scripts_parse_without_execution(self):
        for script in (installer.LAUNCHER, installer.WEBHELPER):
            result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(sys.platform == 'linux' and os.geteuid() == 0, 'root refusal check')
    def test_manual_launcher_refuses_root_before_any_client_execution(self):
        environment = {**os.environ, 'HOME': '/home/steamos', 'DISPLAY': ':99'}
        result = subprocess.run(['bash'], input=installer.LAUNCHER, env=environment,
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Run as the SteamOS steamos user', result.stderr)


if __name__ == '__main__':
    unittest.main()
