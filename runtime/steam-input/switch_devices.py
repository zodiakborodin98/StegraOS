# SPDX-License-Identifier: MIT
"""Identify Nintendo controller roles without treating Steam's pad as hardware."""
from pathlib import Path
import struct


def bitmap(text):
    bits=struct.calcsize('L')*8
    result=0
    for word in text.split():result=(result<<bits)|int(word,16)
    return result


def controllers(sysroot='/sys/class/input'):
    result=[]
    for node in sorted(Path(sysroot).glob('event*'),key=lambda p:int(p.name[5:]) if p.name[5:].isdigit() else -1):
        if not node.name[5:].isdigit():continue
        try:
            d=node/'device'
            name=(d/'name').read_text().strip()
            vendor=int((d/'id/vendor').read_text(),16)
            product=int((d/'id/product').read_text(),16)
            virtual='/devices/virtual/' in str(node.resolve())
            if vendor!=0x057e:continue
            if name=='Nintendo Switch Lite Gamepad' and not virtual:
                role='lite'
            elif name=='Nintendo Switch Combined Joy-Cons' and product==0x2008:
                role='combined'
            elif not virtual and name.startswith('Nintendo Switch ') and product in (0x2006,0x2007):
                role='left' if product==0x2006 else 'right'
            else:continue
            result.append({'path':'/dev/input/'+node.name,'name':name,'role':role,
                           'keys':bitmap((d/'capabilities/key').read_text()),
                           'axes':bitmap((d/'capabilities/abs').read_text())})
        except (OSError,ValueError):continue
    return result


def hotkey_devices(key,sysroot='/sys/class/input',prefer_physical=True):
    nodes=[d for d in controllers(sysroot) if d['keys']&(1<<key)]
    # Raw left/Lite key-state polling also works while joycond owns its stream.
    # Do not watch the merged duplicate as a second hold source.
    physical=[d for d in nodes if d['role'] in ('lite','left','right')]
    combined=[d for d in nodes if d['role']=='combined']
    # joycond's raw rail nodes may be root-only; its merged pad is user-readable.
    chosen=(physical or combined) if prefer_physical else (combined or physical)
    return [d['path'] for d in chosen]


def panel_device(sysroot='/sys/class/input'):
    required=(304,305,314,544,545,546,547)
    mask=sum(1<<k for k in required)
    nodes=controllers(sysroot)
    for role in ('lite','combined'):
        for d in nodes:
            if d['role']==role and d['keys']&mask==mask and d['axes']&(1<<1):
                return d['path']
    return None
