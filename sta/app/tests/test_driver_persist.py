import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('driver_persist',Path(__file__).parents[1]/'scripts/driver_persist.py')
dp=importlib.util.module_from_spec(spec);spec.loader.exec_module(dp)

class DriverPersistTests(unittest.TestCase):
    def test_failed_rts_candidate_cannot_be_installed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);manifest,inspect=self.fixture(root);manifest['rts_retry_abi']=2
            with self.assertRaisesRegex(ValueError,'unapproved RTS ABI'):
                dp.validate_bundle(root,manifest,manifest['kernel'],inspect)
            p=root/'ath9k.ko';p.write_bytes(p.read_bytes()+b' rts_retry_abi=2')
            manifest['modules']['ath9k']['sha256']=dp.digest(p)
            with self.assertRaisesRegex(ValueError,'unapproved RTS ABI'):
                dp.validate_bundle(root,manifest,manifest['kernel'],inspect)
    def fixture(self,root):
        manifest={'kernel':'5.15.0-139-generic','basic_retry_abi':2,'hol_abi':1,'modules':{}}
        for name in dp.NAMES:
            path=root/(name+'.ko');path.write_bytes((name+' basic_retry_abi=2').encode())
            manifest['modules'][name]={'sha256':dp.digest(path),'srcversion':'C6E744C117E5F6ABADF932F'}
        def inspect(args):return manifest['kernel']+' SMP' if args[2]=='vermagic' else 'C6E744C117E5F6ABADF932F'
        return manifest,inspect

    def test_exact_bundle_and_23_digit_srcversion_pass(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);manifest,inspect=self.fixture(root)
            dp.validate_bundle(root,manifest,manifest['kernel'],inspect)

    def test_corrupt_module_rejected_before_commands(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);manifest,inspect=self.fixture(root);(root/'ath9k.ko').write_bytes(b'bad')
            with self.assertRaisesRegex(ValueError,'checksum mismatch'):dp.validate_bundle(root,manifest,manifest['kernel'],inspect)

    def test_kernel_mismatch_and_incomplete_set_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);manifest,inspect=self.fixture(root)
            with self.assertRaisesRegex(ValueError,'kernel'):dp.validate_bundle(root,manifest,'other-kernel',inspect)
            del manifest['modules']['mac80211']
            with self.assertRaisesRegex(ValueError,'four'):dp.validate_bundle(root,manifest,manifest['kernel'],inspect)

    def test_invalid_vermagic_or_srcversion_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);manifest,inspect=self.fixture(root)
            with self.assertRaisesRegex(ValueError,'vermagic'):dp.validate_bundle(root,manifest,manifest['kernel'],lambda args:'bad')
            def badsrc(args):return manifest['kernel']+' SMP' if args[2]=='vermagic' else 'BAD'
            with self.assertRaisesRegex(ValueError,'srcversion'):dp.validate_bundle(root,manifest,manifest['kernel'],badsrc)

    def test_kernel_path_escape_rejected(self):
        for kernel in ['../boot','/','5.15/foo','', 'a;true']:
            with self.assertRaises(ValueError):dp.kernel_dir(kernel)

    def test_abi_marker_required_in_binary(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);manifest,inspect=self.fixture(root)
            (root/'ath9k.ko').write_bytes(b'legacy');manifest['modules']['ath9k']['sha256']=dp.digest(root/'ath9k.ko')
            with self.assertRaisesRegex(ValueError,'marker'):dp.validate_bundle(root,manifest,manifest['kernel'],inspect)

    def test_backups_preserve_original_and_record_absent_files(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);backup=root/'backup';backup.mkdir();p=root/'existing';p.write_text('original')
            record={'files':[]};dp.backup_file(p,backup,record);dp.backup_file(root/'absent',backup,record)
            dp.atomic_text(p,'replacement')
            self.assertEqual(Path(record['files'][0]['saved']).read_text(),'original')
            self.assertFalse(record['files'][1]['existed'])
            self.assertEqual(json.loads((backup/'record.json').read_text())['files'][0]['sha256'],dp.digest(record['files'][0]['saved']))

    def test_rollback_restores_exact_files_without_recursive_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);backup=root/'backup';backup.mkdir();target=root/'installed';target.mkdir()
            original=target/'old';original.write_text('original');untouched=target/'user-file';untouched.write_text('keep')
            record={'kernel':dp.platform.release(),'files':[],'module_dir':str(target),'unit_enabled_before':True}
            dp.backup_file(original,backup,record);dp.backup_file(target/'created',backup,record)
            original.write_text('replacement');(target/'created').write_text('new')
            with patch.object(dp,'command',return_value='') as commands,patch.object(dp,'kernel_dir',return_value=target),patch.object(dp.os,'chown',create=True):
                result=dp.restore(backup)
            self.assertEqual(result['status'],'rolled_back')
            self.assertEqual(original.read_text(),'original')
            self.assertEqual(untouched.read_text(),'keep')
            self.assertFalse((target/'created').exists())
            self.assertEqual(commands.call_count,2)

if __name__=='__main__':unittest.main()
