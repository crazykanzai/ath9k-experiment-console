"""Adapter tests: no real nodes, sockets or Linux interfaces."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock,patch

from rl_wifi import rtt,probe_transport,schedules


class LiveTests(unittest.TestCase):
    def test_node_adapter_has_sealed_timing_and_reuses_base_constructor(self):
        # Loading the node code with fake Linux dependencies must do no I/O.
        class Base:
            def __init__(self,c):self.base_config=c
        file=Path(__file__).resolve().parents[1]/'live_node.py'
        spec=importlib.util.spec_from_file_location('test_rl_node',file)
        module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'demo_node':types.SimpleNamespace(Worker=Base,cmd=Mock()),
                                     'rtt':rtt,'probe_transport':probe_transport,'schedules':schedules}):
            spec.loader.exec_module(module)
        config={'measure_s':30,'segment_s':10,'warmup_s':5,'tail_s':5}
        w=module.LiveWorker(config)
        self.assertEqual(w.total,40);self.assertEqual(w.c,config)
        with self.assertRaises(ValueError):module.LiveWorker({**config,'measure_s':300})

    def test_pilot_two_groups_and_application_not_treated_as_background(self):
        from rl_wifi.live import PilotSession
        request={'initial_w':32,'mode':'fixed','access_mode':'basic'}
        with tempfile.TemporaryDirectory() as temp:
            session=PilotSession(Path(temp)/('a'*32),Mock(),request)
            self.assertEqual(session.stages[1],{'STA1':16,'STA2':64,'STA3':64,'STA4':64})
            self.assertEqual(session.stages[2],{'STA1':64,'STA2':16,'STA3':16,'STA4':16})
            self.assertEqual(session.state['formal_valid'],0)
            self.assertIn('no training',session.state['scope'])

    def test_learning_reward_uses_mature_generation_cohort_and_same_action(self):
        from rl_wifi.live_learning import LearningSession,settings
        from rl_wifi.policies import FixedPolicy
        from rl_wifi.spec import RewardConfig
        request={'initial_w':32,'mode':'fixed','access_mode':'basic','warmup_s':15,'measure_s':180,'segment_s':15}
        with tempfile.TemporaryDirectory() as temp:
            s=LearningSession(Path(temp)/('b'*32),Mock(),request,FixedPolicy(2),RewardConfig())
            self.assertEqual(s.stages[0],settings(2))
            for second in range(19,26):
                s.state['points'].append({'second':second,'per_STA_Mbps':{'STA1':1.,'STA2':2.,'STA3':2.,'STA4':2.},'per_STA_W':settings(2)})
                s.state['application_samples'].append({'second':second,'report':{'complete_cohort':True,
                    'counts':{'mature':100,'on_time':90},'rtt_reply_samples':90,'rtt_mean_ms':10.}})
            row=s.score(0)
            self.assertAlmostEqual(row['reward'],.3)
            self.assertEqual(row['mature_messages'],700)
            self.assertEqual(row['score_seconds'],list(range(19,26)))
            setting,reason=s.online_choice(1)
            self.assertEqual(setting,settings(2))

    def test_incomplete_learning_feedback_stops_instead_of_zero_reward(self):
        from rl_wifi.live_learning import LearningSession
        from rl_wifi.policies import FixedPolicy
        from rl_wifi.spec import RewardConfig
        request={'initial_w':32,'mode':'fixed','access_mode':'basic','warmup_s':15,'measure_s':180,'segment_s':15}
        with tempfile.TemporaryDirectory() as temp:
            s=LearningSession(Path(temp)/('c'*32),Mock(),request,FixedPolicy(4),RewardConfig())
            with self.assertRaisesRegex(RuntimeError,'missing'):s.online_choice(1)


if __name__=='__main__':unittest.main()
