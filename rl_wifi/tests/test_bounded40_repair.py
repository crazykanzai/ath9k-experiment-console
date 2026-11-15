"""Offline timing-path regressions: no network, radio or background training."""
import io
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from rl_wifi.rtt import ProbeCollector, echo_reply
from rl_wifi.bounded40.collector import IndexedCollector
from rl_wifi.bounded40.event_journal import EventJournal
from rl_wifi.bounded40.transport import heartbeat, await_clean_exits

RUN = '00000000-0000-0000-0000-000000000001'
NS = 10**9


class CollectorRepairTests(unittest.TestCase):
    def test_indexed_reports_match_original_for_all_outcomes(self):
        collectors = [ProbeCollector(RUN,1,40_000_000), IndexedCollector(RUN,1,40_000_000)]
        for c in collectors:
            for i in range(12):
                at = i*100_000_000
                packet=c.generate(i,at)
                if i%4==0: c.send_failed(i,at,'generator_missed_schedule')
                else:
                    c.submitted(i,at+1)
                    if i%4!=3: c.receive(echo_reply(packet),at+(20_000_000 if i%4==1 else 60_000_000))
        for start,end,now in [(0,NS,2*NS),(0,2*NS,2*NS),(NS,2*NS,2*NS)]:
            self.assertEqual(collectors[0].report(start,end,now),collectors[1].report(start,end,now))

    def test_mature_pending_same_timestamp_and_immutable_snapshot(self):
        c=IndexedCollector(RUN,1,40_000_000)
        packets=[c.generate(i,0) for i in range(10)]
        for i in range(10): c.submitted(i,1)
        view=c.snapshot(0,1)
        self.assertEqual(c.report(0,1,20_000_000)['counts']['pending'],10)
        c.receive(echo_reply(packets[0]),30_000_000)
        self.assertEqual(view.report(0,1,50_000_000)['counts']['no_reply'],10)
        self.assertEqual(c.report(0,1,50_000_000)['counts']['on_time'],1)

    def test_only_cohort_is_copied_and_clock_evaluated_with_lock(self):
        c=IndexedCollector(RUN,1,40_000_000)
        for i in range(19000): c.generate(i,i*10_000_000)
        def clock():
            self.assertTrue(c.lock._is_owned())
            return 191*NS
        report=c.report_now(180*NS,185*NS,clock)
        self.assertEqual(report['counts']['generated'],500)
        self.assertEqual(c.last_snapshot_rows,500)
        c.report(180*NS,181*NS,191*NS)
        self.assertEqual(c.last_snapshot_rows,100)
        with self.assertRaises(ValueError): c.report('bad',1,191*NS)
        with self.assertRaises(ValueError): c.generate(19000,191*NS,1)


class JournalRepairTests(unittest.TestCase):
    def test_order_and_final_flush_preserve_every_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'events.jsonl'
            journal=EventJournal(path)
            for i in range(1000): journal.write({'seq':i,'text':'原始记录'})
            journal.close()
            self.assertEqual([json.loads(x)['seq'] for x in path.read_text(encoding='utf-8').splitlines()],list(range(1000)))
            with self.assertRaises(RuntimeError): journal.write({'seq':1000})

    def test_io_failure_is_not_success(self):
        class Broken(io.StringIO):
            def write(self,value): raise OSError('injected disk failure')
        j=EventJournal('unused',opener=Broken)
        j.write({'seq':1})
        with self.assertRaisesRegex(RuntimeError,'not fully persisted'): j.close()

    def test_bounded_queue_never_silently_drops(self):
        entered=threading.Event(); release=threading.Event()
        class Blocked(io.StringIO):
            def write(self,value):
                entered.set(); release.wait(2)
                return super().write(value)
        j=EventJournal('unused',capacity=1,opener=Blocked)
        try:
            j.write({'seq':0}); self.assertTrue(entered.wait(1))
            j.write({'seq':1})
            with self.assertRaisesRegex(RuntimeError,'capacity exhausted'): j.write({'seq':2})
        finally:
            release.set(); j.close()

    def test_failed_final_flush_prevents_success_receipt(self):
        from rl_wifi.bounded40 import sender as module
        class BrokenJournal:
            def __init__(self,*a): pass
            def write(self,*a): pass
            def close(self): raise OSError('flush failed')
        with tempfile.TemporaryDirectory() as tmp:
            worker=types.SimpleNamespace(c={'id':RUN,'deadline_ns':40_000_000,'ap_ip':'fixture',
                'probe_port':1,'probe_interval_ns':10_000_000},out=Path(tmp),plan={'T0_ns':0},
                total=40,probe_socket=None,sender_stop=threading.Event(),stop=threading.Event(),
                schedule=None,state={},probe_error=None)
            with patch.object(module,'EventJournal',BrokenJournal), patch.object(module,'collect_sender',return_value={'collection_complete':True}):
                module.sender(worker,0)
            self.assertFalse((Path(tmp)/'application_final.json').exists())
            self.assertIn('flush failed',worker.state['error'])
            self.assertTrue(worker.stop.is_set())


class TransportRepairTests(unittest.TestCase):
    def session(self,receipt=True,code=0):
        def send(msg): raise OSError('Socket is closed')
        self.now=10.
        s=types.SimpleNamespace(last_heartbeat=0,workers={'STA1':types.SimpleNamespace(send=send)},
             finished={},state={'T0_ns':NS},request={'warmup_s':5,'measure_s':30,'tail_s':5},events=[])
        def receive(**kw):
            self.now+=.1
            if receipt: s.finished['STA1']={'completed_schedule':True,'error':None}
            s.state['process_exit_codes']={'STA1':code}
        s.receive=receive
        s.event=lambda *a,**kw:s.events.append((a,kw))
        return s

    def test_closed_normal_finish_consumes_queued_receipt_and_eof(self):
        s=self.session()
        heartbeat(s,lambda:self.now,lambda:42*NS)
        self.assertEqual(len(s.events),1)
        await_clean_exits(s,monotonic=lambda:self.now)

    def test_early_closed_channel_still_fails(self):
        s=self.session()
        with self.assertRaises(OSError): heartbeat(s,lambda:self.now,lambda:40*NS)
        self.assertFalse(s.finished)

    def test_missing_finished_and_nonzero_exit_are_failures(self):
        for receipt,code in [(False,0),(True,1)]:
            s=self.session(receipt,code)
            with self.assertRaises(RuntimeError): heartbeat(s,lambda:self.now,lambda:42*NS,grace_s=.3)

    def test_failed_schedule_cannot_be_reconciled(self):
        s=self.session()
        def receive(**kw):
            self.now+=.1; s.finished['STA1']={'completed_schedule':False,'error':'failure'}
            s.state['process_exit_codes']={'STA1':0}
        s.receive=receive
        with self.assertRaises(RuntimeError): heartbeat(s,lambda:self.now,lambda:42*NS)

    def test_every_worker_clean_exit_is_required(self):
        s=self.session();s.workers['AP']=object()
        with self.assertRaises(RuntimeError): await_clean_exits(s,timeout=.3,monotonic=lambda:self.now)


if __name__=='__main__': unittest.main()
