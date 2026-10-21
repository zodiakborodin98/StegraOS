#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Stegra Control — English performance sidebar for SteamOS on Switch Lite.

GTK3/X11 only.  It uses Gamescope's interactive overlay contract
(STEAM_OVERLAY + STEAM_INPUT_FOCUS), does not use CEF/Electron, does not take
screenshots and does not leave a hidden window grabbing input.
"""
import ctypes
import ctypes.util
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import socket
import struct
import sys
import threading
import time

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GdkX11', '3.0')
from gi.repository import Gtk, Gdk, GLib, GdkX11
import cairo

try:
    gi.require_version('PangoCairo', '1.0')
    from gi.repository import Pango, PangoCairo
    HAVE_PANGO = True
except (ValueError, ImportError):
    HAVE_PANGO = False

import catalog as catalog_module
from common import (
    Hold, boot_id, load_settings, log_event, panel_pid, read_json, save_settings,
    identity, PANEL_PID, PANEL_LOCK, PERF_SOCKET, LOWMEM_SOCKET, STATE_DIR,
)

WIDTH, HEIGHT = 1280, 720
PANEL_X, PANEL_W = 848, 432
TAB_X, TAB_W = 849, 111
CONTENT_X, CONTENT_W = 978, 286
ROW_H = 52
LIST_Y = 254
LIST_H = 364
FOOTER_Y = 660
TABS = [('cpu', 'CPU'), ('gpu', 'GPU'), ('ram', 'RAM'), ('steam', 'Steam')]
KEY_MINUS = 314
BTN_B = 304
BTN_A = 305
DPAD_UP, DPAD_DOWN, DPAD_LEFT, DPAD_RIGHT = 544, 545, 546, 547
ABS_Y = 1
KEY_BYTES = 96
EVENT = struct.Struct('@llHHi')
EVIOCGKEY = (2 << 30) | (KEY_BYTES << 16) | (ord('E') << 8) | 0x18
EVIOCGRAB = (1 << 30) | (4 << 16) | (ord('E') << 8) | 0x90
EVIOCGABS = lambda axis: (2 << 30) | (24 << 16) | (ord('E') << 8) | (0x40 + axis)
# Shared discovery supports Lite and rail/paired Joy-Cons.
import sys
sys.path.insert(0, '/usr/local/lib/steam-input')
from switch_devices import hotkey_devices, panel_device

CYAN = (0.12, 0.72, 0.94)
CYAN_DIM = (0.08, 0.40, 0.54)
BG = (0.02, 0.03, 0.05)
SIDE = (0.043, 0.066, 0.094)
SIDE_SOFT = (0.028, 0.043, 0.062)
ROW_SELECTED = (0.055, 0.20, 0.29)
TEXT = (0.90, 0.95, 1.0)
MUTED = (0.55, 0.64, 0.73)
DIM = (0.38, 0.46, 0.55)
AMBER = (0.78, 0.63, 0.35)
RED = (0.78, 0.40, 0.34)
GREEN = (0.35, 0.75, 0.55)


def say(event, **fields):
    log_event('STEGRA_PANEL', event, **fields)


def cpu_mhz(value):
    if value is None:
        return '—'
    return ('%.1f' % (value / 1000.0)).rstrip('0').rstrip('.') + ' MHz'


def gpu_mhz(value):
    if value is None:
        return '—'
    return ('%.1f' % (value / 1000000.0)).rstrip('0').rstrip('.') + ' MHz'


def ram_mhz(value_khz):
    if value_khz is None:
        return '—'
    return ('%.1f' % (value_khz / 1000.0)).rstrip('0').rstrip('.') + ' MHz'


def battery_percent():
    for candidate in ('/sys/class/power_supply/battery/capacity',
                      '/sys/class/power_supply/BAT0/capacity'):
        try:
            return int(Path(candidate).read_text().strip())
        except (OSError, ValueError):
            continue
    return None


class X11:
    def __init__(self):
        self.lib = ctypes.CDLL(ctypes.util.find_library('X11'))
        self.lib.XOpenDisplay.restype = ctypes.c_void_p
        self.lib.XInternAtom.restype = ctypes.c_ulong
        self.lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        self.lib.XChangeProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        self.lib.XFlush.argtypes = [ctypes.c_void_p]
        self.lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.dpy = self.lib.XOpenDisplay(None)
        if not self.dpy:
            raise RuntimeError('cannot open X display')

    def set_cardinal(self, xid, name, value):
        atom = self.lib.XInternAtom(self.dpy, name.encode(), 0)
        val = ctypes.c_ulong(int(value))
        self.lib.XChangeProperty(self.dpy, xid, atom, 6, 32, 0,
                                 ctypes.byref(val), 1)
        self.lib.XFlush(self.dpy)

    def close(self):
        if self.dpy:
            self.lib.XCloseDisplay(self.dpy)
            self.dpy = None


class Client:
    def __init__(self, path):
        self.path = Path(path)

    def request(self, payload, timeout=6.0):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(str(self.path))
            sock.sendall((json.dumps(payload) + '\n').encode())
            data = b''
            while b'\n' not in data and len(data) < 131072:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                data += chunk
            if not data:
                return {'ok': False, 'status': 'error', 'reason': 'empty response'}
            return json.loads(data.decode('utf-8').strip())
        except (OSError, ValueError) as exc:
            return {'ok': False, 'status': 'error', 'reason': str(exc)}
        finally:
            sock.close()


class Gamepad:
    def __init__(self, app, device=None):
        self.app = app
        self.path = device or self.find_device()
        self.fd = None
        self.stop = False
        self.thread = None
        self.grabbed = False
        self.hold = Hold(False, 1.5)
        self.next_stick = 0.0
        self.nav = {}
        self.skip_nav = False
        self.error = None

    @staticmethod
    def find_device():
        return panel_device()

    def open(self):
        if not self.path:
            raise RuntimeError('Nintendo controller not found; pair the Joy-Cons first')
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)

    def keys(self):
        bits = bytearray(KEY_BYTES)
        fcntl.ioctl(self.fd, EVIOCGKEY, bits, True)
        return bits

    def abs_value(self, axis):
        buf = bytearray(24)
        fcntl.ioctl(self.fd, EVIOCGABS(axis), buf, True)
        return struct.unpack('6i', buf)[0]

    def any_key(self):
        return any(self.keys())

    def idle(self):
        if self.any_key():
            return False
        return abs(self.abs_value(ABS_Y)) <= 9000

    def grab(self):
        fcntl.ioctl(self.fd, EVIOCGRAB, 1)
        self.grabbed = True

    def ungrab(self):
        if self.grabbed and self.fd is not None:
            try:
                fcntl.ioctl(self.fd, EVIOCGRAB, 0)
            except OSError:
                pass
            self.grabbed = False

    def close(self):
        self.stop = True
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        self.ungrab()
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def start(self):
        self.open()
        self.thread = threading.Thread(target=self.run, name='gamepad', daemon=True)
        self.thread.start()

    def run(self):
        try:
            deadline = time.monotonic() + 5
            while not self.stop and time.monotonic() < deadline:
                if self.idle():
                    break
                time.sleep(0.1)
            if not self.idle():
                self.error = 'Release the controller: a button or stick is held.'
                self.app.gamepad_error(self.error)
                return
            self.grab()
            self.app.gamepad_ready(self.path)
            pressed = bool(self.keys()[KEY_MINUS // 8] & (1 << (KEY_MINUS % 8)))
            self.hold.reset(pressed)
            nav_codes = {BTN_A: 'select', BTN_B: 'back', DPAD_UP: 'up',
                         DPAD_DOWN: 'down', DPAD_LEFT: 'left', DPAD_RIGHT: 'right'}
            while not self.stop:
                try:
                    ready, _, _ = select.select([self.fd], [], [], 0.05)
                except (OSError, ValueError):
                    break
                if ready:
                    try:
                        data = os.read(self.fd, EVENT.size * 128)
                    except BlockingIOError:
                        data = b''
                    except OSError:
                        break
                    if len(data) % EVENT.size:
                        break
                    for off in range(0, len(data), EVENT.size):
                        _, _, kind, code, value = EVENT.unpack_from(data, off)
                        if kind == 0 and code == 3:
                            self.hold.reset(True)
                            self.skip_nav = True
                try:
                    keys = self.keys()
                    minus = bool(keys[KEY_MINUS // 8] & (1 << (KEY_MINUS % 8)))
                except OSError:
                    break
                if self.skip_nav:
                    self.nav = {code: bool(keys[code // 8] & (1 << (code % 8)))
                                for code in nav_codes}
                    self.skip_nav = False
                else:
                    for code, action in nav_codes.items():
                        down = bool(keys[code // 8] & (1 << (code % 8)))
                        if down and not self.nav.get(code, False):
                            self.app.gamepad_action(action)
                        self.nav[code] = down
                now = time.monotonic()
                if self.hold.sample(minus, now):
                    self.app.gamepad_action('close')
                value = self.abs_value(ABS_Y)
                if value < -16000 or value > 16000:
                    if now >= self.next_stick:
                        self.next_stick = now + 0.35
                        self.app.gamepad_action('up' if value < 0 else 'down')
                else:
                    self.next_stick = 0.0
        except OSError as exc:
            self.error = str(exc)
            self.app.gamepad_error(self.error)
        finally:
            self.ungrab()


class App:
    def __init__(self, device=None):
        self.settings = load_settings()
        self.cpu_selected = self.settings.get('cpu_selected')
        self.gpu_selected = self.settings.get('gpu_selected')
        self.ram_selected = self.settings.get('ram_selected')
        self.global_lowmem = bool(self.settings.get('global_lowmem', False))
        self.zram_choice = None
        self.zram_initialized = False
        self.status = {}
        self.lowmem = {}
        self.catalog = None
        self.tab = 0
        self.focus = 'tabs'
        self.row = 0
        self.scroll = {0: 0, 1: 0, 2: 0}
        self.message = ''
        self.message_time = 0
        self.busy = False
        self.daemon_ok = False
        self.gamepad_state = 'opening' if not device else 'opening'
        self.gamepad_text = ''
        self.drag = None
        self.areas = []
        self.last_status_key = None
        self.last_minute = ''
        self.last_battery = None
        self.x11 = X11()
        self.pid_written = False
        self.window = Gtk.Window()
        self.window.set_title('Stegra Control')
        self.window.set_wmclass('stegra-control', 'StegraControl')
        self.window.set_decorated(False)
        # The GTK default visual is opaque even when Cairo draws rgba().
        # Use a real ARGB surface; never snapshot or replace the base app.
        visual = self.window.get_screen().get_rgba_visual()
        if visual is None:
            raise RuntimeError('ARGB overlay visual unavailable')
        self.window.set_visual(visual)
        self.window.set_app_paintable(True)
        self.window.set_default_size(WIDTH, HEIGHT)
        self.window.move(0, 0)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_keep_above(True)
        self.window.connect('destroy', self.on_destroy)
        self.window.connect('key-press-event', self.on_key)
        self.window.realize()
        xid = self.window.get_window().get_xid()
        self.x11.set_cardinal(xid, 'STEAM_OVERLAY', 1)
        self.x11.set_cardinal(xid, 'STEAM_INPUT_FOCUS', 1)
        self.area = Gtk.DrawingArea()
        self.area.set_can_focus(True)
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                             Gdk.EventMask.BUTTON_RELEASE_MASK |
                             Gdk.EventMask.POINTER_MOTION_MASK |
                             Gdk.EventMask.SCROLL_MASK)
        self.area.connect('draw', self.on_draw)
        self.area.connect('button-press-event', self.on_click)
        self.area.connect('button-release-event', self.on_release)
        self.area.connect('motion-notify-event', self.on_motion)
        self.area.connect('scroll-event', self.on_scroll)
        self.window.add(self.area)
        self.window.show_all()
        self.area.grab_focus()
        self.write_pid()
        say('PANEL_READY', xid=hex(xid), version='m10t6', visual_depth=visual.get_depth())
        self.gamepad = None
        if device != '--no-gamepad' and '--no-gamepad' not in sys.argv[1:]:
            self.gamepad = Gamepad(self, None if device in (None, '--no-gamepad') else device)
            try:
                self.gamepad.start()
            except (OSError, RuntimeError) as exc:
                self.gamepad_state = 'error'
                self.gamepad_text = str(exc)
                say('GAMEPAD_OPEN_FAILED', error=str(exc))
        else:
            self.gamepad_state = 'disabled'
        threading.Thread(target=self.status_loop, name='status', daemon=True).start()
        GLib.timeout_add(500, self.update_clock)
        self.focus_selected()

    # ---- lifecycle ----
    def write_pid(self):
        try:
            info = identity(os.getpid())
            STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(PANEL_PID, 'w', encoding='utf-8') as handle:
                json.dump({'pid': info['pid'], 'start': info['start']}, handle)
                handle.write('\n')
                os.fchmod(handle.fileno(), 0o600)
            self.pid_written = True
        except OSError as exc:
            say('PID_WRITE_FAILED', error=str(exc))

    def remove_pid(self):
        if not self.pid_written:
            return
        try:
            current = read_json(PANEL_PID, {})
            if current.get('pid') == os.getpid():
                PANEL_PID.unlink()
        except OSError:
            pass

    def update_clock(self):
        minute = time.strftime('%H:%M')
        battery = battery_percent()
        if minute != self.last_minute or battery != self.last_battery:
            self.last_minute = minute
            self.last_battery = battery
            self.area.queue_draw()
        return True

    def status_loop(self):
        perf = Client(PERF_SOCKET)
        low = Client(LOWMEM_SOCKET)
        while not getattr(self, 'closing', False):
            status = perf.request({'op': 'status'}, timeout=6.0)
            lowmem = low.request({'op': 'status'}, timeout=2.0)
            key = self.status_key(status, lowmem)
            GLib.idle_add(self.set_status, status, lowmem, key)
            time.sleep(2.0)

    @staticmethod
    def status_key(status, lowmem):
        if not isinstance(status, dict):
            status = {}
        return json.dumps({
            'status': status,
            'lowmem': {k: lowmem.get(k) for k in ('ok', 'policy', 'lease', 'game', 'cef_processes')}
            if isinstance(lowmem, dict) else None,
        }, sort_keys=True)

    def set_status(self, status, lowmem, key):
        if key == self.last_status_key:
            return False
        self.last_status_key = key
        was_ready = self.daemon_ok
        if isinstance(status, dict):
            self.status = status
            self.daemon_ok = bool(status.get('ok'))
            if status.get('catalog'):
                self.catalog = status['catalog']
        if self.daemon_ok and not was_ready:
            self.focus_selected()
            restore=status.get('startup_restore',{})
            if any(x.get('status')=='error' for x in restore.values()):
                self.notify('Saved profile restore failed. Check the service log.')
            elif any(x.get('status')=='restart_required' and x.get('requested')==getattr(self,d+'_selected',object())
                     for d,x in restore.items()):
                self.notify('Selected OC profile needs the StegraOS - OC entry.')
        if isinstance(lowmem, dict):
            self.lowmem = lowmem
        self.area.queue_draw()
        return False

    def on_destroy(self, _widget):
        self.closing = True
        if self.gamepad:
            self.gamepad.close()
        self.remove_pid()
        Gtk.main_quit()

    def close_panel(self, reason):
        if getattr(self, 'closing', False):
            return False
        self.closing = True
        say('PANEL_CLOSE', reason=reason)
        if self.gamepad:
            self.gamepad.close()
        self.remove_pid()
        try:
            self.window.destroy()
        except Exception:
            pass
        Gtk.main_quit()
        return False

    def gamepad_ready(self, path):
        self.gamepad_state = 'grabbed'
        self.gamepad_text = path
        say('GAMEPAD_GRABBED', device=path)

    def gamepad_error(self, text):
        self.gamepad_state = 'error'
        self.gamepad_text = text
        self.message = text
        self.message_time = time.monotonic()
        say('GAMEPAD_ERROR', error=text)
        GLib.idle_add(self.close_panel, 'gamepad-error')

    def gamepad_action(self, action):
        GLib.idle_add(self.handle_action, action)

    # ---- model ----
    def fallback_catalog(self):
        try:
            return catalog_module.load_catalog(
                '/usr/local/lib/steam-perf-panel/clock-tables-mhz.yaml')
        except (OSError, ValueError):
            return {'cpu': [], 'gpu': [], 'ram': []}

    def domain(self):
        return TABS[self.tab][0]

    def rows(self):
        domain = self.domain()
        if domain == 'steam':
            return [{'kind':'steam_toggle'}, {'kind':'zram_size'}, {'kind':'zram_apply'}]
        if not self.daemon_ok:
            return []
        data = self.catalog or self.fallback_catalog()
        rows = []

        def system_row():
            if domain == 'cpu':
                applied = self.status.get('applied', {}).get('cpu')
                current = self.status.get('cpu', {}).get('max')
                base = self.status.get('baseline', {}).get('cpu_max')
                active = applied is None
                return {
                    'kind': 'system', 'display': 'System default',
                    'sources': ['Linux baseline'], 'status': 'active' if active else 'available',
                    'reason': 'Restore the saved Linux baseline (%s).' % cpu_mhz(base),
                    'active': active, 'canonical': None,
                }
            if domain == 'gpu':
                applied = self.status.get('applied', {}).get('gpu')
                base = self.status.get('baseline', {}).get('gpu_max')
                return {
                    'kind': 'system', 'display': 'System default',
                    'sources': ['Linux baseline'], 'status': 'active' if applied is None else 'available',
                    'reason': 'Restore the saved Linux baseline (%s).' % gpu_mhz(base),
                    'active': applied is None, 'canonical': None,
                }
            boot = self.status.get('emc', {}).get('boot_ceiling_khz')
            base = self.status.get('baseline', {}).get('emc_max')
            active = boot is not None and base is not None and boot == base // 1000
            return {
                'kind': 'system', 'display': 'System default',
                'sources': ['Linux baseline'], 'status': 'active' if active else 'restart',
                'reason': 'Restore the saved Linux boot ceiling (%s).' % ram_mhz(base // 1000 if base else None),
                'active': active, 'canonical': None,
            }

        rows.append(system_row())
        for item in data.get(domain, []):
            row = dict(item)
            row['kind'] = 'value'
            if row.get('status') not in ('available', 'active', 'restart'):
                continue
            if row.get('canonical') is None:
                continue
            rows.append(row)
        # System default is a policy, not a second radio alias for its MHz.
        if rows[0].get('active'):
            for row in rows[1:]:
                if row.get('active'):
                    row['active'] = False
                    row['status'] = 'available'
        return rows

    def radio_selected(self, item):
        """Saved successful choice; moving the focus never moves this dot."""
        selected = self.selected_value()
        rows = self.rows()
        if selected is None:
            return item.get('kind') == 'system'
        if any(row.get('canonical') == selected for row in rows):
            return item.get('kind') == 'value' and item.get('canonical') == selected
        # An obsolete/unsupported saved choice is not an offered profile.
        return bool(item.get('active'))

    def selected_value(self):
        domain = self.domain()
        if domain == 'cpu':
            return self.cpu_selected
        if domain == 'gpu':
            return self.gpu_selected
        if domain == 'ram':
            return self.ram_selected
        return None

    def applied_value(self):
        domain = self.domain()
        if domain == 'cpu':
            return self.status.get('applied', {}).get('cpu')
        if domain == 'gpu':
            return self.status.get('applied', {}).get('gpu')
        if domain == 'ram':
            return self.status.get('emc', {}).get('boot_ceiling_khz')
        return None

    def ensure_row(self):
        rows = self.rows()
        if not rows:
            self.row = 0
            return
        self.row = max(0, min(self.row, len(rows) - 1))
        visible = max(1, LIST_H // ROW_H)
        off = self.scroll.get(self.tab, 0)
        if self.row < off:
            off = self.row
        elif self.row >= off + visible:
            off = self.row - visible + 1
        self.scroll[self.tab] = max(0, min(off, max(0, len(rows) - visible)))

    def focus_selected(self):
        rows = self.rows()
        target = self.selected_value()
        self.row = 0
        for idx, item in enumerate(rows):
            if (target is not None and item.get('canonical') == target) or \
                    (target is None and item.get('active')):
                self.row = idx
                break
        self.ensure_row()

    def switch_tab(self, delta):
        self.tab = (self.tab + delta) % len(TABS)
        self.focus_selected()
        self.message = ''
        self.area.queue_draw()

    def move_row(self, delta):
        rows = self.rows()
        if not rows:
            return
        self.row = max(0, min(self.row + delta, len(rows) - 1))
        self.ensure_row()
        self.area.queue_draw()

    def notify(self, text):
        self.message = text
        self.message_time = time.monotonic()
        self.area.queue_draw()

    def request(self, payload, callback):
        if self.busy:
            return
        self.busy = True
        self.area.queue_draw()

        def worker():
            result = Client(PERF_SOCKET).request(payload, timeout=12.0)
            GLib.idle_add(callback, result)
        threading.Thread(target=worker, daemon=True).start()

    def apply_current(self):
        domain = self.domain()
        if domain == 'steam':
            if self.row == 0: self.toggle_steam_ui()
            elif self.row == 1: self.cycle_zram(1)
            else: self.save_zram()
            return
        if not self.daemon_ok:
            self.notify('Clock service is unavailable.')
            return
        rows = self.rows()
        if not rows:
            return
        item = rows[self.row]
        if item.get('kind') == 'system':
            self.apply_system(domain)
            return
        status = item.get('status')
        if status in ('unavailable', 'requires_setup'):
            self.notify(item.get('reason') or 'This value is not available.')
            say('VALUE_REFUSED', domain=domain, value=item.get('canonical'),
                status=status)
            return
        value = item.get('canonical')
        if status == 'restart':
            # One common boot entry carries both GPU and RAM OC values.
            rate = value if domain == 'ram' else self.ram_selected
            self.request({'op': 'prepare_boot', 'kind': 'oc', 'rate': rate},
                         lambda result: self.prepared_done(domain, value, result))
            return
        self.request({'op': 'apply_domain', 'domain': domain, 'value': value},
                     lambda result: self.applied_done(domain, value, result))

    def apply_system(self, domain):
        if domain == 'ram':
            self.request({'op': 'apply_domain', 'domain': 'ram', 'value': None},
                         lambda result: self.system_done(domain, result))
            return
        self.request({'op': 'apply_domain', 'domain': domain, 'value': None},
                     lambda result: self.system_done(domain, result))

    def system_done(self, domain, result):
        self.busy = False
        if result.get('ok'):
            if domain == 'cpu':
                self.cpu_selected = None
            elif domain == 'gpu':
                self.gpu_selected = None
            elif domain == 'ram':
                self.ram_selected = None
            self.persist()
            self.notify('System default restored.')
            say('SYSTEM_APPLIED', domain=domain)
        elif result.get('status') == 'restart_required':
            if domain == 'ram' and result.get('rate'):
                self.request({'op': 'prepare_boot', 'kind': 'oc',
                              'rate': result.get('rate')},
                             lambda prep: self.prepared_done(domain, result.get('rate'), prep))
            else:
                self.notify(result.get('reason') or 'Restart required.')
        else:
            self.notify(result.get('reason') or 'Failed to restore System default.')
        self.area.queue_draw()

    def prepared_done(self, domain, value, result):
        self.busy = False
        if result.get('ok'):
            self.set_selected(domain, value)
            self.persist()
            self.notify('Hekate: StegraOS - OC (not applied yet).')
            say('BOOT_ENTRY_READY', domain=domain, value=value, path=result.get('path'))
        else:
            self.notify(result.get('reason') or 'Could not prepare the boot entry.')
        self.area.queue_draw()

    def applied_done(self, domain, value, result):
        self.busy = False
        if result.get('ok'):
            self.set_selected(domain, value)
            self.persist()
            self.notify('Applied: %s.' % self.value_label(domain, value))
            say('DOMAIN_APPLIED', domain=domain, value=value)
        else:
            self.notify(result.get('reason') or 'Apply failed; previous values preserved.')
            say('DOMAIN_REFUSED', domain=domain, value=value,
                status=result.get('status'), reason=result.get('reason'))
        self.area.queue_draw()

    def set_selected(self, domain, value):
        if domain == 'cpu':
            self.cpu_selected = value
        elif domain == 'gpu':
            self.gpu_selected = value
        elif domain == 'ram':
            self.ram_selected = value

    def persist(self):
        self.settings['cpu_selected'] = self.cpu_selected
        self.settings['gpu_selected'] = self.gpu_selected
        self.settings['ram_selected'] = self.ram_selected
        self.settings['global_lowmem'] = self.global_lowmem
        save_settings(self.settings)

    def value_label(self, domain, value):
        if domain == 'cpu':
            return cpu_mhz(value)
        if domain == 'gpu':
            return gpu_mhz(value)
        if domain == 'ram':
            return ram_mhz(value)
        return str(value)

    def toggle_steam_ui(self):
        self.global_lowmem = not self.global_lowmem
        self.settings['global_lowmem'] = self.global_lowmem
        save_settings(self.settings)
        self.notify('Applies to new games. The current game is not changed.')
        say('STEAM_UI_TOGGLED', enabled=self.global_lowmem)
        self.area.queue_draw()

    def handle_action(self, action):
        if getattr(self, 'closing', False):
            return False
        say('PANEL_ACTION', action=action, tab=TABS[self.tab][0], row=self.row)
        if action == 'close':
            self.close_panel('button')
        elif action == 'back':
            if self.focus == 'content':
                self.focus = 'tabs'
            else:
                self.close_panel('back')
        elif action == 'select':
            if self.focus == 'tabs':
                self.focus = 'content'
            else:
                self.apply_current()
        elif action in ('up', 'down'):
            delta = -1 if action == 'up' else 1
            if self.focus == 'tabs':
                self.switch_tab(delta)
            else:
                self.move_row(delta)
        elif action in ('left','right') and self.domain() == 'steam' and self.focus == 'content' and self.row == 1:
            self.cycle_zram(-1 if action == 'left' else 1)
        elif action == 'left':
            self.focus = 'tabs'
        elif action == 'right':
            self.focus = 'content'
        self.area.queue_draw()
        return False

    def on_key(self, _widget, event):
        key = event.keyval
        if key in (Gdk.KEY_Escape,):
            self.close_panel('escape')
            return True
        if key in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            self.handle_action('up')
            return True
        if key in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self.handle_action('down')
            return True
        if key in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
            self.handle_action('left')
            return True
        if key in (Gdk.KEY_Right, Gdk.KEY_KP_Right):
            self.handle_action('right')
            return True
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            self.handle_action('select')
            return True
        if key in (Gdk.KEY_Page_Up,):
            self.move_row(-max(1, LIST_H // ROW_H))
            return True
        if key in (Gdk.KEY_Page_Down,):
            self.move_row(max(1, LIST_H // ROW_H))
            return True
        if key in (Gdk.KEY_Tab,):
            self.focus = 'content' if self.focus == 'tabs' else 'tabs'
            self.area.queue_draw()
            return True
        return False

    def on_click(self, _widget, event):
        self.drag = None
        if event.button == 4:
            self.scroll_by(-1)
            return True
        if event.button == 5:
            self.scroll_by(1)
            return True
        for name, x, y, w, h in reversed(self.areas):
            if x <= event.x <= x + w and y <= event.y <= y + h:
                if name == 'close':
                    self.close_panel('click')
                elif name.startswith('tab:'):
                    self.tab = int(name.split(':', 1)[1])
                    self.focus = 'tabs'
                    self.focus_selected()
                    self.message = ''
                elif name == 'steam_toggle':
                    self.focus='content';self.row=0
                    self.toggle_steam_ui()
                elif name in ('zram_prev','zram_next'):
                    self.focus='content';self.row=1
                    self.cycle_zram(-1 if name=='zram_prev' else 1)
                elif name == 'zram_apply':
                    self.focus='content';self.row=2
                    self.save_zram()
                elif name.startswith('row:'):
                    self.focus = 'content'
                    self.row = int(name.split(':', 1)[1])
                    self.ensure_row()
                    self.message = ''
                    self.drag = {'y': event.y, 'offset': self.scroll.get(self.tab, 0)}
                elif name == 'apply':
                    self.handle_action('select')
                elif name == 'back':
                    self.handle_action('back')
                self.area.queue_draw()
                return True
        if LIST_Y <= event.y <= LIST_Y + LIST_H and CONTENT_X <= event.x <= CONTENT_X + CONTENT_W:
            self.drag = {'y': event.y, 'offset': self.scroll.get(self.tab, 0)}
            return True
        return False

    def on_motion(self, _widget, event):
        if self.drag is None:
            return False
        delta = int((self.drag['y'] - event.y) / ROW_H)
        if delta or self.scroll.get(self.tab, 0) != self.drag['offset']:
            self.set_scroll(self.drag['offset'] + delta)
            self.area.queue_draw()
        return True

    def on_release(self, _widget, _event):
        self.drag = None
        return False

    def on_scroll(self, _widget, event):
        if event.direction == Gdk.ScrollDirection.UP:
            self.scroll_by(-1)
        elif event.direction == Gdk.ScrollDirection.DOWN:
            self.scroll_by(1)
        return True

    def scroll_by(self, delta):
        self.focus = 'content'
        self.set_scroll(self.scroll.get(self.tab, 0) + delta)
        self.area.queue_draw()

    def set_scroll(self, offset):
        rows = self.rows()
        visible = max(1, LIST_H // ROW_H)
        off = max(0, min(offset, max(0, len(rows) - visible)))
        self.scroll[self.tab] = off
        self.row = max(off, min(self.row, min(len(rows) - 1, off + visible - 1))) if rows else 0

    # ---- drawing ----
    def text(self, cr, value, x, y, size, color=TEXT, bold=False, width=None):
        # All coordinates are TOP edges, all sizes are physical pixels.
        # Keep every label single-line and clip even when fonts differ.
        width = width if width is not None else max(1, WIDTH - x - 16)
        cr.save()
        cr.rectangle(x, y, width, size * 1.5)
        cr.clip()
        cr.set_source_rgb(*color)
        if HAVE_PANGO:
            layout = PangoCairo.create_layout(cr)
            desc = Pango.FontDescription('Sans')
            desc.set_absolute_size(size * Pango.SCALE)
            if bold:
                desc.set_weight(Pango.Weight.BOLD)
            layout.set_font_description(desc)
            layout.set_text(str(value), -1)
            layout.set_single_paragraph_mode(True)
            layout.set_height(-1)
            if width:
                layout.set_width(int(width * Pango.SCALE))
                layout.set_ellipsize(Pango.EllipsizeMode.END)
            cr.move_to(x, y)
            PangoCairo.show_layout(cr, layout)
        else:
            cr.select_font_face('Sans', 0, 1 if bold else 0)
            cr.set_font_size(size)
            cr.move_to(x, y + size)
            cr.show_text(str(value))
        cr.restore()

    def rounded(self, cr, x, y, w, h, radius=12):
        cr.new_sub_path()
        cr.arc(x + w - radius, y + radius, radius, -1.5708, 0)
        cr.arc(x + w - radius, y + h - radius, radius, 0, 1.5708)
        cr.arc(x + radius, y + h - radius, radius, 1.5708, 3.1416)
        cr.arc(x + radius, y + radius, radius, 3.1416, 4.7124)
        cr.close_path()

    def draw_cpu_icon(self, cr, x, y):
        cr.set_line_width(1.6)
        cr.set_source_rgb(*MUTED)
        cr.rectangle(x + 5, y + 5, 14, 14)
        cr.stroke()
        for dx in (8, 12, 16):
            cr.move_to(x + dx, y + 2)
            cr.line_to(x + dx, y + 5)
            cr.move_to(x + dx, y + 19)
            cr.line_to(x + dx, y + 22)
            cr.move_to(x + 2, y + dx)
            cr.line_to(x + 5, y + dx)
            cr.move_to(x + 19, y + dx)
            cr.line_to(x + 22, y + dx)
        cr.stroke()

    def draw_gpu_icon(self, cr, x, y):
        cr.set_line_width(1.6)
        cr.set_source_rgb(*MUTED)
        cr.rectangle(x + 3, y + 6, 19, 13)
        cr.stroke()
        cr.new_sub_path()
        cr.arc(x + 10, y + 12.5, 3, 0, 6.283)
        cr.stroke()
        for dx in (6, 11, 16):
            cr.move_to(x + dx, y + 19)
            cr.line_to(x + dx, y + 22)
        cr.stroke()

    def draw_ram_icon(self, cr, x, y):
        cr.set_line_width(1.6)
        cr.set_source_rgb(*MUTED)
        cr.rectangle(x + 3, y + 7, 19, 10)
        cr.stroke()
        for dx in (7, 11, 15, 19):
            cr.move_to(x + dx, y + 5)
            cr.line_to(x + dx, y + 7)
            cr.move_to(x + dx, y + 17)
            cr.line_to(x + dx, y + 19)
        cr.stroke()

    def draw_steam_icon(self, cr, x, y):
        cr.set_line_width(1.5)
        cr.set_source_rgb(*MUTED)
        cr.rectangle(x + 3, y + 5, 18, 14)
        cr.stroke()
        cr.move_to(x + 3, y + 9)
        cr.line_to(x + 21, y + 9)
        cr.stroke()
        cr.new_sub_path()
        cr.arc(x + 7, y + 7, 0.9, 0, 6.283)
        cr.fill()

    def icon(self, cr, domain, x, y):
        if domain == 'cpu':
            self.draw_cpu_icon(cr, x, y)
        elif domain == 'gpu':
            self.draw_gpu_icon(cr, x, y)
        elif domain == 'ram':
            self.draw_ram_icon(cr, x, y)
        else:
            self.draw_steam_icon(cr, x, y)

    def status_word(self, status):
        return {
            'active': 'Active',
            'available': 'Ready',
            'restart': 'Restart',
            'requires_setup': 'Setup needed',
            'unavailable': 'Unavailable',
        }.get(status, status)

    def status_color(self, status):
        if status == 'active':
            return CYAN
        if status == 'available':
            return TEXT
        if status == 'restart':
            return AMBER
        if status == 'requires_setup':
            return AMBER
        return DIM

    def on_draw(self, _widget, cr):
        self.areas = []
        self.ensure_row()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        # Right sidebar.
        cr.set_source_rgb(*SIDE)
        cr.rectangle(PANEL_X, 0, PANEL_W, HEIGHT)
        cr.fill()
        cr.set_source_rgba(*CYAN, 0.9)
        cr.rectangle(PANEL_X, 0, 2, HEIGHT)
        cr.fill()
        cr.set_source_rgba(0.15, 0.19, 0.24, 1.0)
        cr.rectangle(865, 80, 398, 1)
        cr.fill()
        cr.rectangle(865, 163, 398, 1)
        cr.fill()
        cr.rectangle(960, 164, 1, 491)
        cr.fill()
        # Header.
        self.text(cr, 'Stegra Control', 870, 18, 22, TEXT, bold=True, width=235)
        self.text(cr, 'APPLIED LIMITS', 870, 50, 11, MUTED, width=235)
        self.draw_battery(cr)
        minute = time.strftime('%H:%M')
        self.text(cr, minute, 1214, 29, 14, (0.82, 0.86, 0.92))
        self.draw_applied_summary(cr)
        # Category column.
        cr.set_source_rgb(*SIDE_SOFT)
        cr.rectangle(TAB_X, 164, TAB_W, 491)
        cr.fill()
        for idx, (domain, label) in enumerate(TABS):
            y = 180 + idx * 64
            selected = idx == self.tab
            if selected:
                cr.set_source_rgba(0.055, 0.20, 0.29, 1.0)
                self.rounded(cr, 858, y, 96, 54, 5)
                cr.fill()
                cr.set_source_rgba(*(CYAN if self.focus == 'tabs' else CYAN_DIM), 1.0)
                self.rounded(cr, 858, y, 96, 54, 5)
                cr.set_line_width(2)
                cr.stroke()
            self.areas.append(('tab:%d' % idx, 858, y, 96, 54))
            self.icon(cr, domain, 870, y + 17)
            self.text(cr, label, 902, y + 17, 18 if domain != 'steam' else 12,
                      TEXT if selected else MUTED, width=50)
        # Content.
        domain = self.domain()
        if domain == 'steam':
            self.draw_steam(cr)
        else:
            self.draw_domain(cr, domain)
        # Footer.
        cr.set_source_rgba(0.15, 0.19, 0.24, 1.0)
        cr.rectangle(TAB_X, 655, PANEL_W - 18, 1)
        cr.fill()
        self.circle_text(cr, 882, 686, 'B', 'Back')
        self.circle_text(cr, 1158, 686, 'A',
                         'Open' if self.focus == 'tabs' else (('Toggle','Next','Save')[self.row] if domain == 'steam' else 'Apply'))
        self.areas.append(('back', 860, 668, 120, 36))
        self.areas.append(('apply', 1136, 668, 120, 36))
        if self.message:
            self.text(cr, self.message, 870, 628, 12, AMBER, width=386)
        elif not self.daemon_ok:
            self.text(cr, 'Backend unavailable. No clock changes are performed.',
                      870, 628, 12, RED, width=386)
        elif domain != 'steam':
            rows = self.rows()
            if rows:
                self.text(cr, rows[self.row].get('reason') or '', 870, 628, 12, MUTED, width=386)

    def applied_summary(self):
        if not self.daemon_ok:
            return [(name, '—', 'Unavailable') for name in ('CPU', 'GPU', 'RAM')]
        applied = self.status.get('applied', {})
        return [
            ('CPU', cpu_mhz(self.status.get('cpu', {}).get('max')),
             'System' if applied.get('cpu') is None else 'Limit'),
            ('GPU', gpu_mhz(self.status.get('gpu', {}).get('max')),
             'System' if applied.get('gpu') is None else 'Limit'),
            ('RAM', ram_mhz(self.status.get('emc', {}).get('boot_ceiling_khz')), 'Boot ceiling'),
        ]

    def draw_applied_summary(self, cr):
        for i, (domain, value, mode) in enumerate(self.applied_summary()):
            x = 870 + i * 130
            self.text(cr, domain, x, 91, 11, MUTED, width=124)
            self.text(cr, value, x, 110, 16, TEXT, bold=True, width=124)
            self.text(cr, mode, x, 137, 10, MUTED, width=124)

    def draw_battery(self, cr):
        pct = battery_percent()
        cr.set_line_width(1.4)
        cr.set_source_rgb(0.78, 0.82, 0.88)
        cr.rectangle(1122, 26, 22, 12)
        cr.stroke()
        cr.rectangle(1144, 30, 2, 4)
        cr.fill()
        if pct is not None:
            width = max(0, min(15, int(round(15 * pct / 100.0))))
            if width:
                cr.rectangle(1125, 29, width, 6)
                cr.fill()
            self.text(cr, '%d%%' % pct, 1154, 29, 14, (0.82, 0.86, 0.92), width=52)
        else:
            self.text(cr, '—', 1154, 29, 14, MUTED, width=52)

    def circle_text(self, cr, x, y, letter, label):
        cr.set_source_rgb(0.85, 0.90, 0.95)
        cr.new_sub_path()
        cr.arc(x, y, 11, 0, 6.283)
        cr.fill()
        self.text(cr, letter, x - 5, y - 9, 14, (0.06, 0.10, 0.15), bold=True, width=12)
        self.text(cr, label, x + 21, y - 11, 17, TEXT, width=80)

    def draw_header_lines(self, cr, domain):
        if domain == 'ram':
            current = ram_mhz((self.status.get('emc', {}).get('cur') or 0) // 1000)
            limit = ram_mhz(self.applied_value())
            title = 'RAM boot ceiling'
        else:
            fmt = cpu_mhz if domain == 'cpu' else gpu_mhz
            current = fmt(self.status.get(domain, {}).get('cur'))
            limit = fmt(self.status.get(domain, {}).get('max'))
            title = domain.upper() + ' frequency limit'
        self.text(cr, title, CONTENT_X, 182, 20, TEXT, bold=True, width=CONTENT_W)
        self.text(cr, 'Current  ' + current, CONTENT_X, 216, 14, MUTED, width=CONTENT_W)

    def draw_domain(self, cr, domain):
        self.draw_header_lines(cr, domain)
        cr.set_source_rgb(0.15, 0.19, 0.24)
        cr.rectangle(CONTENT_X, 242, CONTENT_W, 1)
        cr.fill()
        rows = self.rows()
        visible = max(1, LIST_H // ROW_H)
        off = self.scroll.get(self.tab, 0)
        # Only complete rows, with a clip that also bounds hit targets.
        cr.save()
        cr.rectangle(CONTENT_X - 6, LIST_Y, CONTENT_W + 8, visible * ROW_H)
        cr.clip()
        for idx in range(off, min(len(rows), off + visible)):
            item = rows[idx]
            y = LIST_Y + (idx - off) * ROW_H
            focused = idx == self.row
            chosen = self.radio_selected(item)
            if focused:
                cr.set_source_rgb(*ROW_SELECTED)
                self.rounded(cr, CONTENT_X - 6, y, CONTENT_W + 4, 48, 6)
                cr.fill()
                if self.focus == 'content':
                    cr.set_source_rgb(*CYAN)
                    cr.set_line_width(1.5)
                    self.rounded(cr, CONTENT_X - 5, y + 1, CONTENT_W + 2, 46, 6)
                    cr.stroke()
            cx, cy = CONTENT_X + 12, y + 24
            cr.set_line_width(1.8)
            cr.set_source_rgb(*(CYAN if chosen else DIM))
            cr.new_sub_path()
            cr.arc(cx, cy, 8, 0, 6.2832)
            cr.stroke()
            if chosen:
                cr.new_sub_path()
                cr.arc(cx, cy, 4, 0, 6.2832)
                cr.fill()
            color = DIM if item.get('status') == 'unavailable' else TEXT
            caption = 'Applied' if item.get('active') else \
                      'Choose matching entry in Hekate' if item.get('status') == 'restart' else ''
            self.text(cr, item.get('display', ''), CONTENT_X + 30, y + (5 if caption else 13),
                      17, color, width=CONTENT_W - 44)
            if caption:
                self.text(cr, caption, CONTENT_X + 30, y + 29, 10,
                          AMBER if item.get('status') == 'restart' else MUTED, width=CONTENT_W - 44)
            self.areas.append(('row:%d' % idx, CONTENT_X - 6, y, CONTENT_W + 4, 48))
        cr.restore()
        if len(rows) > visible:
            cr.set_source_rgb(0.15, 0.20, 0.26)
            cr.rectangle(1268, LIST_Y, 3, visible * ROW_H)
            cr.fill()
            thumb_h = max(24, int(visible * ROW_H * visible / len(rows)))
            thumb_y = LIST_Y + int((visible * ROW_H - thumb_h) * off / max(1, len(rows) - visible))
            cr.set_source_rgb(*CYAN_DIM)
            self.rounded(cr, 1268, thumb_y, 3, thumb_h, 1)
            cr.fill()

    def zram_label(self):
        if self.zram_choice is None: return 'System (3 GiB)'
        return 'Off' if self.zram_choice == 0 else '%s GiB' % self.zram_choice

    def cycle_zram(self, delta):
        values=[None,0,1,2,3,4]
        index=values.index(self.zram_choice) if self.zram_choice in values else 0
        self.zram_choice=values[(index+delta)%len(values)]
        self.area.queue_draw()

    def save_zram(self):
        if not self.daemon_ok:
            self.notify('Backend unavailable. ZRAM was not saved.')
            return
        def done(result):
            self.busy=False
            if result.get('ok'):
                self.status['zram']=result['zram']
                self.notify('Saved. Applies after a full system reboot.')
            else: self.notify(result.get('reason') or 'ZRAM setting was not saved.')
            self.area.queue_draw()
            return False
        self.request({'op':'zram_config','size_gib':self.zram_choice},done)

    def draw_steam(self, cr):
        z=self.status.get('zram',{})
        if not self.zram_initialized and 'configured_size_gib' in z:
            self.zram_choice=z['configured_size_gib'];self.zram_initialized=True
        self.text(cr,'Steam UI & memory',CONTENT_X,182,20,TEXT,bold=True,width=CONTENT_W)
        def box(y,h,focused):
            cr.set_source_rgb(*ROW_SELECTED);self.rounded(cr,CONTENT_X,y,CONTENT_W-8,h,6);cr.fill()
            if focused and self.focus=='content':
                cr.set_source_rgb(*CYAN);cr.set_line_width(1.5)
                self.rounded(cr,CONTENT_X,y,CONTENT_W-8,h,6);cr.stroke()
        box(224,48,self.row==0)
        self.areas.append(('steam_toggle',CONTENT_X,224,CONTENT_W-8,48))
        self.text(cr,'Unload webhelper',CONTENT_X+12,238,15,TEXT,width=158)
        self.text(cr,'ON' if self.global_lowmem else 'OFF',CONTENT_X+183,238,15,CYAN if self.global_lowmem else MUTED,width=75)
        self.text(cr,'Applies to new games. Steam menus need webhelper.',CONTENT_X,281,11,MUTED,width=CONTENT_W)
        self.text(cr,'ZRAM compressed swap',CONTENT_X,315,18,TEXT,bold=True,width=CONTENT_W)
        box(350,48,self.row==1)
        self.areas.extend([('zram_prev',CONTENT_X,350,42,48),('zram_next',CONTENT_X+CONTENT_W-50,350,42,48)])
        self.text(cr,'<',CONTENT_X+14,364,18,CYAN,width=22)
        self.text(cr,self.zram_label(),CONTENT_X+49,366,15,TEXT,bold=True,width=175)
        self.text(cr,'>',CONTENT_X+CONTENT_W-36,364,18,CYAN,width=22)
        box(410,42,self.row==2)
        self.areas.append(('zram_apply',CONTENT_X,410,CONTENT_W-8,42))
        self.text(cr,'Save for next boot',CONTENT_X+12,422,14,TEXT,width=CONTENT_W-28)
        gib=1024**3;mib=1024**2
        active='%.1f GiB' % (z.get('size_bytes',0)/gib) if z.get('active') else 'Off'
        self.text(cr,'Active: %s  |  Used: %.0f MiB' % (active,z.get('used_bytes',0)/mib),CONTENT_X,468,12,MUTED,width=CONTENT_W)
        self.text(cr,'RAM used: %.0f MiB  |  %s' % (z.get('ram_bytes',0)/mib,z.get('algorithm','unavailable').upper()),CONTENT_X,491,12,MUTED,width=CONTENT_W)
        saved=z.get('configured_size_gib')
        label='System' if saved is None else 'Off' if saved==0 else '%s GiB'%saved
        self.text(cr,'Saved: '+label+(' (reboot pending)' if z.get('pending') else ''),CONTENT_X,518,12,CYAN,width=CONTENT_W)
        self.text(cr,'Applies after a full system reboot.',CONTENT_X,546,11,AMBER,width=CONTENT_W)
        self.text(cr,'Restarting Steam is not enough.',CONTENT_X,567,11,MUTED,width=CONTENT_W)
        if z.get('error'):self.text(cr,'ZRAM: '+z['error'],CONTENT_X,591,11,RED,width=CONTENT_W)


class InstanceLock:
    def __init__(self):
        self.fd = None

    def acquire(self):
        self.fd = os.open(PANEL_LOCK, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            os.close(self.fd)
            return False


def main():
    device = None
    args = sys.argv[1:]
    if '--device' in args:
        idx = args.index('--device')
        if idx + 1 < len(args):
            device = args[idx + 1]
    if '--no-gamepad' in args:
        device = '--no-gamepad'
    lock = InstanceLock()
    if not lock.acquire():
        print('another panel instance is running', file=sys.stderr)
        return 0
    try:
        app = App(device=device)
    except Exception as exc:
        print('panel start failed: %s' % exc, file=sys.stderr)
        return 23
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: GLib.idle_add(app.close_panel, 'signal'))
    Gtk.main()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
