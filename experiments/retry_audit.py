"""Read-only driver retry audit using an isolated ftrace kprobe instance.

No module rebuild and no retry-counter semantic changes. Hardware structure
offsets are obtained from the exact live module's DWARF, never hardcoded.
"""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import shlex
import time
import re
import shutil
import subprocess
import hashlib

from sync_run import Remote, NODES, write, sta_phy, access_config, set_access_config
from reference_helpers import need, background, create_monitor, delete_monitor, active_survey
from reference_helpers import stop_capture, clock_check, prefetched_text

MODULE = '/home/EXPERIMENT_USER/ath9k-kernel/basic-retry-07/ath9k-basic-retry.ko'
HW_MODULE = '/lib/modules/5.15.0-139-generic/updates/apstaselect/ath9k_hw.ko'
EXPRESSIONS = {
    'bf_skb': '(long)&((struct ath_buf *)0)->bf_mpdu',
    'bf_rates': '(long)&((struct ath_buf *)0)->rates',
    'rate_size': 'sizeof(struct ieee80211_tx_rate)',
    'skb_data': '(long)&((struct sk_buff *)0)->data',
    'frame_len': '(long)&((struct ath_buf *)0)->bf_state.bfs_dcf_frame_len',
    'protection': '(long)&((struct ath_buf *)0)->bf_state.bfs_dcf_protection',
    **{x: '(long)&((struct ath_tx_status *)0)->' + x for x in
       ['ts_seqnum','ts_tstamp','ts_status','ts_rateindex','ts_shortretry','ts_longretry','ts_flags']},
}

def shell(r, cmd, timeout=30):
    return need(r.run('sh -c ' + shlex.quote(cmd), sudo=True, timeout=timeout), r.name)

def offsets(r, module=MODULE, expressions=None, module_name='ath9k'):
    expressions=EXPRESSIONS if expressions is None else expressions
    live=need(r.run('cat /sys/module/'+module_name+'/srcversion'),'live module version').strip()
    disk=need(r.run('modinfo -F srcversion '+shlex.quote(module)),'DWARF module version').strip()
    if not live or live!=disk:raise RuntimeError('DWARF module does not match the live ath9k')
    args = 'gdb -nx -batch ' + shlex.quote(module)
    for key, expression in expressions.items():
        args += ' -ex ' + shlex.quote('printf "' + key + '=%ld\\n", ' + expression)
    raw = need(r.run(args), 'module DWARF')
    values = dict((k, int(v)) for k,v in (line.split('=') for line in raw.splitlines() if '=' in line))
    if set(values) != set(expressions):
        raise RuntimeError('incomplete DWARF offsets: ' + raw)
    return values, raw

def event_definition(o):
    data = '+%d(+%d(%%si))' % (o['skb_data'], o['bf_skb'])
    fields = ['fc=+0(%s):x16' % data, 'seq=+22(%s):u16' % data,
              'qos=+24(%s):x8' % data, 'bf=%si:x64',
              'len=+%d(%%si):u16' % o['frame_len'],
              'protection=+%d(%%si):u8' % o['protection']]
    if o.get('cohort'):
        fields.append('skb=+%d(%%si):x64'%o['bf_skb'])
    types = {'ts_seqnum':'u16','ts_tstamp':'u32','ts_flags':'x8'}
    for name in EXPRESSIONS:
        if name.startswith('ts_'):
            fields.append('%s=+%d(%%dx):%s' % (name, o[name], types.get(name,'u8')))
    for i in range(4):
        at=o['bf_rates'] + i*o['rate_size']
        fields += ['r%d_idx=+%d(%%si):u8' % (i,at), 'r%d_cf=+%d(%%si):x16' % (i,at+1)]
    if 'bf_desc' in o:
        for key,value in o.items():
            if key.startswith('ds_'):
                fields.append('%s=+%d(+%d(%%si)):x32'%(key,value,o['bf_desc']))
    return 'p:apsta_audit/txdone ath9k:ath_debug_dcf_timing_tx ' + ' '.join(fields)


def input_event_definition(o, site='ath_buf_set_rate'):
    """Same pre-encoding site on original and candidate; x86-64 bf is RSI.

    Exact module DWARF/disassembly is archived before enabling this probe.
    The skb identity plus bf pointer and monotonic order join it to completion.
    """
    data='+%d(+%d(%%si))'%(o['skb_data'],o['bf_skb'])
    fields=['fc=+0(%s):x16'%data,'seq=+22(%s):u16'%data,
            'qos=+24(%s):x8'%data,'bf=%si:x64']
    if o.get('cohort'):
        fields.append('skb=+%d(%%si):x64'%o['bf_skb'])
    for i in range(4):
        at=o['bf_rates']+i*o['rate_size']
        fields+=['r%d_idx=+%d(%%si):u8'%(i,at),'r%d_cf=+%d(%%si):x16'%(i,at+1)]
    return 'p:apsta_audit/txinput ath9k:'+site+' '+' '.join(fields)

def collect(rs, out, rd):
    ap=rs['AP']
    for n in ['AP','STA2','STA3','STA4']:
        source=rd[n]+'/air.pcap'
        if n!='AP':
            temp=out/'controller'/('transfer_'+n+'.pcap')
            rs[n].sftp.get(source,str(temp));ap.sftp.put(str(temp),rd['AP']+'/parse_'+n+'.pcap');source=rd['AP']+'/parse_'+n+'.pcap'
        fields=['frame.time_epoch','radiotap.mactime','wlan.fc.type_subtype','wlan.ta','wlan.ra','wlan.seq','wlan.frag','wlan.qos.tid','wlan.fc.retry','radiotap.datarate','radiotap.dbm_antsignal','wlan.fcs.status','frame.len']
        target=rd['AP']+('/air.tsv' if n=='AP' else '/'+n+'_air.tsv')
        cmd='tshark -r '+source+' -T fields -E header=y '+' '.join('-e '+f for f in fields)+' > '+target+' 2> '+target+'.stderr'
        need(ap.run(cmd,timeout=60),'tshark export: '+str(ap.file(target+'.stderr')))
    for n,r in rs.items():r.get_tree(rd[n],out/'raw'/n/rd[n].rsplit('/',1)[1])

def main():
    p=argparse.ArgumentParser(); p.add_argument('--out', required=True); p.add_argument('--inspect', action='store_true'); p.add_argument('--repeats',type=int,default=3)
    p.add_argument('--recover',action='store_true',help='retrieve completed rep01 after a controller export failure; never retransmit')
    p.add_argument('--module',default=MODULE,help='exact currently loaded module with DWARF')
    p.add_argument('--regression',action='store_true',help='require Basic retry ABI 2 before sending traffic')
    p.add_argument('--access',choices=['basic','rts'],default='basic')
    p.add_argument('--measure',type=int,default=30,help='single-STA diagnostic measurement seconds')
    p.add_argument('--record-descriptors',action='store_true',help='read raw ctl/status words at TX completion; never modify descriptors')
    p.add_argument('--record-input',action='store_true',help='read original rates at the selected pre-encoding entry')
    p.add_argument('--input-site',choices=['ath_tx_fill_desc','ath_buf_set_rate'],default='ath_buf_set_rate')
    p.add_argument('--capture-udp',action='store_true',help='capture AP receive payload timestamps for the same driver window')
    p.add_argument('--cohort',action='store_true',help='read-only enqueue/identity/ANI probes, no driver changes')
    args=p.parse_args(); out=Path(args.out); out.mkdir(parents=True,exist_ok=True)
    if not 5<=args.measure<=30:p.error('--measure must be 5..30 for this diagnostic')
    if args.regression and args.access!='basic':p.error('--regression currently validates Basic ABI 2 only')
    if (out/'manifest.json').exists() and not args.recover:
        p.error('output manifest exists; choose a fresh experiment directory')
    rs={}; caps=[]; mons=[]; original=None; instance=None; event=False; server=None
    report={'kind':'single-STA retry diagnostic, not formal CW data','started_ns':time.time_ns()}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            rs=dict(zip(NODES,pool.map(lambda kv:Remote(*kv,os.environ['APSTA_SSH_PASSWORD']),NODES.items())))
        if args.recover:
            report=json.loads((out/'manifest.json').read_text(encoding='utf-8'))
            rd={n:'/home/EXPERIMENT_USER/apsta-runs/'+report.get('remote_run_id',out.name)+'/'+n+'/rep01' for n in rs}
            collect(rs,out,rd);report['controller_export_recovered']=True
            return
        sta=rs['STA1']; ap=rs['AP']
        o,raw=offsets(sta,args.module); report['offsets']=o; report['event_definition']=event_definition(o)
        if args.record_descriptors:
            extra,_=offsets(sta,args.module,{'bf_desc':'(long)&((struct ath_buf *)0)->bf_desc'})
            hardware={**{'ds_ctl%d'%i:'(long)&((struct ar5416_desc *)0)->'+('ds_ctl%d'%i if i<2 else 'u.tx.ctl%d'%i) for i in range(12)},
                      **{'ds_status%d'%i:'(long)&((struct ar5416_desc *)0)->u.tx.status%d'%i for i in range(10)}}
            d,hwraw=offsets(sta,HW_MODULE,hardware,'ath9k_hw');o.update(extra);o.update(d)
            report['hardware_DWARF']={'module':HW_MODULE,'offsets':d,'raw':hwraw}
            report['event_definition']=event_definition(o)
        report['DWARF_module']=args.module;report['regression']=args.regression
        if args.record_input:
            report['input_probe_disassembly']=sta.run('gdb -nx -batch '+shlex.quote(args.module)+
                " -ex 'info functions "+args.input_site+"' -ex 'disassemble /r "+args.input_site+"'",timeout=30)
            if report['input_probe_disassembly']['code'] or 'struct ath_softc *, struct ath_buf *' not in report['input_probe_disassembly']['stdout']:
                raise RuntimeError('pre-encoding probe prototype not confirmed')
            report['input_event_definition']=input_event_definition(o,args.input_site)
        cohort_events={}
        if args.cohort:
            if not (args.record_input and args.record_descriptors and args.input_site=='ath_buf_set_rate'):
                raise RuntimeError('cohort requires per-packet input and raw descriptor probes')
            from cohort_trace import definitions
            cohort_events,report['cohort_probe_verification']=definitions(sta,args.module,o)
            o['cohort']=True
            report['event_definition']=event_definition(o)
            report['input_event_definition']=input_event_definition(o,args.input_site)
            report['cohort_event_definitions']=cohort_events
        write(out/'controller'/'dwarf.json',{'offsets':o,'raw':raw})
        for n,r in rs.items():
            report.setdefault('preflight',{})[n]=r.run('iw dev wlp2s0 info; iw dev wlp2s0 link; ip -j -4 addr show dev wlp2s0; pgrep -a iperf3; cat /sys/module/ath9k/srcversion')
        print('DWARF structure offsets and five-node preflight saved',flush=True)
        if args.inspect: return
        if (out/'raw').exists():
            existing=[p for p in (out/'raw').rglob('*') if p.is_file()]
            if not (args.record_descriptors and existing and all(p.name=='hardware_before.json' for p in existing)):
                raise RuntimeError('refusing to overwrite raw experiment data')
        for n,r in rs.items():
            if r.run('pgrep iperf3')['code']==0:raise RuntimeError(n+' existing traffic')
        report['git_commit']=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
        report['git_status']=subprocess.check_output(['git','status','--short'],text=True)
        shutil.copy2(__file__,out/'controller'/'retry_audit.py')
        report['clocks']=clock_check(rs,out/'controller'/'clock.json')
        phy=sta_phy(sta); debug='/sys/kernel/debug/ieee80211/'+phy+'/ath9k/dcf_stats'
        report['dcf_header_before']=shell(sta,'head -1 '+debug).strip()
        if args.regression and 'basic_retry_abi=2' not in report['dcf_header_before'].split():
            raise RuntimeError('Basic retry ABI 2 is required for regression')
        original=access_config(sta,phy); report['original_access']=original
        set_access_config(sta,phy,{'rts':'off','override':1} if args.access=='basic' else {'rts':'500','override':0})
        candidate='/sys/kernel/tracing/instances/apsta_retry_audit'
        if shell(sta,'test ! -e '+candidate+' && echo free').strip()!='free':raise RuntimeError('trace instance already occupied')
        if 'apsta_audit/' in shell(sta,'cat /sys/kernel/tracing/kprobe_events'):raise RuntimeError('audit probe namespace occupied')
        shell(sta,'mkdir '+candidate);instance=candidate
        shell(sta,"printf '%s\\n' "+shlex.quote(event_definition(o))+' >> /sys/kernel/tracing/kprobe_events')
        event=True
        if args.record_input:
            shell(sta,"printf '%s\\n' "+shlex.quote(input_event_definition(o,args.input_site))+' >> /sys/kernel/tracing/kprobe_events')
        for definition in cohort_events.values():
            shell(sta,"printf '%s\\n' "+shlex.quote(definition)+' >> /sys/kernel/tracing/kprobe_events')
        shell(sta,'echo mono > '+instance+'/trace_clock; echo 32768 > '+instance+'/buffer_size_kb; echo 0 > '+instance+'/tracing_on; echo 1 > '+instance+'/events/apsta_audit/txdone/enable'+('; echo 1 > '+instance+'/events/apsta_audit/txinput/enable' if args.record_input else ''))
        for name in cohort_events:shell(sta,'echo 1 > '+instance+'/events/apsta_audit/'+name+'/enable')
        for n in ['AP','STA2','STA3','STA4']:
            create_monitor(rs[n]); mons.append(n)
        ips={}
        for n in ['AP','STA1']:
            addr=json.loads(need(rs[n].run('ip -j -4 addr show dev wlp2s0'),'IP'))
            ips[n]=addr[0]['addr_info'][0]['local']
        mac=re.search(r'addr ([0-9a-f:]+)',report['preflight']['STA1']['stdout']).group(1)
        run_id=out.name+'-'+hashlib.sha256(str(out.resolve()).encode()).hexdigest()[:8]
        report['remote_run_id']=run_id
        remote='/home/EXPERIMENT_USER/apsta-runs/'+run_id
        dirs={n:remote+'/'+n for n in rs}
        for n,r in rs.items():
            if r.run('test -e '+shlex.quote(dirs[n]))['code']==0:
                raise RuntimeError(n+' remote run folder already exists; refusing overwrite')
        for n,r in rs.items():need(r.run('mkdir -p '+shlex.quote(dirs[n])), 'create user-owned run folder')
        sta.sftp.put(str(Path(__file__).with_name('power_probe_node.py')),remote+'/power_probe_node.py')
        report['parameters']={'sta':'STA1','power_dbm':14,'ap_power_dbm':16,'rate_mbps':24,'udp_bps':5000000,'payload_bytes':1200,'warmup_s':5,'measure_s':args.measure,'tail_s':5,'repeats':args.repeats,'mode':'Basic' if args.access=='basic' else 'RTS','CW':[15,1023], 'read_only_descriptor_probe':args.record_descriptors,'read_only_input_probe':args.record_input}
        report['wireless_ips']=ips; report['phases']=[]
        for rep in range(1,args.repeats+1):
            phase={'repeat':rep,'status':'preparing','idle':{}}
            report['phases'].append(phase); write(out/'manifest.json',report)
            rd={n:dirs[n]+'/rep%02d'%rep for n in rs}
            for n,r in rs.items():need(r.run('mkdir -p '+shlex.quote(rd[n])), 'phase folder')
            phase['idle']['before']=active_survey(need(ap.run('iw dev wlp2s0 survey dump'),'survey'))
            time.sleep(5)
            phase['idle']['after']=active_survey(need(ap.run('iw dev wlp2s0 survey dump'),'survey'))
            server=background(ap,'iperf3 -s -1 -p 5521 -J --logfile '+rd['AP']+'/iperf_server.json',rd['AP'],'server')
            for _ in range(20):
                if ':5521' in need(ap.run('ss -ltn sport = :5521'),'server ready'):break
                time.sleep(.1)
            else:raise RuntimeError('server not ready: '+str(ap.file(rd['AP']+'/server.stderr')))
            for n in mons:
                filt='wlan addr1 '+mac+' or wlan addr2 '+mac
                pid=background(rs[n],'tcpdump -i calmon0 -s 256 -U -w '+rd[n]+'/air.pcap '+shlex.quote(filt),rd[n],'tcpdump',sudo=True)
                caps.append((n,pid))
            if args.capture_udp:
                pid=background(ap,'tcpdump -i wlp2s0 -s 96 -U -w '+rd['AP']+'/udp.pcap '+
                    shlex.quote('udp dst port 5521 and src host '+ips['STA1']),rd['AP'],'udp_tcpdump',sudo=True)
                caps.append(('AP',pid))
            shell(sta,'echo > '+instance+'/trace; echo 1 > '+instance+'/tracing_on')
            cmd='python3 '+remote+'/power_probe_node.py --out '+rd['STA1']+' --debugfs '+debug+' --ap '+ips['AP']+' --bind '+ips['STA1']+' --port 5521 --power-dbm 14 --measure '+str(args.measure)
            if args.cohort:cmd+=' --cohort'
            print('rep%d: isolated TX-status trace and four air captures, 5/%d/5 s'%(rep,args.measure),flush=True)
            phase['worker']=sta.run(cmd,sudo=True,timeout=60)
            shell(sta,'echo 0 > '+instance+'/tracing_on; cat '+instance+'/trace > '+rd['STA1']+'/tx_trace.txt; cat '+instance+'/per_cpu/cpu*/stats > '+rd['STA1']+'/trace_stats.txt; cat /sys/kernel/tracing/kprobe_profile > '+rd['STA1']+'/kprobe_profile.txt')
            for n,pid in caps:stop_capture(rs[n],pid)
            caps=[]; server=None
            collect(rs,out,rd)
            if phase['worker']['code']:raise RuntimeError('worker failed '+str(phase['worker']))
            phase['timing']=json.loads(sta.file(rd['STA1']+'/result.json'))
            phase['status']='completed'
            print('rep%d complete, raw preserved'%rep,flush=True)
        report['status']='completed'
    except Exception as exc:
        report['status']='invalid'; report['error']=repr(exc); print('audit failed: '+repr(exc),flush=True)
        raise
    finally:
        cleanup={}
        for n,pid in caps:
            try:stop_capture(rs[n],pid)
            except Exception as exc:cleanup[n+'_capture']=repr(exc)
        if server and 'AP' in rs:cleanup['server']=rs['AP'].run('kill -INT %d'%server)
        if instance and 'STA1' in rs:
            for name in report.get('cohort_event_definitions',{}):
                try:
                    shell(rs['STA1'],'test ! -e '+instance+'/events/apsta_audit/'+name+'/enable || echo 0 > '+instance+'/events/apsta_audit/'+name+'/enable')
                    shell(rs['STA1'],"echo '-:apsta_audit/"+name+"' >> /sys/kernel/tracing/kprobe_events")
                except Exception as exc:cleanup[name+'_event']=repr(exc)
            if args.record_input and event:
                try:
                    shell(rs['STA1'],'test ! -e '+instance+'/events/apsta_audit/txinput/enable || echo 0 > '+instance+'/events/apsta_audit/txinput/enable')
                    shell(rs['STA1'],"echo '-:apsta_audit/txinput' >> /sys/kernel/tracing/kprobe_events")
                except Exception as exc:cleanup['input_event']=repr(exc)
            for key,cmd in [('trace_off','echo 0 > '+instance+'/tracing_on'),
                            ('event_off','test ! -e '+instance+'/events/apsta_audit/txdone/enable || echo 0 > '+instance+'/events/apsta_audit/txdone/enable'),
                            ('event_delete',"echo '-:apsta_audit/txdone' >> /sys/kernel/tracing/kprobe_events" if event else 'true'),
                            ('instance_remove','rmdir '+instance)]:
                try:shell(rs['STA1'],cmd)
                except Exception as exc:cleanup[key]=repr(exc)
        for n in mons:cleanup[n+'_monitor']=delete_monitor(rs[n])
        if original:
            try:set_access_config(rs['STA1'],sta_phy(rs['STA1']),original);cleanup['access_restored']=True
            except Exception as exc:cleanup['access']=repr(exc)
        report['cleanup']=cleanup
        report['final_state']={n:r.run('iw dev wlp2s0 link; pgrep -a iperf3; iw dev; cat /sys/module/ath9k/srcversion') for n,r in rs.items()}
        write(out/'manifest.json', report)
        for r in rs.values():r.close()

if __name__=='__main__':main()
