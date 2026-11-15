"""End-to-end software acceptance. Reject all real socket creation."""
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec('torch'),'optional torch dependency not installed')
class OfflineTests(unittest.TestCase):
    def test_full_pipeline_no_network_frozen_holdout_and_explicit_manifest(self):
        import torch
        from rl_wifi.__main__ import offline
        with tempfile.TemporaryDirectory() as folder, patch('socket.socket',side_effect=AssertionError('no sockets')):
            output=Path(folder)/'result'
            offline(argparse.Namespace(output=str(output),seed=7,steps=64))
            manifest=json.loads((output/'manifest.json').read_text())
            self.assertEqual(manifest['formal_valid'],0)
            self.assertFalse(manifest['traffic_started'])
            self.assertTrue(set(manifest['evaluation_seeds']).isdisjoint(manifest['development_seeds']))
            trained_seeds={manifest['seed']+i for i in range(3)}
            self.assertTrue(trained_seeds.isdisjoint(manifest['evaluation_seeds']))
            metrics=json.loads((output/'metrics.json').read_text())
            self.assertEqual(set(metrics['results']),{'fixed','ucb','dqn'})
            self.assertTrue(all(d['n']==5 for d in metrics['results'].values()))
            steps=[json.loads(line) for line in (output/'training.jsonl').read_text().splitlines()]
            self.assertEqual(len(steps),64)
            self.assertTrue(all(set(('state','action_index','reward','next_state','terminal'))<=r.keys() for r in steps))
            complete=json.loads((output/'completion.json').read_text())
            self.assertTrue(complete['probe_replay_equal'])
            self.assertTrue((output/'comparison.svg').is_file())
            with self.assertRaises(FileExistsError):offline(argparse.Namespace(output=str(output),seed=7,steps=64))


if __name__=='__main__':unittest.main()
