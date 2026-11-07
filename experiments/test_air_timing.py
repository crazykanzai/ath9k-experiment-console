import math,struct,unittest,zlib
from air_timing import radiotap,decode,phy_duration,ack_pairs,business,lattice
def packet(mpdu,tsft,rate=48):
    frame=mpdu+struct.pack('<I',zlib.crc32(mpdu)&0xffffffff)
    # TSFT, flags, rate, channel, signed signal. Required little-endian padding.
    rt=struct.pack('<BBHI',0,0,23,0x2f)+struct.pack('<QBBHHb',tsft,0x10,rate,2412,0x00c0,-30)
    return rt+frame
class TimingTests(unittest.TestCase):
    def test_radiotap_alignment_crc_and_integer_end_recovery(self):
        raw=packet(b'\xd4\x00\x00\x00'+bytes.fromhex('1c4bd6d158d9'),1000)
        f=decode(raw,0,1,len(raw));self.assertTrue(f['crc_good']);self.assertEqual(f['mpdu_bytes'],14);self.assertEqual(f['native_end_us'],1004);self.assertEqual(f['duration_us'],34);self.assertEqual(f['signal_dBm'],-30)
    def test_airtime_contains_ERP_and_FCS_once(self):
        self.assertEqual(phy_duration(1266,48),450);self.assertEqual(phy_duration(14,48),34);self.assertEqual(phy_duration(20,2),352);self.assertIsNone(phy_duration(1266,None))
    def test_frame_tail_pair_not_naive_TSFT_difference(self):
        data={'index':1,'type':2,'ta':'sta','ra':'ap','UDP_port':5201,'UDP_payload_bytes':1200,'crc_good':True,'bad_fcs_flag':False,'native_end_us':2000}
        ack={'index':2,'type':1,'subtype':13,'ra':'sta','crc_good':True,'bad_fcs_flag':False,'start_us':2010}
        p,n=ack_pairs([data,ack],'sta','ap');self.assertEqual(p[1]['SIFS_us'],10)
        ack['start_us']=2030;self.assertEqual(ack_pairs([data,ack],'sta','ap')[0],{})
    def test_grid_is_descriptive_and_known_period_detectable(self):
        a=lattice([37+9*k for k in range(128)],9);self.assertAlmostEqual(a['circular_concentration'],1);self.assertEqual(a['fraction_within_1_5us'],1);self.assertIsNone(lattice([],9))
if __name__=='__main__':unittest.main()
