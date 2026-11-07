"""Sent in memory by observe.py; read-only Linux telemetry, no traffic/control."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def command(args):
    p = subprocess.run(args, capture_output=True, text=True, timeout=3)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout).strip()[:300])
    return p.stdout


def parse_hardware_cw(text):
    row = next(line for line in text.splitlines() if line.startswith('AC=BE '))
    fields = dict(t.split('=',1) for t in row.split() if '=' in t)
    if fields.get('status_name') != 'verified':
        raise ValueError('hardware CW readback not verified')
    return {'cwmin': int(fields['hw_cwmin']), 'cwmax': int(fields['hw_cwmax']), 'aifsn': int(fields['hw_aifs']),
            'basis': 'hardware_readback'}


def hardware_cw(base):
    import fcntl
    lock = '/run/lock/apstaselect-ath9k-regidx.lock'
    # Existing common lock only; never create a new coordination inode.
    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        end = time.monotonic()+.25
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > end:
                    raise RuntimeError('shared register lock busy; skip sample')
                time.sleep(.01)
        # Registered FIFO20 driver exposes direct REG_READ hw_* fields. This
        # bypasses no shared selector and performs no debugfs writes at all.
        return parse_hardware_cw((base/'edca_ctrl').read_text())
    finally:
        os.close(fd)


def dcf(base, peer):
    rows = (base/'dcf_stats').read_text().splitlines()
    def tokens(s):
        return dict(t.split('=',1) for t in s.split() if '=' in t)
    head = tokens(next(x for x in rows if x.startswith('version=')))
    if head.get('hol_abi') != '1' or head.get('version') != '2':
        raise ValueError('unsupported DCF ABI')
    conf = tokens(next(x for x in rows if x.startswith('AC=BE ')))
    row = next((tokens(x) for x in rows if x.startswith('peer=') and tokens(x).get('peer','').lower()==peer.lower() and tokens(x).get('AC')=='BE'), None)
    if not row:
        return None
    out = {k:int(row[v]) for k,v in {'hol_samples':'hol_service_samples','hol_sum_us':'hol_service_sum_us',
        'attempts':'exchange_attempts','successes':'exchange_successes','packets':'packets','failures':'packet_failures'}.items()}
    out.update(generation=int(head['generation']),running=head['running']=='1',mode=conf.get('mode'),basic_retry_abi=int(head.get('basic_retry_abi','1')))
    return out


def main():
    role, hint, interval, duration = sys.argv[1],sys.argv[2],float(sys.argv[3]),float(sys.argv[4])
    interfaces = re.findall(r'^\s*Interface (\S+)',command(['iw','dev']),re.M)
    if hint:
        if hint not in interfaces:
            raise ValueError('configured interface not present')
        iface = hint
    elif len(interfaces)==1:
        iface = interfaces[0]
    else:
        raise ValueError('ambiguous wireless interface; supply interface_hint')
    info = command(['iw','dev',iface,'info'])
    phy = 'phy'+re.search(r'^\s*wiphy (\d+)',info,re.M)[1]
    own_mac=(Path('/sys/class/net')/iface/'address').read_text().strip().lower()
    base = Path('/sys/kernel/debug/ieee80211')/phy/'ath9k'
    deadline=time.monotonic()+duration
    while time.monotonic()<deadline:
        start=time.monotonic()
        data={'iface':iface,'mac':own_mac,'phy':phy}
        try:
            if role=='AP':
                raw=command(['iw','dev',iface,'station','dump'])
                stations={}
                for chunk in re.split(r'(?m)^Station ',raw)[1:]:
                    mac=chunk.split()[0].lower()
                    count=re.search(r'(?m)^\s*rx bytes:\s*(\d+)',chunk)
                    if count:stations[mac]=int(count[1])
                data['stations_rx_bytes']=stations
            else:
                link=command(['iw','dev',iface,'link'])
                peer=re.search(r'Connected to ([0-9a-f:]+)',link,re.I)
                data['connected']=bool(peer)
                data['cw']=hardware_cw(base)
                data['dcf']=dcf(base,peer[1]) if peer else None
        except Exception as exc:
            data['error']=str(exc)
        print(json.dumps({'wall_ns':time.time_ns(),'read_span_ms':(time.monotonic()-start)*1000,'data':data}),flush=True)
        time.sleep(max(.01,interval-(time.monotonic()-start)))


if __name__=='__main__':
    main()
