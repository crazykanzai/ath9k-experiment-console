"""A separate durable offline executor. No network, SSH, or historical scripts."""
import json
import subprocess
import sys
import time
from pathlib import Path
from .plans import digest
from .storage import Store, atomic_json

STAGES = ['warmup', 'measurement', 'tail', 'collect', 'audit']


def launch(store, job):
    log = store.root / 'runs' / job / 'worker.log'
    with log.open('ab') as output:
        command = [sys.executable, '-m', 'labconsole.worker', '--workspace', str(store.root), '--job', job]
        return subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], stdout=output, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)


def execute(root, job):
    store = Store(root)
    if not store.claim(job):
        return
    folder = store.root / 'runs' / job
    state = store.job(job)
    plan = state['plan']
    records = []
    final = 'completed'
    error = None
    def phase(stage):
        store.update(job, stage)
        deadline = time.monotonic() + state['delay']
        while time.monotonic() < deadline:
            if store.job(job)['stop'] == 'immediate':
                return False
            time.sleep(min(.03, max(0, deadline - time.monotonic())))
        return store.job(job)['stop'] != 'immediate'
    try:
        disk_plan = json.loads((folder / 'plan.json').read_text(encoding='utf-8'))
        disk_platform = json.loads((folder / 'platform.json').read_text(encoding='utf-8'))
        if digest({'plan': disk_plan, 'platform': disk_platform}) != state['seal']:
            raise ValueError('封存计划或平台快照被修改，拒绝继续')
        for stage in ['local_check', 'simulated_preparation', 'simulated_clock_hold', 'simulated_armed_abort']:
            store.event(job, stage, '离线状态机演练，未执行对应节点动作')
            if not phase(stage):
                final = 'stopped'
                break
        if final == 'completed':
            for i, pre in enumerate(plan['prechecks'], 1):
                store.event(job, 'simulated_precheck', json.dumps(pre, ensure_ascii=False))
                if not phase('simulated_precheck_' + str(i)):
                    final = 'stopped'
                    break
                if store.job(job)['stop'] == 'after_round':
                    final = 'stopped'
                    break
        if final == 'completed':
            for trial in plan['formal_inputs']:
                if store.job(job)['stop']:
                    final = 'stopped'
                    break
                row = {**trial, 'status': 'simulating', 'real_measurement': False,
                       'S_Mbps': None, 'HoL_success_us': None, 'p_ACK_original': None,
                       'p_ACK_aux_full': None, 'began_controller_ns': time.time_ns()}
                store.event(job, 'round_started', f'离线第{trial["order"]}轮；尚无真实T0或测量')
                for stage in STAGES:
                    if not phase('simulated_' + stage):
                        row['status'] = 'simulation_aborted'
                        final = 'stopped'
                        break
                if final != 'stopped' and trial['order'] == state['fault_at']:
                    row['status'] = 'simulated_gate_failure'
                    error = '离线注入采集门禁故障；没有真实节点故障'
                    final = 'gate_failed'
                elif final != 'stopped':
                    row['status'] = 'simulated'
                    store.update(job, 'simulated_round_complete', trial['order'])
                records.append(row)
                atomic_json(folder / 'simulation_records.json', records)
                store.event(job, row['status'], f'第{trial["order"]}轮；正式有效数始终为0')
                if final != 'completed':
                    break
                if store.job(job)['stop'] == 'after_round':
                    final = 'stopped'
                    break
    except Exception as exc:
        final, error = 'worker_failed', str(exc)
        store.event(job, 'error', error)
    finally:
        store.update(job, 'simulated_restoration')
        receipt = {'mode': 'offline', 'real_measurement': False, 'remote_actions': [],
                   'remote_restoration': 'not_applicable_no_node_changes',
                   'formal_valid': 0, 'simulated_complete': sum(x['status'] == 'simulated' for x in records),
                   'planned_formal': len(plan['formal_inputs']), 'status': final, 'error': error}
        atomic_json(folder / 'manifest.json', receipt)
        atomic_json(folder / 'simulation_records.json', records)
        store.event(job, 'cleanup', '离线演练结束；未连接节点，无无线或时间配置需要恢复')
        # Reporting failure is distinct from acquisition/cleanup; do not rerun trials.
        try:
            from .reports import simulation_report
            simulation_report(folder, receipt, records)
        except Exception as exc:
            store.event(job, 'report_failed', str(exc))
        store.finish(job, final, error)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--job', required=True)
    args = parser.parse_args()
    execute(Path(args.workspace), args.job)
