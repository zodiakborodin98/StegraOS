#!/usr/bin/env python3
"""PC-only M11P4 build. Preserve release inputs and verify both uImages."""
from pathlib import Path
import argparse
import hashlib
import importlib.util
import json
import stat
import struct
import zlib

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('m11p_base',HERE/'image_codec.py')
prior=importlib.util.module_from_spec(spec);spec.loader.exec_module(prior)
BOOT_SHA='b826698382630227d9f0091167c4e77472e5f76ce9c331e8fa3b1b1db0b04eab'

def patch_boot(data):
    if prior.sha(data)!=BOOT_SHA:raise ValueError('Unreviewed boot.scr')
    h=list(struct.unpack('>7I4B32s',data[:64]));payload=data[64:]
    head=bytearray(data[:64]);head[4:8]=bytes(4)
    if h[0]!=0x27051956 or h[3]!=len(payload) or h[9]!=6 or h[10]!=0:
        raise ValueError('Wrong script uImage')
    if zlib.crc32(head)!=h[1] or zlib.crc32(payload)!=h[6]:raise ValueError('Script CRC')
    size,terminator=struct.unpack('>2I',payload[:8])
    if terminator or size!=len(payload)-8:raise ValueError('Script component lengths')
    script=payload[8:].decode('ascii')
    anchor='# Set kernel cmdline.\n'
    if script.count(anchor)!=1:raise ValueError('Boot console anchor')
    script=script.replace(anchor,
        '# M11P4: visible text console after the early graphical screen.\n'
        'setenv bootargs_extra ${bootargs_extra} "console=tty1 fbcon=rotate:2 loglevel=7 systemd.log_target=journal-or-kmsg"\n\n'+anchor)
    raw=script.encode('ascii');payload=struct.pack('>2I',len(raw),0)+raw
    h[1]=0;h[3]=len(payload);h[6]=zlib.crc32(payload)
    head=struct.pack('>7I4B32s',*h);h[1]=zlib.crc32(head)
    result=struct.pack('>7I4B32s',*h)+payload
    # Round-trip validate lengths and both CRCs independently.
    head=bytearray(result[:64]);head[4:8]=bytes(4)
    check=struct.unpack('>7I4B32s',result[:64])
    assert zlib.crc32(head)==check[1] and zlib.crc32(result[64:])==check[6]
    assert len(result[64:])==check[3]
    return result,raw

def build(release,out):
    release=release.resolve(strict=True)
    src=release/'switchroot/stegra-beta'
    data=(src/'initramfs').read_bytes()
    if prior.sha(data)!=prior.EXPECTED:raise ValueError('Unreviewed initramfs')
    h,records=prior.unpack(data)
    old={name:(fields,body) for name,fields,body in records}
    if len(old)!=len(records):raise ValueError('Duplicate CPIO names')
    init=prior.patch_init(old['init'][1].decode('utf-8'))
    tail="progress_stage 5 'SYSTEMD WILL NOW SHOW SERVICE STARTUP MESSAGES'\nprogress_finish\nbb sync\nexec > /dev/console 2>&1\nmount --move /run /newroot/run || exit 1\nmount --move /dev /newroot/dev || exit 1\nmount --move /sys /newroot/sys || exit 1\nmount --move /proc /newroot/proc || exit 1\nexec switch_root /newroot /usr/lib/systemd/systemd\n"
    if init.count(tail)!=1:raise ValueError('Handoff anchor')
    init=init.replace(tail,(HERE/'handoff-tail.sh').read_text(encoding='utf-8')).encode('utf-8')
    binary=(HERE/'boot-screen-arm64').read_bytes()
    if binary[:6]!=b'\x7fELF\x02\x01' or struct.unpack_from('<H',binary,18)[0]!=183:raise ValueError('Not ARM64')
    additions={'bin/stegra-boot-screen':binary}
    for name in ['boot-progress.sh','init-systemd.sh','capture-systemd.sh','stegra-boot-capture.service']:
        additions[name]=(HERE/name).read_bytes()
    new=[]
    for name,fields,body in records:
        if name=='TRAILER!!!':
            for i,(path,content) in enumerate(additions.items()):
                if path in old:raise ValueError('New path collision')
                mode=0o644 if path.endswith('.service') else 0o755
                f=[0xf10000+i,stat.S_IFREG|mode,0,0,1,h[2],len(content),0,0,0,0,len(path)+1,0]
                new.append((path,f,content))
        new.append((name,fields,init if name=='init' else body))
    candidate=prior.pack(h,new)
    _,verified=prior.unpack(candidate)
    after={name:(fields,body) for name,fields,body in verified}
    for name,record in old.items():
        if name!='init' and after[name]!=record:raise ValueError('Changed original entry '+name)
    boot,boot_text=patch_boot((src/'boot.scr').read_bytes())
    manifest=(release/'SHA256SUMS').read_bytes()
    for oldsha,newsha in [(prior.EXPECTED,prior.sha(candidate)),(BOOT_SHA,prior.sha(boot))]:
        if manifest.count(oldsha.encode())!=1:raise ValueError('Manifest hash occurrence')
        manifest=manifest.replace(oldsha.encode(),newsha.encode())
    out.mkdir(parents=True,exist_ok=False)
    package=out/'package';target=package/'switchroot/stegra-beta';target.mkdir(parents=True)
    (target/'initramfs').write_bytes(candidate);(target/'boot.scr').write_bytes(boot)
    (package/'SHA256SUMS').write_bytes(manifest)
    (out/'init').write_bytes(init);(out/'boot-script.txt').write_bytes(boot_text)
    # Freeze exact sources alongside each binary build for reproducibility.
    frozen=out/'sources';frozen.mkdir()
    source_names=['boot-screen.c','boot-progress.sh','handoff-tail.sh','init-systemd.sh',
                  'capture-systemd.sh','stegra-boot-capture.service','build_update.py',
                  'image_codec.py','build-renderer.sh']
    source_hashes={}
    for name in source_names:
        content=(HERE/name).read_bytes();(frozen/name).write_bytes(content);source_hashes[name]=prior.sha(content)
    receipt={
        'status':'BUILT_NOT_HARDWARE_VERIFIED','base_initramfs_sha256':prior.EXPECTED,
        'base_boot_script_sha256':BOOT_SHA,'candidate_sha256':prior.sha(candidate),
        'candidate_bytes':len(candidate),'boot_script_sha256':prior.sha(boot),
        'sources':source_hashes,'new_files':{k:prior.sha(v) for k,v in additions.items()},
        'changed_existing_cpio':['init'],'unchanged_existing_count':len(old)-1,
        'package_files':['switchroot/stegra-beta/initramfs','switchroot/stegra-beta/boot.scr','SHA256SUMS'],
        'rotation_correction_degrees':180,'kernel_rebuilt':False,'root_image_changed':False,
        'systemd_capture_runtime_only':True,'logging_only_timeout_seconds':1200,
        'logging_per_stream_bytes':128*1024,'device_written':False,'base_archive_changed':False}
    (out/'BUILD.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(receipt,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--package',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();build(a.package,a.output)
