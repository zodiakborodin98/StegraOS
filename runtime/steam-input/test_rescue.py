#!/usr/bin/env python3
"""Safety boundaries. No devices, systemd operations or root required."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('rescue', Path(__file__).with_name('session-rescue.py'))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class Tests(unittest.TestCase):
    def test_short_press(self):
        h = r.Hold()
        self.assertFalse(h.sample(True, 10))
        self.assertFalse(h.sample(True, 12.999))
        self.assertFalse(h.sample(False, 13))
        self.assertFalse(h.sample(True, 20))

    def test_hold_once_and_release(self):
        h = r.Hold()
        h.sample(True, 10)
        self.assertTrue(h.sample(True, 13))
        self.assertFalse(h.sample(True, 30))
        h.sample(False, 31)
        h.sample(True, 32)
        self.assertTrue(h.sample(True, 35))

    def test_startup_held_and_overrun(self):
        h = r.Hold(True)
        self.assertFalse(h.sample(True, 100))
        h.sample(False, 101)
        h.sample(True, 102)
        h.reset(True)
        self.assertFalse(h.sample(True, 200))
        h.sample(False, 201)
        h.sample(True, 202)
        self.assertTrue(h.sample(True, 205))

    def test_dryrun_never_calls_systemctl(self):
        with patch.object(r.subprocess, 'run') as run:
            r.Action(True).fire(10)
            run.assert_not_called()

    def test_fixed_command_and_cooldown(self):
        with patch.object(r.subprocess, 'run') as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ''
            a = r.Action(False)
            a.fire(10)
            a.fire(11)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0],
                ['/usr/bin/systemctl', '--no-block', 'restart', 'steam-cpu-session.service'])
            self.assertEqual(run.call_args.kwargs['timeout'], 8)
            a.fire(40)
            self.assertEqual(run.call_count, 2)

    def test_refusal_timeout_not_success(self):
        with patch.object(r, 'log') as log, patch.object(r.subprocess, 'run') as run:
            run.return_value.returncode = 1
            run.return_value.stderr = 'refused'
            r.Action(False).fire(10)
            self.assertEqual(log.call_args.args[0], 'RESTART_REFUSED')
            run.side_effect = r.subprocess.TimeoutExpired('systemctl', 8)
            r.Action(False).fire(10)
            self.assertEqual(log.call_args.args[0], 'RESTART_COMMAND_TIMEOUT')


if __name__ == '__main__':
    unittest.main(verbosity=2)
