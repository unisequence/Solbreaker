"""Per-arena command and event transport for the opt-in owner mode.

The endpoint owns authentication, session lifecycle and persistence.  This
adapter only serializes accepted commands and delivers their addressed output.
An unknown outcome or delivery error quarantines this arena; no replay occurs.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
import struct
import time

from . import diagnostics
from .arena_owner import Command, CommandRejected, MotionPayload, OwnerError, Result
from .arena_owner_registry import ArenaRoute, RouteFull
from .motion import tick_sync

_LOG = logging.getLogger(__name__)


def _consume(future):
    if not future.cancelled():
        future.exception()


class ArenaOwnerTransport:
    """Event-loop-confined bridge for a single registered arena route."""

    def __init__(self, endpoint, route: ArenaRoute, *, limit=64, on_result=None,
                 on_delivered=None, on_fault=None, simulation_debt=None, motion_tick=None):
        if type(limit) is not int or not 1 <= limit <= 1024:
            raise ValueError('invalid transport queue limit')
        self.endpoint = endpoint
        self.route = route
        self.arena_id = route.arena_id
        self.on_result = on_result
        self.on_delivered = on_delivered
        self.on_fault = on_fault
        self.simulation_debt = simulation_debt
        self.motion_tick = motion_tick
        self._queue = asyncio.Queue(maxsize=limit)
        self._fault = None
        # Result is retained on settlement/delivery failure; it must never be
        # obtained by replaying a possibly mutated command.
        self.undelivered_result = None
        self._closing = None
        self._ticker = None
        self._worker = asyncio.create_task(self._run())

    @property
    def fault(self):
        return self._fault

    def submit_nowait(self, command: Command):
        """Submit a trusted internal command; use submit_client for player input."""
        return self._enqueue(command, None)

    def _enqueue(self, command, origin):
        if self._fault is not None:
            raise OwnerError('arena transport unavailable') from self._fault
        if type(command) is not Command:
            raise ValueError('unsupported arena command')
        command.validate()
        try:
            sessions = self._sessions()
        except OwnerError as exc:
            self._begin_close(exc)
            raise
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(_consume)
        try:
            self._queue.put_nowait((command, future, sessions, origin,
                                    time.perf_counter_ns() if diagnostics.get_recorder() is not None else None))
        except asyncio.QueueFull as exc:
            self._begin_close(OwnerError('arena input queue overflow; command not accepted'))
            raise RouteFull('arena input queue full; command not accepted') from exc
        def cancelled(item):
            if item.cancelled():
                self._begin_close(OwnerError('arena request cancelled; outcome may be unknown'))
        future.add_done_callback(cancelled)
        return future

    def submit_client(self, address, client, command: Command):
        """Reject stale or mismatched sessions before the input enters FIFO."""
        if (not self._entitled(address, client) or type(command) is not Command
                or command.account_id != client.account_id or command.op in ('step', 'motions')):
            raise ValueError('session is not entitled to submit this arena command')
        return self._enqueue(command, (address, client))

    def _sessions(self):
        sessions = {}
        for address, client in list(self.endpoint.clients.items()):
            if (client.arena_id != self.arena_id or client.phase != 'battle'
                    or not client.account_created):
                continue
            if client.account_id in sessions:
                raise OwnerError('duplicate account sessions in arena')
            sessions[client.account_id] = (address, client)
        return sessions

    def _entitled(self, address, client):
        return (self.endpoint.clients.get(address) is client and client.arena_id == self.arena_id
                and client.phase == 'battle' and client.account_created)

    def _current(self, address, client):
        return (self._entitled(address, client) and client.battle_spawned)

    async def _deliver(self, command, result, sessions):
        if type(result) is not Result or result.arena_id != self.arena_id:
            raise OwnerError('invalid arena result')
        if result.settlement is not None and self.on_result is None:
            raise OwnerError('settlement handler is required before delivering result')
        attachments = {}
        if self.on_result is not None:
            maybe = self.on_result(command, result)
            if inspect.isawaitable(maybe):
                maybe = await maybe
            if maybe is not None:
                if type(maybe) is not dict or any(type(key) is not int or type(value) is not bytes
                                                 for key, value in maybe.items()):
                    raise OwnerError('invalid arena delivery attachments')
                attachments = maybe
        for event in result.events:
            session = sessions.get(event.account_id)
            if session is not None:
                address, client = session
                if self._entitled(address, client):
                    attachment = attachments.pop(event.account_id, b'')
                    # Resource data must precede ResetEntities in a leave result.
                    # Avatar owns the report subscription until that reset.
                    self.endpoint.send(address, client, attachment + event.body)
                    if attachment:
                        client.result_sent = True
        # A report can become visible when the postbattle timer expires on
        # a quiet step, with no new gameplay events to carry the resource.
        for account_id, body in attachments.items():
            session = sessions.get(account_id)
            if body and session is not None:
                address, client = session
                if self._entitled(address, client):
                    self.endpoint.send(address, client, body)
                    client.result_sent = True
        if command.op == 'step':
            recipients = {account: (address, client) for account, (address, client) in sessions.items()
                if self._current(address, client) and
                (self.motion_tick is None or self.motion_tick() != client.last_sync_tick)}
            if recipients:
                # One read of the immutable motion frame, rather than a
                # process round trip for every connected observer.
                payload = struct.pack(f'<{len(recipients)}I', *recipients)
                current = await self.route.request(Command('motions', time.monotonic(), 0, payload))
                motions = current.motions
                if (current.arena_id != self.arena_id or type(motions) is not tuple
                        or any(type(motion) is not MotionPayload for motion in motions)
                        or tuple(motion.account_id for motion in motions) != tuple(recipients)
                        or any((motion.tick is not None and type(motion.tick) is not int)
                               or type(motion.body) is not bytes for motion in motions)
                        or len({motion.tick for motion in motions}) > 1):
                    raise OwnerError('invalid arena motion batch')
                for motion in motions:
                    address, client = recipients[motion.account_id]
                    if not self._current(address, client):
                        continue
                    if motion.tick is not None and motion.tick != client.last_sync_tick:
                        self.endpoint.send(address, client, tick_sync(motion.tick) + motion.body, reliable=False)
                        client.last_sync_tick = motion.tick
        if self.on_delivered is not None:
            maybe = self.on_delivered(command, result)
            if inspect.isawaitable(maybe):
                await maybe

    async def _run(self):
        while True:
            command, future, sessions, origin, queued_at = await self._queue.get()
            recorder = diagnostics.get_recorder()
            begun = time.perf_counter_ns() if recorder is not None else None
            route_done = None
            outcome = 'rejected'
            try:
                if origin is not None and not self._entitled(*origin):
                    if not future.done():
                        future.set_exception(CommandRejected('session became stale before execution'))
                    continue
                try:
                    result = await self.route.request(command)
                except CommandRejected as exc:
                    if not future.done():
                        future.set_exception(exc)
                    continue
                route_done = time.perf_counter_ns() if recorder is not None else None
                # A rejection is safe only from owner preflight. Any error in
                # delivery/settlement follows a possibly mutated command.
                self.undelivered_result = (command, result)
                await self._deliver(command, result, sessions)
                self.undelivered_result = None
                outcome = 'delivered'
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                outcome = 'cancelled_unknown'
                if not future.done():
                    future.set_exception(OwnerError('transport closed; outcome may be unknown'))
                raise
            except Exception as exc:
                outcome = type(exc).__name__
                if not future.done():
                    future.set_exception(OwnerError('arena delivery failed; outcome may be unknown'))
                self._begin_close(OwnerError(
                    f'arena transport failed: {type(exc).__name__}: {str(exc)[:512]}'))
                return
            finally:
                if recorder is not None:
                    finished = time.perf_counter_ns()
                    recorder.record('owner_transport', arena_id=self.arena_id, operation=command.op,
                                    account=command.account_id, command_at=command.at, deadline=command.deadline,
                                    queue_wait_ns=begun-queued_at if queued_at is not None else None,
                                    route_wait_ns=route_done-begun if route_done is not None else None,
                                    delivery_ns=finished-route_done if route_done is not None else None,
                                    total_ns=finished-queued_at if queued_at is not None else None,
                                    queue_depth=self._queue.qsize(), sessions=len(sessions), outcome=outcome)
                self._queue.task_done()

    def start_ticker(self, interval, *, clock=time.monotonic, step_budget=.008):
        if self._fault is not None or self._ticker is not None:
            raise OwnerError('arena ticker unavailable')
        if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                or not math.isfinite(interval) or interval <= 0
                or isinstance(step_budget, bool) or not isinstance(step_budget, (int, float))
                or not math.isfinite(step_budget) or step_budget <= 0):
            raise ValueError('invalid ticker interval or step budget')
        async def run():
            next_at = clock() + interval
            while self._fault is None:
                await asyncio.sleep(max(0., next_at-clock()))
                now = clock()
                debt = self.simulation_debt() if self.simulation_debt is not None else 0.
                budget = max(step_budget, 1.5*interval) if debt > 2*interval else step_budget
                try:
                    # Only one timer request may be outstanding. Player input
                    # therefore waits behind at most one bounded step batch,
                    # rather than behind a growing queue of stale wall ticks.
                    await self.submit_nowait(Command('step', now, 0,
                                                     deadline=now+budget))
                except OwnerError:
                    return
                finished = clock()
                recorder = diagnostics.get_recorder()
                if recorder is not None:
                    recorder.record('owner_tick', arena_id=self.arena_id,
                                    scheduled_at=next_at, started_at=now, finished_at=finished,
                                    late_ns=max(0., now-next_at)*1e9,
                                    cycle_ns=(finished-now)*1e9, debt_seconds=debt, budget_seconds=budget)
                next_at = max(next_at+interval, finished)
        self._ticker = asyncio.create_task(run())

    def _begin_close(self, reason, *, fault=True):
        if self._closing is None:
            self._fault = reason
            if fault:
                _LOG.error('Arena %s transport quarantined: %s', self.arena_id, reason)
                if self.on_fault is not None:
                    try:
                        self.on_fault(self.arena_id, reason)
                    except Exception:
                        _LOG.exception('Arena %s fault callback failed', self.arena_id)
            self._closing = asyncio.create_task(self._shutdown())
        return self._closing

    async def _shutdown(self):
        if self._ticker is not None:
            self._ticker.cancel()
            try:
                await self._ticker
            except asyncio.CancelledError:
                pass
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        finally:
            while not self._queue.empty():
                _, future, _, _, _ = self._queue.get_nowait()
                if not future.done():
                    future.set_exception(OwnerError('transport closed before execution'))
                self._queue.task_done()
            await self.route.close()

    async def close(self):
        await asyncio.shield(self._begin_close(OwnerError('transport closed'), fault=False))
