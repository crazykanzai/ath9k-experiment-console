"""Transport-independent deadline probe collection; nanoseconds, sender clock only.

No sockets, SSH, radio writes or wall-clock assumptions. A future node runner must
call these methods and archive the append-only events. Echoes are application
replies, never IEEE 802.11 ACKs. Deadline is generation-to-reply; RTT is submit-to-
reply and is conditional on receiving a reply.
"""
import json
import math
from pathlib import Path
import struct
import uuid

HEADER = struct.Struct('!4sBB16sIQQ')
MAGIC = b'RW01'
REQUEST, REPLY = 0, 1


def integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f'{name} must be integer >= {minimum}')
    return value


def decode(packet):
    if len(packet) < HEADER.size:
        raise ValueError('truncated probe')
    magic, version, kind, run, node, seq, generated = HEADER.unpack_from(packet)
    if magic != MAGIC or version != 1 or kind not in (REQUEST, REPLY):
        raise ValueError('unsupported probe protocol')
    return kind, str(uuid.UUID(bytes=run)), node, seq, generated


def echo_reply(packet):
    """Receiver-side pure function. Small reply avoids echoing 1200 B of load."""
    kind, run, node, seq, generated = decode(packet)
    if kind != REQUEST:
        raise ValueError('receiver expects request')
    return HEADER.pack(MAGIC, 1, REPLY, uuid.UUID(run).bytes, node, seq, generated)


def quantile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered)-1)*q
    lo = int(position); hi = math.ceil(position)
    return ordered[lo] + (ordered[hi]-ordered[lo])*(position-lo)


class ProbeCollector:
    def __init__(self, run_id, node_id, deadline_ns, payload_bytes=1200):
        self.run_id = str(uuid.UUID(run_id))
        self.node_id = integer(node_id, 'node_id')
        if self.node_id > 2**32-1:
            raise ValueError('node_id exceeds wire field')
        self.deadline_ns = integer(deadline_ns, 'deadline_ns', 1)
        self.payload_bytes = integer(payload_bytes, 'payload_bytes', HEADER.size)
        if self.payload_bytes > 65507:
            raise ValueError('UDP payload exceeds limit')
        self.rows = {}; self.events = []; self.last_time = -1

    def _time(self, now):
        integer(now, 'monotonic_ns')
        if now < self.last_time:
            raise ValueError('sender monotonic clock/event order regressed')
        self.last_time = now

    def generate(self, seq, now_ns, scheduled_ns=None):
        integer(seq, 'seq')
        if seq > 2**64-1 or seq in self.rows:
            raise ValueError('duplicate or out-of-range generation sequence')
        self._time(now_ns)
        generated=now_ns if scheduled_ns is None else integer(scheduled_ns,'scheduled_ns')
        if generated>now_ns: raise ValueError('generation is not yet due')
        packet = HEADER.pack(MAGIC, 1, REQUEST, uuid.UUID(self.run_id).bytes,
                             self.node_id, seq, generated)
        self.rows[seq] = {'seq':seq, 'generated_ns':generated, 'observed_generation_ns':now_ns,
                          'submitted_ns':None,
                          'local_failure':None, 'reply_ns':None}
        self.events.append({'event':'generated','seq':seq,'at_ns':now_ns})
        if scheduled_ns is not None: self.events[-1]['scheduled_ns']=scheduled_ns
        return packet + bytes(self.payload_bytes-len(packet))

    def submitted(self, seq, now_ns):
        row = self.rows[seq]
        if row['submitted_ns'] is not None or row['local_failure'] is not None:
            raise ValueError('generation already submitted or failed')
        self._time(now_ns); row['submitted_ns'] = now_ns
        self.events.append({'event':'submitted','seq':seq,'at_ns':now_ns})

    def send_failed(self, seq, now_ns, reason):
        row = self.rows[seq]
        if row['submitted_ns'] is not None or row['local_failure'] is not None:
            raise ValueError('generation already submitted or failed')
        if not reason:
            raise ValueError('failure reason required')
        self._time(now_ns); row['local_failure'] = str(reason)
        self.events.append({'event':'send_failed','seq':seq,'at_ns':now_ns,'reason':str(reason)})

    def receive(self, packet, now_ns):
        self._time(now_ns)
        try:
            kind, run, node, seq, generated = decode(packet)
            row = self.rows.get(seq)
            if (kind != REPLY or run != self.run_id or node != self.node_id or
                    row is None or row['generated_ns'] != generated or
                    row['submitted_ns'] is None):
                raise ValueError('unmatched reply')
        except (ValueError, struct.error) as exc:
            self.events.append({'event':'rejected_reply','at_ns':now_ns,'reason':str(exc)})
            return False
        if row['reply_ns'] is not None:
            self.events.append({'event':'duplicate_reply','seq':seq,'at_ns':now_ns})
            return False
        row['reply_ns'] = now_ns
        self.events.append({'event':'reply','seq':seq,'at_ns':now_ns})
        return True

    def report(self, start_ns, end_ns, as_of_ns):
        """Generation cohort [start,end); no historical/future snapshots allowed."""
        integer(start_ns,'start_ns'); integer(end_ns,'end_ns'); integer(as_of_ns,'as_of_ns')
        if end_ns <= start_ns or as_of_ns < end_ns or as_of_ns < self.last_time:
            raise ValueError('invalid window or as_of precedes observed events')
        counts = dict(generated=0, mature=0, pending=0, on_time=0, late=0,
                      no_reply=0, local_failure=0, never_submitted=0)
        rtts = []; elapsed = []; ledger = []
        for row in self.rows.values():
            if not start_ns <= row['generated_ns'] < end_ns:
                continue
            counts['generated'] += 1
            if row['generated_ns'] + self.deadline_ns > as_of_ns:
                status = 'pending'; counts['pending'] += 1
            else:
                counts['mature'] += 1
                if row['local_failure'] is not None:
                    status = 'local_failure'
                elif row['submitted_ns'] is None:
                    status = 'never_submitted'
                elif row['reply_ns'] is None:
                    status = 'no_reply'
                elif row['reply_ns'] <= row['generated_ns'] + self.deadline_ns:
                    status = 'on_time'
                else:
                    status = 'late'
                counts[status] += 1
                if row['reply_ns'] is not None:
                    # Accumulate integer ns, avoiding cross-Python changes in
                    # floating-point sum algorithms (3.12+ vs node Python).
                    rtts.append(row['reply_ns']-row['submitted_ns'])
                    elapsed.append(row['reply_ns']-row['generated_ns'])
            ledger.append({**row,'status':status})
        mature = counts['mature']
        assert counts['generated'] == mature + counts['pending']
        assert mature == sum(counts[k] for k in ('on_time','late','no_reply','local_failure','never_submitted'))
        return {'run_id':self.run_id,'node_id':self.node_id,'deadline_ns':self.deadline_ns,
                'payload_bytes':self.payload_bytes,
                'window':{'start_ns':start_ns,'end_ns':end_ns,'as_of_ns':as_of_ns,
                          'cohort':'generation [start,end), deadline matured at as_of',
                          'clock':'sender monotonic; no cross-host timestamp subtraction'},
                'counts':counts,'on_time_ratio':counts['on_time']/mature if mature else None,
                'timeout_ratio':1-counts['on_time']/mature if mature else None,
                'rtt_reply_samples':len(rtts),'rtt_mean_ms':sum(rtts)/len(rtts)/1e6 if rtts else None,
                'rtt_p95_ms':quantile(rtts,.95)/1e6 if rtts else None,
                'generation_to_reply_mean_ms':sum(elapsed)/len(elapsed)/1e6 if elapsed else None,
                'complete_cohort':counts['pending']==0,'ledger':ledger}

    def export_events(self, path):
        """Create a new archive; refuse to overwrite any raw event file."""
        with Path(path).open('x',encoding='utf-8') as stream:
            stream.write(json.dumps({'event':'config','run_id':self.run_id,'node_id':self.node_id,
                         'deadline_ns':self.deadline_ns,'payload_bytes':self.payload_bytes})+'\n')
            for event in self.events:
                stream.write(json.dumps(event,ensure_ascii=False)+'\n')


def replay_events(path):
    """Read one explicitly named archive. Strict malformed-input handling."""
    lines = Path(path).read_text(encoding='utf-8').splitlines()
    config = json.loads(lines[0])
    if config.pop('event') != 'config':
        raise ValueError('config event required')
    collector = ProbeCollector(**config); requests = {}
    for line in lines[1:]:
        event = json.loads(line); kind = event['event']; seq = event.get('seq'); now = event['at_ns']
        if kind == 'generated': requests[seq] = collector.generate(seq,now,event.get('scheduled_ns'))
        elif kind == 'submitted': collector.submitted(seq,now)
        elif kind == 'send_failed': collector.send_failed(seq,now,event['reason'])
        elif kind in ('reply','duplicate_reply'):
            accepted = collector.receive(echo_reply(requests[seq]),now)
            if accepted != (kind=='reply'): raise ValueError('inconsistent reply archive')
        elif kind == 'rejected_reply':
            collector._time(now); collector.events.append(event)
        else: raise ValueError('unknown event '+kind)
    return collector
