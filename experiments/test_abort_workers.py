"""Local-only fake-node tests. Never connects or launches traffic."""
import unittest
from unittest.mock import patch
from sync_run import abort_workers

class AbortTests(unittest.TestCase):
 def test_preserves_initial_and_original_errors(self):
  initial={'finished':None,'errors':['association lost']}
  final={'finished':{'epoch_ns':9},'errors':['association lost'],'processes':{'client':{'exit_code':1}}}
  with patch('sync_run.read_state',side_effect=[initial,final]),patch('sync_run.node_send',return_value={'ok':True}):
   r=abort_workers({'STA3':object()},{'STA3':'x'})
  self.assertEqual(r['initial_states']['STA3'],initial)
  self.assertEqual(r['final_states']['STA3']['processes']['client']['exit_code'],1)
  self.assertTrue(r['all_finished']);self.assertIn('dispatch_begin',r['dispatch']['STA3'])
 def test_missing_state_times_out_and_no_fake_completion(self):
  ticks=iter([0,0,1]);sleeps=[]
  with patch('sync_run.read_state',return_value=None),patch('sync_run.node_send',return_value={'ok':True}):
   r=abort_workers({'STA1':object()},{'STA1':'x'},timeout_s=0,now=lambda:next(ticks),sleep=sleeps.append)
  self.assertTrue(r['wait_timed_out']);self.assertEqual(r['pending_nodes'],['STA1']);self.assertFalse(sleeps)
 def test_delivery_error_does_not_erase_readable_final_state(self):
  with patch('sync_run.read_state',return_value={'finished':True,'errors':['original']}),patch('sync_run.node_send',side_effect=OSError('SSH failed')):
   r=abort_workers({'STA2':object()},{'STA2':'x'})
  self.assertIn('error',r['dispatch']['STA2']);self.assertTrue(r['all_finished'])
  self.assertEqual(r['final_states']['STA2']['errors'],['original'])
 def test_state_read_error_remains_explicit(self):
  with patch('sync_run.read_state',side_effect=OSError('missing')),patch('sync_run.node_send',return_value={'ok':True}):
   r=abort_workers({'STA1':object()},{'STA1':'x'},timeout_s=0)
  self.assertIn('STA1',r['state_read_errors']);self.assertEqual(r['pending_nodes'],['STA1'])
 def test_exit_annotation_is_order_only_and_exit_code_preserved(self):
  s={'finished':True,'errors':['original'],'processes':{'client':{'finished':{'epoch_ns':10**25},'exit_code':1}}}
  with patch('sync_run.read_state',return_value=s),patch('sync_run.node_send',return_value={'ok':True}):
   r=abort_workers({'STA1':object()},{'STA1':'x'})
  self.assertEqual(r['exit_order_annotations']['STA1']['client'],'after_abort_dispatch_order_only')
  self.assertEqual(s['processes']['client']['exit_code'],1)
 def test_real_AP_metadata_and_null_timestamps_do_not_mask_original_error(self):
  s={'finished':True,'errors':['original'],'processes':{
   'server_5202_idle_stop':'unused single-STA server, stopped locally after absolute end',
   'client':{'finished':None,'exit_code':1},'other':{'finished':{'epoch_ns':None}}}}
  with patch('sync_run.read_state',return_value=s),patch('sync_run.node_send',return_value={'ok':True}):
   r=abort_workers({'AP':object()},{'AP':'x'})
  self.assertTrue(r['all_finished'])
  self.assertEqual(r['final_states']['AP'],s)
  self.assertEqual(r['exit_order_annotations']['AP']['server_5202_idle_stop'],'metadata_not_a_process_record')

if __name__=='__main__':unittest.main()
