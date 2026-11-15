"""Explicit one-window application, common-time goodput and boundary audit."""
import json
import math
import re

from labconsole.real_demo import STAS,PORTS,NS,import_reference
from labconsole.demo_analysis import envelope
from labconsole.cw_policy import air_mode_issue
from .rtt import replay_events,quantile
from .schedules import UNIFORM,BURST


def load(path):return json.loads(path.read_text(encoding='utf-8'))


def application_audit(raw,mode,delay_limit_ms=5):
    final=load(raw/'application_final.json');c=replay_events(raw/'application_probe.jsonl')
    old=final['report'];anchor=old['window']['start_ns'];asof=old['window']['as_of_ns']
    replay=c.report(anchor,anchor+40*NS,asof)
    if (not final['collection_complete'] or old['counts']!=replay['counts'] or old['ledger']!=replay['ledger']):
        raise RuntimeError('application raw event/ledger/count replay mismatch')
    for field in ('rtt_mean_ms','rtt_p95_ms','generation_to_reply_mean_ms'):
        a,b=old[field],replay[field]
        if (a is None)!=(b is None) or a is not None and abs(a-b)>1e-6:
            raise RuntimeError('application numerical replay mismatch '+field)
    schedule=UNIFORM if mode=='uniform' else BURST
    plan=[json.loads(x) for x in (raw/'application_plan.jsonl').read_text().splitlines()]
    if len(plan)!=4000 or len(c.rows)!=4000 or set(c.rows)!=set(range(4000)):
        raise RuntimeError('incomplete planned/generated message sequence')
    for seq,row in enumerate(plan):
        offset=schedule.offset_ns(seq)
        if (row['seq']!=seq or row['T0_offset_ns']!=offset or row['deadline_T0_offset_ns']!=offset+100_000_000
                or c.rows[seq]['generated_ns']!=anchor+offset):
            raise RuntimeError('message schedule receipt mismatch')
    report=c.report(anchor+5*NS,anchor+35*NS,asof)
    if report['counts']['generated']!=3000 or report['counts']['mature']!=3000 or report['counts']['pending']:
        raise RuntimeError('common 30s application cohort incomplete')
    rows=report.pop('ledger')
    delays=[(r['observed_generation_ns']-r['generated_ns'])/1e6 for r in rows]
    report['generation_delay_p99_ms']=quantile(delays,.99)
    report['rtt_p50_ms']=quantile([(r['reply_ns']-r['submitted_ns'])/1e6 for r in rows if r['reply_ns'] is not None],.5)
    failures={}
    for row in rows:
        if row['local_failure']:failures[row['local_failure']]=failures.get(row['local_failure'],0)+1
    report['local_failure_reasons']=failures
    report['generator_delay_warning']=report['generation_delay_p99_ms']>5
    report['generator_delay_limit_ms']=delay_limit_ms
    report['generator_quality_passed']=(report['counts']['local_failure']/3000<=.01 and report['generation_delay_p99_ms']<=delay_limit_ms)
    # Generator quality is about faithful arrivals. Wireless deadline failures
    # never invalidate a complete measurement, including 100% timeout.
    return report


def boundary_audit(state):
    t0=state['T0_ns'];final=state['final_nodes'];rows={}
    request=state.get('request',{})
    warm,measure,tail,segment,guard=(request.get(k,v) for k,v in
        zip(('warmup_s','measure_s','tail_s','segment_s','guard_s'),(5,30,5,30,3)))
    total=warm+measure+tail
    for role in ('AP',*STAS,'MON15'):
        off,unc=envelope(state['clock_checks'],role);events=final[role]['events'];bounds=[]
        kinds=(('application_launch_call',0),('application_generation_end_boundary',total)) if role=='STA1' else (
              (('iperf_launch_call',0),('iperf_stop_call',total)) if role in STAS else (('absolute_end',total),))
        for kind,second in kinds:
            event=next(e for e in events if e['event']==kind)
            bounds.append({'event':kind,'envelope_ms':(abs(event['wall_ns']-off-(t0+second*NS))+unc)/1e6})
        if role in STAS:
            windows=final[role]['statistics_windows']
            if len(windows)!=measure//segment:raise RuntimeError(role+' measurement statistics boundaries missing')
            for index,window in enumerate(windows):
                start=warm+index*segment
                for field,second in (('start_wall_ns',start+guard),('stop_wall_ns',start+segment)):
                    bounds.append({'event':field,'index':index,'envelope_ms':(abs(window[field]-off-(t0+second*NS))+unc)/1e6})
                cw=next(e for e in events if e['event']=='cw_verified' and e.get('index')==index)
                at=cw['write_call_ns'] or cw['verified_call_ns']
                bounds.append({'event':'CW_common_boundary','index':index,'envelope_ms':(abs(at-off-(t0+start*NS))+unc)/1e6})
        rows[role]=bounds
    return rows


def measure_window(folder,state):
    import_reference()
    from analyze_sync import pcap_udp
    raw=folder/'raw';issues=[]
    app=application_audit(raw/'STA1',state['request']['generation_mode'],state['request'].get('generator_delay_p99_limit_ms',5))
    off,unc=envelope(state['clock_checks'],'AP');t0=state['T0_ns'];start=t0+5*NS;end=t0+35*NS
    packets=pcap_udp(raw/'AP/ap_udp.pcap',set(PORTS.values()))
    rates={};sensitivity={}
    for name,port in PORTS.items():
        rows=[(stamp-off,size) for stamp,size in packets[port] if size==1200]
        rates[name]=sum(size for stamp,size in rows if start<=stamp<end)*8/(30e6)
        sensitivity[name]={
            'boundary_certain_Mbps':sum(size for stamp,size in rows if start+unc<=stamp<end-unc)*8/(30e6),
            'boundary_possible_Mbps':sum(size for stamp,size in rows if start-unc<=stamp<end+unc)*8/(30e6)}
    if state['final_nodes']['AP'].get('packet_socket_statistics',[None,None])[1]!=0:
        issues.append('AP live packet sampler drop gate')
    air=state['final_nodes']['MON15'].get('air_audit',{})
    if set(air)!=set(STAS):issues.append('independent air receipts incomplete')
    for name,value in air.items():
        issue=air_mode_issue(value,'basic')
        if issue:issues.append(name+' air gate '+issue)
    boundaries=boundary_audit(state)
    if any(b['envelope_ms']>100 for values in boundaries.values() for b in values):issues.append('100ms boundary envelope gate')
    queue={};targets=state['applied_W'][0] if 0 in state['applied_W'] else state['applied_W']['0']
    for role in STAS:
        samples=[r['data'] for r in state.get('sample_records',{}).get(role,[]) if 5<r['second']<=35]
        if len(samples)!=30:issues.append(role+' incomplete common queue/readback samples')
        if any(int(v['BE']['hw_cwmin'])+1!=targets[role] or int(v['BE']['hw_cwmax'])+1!=8*targets[role] for v in samples):
            issues.append(role+' fixed CW readback gate')
        queue[role]={'samples':len(samples),'nonempty_fraction':sum(v['qdepth']>0 for v in samples)/len(samples) if samples else None,
                     'backlog_fraction':sum(v['qdepth']>=2 or v['pending']>=3 for v in samples)/len(samples) if samples else None}
        if role!='STA1' and (not samples or queue[role]['nonempty_fraction']<.95 or queue[role]['backlog_fraction']<.90):
            issues.append(role+' background backlog gate')
        own=state['final_nodes'][role]
        native=lambda text:sum(map(int,re.findall(r'\d+',next(s for s in text.splitlines() if 'FIFO Underrun' in s))))
        if native(own['xmit_after'])!=native(own['xmit_before']):issues.append(role+' FIFO delta gate')
        if not (raw/role/'trace_stats.txt').is_file():issues.append(role+' trace loss receipt missing')
        if role!='STA1':
            client=load(raw/role/'iperf_client.json')
            if own['processes']['iperf_client']['exit_code']!=1 or client.get('error')!='interrupt - the client has terminated' or len(client.get('intervals',[]))<30:
                issues.append(role+' background iperf completion gate')
    return {'application':app,'per_STA_Mbps':rates,'background_Mbps':sum(rates[n] for n in STAS if n!='STA1'),
            'AP_boundary_sensitivity':sensitivity,'boundary_receipts':boundaries,'queue':queue,'air':air,
            'issues':issues,'performance_target_met':app['timeout_ratio']<=.05,
            'inference_usable':not issues and app['generator_quality_passed'],
            'switch_cost':'fixed windows; no measured online switching loss'}
