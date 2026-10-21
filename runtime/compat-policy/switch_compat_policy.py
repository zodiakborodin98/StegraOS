"""Read-only launch readiness. Exact environment pins are opt-in diagnostics.

This checks a minimum launch contract, not rendering correctness. Private tool
payload integrity remains the entrypoint's responsibility in both modes.
"""
from pathlib import Path
import gzip,hashlib,os,re,socket,stat,struct,subprocess,time

UNIT='steam-cpu-session.service'
MODE_KEY='SWITCH_COMPAT_MODE'
REQUIRED_GLOBALS={'wl_compositor':3,'gamescope_swapchain_factory_v2':1}

def mode(env):
    value=env.get(MODE_KEY,'normal')
    if value not in ('normal','strict'):
        raise RuntimeError('POLICY_MODE_INVALID: use normal or strict')
    return value

def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()

def elf_machine(path):
    with Path(path).open('rb') as f:header=f.read(20)
    if len(header)!=20 or header[:6]!=b'\x7fELF\x02\x01':
        raise RuntimeError('ENVIRONMENT_ARCHITECTURE: expected a 64-bit little-endian ELF: '+str(path))
    return struct.unpack_from('<H',header,18)[0]

def fingerprint(path):
    s=Path(path).stat()
    return s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns

def runtime_check(entry,kind,env,expected):
    entry=Path(entry);root=entry.parent
    name={'fex':'SteamLinuxRuntime_4-arm64','box64':'SteamLinuxRuntime_4'}.get(kind)
    if name is None or root.name!=name or entry.name!='_v2-entry-point' or root.is_symlink():
        raise RuntimeError('RUNTIME_LOCATION: unexpected runtime for '+str(kind))
    if not entry.resolve().is_relative_to(root.resolve()) or not entry.is_file() or not os.access(entry,os.X_OK):
        raise RuntimeError('RUNTIME_ENTRY_UNAVAILABLE: wait for Steam updates or verify runtime files')
    before=fingerprint(entry)
    if not 0<before[2]<=256*1024:
        raise RuntimeError('RUNTIME_ENTRY_INVALID: unreasonable launcher size')
    data=entry.read_bytes()
    if not data.startswith(b'#!/bin/sh\n') or b'--verb=' not in data:
        raise RuntimeError('RUNTIME_ENTRY_CONTRACT: expected Steam v2 shell launcher with --verb')
    try:
        with gzip.open(root/'mtree.txt.gz','rt',encoding='utf-8') as f:
            total=0;record=None
            for line in f:
                total+=len(line)
                if total>16*1024**2:break
                if line.startswith('./_v2-entry-point '):record=line;break
        if record is None:raise ValueError('launcher entry absent')
        fields=dict(v.split('=',1) for v in record.split()[1:] if '=' in v)
        actual=hashlib.sha256(data).hexdigest()
        if fields.get('type')!='file' or int(fields['size'])!=len(data) or fields.get('sha256',fields.get('sha256digest'))!=actual:
            raise ValueError('launcher differs from installed manifest')
    except (OSError,ValueError,KeyError,EOFError) as e:
        raise RuntimeError('RUNTIME_INSTALL_INCOMPLETE: '+str(e)+'; finish Steam update or verify runtime files') from e
    subprocess.run(['/bin/sh','-n',str(entry)],env={'PATH':'/usr/bin:/bin','LC_ALL':'C'},capture_output=True,check=True,timeout=5)
    run=root/'run'
    if not run.is_file() or not os.access(run,os.X_OK):
        raise RuntimeError('RUNTIME_RUN_UNAVAILABLE: verify runtime files')
    architecture='aarch64-linux-gnu' if kind=='fex' else 'x86_64-linux-gnu'
    if architecture not in run.read_text(encoding='utf-8'):
        raise RuntimeError('RUNTIME_GUEST_ARCHITECTURE: runtime does not advertise '+architecture)
    pv=Path(env.get('PRESSURE_VESSEL_PREFIX') or str(root/'pressure-vessel'))
    wrapper=pv/'bin/pressure-vessel-wrap'
    if not wrapper.is_file() or not os.access(wrapper,os.X_OK):
        raise RuntimeError('RUNTIME_PV_UNAVAILABLE: pressure-vessel launcher missing')
    machine=elf_machine(wrapper)
    if machine!=183:
        # The accepted x86 runtime includes a native ARM PV override. Do not
        # mistake the x86 guest runtime itself for a missing ARM environment.
        alt=root/'pressure-vessel-arm64/bin/pressure-vessel-wrap'
        if kind!='box64' or machine!=62 or not alt.is_file() or not os.access(alt,os.X_OK) or elf_machine(alt)!=183:
            raise RuntimeError('RUNTIME_HOST_ARCHITECTURE: native ARM pressure-vessel support unavailable')
    if mode(env)=='strict' and (not re.fullmatch('[0-9a-f]{64}',str(expected)) or actual!=expected):
        raise RuntimeError('STRICT_RUNTIME_PIN_MISMATCH: runtime changed; normal mode uses installed manifest')
    if fingerprint(entry)!=before:
        raise RuntimeError('RUNTIME_CHANGED_DURING_CHECK: wait for Steam update to finish')
    versions=root/'VERSIONS.txt'
    if not versions.is_file():raise RuntimeError('RUNTIME_VERSION_METADATA_MISSING')
    version=next((l.split()[1] for l in versions.read_text().splitlines() if l.startswith('depot\t')),'unknown')
    return dict(runtime=str(root),runtime_version=version,runtime_entry_sha256=actual,runtime_manifest_match=True)

def session_identity(unit):
    if unit!=UNIT:raise RuntimeError('SESSION_UNIT: unsupported Steam session')
    q=subprocess.run(['systemctl','show','--property=MainPID,ActiveState,SubState,RuntimeMaxUSec',unit],capture_output=True,text=True,check=True,timeout=10)
    fields=dict(l.split('=',1) for l in q.stdout.splitlines() if '=' in l)
    pid=int(fields.get('MainPID','0'))
    if pid<=0 or fields.get('ActiveState')!='active' or fields.get('SubState')!='running':
        raise RuntimeError('SESSION_NOT_READY: start the Steam session first')
    if fields.get('RuntimeMaxUSec')!='infinity':raise RuntimeError('SESSION_HAS_TIMER: ordinary games require an untimed session')
    proc=Path('/proc',str(pid))
    s=proc.joinpath('stat').read_text().rsplit(')',1)[1].split()
    if s[0]=='Z' or proc.stat().st_uid!=os.getuid():raise RuntimeError('SESSION_OWNER: compositor belongs to another user or exited')
    return pid,s[19]

def registry(socket_path,owner_pid,timeout=3):
    """Only get_registry+sync, no interface bind, surface, buffer or GPU call."""
    deadline=time.monotonic()+timeout
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
        client.settimeout(timeout);client.connect(str(socket_path))
        peer,uid,gid=struct.unpack('3i',client.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        tgid=next(int(l.split()[1]) for l in Path('/proc',str(peer),'status').read_text().splitlines() if l.startswith('Tgid:'))
        if uid!=os.getuid() or tgid!=owner_pid:
            raise RuntimeError('WAYLAND_OWNER: socket is not owned by the current Gamescope session')
        client.sendall(struct.pack('<III',1,(12<<16)|1,2)+struct.pack('<III',1,12<<16,3))
        pending=bytearray();total=0;globals={}
        while True:
            client.settimeout(max(.001,deadline-time.monotonic()))
            chunk=client.recv(8192)
            if not chunk:raise RuntimeError('WAYLAND_CLOSED: compositor closed readiness connection')
            pending.extend(chunk);total+=len(chunk)
            if total>65536:raise RuntimeError('WAYLAND_PROTOCOL: registry response exceeded bound')
            while len(pending)>=8:
                obj,header=struct.unpack_from('<II',pending);size=header>>16;opcode=header&0xffff
                if size<8 or size%4:raise RuntimeError('WAYLAND_PROTOCOL: malformed event')
                if len(pending)<size:break
                payload=bytes(pending[8:size]);del pending[:size]
                if obj==1 and opcode==0:raise RuntimeError('WAYLAND_PROTOCOL: display error')
                if obj==2 and opcode==0:
                    if len(payload)<12:raise RuntimeError('WAYLAND_PROTOCOL: truncated global')
                    length=struct.unpack_from('<I',payload,4)[0];padded=(length+3)&~3
                    if length<1 or 8+padded+4!=len(payload) or payload[8+length-1]!=0:
                        raise RuntimeError('WAYLAND_PROTOCOL: malformed interface name')
                    name=payload[8:8+length-1].decode('ascii');version=struct.unpack_from('<I',payload,8+padded)[0]
                    globals[name]=version
                if obj==3 and opcode==0:return globals

def session_check(env,expected,unit=UNIT):
    before=session_identity(unit);pid=before[0];proc=Path('/proc',str(pid))
    exe=proc/'exe';args=proc.joinpath('cmdline').read_bytes().decode().split('\0')
    command=args[:args.index('--')] if '--' in args else args
    backend=any(a=='--backend=tegra-l4t' or (a=='--backend' and i+1<len(command) and command[i+1]=='tegra-l4t') for i,a in enumerate(command))
    if elf_machine(exe)!=183 or not backend or '--expose-wayland' not in command or not any(a in command for a in ['--steam','-e']):
        raise RuntimeError('SESSION_BACKEND: native Tegra Gamescope with Steam/Wayland integration is required')
    if not re.fullmatch(r':\d+(?:\.\d+)?',env.get('DISPLAY','')):
        raise RuntimeError('DISPLAY_CONTEXT: local Xwayland display missing')
    name=env.get('WAYLAND_DISPLAY','');runtime=env.get('XDG_RUNTIME_DIR','')
    if not name or not runtime or not Path(runtime).is_absolute() or '..' in Path(name).parts:
        raise RuntimeError('WAYLAND_CONTEXT: client runtime/socket missing')
    sock=Path(name) if Path(name).is_absolute() else Path(runtime)/name
    if not stat.S_ISSOCK(sock.stat().st_mode):raise RuntimeError('WAYLAND_CONTEXT: endpoint is not a socket')
    globals=registry(sock,pid)
    missing=[f'{n}>={v}' for n,v in REQUIRED_GLOBALS.items() if globals.get(n,0)<v]
    if missing:raise RuntimeError('WAYLAND_CAPABILITIES: missing '+', '.join(missing))
    actual=digest(exe)
    if mode(env)=='strict' and (not re.fullmatch('[0-9a-f]{64}',str(expected)) or actual!=expected):
        raise RuntimeError('STRICT_GAMESCOPE_PIN_MISMATCH: compositor differs from experimental snapshot')
    if session_identity(unit)!=before:raise RuntimeError('SESSION_CHANGED_DURING_CHECK: compositor restarted')
    return dict(session_unit=unit,gamescope_pid=pid,gamescope_starttime=before[1],gamescope_sha256=actual,wayland_globals={n:globals[n] for n in REQUIRED_GLOBALS})

def check_environment(entry,kind,env,expected_gamescope,expected_runtime,unit=UNIT):
    selected=mode(env)
    observed=runtime_check(entry,kind,env,expected_runtime)
    observed.update(session_check(env,expected_gamescope,unit))
    return dict(mode=selected,exact_environment_pins_enforced=selected=='strict',**observed)
