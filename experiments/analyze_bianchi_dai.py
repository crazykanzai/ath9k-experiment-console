"""Trend profile analysis, preserving exact-null fields and raw bytes."""
import hashlib
import json
from pathlib import Path
import shutil
from analyze_cohort_sync import analyze as cohort_analyze
from analyze_fifo_isolation import native
from audit_cohort_four import trace_evidence
from cohort_trace import parse
from bianchi_dai_probability import attempts,merge
from sync_run import write
from sender_selection import active_from_manifest,saturation_gate

def load(path):return json.loads(path.read_text(encoding='utf-8'))

def analyze(folder):
    manifest=load(folder/'manifest.json')
    if manifest.get('kind')!='Bianchi-Dai trend experiment':raise ValueError('wrong profile')
    metrics=cohort_analyze(folder);per={};evidence={};native_rows={};issues=metrics['issues']
    for name in ['STA1','STA2','STA3','STA4']:
        base=folder/'raw'/name
        if name in metrics['per_STA']:
            per[name]=attempts([json.loads(line) for line in (folder/(name+'_packet_ledger.jsonl')).read_text(encoding='utf-8').splitlines()],metrics['mode'])
        ev=trace_evidence(list(parse(base/'tx_trace.txt')))
        short=sum(int(k)*v for k,v in ev['raw_short_histogram'].items())
        long=sum(int(k)*v for k,v in ev['raw_long_histogram'].items())
        ani=ev.get('passive_ANI',{});delta=ani.get('cumulative_deltas',{})
        corrected_den=ev['ACK_success_protected_DATA_attempt_proxy']+short
        mib_den=delta.get('good',0)+delta.get('bad',0)
        sensitivity=None
        if metrics['mode']=='rts' and not ev['terminal_failures'] and corrected_den and mib_den:
            sensitivity=100*ev['TX_completions']*(1/corrected_den-1/mib_den)
        evidence[name]={'TX_completions':ev['TX_completions'],'terminal_failure_count':len(ev['terminal_failures']),
            'raw_terminal_short_sum':short,'raw_terminal_long_sum':long,
            'DATA_proxy':ev['ACK_success_DATA_attempt_proxy'],'passive_ANI':ani,
            'whole_flow_probability_difference_percentage_points':sensitivity,
            'review_flag':sensitivity is not None and abs(sensitivity)>.5,
            'warning':'whole PHY/whole flow asynchronous comparison, NOT exact window p or an error bound'}
        cfg={k:load(base/('config_'+k+'.json')) for k in ['before','window_start','window_stop','after']}
        counts={k:native(c['native_xmit']['stdout']) for k,c in cfg.items()}
        d={field:{ac:counts['after'][field][ac]-counts['before'][field][ac] for ac in counts['before'][field]} for field in counts['before']}
        native_rows[name]={'whole_flow_counter_delta':d,'FIFO_readbacks':{k:c['FIFO_config'] for k,c in cfg.items()}}
        if any(v<0 for row in d.values() for v in row.values()):issues.append(name+' native counters reset')
        if any(c['FIFO_config']['threshold_64B']!=20 or not c['FIFO_config']['selector_restored'] for c in cfg.values()):issues.append(name+' FIFO configuration changed')
        if any(v for field in ['FIFO Underrun','DATA Underrun','DELIM Underrun','DESC CFG Error'] for v in d[field].values()):issues.append(name+' native hardware error')
        if name in per:metrics['per_STA'][name]['probability']=per[name]
    metrics.update(kind='Bianchi-Dai trend experiment',probability=merge(per),probability_crosscheck=evidence,
        native_hardware=native_rows,acquisition_pass=not issues,probability_quality='approximate_not_hardware_calibrated')
    selected=active_from_manifest(manifest)
    metrics['saturation_supported']=saturation_gate(metrics['per_STA'],selected)
    metrics['saturation_scope']='all and only planned active senders; inactive nodes remain association/configuration/FIFO/clock/trace-health gated'
    metrics['single_STA_non_saturation_diagnostic']=manifest.get('parameters',{}).get('single_sta')
    metrics['FIFO_errors']=sum(sum(v['whole_flow_counter_delta']['FIFO Underrun'].values()) for v in native_rows.values())
    write(folder/'metrics.json',metrics)
    manifest=load(folder/'manifest.json')
    manifest.update(status='trend_acquisition_valid' if not issues else 'trend_invalid',valid=not issues,
        validity_scope='acquisition and common window; probability approximate; not pure collision-model validation',
        probability_quality=metrics['probability_quality'],saturation_supported=metrics['saturation_supported'])
    write(folder/'manifest.json',manifest)
    with (folder/'analysis.md').open('a',encoding='utf-8') as f:
        f.write('\n## Bianchi-Dai trend profile\n\n')
        f.write('原精确概率字段保持不可核准；下列概率是补齐已知前序DATA段的独立代理字段。\n\n')
        f.write('|节点|ACK/请求代理|CTS/RTS代理|全程MIB对照差(百分点)|\n|---|---:|---:|---:|\n')
        for n,v in per.items():f.write('|%s|%s|%s|%s|\n'%(n,v['p_ACK'],v['p_CTS_proxy'],evidence[n]['whole_flow_probability_difference_percentage_points']))
        f.write('\n合并：'+json.dumps(metrics['probability'])+'\n\n饱和证据通过：'+str(metrics['saturation_supported'])+'；FIFO错误：'+str(metrics['FIFO_errors'])+'。\n')
        f.write('\nMIB对照含整PHY/预热/收尾，不是180秒业务窗口真值。成功条件HoL、跨界/失败和环境限制仍适用。\n')
    for name in ['analyze_bianchi_dai.py','bianchi_dai_probability.py','audit_cohort_four.py']:
        shutil.copy2(Path(__file__).with_name(name),folder/'controller'/name)
    print('BIANCHI DAI',metrics['acquisition_pass'],'saturated',metrics['saturation_supported'],
        'Mbps',round(metrics['aggregate_goodput_Mbps'],3),'HOL_ms',metrics['pooled_success_conditional_HoL_us']/1000,
        'p',metrics['probability'],flush=True)
    return metrics

if __name__=='__main__':
    import sys
    raise SystemExit(0 if analyze(Path(sys.argv[1]))['acquisition_pass'] else 2)
