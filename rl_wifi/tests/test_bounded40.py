"""No network: bounded learning protocol and failure rollback contracts."""
import copy
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from rl_wifi.bounded40.contract import ACTIONS, FEATURES, reward, targets, validate_feedback, vector
from rl_wifi.bounded40.controller import Controller, PerformanceStop, make_state
from rl_wifi.bounded40.offline import exercise, fixture_feedback
from rl_wifi.bounded40.plan import NS, Schedule, TRAIN_STATES, make_plan, seal
from rl_wifi.bounded40.report import judge
from rl_wifi.bounded40.bridge import Bridge, may_start_run


class Bounded40ProtocolTests(unittest.TestCase):
    def test_all_planned_messages_and_burst_boundaries(self):
        for states in (TRAIN_STATES, ('burst', 'uniform', 'uniform', 'burst')):
            schedule = Schedule(states)
            offsets = [schedule.offset_ns(i) for i in range(schedule.count(schedule.total_s*NS))]
            self.assertEqual(offsets, sorted(offsets))
            self.assertEqual(len(offsets), schedule.total_s*100)
            for i, state in enumerate(states):
                first = 500+i*1500
                self.assertEqual(offsets[first], (5+i*15)*NS)
                self.assertEqual(offsets[first+1]-offsets[first], 0 if state == 'burst' else 10_000_000)
                self.assertEqual(sum((5+i*15)*NS <= t < (20+i*15)*NS for t in offsets), 1500)
            self.assertEqual(schedule.offset_ns(len(offsets)), schedule.total_s*NS)

    def test_budgets_and_plan_cannot_overwrite(self):
        plan = make_plan()
        self.assertEqual(len(plan['runs']), 13)
        self.assertEqual(sum(sum(r['timing_s']) for r in plan['runs']), 1120)
        self.assertEqual(sum(r['timing_s'][1]//5 for r in plan['runs'] if r['phase']=='train'), 72)
        self.assertFalse(plan['live_entrypoint_enabled'])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'sealed'
            seal(path)
            self.assertEqual(len((path/'train_message_plan.jsonl').read_text().splitlines()), 19000)
            with self.assertRaises(FileExistsError):
                seal(path)

    def test_full_denominator_and_late_local_failure_not_dropped(self):
        row = fixture_feedback(0, 0, on_time=470)
        row['counts'].update(late=10, no_reply=10, local_failure=10)
        row['rtt_reply_samples'] = 480
        validate_feedback(row)
        self.assertLess(reward(row, 0), 0)
        feasible = fixture_feedback(0, 0, on_time=475)
        self.assertGreater(reward(feasible, 2), 0)
        row['counts']['generated'] = 490
        with self.assertRaises(ValueError):
            validate_feedback(row)

    def test_conditional_rtt_and_immature_feedback_rejected(self):
        row = fixture_feedback(0, 0)
        row['as_of_ns'] = row['end_ns']
        with self.assertRaises(ValueError):
            validate_feedback(row)
        row = fixture_feedback(0, 0)
        row['rtt_reply_samples'] = 0
        with self.assertRaises(ValueError):
            validate_feedback(row)

    def test_future_feedback_not_an_input_and_missing_explicit(self):
        context = {'state': 'uniform', 'announced': None}
        state = make_state(context, 2, None, NS)
        self.assertEqual(set(state), set(FEATURES))
        self.assertEqual(state['feedback_available'], 0)
        self.assertEqual(state['rtt_observed'], 0)
        self.assertEqual(len(vector(state)), len(FEATURES))
        with self.assertRaises(ValueError):
            make_state(context, 2, fixture_feedback(0, 0), NS)

    def test_all_node_targets_and_no_old_nine_action_index(self):
        for i, w in enumerate(ACTIONS):
            t = targets(i)
            self.assertEqual(t['STA1']['W'], 16)
            self.assertEqual(t['STA4']['CWmax'], 8*w-1)
        with self.assertRaises(ValueError):
            targets(4)

    def test_delayed_reward_join_and_action_mismatch(self):
        c = Controller('P', 'test', 0, 6, 'offline_fixture')
        context = {'state': 'uniform', 'announced': None}
        c.decide(0, NS, context, 2)
        c.decide(1, 6*NS, context, 2)
        self.assertEqual(c.transitions, [])
        wrong = fixture_feedback(0, 0)
        with self.assertRaises(ValueError):
            c.receive(wrong)
        correct = fixture_feedback(0, 2)
        c.receive(correct)
        self.assertEqual(c.transitions[0]['action'], 2)
        self.assertEqual(c.transitions[0]['next_state'], c.decisions[1]['state'])
        with self.assertRaises(ValueError):
            c.receive(correct)

    def test_command_lead_and_incomplete_run_not_committed(self):
        c = Controller('P', 'test', 0, 6, 'offline_fixture')
        ctx = {'state': 'uniform', 'announced': None}
        with self.assertRaises(ValueError):
            c.decide(0, 4*NS, ctx, 2)
        c.decide(0, NS, ctx, 2)
        result = c.close(True, True, 40*NS, ctx)
        self.assertFalse(result['committed'])

    def test_report_best_feasible_fixed_per_block_and_both_repeats(self):
        rows = []
        for b in (1, 2):
            for p, bg, good in [('P', 9, True), ('E', 12, False), ('middle', 10, True),
                                ('rule', 10.3, True), ('DQN', 10.5, True)]:
                rows.append({'block': b, 'policy': p, 'collection_valid': True, 'background_Mbps': bg,
                             'states': {'uniform': {'mature': 3000, 'on_time': 2990},
                                        'burst': {'mature': 3000, 'on_time': 2900 if good else 2500}}})
        verdict = judge(rows)
        self.assertEqual(verdict['code'], 'gain_signal_needs_independent_replication')
        self.assertTrue(verdict['dqn_necessity_unproven'])
        self.assertEqual(verdict['blocks'][0]['best_fixed'], 'middle')
        rows[-1]['background_Mbps'] = 10.4
        self.assertEqual(judge(rows)['code'], 'stop_no_repeated_practical_gain')

    def test_bridge_requires_all_readbacks_and_maps_sender_clock(self):
        c = Controller('P', 'test', 0, 6, 'offline_fixture')
        sends = []
        bridge = Bridge(c, lambda *args: sends.append(args), 2)
        bridge.dispatch(0, NS, {'state': 'uniform', 'announced': None}, 2)
        self.assertEqual(sends, [])
        report = {'deadline_ns': 40_000_000, 'complete_cohort': True, 'common_T0_ns': 0,
                  'sender_T0_monotonic_ns': 100*NS,
                  'window': {'start_ns': 105*NS, 'end_ns': 110*NS, 'as_of_ns': 111*NS},
                  **{k: fixture_feedback(0,2)[k] for k in ('counts','rtt_reply_samples','rtt_mean_ms')}}
        with self.assertRaises(ValueError):
            bridge.observation(0, report, 10, 11*NS, 'fixture')
        for role, t in c.decisions[0].get('targets', targets(2)).items():
            be = {'status_name':'verified','hw_aifs':'3','hw_cwmin':str(t['CWmin']),'hw_cwmax':str(t['CWmax'])}
            bridge.readback(0, role, be, 5*NS, 5*NS+1_000_000, 5*NS+2_000_000, 1_000_000)
        row = bridge.observation(0, report, 10, 11*NS, 'fixture')
        self.assertEqual(row['start_ns'], 5*NS)
        self.assertEqual(len(c.feedbacks), 1)
        report['sender_T0_monotonic_ns'] += NS
        with self.assertRaises(ValueError):
            bridge.observation(0, report, 10, 11*NS, 'fixture')

    def test_field_budget_reserves_recovery(self):
        plan = make_plan()
        self.assertTrue(may_start_run(plan, 2000, 200))
        self.assertFalse(may_start_run(plan, 2500, 100))

    def test_prepared_session_constructor_offline_and_authorization_guard(self):
        from rl_wifi.bounded40.session import PreparedSession
        import time
        with tempfile.TemporaryDirectory() as tmp:
            request = {'mode':'fixed','initial_w':64,'access_mode':'basic','measure_s':30,
                       'warmup_s':5,'tail_s':5,'segment_s':5,'guard_s':3,'deadline_ns':40_000_000,
                       'field_stop_monotonic':time.monotonic()+2700}
            entry = {'phase':'acceptance','states':['uniform','burst'],'policy':'sealed_actions_no_training'}
            with self.assertRaises(ValueError):
                PreparedSession(Path(tmp), None, request, entry)
            request['authorization'] = 'explicit_current_live_authorization_required'
            session = PreparedSession(Path(tmp), None, request, entry)
            self.assertEqual(session.policy_name, 'acceptance')
            self.assertEqual(session.initial_action, 2)
            self.assertIsNone(session.core)
            self.assertTrue(all(p.is_file() for p in session.payload()))

    def test_portable_node_scheduler_uses_local_absolute_boundaries(self):
        import importlib.util
        from rl_wifi.bounded40 import node_schedule
        class Thread:
            def __init__(self, target, **kw): self.target=target
            def start(self): self.target()
        class Parent:
            def __init__(self, config):
                self.c=config;self.role=config['role'];self.out=Path('.')
                self.plan={'T0_ns':30*NS};self.plan_ready=types.SimpleNamespace(wait=lambda n:True)
                self.state={};self.window=None;self.records=[];self.processes={}
                self.probe_error=None;self.probe_collector=None
            def control_reader(self): pass
            def check(self): pass
            def save(self): pass
            def setup(self): pass
            def wait(self, target): self.now=target
            def emit(self, kind, **data):
                row={'event':kind,'wall_ns':self.now,**data};self.records.append(row);return row
            def launch(self, key, args): self.processes[key]=(types.SimpleNamespace(send_signal=lambda v:None),None)
            def boundary(self, k, at):
                self.window=None;self.records.append({'event':'boundary','index':k,'at':at})
            def stats_start(self, k, at): self.window={'index':k}
            def latch_signature(self): pass
            def sample(self, k): pass
        fake=types.SimpleNamespace(LiveWorker=Parent,Worker=Parent,SCHEDULES={})
        fake_sender=types.SimpleNamespace(sender=lambda *a:None,echo_loop=lambda *a:None)
        path=Path(__file__).parents[1]/'bounded40/node.py'
        spec=importlib.util.spec_from_file_location('bounded40_fake_node',path)
        module=importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules', {'live_node':fake,'node_schedule':node_schedule,'sender':fake_sender}):
            spec.loader.exec_module(module)
            module.threading=types.SimpleNamespace(Thread=Thread)
            worker=module.BoundedWorker({'bounded40_schema':'dqn40-three-actions-v1','deadline_ns':40_000_000,
                'warmup_s':5,'tail_s':5,'segment_s':5,'measure_s':30,'guard_s':3,
                'application_states':['uniform','burst'],'role':'STA2','foreground':'STA1',
                'ap_ip':'fixture','own_ip':'fixture','port':1})
            worker.run()
        boundaries=[r['at'] for r in worker.records if r['event']=='boundary']
        self.assertEqual(boundaries,[ (35+5*k)*NS for k in range(7)])
        self.assertTrue(worker.state['completed_schedule'])


class Bounded40DQNTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import torch
        except ImportError:
            raise unittest.SkipTest('optional torch unavailable; no DQN claims')
        torch.set_num_threads(1)

    def model(self):
        from rl_wifi.bounded40.model import DQN
        return DQN()

    def test_true_parameter_updates_and_72_cycle_hard_limit(self):
        model = self.model()
        old = copy.deepcopy(model.online.state_dict())
        for n in (1, 2):
            result = exercise('DQN_train', model, TRAIN_STATES, str(n))
            self.assertTrue(result['committed'])
            self.assertFalse(result['hardware_training_claim_allowed'])
            self.assertEqual(len(result['transitions']), 36)
            self.assertTrue(result['transitions'][-1]['terminal'])
            self.assertEqual([r['action'] for r in result['decisions'][:6]], [2,0,1,2,0,1])
        self.assertEqual(model.steps, 72)
        self.assertEqual(model.updates, 65)
        self.assertTrue(any(not model.torch.equal(old[k], v) for k, v in model.online.state_dict().items()))
        transition = result['transitions'][0]
        with self.assertRaises(ValueError):
            model.observe(transition['state'], 0, 0, transition['next_state'])

    def test_failed_run_rolls_back_weights_optimizer_replay_and_rng(self):
        model = self.model()
        initial = model.checkpoint()
        result = exercise('DQN_train', model, TRAIN_STATES, 'failed', accepted=False)
        self.assertTrue(result['model_rolled_back'])
        self.assertFalse(result['committed'])
        after = model.checkpoint()
        for key in ('steps', 'updates', 'choices', 'rng'):
            self.assertEqual(initial[key], after[key])
        self.assertEqual(len(after['replay']), len(initial['replay']))
        self.assertEqual(after['optimizer'], initial['optimizer'])
        self.assertTrue(all(model.torch.equal(v, after['online'][k]) for k, v in initial['online'].items()))
        retried = exercise('DQN_train', model, TRAIN_STATES, 'retry')
        fresh = exercise('DQN_train', self.model(), TRAIN_STATES, 'fresh')
        self.assertEqual([r['action'] for r in retried['decisions']], [r['action'] for r in fresh['decisions']])

    def test_frozen_model_hash_source_and_test_never_updates(self):
        model = self.model()
        exercise('DQN_train', model, TRAIN_STATES, 'train')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'model.json'
            model.save(path, {'source': 'offline_fixture', 'accepted_run_ids': ['train'], 'evidence_hashes': {'test': 'fixture'}})
            with self.assertRaises(ValueError):
                type(model).load(path, 'live_hardware')
            frozen = type(model).load(path, 'offline_fixture')
            before = frozen.checkpoint()
            exercise('DQN', frozen, ('uniform','burst','burst','uniform'), 'test')
            self.assertEqual(before['steps'], frozen.steps)
            self.assertEqual(before['rng'], frozen.rng.getstate())
            self.assertTrue(all(frozen.torch.equal(v, frozen.online.state_dict()[k]) for k,v in before['online'].items()))
            data = json.loads(path.read_text())
            data['model']['config']['deadline_ms'] = 100
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                type(model).load(path)

    def test_performance_stop_preserves_measurements_and_rolls_back(self):
        model = self.model()
        c = Controller('DQN_train', 'bad', 0, 6, 'offline_fixture', model)
        ctx = {'state': 'burst', 'announced': None}
        for i in range(3):
            c.decide(i, (1+i*5)*NS, ctx, i-1 if i else 2)
            if i < 2:
                c.receive(fixture_feedback(i, (i+2)%3, on_time=400))
        with self.assertRaises(PerformanceStop):
            c.receive(fixture_feedback(2, 1, on_time=400))
        result = c.close(False, True, 30*NS, ctx)
        self.assertEqual(len(result['feedback']), 3)
        self.assertEqual(model.steps, 0)
        self.assertTrue(result['model_rolled_back'])

    def test_supervisor_one_retry_frozen_tests_and_final_restore(self):
        from rl_wifi.bounded40.campaign import execute
        import time
        calls=[]
        def run(entry, model, folder, stop_at):
            calls.append(entry['id'])
            if len(calls) == 1:
                return {'source':'offline_fixture','accepted':False,'restoration_confirmed':True,
                        'resources_released':True,'error':'injected collection failure; preserved'}
            policy = 'acceptance' if entry['phase']=='acceptance' else 'DQN_train' if entry['phase']=='train' else entry['policy']
            result = exercise(policy, model, tuple(entry['states']), entry['id'])
            return {'source':'offline_fixture','accepted':result['committed'],'restoration_confirmed':True,
                    'resources_released':True,'metrics':{'collection_valid':True,'background_Mbps':10.,
                    'states':{s:{'mature':3000,'on_time':2990} for s in ('uniform','burst')}}}
        with tempfile.TemporaryDirectory() as tmp:
            result = execute(Path(tmp)/'campaign',run,lambda:{'confirmed':True},
                             started_monotonic=time.monotonic(),source='offline_fixture')
            self.assertEqual(result['status'],'completed_fixed_budget')
            self.assertEqual(len(result['attempts']),14)
            self.assertEqual(len(result['accepted']),13)
            self.assertEqual(calls[:2],['acceptance','acceptance'])
            self.assertEqual(result['decision']['code'],'stop_no_repeated_practical_gain')
            self.assertEqual(result['unstarted'],[])
            self.assertTrue(result['final_restore']['confirmed'])

    def test_supervisor_second_failure_stops_and_budget_starts_nothing(self):
        from rl_wifi.bounded40.campaign import execute
        import time
        calls=[]
        def fail(*args):
            calls.append(1)
            return {'source':'offline_fixture','accepted':False,'restoration_confirmed':True,
                    'resources_released':True,'error':'fixture fault'}
        with tempfile.TemporaryDirectory() as tmp:
            result = execute(Path(tmp)/'retry',fail,lambda:{'confirmed':True},
                             started_monotonic=time.monotonic(),source='offline_fixture')
            self.assertEqual(result['status'],'stopped_gate_after_retry')
            self.assertEqual(len(calls),2)
            self.assertEqual(len(result['unstarted']),12)
            calls.clear()
            result = execute(Path(tmp)/'budget',fail,lambda:{'confirmed':True},
                             started_monotonic=time.monotonic()-2500,source='offline_fixture')
            self.assertEqual(result['status'],'stopped_budget')
            self.assertEqual(calls,[])
            self.assertTrue(result['final_restore']['confirmed'])


if __name__ == '__main__':
    unittest.main()
