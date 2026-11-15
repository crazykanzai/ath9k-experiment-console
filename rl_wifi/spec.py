"""Common observation, action and reward contract for all three policies."""
from dataclasses import dataclass
import math

WINDOWS = (16,32,64)
FEATURES = ('foreground_Mbps','background_Mbps','rtt_mean_ms','rtt_observed','timeout_ratio',
            'foreground_W','background_W','foreground_active','background_active')


@dataclass(frozen=True)
class Action:
    foreground_W: int
    background_W: int

    def as_dict(self):
        return {'foreground_W':self.foreground_W,'background_W':self.background_W,
                'foreground_CWmin':self.foreground_W-1,'foreground_CWmax':8*self.foreground_W-1,
                'background_CWmin':self.background_W-1,'background_CWmax':8*self.background_W-1,
                'K':3,'AIFSN':3}


ACTIONS = tuple(Action(a,b) for a in WINDOWS for b in WINDOWS)


def node_targets(index, foreground, background):
    """Build a request only. This module has no hardware execution adapter."""
    if isinstance(index,bool) or not isinstance(index,int) or not 0 <= index < len(ACTIONS):
        raise ValueError('action index out of range')
    if not foreground or not background or len(set([foreground,*background])) != 1+len(background):
        raise ValueError('explicit disjoint foreground/background node identities required')
    a = ACTIONS[index]
    return {node:{'W':w,'CWmin':w-1,'CWmax':8*w-1,'K':3,'AIFSN':3}
            for node,w in [(foreground,a.foreground_W),*((n,a.background_W) for n in background)]}


@dataclass(frozen=True)
class RewardConfig:
    background_reference_Mbps: float = 12.0
    deadline_ms: float = 100.0
    timeout_weight: float = 2.0
    switch_weight: float = .02

    def __post_init__(self):
        values = (self.background_reference_Mbps,self.deadline_ms,self.timeout_weight,self.switch_weight)
        if not all(math.isfinite(v) for v in values) or min(values)<0 or min(values[:2])<=0:
            raise ValueError('invalid frozen reward configuration')


def features(observation, config):
    for key in FEATURES:
        if key == 'rtt_observed': continue
        if key == 'rtt_mean_ms' and observation.get(key) is None and observation.get('rtt_reply_samples')==0:
            continue
        if key not in observation or observation[key] is None or not math.isfinite(observation[key]):
            raise ValueError('missing/nonfinite observation: '+key)
    if observation.get('valid') is not True:
        raise ValueError('invalid observation is not a learning sample')
    if 'deadline_ms' in observation and observation['deadline_ms']!=config.deadline_ms:
        raise ValueError('observation deadline differs from frozen model configuration')
    for key in ('foreground_Mbps','background_Mbps'):
        if observation[key]<0: raise ValueError('negative '+key)
    if observation['rtt_mean_ms'] is not None and observation['rtt_mean_ms']<0:
        raise ValueError('negative RTT')
    if not 0<=observation['timeout_ratio']<=1: raise ValueError('invalid timeout ratio')
    if observation['foreground_W'] not in WINDOWS or observation['background_W'] not in WINDOWS:
        raise ValueError('unknown action readback')
    if any(observation[k] not in (0,1) for k in ('foreground_active','background_active')):
        raise ValueError('invalid active flags')
    # Bounded scaling is frozen with model; no normalization fitted on test runs.
    return [min(observation['foreground_Mbps']/config.background_reference_Mbps,4),
            min(observation['background_Mbps']/config.background_reference_Mbps,4),
            min(observation['rtt_mean_ms']/config.deadline_ms,4) if observation['rtt_mean_ms'] is not None else 4.,
            float(observation['rtt_mean_ms'] is not None),observation['timeout_ratio'],
            observation['foreground_W']/64,observation['background_W']/64,
            float(observation['foreground_active']),float(observation['background_active'])]


def reward(observation, switched, config):
    features(observation,config)
    if type(switched) is not bool: raise ValueError('switched must be bool')
    return (observation['background_Mbps']/config.background_reference_Mbps
            -config.timeout_weight*observation['timeout_ratio']-config.switch_weight*int(switched))


def observation_from_report(report, foreground_Mbps, background_Mbps, action_index):
    """Require a fully matured generation cohort; unknown is never zero loss."""
    if not report.get('complete_cohort') or report.get('collection_complete') is False or report['counts']['mature']<=0:
        raise ValueError('cohort incomplete or empty; do not learn')
    if type(action_index) is not int or not 0<=action_index<len(ACTIONS):
        raise ValueError('invalid action index')
    a=ACTIONS[action_index]
    return {'valid':True,'foreground_Mbps':foreground_Mbps,'background_Mbps':background_Mbps,
            'deadline_ms':report['deadline_ns']/1e6,
            'rtt_mean_ms':report['rtt_mean_ms'],'rtt_reply_samples':report['rtt_reply_samples'],
            'timeout_ratio':report['timeout_ratio'],'foreground_W':a.foreground_W,
            'background_W':a.background_W,'foreground_active':1,'background_active':1}
