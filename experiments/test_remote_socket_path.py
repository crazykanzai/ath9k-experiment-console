import unittest
from pathlib import Path
from unittest.mock import patch
from sync_run import remote_dir,remote_run_id,workers_ready,require_unused_remote_directories,OUTPUT_ROOT


class RemoteSocketPathTests(unittest.TestCase):
    def test_long_and_unicode_labels_stay_within_AF_UNIX_limit(self):
        label='2026-10-18_Basic_CW31-255_dryrun_cohort_FIFO_full_BianchiDai_STA1_single_01_实验'*3
        d=remote_dir(label,'STA1')
        self.assertLess(len((d+'/control.sock').encode()),108)
        self.assertEqual(d,remote_dir(label,'STA1'))
        self.assertNotEqual(d,remote_dir(label+'2','STA1'))
    def test_short_existing_directory_mapping_is_preserved(self):
        self.assertTrue(remote_dir('short','AP').endswith('/short/AP'))
    def test_identical_leaf_labels_in_distinct_batches_are_separate(self):
        a=remote_run_id(OUTPUT_ROOT/'batch01'/'precheck_W8')
        b=remote_run_id(OUTPUT_ROOT/'batch02'/'precheck_W8')
        self.assertNotEqual(remote_dir(a,'AP'),remote_dir(b,'AP'))
    def test_finished_armed_state_is_rejected(self):
        with self.assertRaises(RuntimeError):workers_ready({'AP':{'armed':{'epoch_ns':10**18},'finished':True}},10**18)
    def test_stale_armed_state_is_rejected(self):
        with self.assertRaises(RuntimeError):workers_ready({'AP':{'armed':{'epoch_ns':1}}},10**18)
    def test_current_armed_and_not_yet_armed(self):
        self.assertFalse(workers_ready({'AP':None},10**18))
        self.assertTrue(workers_ready({'AP':{'armed':{'epoch_ns':10**18}}},10**18))
    def test_existing_remote_directory_rejected_before_worker_start(self):
        remote=unittest.mock.Mock();remote.run.return_value={'code':1}
        with self.assertRaises(RuntimeError):require_unused_remote_directories({'AP':remote},{'AP':'/historical/AP'})


if __name__=='__main__':unittest.main()
