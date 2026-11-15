"""Explicit opt-in no-business hardware CW pilot. Not called by offline UI."""
import argparse
import concurrent.futures
import getpass
import json
from pathlib import Path
import re
import shlex
import time
from .config import load_json,validate_platform
from .storage import atomic_json


def ap_mac(text):
    blocks=re.split(r'(?m)^\s*Interface ', text)[1:]
    values=[]
    for block in blocks:
        if re.search(r'(?m)^\s*type AP\s*$', block):
            m=re.search(r'(?m)^\s*addr ([0-9a-f:]{17})\s*$',block)
            if m:values.append(m.group(1))
    if len(values)!=1:raise RuntimeError('AP role/MAC discovery missing or ambiguous')
    return values[0]


def clock_check(ssh):
    stdin,stdout,stderr=ssh.exec_command("python3 -u -c "+shlex.quote('import sys,time\nfor line in sys.stdin:\n print(time.time_ns(),flush=True)'),timeout=5)
    values=[]
    try:
        for _ in range(7):
            a=time.time_ns();stdin.write('t\n');stdin.flush();remote=int(stdout.readline());b=time.time_ns()
            values.append({'offset_ns':remote-(a+b)//2,'uncertainty_ns':(b-a)//2})
    finally:stdin.close()
    best=min(values,key=lambda r:r['uncertainty_ns'])
    if abs(best['offset_ns'])+best['uncertainty_ns']>50_000_000:raise RuntimeError('50ms clock gate: '+json.dumps(best))
    return best


def connect(node,password,known_hosts=None):
    import paramiko
    c=paramiko.SSHClient();c.load_system_host_keys()
    if known_hosts:c.load_host_keys(str(known_hosts))
    c.set_missing_host_key_policy(paramiko.RejectPolicy())
    c.connect(node['address'],port=node['port'],username=node['username'],password=password or None,
              timeout=5,auth_timeout=5,banner_timeout=5)
    return c


def pilot(platform,output,password,known_hosts=None):
    platform=validate_platform(platform);output=Path(output)
    output.mkdir(parents=True,exist_ok=False)
    nodes={n['role']:n for n in platform['nodes']}
    worker=(Path(__file__).with_name('cw_worker.py')).read_text(encoding='utf-8')
    clients={};channels={};receipt={'status':'preparing','source':'live_hardware_no_business',
      'traffic_started':False,'statistics_started':False,'automatic_test_started':False,'nodes':{},'events':[]}
    def save():atomic_json(output/'receipt.json',receipt)
    def read(role,expected):
        raw=channels[role][1].readline()
        if not raw:raise RuntimeError(role+' worker exited: '+channels[role][2].read().decode(errors='replace')[:200])
        value=json.loads(raw);receipt['events'].append({'node':role,**value});save()
        if value['event']!=expected:raise RuntimeError(role+' expected '+expected+': '+repr(value))
        return value
    try:
        ap=connect(nodes['AP'],password,known_hosts);clients['AP']=ap
        _,out,_=ap.exec_command('iw dev',timeout=5);aptext=out.read().decode()
        receipt['AP_MAC']=ap_mac(aptext);save()
        for role in ('STA1','STA2','STA3','STA4'):
            c=connect(nodes[role],password,known_hosts);clients[role]=c
            clock=clock_check(c)
            _,out,_=c.exec_command('iw dev',timeout=5);text=out.read().decode()
            matches=re.findall(r'Interface (\S+).*?wiphy (\d+)',text,re.S)
            if len(matches)!=1:raise RuntimeError(role+' interface discovery ambiguous')
            iface,phy=matches[0];phy='phy'+phy
            stdin,stdout,stderr=c.exec_command('sudo -S -k -p "" python3 -u -c '+shlex.quote(worker),timeout=45)
            stdin.write(password+'\n');stdin.flush();channels[role]=(stdin,stdout,stderr)
            read(role,'ready')
            stdin.write(json.dumps({'interface':iface,'phy':phy,'AP_MAC':receipt['AP_MAC']})+'\n');stdin.flush()
            armed=read(role,'armed');receipt['nodes'][role]={'interface':iface,'phy':phy,'clock':clock,'original':armed['original']};save()
        t0=time.time_ns()+20*10**9
        plan={'T0_ns':t0,'actions':[{'offset_s':0,'W':32},{'offset_s':3,'W':64},{'offset_s':6,'W':32}]}
        receipt.update(status='armed',plan=plan);save()
        for stdin,_,_ in channels.values():stdin.write(json.dumps(plan)+'\n');stdin.flush()
        for role in channels:read(role,'plan_received')
        receipt['status']='running';save()
        for stage in range(3):
            for role in channels:
                read(role,'cw_requested');event=read(role,'cw_verified')
                # The receipts include configuration readback latency, not an
                # assertion of atomic simultaneous hardware register writes.
                receipt['nodes'][role].setdefault('verified',[]).append(event)
        for role in channels:
            read(role,'pilot_completed');restore=read(role,'restoration');end=read(role,'finished')
            receipt['nodes'][role]['restoration']=restore
            if not restore['confirmed'] or not end['restoration_confirmed']:raise RuntimeError(role+' restore unconfirmed')
        receipt['status']='manual_no_business_passed';save()
    except Exception as exc:
        receipt.update(status='stopped_at_gate',error=str(exc));save()
        for role,(stdin,_,_) in channels.items():
            try:stdin.write('abort\n');stdin.flush()
            except Exception:pass
        for role,(_,stdout,stderr) in channels.items():
            try:
                for raw in stdout:
                    event=json.loads(raw);receipt['events'].append({'node':role,**event})
                    if event['event']=='restoration':receipt['nodes'].setdefault(role,{})['restoration']=event
            except Exception as err:receipt.setdefault('cleanup_errors',{})[role]=str(err)
        save()
    finally:
        for c in clients.values():c.close()
    return receipt


def main():
    p=argparse.ArgumentParser(description='显式现场授权：四STA无业务CW32→64→32并恢复；不打流、不启停统计')
    p.add_argument('--platform',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--known-hosts',type=Path);p.add_argument('--authorize-no-business-cw',action='store_true')
    args=p.parse_args()
    if not args.authorize_no_business_cw:p.error('必须明确选择--authorize-no-business-cw')
    password=getpass.getpass('本次SSH/sudo密码（仅内存，不保存）：')
    receipt=pilot(load_json(args.platform),args.output,password,args.known_hosts)
    print(json.dumps({'status':receipt['status'],'error':receipt.get('error'),'traffic_started':False},ensure_ascii=False))
    if receipt['status']!='manual_no_business_passed':raise SystemExit(2)


if __name__=='__main__':main()
