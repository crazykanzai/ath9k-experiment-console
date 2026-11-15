"""Prepared five-second adapter to the existing owned hardware session.

No CLI invokes this module. Importing it does not connect to a node. The future
authorized supervisor must supply a freshly accepted preparation/SSH backend,
field deadline and restoration budget. This path has NO hardware acceptance yet.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import shlex
import threading
import time
import traceback

from labconsole.storage import atomic_json
from labconsole.cw_policy import air_mode_issue
from labconsole.demo_analysis import envelope
from labconsole.real_demo import PORTS, STAS, mac
from labconsole.preparation import ROLES
from ..live import PilotSession, RLRemote
from ..quick_screen import QuickSession, ROOT
from ..quick_metrics import boundary_audit
from ..rtt import replay_events, quantile
from .bridge import Bridge
from .contract import ACTIONS, SCHEMA
from .controller import Controller, PerformanceStop
from .plan import Schedule
from .transport import heartbeat, await_clean_exits

NS = 1_000_000_000


class Remote(RLRemote):
    def launch(self, config, files):
        client = self.backend.client(self.role)
        with client.open_sftp() as sftp:
            self.directory = config['output']
            sftp.mkdir(self.directory.rsplit('/', 1)[0], 0o700)
            sftp.mkdir(self.directory, 0o700)
            for src in files:
                sftp.put(str(src), self.directory+'/'+src.name)
        self.channel = client.exec_command('sudo -S -k -p "" env LC_ALL=C python3 -u '+
                                          shlex.quote(self.directory+'/node.py'), timeout=48)
        stdin, out, _ = self.channel
        stdin.write(self.backend.sudo_password+'\n')
        stdin.flush()
        if out.readline().strip() != 'ATH9K_BOUNDED40_READY':
            raise RuntimeError(self.role+' bounded40 handshake failed')
        self.send(config)
        thread = threading.Thread(target=self.read, daemon=True)
        thread.start()
        return thread


class PreparedSession(QuickSession):
    def __init__(self, folder, preparation, request, entry, model=None):
        if request.get('authorization') != 'explicit_current_live_authorization_required':
            raise ValueError('no live authorization marker; offline CLI never supplies this')
        if not request.get('field_stop_monotonic') or time.monotonic()+180 >= request['field_stop_monotonic']:
            raise ValueError('field deadline/restoration reserve required')
        self.schedule = Schedule(entry['states'])
        if request['measure_s'] != self.schedule.measure_s or request['segment_s'] != 5 or request['deadline_ns'] != 40_000_000:
            raise ValueError('sealed five-second timing required')
        policy = 'DQN_train' if entry.get('phase') == 'train' else ('acceptance' if entry.get('phase') == 'acceptance' else entry['policy'])
        self.policy_name, self.model = policy, model
        initial = {'P': 2, 'E': 0, 'middle': 1}.get(self.policy_name, 2)
        if self.policy_name == 'rule':
            initial = 2 if entry['states'][0] == 'burst' else 0
        self.initial_action = initial
        entry = {**entry, 'W': {n: 16 if n == 'STA1' else ACTIONS[initial] for n in STAS}}
        super().__init__(folder, preparation, request, entry)
        self.notices, self.cohorts = {}, {}
        self.bridge = self.core = None
        self.state.update(source='live_bounded40_prepared_adapter', hardware_adapter_accepted=False,
                          policy=self.policy_name, notices=[], delayed_cohorts=[])

    def context(self, at_ns):
        # Only notices actually received from STA1, never indexing the script to
        # give the neural network future phases or elapsed experiment time.
        current = [r for r in self.notices.values() if r['apply_ns'] <= time.time_ns()]
        target = [r for r in self.notices.values() if r['apply_ns'] <= at_ns]
        return {'state': max(current, key=lambda r:r['apply_ns'])['state'] if current else 'uniform',
                'announced': max(target, key=lambda r:r['apply_ns'])['state'] if target else None}

    def receive(self, timeout=.15, strict=True):
        role, event = super().receive(timeout, strict)
        if not strict or not event or self.bridge is None:
            return role, event
        kind = event['event']
        if kind == 'eof':
            self.state.setdefault('process_exit_codes', {})[role] = event['exit_code']
        elif kind == 'application_state_notice':
            phase = event['phase']
            if (role != 'STA1' or phase in self.notices or not 0 <= phase < len(self.schedule.states)
                    or event['state'] != self.schedule.states[phase]
                    or event['apply_ns'] != self.state['T0_ns']+(5+15*phase)*NS):
                raise RuntimeError('application notice does not match sealed script')
            self.notices[phase] = {**event, 'received_ns': time.time_ns()}
            self.state['notices'].append(self.notices[phase])
        elif kind == 'cw_verified' and event.get('index') is not None:
            i = event['index']
            clock = self.state['clock_checks'][-1][role]
            offset = round(clock['offset_ms']*1e6)
            convert = lambda value: None if value is None else value-offset
            self.bridge.readback(i, role, event['readback'], convert(event.get('write_call_ns')),
                                 convert(event.get('write_return_ns')), convert(event['wall_ns']),
                                 round(clock['uncertainty_ms']*1e6))
        elif kind == 'bounded40_cohort':
            if role != 'STA1' or event['index'] in self.cohorts:
                raise RuntimeError('duplicate or wrong-source cohort')
            self.cohorts[event['index']] = event['report']
            self.state['delayed_cohorts'].append({**event, 'controller_received_ns': time.time_ns()})
        self.drain_cohorts()
        return role, event

    def heartbeat(self):
        heartbeat(self)

    def drain_cohorts(self):
        i = len(self.core.feedbacks)
        while i in self.cohorts:
            end = 10+i*5
            seconds = range(end-4, end+1)
            if (set(self.bridge.receipts.get(i, {})) != set(STAS)
                    or not all('AP' in self.samples[s] for s in seconds)):
                return
            byte_count = sum(sum(self.samples[s]['AP']['data']['udp_buckets'].get(str(s-1), {}).get(str(PORTS[n]), 0)
                                 for n in STAS[1:]) for s in seconds)
            try:
                self.bridge.observation(i, self.cohorts[i], byte_count*8/5/1e6, time.time_ns(),
                    {'application': 'raw/STA1/worker_events.jsonl:bounded40_cohort',
                     'background': 'raw/AP/worker_events.jsonl:sample', 'index': i})
            except PerformanceStop as exc:
                self.state['performance_stop'] = str(exc)
                # Finish this bounded window on P, with all remaining data and
                # fallback costs retained. The supervisor ends training here.
            i += 1

    def payload(self):
        files = [ROOT/'labconsole'/n for n in ('demo_node.py', 'cw_policy.py', 'readonly_probe.py')]
        files += [ROOT/'experiments'/n for n in ('reg_access.py', 'cohort_node_capture.py', 'air_timing.py')]
        files += [ROOT/'rl_wifi'/n for n in ('live_node.py', 'rtt.py', 'probe_transport.py', 'schedules.py')]
        files += [Path(__file__).with_name(n) for n in
                  ('node.py', 'node_schedule.py', 'collector.py', 'event_journal.py', 'sender.py')]
        return files

    def traffic(self, rows, ips):
        try:
            return self._traffic(rows, ips)
        except Exception:
            self.state['failure_traceback'] = traceback.format_exc()
            self.save()
            raise

    def _traffic(self, rows, ips):
        specs = self.trace_specs(rows, ips)
        files = self.payload()
        atomic_json(self.folder/'payload_hashes.json', {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
        macs = {n: mac(rows[n]['info']) for n in STAS}
        self.state['macs'] = {'AP': mac(rows['AP']['info']), **macs}
        self.workers = {r: Remote(r, self.backend, self.messages, self.folder/'raw'/r) for r in ROLES}

        def launch(role):
            config = {**self.request, 'role': role, 'id': self.id,
                'output': '/tmp/ath9k-demo-'+self.id+'/'+role, 'bounded40_schema': SCHEMA,
                'application_states': list(self.schedule.states), 'generation_mode': 'mixed',
                'initial_w': self.stages[0].get(role, 16), 'interface': rows[role]['interface'],
                'phy': rows[role]['phy'], 'ap_ip': ips['AP'], 'ap_mac': self.state['macs']['AP'],
                'sta_macs': macs, 'sta_ports': PORTS, 'ports': list(PORTS.values()),
                'port': PORTS.get(role), 'own_ip': ips.get(role), 'trace_spec': specs.get(role),
                'foreground': 'STA1', 'foreground_ip': ips['STA1'], 'probe_port': PORTS['STA1'],
                'deadline_ns': 40_000_000, 'probe_interval_ns': 10_000_000}
            return self.workers[role].launch(config, files)

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(launch, ROLES))
        self.last_heartbeat = 0
        self.state['originals'] = self.await_group('armed', ROLES, 24)
        self.heartbeat()
        self.state['clock_pre_arm'] = self.clock()
        t0 = time.time_ns()+20*NS
        self.core = Controller(self.policy_name, self.id, t0, self.schedule.measure_s//5,
                               'live_hardware', self.model)
        self.bridge = Bridge(self.core, self.send_stage, self.initial_action)
        self.state.update(T0_ns=t0, status='armed', traffic_started=False)
        self.save()
        self.send_all({'op': 'plan', 'T0_ns': t0,
                      'ap_clock_offset_ns': round(self.state['clock_pre_arm']['AP']['offset_ms']*1e6)})
        self.await_group('plan_received', ROLES, 7)
        acknowledgements = {i: set() for i in range(1, self.core.cycles)}
        clock_due = time.monotonic()+10
        while len(self.finished) < len(ROLES) and time.time_ns() < t0+(self.schedule.total_s+45)*NS:
            self.check_cancel()
            self.heartbeat()
            role, event = self.receive()
            now = time.time_ns()
            if time.monotonic()+180 >= self.request['field_stop_monotonic']:
                raise RuntimeError('field budget recovery reserve reached')
            if event and event['event'] == 'stage_received':
                k = event['index']
                if (k not in self.sent or role not in STAS or event['W'] != self.stages[k][role]
                        or event['apply_ns'] != t0+(5+k*5)*NS):
                    raise RuntimeError('stage acknowledgement mismatch')
                acknowledgements[k].add(role)
            for k in range(self.core.cycles):
                at = t0+(5+k*5)*NS
                if k not in self.sent and at-now <= 4*NS:
                    previous = self.core.decisions[k-1]['action'] if k else self.initial_action
                    decision = self.bridge.dispatch(k, time.time_ns(), self.context(at), previous)
                    self.stages[k] = {n: t['W'] for n, t in decision['targets'].items()}
                    self.sent.add(k)
                    self.event('bounded40_decision', decision['reason'], **{n:v for n,v in decision.items() if n!='reason'})
                if k and k in self.sent and at-now < NS and acknowledgements[k] != set(STAS):
                    raise RuntimeError('stage acknowledgement incomplete')
            if time.monotonic() >= clock_due:
                self.clock()
                clock_due = time.monotonic()+10
        if set(self.finished) != set(ROLES) or any(v.get('error') or not v.get('completed_schedule') for v in self.finished.values()):
            raise RuntimeError('final node schedule receipts incomplete')
        await_clean_exits(self)
        self.drain_cohorts()
        self.state['clock_after'] = self.clock()
        self.state['applied_W'] = self.stages
        self.save()

    def acceptance(self):
        issues = PilotSession.acceptance(self)
        if not issues:
            raw = self.folder/'raw/STA1'
            collector = replay_events(raw/'application_probe.jsonl')
            final = json.loads((raw/'application_final.json').read_text(encoding='utf-8'))
            anchor = json.loads((raw/'application_clock_anchor.json').read_text())['start_monotonic_ns']
            expected = self.schedule.count(self.schedule.total_s*NS)
            if collector.deadline_ns != 40_000_000 or set(collector.rows) != set(range(expected)):
                issues.append('complete planned message identities missing')
            else:
                if any(r['generated_ns'] != anchor+self.schedule.offset_ns(i) for i,r in collector.rows.items()):
                    issues.append('planned generation timeline mismatch')
                report = collector.report(anchor, anchor+self.schedule.total_s*NS, final['report']['window']['as_of_ns'])
                if report['counts'] != final['report']['counts'] or report['ledger'] != final['report']['ledger']:
                    issues.append('application final ledger replay mismatch')
                common = collector.report(anchor+5*NS, anchor+(5+self.schedule.measure_s)*NS,
                                          final['report']['window']['as_of_ns'])
                delays = [(r['observed_generation_ns']-r['generated_ns'])/1e6 for r in common['ledger']]
                if quantile(delays, .99) > 10 or common['counts']['local_failure'] > self.schedule.measure_s:
                    issues.append('generator quality p99>10ms or local failure>1%')
                # Count by planned generation state; retain switching seconds.
                states = {s: {'mature': 0, 'on_time': 0, 'rtt_reply_samples': 0,
                              'local_failure': 0, 'late': 0, 'no_reply': 0} for s in ('uniform', 'burst')}
                segments = []
                for phase, mode in enumerate(self.schedule.states):
                    begin = anchor+(5+15*phase)*NS
                    r = collector.report(begin, begin+15*NS, final['report']['window']['as_of_ns'])
                    r.pop('ledger')
                    segments.append({'state': mode, 'report': r})
                    for field in ('mature', 'on_time', 'local_failure', 'late', 'no_reply'):
                        states[mode][field] += r['counts'][field]
                    states[mode]['rtt_reply_samples'] += r['rtt_reply_samples']
                common.pop('ledger')
                self.state['metrics'] = {'application': common, 'states': states, 'segments': segments,
                                         'generation_delay_p99_ms': quantile(delays, .99)}
            bounds = boundary_audit(self.state)
            if any(r['envelope_ms'] > 100 for values in bounds.values() for r in values):
                issues.append('100ms full boundary audit')
            if self.core is None or len(self.core.feedbacks) != self.core.cycles:
                issues.append('full delayed cohorts incomplete')
            final_nodes = self.state['final_nodes']
            if final_nodes['AP'].get('packet_socket_statistics', [None, None])[1] != 0:
                issues.append('AP packet sampler drop gate')
            air = final_nodes['MON15'].get('air_audit', {})
            if set(air) != set(STAS) or any(air_mode_issue(a, 'basic') for a in air.values()):
                issues.append('Basic / DATA24 air gate')
            for role in STAS:
                def native(text):
                    line = next(v for v in text.splitlines() if 'FIFO Underrun' in v)
                    return sum(map(int, re.findall(r'\d+', line)))
                own = final_nodes[role]
                if native(own['xmit_before']) != native(own['xmit_after']):
                    issues.append(role+' FIFO delta gate')
                if not (self.folder/'raw'/role/'trace_stats.txt').is_file():
                    issues.append(role+' trace loss receipt missing')
                if role != 'STA1':
                    iperf = json.loads((self.folder/'raw'/role/'iperf_client.json').read_text())
                    if (own['processes']['iperf_client']['exit_code'] != 1 or
                            iperf.get('error') != 'interrupt - the client has terminated' or
                            len(iperf.get('intervals', [])) < self.schedule.measure_s):
                        issues.append(role+' background iperf completion')
            if set(self.notices) != set(range(len(self.schedule.states))):
                issues.append('application notice receipts incomplete')
            from labconsole.real_demo import import_reference
            import_reference()
            from analyze_sync import pcap_udp
            off, unc = envelope(self.state['clock_checks'], 'AP')
            packets = pcap_udp(self.folder/'raw/AP/ap_udp.pcap', set(PORTS.values()))
            begin = self.state['T0_ns']+5*NS
            bins = [0]*self.schedule.measure_s
            for role in STAS[1:]:
                for stamp, size in packets[PORTS[role]]:
                    second = (stamp-off-begin)//NS
                    if 0 <= second < len(bins) and size == 1200:
                        bins[second] += size
            self.state.setdefault('metrics', {}).update(background_Mbps=sum(bins)*8/(len(bins)*1e6),
                background_Mbps_1s=[v*8/1e6 for v in bins], AP_clock_uncertainty_ms=unc/1e6,
                clock_boundaries=bounds, collection_valid=not issues)
        self.state['hardware_adapter_accepted'] = not issues
        self.state['acceptance'].update(passed=not issues, issues=issues)
        self.save()
        return issues

    def run(self):
        try:
            super().run()
        finally:
            if self.core is not None and not self.core.closed:
                restored = bool((self.state.get('restoration') or {}).get('confirmed'))
                passed = self.state['status'] == 'completed' and self.state.get('acceptance', {}).get('passed') is True
                self.state['learning_run'] = self.core.close(passed, restored, time.time_ns(), self.context(time.time_ns()))
                self.save()
