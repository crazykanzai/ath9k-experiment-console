"""Pure bounded plan and generation schedule. No live entry point."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from .contract import ACTIONS, DEFAULT_CONFIG, FEATURES, SCHEMA

NS = 1_000_000_000
TRAIN_STATES = ('uniform', 'burst') * 6
TEST_STATES = (('burst', 'uniform', 'uniform', 'burst'),
               ('uniform', 'burst', 'burst', 'uniform'))
TEST_ORDER = (('P', 'DQN', 'middle', 'rule', 'E'),
              ('E', 'rule', 'middle', 'DQN', 'P'))


from .node_schedule import Schedule


def make_plan():
    return {
        'schema': SCHEMA, 'status': 'offline_prepared_not_hardware_accepted',
        'live_authorized': False, 'live_entrypoint_enabled': False,
        'config': asdict(DEFAULT_CONFIG), 'features': list(FEATURES),
        'background_actions_W': list(ACTIONS), 'foreground_W': 16,
        'radio': {'Basic': True, 'DATA_Mbps': 24, 'K': 3, 'AIFSN': 3, 'FIFO': 20,
                  'power_position_retry': 'retain existing, rediscover before use'},
        'load': {'foreground_payload_B': 1200, 'foreground_messages_per_s': 100,
                 'uniform_period_ms': 10, 'burst_period_ms': 100, 'burst_messages': 10,
                 'background_each_Mbps': 30, 'reply_B': 42},
        'runs': [{'id': 'acceptance', 'phase': 'acceptance', 'timing_s': [5, 30, 5],
                  'states': ['uniform', 'burst'], 'policy': 'sealed_actions_no_training'}]
                +[{'id': f'train_{i}', 'phase': 'train', 'timing_s': [5, 180, 5],
                   'states': list(TRAIN_STATES), 'policy': 'DQN'} for i in (1, 2)]
                +[{'id': f'test_{b+1}_{p}', 'phase': 'test', 'block': b+1,
                   'timing_s': [5, 60, 5], 'states': list(TEST_STATES[b]), 'policy': p}
                  for b in range(2) for p in TEST_ORDER[b]],
        'control': {'dispatch_lead_s': 4, 'minimum_node_stage_lead_s': 2,
                    'decision_feedback': 'latest full matured cohort received before dispatch; often two cycles old',
                    'reward': 'full action cohort, delayed and keyed by action index; no switch trimming',
                    'application_notice_lead_s': 6, 'unknown_notice': 'explicit missing flag; no future script input'},
        'learning': {'cold_start': True, 'synthetic_weights': False, 'historical_pretraining': False,
                     'coverage': 'first six cycles per run: P,E,middle,P,E,middle; both U/B get all actions',
                     'frozen_initial_action': 'P for first five seconds, included in test costs',
                     'epsilon': 'after coverage: linear 0.5 to 0.05 over 60 training choices',
                     'updates_per_transition': 1, 'checkpoint': 'commit only after run acceptance; rollback replay/optimizer/RNG on failure'},
        'stop': {'field_budget_s': 2700, 'recovery_reserve_s': 180,
                 'max_attempts_per_run': 2, 'max_training_cycles': 72,
                 'performance': 'three consecutive complete cohorts on_time <0.90; revert P and end training',
                 'performance_low_is_invalid': False, 'loss_plateau_is_not_extra_training_authorization': True},
        'evaluation': {'minimum_on_time_per_state': .95, 'minimum_background_gain': .05,
                       'all_switching_seconds_included': True, 'unit': 'run, not packet or second',
                       'baseline': 'best fixed among P/E/middle meeting both states, within each block',
                       'two_block_agreement': True, 'first_success': 'independent replication signal only'},
        'clock': {'initial_envelope_ms': 50, 'continuous_envelope_ms': 100,
                  'warning_above_ms': 10, 'extra_warning_above_ms': 50,
                  'boundary_envelope_ms': 100, 'source_and_sync_required': True,
                  'scope': 'proposed dedicated quick adapter, no global changes'},
        'hardware_status': 'new live bridge still requires authorized no-training acceptance',
        'restore': 'original effective settings, MON15, runtime time config; only owned processes',
    }


def seal(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    data = (json.dumps(make_plan(), ensure_ascii=False, indent=2)+'\n').encode()
    (output/'plan.json').write_bytes(data)
    (output/'plan.sha256').write_text(hashlib.sha256(data).hexdigest()+'  plan.json\n', encoding='ascii')
    for name, states in [('acceptance', ('uniform', 'burst')), ('train', TRAIN_STATES),
                         ('test_block_1', TEST_STATES[0]), ('test_block_2', TEST_STATES[1])]:
        schedule = Schedule(states)
        with (output/(name+'_message_plan.jsonl')).open('x', encoding='utf-8') as stream:
            for seq in range(schedule.count(schedule.total_s*NS)):
                at = schedule.offset_ns(seq)
                stream.write(json.dumps({'seq': seq, 'T0_offset_ns': at,
                    'deadline_T0_offset_ns': at+40_000_000, 'state': schedule.state_at(at),
                    'source': 'planned_not_measurement'})+'\n')
    return make_plan()
