"""Explicit same-batch one-sender windows; no scans or available-subset means."""
import json,math,statistics
from pathlib import Path
from sender_selection import STAS,active_from_manifest
from bianchi_dai_probability import basic_failure_sensitivity
T95={1:12.7062047364,2:4.30265272975,3:3.18244630528,4:2.77644510520}
def describe(values):
    known=[x for x in values if x is not None];n=len(values)
    result={'n_windows':n,'n_available':len(known),'values':values,'mean':None,'SD':None,'CI95':None,'repeat_unit':'independent window'}
    if not n or len(known)!=n:return result
    result['mean']=statistics.mean(known)
    if n>1:
        if n-1 not in T95:raise ValueError('planned maximum five windows per sender')
        sd=statistics.stdev(known);half=T95[n-1]*sd/math.sqrt(n);result.update(SD=sd,CI95=[result['mean']-half,result['mean']+half])
    return result
def load(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def window_row(entry):
    folder=Path(entry['folder']);m=load(folder/'manifest.json');v=load(folder/'metrics.json');name=entry['sender']
    if active_from_manifest(m)!=[name] or set(v['per_STA'])!={name}:raise ValueError('wrong sender or mixed window')
    if not v['acquisition_pass'] or not v['saturation_supported']:raise ValueError('nonvalid acquisition cannot enter primary statistics')
    air=load(folder/'passive_single_audit.json')
    if not air['capture_gate_pass'] or air['sender_STA']!=name:raise ValueError('passive audit is missing/invalid for selected sender')
    s=v['per_STA'][name];p=s['probability'];original=p['p_ACK']
    auxiliary=basic_failure_sensitivity(folder/(name+'_packet_ledger.jsonl')) if original is None else None
    aux_value=original if original is not None else auxiliary['p_ACK_proxy'] if auxiliary else None
    if p['complete_packets']!=s['complete_packets'] or p['terminal_failures']!=s['terminal_failure']:raise ValueError('complete cohort failure coverage differs')
    return {'order':entry['order'],'block':entry['block'],'sender':name,'folder':str(folder),'S_Mbps':s['AP_goodput_Mbps'],
            'HoL_success_us':s['success_conditional_HoL_us'],'p_ACK_original':original,'failure_original':None if original is None else 1-original,
            'p_ACK_aux_full':aux_value,'failure_aux_full':None if aux_value is None else 1-aux_value,
            'auxiliary_formula':auxiliary,'probability_quality':p['quality'],'complete_packets':s['complete_packets'],
            'complete_failures':s['terminal_failure'],'final_MAC_delivery':s['delivery_ratio'],'window_coverage':s['window_coverage'],
            'unknown_fraction':s['unknown_fraction'],'queue_nonempty':s['queue_nonzero_fraction'],'queue_backlog':s['queue_backlog_fraction'],
            'HoL_scope':'success-conditional complete services, host service-start proxy; cross-boundary/unknown separate',
            'p_scope':'all complete services including failures; original NA preserved; full-cohort auxiliary mapping separately labeled'}
def group_rows(rows):
    fields=['S_Mbps','HoL_success_us','p_ACK_original','failure_original','p_ACK_aux_full','failure_aux_full','final_MAC_delivery']
    output={}
    for name in STAS:
        selected=[r for r in rows if r['sender']==name]
        if len(selected)>5 or len({r['block'] for r in selected})!=len(selected):raise ValueError('duplicate/extra repeated windows')
        output[name]={'planned_windows':5,'valid_windows':len(selected),'complete':len(selected)==5,'complete_failures':sum(r['complete_failures'] for r in selected),
                      'statistics':{f:describe([r[f] for r in selected]) for f in fields}}
    return output
