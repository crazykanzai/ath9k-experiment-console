import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from labconsole.live import SCHEMA, Observation, LiveHub
from labconsole.server import ConsoleServer
from labconsole.readonly_probe import parse_hardware_cw


def sample(seq, role='STA1', cw=31, generation=1, failure=0, running=True, mode='basic', uncertainty=1):
    data = {'cw': {'cwmin':cw,'cwmax':255,'aifsn':3,'basis':'hardware_readback'},
            'dcf': {'generation':generation,'hol_samples':seq*100,'hol_sum_us':seq*80000,
                    'attempts':seq*120,'successes':seq*100,'packets':seq*100+failure,
                    'failures':failure,'running':running,'mode':mode,'basic_retry_abi':2}}
    if role=='AP':data={'rx_bytes':{'STA1':seq*1000000,'STA2':seq*500000}}
    return {'schema':SCHEMA,'kind':'sample','role':role,'seq':seq,'sample_ns':seq*10**9,
            'clock_uncertainty_ms':uncertainty,'source':'offline test only','data':data}


class IntervalTests(unittest.TestCase):
    def setUp(self):self.obs=Observation({'kind':'offline_test'})

    def test_interval_means_and_ap_bytes_not_aggregate_percentages(self):
        self.obs.ingest(sample(1));self.obs.ingest(sample(2))
        p=self.obs.points[-1]
        self.assertEqual(p['HoL_us'],800);self.assertEqual(p['p_ACK'],100/120)
        self.assertEqual(p['hol_samples'],100)
        self.obs.ingest(sample(1,'AP'));self.obs.ingest(sample(2,'AP'))
        self.assertEqual(self.obs.points[-1]['per_STA_Mbps'],{'STA1':8.,'STA2':4.})

    def test_cw_transition_interval_is_not_assigned_to_either_cw(self):
        self.obs.ingest(sample(1));self.obs.ingest(sample(2,cw=63))
        self.assertIsNone(self.obs.points[-1]['HoL_us']);self.assertIsNone(self.obs.points[-1]['p_ACK'])
        self.assertEqual(self.obs.events[-1]['earliest_ns'],10**9)
        self.assertEqual(self.obs.events[-1]['event'],'cw_readback_change')
        self.obs.ingest(sample(3,cw=63));self.assertEqual(self.obs.points[-1]['HoL_us'],800)

    def test_failures_and_rts_leave_probability_NA_keep_hol(self):
        for mode,failure in [('basic',1),('rts-cts',0)]:
            o=Observation({});o.ingest(sample(1,mode=mode));o.ingest(sample(2,mode=mode,failure=failure))
            self.assertIsNone(o.points[-1]['p_ACK']);self.assertEqual(o.points[-1]['HoL_us'],800)
            self.assertEqual(o.points[-1]['failures'],failure)

    def test_generation_reset_gap_clock_and_stale_not_carried_forward(self):
        for s in [sample(2,generation=2),sample(2,running=False),sample(2,uncertainty=51),sample(8)]:
            o=Observation({});o.ingest(sample(1));o.ingest(s)
            self.assertIsNone(o.points[-1]['HoL_us']);self.assertIsNone(o.points[-1]['p_ACK'])
        self.obs.ingest(sample(1));self.obs.ingest(sample(2));self.assertTrue(self.obs.snapshot()['latest']['STA1']['stale'])

    def test_validation_is_before_state_mutation(self):
        self.obs.ingest(sample(1))
        invalid=[sample(1),sample(2),sample(2),sample(2)]
        invalid[1]['data']['cw']['basis']='requested'
        invalid[2]['data']['dcf']['attempts']=float('nan')
        invalid[3]['sample_ns']=1
        for x in invalid:
            with self.assertRaises(ValueError):self.obs.ingest(x)
        self.assertEqual(len(self.obs.points),1)
        self.obs.ingest(sample(2));self.assertEqual(len(self.obs.points),2)

    def test_algorithm_requests_separate_from_hardware_verified_changes(self):
        self.obs.ingest({'schema':SCHEMA,'kind':'event','role':'algorithm','seq':1,'sample_ns':1,
            'clock_uncertainty_ms':1,'source':'future-algorithm','target':'STA1','event':'cw_requested','requested':{'cwmin':63}})
        self.assertEqual(self.obs.events[0]['event'],'cw_requested');self.assertEqual(self.obs.points,[])
        self.obs.ingest(sample(1));self.obs.ingest(sample(2,cw=63))
        self.assertEqual(len(self.obs.events),2)

    def test_hardware_readback_not_requested_or_cached_edca(self):
        text='AC=BE qnum=1 aifs=3 cwmin=15 cwmax=1023 hw_aifs=3 hw_cwmin=31 hw_cwmax=255 status_name=verified'
        self.assertEqual(parse_hardware_cw(text)['cwmin'],31)
        with self.assertRaises(ValueError):parse_hardware_cw(text.replace('verified','mismatch'))

    def test_remote_probe_has_no_parameter_writes_or_traffic(self):
        code=(Path(__file__).resolve().parents[1]/'readonly_probe.py').read_text(encoding='utf-8')
        for forbidden in ['write_text(', 'iperf', 'set txpower', 'set rts', 'regval', 'regidx\'']:
            self.assertNotIn(forbidden,code)


class FollowTests(unittest.TestCase):
    def test_partial_line_duplicate_file_reset_restart_and_no_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'telemetry.jsonl';source.write_text('',encoding='utf-8')
            with patch.object(socket.socket,'connect',side_effect=AssertionError('no network')):
                hub=LiveHub(root/'workspace')
                try:
                    m=hub.create(source,'offline','offline_test')
                    self.assertEqual(hub.create(source,'same','offline_test')['id'],m['id'])
                    obs=hub.observations[m['id']]
                    raw=json.dumps(sample(1))
                    with source.open('a',encoding='utf-8') as f:f.write(raw[:30])
                    with obs.lock:obs.follow();self.assertEqual(obs.points,[])
                    with source.open('a',encoding='utf-8') as f:f.write(raw[30:]+'\n'+json.dumps(sample(2))+'\n')
                    with obs.lock:obs.follow();self.assertEqual(len(obs.points),2)
                    before=source.read_bytes();hub.detach(m['id']);self.assertEqual(source.read_bytes(),before)
                    self.assertTrue(hub.get(m['id'])['ended'])
                finally:hub.close()
                again=LiveHub(root/'workspace')
                try:self.assertEqual(again.listing(),[])
                finally:again.close()

    def test_truncate_stops_instead_of_merging_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'a.jsonl';p.write_text(json.dumps(sample(1))+'\n')
            o=Observation({'source_path':str(p)});o.follow();p.write_text('')
            with self.assertRaises(ValueError):o.follow()
            self.assertTrue(o.ended)

    def test_get_retains_background_buffer_without_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'a.jsonl';p.write_text(json.dumps(sample(1))+'\n')
            hub=LiveHub(Path(tmp)/'workspace')
            try:
                m=hub.create(p,'test','offline_test')
                with p.open('a') as f:f.write(json.dumps(sample(2))+'\n')
                deadline=time.monotonic()+2
                while len(hub.get(m['id'])['points'])<2 and time.monotonic()<deadline:time.sleep(.05)
                self.assertEqual(len(hub.get(m['id'])['points']),2)
                self.assertFalse(hub.get(m['id'])['formal_result'])
            finally:hub.close()


class LiveWebTests(unittest.TestCase):
    def test_attach_and_detach_do_not_start_jobs_or_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'a.jsonl';p.write_text(json.dumps(sample(1))+'\n')
            server=ConsoleServer(('127.0.0.1',0),root/'workspace');t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
            def call(path,data=None):
                c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                c.request('POST' if data is not None else 'GET',path,json.dumps(data) if data is not None else None,{'X-Console-Token':server.csrf})
                r=c.getresponse();raw=r.read();code=r.status;c.close();return code,raw
            try:
                self.assertEqual(call('/live.js')[0],200)
                code,raw=call('/api/live/attach',{'source':str(p),'title':'test','kind':'offline_test'});self.assertEqual(code,200)
                identity=json.loads(raw)['session']['id'];self.assertEqual(call('/api/live?id='+identity)[0],200)
                code,raw=call('/api/live/export?id='+identity);self.assertEqual(code,200);self.assertFalse(json.loads(raw)['formal_result'])
                self.assertEqual(call('/api/live/detach',{'id':identity})[0],200)
                self.assertEqual(call('/api/live?id=../../../')[0],400)
                self.assertEqual(server.store.jobs(),[])
                self.assertEqual(call('/api/start',{'mode':'live'})[0],400)
            finally:server.shutdown();server.server_close();t.join(3)


if __name__=='__main__':unittest.main()
