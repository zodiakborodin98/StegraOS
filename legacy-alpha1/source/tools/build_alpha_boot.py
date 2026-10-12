#!/usr/bin/env python3
"""Build file-backed Alpha initramfs/boot entry with exact CRC/CPIO checks."""
import argparse,gzip,hashlib,json,shutil,stat,struct,subprocess,zlib
from pathlib import Path
from build_boot0 import unpack_uboot,unpack_cpio,encode_cpio,diagnostic_boot_script
def pack(header,payload):
 header=header.copy();header[1]=header[2]=0;header[3]=len(payload);header[6]=zlib.crc32(payload)
 header[11]=b'Switchdeck Alpha 1'.ljust(32,b'\0');header[1]=zlib.crc32(struct.pack('>7I4B32s',*header))
 data=struct.pack('>7I4B32s',*header)+payload;unpack_uboot(data);return data
def main():
 ap=argparse.ArgumentParser(description=__doc__)
 for n in ['bsp','busybox','rootfs','output']:ap.add_argument('--'+n,type=Path,required=True)
 a=ap.parse_args();project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
 boot=a.bsp/'opt/switchroot/bootstack';dest=out/'sd-card/switchroot/switchdeck-alpha';dest.mkdir(parents=True)
 with a.rootfs.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
 header,compressed=unpack_uboot((boot/'initramfs').read_bytes());entries=unpack_cpio(gzip.decompress(compressed))
 inode=max(v[0][0] for v in entries.values())+1
 def add(n,data,mode=0o100755):
  nonlocal inode
  entries[n]=([inode,mode,0,0,1,0,len(data),0,0,0,0,len(n)+1,0],data);inode+=1
 add('init',(project/'alpha/init').read_bytes());add('alpha-shutdown',(project/'alpha/shutdown').read_bytes())
 add('bin/busybox',a.busybox.read_bytes());add('alpha-rootfs.sha256',(digest+'  rootfs.squashfs\n').encode(),0o100644)
 cpio=encode_cpio(entries);decoded=unpack_cpio(cpio)
 assert set(decoded)==set(entries) and all(decoded[n][1]==v[1] for n,v in entries.items())
 payload=gzip.compress(cpio,compresslevel=6,mtime=0);assert len(payload)+64<0x0e000000
 (dest/'initramfs').write_bytes(pack(header,payload))
 _,text=diagnostic_boot_script((boot/'boot.scr').read_bytes())
 text=text.replace('generic profile - output unverified','generic profile - Boot0 r6 verified')
 text=text.replace('systemd.unified_cgroup_hierarchy=0','systemd.unified_cgroup_hierarchy=0 SYSTEMD_CGROUP_ENABLE_LEGACY_FORCE=1 systemd.gpt_auto=0')
 text=text.replace(' splash ',' systemd.show_status=1 ')
 hdr,_=unpack_uboot((boot/'boot.scr').read_bytes());encoded=text.encode()
 (dest/'boot.scr').write_bytes(pack(hdr,struct.pack('>II',len(encoded),0)+encoded));(out/'boot.txt').write_text(text)
 for n in ['bl31.bin','bl33.bin','uImage','nx-plat.dtimg']:shutil.copyfile(boot/n,dest/n)
 ini=out/'sd-card/bootloader/ini';ini.mkdir(parents=True)
 (ini/'SWITCHDECK-ALPHA.ini').write_text('[Switchdeck SteamOS Alpha 1]\nl4t=1\nboot_prefixes=/switchroot/switchdeck-alpha/\nid=SWD-ALPHA\nemmc=0\nr2p_action=bootloader\nfbconsole=0\n')
 # Old-kernel-compatible ext4 feature set; never format an existing user's data.
 template=out/'userdata-template.ext4'
 with template.open('xb') as f:f.truncate(4026531840)
 subprocess.run(['mke2fs','-q','-F','-t','ext4','-b','4096','-I','256','-m','0','-L','SWD-DATA',
  '-O','none,has_journal,extent,filetype,dir_index,sparse_super,large_file,extra_isize,resize_inode',str(template)],check=True)
 with template.open('rb') as src,(dest/'userdata-template.ext4.gz').open('wb') as dst:
  with gzip.GzipFile(fileobj=dst,mode='wb',compresslevel=1,mtime=0) as gz:shutil.copyfileobj(src,gz,2**20)
 report=dict(rootfs_sha256=digest,initrd_bytes=(dest/'initramfs').stat().st_size,cpio_entries=len(entries),
  checks=['U-Boot header/data CRC','CPIO full roundtrip','initrd load-address limit','pinned rootfs SHA256'],hardware_boot_tested=False)
 (out/'build-report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
if __name__=='__main__':main()
