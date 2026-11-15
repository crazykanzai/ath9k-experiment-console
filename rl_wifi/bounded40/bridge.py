"""Explicit adapter interface for the existing absolute-time node executor.

No SSH or socket is created here. The caller provides Session.send_stage and
owned raw receipts. This adapter refuses to invent missing observation data.
Five-second node scheduling/deployment is NOT hardware-accepted by these tests.
"""
import copy

from .contract import ACTIONS, validate_feedback
from .controller import PerformanceStop

NS = 1_000_000_000
ROLES = ('STA1', 'STA2', 'STA3', 'STA4')


class Bridge:
    def __init__(self, controller, send_stage, initial_action):
        self.core = controller
        self.send_stage = send_stage
        self.initial_action = initial_action
        self.receipts = {}
        self.dispatches = {}
        self.fallback_requested = False

    def dispatch(self, index, now_ns, context, current_action):
        decision = self.core.decide(index, now_ns, context, current_action)
        settings = {n: t['W'] for n, t in decision['targets'].items()}
        if index == 0:
            # Initial W is set by the existing owned preparation, before armed.
            if decision['action'] != self.initial_action:
                raise ValueError('initial decision differs from armed hardware configuration')
        else:
            self.send_stage(index, settings, decision['apply_ns'])
        self.dispatches[index] = copy.deepcopy(decision)
        return decision

    def readback(self, index, role, readback, write_call_ns, write_return_ns, verified_ns, uncertainty_ns):
        if role not in ROLES or index not in self.dispatches or role in self.receipts.get(index, {}):
            raise ValueError('uncommanded/duplicate hardware receipt')
        expected = self.dispatches[index]['targets'][role]
        if (readback.get('status_name') != 'verified' or
                [int(readback[k]) for k in ('hw_aifs', 'hw_cwmin', 'hw_cwmax')] !=
                [3, expected['CWmin'], expected['CWmax']]):
            raise ValueError('actual CW/AIFS mismatch')
        boundary = self.dispatches[index]['apply_ns']
        if (type(uncertainty_ns) is not int or uncertainty_ns < 0 or type(verified_ns) is not int
                or abs(verified_ns-boundary)+uncertainty_ns > 100_000_000):
            raise ValueError('100 ms readback boundary gate')
        if (write_call_ns is None) != (write_return_ns is None):
            raise ValueError('incomplete hardware write interval')
        if write_call_ns is not None and not boundary-100_000_000 <= write_call_ns <= write_return_ns <= verified_ns:
            raise ValueError('hardware write interval boundary gate')
        self.receipts.setdefault(index, {})[role] = {
            'write_call_ns': write_call_ns, 'write_return_ns': write_return_ns,
            'verified_ns': verified_ns, 'uncertainty_ns': uncertainty_ns, 'readback': dict(readback),
            'clock': 'controller axis, converted with archived node offset/uncertainty'}

    def observation(self, index, report, background_Mbps, received_ns, evidence):
        """Report uses sender monotonic time; only cohort duration is compared.

        All 500 planned generations, including local misses, must be present.
        start/end/asof are mapped to common T0 by the recorded monotonic anchor;
        cross-host subtraction is never used to compute application RTT.
        """
        if set(self.receipts.get(index, {})) != set(ROLES):
            raise ValueError('all four hardware readbacks required before learning')
        decision = self.dispatches[index]
        window = report['window']
        if (not report['complete_cohort'] or report['deadline_ns'] != 40_000_000 or
                window['end_ns']-window['start_ns'] != 5*NS):
            raise ValueError('deadline/full action cohort mismatch')
        # Explicit anchor is mandatory; it prevents substituting the latest
        # five-second report for the requested action's generation cohort.
        mapped_start = report['common_T0_ns'] + window['start_ns']-report['sender_T0_monotonic_ns']
        if mapped_start != decision['apply_ns']:
            raise ValueError('sender cohort does not map to requested action boundary')
        row = {'index': index, 'source': self.core.source, 'valid': True, 'action': decision['action'],
               'start_ns': mapped_start, 'end_ns': mapped_start+5*NS,
               'as_of_ns': mapped_start+window['as_of_ns']-window['start_ns'],
               'received_ns': received_ns, 'deadline_ns': report['deadline_ns'],
               'hardware_readback_confirmed': True, 'counts': report['counts'],
               'rtt_reply_samples': report['rtt_reply_samples'], 'rtt_mean_ms': report['rtt_mean_ms'],
               'background_Mbps': background_Mbps, 'evidence': evidence}
        validate_feedback(row)
        try:
            self.core.receive(row)
        except PerformanceStop:
            self.fallback_requested = True
            raise  # Supervisor must revert P at next owned boundary, restore and finish.
        return row


def may_start_run(plan, elapsed_s, estimated_run_and_collection_s):
    """Never consume the recovery reserve to start another run."""
    remaining = plan['stop']['field_budget_s']-elapsed_s-plan['stop']['recovery_reserve_s']
    return remaining >= estimated_run_and_collection_s
