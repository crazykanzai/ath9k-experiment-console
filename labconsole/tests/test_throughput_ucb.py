"""Offline protocol, policy and ownership regressions; never contacts a node."""
import math
import json
import sys
import types
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from labconsole.cw_policy import STAS, WINDOWS, sta_windows, throughput_ucb
from labconsole.real_demo import DemoHub, NS, Session, validate_request


def request(**kw):
    return validate_request(dict(preparation_id='a'*32, request_key='ucb-v2-offline',
                                 mode='unified', authorize=True, **kw))


class ThroughputUCBTests(unittest.TestCase):
    def test_exact_manual_maps_and_all_five_initial_values(self):
        for w in WINDOWS:
            self.assertEqual(request(initial_w=w)['initial_per_STA_W'], dict.fromkeys(STAS,w))
        mixed = dict(zip(STAS,(8,16,64,128)))
        self.assertEqual(request(initial_per_STA_W=mixed)['initial_per_STA_W'],mixed)
        for bad in (True, 7, {'STA1':8}, {**mixed,'STA4':False}, {**mixed,'STA5':8}, [8]*4):
            with self.subTest(bad=bad), self.assertRaises(ValueError):sta_windows(bad)
        for senders in ([],['STA1','STA1'],['STA5'],True):
            with self.assertRaises(ValueError):request(active_senders=senders)
        self.assertEqual(request(active_senders=['STA3','STA1','STA2'])['active_senders'],list(STAS[:3]))

    def test_policy_only_two_arms_and_zero_is_not_missing(self):
        self.assertEqual(throughput_ucb([])[0],8)
        self.assertEqual(throughput_ucb([(8,0)])[0],16)
        _,_,rows = throughput_ucb([(8,0),(16,None),(16,math.nan),(16,-1),(16,True),(128,99)])
        self.assertEqual([r['W'] for r in rows],[8,16])
        self.assertEqual(rows[0]['count'],1)
        self.assertEqual(rows[0]['mean_Mbps'],0)
        self.assertEqual(rows[1]['count'],0)
        self.assertEqual(throughput_ucb([(8,12),(16,9)])[0],8)

    def test_forgetting_tracks_changed_rewards_and_missing_segments_age(self):
        old = [(8,15),(16,2)]*3
        changed = [(8,3),(16,12)]*3
        self.assertEqual(throughput_ucb(old)[0],8)
        self.assertEqual(throughput_ucb(old+changed)[0],16)
        _,_,rows = throughput_ucb(old+[(8,None)]*6)
        self.assertEqual([r['count'] for r in rows],[0,0])

    def test_manual_mapping_dispatch_takeover_and_epoch_reset(self):
        mixed=dict(zip(STAS,(8,32,64,128)))
        with tempfile.TemporaryDirectory() as folder:
            s=Session(Path(folder),None,request(initial_per_STA_W=mixed,active_senders=list(STAS[:3])))
            s.continuous_score=lambda index:12.0
            s.state['control']['pending']={'action':'manual','segment':1,'W':mixed}
            settings,_=s.online_choice(1)
            self.assertEqual(settings,mixed)
            self.assertIsNone(s.state['control']['blocks']['1']['W'])
            s.workers={n:Mock() for n in STAS}
            s.send_stage(1,settings,125*NS)
            for n in STAS:s.workers[n].send.assert_called_once_with(dict(op='stage',index=1,W=mixed[n],apply_ns=125*NS))
            s.state['control']['pending']={'action':'adapt','segment':2}
            with patch('labconsole.real_demo.throughput_ucb',wraps=throughput_ucb) as policy:
                self.assertEqual(s.online_choice(2)[0],dict.fromkeys(STAS,8))
                policy.assert_called_once_with([])  # No manual rewards, count or sender set.
            self.assertEqual(s.online_choice(3)[0],dict.fromkeys(STAS,16))
            s.state['control']['pending']={'action':'takeback','segment':4}
            self.assertEqual(s.online_choice(4)[0],dict.fromkeys(STAS,16))
            s.state['control']['pending']={'action':'manual','segment':5,'W':mixed}
            self.assertEqual(s.online_choice(5)[0],mixed)
            s.state['control']['pending']={'action':'adapt','segment':6}
            self.assertEqual(s.online_choice(6)[0],dict.fromkeys(STAS,8))
            self.assertEqual(s.state['control']['epoch_start'],6)

    def test_three_sender_acquisition_does_not_require_idle_saturation(self):
        with tempfile.TemporaryDirectory() as folder:
            s=Session(Path(folder),None,request(active_senders=list(STAS[:3])))
            s.air_ok=True
            s.state['points']=[dict(second=k,total_Mbps=0,
                queue={n:[2,3] if n in STAS[:3] else [0,0] for n in STAS}) for k in range(9,21)]
            self.assertEqual(s.continuous_score(0),0)
            s.state['points'][0]['queue']['STA2']=[0,0]
            self.assertIsNone(s.continuous_score(0))
            s.state['points'].pop()
            self.assertIsNone(s.continuous_score(0))

    def test_readback_checks_each_sta_and_manual_map_is_not_uniform(self):
        mixed=dict(zip(STAS,(8,16,32,128)))
        with tempfile.TemporaryDirectory() as folder:
            s=Session(Path(folder),None,request(initial_per_STA_W=mixed))
            s.samples[2]={'AP':{'data':{'udp_buckets':{'1':{'5201':1200}}}},
                **{n:{'data':{'BE':{'hw_cwmin':w-1},'dcf':None,'qdepth':2,'pending':3}} for n,w in mixed.items()}}
            self.assertEqual(s.sample_point(2)['per_STA_W'],mixed)
            self.assertIsNone(s.sample_point(2)['W'])
            s.samples[2]['STA3']['data']['BE']['hw_cwmin']=7
            with self.assertRaisesRegex(RuntimeError,'读回'):s.sample_point(2)

    def test_hub_validates_maps_and_pending_commands_still_exclude_one_another(self):
        with tempfile.TemporaryDirectory() as folder:
            hub=DemoHub(Path(folder),Mock())
            s=Session(hub.root/('b'*32),None,request())
            s.folder.mkdir();s.state.update(status='running',T0_ns=100*NS)
            s.state['points']=[dict(second=10,W=128)]
            s.save();hub.active=s
            mixed=dict(zip(STAS,(8,16,32,128)))
            with patch('labconsole.real_demo.time.time_ns',return_value=110*NS):
                hub.command(dict(id=s.id,action='manual',W=mixed))
                self.assertEqual(s.state['control']['pending']['W'],mixed)
                with self.assertRaises(ValueError):hub.command(dict(id=s.id,action='manual',W=8))
                with self.assertRaises(ValueError):hub.command(dict(id=s.id,action='manual',W={**mixed,'STA4':True}))

    def test_worker_active_and_idle_use_absolute_schedule_without_ssh_trigger(self):
        from labconsole.tests.test_real_demo import RealDemoTests
        module=RealDemoTests().worker_module()
        for role,active in [('STA1',True),('STA4',False)]:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as folder:
                w=object.__new__(module.Worker);w.role=role;w.total=40;w.out=Path(folder)
                w.active_senders=['STA1','STA2','STA3'];w.iface='wlan0'
                w.c=dict(warmup_s=5,measure_s=30,tail_s=5,segment_s=10,guard_s=3,
                         ap_ip='192.0.2.1',own_ip='192.0.2.2',port=5201)
                w.plan=dict(T0_ns=100*NS);w.plan_ready=Mock();w.plan_ready.wait.return_value=True
                w.control_reader=Mock();w.setup=Mock();w.check=Mock();w.state={}
                w.boundary=Mock();w.stats_start=Mock();w.latch_signature=Mock();w.sample=Mock()
                now=[100*NS];w.wait=lambda at:now.__setitem__(0,at)
                receipts=[]
                def emit(event,**kw):receipts.append((event,kw));return dict(wall_ns=now[0])
                w.emit=emit;w.processes={'iperf_client':(Mock(),None)};w.launch=Mock()
                with patch.object(module.threading,'Thread'),patch.object(module.time,'time_ns',side_effect=lambda:now[0]):w.run()
                self.assertTrue(w.state['completed_schedule'])
                self.assertEqual(now[0],140*NS)
                if active:
                    args=w.launch.call_args.args[1]
                    self.assertIn('-u',args);self.assertIn('30M',args);self.assertIn('1200',args)
                    self.assertEqual(receipts[-1][0],'iperf_stop_call')
                else:
                    w.launch.assert_not_called()
                    self.assertEqual([r[0] for r in receipts],['idle_at_T0','absolute_end'])

    def test_offline_final_analysis_accepts_three_senders_and_checks_idle_leakage(self):
        from labconsole.demo_analysis import analyze
        active=list(STAS[:3]);t0=100*NS
        req=request(active_senders=active);req.update(measure_s=20)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=root/'raw';final={}
            for n in ('AP','MON15',*STAS):(raw/n).mkdir(parents=True)
            for n in ('AP','MON15'):
                (raw/n/'tcpdump.stderr').write_text('fixture')
                final[n]=dict(processes={'tcpdump':{'exit_code':0}})
            final['AP']['packet_socket_statistics']=[0,0]
            final['MON15']['air_audit']={n:dict(data=20 if n in active else 0,rates={'24.0':20} if n in active else {},rts=0,cts=0) for n in STAS}
            spec={n:{'proof':{'valid_bit':1}} for n in STAS}
            (root/'trace_specs.json').write_text(json.dumps(spec))
            for n in STAS:
                base=raw/n
                (base/'kprobe_profile.txt').write_text('\n'.join(k+' 10 0' for k in ('txarrival','txinput','txdone','mibupdate')))
                (base/'trace_stats.txt').write_text('overrun: 0\ncommit overrun: 0\ndropped events: 0\n')
                (base/'tx_trace.txt').write_text('offline fixture')
                names=('iperf_launch_call','iperf_stop_call') if n in active else ('idle_at_T0','absolute_end')
                events=[dict(event=names[0],wall_ns=t0),dict(event=names[1],wall_ns=130*NS),
                        dict(event='cw_verified',index=0,write_call_ns=105*NS)]
                final[n]=dict(events=events,statistics_windows=[dict(index=0,start_wall_ns=108*NS,stop_wall_ns=125*NS)],
                              processes={},xmit_before='FIFO Underrun 0',xmit_after='FIFO Underrun 0')
                if n in active:
                    final[n]['processes']['iperf_client']={'exit_code':1}
                    (base/'iperf_client.json').write_text(json.dumps(dict(error='interrupt - the client has terminated',intervals=[{}]*20)))
                    port=5201+STAS.index(n)
                    (raw/'AP'/('iperf_server_%d.json'%port)).write_text(json.dumps(dict(intervals=[{}]*20)))
            clocks={n:dict(offset_ms=0,uncertainty_ms=0) for n in ('AP','MON15',*STAS)}
            state=dict(request=req,T0_ns=t0,clock_checks=[clocks],final_nodes=final,
                       applied_W={0:dict.fromkeys(STAS,16)},control={},
                       points=[dict(second=k,queue={n:[2,3] if n in active else [0,0] for n in STAS}) for k in range(6,26)])
            packets={5201+i:[(106*NS,1200)] if n in active else [] for i,n in enumerate(STAS)}
            fake=types.SimpleNamespace(pcap_udp=lambda *a:packets,tcpdump_stats=lambda *a:dict(kernel_dropped=0,interface_drop_fraction=0))
            seg=dict(index=0,hol_samples=0,hol_sum_us=0,attempt_denominator_proxy=None,ACK_success=0,terminal_failures=0)
            with patch.dict(sys.modules,{'analyze_sync':fake}),patch('labconsole.demo_analysis.analysis_trace',return_value=([seg],{})):
                result=analyze(root,state)
                self.assertEqual(result['issues'],[])
                self.assertEqual(result['per_STA']['STA4']['goodput_Mbps'],0)
                packets[5204]=[(106*NS,1200)]
                self.assertIn('闲置STA出现本轮UDP业务',analyze(root,state)['issues'])
                packets[5204]=[]
                state['roles']=['AP',*STAS];final.pop('MON15')
                no_mon=analyze(root,state)
                self.assertEqual(no_mon['issues'],[])
                self.assertEqual(no_mon['air_evidence'],'not_collected')
                self.assertTrue(any('MON15' in w for w in no_mon['warnings']))


if __name__=='__main__':unittest.main()
