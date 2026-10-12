#!/usr/bin/env python3
"""Validate merged native ELF dependencies on QEMU Cortex-A57, not Switch hardware."""
import argparse,json,posixpath,re,subprocess,hashlib
from pathlib import Path
from dissect.btrfs import Btrfs
STEAM='home/steamos/.local/share/Steam'
def main():
 ap=argparse.ArgumentParser(description=__doc__)
 for n in ['frame','overlay','qemu','output']:ap.add_argument('--'+n,type=Path,required=True)
 a=ap.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 reports=[]
 with a.frame.open('rb') as f:
  fs=Btrfs(f)
  def read(n):
   n=n.lstrip('/');seen=set()
   while n not in seen:
    seen.add(n);p=a.overlay/n
    if p.is_symlink():dest=p.readlink().as_posix()
    elif p.is_file():return p.read_bytes()
    else:
     node=fs.get('/'+n)
     if not node.is_symlink():return node.open().read()
     dest=node.link
    n=posixpath.normpath(posixpath.join(posixpath.dirname(n),dest)).lstrip('/')
   raise ValueError('Symlink loop: '+n)
  def index(dirs):
   result={}
   for folder in dirs:
    p=a.overlay/folder
    if p.is_dir():
     for c in p.iterdir():
      if '.so' in c.name:result.setdefault(c.name,folder+'/'+c.name)
    try:
     for name,node in fs.get('/'+folder).iterdir():
      if '.so' in name:result.setdefault(name,folder+'/'+name)
    except FileNotFoundError:pass
   return result
  standard=['usr/lib/aarch64-linux-gnu/tegra','usr/lib/aarch64-linux-gnu/tegra-egl','usr/lib/aarch64-linux-gnu','usr/lib','usr/lib/systemd','usr/lib/libproxy']
  runtimes=list((a.overlay/STEAM/'steamrtarm64/pv-runtime/steam-runtime-steamrt-arm64').glob('steamrt3c_platform_*/files'))
  rt=[]
  for p in runtimes:rt.extend([(p/'lib/aarch64-linux-gnu').relative_to(a.overlay).as_posix(),(p/'lib').relative_to(a.overlay).as_posix()])
  groups={
   'native':(standard,{'systemd':('usr/lib/systemd/systemd',['--version']),
     'bash':('usr/bin/bash',['--version']),'python3':('usr/bin/python3',['--version']),
     'Xorg':('usr/lib/xorg/Xorg',['-version']),'box64':('usr/local/bin/box64',['-v']),
     'sddm':('usr/bin/sddm',None),'openbox':('usr/bin/openbox',['--version']),
     'joycond':('usr/bin/joycond',None),'zenity':('usr/bin/zenity',None),
     'nvidiaGLX':('usr/lib/aarch64-linux-gnu/tegra/libGLX_nvidia.so.0',None),
     'nvidiaEGL':('usr/lib/aarch64-linux-gnu/tegra-egl/libEGL_nvidia.so.0',None)}),
   'steam':([STEAM+'/steamrtarm64']+rt+standard,{'steam':(STEAM+'/steamrtarm64/steam',None),
     'steamwebhelper':(STEAM+'/steamrtarm64/steamwebhelper',None),'steamclient':(STEAM+'/linuxarm64/steamclient.so',None)})}
  for group,(dirs,programs) in groups.items():
   folder=a.output/group;libs=folder/'lib';bins=folder/'bin';libs.mkdir(parents=True,exist_ok=True);bins.mkdir(exist_ok=True)
   mapping=index(dirs);queue=[];missing=set();saved={}
   for name,(path,args) in programs.items():
    data=read(path);assert data[:4]==b'\x7fELF' and int.from_bytes(data[18:20],'little')==183
    p=bins/name;p.write_bytes(data);p.chmod(0o755);queue.append(p)
   loader=libs/'ld-linux-aarch64.so.1';loader.write_bytes(read('usr/lib/ld-linux-aarch64.so.1'));loader.chmod(0o755);queue.append(loader)
   while queue:
    p=queue.pop();r=subprocess.run(['readelf','-d',str(p)],capture_output=True,text=True,check=True)
    for name in re.findall(r'\(NEEDED\).*?\[(.*?)\]',r.stdout):
     if (libs/name).exists():continue
     if name not in mapping:missing.add(name);continue
     data=read(mapping[name]);out=libs/name;out.write_bytes(data);out.chmod(0o755);queue.append(out);saved[name]=mapping[name]
   report={'group':group,'missing':sorted(missing),'libraries':saved,'commands':[]}
   print(group,'libraries',len(saved),'missing',sorted(missing),flush=True)
   for name,(path,args) in programs.items():
    base=[str(a.qemu.resolve()),'-cpu','cortex-a57',str(loader.resolve()),'--library-path',str(libs.resolve())]
    commands=[('dependencies',base+['--list',str((bins/name).resolve())])]
    if args is not None:commands.append(('version',base+[str((bins/name).resolve())]+args))
    for kind,cmd in commands:
     try:
      r=subprocess.run(cmd,capture_output=True,text=True,timeout=25)
      rec=dict(program=name,check=kind,exit_code=r.returncode,stdout=r.stdout[:5000],stderr=r.stderr[:5000])
     except subprocess.TimeoutExpired:rec=dict(program=name,check=kind,timeout=True)
     report['commands'].append(rec);print(group,name,kind,rec.get('exit_code','TIMEOUT'),flush=True)
   reports.append(report)
 a.output.joinpath('report.json').write_text(json.dumps({'scope':'ELF dependencies and selected version entry points on Cortex-A57 emulation; host kernel, no GPU/OS boot test','groups':reports},indent=2)+'\n')
 if any(r['missing'] or any(c.get('exit_code',-1)!=0 for c in r['commands']) for r in reports):raise SystemExit(1)
if __name__=='__main__':main()
