import json
from pathlib import Path
import tempfile
import unittest
import uuid

from rl_wifi.deadline_block import DEADLINE_NS, application_metrics, targets
from rl_wifi.deadline_report import judge
from rl_wifi.rtt import ProbeCollector, echo_reply
from rl_wifi.schedules import MIXED


class DeadlineBlockTests(unittest.TestCase):
    def test_schedule_boundaries_and_batch_shapes(self):
        self.assertEqual(MIXED.count(70_000_000_000), 7000)
        for i in range(4):
            first = 500+1500*i
            start = (5+15*i)*1_000_000_000
            self.assertEqual(MIXED.offset_ns(first), start)
            self.assertEqual(MIXED.offset_ns(first+1), start+(10_000_000 if i%2==0 else 0))
        self.assertEqual(MIXED.offset_ns(6500), 65_000_000_000)
        self.assertEqual(MIXED.offset_ns(7000), 70_000_000_000)
        self.assertEqual(targets('dynamic', 'burst')['STA3'], 64)
        self.assertEqual(targets('dynamic', 'uniform')['STA3'], 16)

    def test_replay_keeps_boundary_messages_in_generation_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            c = ProbeCollector(str(uuid.uuid4()), 1, DEADLINE_NS)
            events = []
            for seq in range(7000):
                at = MIXED.offset_ns(seq)
                events.append((at, 0, seq))
                if seq != 2000:
                    events.append((at+(50_000_000 if seq==1999 else 1_000_000), 1, seq))
            requests = {}
            for at, event, seq in sorted(events):
                if event:
                    c.receive(echo_reply(requests[seq]), at)
                else:
                    requests[seq] = c.generate(seq, at, at)
                    if seq == 2000:
                        c.send_failed(seq, at, 'generator_missed_schedule')
                    else:
                        c.submitted(seq, at)
            c.export_events(folder/'application_probe.jsonl')
            (folder/'application_final.json').write_text(json.dumps({'collection_complete': True,
                'report': c.report(0, 70_000_000_000, 70_040_000_000)}))
            with (folder/'application_plan.jsonl').open('w') as stream:
                for seq in range(7000):
                    offset = MIXED.offset_ns(seq)
                    stream.write(json.dumps({'seq': seq, 'T0_offset_ns': offset,
                                             'deadline_T0_offset_ns': offset+DEADLINE_NS})+'\n')
            result = application_metrics(folder)
            self.assertEqual(result['states']['uniform']['counts']['late'], 1)
            self.assertEqual(result['states']['burst']['counts']['local_failure'], 1)
            self.assertEqual(result['states']['burst']['counts']['mature'], 3000)
            self.assertEqual(len(result['on_time_1s']), 60)

    def test_decision_uses_best_feasible_fixed_and_handles_empty_set(self):
        def row(policy, on_time, mbps):
            return {'policy': policy, 'metrics': {'application': {'states': {
                'uniform': {'on_time_ratio': 1}, 'burst': {'on_time_ratio': on_time}}},
                'background_Mbps': mbps}}
        rows = [row('P', .99, 9), row('E', .8, 12), row('middle', .95, 10), row('dynamic', .95, 10.4)]
        self.assertEqual(judge(rows)['code'], 'stop_middle')
        self.assertEqual(judge(rows)['best_fixed'], 'middle')
        rows[-1] = row('dynamic', .95, 10.5)
        self.assertEqual(judge(rows)['code'], 'replication_signal')
        rows[:3] = [row('P', .9, 9), row('E', .8, 12), row('middle', .94, 10)]
        self.assertIsNone(judge(rows)['background_gain'])
        self.assertEqual(judge(rows)['code'], 'deadline_advantage')
