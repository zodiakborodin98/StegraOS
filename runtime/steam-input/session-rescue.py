#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Capture hold -> one fixed systemd restart, independent of the GUI.

Reads the physical Lite gamepad without EVIOCGRAB or input injection. Never
logs other keys. Reconnect, overrun and startup-held states require release
before arming. Uses current kernel key state rather than trusting queued presses.
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

KEY = 309  # BTN_Z: measured Capture button on Nintendo Switch Lite Gamepad
# Shared discovery supports Lite and rail/paired Joy-Cons.
import sys
sys.path.insert(0, '/usr/local/lib/steam-input')
from switch_devices import hotkey_devices, panel_device
EVENT = struct.Struct('@llHHi')
KEY_BYTES = 96  # KEY_MAX=0x2ff
EVIOCGKEY = (2 << 30) | (KEY_BYTES << 16) | (ord('E') << 8) | 0x18
STOP = False


def log(event, **fields):
    print(json.dumps(dict(event=event, monotonic=round(time.monotonic(), 3),
                          **fields), sort_keys=True), flush=True)


class Hold:
    def __init__(self, pressed=False, seconds=3.0):
        self.seconds = seconds
        self.reset(pressed)

    def reset(self, pressed):
        self.armed = not pressed
        self.started = None
        self.fired = False

    def sample(self, pressed, now):
        if not pressed:
            self.reset(False)
            return False
        if not self.armed or self.fired:
            return False
        if self.started is None:
            self.started = now
        if now - self.started < self.seconds:
            return False
        self.fired = True
        return True


def pressed(fd):
    bits = bytearray(KEY_BYTES)
    fcntl.ioctl(fd, EVIOCGKEY, bits, True)
    return bool(bits[KEY // 8] & (1 << (KEY % 8)))


class Input:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            self.hold = Hold(pressed(self.fd))
        except BaseException:
            os.close(self.fd)
            raise
        self.dropped = False

    def read(self):
        # A finite read budget prevents continuous stick events starving timer.
        for _ in range(8):
            try:
                data = os.read(self.fd, EVENT.size * 128)
            except BlockingIOError:
                return
            if not data or len(data) % EVENT.size:
                raise OSError('invalid/disconnected evdev stream')
            for off in range(0, len(data), EVENT.size):
                _, _, kind, code, value = EVENT.unpack_from(data, off)
                if kind == 0 and code == 3:  # SYN_DROPPED
                    self.dropped = True
                    self.hold.reset(True)  # require a fresh release
                    log('INPUT_OVERRUN', device=self.path)
                elif kind == 0 and code == 0 and self.dropped:
                    self.dropped = False
                    self.hold.reset(pressed(self.fd))


class Action:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.next_allowed = 0.0

    def fire(self, now):
        if now < self.next_allowed:
            log('RESTART_COOLDOWN')
            return
        self.next_allowed = now + 30.0
        if self.dry_run:
            log('HOLD_CONFIRMED_DRY_RUN', seconds=3)
            return
        log('RESTART_REQUEST', unit='steam-cpu-session.service', seconds=3)
        try:
            result = subprocess.run(
                ['/usr/bin/systemctl', '--no-block', 'restart',
                 'steam-cpu-session.service'], capture_output=True,
                text=True, timeout=8)
            # Success here means the fixed job was queued, not UI recovery.
            log('RESTART_QUEUED' if result.returncode == 0 else 'RESTART_REFUSED',
                rc=result.returncode, stderr=result.stderr[:500])
        except subprocess.TimeoutExpired:
            log('RESTART_COMMAND_TIMEOUT')


def devices():
    return iter(hotkey_devices(KEY))


def stop(_sig, _frame):
    global STOP
    STOP = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--duration', type=float, default=0)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    selector = selectors.DefaultSelector()
    inputs = {}
    action = Action(args.dry_run)
    started = last = time.monotonic()
    next_scan = 0.0
    log('RESCUE_READY', key=KEY, seconds=3, dry_run=args.dry_run)
    try:
        while not STOP:
            now = time.monotonic()
            if args.duration > 0 and now - started >= args.duration:
                break
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
            ready = selector.select(0.05 if active_hold else 1.0)
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
                    down = pressed(item.fd)
                    if now - last > 2.0:
                        item.hold.reset(down)  # do not act on a scheduler stall
                    if not item.dropped and item.hold.sample(down, now):
                        action.fire(now)
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
        log('RESCUE_STOPPED')


if __name__ == '__main__':
    main()
