import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from rl_wifi.spec import ACTIONS, RewardConfig, features, node_targets, reward
from rl_wifi.policies import FixedPolicy,UCBPolicy
from rl_wifi.synthetic import SyntheticQoS


class ContractTests(unittest.TestCase):
    def test_actions_keep_explicit_identity_k3_and_no_hidden_executor(self):
        self.assertEqual(len(ACTIONS),9)
        targets=node_targets(2,'node-A',['node-D','node-B','node-C'])
        self.assertEqual(targets['node-A']['CWmin'],15)
        self.assertEqual(targets['node-D']['CWmax'],511)
        with self.assertRaises(ValueError):node_targets(1,'a',['a'])
        with self.assertRaises(ValueError):node_targets(-1,'a',['b'])

    def test_na_cannot_become_reward_or_zero_failure(self):
        obs=SyntheticQoS(1).reset();config=RewardConfig()
        expected=obs['background_Mbps']/12-2*obs['timeout_ratio']-.02
        self.assertAlmostEqual(reward(obs,True,config),expected)
        for change in ({'timeout_ratio':None},{'valid':False},{'background_Mbps':float('nan')}):
            with self.assertRaises(ValueError):reward({**obs,**change},False,config)

    def test_ucb_exploration_cost_is_retained_and_fixed_is_fixed(self):
        p=UCBPolicy();state=SyntheticQoS(1).reset()
        for a in range(9):
            self.assertEqual(p.choose(state),a)
            p.observe(state,a,1 if a==3 else -1,state)
        self.assertEqual(p.choose(state),3)
        self.assertEqual(FixedPolicy(4).choose(state),4)


@unittest.skipUnless(importlib.util.find_spec('torch'),'optional torch dependency not installed')
class DQNTests(unittest.TestCase):
    def setUp(self):
        import torch
        torch.set_num_threads(1)

    def test_target_bootstrap_terminal_mask_and_target_network_delay(self):
        from rl_wifi.policies import DQNPolicy
        p=DQNPolicy(batch_size=1,gamma=.5,target_every=10)
        torch=p.torch
        with torch.no_grad():
            for parameter in p.online.parameters():parameter.zero_()
            for parameter in p.target.parameters():parameter.zero_()
            p.target[-1].bias.fill_(2)
        obs=SyntheticQoS(1).reset()
        # Q=0, r=1, gamma=.5, target max=2 -> Huber(0,2)=1.5
        loss=p.observe(obs,0,1,obs,False)
        self.assertAlmostEqual(loss,1.5,places=5)
        with torch.no_grad():
            for parameter in p.online.parameters():parameter.zero_()
        p.replay.clear()
        # terminal suppresses bootstrap -> Huber(0,1)=.5
        loss=p.observe(obs,0,1,obs,True)
        self.assertAlmostEqual(loss,.5,places=5)
        self.assertEqual(float(p.target[-1].bias[0]),2)

    def test_state_dependent_learning_checkpoint_and_frozen_inference(self):
        from rl_wifi.policies import DQNPolicy
        p=DQNPolicy(seed=5,batch_size=16,gamma=0,learning_rate=.003)
        a=SyntheticQoS(1).reset(); b={**a,'timeout_ratio':.9,'rtt_mean_ms':150.}
        # Same actions, opposite rewards across observable states. A constant
        # best-arm rule cannot solve this controlled software acceptance case.
        for step in range(700):
            state=(a,b)[step%2];action=(step//2)%2
            correct=(step%2)==action
            p.observe(state,action,1 if correct else -1,state,True)
        self.assertEqual(p.choose(a),0);self.assertEqual(p.choose(b),1)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'model.json';p.save(path,{'source':'synthetic_unit_test'})
            loaded=DQNPolicy.load(path,p.config)
            self.assertEqual(loaded.choose(a),0);self.assertEqual(loaded.choose(b),1)
            with self.assertRaises(ValueError):loaded.observe(a,0,1,b)
            with self.assertRaises(ValueError):DQNPolicy.load(path,RewardConfig(deadline_ms=80))
            data=json.loads(path.read_text());data['model']['weights']['0.bias'][0]+=1
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError,'hash'):DQNPolicy.load(path)


if __name__=='__main__':unittest.main()
