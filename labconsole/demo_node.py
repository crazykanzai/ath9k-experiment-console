"""Linux SSH-owned diagnostic worker. Absolute local scheduling, bounded lease.

Imports are uploaded into this fresh private run folder. No driver installation,
global service stop, direct MIB read or deletion of existing experiment files.
"""
import collections
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

from reg_access import read_registers
from cohort_node_capture import Capture
from readonly_probe import parse_hardware_cw, dcf
from air_timing import decode as air_decode
from cw_policy import ACCESS_SETTINGS, LONG_DEMO_MODES, WINDOWS, STAS, air_mode_issue

SHARED='/run/lock/apstaselect-ath9k-regidx.lock'
CONTROL='/run/lock/ath9k-console-cw-pilot.lock'
VERSION='0AB85B473A0B1B032BFECCD'
SIGNATURE_GRACE_NS=2_000_000_000


def cmd(args,timeout=8,optional=False):
    p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if p.returncode and not optional:raise RuntimeError(' '.join(args[:4])+': '+(p.stderr or p.stdout)[:200])
    return p.stdout


def fields(text):return dict(t.split('=',1) for t in text.split() if '=' in t)

def wait_managed_ready(iface,timeout=30):
    deadline=time.monotonic()+timeout
    while True:
        state=cmd(['nmcli','-g','GENERAL.STATE','device','show',iface],optional=True)
        code=re.match(r'\s*(\d+)',state)
        if code and 30<=int(code[1])<=100:return
        if time.monotonic()>=deadline:raise RuntimeError('MON15 device readiness timeout: '+state.strip())
        time.sleep(.1)


def signature(text, be, access_mode='basic'):
    lines = text.splitlines()
    head = fields(lines[0])
    if head.get('running')!='1' or head.get('hol_abi')!='1' or head.get('basic_retry_abi')!='2':
        raise RuntimeError('statistics ABI/start gate')
    config = next(fields(row) for row in lines if row.startswith('AC=BE '))
    if config.get('ready')=='0':
        return None
    actual = tuple(config.get(k) for k in ('mode','cwmin','cwmax','aifsn'))
    expected = ('rts-cts' if access_mode=='rts' else 'basic',be['hw_cwmin'],be['hw_cwmax'],be['hw_aifs'])
    if actual!=expected:
        raise RuntimeError(f'statistics access/CW signature gate: {actual} != {expected}')
    return config


def udp_frame(frame):
    if len(frame)<42 or frame[12:14]!=b'\x08\x00' or frame[23]!=17:return None
    ip=14;ihl=(frame[ip]&15)*4
    if ihl<20 or len(frame)<ip+ihl+8 or int.from_bytes(frame[ip+6:ip+8],'big')&0x3fff:return None
    port,length=struct.unpack_from('!HH',frame,ip+ihl+2)
    return port,length-8,socket.inet_ntoa(frame[ip+16:ip+20])


class Worker:
    def __init__(self,c):
        self.c=c;self.role=c['role'];self.iface=c['interface'];self.phy=c['phy'];self.out=Path(c['output'])
        self.base=Path('/sys/kernel/debug/ieee80211')/self.phy/'ath9k'
        self.stop=threading.Event();self.plan_ready=threading.Event();self.rx_lock=threading.Lock();self.output_lock=threading.RLock()
        self.last_heartbeat=time.monotonic();self.plan=None;self.staged={};self.window=None
        self.processes={};self.capture=None;self.monitor=None;self.original=None;self.control=None;self.health=None
        self.udp_sock=None;self.udp_thread=None;self.udp_counts=collections.defaultdict(collections.Counter);self.udp_lock=threading.Lock()
        self.air_pos=0;self.air_format=None;self.air={n:{'data':0,'retry':0,'rates':{},'rts':0,'cts':0} for n in c.get('sta_macs',{})}
        self.current_w=None
        self.access_mode=c.get('access_mode','basic')
        if self.access_mode not in ACCESS_SETTINGS:raise ValueError('access mode profile')
        self.active_senders=c.get('active_senders',list(STAS))
        if (not isinstance(self.active_senders,list) or not self.active_senders
                or len(set(self.active_senders))!=len(self.active_senders)
                or any(n not in STAS for n in self.active_senders)):
            raise ValueError('active sender set')
        self.state={'role':self.role,'source':'live_hardware_diagnostic','id':c['id'],'events':[],'access_mode':self.access_mode,
                    'error':None,'original':None,'restoration':None,'statistics_windows':[],'processes':{},'armed':False,'finished':False}
        self.state['statistics_policy']={'signature_grace_ms':SIGNATURE_GRACE_NS//1_000_000,
                                         'pending_metrics':'NA','signature_fault':'segment_NA_continue_demo',
                                         'absolute_boundaries_unchanged':True}
        if not re.fullmatch('[a-zA-Z0-9_]+',self.iface) or not re.fullmatch('phy[0-9]+',self.phy):raise ValueError('interface/PHY')
        if self.out.name!=self.role or self.out.parent.name!='ath9k-demo-'+c['id'] or not re.fullmatch('[a-f0-9]{32}',c['id']):raise ValueError('owned directory identity')
        profile=tuple(c[k] for k in ('warmup_s','measure_s','tail_s','segment_s'))
        if c.get('mode') in LONG_DEMO_MODES:
            timing_ok=profile==(5,600,5,20) if c['mode']=='continuous' else profile[0]==5 and profile[1] in (60,300,600,1200) and profile[2:]==(5,20)
        else:
            timing_ok=profile[:3] in ((5,30,5),(5,60,5),(15,180,15)) and profile[3] in (10,15) and profile[1]%profile[3]==0
        if c['initial_w'] not in WINDOWS or not timing_ok or c['guard_s']!=3:raise ValueError('timing/CW profile')
        self.total=sum(c[k] for k in ('warmup_s','measure_s','tail_s'))
        self.raw=(self.out/'worker_events.jsonl').open('x',buffering=1)

    def emit(self,event,**data):
        value={'event':event,'role':self.role,'wall_ns':time.time_ns(),'monotonic_ns':time.monotonic_ns(),**data}
        with self.output_lock:
            self.raw.write(json.dumps(value)+'\n');print(json.dumps(value),flush=True)
            if event!='sample':self.state['events'].append(value)
        return value

    def save(self):
        with self.output_lock:
            p=self.out/'worker_state.json';tmp=p.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.state,indent=2));os.replace(tmp,p)

    def control_reader(self):
        try:
            for raw in sys.stdin:
                m=json.loads(raw);op=m['op']
                if op=='heartbeat':self.last_heartbeat=time.monotonic();continue
                if op=='abort':raise RuntimeError('controller_abort: '+str(m.get('reason','')))
                if op=='plan':
                    if self.plan or not 10_000_000_000<=m['T0_ns']-time.time_ns()<=25_000_000_000:raise RuntimeError('future T0/duplicate plan gate')
                    self.plan=m;self.state['plan']=m;self.plan_ready.set();self.emit('plan_received',T0_ns=m['T0_ns']);self.save()
                elif op=='stage':
                    k=m['index'];expected=self.plan['T0_ns']+(self.c['warmup_s']+k*self.c['segment_s'])*10**9
                    if type(k)!=int or not 1<=k<self.c['measure_s']//self.c['segment_s'] or m['W'] not in WINDOWS or m['apply_ns']!=expected or expected-time.time_ns()<2_000_000_000:
                        raise RuntimeError('CW command boundary/deadline gate')
                    with self.rx_lock:
                        if k in self.staged:raise RuntimeError('duplicate CW segment command')
                        self.staged[k]=m
                    self.emit('stage_received',index=k,W=m['W'],apply_ns=expected)
                else:raise RuntimeError('unknown control message')
            raise RuntimeError('controller_EOF')
        except Exception as exc:
            self.state['control_error']=str(exc);self.stop.set()

    def check(self):
        if self.stop.is_set():raise RuntimeError(self.state.get('control_error','controller stopped'))
        if time.monotonic()-self.last_heartbeat>8:raise RuntimeError('controller lease expired')
        if self.capture and self.capture.reader.poll() is not None:raise RuntimeError('trace reader exited')

    def wait(self,target):
        while True:
            self.check();remaining=(target-time.time_ns())/1e9
            if remaining<=0:return
            self.stop.wait(min(.02,remaining))

    def shared(self,operation):
        fd=os.open(SHARED,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:
            deadline=time.monotonic()+.5
            while True:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>deadline:raise RuntimeError('shared register lock busy')
                    time.sleep(.005)
            return operation()
        finally:os.close(fd)

    def be(self):
        def read():
            text=(self.base/'edca_ctrl').read_text();row=next(fields(s) for s in text.splitlines() if s.startswith('AC=BE '))
            if row.get('status_name')!='verified':raise RuntimeError('hardware BE readback')
            return row
        return self.shared(read)

    def access_settings(self):
        rts=re.search(r'RTS thr[:=](off|\d+)',cmd(['iwconfig',self.iface]))
        override=(self.base/'basic_access_override').read_text().strip()
        if not rts or override not in ('0','1'):raise RuntimeError('RTS/Basic override readback unavailable')
        return (rts[1],override)

    def verify_access(self):
        settings = self.access_settings()
        if settings!=ACCESS_SETTINGS[self.access_mode]:
            raise RuntimeError(f'access mode configuration mismatch: {settings} != {ACCESS_SETTINGS[self.access_mode]}')
        return {'rts':settings[0],'override':settings[1]}

    def set_access(self, rts, override):
        cmd(['iw','phy',self.phy,'set','rts',rts])
        (self.base/'basic_access_override').write_text(override+'\n')

    def set_cw(self,w,index=None,planned_ns=None):
        unchanged=(self.c.get('mode') in LONG_DEMO_MODES or self.c.get('generation_mode')=='mixed') and w==self.current_w
        if not unchanged:self.emit('cw_requested',W=w,index=index,planned_ns=planned_ns)
        def write():
            at=time.time_ns()
            if planned_ns is not None and abs(at-planned_ns)>100_000_000:raise RuntimeError('100ms CW boundary gate')
            (self.base/'edca_ctrl').write_text(f'BE 3 {w-1} {8*w-1}\n')
            return at,time.time_ns()
        verified_call_ns=time.time_ns()
        if unchanged and planned_ns is not None and abs(verified_call_ns-planned_ns)>100_000_000:
            raise RuntimeError('100ms CW boundary gate')
        write_ns,write_return_ns=(None,None) if unchanged else self.shared(write)
        after=self.be()
        if [after[k] for k in ('hw_aifs','hw_cwmin','hw_cwmax')]!=['3',str(w-1),str(8*w-1)]:raise RuntimeError('CW hardware mismatch')
        self.current_w=w
        self.emit('cw_verified',W=w,index=index,planned_ns=planned_ns,readback=after,
                  write_call_ns=write_ns,write_return_ns=write_return_ns,verified_call_ns=verified_call_ns)

    def launch(self,key,args):
        err=(self.out/(key+'.stderr')).open('x')
        p=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=err)
        self.processes[key]=(p,err);self.state['processes'][key]={'pid':p.pid,'command':args}
        return p

    def setup_monitor(self):
        self.original={'info':cmd(['iw','dev',self.iface,'info']),'link':cmd(['iw','dev',self.iface,'link']),
            'managed':cmd(['nmcli','-g','GENERAL.NM-MANAGED','device','show',self.iface]).strip(),
            'uuid':cmd(['nmcli','-g','GENERAL.CON-UUID','device','show',self.iface]).strip(),
            'power_save':cmd(['iw','dev',self.iface,'get','power_save']),
            'boot':Path('/proc/sys/kernel/random/boot_id').read_text()}
        if self.original['managed']!='yes' or 'Connected to '+self.c['ap_mac'] not in self.original['link']:raise RuntimeError('original MON15 association/management gate')
        self.state['original']=self.original;self.save()
        self.monitor='acm'+self.c['id'][:8]
        if self.monitor in re.findall(r'Interface (\S+)',cmd(['iw','dev'])):raise RuntimeError('monitor identity already exists')
        profiles=[]
        for p in Path('/etc/NetworkManager/system-connections').glob('*'):
            if p.is_file() and ('uuid='+self.original['uuid']) in p.read_text(errors='replace'):
                profiles.append({'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
        if len(profiles)!=1:raise RuntimeError('MON15 connection profile identity')
        self.original['profiles']=profiles;self.original['unmanaged_requested']=True;self.save()
        cmd(['nmcli','device','set',self.iface,'managed','no']);cmd(['ip','link','set',self.iface,'down'])
        cmd(['iw','phy',self.phy,'interface','add',self.monitor,'type','monitor','flags','control','otherbss'])
        self.original['monitor_created']=True;self.save()
        cmd(['ip','link','set',self.monitor,'up'])  # Existing verified order: UP before setting frequency.
        links={r['ifname']:r for r in json.loads(cmd(['ip','-j','link']))}
        if 'UP' in links[self.iface]['flags'] or 'UP' not in links[self.monitor]['flags']:raise RuntimeError('passive interface UP gate')
        for iface in re.findall(r'Interface (\S+)',cmd(['iw','dev'])):
            if iface!=self.monitor and 'UP' in links[iface]['flags']:raise RuntimeError('other running wireless interface')
        cmd(['iw','dev',self.monitor,'set','freq','2412','NOHT'])
        info=cmd(['iw','dev',self.monitor,'info'])
        if 'type monitor' not in info or 'channel 1 (2412 MHz)' not in info:raise RuntimeError('passive monitor/channel readback')
        self.state['monitor_info']=info
        self.launch('tcpdump',['tcpdump','-i',self.monitor,'-s','0','-B','16384','-U','-w',str(self.out/'air_full.pcap')])

    def setup(self):
        self.control=os.fdopen(os.open(CONTROL,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600),'r+b')
        fcntl.flock(self.control,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if cmd(['pgrep','-x','iperf3'],optional=True) or cmd(['pgrep','-x','tcpdump'],optional=True):raise RuntimeError('existing traffic/capture; not stopped')
        usage=os.statvfs(self.out)
        if usage.f_bavail*usage.f_frsize<5*1024**3+self.total*8*1024**2:raise RuntimeError('remote storage reserve gate')
        self.state['boot']=Path('/proc/sys/kernel/random/boot_id').read_text()
        self.state['wireless_before']=cmd(['iw','dev',self.iface,'info'])
        self.state['survey_before']=cmd(['iw','dev',self.iface,'survey','dump'])
        if self.role.startswith('STA'):
            if Path('/sys/module/ath9k/srcversion').read_text().strip()!=VERSION or Path('/sys/module/ath9k/parameters/diag_tx_fifo_trigger').read_text().strip()!='20':raise RuntimeError('driver/FIFO parameter gate')
            if 'running=0' not in (self.base/'dcf_stats').read_text().splitlines()[0]:raise RuntimeError('statistics already owned')
            rts,override=self.access_settings()
            self.original={'BE':self.be(),'override':override,'rts':rts}
            self.state['original']=self.original;self.save()
            self.state['xmit_before']=(self.base/'xmit').read_text()
            self.set_access(*ACCESS_SETTINGS[self.access_mode])
            self.emit('access_verified',access_mode=self.access_mode,settings=self.verify_access())
            self.set_cw(self.c['initial_w'])
            self.capture=Capture(self.c['trace_spec'],self.out).start()
        elif self.role=='MON15':self.setup_monitor()
        elif self.role=='AP':
            if 'type AP' not in self.state['wireless_before']:raise RuntimeError('AP role gate')
            for port in self.c['ports']:
                if ':'+str(port) in cmd(['ss','-H','-ltn','sport','=',':'+str(port)]):raise RuntimeError('AP port occupied')
                self.launch('iperf_server_'+str(port),['iperf3','-s','-1','-B',self.c['ap_ip'],'-p',str(port),'-J','--logfile',str(self.out/('iperf_server_'+str(port)+'.json'))])
            # Capture all registered ports, including idle leakage evidence.
            filt='udp and dst host '+self.c['ap_ip']+' and ('+' or '.join('dst port '+str(p) for p in self.c['sta_ports'].values())+')'
            self.launch('tcpdump',['tcpdump','-i',self.iface,'-s','128','-B','16384','-U','-w',str(self.out/'ap_udp.pcap'),filt])
            self.udp_sock=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3));self.udp_sock.bind((self.iface,0))
            self.udp_sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4*1024**2)
            self.udp_sock.setsockopt(socket.SOL_SOCKET,35,1);self.udp_sock.settimeout(.2)
            self.udp_thread=threading.Thread(target=self.receive_udp,daemon=True);self.udp_thread.start()
        else:raise ValueError('role')
        if self.role in ('AP','MON15'):
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                if self.processes['tcpdump'][0].poll() is not None:raise RuntimeError('capture exited before armed')
                if 'listening on' in (self.out/'tcpdump.stderr').read_text(errors='replace'):break
                time.sleep(.05)
            else:raise RuntimeError('capture ready timeout')
        if self.role=='AP':
            for port in self.c['ports']:
                if ':'+str(port) not in cmd(['ss','-H','-ltn','sport','=',':'+str(port)]):raise RuntimeError('AP server not ready')
        self.state['armed']=True;self.save();self.emit('armed',original=self.original,boot=self.state['boot'])

    def receive_udp(self):
        try:
            while not self.stop.is_set():
                try:frame,anc,flags,address=self.udp_sock.recvmsg(2048,128)
                except socket.timeout:continue
                if address[2]==4:continue
                value=udp_frame(frame)
                if not value or value[0] not in self.c['ports'] or value[1]!=1200 or value[2]!=self.c['ap_ip']:continue
                stamp=next((struct.unpack('ll',v) for level,typ,v in anc if level==socket.SOL_SOCKET and typ==35),None)
                if stamp is None:raise RuntimeError('AP kernel packet timestamp unavailable')
                ns=stamp[0]*10**9+stamp[1]
                if self.plan:
                    bucket=(ns-self.plan['T0_ns']-self.plan.get('ap_clock_offset_ns',0))//10**9
                    if 0<=bucket<self.total:
                        with self.udp_lock:self.udp_counts[bucket][value[0]]+=1200
        except Exception as exc:self.state['udp_error']=str(exc);self.stop.set()

    def finish_stats(self, planned_ns=None):
        if self.window is None:return
        event=self.emit('driver_stop_call',index=self.window['index'])
        (self.base/'dcf_stats').write_text('stop\n')
        self.emit('driver_stop_return',index=self.window['index'])
        text=(self.base/'dcf_stats').read_text()
        (self.out/('dcf_segment_%02d.txt'%self.window['index'])).write_text(text)
        if fields(text.splitlines()[0]).get('running')!='0':raise RuntimeError('driver statistics stop unconfirmed')
        if any(int(fields(row).get('config_mismatches','0')) for row in text.splitlines() if row.startswith(('version=','AC=BE '))):
            self.statistics_unavailable('DCF signature mismatch at window stop')
        self.window.setdefault('metrics_valid',True)
        self.window.update(stop_wall_ns=event['wall_ns'],stop_mono_ns=event['monotonic_ns'])
        self.state['statistics_windows'].append(self.window);self.window=None;self.save()
        if planned_ns is not None and abs(event['wall_ns']-planned_ns)>100_000_000:raise RuntimeError('100ms statistics stop gate')

    def boundary(self,index,target):
        self.finish_stats(target)
        if index==self.c['measure_s']//self.c['segment_s']:return
        if index==0:w=self.c['initial_w']
        else:
            with self.rx_lock:m=self.staged.pop(index,None)
            if m is None:raise RuntimeError('missing pre-staged CW command')
            w=m['W']
        self.set_cw(w,index,target)

    def stats_start(self,index,target):
        event=self.emit('driver_start_call',index=index,planned_ns=target)
        self.window={'index':index,'start_wall_ns':event['wall_ns'],'start_mono_ns':event['monotonic_ns']}
        (self.base/'dcf_stats').write_text('start\n')
        self.emit('driver_start_return',index=index,planned_ns=target)
        be=self.be()
        if abs(event['wall_ns']-target)>100_000_000:raise RuntimeError('100ms statistics boundary gate')
        # Grace is nonblocking: neither absolute boundaries nor 1s samples move.
        # Pending DCF metrics are NA until a matching signature is observed.
        self.window.update(expected_BE=be,signature_deadline_mono_ns=event['monotonic_ns']+SIGNATURE_GRACE_NS)
        if not self.latch_signature():
            self.emit('driver_signature_wait',index=index,grace_ms=SIGNATURE_GRACE_NS//1_000_000)
        self.save()

    def statistics_unavailable(self,reason):
        if self.window is None or self.window.get('metrics_valid') is False:return
        self.window.update(metrics_valid=False,metrics_reason=reason)
        warning={'index':self.window['index'],'reason':reason}
        self.state.setdefault('statistics_warnings',[]).append(warning)
        self.emit('statistics_warning',index=self.window['index'],message=reason,
                  action='DCF/HoL/ACK NA; throughput and verified CW continue')
        self.save()

    def latch_signature(self):
        if self.window is None:return False
        if self.window.get('metrics_valid') is False:return False
        if self.window.get('signature') is not None:return True
        self.check()
        header=(self.base/'dcf_stats').read_text()
        try:conf=signature(header,self.window['expected_BE'],self.access_mode)
        except RuntimeError as exc:
            if not str(exc).startswith('statistics access/CW signature gate:'):raise
            self.statistics_unavailable(str(exc));return False
        now=time.monotonic_ns()
        if now>=self.window['signature_deadline_mono_ns']:
            (self.out/('dcf_segment_%02d_unlatched.txt'%self.window['index'])).write_text(header)
            self.statistics_unavailable('no BE signature latched within 2000ms of start');return False
        if conf is None:
            return False
        (self.out/('dcf_segment_%02d_start.txt'%self.window['index'])).write_text(header)
        self.window.update(signature=conf,signature_latched_mono_ns=now)
        self.emit('driver_signature_ready',index=self.window['index'],
                  latch_observed_ms=(now-self.window['start_mono_ns'])/1e6)
        self.save();return True

    def sample(self,number):
        data={}
        if self.role.startswith('STA'):
            link=cmd(['iw','dev',self.iface,'link'])
            if 'Connected to '+self.c['ap_mac'] not in link:raise RuntimeError('sending STA association gate')
            be=self.be();q=(self.base/'queues').read_text();row=next((s for s in q.splitlines() if s.startswith('(BE):')),'')
            if (be['hw_aifs'],be['hw_cwmin'],be['hw_cwmax'])!=('3',str(self.current_w-1),str(8*self.current_w-1)):
                raise RuntimeError('CW changed outside the scheduled segment')
            vals={k:int(v) for k,v in re.findall(r'(qdepth|pending):\s*(\d+)',row)}
            if set(vals)!={'qdepth','pending'}:raise RuntimeError('BE queue evidence parser')
            # Stopped debugfs counters retain the last window, including its
            # mismatch flags. Warmup/guard/tail never own that old generation.
            # Keep radio/queue gates active but expose no DCF metrics there.
            signature_ready=self.latch_signature() if self.window is not None else False
            counts=None
            if signature_ready:
                try:counts=dcf(self.base,self.c['ap_mac'])
                except ValueError as exc:
                    if not str(exc).startswith('DCF configuration signature changed;'):raise
                    self.statistics_unavailable(str(exc));signature_ready=False
            data={'link':link,'queue':q,'qdepth':vals['qdepth'],'pending':vals['pending'],'BE':be,
                  'dcf':counts,
                  'dcf_scope':'owned_window' if signature_ready else 'statistics_unavailable_NA' if self.window and self.window.get('metrics_valid') is False else 'signature_pending_NA' if self.window is not None else 'outside_window_NA'}
            if number%3==0:
                data['access_settings']=self.verify_access()
                reading=read_registers(self.base,[0x30]);data['FIFO']=reading
                if (int(reading['values']['0x30'],16)&0x3f0)>>4!=20:raise RuntimeError('FIFO hardware threshold gate')
                data['native_xmit']=(self.base/'xmit').read_text()
                def fifo(text):
                    line=next((s for s in text.splitlines() if 'FIFO Underrun' in s),None)
                    if line is None:raise RuntimeError('native FIFO counter unavailable')
                    return sum(map(int,re.findall(r'\d+',line)))
                if fifo(data['native_xmit'])>fifo(self.state['xmit_before']):raise RuntimeError('FIFO underrun observed')
            if self.window and data['dcf'] and (data['dcf']['generation']<0 or not data['dcf']['running']):raise RuntimeError('unexpected DCF reset/stop')
        elif self.role=='AP':
            station=cmd(['iw','dev',self.iface,'station','dump'])
            associated={chunk.split()[0] for chunk in re.split(r'(?m)^Station ',station)[1:]
                        if re.search(r'(?m)^\s*associated:\s+yes\s*$',chunk)}
            if not set(self.c['sta_macs'].values())<=associated:raise RuntimeError('AP active station association gate')
            with self.udp_lock:data['udp_buckets']={str(number-1):dict(self.udp_counts.get(number-1,{}))}
        elif self.role=='MON15':
            info=cmd(['iw','dev',self.monitor,'info'])
            if 'type monitor' not in info or '(2412 MHz)' not in info:raise RuntimeError('monitor/channel changed')
            self.read_air();data['air_audit']=self.air
            if number>=self.c['warmup_s']:
                for n,a in self.air.items():
                    if n not in self.c.get('active_senders',STAS):
                        continue
                    issue=air_mode_issue(a,self.access_mode)
                    if issue:raise RuntimeError(n+' actual air mode gate: '+issue)
        if self.plan and number<self.total-1:
            for name,(process,_) in self.processes.items():
                if process.poll() is not None:raise RuntimeError(name+' early process exit '+str(process.returncode))
        self.emit('sample',second=number,data=data)

    def read_air(self):
        with (self.out/'air_full.pcap').open('rb') as f:
            if self.air_format is None:
                h=f.read(24)
                if len(h)!=24:raise RuntimeError('monitor PCAP header missing')
                formats={b'\xd4\xc3\xb2\xa1':('<',1000),b'\xa1\xb2\xc3\xd4':('>',1000),b'\x4d\x3c\xb2\xa1':('<',1),b'\xa1\xb2\x3c\x4d':('>',1)}
                if h[:4] not in formats:raise RuntimeError('monitor PCAP format')
                self.air_format=formats[h[:4]]
                if struct.unpack(self.air_format[0]+'I',h[20:24])[0]!=127:raise RuntimeError('monitor radiotap required')
                self.air_pos=24
            f.seek(self.air_pos);endian,scale=self.air_format
            while True:
                pos=f.tell();h=f.read(16)
                if len(h)<16:break
                sec,frac,n,wire=struct.unpack(endian+'IIII',h);packet=f.read(n)
                if len(packet)<n:break
                self.air_pos=f.tell()
                frame=air_decode(packet,sec*10**9+frac*scale,0,wire)
                if not frame or frame['bad_fcs_flag'] or frame['crc_good'] is not True:continue
                for name,mac in self.c['sta_macs'].items():
                    row=self.air[name]
                    if frame['type']==1 and frame['subtype']==11 and frame['ta']==mac and frame['ra']==self.c['ap_mac']:row['rts']+=1
                    if frame['type']==1 and frame['subtype']==12 and frame['ra']==mac:row['cts']+=1
                    if frame['type']==2 and frame['ta']==mac and frame['ra']==self.c['ap_mac'] and frame['UDP_port']==self.c['sta_ports'][name] and frame['UDP_payload_bytes']==1200:
                        row['data']+=1;row['retry']+=frame['retry'];key=str(frame['rate_Mbps'])
                        row['rates'][key]=row['rates'].get(key,0)+1

    def run(self):
        threading.Thread(target=self.control_reader,daemon=True).start()
        self.setup();deadline=time.monotonic()+45
        while not self.plan_ready.wait(.05):
            self.check()
            if time.monotonic()>deadline:raise RuntimeError('plan timeout')
        t0=self.plan['T0_ns'];self.wait(t0)
        if self.role in self.active_senders:
            event=self.emit('iperf_launch_call',planned_ns=t0)
            self.launch('iperf_client',['iperf3','-c',self.c['ap_ip'],'-B',self.c['own_ip'],'-p',str(self.c['port']),'-u','-b','30M','-l','1200','-t',str(self.total+10),'-J','--logfile',str(self.out/'iperf_client.json')])
            if abs(event['wall_ns']-t0)>100_000_000:raise RuntimeError('100ms client start gate')
        elif self.role.startswith('STA'):
            self.emit('idle_at_T0',planned_ns=t0)
        count=self.c['measure_s']//self.c['segment_s'];events=[]
        for k in range(count+1):
            at=t0+(self.c['warmup_s']+k*self.c['segment_s'])*10**9
            if self.role.startswith('STA'):
                events.append((at,0,'boundary',k))
                if k<count:
                    start=at+self.c['guard_s']*10**9
                    events.append((start,1,'start',k))
                    for ms in (50,150,500,1000,1500,2000,2050,2200):
                        events.append((start+ms*1_000_000,1,'latch',k))
        # Sample after each exact bucket, away from the CW/stats boundary calls.
        events.extend((t0+n*10**9+200_000_000,2,'sample',n) for n in range(1,self.total))
        for at,_,kind,k in sorted(events):
            self.wait(at)
            if kind=='boundary':self.boundary(k,at)
            elif kind=='start':self.stats_start(k,at)
            elif kind=='latch':self.latch_signature()
            else:self.sample(k)
        end=t0+self.total*10**9;self.wait(end)
        if self.role in self.active_senders:
            self.emit('iperf_stop_call',planned_ns=end,reason='scheduled_local_absolute_SIGINT')
            self.processes['iperf_client'][0].send_signal(signal.SIGINT)
        else:self.emit('absolute_end',planned_ns=end)
        self.state['completed_schedule']=True

    def cleanup(self):
        errors=[];self.stop.set()
        try:self.finish_stats()
        except Exception as exc:errors.append('statistics stop: '+str(exc))
        for name,(p,handle) in self.processes.items():
            try:
                if name.startswith('iperf_server_') and self.state.get('completed_schedule'):
                    try:p.wait(3)
                    except subprocess.TimeoutExpired:pass
                if p.poll() is None:p.send_signal(signal.SIGINT)
                try:p.wait(5)
                except subprocess.TimeoutExpired:p.terminate();p.wait(3)
                self.state['processes'][name]['exit_code']=p.returncode;handle.close()
            except Exception as exc:errors.append(name+': '+str(exc))
        if self.udp_thread:self.udp_thread.join(2)
        if self.udp_sock:
            try:self.state['packet_socket_statistics']=list(struct.unpack('II',self.udp_sock.getsockopt(263,6,8)))
            except Exception as exc:errors.append('owned packet socket statistics: '+str(exc))
            self.udp_sock.close()
            with self.udp_lock:self.state['live_udp_buckets']=dict(self.udp_counts)
            if self.state.get('udp_error'):errors.append('AP live UDP sampler: '+self.state['udp_error'])
        if self.role=='MON15' and (self.out/'air_full.pcap').exists():
            try:self.read_air();self.state['air_audit']=self.air
            except Exception as exc:self.state['air_error']=str(exc)
        if self.capture:
            try:self.capture.close()
            except Exception as exc:errors.append('trace: '+str(exc))
        if self.original and self.role.startswith('STA'):
            try:
                before=self.original['BE'];text=('BE %s %s %s\n'%(before['hw_aifs'],before['hw_cwmin'],before['hw_cwmax'])) if before['override_enabled']=='1' else 'BE reset\n'
                self.shared(lambda:(self.base/'edca_ctrl').write_text(text))
                self.set_access(self.original['rts'],self.original['override'])
                after=self.be();keys=('hw_aifs','hw_cwmin','hw_cwmax','override_enabled','override_valid')
                if any(after[k]!=before[k] for k in keys):raise RuntimeError('original BE mismatch')
                if self.access_settings()!=(self.original['rts'],self.original['override']):raise RuntimeError('original RTS/Basic override mismatch')
                self.state['BE_restored']=after
                self.state['xmit_after']=(self.base/'xmit').read_text()
            except Exception as exc:errors.append('STA restoration: '+str(exc))
        if self.original and self.role=='MON15':
            try:
                if self.original.get('monitor_created'):
                    cmd(['ip','link','set',self.monitor,'down']);cmd(['iw','dev',self.monitor,'del'])
                if self.original.get('unmanaged_requested'):
                    cmd(['ip','link','set',self.iface,'up'])
                    cmd(['nmcli','device','set',self.iface,'managed','yes'])
                    wait_managed_ready(self.iface)
                    cmd(['nmcli','connection','up','uuid',self.original['uuid'],'ifname',self.iface],timeout=30)
                link=cmd(['iw','dev',self.iface,'link'])
                if 'Connected to '+self.c['ap_mac'] not in link:raise RuntimeError('original MON15 association not restored')
                for p in self.original.get('profiles',[]):
                    if hashlib.sha256(Path(p['path']).read_bytes()).hexdigest()!=p['sha256']:raise RuntimeError('original connection profile changed')
                self.state['MON15_restored_link']=link
            except Exception as exc:errors.append('MON15 restoration: '+str(exc))
        try:
            self.state['wireless_after']=cmd(['iw','dev',self.iface,'info'])
            self.state['survey_after']=cmd(['iw','dev',self.iface,'survey','dump'])
            if re.search(r'txpower [\d.]+ dBm',self.state['wireless_after'])[0]!=re.search(r'txpower [\d.]+ dBm',self.state['wireless_before'])[0]:raise RuntimeError('power changed')
            if Path('/proc/sys/kernel/random/boot_id').read_text()!=self.state['boot']:raise RuntimeError('boot changed')
        except Exception as exc:errors.append('postflight: '+str(exc))
        self.state.update(restoration={'confirmed':not errors,'errors':errors},finished=True)
        self.save();self.emit('finished',state=self.state)
        if self.control:self.control.close()
        self.raw.close()


def main():
    print('ATH9K_DEMO_READY',flush=True)
    config=json.loads(sys.stdin.readline());worker=Worker(config)
    try:worker.run()
    except Exception as exc:
        worker.state['error']=str(exc);worker.emit('failure',message=str(exc))
    finally:worker.cleanup()


if __name__=='__main__':main()
