"""Read-only boundary snapshots on a pre-delivered absolute plan; no MIB access."""
import json,subprocess,threading,time
from pathlib import Path
def stamp():return {'epoch_ns':time.time_ns(),'monotonic_ns':time.monotonic_ns()}
def read(args):
    begin=stamp()
    try:
        p=subprocess.run(args,capture_output=True,text=True,timeout=8)
        result={'code':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
    except Exception as e:result={'code':-1,'stdout':'','stderr':repr(e)}
    return {'command':args,'call':begin,'return':stamp(),**result}
def collect(iface,target_ns):
    result={'planned_boundary_ns':target_ns,'observer_call':stamp(),'survey':read(['iw','dev',iface,'survey','dump'])}
    phy=(Path('/sys/class/net')/iface/'phy80211').resolve().name
    # recv/phy_err expose accumulated software rxstats, not clear-on-read MIB.
    if phy.startswith('phy') and phy[3:].isdigit():
        base=Path('/sys/kernel/debug/ieee80211')/phy/'ath9k'
        result['software_recv']=read(['cat',str(base/'recv')])
        result['software_phy_err']=read(['cat',str(base/'phy_err')])
    else:result['software_recv']={'code':-1,'stdout':'','stderr':'PHY unavailable'}
    result['link_counters']=read(['ip','-s','-j','link','show',iface])
    result['radio_info']=read(['iw','dev',iface,'info']);result['observer_return']=stamp()
    result['limitations']='software cumulative counters, non-atomic; each read timestamped; missing field NA, no raw hardware MIB reads'
    return result
class WindowObserver:
    def __init__(self,iface,out,plan,abort):
        self.iface=iface;self.out=Path(out);self.plan=plan;self.abort=abort;self.errors=[]
        self.thread=threading.Thread(target=self.run,daemon=True)
    def start(self):self.thread.start();return self
    def run(self):
        try:
            for label,delay in [('start',self.plan['warmup_s']),('stop',self.plan['warmup_s']+self.plan['measure_s'])]:
                target=self.plan['t0_ns']+delay*1_000_000_000
                while time.time_ns()<target:
                    if self.abort.wait(min(.1,max(.0001,(target-time.time_ns())/1e9))):return
                if self.abort.is_set():return
                result=collect(self.iface,target)
                (self.out/('window_observation_'+label+'.json')).write_text(json.dumps(result,indent=2)+'\n')
        except Exception as e:self.errors.append(repr(e))
    def close(self):
        self.thread.join(timeout=2)
        return {'errors':self.errors,'finished':not self.thread.is_alive()}
