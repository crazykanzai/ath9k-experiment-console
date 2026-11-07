"""Explicit synthetic local test writer, never experiment measurements."""
import argparse
import json
import math
import time
from pathlib import Path
from .live import SCHEMA


def main():
    p=argparse.ArgumentParser(description='本机实时观察演练：合成测试数据，不连接节点，不启动iperf。')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--seconds',type=int,default=60)
    args=p.parse_args()
    if not 1<=args.seconds<=300:p.error('seconds须1～300')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    counts={n:{'bytes':0,'hol':0,'samples':0,'attempts':0,'successes':0} for n in ['STA1','STA2','STA3','STA4']}
    print('离线测试数据，非测量证据。文件：'+str(args.output),flush=True)
    with args.output.open('x',encoding='utf-8',buffering=1) as f:
        def send(record):f.write(json.dumps(record,ensure_ascii=False)+'\n')
        for seq in range(1,args.seconds+1):
            phase=(seq-1)//10%3;cw=[7,31,127][phase];stamp=time.time_ns()
            for i,(n,c) in enumerate(counts.items()):
                c['bytes']+=int((2.8+math.sin(seq/4+i)*.2)*125000)
                c['samples']+=100;c['hol']+=int((800+phase*300+40*math.sin(seq/3+i))*100)
                c['attempts']+=120-phase*5;c['successes']+=100
                data={'cw':{'cwmin':cw,'cwmax':8*(cw+1)-1,'aifsn':3,'basis':'hardware_readback'},
                    'dcf':{'generation':1,'hol_samples':c['samples'],'hol_sum_us':c['hol'],
                           'attempts':c['attempts'],'successes':c['successes'],'packets':c['samples'],
                           'failures':0,'mode':'basic','running':True,'basic_retry_abi':2}}
                send({'schema':SCHEMA,'kind':'sample','role':n,'seq':seq,'sample_ns':stamp,
                      'clock_uncertainty_ms':1,'source':'SYNTHETIC_OFFLINE_TEST_NOT_MEASURED','data':data})
            send({'schema':SCHEMA,'kind':'sample','role':'AP','seq':seq,'sample_ns':stamp,
                  'clock_uncertainty_ms':1,'source':'SYNTHETIC_OFFLINE_TEST_NOT_MEASURED',
                  'data':{'rx_bytes':{n:c['bytes'] for n,c in counts.items()}}})
            time.sleep(1)


if __name__=='__main__':main()
