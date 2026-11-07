"""Streaming passive evidence for one selected STA, short or formal window.

No lattice fit, packet ledger rewrite, PHY configuration or success-rate estimate.
DATA/ACK pairing is receiver-side coverage, not the sender's ACK probability.
"""
from collections import Counter,deque
import json
from pathlib import Path
from air_timing import pcap_frames,business
from analyze_sync import tcpdump_stats
from listener_selection import target_from_run,listener_window,verify_listener_readbacks
from sync_run import write

def scan_frames(frames,target,spec,start_ns,stop_ns,offset_ns):
    sta=target['sender_mac'];ap=spec['AP_mac'];port=target['sender_port']
    fields={kind:Counter() for kind in ['DATA','ACK']};data=acks=pairs=beacons=0
    rates={str(i):Counter() for i in [0,1]};ack_rates=Counter();external_beacons=Counter();others=Counter()
    pending=deque(maxlen=32);truncated=outgoing=bad_data=0;total=0;in_window=0;RTS=CTS=0
    for f in frames:
        total+=1;truncated+=bool(f['truncated'])
        if f['ta'] and f['ta'] in [spec['original_mac'],spec.get('monitor_mac')]:outgoing+=1
        t=f['epoch_ns']-offset_ns
        if not start_ns<=t<stop_ns:continue
        in_window+=1
        if f['type']==0 and f['subtype']==8:
            if f['bssid']==ap:beacons+=1
            elif f['crc_good'] and f['bssid']:external_beacons[f['bssid']]+=1
        if f['crc_good'] and f['ta'] and f['ta'] not in spec['experiment_macs']:others[f['ta']]+=1
        RTS+=bool(f['type']==1 and f['subtype']==11 and f['ta']==sta)
        CTS+=bool(f['type']==1 and f['subtype']==12 and f['ra']==sta)
        if business(f,sta,ap,port):
            data+=1;rates[str(f['retry'])][str(f['rate_Mbps'])]+=1
            for key in ['tsft_us','rate_Mbps','signal_dBm']:fields['DATA'][key]+=f[key] is not None
            if f['crc_good'] and not f['bad_fcs_flag'] and f['native_end_us'] is not None:
                if len(pending)==pending.maxlen:pairs+=pending.popleft()['ACK_candidates']==1
                pending.append({'index':f['index'],'end':f['native_end_us'],'ACK_candidates':0})
            else:bad_data+=1
        elif f['type']==1 and f['subtype']==13 and f['ra']==sta:
            acks+=1;ack_rates[str(f['rate_Mbps'])]+=1
            for key in ['tsft_us','rate_Mbps','signal_dBm']:fields['ACK'][key]+=f[key] is not None
            if f['crc_good'] and not f['bad_fcs_flag'] and f['start_us'] is not None:
                possible=[d for d in pending if 4<=f['start_us']-d['end']<=16]
                if len(possible)==1:possible[0]['ACK_candidates']+=1
    pairs+=sum(d['ACK_candidates']==1 for d in pending)
    coverage={kind:{key:fields[kind][key]/n if n else 0 for key in ['tsft_us','rate_Mbps','signal_dBm']} for kind,n in [('DATA',data),('ACK',acks)]}
    return {'frame_count':total,'measurement_frames':in_window,'measurement_business_DATA':data,'measurement_ACK_to_sender':acks,
            'AP_beacons':beacons,'DATA_ACK_pairs':pairs,'pair_fraction':pairs/data if data else 0,
            'radiotap_coverage':coverage,'rate_histogram_by_Retry':{k:dict(v) for k,v in rates.items()},'ACK_rate_histogram':dict(ack_rates),
            'business_bad_or_unknown_CRC':bad_data,'truncated_frames':truncated,'MON15_transmissions':outgoing,
            'sender_RTS':RTS,'sender_CTS':CTS,'other_BSSID_beacons_CRC_good':dict(external_beacons),'non_experiment_TA_CRC_good':dict(others),
            'pairing_scope':'same-listener native END/PHY-start, CRC-good DATA/ACK, unique SIFS4–16us; observed arrival order, at most 32 preceding business DATA candidates; not a sender ACK-success probability'}

def count_gate(counts):
    issues=[]
    if counts['truncated_frames']:issues.append('passive truncated capture')
    if counts['MON15_transmissions']:issues.append('passive own transmission')
    if counts['AP_beacons']<20:issues.append('AP Beacon below 20')
    if counts['measurement_business_DATA']<1000:issues.append('business DATA below 1000')
    if counts['DATA_ACK_pairs']<500 or counts['pair_fraction']<.5:issues.append('CRC/SIFS pairing coverage gate')
    if any(v<.99 for values in counts['radiotap_coverage'].values() for v in values.values()):issues.append('radiotap coverage below 99%')
    if counts['sender_RTS'] or counts['sender_CTS']:issues.append('Basic air mode gate')
    if any(rate not in ['24.0','24','None'] and n for hist in counts['rate_histogram_by_Retry'].values() for rate,n in hist.items()):issues.append('non24Mbps business DATA')
    return issues

def analyze(folder):
    folder=Path(folder)
    def load(p):return json.loads(p.read_text(encoding='utf-8-sig'))
    manifest=load(folder/'manifest.json');listener=load(folder/'controller/passive_listener.json');spec=listener['spec'];base=folder/'raw/MON15'
    target=target_from_run(spec,manifest);window=listener_window(manifest['plan']);state=load(base/'state.json')
    verify_listener_readbacks(spec,[target['sender_STA']],manifest['node_config'])
    before=listener['clock_before']['best'];after=listener['clock_after']['best']
    low=min(x['offset_ns']-x['uncertainty_ns'] for x in [before,after]);high=max(x['offset_ns']+x['uncertainty_ns'] for x in [before,after])
    offset=(low+high)//2;unc=(high-low+1)//2
    counts=scan_frames(pcap_frames(base/'air_full.pcap'),target,spec,window['start_ns']+unc,window['stop_ns']-unc,offset)
    issues=count_gate(counts);capture=tcpdump_stats((base/'tcpdump.stderr').read_text(errors='replace'))
    if not state.get('valid') or state.get('errors') or state['processes']['tcpdump']['exit_code']!=0:issues.append('passive worker/exit gate')
    if capture['kernel_dropped']!=0 or capture['interface_drop_fraction'] is None or capture['interface_drop_fraction']>.01:issues.append('passive reported loss gate')
    if max(abs(low),abs(high))>50_000_000:issues.append('passive clock envelope gate')
    stop=state['events'].get('capture_stop_call',{}).get('epoch_ns',0)-offset
    stop_upper=abs(stop-window['capture_stop_ns'])+unc
    if stop_upper>100_000_000:issues.append('passive capture stop boundary gate')
    if state['events']['capture_launch_call']['epoch_ns']-offset>manifest['plan']['t0_ns']-5_000_000_000:issues.append('passive preroll gate')
    for label in ['interface_before','interface_after']:
        info=load(base/(label+'.json'))
        if info['code'] or 'type monitor' not in info['stdout'] or f'({spec["frequency"]} MHz)' not in info['stdout']:issues.append('monitor/channel gate '+label)
    output={'capture_gate_pass':not issues,'issues':issues,**target,**window,**counts,'capture_stats':capture,'clock_envelope_ns':[low,high],'stop_upper_ms':stop_upper/1e6,
            'no_lattice_fit':True,'no_full_frame_JSON_copy':True,'success_probability_source':'original complete-service TX ledger, never DATA_ACK pairing fraction',
            'external_activity_warning':'receiver-point observations; not proof of STA CCA or cause of each failure'}
    write(folder/'passive_single_audit.json',output)
    return output

if __name__=='__main__':
    import sys
    result=analyze(Path(sys.argv[1]));print(json.dumps(result,ensure_ascii=False));raise SystemExit(0 if result['capture_gate_pass'] else 2)
