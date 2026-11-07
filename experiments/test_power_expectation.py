import unittest
from analyze_cohort_sync import expected_power_matches

class PowerExpectationTests(unittest.TestCase):
    def test_old_default_stays_fourteen(self):
        self.assertTrue(expected_power_matches('txpower 14.00 dBm',{}))
        self.assertFalse(expected_power_matches('txpower 5.00 dBm',{}))
    def test_explicit_five_checks_all_readbacks(self):
        p={'expected_STA_txpower_dBm':5}
        self.assertTrue(expected_power_matches('txpower 5.00 dBm',p))
        for text in ['txpower 14.00 dBm','txpower 15.00 dBm','txpower 5.50 dBm','no value']:
            self.assertFalse(expected_power_matches(text,p))
    def test_unregistered_or_boolean_rejected(self):
        for value in [8,11,None,True,'5']:
            with self.assertRaises(ValueError):expected_power_matches('txpower 5.00 dBm',{'expected_STA_txpower_dBm':value})

if __name__=='__main__':unittest.main()
