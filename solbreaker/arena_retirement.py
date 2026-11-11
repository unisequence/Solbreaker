"""Cooperative release of detached worlds on their owning event loop."""
import asyncio
from collections import deque
import time


class ArenaRetirement:
    BUDGET = .002
    PAUSE = .001

    def __init__(self, failed=lambda arena,reason:None):
        self.failed = failed
        self.jobs = {}
        self.order = deque()
        self.failures = {}
        self.handle = None
        self.closed = False
        self.max_slice = 0.

    def begin(self, arena):
        if arena.closed:
            return
        if self.closed or arena.world is None:
            arena.close()
            return
        if arena.result_id not in self.jobs:
            arena.retiring = True
            self.jobs[arena.result_id] = arena
            self.order.append(arena.result_id)
            self._schedule()

    def _schedule(self):
        if not self.closed and self.order and self.handle is None:
            self.handle = asyncio.get_running_loop().call_later(self.PAUSE,self._pump)

    def _pump(self):
        self.handle = None
        if self.closed or not self.order:
            return
        ident = self.order.popleft()
        arena = self.jobs[ident]
        start = time.monotonic()
        try:
            complete = False
            while not complete:
                complete = arena.advance_close()
                if time.monotonic()-start>=self.BUDGET:
                    break
            if complete:
                del self.jobs[ident]
            else:
                self.order.append(ident)
        except Exception as exc:
            # Preserve ownership and expose the failure. Shutdown retries this
            # world synchronously; a failed cleanup is never reported as freed.
            self.failures[ident] = str(exc)
            self.failed(arena,str(exc))
        finally:
            self.max_slice = max(self.max_slice,time.monotonic()-start)
            self._schedule()

    def close(self):
        self.closed = True
        if self.handle is not None:
            self.handle.cancel()
            self.handle = None
        errors = []
        for ident,arena in list(self.jobs.items()):
            try:
                arena.close()
            except Exception as exc:
                errors.append(exc)
            else:
                del self.jobs[ident]
                self.failures.pop(ident,None)
        self.order.clear()
        if errors:
            raise errors[0]
