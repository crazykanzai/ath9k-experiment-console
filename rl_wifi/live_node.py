"""Linux node adapter for explicitly authorized short RL acceptance.

Reuses the established demo worker's locks, hardware readbacks, traces, monitor
setup, lease and restoration. Loaded from a private per-run directory only.
"""
import json
from pathlib import Path
import socket
import threading
import time

from demo_node import Worker,cmd
from rtt import ProbeCollector,echo_reply
from probe_transport import collect_sender
from schedules import UNIFORM,BURST,MIXED,MIXED_STATES


SCHEDULES = {'uniform': UNIFORM, 'burst': BURST, 'mixed': MIXED}


class LiveWorker(Worker):
    def __init__(self,config):
        # Keep the established constructor's validated base profile. Actual
        # timings below are separately sealed by this dedicated adapter.
        measure=config['measure_s'];segment=config['segment_s'];warm=config['warmup_s'];tail=config['tail_s']
        if (warm,measure,tail,segment) not in ((5,30,5,10),(15,180,15,15),(5,30,5,30),(5,60,5,15)):
            raise ValueError('sealed RL timing profile')
        if config.get('generation_mode') not in (None, *SCHEDULES):
            raise ValueError('sealed application generation mode')
        if (config.get('generation_mode') == 'mixed') != ((warm,measure,tail,segment) == (5,60,5,15)):
            raise ValueError('mixed arrival schedule/timing mismatch')
        base={**config,'mode':'fixed','warmup_s':5,'measure_s':30,'tail_s':5,'segment_s':10}
        super().__init__(base)
        self.c=config;self.total=warm+measure+tail
        self.probe_socket=None;self.probe_thread=None;self.probe_collector=None
        self.probe_lock=threading.RLock();self.probe_error=None;self.probe_finished=None
        self.clock_anchor=None;self.sender_stop=threading.Event();self.setting_up=True

    def emit(self,event,**data):
        # Do not announce armed until the application endpoint is actually bound.
        if event=='armed' and self.setting_up:return super().emit('hardware_armed',**data)
        return super().emit(event,**data)

    def setup(self):
        super().setup()
        if self.role in ('AP',self.c['foreground']):
            self.probe_socket=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
            self.probe_socket.setsockopt(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,self.iface.encode()+b'\0')
            self.probe_socket.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,2*1024**2)
            self.probe_socket.bind((self.c['ap_ip'] if self.role=='AP' else self.c['own_ip'],self.c['probe_port'] if self.role=='AP' else 0))
            self.probe_socket.setblocking(False)
        if self.role=='AP':
            self.probe_thread=threading.Thread(target=self.echo_loop,daemon=True);self.probe_thread.start()
        if self.role==self.c['foreground'] and self.c.get('generation_mode'):
            schedule=SCHEDULES[self.c['generation_mode']]
            with (self.out/'application_plan.jsonl').open('x') as stream:
                for seq in range(schedule.count(self.total*10**9)):
                    at=schedule.offset_ns(seq)
                    row={'source':'planned','seq':seq,'T0_offset_ns':at,
                         'deadline_T0_offset_ns':at+self.c['deadline_ns']}
                    if self.c['generation_mode']!='mixed':
                        row['batch']=seq//schedule.batch_size
                    stream.write(json.dumps(row)+'\n')
        self.setting_up=False;self.emit('armed',original=self.original,boot=self.state['boot'])

    def echo_loop(self):
        raw=(self.out/'application_echo.jsonl').open('x',buffering=1)
        try:
            while not self.stop.is_set():
                try:packet,peer=self.probe_socket.recvfrom(65535)
                except BlockingIOError:time.sleep(.001);continue
                at=time.time_ns()
                if peer[0]!=self.c['foreground_ip']:
                    raise RuntimeError('unexpected application probe sender')
                reply=echo_reply(packet)
                self.probe_socket.sendto(reply,peer)
                raw.write(json.dumps({'received_wall_ns':at,'peer':peer,'payload_bytes':len(packet),'reply_bytes':len(reply)})+'\n')
        except Exception as exc:
            self.probe_error=str(exc);self.state['application_error']=str(exc);self.stop.set()
        finally:raw.close()

    def sender(self,start_mono):
        class LockedCollector(ProbeCollector):
            pass
        c=LockedCollector(self.c['id'],1,self.c['deadline_ns'])
        # Serialize report snapshots with event mutation; no partial row reads.
        for name in ('generate','submitted','send_failed','receive'):
            method=getattr(c,name)
            def locked(*args,_method=method,**kwargs):
                with self.probe_lock:return _method(*args,**kwargs)
            setattr(c,name,locked)
        self.probe_collector=c;self.clock_anchor=start_mono
        (self.out/'application_clock_anchor.json').write_text(json.dumps({
            'start_monotonic_ns':start_mono,'T0_ns':self.plan['T0_ns'],
            'deadline_ns':self.c['deadline_ns'],'generation_mode':self.c.get('generation_mode','legacy_uniform')}))
        raw=(self.out/'application_probe.jsonl').open('x',buffering=1)
        try:
            def sink(row):raw.write(json.dumps(row)+'\n')
            schedule=SCHEDULES.get(self.c.get('generation_mode'))
            self.probe_finished=collect_sender(self.probe_socket,(self.c['ap_ip'],self.c['probe_port']),c,
                start_mono,self.total*10**9,self.c['probe_interval_ns'],time.monotonic_ns,time.sleep,
                stop_requested=lambda:self.sender_stop.is_set() or self.stop.is_set(),event_sink=sink,schedule=schedule)
            (self.out/'application_final.json').write_text(json.dumps(self.probe_finished,indent=2))
        except Exception as exc:
            self.probe_error=str(exc);self.state['application_error']=str(exc);self.stop.set()
        finally:raw.close()

    def sample(self,number):
        super().sample(number)
        if self.probe_error:raise RuntimeError(self.probe_error)
        if self.role==self.c['foreground'] and self.probe_collector is not None:
            with self.probe_lock:
                start=self.clock_anchor+(number-1)*10**9;end=start+10**9
                report=self.probe_collector.report(start,end,time.monotonic_ns())
            report.pop('ledger')
            self.emit('application_sample',second=number,report=report)

    def run(self):
        threading.Thread(target=self.control_reader,daemon=True).start()
        self.setup();deadline=time.monotonic()+45
        while not self.plan_ready.wait(.05):
            self.check()
            if time.monotonic()>deadline:raise RuntimeError('plan timeout')
        t0=self.plan['T0_ns'];self.wait(t0)
        if self.role==self.c['foreground']:
            start=time.monotonic_ns()+(t0-time.time_ns())
            self.emit('application_launch_call',planned_ns=t0)
            self.probe_thread=threading.Thread(target=self.sender,args=(start,),daemon=True);self.probe_thread.start()
        elif self.role.startswith('STA'):
            event=self.emit('iperf_launch_call',planned_ns=t0)
            self.launch('iperf_client',['iperf3','-c',self.c['ap_ip'],'-B',self.c['own_ip'],'-p',str(self.c['port']),'-u','-b','30M','-l','1200','-t',str(self.total+10),'-J','--logfile',str(self.out/'iperf_client.json')])
            if abs(event['wall_ns']-t0)>100_000_000:raise RuntimeError('100ms background start gate')
        events=[];count=self.c['measure_s']//self.c['segment_s']
        if self.role==self.c['foreground'] and self.c.get('generation_mode')=='mixed':
            for k in range(1,count):
                boundary=t0+(5+k*15)*10**9
                events.append((boundary-6*10**9,0,'notify',k))
        for k in range(count+1):
            at=t0+(self.c['warmup_s']+k*self.c['segment_s'])*10**9
            if self.role.startswith('STA'):
                events.append((at,0,'boundary',k))
                if k<count:
                    start=at+self.c['guard_s']*10**9;events.append((start,1,'start',k))
                    for ms in (50,150,500,1000,1500,2000,2050,2200):events.append((start+ms*1_000_000,1,'latch',k))
        events.extend((t0+n*10**9+250_000_000,2,'sample',n) for n in range(1,self.total))
        for at,_,kind,k in sorted(events):
            self.wait(at)
            if kind=='boundary':self.boundary(k,at)
            elif kind=='start':self.stats_start(k,at)
            elif kind=='latch':self.latch_signature()
            elif kind=='notify':
                self.emit('application_state_notice',index=k,state=MIXED_STATES[k],
                          apply_ns=t0+(5+k*15)*10**9)
            else:self.sample(k)
        self.wait(t0+self.total*10**9)
        if self.role==self.c['foreground']:
            self.emit('application_generation_end_boundary',planned_ns=t0+self.total*10**9)
            self.probe_thread.join(self.c['deadline_ns']/1e9+2)
            if self.probe_thread.is_alive() or self.probe_error or not self.probe_finished or not self.probe_finished['collection_complete']:
                raise RuntimeError('application completion gate: '+str(self.probe_error))
        elif self.role.startswith('STA'):
            self.emit('iperf_stop_call',planned_ns=t0+self.total*10**9)
            self.processes['iperf_client'][0].send_signal(__import__('signal').SIGINT)
        else:self.emit('absolute_end',planned_ns=t0+self.total*10**9)
        self.state['completed_schedule']=True;self.save()

    def cleanup(self):
        self.sender_stop.set()
        if self.role==self.c.get('foreground') and self.probe_thread:
            self.probe_thread.join(3)
        if self.role=='AP':
            self.stop.set()
            if self.probe_thread:self.probe_thread.join(2)
        if self.probe_socket:self.probe_socket.close()
        super().cleanup()


if __name__=='__main__':
    import sys
    print('ATH9K_RL_READY',flush=True)
    config=json.loads(sys.stdin.readline());worker=None
    try:
        worker=LiveWorker(config);worker.run()
    except Exception as exc:
        if worker:worker.state['error']=str(exc);worker.emit('failure',message=str(exc))
        else:raise
    finally:
        if worker:worker.cleanup()
