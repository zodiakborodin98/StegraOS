import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import switch_devices as d


class Devices(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.sys=self.base/'class/input';self.sys.mkdir(parents=True)

    def node(self,num,name,product,keys,axes=(0,1),virtual=False,vendor=0x057e):
        root=self.base/('devices/virtual/input' if virtual else 'devices/serial/input')/('input'+str(num))
        event=root/('event'+str(num));event.mkdir(parents=True)
        dev=root/'device';(dev/'id').mkdir(parents=True);(dev/'capabilities').mkdir()
        # sysfs event/device is a link to its parent input device in reality.
        for n,v in {'name':name,'id/vendor':hex(vendor)[2:],'id/product':hex(product)[2:]}.items():(dev/n).write_text(v)
        for n,values in [('key',keys),('abs',axes)]:
            mask=sum(1<<k for k in values);words=[]
            while mask:words.insert(0,format(mask&((1<<64)-1),'x'));mask>>=64
            (dev/'capabilities'/n).write_text(' '.join(words) or '0')
        (event/'device').symlink_to(dev,target_is_directory=True)
        (self.sys/event.name).symlink_to(event,target_is_directory=True)
        return '/dev/input/'+event.name

    def test_lite_preserved(self):
        p=self.node(3,'Nintendo Switch Lite Gamepad',0x2017,[304,305,309,314,544,545,546,547])
        self.assertEqual(d.hotkey_devices(309,self.sys),[p]);self.assertEqual(d.panel_device(self.sys),p)

    def test_oled_raw_left_for_holds_merged_for_panel(self):
        left=self.node(6,'Nintendo Switch Left Joy-Con Serial',0x2006,[309,314,544,545,546,547])
        self.node(7,'Nintendo Switch Right Joy-Con Serial',0x2007,[304,305])
        combined=self.node(8,'Nintendo Switch Combined Joy-Cons',0x2008,[304,305,309,314,544,545,546,547],virtual=True)
        self.assertEqual(d.hotkey_devices(314,self.sys),[left])
        self.assertEqual(d.hotkey_devices(309,self.sys),[left])
        self.assertEqual(d.hotkey_devices(314,self.sys,prefer_physical=False),[combined])
        self.assertEqual(d.panel_device(self.sys),combined)

    def test_combined_fallback_after_raw_disconnect(self):
        p=self.node(8,'Nintendo Switch Combined Joy-Cons',0x2008,[304,305,309,314,544,545,546,547],virtual=True)
        self.assertEqual(d.hotkey_devices(309,self.sys),[p]);self.assertEqual(d.panel_device(self.sys),p)
        (self.sys/'event8').unlink()
        self.assertEqual(d.hotkey_devices(309,self.sys),[])

    def test_steam_virtual_and_spoofed_names_excluded(self):
        self.node(9,'Microsoft X-Box 360 pad 0',0x11ff,[309,314],virtual=True,vendor=0x28de)
        self.node(10,'Nintendo Switch Lite Gamepad',0x2017,[309,314],virtual=True)
        self.node(11,'Nintendo Switch Combined Joy-Cons',0x11ff,[309,314],virtual=True,vendor=0x28de)
        self.assertEqual(d.controllers(self.sys),[])

    def test_wrong_key_or_incomplete_panel_not_selected(self):
        self.node(6,'Nintendo Switch Left Joy-Con Serial',0x2006,[314])
        self.assertEqual(d.hotkey_devices(309,self.sys),[]);self.assertIsNone(d.panel_device(self.sys))

    def test_missing_device_files_skipped(self):
        (self.sys/'event1').mkdir()
        self.assertEqual(d.controllers(self.sys),[])

    def test_rescue_action_still_fixed_and_release_required(self):
        import sys
        sys.path.insert(0,str(Path(__file__).parent))
        spec=importlib.util.spec_from_file_location('patched_rescue',Path(__file__).parent/'session-rescue.py')
        r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
        h=r.Hold(True);self.assertFalse(h.sample(True,10));h.sample(False,11)
        h.sample(True,12);self.assertFalse(h.sample(True,14.9));self.assertTrue(h.sample(True,15));self.assertFalse(h.sample(True,19))
        with patch.object(r.subprocess,'run') as call:
            r.Action(True).fire(10);call.assert_not_called()
            call.return_value.returncode=0;call.return_value.stderr=''
            a=r.Action(False);a.fire(10);a.fire(11)
            self.assertEqual(call.call_count,1)
            self.assertEqual(call.call_args.args[0],['/usr/bin/systemctl','--no-block','restart','steam-cpu-session.service'])


if __name__=='__main__':unittest.main(verbosity=2)
