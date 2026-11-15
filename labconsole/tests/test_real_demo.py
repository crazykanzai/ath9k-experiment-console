"""Regression cases for the hardware demo protocol, without node access."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from labconsole import cw_policy
from labconsole.cw_policy import STAS, air_mode_issue
from labconsole.real_demo import DemoHub, NS, Session, interval_metrics, validate_request


class RealDemoTests(unittest.TestCase):
    def worker_module(self):
        worker=Path(__file__).resolve().parents[1]/'demo_node.py'
        spec=importlib.util.spec_from_file_location('demo_node_sample_test',worker)
        module=importlib.util.module_from_spec(spec)
        fake={'fcntl':types.SimpleNamespace(),'reg_access':types.SimpleNamespace(read_registers=None),
              'cohort_node_capture':types.SimpleNamespace(Capture=None),
              'readonly_probe':types.SimpleNamespace(parse_hardware_cw=None,dcf=None),
              'air_timing':types.SimpleNamespace(decode=None),'cw_policy':cw_policy}
        with patch.dict(sys.modules,fake):spec.loader.exec_module(module)
        return module

    def test_inactive_windows_ignore_retained_dcf_but_keep_radio_and_queue_checks(self):
        module=self.worker_module();w=object.__new__(module.Worker)
        w.role='STA4';w.iface='wlp2s0';w.c={'ap_mac':'00:11:22:33:44:55'}
        w.current_w=32;w.window=None;w.plan=None;w.latch_signature=lambda:True;w.state={};w.save=lambda:None
        w.be=lambda:{'hw_aifs':'3','hw_cwmin':'31','hw_cwmax':'255'}
        samples=[];w.emit=lambda event,**data:samples.append(data)
        with tempfile.TemporaryDirectory() as folder:
            w.base=Path(folder);(w.base/'queues').write_text('(BE): qdepth: 2 pending: 3\n')
            with patch.object(module,'cmd',return_value='Connected to '+w.c['ap_mac']), \
                 patch.object(module,'dcf',side_effect=ValueError('retained old signature mismatch')) as reader:
                w.sample(1);reader.assert_not_called()
                self.assertIsNone(samples[-1]['data']['dcf'])
                self.assertEqual(samples[-1]['data']['qdepth'],2)
                w.window={'index':0}
                with self.assertRaisesRegex(ValueError,'signature mismatch'):w.sample(1)
                reader.side_effect=ValueError('DCF configuration signature changed; counters cannot represent current CW')
                w.sample(1)
                self.assertFalse(w.window['metrics_valid'])
                self.assertIsNone(samples[-1]['data']['dcf'])
                w.window={'index':1};w.latch_signature=lambda:False
                reader.reset_mock();w.sample(1);reader.assert_not_called()
                self.assertEqual(samples[-1]['data']['dcf_scope'],'signature_pending_NA')
                w.window=None;w.current_w=64
                with self.assertRaisesRegex(RuntimeError,'CW changed'):w.sample(1)

    def test_signature_grace_is_bounded_and_keeps_wrong_cw_rejected(self):
        module=self.worker_module();w=object.__new__(module.Worker)
        w.access_mode='basic'
        w.check=lambda:None;w.save=lambda:None;w.state={};receipts=[]
        w.emit=lambda event,**data:receipts.append((event,data))
        be={'hw_cwmin':'31','hw_cwmax':'255','hw_aifs':'3'}
        head='version=2 running=1 hol_abi=1 basic_retry_abi=2\n'
        with tempfile.TemporaryDirectory() as folder:
            w.base=w.out=Path(folder)
            w.window={'index':0,'start_mono_ns':0,'expected_BE':be,
                      'signature_deadline_mono_ns':module.SIGNATURE_GRACE_NS}
            p=w.base/'dcf_stats';p.write_text(head+'AC=BE ready=0\n')
            with patch.object(module.time,'monotonic_ns',return_value=500_000_000):
                self.assertFalse(w.latch_signature())  # Old 80ms limit would have aborted.
            p.write_text(head+'AC=BE ready=1 mode=basic cwmin=31 cwmax=255 aifsn=3\n')
            with patch.object(module.time,'monotonic_ns',return_value=1_000_000_000):
                self.assertTrue(w.latch_signature())
            self.assertEqual(receipts[-1][1]['latch_observed_ms'],1000)
            w.window.pop('signature')
            p.write_text(head+'AC=BE ready=1 mode=basic cwmin=63 cwmax=511 aifsn=3\n')
            self.assertFalse(w.latch_signature());self.assertFalse(w.window['metrics_valid'])
            self.assertIn('signature gate',w.window['metrics_reason'])
            w.window.pop('metrics_valid');w.window.pop('metrics_reason')
            p.write_text(head+'AC=BE ready=0\n')
            with patch.object(module.time,'monotonic_ns',return_value=2_100_000_000):
                self.assertFalse(w.latch_signature());self.assertIn('2000ms',w.window['metrics_reason'])

    def test_request_explicit_and_bounded(self):
        base = {'preparation_id':'a'*32,'request_key':'manual-test-key','mode':'manual'}
        with self.assertRaises(ValueError):
            validate_request(base)
        valid = validate_request({**base,'authorize':True})
        self.assertEqual([valid[k] for k in ('warmup_s','measure_s','tail_s')],[5,30,5])
        self.assertEqual(valid['access_mode'],'basic')
        self.assertEqual(validate_request({**base,'authorize':True,'access_mode':'rts'})['access_mode'],'rts')
        for field in ({'measure_s':180},{'initial_w':True},{'mode':'auto','initial_w':8},{'mode':'pilot'},{'access_mode':'auto'}):
            with self.assertRaises(ValueError):
                validate_request({**base,'authorize':True,**field})

    def test_ready_preparation_can_start_any_mode_without_a_prior_run(self):
        identity='a'*32
        preparation=Mock()
        preparation.listing.return_value=[{'id':identity,'status':'ready','preparation_ready':True}]
        for mode in ('manual','auto','fixed','continuous','unified'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                hub=DemoHub(Path(folder),preparation)
                with patch('labconsole.real_demo.shutil.disk_usage',return_value=types.SimpleNamespace(free=40*1024**3)), \
                     patch('labconsole.real_demo.threading.Thread') as thread:
                    started=hub.start({'preparation_id':identity,'request_key':'direct-start-'+mode,
                                       'mode':mode,'authorize':True})
                self.assertEqual(started['request']['mode'],mode)
                thread.return_value.start.assert_called_once()

    def test_unified_manual_algorithm_and_takeback_share_one_schedule(self):
        req = validate_request({'preparation_id':'a'*32,'request_key':'unified-test',
                                'mode':'unified','authorize':True})
        with tempfile.TemporaryDirectory() as folder:
            s = Session(Path(folder),None,req)
            t0 = 100*NS
            s.state.update(status='running',T0_ns=t0)
            s.continuous_score = {0:8,1:11,2:12}.get
            s.state['points'] = [{'second':10,'W':128}]
            with patch('labconsole.real_demo.time.time_ns',return_value=t0+10*NS):
                s.request_control('manual',64)
                self.assertFalse(s.control_availability(t0+10*NS)['adapt'])
            self.assertEqual(s.online_choice(1)[0],dict.fromkeys(STAS,64))
            s.sent.add(1)
            self.assertFalse(s.control_availability(t0+24*NS)['manual'])
            self.assertTrue(s.control_availability(t0+24*NS)['pending'])
            s.state['points'].append({'second':40,'W':64})
            with patch('labconsole.real_demo.time.time_ns',return_value=t0+40*NS):
                self.assertTrue(s.control_availability(t0+40*NS)['adapt'])
                s.request_control('adapt')
            self.assertEqual(s.online_choice(2)[0],dict.fromkeys(STAS,8))
            s.sent.add(2)
            s.state['points'].append({'second':55,'W':8})
            with patch('labconsole.real_demo.time.time_ns',return_value=t0+55*NS):
                with self.assertRaises(ValueError):
                    s.request_control('manual',8)
                s.request_control('takeback')
            self.assertEqual(s.online_choice(3)[0],dict.fromkeys(STAS,8))
            self.assertEqual(s.state['control']['blocks']['3']['mode'],'manual')
            self.assertEqual(s.online_choice(4)[0],dict.fromkeys(STAS,8))
            self.assertEqual(s.state['T0_ns'],t0)

    def test_unified_algorithm_keeps_running_and_skips_invalid_rewards(self):
        req = validate_request({'preparation_id':'a'*32,'request_key':'ucb-score-test',
                                'mode':'unified','authorize':True})
        with tempfile.TemporaryDirectory() as folder:
            s = Session(Path(folder),None,req)
            s.state['control']['pending'] = {'segment':1,'action':'adapt'}
            s.continuous_score = {0:8,1:10,3:9}.get
            self.assertEqual(s.online_choice(1)[0],dict.fromkeys(STAS,8))
            self.assertEqual(s.online_choice(2)[0],dict.fromkeys(STAS,16))
            planned,reason = s.online_choice(3)
            self.assertEqual(planned,dict.fromkeys(STAS,16))
            self.assertIn('评分不可用',reason)
            self.assertEqual(s.online_choice(4)[0],dict.fromkeys(STAS,8))
            self.assertEqual(s.state['control']['blocks']['4']['mode'],'auto')

    def test_sliding_window_ucb_prefers_unmeasured_then_balances_reward_and_bonus(self):
        choose = cw_policy.sliding_window_ucb
        self.assertEqual(choose(128,'basic',[(128,6)])[0],32)
        self.assertEqual(choose(128,'basic',[(128,6),(32,10),(16,None)])[0],16)
        w,_,estimates = choose(128,'basic',[(128,5),(32,11),(32,12),(16,7)])
        self.assertEqual(w,32)
        self.assertEqual(estimates[1]['count'],2)
        self.assertEqual(estimates[1]['mean_Mbps'],11.5)
        self.assertGreater(estimates[2]['bonus_Mbps'],estimates[1]['bonus_Mbps'])

    def test_rts_config_readback_and_basic_restore(self):
        module = self.worker_module()
        worker = object.__new__(module.Worker)
        worker.phy, worker.iface, worker.access_mode = 'phy0', 'wlan0', 'rts'
        with tempfile.TemporaryDirectory() as folder:
            worker.base = Path(folder)
            override = worker.base/'basic_access_override'
            override.write_text('1\n')
            with patch.object(module,'cmd',side_effect=['RTS thr:off','','RTS thr=500 B','RTS thr:off','','RTS thr:off']) as command:
                original = worker.access_settings()
                worker.set_access(*cw_policy.ACCESS_SETTINGS['rts'])
                self.assertEqual(worker.verify_access(),{'rts':'500','override':'0'})
                with self.assertRaisesRegex(RuntimeError,'access mode configuration mismatch'):
                    worker.verify_access()
                worker.set_access(*original)
                self.assertEqual(worker.access_settings(),('off','1'))
                command.assert_any_call(['iw','phy','phy0','set','rts','500'])
                command.assert_any_call(['iw','phy','phy0','set','rts','off'])

    def test_air_audit_rejects_missing_cts_in_rts_and_control_frames_in_basic(self):
        basic = {'data':20,'rates':{'24.0':20},'rts':0,'cts':0}
        rts = {**basic,'rts':25,'cts':20}
        self.assertIsNone(air_mode_issue(basic,'basic'))
        self.assertIsNone(air_mode_issue(rts,'rts'))
        self.assertIsNotNone(air_mode_issue(rts,'basic'))
        self.assertIsNotNone(air_mode_issue({**rts,'cts':0},'rts'))
        self.assertIsNotNone(air_mode_issue({**rts,'rates':{'24.0':20,'6.0':1}},'rts'))
        self.assertEqual(cw_policy.continuous_candidates(128,'rts')['A'],dict.fromkeys(STAS,8))

    def test_continuous_waits_for_baseline_then_keeps_each_improvement_without_switchback(self):
        req = validate_request({'preparation_id':'a'*32,'request_key':'continuous-test-key',
                                'mode':'continuous','authorize':True})
        self.assertEqual((req['initial_w'],req['measure_s'],req['segment_s']), (128,600,20))
        with tempfile.TemporaryDirectory() as folder:
            s = Session(Path(folder),None,req)
            baseline = dict.fromkeys(STAS,128)
            a, b = dict.fromkeys(STAS,32), dict.fromkeys(STAS,16)
            t0 = 100*NS
            s.state.update(status='running',T0_ns=t0)
            s.air_ok = True
            for index, rate in enumerate((8,8,10,10.01)):
                start = 5+20*index
                s.state['points'].extend({'second':i,'total_Mbps':rate if i<=start+15 else 100,
                                          'queue':dict.fromkeys(STAS,(2,3))}
                                         for i in range(start+1,start+21))
            self.assertEqual(s.online_choice(1)[0],baseline)
            self.assertIsNone(s.adaptation_gate(t0+34*NS)[0])
            s.sent.add(1)
            self.assertIsNone(s.adaptation_gate(t0+484*NS)[0])
            with patch('labconsole.real_demo.time.time_ns',return_value=t0+35*NS):
                s.enable_adaptation()
                s.enable_adaptation()
            self.assertEqual(s.state['adaptation']['activation']['first_block'],2)
            self.assertEqual(s.state['adaptation']['activation']['baseline_Mbps'],8)
            self.assertEqual(len(s.state['events']),1)
            self.assertEqual(s.online_choice(2)[0],a)
            self.assertEqual(s.online_choice(3)[0],b)
            self.assertEqual(s.online_choice(4)[0],b)
            self.assertEqual(s.online_choice(5)[0],b)
            decisions=s.state['adaptation']['decisions']
            self.assertEqual([(d['previous_candidate'],d['accepted']) for d in decisions],
                             [('baseline',True),('A',True)])
            self.assertEqual(s.state['adaptation']['selection']['hold_block'],4)

    def test_continuous_restores_accepted_candidate_on_a_tie_or_worse(self):
        req = validate_request({'preparation_id':'a'*32,'request_key':'fallback-test',
                                'mode':'continuous','authorize':True})
        for second_score in (10,9,None):
            with self.subTest(second_score=second_score), tempfile.TemporaryDirectory() as folder:
                s = Session(Path(folder),None,req)
                adaptation = s.state['adaptation']
                adaptation['activation'] = {'first_block':2,'baseline_blocks':[0],'baseline_Mbps':7}
                s.continuous_score = {1:8,2:10,3:second_score}.get
                self.assertEqual(s.online_choice(2)[0],dict.fromkeys(STAS,32))
                self.assertEqual(s.online_choice(3)[0],dict.fromkeys(STAS,16))
                self.assertEqual(s.online_choice(4)[0],dict.fromkeys(STAS,32))
                self.assertEqual(s.online_choice(5)[0],dict.fromkeys(STAS,32))
                self.assertFalse(adaptation['decisions'][-1]['accepted'])

    def test_continuous_restores_baseline_after_first_loss_and_uses_fresh_score(self):
        req = validate_request({'preparation_id':'a'*32,'request_key':'refresh-score-test',
                                'mode':'continuous','authorize':True})
        with tempfile.TemporaryDirectory() as folder:
            s = Session(Path(folder),None,req)
            adaptation = s.state['adaptation']
            adaptation['activation'] = {'first_block':2,'baseline_blocks':[0],'baseline_Mbps':8}
            s.continuous_score = {2:7,3:9,4:8.5}.get
            self.assertEqual(s.online_choice(2)[0],dict.fromkeys(STAS,32))
            self.assertEqual(s.online_choice(3)[0],dict.fromkeys(STAS,128))
            self.assertEqual(adaptation['blocks']['3']['phase'],'fallback')
            self.assertEqual(s.online_choice(4)[0],dict.fromkeys(STAS,16))
            self.assertEqual(s.online_choice(5)[0],dict.fromkeys(STAS,128))
            self.assertEqual(adaptation['decisions'][-1]['previous_Mbps'],9)

    def test_per_node_stage_dispatch_and_matching_readbacks(self):
        req = validate_request({'preparation_id':'a'*32,'request_key':'readback-test-key',
                                'mode':'continuous','authorize':True})
        with tempfile.TemporaryDirectory() as folder:
            s = Session(Path(folder),None,req)
            settings = {**dict.fromkeys(STAS,32),'STA1':16}
            s.stages[1] = settings
            s.workers = {n:Mock() for n in STAS}
            s.send_stage(1,settings,135*NS)
            for n in STAS:
                s.workers[n].send.assert_called_once_with({'op':'stage','index':1,'W':settings[n],'apply_ns':135*NS})
            s.samples[25] = {'AP':{'data':{'udp_buckets':{'24':{'5201':1200}}}},
                **{n:{'data':{'BE':{'hw_cwmin':w-1},'dcf':None,'qdepth':2,'pending':3}} for n,w in settings.items()}}
            point = s.sample_point(25)
            self.assertIsNone(point['W'])
            self.assertEqual(point['per_STA_W'],settings)
            self.assertFalse(point['stable'])
            s.samples[25]['STA1']['data']['BE']['hw_cwmin'] = 31
            with self.assertRaisesRegex(RuntimeError,'读回'):
                s.sample_point(25)

    def test_continuous_unchanged_cw_still_verifies_hardware_without_writing(self):
        module = self.worker_module()
        worker = object.__new__(module.Worker)
        worker.c, worker.current_w = {'mode':'continuous'}, 32
        worker.shared, worker.emit = Mock(), Mock()
        worker.be = Mock(return_value={'hw_aifs':'3','hw_cwmin':'31','hw_cwmax':'255'})
        with patch.object(module.time,'time_ns',return_value=10*NS):
            worker.set_cw(32,index=1,planned_ns=10*NS)
            worker.shared.assert_not_called()
            self.assertIsNone(worker.emit.call_args.kwargs['write_call_ns'])
            worker.be.return_value['hw_cwmax'] = '511'
            with self.assertRaisesRegex(RuntimeError,'hardware mismatch'):
                worker.set_cw(32,index=2,planned_ns=10*NS)

    def test_warning_retracts_only_affected_window_and_preserves_throughput(self):
        request=validate_request({'preparation_id':'a'*32,'request_key':'warning-test-key','mode':'manual','authorize':True})
        with tempfile.TemporaryDirectory() as folder:
            s=Session(Path(folder),None,request)
            s.state['points']=[dict(second=k,total_Mbps=8,HoL_ms=4,p_ACK=.8) for k in (10,20,30)]
            s.messages.put(('STA2',{'event':'statistics_warning','index':2,'message':'signature mismatch'}))
            s.receive()
            self.assertEqual(s.state['points'][1]['HoL_ms'],4)
            self.assertIsNone(s.state['points'][2]['HoL_ms'])
            self.assertEqual(s.state['points'][2]['total_Mbps'],8)
            self.assertIn('2',s.state['statistics_unavailable_segments'])

    def test_invalid_stats_are_archived_without_claiming_hardware_restore_failed(self):
        module=self.worker_module();w=object.__new__(module.Worker)
        w.window={'index':0};w.state={'statistics_windows':[]};w.save=lambda:None
        w.emit=lambda *a,**k:dict(wall_ns=200,monotonic_ns=100)
        class Stats:
            def write_text(self,value):pass
            def read_text(self):return 'version=2 running=0 config_mismatches=1\nAC=BE config_mismatches=1\n'
        class Base:
            def __truediv__(self,name):return Stats()
        w.base=Base()
        with tempfile.TemporaryDirectory() as folder:
            w.out=Path(folder);w.finish_stats()
            self.assertIsNone(w.window)
            self.assertFalse(w.state['statistics_windows'][0]['metrics_valid'])
            self.assertIn('config_mismatches=1',(w.out/'dcf_segment_00.txt').read_text())

    def test_monitor_waits_for_nm_readiness_without_restarting_services(self):
        module=self.worker_module()
        with patch.object(module,'cmd',side_effect=['20 (unavailable)','30 (disconnected)']) as call,patch.object(module.time,'sleep'):
            module.wait_managed_ready('wlp1s0')
        self.assertEqual(call.call_count,2)
        self.assertTrue(all(c.args[0][:3]==['nmcli','-g','GENERAL.STATE'] for c in call.call_args_list))

    def test_no_cross_generation_or_failed_denominator(self):
        a = {'generation':1,'running':True,'mode':'basic','basic_retry_abi':2,
             'hol_samples':10,'hol_sum_us':3000,'attempts':12,'successes':10,'failures':0}
        self.assertIsNone(interval_metrics(a,{**a,'generation':2}))
        b = {**a,'hol_samples':11,'hol_sum_us':3400,'attempts':17,'successes':11,'failures':1}
        self.assertEqual(interval_metrics(a,b)['failures'],1)

    def test_auto_uses_only_earlier_complete_stable_segments(self):
        request = validate_request({'preparation_id':'a'*32,'request_key':'auto-test-key','mode':'auto','authorize':True})
        with tempfile.TemporaryDirectory() as folder:
            s = Session(Path(folder),None,request)
            s.air_ok = True
            s.stages.update({1:32,2:64})
            for index,goodput in enumerate((8,11,40)):
                first = 5+index*10
                for second in range(first+4,first+10):
                    s.state['points'].append({'second':second,'stable':True,'total_Mbps':goodput,
                                              'queue':{n:[2,3] for n in ('STA1','STA2','STA3','STA4')}})
            self.assertEqual(s.online_choice(1)[0],32)
            self.assertEqual(s.online_choice(2)[0],64)
            self.assertEqual(s.online_choice(3)[0],32)
            self.assertEqual(s.evaluated,{0,1})
            self.assertIsNone(s.policy.estimates[64]['mean'])

    def test_signature_initialization_keeps_original_validation(self):
        module = self.worker_module()
        be = {'hw_cwmin':'31','hw_cwmax':'255','hw_aifs':'3'}
        head = 'version=2 running=1 hol_abi=1 basic_retry_abi=2\n'
        self.assertIsNone(module.signature(head+'AC=BE ready=0 packets=0\n',be))
        valid = 'AC=BE ready=1 mode=basic cwmin=31 cwmax=255 aifsn=3\n'
        self.assertEqual(module.signature(head+valid,be)['mode'],'basic')
        protected = valid.replace('mode=basic','mode=rts-cts')
        self.assertEqual(module.signature(head+protected,be,'rts')['mode'],'rts-cts')
        with self.assertRaisesRegex(RuntimeError,'signature gate'):
            module.signature(head+valid,be,'rts')
        with self.assertRaisesRegex(RuntimeError,'signature gate'):
            module.signature(head+valid.replace('cwmin=31','cwmin=63'),be)


if __name__=='__main__':
    unittest.main()
