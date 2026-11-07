"""Read-only JSONL telemetry follower. Display intervals are never formal windows."""
import json
import math
import threading
import time
import uuid
from pathlib import Path
from .config import load_json
from .storage import atomic_json

SCHEMA = 'apstaselect.telemetry.v1'
STAS = ['STA1', 'STA2', 'STA3', 'STA4']


def finite(x, name, integer=False):
    if type(x) not in (int, float) or not math.isfinite(x) or x < 0 or (integer and type(x) is not int):
        raise ValueError('遥测字段无效：' + name)
    return x


class Observation:
    def __init__(self, meta):
        self.meta = meta
        self.offset = 0
        self.pending = b''
        self.latest = {}
        self.previous = {}
        self.sequences = {}
        self.points = []
        self.events = []
        self.errors = []
        self.line = 0
        self.source_identity = None
        self.ended = False
        self.lock = threading.Lock()
        expected = meta.get('source_identity')
        if expected is not None:
            self.source_identity = tuple(expected)
        self.source_min_size = meta.get('source_min_size', 0)

    def ingest(self, raw, received_ns=None):
        if not isinstance(raw, dict) or raw.get('schema') != SCHEMA:
            raise ValueError('未知遥测格式')
        role = raw.get('role')
        if role not in ['AP', *STAS, 'algorithm']:
            raise ValueError('未知遥测角色')
        seq = finite(raw.get('seq'), 'seq', True)
        if seq <= self.sequences.get(role, -1):
            raise ValueError('重复或乱序seq：' + role)
        stamp = finite(raw.get('sample_ns'), 'sample_ns', True)
        received = received_ns or time.time_ns()
        if stamp > received + 2_000_000_000:
            raise ValueError('采样时间在控制机未来超过2秒')
        uncertainty = finite(raw.get('clock_uncertainty_ms'), 'clock_uncertainty_ms')
        if not isinstance(raw.get('source'), str) or not raw['source']:
            raise ValueError('必须声明指标来源')
        if raw.get('kind') == 'event':
            if raw.get('event') not in ['cw_requested', 'cw_applied', 'cw_verified', 'note']:
                raise ValueError('未知事件')
            event = {k: raw.get(k) for k in ['sample_ns', 'role', 'event', 'target', 'requested', 'readback', 'source', 'message']}
            self.events.append(event)
            self.events = self.events[-500:]
            self.sequences[role] = seq
            return
        if role not in ['AP', *STAS] or raw.get('kind') != 'sample':
            raise ValueError('未知采样类型')
        if not isinstance(raw.get('data'), dict):
            raise ValueError('data须为对象')
        data = raw['data']
        if role == 'AP':
            if not isinstance(data.get('rx_bytes'), dict):
                raise ValueError('AP缺少逐STA接收计数')
            for n, count in data['rx_bytes'].items():
                if n not in STAS:
                    raise ValueError('未知STA')
                finite(count, 'rx_bytes', True)
        else:
            cw = data.get('cw')
            if cw is not None:
                if not isinstance(cw, dict) or cw.get('basis') != 'hardware_readback':
                    raise ValueError('CW必须声明硬件读回，不将请求值当实值')
                for k in ['cwmin', 'cwmax', 'aifsn']:
                    finite(cw.get(k), k, True)
                if cw['cwmin'] > cw['cwmax'] or cw['cwmax'] > 1023 or cw['aifsn'] > 255:
                    raise ValueError('CW范围无效')
            dcf = data.get('dcf')
            if dcf is not None:
                for k in ['generation', 'hol_samples', 'hol_sum_us', 'attempts', 'successes', 'packets', 'failures']:
                    finite(dcf.get(k), k, True)
                if dcf['successes'] > dcf['attempts'] or dcf['failures'] > dcf['packets']:
                    raise ValueError('DCF计数不一致')
                if type(dcf.get('running')) is not bool or dcf.get('mode') not in ['basic', 'rts-cts', 'unknown']:
                    raise ValueError('DCF运行状态或模式不明确')
        prior = self.previous.get(role)
        if prior and stamp <= prior['sample_ns']:
            raise ValueError('采样时间倒退或重复：' + role)
        point = {'role': role, 'sample_ns': stamp, 'received_ns': received, 'source': raw['source'],
                 'clock_uncertainty_ms': uncertainty, 'per_STA_Mbps': {}, 'HoL_us': None, 'p_ACK': None,
                 'hol_samples': 0, 'attempts': 0, 'failures': None, 'cw': data.get('cw'), 'reason': None}
        gap = (stamp - prior['sample_ns'])/1e9 if prior else None
        cw_changed = prior is not None and data.get('cw') is not None and prior['data'].get('cw') is not None and data['cw'] != prior['data']['cw']
        if cw_changed:
            self.events.append({'sample_ns': stamp, 'role': role, 'event': 'cw_readback_change',
                'previous': prior['data']['cw'], 'readback': data['cw'], 'source': raw['source'],
                'earliest_ns': prior['sample_ns'], 'message': '两次读回之间发生变化；此线是发现时刻，非精确写入时刻'})
        if uncertainty > 50 or (prior and prior['clock_uncertainty_ms'] > 50):
            point['reason'] = '时钟不确定度超过50ms，拒绝对齐区间'
        elif gap is None or not 0 < gap <= 5:
            point['reason'] = '首次采样、乱序或间隔>5秒；重新建立基线'
        elif data.get('error') or prior['data'].get('error'):
            point['reason'] = '采集错误，重新建立基线'
        elif role == 'AP':
            for n, count in data['rx_bytes'].items():
                old = prior['data'].get('rx_bytes', {}).get(n)
                point['per_STA_Mbps'][n] = (count-old)*8/gap/1e6 if old is not None and count >= old else None
        else:
            d, old = data.get('dcf'), prior['data'].get('dcf')
            if cw_changed:
                point['reason'] = '区间跨CW变化，丢弃该区间估计，不归入任一CW'
            elif not d or not old or not d.get('running') or not old.get('running') or d['generation'] != old['generation']:
                point['reason'] = 'DCF未运行、缺失或generation改变；不自动start/reset'
            elif any(d[k] < old[k] for k in ['hol_samples', 'hol_sum_us', 'attempts', 'successes', 'packets', 'failures']):
                point['reason'] = '计数回退；不补零'
            else:
                delta = {k: d[k]-old[k] for k in ['hol_samples', 'hol_sum_us', 'attempts', 'successes', 'packets', 'failures']}
                point.update(hol_samples=delta['hol_samples'], attempts=delta['attempts'], failures=delta['failures'])
                point['HoL_us'] = delta['hol_sum_us']/delta['hol_samples'] if delta['hol_samples'] else None
                if d.get('mode') == old.get('mode') == 'basic' and d.get('basic_retry_abi') == old.get('basic_retry_abi') == 2 and not delta['failures'] and delta['attempts'] and delta['successes'] <= delta['attempts']:
                    point['p_ACK'] = delta['successes']/delta['attempts']
                else:
                    point['reason'] = 'ACK分母未核准/终态失败/无尝试，概率NA'
        # Never derive additional metrics from stale/carry-forward values.
        self.sequences[role] = seq
        self.previous[role] = raw
        self.latest[role] = {'sample_ns': stamp, 'received_ns': received, 'point': point, 'raw': raw}
        self.points.append(point)
        self.points = self.points[-6000:]
        self.events = self.events[-500:]

    def follow(self):
        if self.ended:
            return
        source = Path(self.meta['source_path'])
        stat = source.stat()
        identity = (stat.st_dev, stat.st_ino)
        if self.source_identity is not None and (identity != self.source_identity or stat.st_size < max(self.offset,self.source_min_size)):
            self.ended = True
            raise ValueError('源文件被替换或截断；已停止观察，不自动拼接不同实验')
        self.source_identity = identity
        with source.open('rb') as f:
            f.seek(self.offset)
            data = f.read(2*1024**2)
        self.offset += len(data)
        self.pending += data
        if len(self.pending) > 2*1024**2 and b'\n' not in self.pending:
            self.ended = True
            raise ValueError('单条遥测超过2MiB')
        while b'\n' in self.pending:
            line, self.pending = self.pending.split(b'\n', 1)
            self.line += 1
            if not line.strip():
                continue
            try:
                self.ingest(json.loads(line.decode('utf-8-sig')))
            except (ValueError, KeyError, TypeError) as exc:
                self.errors.append(f'第{self.line}行拒绝：{exc}')
                self.errors = self.errors[-50:]

    def snapshot(self):
        now = time.time_ns()
        latest = {}
        for n, record in self.latest.items():
            age = max(0, (now-record['sample_ns'])/1e9)
            latest[n] = {**record, 'age_s': age, 'stale': age > 5}
        return {'session': self.meta, 'latest': latest, 'points': self.points, 'events': self.events,
                'errors': self.errors, 'ended': self.ended, 'read_offset': self.offset,
                'server_now_ns': str(now), 'formal_result': False}


class LiveHub:
    def __init__(self, workspace):
        self.root = Path(workspace)/'observations'
        self.observations = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        if self.root.exists():
            for p in self.root.glob('*/manifest.json'):
                meta = load_json(p)
                if meta.get('watching'):
                    self.observations[meta['id']] = Observation(meta)
        self.thread = threading.Thread(target=self.run, daemon=True, name='local-telemetry-follow')
        self.thread.start()

    def create(self, source, title, kind):
        if kind not in ['live', 'offline_test']:
            raise ValueError('来源类型须为live或offline_test')
        source = Path(source).expanduser().resolve()
        if not source.is_file() or source.suffix != '.jsonl':
            raise ValueError('请选择明确的本机遥测.jsonl文件')
        if source.stat().st_size > 128*1024**2:
            raise ValueError('观察源超过128MiB；请选择本次会话文件')
        if not isinstance(title, str) or not 1 <= len(title) <= 120:
            raise ValueError('名称须为1～120字符')
        with self.lock:
            existing = next((o for o in self.observations.values() if o.meta['source_path'] == str(source) and not o.ended), None)
            if existing:
                return existing.meta
            identity = uuid.uuid4().hex
            meta = {'id': identity, 'title': title, 'source_path': str(source), 'kind': kind, 'watching': True,
                    'created_ns': time.time_ns(), 'formal_result': False, 'node_control': False,
                    'source_identity': [source.stat().st_dev,source.stat().st_ino], 'source_min_size': source.stat().st_size}
            atomic_json(self.root/identity/'manifest.json', meta)
            obs = Observation(meta)
            obs.follow()
            self.observations[identity] = obs
            return meta

    def listing(self):
        with self.lock:
            return [o.meta for o in self.observations.values()]

    def get(self, identity):
        with self.lock:
            obs = self.observations.get(identity)
        if not obs:
            raise ValueError('观察会话不存在')
        with obs.lock:
            return obs.snapshot()

    def detach(self, identity):
        with self.lock:
            obs = self.observations.get(identity)
            if not obs:
                raise ValueError('观察会话不存在')
            with obs.lock:
                obs.ended = True
                obs.meta['watching'] = False
                atomic_json(self.root/identity/'manifest.json', obs.meta)
        return {'detached': True, 'traffic_stopped': False}

    def run(self):
        while not self.stop_event.wait(.5):
            with self.lock:
                items = list(self.observations.values())
            for obs in items:
                with obs.lock:
                    try:
                        obs.follow()
                    except (OSError, ValueError) as exc:
                        if not obs.errors or obs.errors[-1] != str(exc):
                            obs.errors.append(str(exc))
                            obs.errors = obs.errors[-50:]
                        if obs.ended:
                            obs.meta['watching'] = False
                            atomic_json(self.root/obs.meta['id']/'manifest.json', obs.meta)

    def close(self):
        self.stop_event.set()
        self.thread.join(3)
