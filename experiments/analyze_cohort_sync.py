"""Four-STA diagnostic analysis: complete-service set plus shared-wall goodput."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from cohort_trace import ledger,summarize,window_coverage
from analyze_retry_descriptors import decode
from analyze_sync import pcap_udp,tcpdump_stats,air_monitor_audit,clip_intervals
from sync_run import write
from reference_helpers import active_survey,survey_delta
from sender_selection import active_from_manifest,idle_grace_seconds,idle_grace_policy,audit_idle_association


def load(path):return json.loads(path.read_text(encoding='utf-8'))


def expected_power_matches(wireless,plan,node_name=None):
    """Verify registered STA power; old plans retain their 14 dBm expectation."""
    if 'radio_plan' in plan:
        from radio_plan import validate_radio_plan,power_matches
        radio=validate_radio_plan(plan['radio_plan'])
        if node_name not in radio['STA_txpower_dBm']:raise ValueError('per-node power expectation requires STA identity')
        return power_matches(wireless,radio['STA_txpower_dBm'][node_name])
    expected=plan.get('expected_STA_txpower_dBm',14)
    if isinstance(expected,bool) or expected not in (5,14):
        raise ValueError('unregistered STA transmit-power expectation')
    match=re.search(r'\btxpower\s+(-?\d+(?:\.\d+)?)\s+dBm\b',wireless)
    return bool(match and float(match.group(1))==expected)


def clock_envelope(before,after):
    points=[before['best']]
    if after:points.append(after['best'])
    low=min(p['offset_ns']-p['uncertainty_ns'] for p in points)
    high=max(p['offset_ns']+p['uncertainty_ns'] for p in points)
    return {'center_ns':(low+high)//2,'uncertainty_ns':(high-low+1)//2,
        'low_ns':low,'high_ns':high,
        'observed_offset_drift_ns':points[-1]['offset_ns']-points[0]['offset_ns'],
        'method':'pre/post probe envelope; assumes no unobserved clock excursions outside observed bounds; paired wall/monotonic steps checked separately'}


def analyze(folder):
    m=load(folder/'manifest.json')
    if m.get('kind') not in ['complete-service cohort diagnostic','Bianchi-Dai trend experiment']:raise RuntimeError('not a diagnostic manifest')
    selected=active_from_manifest(m)
    plan=m['plan'];warm,measure,tail=(plan[k] for k in ['warmup_s','measure_s','tail_s'])
    t0=plan['t0_ns'];a=t0+warm*1_000_000_000;b=a+measure*1_000_000_000;end=b+tail*1_000_000_000
    clocks=m.get('clock_after_correction',m['clock_before'])
    envelopes={n:clock_envelope(c,m.get('clock_after',{}).get(n)) for n,c in clocks.items()}
    offsets={n:c['center_ns'] for n,c in envelopes.items()}
    payloads=pcap_udp(folder/'raw/AP/ap_udp.pcap',set(m['ports']))
    macs={n:re.search(r'addr ([0-9a-f:]+)',m['node_config'][n]['wireless']['stdout']).group(1) for n in ['STA1','STA2','STA3','STA4']}
    apmac=re.search(r'addr ([0-9a-f:]+)',m['node_config']['AP']['wireless']['stdout']).group(1)
    air=air_monitor_audit(folder/'raw/AP/air_monitor_frames.tsv',macs,apmac)
    capture={n:tcpdump_stats((folder/'raw/AP'/n).read_text()) for n in ['tcpdump.stderr','monitor_tcpdump.stderr']}
    issues=[];per={};series={};spreads={};timeline={};idle_health={};warnings=[]
    if m['status'] not in ['completed_pending_analysis','diagnostic_valid','diagnostic_invalid']:issues.append('controller incomplete/invalid')
    for key,stats in capture.items():
        if stats['kernel_dropped']!=0 or stats['interface_drop_fraction'] is None or stats['interface_drop_fraction']>.01:
            issues.append(key+' capture loss unknown/exceeds gate')
    for i,name in enumerate(['STA1','STA2','STA3','STA4']):
        if name not in selected:
            base=folder/'raw'/name;state=load(base/'state.json')
            grace=idle_grace_seconds(plan)
            if grace:
                audit=audit_idle_association([json.loads(line) for line in (base/'queue_health.jsonl').read_text().splitlines() if line.strip()],grace,idle_grace_policy(plan))
                idle_health[name]=audit
                if not audit['pass']:issues.append(name+' inactive association grace gate failed')
                elif audit['episodes']:warnings.append(name+' recovered idle association gap; see raw health log')
            if not state.get('idle_sta') or not state['valid'] or state['errors'] or payloads[m['ports'][i]]:
                issues.append(name+' inactive sender/association health gate failed')
            if load(base/'cohort_capture_cleanup.json')['errors']:issues.append(name+' inactive capture cleanup failed')
            profile=(base/'kprobe_profile.txt').read_text()
            misses={line.split()[0]:int(line.split()[-1]) for line in profile.splitlines() if line.split() and line.split()[0] in ['txdone','txinput','txarrival','mibupdate']}
            stats=(base/'trace_stats.txt').read_text()
            if len(misses)!=4 or any(misses.values()) or any(int(v)!=0 for v in re.findall(r'^(?:overrun|commit overrun|dropped events): (\d+)',stats,re.M)):
                issues.append(name+' inactive trace loss/misses')
            for stage in ['before','window_start','window_stop','after']:
                cfg=load(base/('config_'+stage+'.json'))
                if 'Connected to' not in cfg['link']['stdout'] and (not grace or stage in ['before','after']):issues.append(name+' inactive association changed')
                be=next((line for line in cfg['edca']['stdout'].splitlines() if line.startswith('AC=BE ')),'')
                values=dict(t.split('=',1) for t in be.split() if '=' in t)
                if any(values.get(k)!=plan['expected_signature'][key] for k,key in [('hw_aifs','aifsn'),('hw_cwmin','cwmin'),('hw_cwmax','cwmax')]):
                    issues.append(name+' inactive CW/AIFS mismatch')
                if cfg['rate_mask_2ghz']['stdout'].strip()!='0x100' or 'Power save: off' not in cfg['power_save']['stdout']:
                    issues.append(name+' inactive PHY/power-save mismatch')
                if not expected_power_matches(cfg['wireless']['stdout'],plan,name) or cfg['basic_access_override']['stdout'].strip()!=('1' if plan['expected_signature']['mode']=='basic' else '0'):
                    issues.append(name+' inactive power/access mismatch')
            for key,when in [('driver_start_call',a),('driver_stop_call',b)]:
                ts=state['events'].get(key,{}).get('epoch_ns',0)-offsets[name]
                timeline.setdefault(key,[]).append(ts)
                if abs(ts-when)>100_000_000:issues.append(name+' inactive window boundary mismatch')
            continue
        base=folder/'raw'/name;state=load(base/'state.json');events=state['events']
        launch=events['iperf_launch_call'];epoch_minus_mono=launch['epoch_ns']-launch['monotonic_ns']
        am=a+offsets[name]-epoch_minus_mono;bm=b+offsets[name]-epoch_minus_mono
        spec=load(folder/'controller'/('cohort_spec_'+name+'.json'))
        rows,updates,problems=ledger(base/'tx_trace.txt',am,bm,spec['proof']['valid_bit'],
            uncertainty=envelopes[name]['uncertainty_ns']+2048)
        mode='basic' if plan['expected_signature']['mode']=='basic' else 'rts'
        metric=summarize(rows,mode);metric['ledger_issues']=problems;metric['ANI_update_count']=len(updates)
        metric['window_coverage']=window_coverage(rows,am,bm)
        with (folder/(name+'_packet_ledger.jsonl')).open('w',encoding='utf-8') as f:
            for r in rows:f.write(json.dumps(r)+'\n')
        done=[r for r in rows if 'raw' in r]
        metric['identity_coverage']=sum(r['identity_verified'] for r in done)/len(done) if done else 0
        metric['complete_fraction']=metric['window_coverage']['complete_fraction'] or 0
        unknown=sum(r['category'] in ['unknown_start','invalid_order'] for r in done)
        metric['unknown_fraction']=unknown/len(done) if done else 1
        wrong=[]
        retry_layouts=Counter();terminal_statuses=Counter();long_hist=Counter();short_hist=Counter()
        for row in done:
            d=row['raw'];x=decode(d)
            retry_layouts[str(tuple(s['tries'] for s in x['stages']))]+=1
            terminal_statuses[str(d['ts_status'])]+=1;long_hist[str(d['ts_longretry'])]+=1;short_hist[str(d['ts_shortretry'])]+=1
            if (x['RTS_enabled']!=(mode=='rts') or x['CTS_self_enabled'] or
                x['raw_short']!=d['ts_shortretry'] or x['raw_long']!=d['ts_longretry'] or
                x['no_ACK'] or bool(d['ds_status1']&1)!=bool(d['ts_status']&32) or
                x['raw_final_series']!=d['ts_rateindex'] or any(s['hw_rate']!=9 or s['rts_cts_qual']!=(mode=='rts') for s in x['stages'] if s['tries'])):
                wrong.append(row['token'])
        metric['raw_descriptor_mismatches']=wrong
        metric.update(configured_DATA_retry_layouts=dict(retry_layouts),terminal_status_histogram=dict(terminal_statuses),
            raw_long_histogram=dict(long_hist),raw_short_histogram=dict(short_hist))
        profile=(base/'kprobe_profile.txt').read_text()
        misses={line.split()[0]:int(line.split()[-1]) for line in profile.splitlines() if line.split() and line.split()[0] in ['txdone','txinput','txarrival','mibupdate']}
        metric['probe_misses']=misses
        stat=(base/'trace_stats.txt').read_text()
        clean=all(int(v)==0 for v in re.findall(r'^(?:overrun|commit overrun|dropped events): (\d+)',stat,re.M))
        metric['trace_clean']=clean
        metric['clock_offset_ms']=offsets[name]/1e6;metric['clock_uncertainty_ms']=envelopes[name]['uncertainty_ns']/1e6
        metric['clock_envelope']=envelopes[name]
        wall_mono=[v['epoch_ns']-v['monotonic_ns'] for v in events.values() if 'epoch_ns' in v and 'monotonic_ns' in v]
        metric['observed_wall_monotonic_mapping_span_ms']=(max(wall_mono)-min(wall_mono))/1e6 if wall_mono else None
        if wall_mono and max(wall_mono)-min(wall_mono)>1_000_000:issues.append(name+' wall/monotonic step or sampling discrepancy exceeds 1 ms')
        expected={'iperf_launch_call':t0,'driver_start_call':a,'driver_stop_call':b,'iperf_stop_call':end}
        dev={k:(events[k]['epoch_ns']-offsets[name]-t)/1e6 for k,t in expected.items() if k in events}
        metric['boundary_deviation_ms']=dev
        metric['spawn_deviation_ms']=(events['iperf_spawn_return']['epoch_ns']-offsets[name]-t0)/1e6
        for key in expected:timeline.setdefault(key,[]).append(events.get(key,{}).get('epoch_ns',0)-offsets[name])
        port=m['ports'][i];adjusted=[(ts-offsets['AP'],size) for ts,size in payloads[port]]
        buckets=[0]*(warm+measure+tail)
        for ts,size in adjusted:
            if t0<=ts<end:buckets[(ts-t0)//1_000_000_000]+=size
        series[name]=[v*8/1e6 for v in buckets]
        metric['AP_goodput_Mbps']=sum(buckets[warm:warm+measure])*8/measure/1e6
        ap_uncert=envelopes['AP']['uncertainty_ns']
        metric['AP_goodput_lower_Mbps']=sum(size for ts,size in adjusted if a+ap_uncert<=ts<b-ap_uncert)*8/measure/1e6
        metric['AP_goodput_upper_Mbps']=sum(size for ts,size in adjusted if a-ap_uncert<=ts<b+ap_uncert)*8/measure/1e6
        first_business=next((ts for ts,size in adjusted if size==1200),None)
        metric['first_AP_business_UDP_arrival_deviation_ms']=(first_business-t0)/1e6 if first_business is not None else None
        input_times=[r['input_ns'] for r in done if r.get('input_ns') is not None]
        metric['first_business_input_deviation_ms']=(min(input_times)+epoch_minus_mono-offsets[name]-t0)/1e6 if input_times else None
        if first_business is None or first_business>=a:issues.append(name+' business stream not established before measurement window')
        client=load(base/'iperf_client.json');server=load(folder/'raw/AP'/('iperf_server_%d.json'%port))
        if not adjusted or metric['AP_goodput_Mbps']<=0:issues.append(name+' no received business UDP in common window')
        cc=client.get('start',{}).get('connected',[{}])[0];sc=server.get('start',{}).get('connected',[{}])[0]
        sta_ip=m['node_config'][name]['wireless_ipv4'][0];ap_ip=m['ap_wireless_ip']
        if cc.get('local_host')!=sta_ip or cc.get('remote_host')!=ap_ip or sc.get('remote_host')!=sta_ip or sc.get('local_host')!=ap_ip:
            issues.append(name+' iperf connection differs from discovered business IPs')
        configuration_checks=[]
        for filename in ['config_before.json','config_window_start.json','config_window_stop.json']:
            cfg=load(base/filename);be=next((line for line in cfg['edca']['stdout'].splitlines() if line.startswith('AC=BE ')),'')
            values=dict(t.split('=',1) for t in be.split() if '=' in t)
            match=all(values.get(k)==plan['expected_signature'][key] for k,key in [('hw_aifs','aifsn'),('hw_cwmin','cwmin'),('hw_cwmax','cwmax')])
            match=match and 'Power save: off' in cfg['power_save']['stdout'] and cfg['rate_mask_2ghz']['stdout'].strip()=='0x100'
            match=match and expected_power_matches(cfg['wireless']['stdout'],plan,name) and 'Connected to' in cfg['link']['stdout']
            match=match and cfg['basic_access_override']['stdout'].strip()==('1' if mode=='basic' else '0')
            configuration_checks.append({'file':filename,'pass':bool(match)})
        metric['configuration_checks']=configuration_checks
        if not all(c['pass'] for c in configuration_checks):issues.append(name+' configuration changed/mismatched')
        anchor=payloads[port][0][0] if payloads[port] else 0
        mapped,clipped=clip_intervals(server,anchor,a,b,offsets['AP'])
        metric['iperf_absolute_intervals']=mapped;metric['iperf_clipped_common_seconds']=clipped
        expected_interrupt=(client.get('error')=='interrupt - the client has terminated' and server.get('error')=='the client has terminated' and
            state['processes'].get('client',{}).get('exit_code')==1 and len(client.get('intervals',[]))>=measure and
            abs(events['iperf_stop_call']['epoch_ns']-end)<100_000_000)
        metric['termination']='scheduled_local_absolute_SIGINT' if expected_interrupt else 'normal'
        metric['client_error']=client.get('error');metric['server_error']=server.get('error')
        queue=[];cpu_samples=[]
        for line in (base/'queue_health.jsonl').read_text().splitlines():
            entry=json.loads(line);ts=entry['at']['epoch_ns']-offsets[name]
            if a<=ts<b:
                queue.append(entry['queue']['stdout'])
                if entry.get('cpu'):cpu_samples.append(entry['cpu'])
        metric['queue_samples_in_window']=len(queue)
        queue_values=[]
        for v in queue:
            line=next((s for s in v.splitlines() if s.startswith('(BE):')),'')
            queue_values.append({k:int(n) for k,n in re.findall(r'(qdepth|pending|ampdu-depth):\s*(\d+)',line)})
        metric['queue_nonzero_fraction']=sum(v.get('qdepth',0)>0 for v in queue_values)/len(queue) if queue else None
        metric['queue_backlog_fraction']=sum(v.get('qdepth',0)>=2 or v.get('pending',0)>=3 for v in queue_values)/len(queue) if queue else None
        metric['queue_values']=queue_values
        # Preserve raw queue strings; matcher coverage checked during pilot.
        metric['queue_sample_example']=queue[:2]
        cpu_util={}
        if len(cpu_samples)>1:
            initial={s.split()[0]:list(map(int,s.split()[1:9])) for s in cpu_samples[0]}
            final={s.split()[0]:list(map(int,s.split()[1:9])) for s in cpu_samples[-1]}
            for cpu,values in initial.items():
                delta=[v-u for u,v in zip(values,final.get(cpu,values))];total=sum(delta)
                cpu_util[cpu]=1-(delta[3]+delta[4])/total if total>0 else None
        metric['CPU_utilization_between_first_last_window_samples']=cpu_util
        air_sta=air['stations'][name]
        mode_ok=air_sta['qos_data']>10 and (air_sta['rts']==0 and air_sta['cts']==0 if mode=='basic' else air_sta['rts']>0)
        rates_ok=bool(air_sta['qos_rates_mbps']) and all(float(k)==24 for k in air_sta['qos_rates_mbps'])
        if (state['errors'] or not state['valid'] or len(dev)!=4 or any(abs(v)>100 for v in dev.values()) or abs(metric['spawn_deviation_ms'])>100):
            issues.append(name+' worker/timing gate failed')
        if ((client.get('error') or server.get('error')) and not expected_interrupt):issues.append(name+' unexpected iperf error')
        if (problems or metric['identity_coverage']!=1 or metric['unknown_fraction']>=.01 or wrong or not clean or
            len(misses)!=4 or any(misses.values()) or not updates or not metric['complete_packets'] or not mode_ok or not rates_ok):
            issues.append(name+' cohort/descriptor/air acquisition gate failed')
        cleanup=load(base/'cohort_capture_cleanup.json')
        if cleanup['errors']:issues.append(name+' trace cleanup failed')
        per[name]=metric
    for key,times in timeline.items():
        spreads[key]=(max(times)-min(times))/1e6
        if spreads[key]>100:issues.append(key+' spread exceeds 100 ms')
    spread_bounds={key:value+2*max(envelopes[n]['uncertainty_ns'] for n in macs)/1e6 for key,value in spreads.items()}
    if any(v>100 for v in spread_bounds.values()):issues.append('boundary spread uncertainty bound exceeds 100 ms')
    after=m.get('clock_after',{})
    if any(not after.get(n,{}).get('within_50ms_proven',False) for n in ['STA1','STA2','STA3','STA4']):issues.append('post-run clock gate failed')
    apstate=load(folder/'raw/AP/state.json')
    if apstate['errors']:issues.append('AP worker errors')
    monitor_events=apstate['events'];monitor_plan=apstate['monitor']
    monitor_dev={key:(monitor_events[key]['epoch_ns']-offsets['AP']-monitor_plan[target])/1e6
        for key,target in [('monitor_start_call','planned_start_ns'),('monitor_stop_call','planned_stop_ns')] if key in monitor_events}
    if len(monitor_dev)!=2 or any(abs(v)>100 for v in monitor_dev.values()):issues.append('AP monitor timing gate failed')
    values=[v['AP_goodput_Mbps'] for v in per.values()];s=sum(values)
    idle=None
    if m.get('idle_survey_before') and m.get('idle_survey_after'):
        idle=survey_delta(active_survey(m['idle_survey_before']['stdout']),active_survey(m['idle_survey_after']['stdout']))
    samples=sum(v['ACK_success'] for v in per.values())
    result={'kind':'complete-service cohort diagnostic','acquisition_pass':not issues,'issues':issues,'warnings':warnings,'idle_association_health':idle_health,'per_STA':per,'active_stas':selected,'n_active':len(selected),
        'aggregate_goodput_Mbps':s,'Jain_fairness':s*s/(len(values)*sum(v*v for v in values)) if s else None,
        'pooled_success_conditional_HoL_us':sum((v['success_conditional_HoL_us'] or 0)*v['ACK_success'] for v in per.values())/samples if samples else None,
        'combined_exchange_probability':None,'formal_probability_approved':False,'boundary_spread_ms':spreads,
        'boundary_spread_upper_bound_ms':spread_bounds,'clock_envelopes':envelopes,
        'monitor_boundary_deviation_ms':monitor_dev,
        'idle_AP_survey_delta':idle,
        'air_audit':air,'capture_stats':capture,'full_goodput_series_Mbps':series,
        'CW':[int(plan['expected_signature'][k]) for k in ['cwmin','cwmax']],
        'mode':mode,'warmup_s':warm,'measure_s':measure,'tail_s':tail,
        'raw_sha256':{str(p.relative_to(folder)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (folder/'raw').rglob('*') if p.is_file()}}
    # A merged diagnostic Basic ratio is allowed only if ALL complete cohorts have known counts.
    if mode=='basic' and all(v['diagnostic_p_Basic_ACK'] is not None for v in per.values()):
        result['combined_diagnostic_p_Basic_ACK']=samples/sum(v['ACK_success']/v['diagnostic_p_Basic_ACK'] for v in per.values())
    else:result['combined_diagnostic_p_Basic_ACK']=None
    result['saturated_service_goodput_consistency']={
        'n_payload_bits':len(per)*1200*8,
        'ratio_mean_service_us_times_aggregate_Mbps_over_n_payload_bits':
            result['pooled_success_conditional_HoL_us']*s/(len(per)*1200*8) if result['pooled_success_conditional_HoL_us'] is not None else None,
        'warning':'Backlogged FIFO service intervals partition time; inverse-throughput behavior is partly a conservation relation, not independent proof of correct paper HOL or retry probabilities. AP receive/cohort sets and failures need not match.'}
    write(folder/'metrics.json',result)
    fig,ax=plt.subplots(figsize=(11,4.5))
    for name,seq in series.items():ax.step(range(len(seq)),seq,where='post',label=name)
    ax.axvspan(0,warm,color='orange',alpha=.15);ax.axvspan(warm,warm+measure,color='green',alpha=.08)
    ax.axvspan(warm+measure,warm+measure+tail,color='gray',alpha=.15)
    for t in [warm,warm+measure]:ax.axvline(t,ls='--',color='gray')
    ax.set(xlabel='Seconds from common absolute T0',ylabel='AP UDP Mbps',title=mode+' / CW'+str(result['CW'])+' / diagnostic')
    ax.legend();ax.grid(alpha=.25);fig.tight_layout();fig.savefig(folder/'full_timeline.png',dpi=150);plt.close(fig)
    lines=['# 活跃STA完整服务包集合诊断','业务发送集合：'+str(selected),'',
        '采集有效：'+str(result['acquisition_pass'])+'；正式概率核准：False。','',
        '问题：'+str(issues),'',
        '|节点|全窗Mbps|完整包|终态失败|成功条件HoL ms|完整包占比|','|---|---:|---:|---:|---:|---:|']
    for n,v in per.items():lines.append('|%s|%.3f|%d|%d|%s|%.3f%%|'%(n,v['AP_goodput_Mbps'],v['complete_packets'],v['terminal_failure'],
        '%.3f'%(v['success_conditional_HoL_us']/1000) if v['success_conditional_HoL_us'] is not None else 'NA',v['complete_fraction']*100))
    lines+=['','合计Mbps：%.3f；Jain：%s。'%(s,result['Jain_fairness']),
        '启停偏差(ms)：'+str(spreads),'',
        '实验前无业务AP信道调查差值：'+str(idle)+'（busy只表示占用，不能归因每次失败）。','',
        '吞吐按公共AP接收时间窗；p/HoL按完整服务集合，跨界/unknown/未完成单列。主机回调、完整包选择和追踪开销限制理论解释。',
        'RTS原多段p未核准，保持null；MAC失败计数未核准时Basic也不删除失败包后给全体概率。',
        '每台完整逐包账本、类别、原始状态和所有raw SHA256见metrics及 *_packet_ledger.jsonl。']
    (folder/'analysis.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    m.update(status='diagnostic_valid' if not issues else 'diagnostic_invalid',diagnostic_valid=not issues,formal_probability_approved=False)
    write(folder/'manifest.json',m)
    for name in ['analyze_cohort_sync.py','cohort_trace.py','sender_selection.py']:shutil.copy2(Path(__file__).with_name(name),folder/'controller'/name)
    print('COHORT FOUR',result['acquisition_pass'],issues,round(s,3),flush=True)
    return result


if __name__=='__main__':raise SystemExit(0 if analyze(Path(sys.argv[1]))['acquisition_pass'] else 2)
