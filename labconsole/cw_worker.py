"""Linux in-memory, bounded no-business CW pilot. Never starts traffic/statistics.

Run only by cw_control after explicit live authorization. Wire protocol is JSONL
over the existing SSH channel: ready -> armed -> common absolute plan -> receipts.
Original effective EDCA and override enablement are restored in finally.
"""
import fcntl
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

SHARED_LOCK = '/run/lock/apstaselect-ath9k-regidx.lock'
CONTROL_LOCK = '/run/lock/ath9k-console-cw-pilot.lock'
WINDOWS = (8, 16, 32, 64, 128)


def lockfile(path, create=False):
    flags=os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    if create:flags|=os.O_CREAT
    return os.fdopen(os.open(path,flags,0o600),'r+b')


def emit(event, **data):
    print(json.dumps({'event':event, 'wall_ns':time.time_ns(),
                      'monotonic_ns':time.monotonic_ns(), **data}), flush=True)


def receive(timeout):
    if not select.select([sys.stdin], [], [], timeout)[0]:
        raise RuntimeError('controller deadline/lease expired')
    raw=sys.stdin.readline()
    if not raw:raise RuntimeError('controller channel closed')
    return json.loads(raw)


def command(args):
    return subprocess.check_output(args, text=True, timeout=5)


def parse_be(text):
    rows=[dict(item.split('=',1) for item in line.split()) for line in text.splitlines() if line.startswith('AC=BE ')]
    if len(rows)!=1 or rows[0].get('status_name')!='verified':raise RuntimeError('BE hardware readback unverified')
    return rows[0]


def main():
    original=None;control=None;path=None;changed=False
    try:
        if os.geteuid()!=0:raise RuntimeError('requires explicit sudo')
        control=lockfile(CONTROL_LOCK,create=True);fcntl.flock(control,fcntl.LOCK_EX|fcntl.LOCK_NB)
        emit('ready')
        config=receive(10)
        iface=config['interface'];phy=config['phy']
        if '/' in iface or not phy.startswith('phy') or not phy[3:].isdigit():raise ValueError('invalid interface/PHY')
        base=Path('/sys/kernel/debug/ieee80211')/phy/'ath9k';path=base/'edca_ctrl'
        if Path('/sys/module/ath9k/srcversion').read_text().strip()!='0AB85B473A0B1B032BFECCD':raise RuntimeError('registered candidate absent')
        if Path('/sys/module/ath9k/parameters/diag_tx_fifo_trigger').read_text().strip()!='20':raise RuntimeError('FIFO20 parameter absent')
        header=(base/'dcf_stats').read_text().splitlines()[0]
        if 'running=0' not in header or 'hol_abi=1 basic_retry_abi=2' not in header:raise RuntimeError('DCF busy/ABI gate')
        if subprocess.run(['pgrep','-x','iperf3'],stdout=subprocess.DEVNULL).returncode!=1:raise RuntimeError('existing business process')
        if 'Connected to '+config['AP_MAC'] not in command(['iw','dev',iface,'link']):raise RuntimeError('association gate')
        def readback():
            with lockfile(SHARED_LOCK) as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                return parse_be(path.read_text())
        original=readback();emit('armed',original=original,stats_header=header)
        plan=receive(25);t0=plan['T0_ns']
        if type(t0)!=int or not 10e9<=t0-time.time_ns()<=25e9:raise RuntimeError('future T0 guard')
        rows=plan['actions']
        if rows!=[{'offset_s':0,'W':32},{'offset_s':3,'W':64},{'offset_s':6,'W':32}]:raise RuntimeError('pilot sequence must be sealed')
        emit('plan_received',T0_ns=t0)
        for row in rows:
            target=t0+row['offset_s']*10**9
            while time.time_ns()<target:
                if select.select([sys.stdin],[],[],min(.05,max(0,(target-time.time_ns())/1e9)))[0]:
                    raise RuntimeError('controller abort/EOF before action')
            if abs(time.time_ns()-target)>100_000_000:raise RuntimeError('100ms action boundary gate')
            if 'Connected to '+config['AP_MAC'] not in command(['iw','dev',iface,'link']):raise RuntimeError('association changed')
            w=row['W'];emit('cw_requested',W=w,planned_ns=target)
            changed=True
            path.write_text(f'BE 3 {w-1} {8*w-1}\n')
            actual=readback()
            if any(actual.get(k)!=str(v) for k,v in [('hw_aifs',3),('hw_cwmin',w-1),('hw_cwmax',8*w-1)]):raise RuntimeError('hardware CW mismatch')
            emit('cw_verified',W=w,planned_ns=target,readback=actual)
        target=t0+9*10**9
        while time.time_ns()<target:
            if select.select([sys.stdin],[],[],min(.05,max(0,(target-time.time_ns())/1e9)))[0]:
                raise RuntimeError('controller abort/EOF during hold')
        emit('pilot_completed')
    except Exception as exc:
        emit('error',message=str(exc))
    finally:
        restored=not changed
        try:
            if changed and original and path:
                if original['override_enabled']=='1':
                    path.write_text('BE %s %s %s\n'%(original['hw_aifs'],original['hw_cwmin'],original['hw_cwmax']))
                else:path.write_text('BE reset\n')
                with lockfile(SHARED_LOCK) as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    after=parse_be(path.read_text())
                keys=['hw_aifs','hw_cwmin','hw_cwmax','override_enabled','override_valid']
                restored=all(after[k]==original[k] for k in keys)
                emit('restoration',confirmed=restored,readback=after,
                     scope='effective CW/AIFS and override flags; inactive cached fields may differ')
        except Exception as exc:emit('restoration',confirmed=False,message=str(exc))
        emit('finished',restoration_confirmed=restored,remote_reboot=False)
        if control:control.close()


if __name__=='__main__':main()
