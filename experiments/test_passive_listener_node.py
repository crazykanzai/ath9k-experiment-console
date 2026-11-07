"""Regression for the real two-thread listener state-save failure."""
import concurrent.futures,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import passive_listener_node as node

class StateSaveTest(unittest.TestCase):
    def test_concurrent_state_saves_are_serialized(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'state.json'
            original=node.os.replace
            def delayed_replace(a,b):
                # Enlarge the real write/rename interleaving, without mocking IO.
                time.sleep(.002)
                return original(a,b)
            with patch.object(node.os,'replace',delayed_replace):
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    futures=[pool.submit(node.save,target,{'writer':i,'plan':{'t0_ns':i}}) for i in range(64)]
                    for f in futures:f.result()
            result=json.loads(target.read_text())
            self.assertIn(result['writer'],range(64))
            self.assertEqual(result['plan']['t0_ns'],result['writer'])
            self.assertFalse(target.with_name('state.json.tmp').exists())

    def test_sequential_updates_leave_complete_json(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'state.json'
            node.save(target,{'armed':True,'plan':None})
            node.save(target,{'armed':True,'plan':{'t0_ns':42}})
            self.assertEqual(json.loads(target.read_text()),{'armed':True,'plan':{'t0_ns':42}})

if __name__=='__main__':unittest.main()
