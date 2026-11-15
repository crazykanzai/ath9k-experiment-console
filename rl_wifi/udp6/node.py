"""Linux private worker: unchanged legacy UDP primitives, explicit sender set."""
import json
import signal
import threading
import time
import traceback
from demo_node import Worker

NS=10**9

def upload_command(c,total,out):
    return ['iperf3','-c',c['ap_ip'],'-B',c['own_ip'],'-p',str(c['port']),'-u','-b','30M','-l','1200',
            '-t',str(total+10),'-J','--logfile',str(out/'iperf_client.json')]

class UDPWorker(Worker):
    def __init__(self,c):
        if (c.get('udp6_schema')!='udp6-w8w16-v1' or c['initial_w'] not in (8,16)
            or (c['warmup_s'],c['measure_s'],c['tail_s'],c['segment_s'],c['guard_s'])!=(5,30,5,15,3)
            or c['active_senders'] not in (['STA1'],['STA1','STA2'],['STA1','STA2','STA3','STA4'])):
            raise ValueError('explicit UDP6 schema/timing/window/sender set')
        super().__init__(c)

    def launch(self,name,args):
        if self.role=='AP' and name=='tcpdump':
            args=list(args)
            args[-1]='udp and dst host '+self.c['ap_ip']+' and ('+' or '.join('dst port '+str(p) for p in self.c['sta_ports'].values())+')'
        super().launch(name,args)

    def run(self):
        threading.Thread(target=self.control_reader,daemon=True).start();self.setup()
        deadline=time.monotonic()+45
        while not self.plan_ready.wait(.05):
            self.check()
            if time.monotonic()>deadline:raise RuntimeError('plan timeout')
        t0=self.plan['T0_ns'];self.wait(t0)
        if self.role in self.c['active_senders']:
            receipt=self.emit('iperf_launch_call',planned_ns=t0)
            self.launch('iperf_client',upload_command(self.c,self.total,self.out))
            if abs(receipt['wall_ns']-t0)>100_000_000:raise RuntimeError('100ms UDP start gate')
        elif self.role.startswith('STA'):self.emit('idle_at_T0',planned_ns=t0)
        events=[]
        if self.role.startswith('STA'):
            for k in range(3):
                at=t0+(5+15*k)*NS;events.append((at,0,'boundary',k))
                if k<2:
                    events.append((at+3*NS,1,'stats',k))
                    for ms in (50,150,500,1000,1500,2000):events.append((at+3*NS+ms*10**6,1,'latch',k))
        events += [(t0+n*NS+200_000_000,2,'sample',n) for n in range(1,40)]
        for at,_,kind,k in sorted(events):
            self.wait(at)
            if kind=='boundary':self.boundary(k,at)
            elif kind=='stats':self.stats_start(k,at)
            elif kind=='latch':self.latch_signature()
            else:self.sample(k)
        self.wait(t0+40*NS)
        if self.role in self.c['active_senders']:
            self.emit('iperf_stop_call',planned_ns=t0+40*NS)
            self.processes['iperf_client'][0].send_signal(signal.SIGINT)
        else:self.emit('absolute_end',planned_ns=t0+40*NS)
        self.state['completed_schedule']=True;self.save()

if __name__=='__main__':
    import sys
    print('ATH9K_UDP6_READY',flush=True);worker=None
    try:
        worker=UDPWorker(json.loads(sys.stdin.readline()));worker.run()
    except Exception as exc:
        if worker:worker.state.update(error=str(exc),failure_traceback=traceback.format_exc());worker.emit('failure',message=str(exc))
        else:raise
    finally:
        if worker:worker.cleanup()
