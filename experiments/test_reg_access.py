"""Linux helper transaction tests; decoding tests run on any controller OS."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import reg_access


@unittest.skipUnless(sys.platform.startswith('linux'),'Linux flock is required')
class RegisterTransactionTests(unittest.TestCase):
    def fixture(self,d):
        p=Path(d);(p/'regidx').write_text('0x00008014\n');(p/'regval').write_text('0x20145\n');return p
    def test_restore_and_read(self):
        with tempfile.TemporaryDirectory() as d:
            p=self.fixture(d);r=reg_access.read_registers(p,[0x30],str(p/'lock'))
            self.assertEqual(r['values'],{'0x30':'0x20145'});self.assertTrue(r['selector_restored'])
            self.assertEqual((p/'regidx').read_text(),'0x00008014\n')
    def test_no_clear_on_read_MIB_allowed(self):
        with self.assertRaises(ValueError):reg_access.read_registers('/nonexistent',[0x8090])
    def test_restore_after_register_read_error(self):
        with tempfile.TemporaryDirectory() as d:
            p=self.fixture(d);(p/'regval').unlink()
            with self.assertRaises(OSError):reg_access.read_registers(p,[0x30],str(p/'lock'))
            self.assertEqual((p/'regidx').read_text(),'0x00008014\n')
    def test_symlink_lock_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=self.fixture(d);(p/'target').touch();(p/'lock').symlink_to(p/'target')
            with self.assertRaises(OSError):reg_access.read_registers(p,[0x30],str(p/'lock'))


if __name__=='__main__':unittest.main()
