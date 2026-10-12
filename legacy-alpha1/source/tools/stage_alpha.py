#!/usr/bin/env python3
"""Stage Switch hardware/Steam files over an official Frame root without mounts.
Debian packages supply files only: maintainer scripts are never executed.
"""
import argparse, hashlib, io, json, os, posixpath, re, shutil, subprocess, tarfile, zipfile
from pathlib import Path, PurePosixPath
from dissect.btrfs import Btrfs
PROJECT=Path(__file__).resolve().parents[1]
STEAM='home/steamos/.local/share/Steam'
PACKAGES=['nvidia-l4t-core','nvidia-l4t-3d-core','nvidia-l4t-firmware','nvidia-l4t-wayland',
          'nvidia-bsp-32-7','nvidia-l4t-init','joycond','switch-alsa-ucm2','switch-sddm-rule',
          'switch-joystick-mouse','xserver-xorg-input-joystick','box64','xserver-xorg-core','xserver-xorg-input-libinput','libselinux1']

def safe(name):
    name=name.replace('\\','/').removeprefix('./').rstrip('/')
    if name in ('','.'):return ''
    if name.startswith('/') or '..' in PurePosixPath(name).parts:raise ValueError('Unsafe archive path')
    return name

def merged(name):
    for a,b in [('usr/sbin','usr/bin'),('sbin','usr/bin'),('bin','usr/bin'),('lib','usr/lib')]:
        if name==a:return b
        if name.startswith(a+'/'):return b+name[len(a):]
    return name

def target(root,name):
    p=root/safe(name)
    for parent in p.relative_to(root).parents:
        if (root/parent).is_symlink():raise ValueError('Extraction through symlink: '+str(parent))
    p.parent.mkdir(parents=True,exist_ok=True)
    return p

def untar(stream,root,prefix='',keep=lambda n:True,transform=lambda n:n,strip=0):
    pending=[]
    with tarfile.open(fileobj=stream,mode='r|*') as t:
        for m in t:
            n=safe(m.name)
            if not n or not keep(n):continue
            n='/'.join(n.split('/')[strip:])
            if not n:continue
            p=target(root,posixpath.join(prefix,transform(n)))
            if m.isdir():p.mkdir(exist_ok=True);p.chmod(m.mode&0o7777);continue
            if p.is_symlink() or p.is_file():p.unlink()
            if m.issym():p.symlink_to(m.linkname)
            elif m.islnk():pending.append((p,root/prefix/transform('/'.join(safe(m.linkname).split('/')[strip:]))))
            elif m.isfile():
                with t.extractfile(m) as src,p.open('wb') as dst:shutil.copyfileobj(src,dst,2**20)
                p.chmod(m.mode&0o7777)
            else:raise ValueError('Special archive entry: '+n)
    for p,src in pending:os.link(src,p,follow_symlinks=False)

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--frame',type=Path,required=True);ap.add_argument('--downloads',type=Path,required=True)
    ap.add_argument('--bsp',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--resume-config',action='store_true')
    a=ap.parse_args();root=a.output.resolve();root.mkdir(parents=True,exist_ok=a.resume_config);dl=a.downloads
    def write(n,s,mode=0o644):
        p=target(root,n)
        if p.is_symlink():p.unlink()
        if p.exists():p.chmod(p.stat().st_mode|0o200)
        p.write_text(s);p.chmod(mode)
    def link(n,s):
        p=target(root,n)
        if p.is_file() or p.is_symlink():p.unlink()
        p.symlink_to(s)
    def deb(n,keep=lambda p:True):
        print('Package',n,flush=True)
        with subprocess.Popen(['dpkg-deb','--fsys-tarfile',str(dl/(n+'.deb'))],stdout=subprocess.PIPE) as p:
            untar(p.stdout,root,keep=keep,transform=merged)
            if p.wait():raise RuntimeError('dpkg-deb failed '+n)
    if not a.resume_config:
        with (a.bsp/'opt/switchroot/modules.tar.gz').open('rb') as f:untar(f,root,prefix='usr/lib')
        for n in PACKAGES:
            if n=='nvidia-l4t-init':
                deb(n,lambda p:p.startswith('usr/share/doc/') or p in ['etc/udev/rules.d/99-tegra-devices.rules','etc/udev/rules.d/99-tegra-mmc-ra.rules'])
            elif n=='nvidia-bsp-32-7':deb(n,lambda p:p.startswith(('lib/firmware/','usr/share/doc/')))
            elif n.startswith('nvidia-'):deb(n,lambda p:p.startswith(('usr/lib/','usr/share/','lib/firmware/','etc/vulkan/')))
            else:deb(n)
        print('Extract Frame Steam preload',flush=True)
        cache=root.parent/'frame-steam.tar.zst'
        with a.frame.open('rb') as f:
            fs=Btrfs(f)
            with fs.get('/usr/lib/steam/steam.tar.zst').open() as src,cache.open('wb') as dst:shutil.copyfileobj(src,dst,2**20)
        with subprocess.Popen(['zstd','-dc',str(cache)],stdout=subprocess.PIPE) as p:
            untar(p.stdout,root,prefix=STEAM,keep=lambda n:not n.startswith(('androidarm64/','steamrtarm64/','linuxarm64/')))
            if p.wait():raise RuntimeError('Steam preload decompression failed')
        cache.unlink()
        print('Apply Steam ARM compatibility snapshot',flush=True)
        with subprocess.Popen(['cat',*[str(dl/('steam-arm64.part'+s)) for s in ['aa','ab','ac']]],stdout=subprocess.PIPE) as p:
            untar(p.stdout,root,prefix=STEAM+'/steamrtarm64')
            if p.wait():raise RuntimeError('Split archive read failed')
        with (dl/'steam-linuxarm64.tar.gz').open('rb') as f:untar(f,root,prefix=STEAM+'/linuxarm64')
        for n in ['steam-linux_x86_64.zip','steam-ui.zip']:
            with zipfile.ZipFile(dl/n) as z:
                for m in z.infolist():
                    name=safe(m.filename)
                    if not name:continue
                    p=target(root,STEAM+'/'+name)
                    if m.is_dir():p.mkdir(exist_ok=True);continue
                    if p.is_file() or p.is_symlink():p.unlink()
                    with z.open(m) as src,p.open('wb') as dst:shutil.copyfileobj(src,dst,2**20)
                    p.chmod((m.external_attr>>16)&0o777 or 0o644)
        with (dl/'steam-runtime-arm64.tar.xz').open('rb') as f:untar(f,root,prefix=STEAM+'/steamrtarm64/pv-runtime')
        print('Extract Proton/DXVK/VKD3D',flush=True)
        with (dl/'proton.tar.gz').open('rb') as f:untar(f,root,prefix=STEAM+'/compatibilitytools.d')
        with (dl/'dxvk.tar.gz').open('rb') as f:untar(f,root,prefix=STEAM+'/Switchdeck/DXVK',strip=1)
        with subprocess.Popen(['zstd','-dc',str(dl/'vkd3d.tar.zst')],stdout=subprocess.PIPE) as p:
            untar(p.stdout,root,prefix=STEAM+'/Switchdeck/VKD3D',strip=1)
            if p.wait():raise RuntimeError('VKD3D archive failed')
        (root.parent/'archives-extracted').touch()
    elif not (root.parent/'archives-extracted').exists():raise ValueError('Cannot resume an incomplete extraction')
    # Equivalent to official nvidia-bsp-32-7 preinst diversions. Do not replace Frame libdrm.
    for n in ['usr/lib/aarch64-linux-gnu/tegra/libdrm.so.2','usr/lib/aarch64-linux-gnu/libdrm_nvdc.so']:
        p=root/n
        if p.is_file() or p.is_symlink():p.rename(p.with_name('libdrm.orig' if p.name=='libdrm.so.2' else 'libdrm_nvdc.orig'))
    fw=root/'usr/lib/firmware/gm20b/acr_ucode.bin'
    assert fw.is_symlink() and fw.is_file() and fw.resolve().is_relative_to(root)
    link('usr/lib/Xorg','xorg/Xorg')
    for n in ['etc/X11/xorg.conf','etc/X11/xorg.conf.d/10-monitor.conf']:write(n,(a.bsp/n).read_text())
    write('etc/X11/Xwrapper.config','allowed_users=anybody\nneeds_root_rights=yes\n')
    write('etc/ld.so.conf.d/10-switchdeck-nvidia.conf','/usr/lib/aarch64-linux-gnu/tegra\n/usr/lib/aarch64-linux-gnu/tegra-egl\n/usr/lib/aarch64-linux-gnu\n')
    write('etc/vulkan/icd.d/nvidia_icd.json',json.dumps({'file_format_version':'1.0.0','ICD':{'library_path':'/usr/lib/aarch64-linux-gnu/tegra/libGLX_nvidia.so.0','api_version':'1.2.131'}},indent=2)+'\n')
    write('usr/share/glvnd/egl_vendor.d/10_nvidia.json',json.dumps({'file_format_version':'1.0.0','ICD':{'library_path':'/usr/lib/aarch64-linux-gnu/tegra-egl/libEGL_nvidia.so.0'}},indent=2)+'\n')
    proton=root/STEAM/'compatibilitytools.d/Proton 11.0-1 Armv8.0 (FEX)/files'
    for arch,dx,vk in [('x86_64-windows','x64','x64'),('i386-windows','x32','x86')]:
        dlls=list((root/STEAM/'Switchdeck/DXVK/build'/dx).glob('*.dll'));assert len(dlls)>=3
        for p in dlls:link((proton/'lib/wine/dxvk'/arch/p.name).relative_to(root).as_posix(),'/'+p.relative_to(root).as_posix())
        p=root/STEAM/'Switchdeck/VKD3D'/vk/'d3d12.dll'
        if p.is_file():
            for n in ['d3d12.dll','d3d12core.dll']:link((proton/'lib/wine/vkd3d-proton'/arch/n).relative_to(root).as_posix(),'/'+p.relative_to(root).as_posix())
    patched=[]
    for p in proton.glob('lib/wine/*-unix/win32u.so'):
        data=p.read_bytes()
        if b'VK_EXT_external_memory_host' in data:
            mode=p.stat().st_mode&0o7777;p.chmod(mode|0o200)
            p.write_bytes(data.replace(b'VK_EXT_external_memory_host',b'VK_EXT_external_memory_Xost'));p.chmod(mode)
            patched.append(p.relative_to(root).as_posix())
    write(STEAM+'/steam.cfg','BootStrapperInhibitAll=enable\nBootStrapperForceSelfUpdate=disable\n')
    write(STEAM+'/package/beta','publicbeta\n')
    for n in ['steamapps/common','steamapps/compatdata','userdata','config','logs']:(root/STEAM/n).mkdir(parents=True,exist_ok=True)
    link(STEAM+'/steamapps/compatdata/0','/dev/null')
    for name,dest in [('root','../.local/share/Steam'),('steam','../.local/share/Steam'),('sdk64','../.local/share/Steam/linux64'),('sdk32','../.local/share/Steam/linux32')]:link('home/steamos/.steam/'+name,dest)
    shutil.copytree(PROJECT/'alpha/root',root,dirs_exist_ok=True,symlinks=True)
    for folder in ['usr/local/bin','usr/local/sbin']:
        for p in (root/folder).iterdir():p.chmod(0o755)
    from configure_alpha import configure
    with a.frame.open('rb') as f:masks=configure(Btrfs(f),root,write,link)
    shutil.copyfile(dl/'Switchdeck-LICENSE',target(root,'usr/share/licenses/switchdeck/GPL-3.0.txt'))
    report={'revision':'Alpha 1','hardware_boot_tested':False,'masked_frame_units':masks,'proton_patched_files':patched,
            'upstream_switchdeck_commit':'be0578fbf00c5a14a988a830f914f2c34cdaa1a1','userdata_bytes':4026531840,
            'packages':[{'name':n,'sha256':hashlib.sha256((dl/(n+'.deb')).read_bytes()).hexdigest()} for n in PACKAGES]}
    write('usr/share/switchdeck/build.json',json.dumps(report,indent=2)+'\n')
    (root.parent/'stage-report.json').write_text(json.dumps(report,indent=2)+'\n')
    print('STAGE_COMPLETE',flush=True)
if __name__=='__main__':main()
