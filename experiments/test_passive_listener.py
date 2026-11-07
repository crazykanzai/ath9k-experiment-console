"""No radio/network: lifecycle failure gates and CLI scope regression."""
import json,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from passive_listener import PassiveSession
class PassiveLifecycleTests(unittest.TestCase):
    def session(self,p):
        s=PassiveSession.__new__(PassiveSession);s.folder=p;s.out='/home/EXPERIMENT_USER/apsta-air/test';s.record={};s.remote=Mock();return s
    def test_stale_finished_and_error_states_not_ready(self):
        for z in [None,{'armed':{}},{'armed':{'x':1},'finished':1},{'armed':{'x':1},'errors':['capture exited']}]:
            with self.subTest(z=z),tempfile.TemporaryDirectory() as d:
                s=self.session(Path(d));s.read_state=lambda:z
                with self.assertRaises(RuntimeError):s.ready()
    def test_ready_and_poll_error_gate(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.session(Path(d));s.read_state=lambda:{'armed':{'x':1},'errors':[]};s.ready();s.poll()
            s.read_state=lambda:{'errors':['tcpdump failed']}
            with self.assertRaises(RuntimeError):s.poll()
    def test_finished_capture_still_requires_clock_gate(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.session(Path(d));s.read_state=lambda:{'finished':1,'valid':True,'errors':[]}
            with patch('passive_listener.probe',return_value={'within_50ms_proven':False}):
                with self.assertRaises(RuntimeError):s.finish()
    def test_cleanup_waits_after_acknowledged_abort_without_resending(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.session(Path(d));s.record['abort_ack']={'ok':True}
            final={'finished':{'epoch_ns':42},'errors':["RuntimeError('controller abort')"]}
            s.read_state=Mock(side_effect=[{'armed':True},final])
            s.send=Mock(side_effect=AssertionError('must not send a second abort'))
            s.remote.file.return_value=None
            s.cleanup()
            s.send.assert_not_called()
            self.assertEqual(s.record['cleanup_state'],final)
            s.remote.get_tree.assert_called_once()
            s.remote.close.assert_called_once()
    def test_passive_cli_rejects_absent_spec_before_SSH(self):
        p=Path(__file__).with_name('sync_run.py')
        for flags in [['--formal','--active-stas','STA1'],['--dry-run','--active-stas','STA2']]:
            r=subprocess.run([sys.executable,str(p),*flags,'--cohort-diagnostic','--bianchi-dai','--access-mode','basic','--passive-listener-spec','absent.json'],capture_output=True,text=True,timeout=10)
            self.assertEqual(r.returncode,2);self.assertNotIn('Clock offset',r.stdout)
if __name__=='__main__':unittest.main()
