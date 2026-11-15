"""Fixed CW, sliding-window UCB and CPU DQN over the same nine actions.

Independent implementation inspired by ReinWiFi; no upstream source copied.
Torch is imported only by DQN, keeping the existing console dependency-free.
"""
from collections import deque
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import random

from .spec import ACTIONS, FEATURES, RewardConfig, features


class FixedPolicy:
    def __init__(self, action=4):
        if type(action) is not int or not 0<=action<len(ACTIONS): raise ValueError('invalid fixed action')
        self.action = action

    def choose(self, state, training=False):
        return self.action

    def observe(self, *args, **kwargs):
        return None


class UCBPolicy:
    """Common-action-space comparator, not the old three-arm webpage policy."""
    def __init__(self, window=60, exploration=.25):
        if window<len(ACTIONS) or exploration<0: raise ValueError('invalid UCB settings')
        self.history = deque(maxlen=window); self.exploration = exploration

    def choose(self, state, training=False):
        values = {a:[] for a in range(len(ACTIONS))}
        for action, score in self.history: values[action].append(score)
        for a in values:
            if not values[a]: return a
        total = len(self.history)
        return max(values,key=lambda a:sum(values[a])/len(values[a])+
                   self.exploration*math.sqrt(2*math.log(total)/len(values[a])))

    def observe(self, state, action, score, next_state, terminal=False):
        if type(action) is not int or not 0<=action<len(ACTIONS) or not math.isfinite(score):
            raise ValueError('invalid UCB sample')
        self.history.append((action,float(score)))


class DQNPolicy:
    def __init__(self, seed=7, config=None, hidden=32, batch_size=32, capacity=5000,
                 gamma=.9, learning_rate=.001, target_every=50):
        import torch
        if hidden<1 or batch_size<1 or capacity<batch_size or not 0<=gamma<1 or target_every<1 or learning_rate<=0:
            raise ValueError('invalid DQN hyperparameters')
        self.torch = torch; self.config = config or RewardConfig()
        self.hidden=hidden; self.batch_size=batch_size; self.gamma=gamma
        self.target_every=target_every; self.seed=seed; self.steps=0; self.updates=0
        self.learning_rate=learning_rate
        self.rng=random.Random(seed); self.replay=deque(maxlen=capacity); self.frozen=False
        # Do not disturb the caller's global PyTorch RNG.
        with torch.random.fork_rng(devices=[]):
            torch.manual_seed(seed)
            self.online=self._network(); self.target=self._network()
        self.target.load_state_dict(self.online.state_dict()); self.target.eval()
        self.optimizer=torch.optim.Adam(self.online.parameters(),lr=learning_rate)

    def _network(self):
        nn=self.torch.nn
        return nn.Sequential(nn.Linear(len(FEATURES),self.hidden),nn.ReLU(),
                             nn.Linear(self.hidden,self.hidden),nn.ReLU(),
                             nn.Linear(self.hidden,len(ACTIONS)))

    def _vector(self, state):
        value=features(state,self.config)
        return self.torch.tensor(value,dtype=self.torch.float32)

    def choose(self, state, training=False):
        vector=self._vector(state)
        if training and self.frozen: raise ValueError('inference checkpoint is frozen')
        epsilon=max(.05,.8*(.995**self.steps)) if training else 0
        if self.rng.random()<epsilon: return self.rng.randrange(len(ACTIONS))
        with self.torch.no_grad(): return int(self.online(vector).argmax().item())

    def observe(self, state, action, score, next_state, terminal=False):
        if self.frozen: raise ValueError('inference checkpoint cannot train')
        if type(action) is not int or not 0<=action<len(ACTIONS) or not math.isfinite(score):
            raise ValueError('invalid DQN transition')
        if type(terminal) is not bool: raise ValueError('terminal must be bool')
        s=self._vector(state); ns=self._vector(next_state)
        self.replay.append((s,action,float(score),ns,terminal)); self.steps+=1
        if len(self.replay)<self.batch_size: return None
        torch=self.torch
        batch=self.rng.sample(list(self.replay),self.batch_size)
        states=torch.stack([x[0] for x in batch]); next_states=torch.stack([x[3] for x in batch])
        actions=torch.tensor([x[1] for x in batch],dtype=torch.long)
        scores=torch.tensor([x[2] for x in batch],dtype=torch.float32)
        terminal_mask=torch.tensor([x[4] for x in batch],dtype=torch.bool)
        predicted=self.online(states).gather(1,actions[:,None]).squeeze(1)
        with torch.no_grad():
            expected=scores+self.gamma*self.target(next_states).max(1).values*(~terminal_mask)
        loss=torch.nn.functional.smooth_l1_loss(predicted,expected)
        self.optimizer.zero_grad(); loss.backward()
        torch.nn.utils.clip_grad_norm_(self.online.parameters(),5.0); self.optimizer.step()
        self.updates+=1
        if self.updates%self.target_every==0:
            self.target.load_state_dict(self.online.state_dict())
        return float(loss.item())

    def save(self, path, provenance):
        """Safe JSON inference artifact. Never claims to resume optimizer/replay."""
        if not provenance or 'source' not in provenance: raise ValueError('training provenance required')
        data={'schema':1,'algorithm':'DQN','purpose':'frozen_inference_only',
              'config':asdict(self.config),'features':list(FEATURES),
              'actions':[asdict(a) for a in ACTIONS],'hidden':self.hidden,
              'seed':self.seed,'steps':self.steps,'updates':self.updates,
              'training_hyperparameters':{'batch_size':self.batch_size,'replay_capacity':self.replay.maxlen,
                  'gamma':self.gamma,'learning_rate':self.learning_rate,'target_every':self.target_every,
                  'epsilon':'max(0.05,0.8*0.995**steps)','gradient_clip_norm':5.0,'loss':'Huber'},
              'provenance':provenance,
              'weights':{k:v.detach().cpu().tolist() for k,v in self.online.state_dict().items()}}
        payload=json.dumps(data,sort_keys=True,allow_nan=False).encode('utf-8')
        envelope={'sha256':hashlib.sha256(payload).hexdigest(),'model':data}
        with Path(path).open('x',encoding='utf-8') as stream:
            json.dump(envelope,stream,ensure_ascii=False,indent=2,allow_nan=False)

    @classmethod
    def load(cls, path, expected_config=None):
        envelope=json.loads(Path(path).read_text(encoding='utf-8')); data=envelope['model']
        digest=hashlib.sha256(json.dumps(data,sort_keys=True,allow_nan=False).encode('utf-8')).hexdigest()
        if envelope['sha256']!=digest: raise ValueError('model hash mismatch')
        if (data['schema']!=1 or data['algorithm']!='DQN' or data['purpose']!='frozen_inference_only'
                or data['features']!=list(FEATURES) or data['actions']!=[asdict(a) for a in ACTIONS]):
            raise ValueError('model observation/action contract mismatch')
        config=RewardConfig(**data['config'])
        if expected_config is not None and config!=expected_config: raise ValueError('reward configuration mismatch')
        model=cls(seed=data['seed'],config=config,hidden=data['hidden'])
        tensors={k:model.torch.tensor(v,dtype=model.torch.float32) for k,v in data['weights'].items()}
        if not all(model.torch.isfinite(v).all() for v in tensors.values()): raise ValueError('nonfinite model weights')
        model.online.load_state_dict(tensors); model.target.load_state_dict(tensors)
        model.online.eval(); model.steps=data['steps'];model.updates=data['updates']; model.frozen=True
        return model
