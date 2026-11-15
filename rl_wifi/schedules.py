"""Pure, bounded message plans. No sockets, threads or hardware operations."""
from dataclasses import dataclass, asdict

try:
    from .rtt import integer
except ImportError:  # Private flat node bundle, only when explicitly deployed.
    from rtt import integer


@dataclass(frozen=True)
class MessageSchedule:
    mode: str
    period_ns: int
    batch_size: int
    lateness_budget_ns: int = 10_000_000

    def __post_init__(self):
        if self.mode not in ('uniform','burst'):
            raise ValueError('unknown generation mode')
        integer(self.period_ns,'period_ns',1)
        integer(self.batch_size,'batch_size',1)
        integer(self.lateness_budget_ns,'lateness_budget_ns',1)
        if self.mode=='uniform' and self.batch_size!=1:
            raise ValueError('uniform requires one message per period')

    def offset_ns(self,seq):
        return integer(seq,'seq')//self.batch_size*self.period_ns

    def count(self,duration_ns):
        integer(duration_ns,'duration_ns',1)
        return ((duration_ns-1)//self.period_ns+1)*self.batch_size

    def as_dict(self):
        return asdict(self)


UNIFORM = MessageSchedule('uniform',10_000_000,1)
BURST = MessageSchedule('burst',100_000_000,10)

MIXED_STATES = ('uniform', 'burst', 'uniform', 'burst')


class MixedSchedule:
    """Uniform warmup, four 15 s segments, then a burst tail."""

    lateness_budget_ns = UNIFORM.lateness_budget_ns

    def offset_ns(self, seq):
        integer(seq, 'seq')
        if seq < 500:
            return UNIFORM.offset_ns(seq)
        index, relative = divmod(seq - 500, 1500)
        state = MIXED_STATES[min(index, 3)]
        schedule = UNIFORM if state == 'uniform' else BURST
        return 5_000_000_000 + index * 15_000_000_000 + schedule.offset_ns(relative)

    def count(self, duration_ns):
        if duration_ns != 70_000_000_000:
            raise ValueError('mixed schedule requires exactly 70 seconds')
        return 7000

    def as_dict(self):
        return {'mode': 'mixed', 'states': list(MIXED_STATES), 'segment_s': 15}


MIXED = MixedSchedule()
