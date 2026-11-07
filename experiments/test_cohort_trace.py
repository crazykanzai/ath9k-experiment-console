from pathlib import Path
import tempfile
import os
import unittest
from unittest.mock import patch,Mock
from cohort_trace import recover_enqueue,classify,ledger,summarize,window_coverage
import sync_node
from cohort_node_capture import Capture
from sync_run import probe


class CohortTests(unittest.TestCase):
    def test_codel_recovery_across_wrap_and_invalid(self):
        ticks=(1<<32)+100
        self.assertEqual(recover_enqueue(90,1<<30,30,ticks<<10),(ticks-10)<<10)
        self.assertIsNone(recover_enqueue(90,0,30,ticks<<10))
        self.assertIsNone(recover_enqueue(101,1<<30,30,ticks<<10))

    def test_exclusive_categories(self):
        a,b=100,200
        for s,d,want in [(110,190,'complete'),(90,120,'cross_start'),
            (120,210,'cross_stop'),(90,210,'cross_both'),(None,120,'unknown_start'),
            (110,None,'right_censored'),(110,110,'invalid_order'),(90,99,'outside'),
            (100,190,'boundary_uncertain'),(110,200,'boundary_uncertain')]:
            self.assertEqual(classify(s,d,a,b,0),want)

    def make_trace(self,items):
        lines=[]
        for time,kind,values in items:
            lines.append(' task-1 [000] ... %.9f: %s: (f) %s\n'%(time,kind,
                ' '.join('%s=%s'%(k,hex(v) if k in ['fc','bf','skb','valid_word'] else v) for k,v in values.items())))
        return ''.join(lines)

    def packet(self,seq,skb,bf,enq,arrive,done,status=32):
        common={'fc':0x88,'seq':seq<<4,'qos':0,'skb':skb}
        return [(arrive,'txarrival',{**common,'enqueue_raw':int(enq*1e9)>>10,'valid_word':1<<30}),
            (arrive+.0001,'txinput',{**common,'bf':bf}),
            (done,'txdone',{**common,'bf':bf,'len':1266,'ts_status':status,'ts_flags':0,
                'ts_rateindex':0,'ts_longretry':0,'ts_shortretry':0,
                'r0_cf':4,'r1_cf':0,'r2_cf':0,'r3_cf':0})]

    def run_ledger(self,items,a=2_000_000_000,b=4_000_000_000):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'trace';p.write_text(self.make_trace(sorted(items)))
            return ledger(p,a,b,uncertainty=1024)

    def test_enqueue_outside_window_but_service_inside(self):
        items=self.packet(1,10,20,1.0,1.1,2.1)+self.packet(2,11,21,1.5,1.6,2.5)
        rows,_,issues=self.run_ledger(items)
        self.assertEqual(rows[0]['category'],'unknown_start')
        self.assertEqual(rows[1]['service_start_ns'],2_100_000_000)
        self.assertEqual(rows[1]['category'],'complete')
        self.assertFalse(issues)

    def test_final_failure_in_cohort_and_successor_unverified(self):
        items=self.packet(1,10,20,1,1.1,1.3)+self.packet(2,11,21,2.1,2.2,2.5,1)+self.packet(3,12,22,2.6,2.7,3)
        rows,_,_=self.run_ledger(items)
        summary=summarize(rows,'basic')
        self.assertEqual(summary['terminal_failure'],1)
        self.assertIsNone(summary['diagnostic_p_Basic_ACK'])
        self.assertEqual(rows[-1]['category'],'unknown_start')

    def test_sequence_wrap_and_pointer_reuse_are_separate_lifetimes(self):
        items=self.packet(4095,10,20,1,1.1,1.3)+self.packet(0,10,20,2.1,2.2,2.5)
        rows,_,issues=self.run_ledger(items)
        self.assertEqual(rows[-1]['category'],'complete')
        self.assertNotEqual(rows[0]['token'],rows[1]['token'])
        self.assertFalse(issues)

    def test_missing_input_is_not_synthesized(self):
        items=self.packet(1,10,20,1,1.1,1.3)+self.packet(2,11,21,2.1,2.2,2.5)
        rows,_,issues=self.run_ledger([x for x in items if not(x[1]=='txinput' and x[2]['skb']==11)])
        self.assertEqual(rows[-1]['category'],'unknown_start')
        self.assertEqual(issues['identity_missing_or_mismatch'],1)

    def test_inflight_encoded_packet_is_censored_not_complete(self):
        items=self.packet(1,10,20,1,1.1,1.3)+self.packet(2,11,21,2.1,2.2,4.5)[:-1]
        rows,_,_=self.run_ledger(items)
        self.assertEqual(rows[-1]['category'],'right_censored')

    def test_diagnostic_path_does_not_remove_default_formal_rts_gate(self):
        data={k:{'exit_code':0,'stdout':''} for k in ['ip','wireless','power_save','iperf_version',
            'link','dcf_header','edca','basic_access_override','rate_mask_2ghz','rts']}
        data['link']['stdout']='Connected to aa:bb:cc:dd:ee:ff\nSSID: ath9k_test'
        data['power_save']['stdout']='Power save: off'
        data['rts']['stdout']='RTS thr=500 B'
        data['dcf_header']['stdout']='hol_abi=1 basic_retry_abi=2 rts_retry_abi=1'
        data['rate_mask_2ghz']['stdout']='0x100'
        data['retry_counter_reset']={'exit_code':0,'reset_enabled':False,'selector_restored':True}
        with patch.object(sync_node,'cmd',return_value={'stdout':'192.168.50.1'}):
            self.assertTrue(sync_node.valid_preflight(data,'STA','wlp2s0','192.168.50.1'))
            self.assertEqual(sync_node.valid_preflight(data,'STA','wlp2s0','192.168.50.1',True),[])
            data['retry_counter_reset']['reset_enabled']=True
            self.assertTrue(sync_node.valid_preflight(data,'STA','wlp2s0','192.168.50.1',True))

    def test_coverage_denominator_does_not_include_warmup_and_tail(self):
        rows=[{'category':'complete','done_ns':150},
            {'category':'outside','done_ns':50}, {'category':'outside','done_ns':250},
            {'category':'cross_start','done_ns':110,'service_us':20},
            {'category':'unknown_start','done_ns':120}, {'category':'unknown_start','done_ns':20}]
        cov=window_coverage(rows,100,200)
        self.assertEqual(cov['observed_window_related_packets'],3)
        self.assertEqual(cov['complete_fraction'],1/3)

    def test_probe_control_append_does_not_seek_or_truncate(self):
        capture=Capture({},'.')
        with patch('cohort_node_capture.os.open',return_value=7) as opened,\
             patch('cohort_node_capture.os.write') as written,patch('cohort_node_capture.os.close') as closed:
            capture.append_event('p:apsta_audit/test ath9k:ath_debug_dcf_timing_tx')
        self.assertEqual(opened.call_args.args[1],os.O_WRONLY|os.O_APPEND)
        written.assert_called_once_with(7,b'p:apsta_audit/test ath9k:ath_debug_dcf_timing_tx\n')
        closed.assert_called_once_with(7)

    def test_clock_probe_reuses_channel_and_reports_bound(self):
        remote=Mock();remote.name='STA1';stdin=Mock();stdout=Mock();stderr=Mock()
        stdout.readline.side_effect=['1100\n','2020\n'];stdout.channel.recv_exit_status.return_value=0
        remote.ssh.exec_command.return_value=(stdin,stdout,stderr)
        with patch('sync_run.time.time_ns',side_effect=[1000,1200,2000,2020]):result=probe(remote,2)
        self.assertEqual(remote.ssh.exec_command.call_count,1)
        self.assertEqual(result['best']['offset_ns'],10)
        self.assertEqual(result['best']['uncertainty_ns'],10)
        self.assertTrue(result['within_50ms_proven'])
        self.assertEqual(stdin.write.call_count,2)
        stdin.close.assert_called_once()


if __name__=='__main__':unittest.main()
