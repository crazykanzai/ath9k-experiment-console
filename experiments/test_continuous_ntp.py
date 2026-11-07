import unittest
from unittest.mock import Mock,patch
from continuous_ntp import check,require,source_address
from passive_listener import PassiveSession
class ContinuousNTPTests(unittest.TestCase):
    def remote(self,text,code=0):
        r=Mock();r.name='TEST';r.run.return_value={'code':code,'stdout':text,'stderr':''};return r
    def test_exact_active_reference_passes(self):
        self.assertTrue(check(self.remote('NTPSynchronized=yes\n Server: 192.168.100.2 (192.168.100.2)\n'),'192.168.100.2')['pass'])
    def test_unsynchronized_or_wrong_or_unreachable_reference_rejected(self):
        for text in ['NTPSynchronized=no\n Server: 192.168.100.2','NTPSynchronized=yes\n Server: 192.168.100.20','NTPSynchronized=yes\n Server: (null) (192.168.100.2)']:
            with self.subTest(text=text):
                with self.assertRaises(RuntimeError):require(self.remote(text),'192.168.100.2')
    def test_remote_command_failure_rejected(self):
        self.assertFalse(check(self.remote('NTPSynchronized=yes\n Server: 192.168.100.2',1),'192.168.100.2')['pass'])
    def test_source_is_data_not_shell(self):
        for value in ['192.168.100.2; true','example.org','8.8.8.8']:
            with self.assertRaises(ValueError):source_address(value)
    def test_listener_does_not_step_when_ntp_margin_fails(self):
        s=PassiveSession.__new__(PassiveSession);s.spec={'continuous_ntp_source':'192.168.100.2'};s.record={'clock_checks':[]};s.remote=Mock()
        with patch('passive_listener.require_ntp_source',return_value={'pass':True}),patch('passive_listener.probe',return_value={'within_50ms_proven':True,'best':{'offset_ns':20_000_000}}),patch('passive_listener.correct_clock') as step:
            with self.assertRaises(RuntimeError):s.arm()
            step.assert_not_called()
if __name__=='__main__':unittest.main()
