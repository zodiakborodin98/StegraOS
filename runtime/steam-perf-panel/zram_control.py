# SPDX-License-Identifier: MIT
"""Persistent zram sizing. Runtime API saves policy; only boot creates swap."""
import json,os,stat,subprocess,sys
from pathlib import Path

GIB=1024**3
CHOICES=(None,0,1,2,3,4)
POLICY=Path('/var/lib/steam-perf-panel/zram-policy.json')
SYS=Path('/sys/block/zram0')
DEV='/dev/zram0'
SWAPS=Path('/proc/swaps')


def validate(value):
    if value is not None and (type(value) is not int or value not in CHOICES):
        raise ValueError('Choose System, Off, 1, 2, 3 or 4 GiB.')
    return value


def read_policy():
    try:fd=os.open(POLICY,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
    except FileNotFoundError:return None
    with os.fdopen(fd,'r',encoding='utf-8') as f:
        s=os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_size>4096:
            raise ValueError('Invalid zram policy file.')
        data=json.load(f)
    if not isinstance(data,dict) or data.get('schema')!=1 or 'size_gib' not in data:
        raise ValueError('Invalid zram policy schema.')
    return validate(data['size_gib'])


def save_policy(value):
    value=validate(value)
    parent=POLICY.parent
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_uid!=0:
        raise ValueError('Invalid zram policy directory.')
    if POLICY.is_symlink():raise ValueError('Symlink zram policy refused.')
    tmp=POLICY.with_name('.zram-policy.%d'%os.getpid())
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump({'schema':1,'size_gib':value},f);f.write('\n');f.flush();os.fsync(f.fileno())
        os.chown(tmp,0,0);os.replace(tmp,POLICY)
        fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if tmp.exists():tmp.unlink()
    return {'ok':True,'status':'saved','applies':'next_boot','zram':status()}


def number(name):
    try:return int((SYS/name).read_text().strip())
    except (OSError,ValueError):return 0


def status():
    error=None
    try:choice=read_policy()
    except (OSError,ValueError) as e:choice=None;error=str(e)
    size=number('disksize');active=False;used=0
    try:
        for line in SWAPS.read_text().splitlines()[1:]:
            fields=line.split()
            if len(fields)>=5 and fields[0]==DEV:
                active=True;used=int(fields[3])*1024
    except (OSError,ValueError):pass
    try:
        algorithms=(SYS/'comp_algorithm').read_text().split()
        algorithm=next((s[1:-1] for s in algorithms if s.startswith('[') and s.endswith(']')),'unknown')
    except OSError:algorithm='unknown'
    try:mm=[int(x) for x in (SYS/'mm_stat').read_text().split()];orig,compressed,memory=mm[:3]
    except (OSError,ValueError):orig=compressed=memory=0
    target=(3 if choice is None else choice)*GIB
    actual=size if active else 0
    return {'available':(SYS/'disksize').exists(),'configured_size_gib':choice,
            'target_bytes':target,'size_bytes':size,'active':active,'used_bytes':used,
            'ram_bytes':memory,'original_bytes':orig,'compressed_bytes':compressed,
            'algorithm':algorithm,'pending':actual!=target,'error':error}


def boot():
    choice=read_policy();target=(3 if choice is None else choice)*GIB
    current=status()
    if current['active'] or number('disksize'):
        return {'ok':True,'status':'kept_existing','zram':current}
    if target==0:return {'ok':True,'status':'disabled'}
    if not (SYS/'disksize').exists():raise RuntimeError('zram0 is not available.')
    # Restrict all boot writes and commands to the existing zram0 block device.
    s=os.stat(DEV,follow_symlinks=False)
    if not stat.S_ISBLK(s.st_mode):raise RuntimeError('zram0 is not a block device.')
    pair=tuple(int(v) for v in (SYS/'dev').read_text().strip().split(':'))
    if pair!=(os.major(s.st_rdev),os.minor(s.st_rdev)):raise RuntimeError('zram0 device identity mismatch.')
    algorithms=(SYS/'comp_algorithm').read_text().replace('[','').replace(']','').split()
    if 'lz4' not in algorithms:raise RuntimeError('LZ4 is not supported by this kernel.')
    (SYS/'comp_algorithm').write_text('lz4\n')
    (SYS/'disksize').write_text(str(target)+'\n')
    if number('disksize')!=target:raise RuntimeError('zram size readback mismatch.')
    subprocess.run(['/usr/bin/mkswap',DEV],check=True,capture_output=True,text=True,timeout=15)
    subprocess.run(['/usr/bin/swapon','-p','5',DEV],check=True,capture_output=True,text=True,timeout=15)
    result=status()
    if not result['active']:raise RuntimeError('zram activation verification failed.')
    return {'ok':True,'status':'created','zram':result}


if __name__=='__main__':
    if sys.argv[1:]!=['--boot'] or os.geteuid()!=0:raise SystemExit('root --boot only')
    try:print('STEGRA_ZRAM_BOOT',json.dumps(boot()),flush=True)
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as e:
        print('STEGRA_ZRAM_BOOT_FAILED',str(e),file=sys.stderr,flush=True);raise SystemExit(1)
