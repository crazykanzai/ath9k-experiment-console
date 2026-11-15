"""Small CPU DQN with full in-memory transaction rollback and frozen JSON export."""
from collections import deque
import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random

from .contract import ACTIONS, DEFAULT_CONFIG, FEATURES, SCHEMA, vector


class DQN:
    def __init__(self):
        import torch
        self.torch = torch
        self.config = DEFAULT_CONFIG
        self.rng = random.Random(self.config.seed)
        self.replay = deque(maxlen=self.config.capacity)
        self.steps = self.updates = self.choices = 0
        self.frozen = False
        with torch.random.fork_rng(devices=[]):
            torch.manual_seed(self.config.seed)
            self.online = self.network()
            self.target = self.network()
        self.target.load_state_dict(self.online.state_dict())
        self.target.eval()
        self.optimizer = torch.optim.Adam(self.online.parameters(), lr=self.config.learning_rate)

    def network(self):
        nn = self.torch.nn
        return nn.Sequential(nn.Linear(len(FEATURES), 32), nn.ReLU(),
                             nn.Linear(32, 32), nn.ReLU(), nn.Linear(32, len(ACTIONS)))

    def tensor(self, state):
        return self.torch.tensor(vector(state), dtype=self.torch.float32)

    def choose(self, state, training=False):
        data = self.tensor(state)
        if training and self.frozen:
            raise ValueError('frozen test model cannot explore')
        epsilon = max(.05, .5-.45*min(self.choices, 60)/60) if training else 0.
        if training:
            self.choices += 1
            if self.rng.random() < epsilon:
                return self.rng.randrange(3)
        with self.torch.no_grad():
            return int(self.online(data).argmax().item())

    def observe(self, state, action, score, next_state, terminal=False):
        import math
        if self.frozen:
            raise ValueError('frozen test model cannot update')
        if type(action) is not int or not 0 <= action < 3 or not math.isfinite(score) or type(terminal) is not bool:
            raise ValueError('invalid transition')
        if self.steps >= self.config.max_training_cycles:
            raise ValueError('sealed training transition budget exhausted')
        self.replay.append((self.tensor(state), action, float(score), self.tensor(next_state), terminal))
        self.steps += 1
        if len(self.replay) < self.config.batch_size:
            return None
        torch = self.torch
        batch = self.rng.sample(list(self.replay), self.config.batch_size)
        states = torch.stack([r[0] for r in batch])
        actions = torch.tensor([r[1] for r in batch], dtype=torch.long)
        scores = torch.tensor([r[2] for r in batch])
        next_states = torch.stack([r[3] for r in batch])
        terminals = torch.tensor([r[4] for r in batch], dtype=torch.bool)
        values = self.online(states).gather(1, actions[:, None]).squeeze(1)
        with torch.no_grad():
            expected = scores+self.config.gamma*self.target(next_states).max(1).values*(~terminals)
        loss = torch.nn.functional.smooth_l1_loss(values, expected)
        self.optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.online.parameters(), 5.)
        self.optimizer.step()
        self.updates += 1
        if self.updates % self.config.target_every == 0:
            self.target.load_state_dict(self.online.state_dict())
        return float(loss.item())

    def checkpoint(self):
        return copy.deepcopy({'online': self.online.state_dict(), 'target': self.target.state_dict(),
            'optimizer': self.optimizer.state_dict(), 'replay': self.replay, 'rng': self.rng.getstate(),
            'steps': self.steps, 'updates': self.updates, 'choices': self.choices})

    def restore(self, checkpoint):
        if self.frozen:
            raise ValueError('cannot restore a training checkpoint into frozen inference')
        c = copy.deepcopy(checkpoint)
        self.online.load_state_dict(c['online'])
        self.target.load_state_dict(c['target'])
        self.optimizer.load_state_dict(c['optimizer'])
        self.replay = c['replay']
        self.rng.setstate(c['rng'])
        self.steps, self.updates, self.choices = c['steps'], c['updates'], c['choices']

    def save(self, path, provenance):
        if (provenance.get('source') not in ('offline_fixture', 'live_hardware') or
                not provenance.get('accepted_run_ids') or not provenance.get('evidence_hashes')):
            raise ValueError('accepted training provenance and evidence hashes required')
        data = {'schema': SCHEMA, 'features': list(FEATURES), 'actions': list(ACTIONS),
                'config': asdict(self.config), 'purpose': 'frozen_inference_only',
                'training_steps': self.steps, 'updates': self.updates, 'provenance': provenance,
                'weights': {k: v.detach().cpu().tolist() for k, v in self.online.state_dict().items()}}
        payload = json.dumps(data, sort_keys=True, allow_nan=False).encode()
        with Path(path).open('x', encoding='utf-8') as stream:
            json.dump({'sha256': hashlib.sha256(payload).hexdigest(), 'model': data}, stream, indent=2)

    @classmethod
    def load(cls, path, required_source=None):
        envelope = json.loads(Path(path).read_text(encoding='utf-8'))
        data = envelope['model']
        payload = json.dumps(data, sort_keys=True, allow_nan=False).encode()
        if (hashlib.sha256(payload).hexdigest() != envelope['sha256'] or data['schema'] != SCHEMA or
                data['features'] != list(FEATURES) or data['actions'] != list(ACTIONS) or
                data['config'] != asdict(DEFAULT_CONFIG) or data['purpose'] != 'frozen_inference_only'):
            raise ValueError('model hash/contract mismatch')
        if required_source is not None and data['provenance']['source'] != required_source:
            raise ValueError('synthetic/offline checkpoint cannot masquerade as live training')
        model = cls()
        tensors = {k: model.torch.tensor(v, dtype=model.torch.float32) for k, v in data['weights'].items()}
        if not all(model.torch.isfinite(v).all() for v in tensors.values()):
            raise ValueError('nonfinite model weights')
        model.online.load_state_dict(tensors)
        model.target.load_state_dict(tensors)
        model.online.eval()
        model.steps, model.updates = data['training_steps'], data['updates']
        model.frozen = True
        return model
