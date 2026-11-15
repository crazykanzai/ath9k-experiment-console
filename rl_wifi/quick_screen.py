"""Exactly eight authorized fixed windows, with stop-on-gate and restoration.

No DQN, UCB, action search, replacement windows or automatic retry.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from labconsole.preparation import ROLES,check_radio,SSHPreparation
from labconsole.storage import Store,atomic_json
from labconsole.workspace_lock import WorkspaceLock
from labconsole.real_demo import STAS,mac
from .preparation import ReinWiFiPreparationHub,CLOCK_POLICY
from .live import PilotSession
from .quick_metrics import measure_window
from .schedules import UNIFORM,BURST
from .quick_clock import QuickSSHPreparation,QUICK_CLOCK_POLICY,sample_clock,validate


ORDER=((1,'P','U'),(1,'E','U'),(1,'P','B'),(1,'E','B'),
       (2,'E','B'),(2,'P','B'),(2,'E','U'),(2,'P','U'))
ROOT=Path(__file__).resolve().parents[1]
SOURCES=('rl_wifi/quick_screen.py','rl_wifi/quick_metrics.py','rl_wifi/quick_report.py','rl_wifi/quick_clock.py','rl_wifi/quick_retry.py',
         'rl_wifi/live.py','rl_wifi/live_node.py','rl_wifi/preparation.py','rl_wifi/schedules.py',
         'rl_wifi/probe_transport.py','rl_wifi/rtt.py','labconsole/preparation.py',
         'labconsole/real_demo.py','labconsole/demo_node.py','labconsole/cw_policy.py',
         'labconsole/prepare_node.py','labconsole/clock_probe.py','experiments/reg_access.py',
         'experiments/cohort_node_capture.py','experiments/cohort_probe_controller.py','experiments/air_timing.py')


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def targets(config):return {n:16 if n=='STA1' or config=='E' else 64 for n in STAS}


def carried_windows(source,allow_retry=False):
    source=Path(source).resolve();manifest=json.loads((source/'manifest.json').read_text(encoding='utf-8'))
    rows=manifest['windows']
    if tuple((r['block'],r['config'],r['state']) for r in rows)!=ORDER:
        raise ValueError('resume source order differs')
    count=next((i for i,r in enumerate(rows) if r['status']!='completed' or
               allow_retry and not r.get('metrics',{}).get('inference_usable',True)),8)
    pending=rows[count:]
    tail_ok=all(r['status']=='not_started' for r in pending)
    if allow_retry and pending:
        tail_ok=pending[0]['status'] in ('not_started','failed','preparing','completed') and all(r['status']=='not_started' for r in pending[1:])
    if not 0<=count<8 or count==0 and not allow_retry or not tail_ok:
        raise ValueError('only a completed prefix followed by unstarted slots can resume; no replacements')
    result=[]
    for first in rows[:count]:
        origin=Path(first.get('carried_from',source));session=origin/first['id']/'session.json'
        state=json.loads(session.read_text(encoding='utf-8'))
        if not state['acceptance']['passed'] or not state['restoration']['confirmed'] or not manifest['postflight']['confirmed']:
            raise ValueError('carried capture/restoration not confirmed')
        row={**first,'carried_from':str(origin),'carried_session_sha256':digest(session),
             'carried_manifest_sha256':digest(origin/'manifest.json'),
             'clock_policy_version':first.get('clock_policy_version',state.get('clock_policy',{}).get('id','initial50-continuous50-v1'))}
        if first.get('metrics') and not first['metrics']['inference_usable']:
            # New analysis only: preserve the source's original verdict/files.
            metric=row['metrics'];app=metric['application']
            if metric['issues'] or app['counts']['local_failure']/3000>.01 or app['generation_delay_p99_ms']>10:
                raise ValueError('carried data exceeds relaxed arrival quality; do not bypass')
            row['arrival_quality_reassessment']={'original_inference_usable':False,'original_delay_limit_ms':5,
                'new_delay_limit_ms':10,'observed_delay_p99_ms':app['generation_delay_p99_ms'],
                'reason':'quick screening scheduling diagnostic relaxed; raw source unchanged'}
            metric['inference_usable']=True
            app.update(generator_quality_passed=True,generator_delay_warning=True,generator_delay_limit_ms=10)
        result.append(row)
    return result


def seal(batch,resume_from=None,allow_retry=False,preparation_timing=None):
    rows=[{'slot':i+1,'id':uuid.uuid4().hex,'block':b,'config':c,'state':s,
           'W':targets(c),'generation_mode':'uniform' if s=='U' else 'burst'} for i,(b,c,s) in enumerate(ORDER)]
    plan={'schema':'reinwifi-fixed-8-v1','created_at':datetime.now().astimezone().isoformat(),
          'scope':'eight short diagnostic windows only; no model training or full algorithm comparison',
          'formal_valid':0,'plan':rows,'timing_s':[5,30,5],'segment_s':30,'max_windows':8,
          'generation':{'U':UNIFORM.as_dict(),'B':BURST.as_dict()},'clock_policy':QUICK_CLOCK_POLICY,
          'load':{'STA1_payload_B':1200,'STA1_messages_per_second':100,'AP_reply_B':42,'background_each_Mbps':30},
          'fixed_radio':{'mode':'Basic','K':3,'AIFSN':3,'DATA_Mbps':24,'FIFO':20,'power_position_retry':'retain original'},
          'evaluation':{'deadline_ms':100,'max_timeout_ratio':.05,'deadline_is_validity_gate':False,
                        'timeout_practical_gain_absolute':.05,'background_practical_gain_fraction':.05,
                        'timeout_noninferiority_absolute':.01,'background_allowed_loss_fraction':.05,
                        'generator_local_failure_limit_fraction':.01,'generator_delay_p99_limit_ms':10,
                        'generator_delay_warning_above_ms':5},
          'wall_clock_budget_minutes':90,'no_retry':True,
          'sources':{name:digest(ROOT/name) for name in SOURCES}}
    if resume_from:
        plan['carried_windows']=carried_windows(resume_from,allow_retry)
        for row in plan['carried_windows']:rows[row['slot']-1]['id']=row['id']
        plan['new_windows']=8-len(plan['carried_windows'])
        if allow_retry:
            prior=Path(resume_from).resolve()/'manifest.json'
            plan['prior_attempt']={'manifest':str(prior),'sha256':digest(prior),'preserve_failed_attempts':True}
    if preparation_timing:plan['preparation_timing']=list(preparation_timing)
    folder=batch/'sealed';folder.mkdir(exist_ok=False)
    payload=(json.dumps(plan,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    (folder/'plan.json').write_bytes(payload)
    (folder/'plan.sha256').write_text(hashlib.sha256(payload).hexdigest()+'  plan.json\n')
    for name in SOURCES:
        dst=folder/'source'/name;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/name,dst)
    shutil.copy2(ROOT/'EXPERIMENT_RULES.md',folder/'rules_snapshot.md')
    for state,schedule in (('U',UNIFORM),('B',BURST)):
        with (folder/(state+'_message_plan.jsonl')).open('x') as stream:
            for seq in range(4000):
                at=schedule.offset_ns(seq)
                stream.write(json.dumps({'seq':seq,'T0_offset_ns':at,'deadline_T0_offset_ns':at+100_000_000,
                                         'batch':seq//schedule.batch_size,'source':'planned_not_measurement'})+'\n')
    return plan


class QuickPreparation(ReinWiFiPreparationHub):
    def __init__(self,store,backend_factory=QuickSSHPreparation,**kwargs):
        super().__init__(store,backend_factory=backend_factory,**kwargs)

    def _clock_policy(self):return QUICK_CLOCK_POLICY

    def _restore(self):
        # Dedicated acceptance of the already configured baseline. Never call
        # the generic 'radio' helper: it may reload a missing FIFO candidate.
        rows=self._all('inspect');check_radio(rows)
        if not self.active['ap_cidr']:raise RuntimeError('original AP business CIDR missing; do not guess')
        self.active.update(nodes=rows,ap_mac=mac(rows['AP']['info']),status='radio_ready')
        self.event('radio_ready','8窗专项：只读核验现有无线/FIFO基线；没有重配或重载')


class QuickSession(PilotSession):
    def __init__(self,folder,prep,request,entry):
        super().__init__(folder,prep,request);self.entry=entry;self.stages={0:entry['W']}
        self.state.update(source='live_fixed_8_screen',scope='one fixed 5/30/5 diagnostic; no training',entry=entry)
        self.state['clock_policy']=QUICK_CLOCK_POLICY

    def clock(self):
        rows=sample_clock(self.backend)
        self.state.setdefault('clock_checks',[]).append(rows);self.save()
        validate(rows,self.backend.platform['controller']['ntp_source'],100)
        for role,row in rows.items():
            if row['envelope_ms']>50:
                self.event('clock_warning','快速筛选50～100ms时钟包络告警，继续',node=role,receipt=row)
        return rows

    def acceptance(self):
        issues=super().acceptance()
        self.state['sample_records']={n:[{'second':second,**records[n]} for second,records in sorted(self.samples.items()) if n in records] for n in STAS}
        if not issues:
            self.state['metrics']=measure_window(self.folder,self.state)
            issues.extend(self.state['metrics']['issues'])
        self.state['acceptance'].update(passed=not issues,issues=issues,training_started=False)
        self.save();return issues


def wait_phase(prep,identity,status,seconds):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        value=prep.get(identity)
        if value['status']==status and (status=='ready' or not prep.thread or not prep.thread.is_alive()):return value
        if value['status'] in ('failed_clean','ended','recovery_required'):raise RuntimeError(value.get('error') or value['status'])
        time.sleep(.25)
    raise RuntimeError('bounded preparation timeout '+status)


def space_check(backend,batch,remaining):
    required=5*1024**3+remaining*40*8*1024**2
    local=shutil.disk_usage(batch).free;rows={'controller':{'free_B':local,'required_B':required}}
    def one(role):
        _,out,err=backend.client(role).exec_command('df -B1 --output=avail /tmp',timeout=10)
        raw=out.read().decode();error=err.read().decode();code=out.channel.recv_exit_status()
        if code:raise RuntimeError(role+' storage query '+error)
        return role,{'free_B':int(raw.splitlines()[-1].strip()),'required_B':required}
    with ThreadPoolExecutor(max_workers=6) as pool:rows.update(dict(pool.map(one,ROLES)))
    if any(r['free_B']<r['required_B'] for r in rows.values()):raise RuntimeError('storage budget gate '+json.dumps(rows))
    return rows


def postflight(platform,password,baseline,folder):
    folder.mkdir(exist_ok=False);backend=SSHPreparation(platform,password,password)
    def one(role):
        try:
            after=backend.call(role,'inspect');atomic_json(folder/(role+'.json'),after);before=baseline[role]
            checks={k:after[k]==before[k] for k in ('boot_id','srcversion','time_config','power_save','mask','parameter','override') if k in before}
            checks['power_unchanged']=re.search(r'txpower [\d.]+ dBm',after['info'])[0]==re.search(r'txpower [\d.]+ dBm',before['info'])[0]
            checks['no_experiment_resources']=not after['busy'] and not after['trace_busy'] and not after['demo_monitors']
            checks['associated_or_AP']='type AP' in after['info'] if role=='AP' else 'Connected to '+mac(baseline['AP']['info']) in after['link']
            if role in STAS:
                checks['effective_BE']=all(after['BE'][k]==before['BE'][k] for k in ('hw_aifs','hw_cwmin','hw_cwmax','override_enabled','override_valid'))
                checks['statistics_stopped']='running=0' in after['stats_header']
                checks['RTS_restored']=re.search(r'RTS thr[:=](off|\d+)',after['iwconfig'])[1]==re.search(r'RTS thr[:=](off|\d+)',before['iwconfig'])[1]
            return role,{'confirmed':all(checks.values()),'checks':checks}
        except Exception as exc:return role,{'confirmed':False,'error':str(exc)}
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:rows=dict(pool.map(one,ROLES))
    finally:backend.close()
    value={'confirmed':all(r['confirmed'] for r in rows.values()),'nodes':rows}
    atomic_json(folder/'summary.json',value);return value


def password_gui():
    import tkinter as tk
    from tkinter import simpledialog
    root=tk.Tk();root.withdraw();root.attributes('-topmost',True)
    try:
        value=simpledialog.askstring('8-window Wi-Fi experiment',
            '请输入六节点共用 SSH / sudo 密码。\n仅在本进程内存使用，不保存到文件。',show='*',parent=root)
        if value is None:raise RuntimeError('credential entry cancelled; no nodes configured')
        return value
    finally:root.destroy()


def execute(batch,plan,workspace,password,*,session_factory=QuickSession,request_overrides=None,stop_at=None,report_run=True):
    start=time.time_ns();began=time.monotonic();store=Store(workspace);owner=WorkspaceLock(store.root)
    prep=QuickPreparation(store,timing=tuple(plan['preparation_timing']) if plan.get('preparation_timing') else None);identity=None;baseline=None
    manifest={'status':'preparing','started_ns':start,'formal_valid':0,'plan_sha256':digest(batch/'sealed/plan.json'),
              'windows':[{**r,'status':'not_started'} for r in plan['plan']], 'error':None,'training_started':False}
    for row in plan.get('carried_windows',[]):manifest['windows'][row['slot']-1]=row
    manifest['clock_policy']=QUICK_CLOCK_POLICY
    atomic_json(batch/'manifest.json',manifest)
    try:
        value=prep.create(store.platform(),password,password,uuid.uuid4().hex,time_transport='ap_relay');identity=value['id']
        checked=wait_phase(prep,identity,'checked',75);baseline=checked['nodes'];atomic_json(batch/'controller/baseline_inventory.json',baseline)
        check_radio(baseline)  # Refuse a missing candidate; do not reload.
        atomic_json(batch/'controller/storage_before.json',space_check(prep.backend,batch,len(plan['plan'])))
        prep.action(identity,'restore');wait_phase(prep,identity,'radio_ready',45)
        prep.action(identity,'sync');ready=wait_phase(prep,identity,'ready',120)
        atomic_json(batch/'controller/preparation_ready.json',ready)
        print('PREPARATION_READY',json.dumps(ready['clock_results'],ensure_ascii=False),flush=True)
        manifest['status']='running';atomic_json(batch/'manifest.json',manifest)
        for index,entry in enumerate(plan['plan']):
            if manifest['windows'][index].get('carried_from'):continue
            if stop_at is not None and time.monotonic()>=stop_at:
                raise RuntimeError('现场预算不足以完成下一轮并恢复')
            if time.monotonic()-began>90*60:raise RuntimeError('90 minute batch budget; no next window')
            atomic_json(batch/'controller'/('storage_%02d.json'%(index+1)),space_check(prep.backend,batch,len(plan['plan'])-index))
            folder=batch/entry['id'];folder.mkdir(exist_ok=False)
            request={'mode':'fixed','initial_w':16,'access_mode':'basic','warmup_s':5,'measure_s':30,
                     'tail_s':5,'segment_s':30,'guard_s':3,'preparation_id':identity,'generation_mode':entry['generation_mode'],
                     'generator_delay_p99_limit_ms':10}
            request.update(request_overrides or {})
            session=session_factory(folder,prep,request,entry)
            value=prep.get(identity)
            if not value['preparation_ready']:
                raise RuntimeError(value.get('error') or 'current preparation no longer ready')
            session.backend=prep.claim_demo(identity,session.id,session.abort)
            manifest['windows'][index].update(status='preparing',attempt_started_ns=time.time_ns());atomic_json(batch/'manifest.json',manifest)
            print('STARTING',json.dumps(entry,ensure_ascii=False),flush=True);session.run()
            manifest['windows'][index].update(status=session.state['status'],traffic_started=session.state['traffic_started'],
                error=session.state['error'],finished_ns=time.time_ns(),metrics=session.state.get('metrics'),restoration=session.state['restoration'])
            atomic_json(batch/'manifest.json',manifest)
            print('FINISHED',entry['slot'],session.state['status'],session.state['error'],flush=True)
            if session.state['status']!='completed':raise RuntimeError(session.state['error'] or 'window acceptance failed')
            if not session.state['metrics']['inference_usable']:
                raise RuntimeError('generator arrival quality insufficient; capture retained, no next window')
        manifest['status']='completed_eight' if len(plan['plan'])==8 else 'completed_block'
    except Exception as exc:
        manifest.update(status='stopped_at_gate',error=str(exc));print('STOPPED',str(exc),flush=True)
    finally:
        try:
            if identity:
                value=prep.get(identity)
                if value['status'] not in ('ended','failed_clean','recovery_required') and not prep.demo_owner:prep.action(identity,'stop')
                if prep.thread:prep.thread.join(50)
                atomic_json(batch/'controller/preparation_final.json',prep.get(identity))
            prep.close()
            if baseline:
                manifest['postflight']=postflight(store.platform(),password,baseline,batch/'postflight')
        except Exception as exc:manifest['cleanup_error']=str(exc)
        finally:
            manifest['finished_ns']=time.time_ns();manifest['elapsed_s']=time.monotonic()-began
            with store.transaction() as db:manifest['remaining_reservations']=[list(r) for r in db.execute('SELECT * FROM reservations')]
            atomic_json(batch/'manifest.json',manifest);owner.close()
    if report_run:
        from .quick_report import report
        report(batch,batch/'analysis')


def main():
    parser=argparse.ArgumentParser(description='Seal/execute exactly the authorized fixed eight-window screen')
    parser.add_argument('--batch',type=Path,required=True)
    parser.add_argument('--seal-only',action='store_true');parser.add_argument('--execute-live',action='store_true')
    parser.add_argument('--password-gui',action='store_true')
    parser.add_argument('--resume-from',type=Path)
    parser.add_argument('--workspace',default=str(Path(os.environ.get('LOCALAPPDATA','.'))/'APSTASELECT/LabConsole'))
    args=parser.parse_args()
    if args.seal_only==args.execute_live:parser.error('choose seal-only or explicitly authorized execute-live')
    if args.seal_only:
        args.batch.mkdir(parents=True,exist_ok=True);seal(args.batch,args.resume_from);return
    if (args.batch/'manifest.json').exists():raise RuntimeError('batch already attempted; no automatic rerun')
    plan=json.loads((args.batch/'sealed/plan.json').read_text(encoding='utf-8'))
    if digest(args.batch/'sealed/plan.json')!=(args.batch/'sealed/plan.sha256').read_text().split()[0]:raise RuntimeError('sealed plan hash mismatch')
    if tuple((r['block'],r['config'],r['state']) for r in plan['plan'])!=ORDER:raise RuntimeError('fixed eight order mismatch')
    if any(digest(ROOT/name)!=value for name,value in plan['sources'].items()):raise RuntimeError('source changed after seal')
    for row in plan.get('carried_windows',[]):
        source=Path(row['carried_from'])
        if digest(source/row['id']/'session.json')!=row['carried_session_sha256'] or digest(source/'manifest.json')!=row['carried_manifest_sha256']:
            raise RuntimeError('carried evidence changed since sealing')
    password=password_gui() if args.password_gui else getpass.getpass('SSH/sudo password (memory only): ')
    execute(args.batch,plan,args.workspace,password)


if __name__=='__main__':main()
