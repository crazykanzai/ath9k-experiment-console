"""Offline-only commands. No node or socket execution modes are exposed."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import statistics
import sys

from . import VERSION
from .rtt import ProbeCollector, echo_reply, replay_events
from .spec import ACTIONS, FEATURES, RewardConfig, reward


def write(path, value):
    with path.open('x',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)


def comparison_svg(results):
    """All held-out episode points, no invented or hardware performance data."""
    colors={'fixed':'#64748b','ucb':'#b36b23','dqn':'#2563a0'}
    parts=['<svg xmlns="http://www.w3.org/2000/svg" width="900" height="470" viewBox="0 0 900 470">',
           '<rect width="900" height="470" fill="white"/>',
           '<text x="35" y="32" font-family="sans-serif" font-size="19">Synthetic software check - NOT hardware results</text>']
    panels=[('mean_reward','Mean reward',lambda r:r['mean_reward']),
            ('mean_timeout_ratio','Timeout ratio',lambda r:r['timeout_ratio'])]
    for panel,(key,label,fn) in enumerate(panels):
        left=65+panel*440;top=80;width=345;height=270
        values=[fn(r) for d in results.values() for r in d['runs']]
        lo=min(0,min(values));hi=max(values);hi=hi+.12*max(.1,hi-lo)
        def y(v):return top+height-(v-lo)/max(.01,hi-lo)*height
        parts.append(f'<text x="{left}" y="64" font-family="sans-serif" font-size="16">{label}</text>')
        for i in range(5):
            v=lo+(hi-lo)*i/4;yy=y(v)
            parts.append(f'<path d="M{left},{yy}h{width}" stroke="#dce1e7"/>')
            parts.append(f'<text x="{left-8}" y="{yy+4}" text-anchor="end" font-size="12">{v:.2f}</text>')
        for i,(name,data) in enumerate(results.items()):
            x=left+60+i*110;mean=data[key]
            for j,r in enumerate(data['runs']):
                parts.append(f'<circle cx="{x+(j-2)*6}" cy="{y(fn(r))}" r="4" fill="{colors[name]}"/>')
            parts.append(f'<path d="M{x-22},{y(mean)}h44" stroke="{colors[name]}" stroke-width="3"/>')
            parts.append(f'<text x="{x}" y="380" text-anchor="middle" font-family="sans-serif" font-size="15">{name}</text>')
    parts.append('<text x="35" y="427" font-family="sans-serif" font-size="14">Dots: five held-out synthetic episodes. Bars: means. All exploration cost retained.</text>')
    parts.append('</svg>');return '\n'.join(parts)


def trial(policy, seed, config, training=False, length=30):
    from .synthetic import SyntheticQoS
    env=SyntheticQoS(seed,length);state=env.reset();previous=4;rows=[]
    for _ in range(length):
        action=policy.choose(state,training=training)
        following,terminal=env.step(action); score=reward(following,action!=previous,config)
        if training or policy.__class__.__name__=='UCBPolicy':
            policy.observe(state,action,score,following,terminal)
        rows.append({'tick':env.tick,'action_index':action,'action':asdict(ACTIONS[action]),
                     'state':dict(state),'reward':score,'next_state':dict(following),
                     'terminal':terminal,'switched':action!=previous,'observation':following})
        state=following;previous=action
    return {'seed':seed,'mean_reward':statistics.mean(x['reward'] for x in rows),
            'background_Mbps':statistics.mean(x['observation']['background_Mbps'] for x in rows),
            'timeout_ratio':statistics.mean(x['observation']['timeout_ratio'] for x in rows),
            'switches':sum(x['action_index']!=(rows[i-1]['action_index'] if i else 4) for i,x in enumerate(rows)),
            'rows':rows}


def offline(args):
    import torch
    from .policies import DQNPolicy,FixedPolicy,UCBPolicy
    torch.set_num_threads(1)
    if any(args.seed<=s<args.seed+(args.steps+29)//30 for s in list(range(7000,7005))+list(range(9000,9005))):
        raise ValueError('training seeds overlap frozen development/evaluation seeds; choose another seed')
    out=Path(args.output);out.mkdir(parents=True,exist_ok=False)
    config=RewardConfig();manifest={'version':VERSION,'source':'synthetic_toy_not_wifi_model',
        'formal_valid':0,'hardware_validated':False,'node_connections':0,'traffic_started':False,
        'seed':args.seed,'train_steps':args.steps,'evaluation_seeds':list(range(9000,9005)),
        'development_seeds':list(range(7000,7005)),
        'config':asdict(config),'features':list(FEATURES),'actions':[asdict(a) for a in ACTIONS],
        'training_hyperparameters':{'hidden':[32,32],'gamma':.9,'learning_rate':.001,'batch_size':32,
            'replay_capacity':5000,'target_every':50,'epsilon':'max(0.05,0.8*0.995**steps)'},
        'protocol':'all candidates same; fixed chosen on development seeds; test DQN frozen; UCB online',
        'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')},
        'status':'running'}
    write(out/'manifest.json',manifest)
    dqn=DQNPolicy(seed=args.seed,config=config);trained=0
    with (out/'training.jsonl').open('x',encoding='utf-8') as stream:
        episode=0
        while trained<args.steps:
            record=trial(dqn,args.seed+episode,config,True,min(30,args.steps-trained))
            for row in record['rows']: stream.write(json.dumps({'episode':episode,**row})+'\n')
            trained+=len(record['rows']);episode+=1
    dqn.save(out/'dqn.json',{'source':manifest['source'],'training_file':'training.jsonl','seed':args.seed})
    frozen=DQNPolicy.load(out/'dqn.json',config)
    development=[]
    for action in range(len(ACTIONS)):
        scores=[trial(FixedPolicy(action),s,config)['mean_reward'] for s in manifest['development_seeds']]
        development.append({'action_index':action,'mean_reward':statistics.mean(scores)})
    best=max(development,key=lambda x:x['mean_reward'])['action_index']
    results={}
    for name,factory in [('fixed',lambda:FixedPolicy(best)),('ucb',UCBPolicy),('dqn',lambda:frozen)]:
        runs=[trial(factory(),seed,config) for seed in manifest['evaluation_seeds']]
        results[name]={'runs':runs,'n':len(runs),'mean_reward':statistics.mean(r['mean_reward'] for r in runs),
            'mean_background_Mbps':statistics.mean(r['background_Mbps'] for r in runs),
            'mean_timeout_ratio':statistics.mean(r['timeout_ratio'] for r in runs)}
    write(out/'development.json',{'candidates':development,'selected_action':best})
    write(out/'metrics.json',{'source':manifest['source'],'formal_valid':0,'results':results})
    (out/'comparison.svg').write_text(comparison_svg(results),encoding='utf-8')
    # Collector acceptance sample contains an on-time reply, late reply, loss,
    # local failure, never-submitted message and an immature generation.
    c=ProbeCollector('00000000-0000-4000-8000-000000000001',1,100_000_000)
    packets={i:c.generate(i,0) for i in range(5)}
    for i in range(3):c.submitted(i,1_000_000)
    c.send_failed(3,1_000_000,'synthetic EAGAIN')
    c.receive(echo_reply(packets[0]),20_000_000)
    c.receive(echo_reply(packets[1]),120_000_000)
    c.generate(5,150_000_000)
    c.export_events(out/'probe_events.jsonl')
    report=c.report(0,160_000_000,160_000_000)
    assert replay_events(out/'probe_events.jsonl').report(0,160_000_000,160_000_000)==report
    write(out/'probe_metrics.json',{'source':'synthetic_in_memory_no_sockets',**report})
    lines=['# 离线验证结果','','所有业务与链路数值来自人工合成环境。没有连接节点、发送网络流量或修改控制台。',
           '该环境用于验证学习/采集/对照链路，不是 ath9k 模型，不用于预测硬件收益。','',
           '| 策略 | 独立模拟 episode 数 | 平均奖励 | 背景吞吐（合成 Mbps） | 超时比例（合成） |',
           '|---|---:|---:|---:|---:|']
    for name,data in results.items():
        lines.append(f"| {name} | {data['n']} | {data['mean_reward']:.4f} | {data['mean_background_Mbps']:.3f} | {data['mean_timeout_ratio']:.3%} |")
    lines+=['','固定策略在独立开发种子中选定；DQN 测试冻结、不探索；UCB 按在线算法更新，探索成本全部计入。',
            '五个模拟种子不是五个实机正式重复，不对这些数值声称硬件显著性。',
            '探针账本回放一致；成熟分母包含迟到、未回复、本地失败和未提交，未成熟消息单列。',
            'DQN JSON 仅用于冻结推理，未保存优化器/经验回放，不支持无损续训。',
            '硬件接口、业务生成器/echo 进程、实际分段采集、实机训练与网页接入尚未验收。']
    (out/'analysis.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    write(out/'completion.json',{'status':'completed','formal_valid':0,'node_connections':0,
                               'updates':dqn.updates,'trained_steps':trained,'probe_replay_equal':True})
    print(json.dumps({'output':str(out.resolve()),'source':manifest['source'],'updates':dqn.updates,
                      'mean_reward':{k:v['mean_reward'] for k,v in results.items()}},ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser(description='ReinWiFi-inspired offline adaptation; no hardware modes')
    sub=parser.add_subparsers(dest='command',required=True)
    run=sub.add_parser('offline',help='in-memory toy training and held-out comparison, no sockets')
    run.add_argument('--output',required=True);run.add_argument('--steps',type=int,default=1200)
    run.add_argument('--seed',type=int,default=7)
    replay=sub.add_parser('rtt-replay',help='read exactly one explicit probe archive')
    replay.add_argument('--input',required=True);replay.add_argument('--output',required=True)
    replay.add_argument('--start-ns',type=int,required=True);replay.add_argument('--end-ns',type=int,required=True)
    replay.add_argument('--as-of-ns',type=int,required=True)
    args=parser.parse_args()
    if args.command=='offline':
        if not 32<=args.steps<=100000: parser.error('steps must be 32..100000')
        offline(args)
    else:
        c=replay_events(args.input)
        write(Path(args.output),c.report(args.start_ns,args.end_ns,args.as_of_ns))


if __name__=='__main__':main()
