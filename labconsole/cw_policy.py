"""CW reward policy shared by real on-site demonstrations."""
import math
import random
import statistics
WINDOWS=(8,16,32,64,128)
STAS=("STA1","STA2","STA3","STA4")
ACCESS_SETTINGS = {'basic': ('off', '1'), 'rts': ('500', '0')}
LONG_DEMO_MODES = ('continuous', 'unified')

CONTINUOUS_TRIALS = ('A', 'B')
CONTINUOUS_HOLD_SEGMENTS = 3
UCB_ARMS = (8, 16)
UCB_WINDOW = 6
UCB_BONUS_MBPS = 1.0
HOLD_DROP_FRACTION = .10
HOLD_DROP_SEGMENTS = 2
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
    state.setdefault('drop_streak',0)
    state.setdefault('hold_recent',[])
    valid=type(reward) in (int,float) and math.isfinite(reward) and reward>=0
    if state.get('last_action') is not None:
        if not valid:
            state['drop_streak']=0;state['hold_recent']=[]
            _,_,rows=estimator(state['observations'])
            return previous_w,'上一段评分不可用，维持当前 W，不更新基准',rows
        if state['phase']=='hold':
            reference=state['reference_Mbps']
            state['hold_recent']=(state['hold_recent']+[reward])[-3:]
            dropped=reference>0 and reward<=reference*(1-HOLD_DROP_FRACTION)
            state['drop_streak']=state['drop_streak']+1 if dropped else 0
            if len(state['hold_recent'])==3:
                state['reference_Mbps']=max(reference,statistics.median(state['hold_recent']))
            if state['drop_streak']<HOLD_DROP_SEGMENTS:
                _,_,rows=estimator(state['observations'])
                state['last_action']=previous_w
                return previous_w,('吞吐下降观察中（1/2），保持当前 W' if dropped else '吞吐保持稳定，保持当前 W'),rows
            state.update(phase='explore',observations=[],incumbent=previous_w,
                         drop_streak=0,hold_recent=[],trigger='连续两段吞吐较保持基准下降至少10%')
        else:
            state['observations'].append((previous_w,reward))
    _,_,rows=estimator(state['observations'])
    if all(r['count']>=HOLD_SAMPLES_PER_ARM for r in rows):
        best=max(rows,key=lambda r:r['mean_Mbps'])
        incumbent=state.get('incumbent')
        chosen=best['W']
        if incumbent in UCB_ARMS:
            old=next(r for r in rows if r['W']==incumbent)
            if best['mean_Mbps']<=old['mean_Mbps']*(1+HOLD_SWITCH_MARGIN):chosen=incumbent
        reference=next(r['mean_Mbps'] for r in rows if r['W']==chosen)
        state.update(phase='hold',reference_Mbps=reference,last_action=chosen,drop_streak=0,hold_recent=[])
        return chosen,'本次探索完成，保持较好 W；复探换档须比原档高5%以上',rows
    # Bounded four-valid-segment coverage prevents a lucky first sample locking in.
    under=[r for r in rows if r['count']<HOLD_SAMPLES_PER_ARM]
    order=(16,8) if state.get('incumbent')==8 else UCB_ARMS
    unseen=[r for r in under if not r['count']]
    chosen=(min(unseen,key=lambda r:order.index(r['W'])) if unseen
            else max(under,key=lambda r:r['ucb_Mbps']))['W']
    state['last_action']=chosen
    return chosen,('吞吐持续下降，重新探索 W' if state.get('trigger') and not state['observations'] else '探索候选，每档收集两个有效段'),rows


def sta_windows(value):
    """Validate a uniform W or an exact four-STA mapping, without coercion."""
    settings = dict.fromkeys(STAS, value) if type(value) is int else value
    if (not isinstance(settings, dict) or set(settings) != set(STAS)
            or any(type(w) is not int or w not in WINDOWS for w in settings.values())):
        raise ValueError('每台STA的W只允许8、16、32、64、128，且须包含四台STA')
    return dict(settings)


def uniform_window(settings):
    settings = sta_windows(settings)
    values = set(settings.values())
    return next(iter(values)) if len(values) == 1 else None


def throughput_ucb(recent):
    """Two arms, recent segment rewards only; no sender count or scenario input.

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
