#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Hold the physical Minus button for 1.5 s to open the M10T panel.

The daemon reads the real Lite gamepad without EVIOCGRAB.  It never logs other
keys and leaves short presses to Steam/games.  One hold produces one launch;
the panel itself handles a second hold to close while it owns the gamepad.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import selectors
import signal
import struct
import subprocess
import time

from common import Hold, PANEL_PID, panel_pid, STATE_DIR

KEY = 314  # BTN_SELECT: measured Minus on the Lite gamepad (hid-nintendo)
# Shared discovery supports Lite and rail/paired Joy-Cons.
import sys
sys.path.insert(0, '/usr/local/lib/steam-input')
from switch_devices import hotkey_devices, panel_device
EVENT = struct.Struct('@llHHi')
KEY_BYTES = 96
EVIOCGKEY = (2 << 30) | (KEY_BYTES << 16) | (ord('E') << 8) | 0x18
PANEL = '/usr/local/lib/steam-perf-panel/panel.py'
STOP = False


def log(event, **fields):
    print(json.dumps(dict(event=event, monotonic=round(time.monotonic(), 3),
                          **fields), sort_keys=True), flush=True)


class HoldAction:
    def __init__(self):
        self.next_allowed = 0.0

    def fire(self, now):
        if now < self.next_allowed:
            log('PANEL_OPEN_COOLDOWN')
            return
        self.next_allowed = now + 1.0
        current = panel_pid()
        if current is not None:
            log('PANEL_ALREADY_OPEN', pid=current['pid'])
            return
        env = os.environ.copy()
        env.update({
            'DISPLAY': ':0',
            'XDG_RUNTIME_DIR': '/run/user/1000',
            'HOME': '/home/steamos',
            'PATH': '/usr/local/bin:/usr/bin:/bin',
        })
        try:
            STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            logfile = open(STATE_DIR / 'panel.log', 'ab', buffering=0)
            proc = subprocess.Popen(
                ['/usr/bin/python3', PANEL], env=env,
                stdin=subprocess.DEVNULL, stdout=logfile, stderr=logfile,
                start_new_session=True, close_fds=True)
            log('PANEL_LAUNCHED', pid=proc.pid)
        except OSError as exc:
            log('PANEL_LAUNCH_FAILED', error=str(exc))


class Input:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            self.hold = Hold(self.pressed(), 1.5)
        except BaseException:
            os.close(self.fd)
            raise
        self.dropped = False

    def pressed(self):
        bits = bytearray(KEY_BYTES)
        fcntl.ioctl(self.fd, EVIOCGKEY, bits, True)
        return bool(bits[KEY // 8] & (1 << (KEY % 8)))

    def read(self):
        for _ in range(8):
            try:
                data = os.read(self.fd, EVENT.size * 128)
            except BlockingIOError:
                return
            if not data or len(data) % EVENT.size:
                raise OSError('invalid/disconnected evdev stream')
            for off in range(0, len(data), EVENT.size):
                _, _, kind, code, value = EVENT.unpack_from(data, off)
                if kind == 0 and code == 3:
                    self.dropped = True
                    self.hold.reset(True)
                    log('INPUT_OVERRUN', device=self.path)
                elif kind == 0 and code == 0 and self.dropped:
                    self.dropped = False
                    self.hold.reset(self.pressed())
                elif kind == 1 and code == KEY and value == 1 and not self.dropped:
                    # A fresh press starts the hold; the state poll below is
                    # authoritative and also handles missed release events.
                    pass


def devices():
    return iter(hotkey_devices(KEY, prefer_physical=False))


def stop(_sig, _frame):
    global STOP
    STOP = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    selector = selectors.DefaultSelector()
    inputs = {}
    action = HoldAction()
    started = last = time.monotonic()
    next_scan = 0.0
    log('PANEL_HOTKEY_READY', key=KEY, seconds=1.5)
    try:
        while not STOP:
            now = time.monotonic()
            if now >= next_scan:
                next_scan = now + 2
                current = set(devices())
                for path in list(inputs):
                    if path not in current:
                        selector.unregister(inputs[path].fd)
                        os.close(inputs.pop(path).fd)
                        log('INPUT_DETACHED', device=path)
                for path in current - inputs.keys():
                    try:
                        item = Input(path)
                        selector.register(item.fd, selectors.EVENT_READ, item)
                        inputs[path] = item
                        log('INPUT_ATTACHED', device=path,
                            waiting_release=not item.hold.armed)
                    except OSError as exc:
                        log('INPUT_OPEN_ERROR', device=path, error=str(exc))
            active_hold = any(i.hold.started is not None and not i.hold.fired
                              for i in inputs.values())
            ready = selector.select(0.05 if active_hold else 0.5)
            now = time.monotonic()
            failures = []
            for key, _ in ready:
                try:
                    key.data.read()
                except OSError:
                    failures.append(key.data.path)
            for path, item in inputs.items():
                if path in failures:
                    continue
                try:
                    down = item.pressed()
                    if now - last > 2.0:
                        item.hold.reset(down)
                    if not item.dropped and item.hold.sample(down, now):
                        if not args.dry_run:
                            action.fire(now)
                        else:
                            log('PANEL_OPEN_DRY_RUN')
                except OSError:
                    failures.append(path)
            for path in set(failures):
                selector.unregister(inputs[path].fd)
                os.close(inputs.pop(path).fd)
                log('INPUT_DISCONNECTED', device=path)
            last = now
    finally:
        for item in inputs.values():
            os.close(item.fd)
        selector.close()
        log('PANEL_HOTKEY_STOPPED')


if __name__ == '__main__':
    main()
