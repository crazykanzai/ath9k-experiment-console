"""Explicit business sender identity; legacy single/four defaults preserved.

All four nodes are still associated, configured, armed and health-checked.
Ports retain their stable STA mapping even when some servers are unused.
"""
STAS=('STA1','STA2','STA3','STA4')
PORTS=(5201,5202,5203,5204)

def normalize_senders(values):
    if not isinstance(values,(list,tuple)) or not values or any(n not in STAS for n in values):
        raise ValueError('active_stas must be a nonempty list of STA1..STA4')
    if len(set(values))!=len(values):raise ValueError('duplicate active STA')
    return [n for n in STAS if n in values]

def active_from_parameters(parameters):
    explicit=parameters.get('active_stas')
    single=parameters.get('single_sta')
    selected=normalize_senders(explicit) if explicit is not None else normalize_senders([single]) if single is not None else list(STAS)
    if single is not None and selected!=normalize_senders([single]):raise ValueError('single_sta conflicts with active_stas')
    expected=[PORTS[STAS.index(n)] for n in selected]
    if 'active_ports' in parameters and parameters['active_ports']!=expected:raise ValueError('active ports do not match STA identities')
    if 'n_active' in parameters and parameters['n_active']!=len(selected):raise ValueError('n_active conflicts with sender list')
    return selected

def active_from_manifest(manifest):
    declarations=[v for v in (manifest.get('parameters',{}),manifest.get('plan',{})) if any(k in v for k in ['active_stas','single_sta'])]
    selections=[active_from_parameters(v) for v in declarations]
    if selections and any(v!=selections[0] for v in selections):raise ValueError('manifest/plan sender selection differs')
    return selections[0] if selections else list(STAS)

def selection_parameters(values):
    selected=normalize_senders(values)
    return {'active_stas':selected,'n_active':len(selected),'active_ports':[PORTS[STAS.index(n)] for n in selected]}

def inactive_worker(name,selected):
    return name in STAS and name not in normalize_senders(selected)

def required_server(key,plan):
    return key in ['server_%d'%PORTS[STAS.index(n)] for n in active_from_parameters(plan)]

def validate_worker_selection(plan,role,port=None,idle=False):
    selected=active_from_parameters(plan)
    if role=='STA':
        if port not in PORTS:raise ValueError('STA port outside stable identity map')
        if bool(idle)!=(STAS[PORTS.index(port)] not in selected):raise ValueError('worker idle flag conflicts with common plan')
    elif role!='AP':raise ValueError('unknown worker role')
    return selected

def saturation_gate(per_sta,selected):
    selected=normalize_senders(selected)
    return set(per_sta)==set(selected) and all(
        (per_sta[n].get('queue_nonzero_fraction') or 0)>=.95 and
        (per_sta[n].get('queue_backlog_fraction') or 0)>=.90 for n in selected)


def idle_grace_seconds(plan):
    """Opt-in association-only relaxation. Old plans remain strict."""
    value=plan.get('idle_association_grace_s',0)
    if type(value) not in (int,float) or value not in (0,5):
        raise ValueError('idle association grace must be explicitly 0 or 5 seconds')
    return float(value)

def idle_grace_policy(plan):
    value=plan.get('idle_association_grace_policy','cumulative')
    if value not in ('cumulative','per_episode'):raise ValueError('invalid idle association grace policy')
    if value=='per_episode' and idle_grace_seconds(plan)!=5:raise ValueError('per-episode idle policy requires explicit five-second grace')
    return value


class IdleAssociationMonitor:
    """Conservative gap bound from last connected to next connected sample.

    Only passive health observations; never schedules traffic or repairs Wi-Fi.
    Historical/default budgets are cumulative. Explicit per_episode budgets
    constrain each outage separately; cumulative time is still reported.
    """
    def __init__(self,grace_s=0,policy='cumulative'):
        self.grace_s=idle_grace_seconds({'idle_association_grace_s':grace_s})
        self.policy=idle_grace_policy({'idle_association_grace_s':grace_s,'idle_association_grace_policy':policy})
        self.last_good=None;self.first_bad=None;self.last_at=None
        self.episodes=[];self.errors=[];self.samples=0

    def observe(self,at_ns,connected,read_ok=True):
        self.samples+=1
        if self.last_at is not None and at_ns<self.last_at:
            self.errors.append('nonmonotonic health timestamps')
        self.last_at=at_ns
        if not read_ok:self.errors.append('iw link query failed')
        if connected and read_ok:
            if self.first_bad is not None and self.last_good is not None:
                upper=(at_ns-self.last_good)/1e9
                self.episodes.append({'last_connected_monotonic_ns':self.last_good,
                    'first_disconnected_monotonic_ns':self.first_bad,
                    'reconnected_monotonic_ns':at_ns,'gap_upper_bound_s':upper})
            self.first_bad=None
            self.last_good=at_ns
        else:
            if self.first_bad is None:self.first_bad=at_ns
            if self.last_good is None:self.errors.append('no connected baseline')
            if self.grace_s==0:self.errors.append('strict idle association lost')
        gaps=[e['gap_upper_bound_s'] for e in self.episodes]
        if self.first_bad is not None and self.last_good is not None:gaps.append((at_ns-self.last_good)/1e9)
        budget_use=sum(gaps) if self.policy=='cumulative' else max(gaps,default=0)
        if budget_use>self.grace_s:self.errors.append(('cumulative idle association gap' if self.policy=='cumulative' else 'individual idle association episode')+' exceeds grace')
        return not self.errors

    def report(self):
        errors=list(dict.fromkeys(self.errors))
        if self.first_bad is not None:errors.append('idle association not recovered at end')
        if not self.samples:errors.append('no idle health observations')
        return {'pass':not errors,'grace_s':self.grace_s,'policy':self.policy,'sample_count':self.samples,
            'episodes':list(self.episodes),'errors':errors,
            'total_gap_upper_bound_s':sum(e['gap_upper_bound_s'] for e in self.episodes),
            'max_episode_gap_upper_bound_s':max((e['gap_upper_bound_s'] for e in self.episodes),default=0),
            'meaning':'sampled outage upper bounds; selected policy governs validation, not exact physical outage duration'}


def audit_idle_association(samples,grace_s,policy='cumulative'):
    monitor=IdleAssociationMonitor(grace_s,policy)
    for sample in samples:
        link=sample['link']
        monitor.observe(sample['at']['monotonic_ns'],'Connected to' in link['stdout'],link['exit_code']==0)
    return monitor.report()
