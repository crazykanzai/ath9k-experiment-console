import unittest
from individual_sta_summary import describe,group_rows
from sender_selection import STAS
class SummaryTests(unittest.TestCase):
    def test_five_windows_use_t4_not_packet_count(self):
        r=describe([1,2,3,4,5]);self.assertEqual(r['mean'],3);self.assertEqual(r['n_windows'],5)
        self.assertAlmostEqual(r['CI95'][1],3+2.7764451052*2**-.5)
    def test_missing_probability_does_not_become_subset_mean(self):
        r=describe([.9,.8,None,.9,.8]);self.assertIsNone(r['mean']);self.assertIsNone(r['CI95']);self.assertEqual(r['n_available'],4)
    def test_nodes_are_grouped_by_explicit_identity_not_port_or_file_order(self):
        rows=[]
        for b in range(1,6):
            for i,n in enumerate(STAS):
                r={'block':b,'sender':n,'complete_failures':1 if n=='STA2' else 0}
                for k in ['S_Mbps','HoL_success_us','p_ACK_original','failure_original','p_ACK_aux_full','failure_aux_full','final_MAC_delivery']:r[k]=i+b
                rows.append(r)
        g=group_rows(list(reversed(rows)));self.assertEqual(g['STA1']['statistics']['S_Mbps']['mean'],3)
        self.assertEqual(g['STA4']['statistics']['S_Mbps']['mean'],6);self.assertEqual(g['STA2']['complete_failures'],5)
        with self.assertRaises(ValueError):group_rows(rows+[rows[0]])
if __name__=='__main__':unittest.main()
