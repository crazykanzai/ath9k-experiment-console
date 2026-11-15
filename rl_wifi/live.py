"""Explicit opt-in bounded hardware pilot. Existing webpage remains untouched."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import getpass
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import threading
import time
import uuid

from labconsole.preparation import ROLES
from .preparation import ReinWiFiPreparationHub
from labconsole.storage import Store,atomic_json
from labconsole.workspace_lock import WorkspaceLock
from labconsole.real_demo import Session,RemoteWorker,ROOT,STAS,PORTS,mac,NS


class RLRemote(RemoteWorker):
    def launch(self,config,files):
        client=self.backend.client(self.role)
        with client.open_sftp() as sftp:
            self.directory=config['output'];sftp.mkdir(self.directory.rsplit('/',1)[0],0o700);sftp.mkdir(self.directory,0o700)
            for src in files:sftp.put(str(src),self.directory+'/'+src.name)
        self.channel=client.exec_command('sudo -S -k -p "" env LC_ALL=C python3 -u '+shlex.quote(self.directory+'/live_node.py'),timeout=48)
        stdin,out,_=self.channel;stdin.write(self.backend.sudo_password+'\n');stdin.flush()
        if out.readline().strip()!='ATH9K_RL_READY':raise RuntimeError(self.role+' RL handshake failed')
        self.send(config);thread=threading.Thread(target=self.read,daemon=True);thread.start();return thread


class PilotSession(Session):
    def __init__(self,folder,prep,request):
        super().__init__(folder,prep,request)
        self.state.update(source='live_RL_acceptance',formal_valid=0,
            scope='40s mixed-business RTT and asymmetric CW acceptance; no training',application_samples=[])
        self.stages={0:{n:32 for n in STAS},1:{n:16 if n=='STA1' else 64 for n in STAS},
                     2:{n:64 if n=='STA1' else 16 for n in STAS}}

    def receive(self,timeout=.15,strict=True):
        role,event=super().receive(timeout,strict)
        if event and event['event']=='application_sample':
            self.state['application_samples'].append(event);self.save()
        return role,event

    def sample_point(self,second):
        if not all(n in self.samples[second] for n in ('AP',*STAS)):return None
        counts=self.samples[second]['AP']['data']['udp_buckets'].get(str(second-1),{})
        values={n:counts.get(str(PORTS[n]),0)*8/1e6 for n in STAS}
        return {'second':second,'total_Mbps':sum(values.values()),'per_STA_Mbps':values,
                'per_STA_W':{n:int(self.samples[second][n]['data']['BE']['hw_cwmin'])+1 for n in STAS},
                'source':'AP received payload; actual hardware readback'}

    def online_choice(self,index):
        return self.stages[index],'sealed asymmetric acceptance sequence'

    def traffic(self,rows,ips):
        specs=self.trace_specs(rows,ips);root=ROOT/'experiments'
        files=[ROOT/'labconsole'/name for name in ('demo_node.py','cw_policy.py','readonly_probe.py')]
        files += [root/name for name in ('reg_access.py','cohort_node_capture.py','air_timing.py')]
        files += [Path(__file__).with_name(name) for name in ('live_node.py','rtt.py','probe_transport.py','schedules.py')]
        atomic_json(self.folder/'payload_hashes.json',{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
        macs={n:mac(rows[n]['info']) for n in STAS}
        self.state['macs']={'AP':mac(rows['AP']['info']),**macs}
        self.workers={r:RLRemote(r,self.backend,self.messages,self.folder/'raw'/r) for r in ROLES}
        def launch(role):
            config={**self.request,'role':role,'id':self.id,'output':'/tmp/ath9k-demo-'+self.id+'/'+role,
                'initial_w':self.stages[0].get(role,self.request['initial_w']),
                'interface':rows[role]['interface'],'phy':rows[role]['phy'],'ap_ip':ips['AP'],
                'ap_mac':self.state['macs']['AP'],'sta_macs':macs,'sta_ports':PORTS,
                'ports':list(PORTS.values()),'port':PORTS.get(role),'own_ip':ips.get(role),'trace_spec':specs.get(role),
                'foreground':'STA1','foreground_ip':ips['STA1'],'probe_port':PORTS['STA1'],
                'deadline_ns':self.request.get('deadline_ns',100_000_000),'probe_interval_ns':10_000_000}
            return self.workers[role].launch(config,files)
        with ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(launch,ROLES))
        self.last_heartbeat=0;self.state['originals']=self.await_group('armed',ROLES,24);self.heartbeat()
        self.state['clock_pre_arm']=self.clock();t0=time.time_ns()+20*NS
        self.state.update(T0_ns=t0,status='armed',traffic_started=False);self.save()
        self.send_all({'op':'plan','T0_ns':t0,'ap_clock_offset_ns':round(self.state['clock_pre_arm']['AP']['offset_ms']*1e6)})
        self.await_group('plan_received',ROLES,7)
        warm,measure,tail,seg=[self.request[k] for k in ('warmup_s','measure_s','tail_s','segment_s')]
        indices=range(1,measure//seg)
        acknowledgements={k:set() for k in indices};clock_due=time.monotonic()+10
        while len(self.finished)<len(ROLES) and time.time_ns()<t0+(warm+measure+tail+45)*NS:
            self.check_cancel();self.heartbeat();role,event=self.receive()
            now=time.time_ns()
            if now>=t0:self.state['traffic_started']=True
            if event and event['event']=='stage_received':
                k=event['index']
                if k not in self.sent or role not in STAS or event['W']!=self.stages[k][role] or event['apply_ns']!=t0+(warm+k*seg)*NS:
                    raise RuntimeError('RL stage acknowledgement disagrees with sealed plan')
                acknowledgements[k].add(role)
            for k in indices:
                at=t0+(warm+k*seg)*NS
                if k not in self.sent and at-now<=4*NS:
                    settings,reason=self.online_choice(k);self.stages[k]=settings
                    self.sent.add(k);dispatch_ns=time.time_ns();self.send_stage(k,self.stages[k],at)
                    self.event('RL_stage_plan',reason,index=k,settings=settings,apply_ns=at,dispatch_ns=dispatch_ns)
                if k in self.sent and at-now<NS and acknowledgements[k]!=set(STAS):
                    raise RuntimeError('RL stage acknowledgements incomplete')
            if event and event['event']=='sample':
                second=event['second']
                if not any(p['second']==second for p in self.state['points']):
                    point=self.sample_point(second)
                    if point:self.state['points'].append(point);self.save()
            if time.monotonic()>=clock_due:
                self.clock();clock_due=time.monotonic()+10
        if set(self.finished)!=set(ROLES):raise RuntimeError('RL final receipts incomplete')
        if any(v.get('error') or not v.get('completed_schedule') for v in self.finished.values()):raise RuntimeError('RL node schedule failed')
        self.state['clock_after']=self.clock();self.state['applied_W']=self.stages;self.save()

    def acceptance(self):
        issues=[]
        from labconsole.real_demo import import_reference
        import_reference()
        from analyze_sync import tcpdump_stats
        captures={}
        for role in ('AP','MON15'):
            file=self.folder/'raw'/role/'tcpdump.stderr'
            if not file.exists():issues.append(role+' capture stderr missing');continue
            captures[role]=tcpdump_stats(file.read_text());c=captures[role]
            if c['kernel_dropped']!=0 or c['interface_drop_fraction'] is None or c['interface_drop_fraction']>.01:
                issues.append(role+' capture drop gate')
        self.state['capture_audit']=captures
        path=self.folder/'raw'/'STA1'/'application_final.json'
        if not path.is_file():issues.append('application archive missing')
        else:
            final=json.loads(path.read_text(encoding='utf-8'))
            if not final['collection_complete']:issues.append('application collection incomplete')
            self.state['application_final_summary']={k:v for k,v in final['report'].items() if k!='ledger'}
        # Throughput/timeout values never act as pass criteria. Queue backlog,
        # capture completeness and parameter receipts do.
        for role in STAS:
            states=self.finished.get(role,{})
            trace=self.folder/'raw'/role/'kprobe_profile.txt'
            if not trace.is_file():issues.append(role+' trace profile missing')
            else:
                misses={k:int(v) for k,v in re.findall(r'(?m)^\s*(txarrival|txinput|txdone|mibupdate)\s+\d+\s+(\d+)\s*$',trace.read_text())}
                if len(misses)!=4 or any(misses.values()):issues.append(role+' missing/nonzero probe misses')
            if role!='STA1':
                warm=self.request['warmup_s'];measure=self.request['measure_s']
                samples=[r[role]['data'] for second,r in self.samples.items() if warm<second<=warm+measure and role in r]
                nonempty=sum(v['qdepth']>0 for v in samples)/len(samples) if samples else 0
                backlog=sum(v['qdepth']>=2 or v['pending']>=3 for v in samples)/len(samples) if samples else 0
                if nonempty<.95 or backlog<.9:
                    issues.append(role+' background queue nonempty <95%')
            stats=self.folder/'raw'/role/'trace_stats.txt'
            if stats.exists():
                losses=re.findall(r'(?m)^\s*(?:overrun|commit overrun|dropped events):\s*(\d+)',stats.read_text())
                if not losses or any(int(v) for v in losses):
                    issues.append(role+' trace overrun/drop')
        self.state['acceptance']={'passed':not issues,'issues':issues,'performance_is_not_gate':True,
                                 'formal_valid':0,'training_started':False}
        self.save();return issues

    def run(self):
        failed=False;restored=False
        try:
            rows,ips=self.preflight();self.traffic(rows,ips)
        except Exception as exc:
            failed=True;self.state['error']=str(exc);self.event('gate_stop',str(exc))
        try:
            restored,errors=self.cleanup(failed)
            if errors or not restored:raise RuntimeError('restoration/archive: '+str(errors))
            if not failed:
                issues=self.acceptance()
                if issues:raise RuntimeError('; '.join(issues))
        except Exception as exc:
            failed=True;self.state['error']=self.state.get('error') or str(exc)
        finally:
            self.state.update(status='failed' if failed else 'completed');self.save()
            self.prep.release_demo(self.id,restored)


def main():
    parser=argparse.ArgumentParser(description='Explicit live RTT/asymmetric-CW short acceptance, no training')
    parser.add_argument('--execute-live',action='store_true');parser.add_argument('--output',required=True)
    parser.add_argument('--workspace',default=str(Path(os.environ.get('LOCALAPPDATA','.'))/'APSTASELECT/LabConsole'))
    args=parser.parse_args()
    if not args.execute_live:parser.error('requires explicit --execute-live and current user authorization')
    output=Path(args.output);output.mkdir(parents=True,exist_ok=False)
    store=Store(args.workspace);owner=WorkspaceLock(store.root);prep=ReinWiFiPreparationHub(store)
    password=getpass.getpass('SSH/sudo password (memory only): ');identity=None
    try:
        session=prep.create(store.platform(),password,password,uuid.uuid4().hex,time_transport='ap_relay');identity=session['id']
        def wait(status,limit):
            until=time.monotonic()+limit
            while time.monotonic()<until:
                value=prep.get(identity)
                if value['status']==status:return value
                if value['status'] in ('failed_clean','recovery_required','ended'):
                    raise RuntimeError(value.get('error') or value.get('cleanup_error') or value['status'])
                time.sleep(.5)
            raise RuntimeError('preparation phase timeout '+status)
        wait('checked',75);prep.action(identity,'restore');wait('radio_ready',90)
        prep.action(identity,'sync');wait('ready',120)
        folder=output/uuid.uuid4().hex;folder.mkdir()
        request={'mode':'fixed','initial_w':32,'access_mode':'basic','warmup_s':5,'measure_s':30,
                 'tail_s':5,'segment_s':10,'guard_s':3,'preparation_id':identity}
        pilot=PilotSession(folder,prep,request)
        pilot.backend=prep.claim_demo(identity,pilot.id,pilot.abort)
        print('PILOT',str(folder),flush=True);pilot.run()
        atomic_json(output/'result.json',pilot.state)
        print(json.dumps({'status':pilot.state['status'],'error':pilot.state['error'],
                         'acceptance':pilot.state.get('acceptance'),'restoration':pilot.state['restoration']},ensure_ascii=False),flush=True)
    finally:
        if identity:
            value=prep.get(identity)
            if value['status'] not in ('ended','failed_clean','recovery_required') and not prep.demo_owner:
                prep.action(identity,'stop')
            if prep.thread:prep.thread.join(50)
            atomic_json(output/'preparation_final.json',prep.get(identity))
        prep.close();owner.close()


if __name__=='__main__':main()
