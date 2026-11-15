"""Bounded supervisor with injected owned runner; no automatic device discovery.

There is intentionally no live CLI. Future explicit authorization must provide
the preparation, credential-bearing callback and final cleanup callback.
"""
import hashlib
import json
from pathlib import Path
import time
import uuid

from .bridge import may_start_run
from .model import DQN
from .plan import make_plan


def write_new(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def execute(output, run_owned, final_restore, *, started_monotonic, source):
    """Run a sealed campaign through supplied callbacks, preserving all attempts.

    run_owned(entry, model, attempt_folder, stop_monotonic) -> explicit receipt.
    final_restore() -> {'confirmed': bool, ...}; always called, even on failure.
    Source must match every receipt and the inference artifact's provenance.
    """
    if source not in ('live_hardware', 'offline_fixture'):
        raise ValueError('explicit measurement source required')
    folder = Path(output)
    folder.mkdir(parents=True, exist_ok=False)
    plan = make_plan()
    write_new(folder/'plan.json', plan)
    stop_at = started_monotonic+plan['stop']['field_budget_s']
    state = {'source': source, 'status': 'running', 'attempts': [], 'accepted': [],
             'model_file': None, 'new_training_after_test': False}
    model, frozen = DQN(), None
    try:
        for entry in plan['runs']:
            if entry['phase'] == 'test' and frozen is None:
                training = [r for r in state['accepted'] if r['phase'] == 'train']
                if len(training) != 2 or model.steps != 72 or not model.updates:
                    raise RuntimeError('two complete accepted training runs required before frozen test')
                path = folder/'frozen_model.json'
                model.save(path, {'source': source, 'accepted_run_ids': [r['id'] for r in training],
                    'evidence_hashes': {r['receipt']: r['sha256'] for r in training}})
                frozen = DQN.load(path, source)
                state['model_file'] = path.name
            policy = model if entry['phase'] == 'train' else frozen if entry['policy'] == 'DQN' else None
            for attempt in (1, 2):
                if not may_start_run(plan, time.monotonic()-started_monotonic, sum(entry['timing_s'])+60):
                    state['status'] = 'stopped_budget'
                    return state
                checkpoint = model.checkpoint() if entry['phase'] == 'train' else None
                destination = folder/(entry['id']+f'_attempt_{attempt}')
                destination.mkdir()
                try:
                    receipt = run_owned(dict(entry), policy, destination, stop_at)
                    if receipt['source'] != source:
                        raise ValueError('source mismatch, cannot claim real training from fixture')
                except Exception as exc:
                    receipt = {'source': source, 'accepted': False, 'restoration_confirmed': False,
                               'resources_released': False, 'error': str(exc)}
                if not receipt.get('accepted') or receipt.get('performance_stopped'):
                    if checkpoint is not None:
                        model.restore(checkpoint)
                evidence = destination/'receipt.json'
                write_new(evidence, receipt)
                row = {'id': entry['id'], 'phase': entry['phase'], 'policy': entry['policy'],
                       'block': entry.get('block'),
                       'attempt': attempt, 'receipt': str(evidence.relative_to(folder)),
                       'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest()}
                state['attempts'].append(row)
                if receipt.get('performance_stopped'):
                    state['status'] = 'stopped_performance'
                    return state
                restored = receipt.get('restoration_confirmed') is True and receipt.get('resources_released') is True
                if receipt.get('accepted') and restored:
                    state['accepted'].append(row)
                    break
                if attempt == 2 or not restored:
                    state['status'] = 'stopped_gate_after_retry' if attempt == 2 else 'stopped_restore_unknown'
                    return state
        from .report import judge
        tests = []
        for row in state['accepted']:
            if row['phase'] == 'test':
                metric = json.loads((folder/row['receipt']).read_text(encoding='utf-8'))['metrics']
                tests.append({'block': row['block'], 'policy': row['policy'], **metric})
        state['decision'] = judge(tests)
        state['status'] = 'completed_fixed_budget'
        return state
    except Exception as exc:
        state['status'] = 'stopped_error'
        state['error'] = str(exc)
        return state
    finally:
        try:
            state['final_restore'] = final_restore()
        except Exception as exc:
            state['final_restore'] = {'confirmed': False, 'error': str(exc)}
        if state['final_restore'].get('confirmed') is not True:
            state['status'] = 'recovery_required'
        state['elapsed_s'] = time.monotonic()-started_monotonic
        state['unstarted'] = [r['id'] for r in plan['runs'] if r['id'] not in {a['id'] for a in state['attempts']}]
        write_new(folder/'result.json', state)


def prepared_owned_runner(preparation, preparation_id):
    """Bind an ALREADY accepted owned preparation; constructor does no I/O.

    This function is a library hook, not authorization or a preparation bypass.
    The caller still owns final NTP/MON15 cleanup and postflight verification.
    """
    def run(entry, model, folder, stop_at):
        from .session import PreparedSession
        if preparation.get(preparation_id)['status'] != 'ready':
            raise RuntimeError('current preparation is not ready')
        request = {'mode': 'fixed', 'initial_w': 64, 'access_mode': 'basic', 'guard_s': 3,
                   'warmup_s': 5, 'measure_s': entry['timing_s'][1], 'tail_s': 5, 'segment_s': 5,
                   'deadline_ns': 40_000_000, 'preparation_id': preparation_id,
                   'authorization': 'explicit_current_live_authorization_required',
                   'field_stop_monotonic': stop_at}
        # The existing node helper's ownership contract requires a UUID basename.
        owned = folder/uuid.uuid4().hex
        owned.mkdir()
        session = PreparedSession(owned, preparation, request, entry, model)
        session.backend = preparation.claim_demo(preparation_id, session.id, session.abort)
        session.run()
        value = session.state
        learning = value.get('learning_run', {})
        restored = bool((value.get('restoration') or {}).get('confirmed'))
        return {'source': 'live_hardware', 'accepted': value['status'] == 'completed' and
                value.get('acceptance', {}).get('passed') is True,
                'restoration_confirmed': restored, 'resources_released': not preparation.demo_owner,
                'performance_stopped': learning.get('performance_stopped', False),
                'error': value.get('error'), 'session': str(owned.name)+'/session.json',
                'metrics': value.get('metrics'), 'model_training_committed': learning.get('committed')}
    return run
