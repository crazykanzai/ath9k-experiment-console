"""Reconcile only post-deadline closed channels with successful final receipts."""
import time


def heartbeat(session, monotonic=time.monotonic, wall_ns=time.time_ns, grace_s=2.):
    if monotonic()-session.last_heartbeat<1.5:return
    for role in list(session.workers):
        if role in session.finished:continue
        try:session.workers[role].send({'op':'heartbeat'})
        except (OSError,EOFError):
            t0=session.state.get('T0_ns')
            end=t0+sum(session.request[k] for k in ('warmup_s','measure_s','tail_s'))*10**9 if t0 else None
            if end is None or wall_ns()<end:raise
            deadline=monotonic()+grace_s
            while monotonic()<deadline:
                final=session.finished.get(role);code=session.state.get('process_exit_codes',{}).get(role)
                if final is not None and code==0:
                    if final.get('error') or not final.get('completed_schedule'):
                        raise RuntimeError(role+' closed without successful scheduled completion')
                    session.event('normal_finish_channel_closed','收尾心跳与完成回执、正常EOF核对通过',node=role)
                    break
                session.receive(timeout=.05,strict=True)
            else:raise RuntimeError(role+' closed without finished/clean EOF within grace')
    session.last_heartbeat=monotonic()


def await_clean_exits(session, timeout=3., monotonic=time.monotonic):
    deadline=monotonic()+timeout
    while set(session.state.get('process_exit_codes',{}))!=set(session.workers):
        if monotonic()>=deadline:raise RuntimeError('final SSH EOF receipts incomplete')
        session.receive(timeout=.05,strict=True)
    if any(code!=0 for code in session.state['process_exit_codes'].values()):
        raise RuntimeError('nonzero SSH worker exit code')
