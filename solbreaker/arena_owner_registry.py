"""Per-arena FIFO ownership for the production owner service.

One actor owns each AsyncArenaOwner.  Accepting a command transfers it to that
actor; cancelling a waiting caller quarantines the route because its outcome
may be unknown.  There is no automatic replay or recovery.
"""
from __future__ import annotations

import asyncio

from .arena_owner import AsyncArenaOwner, Command, CommandRejected, OwnerError, Result


class RouteFull(OwnerError):
    """The command was not enqueued; the route remains usable."""


def _observe_future(future: asyncio.Future) -> None:
    # A cancelled waiter can leave an internal future without a consumer.
    if not future.cancelled():
        future.exception()


class ArenaRoute:
    def __init__(self, arena_id: str, owner: AsyncArenaOwner, *, limit: int):
        if type(arena_id) is not str or not arena_id:
            raise ValueError('invalid arena id')
        if type(limit) is not int or not 1 <= limit <= 1024:
            raise ValueError('invalid arena queue limit')
        self.arena_id = arena_id
        self.owner = owner
        self._queue = asyncio.Queue(maxsize=limit)
        self._fault: OwnerError | None = None
        self._closing: asyncio.Task | None = None
        self._runner = asyncio.create_task(self._run())

    @property
    def fault(self) -> OwnerError | None:
        return self._fault

    async def request(self, command: Command) -> Result:
        if self._fault is not None:
            raise OwnerError('arena route is unavailable') from self._fault
        if type(command) is not Command:
            raise ValueError('unsupported message')
        command.validate()
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(_observe_future)
        try:
            self._queue.put_nowait((command, future))
        except asyncio.QueueFull as exc:
            raise RouteFull('arena queue is full; command not accepted') from exc
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Even if execution has not yet begun, callers may not rely on its
            # outcome after cancellation.  Quarantine; never replay the input.
            self._begin_close(OwnerError('arena request cancelled; outcome may be unknown'))
            raise

    async def _run(self) -> None:
        while True:
            command, future = await self._queue.get()
            try:
                try:
                    result = await self.owner.apply(command)
                    if type(result) is not Result or result.arena_id != self.arena_id:
                        raise OwnerError('owner returned mismatched arena identity')
                except CommandRejected as exc:
                    if not future.done():
                        future.set_exception(exc)
                    continue
                except Exception as exc:
                    failure = OwnerError(
                        f'arena command failed; outcome unknown: {type(exc).__name__}: {str(exc)[:512]}')
                    if not future.done():
                        future.set_exception(failure)
                    self._begin_close(failure)
                    return
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                if not future.done():
                    future.set_exception(OwnerError('arena closed; command outcome may be unknown'))
                raise
            finally:
                self._queue.task_done()

    def _begin_close(self, reason: OwnerError) -> asyncio.Task:
        if self._closing is None:
            self._fault = reason
            self._closing = asyncio.create_task(self._shutdown())
        return self._closing

    async def _shutdown(self) -> None:
        self._runner.cancel()
        try:
            await self._runner
        except asyncio.CancelledError:
            pass
        finally:
            while not self._queue.empty():
                _, future = self._queue.get_nowait()
                if not future.done():
                    future.set_exception(OwnerError('arena route closed before command execution'))
                self._queue.task_done()
            await self.owner.close()

    async def close(self) -> None:
        await asyncio.shield(self._begin_close(OwnerError('arena route closed')))


class ArenaOwnerRegistry:
    """Event-loop-confined routes; registry does not construct arenas or own IPC policy."""
    def __init__(self, *, queue_limit: int = 64):
        if type(queue_limit) is not int or not 1 <= queue_limit <= 1024:
            raise ValueError('invalid arena queue limit')
        self._limit = queue_limit
        self._routes: dict[str, ArenaRoute] = {}
        self._closing: asyncio.Task | None = None

    def register(self, arena_id: str, owner: AsyncArenaOwner) -> ArenaRoute:
        if self._closing is not None:
            raise OwnerError('registry is closing')
        if arena_id in self._routes:
            raise ValueError('arena already registered')
        route = ArenaRoute(arena_id, owner, limit=self._limit)
        self._routes[arena_id] = route
        return route

    def route(self, arena_id: str) -> ArenaRoute:
        try:
            return self._routes[arena_id]
        except KeyError as exc:
            raise OwnerError('unknown arena') from exc

    async def remove(self, arena_id: str) -> None:
        route = self.route(arena_id)
        # Keep the id reserved until shutdown has actually completed, even if
        # this caller is cancelled.  A new generation cannot race the old one.
        task = route._begin_close(OwnerError('arena route removed'))
        def forget(_):
            if self._routes.get(arena_id) is route:
                self._routes.pop(arena_id, None)
        task.add_done_callback(forget)
        await asyncio.shield(task)

    async def close(self) -> None:
        if self._closing is None:
            async def close_all():
                routes = tuple(self._routes.values())
                results = await asyncio.gather(*(route.close() for route in routes), return_exceptions=True)
                self._routes.clear()
                if any(isinstance(result, BaseException) for result in results):
                    raise OwnerError('one or more arena owners failed to close')
            self._closing = asyncio.create_task(close_all())
        await asyncio.shield(self._closing)
