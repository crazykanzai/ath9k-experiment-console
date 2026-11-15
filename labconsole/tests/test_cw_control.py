"""Local protocol/parser tests; no host connection or radio operation."""
import ast
import io
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from labconsole.cw_control import ap_mac,clock_check,connect
from labconsole.clock_probe import clock_probe
from labconsole.readonly_probe import dcf


class CWControlTests(unittest.TestCase):
    def test_ap_discovery_accepts_ssid_between_address_and_type(self):
        text='phy#0\n Interface wlp2s0\n  addr 00:11:22:33:44:55\n  ssid ath9k_test\n  type AP\n'
        self.assertEqual(ap_mac(text),'00:11:22:33:44:55')
        for bad in ('',text.replace('type AP','type managed'),text+text):
            with self.assertRaises(RuntimeError):ap_mac(bad)

    def test_clock_gate_includes_uncertainty(self):
        remote=types.SimpleNamespace(exec_command=lambda *a,**k:(io.StringIO(),io.StringIO('100000000\n'*7),io.StringIO()))
        with patch('labconsole.cw_control.time.time_ns',side_effect=[0,200000000]*7):
            with self.assertRaisesRegex(RuntimeError,'clock gate'):clock_check(remote)
        remote.exec_command=lambda *a,**k:(io.StringIO(),io.StringIO('100\n'*7),io.StringIO())
        with patch('labconsole.cw_control.time.time_ns',side_effect=[0,200]*7):
            self.assertEqual(clock_check(remote),{'offset_ns':0,'uncertainty_ns':100})

    def test_observer_echo_reuses_one_channel_and_retains_uncertainty(self):
        calls=[]
        def execute(*args,**kwargs):
            calls.append(args)
            return io.StringIO(),io.StringIO('100\n'*7),io.StringIO()
        remote=types.SimpleNamespace(exec_command=execute)
        with patch('labconsole.clock_probe.time.time_ns',side_effect=[0,200]*7):
            self.assertEqual(clock_probe(remote),(0,0.0001))
        self.assertEqual(len(calls),1)

    def test_changed_driver_signature_rejected_before_probability(self):
        base=types.SimpleNamespace(__truediv__=lambda *args:None)
        class Base:
            def __truediv__(self,other):
                return types.SimpleNamespace(read_text=lambda:'version=2 hol_abi=1 config_mismatches=1\nAC=BE config_mismatches=1\n')
        with self.assertRaisesRegex(ValueError,'signature changed'):dcf(Base(),'00:11:22:33:44:55')

    def test_linux_worker_refuses_existing_flow_and_requires_fixed_sequence(self):
        text=Path(__file__).resolve().parents[1].joinpath('cw_worker.py').read_text(encoding='utf-8')
        ast.parse(text)
        self.assertIn("['pgrep','-x','iperf3']",text)
        self.assertIn("emit('armed'",text)
        self.assertIn("'offset_s':6,'W':32",text)
        self.assertIn('controller deadline/lease expired',text)
        self.assertNotIn('start\\n',text)
        self.assertNotIn('insmod',text)

    def test_strict_host_key_policy_and_no_runtime_credentials_written(self):
        import sys
        calls=[]
        class Client:
            def load_system_host_keys(self):pass
            def load_host_keys(self,p):calls.append(('keys',p))
            def set_missing_host_key_policy(self,p):calls.append(('policy',p))
            def connect(self,*a,**k):calls.append(('connect',k))
        mock=types.SimpleNamespace(SSHClient=Client,RejectPolicy=lambda:'REJECT')
        with patch.dict(sys.modules,{'paramiko':mock}):
            connect({'address':'192.0.2.11','port':22,'username':'test'},'memory-only','known_hosts')
        self.assertIn(('policy','REJECT'),calls)
        self.assertEqual(calls[-1][1]['password'],'memory-only')


if __name__=='__main__':unittest.main()
