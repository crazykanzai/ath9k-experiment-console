"""Prepared Linux-only worker, deployed only in an explicitly owned private bundle.

Imports the already deployed legacy helpers from that bundle. No importing or
running this file is required by the offline CLI. The old worker is unchanged.
"""
import json
import threading
import time

import live_node
from live_node import LiveWorker
from node_schedule import Schedule
from sender import sender as bounded_sender, echo_loop as bounded_echo

NS = 1_000_000_000


class BoundedWorker(LiveWorker):
    def __init__(self, config):
        if (config.get('bounded40_schema') != 'dqn40-three-actions-v1' or config['deadline_ns'] != 40_000_000
                or config['warmup_s'] != 5 or config['tail_s'] != 5 or config['segment_s'] != 5
                or config['measure_s'] not in (30, 60, 180) or config['guard_s'] != 3):
            raise ValueError('bounded40 explicit timing/deadline contract')
        self.schedule = Schedule(config['application_states'])
        if self.schedule.measure_s != config['measure_s']:
            raise ValueError('business script length mismatch')
        # Only the subclass bypasses the OLD timing whitelist. No shared gate is
        # relaxed; the actual new profile is validated above before construction.
        super().__init__({**config, 'warmup_s': 5, 'measure_s': 60, 'tail_s': 5,
                          'segment_s': 15, 'generation_mode': 'mixed'})
        self.c = {**config, 'generation_mode': 'mixed'}
        self.total = self.schedule.total_s
        # Each existing worker has its own process. Replace this process-local
        # schedule binding so legacy sender/setup keep the existing UDP ledger.
        live_node.SCHEDULES['mixed'] = self.schedule

    def sample(self, number):
        # Keep original hardware/queue samples, but replace the old full-history
        # app report under probe_lock with a short immutable indexed snapshot.
        live_node.Worker.sample(self, number)
        if self.probe_error:
            raise RuntimeError(self.probe_error)
        if self.role == self.c['foreground'] and self.probe_collector is not None:
            start = self.clock_anchor+(number-1)*NS
            report = self.probe_collector.report_now(start, start+NS, time.monotonic_ns)
            report.pop('ledger')
            self.emit('application_sample', second=number, report=report)
        if (self.role != self.c['foreground'] or self.probe_collector is None or number <= 5
                or (number-5) % 5 or number > 5+self.c['measure_s']):
            return
        index = (number-5)//5-1
        start = self.clock_anchor+(number-5)*NS
        report = self.probe_collector.report_now(start, start+5*NS, time.monotonic_ns)
        report.pop('ledger')
        report.update(common_T0_ns=self.plan['T0_ns'], sender_T0_monotonic_ns=self.clock_anchor)
        self.emit('bounded40_cohort', index=index, report=report)

    def sender(self, start_mono):
        bounded_sender(self, start_mono)

    def echo_loop(self):
        bounded_echo(self)

    def run(self):
        threading.Thread(target=self.control_reader, daemon=True).start()
        self.setup()
        deadline = time.monotonic()+45
        while not self.plan_ready.wait(.05):
            self.check()
            if time.monotonic() > deadline:
                raise RuntimeError('plan timeout')
        t0 = self.plan['T0_ns']
        events = [(t0, 0, 'launch', 0)]
        if self.role == self.c['foreground']:
            for phase, state in enumerate(self.schedule.states):
                boundary = t0+(5+15*phase)*NS
                events.append((boundary-6*NS, 0, 'notice', phase))
        cycles = self.c['measure_s']//5
        if self.role.startswith('STA'):
            for k in range(cycles+1):
                at = t0+(5+k*5)*NS
                events.append((at, 0, 'boundary', k))
                if k < cycles:
                    start = at+3*NS
                    events.append((start, 1, 'stats', k))
                    for ms in (50, 150, 500, 1000, 1500, 2000, 2050, 2200):
                        events.append((start+ms*1_000_000, 1, 'latch', k))
        events += [(t0+n*NS+250_000_000, 2, 'sample', n) for n in range(1, self.total)]
        for at, _, kind, k in sorted(events):
            self.wait(at)
            if kind == 'notice':
                self.emit('application_state_notice', phase=k, state=self.schedule.states[k],
                          apply_ns=t0+(5+15*k)*NS)
            elif kind == 'launch':
                if self.role == self.c['foreground']:
                    start = time.monotonic_ns()+(t0-time.time_ns())
                    self.emit('application_launch_call', planned_ns=t0)
                    self.probe_thread = threading.Thread(target=self.sender, args=(start,), daemon=True)
                    self.probe_thread.start()
                elif self.role.startswith('STA'):
                    receipt = self.emit('iperf_launch_call', planned_ns=t0)
                    self.launch('iperf_client', ['iperf3', '-c', self.c['ap_ip'], '-B', self.c['own_ip'],
                        '-p', str(self.c['port']), '-u', '-b', '30M', '-l', '1200',
                        '-t', str(self.total+10), '-J', '--logfile', str(self.out/'iperf_client.json')])
                    if abs(receipt['wall_ns']-t0) > 100_000_000:
                        raise RuntimeError('100ms background start gate')
            elif kind == 'boundary':
                self.boundary(k, at)
            elif kind == 'stats':
                self.stats_start(k, at)
            elif kind == 'latch':
                # The next boundary may have closed this short auxiliary driver
                # window; never latch an old segment into a newer generation.
                if self.window is not None and self.window['index'] == k:
                    self.latch_signature()
            else:
                self.sample(k)
        self.wait(t0+self.total*NS)
        if self.role == self.c['foreground']:
            self.emit('application_generation_end_boundary', planned_ns=t0+self.total*NS)
            self.probe_thread.join(2.04)
            if self.probe_thread.is_alive() or self.probe_error or not self.probe_finished or not self.probe_finished['collection_complete']:
                raise RuntimeError('application completion gate: '+str(self.probe_error))
        elif self.role.startswith('STA'):
            self.emit('iperf_stop_call', planned_ns=t0+self.total*NS)
            self.processes['iperf_client'][0].send_signal(__import__('signal').SIGINT)
        else:
            self.emit('absolute_end', planned_ns=t0+self.total*NS)
        self.state['completed_schedule'] = True
        self.save()


if __name__ == '__main__':
    import sys
    print('ATH9K_BOUNDED40_READY', flush=True)
    config = json.loads(sys.stdin.readline())
    worker = None
    try:
        worker = BoundedWorker(config)
        worker.run()
    except Exception as exc:
        if worker:
            worker.state['error'] = str(exc)
            worker.emit('failure', message=str(exc))
        else:
            raise
    finally:
        if worker:
            worker.cleanup()
