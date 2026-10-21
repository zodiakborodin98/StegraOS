#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Shared constants and small helpers for the M10T performance panel.

This module is used by both the root clock daemon and the unprivileged UI
processes.  It contains no policy decisions and never writes to sysfs.
"""
import json
import os
from pathlib import Path
import stat
import time

APP_DIR = Path('/usr/local/lib/steam-perf-panel')
HOME = Path('/home/steamos')
CONFIG_DIR = HOME / '.config' / 'steam-perf-panel'
SETTINGS_PATH = CONFIG_DIR / 'settings.json'
STATE_DIR = HOME / '.local' / 'state' / 'steam-perf-panel'
PANEL_PID = Path('/run/user/1000/steam-perf-panel.pid')
PANEL_LOCK = Path('/run/user/1000/steam-perf-panel.lock')
PANEL_APPLIED = Path('/run/user/1000/steam-perf-panel-applied')
PERF_SOCKET = Path('/run/steam-perf-panel/control.sock')
LOWMEM_SOCKET = Path('/run/user/1000/steam-lowmem-global.sock')
LOWMEM_STATE = HOME / '.local' / 'state' / 'steam-ui-lowmem'

DEFAULT_SETTINGS = {
    'schema': 2,
    'cpu_selected': None,
    'gpu_selected': None,
    'ram_selected': None,
    'profile': 'system',
    'custom': {'cpu_max': 1020000, 'gpu_max': 384000000},
    'global_lowmem': False,
}

LEGACY_PROFILE_VALUES = {
    'eco': (816000, 230400000),
    'balanced': (1020000, 384000000),
    'performance': (1963500, 768000000),
}

PROFILE_ORDER = ['system', 'eco', 'balanced', 'performance', 'custom']
PROFILE_LABELS = {
    'system': 'Системный',
    'eco': 'Экономичный',
    'balanced': 'Сбалансированный',
    'performance': 'Производительность',
    'custom': 'Пользовательский',
}


def log_event(prefix, event, **fields):
    record = dict(event=event, monotonic=round(time.monotonic(), 3), **fields)
    print(prefix + ' ' + json.dumps(record, sort_keys=True, ensure_ascii=False),
          flush=True)


def read_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _ensure_dir(path, mode=0o700):
    path = Path(path)
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError('unsafe directory: %s' % path)
    return path


def atomic_write_json(path, data, mode=0o600):
    path = Path(path)
    _ensure_dir(path.parent, 0o700)
    tmp = path.with_name('.%s.tmp.%d' % (path.name, os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, 'O_NOFOLLOW', 0), mode)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, mode)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def load_settings(path=SETTINGS_PATH):
    data = read_json(path, {})
    return normalise_settings(data)


def normalise_settings(data):
    if not isinstance(data, dict):
        data = {}
    result = json.loads(json.dumps(DEFAULT_SETTINGS))
    profile = data.get('profile')
    if profile in PROFILE_ORDER:
        result['profile'] = profile
    custom = data.get('custom')
    if isinstance(custom, dict):
        for key in ('cpu_max', 'gpu_max'):
            if isinstance(custom.get(key), int):
                result['custom'][key] = custom[key]
    # New independent-domain selection.
    cpu = data.get('cpu_selected')
    gpu = data.get('gpu_selected')
    ram = data.get('ram_selected')
    if 'cpu_selected' not in data and profile == 'custom':
        cpu = custom.get('cpu_max') if isinstance(custom, dict) else None
    elif 'cpu_selected' not in data and profile in LEGACY_PROFILE_VALUES:
        cpu = LEGACY_PROFILE_VALUES[profile][0]
    if 'gpu_selected' not in data and profile == 'custom':
        gpu = custom.get('gpu_max') if isinstance(custom, dict) else None
    elif 'gpu_selected' not in data and profile in LEGACY_PROFILE_VALUES:
        gpu = LEGACY_PROFILE_VALUES[profile][1]
    result['cpu_selected'] = cpu if isinstance(cpu, int) else None
    result['gpu_selected'] = gpu if isinstance(gpu, int) else None
    result['ram_selected'] = ram if isinstance(ram, int) else None
    result['global_lowmem'] = bool(data.get('global_lowmem', False))
    if result['cpu_selected'] is not None or result['gpu_selected'] is not None:
        result['profile'] = 'custom'
        result['custom'] = {
            'cpu_max': result['cpu_selected'] if result['cpu_selected'] is not None else DEFAULT_SETTINGS['custom']['cpu_max'],
            'gpu_max': result['gpu_selected'] if result['gpu_selected'] is not None else DEFAULT_SETTINGS['custom']['gpu_max'],
        }
    else:
        result['profile'] = 'system'
    return result


def save_settings(data, path=SETTINGS_PATH):
    cpu = data.get('cpu_selected')
    gpu = data.get('gpu_selected')
    ram = data.get('ram_selected')
    has_new = 'cpu_selected' in data or 'gpu_selected' in data or 'ram_selected' in data
    if not has_new:
        profile = data.get('profile')
        if profile == 'custom' and isinstance(data.get('custom'), dict):
            cpu = data['custom'].get('cpu_max')
            gpu = data['custom'].get('gpu_max')
        elif profile in LEGACY_PROFILE_VALUES:
            cpu, gpu = LEGACY_PROFILE_VALUES[profile]
    cpu = int(cpu) if isinstance(cpu, int) else None
    gpu = int(gpu) if isinstance(gpu, int) else None
    ram = int(ram) if isinstance(ram, int) else None
    legacy_profile = 'custom' if any(v is not None for v in (cpu, gpu, ram)) else 'system'
    clean = {
        'schema': 2,
        'cpu_selected': cpu,
        'gpu_selected': gpu,
        'ram_selected': ram,
        'profile': legacy_profile,
        'custom': {
            'cpu_max': cpu if cpu is not None else DEFAULT_SETTINGS['custom']['cpu_max'],
            'gpu_max': gpu if gpu is not None else DEFAULT_SETTINGS['custom']['gpu_max'],
        },
        'global_lowmem': bool(data.get('global_lowmem', False)),
    }
    atomic_write_json(path, clean)
    return clean


def proc_stat(pid):
    text = Path('/proc/%d/stat' % int(pid)).read_text()
    return text.rsplit(')', 1)[1].split()


def identity(pid):
    p = Path('/proc') / str(int(pid))
    fields = proc_stat(pid)
    return {
        'pid': int(pid),
        'start': fields[19],
        'ppid': int(fields[1]),
        'uid': p.stat().st_uid,
        'exe': os.readlink(p / 'exe'),
    }


def same(wanted):
    try:
        now = identity(wanted['pid'])
        return all(now[key] == wanted[key] for key in ('pid', 'start', 'uid', 'exe'))
    except (OSError, KeyError, IndexError):
        return False


def panel_pid():
    data = read_json(PANEL_PID, {})
    if not isinstance(data, dict):
        return None
    pid = data.get('pid')
    start = data.get('start')
    if not isinstance(pid, int) or not isinstance(start, str):
        return None
    try:
        now = identity(pid)
    except OSError:
        return None
    if now['start'] != start or now['uid'] != 1000:
        return None
    return now


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return ''


class Hold:
    """Generic hold detector: one action per press, release required."""

    def __init__(self, pressed=False, seconds=1.5):
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
