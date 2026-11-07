"""Explicit opt-in SSH observer CLI. Never called by the console automatically."""
import argparse
import getpass
import json
import shlex
import threading
import time
from pathlib import Path
from .config import load_json, validate_platform
from .live import SCHEMA


def clock_probe(client):
    values=[]
    for _ in range(3):
        a=time.time_ns()
        _,out,err=client.exec_command("python3 -c 'import time; print(time.time_ns())'",timeout=5)
        raw=out.read().decode().strip()
        b=time.time_ns()
        if out.channel.recv_exit_status()!=0:
            raise RuntimeError('clock probe failed: '+err.read().decode()[:100])
        remote=int(raw)
        values.append((b-a,remote-(a+b)//2))
    rtt,offset=min(values)
    return offset,rtt/2/1e6


def main():
    parser=argparse.ArgumentParser(description='只读SSH观察；不启动/停止业务、不改CW、不清零驱动。需单独现场授权后使用。')
    parser.add_argument('--platform',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True,help='新的.jsonl文件，禁止覆盖')
    parser.add_argument('--known-hosts',type=Path,help='已核验的SSH主机公钥文件；不自动接受陌生主机')
    parser.add_argument('--interval',type=float,default=1)
    parser.add_argument('--duration',type=int,default=1800)
    parser.add_argument('--sudo',action='store_true',help='内存中sudo运行只读探针，需要交互输入密码')
    args=parser.parse_args()
    if not 1<=args.interval<=10 or not 1<=args.duration<=3600 or args.output.suffix!='.jsonl':
        parser.error('interval须1～10秒，duration须1～3600秒，output须.jsonl')
    import paramiko  # optional, offline console itself uses standard library only
    platform=validate_platform(load_json(args.platform))
    nodes=[n for n in platform['nodes'] if n['role']!='MON15']
    args.output.parent.mkdir(parents=True,exist_ok=True)
    # Passwords stay in memory and are never written into platform or telemetry.
    secrets={}
    for n in nodes:
        ref=n['credential_ref']
        if ref not in secrets:
            secrets[ref]=getpass.getpass(f'SSH密码（{ref}；直接回车使用SSH密钥/agent）：')
    sudo_secret=getpass.getpass('只读探针sudo密码（五节点需一致；不同则分别运行适配）：') if args.sudo else None
    code=(Path(__file__).parent/'readonly_probe.py').read_text(encoding='utf-8')
    clients=[];threads=[];stop=threading.Event();lock=threading.Lock();mac_roles={};errors=[]
    with args.output.open('x',encoding='utf-8',buffering=1) as output:
        def worker(node):
            client=paramiko.SSHClient();client.load_system_host_keys()
            if args.known_hosts:client.load_host_keys(str(args.known_hosts))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            seq=0
            try:
                client.connect(node['address'],port=node['port'],username=node['username'],password=secrets[node['credential_ref']] or None,timeout=5,auth_timeout=5,banner_timeout=5)
                with lock:clients.append(client)
                offset,uncertainty=clock_probe(client)
                if abs(offset)/1e6+uncertainty>50:
                    raise RuntimeError('clock offset + uncertainty exceeds50ms; no time correction performed')
                cmd='python3 -u -c '+shlex.quote(code)+' '+shlex.quote(node['role'])+' '+shlex.quote(node.get('interface_hint',''))+' '+str(args.interval)+' '+str(args.duration)
                if args.sudo:cmd='sudo -S -p "" '+cmd
                stdin,stdout,stderr=client.exec_command(cmd,timeout=8)
                if args.sudo:stdin.write(sudo_secret+'\n');stdin.flush()
                stdout.channel.settimeout(8)
                nextclock=time.monotonic()+10
                for line in stdout:
                    if stop.is_set():break
                    record=json.loads(line)
                    if time.monotonic()>=nextclock:
                        offset,uncertainty=clock_probe(client);nextclock=time.monotonic()+10
                        if abs(offset)/1e6+uncertainty>50:raise RuntimeError('runtime clock gate; stopped observing, traffic untouched')
                    data=record['data']
                    with lock:
                        if node['role']!='AP':mac_roles[data['mac']]=node['role']
                        else:data['rx_bytes']={mac_roles[m]:v for m,v in data.get('stations_rx_bytes',{}).items() if m in mac_roles}
                        seq+=1
                        item={'schema':SCHEMA,'kind':'sample','role':node['role'],'seq':seq,
                            'sample_ns':record['wall_ns']-offset,'clock_uncertainty_ms':abs(offset)/1e6+uncertainty+record['read_span_ms'],
                            'source':'SSH只读：'+('AP iw station dump字节增量（非iperf应用吞吐）' if node['role']=='AP' else 'STA dcf_stats累计差分、共同锁BE硬件读回'),
                            'data':data,'clock_offset_ns':offset}
                        output.write(json.dumps(item,ensure_ascii=False)+'\n')
                if not stop.is_set() and stdout.channel.recv_exit_status()!=0:
                    raise RuntimeError(stderr.read().decode(errors='replace')[:300])
            except Exception as exc:
                with lock:errors.append(node['role']+': '+str(exc))
                stop.set()
            finally:client.close()
        try:
            for n in nodes:
                t=threading.Thread(target=worker,args=(n,),daemon=True);t.start();threads.append(t)
            while any(t.is_alive() for t in threads) and not stop.wait(.2):pass
        except KeyboardInterrupt:stop.set()
        finally:
            stop.set()
            for c in clients:c.close()
            for t in threads:t.join(5)
    print('观察结束；没有停止业务或改变参数。输出：'+str(args.output))
    if errors:raise SystemExit('\n'.join(errors))


if __name__=='__main__':main()
