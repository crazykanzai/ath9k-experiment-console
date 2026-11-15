"""Application-delivered TCP bytes, common AP clock interval, no wire duplicates."""
ORDER=(('L',16),('M',32),('H',64),('M',64),('H',16),('L',32),('H',32),('L',64),('M',16))
ACTIVE={'L':('STA1',),'M':('STA1','STA2'),'H':('STA1','STA2','STA3','STA4')}
NS=10**9

def aggregate(events,t0_ns,offset_ns,uncertainty_ns,active):
    counts={n:[0]*30 for n in active}; edge={n:[0,0] for n in active}
    start=t0_ns+5*NS;end=start+30*NS
    for row in events:
        if row['event']!='received':continue
        role=row['node'];size=row['bytes'];stamp=row['wall_ns']-offset_ns
        if role not in counts or type(size) is not int or size<=0:raise ValueError('unexpected TCP receiver identity/byte count')
        if start<=stamp<end:counts[role][(stamp-start)//NS]+=size
        if start+uncertainty_ns<=stamp<end-uncertainty_ns:edge[role][0]+=size
        if start-uncertainty_ns<=stamp<end+uncertainty_ns:edge[role][1]+=size
    rates={n:sum(bins)*8/30e6 for n,bins in counts.items()};total=sum(rates.values())
    j=total**2/(len(rates)*sum(v*v for v in rates.values())) if total else 0.
    return {'per_STA_Mbps':rates,'total_Mbps':total,'Jain':j,'fairness_target_met':j>=.90,
        'per_STA_Mbps_1s':{n:[v*8/1e6 for v in bins] for n,bins in counts.items()},
        'boundary_Mbps_bounds':{n:[v*8/30e6 for v in pair] for n,pair in edge.items()},
        'clock_uncertainty_ms':uncertainty_ns/1e6,'measurement':'application TCP recv bytes, [T0+5,T0+35); not file completion time'}

def judge(rows):
    good=[r for r in rows if r['accepted']]
    if len(good)!=9:return {'code':'incomplete_screen_no_policy_conclusion','valid':len(good)}
    table={(r['state'],r['W']):r['metrics'] for r in good}
    fixed={w:sum(table[s,w]['total_Mbps'] for s in ACTIVE)/3 for w in (16,32,64)
        if all(table[s,w]['fairness_target_met'] for s in ACTIVE)}
    selected={}
    for s in ACTIVE:
        feasible=[w for w in (16,32,64) if table[s,w]['fairness_target_met']]
        if not feasible:return {'code':'fairness_capacity_gap','state':s}
        selected[s]=max(feasible,key=lambda w:table[s,w]['total_Mbps'])
    ideal=sum(table[s,selected[s]]['total_Mbps'] for s in ACTIVE)/3
    best=max(fixed,key=fixed.get) if fixed else None
    gain=ideal/fixed[best]-1 if best is not None and fixed[best]>0 else None
    return {'code':'candidate_signal_needs_replication_and_switch_test' if best is None or gain>=.05 and len(set(selected.values()))>1 else 'stop_no_practical_dynamic_signal',
        'selected_W':selected,'fixed_average_Mbps':fixed,'best_fixed_W':best,'ideal_average_Mbps':ideal,
        'ideal_gain_fraction':gain,'repeats_per_condition':1,'switch_cost_not_measured':True,'RL_advantage_proven':False}
