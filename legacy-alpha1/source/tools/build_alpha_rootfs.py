#!/usr/bin/env python3
"""Stream official Frame Btrfs + reviewed overlay into XZ SquashFS, no mounts."""
import argparse, hashlib, io, json, os, re, stat, subprocess, tarfile
from pathlib import Path
from dissect.btrfs import Btrfs
EXCLUDE=(
 'opt/steamvr','usr/share/guestos','boot','usr/lib/modules','usr/lib/steam/steam.tar.zst',
 'usr/share/factory','etc/pacman.d','etc/pacman.conf','etc/steamos-atomupd','etc/ssh',
 'etc/NetworkManager/system-connections','var/cache','var/log','var/lib/NetworkManager',
 'var/lib/bluetooth','var/lib/systemd/random-seed','var/lib/dbus/machine-id','root','home',
 'usr/share/doc','usr/share/man','usr/share/info','etc/modules-load.d','usr/lib/modules-load.d',
 'etc/ld.so.cache','etc/machine-id','etc/sddm.conf.d',
 'usr/share/X11/xorg.conf.d/10-steamos-virtualdisplaysize.conf',
 'usr/lib/udev/rules.d/90-steam-inputplumber.rules','usr/lib/udev/rules.d/99-steamos-automount.rules',
 'usr/lib/udev/rules.d/99-steamvr.rules','usr/lib/udev/rules.d/70-steamos-power-button.rules')
EPOCH=1790596800
def excluded(n):return any(n==p or n.startswith(p+'/') for p in EXCLUDE)
def main():
 ap=argparse.ArgumentParser(description=__doc__)
 for n in ['frame','overlay','mksquashfs','host-lib','output']:ap.add_argument('--'+n,type=Path,required=True)
 a=ap.parse_args()
 if a.output.exists():ap.error('Refusing to replace existing image')
 a.output.parent.mkdir(parents=True,exist_ok=True)
 overlay={p.relative_to(a.overlay).as_posix():p for p in a.overlay.rglob('*')};written=set();count=total=checked=0;private_excluded=[]
 env=os.environ.copy();env['LD_LIBRARY_PATH']=str(a.host_lib.resolve())
 cmd=[str(a.mksquashfs.resolve()),'-',str(a.output.resolve()),'-tar','-comp','xz','-b','128K','-noappend',
      '-no-progress','-processors','3','-mem','768M','-mkfs-time',str(EPOCH),'-all-time',str(EPOCH),'-no-xattrs']
 with a.output.with_suffix('.build.log').open('wb') as log,subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=log,stderr=log,env=env) as p:
  try:
   with tarfile.open(fileobj=p.stdin,mode='w|',format=tarfile.PAX_FORMAT) as t,a.frame.open('rb') as f:
    fs=Btrfs(f);stack=[('',fs.get('/'))]
    while stack:
     n,node=stack.pop()
     if n and excluded(n):continue
     local=overlay.get(n)
     if local is not None and not(node.is_dir() and local.is_dir() and not local.is_symlink()):continue
     if n:
      m=tarfile.TarInfo(n);m.mode=stat.S_IMODE(node.mode);m.uid=node.uid;m.gid=node.gid;m.mtime=EPOCH
      if node.is_symlink():m.type=tarfile.SYMTYPE;m.linkname=node.link;t.addfile(m)
      elif node.is_dir():m.type=tarfile.DIRTYPE;t.addfile(m)
      elif node.is_file():
       m.size=node.size;s=node.open()
       if node.size<=2**20 and n.startswith(('etc/','usr/share/','usr/lib/holo/','var/lib/')):
        data=s.read();s.close()
        if b'\0' not in data[:1024]:
         checked+=1
         if b'_DO_NOT_SHARE_URL' in data or re.search(rb'https?://[^/\s:@]+:[^/\s@]+@',data):
          private_excluded.append(n);print('Excluded private vendor configuration:',n,flush=True);continue
        s=io.BytesIO(data)
       t.addfile(m,s);s.close();total+=node.size
      else:raise ValueError('Special source inode: '+n)
      written.add(n);count+=1
     if node.is_dir() and n not in {'dev','proc','sys','run','tmp'}:
      children=sorted(((v,x) for v,x in node.iterdir() if v not in ('.','..')),reverse=True)
      stack.extend(((n+'/'+v).lstrip('/'),x) for v,x in children)
     if count and count%10000==0:print('Frame entries',count,'bytes',total,flush=True)
    print('Switch overlay',len(overlay),flush=True)
    for n,path in sorted(overlay.items(),key=lambda x:(x[0].count('/'),x[0])):
     if n in written:continue
     m=t.gettarinfo(str(path),arcname=n);m.mtime=EPOCH;m.uname=m.gname=''
     m.uid=m.gid=1000 if n=='home/steamos' or n.startswith('home/steamos/') else 0
     if m.isfile():
      with path.open('rb') as s:t.addfile(m,s)
      total+=m.size
     else:t.addfile(m)
     count+=1
  except BaseException:
   p.stdin.close();p.terminate();p.wait();raise
  p.stdin.close()
  if p.wait():raise RuntimeError('mksquashfs failed; inspect build log')
 size=a.output.stat().st_size
 if size>=2**32:raise ValueError('FAT32 single-file limit exceeded')
 with a.output.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
 report=dict(entries=count,input_regular_bytes=total,compressed_bytes=size,sha256=digest,checked_text_files=checked,
             format='SquashFS 4 XZ 128KiB',excluded_frame_trees=EXCLUDE,excluded_private_configurations=private_excluded,hardware_boot_tested=False)
 a.output.with_suffix('.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
if __name__=='__main__':main()
