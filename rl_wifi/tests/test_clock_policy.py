"""Fake clock receipts only; no SSH, reference service or node connections."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from labconsole.preparation import PreparationHub,ROLES
from labconsole.storage import Store
from rl_wifi.preparation import ReinWiFiPreparationHub,CLOCK_POLICY


class ClockPolicyTests(unittest.TestCase):
    def run_sample(self,cls,offset,uncertainty=.8,settled=True,status=None,mutate=None):
        row={'offset_ms':offset,'uncertainty_ms':uncertainty,
             'envelope_ms':abs(offset)+uncertainty,
             'status':status or 'NTPSynchronized=yes\nServer: 192.168.100.10 (192.168.100.10)',
             'wall_ns':100,'monotonic_ns':80}
        if mutate:row.update(mutate)
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        hub=cls(Store(temp.name));hub.active={'id':'a'*32,'events':[],
             'platform':{'controller':{'ntp_source':'192.168.100.10'}}}
        (hub.root/('a'*32)).mkdir()
        hub.backend=Mock();hub.backend.reference_ok.return_value=True
        hub.backend.clock.side_effect=lambda role:copy.deepcopy(row)
        self.hub=hub
        return hub._clock_sample(settled)

    def test_default_first_check_still_10ms(self):
        with self.assertRaisesRegex(RuntimeError,'时间/源'):
            self.run_sample(PreparationHub,11.49,.833)
        self.assertNotIn('rl_clock_policy',self.hub.active)

    def test_rl_accepts_old_stopped_envelope_and_records_raw_warning(self):
        rows=self.run_sample(ReinWiFiPreparationHub,11.490596,.832700)
        self.assertEqual(len(rows),6)
        self.assertEqual(self.hub.active['rl_clock_policy'],CLOCK_POLICY)
        warnings=[e for e in self.hub.active['events'] if e['stage']=='clock_warning']
        self.assertEqual(len(warnings),6)
        self.assertAlmostEqual(warnings[0]['envelope_ms'],12.323296)
        self.assertEqual(self.hub.active['clock_samples'][0]['nodes'],rows)

    def test_edges_10_no_warning_50_warning_over50_stops(self):
        self.run_sample(ReinWiFiPreparationHub,9.2)
        self.assertEqual(self.hub.active['events'],[])
        self.run_sample(ReinWiFiPreparationHub,-49.2)
        self.assertEqual(len(self.hub.active['events']),6)
        with self.assertRaises(RuntimeError):self.run_sample(ReinWiFiPreparationHub,49.21)
        self.assertEqual(len(self.hub.active['clock_samples']),1)

    def test_continuous_gate_is_50_for_both_flows(self):
        for cls in (PreparationHub,ReinWiFiPreparationHub):
            self.run_sample(cls,49.2,settled=False)
            with self.assertRaises(RuntimeError):self.run_sample(cls,50,settled=False)

    def test_correct_source_and_synchronized_status_still_required(self):
        for status in ('NTPSynchronized=no\nServer: 192.168.100.10',
                       'NTPSynchronized=yes\nServer: 192.168.100.2'):
            with self.assertRaises(RuntimeError):self.run_sample(ReinWiFiPreparationHub,1,status=status)

    def test_nonfinite_or_inconsistent_clock_receipt_cannot_pass(self):
        for mutate in ({'offset_ms':float('nan')},{'uncertainty_ms':-1},{'envelope_ms':1}):
            # The existing strict JSON archive also rejects NaN before the
            # subclass receipt check; either failure must stop preparation.
            with self.assertRaises((RuntimeError,ValueError)):
                self.run_sample(ReinWiFiPreparationHub,20,mutate=mutate)

    def test_reference_failure_still_stops_before_sampling(self):
        self.run_sample(ReinWiFiPreparationHub,1)
        self.hub.backend.reference_ok.return_value=False
        with self.assertRaisesRegex(RuntimeError,'参考不可用'):self.hub._clock_sample(True)


if __name__=='__main__':unittest.main()
