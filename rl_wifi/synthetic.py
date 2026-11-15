"""Explicit toy QoS environment for software checks, NOT an ath9k digital twin.

Numbers below are invented and do not derive from historical hardware windows.
Useful for testing state-dependent choices and costs, never estimating live gain.
"""
import random
from .spec import ACTIONS


class SyntheticQoS:
    source='synthetic_toy_not_wifi_model'

    def __init__(self, seed, length=30):
        self.seed=seed;self.length=length;self.tick=0;self.rng=random.Random(seed)

    def observation(self, action, tick):
        a=ACTIONS[action]; burst=(tick//6)%2
        # Artificial tradeoff: preferential foreground access helps deadlines
        # but may reduce background throughput. No analytical 802.11 claim.
        priority=a.background_W/a.foreground_W
        timeout=max(0,min(1,.12+.36*burst-.16*priority+self.rng.uniform(-.015,.015)))
        background=max(0,10.5-1.4*priority-.012*a.background_W+self.rng.uniform(-.15,.15))
        return {'valid':True,'foreground_Mbps':1.5+1.0*burst,
                'background_Mbps':background,'rtt_mean_ms':20+130*timeout,
                'timeout_ratio':timeout,'foreground_W':a.foreground_W,
                'background_W':a.background_W,'foreground_active':1,'background_active':1,
                'source':self.source}

    def reset(self):
        self.tick=0;return self.observation(4,0)

    def step(self, action):
        self.tick+=1
        observation=self.observation(action,self.tick)
        return observation,self.tick>=self.length
