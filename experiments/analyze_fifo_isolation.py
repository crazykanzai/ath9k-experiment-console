"""Native FIFO counters and shared-wall goodput; NOT formal/cohort metrics."""
import hashlib
import json
from pathlib import Path
import re
import sys
from analyze_sync import pcap_udp,tcpdump_stats,air_monitor_audit
from analyze_cohort_sync import clock_envelope
from cohort_trace import parse
from sync_run import write


def native(text):
    fields=['FIFO Underrun','DATA Underrun','DELIM Underrun','DESC CFG Error','MPDUs Completed','TX-Failed']
    out={}
    for field in fields:
        match=re.search(r'^'+re.escape(field)+r':\s+([\d ]+)$',text,re.M)
        if not match:raise ValueError('missing native counter '+field)
        values=list(map(int,match[1].split()))
        if len(values)!=4:raise ValueError('expected BE/BK/VI/VO')
        out[field]=dict(zip(['BE','BK','VI','VO'],values))
    return out


def analyze(folder):
    m=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if m.get('kind')!='FIFO isolation diagnostic':raise ValueError('not FIFO diagnostic')
    load=lambda p:json.loads(p.read_text(encoding='utf-8'))
    plan=m['plan'];t0=plan['t0_ns'];a=t0+plan['warmup_s']*10**9;b=a+plan['measure_s']*10**9
    pre=m.get('clock_after_correction',m['clock_before']);post=m['clock_after']
    env={n:clock_envelope(pre[n],post[n]) for n in pre};packets=pcap_udp(folder/'raw/AP/ap_udp.pcap',set(m['ports']))
    macs={n:re.search(r'addr ([0-9a-f:]+)',m['node_config'][n]['wireless']['stdout']).group(1) for n in ['STA1','STA2','STA3','STA4']}
    apmac=re.search(r'addr ([0-9a-f:]+)',m['node_config']['AP']['wireless']['stdout']).group(1)
    air=air_monitor_audit(folder/'raw/AP/air_monitor_frames.tsv',macs,apmac)
    per={};issues=[]
    if m['status'] not in ['completed_pending_analysis','FIFO_diagnostic_completed']:issues.append('controller did not complete')
    for i,n in enumerate(['STA1','STA2','STA3','STA4']):
        base=folder/'raw'/n;state=load(base/'state.json')
        cfg={k:load(base/('config_'+k+'.json')) for k in ['before','after','window_start','window_stop']}
        counters={k:native(v['native_xmit']['stdout']) for k,v in cfg.items()}
        delta={key:{ac:counters['after'][key][ac]-counters['before'][key][ac] for ac in counters['before'][key]} for key in counters['before']}
        e=state['events'];offset=env[n]['center_ns'];apoffset=env['AP']['center_ns']
        targets={'iperf_launch_call':t0,'driver_start_call':a,'driver_stop_call':b,'iperf_stop_call':t0+(plan['warmup_s']+plan['measure_s']+plan['tail_s'])*10**9}
        timing={key:(e[key]['epoch_ns']-offset-target)/1e6 for key,target in targets.items()}
        if not state['valid'] or state['errors'] or any(abs(v)>100 for v in timing.values()):issues.append(n+' worker/timing failed')
        if not pre[n]['within_50ms_proven'] or not post[n]['within_50ms_proven']:issues.append(n+' clock gate failed')
        if any(value<0 for row in delta.values() for value in row.values()):issues.append(n+' native counters reset')
        rate=cfg['window_start']['rate_mask_2ghz']['stdout'].strip()
        for c in cfg.values():
            if 'explicit_FIFO_candidate' in m['driver_preflight'][n] and c['FIFO_config']['threshold_64B']!=20:issues.append(n+' FIFO candidate threshold not stable at 20')
            be=next(line for line in c['edca']['stdout'].splitlines() if line.startswith('AC=BE '))
            values=dict(v.split('=',1) for v in be.split() if '=' in v)
            if (any(values.get(k)!=plan['expected_signature'][v] for k,v in [('hw_aifs','aifsn'),('hw_cwmin','cwmin'),('hw_cwmax','cwmax')]) or
                c['rate_mask_2ghz']['stdout'].strip()!='0x100' or 'Power save: off' not in c['power_save']['stdout'] or
                'txpower 14.00 dBm' not in c['wireless']['stdout'] or 'Connected to' not in c['link']['stdout']):issues.append(n+' configuration mismatch')
        rx=[(ts-apoffset,size) for ts,size in packets[m['ports'][i]]]
        series=[0.]*(plan['warmup_s']+plan['measure_s']+plan['tail_s'])
        for ts,size in rx:
            bucket=(ts-t0)//10**9
            if 0<=bucket<len(series):series[bucket]+=size*8/1e6
        trace=None
        if (base/'tx_trace.txt').exists():
            failures=[d for d in parse(base/'tx_trace.txt') if d['kind']=='txdone' and (d['ts_status']&4 or d['ts_flags']&24)]
            trace=[dict(raw=d,T0_relative_s=(d['mono_ns']-e['iperf_launch_call']['monotonic_ns'])/1e9) for d in failures]
            profile=(base/'kprobe_profile.txt').read_text()
            missed={line.split()[0]:int(line.split()[-1]) for line in profile.splitlines() if line.split() and line.split()[0] in ['txarrival','txinput','txdone','mibupdate']}
            stats=(base/'trace_stats.txt').read_text()
            if len(missed)!=4 or any(missed.values()) or any(int(v) for v in re.findall(r'^(?:overrun|commit overrun|dropped events): (\d+)',stats,re.M)):issues.append(n+' trace loss')
            if load(base/'cohort_capture_cleanup.json')['errors']:issues.append(n+' trace cleanup failed')
        elif m['FIFO_trace_profile']=='full':issues.append(n+' full trace missing')
        av=air['stations'][n];basic=plan['expected_signature']['mode']=='basic'
        if (av['qos_data']<10 or not av['qos_rates_mbps'] or any(float(k)!=24 for k in av['qos_rates_mbps']) or
            (av['rts'] or av['cts'] if basic else av['rts']==0)):issues.append(n+' air mode/rate gate failed')
        per[n]={'native_whole_flow_counter_delta':delta,'FIFO_before':cfg['before']['FIFO_config'],'FIFO_after':cfg['after']['FIFO_config'],
            'boundary_deviation_ms':timing,'AP_goodput_Mbps':sum(size for ts,size in rx if a<=ts<b)*8/(plan['measure_s']*1e6),
            'trace_error_events':trace,'full_goodput_series_Mbps':series,
            'counter_scope':'native full-PHY counters before/after entire warm+measure+tail+cleanup; not strict 180s, not MPDU cohort'}
    captures={k:tcpdump_stats((folder/'raw/AP'/k).read_text()) for k in ['tcpdump.stderr','monitor_tcpdump.stderr']}
    if any(v['kernel_dropped']!=0 or v['interface_drop_fraction'] is None or v['interface_drop_fraction']>.01 for v in captures.values()):issues.append('capture loss')
    result={'kind':'FIFO isolation diagnostic','trace_profile':m['FIFO_trace_profile'],'acquisition_pass':not issues,'issues':issues,
        'FIFO_error_count':sum(sum(v['native_whole_flow_counter_delta']['FIFO Underrun'].values()) for v in per.values()),
        'per_STA':per,'aggregate_goodput_Mbps':sum(v['AP_goodput_Mbps'] for v in per.values()),
        'air_audit':air,'capture_stats':captures,
        'formal_probability_approved':False,'HoL':None,'exchange_probability':None,
        'raw_sha256':{str(p.relative_to(folder)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (folder/'raw').rglob('*') if p.is_file()}}
    result['performance_valid']=False
    result['hardware_error_observed']=result['FIFO_error_count']>0
    write(folder/'metrics.json',result)
    m.update(status='FIFO_diagnostic_completed',diagnostic_acquisition_pass=not issues,FIFO_error_count=result['FIFO_error_count'],formal_probability_approved=False)
    write(folder/'manifest.json',m)
    lines=['# FIFO隔离诊断','',f"采集通过：{not issues}；FIFO错误：{result['FIFO_error_count']}；不是正式性能数据。",'',str(issues),'',
        '|节点|全程FIFO错误|前/后阈值(64B)|共同窗Mbps|','|---|---:|---|---:|']
    for n,v in per.items():lines.append('|%s|%d|%d / %d|%.3f|'%(n,sum(v['native_whole_flow_counter_delta']['FIFO Underrun'].values()),v['FIFO_before']['threshold_64B'],v['FIFO_after']['threshold_64B'],v['AP_goodput_Mbps']))
    lines+=['','关闭trace时不输出逐包HoL/交换概率。原生软件计数不是clear-on-read硬件寄存器；全程计数含预热/收尾，不能改名为公共180秒内准确事件数。零错误只表示本轮未观测，不证明永不发生。']
    (folder/'analysis.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    import shutil
    for name in ['analyze_fifo_isolation.py','cohort_trace.py']:
        shutil.copy2(Path(__file__).with_name(name),folder/'controller'/name)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,ax=plt.subplots(figsize=(10,4))
    for n,v in per.items():ax.step(range(len(v['full_goodput_series_Mbps'])),v['full_goodput_series_Mbps'],where='post',label=n)
    for x in [plan['warmup_s'],plan['warmup_s']+plan['measure_s']]:ax.axvline(x,color='gray',ls='--')
    ax.axvspan(0,plan['warmup_s'],alpha=.15,color='orange');ax.axvspan(plan['warmup_s'],plan['warmup_s']+plan['measure_s'],alpha=.08,color='green')
    ax.set(xlabel='Seconds from common absolute T0',ylabel='AP UDP Mbps',title='FIFO diagnostic / trace '+m['FIFO_trace_profile']);ax.legend();fig.tight_layout();fig.savefig(folder/'full_timeline.png',dpi=140);plt.close(fig)
    print('FIFO',result['trace_profile'],'acquisition',not issues,'errors',result['FIFO_error_count'],flush=True)
    return result


if __name__=='__main__':analyze(Path(sys.argv[1]))
