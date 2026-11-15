"""Read explicit live pilot/learning inputs; write a new report directory only."""
import argparse
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
import sys

from .rtt import replay_events


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--batch',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();root=args.batch;out=args.output;out.mkdir(parents=True,exist_ok=False)
    source=root/'pilot_01/result.json';s=json.loads(source.read_text(encoding='utf-8'))
    learning=json.loads((root/'learning_01/manifest.json').read_text(encoding='utf-8'))
    raw=root/'pilot_01'/s['id']/'raw'
    report=json.loads((raw/'STA1/application_final.json').read_text(encoding='utf-8'))['report']
    collector=replay_events(raw/'STA1/application_probe.jsonl');anchor=report['window']['start_ns'];asof=report['window']['as_of_ns']
    common=collector.report(anchor+5*10**9,anchor+35*10**9,asof);common.pop('ledger')
    repo=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(repo/'experiments'))
    from analyze_sync import pcap_udp
    from labconsole.demo_analysis import envelope
    offset,uncertainty=envelope(s['clock_checks'],'AP')
    packets=pcap_udp(raw/'AP/ap_udp.pcap',{5201,5202,5203,5204})
    segments=[]
    for i,(a,b) in enumerate(((32,32),(16,64),(64,16))):
        start=5+i*10;end=start+10
        r=collector.report(anchor+start*10**9,anchor+end*10**9,asof);r.pop('ledger')
        rates={f'STA{j+1}':sum(size*8/1e6/10 for stamp,size in packets.get(5201+j,[])
               if s['T0_ns']+start*10**9<=stamp-offset<s['T0_ns']+end*10**9 and size==1200) for j in range(4)}
        segments.append({'index':i,'seconds':[start,end],'foreground_W':a,'background_W':b,
                         'application':r,'per_STA_Mbps':rates,'aggregate_Mbps':sum(rates.values())})
    metrics={'scope':'one 40s acceptance run; 30s common measurement; no independent repeats',
        'formal_valid':0,'application_common_window':common,'segments':segments,
        'AP_clock_envelope_uncertainty_ms':uncertainty/1e6,'pilot_passed':s['acceptance']['passed'],
        'restoration_confirmed':s['restoration']['confirmed'],'training_status':learning['status'],
        'training_windows_started':sum(x['phase']=='training' for x in learning['completed']),
        'test_windows_started':sum(x['phase']=='test' for x in learning['completed']),
        'training_error':learning['error'],'planned_windows':len(learning['plan'])}
    (out/'metrics.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2),encoding='utf-8')
    t0=datetime.fromtimestamp(s['T0_ns']/1e9,timezone(timedelta(hours=8))).isoformat(timespec='milliseconds')
    lines=['# 实机接入结果与停止说明','',
        '已完成一轮RTT/超时与两组CW的小规模实机验收；随后有界训练在准备阶段因首检时钟门禁停止，未启动训练或对照业务。','',
        f'共同T0：{t0}。5秒预热、30秒测量、5秒收尾；首个共同窗口不是正式五重复标定。',
        'STA1每10ms计划产生1200B请求，AP返回42B应用确认；STA2～4各30Mbps背景。Basic/24Mbps/K3/AIFSN3/FIFO20，功率STA14dBm/AP16dBm。',
        'RTT是同一发送端单调时钟测得的提交到应用确认时间；超时以计划生成到确认100ms判断，不是MAC ACK成功率。','',
        '| 测量段 | STA1 W | 背景W | AP聚合吞吐 Mbps | 100ms内完成比例 | 本地失败 | 条件RTT均值 ms |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for row in segments:
        r=row['application'];mean=r['rtt_mean_ms']
        lines.append(f"| {row['seconds'][0]}～{row['seconds'][1]} s | {row['foreground_W']} | {row['background_W']} | {row['aggregate_Mbps']:.3f} | {r['on_time_ratio']:.2%} | {r['counts']['local_failure']} | {mean:.3f} |" if mean is not None else f"| {row['index']} | {row['foreground_W']} | {row['background_W']} | {row['aggregate_Mbps']:.3f} | {r['on_time_ratio']:.2%} | {r['counts']['local_failure']} | NA |")
    lines+=['',f"公共30秒生成{common['counts']['generated']}条，成熟{common['counts']['mature']}条，按时{common['counts']['on_time']}、迟到{common['counts']['late']}、未回复{common['counts']['no_reply']}、本地失败{common['counts']['local_failure']}。",
        '本地失败中的generator_missed_schedule属于主机发生器调度未赶上计划，不归因无线丢失。所有消息保留在分母。',
        '迟到与未回复的分类以最终回收时刻为准；100ms按时比例在截止成熟后不再因迟到而提高。',
        '三段是同一轮的短顺序诊断，不是独立重复，不能据此认定最优CW或算法增益；不报告虚构置信区间。','',
        '## 验收与恢复',
        '四STA硬件CW读回、Basic/24Mbps监听旁证、FIFO/追踪/队列和进程验收通过。CW边界最大偏差约13.14ms，持续时钟包络最大14.16ms；AP实时采样内核丢包为0。',
        '监听接口3包丢失，占接收计数约0.011%，低于原1%门限；内核抓包丢失0。四STA各四个probe计数均零miss。',
        '节点完成回执确认原CW/AIFS、override、接入模式、MON15关联及功率恢复。临时时间覆盖和中继已清理；独立postflight核对结果位于批次postflight/summary.json。','',
        '## 训练停止',
        '封存计划为5个训练窗口（60个决策段，最多55条转移）及固定/UCB/DQN各5个测试窗口。训练纯业务预算17.5分钟。',
        '准备首检STA1 offset=11.490596ms，uncertainty=0.832700ms，包络12.323296ms，超过首检10ms要求。授时源正确；不能以运行期50ms门限替代更严格的首检。',
        '本次completed清单为空：训练0、对照0，无实机训练模型，不报告学习效果。20个后续计划窗口均未启动，不是20个invalid业务窗口。',
        '准备状态failed_clean，六节点原时间配置和服务清理已确认。没有放宽门限、自动补轮或进入长训练。','',
        '## 软件与证据边界',
        '原始probe事件、全部逐消息账本及整数计数回放完全一致。最初严格浮点比较因约1e-13ms尾数差异失败，保留原记录；独立数值核对说明原Python浮点求和与整数ns求和的差异。后续代码先求整数ns再换算，不追改原报告。',
        '新增独立CLI适配复用旧执行器，原网页功能未替换。没有重启、重载模块、改功率、改重试或覆盖持久模块。','',
        '下一步需先处理短时授时收敛：可以单独提出有界等待/复核方案，或明确区分演示与正式训练的时间要求；本次不自动重试。算法训练和测试须待准备通过后恢复，不能把本轮验收当作训练完成。']
    (out/'analysis.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    inputs=[source,root/'learning_01/manifest.json',raw/'STA1/application_probe.jsonl',raw/'STA1/application_final.json',raw/'AP/ap_udp.pcap']
    (out/'inputs.json').write_text(json.dumps([{'path':str(p.relative_to(root)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in inputs],ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'output':str(out.resolve()),'pilot':metrics['pilot_passed'],'training':metrics['training_status'],'measurement_counts':common['counts']},ensure_ascii=False))


if __name__=='__main__':main()
