"""Offline-only preregistration. This module cannot connect to any node."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import random

from .preparation import CLOCK_POLICY
from .schedules import UNIFORM,BURST
from .spec import ACTIONS


SEED=2026111402
STATES=('uniform','burst')
PRIORITY_ACTION=2  # W16/W64 in the unchanged nine-action action set.
OBJECTIVE={
    'deadline_ms':100,'max_timeout_ratio':.05,
    'selection':'feasible: maximize background; within 5% of maximum choose lowest timeout; '
                'infeasible: minimize timeout; within 1 percentage point choose highest background',
    'remaining_ties':'lower conditional RTT p95; fixed-priority action; action index',
    'background_tie_fraction':.05,'timeout_tie_absolute':.01,
    'practical_timeout_gain_absolute':.05,
    'background_gain_fraction':.05,'timeout_noninferiority_absolute':.01,
    'allowed_background_loss_fraction':.05,
    'generator_quality':'all planned messages accounted; local failures <=1% and generation delay p99 <=5ms',
    'switch_sensitivity':'one 3s zero-service allowance per 30s state episode (assumption, not measured cost)',
    'reward':'no DQN reward or weight fitting in this screening study',
}


def select_action(rows):
    """Deterministic development selection; requires all nine valid windows.

    RTT alone can never override the deadline/background rules. A missing RTT
    means no finite tie-breaking benefit. This does not perform inference.
    """
    if len(rows)!=9 or {r['action_index'] for r in rows}!=set(range(9)):
        raise ValueError('exactly nine explicit action results required')
    for row in rows:
        if (row.get('valid') is not True or
                not math.isfinite(row['timeout_ratio']) or not 0<=row['timeout_ratio']<=1 or
                not math.isfinite(row['background_Mbps']) or row['background_Mbps']<0):
            raise ValueError('invalid/missing screening result')
        p95=row.get('rtt_p95_ms')
        if p95 is not None and (not math.isfinite(p95) or p95<0):
            raise ValueError('invalid conditional RTT')
    feasible=[r for r in rows if r['timeout_ratio']<=OBJECTIVE['max_timeout_ratio']]
    if feasible:
        best=max(r['background_Mbps'] for r in feasible)
        candidates=[r for r in feasible if r['background_Mbps']>=best*(1-OBJECTIVE['background_tie_fraction'])]
        key=lambda r:(r['timeout_ratio'],r.get('rtt_p95_ms') if r.get('rtt_p95_ms') is not None else math.inf,
                      r['action_index']!=PRIORITY_ACTION,r['action_index'])
    else:
        best=min(r['timeout_ratio'] for r in rows)
        candidates=[r for r in rows if r['timeout_ratio']<=best+OBJECTIVE['timeout_tie_absolute']]
        key=lambda r:(-r['background_Mbps'],r.get('rtt_p95_ms') if r.get('rtt_p95_ms') is not None else math.inf,
                      r['action_index']!=PRIORITY_ACTION,r['action_index'])
    return min(candidates,key=key)['action_index']


def meaningful_preference(own,alternative):
    """Exploratory confirmation trigger, not a significance test."""
    deadline_gain=alternative['timeout_ratio']-own['timeout_ratio']>=.05
    background_retained=own['background_Mbps']>=.95*alternative['background_Mbps']
    feasible=own['timeout_ratio']<=.05 and alternative['timeout_ratio']<=.05
    throughput_gain=own['background_Mbps']>=1.05*alternative['background_Mbps'] and alternative['background_Mbps']>0
    timeout_retained=own['timeout_ratio']<=alternative['timeout_ratio']+.01
    return (deadline_gain and background_retained) or (feasible and throughput_gain and timeout_retained)


def paired_order(rng):
    actions=list(range(9));rng.shuffle(actions);rows=[]
    # Adjacent state pairs for each action reduce (not remove) time drift.
    for action in actions:
        states=list(STATES);rng.shuffle(states)
        for state in states:rows.append({'action_index':action,'state':state})
    return rows


def make_plan():
    rng=random.Random(SEED)
    screen=[{'slot':i+1,'phase':'screen',**row} for i,row in enumerate(paired_order(rng))]
    masters=[paired_order(rng) for _ in range(2)]
    return {
        'schema':'reinwifi-fixed-screen-v1','status':'proposed_waiting_user_confirmation',
        'execution_authorized':False,'network_operations':0,
        'seed':SEED,'formal_valid':0,'clock_policy':CLOCK_POLICY,
        'actions':[{'action_index':i,**a.as_dict()} for i,a in enumerate(ACTIONS)],
        'fixed_priority_action':PRIORITY_ACTION,
        'states':{'uniform':UNIFORM.as_dict(),'burst':BURST.as_dict()},
        'traffic':{'foreground':'STA1','background':['STA2','STA3','STA4'],
                   'request_payload_B':1200,'app_reply_payload_B':42,
                   'foreground_messages_per_second':100,'foreground_average_Mbps':.96,
                   'background_offered_Mbps_each':30,'access':'Basic','DATA_rate_Mbps':24,
                   'K':3,'AIFSN':3,'FIFO':20,'power_position_retry':'retain original; verify each window'},
        'timing_s':{'warmup':5,'measurement':30,'tail':5,'future_T0_lead':20},
        'planned_messages':{'whole_window':4000,'measurement':3000},
        'objective':OBJECTIVE,'screen_order':screen,
        'confirmation':{'max_candidates':3,'extra_repeats_per_state':2,
                        'trigger':'different state winners AND meaningful_preference in both directions AND generator quality passed',
                        'candidate_set':'deduplicate uniform winner, burst winner, fixed priority action',
                        'sealed_master_orders':masters,
                        'realization':'filter each master to candidate_set without reshuffling; two separate blocks',
                        'max_windows':12},
        'maximum_windows':30,'screen_business_s':720,'maximum_business_s':1200,
        'wall_clock_budget_minutes':90,
        'duration_estimate_minutes':{'screen':[30,45],'including_confirmation':[50,75]},
        'stop':'gate failure stops batch and restores; no replacements or extra windows; no DQN/UCB traffic',
    }


def confirmation_order(plan,winners):
    if set(winners)!=set(STATES) or any(type(v) is not int or not 0<=v<9 for v in winners.values()):
        raise ValueError('explicit state winners required')
    candidates=set(winners.values())|{PRIORITY_ACTION}
    return [{'block':b+1,**r} for b,master in enumerate(plan['confirmation']['sealed_master_orders'])
            for r in master if r['action_index'] in candidates]


def main():
    parser=argparse.ArgumentParser(description='Write an offline proposal only; never execute traffic')
    parser.add_argument('--output',required=True)
    args=parser.parse_args();out=Path(args.output);out.mkdir(parents=True,exist_ok=False)
    payload=(json.dumps(make_plan(),ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    (out/'plan.json').write_bytes(payload)
    (out/'plan.sha256').write_text(hashlib.sha256(payload).hexdigest()+'  plan.json\n',encoding='utf-8')


if __name__=='__main__':main()
