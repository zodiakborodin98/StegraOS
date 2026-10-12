"""Frame-to-Switch service and session configuration; used by stage_alpha.py."""
import re
def configure(fs,root,write,link):
    def read(n):return fs.get('/'+n).open().read().decode()
    groups=[]
    for line in read('etc/group').splitlines():
        p=line.split(':')
        if p[0] in ['video','render','audio','input','wheel']:
            users=list(filter(None,p[3].split(',')))
            if 'steamos' not in users:users.append('steamos')
            p[3]=','.join(users)
        groups.append(':'.join(p))
    write('etc/group','\n'.join(groups)+'\n')
    shadow=[]
    for line in read('etc/shadow').splitlines():
        p=line.split(':');p[1]='!';shadow.append(':'.join(p))
    write('etc/shadow','\n'.join(shadow)+'\n',0o600)
    pattern=re.compile(r'^(deckard|steamvr|steamos|holo|rauc|fwupd|jupiter|setubootenv|dsp_service|iris-driver|gpu-trace|softapmanager|inputplumber|steam-video-codec|systemd-oomd|systemd-repart|adbd|sshd|firewalld|podman)')
    masks=[]
    for level in ['system','user']:
        for n,node in fs.get('/usr/lib/systemd/'+level).iterdir():
            if n.endswith(('.service','.socket','.timer','.path','.target')) and pattern.search(n):
                link('etc/systemd/'+level+'/'+n,'/dev/null');masks.append(level+'/'+n)
    for n in ['steam.service','gamescope-session.service','steam-notif-daemon.service']:link('etc/systemd/user/'+n,'/dev/null')
    link('etc/systemd/system/systemd-firstboot.service','/dev/null')
    for n in ['usr/bin/nmtui','usr/bin/python3','usr/bin/xterm','usr/bin/zenity','usr/bin/openbox','usr/bin/sddm']:fs.get('/'+n)
    write('usr/share/switchdeck/frame-os-release',read('etc/os-release'))
    write('usr/share/switchdeck/ABOUT','Unofficial SteamOS 0.3.0 port for Nintendo Switch Lite, Alpha 1.\nFrame root + Switchroot 4.9.140 + NVIDIA 32.7.4 + SildurFX compatibility recipe.\nComplete OS not yet hardware-validated. Boot0 r6 display and 32.3.1 GPU compute passed separately.\n')
    write('etc/fstab','# File-backed root mounted by Switchdeck initramfs.\n')
    write('etc/hostname','switchdeck\n');write('etc/machine-id','');write('etc/locale.conf','LANG=en_US.UTF-8\n')
    write('etc/pacman.conf','# Alpha: Frame private package servers intentionally removed. Rebuild to update.\n[options]\nArchitecture = auto\nCheckSpace\nSigLevel = Required DatabaseOptional\nLocalFileSigLevel = Optional\n')
    write('etc/sddm.conf.d/zz-switchdeck.conf','[General]\nDisplayServer=x11\n[X11]\nServerPath=/usr/bin/Xorg\nDisplayCommand=/usr/bin/true\nDisplayStopCommand=/usr/bin/true\nServerArguments=-nolisten tcp -noreset\n[Autologin]\nUser=steamos\nSession=switchdeck.desktop\nRelogin=false\n')
    write('usr/share/xsessions/switchdeck.desktop','[Desktop Entry]\nName=Switchdeck SteamOS Alpha\nExec=/usr/local/bin/switchdeck-session\nType=Application\nDesktopNames=Openbox\n')
    write('etc/bluetooth/main.conf','[General]\nFastConnectable=true\nMultiProfile=multiple\n[Policy]\nAutoEnable=true\n')
    write('etc/NetworkManager/conf.d/90-switchdeck.conf','[device]\nwifi.scan-rand-mac-address=no\n[connection]\nwifi.cloned-mac-address=permanent\n')
    write('etc/sysctl.d/90-switchdeck.conf','vm.swappiness=100\nvm.page-cluster=0\nvm.max_map_count=1048576\n')
    write('etc/systemd/logind.conf.d/switchdeck.conf','[Login]\nHandlePowerKey=poweroff\nHandleSuspendKey=ignore\nHandleLidSwitch=ignore\n')
    write('etc/systemd/journald.conf.d/switchdeck.conf','[Journal]\nStorage=persistent\nSystemMaxUse=64M\nRuntimeMaxUse=16M\n')
    write('etc/systemd/system.conf.d/switchdeck.conf','[Manager]\nDefaultTimeoutStopSec=20s\n')
    write('etc/sudoers.d/switchdeck','steamos ALL=(root) NOPASSWD: /usr/local/sbin/switchdeck-diagnostics, /usr/bin/passwd steamos\n',0o440)
    write('usr/lib/systemd/system/switchdeck-hardware.service','[Unit]\nDescription=Switch hardware preparation\nAfter=systemd-udev-trigger.service\nBefore=display-manager.service\n[Service]\nType=oneshot\nExecStart=/usr/local/sbin/switchdeck-hardware\nRemainAfterExit=yes\n[Install]\nWantedBy=multi-user.target\n')
    write('usr/lib/systemd/system/switchdeck-wifi-import.service','[Unit]\nDescription=Import optional SD Wi-Fi profile\nAfter=NetworkManager.service\nWants=NetworkManager.service\n[Service]\nType=oneshot\nExecStart=/usr/local/sbin/switchdeck-wifi-import\n[Install]\nWantedBy=multi-user.target\n')
    write('usr/lib/systemd/system/switchdeck-diagnostics.service','[Unit]\nDescription=Export boot diagnostics\n[Service]\nType=oneshot\nExecStart=/usr/local/sbin/switchdeck-diagnostics\n')
    write('usr/lib/systemd/system/switchdeck-diagnostics.timer','[Unit]\nDescription=Save boot diagnostics to SD\n[Timer]\nOnBootSec=45s\nOnUnitActiveSec=60s\n[Install]\nWantedBy=timers.target\n')
    for n in ['NetworkManager','bluetooth','switchdeck-hardware','switchdeck-wifi-import']:link('etc/systemd/system/multi-user.target.wants/'+n+'.service','/usr/lib/systemd/system/'+n+'.service')
    link('etc/systemd/system/multi-user.target.wants/joycond.service','../joycond.service')
    link('etc/systemd/system/timers.target.wants/switchdeck-diagnostics.timer','/usr/lib/systemd/system/switchdeck-diagnostics.timer')
    link('etc/systemd/system/default.target','/usr/lib/systemd/system/graphical.target')
    link('etc/systemd/system/display-manager.service','/usr/lib/systemd/system/sddm.service')
    for n in ['systemd-gpt-auto-generator','ostree-system-generator','zram-generator','podman-system-generator']:link('etc/systemd/system-generators/'+n,'/dev/null')
    link('etc/resolv.conf','/run/systemd/resolve/stub-resolv.conf');link('var/lib/dbus/machine-id','/etc/machine-id')
    write('home/steamos/.config/openbox/rc.xml','''<?xml version="1.0" encoding="UTF-8"?>
<openbox_config xmlns="http://openbox.org/3.4/rc"><keyboard>
<keybind key="W-space"><action name="Execute"><command>/usr/local/bin/switchdeck-menu</command></action></keybind>
<keybind key="W-Return"><action name="Execute"><command>xterm</command></action></keybind>
<keybind key="A-F4"><action name="Close"/></keybind></keyboard>
<applications><application class="Steam"><maximized>yes</maximized></application></applications></openbox_config>
''')
    for n in ['dev','proc','sys','run','tmp','root','boot','var/log','var/lib/sddm','home/steamos/.local/state/switchdeck']:(root/n).mkdir(parents=True,exist_ok=True)
    (root/'tmp').chmod(0o1777)
    return masks
