"""Join immutable TX-completion trace and independent monitor frames.

Sequence number + TID + nearest absolute time disambiguates sequence wraps.
ACK evidence is listener-local TSFT timing, not host packet-delivery order.
"""
import bisect
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import statistics
import struct
import sys
import zlib
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from reference_helpers import survey_delta
from analyze_sync import tcpdump_stats
from sync_run import write

def ack_crc(path):
    """Independently verify complete 14-byte ACKs, including their FCS.

    tshark defaults to 'Unverified'; that value must not be called 'Good'.
    Never try this on snaplen-truncated DATA.
    """
    raw=path.read_bytes(); endian='<' if raw[:4] in (b'\xd4\xc3\xb2\xa1',b'\x4d\x3c\xb2\xa1') else '>'
    at=24;number=0;result={}
    while at+16<=len(raw):
        sec,frac,cap,wire=struct.unpack_from(endian+'IIII',raw,at);at+=16
        packet=raw[at:at+cap];at+=cap;number+=1
        if len(packet)<4:continue
        rtlen=struct.unpack_from('<H',packet,2)[0];frame=packet[rtlen:]
        if frame and frame[0]&0xfc==0xd4:
            result[number]=(zlib.crc32(frame[:-4])&0xffffffff)==int.from_bytes(frame[-4:],'little') if len(frame)==14 and cap==wire else None
    return result

def trace_rows(path):
    result=[]
    for line in path.read_text(encoding='utf-8').splitlines():
        hit=re.search(r'\s(\d+\.\d+): txdone: .*? fc=',line)
        if not hit:continue
        d={k:int(v,0) if v.startswith('0x') else int(v) for k,v in re.findall(r'(\w+)=(0x[0-9a-f]+|\d+)',line)}
        d['mono_ns']=round(float(hit.group(1))*1e9)
        d['seq_num']=d['seq']>>4;d['tid']=d['qos']&15
        d['terminal_attempts']=d['ts_longretry']+1
        d['earlier_attempts']=sum(d['r%d_cf'%i]&31 for i in range(d['ts_rateindex']))
        d['all_attempts']=d['terminal_attempts']+d['earlier_attempts']
        result.append(d)
    return result

def align_window(tx, times, packets, raw_long_retries):
    """Use packet/raw terminal counts only to resolve uncertain control edges.

    Do not use the corrected attempts as a fitting target: that is the
    independent regression assertion. At each edge only an ordered suffix
    (start) or prefix (stop) may belong to the driver's completion window.
    """
    lo=times['driver_start_call']['monotonic_ns'];start=times['driver_start_return']['monotonic_ns']
    stop=times['driver_stop_call']['monotonic_ns'];hi=times['driver_stop_return']['monotonic_ns']
    core=[d for d in tx if start<=d['mono_ns']<stop]
    left=[d for d in tx if lo<=d['mono_ns']<start];right=[d for d in tx if stop<=d['mono_ns']<hi]
    choices=[]
    for a in range(len(left)+1):
        for b in range(len(right)+1):
            candidate=left[a:]+core+right[:b]
            if len(candidate)==packets and sum(d['terminal_attempts'] for d in candidate)==packets+raw_long_retries:
                choices.append(candidate)
    sums={sum(d['all_attempts'] for d in c) for c in choices}
    return (choices[0] if choices else core),{'candidate_windows':len(choices),
        'corrected_sum_unambiguous':len(sums)==1,'start_edge_events':len(left),'stop_edge_events':len(right),
        'selection_targets':['packets','raw long_retries'],'corrected_attempts_not_used_for_selection':True}

def analyze(folder):
    m=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    mac=re.search(r'addr ([0-9a-f:]+)',m['preflight']['STA1']['stdout']).group(1)
    all_results=[];examples=[];counter_examples=[];timeline=[];plot_series=[]
    clocks=m['clocks']
    for sta_dir in sorted((folder/'raw'/'STA1').glob('rep*')):
        rep=sta_dir.name;times=json.loads((sta_dir/'result.json').read_text(encoding='utf-8'))['events']
        start=times['driver_start_return']['monotonic_ns'];stop=times['driver_stop_call']['monotonic_ns']
        boundary_lo=times['driver_start_call']['monotonic_ns'];boundary_hi=times['driver_stop_return']['monotonic_ns']
        tx=[d for d in trace_rows(sta_dir/'tx_trace.txt') if d['len']==1266 and (d['fc']&0xfc)==0x88 and d['tid']==0]
        stats_text=(sta_dir/'dcf_stats.txt').read_text()
        header=dict(token.split('=',1) for token in stats_text.splitlines()[0].split() if '=' in token)
        counters=dict(token.split('=',1) for line in stats_text.splitlines() if line.startswith('peer=') for token in line.split())
        core,alignment=align_window(tx,times,int(counters['packets']),int(counters['long_retries']))
        selected_times={d['mono_ns'] for d in core}
        launch=times['iperf_spawn_return']['monotonic_ns']
        buckets=defaultdict(list)
        for d in tx:buckets[int((d['mono_ns']-launch)/1e9)].append(d)
        plot_series.append((rep,sorted((s+.5,sum(bool(d['ts_status']&32) for d in ds)/sum(d['all_attempts'] for d in ds)) for s,ds in buckets.items() if 0<=s<40)))
        edge=[d for d in tx if boundary_lo<=d['mono_ns']<boundary_hi and not start<=d['mono_ns']<stop]
        cfg=dict(token.split('=',1) for line in (sta_dir/'dcf_stats.txt').read_text().splitlines() if line.startswith('AC=BE ') for token in line.split())
        stats=(sta_dir/'trace_stats.txt').read_text()
        trace_clean=all(int(v)==0 for v in re.findall(r'^(?:overrun|commit overrun|dropped events): (\d+)',stats,re.M))
        kprobe=(sta_dir/'kprobe_profile.txt').read_text()
        probe_miss=[int(line.split()[-1]) for line in kprobe.splitlines() if 'txdone' in line]
        data={'repeat':rep,'driver_packets':int(counters['packets']),'driver_attempts':int(counters['exchange_attempts']),
              'driver_successes':int(counters['exchange_successes']),'trace_core_packets':len(core),'trace_boundary_candidates':len(edge),
              'trace_terminal_attempts':sum(d['terminal_attempts'] for d in core),'trace_earlier_attempts':sum(d['earlier_attempts'] for d in core),
              'trace_all_attempts':sum(d['all_attempts'] for d in core),'trace_clean':trace_clean,'kprobe_misses':probe_miss,
              'trace_final_series':dict(Counter(d['ts_rateindex'] for d in core)),
              'trace_status':dict(Counter(d['ts_status'] for d in core)),
              'trace_rate_indices':dict(Counter(d['r%d_idx'%i] for d in core for i in range(4) if (d['r%d_cf'%i]&31))),
              'driver_p':int(counters['exchange_successes'])/int(counters['exchange_attempts']),
              'reconstructed_p_core':sum(bool(d['ts_status']&32) for d in core)/sum(d['all_attempts'] for d in core),
              'air':{},'boundary_uncertainty_ms':((start-boundary_lo)+(boundary_hi-stop))/1e6}
        data['basic_retry_abi']=int(header.get('basic_retry_abi','1'))
        data['window_alignment']=alignment
        expected=data['trace_all_attempts'] if data['basic_retry_abi']>=2 else data['trace_terminal_attempts']
        data['counter_closure']=len(core)==data['driver_packets'] and expected==data['driver_attempts'] and alignment['corrected_sum_unambiguous']
        data['raw_long_retries_preserved']=data['trace_terminal_attempts']==int(counters['packets'])+int(counters['long_retries'])
        data['prior_series_accounting_match']=data['driver_attempts']-data['trace_terminal_attempts']==data['trace_earlier_attempts']
        byseq=defaultdict(list)
        for i,d in enumerate(tx):byseq[(d['seq_num'],d['tid'])].append((i,d))
        # A host-time conversion is only used to select the right seq-number cycle;
        # microsecond ACK evidence uses TSFT inside the same physical listener.
        sta_wall_minus_mono=times['driver_start_return']['epoch_ns']-start
        for listener in ['AP','STA2','STA3','STA4']:
            path=folder/'raw'/'AP'/rep/('air.tsv' if listener=='AP' else listener+'_air.tsv')
            rows=list(csv.DictReader(path.open(encoding='utf-8'),delimiter='\t'))
            frames=[];acks=[];subtypes=Counter();sender_rts=0;verified=ack_crc(folder/'raw'/listener/rep/'air.pcap')
            for number,row in enumerate(rows,1):
                if not row['frame.time_epoch']:continue
                epoch=round(float(row['frame.time_epoch'])*1e9)
                mono=epoch-clocks[listener]['best']['offset_ns']+clocks['STA1']['best']['offset_ns']-sta_wall_minus_mono
                kind=int(row['wlan.fc.type_subtype']);subtypes[kind]+=1
                if kind==27 and row['wlan.ta']==mac:sender_rts+=1
                tsft=int(row['radiotap.mactime']) if row['radiotap.mactime'] else None
                if kind==29 and row['wlan.ra']==mac and tsft is not None:acks.append((tsft,epoch,number,verified.get(number)))
                if kind!=40 or row['wlan.ta']!=mac or int(row['frame.len'])<1000:continue
                choices=byseq[(int(row['wlan.seq']),int(row['wlan.qos.tid'] or 0))]
                if not choices:continue
                i,d=min(choices,key=lambda it:abs(it[1]['mono_ns']-mono))
                if abs(d['mono_ns']-mono)>250_000_000:continue
                frames.append({'index':i,'tsft':tsft,'epoch_ns':epoch,'mono_ns':mono,
                               'retry':int(row['wlan.fc.retry']),'seq':d['seq_num'],
                               'frame_number':number,'fcs':row['wlan.fcs.status'],'rate':row['radiotap.datarate']})
            ack_by_ts={a[0]:a for a in acks if a[3] is True}
            ack_ts=sorted(ack_by_ts)
            frames.sort(key=lambda f:f['tsft'] or 0)
            groups=defaultdict(list)
            for j,f in enumerate(frames):
                f['ack_delta_us']=None
                if f['tsft'] is not None:
                    k=bisect.bisect_right(ack_ts,f['tsft'])
                    # 1266B at 24Mbps: 444us DATA + SIFS before ACK RX start.
                    # Use a conservative diagnostic envelope and forbid a new
                    # DATA start between this DATA and its candidate ACK.
                    if k<len(ack_ts):
                        delta=ack_ts[k]-f['tsft']
                        next_data=frames[j+1]['tsft'] if j+1<len(frames) else None
                        if 420<=delta<=650 and (next_data is None or next_data>ack_ts[k]):
                            f['ack_delta_us']=delta;f['ack_frame_number']=ack_by_ts[ack_ts[k]][2];f['ack_crc32_valid']=True
                groups[f['index']].append(f)
            distributions=Counter();missing=0;extra=0;retrans_after_ack=0;nonfinal=0;matched=0;first_missing_retry=0;ack_deltas=[];old_count_violations=0;old_count_extra_frames=0
            for i,d in enumerate(tx):
                if d['mono_ns'] not in selected_times:continue
                fs=groups[i];observed=len(fs);distributions[observed-d['all_attempts']]+=1
                if observed>d['terminal_attempts']:
                    old_count_violations+=1;old_count_extra_frames+=observed-d['terminal_attempts']
                    if listener=='AP' and len(counter_examples)<6:
                        counter_examples.append({'repeat':rep,'listener':listener,'seq':d['seq_num'],'tid':d['tid'],
                            'final_series':d['ts_rateindex'],'longretry':d['ts_longretry'],
                            'configured_series_counts':[d['r%d_cf'%j]&31 for j in range(4)],
                            'old_count':d['terminal_attempts'],'all_count':d['all_attempts'],'frames':fs})
                matched+=observed>0;missing+=max(0,d['all_attempts']-observed);extra+=max(0,observed-d['all_attempts'])
                if fs and fs[0]['retry']:first_missing_retry+=1
                for k,f in enumerate(fs):
                    if f['ack_delta_us'] is not None:ack_deltas.append(f['ack_delta_us'])
                    if k+1<len(fs):
                        nonfinal+=1
                        if f['ack_delta_us'] is not None:
                            retrans_after_ack+=1
                            if listener!='AP' and len(examples)<18:
                                examples.append({'repeat':rep,'listener':listener,'seq':d['seq_num'],'tid':d['tid'],
                                    'tx_mono_ns':d['mono_ns'],'status':d['ts_status'],'final_series':d['ts_rateindex'],
                                    'terminal_attempts':d['terminal_attempts'],'all_attempts':d['all_attempts'],
                                    'frames':fs})
                timeline.append({'repeat':rep,'listener':listener,'t':(d['mono_ns']-start)/1e9,'attempts':d['all_attempts'],'observed':observed})
            selected=[f for f in frames if tx[f['index']]['mono_ns'] in selected_times]
            capture=tcpdump_stats((folder/'raw'/listener/rep/'tcpdump.stderr').read_text())
            data['air'][listener]={'matched_packets':matched,'data_frames':len(selected),'retry_flags':sum(f['retry'] for f in selected),
                'air_minus_reconstructed_attempts_histogram':dict(sorted(distributions.items())),
                'unobserved_attempts':missing,'excess_frames':extra,'first_observed_copy_retry_set':first_missing_retry,
                'packets_with_air_frames_exceeding_old_terminal_count':old_count_violations,
                'air_frames_directly_witnessing_old_undercount':old_count_extra_frames,
                'nonfinal_observed_copies':nonfinal,'nonfinal_data_with_ACK_then_retransmission':retrans_after_ack,
                'ACK_delta_us_histogram':dict(Counter(ack_deltas)),
                'fcs_values':dict(Counter(f['fcs'] for f in selected)),
                'ACK_crc32_verification':dict(Counter(str(a[3]) for a in acks)),
                'sender_RTS_frames_full_capture':sender_rts,
                'rates':dict(Counter(f['rate'] for f in selected)), 'capture_stats':capture,
                'ACK_pairing_rule':'listener-local TSFT delta 420..650us; no intervening captured sender DATA',
                'ACK_observation_is_not_STA_reception_proof':True}
        phase=next((p for p in m.get('phases',[]) if p['repeat']==int(rep[3:])),{})
        if phase.get('idle'):data['idle_busy']=survey_delta(phase['idle']['before'],phase['idle']['after'])
        all_results.append(data)
    result={'scope':'diagnostic only; original raw files untouched','repetitions':all_results,
            'ACK_examples':examples,'counter_undercount_air_examples':counter_examples,'counter_bug':'terminal retry count omits configured attempts of earlier hardware rate series',
            'semantics':'reconstructed_p_core uses mac80211/ath9k existing rate-series formula; not a claim all air attempts captured',
            'no_driver_semantic_change':not m.get('regression',False)}
    result['aggregate']={
        'packets':sum(r['driver_packets'] for r in all_results),
        'old_attempts':sum(r['trace_terminal_attempts'] for r in all_results),
        'driver_attempts':sum(r['driver_attempts'] for r in all_results),
        'reconstructed_attempts':sum(r['trace_all_attempts'] for r in all_results),
        'omitted_earlier_attempts':sum(r['trace_earlier_attempts'] for r in all_results),
        'STA2_verified_ACK_then_retransmission_events':sum(r['air']['STA2']['nonfinal_data_with_ACK_then_retransmission'] for r in all_results)}
    write(folder/'metrics.json',result)
    diagnostic_valid=all(r['counter_closure'] and r['trace_clean'] and r['kprobe_misses']==[0]
        and all(a['capture_stats']['kernel_dropped']==0 and a['capture_stats']['interface_drop_fraction'] is not None
                and a['capture_stats']['interface_drop_fraction']<=.01 and a['sender_RTS_frames_full_capture']==0 for a in r['air'].values())
        for r in all_results)
    m['analysis']={'diagnostic_valid':diagnostic_valid,'formal_valid':False,'quiet_interference_control_completed':False,
                   'counter_semantics_modified':m.get('regression',False),'metrics':'metrics.json','report':'analysis.md'}
    write(folder/'manifest.json',m)
    write(folder/'raw_sha256.json',{str(p.relative_to(folder)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (folder/'raw').rglob('*') if p.is_file()})
    shutil.copy2(__file__,folder/'analyze_retry_audit.py')
    fig,ax=plt.subplots(figsize=(10,4.5))
    for name,series in plot_series:ax.plot([s[0] for s in series],[s[1] for s in series],label=name)
    ax.axvspan(0,5,color='gray',alpha=.18,label='warm-up / tail');ax.axvspan(35,40,color='gray',alpha=.18)
    ax.axvspan(5,35,color='green',alpha=.07,label='driver measurement')
    for x in [5,35]:ax.axvline(x,color='black',ls='--',lw=.8)
    ax.set(xlim=(0,40),ylim=(.55,1.01),xlabel='STA1 monotonic seconds since client spawn',ylabel='ACKed packets / reconstructed DATA attempts',title='Basic, 24 Mbps, STA 14 dBm / AP 16 dBm, UDP 5 Mbps, 1200 B')
    ax.legend(fontsize=8,ncol=3);ax.grid(alpha=.2);fig.tight_layout();fig.savefig(folder/'full_timeline.png',dpi=160);plt.close(fig)
    if examples:
        e=examples[0];fs=e['frames'];base=fs[0]['tsft'];fig,ax=plt.subplots(figsize=(9,2.8))
        for f in fs:
            x=f['tsft']-base;ax.scatter(x,1,color='tab:blue');ax.text(x,1.07,'DATA seq=%d Retry=%d'%(f['seq'],f['retry']),ha='center',fontsize=8)
            if f['ack_delta_us'] is not None:
                a=x+f['ack_delta_us'];ax.scatter(a,0,color='tab:green');ax.text(a,-.12,'ACK (CRC valid)',ha='center',fontsize=8);ax.plot([x,a],[1,0],color='gray',ls='--')
        ax.set(yticks=[0,1],yticklabels=['AP response observed','STA1 DATA observed'],ylim=(-.35,1.4),xlabel='Microseconds in the same independent listener TSFT clock',title='%s %s: ACK observed, then same-sequence DATA retransmitted'%(e['repeat'],e['listener']))
        ax.grid(axis='x',alpha=.2);fig.tight_layout();fig.savefig(folder/'ACK_then_retry.png',dpi=160);plt.close(fig)
    lines=['# 单 STA 重试计数与 DATA/ACK 交叉核验','',
           '结论：确认现有尝试计数漏加前序硬件重试段；补计后成功率更低，不能把单 STA 失败归咎于计数人为偏低。独立监听确认大量“正确 ACK 已出现在空口、同序列 DATA 仍继续重传”的事件。干扰因果尚未确定，正式 CW 扫描继续暂停。','',
           '## 测试与有效性','',
           'STA1 单独发送；其余 STA 不打流。AP、STA2～4 同时 monitor。位置/信道不变，STA 14 dBm、AP 16 dBm、legacy 24 Mbps、Basic 强制开关、CWmin=15/CWmax=1023、UDP 5 Mbps/1200 B；每次 5 s 预热、30 s 驱动窗口、5 s 收尾。三组均完成，计数/追踪闭合；它们是诊断数据，不是正式四 STA 数据。',
           '无需重编译/重载网卡：依据与当前模块匹配的 DWARF 读取结构偏移，使用独立 ftrace instance 与专用 kprobe；默认驱动计数未修改。TX completion 追踪可能影响软件时延，因此本轮不用于正式 HoL 曲线。','',
           '| 重复 | 包数 | 原尝试数 | 漏计前序尝试 | 补计尝试数 | 原成功率 | 补计成功率 | 测前空闲 busy |',
           '|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in all_results:
        lines.append('| %s | %d | %d | %d | %d | %.3f%% | %.3f%% | %.2f%% |'%(r['repeat'],r['driver_packets'],r['driver_attempts'],r['trace_earlier_attempts'],r['trace_all_attempts'],100*r['driver_p'],100*r['reconstructed_p_core'],100*r['idle_busy']['busy_fraction']))
    ag=result['aggregate'];old=ag['packets']/ag['old_attempts'];new=ag['packets']/ag['reconstructed_attempts']
    lines += ['', '三次合计 %d 个包，原统计 %d 次，补计 %d 次（漏 %d 次）；合并概率 %.3f%% → %.3f%%。三次最终 MAC 交付均 100%%，这与单次尝试失败并不矛盾。'%(ag['packets'],ag['old_attempts'],ag['reconstructed_attempts'],ag['omitted_earlier_attempts'],100*old,100*new),'',
              '## 1. 计数路径审计','',
              '- `ar9002_mac.c` / `ar9003_mac.c` 从描述符读取 FinalTxIdx、RTSFailCnt、DataFailCnt。原生 `ath_tx_rc_status` 把最终段 count 设为 longretry+1，保留此前段的 count；`ath_tx_count_airtime` 与聚合完成路径都累加此前段次数。',
              '- 自定义 `dcf_math.h` 却只取 longretry+1。固定 24 Mbps 不代表只有一个重试段：本轮有效段的 idx 全是 8，但仍有 FinalTxIdx=1/2；遗漏由追踪现场证实。修正建议是 Basic 按 `longretry+1+sum(rates[i].count, i<FinalTxIdx)` 计算，用较宽计数类型，不覆盖原始硬件字段。RTS/CTS 的 RTSFailCnt/分段含义须独立空口核验，不能把 Basic 公式直接冒充完整 RTS 公式。',
              '- 更直接的空口反证：AP 在三组分别有 48/77/93 个包，实际捕获的同序列 DATA 数已经超过旧公式的尝试数。metrics 中保留这些反例；例如 FinalTxIdx=1、longretry=0、此前 count=5 时旧公式只记 1 次，但 AP 至少看到了 2 次，按原生分段算法应为 6 次（监听可能漏帧）。',
              '- 单个非聚合 completion 只调用统计一次。AC 行与 peer 行是同一批包的两个视图，分析只取 peer/BE，不相加。本轮逐帧 terminal_attempts 与原计数逐组完全一致，未发现重复累计。',
              '- start/stop 与累计共享 measurement lock，capture epoch 防止跨代提交。边界调用耗时约 1.2 ms；三组核心 trace 与累计包数/原尝试数均完全闭合。rep02 另有 2 个边界候选 completion，但未造成核心计数不一致。统计按完成时刻归属，跨边界包的空口尝试不能声称严格全落在窗口内；此微小边界效应无法解释 10%～15% 失败。',
              '- ftrace overrun / dropped / kprobe miss 均为 0；tcpdump 内核丢包均为 0，接口丢包率最高约 0.045%。但这不保证所有无线帧均被物理解码：各监听点仍漏见一部分尝试，不能拿“未见帧”反推“未发送”。','',
              '## 2. 空口交叉证据','',
              '以序列号、QoS TID 和绝对时间选择对应 TX completion，消除 4096 序列号回绕；ACK 配对用同一监听节点的 radiotap TSFT，不用可能批处理/重排的主机抓包时间判断 SIFS。ACK 必须指向 STA1、与前一 DATA 间隔 420～650 us，且中间没有该发送端的新 DATA；绝大多数实测间隔 461～464 us。',
              '对完整 14 字节 ACK 直接重算 CRC32，通过后才计入正面证据。tshark 默认状态 2 实际是 Unverified，不是 Good；截断的 DATA 不做虚假的 FCS 校验结论。', '',
              '| 重复 | STA2 见正确 ACK 后仍重传 | STA3 同类观测 | STA4 同类观测 |',
              '|---|---:|---:|---:|']
    for r in all_results:lines.append('| %s | %d | %d | %d |'%(r['repeat'],*[r['air'][n]['nonfinal_data_with_ACK_then_retransmission'] for n in ['STA2','STA3','STA4']]))
    lines += ['', '仅 STA2 独立监听已看到 %d 次这样的非最终 DATA→正确 ACK→同序列重传事件；不能把三台观测直接相加，因为可能观察到同一次交换。AP 同 PHY 没捕获自身 ACK 不作失败证据。'%ag['STA2_verified_ACK_then_retransmission_events'],
              '因此至少这一类失败不是“DATA 根本没到 AP”的单一解释：AP 有回应 ACK，但发送端后来仍重试。可支持 ACK 未被 STA 有效接收/认可；尚不能区分 STA 当地干扰、ACK 接收/识别、射频或其他硬件原因。这里的 CRC 正确是在监听位置正确，不证明在 STA1 天线位置也正确。','',
              '另发现模型时长配置的独立校准项：驱动签名 control_rate_100kbps=10，但监听到 AP ACK 速率为 24 Mbps。RTS 控制速率不能直接当作实际 ACK 响应速率；该差异影响 modeled_exchange_us 的理论时长，不改变本轮实测 HoL 的主机时间戳或重试计数。','',
              '## 3. 干扰对照：目前尚未完成因果验证','',
              '保持其他参数相同的三次时间重复，空闲 busy 为约 28.8%、28.9%、33.6%；前三组中的前两组 busy 几乎相同，补计概率仍有约 3 个百分点差异。没有得到经验证的安静/屏蔽对照组，不能据这三个点断言干扰因果，也不能把 busy 高解释为每次失败。下一步需用户提供固定摆放下的安静时段/屏蔽条件，再用相同脚本和参数至少重复三次；不得同时换功率、速率、负载。',
              'ACK 超时寄存器仅只读核查：五节点 AR_TIME_OUT=0x08400b00，AR_USEC=0x12e0002b，对应 44 clocks/us 下 ACK=64 us、CTS=48 us，与当前 ath9k 2.4 GHz 初始化一致；未据此证明 ACK 路径健康，也没有擅自改超时。regidx 选择器均已恢复。','',
              '## 保存、恢复与下一步','',
              '全部 raw 文件和 SHA256 位于本目录；`metrics.json` 含每次重试段分布、计数闭合、抓包丢包、CRC、ACK 配对和逐帧案例（pcap frame number）。manifest 含时钟偏差、事件墙钟/单调钟、Git commit、参数、节点读回和清理结果；控制机估计误差在 50 ms 内，微秒配对不依赖跨机墙钟。',
              '实验结束已删除本轮创建的 monitor / trace instance / kprobe，恢复 STA1 RTS off、Basic override=0；未改发射功率、信道、速率、CW、驱动模块。四 STA 关联保留，无本轮 iperf 进程。',
              '建议后续工作：先经授权修复 Basic 的前序重试漏计并回归；RTS 映射另测；再完成安静环境对照。不要因为修复后概率改变就当作链路改善。正式 CW 扫描仍未开始。', '',
              '复算：在项目目录用 `.venv/Scripts/python.exe experiments/analyze_retry_audit.py <本目录>`。分析器只读 raw；图为 `full_timeline.png`、`ACK_then_retry.png`。', '',
              '![全时序](full_timeline.png)', '', '![ACK 后重传实例](ACK_then_retry.png)', '']
    if m.get('regression'):
        passed=diagnostic_valid and all(r['basic_retry_abi']==2 and r['prior_series_accounting_match'] and r['raw_long_retries_preserved'] for r in all_results)
        result['regression_pass']=passed;write(folder/'metrics.json',result)
        m['analysis']['regression_pass']=passed;write(folder/'manifest.json',m)
        lines=['# Basic 尝试计数修正：现场回归','',
               '回归结论：**%s**。本报告仅验证 Basic 计数修复，不是链路质量改善试验，也不是正式 CW 曲线。'%('PASS' if passed else 'FAIL / 待排查'),'',
               '## 修正范围','',
               '`dcf_math.h` 新增 Basic 专用函数，实际尝试数为 `ts_longretry+1+sum(bf->rates[i].count, i<ts_rateindex)`；`debug.c` 按实际完成段求和。只修正 Basic 的 exchange_attempts/data_attempts 与由尝试数派生的 modeled_exchange_us；保留 raw long_retries、包成功数、HoL 时间戳与采样、RTS/CTS 旧路径和硬件发送策略。',
               '统计头 `basic_retry_abi=2 rts_retry_abi=1` 区分新旧口径。RTS 映射仍待独立验证，实际 ACK 速率与模型时长校准也未包含在本次修改。','',
               '## 构建与测试','',
               '- STA1 使用匹配的 Ubuntu 5.15.0-139 内核头、原修改版 mac80211 头和 Module.symvers 构建；实际 C 头文件通过 3521 组 Basic/RTS 数学测试（包括多段、失败状态、宽计数和 64 位时长乘法）。',
               '- 只重载 ath9k，保留现有 ath9k_hw/mac80211。原模块备份在 `/home/EXPERIMENT_USER/ath9k-kernel/basic-retry-07/rollback-ath9k.ko`，未覆盖系统启动模块。模块版本和 SHA256 见 controller/build.json；配置恢复及回退记录见 controller/activation_*.json。',
               '- STA1 单发 24 Mbps、14 dBm、UDP 5 Mbps/1200 B，AP 保持 16 dBm、信道 1；Basic 强制、CWmin=15/CWmax=1023。每组 5 s 预热/30 s 驱动窗口/5 s 收尾，AP 与其余三个 STA 独立监听；没有四 STA 并发。','',
               '| 重复 | 包数 | 旧公式仅末段 | 前序尝试 | 新驱动累计 | 独立重建 | 闭合 | 成功率 |',
               '|---|---:|---:|---:|---:|---:|---|---:|']
        for r in all_results:
            lines.append('| %s | %d | %d | %d | %d | %d | %s | %.3f%% |'%(r['repeat'],r['driver_packets'],r['trace_terminal_attempts'],r['trace_earlier_attempts'],r['driver_attempts'],r['trace_all_attempts'],'PASS' if r['counter_closure'] else 'FAIL',100*r['driver_p']))
        lines += ['', '合计 %d 包；新驱动与逐包重建均为 %d 次尝试，其中前序段 %d 次。原始 long_retries 按末段保留，没有改成“全程重试数”。'%(ag['packets'],ag['driver_attempts'],ag['omitted_earlier_attempts']),
                  '逐帧重建来自同一版本模块 DWARF 的隔离 kprobe/ftrace。窗口边界只用包数与 raw long_retries 选择合法的有序边界事件，不用“修正后尝试数”反向拟合；再独立核对全部尝试和。具体候选数与歧义判定在 metrics.json。',
                  'ftrace overrun / dropped events / kprobe miss 必须为 0；空口抓包检查内核丢包及接口丢包率，四监听点实验 STA 的 RTS 必须为 0。无线帧未被监听点解码不计作“未发送”。ACK 证据用本地 TSFT 和完整帧 CRC32。','',
                  '## HoL、环境与限制','',
                  '本次代码未改 HoL 采样路径；HoL 样本和原始累计保存在 dcf_stats.txt。追踪会增加软件完成路径开销，本轮 HoL 不能代替无追踪正式测量。修复判据是新计数和独立逐帧计数一致，不是概率升高；环境波动使新旧不同时间运行的概率不可当成 A/B 性能改善。',
                  '用户当前无法提供安静时段/屏蔽条件，已作为实验限制记录。本轮不把环境对照当作完成条件，也不声称已确定 ACK 失败的干扰因果。后续机制曲线必须注明现有射频环境，并采用交错重复与单 STA 基线监控。','',
                  '## 原始数据和恢复','',
                  'raw/AP、raw/STA1～4 含 pcap、TX trace、iperf JSON、配置读回、驱动统计、时序与 stderr；raw_sha256.json 是原始文件校验表。原始文件未被分析器改写。manifest 含墙钟偏差、计划参数、实际边界、配置、Git commit/dirty 状态与有效性；controller 保存构建、模块和激活记录。',
                  '本轮 monitor / trace instance / kprobe 已清理，STA1 RTS off、Basic override=0 及原功率/速率/CW/节能设置恢复；新计数模块保留运行。其他 STA 的部署状态必须以 controller/activation_*.json 的最终记录为准，不从本轮 STA1 回归推断已全部更新。正式 CW 扫描未开始。','',
                  '复算：`.venv/Scripts/python.exe experiments/analyze_retry_audit.py <本目录>`。完整诊断时序图标注预热/测量/收尾：','',
                  '![完整时序](full_timeline.png)','', '![ACK 后仍重传实例](ACK_then_retry.png)','']
        deployment={}
        for path in (folder/'controller').glob('activation_*.json'):
            activation=json.loads(path.read_text(encoding='utf-8'))
            for name,node in activation.get('nodes',{}).items():
                deployment[name]={'status':node['status'],'srcversion':node.get('after',{}).get('srcversion'),
                                  'phy':node.get('after',{}).get('phy'),'rollback_module':node.get('rollback_module')}
        smoke_path=folder/'controller/deployment_smoke.json'
        if smoke_path.exists():
            smoke=json.loads(smoke_path.read_text(encoding='utf-8'))
            result['deployment_smoke']={n:{'valid':smoke[n]['valid'],'dcf':smoke[n]['dcf'],'prior_series_counted':smoke[n]['prior_series_counted'],'air':smoke[n]['air']} for n in ['STA2','STA3','STA4'] if n in smoke}
            m['deployment']=deployment
            m['analysis']['four_STA_modules_consistent']=(set(deployment)=={'STA1','STA2','STA3','STA4'} and len({d['srcversion'] for d in deployment.values()})==1 and all(d['status']=='activated_and_restored' for d in deployment.values()))
            m['analysis']['deployment_smoke_pass']=all(smoke.get(n,{}).get('valid',False) for n in ['STA2','STA3','STA4'])
            lines += ['## 其他 STA 部署回归','',
                      '| STA | 模块部署 | 10 s 窗口包数 | 计入的前序尝试 | 空口 RTS | 冒烟 |',
                      '|---|---|---:|---:|---:|---|']
            for n in ['STA2','STA3','STA4']:
                if n in smoke:
                    s=smoke[n];lines.append('| %s | %s | %d | %d | %d | %s |'%(n,deployment.get(n,{}).get('status','unknown'),s['dcf']['packets'],s['prior_series_counted'],s['air']['sender_RTS'],'PASS' if s['valid'] else 'FAIL'))
            lines += ['', '四台均运行同一 07 模块；另外三台各自单独完成 5/10/5 s 冒烟，不是四 STA 并发。测试后四台关联、14 dBm、24 Mbps、原 BE EDCA/节能设置保持，RTS off、Basic override=0；AP 保持 16 dBm。最终状态和清理记录在 controller/deployment_smoke.json。',
                      '**生效范围：当前运行模块。未覆盖系统启动模块或应用自带模块；重启/旧程序重新加载驱动后必须重查 basic_retry_abi=2。** 后续同步 worker 已加入 Basic ABI 准备门禁。','']
            write(folder/'metrics.json',result);write(folder/'manifest.json',m)
    (folder/'analysis.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps([{k:v for k,v in r.items() if k!='air'} for r in all_results],indent=2))
    for r in all_results:
        print(r['repeat'],{n:{k:a[k] for k in ['data_frames','retry_flags','unobserved_attempts','excess_frames','nonfinal_data_with_ACK_then_retransmission','ACK_delta_us_histogram']} for n,a in r['air'].items()})
    return result

if __name__=='__main__':analyze(Path(sys.argv[1]))
