"""Read-only clock measurement used by environment preparation."""
import shlex
import time

def clock_probe(client):
    # Reuse one echo process. Per-call Python/SSH startup inflated the former
    # three exec-command RTTs on the wired platform. This is observation only;
    # the same hard offset + uncertainty gate remains below.
    values=[]
    code='import sys,time\nfor line in sys.stdin:\n if line.strip()=="t": print(time.time_ns(),flush=True)'
    stdin,out,err=client.exec_command('python3 -u -c '+shlex.quote(code),timeout=5)
    try:
        for _ in range(7):
            a=time.time_ns();stdin.write('t\n');stdin.flush()
            raw=out.readline().strip();b=time.time_ns()
            if not raw:raise RuntimeError('clock echo returned EOF')
            remote=int(raw)
            values.append((b-a,remote-(a+b)//2))
    finally:stdin.close()
    rtt,offset=min(values)
    return offset,rtt/2/1e6
