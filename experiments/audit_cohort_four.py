"""Offline retry/failure and passive ANI evidence, never a probability approval."""
from collections import Counter
import json
from pathlib import Path
import sys
from cohort_trace import parse
from analyze_retry_descriptors import decode
from sync_run import write


def trace_evidence(events):
    tx=[d for d in events if d['kind']=='txdone']
    updates=[d for d in events if d['kind']=='mibupdate']
    layouts=Counter();budgets=Counter();long=Counter();short=Counter();status=Counter()
    failures=[];success_DATA_proxy=0;success_protected_DATA_proxy=0;prior_count=0
    for d in tx:
        x=decode(d);layout=[s['tries'] for s in x['stages']]
        layouts[str(layout)]+=1;budgets[str(sum(layout))]+=1
        long[str(d['ts_longretry'])]+=1;short[str(d['ts_shortretry'])]+=1
        status[str(d['ts_status'])]+=1
        prior=sum(layout[:d['ts_rateindex']]);prior_count+=bool(prior)
        if d['ts_status']&32:
            success_DATA_proxy+=prior+d['ts_longretry']+1
            if x['RTS_enabled']:success_protected_DATA_proxy+=prior+d['ts_longretry']+1
        else:
            failures.append({'seq':d['seq']>>4,'skb':d.get('skb'),'bf':d.get('bf'),
                'mono_ns':d['mono_ns'],'len':d['len'],'status':d['ts_status'],'flags':d['ts_flags'],
                'retry_exhausted':d['ts_status']==1 and d['ts_flags']==0,
                'protected':x['RTS_enabled'],'configured_tries':layout,'final_series':d['ts_rateindex'],
                'raw_long':d['ts_longretry'],'raw_short':d['ts_shortretry'],
                'prefix_plus_raw_long':prior+d['ts_longretry'],
                'prefix_plus_raw_long_plus_one':prior+d['ts_longretry']+1,'raw':d})
    ani={}
    if len(updates)>1:
        first,last=updates[0],updates[-1]
        fields={'good':'incgood','bad':'incbad','ackbad':'incack'}
        ani={'first_update_ns':first['mono_ns'],'last_update_ns':last['mono_ns'],
            'cumulative_deltas':{k:(last[k]-first[k])&0xffffffff for k in fields},
            'increment_closure':{k:((last[k]-first[k])&0xffffffff)==(sum(d[v] for d in updates[1:])&0xffffffff) for k,v in fields.items()},
            'first_TX_minus_first_update_ms':(tx[0]['mono_ns']-first['mono_ns'])/1e6 if tx else None,
            'last_update_minus_last_TX_ms':(last['mono_ns']-tx[-1]['mono_ns'])/1e6 if tx else None,
            'TX_completions_outside_ANI_endpoint_interval':sum(not(first['mono_ns']<=d['mono_ns']<=last['mono_ns']) for d in tx)}
        ani['RTS_success_delta_minus_ACK_success_protected_DATA_proxy']=ani['cumulative_deltas']['good']-success_protected_DATA_proxy
        ani['comparison_warning']='Whole PHY cumulative updates and completion proxies have different event boundaries; includes nonbusiness traffic. Agreement is not calibration; no one-count tolerance.'
    return {'scope':'whole trace/whole PHY; NOT the complete-business measurement cohort',
        'TX_completions':len(tx),'ANI_updates':len(updates),'configured_retry_layouts':dict(layouts),
        'configured_DATA_budget_histogram':dict(budgets),'cross_series_completion_count':prior_count,
        'raw_long_histogram':dict(long),'raw_short_histogram':dict(short),'terminal_status_histogram':dict(status),
        'ACK_success_DATA_attempt_proxy':success_DATA_proxy,
        'ACK_success_protected_DATA_attempt_proxy':success_protected_DATA_proxy,
        'terminal_failures':failures,'passive_ANI':ani,'formal_probability_approved':False,
        'terminal_failure_mapping_approved':False,'CW_trajectory_observed':False}


def analyze(folder):
    metrics=json.loads((folder/'metrics.json').read_text(encoding='utf-8'))
    per={}
    for name in ['STA1','STA2','STA3','STA4']:
        evidence=trace_evidence(list(parse(folder/'raw'/name/'tx_trace.txt')))
        evidence['complete_cohort_terminal_failure_count']=metrics['per_STA'][name]['terminal_failure']
        evidence['complete_cohort_delivery_ratio']=metrics['per_STA'][name]['delivery_ratio']
        evidence['complete_cohort_categories']=metrics['per_STA'][name]['window_coverage']['categories']
        evidence['air_scope']='AP same-PHY monitor, middle 10/15 s only; cannot observe its own ACK/CTS and not a full-window failure oracle'
        per[name]=evidence
    result={'kind':'offline four-STA failure coverage evidence','per_STA':per,
        'raw_preserved':True,'formal_probability_approved':False}
    write(folder/'retry_failure_evidence.json',result)
    return result


if __name__=='__main__':
    result=analyze(Path(sys.argv[1]))
    print({n:{'TX':v['TX_completions'],'failures':len(v['terminal_failures']),
        'complete_failures':v['complete_cohort_terminal_failure_count']} for n,v in result['per_STA'].items()})
