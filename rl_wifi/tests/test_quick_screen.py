import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock,patch

from rl_wifi import rtt,probe_transport,schedules
from rl_wifi.quick_screen import ORDER,QuickSession,QuickPreparation,targets
from rl_wifi.quick_metrics import application_audit
from rl_wifi.quick_report import decision


class QuickScreenTests(unittest.TestCase):
    def test_exact_order_fixed_groups_and_no_within_measurement_actions(self):
        self.assertEqual(ORDER,((1,'P','U'),(1,'E','U'),(1,'P','B'),(1,'E','B'),(2,'E','B'),(2,'P','B'),(2,'E','U'),(2,'P','U')))
        req={'mode':'fixed','initial_w':16,'access_mode':'basic','measure_s':30,'segment_s':30}
        with tempfile.TemporaryDirectory() as temp:
            session=QuickSession(Path(temp),Mock(),req,{'W':targets('P')})
            self.assertEqual(session.stages,{0:{'STA1':16,'STA2':64,'STA3':64,'STA4':64}})
            self.assertEqual(list(range(1,req['measure_s']//req['segment_s'])),[])

    def test_short_node_profile_and_burst_import_without_any_io(self):
        class Base:
            def __init__(self,c):pass
        spec=importlib.util.spec_from_file_location('quick_test_node',Path(__file__).resolve().parents[1]/'live_node.py')
        module=importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules',{'demo_node':types.SimpleNamespace(Worker=Base,cmd=Mock()),'rtt':rtt,'probe_transport':probe_transport,'schedules':schedules}):
            spec.loader.exec_module(module)
        w=module.LiveWorker({'warmup_s':5,'measure_s':30,'tail_s':5,'segment_s':30,'generation_mode':'burst'})
        self.assertEqual(w.total,40)
        with self.assertRaises(ValueError):module.LiveWorker({'warmup_s':5,'measure_s':30,'tail_s':5,'segment_s':30,'generation_mode':'other'})

    def test_no_radio_mutation_in_quick_preparation(self):
        hub=Mock();hub.active={'ap_cidr':'192.168.50.1/24'}
        hub._all.return_value={'AP':{'info':'addr 00:26:b6:f7:78:e7'}}
        with patch('rl_wifi.quick_screen.check_radio'):
            QuickPreparation._restore(hub)
        hub._all.assert_called_once_with('inspect');hub.backend.call.assert_not_called()
        self.assertEqual(hub.active['status'],'radio_ready')

    def rows(self,timeout=.01,background=5):
        return [{'block':b,'config':c,'state':s,'status':'completed','metrics':{'inference_usable':True,
                'performance_target_met':timeout<=.05,'background_Mbps':background,'application':{'timeout_ratio':timeout}}} for b,c,s in ORDER]

    def test_fixed_all_bad_reversal_and_dynamic_signal_outcomes(self):
        self.assertEqual(decision(self.rows())['code'],'fixed_priority_sufficient')
        self.assertEqual(decision(self.rows(.9))['code'],'capacity_gap')
        rows=self.rows()
        for row in rows:
            if row['config']=='E' and row['state']=='U':row['metrics']['background_Mbps']=6
            if row['config']=='E' and row['state']=='B':
                row['metrics']['application']['timeout_ratio']=.2;row['metrics']['performance_target_met']=False
        self.assertEqual(decision(rows)['code'],'dynamic_signal')
        rows[4]['metrics']['performance_target_met']=True;rows[4]['metrics']['application']['timeout_ratio']=.01
        self.assertEqual(decision(rows)['code'],'insufficient')

    def test_missing_or_bad_arrival_quality_never_claims_dynamic_signal(self):
        self.assertEqual(decision(self.rows()[:-1])['code'],'insufficient')
        rows=self.rows();rows[0]['metrics']['inference_usable']=False
        self.assertEqual(decision(rows)['code'],'insufficient')

    def test_zero_reply_full_message_ledger_is_valid_high_timeout(self):
        c=rtt.ProbeCollector('00000000-0000-4000-8000-000000000001',1,100_000_000)
        for seq in range(4000):
            at=schedules.BURST.offset_ns(seq);c.generate(seq,at,at);c.submitted(seq,at)
        report=c.report(0,40_000_000_000,40_100_000_000)
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);c.export_events(p/'application_probe.jsonl')
            (p/'application_final.json').write_text(json.dumps({'collection_complete':True,'report':report}))
            (p/'application_plan.jsonl').write_text('\n'.join(json.dumps({'seq':i,'T0_offset_ns':schedules.BURST.offset_ns(i),'deadline_T0_offset_ns':schedules.BURST.offset_ns(i)+100_000_000}) for i in range(4000)))
            result=application_audit(p,'burst')
            self.assertEqual(result['counts']['mature'],3000);self.assertEqual(result['timeout_ratio'],1)
            self.assertIsNone(result['rtt_mean_ms']);self.assertTrue(result['generator_quality_passed'])


if __name__=='__main__':unittest.main()
