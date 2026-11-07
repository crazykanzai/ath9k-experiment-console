"""Autonomous receive-only capture worker. Never configures radio or sends data."""
import argparse,json,os,re,signal,socket,subprocess,threading,time,traceback
from datetime import datetime
from pathlib import Path
def stamp():return {'epoch_ns':time.time_ns(),'monotonic_ns':time.monotonic_ns(),'iso':datetime.now().astimezone().isoformat(timespec='microseconds')}
_save_lock=threading.Lock()
def save(p,v):
    # Plan reception and the capture thread share state.json and its tmp path.
    # Serialize the entire write/replace transaction, not just the rename.
    with _save_lock:
        t=p.with_name(p.name+'.tmp');t.write_text(json.dumps(v,indent=2)+'\n');os.replace(t,p)
def cmd(args):
    p=subprocess.run(args,capture_output=True,text=True,timeout=8)
    return {'command':args,'code':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
def worker(args):
    out=Path(args.out);out.mkdir(parents=True,exist_ok=False)
    state={'role':'passive_listener','armed':None,'plan':None,'events':{},'errors':[],'processes':{},'valid':False}
    abort=threading.Event();selected=threading.Event();sock=socket.socket(socket.AF_UNIX);sock.bind(str(out/'control.sock'));sock.listen(4)
    def listener():
        while not abort.is_set():
            try:
                c,_=sock.accept()
                with c:
                    message=json.loads(c.recv(65536))
                    if message.get('action')=='abort':abort.set();reply={'ok':True}
                    elif message.get('action')=='plan' and not selected.is_set():
                        plan=message['plan']
                        if plan['t0_ns']<time.time_ns()+5_000_000_000:reply={'ok':False,'error':'T0 too near'}
                        else:state['plan']=plan;state['events']['plan_received']=stamp();selected.set();reply={'ok':True}
                    else:reply={'ok':False,'error':'unknown/repeated request'}
                    save(out/'state.json',state);c.sendall(json.dumps(reply).encode())
            except OSError:break
    threading.Thread(target=listener,daemon=True).start();process=None;err=None
    try:
        info=cmd(['iw','dev',args.iface,'info']);save(out/'interface_before.json',info)
        if info['code'] or 'type monitor' not in info['stdout'] or f'({args.frequency} MHz)' not in info['stdout']:raise RuntimeError('monitor/channel gate')
        save(out/'link_stats_before.json',cmd(['ip','-s','-j','link','show',args.iface]))
        save(out/'survey_before.json',cmd(['iw','dev',args.iface,'survey','dump']))
        command=['tcpdump','-i',args.iface,'-s','0','-B','16384','-U','-w',str(out/'air_full.pcap')]
        err=(out/'tcpdump.stderr').open('w');state['events']['capture_launch_call']=stamp()
        process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=err)
        state['processes']['tcpdump']={'pid':process.pid,'command':command}
        time.sleep(.5)
        if process.poll() is not None:raise RuntimeError('capture exited before armed')
        state['armed']=stamp();save(out/'state.json',state)
        deadline=time.monotonic()+150
        while not selected.wait(.1):
            if abort.is_set():raise RuntimeError('aborted before T0')
            if process.poll() is not None:raise RuntimeError('capture died before plan')
            if time.monotonic()>deadline:raise RuntimeError('no plan before bounded timeout')
        plan=state['plan'];end=plan['t0_ns']+(plan['warmup_s']+plan['measure_s']+plan['tail_s']+5)*1_000_000_000
        if plan.get('window_observation'):
            from window_observation import WindowObserver
            window_observer=WindowObserver(args.iface,out,plan,abort).start()
        state['planned_capture_stop_ns']=end;save(out/'state.json',state)
        while time.time_ns()<end:
            if abort.is_set():raise RuntimeError('controller abort')
            if process.poll() is not None:raise RuntimeError('capture died during plan')
            abort.wait(min(.1,max(.0005,(end-time.time_ns())/1e9)))
        state['events']['capture_stop_call']=stamp();process.send_signal(signal.SIGINT);process.wait(timeout=8)
        state['events']['capture_stop_return']=stamp()
        if process.returncode!=0:raise RuntimeError('capture exit code '+str(process.returncode))
        state['valid']=True
    except Exception as e:
        state['errors'].append(repr(e))
        state.setdefault('error_tracebacks',[]).append(traceback.format_exc())
    finally:
        if 'window_observer' in locals():
            abort.set()
            state['window_observation_cleanup']=window_observer.close()
        if process is not None:
            if process.poll() is None:
                state['events']['cleanup_stop_call']=stamp();process.send_signal(signal.SIGINT)
                try:process.wait(timeout=8)
                except subprocess.TimeoutExpired:process.kill();process.wait();state['errors'].append('capture cleanup required SIGKILL')
            state['processes']['tcpdump']['exit_code']=process.returncode
        if err:err.close()
        for name,args2 in [('interface_after',['iw','dev',args.iface,'info']),('link_stats_after',['ip','-s','-j','link','show',args.iface]),('survey_after',['iw','dev',args.iface,'survey','dump'])]:
            try:save(out/(name+'.json'),cmd(args2))
            except Exception as e:state['errors'].append(name+': '+repr(e))
        if state['errors']:state['valid']=False
        abort.set();sock.close()
        try:(out/'control.sock').unlink()
        except FileNotFoundError:pass
        state['finished']=stamp();save(out/'state.json',state)
def send(args):
    s=socket.socket(socket.AF_UNIX);s.settimeout(5);s.connect(str(Path(args.out)/'control.sock'))
    message={'action':args.action}
    if args.action=='plan':message['plan']=json.loads(Path(args.plan_file).read_text())
    s.sendall(json.dumps(message).encode());print(s.recv(65536).decode());s.close()
if __name__=='__main__':
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='mode',required=True)
    a=sub.add_parser('arm');a.add_argument('--out',required=True);a.add_argument('--iface',required=True);a.add_argument('--frequency',type=int,required=True)
    a=sub.add_parser('send');a.add_argument('--out',required=True);a.add_argument('--action',choices=['abort','plan'],required=True);a.add_argument('--plan-file')
    a=p.parse_args();worker(a) if a.mode=='arm' else send(a)
