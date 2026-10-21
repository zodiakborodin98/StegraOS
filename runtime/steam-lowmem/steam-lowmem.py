#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Per-game CEF RAM unloading. Independent implementation inspired by Switchdeck.

No Steam files are replaced. An opt-in wrapper owns a kernel flock lease;
the CEF_PATH gate waits for its release then execs the unchanged Zink wrapper.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import time

REAL_CEF = '/home/steamos/steamos-switch-dev/steam-zink-unit15-baseline-20261018/cef-zink.sh'
STEAM_EXE = '/run/initramfs/mounts/data/steam-gm/m7g-seed-1/steam/steamrtarm64/steam'
CEF_EXE = '/home/steamos/steamos-switch-dev/steam-zink-baseline-540p/cef/steamwebhelper'
SETTINGS = '/home/steamos/.config/steam-perf-panel/settings.json'
GLOBAL_SOCKET = '/run/user/1000/steam-lowmem-global.sock'


def runtime():
    # HOME is shared into Steam Runtime; /run and /tmp can be private mounts.
    p = Path('/home/steamos/.local/state')
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    if p.is_symlink() or p.stat().st_uid != os.getuid():
        raise RuntimeError('owned shared state directory missing')
    d = p / 'steam-ui-lowmem'
    d.mkdir(mode=0o700, exist_ok=True)
    if d.is_symlink() or d.stat().st_uid != os.getuid() or stat.S_IMODE(d.stat().st_mode) != 0o700:
        raise RuntimeError('unsafe lowmem state directory')
    return d


def log(event, **fields):
    record = dict(event=event, monotonic=round(time.monotonic(), 3), pid=os.getpid(), **fields)
    text = json.dumps(record, sort_keys=True) + '\n'
    print('STEAM_LOWMEM ' + text.rstrip(), file=sys.stderr, flush=True)
    d = runtime()
    fd = os.open(d / 'events.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError('unsafe lowmem log')
        if info.st_size < 262144:
            os.write(fd, text.encode())
    finally:
        os.close(fd)


def identity(pid):
    p = Path('/proc') / str(pid)
    st = (p / 'stat').read_text().rsplit(')', 1)[1].split()
    return dict(pid=int(pid), start=st[19], ppid=int(st[1]), uid=p.stat().st_uid,
                exe=os.readlink(p / 'exe'))


def same(wanted):
    try:
        now = identity(wanted['pid'])
        return all(now[k] == wanted[k] for k in ['pid', 'start', 'uid', 'exe'])
    except OSError:
        return False


def env_of(pid):
    return dict(item.split(b'=', 1) for item in (Path('/proc') / str(pid) / 'environ').read_bytes().split(b'\0') if b'=' in item)


def client():
    pid = os.getppid()
    for _ in range(80):
        p = identity(pid)
        if p['uid'] != os.getuid():
            break
        if p['exe'] == STEAM_EXE:
            token = env_of(pid).get(b'STEAM_ZINK_TOKEN', b'')
            if not re.fullmatch(b'[0-9a-fA-F]{32}', token):
                raise RuntimeError('current Steam identity lacks session token')
            return p, token
        if p['ppid'] <= 1 or p['ppid'] == pid:
            break
        pid = p['ppid']
    raise RuntimeError('must be launched by the current Steam client')


class Lease:
    def __init__(self, steam):
        self.path = runtime() / ('lease-' + secrets.token_hex(16))
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        os.write(self.fd, json.dumps(dict(steam=steam, owner=identity(os.getpid()))).encode())

    def close(self):
        # Unlink before unlock so no new gate can observe an unlocked live lease.
        try:
            self.path.unlink(missing_ok=True)
        finally:
            os.close(self.fd)


def blocked(steam):
    for p in runtime().glob('lease-*'):
        fd = None
        try:
            fd = os.open(p, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 4096:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                continue  # no owner: a crashed wrapper must not block UI
            except BlockingIOError:
                record = json.loads(os.read(fd, 4096))
                if record.get('steam') == steam and same(record['owner']):
                    return True
        except (OSError, ValueError, KeyError, TypeError):
            continue
        finally:
            if fd is not None:
                os.close(fd)
    return False


def helpers(token):
    result = []
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            item = identity(int(p.name))
            if item['uid'] == os.getuid() and item['exe'] == CEF_EXE and env_of(item['pid']).get(b'STEAM_ZINK_TOKEN') == token:
                result.append(item)
        except OSError:
            continue
    return result


def memory(items):
    total = 0
    count = 0
    for item in items:
        try:
            if not same(item):
                continue
            # Linux4.9 has smaps, not necessarily smaps_rollup. PSS avoids
            # summing shared CEF libraries repeatedly as RSS would do.
            for line in (Path('/proc') / str(item['pid']) / 'smaps').read_text().splitlines():
                if line.startswith('Pss:'):
                    total += int(line.split()[1])
            count += 1
        except OSError:
            continue
    return dict(cef_processes=count, pss_kib=total)


def send(items, sig):
    for item in items:
        try:
            if same(item):
                os.kill(item['pid'], sig)
        except ProcessLookupError:
            pass


def still_running(item):
    try:
        fields = (Path('/proc') / str(item['pid']) / 'stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] == item['start'] and fields[0] not in ('Z', 'X')
    except FileNotFoundError:
        return False
    except (OSError, IndexError):
        return True  # inaccessible is UNKNOWN, never evidence of release


def unload(token):
    owned = helpers(token)
    before = memory(owned)
    if not owned or before['cef_processes'] != len(owned):
        log('CEF_UNLOAD_REFUSED_UNQUALIFIED_MEMORY', before=before)
        return False
    log('CEF_UNLOAD_BEGIN', before=before, owned_pids=[p['pid'] for p in owned], method='SIGKILL')
    # Same stop method as Switchdeck. No Chromium user-space shutdown/late GL
    # work is requested; only the already qualified CEF identities are killed.
    send(owned, signal.SIGKILL)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (helpers(token) or any(still_running(p) for p in owned)):
        time.sleep(0.1)
    after = memory(helpers(token))
    remaining = [p['pid'] for p in owned if still_running(p)]
    success = after['cef_processes'] == 0 and not remaining
    log('CEF_UNLOADED' if success else 'CEF_UNLOAD_INCOMPLETE', before=before,
        after=after, owned_remaining=remaining)
    return success


def focused(appid):
    try:
        result = subprocess.run(['/usr/bin/xprop', '-root', 'GAMESCOPE_FOCUSED_APP'],
                                capture_output=True, text=True, timeout=2)
        values = result.stdout.partition('=')[2].strip().split(',')
        return result.returncode == 0 and values[0].strip() == appid
    except (OSError, subprocess.TimeoutExpired):
        return False


def gate(args):
    # Always exec the accepted CEF wrapper when identity/state is unavailable.
    # Memory optimization failure must not prevent Steam UI from starting.
    try:
        log('CEF_GATE_INVOKED')
        steam, _ = client()
        waiting = blocked(steam)
        log('CEF_GATE_ENTER', blocked=waiting)
        if waiting:
            log('CEF_LAUNCH_WAITING')
        while blocked(steam):
            if not same(steam):
                return 0
            time.sleep(0.25)
        if waiting:
            log('CEF_LAUNCH_RESTORING')
    except (OSError, RuntimeError) as exc:
        print('STEAM_LOWMEM_GATE_BYPASS ' + str(exc), file=sys.stderr, flush=True)
    os.execv(REAL_CEF, [REAL_CEF, *args])


def policy():
    try:
        with open(SETTINGS, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def run(args):
    # M10T: the global switch owns unloading.  Old per-game LaunchOptions stay
    # valid and are intentionally pass-through, so they can never create a
    # second lease or unload CEF behind the global daemon's back.  When the
    # global switch is OFF, this also disables the old per-game optimization.
    if not args:
        raise RuntimeError('use: steam-lowmem %command%')
    data = policy()
    enabled = bool(data.get('global_lowmem', False))
    daemon = os.path.exists(GLOBAL_SOCKET)
    log('LOWMEM_WRAPPER_PASSTHROUGH', global_lowmem=enabled,
        global_daemon=daemon,
        reason='global switch controls CEF unloading')
    if enabled and not daemon:
        log('GLOBAL_LOWMEM_DAEMON_UNAVAILABLE',
            action='game runs normally; optimization skipped')
    child = subprocess.Popen(args)  # exact command/argv/env: no shell substitution
    def forward(sig, _frame):
        if child.poll() is None:
            child.send_signal(sig)
    for sig in [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]:
        signal.signal(sig, forward)
    rc = child.wait()
    return rc if rc >= 0 else 128 - rc


def main():
    try:
        if len(sys.argv) < 2:
            raise RuntimeError('expected gate or run')
        if sys.argv[1] == 'gate':
            return gate(sys.argv[2:])
        if sys.argv[1] == 'run':
            return run(sys.argv[2:])
        raise RuntimeError('unknown operation')
    except (OSError, RuntimeError, ValueError) as exc:
        print('STEAM_LOWMEM_REFUSED ' + str(exc), file=sys.stderr, flush=True)
        return 23


if __name__ == '__main__':
    raise SystemExit(main())
