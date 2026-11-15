"""ReinWiFi-only clock policy; never changes shared/demo defaults.

No I/O on import. Raw clock samples, source/status checks and cleanup are
inherited. The user approved this policy on 2026-11-14; old results stay sealed.
"""
import math

from labconsole.preparation import PreparationHub


CLOCK_POLICY = {
    'id':'reinwifi-initial50-warning10-v1',
    'scope':'ReinWiFi lightweight live adapters only',
    'initial_envelope_limit_ms':50,
    'continuous_envelope_limit_ms':50,
    'warning_above_ms':10,
    'envelope':'abs(offset_ms) + uncertainty_ms',
    'boundary_limit_ms':100,
    'authorization_date':'2026-11-14',
}


class ReinWiFiPreparationHub(PreparationHub):
    def _clock_policy(self):
        return CLOCK_POLICY

    def _clock_limit_ms(self,settled):
        policy=self._clock_policy()
        return policy['initial_envelope_limit_ms'] if settled else policy['continuous_envelope_limit_ms']

    def _clock_sample(self,settled=False):
        policy=self._clock_policy();limit=self._clock_limit_ms(settled)
        self.active['rl_clock_policy']=dict(policy)
        self._save()
        rows=super()._clock_sample(settled)
        # Validate the underlying envelope as well as the retained backend
        # field. NaN, negative uncertainty or an inconsistent receipt must not
        # become an accidental way around the finite 50 ms gate.
        for role,row in rows.items():
            offset=row['offset_ms'];uncertainty=row['uncertainty_ms']
            envelope=abs(offset)+uncertainty
            if (not all(math.isfinite(v) for v in (offset,uncertainty,row['envelope_ms']))
                    or uncertainty<0 or abs(envelope-row['envelope_ms'])>1e-6 or envelope>limit):
                raise RuntimeError(role+' ReinWiFi clock envelope/receipt gate')
            if envelope>policy['warning_above_ms']:
                self.event('clock_warning',f'ReinWiFi 时钟包络超过告警线、未超过专项 {limit} ms 上限；继续',
                           role=role,initial=settled,offset_ms=offset,
                           uncertainty_ms=uncertainty,envelope_ms=row['envelope_ms'],
                           policy_id=policy['id'])
        return rows
