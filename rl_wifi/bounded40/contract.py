"""Versioned, explicit state/action/reward contract; separate from old nine-arm RL."""
from dataclasses import asdict, dataclass
import math

SCHEMA = 'dqn40-three-actions-v1'
ACTIONS = (16, 32, 64)  # Background W; foreground remains W16.
FEATURES = ('burst', 'announced_burst', 'notice_available', 'timeout_ratio',
            'rtt_mean_ms', 'rtt_observed', 'background_Mbps', 'current_background_W',
            'feedback_background_W', 'feedback_age_s', 'feedback_available')
COUNTS = ('generated', 'mature', 'pending', 'on_time', 'late', 'no_reply',
          'local_failure', 'never_submitted')


@dataclass(frozen=True)
class Config:
    deadline_ms: int = 40
    minimum_on_time: float = .95
    reference_Mbps: float = 12.
    control_s: int = 5
    switch_penalty: float = .02
    seed: int = 7
    hidden: int = 32
    batch_size: int = 8
    capacity: int = 1000
    gamma: float = .9
    learning_rate: float = .001
    target_every: int = 10
    max_training_cycles: int = 72

DEFAULT_CONFIG = Config()


def targets(action):
    if type(action) is not int or not 0 <= action < len(ACTIONS):
        raise ValueError('unknown three-action index')
    return {n: {'W': 16 if n == 'STA1' else ACTIONS[action], 'AIFSN': 3,
                'CWmin': (16 if n == 'STA1' else ACTIONS[action])-1,
                'CWmax': 8*(16 if n == 'STA1' else ACTIONS[action])-1}
            for n in ('STA1', 'STA2', 'STA3', 'STA4')}


def finite(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError('invalid ' + name)
    return value


def vector(state):
    if set(state) != set(FEATURES) or state['current_background_W'] not in ACTIONS or state['feedback_background_W'] not in ACTIONS:
        raise ValueError('state/action schema mismatch')
    for name in ('burst', 'announced_burst', 'notice_available', 'rtt_observed', 'feedback_available'):
        if type(state[name]) is not int or state[name] not in (0, 1):
            raise ValueError('invalid flag ' + name)
    q = finite(state['timeout_ratio'], 'timeout ratio')
    if q > 1:
        raise ValueError('timeout ratio exceeds one')
    age = finite(state['feedback_age_s'], 'feedback age')
    bg = finite(state['background_Mbps'], 'background throughput')
    rtt = state['rtt_mean_ms']
    if bool(state['rtt_observed']) != (rtt is not None):
        raise ValueError('conditional RTT missingness mismatch')
    if rtt is not None:
        finite(rtt, 'RTT')
    return [float(state['burst']), float(state['announced_burst']), float(state['notice_available']),
            q, min(rtt/40, 4) if rtt is not None else 4., float(state['rtt_observed']),
            min(bg/12, 4), state['current_background_W']/64,
            state['feedback_background_W']/64, min(age/10, 4), float(state['feedback_available'])]


def validate_feedback(row):
    """Validate one FULL five-second generation cohort, including switch costs."""
    for key in ('index', 'start_ns', 'end_ns', 'as_of_ns', 'received_ns'):
        if type(row[key]) is not int or row[key] < 0:
            raise ValueError('invalid feedback ' + key)
    if row['source'] not in ('live_hardware', 'offline_fixture') or row['valid'] is not True:
        raise ValueError('invalid/unidentified measurement source')
    if row['deadline_ns'] != 40_000_000 or row['end_ns']-row['start_ns'] != 5_000_000_000:
        raise ValueError('feedback deadline/duration differs from contract')
    if row['as_of_ns'] < row['end_ns']+40_000_000 or row['received_ns'] < row['end_ns']:
        raise ValueError('immature/future feedback')
    targets(row['action'])
    if row['hardware_readback_confirmed'] is not True:
        raise ValueError('hardware readback unconfirmed')
    c = row['counts']
    if set(c) != set(COUNTS) or any(type(c[k]) is not int or c[k] < 0 for k in COUNTS):
        raise ValueError('malformed message counts')
    if (c['generated'] != 500 or c['mature'] != 500 or c['pending'] != 0 or
            sum(c[k] for k in ('on_time', 'late', 'no_reply', 'local_failure', 'never_submitted')) != 500):
        raise ValueError('full planned generation denominator required')
    replies = row['rtt_reply_samples']
    if type(replies) is not int or replies != c['on_time']+c['late']:
        raise ValueError('invalid conditional RTT sample count')
    if (replies == 0) != (row['rtt_mean_ms'] is None):
        raise ValueError('RTT and reply sample count disagree')
    if row['rtt_mean_ms'] is not None:
        finite(row['rtt_mean_ms'], 'RTT')
    finite(row['background_Mbps'], 'background throughput')
    if not row.get('evidence'):
        raise ValueError('raw evidence reference required')
    return row


def reward(row, previous_action):
    validate_feedback(row)
    targets(previous_action)
    q = 1-row['counts']['on_time']/row['counts']['mature']
    # Every feasible segment scores above every infeasible one, before bootstrapping.
    base = (1+min(row['background_Mbps']/12, 1)) if q <= .05+1e-12 else (-1-10*(q-.05))
    return base-.02*int(row['action'] != previous_action)
