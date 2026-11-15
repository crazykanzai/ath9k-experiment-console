"""Session-owned wired SNTP reference. Never adjusts the controller clock."""
import socket
import struct
import threading
import time


def stamp(ns):
    sec,rem=divmod(ns,10**9)
    return struct.pack('!II',(sec+2208988800)&0xffffffff,rem*2**32//10**9)


def response(request,rx,tx):
    if len(request)<48 or request[0]&7!=3 or (request[0]>>3)&7 not in (3,4):return None
    value=bytearray(48);value[:4]=bytes([36,1,request[2],236])
    struct.pack_into('!II',value,4,0,66)
    value[12:16]=b'LOCL';value[16:24]=stamp(rx);value[24:32]=request[40:48]
    value[32:40]=stamp(rx);value[40:48]=stamp(tx)
    return bytes(value)


class TimeReference:
    def __init__(self,ip,allowed,port=123):
        self.allowed=set(allowed);self.closed=threading.Event();self.error=None;self.packets=0
        self.sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        try:self.sock.bind((ip,port));self.sock.settimeout(.5)
        except Exception:self.sock.close();raise
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def run(self):
        try:
            while not self.closed.is_set():
                try:request,peer=self.sock.recvfrom(512)
                except socket.timeout:continue
                if peer[0] not in self.allowed:continue
                payload=response(request,time.time_ns(),time.time_ns())
                if payload:self.sock.sendto(payload,peer);self.packets+=1
        except OSError as exc:
            if not self.closed.is_set():self.error=str(exc)

    def close(self):
        self.closed.set();self.thread.join(2);self.sock.close()
