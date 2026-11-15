import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from rl_wifi.quick_clock import validate,QuickSSHPreparation
from rl_wifi.quick_screen import QuickPreparation,carried_windows,ORDER
from rl_wifi.tests.test_clock_policy import ClockPolicyTests


class QuickClockTests(unittest.TestCase):
    def test_original94ms_sample_passes_only_relaxed_continuous_check(self):
        ClockPolicyTests.run_sample(self,QuickPreparation,-47.447865,47.182750,settled=False)
        self.assertEqual(self.hub.active['rl_clock_policy']['continuous_envelope_limit_ms'],100)
        self.assertEqual(len(self.hub.active['events']),6)
        with self.assertRaises(RuntimeError):
            ClockPolicyTests.run_sample(self,QuickPreparation,-47.447865,47.182750,settled=True)

    def test_runtime100_edge_and_source_gate_remain(self):
        ClockPolicyTests.run_sample(self,QuickPreparation,99.2,.8,settled=False)
        with self.assertRaises(RuntimeError):ClockPolicyTests.run_sample(self,QuickPreparation,99.3,.8,settled=False)
        with self.assertRaises(RuntimeError):
            ClockPolicyTests.run_sample(self,QuickPreparation,1,settled=False,status='NTPSynchronized=no\nServer: 192.168.100.10')

    def test_session_checker_includes_uncertainty_and_keeps_raw_receipt(self):
        row={'offset_ms':-47.447865,'uncertainty_ms':47.182750,'envelope_ms':94.630615,
             'status':'NTPSynchronized=yes\nServer: 192.168.100.10'}
        validate({'STA4':row},'192.168.100.10')
        with self.assertRaises(RuntimeError):validate({'STA4':row},'192.168.100.10',50)
        row['envelope_ms']=1
        with self.assertRaises(RuntimeError):validate({'STA4':row},'192.168.100.10')

    def test_clock_uses_dedicated_backend_not_bulk_transport(self):
        backend=QuickSSHPreparation.__new__(QuickSSHPreparation)
        backend.clock_backend=Mock();backend.client=Mock()
        backend.clock_backend.clock.return_value={'offset_ms':1}
        self.assertEqual(backend.clock('STA1')['transport'],'dedicated_clock_SSH')
        backend.clock_backend.clock.assert_called_once_with('STA1');backend.client.assert_not_called()

    def test_carry_only_exact_completed_first_slot_no_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);identity='a'*32;(root/identity).mkdir()
            state={'acceptance':{'passed':True},'restoration':{'confirmed':True}}
            (root/identity/'session.json').write_text(json.dumps(state))
            rows=[{'slot':i+1,'block':b,'config':c,'state':s,'id':identity if i==0 else 'b'*32,
                   'status':'completed' if i==0 else 'not_started'} for i,(b,c,s) in enumerate(ORDER)]
            path=root/'manifest.json';path.write_text(json.dumps({'windows':rows,'postflight':{'confirmed':True}}))
            carried=carried_windows(root)
            self.assertEqual(len(carried),1);self.assertEqual(carried[0]['slot'],1)
            rows[1]['status']='failed';path.write_text(json.dumps({'windows':rows,'postflight':{'confirmed':True}}))
            with self.assertRaises(ValueError):carried_windows(root)

    def test_three_captured_slots_reassess_only_arrival_diagnostic_without_writing_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);rows=[]
            for i,(b,c,s) in enumerate(ORDER):
                identity=f'{i+1:032x}'
                metric={'inference_usable':i!=2,'issues':[],
                        'application':{'counts':{'local_failure':3 if i==2 else 0},
                          'generation_delay_p99_ms':5.602 if i==2 else 1.,'generator_quality_passed':i!=2}}
                rows.append({'slot':i+1,'block':b,'config':c,'state':s,'id':identity,
                             'status':'completed' if i<3 else 'not_started','metrics':metric})
                if i<3:
                    (root/identity).mkdir();(root/identity/'session.json').write_text(json.dumps(
                        {'acceptance':{'passed':True},'restoration':{'confirmed':True}}))
            path=root/'manifest.json';path.write_text(json.dumps({'windows':rows,'postflight':{'confirmed':True}}))
            before=path.read_bytes();carried=carried_windows(root)
            self.assertEqual(len(carried),3);self.assertTrue(carried[2]['metrics']['inference_usable'])
            self.assertFalse(carried[2]['arrival_quality_reassessment']['original_inference_usable'])
            self.assertEqual(path.read_bytes(),before)


if __name__=='__main__':unittest.main()
