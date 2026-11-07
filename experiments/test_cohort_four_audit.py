import unittest
from unittest.mock import patch
from audit_cohort_four import trace_evidence


class FourAuditTests(unittest.TestCase):
    def test_failure_is_not_given_success_formula_or_probability(self):
        def tx(status,long):
            return dict(kind='txdone',mono_ns=20,ts_status=status,ts_flags=0,ts_rateindex=1,
                ts_longretry=long,ts_shortretry=0,seq=16,len=1266)
        def update(t,good,inc):
            return dict(kind='mibupdate',mono_ns=t,good=good,bad=0,ackbad=0,
                incgood=inc,incbad=0,incack=0)
        decoded={'stages':[{'tries':4},{'tries':5}],'RTS_enabled':True}
        with patch('audit_cohort_four.decode',return_value=decoded):
            evidence=trace_evidence([update(10,100,0),tx(32,1),tx(1,5),update(30,106,6)])
        self.assertEqual(evidence['ACK_success_protected_DATA_attempt_proxy'],6)
        self.assertEqual(evidence['terminal_failures'][0]['prefix_plus_raw_long'],9)
        self.assertEqual(evidence['terminal_failures'][0]['prefix_plus_raw_long_plus_one'],10)
        self.assertTrue(evidence['terminal_failures'][0]['retry_exhausted'])
        self.assertTrue(all(evidence['passive_ANI']['increment_closure'].values()))
        self.assertFalse(evidence['formal_probability_approved'])


if __name__=='__main__':unittest.main()
