import importlib.util,json,os,stat,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import zram_control as z


class Policy(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.p=Path(self.t.name);self.s=self.p/'sys';self.s.mkdir()
        self.cfg=self.p/'policy.json';self.swaps=self.p/'swaps';self.swaps.write_text('Filename Type Size Used Priority\n')
        for n,v in {'disksize':str(3*z.GIB),'comp_algorithm':'lzo [lz4]','mm_stat':'1048576 524288 655360 0 0 0 0'}.items():(self.s/n).write_text(v)
        for key,val in [('SYS',self.s),('POLICY',self.cfg),('SWAPS',self.swaps)]:
            p=patch.object(z,key,val);p.start();self.addCleanup(p.stop)

    def active(self):self.swaps.write_text('Filename Type Size Used Priority\n/dev/zram0 partition 3145724 1024 5\n')

    def test_save_never_changes_active_swap(self):
        self.active();before={p.name:p.read_bytes() for p in self.s.iterdir()}
        with patch.object(z.subprocess,'run') as run:
            r=z.save_policy(0);self.assertTrue(r['zram']['pending']);self.assertTrue(r['zram']['active']);run.assert_not_called()
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.s.iterdir()})
        self.assertEqual(z.read_policy(),0)

    def test_allowlist_rejects_bad_types_and_injection(self):
        for v in (True,False,-1,8,'4','0;swapoff -a',1.5,{},[]):
            with self.assertRaises(ValueError):z.save_policy(v)

    def test_default_matches_existing_three_gib(self):
        self.active();r=z.status();self.assertIsNone(r['configured_size_gib']);self.assertFalse(r['pending'])
        self.assertEqual(r['ram_bytes'],655360);self.assertEqual(r['algorithm'],'lz4')

    def test_pending_and_saved_choice_are_distinct_from_actual(self):
        self.active();r=z.save_policy(4)['zram'];self.assertTrue(r['pending'])
        self.assertEqual(r['size_bytes'],3*z.GIB);self.assertEqual(r['configured_size_gib'],4)

    def test_symlink_policy_refused(self):
        target=self.p/'other';target.write_text('keep');self.cfg.symlink_to(target)
        with self.assertRaises(OSError):z.read_policy()
        with self.assertRaises(ValueError):z.save_policy(2)
        self.assertEqual(target.read_text(),'keep')

    def test_boot_does_not_reset_active_even_when_off_saved(self):
        self.active();z.save_policy(0)
        with patch.object(z.subprocess,'run') as run:
            self.assertEqual(z.boot()['status'],'kept_existing');run.assert_not_called()

    def test_off_boot_does_not_create_swap(self):
        (self.s/'disksize').write_text('0');z.save_policy(0)
        with patch.object(z.subprocess,'run') as run:
            self.assertEqual(z.boot()['status'],'disabled');run.assert_not_called()

    def test_boot_creates_only_fixed_device_with_lz4(self):
        (self.s/'disksize').write_text('0');(self.s/'dev').write_text('252:0');z.save_policy(1)
        real=os.stat
        def fake_stat(path,*args,**kw):
            if path==z.DEV:return type('S',(),{'st_mode':stat.S_IFBLK,'st_rdev':os.makedev(252,0)})()
            return real(path,*args,**kw)
        def command(args,**kw):
            if args[0]=='/usr/bin/swapon':self.active()
        with patch.object(z.os,'stat',side_effect=fake_stat),patch.object(z.subprocess,'run',side_effect=command) as run:
            self.assertEqual(z.boot()['status'],'created')
            self.assertEqual([c.args[0] for c in run.call_args_list],[['/usr/bin/mkswap','/dev/zram0'],['/usr/bin/swapon','-p','5','/dev/zram0']])
        self.assertEqual((self.s/'disksize').read_text().strip(),str(z.GIB))


if __name__=='__main__':unittest.main(verbosity=2)
