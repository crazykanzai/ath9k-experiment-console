"""Streaming radiotap/802.11 decoding and end-referenced legacy timing.

No host-PCAP timestamps are used for microsecond intervals. No scipy/tshark
dependency and no edits to capture files. Unknown PHY durations stay unknown.
"""
import bisect,collections,json,math,struct,zlib
from pathlib import Path
LAYOUT={0:(8,8),1:(1,1),2:(1,1),3:(2,4),4:(2,2),5:(1,1),6:(1,1),7:(2,2),8:(2,2),9:(2,2),10:(1,1),11:(1,1),12:(1,1),13:(1,1),14:(2,2),15:(2,2),16:(1,1),17:(1,1),18:(4,8),19:(1,3),20:(4,8),21:(2,12),22:(8,12),23:(2,12),24:(2,12)}
def radiotap(packet):
    if len(packet)<8 or packet[0]!=0:raise ValueError('invalid radiotap')
    size=struct.unpack_from('<H',packet,2)[0]
    if size>len(packet):raise ValueError('radiotap length exceeds capture')
    words=[];p=4
    while True:
        w=struct.unpack_from('<I',packet,p)[0];words.append(w);p+=4
        if not w&(1<<31):break
        if len(words)>8:raise ValueError('radiotap bitmap overflow')
    fields={}
    for wi,w in enumerate(words):
        for bit in range(31):
            if not w&(1<<bit):continue
            index=32*wi+bit
            if index not in LAYOUT:return size,fields
            align,n=LAYOUT[index];p=(p+align-1)//align*align
            if p+n>size:raise ValueError('truncated radiotap field')
            fields[index]=packet[p:p+n];p+=n
    return size,fields
def phy_duration(length,rate_raw,flags=0,frequency=2412):
    if not rate_raw:return None
    rate=rate_raw/2
    if rate in [6,9,12,18,24,36,48,54]:return 20+4*math.ceil((16+8*length+6)/(rate*4))+(6 if 2300<=frequency<2500 else 0)
    if rate in [1,2,5.5,11]:return (96 if flags&2 and rate!=1 else 192)+math.ceil(8*length/rate)
    return None
def decode(packet,epoch_ns,index,wirelen):
    rt,fields=radiotap(packet);frame=packet[rt:];flags=fields.get(1,b'\0')[0]
    if len(frame)<10:return None
    fc=struct.unpack_from('<H',frame)[0];typ=(fc>>2)&3;sub=(fc>>4)&15
    header=24 if typ in (0,2) else (10 if sub in (12,13) else 16)
    if typ==2:
        header+=6 if fc&0x300==0x300 else 0
        if sub&8:header+=2
        if fc&0x8000 and sub&8:header+=4
    if flags&0x20 and typ==2:
        pad=(-header)%4
        frame=frame[:header]+frame[header+pad:]
    fcs_included=bool(flags&0x10);bad=bool(flags&0x40)
    crc=None
    if fcs_included and len(frame)>=14 and len(packet)==wirelen:crc=(zlib.crc32(frame[:-4])&0xffffffff)==int.from_bytes(frame[-4:],'little')
    mpdu_length=len(frame) if fcs_included else len(frame)+4
    rate_raw=fields.get(2,b'\0')[0] or None
    tsft=struct.unpack('<Q',fields[0])[0] if 0 in fields else None
    freq=struct.unpack('<H',fields[3][:2])[0] if 3 in fields else None
    duration=phy_duration(mpdu_length,rate_raw,flags,freq or 2412)
    # ath9k END -> mac80211 MPDU-start rewind is integer bits/rate. Undo it.
    native_end=tsft+mpdu_length*16//rate_raw if tsft is not None and rate_raw else None
    start=native_end-duration if native_end is not None and duration is not None else None
    mac=lambda b:':'.join(f'{x:02x}' for x in b)
    ra=mac(frame[4:10]);ta=mac(frame[10:16]) if len(frame)>=16 and not(typ==1 and sub in (12,13)) else None
    bssid=mac(frame[16:22]) if len(frame)>=24 and typ==0 else None
    if typ==2 and len(frame)>=24:
        ds=(fc>>8)&3;bssid=ra if ds==1 else ta if ds==2 else mac(frame[16:22]) if ds==0 else None
    seq=struct.unpack_from('<H',frame,22)[0]>>4 if typ in (0,2) and len(frame)>=24 else None
    tid=frame[24+(6 if fc&0x300==0x300 else 0)]&15 if typ==2 and sub&8 and len(frame)>24 else 0
    udp_length=None;udp_port=None;udp_id=None
    if typ==2 and len(frame)>header+8 and frame[header:header+8]==b'\xaa\xaa\x03\x00\x00\x00\x08\x00':
        ip=header+8;ihl=(frame[ip]&15)*4
        if len(frame)>=ip+ihl+8 and frame[ip+9]==17:
            udp=ip+ihl;udp_port=struct.unpack_from('!H',frame,udp+2)[0];udp_length=struct.unpack_from('!H',frame,udp+4)[0]-8
            if len(frame)>=udp+12:udp_id=struct.unpack_from('!i',frame,udp+8)[0]
    return {'index':index,'epoch_ns':epoch_ns,'tsft_us':tsft,'native_end_us':native_end,'start_us':start,'duration_us':duration,'type':typ,'subtype':sub,'fc':fc,'retry':int(bool(fc&0x800)),'ra':ra,'ta':ta,'bssid':bssid,'seq':seq,'tid':tid,'rate_raw':rate_raw,'rate_Mbps':rate_raw/2 if rate_raw else None,'signal_dBm':struct.unpack('b',fields[5])[0] if 5 in fields else None,'frequency_MHz':freq,'mpdu_bytes':mpdu_length,'fcs_included':fcs_included,'crc_good':crc,'bad_fcs_flag':bad,'truncated':len(packet)!=wirelen,'UDP_payload_bytes':udp_length,'UDP_port':udp_port,'UDP_id':udp_id}
def pcap_frames(path):
    with Path(path).open('rb') as f:
        h=f.read(24)
        formats={b'\xd4\xc3\xb2\xa1':('<',1000),b'\xa1\xb2\xc3\xd4':('>',1000),b'\x4d\x3c\xb2\xa1':('<',1),b'\xa1\xb2\x3c\x4d':('>',1)}
        if h[:4] not in formats:raise ValueError('unsupported pcap format')
        endian,scale=formats[h[:4]]
        if struct.unpack_from(endian+'I',h,20)[0]!=127:raise ValueError('capture not DLT radiotap')
        index=0
        while True:
            h=f.read(16)
            if not h:break
            if len(h)!=16:raise ValueError('truncated pcap header')
            sec,frac,n,wire=struct.unpack(endian+'IIII',h);packet=f.read(n);index+=1
            if len(packet)!=n:raise ValueError('truncated pcap body')
            row=decode(packet,sec*1_000_000_000+frac*scale,index,wire)
            if row:yield row
def business(row,sta,ap,port=5201):return row['type']==2 and row['ta']==sta and row['ra']==ap and row['UDP_port']==port and row['UDP_payload_bytes']==1200
def ack_pairs(frames,sta,ap):
    data=[f for f in frames if business(f,sta,ap) and f['crc_good'] and not f['bad_fcs_flag'] and f['native_end_us'] is not None]
    acks=sorted([f for f in frames if f['type']==1 and f['subtype']==13 and f['ra']==sta and f['crc_good'] and not f['bad_fcs_flag'] and f['start_us'] is not None],key=lambda f:f['start_us'])
    starts=[a['start_us'] for a in acks];pairs={};near=[]
    for d in data:
        lo=bisect.bisect_left(starts,d['native_end_us']+4);hi=bisect.bisect_right(starts,d['native_end_us']+16)
        if hi-lo==1:pairs[d['index']]={'DATA':d,'ACK':acks[lo],'SIFS_us':starts[lo]-d['native_end_us']}
        k=bisect.bisect_left(starts,d['native_end_us'])
        if k<len(acks) and starts[k]-d['native_end_us']<=100:near.append(starts[k]-d['native_end_us'])
    return pairs,near
def lattice(values,period):
    if not values:return None
    cs=sum(math.cos(2*math.pi*x/period) for x in values)/len(values);sn=sum(math.sin(2*math.pi*x/period) for x in values)/len(values)
    phase=math.atan2(sn,cs)*period/(2*math.pi);res=[abs((x-phase+period/2)%period-period/2) for x in values]
    return {'period_us':period,'n':len(values),'circular_concentration':math.hypot(cs,sn),'phase_us':phase,'median_distance_to_grid_us':sorted(res)[len(res)//2],'fraction_within_1_5us':sum(r<=1.5 for r in res)/len(res),'warning':'descriptive candidate grid; not proof of physical slot or idle CCA'}
