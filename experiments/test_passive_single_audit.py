"""Offline sender/port/window isolation and receiver-side pairing regression."""
import copy,unittest
from listener_selection import listener_target,listener_window,verify_listener_readbacks
from sender_selection import STAS,PORTS
from audit_passive_single import scan_frames,count_gate
from air_timing import business

MACS={n:f'02:00:00:00:00:{i:02x}' for i,n in enumerate(STAS,1)}
AP='02:00:00:00:00:10';MON='02:00:00:00:00:15'
def spec(n):
    return {'sender_STA':n,'sender_mac':MACS[n],'sender_port':PORTS[STAS.index(n)],'AP_mac':AP,'original_mac':MON,'monitor_mac':MON,'experiment_macs':[AP,MON,*MACS.values()]}
def frame(n='STA1',kind='DATA',index=1,epoch=100):
    return {'index':index,'epoch_ns':epoch,'truncated':False,'ta':MACS[n] if kind=='DATA' else None,'ra':AP if kind=='DATA' else MACS[n],
            'bssid':AP,'type':2 if kind=='DATA' else 1,'subtype':8 if kind=='DATA' else 13,'UDP_port':PORTS[STAS.index(n)],'UDP_payload_bytes':1200,'retry':0,
            'crc_good':True,'bad_fcs_flag':False,'native_end_us':2000 if kind=='DATA' else 2044,'start_us':1550 if kind=='DATA' else 2010,
            'tsft_us':1578 if kind=='DATA' else 2040,'rate_Mbps':24.0,'signal_dBm':-35}
class SinglePassiveTests(unittest.TestCase):
    def test_all_four_targets_bind_stable_ports_and_live_MAC(self):
        for n in STAS:
            for measure in [30,180]:
                with self.subTest(n=n,measure=measure):
                    s=spec(n);t=listener_target(s,[n])
                    cfg={n:{'wireless':{'stdout':'Interface anyname\n addr '+MACS[n]}},'AP':{'wireless':{'stdout':'addr '+AP}}}
                    self.assertEqual(verify_listener_readbacks(s,[n],cfg),t)
                    warm=5 if measure==30 else 15
                    w=listener_window({'t0_ns':7,'warmup_s':warm,'measure_s':measure,'tail_s':warm})
                    self.assertEqual(w['stop_ns']-w['start_ns'],measure*10**9)
                    self.assertEqual(w['capture_stop_ns'],7+(2*warm+measure+5)*10**9)
    def test_wrong_sender_port_and_live_MAC_are_rejected(self):
        for bad in [{'sender_STA':'STA1'},{'sender_port':5201},{'sender_mac':'garbage'}]:
            s=spec('STA2');s.update(bad)
            with self.assertRaises(ValueError):listener_target(s,['STA2'])
        with self.assertRaises(ValueError):listener_target(spec('STA1'),['STA1','STA2'])
        cfg={'STA2':{'wireless':{'stdout':'addr '+MACS['STA1']}},'AP':{'wireless':{'stdout':'addr '+AP}}}
        with self.assertRaises(ValueError):verify_listener_readbacks(spec('STA2'),['STA2'],cfg)
    def test_legacy_STA1_spec_still_supported_only_for_STA1(self):
        s={'STA1_mac':MACS['STA1']}
        self.assertEqual(listener_target(s,['STA1'])['sender_port'],5201)
        with self.assertRaises(ValueError):listener_target(s,['STA2'])
    def test_every_sender_filters_MAC_port_and_other_senders(self):
        for n in STAS:
            t=listener_target(spec(n),[n]);mine=frame(n);ack=frame(n,'ACK',2)
            wrong_port=frame(n,index=3);wrong_port['UDP_port']=5999
            foreign=frame(next(x for x in STAS if x!=n),index=4)
            result=scan_frames([mine,ack,wrong_port,foreign],t,spec(n),0,1000,0)
            self.assertEqual(result['measurement_business_DATA'],1);self.assertEqual(result['DATA_ACK_pairs'],1)
            self.assertEqual(result['rate_histogram_by_Retry']['0'],{'24.0':1})
    def test_pairing_uses_hardware_time_and_rejects_duplicate_ACKs(self):
        t=listener_target(spec('STA1'),['STA1']);d=frame();a=frame(kind='ACK',index=2,epoch=900)
        result=scan_frames([d,a],t,spec('STA1'),0,1000,0)
        self.assertEqual(result['DATA_ACK_pairs'],1)
        result=scan_frames([d,a,copy.deepcopy(a)],t,spec('STA1'),0,1000,0)
        self.assertEqual(result['DATA_ACK_pairs'],0)
        a['start_us']=2050
        self.assertEqual(scan_frames([d,a],t,spec('STA1'),0,1000,0)['DATA_ACK_pairs'],0)
    def test_common_wall_window_excludes_warmup_tail_and_bad_CRC(self):
        t=listener_target(spec('STA4'),['STA4']);d=frame('STA4',epoch=101);a=frame('STA4','ACK',2,102)
        early=frame('STA4',index=3,epoch=99);late=frame('STA4',index=4,epoch=200)
        result=scan_frames([early,d,a,late],t,spec('STA4'),100,200,0)
        self.assertEqual(result['measurement_business_DATA'],1)
        d['crc_good']=False
        self.assertEqual(scan_frames([d,a],t,spec('STA4'),100,200,0)['DATA_ACK_pairs'],0)
    def test_existing_capture_thresholds_not_relaxed(self):
        c={'truncated_frames':0,'MON15_transmissions':0,'AP_beacons':20,'measurement_business_DATA':1000,'DATA_ACK_pairs':500,'pair_fraction':.5,'radiotap_coverage':{k:{f:1 for f in ['tsft_us','rate_Mbps','signal_dBm']} for k in ['DATA','ACK']},'sender_RTS':0,'sender_CTS':0,'rate_histogram_by_Retry':{'0':{'24.0':900},'1':{'24.0':100}}}
        self.assertFalse(count_gate(c))
        for key,value in [('truncated_frames',1),('MON15_transmissions',1),('AP_beacons',19),('measurement_business_DATA',999),('DATA_ACK_pairs',499),('pair_fraction',.49),('sender_RTS',1)]:
            bad=copy.deepcopy(c);bad[key]=value;self.assertTrue(count_gate(bad))
        bad=copy.deepcopy(c);bad['rate_histogram_by_Retry']['1']={'54.0':1};self.assertTrue(count_gate(bad))
    def test_business_default_port_compatibility(self):
        a=frame();self.assertTrue(business(a,MACS['STA1'],AP))
        b=frame('STA3');self.assertFalse(business(b,MACS['STA3'],AP));self.assertTrue(business(b,MACS['STA3'],AP,5203))
if __name__=='__main__':unittest.main()
