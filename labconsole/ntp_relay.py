"""Linux SSH-owned UDP123 relay; no clock writes, daemon or persistent files."""
import ipaddress
import select
import socket
import sys
import threading
import time


def run(source,local,allowed):
    for ip in [source,local,*allowed]:
        if not ipaddress.IPv4Address(ip).is_private:raise ValueError('private wired IPv4 only')
    stop=threading.Event();last=[time.monotonic()]
    def control():
        for line in sys.stdin:
            if line.strip()=='quit':break
            if line.strip()=='heartbeat':last[0]=time.monotonic()
        stop.set()
    listener=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    listener.bind((local,123));listener.settimeout(.2)
    forward=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);forward.connect((source,1123));forward.settimeout(1)
    threading.Thread(target=control,daemon=True).start()
    print('ATH9K_RELAY_READY',flush=True)
    try:
        while not stop.is_set() and time.monotonic()-last[0]<8:
            try:payload,peer=listener.recvfrom(512)
            except socket.timeout:continue
            if peer[0] not in allowed or len(payload)!=48 or payload[0]&7!=3:continue
            forward.send(payload)
            try:answer=forward.recv(512)
            except socket.timeout:continue
            if len(answer)==48 and answer[0]&7==4 and answer[24:32]==payload[40:48]:listener.sendto(answer,peer)
    finally:listener.close();forward.close()
    print('ATH9K_RELAY_CLOSED',flush=True)


if __name__=='__main__':run(sys.argv[1],sys.argv[2],set(sys.argv[3:]))
