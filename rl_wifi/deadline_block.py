"""One predeclared application-state CW comparison on the four-STA testbed."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from labconsole.cw_policy import air_mode_issue
from labconsole.demo_analysis import envelope
from labconsole.real_demo import NS, PORTS, STAS, import_reference
from labconsole.storage import atomic_json

from .quick_metrics import boundary_audit, load
from .quick_retry import PREPARATION_TIMING, can_retry, verify_plan
from .quick_screen import QuickSession, ROOT, SOURCES, digest, execute, password_gui
from .rtt import quantile, replay_events
from .schedules import MIXED, MIXED_STATES


ORDER = ('P', 'dynamic', 'middle', 'E')
DEADLINE_NS = 40_000_000
TIMING = {'warmup_s': 5, 'measure_s': 60, 'tail_s': 5, 'segment_s': 15}


def targets(policy, state):
    if policy == 'dynamic':
        policy = 'P' if state == 'burst' else 'E'
    background = {'P': 64, 'E': 16, 'middle': 32}[policy]
    return {role: 16 if role == 'STA1' else background for role in STAS}


def make_plan():
    return {
        'schema': 'application_state_cw_40ms_v1', 'preparation_timing': list(PREPARATION_TIMING),
        'plan': [{'slot': i, 'id': uuid.uuid4().hex, 'policy': policy, 'generation_mode': 'mixed',
                  'W': targets(policy, 'uniform')} for i, policy in enumerate(ORDER, 1)],
        'timing': TIMING, 'arrival': MIXED.as_dict(), 'states': list(MIXED_STATES),
        'arrival_profiles': {'uniform': '1 message every 10 ms', 'burst': '10 messages every 100 ms'},
        'payload_bytes': 1200, 'background_Mbps_per_STA': 30, 'mode': 'Basic', 'DATA_Mbps': 24,
        'deadline_ns': DEADLINE_NS, 'minimum_on_time': .95, 'minimum_gain': .05,
        'best_fixed': 'maximum 60 s throughput among fixed policies passing both states',
        'notice_lead_seconds': 6, 'clock_policy': 'quick8-initial50-continuous100-v2',
        'max_attempts_per_run': 2, 'field_budget_s': 1500,
        'sources': {name: digest(ROOT/name) for name in (*SOURCES, 'rl_wifi/deadline_block.py', 'rl_wifi/deadline_report.py')},
    }


def seal(folder, plan):
    sealed = folder/'sealed'
    sealed.mkdir(parents=True)
    (folder/'controller').mkdir()
    atomic_json(sealed/'plan.json', plan)
    (sealed/'plan.sha256').write_text(digest(sealed/'plan.json') + '  plan.json\n')
    for name in plan['sources']:
        target = sealed/'source'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/name, target)
    shutil.copyfile(ROOT/'EXPERIMENT_RULES.md', sealed/'rules.md')


def application_metrics(raw):
    archived = load(raw/'application_final.json')
    old = archived['report']
    anchor, asof = (old['window'][key] for key in ('start_ns', 'as_of_ns'))
    collector = replay_events(raw/'application_probe.jsonl')
    final = collector.report(anchor, anchor+70*NS, asof)
    planned = [json.loads(line) for line in (raw/'application_plan.jsonl').read_text().splitlines()]
    if (not archived['collection_complete'] or collector.deadline_ns != DEADLINE_NS
            or old['ledger'] != final['ledger'] or old['counts'] != final['counts']
            or len(planned) != 7000 or set(collector.rows) != set(range(7000))):
        raise RuntimeError('40 ms message ledger/plan replay mismatch')
    for seq, row in enumerate(planned):
        offset = MIXED.offset_ns(seq)
        if (row['seq'] != seq or row['T0_offset_ns'] != offset or
                row['deadline_T0_offset_ns'] != offset+DEADLINE_NS or
                collector.rows[seq]['generated_ns'] != anchor+offset):
            raise RuntimeError('planned generation schedule mismatch')

    def segment(begin, end):
        value = collector.report(anchor+begin*NS, anchor+end*NS, asof)
        counts = value['counts']
        if counts['mature'] != 100*(end-begin) or counts['pending']:
            raise RuntimeError('incomplete fixed generation cohort')
        ledger = value.pop('ledger')
        value['generation_delay_p99_ms'] = quantile([
            (row['observed_generation_ns']-row['generated_ns'])/1e6 for row in ledger], .99)
        value['local_failure_reasons'] = dict(Counter(
            row['local_failure'] for row in ledger if row['local_failure']))
        return value

    segments = [segment(start, start+15) for start in (5, 20, 35, 50)]
    common = segment(5, 65)
    states = {}
    for mode in ('uniform', 'burst'):
        matching = [row for state, row in zip(MIXED_STATES, segments) if state == mode]
        counts = Counter()
        for row in matching:
            counts.update(row['counts'])
        states[mode] = {'counts': dict(counts), 'on_time_ratio': counts['on_time']/counts['mature']}
    return {
        'states': states, 'segments': segments, 'common': common,
        'on_time_1s': [segment(s, s+1)['on_time_ratio'] for s in range(5, 65)],
        'generator_quality_passed': common['generation_delay_p99_ms'] <= 10
        and common['counts']['local_failure'] <= 60,
    }


class DeadlineSession(QuickSession):
    def __init__(self, folder, prep, request, entry):
        super().__init__(folder, prep, request, entry)
        self.policy_name = entry['policy']
        self.notices = {}
        self.state.update(source='live_40ms_fixed_vs_dynamic', application_state_notices=[])

    def receive(self, timeout=.15, strict=True):
        role, event = super().receive(timeout, strict)
        if strict and event and event['event']=='application_state_notice':
            index = event['index']
            if (role != 'STA1' or index not in (1, 2, 3) or index in self.notices
                    or event['state'] != MIXED_STATES[index]
                    or event['apply_ns'] != self.state['T0_ns'] + (5+index*15)*NS):
                raise RuntimeError('invalid application state notice')
            receipt = {**event, 'controller_received_ns': time.time_ns()}
            self.notices[index] = receipt
            self.state['application_state_notices'].append(receipt)
            self.save()
        return role, event

    def online_choice(self, index):
        if self.policy_name == 'dynamic':
            notice = self.notices.get(index)
            if not notice:
                raise RuntimeError('application state notice missing before CW dispatch')
            return targets(self.policy_name, notice['state']), 'application state notice'
        return self.entry['W'], 'fixed CW control'

    def acceptance(self):
        # The parent of QuickSession checks queues, capture, and trace receipts.
        from .live import PilotSession
        issues = PilotSession.acceptance(self)
        if not issues:
            self.state['metrics'] = self.measure()
            issues.extend(self.state['metrics']['issues'])
        self.state['acceptance'].update(passed=not issues, issues=issues)
        self.save()
        return issues

    def measure(self):
        import_reference()
        from analyze_sync import pcap_udp

        raw = self.folder/'raw'
        state = self.state
        app = application_metrics(raw/'STA1')
        issues = []
        bounds = boundary_audit(state)
        if any(row['envelope_ms'] > 100 for values in bounds.values() for row in values):
            issues.append('100 ms boundary envelope gate')
        final = state['final_nodes']
        if final['AP'].get('packet_socket_statistics', [None,None])[1] != 0:
            issues.append('AP live packet sampler drop gate')
        air = final['MON15'].get('air_audit', {})
        if set(air) != set(STAS) or any(air_mode_issue(a, 'basic') for a in air.values()):
            issues.append('Basic / 24 Mbps air audit gate')
        off, unc = envelope(state['clock_checks'], 'AP')
        start = state['T0_ns']+5*NS
        bins = [0]*60
        packets = pcap_udp(raw/'AP/ap_udp.pcap', set(PORTS.values()))
        for role in STAS[1:]:
            for stamp, size in packets[PORTS[role]]:
                second = (stamp-off-start)//NS
                if 0 <= second < 60 and size == 1200:
                    bins[second] += size
            client = load(raw/role/'iperf_client.json')
            own = final[role]
            if (own['processes']['iperf_client']['exit_code'] != 1
                    or client.get('error') != 'interrupt - the client has terminated'
                    or len(client.get('intervals', [])) < 60):
                issues.append(role+' background iperf completion gate')
        cw = {}
        for role in STAS:
            own = final[role]
            if not (raw/role/'trace_stats.txt').is_file():
                issues.append(role+' trace loss receipt missing')
            native = lambda txt: sum(map(int, re.findall(r'\d+', next(
                line for line in txt.splitlines() if 'FIFO Underrun' in line))))
            if native(own['xmit_after']) != native(own['xmit_before']):
                issues.append(role+' FIFO delta gate')
            role_offset, _ = envelope(state['clock_checks'], role)
            cw[role] = []
            for index, mode in enumerate(MIXED_STATES):
                event = next(e for e in own['events'] if e['event']=='cw_verified' and e.get('index')==index)
                readback = event['readback']
                w = int(readback['hw_cwmin'])+1
                if (w != targets(self.policy_name, mode)[role] or
                        int(readback['hw_cwmax'])+1 != 8*w):
                    issues.append(role+' CW readback gate')
                cw[role].append({'index': index, 'W': w, 'verified_wall_ns': event['wall_ns'],
                    'write_call_wall_ns': event['write_call_ns'], 'write_return_wall_ns': event['write_return_ns'],
                    'verified_time_s': (event['wall_ns']-role_offset-start)/NS})
            if any(role not in self.samples[s] for s in range(6,66)):
                issues.append(role+' missing common queue/CW samples')
        if set(self.notices) != {1,2,3}:
            issues.append('application state notice recording gate')
        state['sample_records'] = {role: [self.samples[s][role] for s in range(6,66) if role in self.samples[s]]
                                   for role in STAS}
        return {
            'application': app, 'background_Mbps': sum(bins)*8/60e6,
            'background_Mbps_1s': [n*8/1e6 for n in bins], 'clock_boundary_ms': bounds,
            'AP_clock_uncertainty_ms': unc/1e6, 'actual_CW': cw, 'issues': issues,
            'inference_usable': not issues and app['generator_quality_passed'],
        }


def run_block(root, workspace, password):
    original = verify_plan(root/'attempt_01')
    stop_at = time.monotonic()+1500
    attempts, accepted = [], {}
    retry_used = set()
    preparation_retry_used = False
    folder = root/'attempt_01'
    plan = original
    while True:
        remaining = {r['slot'] for r in plan['plan']} - set(accepted)
        print('START_ATTEMPT', folder.name, sorted(remaining), flush=True)
        execute(folder, plan, workspace, password, session_factory=DeadlineSession,
                request_overrides={**TIMING, 'deadline_ns': DEADLINE_NS}, stop_at=stop_at-165,
                report_run=False)
        manifest = load(folder/'manifest.json')
        preparation_file = folder/'controller/preparation_final.json'
        preparation = load(preparation_file) if preparation_file.exists() else {}
        for row in manifest['windows']:
            if row['status']=='completed' and row.get('metrics', {}).get('inference_usable'):
                accepted[row['slot']] = {**row, 'source_dir': str(folder/row['id'])}
        attempts.append({'folder': str(folder), 'manifest': manifest['status'], 'error': manifest['error'],
                         'elapsed_s': manifest['elapsed_s'], 'restoration': manifest.get('postflight'),
                         'cleanup': preparation.get('cleanup', {}).get('confirmed')})
        atomic_json(root/'progress.json', {'attempts': attempts, 'accepted': list(accepted.values())})
        if len(accepted)==4 or time.monotonic()+320>=stop_at or not can_retry(manifest, preparation):
            break
        attempted = [r for r in manifest['windows'] if r['status']!='not_started' and r['slot'] not in accepted]
        slot = attempted[0]['slot'] if attempted else None
        if slot is None:
            if preparation_retry_used:
                break
            preparation_retry_used = True
        elif slot in retry_used:
            break
        else:
            retry_used.add(slot)
        folder = root/f'attempt_{len(attempts)+1:02}'
        plan = {**original, 'plan': [{**row, 'id': uuid.uuid4().hex} for row in original['plan']
                                     if row['slot'] not in accepted], 'previous_attempt': attempts[-1]['folder']}
        seal(folder, plan)
        verify_plan(folder)
    atomic_json(root/'result.json', {'rows': [accepted[k] for k in sorted(accepted)],
        'attempts': attempts, 'complete': len(accepted)==4, 'elapsed_s': 1500-(stop_at-time.monotonic())})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('seal', 'run'))
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    if args.action=='seal':
        if (args.root/'attempt_01').exists():
            raise RuntimeError('existing immutable experiment plan')
        seal(args.root/'attempt_01', make_plan())
        return
    verify_plan(args.root/'attempt_01')
    if (args.root/'attempt_01/manifest.json').exists():
        raise RuntimeError('experiment already attempted')
    workspace = Path(os.environ.get('LOCALAPPDATA', '.'))/'APSTASELECT/LabConsole'
    run_block(args.root, workspace, password_gui())


if __name__=='__main__':
    main()
