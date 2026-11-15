import tempfile
import unittest
import uuid
from pathlib import Path

from rl_wifi.schedules import MessageSchedule,UNIFORM,BURST
from rl_wifi.probe_transport import collect_sender
from rl_wifi.rtt import ProbeCollector,echo_reply,replay_events
from rl_wifi.screening_plan import make_plan,confirmation_order,select_action,meaningful_preference


class Clock:
    def __init__(self):self.now=0
    def read(self):return self.now
    def wait(self,seconds):self.now+=round(seconds*1e9)


class Socket:
    def __init__(self,clock,fail=False):self.clock=clock;self.replies=[];self.fail=fail;self.submissions=[]
    def sendto(self,packet,peer):
        if self.fail:raise BlockingIOError('full local socket')
        self.submissions.append(self.clock.now);self.replies.append((echo_reply(packet),peer));return len(packet)
    def recvfrom(self,size):
        if not self.replies:raise BlockingIOError()
        return self.replies.pop(0)


class ScheduleTests(unittest.TestCase):
    def collect(self,schedule,clock=None,fail=False,duration=1_000_000_000):
        clock=clock or Clock();sock=Socket(clock,fail)
        collector=ProbeCollector(str(uuid.uuid4()),1,100_000_000)
        events=[]
        result=collect_sender(sock,('ap',5201),collector,0,duration,10_000_000,
                              clock.read,clock.wait,schedule=schedule,event_sink=events.append)
        return result,collector,sock,events

    def test_equal_long_term_load_and_exact_burst_plan(self):
        for schedule in (UNIFORM,BURST):
            self.assertEqual(schedule.count(40_000_000_000),4000)
            offsets=[schedule.offset_ns(i) for i in range(4000)]
            self.assertEqual(sum(5_000_000_000<=t<35_000_000_000 for t in offsets),3000)
        self.assertEqual([BURST.offset_ns(i) for i in range(11)],[0]*10+[100_000_000])
        self.assertEqual(UNIFORM.lateness_budget_ns,BURST.lateness_budget_ns)

    def test_both_schedules_submit_all_and_replay_exactly(self):
        for schedule in (UNIFORM,BURST):
            result,c,sock,events=self.collect(schedule)
            self.assertEqual(result['planned_message_count'],100)
            self.assertEqual(result['report']['counts']['on_time'],100)
            self.assertEqual(result['report']['counts']['local_failure'],0)
            self.assertEqual(len([e for e in events if e['event']=='generated']),100)
            self.assertEqual(len(sock.submissions),100)
            with tempfile.TemporaryDirectory() as temp:
                p=Path(temp)/'events.jsonl';c.export_events(p);replayed=replay_events(p)
                self.assertEqual(c.rows,replayed.rows)

    def test_full_40s_fake_execution_keeps_3000_common_messages(self):
        for schedule in (UNIFORM,BURST):
            result,c,_,_=self.collect(schedule,duration=40_000_000_000)
            self.assertEqual(result['report']['counts']['generated'],4000)
            common=c.report(5_000_000_000,35_000_000_000,c.last_time+100_000_000)
            self.assertEqual(common['counts']['mature'],3000)
            self.assertEqual(common['counts']['on_time'],3000)
            self.assertEqual(common['counts']['pending'],0)

    def test_delayed_batch_is_accounted_without_catchup_blast(self):
        clock=Clock();clock.now=20_000_000
        result,c,sock,_=self.collect(BURST,clock)
        self.assertEqual(result['report']['counts']['generated'],100)
        self.assertEqual(result['report']['counts']['local_failure'],10)
        self.assertEqual(result['report']['counts']['on_time'],90)
        self.assertEqual(sock.submissions[0],100_000_000)
        self.assertTrue(all(c.rows[i]['local_failure']=='generator_missed_schedule' for i in range(10)))

    def test_execution_blocked_past_end_records_every_planned_message(self):
        clock=Clock();clock.now=2_000_000_000
        result,_,sock,_=self.collect(BURST,clock)
        self.assertEqual(result['report']['counts']['local_failure'],100)
        self.assertEqual(result['report']['timeout_ratio'],1)
        self.assertFalse(sock.submissions)

    def test_local_send_failures_stay_in_deadline_denominator(self):
        result,_,_,_=self.collect(BURST,fail=True)
        self.assertEqual(result['report']['counts']['mature'],100)
        self.assertEqual(result['report']['counts']['local_failure'],100)
        self.assertEqual(result['report']['timeout_ratio'],1)

    def test_invalid_schedule_rejected(self):
        for args in (('unknown',10,1),('burst',0,10),('burst',10,True),('uniform',10,10)):
            with self.assertRaises(ValueError):MessageSchedule(*args)

    def test_sealed_order_bounds_and_conditional_filter(self):
        p=make_plan();self.assertEqual(p,make_plan())
        self.assertFalse(p['execution_authorized']);self.assertEqual(len(p['screen_order']),18)
        self.assertEqual({(r['action_index'],r['state']) for r in p['screen_order']},
                         {(a,s) for a in range(9) for s in ('uniform','burst')})
        confirm=confirmation_order(p,{'uniform':0,'burst':8})
        self.assertEqual(len(confirm),12)
        for b in (1,2):
            self.assertEqual({(r['action_index'],r['state']) for r in confirm if r['block']==b},
                             {(a,s) for a in (0,2,8) for s in ('uniform','burst')})

    def test_selection_does_not_reward_short_rtt_with_more_timeouts(self):
        rows=[{'action_index':i,'valid':True,'timeout_ratio':.5,'background_Mbps':10,'rtt_p95_ms':1} for i in range(9)]
        rows[2].update(timeout_ratio=.01,background_Mbps=5,rtt_p95_ms=80)
        self.assertEqual(select_action(rows),2)
        rows[3].update(timeout_ratio=.02,background_Mbps=6,rtt_p95_ms=90)
        self.assertEqual(select_action(rows),3)
        self.assertFalse(meaningful_preference(rows[0],rows[2]))
        with self.assertRaises(ValueError):select_action(rows[:-1])


if __name__=='__main__':unittest.main()
