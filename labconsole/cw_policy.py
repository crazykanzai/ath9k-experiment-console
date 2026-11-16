"""CW reward policy shared by real on-site demonstrations."""
import math
import random
WINDOWS=(8,16,32,64,128)
CW_VALUES=tuple((1 << k)-1 for k in range(1,11))
STAS=("STA1","STA2","STA3","STA4")
ACCESS_SETTINGS = {'basic': ('off', '1'), 'rts': ('500', '0')}
LONG_DEMO_MODES = ('continuous', 'unified')
UNIFIED_SEGMENT_S = 10
UNIFIED_GUARD_S = 2

CONTINUOUS_TRIALS = ('A', 'B')
CONTINUOUS_HOLD_SEGMENTS = 3
UCB_ARMS = (8, 16, 32)
UCB_WINDOW = 6
UCB_BONUS_MBPS = 1.0
HOLD_DROP_FRACTION = .05
HOLD_SWITCH_MARGIN = .05
HOLD_SAMPLES_PER_ARM = 2


def hold_ucb_step(state, previous_w, reward, estimator=None):
    """Feedback-only, event-triggered exploration; one call per new segment.

    A hold has no periodic probes. It cannot discover an improved other arm if
    the current arm never degrades. All thresholds are fixed engineering values.
    """
    estimator = estimator or throughput_ucb
    state.setdefault('phase','explore')
    state.setdefault('observations',[])
    state.pop('hold_comparison',None)
    valid=type(reward) in (int,float) and math.isfinite(reward) and reward>=0
    if state.get('last_action') is not None:
        if not valid:
            _,_,rows=estimator(state['observations'])
            return previous_w,'上一段评分不可用，保持当前 W',rows
        if state['phase']=='hold':
            reference=state['last_same_w_Mbps']
            threshold=round(reference*(1-HOLD_DROP_FRACTION),9)
            state['hold_comparison']={'W':previous_w,'previous_Mbps':reference,
                                      'observed_Mbps':reward,'threshold_Mbps':threshold}
            state['last_same_w_Mbps']=reward
            if reference<=0 or reward>threshold:
                _,_,rows=estimator(state['observations'])
                return previous_w,'同档吞吐未下降到触发线，保持当前 W',rows
            state.update(phase='explore',observations=[],incumbent=previous_w,
                         trigger='较上一有效同档段下降至少5%，重新探索 W')
        else:
            state['observations'].append((previous_w,reward))
    _,_,rows=estimator(state['observations'])
    if all(r['count']>=HOLD_SAMPLES_PER_ARM for r in rows):
        best=max(rows,key=lambda r:r['mean_Mbps'])
        incumbent=state.get('incumbent')
        chosen=best['W']
        old=next((r for r in rows if r['W']==incumbent),None)
        if old is not None:
            if best['mean_Mbps']<=old['mean_Mbps']*(1+HOLD_SWITCH_MARGIN):chosen=incumbent
        reference=next(value for w,value in reversed(state['observations']) if w==chosen)
        state.update(phase='hold',last_same_w_Mbps=reference,last_action=chosen)
        state.pop('trigger',None)
        return chosen,'本次探索完成，保持较好 W；复探换档须比原档高5%以上',rows
    # Cover every candidate twice before accepting an initial winner.
    under=[r for r in rows if r['count']<HOLD_SAMPLES_PER_ARM]
    incumbent=state.get('incumbent')
    unseen=[r for r in under if not r['count']]
    chosen=(min(unseen,key=lambda r:r['W']==incumbent) if unseen
            else max(under,key=lambda r:r['ucb_Mbps']))['W']
    state['last_action']=chosen
    return chosen,(state['trigger'] if state.get('trigger') and not state['observations'] else '探索候选，每档收集两个有效段'),rows


def sta_windows(value):
    """Validate a uniform W or an exact four-STA mapping, without coercion."""
    settings = dict.fromkeys(STAS, value) if type(value) is int else value
    if (not isinstance(settings, dict) or set(settings) != set(STAS)
            or any(type(w) is not int or w not in WINDOWS for w in settings.values())):
        raise ValueError('每台STA的W只允许8、16、32、64、128，且须包含四台STA')
    return dict(settings)


def cw_pair(value):
    if type(value) is int and value in WINDOWS:
        return {'cwmin':value-1, 'cwmax':8*value-1}
    if (not isinstance(value,dict) or set(value)!={'cwmin','cwmax'}
            or any(type(v) is not int or v not in CW_VALUES for v in value.values())
            or value['cwmin']>value['cwmax']):
        raise ValueError('CWmin 和 CWmax 须为 1 至 1023 范围内的 2^k−1，且 CWmin ≤ CWmax')
    return {'cwmin':value['cwmin'], 'cwmax':value['cwmax']}


def sta_cw_pairs(value):
    if not isinstance(value,dict) or set(value)!=set(STAS) or not all(isinstance(v,dict) for v in value.values()):
        raise ValueError('须为四台 STA 分别填写 CWmin 和 CWmax')
    return {n:cw_pair(value[n]) for n in STAS}


def stage_windows(settings):
    return {n:value['cwmin']+1 if isinstance(value,dict) else value for n,value in settings.items()}


def uniform_window(settings):
    values = set(stage_windows(settings).values())
    return next(iter(values)) if len(values) == 1 else None


def throughput_ucb(recent):
    """UCB scores for uniform CW candidates using recent segment rewards.

    The engineering exploration amplitude is fixed at 1 Mbps. It is not a
    confidence interval or a claim of stationary-bandit theoretical guarantees.
    Missing rewards age out with their segment; measured zero is an observation.
    """
    observations = {w: [] for w in UCB_ARMS}
    for w, reward in list(recent)[-UCB_WINDOW:]:
        if (type(w) is int and w in observations and type(reward) in (int, float)
                and math.isfinite(reward) and reward >= 0):
            observations[w].append(reward)
    total = sum(map(len, observations.values()))
    estimates = []
    for w, rewards in observations.items():
        count = len(rewards)
        mean = sum(rewards)/count if count else None
        bonus = UCB_BONUS_MBPS*math.sqrt(2*math.log(max(2, total))/count) if count else None
        estimates.append({'W': w, 'count': count, 'mean_Mbps': mean,
                          'bonus_Mbps': bonus, 'ucb_Mbps': mean+bonus if count else None})
    unseen = next((r for r in estimates if not r['count']), None)
    if unseen:
        return unseen['W'], '探索近期没有有效样本的 W', estimates
    chosen = max(estimates, key=lambda r: r['ucb_Mbps'])
    return chosen['W'], '按近期吞吐与探索奖励选择 W', estimates


def continuous_candidates(initial_w, access_mode='basic'):
    # Historical four-STA means provide a mode-specific order for exploration.
    # The ranking supplies candidates only. Live AP samples decide what to keep.
    order = (8, 16, 32) if access_mode=='rts' else (32, 16, 8)
    first, second = [w for w in order if w != initial_w][:2]
    return {name: dict.fromkeys(STAS, w)
            for name, w in [('baseline', initial_w), ('A', first), ('B', second)]}


def sliding_window_ucb(anchor_w, access_mode, recent):
    """Choose among the takeover W and two candidates using recent segment rewards."""
    candidates = continuous_candidates(anchor_w, access_mode)
    arms = [row[STAS[0]] for row in candidates.values()]
    observations = {w: [] for w in arms}
    for w, reward in recent:
        if w in observations and reward is not None:
            observations[w].append(reward)
    total = sum(map(len, observations.values()))
    estimates = []
    for w, rewards in observations.items():
        count = len(rewards)
        mean = sum(rewards)/count if count else None
        bonus = math.sqrt(2*math.log(max(2,total))/count) if count else None
        estimates.append({'W':w,'count':count,'mean_Mbps':mean,'bonus_Mbps':bonus,
                          'ucb_Mbps':mean+bonus if count else None})
    unseen = next((row for row in estimates if not row['count']),None)
    if unseen:
        return unseen['W'], '探索近期没有有效样本的 W', estimates
    chosen = max(estimates,key=lambda row:row['ucb_Mbps'])
    return chosen['W'], '选择近期平均吞吐与探索奖励之和最高的 W', estimates


def air_mode_issue(sample, access_mode):
    if sample['data']<10 or set(sample['rates'])!={'24.0'}:
        return 'insufficient UDP DATA or non-24Mbps DATA'
    if access_mode=='basic' and (sample['rts'] or sample['cts']):
        return 'unexpected RTS/CTS in Basic mode'
    if access_mode=='rts' and (not sample['rts'] or not sample['cts']):
        return 'missing RTS or CTS evidence in RTS/CTS mode'
    return None


class Policy:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.order = list(WINDOWS)
        self.rng.shuffle(self.order)
        self.estimates = {w: {'mean': None, 'segments': 0, 'seconds': 0} for w in WINDOWS}

    def choose(self):
        for w in self.order:
            if self.estimates[w]['mean'] is None:
                return w, '初始化探索：尚无有效稳定段'
        if self.rng.random() < .1:
            return self.rng.choice(list(WINDOWS)), 'ε=0.1探索'
        # Randomize equal-score ties; never bake in the historically best CW.
        values = list(WINDOWS); self.rng.shuffle(values)
        return max(values, key=lambda w: self.estimates[w]['mean']), '利用近期平均奖励'

    def update(self, w, throughput, seconds):
        row = self.estimates[w]
        row['mean'] = throughput if row['mean'] is None else .7*row['mean'] + .3*throughput
        row['segments'] += 1; row['seconds'] += seconds
