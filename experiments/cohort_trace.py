"""Read-only kprobe definitions and strict complete-service cohort ledger."""
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import re
import shlex
from analyze_retry_descriptors import decode


def definitions(remote,module,o):
    from retry_audit import offsets,HW_MODULE
    extra,raw=offsets(remote,module,{
        'skb_cb':'(long)&((struct sk_buff *)0)->cb',
        'enqueue_offset':'(long)&((struct ieee80211_tx_info *)0)->control.enqueue_time'})
    o.update(extra)
    script='python print("hol_bitpos=%d" % next(f.bitpos for f in gdb.lookup_type("struct ieee80211_tx_info").fields() if f.name=="hol_enqueue_valid"))'
    bit=remote.run('gdb -nx -batch '+shlex.quote(module)+' -ex '+shlex.quote(script))
    match=re.search(r'hol_bitpos=(\d+)',bit['stdout'])
    if bit['code'] or not match:raise RuntimeError('cannot verify enqueue valid-bit layout')
    bitpos=int(match[1]);o['hol_bitpos']=bitpos
    data='+%d(%%si)'%o['skb_data']
    arrival=('p:apsta_audit/txarrival ath9k:ath_debug_dcf_hol_enqueue_time skb=%si:x64 '+
        'fc=+0(%s):x16 seq=+22(%s):u16 qos=+24(%s):x8 '%(data,data,data)+
        'enqueue_raw=+%d(%%si):u32 valid_word=+%d(%%si):x32'%(o['skb_cb']+o['enqueue_offset'],o['skb_cb']+(bitpos//32)*4))
    dis=remote.run('gdb -nx -batch '+shlex.quote(module)+" -ex 'info functions ath_debug_dcf_hol_enqueue_time' -ex 'disassemble /r ath_debug_dcf_hol_enqueue_time'",timeout=30)
    if dis['code'] or 'struct ath_softc *, struct sk_buff *' not in dis['stdout']:
        raise RuntimeError('arrival ABI not verified')
    hw=remote.run('gdb -nx -batch '+shlex.quote(HW_MODULE)+" -ex 'disassemble /r ath9k_hw_update_mibstats'",timeout=30)
    # This exact instruction is after all additions, before canary/epilogue;
    # DWARF/source matched HW_MODULE is separately verified by retry_audit.
    m=re.search(r'<\+(\d+)>:\s*48 8b 45 f0\s+mov\s+-0x10\(%rbp\),%rax',hw['stdout'])
    required=['add    %eax,0x8(%rbx)','add    %eax,0x4(%rbx)','add    %eax,(%rbx)']
    if hw['code'] or not m or not all(s in hw['stdout'] for s in required):
        raise RuntimeError('MIB post-update site/registers not verified; do not guess offsets')
    mib=('p:apsta_audit/mibupdate ath9k_hw:ath9k_hw_update_mibstats+'+m[1]+
        ' good=+8(%bx):u32 bad=+4(%bx):u32 ackbad=+0(%bx):u32'+
        ' incgood=-36(%bp):u32 incbad=-32(%bp):u32 incack=-28(%bp):u32')
    return {'txarrival':arrival,'mibupdate':mib},{'extra_offsets':extra,'bit_layout':bit,
        'arrival_disassembly':dis,'MIB_disassembly':hw,'valid_bit':bitpos%32}


def parse(path):
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        m=re.search(r'\s(\d+\.\d+): (txarrival|txinput|txdone|mibupdate): .*? (\w+)=',line)
        if not m:continue
        d={k:int(v,0) if v.startswith('0x') else int(v) for k,v in re.findall(r'(\w+)=(0x[0-9a-f]+|\d+)',line)}
        d.update(kind=m[2],mono_ns=int(Decimal(m[1])*1_000_000_000))
        yield d


def recover_enqueue(raw,valid_word,bit,now_ns):
    if not valid_word&(1<<bit):return None
    ticks=now_ns>>10;elapsed=((ticks&0xffffffff)-raw)&0xffffffff
    return (ticks-elapsed)<<10 if elapsed<1<<31 else None


def classify(start,done,a,b,uncertainty=1024):
    if start is None:return 'unknown_start'
    if done is None:return 'right_censored' if start<b else 'outside'
    if done<=start:return 'invalid_order'
    if any(abs(t-edge)<=uncertainty for t in (start,done) for edge in (a,b)):
        return 'boundary_uncertain'
    if start>=a and done<b:return 'complete'
    if start<a and done>=b:return 'cross_both'
    if start<a and done>=a:return 'cross_start'
    if start<b and done>=b:return 'cross_stop'
    return 'outside'


def ledger(path,a,b,bit=30,uncertainty=1024):
    arrivals={};inputs={};previous={};records=[];mib=[];issues=Counter();generation=0
    for d in parse(path):
        kind=d['kind']
        if kind=='mibupdate':mib.append(d);continue
        if kind=='txarrival':
            generation+=1
            if d['skb'] in arrivals:issues['skb_reused_before_completion']+=1
            d['token']=generation
            d['enqueue_ns']=recover_enqueue(d['enqueue_raw'],d['valid_word'],bit,d['mono_ns'])
            arrivals[d['skb']]=d;continue
        if kind=='txinput':
            if d['bf'] in inputs:issues['bf_reencoded_before_completion']+=1
            inputs[d['bf']]=d;continue
        arrival=arrivals.pop(d.get('skb'),None);original=inputs.pop(d.get('bf'),None)
        # Both pointers plus per-lifetime token, final seq/TID and stream order.
        identity=bool(arrival and original and original.get('skb')==d.get('skb') and
            original['seq']==d['seq'] and original['qos']&15==d['qos']&15)
        tid=d['qos']&15;seq=d['seq']>>4
        business=d.get('len')==1266 and d.get('fc',0)&0xfc==0x88 and tid==0
        if not business:
            # Conservatively re-anchor after other DATA, including non-QoS
            # control traffic whose queue/TID cannot be assumed identical.
            if d.get('fc',0)&0x0c==0x08:previous.clear()
            continue
        enq=arrival.get('enqueue_ns') if arrival else None
        prev=previous.get(tid)
        continuity=bool(prev and prev['acked'] and seq==((prev['seq']+1)&4095) and enq is not None and
            prev['enqueue_ns'] is not None and enq>=prev['enqueue_ns'])
        start=max(enq,prev['done_ns']) if identity and continuity else None
        # Errors other than excessive retry are not ordinary MAC terminal drops.
        acked=bool(d['ts_status']&32)
        hardware_error=bool(d['ts_flags'] or d['ts_status'] not in (32,1))
        if hardware_error:start=None;issues['hardware_error']+=1
        previous[tid]={'seq':seq,'enqueue_ns':enq,'done_ns':d['mono_ns'],'acked':acked and not hardware_error}
        if not identity:issues['identity_missing_or_mismatch']+=1
        row={'token':arrival['token'] if arrival else None,'skb':d.get('skb'),'bf':d.get('bf'),
            'seq':seq,'tid':tid,'enqueue_ns':enq,'input_ns':original['mono_ns'] if original else None,
            'service_start_ns':start,'done_ns':d['mono_ns'],'acked':acked,
            'MAC_retry_exhausted':not acked and d['ts_status']==1 and not hardware_error,
            'identity_verified':identity,'predecessor_continuity':continuity,
            'input_tries':[original.get('r%d_cf'%i,0)&31 for i in range(4)] if original else None,
            'service_us':(d['mono_ns']-start)/1000 if start is not None else None,
            'category':classify(start,d['mono_ns'],a,b,uncertainty),
            'raw':d}
        if start is not None and enq is not None and start<enq:raise AssertionError('start before enqueue')
        records.append(row)
    # These have reached descriptor encoding but no TX completion was seen.
    for bf,d in inputs.items():
        arrival=arrivals.get(d.get('skb'));tid=d['qos']&15
        if d.get('fc',0)&0xfc!=0x88 or tid:continue
        enq=arrival.get('enqueue_ns') if arrival else None;prev=previous.get(tid)
        start=max(enq,prev['done_ns']) if enq is not None and prev and prev['acked'] and d['seq']>>4==((prev['seq']+1)&4095) else None
        records.append({'token':arrival['token'] if arrival else None,'skb':d.get('skb'),'bf':bf,
            'seq':d['seq']>>4,'tid':tid,'service_start_ns':start,'done_ns':None,
            'category':classify(start,None,a,b,uncertainty),'unfinished':True,
            'known_service_lower_bound_us':max(0,b-start)/1000 if start is not None else None})
    return records,mib,dict(issues)


def summarize(records,mode):
    complete=[r for r in records if r['category']=='complete'];succ=[r for r in complete if r['acked']]
    failures=[r for r in complete if not r['acked']];attempts=[];cts=[];rts=[]
    for r in complete:
        d=r['raw'];data=sum(d['r%d_cf'%i]&31 for i in range(d['ts_rateindex']))+d['ts_longretry']+1
        attempts.append(data)
        single=sum(bool(d['r%d_cf'%i]&31) for i in range(4))==1
        if mode=='rts' and single and r['acked']:
            cts.append(data);rts.append(data+d['ts_shortretry'])
    basic_known=mode=='basic' and complete and not failures
    rts_known=mode=='rts' and complete and len(rts)==len(complete)
    mean=lambda rows:sum(r['service_us'] for r in rows)/len(rows) if rows else None
    return {'categories':dict(Counter(r['category'] for r in records)),
        'complete_packets':len(complete),'ACK_success':len(succ),'terminal_failure':len(failures),
        'success_conditional_HoL_us':mean(succ),'failed_service_us':mean(failures),
        'all_terminal_service_us':mean(complete),'delivery_ratio':len(succ)/len(complete) if complete else None,
        'diagnostic_p_Basic_ACK':len(succ)/sum(attempts) if basic_known else None,
        'diagnostic_p_RTS_ACK':len(succ)/sum(rts) if rts_known else None,
        'diagnostic_p_RTS_CTS_proxy':sum(cts)/sum(rts) if rts_known else None,
        'formal_probability_approved':False,'failure_attempt_mapping_validated':False,
        'unobserved_software_queue_frames_possible':True,
        'censoring_scope':'encoded packets without TX completion; software queue snapshots retained separately',
        'service_clock':'host CLOCK_MONOTONIC; enqueue reconstructed from CoDel 1024ns ticks',
        'service_start':'max(enqueue, immediately preceding same-TID FIFO terminal callback); continuity required',
        'warning':'complete-service selection and callback/probe overhead; not unconditional paper delay'}


def window_coverage(records,a,b):
    related=[]
    for r in records:
        category=r['category'];done=r.get('done_ns')
        if (category in ['complete','cross_start','cross_stop','cross_both','boundary_uncertain','right_censored'] or
            category in ['unknown_start','invalid_order'] and done is not None and a<=done<b):
            related.append(r)
    full=sum(r['category']=='complete' for r in related)
    cross=[r['service_us'] for r in related if r['category'].startswith('cross_') and r.get('service_us') is not None]
    return {'observed_window_related_packets':len(related),'complete_packets':full,
        'complete_fraction':full/len(related) if related else None,
        'categories':dict(Counter(r['category'] for r in related)),
        'excluded_cross_service_mean_us':sum(cross)/len(cross) if cross else None,
        'excluded_cross_service_max_us':max(cross) if cross else None,
        'unfinished_lower_bounds_us':[r['known_service_lower_bound_us'] for r in related if r.get('known_service_lower_bound_us') is not None],
        'denominator_scope':'observed services intersecting/possibly intersecting the window, not all warmup/tail completions or unseen software queue frames'}
