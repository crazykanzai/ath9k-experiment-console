"""Bounded asynchronous event journal. Never silently discards an event."""
import json
from pathlib import Path
import queue
import threading
import time


class EventJournal:
    def __init__(self, path, capacity=4096, flush_s=.25, opener=None):
        self.path = Path(path)
        self.queue = queue.Queue(maxsize=capacity)
        self.flush_s = flush_s
        self.done = threading.Event()
        self.error = None
        self.closed = False
        # Open synchronously: ownership/permissions errors precede generation.
        self.stream = opener() if opener else self.path.open('x', encoding='utf-8', buffering=65536)
        self.thread = threading.Thread(target=self.consume, daemon=True)
        self.thread.start()

    def write(self, event):
        if self.closed or self.error is not None:
            raise RuntimeError('event journal unavailable: '+str(self.error))
        try:
            self.queue.put_nowait(dict(event))
        except queue.Full as exc:
            # An overloaded logger invalidates collection; no drop and no claim
            # of successful performance. The collector still retains its events.
            raise RuntimeError('event journal capacity exhausted; collection incomplete') from exc

    def consume(self):
        next_flush = time.monotonic()+self.flush_s
        try:
            while not self.done.is_set() or not self.queue.empty():
                try:
                    row = self.queue.get(timeout=min(self.flush_s, .05))
                except queue.Empty:
                    row = None
                if row is not None:
                    self.stream.write(json.dumps(row, ensure_ascii=False)+'\n')
                if time.monotonic() >= next_flush:
                    self.stream.flush()
                    next_flush = time.monotonic()+self.flush_s
            self.stream.flush()
        except Exception as exc:
            self.error = str(exc)
        finally:
            try:
                self.stream.close()
            except Exception as exc:
                self.error = self.error or str(exc)

    def close(self, timeout=1.):
        self.closed = True
        self.done.set()
        self.thread.join(timeout)
        if self.thread.is_alive() or self.error is not None or not self.queue.empty():
            raise RuntimeError('event journal not fully persisted: '+str(self.error))
