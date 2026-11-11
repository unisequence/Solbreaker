"""Cooperative Bullet construction on the simulation's owning event loop."""
import asyncio
from collections import deque
from dataclasses import dataclass
import time


@dataclass
class ArenaLoad:
    arena: object
    context: object
    started: float
    slices: int = 0
    max_slice: float = 0.
    work_seconds: float = 0.


class ArenaLoader:
    BUDGET = .002
    PAUSE = .001

    def __init__(self, validate, ready, failed):
        self.validate,self.ready,self.failed = validate,ready,failed
        self.jobs = {}
        self.order = deque()
        self.handle = None
        self.closed = False

    def begin(self,arena,context):
        if self.closed or arena.result_id in self.jobs:
            raise ValueError('Arena loader is closed or already loading this arena')
        job = ArenaLoad(arena,context,time.monotonic())
        self.jobs[arena.result_id] = job
        self.order.append(arena.result_id)
        self._schedule()
        return job

    def _schedule(self):
        if not self.closed and self.order and self.handle is None:
            self.handle = asyncio.get_running_loop().call_later(self.PAUSE,self._pump)

    def _pump(self):
        # One global budget, even if many rooms start together. Scheduling one
        # timer per job would let all their budgets accumulate before a tick.
        self.handle = None
        if self.closed or not self.order:
            return
        ident = self.order.popleft()
        job = self.jobs.get(ident)
        try:
            if job is not None:
                self._advance(job)
        finally:
            self._schedule()

    def _advance(self,job):
        if self.closed or self.jobs.get(job.arena.result_id) is not job:
            return
        start = time.monotonic()
        complete = False
        try:
            self.validate(job)
            if self.jobs.get(job.arena.result_id) is not job:
                return
            while not complete:
                complete = job.arena.advance_build()
                if time.monotonic()-start>=self.BUDGET:
                    break
        except Exception as exc:
            self.jobs.pop(job.arena.result_id,None)
            self.failed(job,str(exc))
            return
        elapsed = time.monotonic()-start
        job.slices += 1
        job.max_slice = max(job.max_slice,elapsed)
        job.work_seconds += elapsed
        if complete:
            self.jobs.pop(job.arena.result_id,None)
            self.ready(job)
        else:
            self.order.append(job.arena.result_id)

    def cancel(self,arena,reason):
        job = self.jobs.pop(arena.result_id,None)
        if job is not None:
            try:self.order.remove(arena.result_id)
            except ValueError:pass  # The current callback already removed it.
            if not self.order and self.handle is not None:
                self.handle.cancel()
                self.handle = None
            self.failed(job,reason)
        return job is not None

    def close(self):
        self.closed = True
        if self.handle is not None:
            self.handle.cancel()
            self.handle = None
        self.jobs.clear()
        self.order.clear()
