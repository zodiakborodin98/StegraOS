#!/usr/bin/env python3
"""Build a separate initramfs candidate; never writes to a card/release input."""
from pathlib import Path
import argparse
import gzip
import hashlib
import json
import stat
import struct
import zlib

EXPECTED = '938598928919e796bd8c1257e165417fd71237be08ad5e5c29ff7c549c52e9b0'

def sha(data):
    return hashlib.sha256(data).hexdigest()

def unpack(data):
    if len(data) < 64:
        raise ValueError('Short uImage')
    h = list(struct.unpack('>7I4B32s', data[:64]))
    if h[0] != 0x27051956 or h[3] != len(data)-64 or h[10] != 1:
        raise ValueError('Not an exact gzip legacy uImage')
    header = bytearray(data[:64]); header[4:8] = bytes(4)
    if zlib.crc32(header) != h[1] or zlib.crc32(data[64:]) != h[6]:
        raise ValueError('uImage CRC failure')
    raw = gzip.decompress(data[64:])
    records = []; offset = 0
    while True:
        if raw[offset:offset+6] != b'070701':
            raise ValueError('Not newc')
        fields = [int(raw[offset+6+i*8:offset+14+i*8],16) for i in range(13)]
        start = offset+110
        name = raw[start:start+fields[11]-1].decode('utf-8')
        if not fields[11] or raw[start+fields[11]-1] != 0:
            raise ValueError('Bad cpio name')
        body = (start+fields[11]+3) & ~3
        end = body+fields[6]
        content = raw[body:end]
        if len(content) != fields[6]:
            raise ValueError('Short cpio body')
        records.append((name,fields,content))
        offset = (end+3) & ~3
        if name == 'TRAILER!!!':
            break
    if any(raw[offset:]):
        raise ValueError('Unexpected nonzero cpio tail')
    return h,records

def pack(h,records):
    output=bytearray()
    for name,fields,content in records:
        name=name.encode()+b'\0'; fields=fields.copy()
        fields[6]=len(content); fields[11]=len(name)
        output += b'070701'+''.join(f'{n:08x}' for n in fields).encode()+name
        output += bytes((-len(output))%4)
        output += content
        output += bytes((-len(output))%4)
    output += bytes((-len(output))%512)
    payload=gzip.compress(bytes(output),compresslevel=9,mtime=0)
    h=h.copy();h[1]=0;h[3]=len(payload);h[6]=zlib.crc32(payload)
    header=struct.pack('>7I4B32s',*h)
    h[1]=zlib.crc32(header)
    return struct.pack('>7I4B32s',*h)+payload

def patch_init(src):
    def replace(before,after):
        nonlocal src
        if src.count(before) != 1:
            raise ValueError(f'Init anchor count != 1: {before!r}')
        src=src.replace(before,after)
    replace('chmod 1777 /tmp\n','chmod 1777 /tmp\n\n. /boot-progress.sh\nprogress_start\nsay() { progress_note "$*"; }\nstop() { progress_error "$*"; while :; do sleep 60; done; }\n')
    replace('mount -t vfat -o rw,noatime,umask=0077 "$sd_fat" "$base/boot" || stop \'Cannot mount FAT32 /boot\'\n',
            'progress_run mount -t vfat -o rw,noatime,umask=0077 "$sd_fat" "$base/boot" || stop \'Cannot mount FAT32 /boot\'\nprogress_attach_log "$base/boot"\n')
    replace('say "Growing ext4 root $root_dev to its partition"\nif [ -x /sbin/resize2fs ]; then\n  /sbin/resize2fs "$root_dev" >/dev/console 2>&1 || say \'resize2fs failed; systemd fallback will retry\'\nfi',
            'progress_stage 2 "ROOT $root_dev - PLEASE KEEP POWER CONNECTED"\nif progress_run /bin/stegra-boot-screen --probe-size "$root_dev"; then\n  [ -x /sbin/resize2fs ] || stop \'Missing resize2fs\'\n  if progress_run /sbin/resize2fs -p "$root_dev"; then\n    say \'RESIZE COMPLETE\'\n  else\n    resize_rc=$?\n    stop "resize2fs failed (rc=$resize_rc); check the SD log"\n  fi\nelse\n  size_rc=$?\n  [ "$size_rc" = 3 ] || stop "Cannot validate root size (rc=$size_rc)"\nfi')
    replace('say "Mounting ext4 root $root_dev"\nmount -t ext4 -o rw,noatime "$root_dev" /newroot',
            'progress_stage 3 "MOUNTING $root_dev"\nprogress_run mount -t ext4 -o rw,noatime "$root_dev" /newroot')
    # Keep the persistent log path valid after the FAT mount moves.
    replace('mount --move "$base/boot" /newroot/boot || stop \'Cannot move /boot into root\'',
            'progress_run mount --move "$base/boot" /newroot/boot || stop \'Cannot move /boot into root\'\nif [ -n "$progress_persistent" ]; then progress_persistent="/newroot/boot/stegra-logs/${progress_persistent##*/}"; fi\nprogress_stage 4 \'CONFIGURING FIRMWARE AND NATIVE LOADER\'')
    replace('chroot /newroot /usr/bin/ldconfig || stop',
            'progress_run chroot /newroot /usr/bin/ldconfig || stop')
    replace("say 'Starting StegraOS systemd from ext4 root.'\nbb sync",
            "progress_stage 5 'SYSTEMD WILL NOW SHOW SERVICE STARTUP MESSAGES'\nprogress_finish\nbb sync")
    return src

def build(input_path,binary,output_dir):
    data=input_path.read_bytes()
    if sha(data) != EXPECTED:
        raise ValueError('Unreviewed base initramfs')
    h,records=unpack(data)
    before={name:(fields,content) for name,fields,content in records}
    if len(before) != len(records):
        raise ValueError('Duplicate cpio names')
    src=before['init'][1].decode('utf-8')
    modified=patch_init(src).encode()
    elf=binary.read_bytes()
    if elf[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H',elf,18)[0] != 183:
        raise ValueError('Expected AArch64 ELF')
    here=Path(__file__).resolve().parent
    additions={'boot-progress.sh':(here/'boot-progress.sh').read_bytes(),'bin/stegra-boot-screen':elf}
    result=[]
    for name,fields,content in records:
        if name=='TRAILER!!!':
            for i,(new,body) in enumerate(additions.items()):
                if new in before:raise ValueError('New path already exists')
                f=[0xf00000+i,stat.S_IFREG|0o755,0,0,1,h[2],len(body),0,0,0,0,len(new)+1,0]
                result.append((new,f,body))
        result.append((name,fields,modified if name=='init' else content))
    candidate=pack(h,result)
    _,verified=unpack(candidate)
    after={name:(fields,content) for name,fields,content in verified}
    unchanged=[]
    for name,record in before.items():
        if name!='init':
            if after[name] != record:raise ValueError(f'Unexpected change: {name}')
            unchanged.append(name)
    output_dir.mkdir(parents=True,exist_ok=False)
    (output_dir/'initramfs').write_bytes(candidate)
    (output_dir/'init').write_bytes(modified)
    (output_dir/'initramfs.before.sha256').write_text(EXPECTED+'\n',encoding='utf-8')
    receipt={'status':'PC_CANDIDATE_NOT_HARDWARE_VERIFIED','base_sha256':EXPECTED,
             'candidate_sha256':sha(candidate),'candidate_bytes':len(candidate),
             'new_files':{name:sha(body) for name,body in additions.items()},
             'changed_existing':['init'],'unchanged_existing_count':len(unchanged),
             'device_written':False,'release_archive_changed':False,
             'resize_failure_policy':'stop with error; no blind fallback retry or repair'}
    (output_dir/'BUILD.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(receipt,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('input',type=Path);p.add_argument('binary',type=Path);p.add_argument('output',type=Path)
    a=p.parse_args();build(a.input,a.binary,a.output)
