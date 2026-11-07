"""Linux shared-selector transaction ABI 1. Never writes hardware regval."""
import os
from pathlib import Path

LOCK_PATH='/run/lock/apstaselect-ath9k-regidx.lock'
ALLOWLIST={0x30,0x1030,0x1070,0x8014,0x801c,0x8004}|{b+4*q for b in [0x1040,0x1080,0x10c0,0x1100] for q in range(10)}


def read_registers(base,addresses,lock_path=LOCK_PATH):
    import fcntl
    addresses=list(addresses)
    if not addresses or any(a not in ALLOWLIST for a in addresses):
        raise ValueError('register is not in the non-clear-on-read allowlist')
    base=Path(base);idx=base/'regidx';values={}
    fd=os.open(lock_path,os.O_RDWR|os.O_CREAT|os.O_CLOEXEC|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        old=idx.read_text()
        try:
            for address in addresses:
                idx.write_text(hex(address)+'\n')
                if int(idx.read_text().strip(),0)!=address:raise RuntimeError('selector changed before register read')
                values[hex(address)]=(base/'regval').read_text().strip()
                if int(idx.read_text().strip(),0)!=address:raise RuntimeError('selector changed during register read')
        finally:
            idx.write_text(old)
            restored=idx.read_text()==old
            if not restored:raise RuntimeError('selector restoration failed')
        return {'values':values,'selector_before':old,'selector_after':old,
                'selector_restored':restored,'regidx_restored':restored,'reg_access_abi':1}
    finally:
        os.close(fd)  # closes the locked file description even on failed reads
