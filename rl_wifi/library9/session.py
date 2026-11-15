"""Owned 40-second TCP screen, no learning or alteration of old executors."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import shlex
import threading
import time
import traceback

from labconsole.real_demo import STAS,PORTS,mac,import_reference,Session
from labconsole.preparation import ROLES
from labconsole.storage import atomic_json
from labconsole.demo_analysis import envelope
from labconsole.cw_policy import air_mode_issue
from rl_wifi.live import RLRemote
from rl_wifi.quick_screen import QuickSession,ROOT
from rl_wifi.bounded40.transport import heartbeat,await_clean_exits
from .metrics import aggregate,ACTIVE

NS=10**9

class Remote(RLRemote):
    def launch(self,config,files):
        client=self.backend.client(self.role)
        with client.open_sftp() as sftp:
            self.directory=config['output'];sftp.mkdir(self.directory.rsplit('/',1)[0],0o700);sftp.mkdir(self.directory,0o700)
            for src in files:sftp.put(str(src),self.directory+'/'+src.name)
        self.channel=client.exec_command('sudo -S -k -p "" env LC_ALL=C python3 -u '+shlex.quote(self.directory+'/node.py'),timeout=48)
        stdin,out,_=self.channel;stdin.write(self.backend.sudo_password+'\n');stdin.flush()
        if out.readline().strip()!='ATH9K_LIBRARY9_READY':raise RuntimeError(self.role+' TCP9 handshake')
        self.send(config);thread=threading.Thread(target=self.read,daemon=True);thread.start();return thread

class TCPSession(QuickSession):
    def __init__(self,folder,prep,request,entry):
        if request.get('authorization')!='library9-authorized' or entry['state'] not in ACTIVE or entry['W'] not in (16,32,64):raise ValueError('TCP9 authorization/entry')
        self.entry_spec=entry;self.active=ACTIVE[entry['state']]
        super().__init__(folder,prep,request,{'W':dict.fromkeys(STAS,entry['W'])})
        self.stages={0:dict.fromkeys(STAS,entry['W']),1:dict.fromkeys(STAS,entry['W'])}
        self.state.update(source='library_tcp9_short_screen',entry=entry,active_senders=list(self.active))

    def receive(self,timeout=.15,strict=True):
        role,event=super().receive(timeout,strict)
        if event and event['event']=='eof':self.state.setdefault('process_exit_codes',{})[role]=event['exit_code']
        return role,event

    def heartbeat(self):heartbeat(self)

    def payload(self):
        return [ROOT/'labconsole'/n for n in ('demo_node.py','cw_policy.py','readonly_probe.py')]+[
            ROOT/'experiments'/n for n in ('reg_access.py','cohort_node_capture.py','air_timing.py')]+[
            Path(__file__).with_name('node.py'),Path(__file__).with_name('air.py'),ROOT/'rl_wifi/bounded40/event_journal.py']

    def traffic(self,rows,ips):
        try:self._traffic(rows,ips)
        except Exception:
            self.state['failure_traceback']=traceback.format_exc();self.save();raise

    def _traffic(self,rows,ips):
        specs=self.trace_specs(rows,ips);files=self.payload()
        atomic_json(self.folder/'payload_hashes.json',{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
        macs={n:mac(rows[n]['info']) for n in STAS}
        self.workers={r:Remote(r,self.backend,self.messages,self.folder/'raw'/r) for r in ROLES}
        def launch(role):
            config={**self.request,'role':role,'id':self.id,'library_schema':'library-tcp9-v1',
                'active_senders':list(self.active),'business_ips':ips,'output':'/tmp/ath9k-demo-'+self.id+'/'+role,
                'interface':rows[role]['interface'],'phy':rows[role]['phy'],'ap_ip':ips['AP'],'ap_mac':mac(rows['AP']['info']),
                'sta_macs':{n:macs[n] for n in self.active} if role=='MON15' else macs,
                'sta_ports':PORTS,'ports':list(PORTS.values()),'port':PORTS.get(role),
                'own_ip':ips.get(role),'trace_spec':specs.get(role)}
            return self.workers[role].launch(config,files)
        with ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(launch,ROLES))
        self.last_heartbeat=0;self.state['originals']=self.await_group('armed',ROLES,24)
        self.state['clock_pre_arm']=self.clock();t0=time.time_ns()+20*NS
        self.state.update(T0_ns=t0,status='armed',traffic_started=True);self.save()
        self.send_all({'op':'plan','T0_ns':t0,'ap_clock_offset_ns':round(self.state['clock_pre_arm']['AP']['offset_ms']*1e6)})
        self.await_group('plan_received',ROLES,7)
        self.send_stage(1,self.stages[1],t0+20*NS)
        self.await_group('stage_received',STAS,7)
        due=time.monotonic()+10
        while len(self.finished)<6 and time.time_ns()<t0+85*NS:
            self.check_cancel();self.heartbeat();self.receive()
            if time.monotonic()+180>=self.request['field_stop_monotonic']:raise RuntimeError('recovery budget reached')
            if time.monotonic()>=due:self.clock();due=time.monotonic()+10
        if set(self.finished)!=set(ROLES) or any(v.get('error') or not v.get('completed_schedule') for v in self.finished.values()):raise RuntimeError('TCP final receipts incomplete/error')
        await_clean_exits(self);self.clock();self.state['applied_W']=self.stages;self.save()

    def acceptance(self):
        import_reference();from analyze_sync import tcpdump_stats
        issues=[];raw=self.folder/'raw';final=self.state['final_nodes'];boundaries={};queue={}
        for role in ROLES:
            own=final[role];off,unc=envelope(self.state['clock_checks'],role)
            events=own['events'];bounds=[]
            checks=[('absolute_end',40)]
            if role in self.active:checks.append(('tcp_launch_call',0))
            for kind,second in checks:
                found=[e for e in events if e['event']==kind]
                if len(found)!=1:issues.append(role+' missing boundary '+kind);continue
                bounds.append((abs(found[0]['wall_ns']-off-self.state['T0_ns']-second*NS)+unc)/1e6)
            if role in STAS:
                for index,w in enumerate(own['statistics_windows']):
                    for field,second in [('start_wall_ns',8+15*index),('stop_wall_ns',20+15*index)]:
                        bounds.append((abs(w[field]-off-self.state['T0_ns']-second*NS)+unc)/1e6)
                if len(own['statistics_windows'])!=2:issues.append(role+' driver windows incomplete')
                for k in (0,1):
                    e=next((e for e in events if e['event']=='cw_verified' and e.get('index')==k),None)
                    if not e:issues.append(role+' CW boundary missing');continue
                    at=e['write_call_ns'] or e['verified_call_ns'];bounds.append((abs(at-off-self.state['T0_ns']-(5+15*k)*NS)+unc)/1e6)
                samples=[self.samples[s][role]['data'] for s in range(6,36) if role in self.samples[s]]
                if len(samples)!=30:issues.append(role+' common samples incomplete')
                if any((v['BE']['hw_aifs'],v['BE']['hw_cwmin'],v['BE']['hw_cwmax'])!=('3',str(self.entry_spec['W']-1),str(8*self.entry_spec['W']-1)) for v in samples):issues.append(role+' CW readback')
                queue[role]={'samples':len(samples),'nonempty_fraction':sum(v['qdepth']>0 for v in samples)/30,'backlog_fraction':sum(v['qdepth']>=2 or v['pending']>=3 for v in samples)/30,
                    'gate':'TCP closed loop: queue evidence retained, no continuous hardware backlog requirement'}
                profile=(raw/role/'kprobe_profile.txt').read_text();misses=re.findall(r'(?m)^\s*(?:txarrival|txinput|txdone|mibupdate)\s+\d+\s+(\d+)\s*$',profile)
                losses=re.findall(r'(?m)^\s*(?:overrun|commit overrun|dropped events):\s*(\d+)',(raw/role/'trace_stats.txt').read_text())
                if len(misses)!=4 or any(map(int,misses)) or not losses or any(map(int,losses)):issues.append(role+' trace loss/miss')
                native=lambda text:sum(map(int,re.findall(r'\d+',next(v for v in text.splitlines() if 'FIFO Underrun' in v))))
                if native(own['xmit_after'])!=native(own['xmit_before']):issues.append(role+' FIFO delta')
                if role in self.active and not own.get('tcp_sender',{}).get('continuous_application_backlog'):issues.append(role+' application backlog receipt')
            if any(v>100 for v in bounds):issues.append(role+' 100ms boundary envelope')
            boundaries[role]=bounds
        for role in ('AP','MON15'):
            c=tcpdump_stats((raw/role/'tcpdump.stderr').read_text())
            if c['kernel_dropped']!=0 or c['interface_drop_fraction'] is None or c['interface_drop_fraction']>.01:issues.append(role+' capture drop')
        for role in self.active:
            air=final['MON15'].get('air_audit',{}).get(role,{})
            issue=air_mode_issue(air,'basic')
            if issue:issues.append(role+' air '+issue)
        events=[json.loads(x) for x in (raw/'AP/tcp_receive.jsonl').read_text().splitlines()]
        totals={n:sum(e['bytes'] for e in events if e['event']=='received' and e['node']==n) for n in self.active}
        if totals!=final['AP'].get('tcp_received_totals'):issues.append('TCP receiver journal/counter mismatch')
        if set(final['AP'].get('tcp_eof_nodes',[]))!=set(self.active):issues.append('TCP FIN drain receipts incomplete')
        if any(totals[n]!=final[n].get('tcp_sender',{}).get('bytes_submitted') for n in self.active):issues.append('TCP submitted/delivered final stream bytes mismatch')
        if sorted(e['node'] for e in events if e['event']=='accepted')!=sorted(self.active):issues.append('TCP accepted flow set mismatch')
        off,unc=envelope(self.state['clock_checks'],'AP')
        metric=aggregate(events,self.state['T0_ns'],off,unc,self.active)
        if any(v==0 for v in metric['per_STA_Mbps'].values()):issues.append('active uploader no common data')
        metric.update(queue=queue,boundary_envelopes_ms=boundaries,issues=issues,collection_valid=not issues,
            per_node_cpu_seconds={n:final[n]['tcp_worker_cpu_seconds'] for n in ROLES})
        self.state['metrics']=metric;self.state['acceptance']={'passed':not issues,'issues':issues,'performance_is_not_gate':True};self.save()
        return issues
