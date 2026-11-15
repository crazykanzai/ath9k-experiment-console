"""Bounded, explicitly authorized hardware training after a passing pilot.

Uses live.py's independently owned sessions; no changes to existing webpage.
Not a reproduction of ReinWiFi's neural architecture or joint rate control.
"""
import argparse
from dataclasses import asdict
import getpass
import hashlib
import json
import os
from pathlib import Path
import random
import statistics
import time
import uuid

from .preparation import ReinWiFiPreparationHub
from labconsole.storage import Store,atomic_json
from labconsole.workspace_lock import WorkspaceLock
from labconsole.real_demo import STAS
from .live import PilotSession
from .policies import DQNPolicy,FixedPolicy,UCBPolicy
from .spec import ACTIONS,RewardConfig,features,reward


def settings(index):
    a=ACTIONS[index]
    return {n:a.foreground_W if n=='STA1' else a.background_W for n in STAS}


class LearningSession(PilotSession):
    def __init__(self,folder,prep,request,policy,config,training=False,sweep=False):
        super().__init__(folder,prep,request)
        self.policy=policy;self.config=config;self.training=training;self.sweep=sweep
        first=policy.action if isinstance(policy,FixedPolicy) else 4
        self.pending=None;self.index_actions={0:first};self.processed=set();self.transitions=[];self.scores=[]
        self.stages={0:settings(first)}
        self.state.update(source='live_RL_limited_training' if training else 'live_RL_frozen_comparison',
            scope='independent 15/180/15 dynamic-control window; not old fixed-CW paper data',
            learning_scores=[],learning_transitions=[],learning_config=asdict(config),
            policy_name='DQN_train' if training else type(policy).__name__)

    def score(self,index):
        if index in self.processed:return next(s for s in self.scores if s['index']==index)
        warm,seg=self.request['warmup_s'],self.request['segment_s']
        begin=warm+index*seg+3;end=warm+(index+1)*seg-5
        seconds=list(range(begin+1,end+1))
        points={p['second']:p for p in self.state['points']}
        probes={p['second']:p['report'] for p in self.state['application_samples']}
        if not all(s in points and s in probes for s in seconds):raise RuntimeError('learning observation missing; no fake reward')
        if not all(probes[s]['complete_cohort'] and probes[s]['counts']['mature']>0 for s in seconds):
            raise RuntimeError('immature/empty deadline cohort')
        action=self.index_actions[index];expected=settings(action)
        if not all(points[s]['per_STA_W']==expected for s in seconds):raise RuntimeError('learning CW association mismatch')
        mature=sum(probes[s]['counts']['mature'] for s in seconds)
        ontime=sum(probes[s]['counts']['on_time'] for s in seconds)
        replies=sum(probes[s]['rtt_reply_samples'] for s in seconds)
        observation={'valid':True,'foreground_Mbps':statistics.mean(points[s]['per_STA_Mbps']['STA1'] for s in seconds),
            'background_Mbps':statistics.mean(sum(v for n,v in points[s]['per_STA_Mbps'].items() if n!='STA1') for s in seconds),
            'rtt_mean_ms':sum(probes[s]['rtt_mean_ms']*probes[s]['rtt_reply_samples'] for s in seconds if probes[s]['rtt_reply_samples'])/replies if replies else None,
            'rtt_reply_samples':replies,'timeout_ratio':1-ontime/mature,'deadline_ms':100.,
            'foreground_W':ACTIONS[action].foreground_W,'background_W':ACTIONS[action].background_W,
            'foreground_active':1,'background_active':1}
        features(observation,self.config)
        previous=self.index_actions.get(index-1,action)
        value=reward(observation,action!=previous,self.config)
        row={'index':index,'action_index':action,'score_seconds':seconds,'observation':observation,
             'mature_messages':mature,'on_time_messages':ontime,'reward':value}
        self.scores.append(row);self.processed.add(index);self.state['learning_scores']=self.scores;self.save();return row

    def update_transition(self,row,terminal=False):
        state=row['observation']
        if isinstance(self.policy,UCBPolicy):self.policy.observe(state,row['action_index'],row['reward'],state,terminal)
        if self.pending is not None:
            previous,action=self.pending
            if action!=row['action_index']:raise RuntimeError('transition/action alignment failure')
            transition={'state':previous,'action_index':action,'reward':row['reward'],'next_state':state,
                        'terminal':terminal,'source':'live_hardware','run_id':self.id,'segment':row['index']}
            self.transitions.append(transition);self.state['learning_transitions']=self.transitions
            if self.training:self.policy.observe(previous,action,row['reward'],state,terminal)
        self.pending=None;self.save()

    def online_choice(self,index):
        row=self.score(index-1);self.update_transition(row)
        if self.sweep and index<=9:action=index-1;reason='sealed nine-action development coverage'
        else:action=self.policy.choose(row['observation'],training=self.training);reason='Q policy' if isinstance(self.policy,DQNPolicy) else type(self.policy).__name__
        self.pending=(dict(row['observation']),action);self.index_actions[index]=action
        self.state.setdefault('decisions',[]).append({'index':index,'action_index':action,'reason':reason})
        return settings(action),reason

    def acceptance(self):
        issues=super().acceptance()
        if not issues:
            last=self.request['measure_s']//self.request['segment_s']-1
            row=self.score(last);self.update_transition(row,terminal=True)
        self.state['acceptance']['training_started']=self.training;self.save()
        return issues


def main():
    parser=argparse.ArgumentParser(description='After-pilot bounded hardware training/comparison')
    parser.add_argument('--execute-live',action='store_true');parser.add_argument('--pilot',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--workspace',default=str(Path(os.environ.get('LOCALAPPDATA','.'))/'APSTASELECT/LabConsole'))
    args=parser.parse_args()
    if not args.execute_live:parser.error('explicit --execute-live and current user authorization required')
    pilot=json.loads(Path(args.pilot).read_text(encoding='utf-8'))
    if pilot['status']!='completed' or not pilot['acceptance']['passed'] or not pilot['restoration']['confirmed']:
        raise RuntimeError('passing pilot/restoration required')
    import torch
    torch.set_num_threads(1)
    output=Path(args.output);output.mkdir(parents=True,exist_ok=False)
    config=RewardConfig(background_reference_Mbps=12,deadline_ms=100,timeout_weight=2,switch_weight=.02)
    model=DQNPolicy(seed=7,config=config,batch_size=8,target_every=10)
    plan=[]
    for n in range(5):plan.append({'id':uuid.uuid4().hex,'phase':'training','repeat':n+1,'policy':'DQN'})
    rng=random.Random(20261114)
    for block in range(5):
        order=['fixed','ucb','dqn'];rng.shuffle(order)
        for policy in order:plan.append({'id':uuid.uuid4().hex,'phase':'test','repeat':block+1,'policy':policy})
    manifest={'status':'preparing','source':'live_hardware_RL_limited','formal_valid':0,
        'pilot':str(Path(args.pilot).resolve()),'pilot_sha256':hashlib.sha256(Path(args.pilot).read_bytes()).hexdigest(),
        'plan':plan,'training_windows':5,'test_windows':15,'max_segments':60,'expected_training_transitions':55,
        'timing_s':[15,180,15],'segment_s':15,'score':'segment [start+3,end-5), deadline cohort fully matured',
        'training_budget_minutes':17.5,'test_business_minutes':52.5,'reward':asdict(config),
        'load':'STA1 UDP1200B every10ms; STA2-4 each30Mbps; AP app reply42B',
        'frozen_hyperparameters':{'seed':7,'hidden':[32,32],'gamma':.9,'batch_size':8,'target_every':10,'learning_rate':.001},
        'completed':[],'error':None,'code_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')}}
    atomic_json(output/'manifest.json',manifest)
    store=Store(args.workspace);owner=WorkspaceLock(store.root);prep=ReinWiFiPreparationHub(store);identity=None
    password=getpass.getpass('SSH/sudo password (memory only): ')
    frozen=None;fixed=None;development=[]
    try:
        value=prep.create(store.platform(),password,password,uuid.uuid4().hex,time_transport='ap_relay');identity=value['id']
        def wait(status,seconds):
            deadline=time.monotonic()+seconds
            while time.monotonic()<deadline:
                v=prep.get(identity)
                if v['status']==status:return
                if v['status'] in ('failed_clean','recovery_required','ended'):raise RuntimeError(v.get('error') or v['status'])
                time.sleep(.5)
            raise RuntimeError('bounded preparation timeout '+status)
        wait('checked',75);prep.action(identity,'restore');wait('radio_ready',90)
        prep.action(identity,'sync');wait('ready',120)
        manifest['status']='running';atomic_json(output/'manifest.json',manifest)
        for entry in plan:
            if entry['phase']=='test' and frozen is None:
                model.save(output/'live_dqn.json',{'source':'live_hardware_RL_limited','training_runs':[r['id'] for r in plan if r['phase']=='training'],'observed_transitions':model.steps})
                frozen=DQNPolicy.load(output/'live_dqn.json',config)
                by_action={i:[r['reward'] for r in development if r['action_index']==i] for i in range(9)}
                if any(not v for v in by_action.values()):raise RuntimeError('development action coverage incomplete; no invented fixed baseline')
                fixed=max(by_action,key=lambda a:statistics.mean(by_action[a]))
                atomic_json(output/'development.json',{'fixed_action':fixed,'per_action_rewards':by_action,'model_updates':model.updates})
            policy=model if entry['phase']=='training' else FixedPolicy(fixed) if entry['policy']=='fixed' else UCBPolicy() if entry['policy']=='ucb' else frozen
            folder=output/entry['id'];folder.mkdir()
            request={'mode':'fixed','initial_w':32,'access_mode':'basic','warmup_s':15,'measure_s':180,
                'tail_s':15,'segment_s':15,'guard_s':3,'preparation_id':identity}
            session=LearningSession(folder,prep,request,policy,config,entry['phase']=='training',entry['phase']=='training' and entry['repeat']==1)
            session.backend=prep.claim_demo(identity,session.id,session.abort)
            print(json.dumps({'starting':entry,'model_steps':model.steps},ensure_ascii=False),flush=True)
            session.run()
            manifest['completed'].append({'id':entry['id'],'phase':entry['phase'],'policy':entry['policy'],'status':session.state['status'],'error':session.state['error']})
            atomic_json(output/'manifest.json',manifest)
            print(json.dumps({'finished':entry['id'],'status':session.state['status'],'error':session.state['error'],'model_steps':model.steps},ensure_ascii=False),flush=True)
            if session.state['status']!='completed':raise RuntimeError(session.state['error'] or 'acceptance failed')
            if entry['phase']=='training':development.extend(session.scores)
        manifest['status']='completed_fixed_budget'
    except Exception as exc:
        manifest['status']='stopped_at_gate';manifest['error']=str(exc)
        print('STOPPED',str(exc),flush=True)
    finally:
        if identity:
            v=prep.get(identity)
            if v['status'] not in ('ended','failed_clean','recovery_required') and not prep.demo_owner:prep.action(identity,'stop')
            if prep.thread:prep.thread.join(50)
            atomic_json(output/'preparation_final.json',prep.get(identity))
        atomic_json(output/'manifest.json',manifest);prep.close();owner.close()


if __name__=='__main__':main()
