import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rl_wifi.quick_retry import supervise,can_retry,PREPARATION_TIMING
from rl_wifi.quick_screen import carried_windows,ORDER


class RetryTests(unittest.TestCase):
    def simulate(self,statuses,restored=True):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
        (root/'attempt_01').mkdir();calls=[]
        def execute(folder,plan,workspace,password):
            calls.append(folder.name);(folder/'controller').mkdir(exist_ok=True)
            value={'status':statuses[len(calls)-1],'error':'simulated gate' if statuses[len(calls)-1]!='completed_eight' else None,
                   'postflight':{'confirmed':restored},'remaining_reservations':[]}
            (folder/'manifest.json').write_text(json.dumps(value))
            (folder/'controller/preparation_final.json').write_text(json.dumps({'cleanup':{'confirmed':restored}}))
        with patch('rl_wifi.quick_retry.verify_plan',return_value={'new_windows':5}),\
             patch('rl_wifi.quick_retry.execute',side_effect=execute),patch('rl_wifi.quick_retry.seal') as seal:
            supervise(root,'fake','in-memory-test-only')
        return json.loads((root/'supervisor.json').read_text()),calls,seal

    def test_gate_then_success_retries_once_without_new_password_or_reboot(self):
        result,calls,seal=self.simulate(['stopped_at_gate','completed_eight'])
        self.assertEqual(calls,['attempt_01','attempt_02']);self.assertEqual(result['status'],'completed_eight')
        self.assertEqual(seal.call_count,1);self.assertFalse(result['device_reboots_authorized'])
        self.assertEqual(PREPARATION_TIMING,(90,0,0,10))

    def test_second_gate_stops_without_third_attempt(self):
        result,calls,_=self.simulate(['stopped_at_gate','stopped_at_gate'])
        self.assertEqual(len(calls),2);self.assertEqual(result['status'],'stopped_after_retry')

    def test_unconfirmed_restoration_never_retries(self):
        result,calls,seal=self.simulate(['stopped_at_gate'],False)
        self.assertEqual(len(calls),1);self.assertEqual(seal.call_count,0)

    def test_failed_slot_retained_in_source_and_not_carried_as_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);rows=[]
            for i,(b,c,s) in enumerate(ORDER):
                identity=f'{i+1:032x}'
                rows.append({'slot':i+1,'id':identity,'block':b,'config':c,'state':s,
                             'status':'completed' if i==0 else 'failed' if i==1 else 'not_started'})
                if i==0:
                    (root/identity).mkdir();(root/identity/'session.json').write_text(json.dumps(
                        {'acceptance':{'passed':True},'restoration':{'confirmed':True}}))
            path=root/'manifest.json';path.write_text(json.dumps({'windows':rows,'postflight':{'confirmed':True}}))
            before=path.read_bytes()
            with self.assertRaises(ValueError):carried_windows(root)
            carried=carried_windows(root,allow_retry=True)
            self.assertEqual(len(carried),1);self.assertEqual(path.read_bytes(),before)


if __name__=='__main__':unittest.main()
