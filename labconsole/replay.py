"""Read-only historical playback. Reuse archived common-wall-clock bins, never nodes."""
import hashlib
import json
import math
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .config import load_json
from .storage import atomic_json

PROJECT = Path(__file__).resolve().parents[1]
STAS = ['STA1', 'STA2', 'STA3', 'STA4']
PRESETS = {
    'nodes20': ('Basic W32 节点数扫描 · 20窗', '2026-10-31_Basic_W32_n1-4_20窗扫描_01'),
    'cw25': ('STA1 五档CW标定 · 25窗', '2026-10-25_单节点五档标定_周报汇总_01'),
    'individual20': ('原功率四STA分别单发 · 20窗', '2026-11-01_Basic_W32_四STA单发五重复_MON15_01'),
    'power20': ('5 dBm四STA分别单发 · 20窗', '2026-11-01_Basic_W32_四STA单发_5dBm对照_MON15_01'),
}
NOTES = [
    '历史数据回放，不是实时采集；不连接节点，不启动业务，不改变原有效性。',
    '吞吐序列复用既有AP UDP抓包、时钟修正后的公共绝对时间1秒桶；未重新解析原始pcap。',
    'HoL与ACK代理只有整窗结果，时序图仅在测量结束边界显示结果点，不伪造逐秒值。',
    'HoL为成功条件、窗口内完整服务包、主机服务起点代理；失败、跨界、未知单列。',
    '原ACK概率NA保持，含全部完整失败包的辅助近似另选另标；不是精确碰撞概率。',
    '批次图横轴是预登记轮次，不是连续墙钟；不连接不同实验的时间或合并批次。',
]


def numeric(value, name, probability=False, nullable=True):
    if value is None and nullable:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('回放数值无效：' + name)
    if probability and value > 1:
        raise ValueError('概率超范围：' + name)
    return value


def first(raw, *names):
    # A present null is authoritative; do not replace NA with another estimator.
    for name in names:
        if name in raw:
            return raw[name]
    return None


def iso_ns(ns):
    return datetime.fromtimestamp(ns / 1e9, timezone(timedelta(hours=8))).isoformat(timespec='milliseconds')


def stable_json(path, sources):
    path = Path(path).resolve()
    if path.stat().st_size > 32 * 1024**2:
        raise ValueError('回放源JSON超过32MiB限制')
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    def bad(value):
        raise ValueError('JSON不接受NaN或Infinity：' + value)
    data = json.loads(raw.decode('utf-8-sig'), parse_constant=bad)
    if not isinstance(data, dict):
        raise ValueError('回放源JSON须为对象')
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError('读取期间源文件变化：' + str(path))
    sources[str(path)] = digest
    return data


def source_catalog(project=PROJECT):
    result = []
    for key, (title, folder) in PRESETS.items():
        original = project.parent / '打流分析' / folder / 'metrics.json'
        snapshot = project / 'labconsole' / 'examples' / ('replay_' + key + '.json')
        selected = original if original.is_file() else snapshot
        result.append({'key': key, 'title': title, 'available': selected.is_file(),
                       'source': str(selected), 'source_kind': '原批显式汇总' if selected == original else '随程序提供的历史快照'})
    return result


def preset_source(key):
    match = next((r for r in source_catalog() if r['key'] == key), None)
    if not match or not match['available']:
        raise ValueError('指定历史样例不存在，请填写本机metrics.json路径')
    return match['source']


def summary(raw, leaf):
    def num(label, *keys, probability=False):
        return numeric(first(raw, *keys), label, probability=probability)
    coverage = raw.get('window_coverage') or leaf.get('window_coverage') or {}
    counts = coverage.get('categories') or {}
    return {
        'throughput_Mbps': num('吞吐', 'S_Mbps'),
        'HoL_us': num('HoL', 'HoL_success_us', 'HoL_success_conditional_us', 'HoL_us'),
        'p_original': num('原ACK代理', 'p_ACK_original', probability=True),
        'p_auxiliary': num('全包辅助ACK代理', 'p_ACK_aux_full', 'p_ACK_aux_all_complete', probability=True),
        'final_delivery': num('最终MAC交付', 'final_MAC_delivery', 'final_delivery', probability=True),
        'complete_packets': first(raw, 'complete_packets') if 'complete_packets' in raw else leaf.get('complete_packets'),
        'successes': first(raw, 'complete_successes', 'ACK_success') if any(k in raw for k in ['complete_successes', 'ACK_success']) else leaf.get('ACK_success'),
        'failures': first(raw, 'complete_failures', 'terminal_failures') if any(k in raw for k in ['complete_failures', 'terminal_failures']) else leaf.get('terminal_failure'),
        'boundary': raw.get('boundary_or_cross', sum(v for k, v in counts.items() if k not in ['complete', 'unknown_start', 'outside'])),
        'unknown': raw.get('unknown_start', counts.get('unknown_start')),
        'right_censored': raw.get('right_censored'),
        'queue_nonempty': first(raw, 'queue_nonempty', 'queue_nonzero'),
        'queue_backlog': raw.get('queue_backlog'),
        'auxiliary_formula': first(raw, 'auxiliary_formula', 'auxiliary_failure_mapping', 'Basic_failure_auxiliary'),
        'probability_quality': raw.get('probability_quality', '源汇总的交换成功代理；非精确碰撞概率'),
    }


def validate_snapshot(value):
    if not isinstance(value, dict) or value.get('schema') != 'apstaselect.history-replay.v1':
        raise ValueError('不支持的回放快照')
    records = value.get('records')
    if not isinstance(records, list) or not 1 <= len(records) <= 120:
        raise ValueError('回放窗口数无效')
    seen = set()
    for r in records:
        order = r['order']
        if type(order) is not int or order < 1 or order in seen:
            raise ValueError('回放轮次重复或无效')
        seen.add(order)
        active = r['active_stas']
        if not isinstance(active, list) or not active or len(set(active)) != len(active) or any(n not in STAS for n in active):
            raise ValueError('发送节点集合无效')
        if type(r['valid']) is not bool:
            raise ValueError('缺少原有效性判定')
        timing = r['timing']
        for k in ['warmup_s', 'measure_s', 'tail_s']:
            if type(timing[k]) is not int or not 0 <= timing[k] <= 3600:
                raise ValueError('时间配置无效')
        if timing['measure_s'] < 1:
            raise ValueError('测量期为空')
        total = sum(timing.values())
        if total > 7200 or set(timing) != {'warmup_s', 'measure_s', 'tail_s'}:
            raise ValueError('时间配置不受支持')
        for n in active:
            a = r['throughput_1s_Mbps'][n]
            if not isinstance(a, list) or len(a) != total:
                raise ValueError('吞吐时间序列长度与计划不一致')
            for x in a:
                numeric(x, '逐秒吞吐')
            s = r['per_STA'][n]
            for k in ['throughput_Mbps', 'HoL_us', 'p_original', 'p_auxiliary', 'final_delivery', 'queue_nonempty', 'queue_backlog']:
                numeric(s.get(k), k, probability=k.startswith('p_') or k in ['final_delivery', 'queue_nonempty', 'queue_backlog'])
            for k in ['complete_packets', 'successes', 'failures', 'boundary', 'unknown', 'right_censored']:
                x = s.get(k)
                if x is not None and (type(x) is not int or x < 0):
                    raise ValueError('包计数无效：' + k)
            if s.get('failures') is not None and s.get('complete_packets') is not None and s['failures'] > s['complete_packets']:
                raise ValueError('失败包超过完整包数')
        for k in ['throughput_Mbps', 'HoL_us', 'p_original', 'p_auxiliary']:
            numeric(r['aggregate'].get(k), k, probability=k.startswith('p_'))
        if not isinstance(r.get('T0_ns'), str) or not r['T0_ns'].isdigit():
            raise ValueError('T0缺失')
    return value


def build_replay(source):
    source = Path(source).expanduser().resolve()
    sources = {}
    batch = stable_json(source, sources)
    if batch.get('schema') == 'apstaselect.history-replay.v1':
        validate_snapshot(batch)
        return batch, sources
    rows = batch.get('rows')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 120:
        raise ValueError('请选择含显式rows的已归档批次metrics.json，或本程序导出的replay.json')
    declared = first(batch, 'valid_windows', 'valid_formal_windows', 'valid_count')
    records = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('folder'), str):
            raise ValueError('显式窗口缺少folder；不递归搜索或猜测窗口')
        folder = Path(row['folder'])
        if not folder.is_absolute():
            folder = source.parent / folder
        manifest = stable_json(folder / 'manifest.json', sources)
        metrics = stable_json(folder / 'metrics.json', sources)
        plan = manifest.get('plan') or {}
        active = plan.get('active_stas') or ([plan['single_sta']] if plan.get('single_sta') else None)
        if not active:
            active = row.get('senders') or ([row['sender']] if row.get('sender') else None)
        if not active:
            raise ValueError('源文件未明确发送节点')
        timing = {k: plan[k] for k in ['warmup_s', 'measure_s', 'tail_s']}
        start = plan['t0_ns']
        if type(start) is not int:
            raise ValueError('源计划T0不是整数纳秒')
        for k, offset in [('measurement_start_ns', timing['warmup_s']), ('measurement_stop_ns', timing['warmup_s'] + timing['measure_s']), ('traffic_stop_ns', sum(timing.values()))]:
            if manifest.get(k) != start + offset * 1_000_000_000:
                raise ValueError('源计划公共边界不一致：' + k)
        if type(manifest.get('valid')) is not bool:
            raise ValueError('源manifest未提供有效性判定')
        peers = {}
        warnings = []
        for n in active:
            leaf = metrics.get('per_STA', {}).get(n)
            if not isinstance(leaf, dict):
                raise ValueError('源窗口缺少发送节点指标：' + n)
            raw = row.get('per_STA', {}).get(n, row)
            peers[n] = summary(raw, leaf)
        aggregate = {'throughput_Mbps': numeric(row.get('S_Mbps'), '聚合吞吐'),
                     'HoL_us': numeric(first(row, 'HoL_us', 'HoL_success_us', 'HoL_success_conditional_us'), '聚合HoL'),
                     'p_original': numeric(row.get('p_ACK_original'), '聚合原代理', probability=True),
                     'p_auxiliary': numeric(first(row, 'p_ACK_aux_full', 'p_ACK_aux_all_complete'), '聚合辅助代理', probability=True)}
        signature = plan.get('expected_signature', {})
        series = metrics.get('full_goodput_series_Mbps')
        if not isinstance(series, dict) or any(n not in series for n in active):
            raise ValueError('没有已归档公共墙钟吞吐桶；不根据iperf相对秒数或均值伪造时序')
        warm, measure = timing['warmup_s'], timing['measure_s']
        for n in active:
            bins = series[n]
            # Integrity check of the reused derived sequence, not a new validity decision.
            if len(bins) == sum(timing.values()) and all(type(x) in (int, float) and math.isfinite(x) for x in bins):
                s = peers[n]['throughput_Mbps']
                if s is not None and abs(sum(bins[warm:warm + measure]) / measure - s) > 1e-6:
                    warnings.append(n + '归档时间桶均值与源汇总有差异；两者原值均保留')
        records.append({'order': row.get('global_order', row.get('order')), 'source_order': row.get('order'),
            'block': row.get('block'), 'run_id': row.get('run_id', folder.name), 'source_folder': str(folder),
            'source_row_availability': row.get('availability'),
            'valid': manifest['valid'], 'source_status': manifest.get('status'),
            'W': int(signature['cwmin']) + 1 if 'cwmin' in signature else row.get('W'),
            'CW': [int(signature[k]) for k in ['cwmin', 'cwmax']] if 'cwmin' in signature else row.get('CW'),
            'mode': plan.get('access_mode'), 'active_stas': active, 'n': len(active),
            'T0_ns': str(start), 'T0': iso_ns(start), 'measurement_start': iso_ns(manifest['measurement_start_ns']),
            'measurement_stop': iso_ns(manifest['measurement_stop_ns']), 'timing': timing,
            'throughput_1s_Mbps': {n: series[n] for n in active}, 'per_STA': peers, 'aggregate': aggregate,
            'warnings': warnings, 'sample_kind': {'throughput': 'archived_AP_common_wall_1s_bins', 'HoL': 'whole_window_only', 'probability': 'whole_window_only'}})
    if declared is not None and declared != sum(r['valid'] for r in records):
        raise ValueError('源汇总有效窗口数与显式窗口manifest不一致；拒绝自动选择或追改')
    records.sort(key=lambda r: r['order'])
    value = {'schema': 'apstaselect.history-replay.v1', 'title': source.parent.name,
             'source_status': first(batch, 'collection_status', 'status'), 'source_path': str(source),
             'planned_windows': first(batch, 'planned_windows', 'planned', 'expected'),
             'missing_or_invalid': batch.get('missing_or_invalid', []), 'records': records,
             'notes': NOTES, 'source_files': sources,
             'scope': '只读复用归档汇总及公共墙钟吞吐桶；未独立重验原始采集，未重算HoL或概率'}
    validate_snapshot(value)
    for filename, digest in sources.items():
        if hashlib.sha256(Path(filename).read_bytes()).hexdigest() != digest:
            raise ValueError('导入期间源文件变化：' + filename)
    return value, sources


def import_replay(workspace, source):
    value, sources = build_replay(source)
    identity = uuid.uuid4().hex
    folder = Path(workspace) / 'replays' / identity
    folder.mkdir(parents=True, exist_ok=False)
    atomic_json(folder / 'replay.json', value)
    sha = hashlib.sha256((folder / 'replay.json').read_bytes()).hexdigest()
    meta = {'id': identity, 'kind': 'history_replay', 'title': value['title'],
            'created_ns': time.time_ns(), 'source_path': str(Path(source).resolve()),
            'source_files_read': len(sources), 'source_unchanged': True,
            'windows': len(value['records']), 'valid_windows': sum(r['valid'] for r in value['records']),
            'replay_sha256': sha, 'mode': 'offline_history', 'node_connections': 0}
    atomic_json(folder / 'manifest.json', meta)
    (folder / 'analysis.md').write_text('# 历史回放说明\n\n' + '\n\n'.join(NOTES) +
        '\n\n本目录的replay.json是自包含快照，可以在其他电脑的历史回放页导入；来源路径只用于溯源。\n', encoding='utf-8')
    return meta


def replay_folder(workspace, identity):
    if not isinstance(identity, str) or len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
        raise ValueError('无效回放标识')
    folder = Path(workspace) / 'replays' / identity
    if not folder.is_dir() or folder.is_symlink():
        raise ValueError('回放不存在')
    return folder


def get_replay(workspace, identity):
    folder = replay_folder(workspace, identity)
    meta = load_json(folder / 'manifest.json')
    path = folder / 'replay.json'
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != meta['replay_sha256']:
        raise ValueError('回放快照哈希不符')
    return {'manifest': meta, 'data': validate_snapshot(load_json(path))}


def list_replays(workspace):
    root = Path(workspace) / 'replays'
    if not root.exists():
        return []
    return [load_json(p) for p in sorted(root.glob('*/manifest.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
            if not p.is_symlink() and not p.parent.is_symlink()]
