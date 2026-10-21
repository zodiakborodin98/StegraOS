#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Global, game-agnostic CEF unload policy for M10T.

The daemon watches Steam game processes (SteamAppId/SteamGameId in /proc), not
a per-AppID list.  On the first focused game after the global switch was on at
game start it creates the same kernel flock lease used by the accepted
steam-lowmem gate, then kills only the qualified CEF processes of the current
Steam session.  The lease is released when the last game process exits, which
lets the existing CEF gate restore the UI.

The switch is snapshotted at game start.  Toggling it during a game never
changes that game's CEF state.  Old per-game launch wrappers are pass-through
in M10T and cannot create a second lease.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

from common import (
    LOWMEM_SOCKET, log_event, load_settings, save_settings, identity, same,
)

STEAM_EXE = '/run/initramfs/mounts/data/steam-gm/m7g-seed-1/steam/steamrtarm64/steam'
CEF_EXE = '/home/steamos/steamos-switch-dev/steam-zink-baseline-540p/cef/steamwebhelper'
HOME = Path('/home/steamos')
STATE_DIR = HOME / '.local' / 'state' / 'steam-perf-panel'
LOG_PATH = STATE_DIR / 'lowmem-global.log'
LOCK_PATH = Path('/run/user/1000/steam-lowmem-global.lock')
STOP = False
STATE = {
    'ok': True,
    'policy': False,
    'game': {'active': False},
    'lease': False,
    'cef_processes': None,
    'last_event': 'starting',
    'pid': os.getpid(),
}
STATE_LOCK = threading.Lock()


def runtime_dir():
    p = HOME / '.local' / 'state'
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    if p.is_symlink() or p.stat().st_uid != os.getuid():
        raise RuntimeError('owned shared state directory missing')
    d = p / 'steam-ui-lowmem'
    d.mkdir(mode=0o700, exist_ok=True)
    if d.is_symlink() or d.stat().st_uid != os.getuid() or \
            stat.S_IMODE(d.stat().st_mode) != 0o700:
        raise RuntimeError('unsafe lowmem state directory')
    return d


def set_last(event):
    with STATE_LOCK:
        STATE['last_event'] = event


def log(event, **fields):
    record = dict(event=event, monotonic=round(time.monotonic(), 3),
                  pid=os.getpid(), **fields)
    text = json.dumps(record, sort_keys=True, ensure_ascii=False) + '\n'
    state = json.dumps(record, sort_keys=True, ensure_ascii=False)
    print('STEAM_LOWMEM_GLOBAL ' + text.rstrip(), flush=True)
    set_last(state)
    try:
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 262144:
            os.replace(LOG_PATH, LOG_PATH.with_suffix('.log.1'))
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND |
                     os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
                os.write(fd, text.encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def env_of(pid):
    try:
        raw = (Path('/proc') / str(pid) / 'environ').read_bytes()
    except OSError:
        return {}
    return dict(item.split(b'=', 1) for item in raw.split(b'\0') if b'=' in item)


def steam_identity():
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            item = identity(int(p.name))
            if item['uid'] != os.getuid() or item['exe'] != STEAM_EXE:
                continue
            token = env_of(item['pid']).get(b'STEAM_ZINK_TOKEN', b'')
            if re.fullmatch(b'[0-9a-fA-F]{32}', token):
                return item, token
        except OSError:
            continue
    return None, None


def game_processes():
    """Return {appid: [identity...]} for real Steam game processes.

    Steam itself, CEF, reaper and the M10T/steam-lowmem wrappers are excluded.
    No AppID is hard-coded; 0 and the Steam UI appid 769 are not games.
    """
    result = {}
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            item = identity(int(p.name))
            if item['uid'] != os.getuid():
                continue
            exe = item['exe']
            if exe == STEAM_EXE or 'steamwebhelper' in exe:
                continue
            if exe.endswith('/reaper') or 'steam-lowmem.py' in exe or \
                    'steam-perf-panel' in exe:
                continue
            env = env_of(item['pid'])
            appid = ''
            for key in (b'SteamAppId', b'SteamGameId', b'STEAM_COMPAT_APP_ID'):
                candidate = env.get(key, b'').decode('ascii', 'ignore')
                if candidate.isdigit() and candidate not in ('0', '769'):
                    appid = candidate
                    break
            if not appid:
                continue
            result.setdefault(appid, []).append(item)
        except (OSError, ValueError):
            continue
    return result


def focused_app():
    env = os.environ.copy()
    env['DISPLAY'] = ':0'
    try:
        result = subprocess.run(['/usr/bin/xprop', '-root', 'GAMESCOPE_FOCUSED_APP'],
                                capture_output=True, text=True, timeout=2, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r'=\s*([0-9]+)', result.stdout)
    return match.group(1) if match else None


def helpers(token):
    found = []
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            item = identity(int(p.name))
            if item['uid'] == os.getuid() and item['exe'] == CEF_EXE and \
                    env_of(item['pid']).get(b'STEAM_ZINK_TOKEN') == token:
                found.append(item)
        except OSError:
            continue
    return found


def memory(items):
    total = 0
    count = 0
    for item in items:
        try:
            if not same(item):
                continue
            for line in (Path('/proc') / str(item['pid']) / 'smaps').read_text().splitlines():
                if line.startswith('Pss:'):
                    total += int(line.split()[1])
            count += 1
        except OSError:
            continue
    return {'cef_processes': count, 'pss_kib': total}


def still_running(item):
    try:
        fields = (Path('/proc') / str(item['pid']) / 'stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] == item['start'] and fields[0] not in ('Z', 'X')
    except FileNotFoundError:
        return False
    except (OSError, IndexError):
        return True


def unload(token):
    owned = helpers(token)
    before = memory(owned)
    if not owned:
        log('CEF_ABSENT_NOOP', before=before)
        return True
    log('CEF_UNLOAD_BEGIN', before=before, owned_pids=[p['pid'] for p in owned],
        method='SIGKILL')
    for item in owned:
        try:
            if same(item):
                os.kill(item['pid'], signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (helpers(token) or
                                           any(still_running(p) for p in owned)):
        time.sleep(0.1)
    after = memory(helpers(token))
    remaining = [p['pid'] for p in owned if still_running(p)]
    success = after['cef_processes'] == 0 and not remaining
    log('CEF_UNLOADED' if success else 'CEF_UNLOAD_INCOMPLETE',
        before=before, after=after, owned_remaining=remaining)
    return success


class Lease:
    def __init__(self, steam, appid):
        self.path = runtime_dir() / ('lease-' + secrets.token_hex(16))
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                          os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        os.write(self.fd, json.dumps({
            'steam': steam,
            'owner': identity(os.getpid()),
            'source': 'global',
            'appid': str(appid),
        }).encode())

    def close(self):
        try:
            self.path.unlink(missing_ok=True)
        finally:
            os.close(self.fd)


def cleanup_stale():
    try:
        for path in runtime_dir().glob('lease-*'):
            fd = None
            try:
                fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    record = json.loads(os.read(fd, 4096))
                except ValueError:
                    continue
                if record.get('source') == 'global':
                    path.unlink(missing_ok=True)
                    log('STALE_GLOBAL_LEASE_REMOVED', path=str(path))
            except OSError:
                continue
            finally:
                if fd is not None:
                    os.close(fd)
    except OSError as exc:
        log('STALE_CLEANUP_FAILED', error=str(exc))


def status():
    with STATE_LOCK:
        return json.loads(json.dumps(STATE))


def status_server(lock_fd):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        LOWMEM_SOCKET.unlink()
    except FileNotFoundError:
        pass
    server.bind(str(LOWMEM_SOCKET))
    os.chmod(LOWMEM_SOCKET, 0o600)
    server.listen(4)
    server.settimeout(1.0)
    log('STATUS_READY', socket=str(LOWMEM_SOCKET))
    while not STOP:
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            conn.settimeout(2.0)
            data = b''
            while len(data) < 65536 and b'\n' not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            request = json.loads(data.decode('utf-8').strip() or '{}')
            response = status() if request.get('op') == 'status' else {
                'ok': False, 'error': 'unsupported operation'}
            conn.sendall((json.dumps(response, ensure_ascii=False) + '\n').encode())
        except (OSError, ValueError):
            pass
        finally:
            conn.close()
    server.close()
    try:
        LOWMEM_SOCKET.unlink()
    except FileNotFoundError:
        pass


def current_cef_count():
    steam, token = steam_identity()
    if not steam:
        return None
    return memory(helpers(token))['cef_processes']


def release(current, reason):
    if current and current.get('lease') is not None:
        try:
            current['lease'].close()
        except OSError:
            pass
        current['lease'] = None
        log('GAME_END_UI_RELEASED', appid=current.get('appid'), reason=reason)
    with STATE_LOCK:
        STATE['lease'] = False
        STATE['game'] = {'active': False}


def stop(_sig, _frame):
    global STOP
    STOP = True


def main():
    lock_fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('another global lowmem instance is running')
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    cleanup_stale()
    server = threading.Thread(target=status_server, args=(lock_fd,),
                              name='status', daemon=True)
    server.start()
    startup_had_game = bool(game_processes())
    log('GLOBAL_LOWMEM_READY', startup_had_game=startup_had_game,
        policy=load_settings()['global_lowmem'])
    current = None
    startup = startup_had_game
    try:
        while not STOP:
            settings = load_settings()
            games = game_processes()
            focused = focused_app() if games else None
            now = time.monotonic()
            with STATE_LOCK:
                STATE['policy'] = bool(settings['global_lowmem'])
                STATE['cef_processes'] = current_cef_count()
                STATE['focused_app'] = focused
            if not games:
                if current is not None:
                    release(current, 'all-games-exited')
                    current = None
                startup = False
            else:
                if current is not None and current['appid'] not in games:
                    release(current, 'game-exited')
                    current = None
                if current is None:
                    appid = focused if focused in games else sorted(games)[0]
                    current = {
                        'appid': appid,
                        'started': now,
                        'policy': bool(settings['global_lowmem']) and not startup,
                        'attempted': False,
                        'lease': None,
                        'steam_retry': 0.0,
                        'logged_off': False,
                    }
                    startup = False
                    log('GAME_START', appid=appid, policy=current['policy'],
                        processes=[p['pid'] for p in games.get(appid, [])])
                    with STATE_LOCK:
                        STATE['game'] = {
                            'active': True, 'appid': appid,
                            'policy': current['policy'], 'lease': False,
                            'attempted': False,
                        }
                if not current['policy']:
                    if not current['logged_off']:
                        log('GAME_POLICY_OFF', appid=current['appid'])
                        current['logged_off'] = True
                elif not current['attempted']:
                    if focused == current['appid'] or now >= current['started'] + 600:
                        if now >= current['started'] + 600 and focused != current['appid']:
                            log('UNLOAD_SKIPPED_FOCUS_TIMEOUT', appid=current['appid'])
                            current['attempted'] = True
                        else:
                            steam, token = steam_identity()
                            if steam is None:
                                if now - current['steam_retry'] >= 5:
                                    current['steam_retry'] = now
                                    log('STEAM_IDENTITY_NOT_READY', appid=current['appid'])
                                if now >= current['started'] + 60:
                                    current['attempted'] = True
                                    log('UNLOAD_SKIPPED_STEAM_IDENTITY', appid=current['appid'])
                            else:
                                current['attempted'] = True
                                lease = Lease(steam, current['appid'])
                                current['lease'] = lease
                                log('GLOBAL_LEASE_CREATED', appid=current['appid'],
                                    path=str(lease.path))
                                with STATE_LOCK:
                                    STATE['lease'] = True
                                    STATE['game'] = {
                                        'active': True, 'appid': current['appid'],
                                        'policy': True, 'lease': True,
                                        'attempted': True,
                                    }
                                if not unload(token):
                                    try:
                                        lease.close()
                                    except OSError:
                                        pass
                                    current['lease'] = None
                                    with STATE_LOCK:
                                        STATE['lease'] = False
                                    log('GLOBAL_UNLOAD_FAILED', appid=current['appid'],
                                        action='lease-released')
                if current is not None:
                    with STATE_LOCK:
                        STATE['game'] = {
                            'active': True,
                            'appid': current['appid'],
                            'policy': current['policy'],
                            'lease': current['lease'] is not None,
                            'attempted': current['attempted'],
                        }
            time.sleep(1.0)
    finally:
        if current is not None and current.get('lease') is not None:
            try:
                current['lease'].close()
            except OSError:
                pass
        log('GLOBAL_LOWMEM_STOPPED')
        os.close(lock_fd)


if __name__ == '__main__':
    raise SystemExit(main())
