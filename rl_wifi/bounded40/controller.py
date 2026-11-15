"""Delayed feedback/action pairing, fixed budgets and transactional learning.

The transport is deliberately external: this core cannot connect to devices.
The state includes current action AND the action behind the delayed observation.
No transition crosses a run boundary. Full action rewards arrive after the next
decision, so they are joined by index, never assigned to the newest decision.
"""
import copy

from .contract import ACTIONS, reward, targets, validate_feedback, vector

NS = 1_000_000_000


class PerformanceStop(RuntimeError):
    """Low performance remains valid evidence, not a collection failure."""


def make_state(context, current_action, feedback, now_ns):
    targets(current_action)
    if context['state'] not in ('uniform', 'burst') or context.get('announced') not in (None, 'uniform', 'burst'):
        raise ValueError('application context must come from observed notices')
    state = {'burst': int(context['state'] == 'burst'),
             'announced_burst': int(context.get('announced') == 'burst'),
             'notice_available': int(context.get('announced') is not None),
             'current_background_W': ACTIONS[current_action], 'feedback_available': int(feedback is not None)}
    if feedback is None:
        state.update(timeout_ratio=0., rtt_mean_ms=None, rtt_observed=0,
                     background_Mbps=0., feedback_background_W=ACTIONS[current_action], feedback_age_s=0.)
    else:
        validate_feedback(feedback)
        if feedback['received_ns'] > now_ns or feedback['end_ns'] > now_ns:
            raise ValueError('future feedback leakage')
        state.update(timeout_ratio=1-feedback['counts']['on_time']/500,
                     rtt_mean_ms=feedback['rtt_mean_ms'], rtt_observed=int(feedback['rtt_mean_ms'] is not None),
                     background_Mbps=feedback['background_Mbps'], feedback_background_W=ACTIONS[feedback['action']],
                     feedback_age_s=(now_ns-feedback['end_ns'])/NS)
    vector(state)
    return state


class Controller:
    def __init__(self, policy, run_id, t0_ns, cycles, source, model=None):
        if policy not in ('DQN_train', 'DQN', 'P', 'E', 'middle', 'rule', 'acceptance'):
            raise ValueError('unknown policy')
        if source not in ('offline_fixture', 'live_hardware') or cycles not in (6, 12, 36):
            raise ValueError('explicit source/sealed cycle count required')
        if policy.startswith('DQN') and model is None:
            raise ValueError('DQN model required')
        if policy == 'DQN' and not model.frozen:
            raise ValueError('test requires frozen model')
        self.policy, self.model, self.run_id = policy, model, run_id
        self.t0, self.cycles, self.source = t0_ns, cycles, source
        self.decisions, self.feedbacks, self.transitions = {}, {}, []
        self.finalized = set()
        self.closed = False
        self.performance_stopped = False
        self.checkpoint = model.checkpoint() if policy == 'DQN_train' else None

    def __enter__(self):
        return self

    def __exit__(self, kind, value, trace):
        # A transport/collection exception must not leak unaccepted training.
        if not self.closed:
            if self.checkpoint is not None:
                self.model.restore(self.checkpoint)
            self.closed = True
        return False

    def decide(self, index, now_ns, context, current_action):
        if self.closed or index != len(self.decisions) or index >= self.cycles:
            raise ValueError('duplicate/out-of-order/out-of-budget decision')
        boundary = self.t0+(5+index*5)*NS
        # Existing node command gate requires >=2 s; controller targets 4 s.
        if not 2*NS <= boundary-now_ns <= 25*NS:
            raise ValueError('decision outside prestaging deadline')
        latest = max((r for r in self.feedbacks.values() if r['received_ns'] <= now_ns),
                     key=lambda r: r['index'], default=None)
        state = make_state(context, current_action, latest, now_ns)
        if self.performance_stopped:
            action, reason = 2, 'performance fallback P; no further learning'
        elif (self.policy == 'DQN_train' and index < 6) or self.policy == 'acceptance':
            action = (index+2) % 3
            reason = 'predeclared action coverage; not learned'
        elif self.policy == 'DQN' and index == 0:
            action, reason = 2, 'cold-start P; initial setup is not a learned decision'
        elif self.policy.startswith('DQN'):
            action = self.model.choose(state, training=self.policy == 'DQN_train')
            reason = 'DQN epsilon-greedy' if self.policy == 'DQN_train' else 'frozen DQN, epsilon=0'
        elif self.policy == 'rule':
            # The same advance notice available to DQN; no hidden future script.
            action = 2 if (context.get('announced') or context['state']) == 'burst' else 0
            reason = 'application notice rule'
        else:
            action = {'E': 0, 'middle': 1, 'P': 2}[self.policy]
            reason = 'fixed control'
        targets(action)
        row = {'index': index, 'action': action, 'state': state, 'decision_ns': now_ns,
               'apply_ns': boundary, 'reason': reason, 'feedback_index': latest['index'] if latest else None}
        self.decisions[index] = copy.deepcopy(row)
        self.drain()
        return {**row, 'targets': targets(action)}

    def receive(self, row):
        row = copy.deepcopy(validate_feedback(row))
        i = row['index']
        if self.closed or i != len(self.feedbacks) or i not in self.decisions:
            raise ValueError('duplicate/out-of-order/uncommanded feedback')
        d = self.decisions[i]
        if (row['source'] != self.source or row['action'] != d['action'] or
                row['start_ns'] != d['apply_ns'] or row['end_ns'] != d['apply_ns']+5*NS):
            raise ValueError('feedback does not belong to recorded action')
        self.feedbacks[i] = row
        self.drain()
        recent = list(self.feedbacks.values())[-3:]
        if (self.policy == 'DQN_train' and not self.performance_stopped and len(recent) == 3
                and all(r['counts']['on_time'] < 450 for r in recent)):
            self.performance_stopped = True
            raise PerformanceStop('three complete cohorts below 90%; request P, preserve evidence and stop training')

    def drain(self, terminal_state=None):
        for i in sorted(self.feedbacks):
            if i in self.finalized:
                continue
            terminal = i == self.cycles-1 and terminal_state is not None
            next_state = terminal_state if terminal else self.decisions.get(i+1, {}).get('state')
            if next_state is None:
                continue
            row, decision = self.feedbacks[i], self.decisions[i]
            # Initial setup belongs to warmup, not a fictitious measurement switch.
            previous = self.decisions[i-1]['action'] if i else decision['action']
            score = reward(row, previous)
            transition = {'run_id': self.run_id, 'index': i, 'source': self.source,
                          'state': decision['state'], 'action': decision['action'], 'reward': score,
                          'next_state': copy.deepcopy(next_state), 'terminal': terminal,
                          'evidence': row['evidence']}
            if self.policy == 'DQN_train' and not self.performance_stopped:
                transition['loss'] = self.model.observe(transition['state'], transition['action'], score,
                                                       transition['next_state'], terminal)
            self.transitions.append(transition)
            self.finalized.add(i)

    def close(self, accepted, restoration_confirmed, now_ns, context):
        if self.closed or type(accepted) is not bool or type(restoration_confirmed) is not bool:
            raise ValueError('explicit one-time acceptance/restoration required')
        complete = len(self.decisions) == len(self.feedbacks) == self.cycles
        commit = accepted and restoration_confirmed and complete and not self.performance_stopped
        try:
            if commit:
                last = self.feedbacks[self.cycles-1]
                terminal = make_state(context, last['action'], last, now_ns)
                self.drain(terminal)
            elif self.checkpoint is not None:
                self.model.restore(self.checkpoint)
        except Exception:
            if self.checkpoint is not None:
                self.model.restore(self.checkpoint)
            raise
        finally:
            self.closed = True
        return {'run_id': self.run_id, 'source': self.source, 'committed': commit,
                'restoration_confirmed': restoration_confirmed, 'complete': complete,
                'collection_accepted': accepted, 'performance_stopped': self.performance_stopped,
                'model_rolled_back': self.checkpoint is not None and not commit,
                'decisions': list(self.decisions.values()), 'feedback': list(self.feedbacks.values()),
                'transitions': self.transitions, 'discarded_from_training': not commit,
                'hardware_training_claim_allowed': commit and self.source == 'live_hardware' and
                self.policy == 'DQN_train' and self.model.updates > self.checkpoint['updates']}
