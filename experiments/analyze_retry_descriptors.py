"""Decode read-only AR9285 descriptors and inventory; never approve RTS p."""
import csv
from collections import Counter
import hashlib
import json
import re
from pathlib import Path
import shutil
import sys

from analyze_retry_audit import trace_rows
from sync_run import ROOT,write


def decode(d):
    stages=[]
    for i in range(4):
        packed=d['ds_ctl4' if i<2 else 'ds_ctl5'];shift=16*(i%2)
        idx=d['r%d_idx'%i];cf=d['r%d_cf'%i]
        stages.append({'tries':(d['ds_ctl2']>>(16+4*i))&15,
            'hw_rate':(d['ds_ctl3']>>(8*i))&255,
            'packet_duration':(packed>>shift)&32767,'rts_cts_qual':bool(packed&(1<<(shift+15))),
            'chain':(d['ds_ctl7']>>(5*i+2))&7,'width40':bool(d['ds_ctl7']&(1<<(5*i))),
            'short_gi':bool(d['ds_ctl7']&(1<<(5*i+1))),
            'power_field':((d['ds_ctl0']>>16) if i==0 else (d['ds_ctl%d'%(8+i)]>>24))&63,
            'software_idx':idx if idx<128 else idx-256,'software_count':cf&31,'software_flags':cf>>5})
    return {'stages':stages,'RTS_enabled':bool(d['ds_ctl0']&0x00400000),
            'CTS_self_enabled':bool(d['ds_ctl0']&0x80000000),'no_ACK':bool(d['ds_ctl1']&0x01000000),
            'control_hw_rate':(d['ds_ctl7']>>20)&255,
            'raw_short':(d['ds_status1']>>4)&15,'raw_long':(d['ds_status1']>>8)&15,
            'raw_final_series':(d['ds_status9']>>21)&3,'done':bool(d['ds_status9']&1)}


def proposal_budget(decoded):
    stages=decoded['stages'];active=[s for s in stages if s['tries']]
    fields=['hw_rate','packet_duration','rts_cts_qual','chain','width40','short_gi','power_field','software_idx','software_flags']
    if len(active)<2 or any(not stages[i]['tries'] for i in range(len(active))):return None
    if any(s['tries']!=s['software_count'] or s['tries']>15 or any(s[k]!=active[0][k] for k in fields) for s in active):return None
    budget=sum(s['tries'] for s in active)
    return budget if budget<=15 else None


def main(folder):
    trace=folder/'raw/STA1/rep01/tx_trace.txt'
    rows=[d for d in trace_rows(trace) if d['len']==1266 and d['fc']&0xfc==0x88 and d['tid']==0]
    counts=Counter();waveforms=Counter();budgets=Counter();mismatches=[];examples={}
    with (folder/'decoded_descriptors.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.writer(f);w.writerow(['mono_ns','seq','tries0','tries1','tries2','tries3','nominal_DATA_budget','final_series','raw_short','raw_long','ctl0','ctl2','ctl3','ctl4','ctl5','ctl7','status1','status9'])
        for d in rows:
            x=decode(d);tries=tuple(s['tries'] for s in x['stages']);counts[tries]+=1
            budgets[proposal_budget(x)]+=1
            waveform=tuple((s['hw_rate'],s['packet_duration'],s['rts_cts_qual'],s['chain'],s['width40'],s['short_gi'],s['power_field']) for s in x['stages'] if s['tries'])
            waveforms[waveform]+=1;examples.setdefault(str(tries),{'decoded':x,'trace_fields':d})
            if not (x['raw_short']==d['ts_shortretry'] and x['raw_long']==d['ts_longretry'] and x['raw_final_series']==d['ts_rateindex'] and x['done'] and all(s['tries']==s['software_count'] for s in x['stages'])):
                mismatches.append(d['seq_num'])
            w.writerow([d['mono_ns'],d['seq_num'],*tries,sum(tries),d['ts_rateindex'],x['raw_short'],x['raw_long'],*[hex(d[k]) for k in ['ds_ctl0','ds_ctl2','ds_ctl3','ds_ctl4','ds_ctl5','ds_ctl7','ds_status1','ds_status9']]])
    nodes={};restored=True
    for n in ['AP','STA1','STA2','STA3','STA4']:
        before=json.loads((folder/'raw'/n/'hardware_before.json').read_text());after=json.loads((folder/'raw'/n/'hardware_after.json').read_text())
        different={k:[v,after['raw_registers']['values'][k]] for k,v in before['raw_registers']['values'].items() if v!=after['raw_registers']['values'][k]}
        # AP intentionally retains its stock driver and lacks edca_ctrl.
        q=int(re.search(r'\(BE\):\s+qnum:\s+(\d+)',before['commands']['queues']['stdout']).group(1))
        radio=before['commands']['wireless']['stdout']
        retry=re.search(r'Retry short limit:(\d+)',radio)
        after_radio=after['commands']['wireless']['stdout']
        same_radio=all(re.search(pattern,radio).group(0)==re.search(pattern,after_radio).group(0)
            for pattern in [r'channel \d+ \(\d+ MHz\)',r'txpower [\d.]+ dBm',r'RTS thr[:=](?:off|\d+)'])
        ok=(before['commands']['modules']['stdout']==after['commands']['modules']['stdout'] and
            before['commands']['identity']['stdout'].splitlines()[2]==after['commands']['identity']['stdout'].splitlines()[2] and
            before['commands']['basic_override']['stdout']==after['commands']['basic_override']['stdout'] and
            before['raw_registers']['regidx_restored'] and after['raw_registers']['regidx_restored'] and
            after['commands']['processes']['code']==1 and not different and same_radio and
            (n=='AP' or 'Connected to' in after_radio))
        nodes[n]={'BE_qnum':q,'before_BE':before['decoded_queues'][str(q)],'after_BE':after['decoded_queues'][str(q)],
            'iwconfig_retry_short':int(retry.group(1)) if retry else None,'register_changes':different,'unchanged_checks_pass':ok}
        restored=restored and ok
    rts=json.loads((folder/'metrics.json').read_text(encoding='utf-8'))
    audited=rts['repetitions'][0]
    mib=audited.get('whole_flow_MIB',{})
    e=next((e for v in audited['air'].values() for e in v['RTS_excess_examples']),None)
    evidence=[]
    if mib:
        evidence.append('本轮整段 MIB / TX trace：成功 RTS %d/%d，ACK 失败 %d/%d，失败 RTS %d/%d。该口径含预热/收尾和 PHY 控制流量，不是严格测量窗口。'%(mib['delta']['dot11RTSSuccessCount'],mib['trace_data'],mib['delta']['dot11ACKFailureCount'],mib['trace_ACK_failed'],mib['delta']['dot11RTSFailureCount'],mib['trace_rts_failed']))
    if e:
        matching=[d for d in rows if d['seq_num']==e['seq'] and d['ts_rateindex']==e['final_series'] and d['ts_shortretry']==e['shortretry']]
        evidence.append('空口例 seq=%d：至少捕获 %d 次 RTS，候选公式仅 %d 次。现有最终字段未提供这部分前序历史，不能称为核准。'%(e['seq'],e['captured_RTS_lower_bound'],e['trace_RTS_estimate']))
        if len(matching)==1:
            d=matching[0]
            evidence.append('对应原始 ctl2=%s，status1=%s，status9=%s；软件 short=%d、long=%d、final-series=%d 与这些原始字段吻合。'%(hex(d['ds_ctl2']),hex(d['ds_status1']),hex(d['ds_status9']),d['ts_shortretry'],d['ts_longretry'],d['ts_rateindex']))
    result={'scope':'normal multi-series RTS baseline only; no single-series driver compiled/loaded',
            'packets_full_flow':len(rows),'descriptor_mismatches':mismatches,
            'tries_histogram':{str(k):v for k,v in counts.items()},'nominal_budget_histogram':{str(k):v for k,v in budgets.items()},
            'waveform_patterns':[{"pattern":k,"packets":v} for k,v in waveforms.items()],
            'examples':examples,'nodes':nodes,'final_inventory_pass':restored,
            'RTS_p_calibrated':False,'air_contradiction_seen_this_probe':rts['air_RTS_contradiction'],
            'proposal_status':'review-only, not linked into production',
            'raw_sha256':{str(p.relative_to(folder)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (folder/'raw').rglob('*') if p.is_file()}}
    write(folder/'descriptor_metrics.json',result)
    lines=['# 当前重试段与原始硬件状态记录','',
           '仅 STA1 按原发送策略做 5/10/5 s 单发；AP/另外三个 STA 监听。未编译或加载单段驱动，未改寄存器值、未重启；RTS 阈值临时设 500 后恢复，原始文件保留。','',
           '## 描述符与软件核对','',
           '- 整段流量业务包数：%d；原始 short/long/final-series、TxDone、段次数与软件不一致：%d。'%(len(rows),len(mismatches)),
           '- 这里只验证字段读出无误，不能从最终描述符恢复已丢失的前序失败 RTS 历史。',
           '- 速率编码 0x09 = 24 Mbps，控制速率编码 0x1b = 1 Mbps；原始 descriptor PacketDuration=460，不能直接当作完整交换时长。',
           '- 描述符 power_field=63 是原始硬件字段，不等于实际发射 31.5 dBm；iw 读回 STA 为 14 dBm，此字段在方案中保持不变。','',
           '|实际 DATA 段次数|整段包数|名义 DATA 预算|拟合并后|','|---|---:|---:|---|']
    for c,count in counts.items():lines.append('|%s|%d|%d|[%d,0,0,0]|'%(list(c),count,sum(c),sum(c)))
    lines+=['','有效段均为相同 legacy 24 Mbps、RTS 资格开启、相同 PacketDuration/天线链/功率字段；每包原始全部 ctl/status 字保存在 tx_trace.txt，解码明细在 decoded_descriptors.csv。','',
            '## 原始寄存器（按各节点实际 BE 队列解析）','','|节点|iw short limit|硬件 frame-short|station-short|station-long|reset bit|CWmin/max|前后检查|','|---|---:|---:|---:|---:|---|---|---|']
    for n,x in nodes.items():
        q=x['before_BE'];lines.append('|%s|%s|%d|%d|%d|%s|%d/%d|%s|'%(n,x['iwconfig_retry_short'],q['frame_short_retry'],q['station_short_retry'],q['station_long_retry'],q['retry_counter_reset_enable'],q['cwmin'],q['cwmax'],x['unchanged_checks_pass']))
    lines+=['','硬件 BE D_RETRY_LIMIT=0x0008200a、D_MISC=0x00001102；只切换并恢复 regidx 来读取，未写 regval，未直接读取 clear-on-read MIB。',
            'iw 的 7、硬件 frame-short 的 10、descriptor DATA 的 9/12 是不同层次/含义，不能互相替代。对应源码 mac.c 的默认 INIT_SH_RETRY=10，station short/long 默认 32。','',
            '## 当前结论与单段草案','',
            '本次是否仍看到 RTS 公式空口反例：%s；RTS 概率仍不核准。'%rts['air_RTS_contradiction'],
            *evidence,
            '草案只逐包保留 ΣTries 的名义 DATA 预算；若总和超过 4-bit 上限 15 或任一有效段配置不同，则拒绝合并，绝不截断。同步更新 bf->rates 的软件记账，以免描述符改了、完成回报仍按旧段解释。',
            '**不能承诺总 RTS 尝试上限、提前终止条件、CW 演化或速控反馈等价。** 单段只用于解释计数边界，不是正式实验的无影响替换，也不能预先保证它会解决 shortretry 漏计。',
            '拟修改代码见 proposal/，尚未接入生产 xmit.c、patch series 或持久包。需先审阅再决定是否对 STA1 短时 A/B/A；其余 STA 与 AP 保持不变。','',
            '字段定义参考原始 [Linux ath9k ar9002 描述符处理](https://kernel.googlesource.com/pub/scm/linux/kernel/git/netdev/net/+/b85966adbf5de0668a815c6e3527f87e0c387fb4/drivers/net/wireless/ath/ath9k/ar9002_mac.c)，本次偏移来自与实际运行 ath9k/ath9k_hw srcversion 匹配的本机 DWARF，具体掩码以存档的 5.15 源码为准。']
    (folder/'analysis.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    m=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    m['descriptor_analysis']='descriptor_metrics.json';m['RTS_count_calibration_pass']=False
    m['single_series_driver_loaded']=False;m['final_inventory_pass']=restored
    m['status']='normal_policy_descriptor_audit_complete_pending_code_review'
    write(folder/'manifest.json',m)
    if Path(__file__).resolve()!=(folder/'analyze_retry_descriptors.py').resolve():shutil.copy2(__file__,folder/'analyze_retry_descriptors.py')
    print('Descriptor records',len(rows),'raw/software mismatches',len(mismatches),'restored',restored,'budgets',counts)

if __name__=='__main__':main(Path(sys.argv[1]))
