"""UDP payload counters and preregistered two-candidate screening decision."""
ORDER=(('L',16),('L',8),('H',8),('H',16),('M',16),('M',8))
ACTIVE={'L':('STA1',),'M':('STA1','STA2'),'H':('STA1','STA2','STA3','STA4')}
NS=10**9

def aggregate(packets,t0,offset,uncertainty,active,ports):
    begin=t0+5*NS;end=begin+30*NS
    bins={n:[0]*30 for n in active};bounds={n:[0,0] for n in active}
    for name in active:
        for stamp,size in packets[ports[name]]:
            if size!=1200:continue
            at=stamp-offset
            if begin<=at<end:bins[name][(at-begin)//NS]+=size
            if begin+uncertainty<=at<end-uncertainty:bounds[name][0]+=size
            if begin-uncertainty<=at<end+uncertainty:bounds[name][1]+=size
    rates={n:sum(v)*8/30e6 for n,v in bins.items()};total=sum(rates.values())
    j=total**2/(len(rates)*sum(v*v for v in rates.values())) if total else 0.
    return {'per_STA_Mbps':rates,'total_Mbps':total,'Jain':j,'fairness_target_met':j>=.90,
        'per_STA_Mbps_1s':{n:[x*8/1e6 for x in v] for n,v in bins.items()},
        'boundary_Mbps_bounds':{n:[x*8/30e6 for x in v] for n,v in bounds.items()},
        'clock_uncertainty_ms':uncertainty/1e6,'measurement':'AP UDP 1200B payload in common [T0+5,T0+35); not TCP or real file completion'}

def saturation(samples):
    count=len(samples)
    nonempty=sum(v['qdepth']>0 for v in samples)/count if count else 0.
    backlog=sum(v['qdepth']>=2 or v['pending']>=3 for v in samples)/count if count else 0.
    return {'samples':count,'queue_nonzero_fraction':nonempty,'queue_backlog_fraction':backlog,
        'passed':count==30 and nonempty>=.95 and backlog>=.90}

def judge(rows):
    good=[r for r in rows if r['accepted']]
    if len(good)!=6:return {'code':'incomplete_no_policy_conclusion','valid':len(good)}
    table={(r['state'],r['W']):r['metrics'] for r in good}
    if set(table)!=set(ORDER):raise ValueError('duplicate or unexpected UDP condition')
    fixed={w:sum(table[s,w]['total_Mbps'] for s in ACTIVE)/3 for w in (8,16)
        if all(table[s,w]['fairness_target_met'] for s in ACTIVE)}
    selected={}
    for s in ACTIVE:
        eligible=[w for w in (8,16) if table[s,w]['fairness_target_met']]
        if not eligible:return {'code':'fairness_capacity_gap','state':s}
        selected[s]=max(eligible,key=lambda w:table[s,w]['total_Mbps'])
    ideal=sum(table[s,selected[s]]['total_Mbps'] for s in ACTIVE)/3
    best=max(fixed,key=fixed.get) if fixed else None
    gain=ideal/fixed[best]-1 if best is not None and fixed[best]>0 else None
    code='fairness_feasibility_signal' if best is None else 'candidate_signal_needs_replication' if gain>=.05 and len(set(selected.values()))>1 else 'stop_no_practical_dynamic_signal'
    return {'code':code,'selected_W':selected,'fixed_average_Mbps':fixed,'best_fixed_W':best,
        'ideal_average_Mbps':ideal,'ideal_gain_fraction':gain,'minimum_gain_fraction':.05,
        'repeats_per_condition':1,'switch_cost_unmeasured':True,'RL_advantage_proven':False,
        'comparison_scope':'W8/W16 only; no extrapolation to all CW or other workloads'}
