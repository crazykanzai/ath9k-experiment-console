"""Indexed immutable cohort snapshots; original RTT/count semantics unchanged.

Portable into the private Linux bundle. No sockets or OS scheduling changes.
Only the bounded worker uses this collector; historical readers stay unchanged.
"""
from bisect import bisect_left
import threading

try:
    from ..rtt import ProbeCollector, integer
except ImportError:  # Explicit flat, private node bundle.
    from rtt import ProbeCollector, integer


class IndexedCollector(ProbeCollector):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock = threading.RLock()
        self.generation_times = []
        self.generation_sequences = []
        self.last_snapshot_rows = 0

    def _time(self, now):
        with self.lock:
            return super()._time(now)

    def generate(self, seq, now_ns, scheduled_ns=None):
        with self.lock:
            at = integer(now_ns if scheduled_ns is None else scheduled_ns, 'scheduled_ns')
            if self.generation_times and at < self.generation_times[-1]:
                raise ValueError('bounded generator scheduled time regressed')
            packet = super().generate(seq, now_ns, scheduled_ns)
            self.generation_times.append(at)
            self.generation_sequences.append(seq)
            return packet

    def submitted(self, *args, **kwargs):
        with self.lock:
            return super().submitted(*args, **kwargs)

    def send_failed(self, *args, **kwargs):
        with self.lock:
            return super().send_failed(*args, **kwargs)

    def receive(self, *args, **kwargs):
        with self.lock:
            return super().receive(*args, **kwargs)

    def snapshot(self, start_ns, end_ns):
        """Caller owns lock; copy only the requested cohort, not all history."""
        integer(start_ns, 'start_ns'); integer(end_ns, 'end_ns')
        lo = bisect_left(self.generation_times, start_ns)
        hi = bisect_left(self.generation_times, end_ns)
        view = ProbeCollector.__new__(ProbeCollector)
        for name in ('run_id', 'node_id', 'deadline_ns', 'payload_bytes', 'last_time'):
            setattr(view, name, getattr(self, name))
        view.rows = {seq: dict(self.rows[seq]) for seq in self.generation_sequences[lo:hi]}
        self.last_snapshot_rows = hi-lo
        return view

    def report(self, start_ns, end_ns, as_of_ns):
        with self.lock:
            view = self.snapshot(start_ns, end_ns)
        # Aggregation, sorting, quantiles and ledger formatting run OUTSIDE the
        # mutation lock. The snapshot cannot gain future replies afterwards.
        return ProbeCollector.report(view, start_ns, end_ns, as_of_ns)

    def report_now(self, start_ns, end_ns, clock_ns):
        with self.lock:
            as_of_ns = clock_ns()  # Evaluate after lock, avoiding a time race.
            view = self.snapshot(start_ns, end_ns)
        return ProbeCollector.report(view, start_ns, end_ns, as_of_ns)
