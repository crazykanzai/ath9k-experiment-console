"""Fixed first-round/passive capture gates and descriptive same-device timing."""
import bisect,collections,json,math,statistics as st
from pathlib import Path
from air_timing import pcap_frames,business,ack_pairs,lattice
from analyze_sync import tcpdump_stats
from sync_run import write
def load(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def stats(values):
    if not values:return {'n':0,'mean':None,'median':None,'p95':None,'max':None}
    a=sorted(values);return {'n':len(a),'mean':st.mean(a),'median':st.median(a),'p95':a[min(len(a)-1,int(.95*len(a)))],'max':max(a)}
def analyze_air(folder):
    m=load(folder/'manifest.json');v=load(folder/'metrics.json');listener=load(folder/'controller/passive_listener.json');base=folder/'raw/MON15';state=load(base/'state.json')
    spec=listener['spec'];sta=spec['STA1_mac'];ap=spec['AP_mac'];own=spec['original_mac'];plan=m['plan'];t0=plan['t0_ns'];a=t0+5_000_000_000;b=a+30_000_000_000
    cb=listener['clock_before']['best'];ca=listener['clock_after']['best'];low=min(z['offset_ns']-z['uncertainty_ns'] for z in [cb,ca]);high=max(z['offset_ns']+z['uncertainty_ns'] for z in [cb,ca]);offset=(low+high)//2;unc=(high-low+1)//2
    frames=list(pcap_frames(base/'air_full.pcap'))
    with (folder/'MON15_frames.jsonl').open('w',encoding='utf-8') as f:
        for frame in frames:f.write(json.dumps(frame,separators=(',',':'))+'\n')
    for f in frames:f['global_epoch_ns']=f['epoch_ns']-offset
    measure=[f for f in frames if a+unc<=f['global_epoch_ns']<b-unc]
    data=[f for f in measure if business(f,sta,ap)];acks=[f for f in measure if f['type']==1 and f['subtype']==13 and f['ra']==sta]
    pairs,near=ack_pairs(measure,sta,ap)
    capture=tcpdump_stats((base/'tcpdump.stderr').read_text(encoding='utf-8',errors='replace'))
    issues=[]
    if state['errors'] or not state['valid'] or state['processes']['tcpdump']['exit_code']!=0:issues.append('passive worker/process gate')
    if capture['kernel_dropped']!=0 or capture['interface_drop_fraction'] is None or capture['interface_drop_fraction']>.01:issues.append('passive reported capture-loss gate')
    if max(abs(low),abs(high))>50_000_000:issues.append('passive clock envelope gate')
    stop=state['events'].get('capture_stop_call',{}).get('epoch_ns',0)-offset;planned_stop=t0+45_000_000_000
    if abs(stop-planned_stop)+unc>100_000_000:issues.append('passive stop boundary gate')
    if state['events']['capture_launch_call']['epoch_ns']-offset>t0-5_000_000_000:issues.append('passive capture preroll less than 5 seconds')
    for part in ['interface_before','interface_after']:
        c=load(base/(part+'.json'))
        if c['code'] or 'type monitor' not in c['stdout'] or f'({spec["frequency"]} MHz)' not in c['stdout']:issues.append('passive interface/channel gate '+part)
    if any(f['truncated'] for f in frames):issues.append('passive truncated full-frame capture')
    beacons=[f for f in measure if f['type']==0 and f['subtype']==8 and f['bssid']==ap]
    if len(beacons)<20:issues.append('AP beacon reception below 20')
    if len(data)<1000:issues.append('business DATA reception below 1000')
    coverage={}
    for name,rows in [('DATA',data),('ACK',acks)]:
        coverage[name]={k:sum(x[k] is not None for x in rows)/len(rows) if rows else 0 for k in ['tsft_us','rate_Mbps','signal_dBm']}
        if any(x<.99 for x in coverage[name].values()):issues.append(name+' radiotap field coverage below 99%')
    pair_fraction=len(pairs)/len(data) if data else 0
    if len(pairs)<500 or pair_fraction<.5:issues.append('CRC and SIFS DATA/ACK pairing gate')
    outgoing=sum(f['ta'] in [own,spec.get('monitor_mac')] for f in frames if f['ta'])
    if outgoing:issues.append('MON15 own transmission observed')
    rate_hist={str(retry):dict(collections.Counter(str(f['rate_Mbps']) for f in data if f['retry']==retry)) for retry in [0,1]}
    if any(f['rate_Mbps'] not in (24,None) for f in data):issues.append('observed non24Mbps business DATA')
    # Use both recorded header and final-status sequence candidates. Match only
    # if a unique service record fits the independent coarse clock envelope.
    source_state=load(folder/'raw/STA1/state.json');launch=source_state['events']['iperf_launch_call'];epoch_minus_mono=launch['epoch_ns']-launch['monotonic_ns'];sender_offset=v['clock_envelopes']['STA1']['center_ns'];sender_unc=v['clock_envelopes']['STA1']['uncertainty_ns']
    services=collections.defaultdict(list)
    with (folder/'STA1_packet_ledger.jsonl').open(encoding='utf-8') as f:
        for line in f:
            row=json.loads(line)
            if 'raw' not in row:continue
            row['done_global_ns']=row['done_ns']+epoch_minus_mono-sender_offset
            for key in set([row['seq'],row['raw'].get('ts_seqnum')]):
                if key is not None:services[(key,row['tid'])].append(row)
    matched={};mapping_counts=collections.Counter();tolerance=20_000_000+sender_unc+unc
    for f in data:
        candidates={r['token']:r for r in services[(f['seq'],f['tid'])] if abs(r['done_global_ns']-f['global_epoch_ns'])<=tolerance}
        if len(candidates)==1:
            r=next(iter(candidates.values()));matched[f['index']]=r
            mapping_counts['header' if r['seq']==f['seq'] else 'final_status']+=1
    # Adjacent queue observations support backlog; they do not prove every
    # microsecond's queue occupancy. Apply only this measured proxy.
    q=[]
    for line in (folder/'raw/STA1/queue_health.jsonl').read_text().splitlines():
        z=json.loads(line);text=z['queue']['stdout'];import re
        mm=re.search(r'\(BE\):.*qdepth:\s*(\d+).*pending:\s*(\d+)',text)
        backlog=bool(mm and (int(mm.group(1))>1 or int(mm.group(2))>1))
        q.append((z['at']['epoch_ns']-sender_offset,backlog))
    qt=[x[0] for x in q]
    def backlogged(t):
        i=bisect.bisect_left(qt,t)
        return 0<i<len(q) and q[i-1][1] and q[i][1] and max(t-qt[i-1],qt[i]-t)<=2_000_000_000
    all_known=sorted([f for f in measure if f['tsft_us'] is not None],key=lambda f:f['tsft_us']);ts=[f['tsft_us'] for f in all_known]
    ordered=sorted(data,key=lambda f:f['native_end_us'] if f['native_end_us'] is not None else float('inf'))
    gaps=[];retry_gaps=[];funnel=collections.Counter()
    for previous,current in zip(ordered,ordered[1:]):
        if current['start_us'] is None or previous['native_end_us'] is None:continue
        if current['retry']==1:
            if current['seq']==previous['seq'] and current['tid']==previous['tid']:
                pair=pairs.get(previous['index']);origin=pair['ACK']['native_end_us'] if pair else previous['native_end_us']
                retry_gaps.append({'gap_us':current['start_us']-origin,'origin':'observed_ACK_end' if pair else 'previous_DATA_end_includes_recovery','seq':current['seq'],'previous':previous['index'],'current':current['index']})
            continue
        funnel['adjacent_new_candidates']+=1
        if not current['crc_good'] or current['bad_fcs_flag'] or current['tid']!=previous['tid'] or current['seq']!=(previous['seq']+1)%4096:continue
        funnel['CRC_and_sequence_continuity']+=1
        pair=pairs.get(previous['index'])
        if not pair:continue
        funnel['previous_DATA_ACK_observed']+=1
        service=matched.get(previous['index']);following=matched.get(current['index'])
        if not service or not following or not service['acked'] or not service['identity_verified']:continue
        funnel['unique_TX_success_verified']+=1
        if not backlogged(current['global_epoch_ns']):continue
        funnel['backlog_proxy_supported']+=1
        ack=pair['ACK'];gap=current['start_us']-ack['native_end_us']
        if gap<0:continue
        between=[x for x in all_known[bisect.bisect_right(ts,ack['tsft_us']):bisect.bisect_left(ts,current['tsft_us'])] if x['index'] not in [previous['index'],current['index'],ack['index']] and (x['start_us'] is None or x['start_us']>=ack['native_end_us']-6)]
        external=[x for x in between if (x['ta'] and x['ta'] not in spec['experiment_macs']) or (x['bssid'] and x['bssid']!=ap)]
        gaps.append({'gap_us':gap,'seq':current['seq'],'previous_seq':previous['seq'],'previous_DATA_index':previous['index'],'ACK_index':ack['index'],'DATA_index':current['index'],'ACK_end_us':ack['native_end_us'],'DATA_start_us':current['start_us'],'intervening_frames':len(between),'external_frames':len(external),'intervening_indices':[x['index'] for x in between[:100]],'queue_proxy':True})
    # Lack of a visible lattice is never a capture gate. Insufficient primary
    # samples is reported as an analysis limitation, not retuned until it fits.
    lattice_results={kind:{str(period):lattice([g['gap_us'] for g in gaps if (not g['intervening_frames'] if kind=='no_observed_intervening' else bool(g['intervening_frames']))],period) for period in [9,18,20]} for kind in ['no_observed_intervening','intervening']}
    summary={'capture_gate_pass':not issues,'issues':issues,'frame_count':len(frames),'measurement_frames':len(measure),'measurement_business_DATA':len(data),'measurement_ACK_to_STA1':len(acks),'AP_beacons':len(beacons),'radiotap_coverage':coverage,'capture_stats':capture,'passive_clock_envelope_ns':[low,high],'capture_stop_upper_ms':(abs(stop-planned_stop)+unc)/1e6,'MON15_transmissions':outgoing,'rate_histogram_by_Retry':rate_hist,'DATA_ACK_pairs':len(pairs),'pair_fraction':pair_fraction,'paired_SIFS_us':stats([p['SIFS_us'] for p in pairs.values()]),'all_nearest_ACK_0_100us':stats(near),'TX_sequence_mapping_counts':dict(mapping_counts),'TX_unique_mapping_fraction':len(matched)/len(data) if data else 0,'new_gap_selection_funnel':dict(funnel),'new_gap_us':stats([g['gap_us'] for g in gaps]),'primary_sample_sufficiency':len(gaps)>=100,'retry_gap_us':stats([g['gap_us'] for g in retry_gaps]),'candidate_lattices':lattice_results,'no_grid_is_not_gate_failure':True,'timestamp_convention':'undo mac80211 integer MPDU-length/rate rewind; intervals use same listener native END differences and explicit legacy PHY duration; absolute PPDU endpoints retain constant ERP reference uncertainty','host_time_use':'coarse unique trace match and common-window selection only; never microsecond gap','queue_use':'adjacent sampled backlog proxy, not continuous per-packet queue telemetry','first_round_acceptance_does_not_prove_lossless_RF_reception':True}
    write(folder/'air_timing_metrics.json',summary)
    with (folder/'new_packet_gaps.jsonl').open('w',encoding='utf-8') as f:
        for g in gaps:f.write(json.dumps(g)+'\n')
    with (folder/'retry_packet_gaps.jsonl').open('w',encoding='utf-8') as f:
        for g in retry_gaps:f.write(json.dumps(g)+'\n')
    print('PASSIVE_ACCEPT',summary['capture_gate_pass'],summary['issues'],'DATA',len(data),'ACK_pairs',len(pairs),'primary_gaps',len(gaps),flush=True)
    return summary
