"""Portable pure schedule, also copied into the private Linux node bundle."""
NS = 1_000_000_000


class Schedule:
    lateness_budget_ns = 10_000_000

    def __init__(self, states):
        if not states or any(s not in ('uniform', 'burst') for s in states):
            raise ValueError('explicit business states required')
        self.states = tuple(states)
        self.measure_s = 15*len(states)
        self.total_s = 10+self.measure_s

    def state_at(self, offset_ns):
        if type(offset_ns) is not int or not 0 <= offset_ns < self.total_s*NS:
            raise ValueError('offset outside generation plan')
        if offset_ns < 5*NS:
            return 'uniform'
        return self.states[min((offset_ns-5*NS)//(15*NS), len(self.states)-1)]

    def offset_ns(self, seq):
        if type(seq) is not int or not 0 <= seq <= self.total_s*100:
            raise ValueError('sequence outside plan')
        if seq == self.total_s*100:
            return self.total_s*NS  # Generator sentinel.
        if seq < 500:
            return seq*10_000_000
        if seq < 500+self.measure_s*100:
            phase, relative = divmod(seq-500, 1500)
            base = (5+phase*15)*NS
            return base+(relative*10_000_000 if self.states[phase] == 'uniform' else relative//10*100_000_000)
        relative = seq-500-self.measure_s*100
        base = (5+self.measure_s)*NS
        return base+(relative*10_000_000 if self.states[-1] == 'uniform' else relative//10*100_000_000)

    def count(self, duration_ns):
        if duration_ns != self.total_s*NS:
            raise ValueError('duration must match sealed schedule')
        return self.total_s*100

    def as_dict(self):
        return {'mode': 'bounded40_mixed', 'states': list(self.states), 'state_duration_s': 15,
                'warmup_s': 5, 'tail_s': 5, 'lateness_budget_ns': self.lateness_budget_ns}

