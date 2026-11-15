"""Private Linux TCP worker. Reuses owned radio/trace/restore helpers unchanged."""
import json
import socket
import threading
import time
import traceback
import resource
import struct
from demo_node import Worker
from air_timing import decode as air_decode
from air import tcp_payload
from event_journal import EventJournal

NS=10**9

def drain_finished_uploads(worker,timeout_s=30,clock=time.monotonic,wait=time.sleep):
    """Bounded post-run FIN drain, outside the fixed common measurement window.

    TCP may have megabytes accepted by send() but not yet delivered. Never
    discard those bytes or turn an incomplete stream into a successful receipt.
    """
    deadline=clock()+timeout_s
    while worker.receiver_eof!=set(worker.c['active_senders']) and clock()<deadline and not worker.tcp_error:
        wait(.01)
    worker.state['tcp_drain_timeout_s']=timeout_s
    worker.state['tcp_eof_nodes']=sorted(worker.receiver_eof)
    if worker.receiver_eof!=set(worker.c['active_senders']):
        worker.state['error']='TCP FIN/drain receipts incomplete within '+str(timeout_s)+'s'

class TCPWorker(Worker):
    def __init__(self,c):
        if c.get('library_schema')!='library-tcp9-v1' or (c['warmup_s'],c['measure_s'],c['tail_s'],c['segment_s'])!=(5,30,5,15):raise ValueError('TCP9 sealed profile')
        super().__init__(c)
        self.threads=[];self.listeners=[];self.sockets=[];self.tcp_error=None
        self.journal=EventJournal(self.out/'tcp_receive.jsonl') if self.role=='AP' else None
        self.cpu_before=resource.getrusage(resource.RUSAGE_SELF)
        self.received_totals={n:0 for n in c['active_senders']}
        self.receiver_eof=set()

    def fail_tcp(self,exc):
        self.tcp_error=str(exc);self.state.update(error=str(exc),tcp_error=str(exc),tcp_traceback=traceback.format_exc());self.stop.set()

    def launch(self,name,args):
        if self.role=='AP' and name.startswith('iperf_server_'):
            port=int(name.rsplit('_',1)[1]);role=next(n for n,p in self.c['sta_ports'].items() if p==port)
            sock=socket.socket();sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,self.iface.encode()+b'\0')
            sock.bind((self.c['ap_ip'],port));sock.listen(1);sock.settimeout(.2);self.listeners.append(sock)
            thread=threading.Thread(target=self.receiver,args=(sock,role),daemon=True);thread.start();self.threads.append(thread)
        else:
            if self.role=='AP' and name=='tcpdump':
                args=[str(self.out/'ap_tcp.pcap') if v==str(self.out/'ap_udp.pcap') else v for v in args]
                args[-1]=args[-1].replace('udp and','tcp and',1)
            super().launch(name,args)

    def receiver(self,listener,role):
        try:
            conn=None
            while not self.stop.is_set():
                try:conn,peer=listener.accept();break
                except socket.timeout:continue
            if conn is None:return
            self.sockets.append(conn);conn.settimeout(.2)
            if role not in self.c['active_senders'] or peer[0]!=self.c['business_ips'][role]:raise RuntimeError('unexpected TCP upload peer')
            self.journal.write({'event':'accepted','node':role,'wall_ns':time.time_ns(),'peer':peer})
            total=0
            while not self.stop.is_set():
                try:data=conn.recv(65536)
                except socket.timeout:continue
                if not data:
                    self.journal.write({'event':'eof','node':role,'wall_ns':time.time_ns(),'total_bytes':total})
                    if self.plan and time.time_ns()<self.plan['T0_ns']+35*NS:raise RuntimeError('TCP upload ended before common measurement end')
                    self.receiver_eof.add(role)
                    break
                total+=len(data)
                self.received_totals[role]=total
                self.journal.write({'event':'received','node':role,'bytes':len(data),'wall_ns':time.time_ns()})
        except Exception as exc:self.fail_tcp(exc)

    def upload(self):
        sock=None;sent=0;timeouts=0
        try:
            sock=socket.socket();self.sockets.append(sock)
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,self.iface.encode()+b'\0')
            sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_MAXSEG,1200)
            sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_CONGESTION,b'cubic')
            sock.bind((self.c['own_ip'],0));sock.settimeout(.2)
            sock.connect((self.c['ap_ip'],self.c['port']))
            self.emit('tcp_connected',mss=sock.getsockopt(socket.IPPROTO_TCP,socket.TCP_MAXSEG),
                congestion=sock.getsockopt(socket.IPPROTO_TCP,socket.TCP_CONGESTION,32).decode().strip('\0'))
            payload=b'\x5a'*65536;sent=0;timeouts=0
            while not self.stop.is_set() and time.time_ns()<self.plan['T0_ns']+40*NS:
                try:sent+=sock.send(payload)
                except socket.timeout:timeouts+=1
            self.state['tcp_sender']={'bytes_submitted':sent,'socket_backpressure_timeouts':timeouts,'continuous_application_backlog':True}
        except Exception as exc:self.fail_tcp(exc)
        finally:
            # FIN follows all queued TCP bytes. AP must drain this stream before
            # closing its socket; abrupt AP close used to reset four senders.
            if sock is not None:
                try:sock.shutdown(socket.SHUT_WR)
                except OSError:
                    if not self.tcp_error:self.fail_tcp(RuntimeError('TCP write shutdown failed'))

    def check(self):
        if self.tcp_error:raise RuntimeError(self.tcp_error)
        super().check()

    def read_air(self):
        with (self.out/'air_full.pcap').open('rb') as stream:
            if self.air_format is None:
                h=stream.read(24)
                if len(h)<24:raise RuntimeError('TCP air PCAP header missing')
                formats={b'\xd4\xc3\xb2\xa1':('<',1000),b'\xa1\xb2\xc3\xd4':('>',1000),b'\x4d\x3c\xb2\xa1':('<',1),b'\xa1\xb2\x3c\x4d':('>',1)}
                self.air_format=formats[h[:4]]
                if struct.unpack(self.air_format[0]+'I',h[20:24])[0]!=127:raise RuntimeError('radiotap required')
                self.air_pos=24
            stream.seek(self.air_pos);endian,scale=self.air_format
            while True:
                h=stream.read(16)
                if len(h)<16:break
                sec,frac,n,wire=struct.unpack(endian+'IIII',h);packet=stream.read(n)
                if len(packet)<n:break
                self.air_pos=stream.tell();frame=air_decode(packet,sec*NS+frac*scale,0,wire)
                if not frame or frame['bad_fcs_flag'] or frame['crc_good'] is not True:continue
                tcp=tcp_payload(packet)
                for role,mac in self.c['sta_macs'].items():
                    row=self.air[role]
                    if frame['type']==1 and frame['subtype']==11 and frame['ta']==mac and frame['ra']==self.c['ap_mac']:row['rts']+=1
                    if frame['type']==1 and frame['subtype']==12 and frame['ra']==mac:row['cts']+=1
                    if frame['type']==2 and frame['ta']==mac and frame['ra']==self.c['ap_mac'] and tcp and tcp[0]==self.c['sta_ports'][role]:
                        row['data']+=1;row['retry']+=frame['retry'];key=str(frame['rate_Mbps']);row['rates'][key]=row['rates'].get(key,0)+1

    def run(self):
        threading.Thread(target=self.control_reader,daemon=True).start();self.setup()
        until=time.monotonic()+45
        while not self.plan_ready.wait(.05):
            self.check()
            if time.monotonic()>until:raise RuntimeError('plan timeout')
        t0=self.plan['T0_ns'];self.wait(t0)
        if self.role in self.c['active_senders']:
            self.emit('tcp_launch_call',planned_ns=t0)
            thread=threading.Thread(target=self.upload,daemon=True);thread.start();self.threads.append(thread)
        events=[]
        if self.role.startswith('STA'):
            for k in range(3):
                at=t0+(5+15*k)*NS;events.append((at,0,'boundary',k))
                if k<2:
                    events.append((at+3*NS,1,'stats',k))
                    for ms in (50,150,500,1000,1500,2000):events.append((at+3*NS+ms*10**6,1,'latch',k))
        events += [(t0+n*NS+250_000_000,2,'sample',n) for n in range(1,40)]
        for at,_,kind,k in sorted(events):
            self.wait(at)
            if kind=='boundary':self.boundary(k,at)
            elif kind=='stats':self.stats_start(k,at)
            elif kind=='latch':self.latch_signature()
            else:self.sample(k)
        self.wait(t0+40*NS);self.emit('absolute_end',planned_ns=t0+40*NS)
        self.state['completed_schedule']=True;self.save()

    def cleanup(self):
        if self.role=='AP' and self.state.get('completed_schedule') and not self.tcp_error:
            drain_finished_uploads(self)
        self.stop.set()
        for thread in self.threads:thread.join(1)
        for sock in self.sockets+self.listeners:
            try:sock.close()
            except OSError:pass
        if any(t.is_alive() for t in self.threads):self.state['error']='TCP threads failed to stop'
        if self.journal:
            try:self.journal.close()
            except Exception as exc:self.state['error']='TCP journal: '+str(exc)
            self.state['tcp_received_totals']=self.received_totals
        cpu=resource.getrusage(resource.RUSAGE_SELF)
        self.state['tcp_worker_cpu_seconds']=cpu.ru_utime+cpu.ru_stime-self.cpu_before.ru_utime-self.cpu_before.ru_stime
        super().cleanup()

if __name__=='__main__':
    import sys
    print('ATH9K_LIBRARY9_READY',flush=True);worker=None
    try:
        worker=TCPWorker(json.loads(sys.stdin.readline()));worker.run()
    except Exception as exc:
        if worker:worker.state['error']=str(exc);worker.state['failure_traceback']=traceback.format_exc();worker.emit('failure',message=str(exc))
        else:raise
    finally:
        if worker:worker.cleanup()
