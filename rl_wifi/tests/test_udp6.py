"""Offline UDP6 contracts, active/idle timing, payload and decision accounting."""
import importlib.util
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from rl_wifi.udp6.metrics import ACTIVE,ORDER,NS,aggregate,saturation,judge

class UDP6Tests(unittest.TestCase):
    def test_exact_six_paired_order_no_TCP_carry(self):
        self.assertEqual(ORDER,(('L',16),('L',8),('H',8),('H',16),('M',16),('M',8)))
        self.assertEqual(set(ORDER),{(s,w) for s in ACTIVE for w in (8,16)})

    def test_udp_common_window_packet_size_clock_and_zero_result(self):
        packets={5201:[(7*NS-1,1200),(7*NS,1200),(37*NS-1,1200),(37*NS,1200),(8*NS,1199)]}
        row=aggregate(packets,0,2*NS,1,['STA1'],{'STA1':5201})
        self.assertEqual(row['total_Mbps'],2400*8/30e6);self.assertEqual(row['Jain'],1)
        zero=aggregate({5201:[]},0,0,0,['STA1'],{'STA1':5201})
        self.assertEqual(zero['total_Mbps'],0);self.assertFalse(zero['fairness_target_met'])

    def test_saturation_needs_actual_queue_and_complete_samples(self):
        samples=[{'qdepth':2,'pending':3} for _ in range(30)]
        self.assertTrue(saturation(samples)['passed'])
        self.assertFalse(saturation(samples[:29])['passed'])
        samples[:2]=[{'qdepth':0,'pending':0}]*2
        self.assertFalse(saturation(samples)['passed'])

    def rows(self):
        return [{'state':s,'W':w,'accepted':True,'metrics':{'total_Mbps':10.,'fairness_target_met':True}} for s,w in ORDER]

    def test_same_best_and_small_crossover_stop(self):
        rows=self.rows()
        for r in rows:r['metrics']['total_Mbps']+=.1 if (r['state']=='L')==(r['W']==8) else 0
        self.assertEqual(judge(rows)['code'],'stop_no_practical_dynamic_signal')
        rows[0]['accepted']=False;self.assertEqual(judge(rows)['code'],'incomplete_no_policy_conclusion')

    def test_practical_crossover_is_only_signal(self):
        rows=self.rows()
        for r in rows:r['metrics']['total_Mbps']=15. if (r['state']=='L')==(r['W']==8) else 10.
        result=judge(rows);self.assertEqual(result['code'],'candidate_signal_needs_replication')
        self.assertFalse(result['RL_advantage_proven']);self.assertEqual(result['repeats_per_condition'],1)

    def test_fairness_failure_not_collection_failure_and_no_fixed_case(self):
        rows=self.rows()
        for r in rows:
            if r['state']=='L':r['metrics']['fairness_target_met']=r['W']==8
            if r['state']=='H':r['metrics']['fairness_target_met']=r['W']==16
        result=judge(rows);self.assertEqual(result['code'],'fairness_feasibility_signal');self.assertIsNone(result['ideal_gain_fraction'])
        for r in rows:
            if r['state']=='M':r['metrics']['fairness_target_met']=False
        self.assertEqual(judge(rows)['code'],'fairness_capacity_gap')

    def test_host_authorization_window_and_explicit_payload(self):
        from rl_wifi.udp6.session import UDPSession
        with tempfile.TemporaryDirectory() as tmp:
            req={'mode':'fixed','initial_w':8,'access_mode':'basic','warmup_s':5,'measure_s':30,'tail_s':5,'segment_s':15,'guard_s':3,
                 'field_stop_monotonic':time.monotonic()+1200}
            with self.assertRaises(ValueError):UDPSession(Path(tmp),None,req,{'state':'M','W':8})
            req['authorization']='udp6-authorized';session=UDPSession(Path(tmp),None,req,{'state':'M','W':8})
            self.assertTrue(all(p.is_file() for p in session.payload()))
            self.assertEqual(session.active,('STA1','STA2'));self.assertEqual(session.stages[1]['STA4'],8)
            self.assertFalse(any('library9' in str(p) or p.name=='event_journal.py' for p in session.payload()))

    def test_portable_active_and_idle_schedules(self):
        path=Path(__file__).parents[1]/'udp6/node.py'
        class Parent:
            def __init__(self,c):
                self.c=c;self.role=c['role'];self.out=Path('.');self.total=40
                self.plan={'T0_ns':100*NS};self.plan_ready=types.SimpleNamespace(wait=lambda _:True)
                self.state={};self.events=[];self.processes={}
            def control_reader(self):pass
            def setup(self):pass
            def check(self):pass
            def wait(self,t):self.now=t
            def emit(self,event,**data):
                row={'event':event,'wall_ns':self.now,**data};self.events.append(row);return row
            def launch(self,key,args):
                self.command=args;self.processes[key]=(types.SimpleNamespace(send_signal=lambda _:None),None)
            def boundary(self,k,t):self.events.append({'event':'boundary','at':t})
            def stats_start(self,*a):pass
            def latch_signature(self):pass
            def sample(self,*a):pass
            def save(self):pass
        class Thread:
            def __init__(self,target,**kw):self.target=target
            def start(self):self.target()
        with patch.dict('sys.modules',{'demo_node':types.SimpleNamespace(Worker=Parent)}):
            spec=importlib.util.spec_from_file_location('fake_udp6_node',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            module.threading=types.SimpleNamespace(Thread=Thread)
            for role in ('STA1','STA2'):
                config={'udp6_schema':'udp6-w8w16-v1','initial_w':8,'warmup_s':5,'measure_s':30,'tail_s':5,'segment_s':15,'guard_s':3,
                        'active_senders':['STA1'],'role':role,'ap_ip':'fixture','own_ip':'fixture','port':5201}
                worker=module.UDPWorker(config);worker.run()
                self.assertEqual([r['at'] for r in worker.events if r['event']=='boundary'],[105*NS,120*NS,135*NS])
                self.assertTrue(worker.state['completed_schedule'])
                if role=='STA1':
                    self.assertIn('-u',worker.command);self.assertEqual(worker.command[worker.command.index('-b')+1],'30M')
                    self.assertEqual(worker.command[worker.command.index('-l')+1],'1200')
                    self.assertEqual(next(e['wall_ns'] for e in worker.events if e['event']=='iperf_stop_call'),140*NS)
                else:self.assertEqual(worker.processes,{})

if __name__=='__main__':unittest.main()
