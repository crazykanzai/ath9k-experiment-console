import json
import errno
from pathlib import Path
import tempfile
import unittest

from rl_wifi.rtt import ProbeCollector, decode, echo_reply, replay_events
from rl_wifi.probe_transport import collect_sender, echo_once
from rl_wifi.spec import RewardConfig,features,observation_from_report

RUN='00000000-0000-4000-8000-000000000001'


class RTTTests(unittest.TestCase):
    def test_all_generated_denominator_deadline_duplicate_local_fail_and_missing(self):
        c=ProbeCollector(RUN,1,100)
        packets=[c.generate(i,0) for i in range(5)]
        for i in range(3):c.submitted(i,1)
        c.send_failed(3,1,'EAGAIN')
        c.receive(echo_reply(packets[0]),100)  # exactly at deadline is on time
        c.receive(echo_reply(packets[0]),101)
        c.receive(echo_reply(packets[1]),102)
        c.generate(5,150)
        report=c.report(0,160,160)
        self.assertEqual(report['counts'],{'generated':6,'mature':5,'pending':1,'on_time':1,
                         'late':1,'no_reply':1,'local_failure':1,'never_submitted':1})
        self.assertAlmostEqual(report['timeout_ratio'],.8)
        self.assertEqual(report['rtt_reply_samples'],2)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'events.jsonl';c.export_events(path)
            before=path.read_bytes()
            self.assertEqual(replay_events(path).report(0,160,160),report)
            self.assertEqual(path.read_bytes(),before)
            with self.assertRaises(FileExistsError):c.export_events(path)

    def test_wrong_run_node_corrupt_packet_and_unsubmitted_cannot_be_success(self):
        c=ProbeCollector(RUN,1,100);packet=c.generate(1,0)
        self.assertFalse(c.receive(echo_reply(packet),1))
        c.submitted(1,2)
        other=ProbeCollector('00000000-0000-4000-8000-000000000002',1,100)
        self.assertFalse(c.receive(echo_reply(other.generate(1,0)),3))
        self.assertFalse(c.receive(b'bad',4))
        self.assertEqual(c.report(0,10,100)['timeout_ratio'],1)
        with self.assertRaises(ValueError):c.generate(2,1)
        with self.assertRaises(ValueError):c.generate(1,5)

    def test_generation_wait_is_not_disguised_as_rtt(self):
        c=ProbeCollector(RUN,1,100)
        packet=c.generate(0,80,scheduled_ns=0);c.submitted(0,80)
        c.receive(echo_reply(packet),110)
        r=c.report(0,1,110)
        self.assertEqual(r['counts']['late'],1)
        self.assertEqual(r['rtt_mean_ms'],30/1e6)
        self.assertEqual(r['generation_to_reply_mean_ms'],110/1e6)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'e.jsonl';c.export_events(path)
            self.assertEqual(replay_events(path).report(0,1,110),r)

    def test_all_lost_is_valid_high_risk_but_missing_collection_is_not_zero(self):
        c=ProbeCollector(RUN,1,100);c.generate(0,0);c.submitted(0,0)
        r=c.report(0,1,100)
        obs=observation_from_report(r,0,5,4)
        f=features(obs,RewardConfig(deadline_ms=.0001))
        self.assertEqual(f[2:5],[4.,0.,1.])
        r['complete_cohort']=False
        with self.assertRaises(ValueError):observation_from_report(r,0,5,4)

    def test_in_memory_transport_archive_and_bounded_drain_no_sockets(self):
        class Clock:
            now=0
            def read(self):return self.now
            def wait(self,seconds):self.now+=round(seconds*1e9)
        class FakeUDP:
            replies=[]
            def sendto(self,packet,peer):self.replies.append((echo_reply(packet),peer));return len(packet)
            def recvfrom(self,size):
                if not self.replies:raise BlockingIOError()
                return self.replies.pop(0)
        clock=Clock();events=[];collector=ProbeCollector(RUN,1,5_000_000)
        result=collect_sender(FakeUDP(),('fake',100),collector,2_000_000,10_000_000,
                              2_000_000,clock.read,clock.wait,event_sink=events.append)
        self.assertTrue(result['collection_complete'])
        self.assertEqual(result['report']['counts']['on_time'],5)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'stream.jsonl';p.write_text('\n'.join(json.dumps(e) for e in events))
            self.assertEqual(replay_events(p).report(2_000_000,12_000_000,17_000_000),
                             {k:v for k,v in result['report'].items() if k!='collection_complete'})

    def test_sender_scheduler_stall_records_all_expected_messages_and_abort_is_incomplete(self):
        class DropUDP:
            def sendto(self,*args):raise BlockingIOError('buffer full')
            def recvfrom(self,*args):raise BlockingIOError()
        times=iter([100,100,100,100,100,100,100,100,100,100])
        c=ProbeCollector(RUN,1,5)
        result=collect_sender(DropUDP(),('fake',1),c,0,10,2,lambda:next(times),lambda x:None)
        self.assertEqual(result['report']['counts']['generated'],5)
        self.assertEqual(result['report']['counts']['local_failure'],5)
        c=ProbeCollector(RUN,1,5)
        result=collect_sender(DropUDP(),('fake',1),c,0,10,2,lambda:0,lambda x:None,lambda:True)
        self.assertFalse(result['collection_complete']);self.assertIsNone(result['report'])

    def test_transport_fault_is_not_reported_as_valid_performance_loss(self):
        class Broken:
            def sendto(self,*args):raise OSError(errno.EBADF,'closed socket')
        events=[];c=ProbeCollector(RUN,1,100)
        with self.assertRaises(OSError):
            collect_sender(Broken(),('fake',1),c,0,10,2,lambda:0,lambda x:None,event_sink=events.append)
        self.assertEqual(events[-1]['event'],'send_failed')
        self.assertIn('closed socket',events[-1]['reason'])

    def test_mean_sums_integer_ns_before_converting_units(self):
        c=ProbeCollector(RUN,1,10**9)
        packets=[c.generate(i,0) for i in range(3)]
        for i in range(3):c.submitted(i,1)
        durations=[100000001,200000003,300000007]
        for i,ns in enumerate(durations):c.receive(echo_reply(packets[i]),1+ns)
        r=c.report(0,1,10**9)
        self.assertEqual(r['rtt_mean_ms'],sum(durations)/3/1e6)

    def test_receiver_rejects_malformed_and_does_not_amplify(self):
        p=ProbeCollector(RUN,1,100).generate(0,0)
        class Fake:
            packets=[(b'bad',('x',1)),(p,('x',1))];sent=[]
            def recvfrom(self,size):
                if not self.packets:raise BlockingIOError()
                return self.packets.pop(0)
            def sendto(self,p,peer):self.sent.append(p);return len(p)
        sock=Fake();counts=echo_once(sock)
        self.assertEqual(counts,{'received':2,'replied':1,'malformed':1,'send_failed':0})
        self.assertLess(len(sock.sent[0]),len(p))


if __name__=='__main__':unittest.main()
