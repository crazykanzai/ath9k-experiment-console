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
        s,w,history=initial({8:7.,16:6.8})
        self.assertEqual(sorted(history),[8,8,16,16]);self.assertEqual(w,8)
        for score in [7.0,6.9,7.1,6.8]*5:
            w,reason,_=hold_ucb_step(s,w,score)
            self.assertEqual(w,8);self.assertEqual(s['phase'],'hold')

    def test_one_dip_is_not_a_trigger_and_two_drops_are(self):
        s,w,_=initial({8:12.,16:11.})
        w,_,_=hold_ucb_step(s,w,9.)
        self.assertEqual(s['phase'],'hold');self.assertEqual(s['drop_streak'],1)
        w,_,_=hold_ucb_step(s,w,12.)
        self.assertEqual(s['drop_streak'],0)
        w,_,_=hold_ucb_step(s,w,10.)
        w,reason,_=hold_ucb_step(s,w,10.)
        self.assertEqual(s['phase'],'explore');self.assertEqual(w,16)
        self.assertIn('重新探索',reason)
        while s['phase']=='explore':w,_,_=hold_ucb_step(s,w,{8:10.,16:11.}[w])
        self.assertEqual(w,16)
        self.assertEqual(s['reference_Mbps'],11.)

    def test_small_reprobe_improvement_keeps_incumbent_and_refreshes_reference(self):
        s,w,_=initial({8:12.,16:11.})
        for _ in range(2):w,_,_=hold_ucb_step(s,w,10.)
        while s['phase']=='explore':w,_,_=hold_ucb_step(s,w,{8:10.,16:10.3}[w])
        self.assertEqual(w,8);self.assertEqual(s['reference_Mbps'],10.)
        for _ in range(6):
            w,_,_=hold_ucb_step(s,w,10.)
            self.assertEqual(s['phase'],'hold')

    def test_missing_samples_break_consecutive_decline_and_zero_is_valid(self):
        s,w,_=initial({8:12.,16:11.})
        w,_,_=hold_ucb_step(s,w,0.)
        self.assertEqual(s['drop_streak'],1)
        reference=s['reference_Mbps'];obs=copy.deepcopy(s['observations'])
        for missing in [None,float('nan'),-1,True]:
            w,_,_=hold_ucb_step(s,w,missing)
            self.assertEqual(s['phase'],'hold');self.assertEqual(s['drop_streak'],0)
            self.assertEqual(s['reference_Mbps'],reference);self.assertEqual(s['observations'],obs)
        for _ in range(2):w,_,_=hold_ucb_step(s,w,0.)
        self.assertEqual(s['phase'],'explore')

    def test_reference_does_not_follow_gradual_decline_downward(self):
        s,w,_=initial({8:12.,16:11.})
        for x in [11.8,11.5,11.1,10.9]:
            w,_,_=hold_ucb_step(s,w,x)
            self.assertEqual(s['reference_Mbps'],12.)
        for x in [10.7,10.6]:w,_,_=hold_ucb_step(s,w,x)
        self.assertEqual(s['phase'],'explore')

    def test_restart_has_no_stale_manual_or_previous_cycle_rewards(self):
        s,w,_=initial({8:12.,16:11.})
        fresh={};new,_,rows=hold_ucb_step(fresh,128,None)
        self.assertEqual(new,8);self.assertTrue(all(r['count']==0 for r in rows))


if __name__=='__main__':unittest.main()
