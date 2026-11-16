"""Bounded real diagnostic sessions. Formal experiment routes remain separate."""
import collections
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import ipaddress
import json
from pathlib import Path
import queue
import re
import shlex
import shutil
import stat
import sys
import threading
import time
import uuid

from .cw_policy import (ACCESS_SETTINGS, CONTINUOUS_HOLD_SEGMENTS, CONTINUOUS_TRIALS,
                        LONG_DEMO_MODES, UNIFIED_GUARD_S, UNIFIED_SEGMENT_S, Policy, STAS, WINDOWS, continuous_candidates,
                        sliding_window_ucb, sta_windows, uniform_window, throughput_ucb, hold_ucb_step,
                        sta_cw_pairs, stage_windows,
                        UCB_WINDOW, UCB_ARMS, UCB_BONUS_MBPS,
                        HOLD_DROP_FRACTION,HOLD_SWITCH_MARGIN,HOLD_SAMPLES_PER_ARM)
from .preparation import ROLES, check_radio
from .prepare_node import MODULE
from .storage import atomic_json
from .config import enabled_roles
from .finish_protocol import heartbeat as finish_heartbeat, await_clean_exits

NS = 10**9
PORTS = dict(zip(STAS, range(5201, 5205)))
ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {'completed', 'failed', 'stopped', 'interrupted'}
SOURCE = 'live_hardware_diagnostic'
STAGE_LEAD_NS = 4*NS
CONTINUOUS_SCORE_TAIL_S = 5
CONTINUOUS_BASELINE_S = 30
UCB_LOOKBACK = UCB_WINDOW
TIMINGS = {'manual': (5, 30, 5, 10),
           'auto': (5, 60, 5, 10), 'fixed': (5, 60, 5, 10),
           'continuous': (5, 600, 5, 20), 'unified': (5, 600, 5, UNIFIED_SEGMENT_S)}


def validate_request(data):
    if set(data) - {'preparation_id', 'request_key', 'mode', 'initial_w', 'initial_per_STA_W', 'initial_per_STA_CW', 'active_senders', 'measure_s', 'access_mode', 'authorize'}:
        raise ValueError('未知现场演示参数')
    if data.get('authorize') is not True:
        raise ValueError('请明确选择执行真实设备演示')
    mode = data.get('mode')
    if not isinstance(mode,str) or mode not in TIMINGS:
        raise ValueError('未知现场演示模式')
    access_mode = data.get('access_mode','basic')
    if not isinstance(access_mode,str) or access_mode not in ACCESS_SETTINGS:
        raise ValueError('接入机制只支持 Basic 或 RTS/CTS')
    identity, key = data.get('preparation_id'), data.get('request_key')
    if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity):
        raise ValueError('请选择当前环境准备会话')
    if not isinstance(key, str) or not 8 <= len(key) <= 100:
        raise ValueError('请求标识无效')
    w = data.get('initial_w', 128 if mode in ('auto', 'fixed', *LONG_DEMO_MODES) else 32)
    if type(w) is not int or w not in WINDOWS:
        raise ValueError('W只允许8、16、32、64、128')
    if mode == 'auto' and w != 128:
        raise ValueError('自动演示以W128为固定基线，探索W32与W64')
    warmup, measure, tail, segment = TIMINGS[mode]
    result = {'preparation_id': identity, 'request_key': key, 'mode': mode, 'initial_w': w, 'access_mode':access_mode,
              'warmup_s': warmup, 'measure_s': measure, 'tail_s': tail, 'segment_s': segment,
              'guard_s': UNIFIED_GUARD_S if mode=='unified' else 3}
    if mode == 'unified':
        duration=data.get('measure_s',600)
        if type(duration) is not int or duration not in (60,300,600,1200):raise ValueError('演示测量时长只允许1、5、10、20分钟')
        result['measure_s']=duration
        if 'initial_per_STA_CW' in data:
            if 'initial_per_STA_W' in data:
                raise ValueError('初始 CW 与旧版 W 设置只能提交一组')
            result['initial_per_STA_CW'] = sta_cw_pairs(data['initial_per_STA_CW'])
        else:
            result['initial_per_STA_W'] = sta_windows(data.get('initial_per_STA_W', w))
        senders = data.get('active_senders', list(STAS))
        if (not isinstance(senders, list) or not senders or any(type(n) is not str or n not in STAS for n in senders)
                or len(set(senders)) != len(senders)):
            raise ValueError('上传集合须为四台STA中的非空、不重复集合')
        result['active_senders'] = [n for n in STAS if n in senders]
    elif any(k in data for k in ('initial_per_STA_W','initial_per_STA_CW','active_senders','measure_s')):
        raise ValueError('逐STA初始配置和上传集合仅供统一演示使用')
    return result


def stage_block(settings):
    block = {'W':uniform_window(settings), 'per_STA_W':stage_windows(settings)}
    if isinstance(settings[STAS[0]],dict):
        block['per_STA_CW'] = settings
    return block


def import_reference():
    # Import only the established definitions/analysis. The legacy Remote remains guarded.
    path = str(ROOT/'experiments')
    if path not in sys.path:
        sys.path.insert(0, path)


class ProbeReader:
    def __init__(self, ssh, role, iface):
        self.ssh, self.name, self.iface = ssh, role, iface

    def run(self, command, timeout=20):
        stdin, out, err = self.ssh.exec_command('LC_ALL=C '+command, timeout=timeout)
        stdin.close()
        stdout, stderr = out.read().decode(errors='replace'), err.read().decode(errors='replace')
        return {'stdout': stdout, 'stderr': stderr, 'code': out.channel.recv_exit_status()}


class RemoteWorker:
    """One SSH process per role. A live stdin lease also covers browser refresh."""
    def __init__(self, role, backend, events, output):
        self.role, self.backend, self.events, self.output = role, backend, events, output
        self.channel = None
        self.send_lock = threading.Lock()
        self.directory = None

    def send(self, value):
        with self.send_lock:
            stdin = self.channel[0]
            stdin.write(json.dumps(value)+'\n')
            stdin.flush()

    def launch(self, config, files):
        segments=config['measure_s']//config['segment_s']
        # Each segment may retain start, unlatched and final statistics files.
        self.archive_file_limit=32+3*segments
        self.archive_byte_limit=max(4*1024**3,sum(config[k] for k in ('warmup_s','measure_s','tail_s'))*8*1024**2)
        client = self.backend.client(self.role)
        with client.open_sftp() as sftp:
            self.directory = config['output']
            sftp.mkdir(str(Path(self.directory).as_posix()).rsplit('/', 1)[0], 0o700)
            sftp.mkdir(self.directory, 0o700)
            for src in files:
                sftp.put(str(src), self.directory+'/'+src.name)
        command = 'python3 -u '+shlex.quote(self.directory+'/demo_node.py')
        self.channel = client.exec_command('sudo -S -k -p "" env LC_ALL=C '+command, timeout=48)
        stdin, out, _ = self.channel
        stdin.write(self.backend.sudo_password+'\n')
        stdin.flush()
        if out.readline().strip() != 'ATH9K_DEMO_READY':
            raise RuntimeError(self.role+' demo helper failed before handshake')
        self.send(config)
        thread = threading.Thread(target=self.read, daemon=True)
        thread.start()
        return thread

    def read(self):
        _, out, err = self.channel
        try:
            for line in out:
                self.events.put((self.role, json.loads(line)))
            error = err.read().decode(errors='replace')
            self.output.mkdir(parents=True, exist_ok=True)
            (self.output/'ssh.stderr').write_text(error, encoding='utf-8')
            self.events.put((self.role, {'event': 'eof', 'exit_code': out.channel.recv_exit_status()}))
        except Exception as exc:
            self.events.put((self.role, {'event': 'transport_error', 'message': str(exc)}))

    def collect(self):
        if not self.directory:
            return
        self.output.mkdir(parents=True, exist_ok=True)
        with self.backend.client(self.role).open_sftp() as sftp:
            sftp.get_channel().settimeout(18)
            files = sftp.listdir_attr(self.directory)
            if (len(files)>getattr(self,'archive_file_limit',100)
                    or sum(f.st_size for f in files)>getattr(self,'archive_byte_limit',4*1024**3)):
                raise RuntimeError(self.role+' remote archive exceeds budget')
            for f in files:
                if stat.S_ISREG(f.st_mode) and re.fullmatch('[A-Za-z0-9_.-]+', f.filename) and f.filename not in ('.','..'):
                    target = self.output/f.filename
                    if target.exists():
                        raise RuntimeError('raw file already exists')
                    sftp.get(self.directory+'/'+f.filename, str(target))
                    if target.stat().st_size != f.st_size:
                        raise RuntimeError('raw file size mismatch '+f.filename)


def mac(info):
    return re.search(r'\baddr ([0-9a-f:]{17})', info)[1]


def interval_metrics(prior, current):
    if not prior or not current or prior['generation']!=current['generation'] or not current['running']:
        return None
    keys = ('hol_samples', 'hol_sum_us', 'attempts', 'successes', 'failures')
    delta = {k: current[k]-prior[k] for k in keys}
    if min(delta.values()) < 0 or delta['successes'] > delta['attempts']:
        return None
    delta['basic'] = current['mode']=='basic' and current['basic_retry_abi']==2
    return delta


class Session:
    def __init__(self, folder, prep, request):
        self.folder, self.prep, self.request = folder, prep, request
        self.id = folder.name
        self.roles=ROLES
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.messages = queue.Queue()
        self.workers, self.finished, self.latest = {}, {}, {}
        self.samples = collections.defaultdict(dict)
        self.stages = {0: request['initial_w']}
        self.sent = set()
        self.air_ok = False
        self.pending_w = None
        self.policy = Policy(20261111)
        self.policy.estimates = {w: self.policy.estimates[w] for w in (32, 64, 128)}
        self.evaluated = set()
        self.state = {'id': self.id, 'source': SOURCE, 'formal_valid': 0, 'created_ns': time.time_ns(),
                      'status': 'preparing', 'request': request, 'events': [], 'points': [], 'segments': [],
                      'nodes': {}, 'error': None, 'T0_ns': None, 'traffic_started': False,
                      'restoration': None, 'manual_pending': None, 'phase': '准备节点'}
        if request['mode']=='continuous':
            candidates = continuous_candidates(request['initial_w'],request['access_mode'])
            self.stages[0] = candidates['baseline']
            self.state['adaptation'] = {'candidates':candidates,'activation':None,'selection':None,
                                        'blocks':{},'policy':'sequential','decisions':[],
                                        'score_tail_s':CONTINUOUS_SCORE_TAIL_S,
                                        'minimum_hold_s':CONTINUOUS_HOLD_SEGMENTS*request['segment_s']}
        elif request['mode']=='unified':
            self.stages[0] = request.get('initial_per_STA_CW') or sta_windows(request.get('initial_per_STA_W', request['initial_w']))
            self.state['control'] = {'policy':'sliding_window_ucb','window_segments':UCB_LOOKBACK,
                                     'revision':7,'arms':list(UCB_ARMS),'bonus_Mbps':UCB_BONUS_MBPS,
                                     'strategy':'UCB_explore_then_degradation_triggered_hold',
                                     'hold_rules':{'drop_fraction':HOLD_DROP_FRACTION,'drop_segments':1,
                                                   'drop_reference':'previous_valid_same_w',
                                                   'switch_margin':HOLD_SWITCH_MARGIN,'samples_per_arm':HOLD_SAMPLES_PER_ARM},
                                     'observation':'AP_received_payload_Mbps_only','epoch_start':None,
                                     'score_tail_s':CONTINUOUS_SCORE_TAIL_S,'pending':None,
                                     'blocks':{'0':{'mode':'manual',**stage_block(self.stages[0])}}}

    def save(self):
        with self.lock:
            for attempt in range(5):
                try:
                    atomic_json(self.folder/'session.json', self.state)
                    return
                except PermissionError:
                    if attempt==4:
                        raise
                    time.sleep(.02*(attempt+1))

    def event(self, kind, message, **data):
        with self.lock:
            self.state['events'].append({'at_ns':time.time_ns(), 'kind':kind, 'message':message, **data})
            self.save()

    def abort(self, reason):
        with self.lock:
            if not self.cancel.is_set():
                self.state['abort_reason'] = reason
                self.cancel.set()

    def check_cancel(self):
        if self.cancel.is_set():
            raise RuntimeError(self.state['abort_reason'])

    def send_all(self, value, roles=None):
        roles = tuple(self.workers) if roles is None else tuple(roles)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda n: self.workers[n].send(value), roles))

    def send_stage(self, index, settings, at):
        def send(node):
            w = settings[node] if isinstance(settings, dict) else settings
            field = 'CW' if isinstance(w,dict) else 'W'
            self.workers[node].send({'op':'stage','index':index,field:w,'apply_ns':at})
        with ThreadPoolExecutor(max_workers=len(STAS)) as pool:
            list(pool.map(send, STAS))

    def heartbeat(self):
        finish_heartbeat(self)

    def receive(self, timeout=.15, strict=True):
        try:
            role, e = self.messages.get(timeout=timeout)
        except queue.Empty:
            return None, None
        kind = e['event']
        if kind=='eof':self.state.setdefault('process_exit_codes',{})[role]=e['exit_code']
        if kind == 'sample':
            self.samples[e['second']][role] = e
            self.latest[role] = e
            if role == 'MON15' and e['second']>=self.request['warmup_s']:
                self.air_ok = True
        else:
            if kind=='statistics_warning':
                index=e['index']
                self.state.setdefault('statistics_unavailable_segments',{}).setdefault(str(index),[]).append({'node':role,'reason':e['message']})
                start=self.request['warmup_s']+index*self.request['segment_s']
                for point in self.state['points']:
                    if start<=point['second']-1<start+self.request['segment_s']:
                        point.update(HoL_ms=None,p_ACK=None,statistics_quality='unavailable')
            if kind == 'finished':
                self.finished[role] = e.get('state', e)
            if kind == 'iperf_launch_call':
                self.state['traffic_started'] = True
            if kind == 'cw_verified':
                self.state['nodes'].setdefault(role, {})['readback'] = e['readback']
            self.event(kind, e.get('message', ''), node=role, receipt=e if kind!='finished' else {'wall_ns':e.get('wall_ns')})
            if strict and (kind in ('failure','error','transport_error') or kind=='eof' and (role not in self.finished or e.get('exit_code')!=0)):
                raise RuntimeError(role+' '+kind+': '+str(e.get('message',e.get('exit_code'))))
        return role, e

    def await_group(self, kind, roles, seconds):
        pending = set(roles)
        deadline = time.monotonic()+seconds
        receipts = {}
        while pending and time.monotonic()<deadline:
            self.check_cancel()
            self.heartbeat()
            role, row = self.receive()
            if row and row['event']==kind and role in pending:
                receipts[role] = row
                pending.remove(role)
        if pending:
            raise RuntimeError(kind+' timeout: '+', '.join(sorted(pending)))
        return receipts

    def preflight(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = dict(zip(self.roles, pool.map(lambda n: self.backend.call(n,'inspect'), self.roles)))
        check_radio(rows)
        self.state['inventory'] = rows
        self.state['clock_before'] = self.clock()
        prepared = self.prep.get(self.request['preparation_id'])
        for role, v in rows.items():
            if v['boot_id'] != prepared['nodes'][role]['boot_id']:
                raise RuntimeError(role+' reboot after preparation')
        ap = ipaddress.IPv4Interface(prepared['ap_cidr'])
        if str(ap) not in rows['AP']['ips']:
            raise RuntimeError('AP business address mismatch')
        ips = {'AP':str(ap.ip)}
        for n in STAS:
            choices = [str(x.ip) for x in map(ipaddress.IPv4Interface,rows[n]['ips']) if x.ip in ap.network]
            if len(choices)!=1:
                raise RuntimeError(n+' business address missing/ambiguous')
            ips[n] = choices[0]
        if len(set(ips.values()))!=len(ips):
            raise RuntimeError('duplicate business IPv4')
        self.state['addresses'] = ips
        self.save()
        return rows, ips

    def clock(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            clocks = dict(zip(self.roles,pool.map(self.backend.clock,self.roles)))
        self.state.setdefault('clock_checks',[]).append(clocks)
        self.save()
        source = self.backend.platform['controller']['ntp_source']
        for n, c in clocks.items():
            match = re.search(r'(?m)^\s*Server:\s+(\S+)',c['status'])
            if c['envelope_ms']>50 or 'NTPSynchronized=yes' not in c['status'] or not match or match[1]!=source:
                raise RuntimeError(n+' 50ms/continuous NTP gate')
        return clocks

    def trace_specs(self, rows, ips):
        import_reference()
        from cohort_probe_controller import prepare

        def one(role):
            probe = ProbeReader(self.backend.client(role),role,rows[role]['interface'])
            route = probe.run('ip route get '+shlex.quote(ips['AP'])+' from '+shlex.quote(ips[role]))
            if route['code'] or not re.search(r'\bdev '+re.escape(rows[role]['interface'])+r'\b',route['stdout']):
                raise RuntimeError(role+' business route not wireless')
            value = prepare(probe,MODULE)
            if value['expected_srcversion']!=rows[role]['srcversion']:
                raise RuntimeError(role+' trace/module identity mismatch')
            return value

        with ThreadPoolExecutor(max_workers=4) as pool:
            result = dict(zip(STAS,pool.map(one,STAS)))
        atomic_json(self.folder/'trace_specs.json',result)
        return result

    def sample_point(self, second):
        ap = self.samples[second].get('AP')
        if not ap or not all(n in self.samples[second] for n in STAS):
            return None
        bucket = second-1
        counts = ap['data']['udp_buckets'].get(str(bucket),{})
        values = {n:counts.get(str(p),0)*8/1e6 for n,p in PORTS.items()}
        data = self.samples[second]
        prev = self.samples.get(second-1,{})
        be = {n:int(data[n]['data']['BE']['hw_cwmin'])+1 for n in STAS}
        pairs = None
        warm, measure, step = (self.request[k] for k in ('warmup_s','measure_s','segment_s'))
        if self.request['mode'] in LONG_DEMO_MODES:
            sample_index = min(measure//step-1,max(0,(second-warm)//step))
            expected = self.stages.get(sample_index)
            actual = be
            if expected and isinstance(expected[STAS[0]],dict):
                pairs = {n:{k:int(data[n]['data']['BE']['hw_'+k]) for k in ('cwmin','cwmax')} for n in STAS}
                actual = pairs
            if actual!=expected:
                raise RuntimeError('逐STA硬件CW读回与已封存计划不一致')
        elif len(set(be.values()))!=1:
            raise RuntimeError('four STA hardware CW readbacks differ')
        offset = bucket-self.request['warmup_s']
        stable = 0<=offset<self.request['measure_s'] and offset%self.request['segment_s']>=self.request['guard_s'] and (offset+1)%self.request['segment_s']!=0
        ds = [interval_metrics(prev.get(n,{}).get('data',{}).get('dcf'),data[n]['data']['dcf']) for n in STAS]
        hol = p = None
        index=offset//self.request['segment_s']
        if stable and str(index) not in self.state.get('statistics_unavailable_segments',{}) and all(x is not None for x in ds):
            count = sum(x['hol_samples'] for x in ds)
            hol = sum(x['hol_sum_us'] for x in ds)/count/1000 if count else None
            attempts = sum(x['attempts'] for x in ds)
            if attempts and all(x['basic'] and x['failures']==0 for x in ds):
                p = sum(x['successes'] for x in ds)/attempts
        return {'second':second,'total_Mbps':sum(values.values()),'per_STA_Mbps':values,
                'W':be[STAS[0]] if len(set(be.values()))==1 else None,'per_STA_W':be,
                'HoL_ms':hol,'p_ACK':p,'stable':stable,'source':'AP receive payload / TX completion interval',
                'queue':{n:[data[n]['data']['qdepth'],data[n]['data']['pending']] for n in STAS},
                **({'per_STA_CW':pairs} if pairs is not None else {})}

    def continuous_score(self, index):
        request = self.request
        step = request['segment_s']
        start = request['warmup_s']+index*step
        points = [p for p in self.state['points']
                  if start+request['guard_s']<p['second']<=start+step-CONTINUOUS_SCORE_TAIL_S]
        if len(points)!=step-request['guard_s']-CONTINUOUS_SCORE_TAIL_S or ('MON15' in self.roles and not self.air_ok):
            return None
        # Sender membership is acquisition validation, never a policy argument.
        for node in request.get('active_senders', STAS):
            queues = [p['queue'][node] for p in points]
            if sum(d>0 for d,p in queues)<.95*len(queues) or sum(d>=2 or p>=3 for d,p in queues)<.90*len(queues):
                return None
        # Equal byte totals can differ by float rounding, well below one packet.
        return round(sum(p['total_Mbps'] for p in points)/len(points),9)

    def adaptation_gate(self, now):
        state = self.state
        if state['adaptation']['activation'] is not None:
            return None, '自适应已启用'
        if state['status']!='running' or self.cancel.is_set():
            return None, '等待连续业务启动'
        t0 = state['T0_ns']
        warm,measure,step = [self.request[k] for k in ('warmup_s','measure_s','segment_s')]
        elapsed = (now-t0)//NS-warm
        complete = elapsed//step
        if elapsed<CONTINUOUS_BASELINE_S:
            return None, '等待预热及至少30秒固定W基线'
        # Allow one recovery segment if the first candidate loses.
        latest = measure//step-len(CONTINUOUS_TRIALS)-1-CONTINUOUS_HOLD_SEGMENTS
        first = next((k for k in range(complete,latest+1)
                      if k not in self.sent and t0+(warm+k*step)*NS-now>STAGE_LEAD_NS),None)
        if first is None:
            return None, '剩余时间不足以完成逐档比较和60秒保持'
        if self.continuous_score(complete-1) is None:
            return None, '最近的完整基线段缺少吞吐、空口或积压证据'
        return (first,[complete-1]), '可启用自适应'

    def enable_adaptation(self):
        with self.lock:
            adaptation = self.state['adaptation']
            if adaptation['activation'] is not None:
                return
            now = time.time_ns()
            plan, reason = self.adaptation_gate(now)
            if plan is None:
                raise ValueError(reason)
            first, baselines = plan
            adaptation['activation'] = {'at_ns':now,'first_block':first,'baseline_blocks':baselines,
                                        'baseline_Mbps':self.continuous_score(baselines[0])}
            self.event('adaptation_enabled','用户启用自适应；下一未封存边界开始探索',index=first)

    def continuous_choice(self, index):
        adaptation = self.state['adaptation']
        activation = adaptation['activation']
        candidates = adaptation['candidates']
        relative = index-activation['first_block'] if activation else -1
        if relative<0:
            key, phase, reason = 'baseline', 'baseline', '保持固定基线'
        elif relative==0:
            score = self.continuous_score(index-1)
            adaptation['accepted'] = {'candidate':'baseline',
                                      'Mbps':activation['baseline_Mbps'] if score is None else score,
                                      'block':activation['baseline_blocks'][0] if score is None else index-1}
            key, phase, reason = CONTINUOUS_TRIALS[0], 'explore', '测试第一档候选'
        elif adaptation['selection'] is not None:
            key, phase, reason = adaptation['selection']['candidate'], 'hold', '保持已选配置'
        else:
            key, phase, reason = self.continuous_next(index)
        adaptation['blocks'][str(index)] = {'candidate':key,'phase':phase,'W':candidates[key]}
        return candidates[key], reason

    def continuous_next(self, index):
        adaptation = self.state['adaptation']
        accepted = adaptation['accepted']
        previous = adaptation['blocks'][str(index-1)]
        score = self.continuous_score(index-1)
        decisions = adaptation['decisions']
        if previous['phase']=='fallback':
            if score is not None:
                accepted.update(Mbps=score, block=index-1)
            return CONTINUOUS_TRIALS[len(decisions)], 'explore', '测试下一档候选'

        candidate = previous['candidate']
        improved = score is not None and score>accepted['Mbps']
        decisions.append({'candidate':candidate,'block':index-1,'candidate_Mbps':score,
                          'previous_candidate':accepted['candidate'],'previous_block':accepted['block'],
                          'previous_Mbps':accepted['Mbps'],'accepted':improved})
        if improved:
            accepted.update(candidate=candidate, Mbps=score, block=index-1)
        key = accepted['candidate']
        if len(decisions)==len(CONTINUOUS_TRIALS):
            adaptation['selection'] = {'candidate':key,'hold_block':index,'W':adaptation['candidates'][key]}
            return key, 'hold', '保留吞吐更高的候选' if improved else '恢复并保持上一档已采用的 W'
        if improved:
            return CONTINUOUS_TRIALS[len(decisions)], 'explore', '已采用吞吐更高的候选，继续测试下一档'
        return key, 'fallback', '候选吞吐未提升或评分不可用，恢复上一档已采用的 W'

    def control_availability(self, now):
        control = self.state['control']
        t0 = self.state['T0_ns']
        warm,measure,step = [self.request[k] for k in ('warmup_s','measure_s','segment_s')]
        elapsed_ns = now-t0 if t0 is not None else -NS
        index = min(measure//step-1,max(0,(elapsed_ns//NS-warm)//step))
        active = control['blocks'].get(str(index),control['blocks']['0'])
        pending = control['pending']
        available = self.state['status']=='running' and warm*NS<=elapsed_ns<(warm+measure)*NS and not self.cancel.is_set()
        upcoming = control['blocks'].get(str(index+1),{})
        if not pending and upcoming.get('action'):
            pending = {'segment':index+1,'action':upcoming['action'],'W':upcoming['W'],
                       **({'CW':upcoming['per_STA_CW']} if 'per_STA_CW' in upcoming else {})}
        next_index = next((k for k in range(index+1,measure//step)
                           if k not in self.sent and (warm+k*step)*NS-elapsed_ns>STAGE_LEAD_NS),None)
        points = self.state['points']
        current_readback = bool(points and points[-1]['second']>=warm+index*step)
        command_ready = available and next_index is not None and not pending and current_readback
        recent_score = self.continuous_score(index) if available and active['mode']=='manual' else None
        prior = control['blocks'].get(str(index-1),{})
        if (available and recent_score is None and index
                and prior.get('per_STA_CW',prior.get('per_STA_W')) == active.get('per_STA_CW',active.get('per_STA_W'))):
            recent_score = self.continuous_score(index-1)
        return {'mode':active['mode'],'pending':pending,'next_segment':next_index,
                'manual':command_ready and active['mode']=='manual',
                'adapt':command_ready and active['mode']=='manual' and recent_score is not None,
                'takeback':command_ready and active['mode']=='auto'}

    def request_control(self, action, w=None, cw=None):
        with self.lock:
            availability = self.control_availability(time.time_ns())
            if not availability[action]:
                raise ValueError('当前操作不可用，请等待有效吞吐样本、已提交切换生效或下一轮演示')
            pending = {'action':action,'segment':availability['next_segment']}
            if action=='manual':
                if cw is not None:
                    pending['CW'] = sta_cw_pairs(cw)
                else:
                    pending['W'] = sta_windows(w)
            self.state['control']['pending'] = pending
            self.event('control_request','等待下一个未封存的共同分段边界',**pending)

    def unified_choice(self, index):
        control = self.state['control']
        blocks = control['blocks']
        previous = blocks[str(index-1)]
        mode = previous['mode']
        settings = previous.get('per_STA_CW') or sta_windows(previous.get('per_STA_W',previous['W']))
        pending = control['pending']
        action = pending['action'] if pending and pending['segment']==index else None
        block = {}
        reason = '保持当前手动 W'
        if action:
            control['pending'] = None
            block['action'] = action
        if action=='adapt':
            mode = 'auto'
            control['epoch_start'] = index
            control['hold_state']={}
        elif action in ('manual','takeback'):
            mode = 'manual'
            if action=='manual':
                settings = pending['CW'] if 'CW' in pending else sta_windows(pending['W'])
            reason = '应用手动 W' if action=='manual' else '接回手动，保持当前 W'
        if mode=='auto':
            # Only this takeover epoch's uniform AUTO segments train the arms.
            # Manual configurations are not rewards.
            recent = [(blocks[str(k)]['W'],self.continuous_score(k))
                      for k in range(max(control['epoch_start'],index-UCB_LOOKBACK),index)]
            prior_reward=recent[-1][1] if recent else None
            chosen,reason,estimates = hold_ucb_step(control.setdefault('hold_state',{}),
                uniform_window(settings),prior_reward,estimator=throughput_ucb)
            # A scalar W tells the node to apply the full K3 profile.
            settings = dict.fromkeys(STAS,chosen)
            block['estimates'] = estimates
            block['previous_reward_Mbps'] = prior_reward
            block['hold_status']=copy.deepcopy(control['hold_state'])
        blocks[str(index)] = {**block,'mode':mode,**stage_block(settings),'reason':reason}
        return settings,reason

    def online_choice(self, index):
        if self.request['mode']=='unified':
            return self.unified_choice(index)
        if self.request['mode']=='continuous':
            return self.continuous_choice(index)
        if self.request['mode']=='manual':
            with self.lock:
                value = self.pending_w if self.pending_w is not None else self.stages[index-1]
                self.pending_w = None
                self.state['manual_pending'] = None
                return value,'手动提交或保持当前CW'
        if self.request['mode']=='fixed':
            return self.request['initial_w'],'固定CW对照'
        if index in (1,2):
            return (32,64)[index-1],'预登记候选探索'
        for k in range(index-1):
            if k in self.evaluated:
                continue
            a = self.request['warmup_s']+k*self.request['segment_s']
            pts = [p for p in self.state['points'] if a+3<p['second']<a+self.request['segment_s'] and p['stable']]
            if len(pts)!=self.request['segment_s']-4 or not self.air_ok:
                raise RuntimeError('自动决策所需完整稳定段/空口核验缺失')
            for n in STAS:
                if sum(p['queue'][n][0]>0 for p in pts)/len(pts)<.95 or sum(p['queue'][n][0]>=2 or p['queue'][n][1]>=3 for p in pts)/len(pts)<.90:
                    raise RuntimeError(n+' 自动评价段饱和门禁')
            w = self.stages[k]
            self.policy.update(w,sum(p['total_Mbps'] for p in pts)/len(pts),len(pts))
            self.evaluated.add(k)
        options = {w:r['mean'] for w,r in self.policy.estimates.items() if r['mean'] is not None}
        if not options:
            raise RuntimeError('没有可用的吞吐奖励')
        if index>=4 and self.policy.rng.random()<.1:
            return self.policy.rng.choice(sorted(options)),'ε=0.1探索已验收候选'
        return max(options,key=options.get),'按已完成稳定段的吞吐选择'

    def traffic(self, rows, ips):
        specs = self.trace_specs(rows,ips)
        root = ROOT/'experiments'
        files = [ROOT/'labconsole/demo_node.py',ROOT/'labconsole/cw_policy.py',ROOT/'labconsole/readonly_probe.py',
                 root/'reg_access.py',root/'cohort_node_capture.py',root/'air_timing.py']
        macs = {n:mac(rows[n]['info']) for n in STAS}
        self.state['macs'] = {'AP':mac(rows['AP']['info']),**macs}
        self.event('deploying','复用已有追踪定义，启动本次独立AP、STA、MON15 worker')

        def launch(role):
            start = self.stages[0][role] if self.request['mode']=='unified' and role in STAS else self.request['initial_w']
            initial = {'initial_cw':start} if isinstance(start,dict) else {'initial_w':start}
            self.workers[role].launch({**self.request,
                **initial,
                'role':role,'id':self.id,
                'output':'/tmp/ath9k-demo-'+self.id+'/'+role,'interface':rows[role]['interface'],'phy':rows[role]['phy'],
                'ap_ip':ips['AP'],'ap_mac':self.state['macs']['AP'],'sta_macs':macs,'sta_ports':PORTS,
                'ports':[PORTS[n] for n in self.request.get('active_senders',STAS)],
                'port':PORTS.get(role),'own_ip':ips.get(role),'trace_spec':specs.get(role)},files)

        self.workers = {r:RemoteWorker(r,self.backend,self.messages,self.folder/'raw'/r) for r in self.roles}
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(launch,self.roles))
        self.last_heartbeat = 0
        self.state['originals'] = self.await_group('armed',self.roles,24)
        self.heartbeat()
        self.state['clock_pre_arm'] = self.clock()
        self.check_cancel()
        t0 = time.time_ns()+20*NS
        self.state.update(T0_ns=t0,status='armed',phase='等待共同T0')
        if self.request['mode']=='continuous':
            self.continuous_choice(0)
        self.save()
        self.send_all({'op':'plan','T0_ns':t0,
                       'ap_clock_offset_ns':round(self.state['clock_pre_arm']['AP']['offset_ms']*1e6)})
        self.await_group('plan_received',self.roles,7)
        warm,measure,tail,seg = [self.request[k] for k in ('warmup_s','measure_s','tail_s','segment_s')]
        deadline = t0+(warm+measure+tail+45)*NS
        acknowledgements = collections.defaultdict(set)
        while len(self.finished)<len(self.roles) and time.time_ns()<deadline:
            self.check_cancel()
            self.heartbeat()
            role,e = self.receive()
            if e and e['event']=='stage_received':
                index = e['index']
                settings = self.stages.get(index)
                target = settings.get(role) if isinstance(settings,dict) else settings
                if role not in STAS or index not in self.sent or e.get('CW',e.get('W'))!=target or e['apply_ns']!=t0+(warm+index*seg)*NS:
                    raise RuntimeError(role+' CW stage acknowledgement disagrees with the plan')
                acknowledgements[index].add(role)
            now = time.time_ns()
            elapsed = (now-t0)/NS
            if elapsed>=0:
                self.state.update(status='running',phase='预热' if elapsed<warm else '动态测量' if elapsed<warm+measure else '收尾与恢复')
            for k in range(1,measure//seg):
                at = t0+(warm+k*seg)*NS
                if k in self.sent and at-now<NS and acknowledgements[k]!=set(STAS):
                    raise RuntimeError('four STA CW command acknowledgements missing')
                if k not in self.sent and at-now<=STAGE_LEAD_NS:
                    with self.lock:
                        w, reason = self.online_choice(k)
                        self.stages[k] = w
                        self.sent.add(k)
                    fields = {'CW':w} if isinstance(w,dict) and isinstance(w[STAS[0]],dict) else {'W':w}
                    self.event('stage_plan',reason,index=k,apply_ns=at,**fields)
                    self.send_stage(k,w,at)
            if e and e['event']=='sample':
                seconds = {p['second'] for p in self.state['points']}
                k = e['second']
                if k not in seconds:
                    point = self.sample_point(k)
                    if point:
                        with self.lock:
                            self.state['points'].append(point)
                            self.state['points'].sort(key=lambda p:p['second'])
                            self.save()
        if set(self.finished)!=set(self.roles):
            raise RuntimeError('node final receipts incomplete')
        await_clean_exits(self)
        if any(v.get('error') or not v.get('completed_schedule') for v in self.finished.values()):
            raise RuntimeError('node schedule failed')
        self.state['clock_after'] = self.clock()
        blocks = self.state.get('control',{}).get('blocks',{})
        pairs = {k:b['per_STA_CW'] for k,b in blocks.items() if 'per_STA_CW' in b}
        if pairs:
            self.state['applied_CW'] = pairs
        self.state['applied_W'] = {k:stage_windows(w) if isinstance(w,dict) else w for k,w in self.stages.items()}
        self.save()

    def cleanup(self, failed):
        if failed:
            for n,w in self.workers.items():
                if w.channel and n not in self.finished:
                    try:
                        w.send({'op':'abort','reason':self.state.get('error')})
                    except Exception:
                        pass
        deadline = time.monotonic()+48
        started = {n for n,w in self.workers.items() if w.channel}
        while started-set(self.finished) and time.monotonic()<deadline:
            self.receive(timeout=.25,strict=False)
        missing = sorted(started-set(self.finished))
        node_ok = all(v.get('restoration_confirmed',v.get('restoration',{}).get('confirmed',False)) for v in self.finished.values())
        errors = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            jobs = {n:pool.submit(w.collect) for n,w in self.workers.items()}
            for n,f in jobs.items():
                try:
                    f.result()
                except Exception as exc:
                    errors.append(n+' '+str(exc))
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                post = dict(zip(self.roles,pool.map(lambda n:self.backend.call(n,'inspect'),self.roles)))
            check_radio(post)
            for n in self.roles:
                if n in self.state.get('inventory',{}) and post[n]['boot_id']!=self.state['inventory'][n]['boot_id']:
                    raise RuntimeError(n+' reboot after run')
            self.state['postflight'] = post
        except Exception as exc:
            node_ok = False
            errors.append('postflight '+str(exc))
        restored = not missing and node_ok
        self.state.update(restoration={'confirmed':restored,'missing_nodes':missing,'archive_errors':errors},final_nodes=self.finished)
        self.save()
        return restored, errors

    def run(self):
        failed = False
        restored = False
        try:
            rows, ips = self.preflight()
            self.check_cancel()
            self.traffic(rows,ips)
        except Exception as exc:
            failed = True
            self.state['error'] = str(exc)
            self.event('gate_stop',str(exc))
        try:
            self.state.update(status='restoring',phase='回收记录并核对恢复')
            self.save()
            restored, errors = self.cleanup(failed)
            if errors or not restored:
                failed = True
                self.state['error'] = self.state.get('error') or '; '.join(errors) or '恢复未确认'
            if not failed:
                from .demo_analysis import analyze
                result = analyze(self.folder,self.state)
                self.state['analysis'] = result
                self.state['statistics_quality']='partial_NA' if result.get('warnings') else 'available'
                if result['issues']:
                    failed = True
                    self.state['error'] = '; '.join(result['issues'])
        except Exception as exc:
            failed = True
            self.state['error'] = self.state.get('error') or str(exc)
        finally:
            status = 'completed' if not failed else 'stopped' if self.state.get('abort_reason')=='用户停止演示' and restored else 'failed'
            try:
                self.state.update(status=status,phase='已结束' if status=='completed' else '已停止')
                self.save()
            finally:
                self.prep.release_demo(self.id,restored)


class DemoHub:
    def __init__(self, workspace, preparation):
        self.root = Path(workspace)/'real_demos'
        self.root.mkdir(exist_ok=True)
        self.prep = preparation
        self.lock = threading.RLock()
        self.active = None
        self.thread = None
        for p in self.root.glob('*/session.json'):
            if p.is_symlink() or p.parent.is_symlink():
                continue
            v = json.loads(p.read_text(encoding='utf-8'))
            if v['status'] not in TERMINAL:
                v.update(status='interrupted',error='控制台中断，请核对远端恢复回执')
                atomic_json(p,v)

    def get(self, identity):
        if not isinstance(identity,str) or not re.fullmatch('[a-f0-9]{32}',identity):
            raise ValueError('演示标识无效')
        with self.lock:
            p = self.root/identity/'session.json'
            if not p.is_file() or p.is_symlink() or p.parent.is_symlink():
                raise ValueError('找不到演示会话')
            if self.active and self.active.id==identity:
                with self.active.lock:
                    value = json.loads(p.read_text(encoding='utf-8'))
                    if value.get('adaptation'):
                        plan, reason = self.active.adaptation_gate(time.time_ns())
                        value['adaptation_control'] = {'enabled':plan is not None,'message':reason}
                    if value.get('control'):
                        value['control_actions'] = self.active.control_availability(time.time_ns())
                    return value
            return json.loads(p.read_text(encoding='utf-8'))

    def listing(self):
        with self.lock:
            rows = []
            for p in self.root.glob('*/session.json'):
                v = self.get(p.parent.name)
                rows.append({k:v.get(k) for k in ('id','status','request','created_ns','restoration','error')})
            rows.sort(key=lambda x:x['created_ns'],reverse=True)
            preparations = self.prep.listing()
            current = next((v for v in preparations if v['status'] not in ('ended','failed_clean','recovery_required')),None)
            recovery = next((v for v in preparations if v['status']=='recovery_required'),None)
            prep = current or recovery or next(iter(preparations),None)
            status = prep['status'] if prep else None
            needs_recovery = bool(prep and (status=='recovery_required' or prep.get('demo_recovery_required')))
            reusable = bool(status=='ready' and prep['preparation_ready'] and not needs_recovery)
            return {'sessions':rows,'ready_preparation':prep['id'] if reusable else None,
                    'preparation_status':status,'preparation_recovery_required':needs_recovery,
                    'demo_busy':bool((self.thread and self.thread.is_alive()) or (prep and prep.get('demo_owner')))}

    def start(self, data):
        req = validate_request(data)
        with self.lock:
            summary = self.listing()
            for v in summary['sessions']:
                if v['request']['request_key']==req['request_key']:
                    prior = {'access_mode':'basic',**v['request']}
                    if prior['mode']=='unified':
                        if 'initial_per_STA_CW' not in prior:
                            prior.setdefault('initial_per_STA_W',sta_windows(prior['initial_w']))
                        prior.setdefault('active_senders',list(STAS))
                    if prior!=req:
                        raise ValueError('重复请求内容变化')
                    return self.get(v['id'])
            if self.thread and self.thread.is_alive():
                raise ValueError('已有现场演示运行或正在恢复')
            if summary['ready_preparation']!=req['preparation_id']:
                raise ValueError('先完成环境准备和时间同步核验')
            reserve = 5*1024**3
            if req['mode'] in LONG_DEMO_MODES:
                reserve += len(ROLES)*sum(req[k] for k in ('warmup_s','measure_s','tail_s'))*8*1024**2
            if shutil.disk_usage(self.root).free<reserve:
                raise ValueError(f'控制机可用空间不足 {reserve/1024**3:.1f} GiB')
            folder = self.root/uuid.uuid4().hex
            folder.mkdir()
            session = Session(folder,self.prep,req)
            session.backend = self.prep.claim_demo(req['preparation_id'],session.id,session.abort)
            try:
                prepared=self.prep.get(req['preparation_id'])
                platform=prepared.get('platform',{}) if isinstance(prepared,dict) else {}
                session.roles=enabled_roles(platform)
                session.state.update(roles=list(session.roles),air_evidence='required' if 'MON15' in session.roles else 'not_collected_MON15_disabled')
                paths = ['real_demo.py','demo_node.py','cw_policy.py','demo_analysis.py','finish_protocol.py','config.py','preparation.py']
                session.state['code_sha256'] = {p:hashlib.sha256((ROOT/'labconsole'/p).read_bytes()).hexdigest() for p in paths}
                shutil.copyfile(ROOT/'EXPERIMENT_RULES.md',folder/'rules_snapshot.md')
                session.save()
                self.active = session
                self.thread = threading.Thread(target=session.run,daemon=True)
                self.thread.start()
            except Exception:
                self.prep.release_demo(session.id,True)
                raise
            return self.get(session.id)

    def command(self, data):
        with self.lock:
            v = self.get(data.get('id'))
            s = self.active
            if not s or v['id']!=s.id or v['status'] not in ('preparing','armed','running'):
                raise ValueError('请选择正在运行的演示')
            if data.get('action')=='stop':
                s.abort('用户停止演示')
            elif s.request['mode']=='unified' and data.get('action') in ('manual','adapt','takeback'):
                action = data['action']
                if action=='manual' and 'CW' in data:
                    if 'W' in data:
                        raise ValueError('手动 CW 与旧版 W 设置只能提交一组')
                    s.request_control(action,cw=sta_cw_pairs(data['CW']))
                else:
                    s.request_control(action,data.get('W'))
            elif data.get('action')=='adapt' and s.request['mode']=='continuous':
                s.enable_adaptation()
            elif data.get('action')=='manual' and s.request['mode']=='manual':
                w = data.get('W')
                if type(w) is not int or w not in WINDOWS:
                    raise ValueError('W无效')
                with s.lock:
                    remaining = [k for k in (1,2) if k not in s.sent]
                    if not remaining:
                        raise ValueError('本轮剩余测量段已封存')
                    s.pending_w = w
                    s.state['manual_pending'] = {'W':w,'segment':remaining[0]}
                    s.event('manual_request','用户提交CW，等待下一个未封存段',W=w)
            else:
                raise ValueError('此模式不接受手动修改CW')
            return self.get(s.id)

    def close(self):
        if self.active and self.thread and self.thread.is_alive():
            self.active.abort('控制台关闭')
            self.thread.join(80)
