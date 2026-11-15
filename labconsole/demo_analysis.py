"""Reconcile diagnostic curves with immutable AP pcap and per-STA traces."""
from collections import Counter
import hashlib
import json
import re

from .cw_policy import air_mode_issue
from .real_demo import import_reference, NS, PORTS, STAS
from .storage import atomic_json


def read(path):
    return path.read_text(encoding='utf-8',errors='replace')


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024*1024), b''):
            value.update(block)
    return value.hexdigest()


def envelope(checks, role):
    low = min(round((c[role]['offset_ms']-c[role]['uncertainty_ms'])*1e6) for c in checks)
    high = max(round((c[role]['offset_ms']+c[role]['uncertainty_ms'])*1e6) for c in checks)
    return (low+high)//2,(high-low+1)//2


def analysis_trace(path, windows, valid_bit, access_mode='basic'):
    from cohort_trace import ledger, classify
    from bianchi_dai_probability import attempts

    # Join once, then classify separately against each stable driver window.
    records,_,issues = ledger(path,windows[0]['start_mono_ns'],windows[-1]['stop_mono_ns'],bit=valid_bit)
    segments = []
    for window in windows:
        start,end = window['start_mono_ns'],window['stop_mono_ns']
        related = []
        for row in records:
            a,b = row.get('service_start_ns'),row.get('done_ns')
            cat = classify(a,b,start,end)
            if cat in ('unknown_start','invalid_order') and (b is None or not start<=b<end):
                continue
            if cat!='outside':
                related.append({**row,'category':cat})
        probabilities = attempts(related,access_mode)
        complete = [r for r in related if r['category']=='complete']
        success = [r for r in complete if r['acked']]
        segments.append({'index':window['index'],'categories':dict(Counter(r['category'] for r in related)),
                         'hol_samples':len(success),'hol_sum_us':sum(r['service_us'] for r in success),
                         'HoL_ms':sum(r['service_us'] for r in success)/len(success)/1000 if success else None,
                         **probabilities})
    return segments,issues


def analyze(folder, state):
    import_reference()
    from analyze_sync import pcap_udp, tcpdump_stats
    from sender_selection import saturation_gate

    request = state['request']
    roles=state.get('roles',['AP',*STAS,'MON15'])
    monitored='MON15' in roles
    active_senders = request.get('active_senders',list(STAS))
    access_mode = request.get('access_mode','basic')
    warm,measure,tail,step = [request[k] for k in ('warmup_s','measure_s','tail_s','segment_s')]
    checks = state['clock_checks']
    t0 = state['T0_ns']
    total = warm+measure+tail
    issues = []
    warnings = []
    raw = folder/'raw'
    final = state['final_nodes']
    captures = {}
    for role in ('AP','MON15') if monitored else ('AP',):
        captures[role] = tcpdump_stats(read(raw/role/'tcpdump.stderr'))
        c = captures[role]
        if c['kernel_dropped']!=0 or c['interface_drop_fraction'] is None or c['interface_drop_fraction']>.01 or final[role]['processes']['tcpdump']['exit_code']!=0:
            issues.append(role+' 抓包完整性门禁')
    air = final.get('MON15',{}).get('air_audit',{})
    if monitored and (final['MON15'].get('air_error') or set(air)!=set(STAS)):
        issues.append('MON15空口审计缺失')
    if not monitored:warnings.append('MON15未启用：空口DATA速率/RTS-CTS旁证未采集，仅记录配置与吞吐')
    for role,a in air.items():
        if role not in active_senders:
            continue
        issue = air_mode_issue(a,access_mode)
        if issue:
            issues.append(role+' 实际空口模式核验失败: '+issue)
    if final['AP'].get('packet_socket_statistics',[None,None])[1]!=0:
        issues.append('AP实时接收采样有丢包')
    offset,uncertainty = envelope(checks,'AP')
    buckets = {n:[0]*total for n in STAS}
    edge = 0
    packets = pcap_udp(raw/'AP'/'ap_udp.pcap',set(PORTS.values()))
    for n,p in PORTS.items():
        for stamp,size in packets[p]:
            wall = stamp-offset
            sec = (wall-t0)//NS
            if 0<=sec<total and size==1200:
                buckets[n][sec]+=size
                if any(abs(wall-(t0+k*NS))<=uncertainty for k in (warm,warm+measure)):
                    edge+=size
    if request['mode']!='unified' and any(not sum(buckets[n][warm:warm+measure]) for n in active_senders):
        issues.append('AP有效区间缺少某STA业务')
    if any(sum(buckets[n]) for n in STAS if n not in active_senders):
        issues.append('闲置STA出现本轮UDP业务')
    per = {}
    spec = json.loads(read(folder/'trace_specs.json'))
    for n in STAS:
        base = raw/n
        own = final[n]
        profile = read(base/'kprobe_profile.txt')
        misses = {name:int(count) for name,count in re.findall(r'(?m)^\s*(txarrival|txinput|txdone|mibupdate)\s+\d+\s+(\d+)\s*$',profile)}
        loss = list(map(int,re.findall(r'(?m)^\s*(?:overrun|commit overrun|dropped events):\s*(\d+)',read(base/'trace_stats.txt'))))
        if len(misses)!=4 or any(misses.values()) or not loss or any(loss):
            issues.append(n+' 追踪丢失或probe miss')
        windows = own['statistics_windows']
        if len(windows)!=measure//step:
            issues.append(n+' 分段驱动窗口不完整')
            continue
        offset_n,unc = envelope(checks,n)
        timing = []
        events = own['events']
        kinds = ('iperf_launch_call','iperf_stop_call') if n in active_senders else ('idle_at_T0','absolute_end')
        for kind,expected in zip(kinds,(t0,t0+total*NS)):
            event = next(e for e in events if e['event']==kind)
            timing.append((abs(event['wall_ns']-offset_n-expected)+unc)/1e6)
        for k,w in enumerate(windows):
            end = t0+(warm+(k+1)*step)*NS
            start = t0+(warm+k*step+request['guard_s'])*NS
            timing.extend([(abs(w['start_wall_ns']-offset_n-start)+unc)/1e6,(abs(w['stop_wall_ns']-offset_n-end)+unc)/1e6])
            switch = next(e for e in events if e['event']=='cw_verified' and e.get('index')==k)
            call_ns = switch['write_call_ns'] or switch['verified_call_ns']
            timing.append((abs(call_ns-offset_n-(t0+(warm+k*step)*NS))+unc)/1e6)
        if max(timing)>100:
            issues.append(n+' 启停/切换100ms门禁')
        if n in active_senders:
            c = json.loads(read(base/'iperf_client.json'))
            server = json.loads(read(raw/'AP'/('iperf_server_%d.json'%PORTS[n])))
            if (own['processes']['iperf_client']['exit_code']!=1 or c.get('error')!='interrupt - the client has terminated' or
                server.get('error') not in (None,'the client has terminated') or len(c.get('intervals',[]))<measure or len(server.get('intervals',[]))<measure):
                issues.append(n+' iperf退出/JSON门禁')
        elif 'iperf_client' in own['processes'] or (base/'iperf_client.json').exists():
            issues.append(n+' 闲置STA启动了业务进程')
        native = lambda text:sum(map(int,re.findall(r'\d+',next(s for s in text.splitlines() if 'FIFO Underrun' in s))))
        fifo = native(own['xmit_after'])-native(own['xmit_before'])
        if fifo!=0:
            issues.append(n+' FIFO软件计数变化')
        q = [p['queue'][n] for p in state['points'] if warm<p['second']<=warm+measure]
        if len(q)!=measure:
            issues.append(n+' 每秒队列样本缺失')
        segs,trace_issues = analysis_trace(base/'tx_trace.txt',windows,spec[n]['proof']['valid_bit'],access_mode)
        for seg,window in zip(segs,windows):
            seg['metrics_usable']=window.get('metrics_valid',True)
            if not seg['metrics_usable']:
                warnings.append(n+' segment '+str(window['index'])+': '+window.get('metrics_reason','DCF unavailable'))
                seg['trace_diagnostic']=dict(seg)
                seg.update(HoL_ms=None,p_ACK=None,p_CTS_proxy=None,attempt_denominator_proxy=None,
                           metrics_reason=window.get('metrics_reason'))
        if trace_issues.get('hardware_error'):
            issues.append(n+' 追踪记录到硬件发送异常')
        per[n] = {'queue_samples':len(q),'queue_nonzero_fraction':sum(d>0 for d,p in q)/len(q) if q else None,
                  'queue_backlog_fraction':sum(d>=2 or p>=3 for d,p in q)/len(q) if q else None,
                  'max_boundary_envelope_ms':max(timing),'probe_misses':misses,'trace_issues':trace_issues,
                  'FIFO_underrun_delta':fifo,'segments':segs,'goodput_Mbps':sum(buckets[n][warm:warm+measure])*8/(measure*1e6)}
    if not saturation_gate({n:per[n] for n in active_senders if n in per},active_senders):
        issues.append('活跃STA队列饱和门禁')
    segments = []
    for k in range(measure//step):
        start = warm+k*step
        stop = start+step
        stable_start = start+request['guard_s']
        parts = [per[n]['segments'][k] for n in STAS] if set(per)==set(STAS) else []
        usable=bool(parts) and all(p.get('metrics_usable',True) for p in parts)
        count = sum(p['hol_samples'] for p in parts) if usable else 0
        denominator = sum(p['attempt_denominator_proxy'] or 0 for p in parts)
        probability = sum(p['ACK_success'] for p in parts)/denominator if usable and denominator and all(p['attempt_denominator_proxy'] is not None for p in parts) else None
        settings = state['applied_W'][k]
        uniform = len(set(settings.values()))==1 if isinstance(settings,dict) else True
        w = next(iter(settings.values())) if uniform and isinstance(settings,dict) else settings if uniform else None
        dynamic = state.get('control') or state.get('adaptation')
        stable_stop = stop-dynamic.get('score_tail_s',1) if dynamic else stop
        segments.append({'index':k,'W':w,'per_STA_W':settings if isinstance(settings,dict) else dict.fromkeys(STAS,w),
                         'start_s':start,'stop_s':stop,'stable_start_s':stable_start,'stable_stop_s':stable_stop,
                         'total_Mbps':sum(sum(x[start:stop]) for x in buckets.values())*8/(step*1e6),
                         'stable_Mbps':sum(sum(x[stable_start:stable_stop]) for x in buckets.values())*8/((stable_stop-stable_start)*1e6),
                         'HoL_ms':sum(p['hol_sum_us'] for p in parts)/count/1000 if count else None,
                         'p_ACK':probability,'statistics_quality':'available' if usable else 'unavailable',
                         'complete_failures':sum(p['terminal_failures'] for p in parts)})
    result = {'issues':issues,'warnings':warnings,'capture':captures,'air':air,'per_STA':per,'segments':segments,
              'active_senders':active_senders,'sender_set_usage':'acquisition_only_not_UCB_input',
              'air_evidence':'collected' if monitored else 'not_collected',
              'access_mode':access_mode,
              'p_ACK_quality':'approximate_missing_earlier_failed_RTS' if access_mode=='rts' else 'Basic_ABI2_success_path',
              'aggregate_Mbps':sum(v['goodput_Mbps'] for v in per.values()) if len(per)==4 else None,
              'ap_clock_correction_ns':offset,'ap_clock_uncertainty_ns':uncertainty,'ap_measurement_boundary_payload_bytes':edge,
              'ap_payload_bytes_by_second':buckets,'formal_valid':0,
              'raw_sha256':{str(p.relative_to(folder)):digest(p)
                            for role in roles for p in (raw/role).iterdir() if p.is_file()}}
    if request['mode']=='auto':
        baseline = segments[0]['stable_Mbps']
        selected = segments[3:]
        online = sum(s['stable_Mbps'] for s in selected)/len(selected)
        result['comparison'] = {'baseline_W':segments[0]['W'],'baseline_stable_Mbps':baseline,
                                'selected_stable_Mbps':online,'whole_measurement_Mbps':result['aggregate_Mbps'],
                                'relative_stage_difference_pct':100*(online/baseline-1) if baseline else None,
                                'independent_fixed_run':False}
    elif request['mode']=='continuous':
        adaptation = state['adaptation']
        activation, selection = adaptation['activation'], adaptation['selection']
        result['adaptation'] = adaptation
        if activation:
            baseline = sum(segments[k]['stable_Mbps'] for k in activation['baseline_blocks'])/len(activation['baseline_blocks'])
            hold = segments[selection['hold_block']:] if selection else []
            held = sum(s['stable_Mbps'] for s in hold)/len(hold) if hold else None
            first = max(warm, (activation['at_ns']-t0+NS-1)//NS)
            after = sum(sum(x[first:warm+measure]) for x in buckets.values())*8/((warm+measure-first)*1e6)
            result['comparison'] = {'baseline_stable_Mbps':baseline,'selected_stable_Mbps':held,
                                    'post_activation_Mbps':after,
                                    'relative_stage_difference_pct':100*(held/baseline-1) if baseline and held is not None else None,
                                    'independent_fixed_run':False,'basis':'AP pcap'}
    if 'control' in state:
        result['control'] = state['control']
    atomic_json(folder/'metrics.json',result)
    return result
