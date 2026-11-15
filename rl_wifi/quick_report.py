"""Read exactly the eight sealed inputs; no historical inference or node I/O."""
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import statistics


def decision(rows):
    if len(rows)!=8 or any(r['status']!='completed' or not r.get('metrics',{}).get('inference_usable') for r in rows):
        return {'code':'insufficient','text':'有效且可用于业务推断的数据不足8窗，当前证据不足。','worth_training':False}
    by={(r['block'],r['config'],r['state']):r['metrics'] for r in rows}
    p_ok=all(by[b,'P',s]['performance_target_met'] for b in (1,2) for s in ('U','B'))
    all_bad=all(not r['metrics']['performance_target_met'] for r in rows)
    pairs=[]
    for b in (1,2):
        for s in ('U','B'):
            p,e=by[b,'P',s],by[b,'E',s]
            pt,et=p['application']['timeout_ratio'],e['application']['timeout_ratio']
            pb,eb=p['background_Mbps'],e['background_Mbps']
            pairs.append({'block':b,'state':s,'E_minus_P_timeout_pp':100*(et-pt),
                          'E_minus_P_background_Mbps':eb-pb,'E_background_gain_fraction':eb/pb-1 if pb else None})
    # Two blocks must each repeat the predeclared practical trade-off. These
    # comparisons are signals, never a test of DQN or a global fixed optimum.
    uniform_signal=all(by[b,'E','U']['performance_target_met'] and
          by[b,'E','U']['background_Mbps']>=1.05*by[b,'P','U']['background_Mbps'] and
          by[b,'E','U']['application']['timeout_ratio']<=by[b,'P','U']['application']['timeout_ratio']+.01 for b in (1,2))
    burst_signal=all(by[b,'P','B']['performance_target_met'] and
          by[b,'E','B']['application']['timeout_ratio']-by[b,'P','B']['application']['timeout_ratio']>=.05 and
          by[b,'P','B']['background_Mbps']>=.95*by[b,'E','B']['background_Mbps'] for b in (1,2))
    practical_e=[]
    for s in ('U','B'):
        repeated=all((by[b,'P',s]['application']['timeout_ratio']-by[b,'E',s]['application']['timeout_ratio']>=.05 and
                      by[b,'E',s]['background_Mbps']>=.95*by[b,'P',s]['background_Mbps']) or
                     (by[b,'E',s]['performance_target_met'] and by[b,'P',s]['performance_target_met'] and
                      by[b,'E',s]['background_Mbps']>=1.05*by[b,'P',s]['background_Mbps'] and
                      by[b,'E',s]['application']['timeout_ratio']<=by[b,'P',s]['application']['timeout_ratio']+.01) for b in (1,2))
        practical_e.append(repeated)
    if all_bad:code,text='capacity_gap','两个配置在本次两状态的全部重复都未达到业务目标；优先查业务目标与平台服务能力缺口，当前不投入长训练。'
    elif uniform_signal and burst_signal:code,text='dynamic_signal','两个区组重复出现“均匀用E保吞吐、突发用P保期限”的工程权衡，值得进一步研究动态选择；尚未证明在线净收益或DQN优势。'
    elif p_ok and not any(practical_e):code,text='fixed_priority_sufficient','P在两状态的全部重复均达标，E未重复出现预登记的实用收益；本次筛选支持暂用固定优先配置，暂不投入长训练。'
    else:code,text='insufficient','配置差异小、方向不一致或目标满足情况不足以构成重复的状态权衡；当前证据不足，不追加样本或长训练。'
    return {'code':code,'text':text,'worth_training':False,'worth_studying_dynamic_selection':code=='dynamic_signal','paired_differences':pairs}


def report(batch,out):
    batch=Path(batch);out=Path(out);out.mkdir(exist_ok=False)
    source=batch/'manifest.json';manifest=json.loads(source.read_text(encoding='utf-8'))
    rows=manifest['windows'];result=decision(rows)
    valid=sum(r['status']=='completed' for r in rows)
    not_started=sum(r['status']=='not_started' for r in rows)
    invalid=8-valid-not_started
    result.update(valid=valid,invalid=invalid,not_started=not_started,elapsed_s=manifest['elapsed_s'],
                  postflight=manifest.get('postflight'),batch_status=manifest['status'],error=manifest['error'])
    (out/'metrics.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    fields=['slot','block','config','state','status','timeout_ratio','rtt_mean_ms','rtt_p95_ms','rtt_reply_samples',
            'background_Mbps','on_time','late','no_reply','local_failure','pending','generation_delay_p99_ms']
    table=[]
    for row in rows:
        metric=row.get('metrics',{});app=metric.get('application',{});counts=app.get('counts',{})
        table.append({**{k:row[k] for k in fields if k in row},**{k:app.get(k) for k in fields if k in app},
                      **{k:counts.get(k) for k in fields if k in counts},'background_Mbps':metric.get('background_Mbps')})
    with (out/'all_windows.csv').open('w',newline='',encoding='utf-8-sig') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(table)
    lines=['# 固定8窗快速筛选结果','',result['text'],'',
        f"采集有效{valid}/8，无效或准备失败{invalid}/8，未启动{not_started}/8；本批实际耗时{manifest['elapsed_s']/60:.2f}分钟。",
        'P=STA1 W16、背景W64；E=全部W16。U=10ms一条；B=100ms同刻十条。每窗5/30/5秒。',
        '业务目标：100ms内至少95%按时完成。未达目标不是采集无效理由；全部本地失败仍计入分母。','',
        '|轮次|区组|配置/状态|采集状态|超时率|条件RTT均值ms|回复样本数|背景Mbps|本地失败|',
        '|---:|---:|---|---|---:|---:|---:|---:|---:|']
    carried=sum(bool(r.get('carried_from')) for r in rows)
    if carried:
        lines[3:3]=[f'显式续跑：保留原批{carried}个有效槽，仅执行剩余{8-carried}个；原失败/暂停及50→100ms持续门限版本分开记录。',
                    '跨暂停时段比较可能含时间漂移，不称完全同期试验。','']
    fmt=lambda v: 'NA' if v is None else f'{v:.3f}'
    for r,t in zip(rows,table):
        timeout='NA' if t.get('timeout_ratio') is None else f"{t['timeout_ratio']:.2%}"
        lines.append(f"|{r['slot']}|{r['block']}|{r['config']}/{r['state']}|{r['status']}|{timeout}|{fmt(t.get('rtt_mean_ms'))}|{t.get('rtt_reply_samples','NA')}|{fmt(t.get('background_Mbps'))}|{t.get('local_failure','NA')}|")
    lines+=['','## 全部重复与不确定性','',
            '重复单位为窗口，不是消息或秒桶。每条件n=2的t95%CI仅作宽度参照（df=1，t=12.706）；时段/小样本/系统误差限制保留。']
    groups=defaultdict(list)
    for row in rows:
        if row['status']=='completed' and row.get('metrics'):groups[row['config'],row['state']].append(row['metrics'])
    summaries={}
    for key,values in sorted(groups.items()):
        summaries['/'.join(key)]={}
        for field,values_field in [('timeout_ratio',[m['application']['timeout_ratio'] for m in values]),('background_Mbps',[m['background_Mbps'] for m in values])]:
            mean=statistics.mean(values_field);half=12.706*statistics.stdev(values_field)/2**.5 if len(values_field)==2 else None
            summaries['/'.join(key)][field]={'points':values_field,'mean':mean,'t95_CI':[mean-half,mean+half] if half is not None else None}
        lines.append(f"- {'/'.join(key)}：{json.dumps(summaries['/'.join(key)],ensure_ascii=False)}")
    lines+=['','## 停止、恢复与证据边界','',f"批次状态：{manifest['status']}；原因：{manifest['error'] or '完成固定8窗后停止'}。",
            f"六节点独立后检：{manifest.get('postflight',{}).get('confirmed','NA')}；授时清理见controller/preparation_final.json。",
            '固定窗口只比较稳态性能。业务中切换损失尚未测量；硬件写入/边界回执不能替代在线净收益。',
            '未启动窗口无性能值。无效窗口保留原始与诊断，不截取补点。不混入历史正式标定或旧40秒验收。',
            '本批未训练DQN、未运行UCB或完整算法对照，结束即停止；未发现收益不代表固定配置永远最优。']
    (out/'analysis.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    (out/'group_summaries.json').write_text(json.dumps(summaries,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    explicit=[source,batch/'sealed/plan.json']
    for row in rows:
        if row['status']!='not_started':
            for name in ('session.json','raw/STA1/application_probe.jsonl','raw/STA1/application_final.json','raw/AP/ap_udp.pcap','raw/MON15/air_full.pcap'):
                p=Path(row['carried_from'])/row['id']/name if row.get('carried_from') else batch/row['id']/name
                if p.is_file():explicit.append(p)
    inputs=[{'path':str(p.relative_to(batch)) if p.is_relative_to(batch) else str(p),
             'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in explicit]
    (out/'inputs.json').write_text(json.dumps(inputs,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False),flush=True)
