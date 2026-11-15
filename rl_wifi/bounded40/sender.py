"""Bounded worker's sender/echo paths, isolated from old live adapters."""
import json
import time
import traceback

try:
    from .collector import IndexedCollector
    from .event_journal import EventJournal
    from ..probe_transport import collect_sender
    from ..rtt import echo_reply
except ImportError:
    from collector import IndexedCollector
    from event_journal import EventJournal
    from probe_transport import collect_sender
    from rtt import echo_reply


def sender(worker, start_mono):
    collector = IndexedCollector(worker.c['id'], 1, worker.c['deadline_ns'])
    worker.probe_collector = collector
    worker.clock_anchor = start_mono
    (worker.out/'application_clock_anchor.json').write_text(json.dumps({
        'start_monotonic_ns': start_mono, 'T0_ns': worker.plan['T0_ns'],
        'deadline_ns': worker.c['deadline_ns'], 'generation_mode': 'bounded40_mixed',
        'collector': 'indexed_snapshot_v1', 'journal': 'bounded_async_v1'}))
    journal = None
    try:
        journal = EventJournal(worker.out/'application_probe.jsonl')
        worker.probe_finished = collect_sender(worker.probe_socket,
            (worker.c['ap_ip'], worker.c['probe_port']), collector, start_mono,
            worker.total*10**9, worker.c['probe_interval_ns'], time.monotonic_ns, time.sleep,
            stop_requested=lambda:worker.sender_stop.is_set() or worker.stop.is_set(),
            event_sink=journal.write, schedule=worker.schedule)
        journal.close()
        journal = None
        # A complete receipt is written only AFTER every journal event persisted.
        (worker.out/'application_final.json').write_text(json.dumps(worker.probe_finished, indent=2))
    except Exception as exc:
        worker.probe_error = str(exc)
        worker.state.update(error=str(exc), application_error=str(exc), application_traceback=traceback.format_exc())
        worker.stop.set()
    finally:
        if journal is not None:
            try:
                journal.close()
            except Exception as exc:
                worker.probe_error = worker.probe_error or str(exc)
                worker.state['application_error'] = worker.probe_error
                worker.state['error'] = worker.probe_error
                worker.stop.set()


def echo_loop(worker):
    journal = None
    try:
        journal = EventJournal(worker.out/'application_echo.jsonl')
        while not worker.stop.is_set():
            try:
                packet, peer = worker.probe_socket.recvfrom(65535)
            except BlockingIOError:
                time.sleep(.001)
                continue
            at = time.time_ns()
            if peer[0] != worker.c['foreground_ip']:
                raise RuntimeError('unexpected application probe sender')
            reply = echo_reply(packet)
            worker.probe_socket.sendto(reply, peer)
            journal.write({'received_wall_ns': at, 'peer': peer,
                           'payload_bytes': len(packet), 'reply_bytes': len(reply)})
    except Exception as exc:
        worker.probe_error = str(exc)
        worker.state.update(error=str(exc), application_error=str(exc), application_traceback=traceback.format_exc())
        worker.stop.set()
    finally:
        if journal is not None:
            try:
                journal.close()
            except Exception as exc:
                worker.probe_error = worker.probe_error or str(exc)
                worker.state['application_error'] = worker.probe_error
                worker.state['error'] = worker.probe_error
                worker.stop.set()
