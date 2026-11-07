"""Paper baselines with explicit hardware timing assumptions, no fitting to outputs."""
import math
from dcf_compare import prediction

def timings(aifsn=3,slot_us=9.,sifs_us=10.,mpdu_bytes=1266):
    # ERP OFDM: 20us PLCP + 4us symbols + 6us signal extension.
    ofdm=lambda length:20+4*math.ceil((16+8*length+6)/96)+6
    data=ofdm(mpdu_bytes);ack=ofdm(14)
    # Historical independent radiotap evidence: RTS/CTS 1Mbps => long preamble.
    rts=192+20*8;cts=192+14*8;ifs=sifs_us+aifsn*slot_us
    return {'slot_us':slot_us,'sifs_us':sifs_us,'aifsn':aifsn,'AIFS_us':ifs,
        'DATA_us':data,'ACK_us':ack,'RTS_us':rts,'CTS_us':cts,'mpdu_bytes':mpdu_bytes,
        'basic':{'success_us':data+sifs_us+ack+ifs,'collision_us':data+ifs},
        'rts':{'success_us':rts+sifs_us+cts+sifs_us+data+sifs_us+ack+ifs,'collision_us':rts+ifs},
        'assumptions':'DATA/ACK24Mbps ERP OFDM, RTS/CTS1Mbps long CCK, no propagation; collision busy time excludes extra sender timeout as in Bianchi; not fitted to measured throughput/HOL'}

def dai_success(n,w,k,q=.5):
    # Dai-Sun (26)-(28), including its large-n exponential approximation.
    def rhs(p):
        d=1+sum(p*(1-p)**i*w*q**(-i) for i in range(k))+(1-p)**k*w*q**(-k)
        return math.exp(-2*n/d)
    lo,hi=0.,1.
    for _ in range(100):
        p=(lo+hi)/2
        if rhs(p)>p:lo=p
        else:hi=p
    return (lo+hi)/2

def dai_mean(p,w,k,success_us,collision_us,slot_us=9.,q=.5):
    # (6), (41), (43): dimensionless slots throughout, then convert to us.
    t=success_us/slot_us;f=collision_us/slot_us
    alpha=1/(1+f-f*p-(t-f)*p*math.log(p))
    expected_wait=lambda i:(1+w*q**(-i))/(2*alpha)
    value=t+(1-p)/p*f+sum((1-p)**i*expected_wait(i) for i in range(k))+(1-p)**k/p*expected_wait(k)
    return {'p_success':p,'alpha':alpha,'mean_HoL_us':value*slot_us}

def theoretical(n,w,k,mode,aifsn=3):
    timing=timings(aifsn);t=timing[mode]
    b=prediction(n,w-1,int(w*2**k-1),timing['slot_us'],t['success_us'],t['collision_us'],1200)
    b.update(p_request_success=b['p'],p_collision_given_attempt=1-b['p'],
        P_tr=1-b['idle_probability'],P_s_given_nonempty=b['success_probability']/(1-b['idle_probability']),
        P_success_generic_slot=b['success_probability'])
    dp=dai_success(n,w,k)
    difs=timings(2);dt=difs[mode]
    return {'Bianchi_finite_n':b,'Dai_Sun_original_large_n_approximation':dai_mean(dp,w,k,**t),
        'hybrid_Bianchi_p_Dai_service_approximation':dai_mean(b['p'],w,k,**t),'timing':timing,
        'model_variant':'same paper equations mapped to actual AIFSN; not an unmodified DIFS-only DCF',
        'DCF_DIFS_reference':{'Bianchi':prediction(n,w-1,int(w*2**k-1),difs['slot_us'],dt['success_us'],dt['collision_us'],1200),
            'Dai_Sun':dai_mean(dp,w,k,**dt),'timing':difs}}
