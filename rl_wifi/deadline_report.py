"""Apply the sealed decision rule and plot accepted runs from explicit inputs."""
import argparse
from pathlib import Path

from labconsole.storage import atomic_json
from .quick_metrics import load


LABELS = {'P': '固定 P', 'E': '固定 E', 'middle': '固定中间档', 'dynamic': '动态规则'}


def meets_target(row):
    return all(s['on_time_ratio'] >= .95 for s in row['metrics']['application']['states'].values())


def judge(rows):
    by_policy = {row['policy']: row for row in rows}
    if set(by_policy) != set(LABELS):
        return {'code': 'incomplete', 'background_gain': None}
    dynamic = by_policy['dynamic']
    fixed = [row for label, row in by_policy.items() if label != 'dynamic' and meets_target(row)]
    if not fixed:
        return {'code': 'deadline_advantage' if meets_target(dynamic) else 'no_qualified_policy',
                'background_gain': None}
    best = max(fixed, key=lambda r: r['metrics']['background_Mbps'])
    base = best['metrics']['background_Mbps']
    gain = dynamic['metrics']['background_Mbps']/base-1 if base else None
    if meets_target(dynamic) and gain is not None and gain >= .05:
        code = 'replication_signal'
    elif meets_target(by_policy['middle']):
        code = 'stop_middle'
    else:
        code = 'stop_no_gain'
    return {'code': code, 'background_gain': gain, 'best_fixed': best['policy'],
            'dynamic_meets_target': meets_target(dynamic)}


def plot(rows, out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter

    fig, axes = plt.subplots(4, 1, figsize=(11, 8), sharex=True, layout='constrained')
    states, cw, deadline, background = axes
    colors = {'P': '#3669a0', 'dynamic': '#b74d25', 'middle': '#758536', 'E': '#785498'}
    names = {'P': 'Fixed P', 'dynamic': 'Dynamic', 'middle': 'Fixed W32 background', 'E': 'Fixed E'}
    for i in range(4):
        color = '#ebf1f9' if i % 2 == 0 else '#f8e8da'
        for ax in axes:
            ax.axvspan(i*15, (i+1)*15, facecolor=color, lw=0)
        states.text(i*15+7.5, .5, 'Uniform' if i % 2 == 0 else 'Burst', ha='center', va='center')
    states.set_axis_off()
    for row in rows:
        name = row['policy']
        m = row['metrics']
        for role in ('STA2', 'STA3', 'STA4'):
            switches = m['actual_CW'][role]
            stamps = [0, *[max(0, r['verified_time_s']) for r in switches[1:]], 60]
            w = [r['W'] for r in switches]
            cw.step(stamps, [*w, w[-1]], where='post', color=colors[name],
                    lw=1.5, alpha=.8 if role=='STA2' else .33,
                    label=names[name] if role=='STA2' else None)
        deadline.plot([k+.5 for k in range(60)], m['application']['on_time_1s'], color=colors[name], lw=1.1)
        background.plot([k+.5 for k in range(60)], m['background_Mbps_1s'], color=colors[name], lw=1.1)
    cw.set_ylabel('Background W')
    cw.set_yticks([16, 32, 64])
    cw.legend(loc='upper center', bbox_to_anchor=(.5, 1.22), ncol=4, fontsize=8)
    deadline.set_ylabel('On time (40 ms)')
    deadline.yaxis.set_major_formatter(PercentFormatter(1))
    deadline.axhline(.95, color='#555', ls='--', lw=1)
    background.set_ylabel('Background Mbps')
    background.set_xlabel('Seconds from measurement start (1 s bins)')
    background.set_xlim(0, 60)
    for ax in axes[1:]:
        ax.grid(axis='y', alpha=.22)
    fig.suptitle('Application-state CW comparison | STA1 W16 in all runs', fontsize=13)
    fig.savefig(out/'timeline.png', dpi=180)
    plt.close(fig)


def report(root):
    result = load(root/'result.json')
    rows = result['rows']
    decision = judge(rows)
    out = root/'analysis'
    out.mkdir()
    atomic_json(out/'decision.json', decision)
    captions = {
        'incomplete': '区组数据不完整，判据无法计算。',
        'deadline_advantage': '本组观察到达标优势，背景吞吐 5% 判据不可比较。',
        'no_qualified_policy': '本组四种方案均未同时达到两种状态的期限目标。',
        'replication_signal': '本组达到预登记判据，值得独立复验。',
        'stop_middle': '固定中间档达标，动态规则未达到预登记净收益判据，本组支持止损。',
        'stop_no_gain': '动态规则未达到预登记判据，本组支持停止扩展。'
    }
    lines = ['# 40 ms 业务状态 CW 对照', '', captions[decision['code']], '',
             '| 配置 | 均匀按时率 | 突发按时率 | 60 秒背景 Mbps | 本地漏发 | 生成延迟 p99 ms |',
             '|---|---:|---:|---:|---:|---:|']
    for row in rows:
        m = row['metrics']
        a = m['application']
        states = a['states']
        c = a['common']
        lines.append(f"| {LABELS[row['policy']]} | {states['uniform']['on_time_ratio']:.2%} | "
                     f"{states['burst']['on_time_ratio']:.2%} | {m['background_Mbps']:.3f} | "
                     f"{c['counts']['local_failure']} | {c['generation_delay_p99_ms']:.3f} |")
    if decision.get('best_fixed'):
        best = LABELS[decision['best_fixed']]
        gain = decision['background_gain']
        if gain is not None:
            lines.extend(['', f'最好的达标固定配置为{best}。动态规则相对吞吐变化为 {gain:+.2%}。'])
    lines.extend(['', '各状态按其 3000 条计划生成消息计算，切换附近数据全计。图上 CW 使用硬件读回时刻。'])
    if result['attempts']:
        attempt = result['attempts'][-1]
        postflight = attempt.get('restoration') or {}
        lines.extend(['', f"采集有效 {len(rows)}/4，现场耗时 {result['elapsed_s']/60:.2f} 分钟。"
                      f"最终设备恢复为 {postflight.get('confirmed')}，时间配置清理为 {attempt['cleanup']}。",
                      '', '各次尝试和失败原因保存在 result.json。'])
    (out/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    if rows:
        plot(rows, out)
    return decision


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    print(report(parser.parse_args().root))
