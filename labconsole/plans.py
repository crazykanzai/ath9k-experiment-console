"""Sealed explicit plans, never recursively inferred from result directories."""
import hashlib
import itertools
import json
import random

TEMPLATES = {
    'individual': '四台STA分别单发', 'cw': '单STA五档CW标定',
    'nodes': '固定W节点数扫描', 'power': '四STA低功率单发对照',
}
FIXED_BLOCKS = [[2, 1, 4, 3], [1, 3, 2, 4], [4, 2, 3, 1], [3, 4, 1, 2], [4, 1, 2, 3]]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name}须为{low}～{high}的整数')
    return value


def build_plan(request):
    if not isinstance(request, dict):
        raise ValueError('方案参数须为JSON对象')
    kind = request.get('template', 'individual')
    if kind not in TEMPLATES:
        raise ValueError('未知实验模板')
    repeat = integer(request.get('repeats', 5), '重复数', 1, 10)
    seed = integer(request.get('seed', 2026103120), '随机种子', 0, 2**31 - 1)
    sender = request.get('sender', 'STA1')
    if sender not in ['STA1', 'STA2', 'STA3', 'STA4']:
        raise ValueError('发送节点无效')
    W = request.get('W', 32)
    if type(W) is not int or W not in [8, 16, 32, 64, 128]:
        raise ValueError('W须为8/16/32/64/128')
    rng = random.Random(seed)
    trials = []
    def add(block, active, w):
        trials.append({'order': len(trials) + 1, 'block': block, 'active_stas': active,
                       'W': w, 'cwmin': w - 1, 'cwmax': 8 * w - 1})
    for block in range(1, repeat + 1):
        if kind in ['individual', 'power']:
            order = FIXED_BLOCKS[block - 1] if repeat == 5 and seed == 2026103120 else rng.sample([1, 2, 3, 4], 4)
            for n in order:
                add(block, ['STA' + str(n)], W)
        elif kind == 'cw':
            for w in rng.sample([8, 16, 32, 64, 128], 5):
                add(block, [sender], w)
        else:
            for n in rng.sample([1, 2, 3, 4], 4):
                combos = list(itertools.combinations(['STA1', 'STA2', 'STA3', 'STA4'], n))
                add(block, list(combos[(block - 1) % len(combos)]), W)
    if kind in ['individual', 'power', 'nodes']:
        prechecks = [{'active_stas': ['STA' + str(n)], 'W': W} for n in [1, 3, 4, 2]]
        if kind == 'nodes':
            prechecks += [{'active_stas': ['STA1', 'STA2'], 'W': W},
                          {'active_stas': ['STA1', 'STA2', 'STA3'], 'W': W},
                          {'active_stas': ['STA1', 'STA2', 'STA3', 'STA4'], 'W': W}]
    else:
        prechecks = [{'active_stas': [sender], 'W': w} for w in [8, 16, 32, 64, 128]]
    power = 5 if kind == 'power' else 'preserve_readback'
    return {
        'schema_version': 1, 'template': kind, 'title': TEMPLATES[kind], 'seed': seed,
        'repeats': repeat, 'formal_inputs': trials, 'prechecks': prechecks,
        'parameters': {'access': 'basic', 'K': 3, 'BE_AIFSN': 3, 'DATA_Mbps': 24,
                       'UDP_payload_B': 1200, 'active_load_Mbps': 30,
                       'STA_power_dBm': power, 'AP_power': 'preserve_readback',
                       'retry_strategy': 'preserve_original_multiseries', 'FIFO': 20},
        'timing': {'warmup_s': 15, 'measure_s': 180, 'tail_s': 15,
                   'precheck_s': [5, 30, 5], 'clock_limit_ms': 50, 'boundary_limit_ms': 100,
                   'start_margin_s': 25, 'ntp_settle_s': 90, 'ntp_hold_s': 300},
        'gates': {'idle_grace_s': 5, 'idle_policy': 'per_episode',
                  'active_association_strict': True, 'probe_misses_max': 0,
                  'queue_nonempty_min': .95, 'queue_backlog_min': .90,
                  'stop_on_gate_failure': True, 'auto_replace_invalid': False},
        'formal_business_s': len(trials) * 210, 'precheck_business_s': len(prechecks) * 40,
        'estimated_total_minutes_range': [round(len(trials) * 6 + 15), round(len(trials) * 10 + 25)],
        'notes': ['节点数扫描的波动包含节点组合与时间变化；组合未承诺完全平衡。',
                  '当前版本仅可离线演练；所有真实节点门禁均未现场检查。',
                  '计划冻结后不可改序、删轮或自动补轮。'],
        'measurement_scope': {'HoL': '成功条件、窗口内完整服务包、主机服务起点代理',
                              'probability': '全部完整服务包含终态失败；未核准NA，辅助近似单列'},
    }
