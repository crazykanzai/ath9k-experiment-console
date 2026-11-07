import struct
import tempfile
import unittest
from pathlib import Path
import zlib
from unittest.mock import patch

from analyze_retry_audit import ack_crc, trace_rows, align_window
import sync_node


class RetryAuditTests(unittest.TestCase):
    def test_retry_reset_read_preserves_selector(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'regidx').write_text('0x8014\n');(root/'regval').write_text('0x00000002\n')
            def transaction(base,addresses):
                return {'values':{hex(addresses[0]):(root/'regval').read_text()},'selector_restored':True}
            with patch.object(sync_node,'read_registers',side_effect=transaction) as reader:
                value=sync_node.retry_reset_state(root/'dcf_stats','AC=BE qnum=1 aifs=3')
                reader.assert_called_once_with(root,[0x1104])
            self.assertEqual(value['register'],'0x1104')
            self.assertFalse(value['reset_enabled'])
            self.assertTrue(value['selector_restored'])
            self.assertEqual((root/'regidx').read_text(),'0x8014\n')
            (root/'regval').write_text('0x42\n')
            with patch.object(sync_node,'read_registers',side_effect=transaction):
                self.assertTrue(sync_node.retry_reset_state(root/'dcf_stats','AC=BE qnum=1')['reset_enabled'])

    def test_rts_arming_requires_new_abi_and_reset_off(self):
        good={'exit_code':0,'stdout':''}
        data={key:dict(good) for key in ['ip','wireless','power_save','iperf_version','link','dcf_header','edca','basic_access_override','rate_mask_2ghz','rts']}
        data['link']['stdout']='Connected to 00:11:22:33:44:55\nSSID: ath9k_test'
        data['power_save']['stdout']='Power save: off'
        data['rts']['stdout']='RTS thr=500 B'
        data['dcf_header']['stdout']='hol_abi=1 basic_retry_abi=2 rts_retry_abi=1'
        data['retry_counter_reset']={'exit_code':0,'reset_enabled':False,'selector_restored':True}
        with patch.object(sync_node,'cmd',return_value={'stdout':'192.168.50.1'}):
            self.assertIn('RTS retry ABI 2 missing',sync_node.valid_preflight(data,'STA','wlp2s0','192.168.50.1'))
            data['dcf_header']['stdout']=data['dcf_header']['stdout'].replace('rts_retry_abi=1','rts_retry_abi=2')
            self.assertTrue(any('calibration unresolved' in f for f in sync_node.valid_preflight(data,'STA','wlp2s0','192.168.50.1')))
            data['retry_counter_reset']['reset_enabled']=True
            self.assertTrue(sync_node.valid_preflight(data,'STA','wlp2s0','192.168.50.1'))

    def test_sync_worker_rejects_old_basic_counter_before_arming(self):
        good={'exit_code':0,'stdout':''}
        data={key:dict(good) for key in ['ip','wireless','power_save','iperf_version','link','dcf_header','edca','basic_access_override','rate_mask_2ghz']}
        data['link']['stdout']='Connected to 00:11:22:33:44:55\nSSID: ath9k_test'
        data['power_save']['stdout']='Power save: off'
        data['basic_access_override']['stdout']='1'
        data['dcf_header']['stdout']='version=2 hol_abi=1'
        with patch.object(sync_node,'cmd',return_value={'stdout':'192.168.50.1'}):
            self.assertTrue(any('Basic retry ABI' in f for f in sync_node.valid_preflight(data,'STA','wlp2s0','192.168.50.1')))
            data['dcf_header']['stdout']+=' basic_retry_abi=2'
            self.assertEqual(sync_node.valid_preflight(data,'STA','wlp2s0','192.168.50.1'),[])
            data['dcf_header']['stdout']=data['dcf_header']['stdout'].replace('basic_retry_abi=2','basic_retry_abi=20')
            self.assertTrue(any('Basic retry ABI' in f for f in sync_node.valid_preflight(data,'STA','wlp2s0','192.168.50.1')))

    def test_edge_alignment_does_not_fit_corrected_attempts(self):
        times={k:{'monotonic_ns':v} for k,v in [('driver_start_call',0),('driver_start_return',10),('driver_stop_call',20),('driver_stop_return',30)]}
        tx=[{'mono_ns':5,'terminal_attempts':1,'all_attempts':6},
            {'mono_ns':15,'terminal_attempts':2,'all_attempts':2},
            {'mono_ns':25,'terminal_attempts':4,'all_attempts':8}]
        selected,a=align_window(tx,times,2,1)
        self.assertEqual([d['mono_ns'] for d in selected],[5,15])
        self.assertTrue(a['corrected_sum_unambiguous'])
        self.assertTrue(a['corrected_attempts_not_used_for_selection'])

    def test_ambiguous_corrected_boundary_sum_is_not_accepted(self):
        times={k:{'monotonic_ns':v} for k,v in [('driver_start_call',0),('driver_start_return',10),('driver_stop_call',20),('driver_stop_return',30)]}
        tx=[{'mono_ns':5,'terminal_attempts':1,'all_attempts':6},
            {'mono_ns':15,'terminal_attempts':1,'all_attempts':1},
            {'mono_ns':25,'terminal_attempts':1,'all_attempts':2}]
        _,a=align_window(tx,times,2,0)
        self.assertEqual(a['candidate_windows'],2)
        self.assertFalse(a['corrected_sum_unambiguous'])

    def test_preceding_series_are_counted_even_when_all_rates_equal(self):
        line=(' task-1 [000] ... 1.000000: txdone: (ath_debug_dcf_timing_tx) '
              'fc=0x188 seq=160 qos=0x0 len=1266 ts_longretry=2 ts_rateindex=2 '
              'r0_idx=8 r0_cf=0x41 r1_idx=8 r1_cf=0x24 r2_idx=8 r2_cf=0x25 r3_idx=255 r3_cf=0x0')
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'trace';p.write_text(line)
            r=trace_rows(p)[0]
        self.assertEqual(r['terminal_attempts'],3)
        self.assertEqual(r['earlier_attempts'],5)
        self.assertEqual(r['all_attempts'],8)
        self.assertEqual(r['seq_num'],10)

    def test_complete_ack_crc_detects_corruption(self):
        ack=bytes.fromhex('d40000001c4bd6d158d9')
        good=ack+struct.pack('<I',zlib.crc32(ack)&0xffffffff)
        bad=good[:-1]+bytes([good[-1]^1])
        radiotap=bytes.fromhex('0000080000000000')
        raw=struct.pack('<IHHIIII',0xa1b2c3d4,2,4,0,0,256,127)
        for frame in [good,bad]:
            packet=radiotap+frame
            raw+=struct.pack('<IIII',1,0,len(packet),len(packet))+packet
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'air.pcap';p.write_bytes(raw);result=ack_crc(p)
        self.assertEqual(result,{1:True,2:False})

    def test_truncated_ack_is_not_treated_as_verified(self):
        packet=bytes.fromhex('0000080000000000d40000001c4bd6d158d9')
        raw=struct.pack('<IHHIIII',0xa1b2c3d4,2,4,0,0,256,127)+struct.pack('<IIII',1,0,len(packet),len(packet)+4)+packet
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'air.pcap';p.write_bytes(raw);result=ack_crc(p)
        self.assertEqual(result,{1:None})


if __name__=='__main__':unittest.main()
