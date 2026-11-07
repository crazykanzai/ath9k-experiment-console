import copy,unittest
from radio_plan import validate_radio_plan,matches
from analyze_cohort_sync import expected_power_matches
class RadioPlanTests(unittest.TestCase):
    def setUp(self):self.plan={'channel':6,'frequency_MHz':2437,'AP_txpower_dBm':5,'STA_txpower_dBm':{'STA1':5,'STA2':14,'STA3':14,'STA4':14}}
    def test_mixed_power_is_per_identity(self):
        p={'radio_plan':self.plan}
        self.assertTrue(expected_power_matches('txpower 5.00 dBm',p,'STA1'))
        self.assertFalse(expected_power_matches('txpower 5.00 dBm',p,'STA2'))
        self.assertTrue(expected_power_matches('txpower 14.00 dBm',p,'STA2'))
        with self.assertRaises(ValueError):expected_power_matches('txpower 5.00 dBm',p)
    def test_channel_power_width_verified_together(self):
        text='channel 6 (2437 MHz), width: 20 MHz (no HT)\ntxpower 5.00 dBm'
        self.assertTrue(matches(text,self.plan,'AP'));self.assertTrue(matches(text,self.plan,'STA1'))
        self.assertFalse(matches(text,self.plan,'STA2'))
        self.assertFalse(matches(text.replace('2437','2412'),self.plan,'AP'))
        self.assertFalse(matches(text.replace('20 MHz (no HT)','40 MHz'),self.plan,'AP'))
    def test_bad_registered_inputs_rejected(self):
        for k,v in [('channel',True),('channel',13),('frequency_MHz',2412),('AP_txpower_dBm',True),('AP_txpower_dBm',14),('STA_txpower_dBm',{'STA1':5})]:
            p=copy.deepcopy(self.plan);p[k]=v
            with self.assertRaises(ValueError):validate_radio_plan(p)
if __name__=='__main__':unittest.main()
