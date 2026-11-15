"""Bounded normal-finish race handling; early disconnects still fail."""
import time


def heartbeat(session, monotonic=time.monotonic, wall_ns=time.time_ns, grace_s=2.):
    if monotonic()-session.last_heartbeat < 1.5:
        return
    for role in list(session.workers):
        if role in session.finished:
            continue
        try:
            session.workers[role].send({'op': 'heartbeat'})
        except (OSError, EOFError):
            t0 = session.state.get('T0_ns')
            end = t0+sum(session.request[k] for k in ('warmup_s','measure_s','tail_s'))*10**9 if t0 else None
            if end is None or wall_ns() < end:
                raise  # A closed channel during work is NEVER normal completion.
            deadline = monotonic()+grace_s
            while monotonic() < deadline:
                receipt = session.finished.get(role)
                code = session.state.get('process_exit_codes', {}).get(role)
                if receipt is not None and code == 0:
                    if receipt.get('error') or not receipt.get('completed_schedule'):
                        raise RuntimeError(role+' closed without successful scheduled completion')
                    session.event('normal_finish_channel_closed',
                                  'late heartbeat reconciled with finished and clean EOF', node=role)
                    break
                session.receive(timeout=.05, strict=True)
            else:
                raise RuntimeError(role+' closed channel without finished/clean EOF within grace')
    session.last_heartbeat = monotonic()


def await_clean_exits(session, timeout=3., monotonic=time.monotonic):
    deadline = monotonic()+timeout
    while set(session.state.get('process_exit_codes', {})) != set(session.workers):
        if monotonic() >= deadline:
            raise RuntimeError('final SSH EOF receipts incomplete')
        session.receive(timeout=.05, strict=True)
    if any(code != 0 for code in session.state['process_exit_codes'].values()):
        raise RuntimeError('nonzero SSH worker exit code')
