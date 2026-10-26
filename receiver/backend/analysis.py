"""Dedicated, bounded analysis workers. Never execute FFT on the collector."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from fastapi import HTTPException
from fastapi.responses import JSONResponse


class AnalysisWorker:
    def __init__(self, name, workers=1, capacity=32):
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self.capacity = capacity
        self.pending = set()
        self.lock = Lock()
        self.closed = False

    async def run(self, fn):
        with self.lock:
            if self.closed or len(self.pending) >= self.capacity:
                raise HTTPException(503, 'Display analysis busy; collection continues', headers={'Retry-After': '1'})
            job = self.executor.submit(fn)
            self.pending.add(job)
        job.add_done_callback(self._finished)
        # Cancellation can cancel queued work. Running work retains its slot until
        # actually finished, so aborted browser requests cannot grow the queue.
        return await asyncio.wrap_future(job)

    def _finished(self, job):
        with self.lock:
            self.pending.discard(job)

    def status(self):
        with self.lock:
            return {'pending': len(self.pending), 'capacity': self.capacity}

    async def close(self):
        with self.lock:
            self.closed = True
        await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)


def live_response(store, receiver=None):
    snapshot = store.view().snapshot()
    if receiver is not None:
        snapshot['receiver'] = receiver
    return JSONResponse(snapshot)


def frame_response(store, kind, device, *args):
    try:
        # Small copy under the node lock; calculation AND JSON encoding occur on
        # an independent worker after releasing the collector's buffer lock.
        window_ms = args[1]
        view = store.view(device, window_ms)
        frame = getattr(view, kind)(device, *args)
    except KeyError:
        raise HTTPException(404, 'No connection received from this node')
    if not store.current_frame(frame):
        frame['online'] = False
        frame['source_samples'] = 0
        if kind == 'spectrum':
            frame['frequencies'] = []
            frame['amplitudes'] = []
        else:
            frame['channels'] = {}
    return JSONResponse(frame)
