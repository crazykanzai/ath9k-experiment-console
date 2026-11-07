"""Bind one passive capture to the actual sender, stable port and common plan."""
import re
from sender_selection import STAS,PORTS,normalize_senders,active_from_manifest

def listener_target(spec,selected):
    selected=normalize_senders(selected)
    if len(selected)!=1:raise ValueError('passive single-sender audit needs exactly one active STA')
    name=selected[0];port=PORTS[STAS.index(name)]
    if 'sender_STA' in spec:
        if spec['sender_STA']!=name or spec.get('sender_port')!=port:raise ValueError('passive sender identity/port conflicts with plan')
        mac=spec.get('sender_mac')
    else:
        # Preserve the already validated old STA1-only short capture files.
        if name!='STA1':raise ValueError('legacy passive spec only identifies STA1')
        mac=spec.get('STA1_mac')
    if not isinstance(mac,str) or not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',mac):raise ValueError('invalid passive sender MAC')
    return {'sender_STA':name,'sender_mac':mac,'sender_port':port}

def listener_window(plan):
    durations=tuple(plan.get(k) for k in ['warmup_s','measure_s','tail_s'])
    if durations not in [(5,30,5),(15,180,15)]:raise ValueError('unexpected single-sender window')
    t0=plan['t0_ns'];warm,measure,tail=durations
    return {'start_ns':t0+warm*1_000_000_000,'stop_ns':t0+(warm+measure)*1_000_000_000,
            'capture_stop_ns':t0+(warm+measure+tail+5)*1_000_000_000}

def verify_listener_readbacks(spec,selected,configs):
    target=listener_target(spec,selected)
    for name,mac in [(target['sender_STA'],target['sender_mac']),('AP',spec['AP_mac'])]:
        text=configs[name]['wireless']['stdout']
        match=re.search(r'\baddr ([0-9a-f:]{17})\b',text)
        if not match or match.group(1)!=mac:raise ValueError('passive MAC conflicts with live configuration: '+name)
    return target

def target_from_run(spec,manifest):
    return listener_target(spec,active_from_manifest(manifest))
