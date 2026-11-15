"""Explicit fixed six-window UDP adapter; old TCP/demo paths unchanged."""
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
from .metrics import aggregate,ACTIVE,saturation

NS=10**9

class Remote(RLRemote):
    def launch(self,config,files):
        client=self.backend.client(self.role)
        with client.open_sftp() as sftp:
            self.directory=config['output'];sftp.mkdir(self.directory.rsplit('/',1)[0],0o700);sftp.mkdir(self.directory,0o700)
            for src in files:sftp.put(str(src),self.directory+'/'+src.name)
        self.channel=client.exec_command('sudo -S -k -p "" env LC_ALL=C python3 -u '+shlex.quote(self.directory+'/node.py'),timeout=48)
        stdin,out,_=self.channel;stdin.write(self.backend.sudo_password+'\n');stdin.flush()
        if out.readline().strip()!='ATH9K_UDP6_READY':raise RuntimeError(self.role+' UDP6 handshake')
        self.send(config);thread=threading.Thread(target=self.read,daemon=True);thread.start();return thread

class UDPSession(QuickSession):
    def __init__(self,folder,prep,request,entry):
        if request.get('authorization')!='udp6-authorized' or entry['state'] not in ACTIVE or entry['W'] not in (8,16):raise ValueError('UDP6 authorization/entry')
        self.entry_spec=entry;self.active=ACTIVE[entry['state']]
        super().__init__(folder,prep,request,{'W':dict.fromkeys(STAS,entry['W'])})
        self.stages={0:dict.fromkeys(STAS,entry['W']),1:dict.fromkeys(STAS,entry['W'])}
        self.state.update(source='udp6_w8_w16_short_screen',entry=entry,active_senders=list(self.active))

    def receive(self,timeout=.15,strict=True):
        role,event=super().receive(timeout,strict)
        if event and event['event']=='eof':self.state.setdefault('process_exit_codes',{})[role]=event['exit_code']
        return role,event

    def heartbeat(self):heartbeat(self)

    def payload(self):
        return [ROOT/'labconsole'/n for n in ('demo_node.py','cw_policy.py','readonly_probe.py')]+[
            ROOT/'experiments'/n for n in ('reg_access.py','cohort_node_capture.py','air_timing.py')]+[
            Path(__file__).with_name('node.py')]

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
            config={**self.request,'role':role,'id':self.id,'udp6_schema':'udp6-w8w16-v1',
                'active_senders':list(self.active),'business_ips':ips,'output':'/tmp/ath9k-demo-'+self.id+'/'+role,
                'interface':rows[role]['interface'],'phy':rows[role]['phy'],'ap_ip':ips['AP'],'ap_mac':mac(rows['AP']['info']),
                'sta_macs':{n:macs[n] for n in self.active} if role=='MON15' else macs,
                'sta_ports':PORTS,'ports':[PORTS[n] for n in self.active],'port':PORTS.get(role),
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
        if set(self.finished)!=set(ROLES) or any(v.get('error') or not v.get('completed_schedule') for v in self.finished.values()):raise RuntimeError('UDP final receipts incomplete/error')
        await_clean_exits(self);self.clock();self.state['applied_W']=self.stages;self.save()

    def acceptance(self):
        import_reference()
        from analyze_sync import tcpdump_stats,pcap_udp
        from labconsole.demo_analysis import analysis_trace
        issues=[];raw=self.folder/'raw';final=self.state['final_nodes'];bounds={};queues={};diagnostics={}
        specs=json.loads((self.folder/'trace_specs.json').read_text(encoding='utf-8'))
        for role in ROLES:
            own=final[role];off,unc=envelope(self.state['clock_checks'],role);events=own['events'];values=[]
            checks=[('iperf_launch_call',0),('iperf_stop_call',40)] if role in self.active else [('absolute_end',40)]
            if role in STAS and role not in self.active:checks.append(('idle_at_T0',0))
            for kind,second in checks:
                found=[e for e in events if e['event']==kind]
                if len(found)!=1:issues.append(role+' missing boundary '+kind);continue
                values.append((abs(found[0]['wall_ns']-off-self.state['T0_ns']-second*NS)+unc)/1e6)
            if role in STAS:
                windows=own['statistics_windows']
                if len(windows)!=2:issues.append(role+' auxiliary driver windows incomplete')
                for index,window in enumerate(windows):
                    for field,second in [('start_wall_ns',8+15*index),('stop_wall_ns',20+15*index)]:
                        values.append((abs(window[field]-off-self.state['T0_ns']-second*NS)+unc)/1e6)
                for index in (0,1):
                    event=next((e for e in events if e['event']=='cw_verified' and e.get('index')==index),None)
                    if event is None:issues.append(role+' CW boundary receipt missing');continue
                    at=event['write_call_ns'] or event['verified_call_ns']
                    values.append((abs(at-off-self.state['T0_ns']-(5+15*index)*NS)+unc)/1e6)
                samples=[self.samples[s][role]['data'] for s in range(6,36) if role in self.samples[s]]
                if len(samples)!=30:issues.append(role+' missing common queue samples')
                if any((v['BE']['hw_aifs'],v['BE']['hw_cwmin'],v['BE']['hw_cwmax'])!=('3',str(self.entry_spec['W']-1),str(8*self.entry_spec['W']-1)) for v in samples):issues.append(role+' CW/AIFS readback')
                queues[role]=saturation(samples)
                if role in self.active and not queues[role]['passed']:issues.append(role+' UDP saturation/backlog gate')
                profile=(raw/role/'kprobe_profile.txt').read_text(encoding='utf-8')
                missed=re.findall(r'(?m)^\s*(?:txarrival|txinput|txdone|mibupdate)\s+\d+\s+(\d+)\s*$',profile)
                loss=re.findall(r'(?m)^\s*(?:overrun|commit overrun|dropped events):\s*(\d+)',(raw/role/'trace_stats.txt').read_text(encoding='utf-8'))
                if len(missed)!=4 or any(map(int,missed)) or not loss or any(map(int,loss)):issues.append(role+' probe miss/trace loss')
                native=lambda text:sum(map(int,re.findall(r'\d+',next(v for v in text.splitlines() if 'FIFO Underrun' in v))))
                if native(own['xmit_after'])!=native(own['xmit_before']):issues.append(role+' FIFO underrun delta')
                if role in self.active:
                    client=json.loads((raw/role/'iperf_client.json').read_text(encoding='utf-8'))
                    server=json.loads((raw/'AP'/('iperf_server_'+str(PORTS[role])+'.json')).read_text(encoding='utf-8'))
                    if (own['processes']['iperf_client']['exit_code']!=1 or client.get('error')!='interrupt - the client has terminated'
                        or server.get('error') not in (None,'the client has terminated') or len(client.get('intervals',[]))<30 or len(server.get('intervals',[]))<30):
                        issues.append(role+' UDP process/iperf JSON completion')
                    if windows:
                        segments,trace_issues=analysis_trace(raw/role/'tx_trace.txt',windows,specs[role]['proof']['valid_bit'],'basic')
                        if trace_issues.get('hardware_error'):issues.append(role+' trace hardware error')
                        for part,window in zip(segments,windows):
                            part['metrics_usable']=window.get('metrics_valid',True)
                            if not part['metrics_usable']:part.update(HoL_ms=None,p_ACK=None,metrics_reason=window.get('metrics_reason'))
                        diagnostics[role]={'auxiliary_segments':segments,'trace_issues':trace_issues,
                            'scope':'driver [T0+8,T0+20) and [T0+23,T0+35); successful complete HoL, not full 30s throughput window',
                            'iperf_server_end':server.get('end'),'iperf_client_end':client.get('end')}
                elif 'iperf_client' in own['processes']:issues.append(role+' unexpected idle sender process')
            if any(v>100 for v in values):issues.append(role+' 100ms boundary envelope')
            bounds[role]=values
        for role in ('AP','MON15'):
            capture=tcpdump_stats((raw/role/'tcpdump.stderr').read_text(encoding='utf-8'))
            empty_complete=(role=='AP' and capture['received_by_filter']==0 and capture['interface_dropped']==0 and capture['captured']==0)
            if capture['kernel_dropped']!=0 or capture['interface_drop_fraction'] is None and not empty_complete or capture['interface_drop_fraction'] is not None and capture['interface_drop_fraction']>.01:issues.append(role+' capture drop')
        for role in self.active:
            air=final['MON15'].get('air_audit',{}).get(role)
            if air is None or air_mode_issue(air,'basic'):issues.append(role+' UDP Basic/DATA24 air gate')
        if final['AP'].get('packet_socket_statistics',[None,None])[1]!=0:issues.append('AP kernel packet sampler dropped packets')
        off,unc=envelope(self.state['clock_checks'],'AP')
        packets=pcap_udp(raw/'AP/ap_udp.pcap',set(PORTS.values()))
        for role in STAS:
            if role not in self.active and any(n==1200 and self.state['T0_ns']+5*NS<=t-off<self.state['T0_ns']+35*NS for t,n in packets[PORTS[role]]):issues.append(role+' idle node UDP traffic')
        metric=aggregate(packets,self.state['T0_ns'],off,unc,self.active,PORTS)
        metric.update(queue=queues,boundary_envelopes_ms=bounds,auxiliary=diagnostics,issues=issues,collection_valid=not issues,
            active_senders=list(self.active),radio='Basic DATA24 AIFSN3 K3 FIFO20',transport='UDP 1200B 30Mbps per active STA')
        self.state['metrics']=metric;self.state['acceptance']={'passed':not issues,'issues':issues,'performance_is_not_gate':True,
            'saturation_is_required':True,'training_started':False};self.save();return issues

