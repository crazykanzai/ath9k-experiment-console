"""Offline reports: simulated runs have no numeric observations; imports are explicit."""
import hashlib
import html
import json
import math
import statistics
import time
import uuid
from pathlib import Path
from .config import load_json
from .storage import atomic_json

T95 = [None, 12.7062047364, 4.30265272975, 3.18244630528, 2.7764451052,
       2.5705818356, 2.4469118488, 2.3646242516, 2.3060041352, 2.2621571629,
       2.2281388520, 2.2009851601, 2.1788128297, 2.1603686565, 2.1447866879,
       2.1314495456, 2.1199052992, 2.1098155778, 2.1009220402, 2.0930240544,
       2.0859634473, 2.0796138447, 2.0738730679, 2.0686576104, 2.0638985616,
       2.0595385528, 2.0555294386, 2.0518305165, 2.0484071418, 2.0452296421]
FIELDS = ['S_Mbps', 'HoL_success_us', 'p_ACK_original', 'p_ACK_aux_full']


def describe(values):
    known = [v for v in values if v is not None]
    result = {'n_windows': len(values), 'n_available': len(known), 'values': values,
              'mean': None, 'SD': None, 'CI95': None, 'repeat_unit': 'window'}
    if not values or len(known) != len(values):
        return result
    result['mean'] = statistics.mean(known)
    if len(known) >= 2:
        sd = statistics.stdev(known)
        result['SD'] = sd
        if len(known) - 1 < len(T95):
            half = T95[len(known) - 1] * sd / math.sqrt(len(known))
            result['CI95'] = [result['mean'] - half, result['mean'] + half]
    return result


def html_page(title, body):
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + html.escape(title) + '</title><style>body{font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif;max-width:1100px;margin:40px auto;padding:0 22px;color:#173042}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccd8de;padding:9px;text-align:left}.note{background:#fff3d6;padding:16px;border-radius:8px}img{max-width:100%}code{overflow-wrap:anywhere}</style><h1>' + html.escape(title) + '</h1>' + body + '</html>'


def simulation_report(folder, receipt, records):
    folder = Path(folder)
    text = '# 离线演练报告\n\n本次没有连接节点或启动业务，所有数值指标为NA；演练轮次不能当作正式有效窗口。\n\n'
    text += f'计划{receipt["planned_formal"]}轮，演练完成{receipt["simulated_complete"]}轮，正式有效0轮。状态：{receipt["status"]}。\n\n'
    text += '|轮次|发送节点|W|演练状态|吞吐|平均HoL|ACK代理|\n|---|---|---|---|---|---|---|\n'
    rows = ''
    for r in records:
        values = [str(r['order']), ','.join(r['active_stas']), str(r['W']), r['status'], 'NA', 'NA', 'NA']
        text += '|' + '|'.join(values) + '|\n'
        rows += '<tr>' + ''.join('<td>' + html.escape(v) + '</td>' for v in values) + '</tr>'
    text += '\n这里只验证计划、进程、停止、故障和报告流程；真实节点状态、同步、FIFO、追踪、空口与饱和均未验收。\n'
    (folder / 'analysis.md').write_text(text, encoding='utf-8')
    (folder / 'report.html').write_text(html_page('离线演练报告', '<p class="note">没有真实测量；正式有效0，所有指标NA。</p><p>' + html.escape(receipt['status']) + '</p><table><tr><th>轮</th><th>发送</th><th>W</th><th>演练状态</th><th>吞吐</th><th>HoL</th><th>ACK代理</th></tr>' + rows + '</table>'), encoding='utf-8')


def normalize_metrics(path):
    value = load_json(path)
    if not isinstance(value, dict) or value.get('status') not in ['completed_twenty_formal', 'stopped_at_gate']:
        raise ValueError('第一版离线导入仅支持已归档的四STA分别单发批次metrics.json（完成或按门禁停止）')
    source_rows = value.get('rows')
    if not isinstance(source_rows, list) or not 1 <= len(source_rows) <= 120 or value.get('valid_windows', value.get('source_valid_windows')) != len(source_rows):
        raise ValueError('有效窗口数与显式rows不一致，或没有可分析窗口')
    rows = []
    seen = set()
    for raw in source_rows:
        if not isinstance(raw, dict) or raw.get('sender') not in ['STA1', 'STA2', 'STA3', 'STA4']:
            raise ValueError('仅支持单发送者窗口')
        order = raw.get('order')
        if type(order) is not int or order < 1 or order in seen:
            raise ValueError('轮次重复或无效')
        seen.add(order)
        row = {'order': order, 'sender': raw['sender'], 'block': raw.get('block'), 'source_folder': raw.get('folder', raw.get('source_folder'))}
        for f in FIELDS + ['final_MAC_delivery', 'queue_nonempty', 'queue_backlog']:
            if f not in raw:
                raise ValueError('源指标缺少字段：' + f)
            x = raw[f]
            if x is not None and (type(x) not in [int, float] or not math.isfinite(x)):
                raise ValueError('指标不是有限数字或NA：' + f)
            if x is not None and (x < 0 or (f not in ['S_Mbps', 'HoL_success_us'] and x > 1)):
                raise ValueError('指标超范围：' + f)
            row[f] = x
        for f in ['complete_packets', 'complete_failures', 'boundary_or_cross', 'unknown_start', 'right_censored']:
            x = raw.get(f)
            if type(x) is not int or x < 0:
                raise ValueError('包计数缺失或无效：' + f)
            row[f] = x
        if row['complete_failures'] > row['complete_packets']:
            raise ValueError('失败包数超过完整包数')
        # Preserve the source formula, scope, and missing denominator. Never derive p from pairs.
        for f in ['probability_quality', 'auxiliary_formula', 'p_scope', 'HoL_scope']:
            row[f] = raw.get(f)
        rows.append(row)
    return {'status': value['status'], 'rows': rows, 'source_valid_windows': len(rows),
            'planned_windows': value.get('planned_windows'), 'missing_or_invalid': value.get('missing_or_invalid', [])}


def svg_plot(groups, field, label, scale=1):
    names = ['STA1', 'STA2', 'STA3', 'STA4']
    values = [x * scale for n in names for x in groups[n][field]['values'] if x is not None]
    ends = [x * scale for n in names for x in (groups[n][field]['CI95'] or [])]
    low, high = min([0] + values + ends), max([1] + values + ends)
    span = high - low
    high += .12 * span
    def y(x):
        return 365 - (x - low) / (high - low) * 285
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="880" height="470" viewBox="0 0 880 470"><rect width="880" height="470" fill="white"/><g font-family="Microsoft YaHei,system-ui,sans-serif" fill="#173042">',
           '<text x="40" y="30" font-size="19">' + html.escape(label) + '：全部窗口点与t分布95%CI</text>']
    for i in range(6):
        v = low + (high - low) * i / 5
        yy = y(v)
        out.append(f'<line x1="85" x2="845" y1="{yy:.2f}" y2="{yy:.2f}" stroke="#dde5eb"/><text x="78" y="{yy+5:.2f}" text-anchor="end" font-size="13">{v:.3g}</text>')
    for i, n in enumerate(names):
        x = 170 + i * 190
        d = groups[n][field]
        for j, v in enumerate(d['values']):
            if v is not None:
                xx = x + (j - (len(d['values']) - 1) / 2) * 9
                out.append(f'<circle cx="{xx:.2f}" cy="{y(v*scale):.2f}" r="5" fill="#1b8d8d" opacity=".7"/>')
        if d['mean'] is not None:
            if d['CI95']:
                a, b = [y(v * scale) for v in d['CI95']]
                out.append(f'<path d="M{x},{a:.2f}V{b:.2f}M{x-7},{a:.2f}H{x+7}M{x-7},{b:.2f}H{x+7}" stroke="#173042" stroke-width="2"/>')
            out.append(f'<rect x="{x-5}" y="{y(d["mean"]*scale)-5:.2f}" width="10" height="10" fill="#173042"/>')
        else:
            out.append(f'<text x="{x}" y="385" text-anchor="middle" font-size="13">整组NA</text>')
        out.append(f'<text x="{x}" y="412" text-anchor="middle">{n}（{d["n_available"]}/{d["n_windows"]}可得）</text>')
    out.append('<text x="40" y="452" font-size="13">独立窗口为重复单位；原NA不填补，已知子集不代替整组均值。</text></g></svg>')
    return ''.join(out)


def import_report(workspace, source):
    source = Path(source).expanduser().resolve()
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest() if source.stat().st_size <= 32 * 1024**2 else None
    if source_hash is None:
        raise ValueError('文件过大')
    normalized = normalize_metrics(source)
    # Verify source did not change during the read; archive normalized input, never mutate source.
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
        raise ValueError('读取期间源文件变化，请在采集结束后导入')
    report_id = uuid.uuid4().hex
    folder = Path(workspace) / 'reports' / report_id
    folder.mkdir(parents=True, exist_ok=False)
    groups = {}
    for name in ['STA1', 'STA2', 'STA3', 'STA4']:
        rows = [r for r in normalized['rows'] if r['sender'] == name]
        groups[name] = {f: describe([r[f] for r in rows]) for f in FIELDS + ['final_MAC_delivery']}
        groups[name]['complete_failures'] = sum(r['complete_failures'] for r in rows)
    provenance = {'id': report_id, 'kind': 'imported_measurements', 'source_path': str(source),
                  'source_sha256': source_hash, 'created_ns': time.time_ns(),
                  'explicit_orders': [r['order'] for r in normalized['rows']],
                  'no_recursive_scan': True, 'source_validity_preserved': True,
                  'source_scope': '源汇总文件的只读复算，未独立重验原始追踪、配置或有效性',
                  'input': 'input_snapshot.json', 'report': 'report.html'}
    atomic_json(folder / 'input_snapshot.json', normalized)
    atomic_json(folder / 'metrics.json', {'per_STA': groups, 'provenance': provenance})
    atomic_json(folder / 'manifest.json', provenance)
    lines = ['# 已有测量结果的离线复算', '', '输入：' + str(source), '',
             '仅复算显式rows，不访问节点，不重新判断原有效性，不混入其他批次。', '',
             '|STA|窗口数|吞吐Mbps|成功条件平均HoL ms|原ACK代理%|全包辅助ACK代理%|完整终态失败|',
             '|---|---|---|---|---|---|---|']
    table = ''
    def fmt(d, scale=1):
        if d['mean'] is None:
            return f'NA（{d["n_available"]}/{d["n_windows"]}可得）'
        txt = f'{d["mean"]*scale:.4f}'
        if d['CI95']:
            txt += ' [' + ', '.join(f'{v*scale:.4f}' for v in d['CI95']) + ']'
        return txt
    for name, g in groups.items():
        cells = [name, str(g['S_Mbps']['n_windows']), fmt(g['S_Mbps']), fmt(g['HoL_success_us'], .001),
                 fmt(g['p_ACK_original'], 100), fmt(g['p_ACK_aux_full'], 100), str(g['complete_failures'])]
        lines.append('|' + '|'.join(cells) + '|')
        table += '<tr>' + ''.join('<td>' + html.escape(c) + '</td>' for c in cells) + '</tr>'
    caveat = '平均HoL是成功条件、完整服务包、主机服务起点代理；与吞吐近似互为换算，不是两份独立验证。概率含全部完整服务失败包，未核准NA保留，辅助近似单列。单发失败不等于纯误码或精确碰撞概率。CI以独立窗口为重复单位，未覆盖全部时间漂移或系统误差；单窗无CI，超过30窗的CI暂不计算。'
    lines += ['', caveat, '', '源文件SHA256：' + source_hash]
    (folder / 'analysis.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    body = '<p class="note">已有测量的离线复算；原判定不改，未重新验收原始采集。</p><p>源文件：' + html.escape(str(source)) + '</p><table><tr><th>STA</th><th>窗口数</th><th>吞吐Mbps</th><th>HoL ms</th><th>原ACK代理%</th><th>辅助ACK代理%</th><th>完整失败</th></tr>' + table + '</table><p>' + html.escape(caveat) + '</p>'
    for field, scale, label in [('S_Mbps', 1, '吞吐Mbps'), ('HoL_success_us', .001, '成功条件平均HoL ms'),
                                ('p_ACK_original', 100, '原ACK成功代理%'), ('p_ACK_aux_full', 100, '全包辅助ACK成功代理%')]:
        name = field + '.svg'
        (folder / name).write_text(svg_plot(groups, field, label, scale), encoding='utf-8')
        body += '<img src="' + name + '" alt="' + html.escape(label) + '">'
    (folder / 'report.html').write_text(html_page('已有单发测量结果', body), encoding='utf-8')
    import csv
    with (folder / 'windows.csv').open('w', encoding='utf-8-sig', newline='') as f:
        keys = ['order', 'block', 'sender'] + FIELDS + ['complete_packets', 'complete_failures', 'boundary_or_cross', 'unknown_start', 'right_censored']
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows({k: r.get(k) if r.get(k) is not None else 'NA' for k in keys} for r in normalized['rows'])
    atomic_json(folder / 'sha256.json', {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()})
    return provenance
