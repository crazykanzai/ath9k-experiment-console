"""Deterministic transport fixture; deliberately NOT a Wi-Fi simulator."""
import hashlib
import json
from pathlib import Path

from .contract import COUNTS
from .controller import Controller
from .model import DQN
from .plan import NS, Schedule, TRAIN_STATES


def fixture_feedback(index, action, t0_ns=0, on_time=495):
    start = t0_ns+(5+index*5)*NS
    counts = dict.fromkeys(COUNTS, 0)
    counts.update(generated=500, mature=500, on_time=on_time, local_failure=500-on_time)
    return {'index': index, 'source': 'offline_fixture', 'valid': True, 'action': action,
            'start_ns': start, 'end_ns': start+5*NS, 'as_of_ns': start+5*NS+250_000_000,
            'received_ns': start+5*NS+300_000_000, 'deadline_ns': 40_000_000,
            'hardware_readback_confirmed': True, 'counts': counts, 'rtt_reply_samples': on_time,
            'rtt_mean_ms': 8. if on_time else None, 'background_Mbps': 9.+action*.1,
            'evidence': 'offline_fixture_only:no_hardware_observation'}


def exercise(policy, model, states, run_id, accepted=True):
    """Exercise the real controller with a fake clock and delayed full cohorts."""
    schedule = Schedule(states)
    cycles = schedule.measure_s//5
    core = Controller(policy, run_id, 0, cycles, 'offline_fixture', model)
    events = [(1+i*5, 0, i) for i in range(cycles)]
    events += [(10+i*5+.3, 1, i) for i in range(cycles)]
    for seconds, kind, i in sorted(events):
        if kind == 0:
            apply = (5+i*5)*NS
            before = max(0, apply-5*NS)
            current_state, upcoming = schedule.state_at(before), schedule.state_at(apply)
            current_action = core.decisions[i-1]['action'] if i else 2
            core.decide(i, round(seconds*NS), {'state': current_state, 'announced': upcoming}, current_action)
        else:
            core.receive(fixture_feedback(i, core.decisions[i]['action']))
    return core.close(accepted, True, (5+schedule.measure_s+1)*NS,
                      {'state': states[-1], 'announced': None})


def smoke(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    model = DQN()
    model.torch.set_num_threads(1)
    reports = []
    for n in (1, 2):
        report = exercise('DQN_train', model, TRAIN_STATES, f'offline_fixture_train_{n}')
        assert report['committed'] and not report['hardware_training_claim_allowed']
        reports.append(report)
        (output/f'train_{n}.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    evidence = {f'train_{n}.json': hashlib.sha256((output/f'train_{n}.json').read_bytes()).hexdigest() for n in (1, 2)}
    model.save(output/'offline_fixture_model.json', {'source': 'offline_fixture',
               'accepted_run_ids': [r['run_id'] for r in reports], 'evidence_hashes': evidence})
    frozen = DQN.load(output/'offline_fixture_model.json', 'offline_fixture')
    initial = frozen.checkpoint()
    test = exercise('DQN', frozen, ('burst', 'uniform', 'uniform', 'burst'), 'offline_fixture_test')
    assert frozen.steps == initial['steps'] and frozen.updates == initial['updates']
    (output/'test.json').write_text(json.dumps(test, indent=2), encoding='utf-8')
    summary = {'source': 'offline_fixture_only_not_wireless_simulation', 'live_connections': 0,
               'hardware_training_runs': 0, 'fixture_training_runs': 2,
               'training_transitions': model.steps, 'gradient_updates': model.updates,
               'frozen_test_cycles': len(test['decisions']), 'hardware_acceptance': False,
               'purpose': 'protocol, delayed transition joins, weight updates and freezing only; no performance claim'}
    (output/'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return summary
