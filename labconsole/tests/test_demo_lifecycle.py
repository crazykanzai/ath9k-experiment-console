"""No-node coverage for final-channel races, optional monitor and reuse."""
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock
from labconsole.config import default_platform,enabled_roles,validate_platform
from labconsole.finish_protocol import heartbeat,await_clean_exits
from labconsole.real_demo import validate_request,NS,Session
from labconsole.preparation import PreparationHub,SSHPreparation
from labconsole.storage import Store


class DemoLifecycleTests(unittest.TestCase):
    def fake_session(self,when,events):
        now=[0.]
        s=types.SimpleNamespace(last_heartbeat=-2,workers={'AP':Mock()},finished={},
             request=dict(warmup_s=5,measure_s=600,tail_s=5),state={'T0_ns':100*NS},event=Mock())
        s.workers['AP'].send.side_effect=OSError('Socket is closed')
        def receive(**kw):
            now[0]+=.1
            if events:events.pop(0)(s)
        s.receive=receive
        return s,dict(monotonic=lambda:now[0],wall_ns=lambda:when)

    def test_finish_race_requires_successful_receipt_and_clean_eof(self):
        events=[lambda s:s.finished.update(AP=dict(completed_schedule=True,error=None)),
                lambda s:s.state.update(process_exit_codes={'AP':0})]
        s,clock=self.fake_session(710*NS,events)
        heartbeat(s,**clock)
        s.event.assert_called_once()
        await_clean_exits(s)

    def test_early_close_missing_eof_and_bad_schedule_remain_failures(self):
        s,clock=self.fake_session(709*NS,[])
        with self.assertRaisesRegex(OSError,'Socket is closed'):heartbeat(s,**clock)
        for events in ([],[lambda s:s.finished.update(AP=dict(completed_schedule=True,error=None))],
              [lambda s:(s.finished.update(AP=dict(completed_schedule=False,error='bad')),s.state.update(process_exit_codes={'AP':0}))]):
            s,clock=self.fake_session(710*NS,events)
            with self.assertRaises(RuntimeError):heartbeat(s,**clock,grace_s=.5)

    def test_optional_monitor_full_preparation_cleanup_and_same_preparation_reuse(self):
        from labconsole.tests.test_preparation import FakeBackend,fast_wait,wait_state
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);hub=PreparationHub(store,FakeBackend,fast_wait)
            platform=default_platform();platform['monitor_enabled']=False
            try:
                v=hub.create(platform,'memory-only','memory-only','five-node-offline',cidr='192.168.50.1/24')
                identity=v['id'];wait_state(hub,identity,{'checked'})
                self.assertEqual(len(hub.roles),5)
                with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0],5)
                hub.action(identity,'restore');wait_state(hub,identity,{'radio_ready'})
                hub.action(identity,'sync');wait_state(hub,identity,{'ready'})
                backend=hub.backend
                for owner in ['round1','round2']:
                    self.assertIs(hub.claim_demo(identity,owner,Mock()),backend)
                    hub.release_demo(owner,True)
                    self.assertTrue(hub.get(identity)['preparation_ready'])
                self.assertEqual(set(hub.active['nodes']),set(enabled_roles(platform)))
                self.assertNotIn('MON15',hub.active['clock_intents'])
                hub.action(identity,'stop');wait_state(hub,identity,{'ended'})
                self.assertFalse(any(role=='MON15' for role,op,args in backend.calls))
                with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0],0)
            finally:hub.close()

    def test_disabled_monitor_cannot_be_connected_accidentally_and_old_snapshots_stay_six(self):
        self.assertEqual(len(enabled_roles(default_platform())),6)
        for v in ('false',0,None):
            p=default_platform();p['monitor_enabled']=v
            with self.assertRaises(ValueError):validate_platform(p)
        backend=object.__new__(SSHPreparation);backend.platform={'monitor_enabled':False}
        with self.assertRaisesRegex(ValueError,'未启用'):backend.client('MON15')

    def test_duration_is_bounded_and_no_monitor_score_is_explicit(self):
        base=dict(preparation_id='a'*32,request_key='duration-offline',mode='unified',authorize=True)
        for duration in (60,300,600,1200):self.assertEqual(validate_request({**base,'measure_s':duration})['measure_s'],duration)
        for duration in (0,True,601,3600):
            with self.assertRaises(ValueError):validate_request({**base,'measure_s':duration})
        with tempfile.TemporaryDirectory() as folder:
            s=Session(Path(folder),None,validate_request(base));s.roles=tuple(enabled_roles({'monitor_enabled':False}))
            s.state['points']=[dict(second=k,total_Mbps=12,queue={n:[2,3] for n in s.roles if n.startswith('STA')}) for k in (8,9,10)]
            self.assertEqual(s.continuous_score(0),12)


if __name__=='__main__':unittest.main()
