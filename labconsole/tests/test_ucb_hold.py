"""Event-triggered hold logic uses rewards only; no devices or live traffic."""
import copy
import unittest
from labconsole.cw_policy import hold_ucb_step


def initial(rewards):
    state={};w,_,_=hold_ucb_step(state,128,None)
    history=[]
    while state['phase']!='hold':
        history.append(w)
        w,_,_=hold_ucb_step(state,w,rewards[w])
    return state,w,history


class HoldTests(unittest.TestCase):
    def test_initial_coverage_then_twenty_stable_segments_do_not_switch(self):
        s,w,history=initial({8:6.8,16:6.7,32:7.})
        self.assertEqual(history[:3],[8,16,32])
        self.assertEqual(sorted(history),[8,8,16,16,32,32]);self.assertEqual(w,32)
        for score in [7.0,6.9,7.1,6.8]*5:
            w,reason,_=hold_ucb_step(s,w,score)
            self.assertEqual(w,32);self.assertEqual(s['phase'],'hold')

    def test_single_drop_of_five_percent_immediately_reprobes(self):
        s,w,_=initial({8:12.,16:11.,32:10.})
        w,reason,_=hold_ucb_step(s,w,11.4)
        self.assertEqual(s['phase'],'explore');self.assertEqual(w,16)
        self.assertIn('重新探索',reason)
        self.assertEqual(s['hold_comparison'],{'W':8,'previous_Mbps':12.,'observed_Mbps':11.4,'threshold_Mbps':11.4})
        while s['phase']=='explore':w,_,_=hold_ucb_step(s,w,{8:10.,16:10.5,32:11.}[w])
        self.assertEqual(w,32)
        self.assertEqual(s['last_same_w_Mbps'],11.)

    def test_small_reprobe_improvement_keeps_incumbent_and_refreshes_reference(self):
        s,w,_=initial({8:10.,16:11.,32:12.})
        w,_,_=hold_ucb_step(s,w,10.)
        while s['phase']=='explore':w,_,_=hold_ucb_step(s,w,{8:10.5,16:10.3,32:10.}[w])
        self.assertEqual(w,32);self.assertEqual(s['last_same_w_Mbps'],10.)
        for _ in range(6):
            w,_,_=hold_ucb_step(s,w,10.)
            self.assertEqual(s['phase'],'hold')

    def test_missing_samples_retain_last_valid_reference_and_zero_is_valid(self):
        s,w,_=initial({8:12.,16:11.,32:10.})
        reference=s['last_same_w_Mbps'];obs=copy.deepcopy(s['observations'])
        for missing in [None,float('nan'),-1,True]:
            w,_,_=hold_ucb_step(s,w,missing)
            self.assertEqual(s['phase'],'hold');self.assertNotIn('hold_comparison',s)
            self.assertEqual(s['last_same_w_Mbps'],reference);self.assertEqual(s['observations'],obs)
        w,_,_=hold_ucb_step(s,w,0.)
        self.assertEqual(s['phase'],'explore')

    def test_gradual_decline_without_single_five_percent_drop_keeps_holding(self):
        s,w,_=initial({8:12.,16:11.,32:10.})
        for x in [11.8,11.5,11.1,10.9]:
            w,_,_=hold_ucb_step(s,w,x)
            self.assertEqual(s['phase'],'hold')
            self.assertEqual(s['last_same_w_Mbps'],x)

    def test_restart_has_no_stale_manual_or_previous_cycle_rewards(self):
        s,w,_=initial({8:12.,16:11.,32:10.})
        fresh={};new,_,rows=hold_ucb_step(fresh,128,None)
        self.assertEqual(new,8);self.assertTrue(all(r['count']==0 for r in rows))


if __name__=='__main__':unittest.main()
