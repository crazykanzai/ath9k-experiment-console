"""Opt-in node-side primitives, not wired to console or any CLI.

Functions operate on an explicitly supplied already-created nonblocking UDP
socket; importing/testing never creates sockets. Peer/interface/absolute T0
discovery, ownership, leases, clock gates and SSH deployment belong to a future
hardware runner. No function changes wireless, clocks or drivers.
"""
import errno
try:
    from .rtt import ProbeCollector, echo_reply, integer
except ImportError:  # Explicit private node bundle, no package installation.
    from rtt import ProbeCollector, echo_reply, integer


def collect_sender(sock, peer, collector, start_ns, duration_ns, interval_ns,
                   clock_ns, wait, stop_requested=lambda:False, event_sink=lambda row:None,
                   tick_ns=1_000_000, max_drain=128, schedule=None):
    """Bounded generation with deadline drain, using a single monotonic clock.

    start_ns must be converted from the shared absolute T0 by the future runner.
    If the generator misses multiple periods, all scheduled messages are logged
    as generated then locally failed, rather than silently omitting them or
    blasting catch-up packets. Caller archives config + every emitted event.
    """
    for name,value in [('start',start_ns),('duration',duration_ns),('interval',interval_ns),('tick',tick_ns)]:
        integer(value,name,0 if name=='start' else 1)
    integer(max_drain,'max_drain',1)
    end=start_ns+duration_ns; finish=end+collector.deadline_ns
    if schedule is not None:
        # An explicit pure schedule enables batches. The default path preserves
        # the already accepted uniform pilot and its missed-period semantics.
        schedule.count(duration_ns)
    generation_at=lambda seq:start_ns+(schedule.offset_ns(seq) if schedule else seq*interval_ns)
    lateness_budget=schedule.lateness_budget_ns if schedule else min(interval_ns,collector.deadline_ns)
    seq=0;next_generation=start_ns;written=0;aborted=False
    def flush():
        nonlocal written
        while written<len(collector.events):
            event_sink(dict(collector.events[written]));written+=1
    event_sink({'event':'config','run_id':collector.run_id,'node_id':collector.node_id,
                'deadline_ns':collector.deadline_ns,'payload_bytes':collector.payload_bytes})
    try:
        while True:
            now=clock_ns()
            if stop_requested(): aborted=True;break
            if now>=finish: break
            while next_generation<end and next_generation<=now:
                now=clock_ns()
                packet=collector.generate(seq,now,scheduled_ns=next_generation)
                if now-next_generation>=lateness_budget:
                    collector.send_failed(seq,now,'generator_missed_schedule')
                else:
                    try:
                        sent=sock.sendto(packet,peer)
                        if sent!=len(packet): raise OSError('short UDP send')
                        collector.submitted(seq,clock_ns())
                    except OSError as exc:
                        collector.send_failed(seq,clock_ns(),f'{type(exc).__name__}: {exc}')
                        if not isinstance(exc,BlockingIOError) and exc.errno not in (errno.ENOBUFS,errno.EAGAIN,errno.EWOULDBLOCK):
                            raise
                seq+=1;next_generation=generation_at(seq);flush();now=clock_ns()
            for _ in range(max_drain):
                try: packet,address=sock.recvfrom(65535)
                except BlockingIOError: break
                if address!=peer:
                    collector._time(clock_ns());collector.events.append({'event':'rejected_reply',
                        'at_ns':collector.last_time,'reason':'unexpected UDP peer'})
                else: collector.receive(packet,clock_ns())
                flush()
            wait(min(tick_ns,max(1,finish-clock_ns()))/1e9)
        # If execution was blocked until after end, record all missing scheduled
        # generations as local failures. A cancelled run remains incomplete.
        if not aborted:
            now=clock_ns()
            while next_generation<end:
                collector.generate(seq,now,scheduled_ns=next_generation)
                collector.send_failed(seq,now,'generator_missed_schedule')
                seq+=1;next_generation=generation_at(seq)
        flush()
    except Exception:
        flush();raise
    report=collector.report(start_ns,end,max(end,clock_ns())) if clock_ns()>=end else None
    if report is not None: report['collection_complete']=not aborted
    return {'status':'aborted' if aborted else 'completed','collection_complete':not aborted,
            'generation_schedule':schedule.as_dict() if schedule else {'mode':'legacy_uniform','interval_ns':interval_ns},
            'planned_message_count':schedule.count(duration_ns) if schedule else (duration_ns-1)//interval_ns+1,
            'report':report,'clock':'sender_monotonic','transport':'application UDP echo'}


def echo_once(sock, max_packets=128):
    """One bounded passive receive/reply pass; caller supplies lifecycle/lease."""
    integer(max_packets,'max_packets',1)
    counts={'received':0,'replied':0,'malformed':0,'send_failed':0}
    for _ in range(max_packets):
        try: packet,peer=sock.recvfrom(65535)
        except BlockingIOError: break
        counts['received']+=1
        try: reply=echo_reply(packet)
        except ValueError: counts['malformed']+=1;continue
        try:
            if sock.sendto(reply,peer)!=len(reply): raise OSError('short reply')
            counts['replied']+=1
        except OSError as exc:
            counts['send_failed']+=1
            if not isinstance(exc,BlockingIOError) and exc.errno not in (errno.ENOBUFS,errno.EAGAIN,errno.EWOULDBLOCK):
                raise
    return counts
