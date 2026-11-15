"""Explicit IPv4 TCP payload identification in unencrypted 802.11 captures."""
import struct
from air_timing import radiotap

def tcp_payload(packet):
    rt,fields=radiotap(packet);frame=packet[rt:]
    if len(frame)<24:return None
    fc=struct.unpack_from('<H',frame)[0];sub=(fc>>4)&15
    if (fc>>2)&3!=2:return None
    header=24+(6 if fc&0x300==0x300 else 0)+(2 if sub&8 else 0)+(4 if fc&0x8000 and sub&8 else 0)
    if fields.get(1,b'\0')[0]&0x20:header+=(-header)%4
    if frame[header:header+8]!=b'\xaa\xaa\x03\x00\x00\x00\x08\x00':return None
    ip=header+8
    if len(frame)<ip+20 or frame[ip]>>4!=4 or frame[ip+9]!=6:return None
    ihl=(frame[ip]&15)*4;tcp=ip+ihl
    if ihl<20 or len(frame)<tcp+20 or int.from_bytes(frame[ip+6:ip+8],'big')&0x3fff:return None
    th=(frame[tcp+12]>>4)*4;size=int.from_bytes(frame[ip+2:ip+4],'big')-ihl-th
    return (int.from_bytes(frame[tcp+2:tcp+4],'big'),size) if th>=20 and size>0 else None
