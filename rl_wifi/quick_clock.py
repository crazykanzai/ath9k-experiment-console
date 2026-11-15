"""Quick-screen-only observation transport and explicitly relaxed policy."""
from concurrent.futures import ThreadPoolExecutor
import json
import math
import re
from labconsole.preparation import SSHPreparation,ROLES

QUICK_CLOCK_POLICY={
    'id':'quick8-initial50-continuous100-v2',
    'scope':'fixed eight-window quick screening only; not formal or other RL workflows',
    'initial_envelope_limit_ms':50,'continuous_envelope_limit_ms':100,
    'warning_above_ms':10,'continuous_soft_warning_ms':50,
    'envelope':'abs(offset_ms) + uncertainty_ms','boundary_limit_ms':100,
    'authorization_date':'2026-11-14','transport':'dedicated SSH connection for clock observation',
}


class QuickSSHPreparation(SSHPreparation):
    def __init__(self,platform,password,sudo_password):
        super().__init__(platform,password,sudo_password)
        self.clock_backend=SSHPreparation(platform,password,sudo_password)

    def clock(self,role):
        # Same seven-echo estimator and source/status checks. Only isolate its
        # TCP/SSH transport from large SFTP transfers; do not change timestamps.
        return {**self.clock_backend.clock(role),'transport':'dedicated_clock_SSH'}

    def close(self):
        try:self.clock_backend.close()
        finally:super().close()


def sample_clock(backend):
    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(zip(ROLES,pool.map(backend.clock,ROLES)))


def validate(rows,source,limit=100):
    for role,row in rows.items():
        off,unc,env=(row[k] for k in ('offset_ms','uncertainty_ms','envelope_ms'))
        server=re.search(r'(?m)^\s*Server:\s+(\S+)',row['status'])
        if (not all(math.isfinite(v) for v in (off,unc,env)) or unc<0 or
            abs(abs(off)+unc-env)>1e-6 or env>limit or 'NTPSynchronized=yes' not in row['status'] or
            not server or server[1]!=source):
            raise RuntimeError(role+f' quick-screen {limit}ms/source gate: '+json.dumps(row,ensure_ascii=False))
