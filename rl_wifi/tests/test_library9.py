"""No device access: sealed matrix, common application bytes and stop decisions."""
import importlib.util
from pathlib import Path
import struct
import sys
import unittest
import tempfile
import threading
import types
import socket
from unittest.mock import patch
from rl_wifi.library9.metrics import ACTIVE,ORDER,NS,aggregate,judge

class LibraryTests(unittest.TestCase):
    def test_exact_nine_and_active_identity(self):
        self.assertEqual(len(ORDER),9)
        self.assertEqual(set(ORDER),{(s,w) for s in ACTIVE for w in (16,32,64)})
        self.assertEqual(ACTIVE['M'],('STA1','STA2'))

    def test_half_open_common_window_and_offset(self):
        rows=[{'event':'received','node':'STA1','bytes':100,'wall_ns':t+2*NS} for t in (5*NS-1,5*NS,35*NS-1,35*NS)]
        r=aggregate(rows,0,2*NS,1,('STA1',))
        self.assertAlmostEqual(r['total_Mbps'],200*8/30e6)
        self.assertEqual(r['Jain'],1)
        self.assertEqual(r['per_STA_Mbps_1s']['STA1'][0],.0008)

    def test_fairness_is_performance_not_validity(self):
        rows=[{'event':'received','node':'STA1','bytes':1000,'wall_ns':6*NS},
              {'event':'received','node':'STA2','bytes':1,'wall_ns':6*NS}]
        self.assertFalse(aggregate(rows,0,0,0,ACTIVE['M'])['fairness_target_met'])
        with self.assertRaises(ValueError):aggregate(rows,0,0,0,ACTIVE['L'])

    def fixtures(self):
        return [{'state':s,'W':w,'accepted':True,'metrics':{'total_Mbps':10.,'fairness_target_met':True}} for s,w in ORDER]

    def test_same_fixed_enough_and_incomplete_not_conclusive(self):
        rows=self.fixtures();self.assertEqual(judge(rows)['code'],'stop_no_practical_dynamic_signal')
        rows[0]['accepted']=False;self.assertEqual(judge(rows)['code'],'incomplete_screen_no_policy_conclusion')

    def test_optimistic_dynamic_only_signal_not_RL_claim(self):
        rows=self.fixtures()
        for r in rows:
            r['metrics']['total_Mbps']=12 if {'L':16,'M':32,'H':64}[r['state']]==r['W'] else 10
        result=judge(rows)
        self.assertEqual(result['code'],'candidate_signal_needs_replication_and_switch_test')
        self.assertFalse(result['RL_advantage_proven']);self.assertEqual(result['repeats_per_condition'],1)

    def test_unfair_candidates_excluded(self):
        rows=self.fixtures()
        for r in rows:
            if r['state']=='H':r['metrics']['fairness_target_met']=False
        self.assertEqual(judge(rows)['code'],'fairness_capacity_gap')

    def test_tcp_air_parser_data_ack_udp_and_truncation(self):
        path=Path(__file__).parents[1]/'library9/air.py'
        with patch.dict(sys.modules,{}),patch.object(sys,'path',[str(path.parents[2]/'experiments')]+sys.path):
            spec=importlib.util.spec_from_file_location('library_air_test',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            rt=b'\0\0\x08\0\0\0\0\0';hdr=struct.pack('<H',0x0108)+bytes(22);llc=b'\xaa\xaa\x03\0\0\0\x08\0'
            ip=bytearray(20);ip[0]=0x45;ip[9]=6;ip[2:4]=(1240).to_bytes(2,'big')
            tcp=bytearray(20);tcp[2:4]=(5201).to_bytes(2,'big');tcp[12]=0x50
            packet=rt+hdr+llc+ip+tcp+bytes(1200)
            self.assertEqual(module.tcp_payload(packet),(5201,1200))
            ip[2:4]=(40).to_bytes(2,'big');self.assertIsNone(module.tcp_payload(rt+hdr+llc+ip+tcp))
            ip[9]=17;self.assertIsNone(module.tcp_payload(rt+hdr+llc+ip+tcp))
            self.assertIsNone(module.tcp_payload(rt+hdr))

    def test_session_payload_explicit_and_authorization(self):
        import tempfile,time
        from rl_wifi.library9.session import TCPSession
        with tempfile.TemporaryDirectory() as tmp:
            req={'mode':'fixed','initial_w':32,'access_mode':'basic','warmup_s':5,'measure_s':30,'tail_s':5,
                 'segment_s':15,'guard_s':3,'field_stop_monotonic':time.monotonic()+1200}
            with self.assertRaises(ValueError):TCPSession(Path(tmp),None,req,{'state':'M','W':32})
            req['authorization']='library9-authorized';s=TCPSession(Path(tmp),None,req,{'state':'M','W':32})
            self.assertTrue(all(p.is_file() for p in s.payload()))
            self.assertEqual(s.stages[1]['STA4'],32)

    def test_real_local_tcp_receiver_journal_matches_delivered_bytes(self):
        # Loopback only, no SSH/devices. Linux hardware parent is replaced.
        from rl_wifi.bounded40 import event_journal
        path=Path(__file__).parents[1]/'library9/node.py'
        with patch.object(sys,'path',[str(path.parents[2]/'experiments'),str(path.parent)]+sys.path), patch.dict(sys.modules,{
            'demo_node':types.SimpleNamespace(Worker=object),'event_journal':event_journal,
            'resource':types.SimpleNamespace()}):
            spec=importlib.util.spec_from_file_location('library_node_test',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            w=module.TCPWorker.__new__(module.TCPWorker)
            w.c={'active_senders':['STA1'],'business_ips':{'STA1':'127.0.0.1'}};w.sockets=[];w.stop=threading.Event()
            w.state={};w.plan={'T0_ns':0};w.tcp_error=None;w.received_totals={'STA1':0};w.receiver_eof=set()
            listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1);listener.settimeout(.2)
            with tempfile.TemporaryDirectory() as tmp:
                w.journal=event_journal.EventJournal(Path(tmp)/'recv.jsonl')
                thread=threading.Thread(target=w.receiver,args=(listener,'STA1'));thread.start()
                client=socket.create_connection(listener.getsockname());client.sendall(b'x'*200000);client.shutdown(socket.SHUT_WR)
                thread.join(2);client.close();listener.close()
                for sock in w.sockets:sock.close()
                w.journal.close();self.assertFalse(thread.is_alive());self.assertIsNone(w.tcp_error)
                import json
                rows=[json.loads(x) for x in (Path(tmp)/'recv.jsonl').read_text().splitlines()]
                self.assertEqual(sum(r['bytes'] for r in rows if r['event']=='received'),200000)
                self.assertEqual(w.received_totals['STA1'],200000)
                self.assertEqual(w.receiver_eof,{'STA1'})

    def test_four_uploaders_AP_cleanup_waits_for_FIN_and_all_queued_bytes(self):
        from rl_wifi.bounded40 import event_journal
        import time,json
        path=Path(__file__).parents[1]/'library9/node.py'
        class Parent:
            def cleanup(self):self.parent_cleanup_called=True
        resource=types.SimpleNamespace(RUSAGE_SELF=0,getrusage=lambda _:types.SimpleNamespace(ru_utime=0,ru_stime=0))
        with patch.object(sys,'path',[str(path.parents[2]/'experiments'),str(path.parent)]+sys.path), patch.dict(sys.modules,{
            'demo_node':types.SimpleNamespace(Worker=Parent),'event_journal':event_journal,'resource':resource}):
            spec=importlib.util.spec_from_file_location('library_node_drain_test',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            # Delayed FIN after six seconds is valid drainage, not additional
            # application generation. A missing FIN still fails after the bound.
            fake=types.SimpleNamespace(c={'active_senders':['STA1']},receiver_eof=set(),tcp_error=None,state={})
            ticks=[0.]
            def advance(_):
                ticks[0]+=1
                if ticks[0]>=6:fake.receiver_eof.add('STA1')
            module.drain_finished_uploads(fake,clock=lambda:ticks[0],wait=advance)
            self.assertNotIn('error',fake.state);self.assertEqual(ticks[0],6)
            fake.receiver_eof.clear();fake.state={};ticks[0]=0
            module.drain_finished_uploads(fake,clock=lambda:ticks[0],wait=lambda _:ticks.__setitem__(0,ticks[0]+1))
            self.assertIn('incomplete',fake.state['error']);self.assertEqual(ticks[0],30)
            w=module.TCPWorker.__new__(module.TCPWorker);roles=list(ACTIVE['H'])
            w.c={'active_senders':roles,'business_ips':dict.fromkeys(roles,'127.0.0.1')}
            w.role='AP';w.sockets=[];w.listeners=[];w.threads=[];w.stop=threading.Event();w.receiver_eof=set()
            w.state={'completed_schedule':True};w.plan={'T0_ns':0};w.tcp_error=None;w.received_totals=dict.fromkeys(roles,0)
            w.cpu_before=resource.getrusage(0);release=threading.Event();senders=[];errors=[]
            with tempfile.TemporaryDirectory() as tmp:
                w.journal=event_journal.EventJournal(Path(tmp)/'recv.jsonl')
                for role in roles:
                    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1);listener.settimeout(.2);w.listeners.append(listener)
                    receiver=threading.Thread(target=w.receiver,args=(listener,role));receiver.start();w.threads.append(receiver)
                    def upload(address=listener.getsockname()):
                        try:
                            with socket.create_connection(address) as client:
                                client.sendall(b'x'*524288);release.wait(2);client.sendall(b'y'*32768);client.shutdown(socket.SHUT_WR)
                        except Exception as exc:errors.append(str(exc))
                    sender=threading.Thread(target=upload);sender.start();senders.append(sender)
                try:
                    until=time.monotonic()+2
                    while not all(w.received_totals.values()) and time.monotonic()<until:time.sleep(.005)
                    cleanup=threading.Thread(target=w.cleanup);cleanup.start();time.sleep(.05)
                    self.assertTrue(cleanup.is_alive());self.assertFalse(w.stop.is_set())
                finally:release.set()
                for sender in senders:sender.join(2)
                cleanup.join(2)
                self.assertFalse(cleanup.is_alive());self.assertFalse(errors);self.assertIsNone(w.tcp_error)
                self.assertEqual(w.state['tcp_eof_nodes'],roles)
                self.assertEqual(w.received_totals,dict.fromkeys(roles,557056))
                rows=[json.loads(x) for x in (Path(tmp)/'recv.jsonl').read_text().splitlines()]
                self.assertEqual(sum(r['bytes'] for r in rows if r['event']=='received'),557056*4)

if __name__=='__main__':unittest.main()
