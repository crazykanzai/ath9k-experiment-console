"""Explicit approximate counters; never relabel a proxy as calibrated hardware p."""
import json

def basic_failure_sensitivity(path):
    """Two explicit failure formulas, not a proven physical-count bound."""
    success=failed=den=0
    with path.open(encoding='utf-8') as handle:
        for line in handle:
            r=json.loads(line)
            if r['category']!='complete':continue
            d=r['raw'];layout=[d['r%d_cf'%i]&31 for i in range(4)]
            n=sum(layout[:d['ts_rateindex']])+d['ts_longretry']
            if r['acked']:success+=1;den+=n+1
            else:
                if d['ts_status']!=1 or d['ts_flags']!=0 or n!=sum(layout):return None
                failed+=1;den+=n
    if not den:return None
    return {'p_ACK_proxy':success/den,'ACK_success':success,'terminal_failures':failed,
        'denominator_prefix_raw_failure':den,'denominator_plus_one_failure':den+failed,
        'formula_sensitivity_interval':[success/(den+failed),success/den],
        'formula_gap_percentage_points':100*(success/den-success/(den+failed)),
        'quality':'approximate_Basic_exhausted_packet_mapping',
        'warning':'all failed packets included; interval compares prefix+raw vs prefix+raw+1, NOT a confidence interval or proven physical-count bound'}

def attempts(records, mode):
    complete=[r for r in records if r['category']=='complete']
    success=sum(r['acked'] for r in complete)
    failures=sum(not r['acked'] for r in complete)
    data=short=prefix=0
    for r in complete:
        d=r['raw'];prior=sum(d['r%d_cf'%i]&31 for i in range(d['ts_rateindex']))
        prefix+=prior;data+=prior+d['ts_longretry']+1;short+=d['ts_shortretry']
    known=bool(complete) and not failures
    den=data+(short if mode=='rts' else 0)
    return {'complete_packets':len(complete),'ACK_success':success,'terminal_failures':failures,
        'successful_path_DATA_proxy':data if known else None,
        'preceding_DATA_series_added':prefix,'terminal_short_sum':short,
        'attempt_denominator_proxy':den if known else None,
        'p_ACK':success/den if known and den else None,
        'p_CTS_proxy':data/den if known and den and mode=='rts' else None,
        'quality':'approximate_missing_earlier_failed_RTS' if mode=='rts' else 'Basic_ABI2_success_path',
        'failure_policy':'any unmapped terminal failure => p unavailable; failures are NOT removed',
        'scope':'complete services in common wall-clock window; not every physical attempt in that interval'}


def merge(parts):
    known=all(v['attempt_denominator_proxy'] is not None for v in parts.values())
    den=sum(v['attempt_denominator_proxy'] or 0 for v in parts.values())
    successes=sum(v['ACK_success'] for v in parts.values())
    cts=sum(v['successful_path_DATA_proxy'] or 0 for v in parts.values())
    protected=all(v['p_CTS_proxy'] is not None for v in parts.values())
    return {'p_ACK':successes/den if known and den else None,
        'p_CTS_proxy':cts/den if known and den and protected else None,
        'attempt_denominator_proxy':den if known else None,'ACK_success':successes,
        'terminal_failures':sum(v['terminal_failures'] for v in parts.values()),
        'formal_probability_approved':False}
