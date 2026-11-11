"""Per-session owner spawn; not connected to the production endpoint yet.

The caller owns arena/session lifecycle and fault handling. A reconnect replay
requires extra owner-only state and is deliberately not supported here.
"""
from __future__ import annotations

import asyncio
import inspect
import math
import struct
import time

from .arena_owner import Command, SpawnData
from .motion import UPDATE_HZ, game_tick


def avatar_initial(data: SpawnData, now: float) -> bytes:
    if type(data) is not SpawnData:
        raise ValueError('expected spawn data')
    return b'\x02' + bytes([UPDATE_HZ]) + b'\x03' + struct.pack('<I', game_tick(now, data.epoch)) + data.initial


class OwnerSpawnManager:
    """Track separate pending spawns; no ownership of the underlying routes.

    Failure callback is mandatory: the endpoint must quarantine/disconnect on
    partial sends or an unknown owner outcome. No retry is performed here.
    """
    def __init__(self, endpoint, registry, *, on_failure, on_spawn=None,
                 delay=2.0, clock=time.monotonic):
        if (not callable(on_failure) or not callable(clock) or
                (on_spawn is not None and not callable(on_spawn)) or
                isinstance(delay, bool) or not isinstance(delay, (int, float)) or
                not math.isfinite(delay) or delay < 0):
            raise ValueError('invalid spawn manager configuration')
        self.endpoint = endpoint
        self.registry = registry
        self.on_failure = on_failure
        self.on_spawn = on_spawn
        self.delay = delay
        self.clock = clock
        self._tasks = {}
        self._closing = False

    def _current(self, route, addr, client, arena_id, account_id, phase):
        try:
            current_route = self.registry.route(arena_id)
        except Exception:
            return False
        return (current_route is route and self.endpoint.clients.get(addr) is client and
                client.arena_id == arena_id and client.account_id == account_id and
                client.phase == phase)

    def start(self, addr, client):
        if self._closing or self.endpoint.clients.get(addr) is not client:
            raise ValueError('spawn session unavailable')
        if client.phase != 'battle_pending' or not client.arena_id:
            raise ValueError('unsupported spawn session')
        if addr in self._tasks:
            raise ValueError('spawn already pending for address')
        arena_id, account_id = client.arena_id, client.account_id
        route = self.registry.route(arena_id)  # fail before mutating the session
        asyncio.get_running_loop()  # do not alter phase when called outside a loop
        client.phase = 'battle_owner_spawn'
        task = asyncio.create_task(self._spawn(route, addr, client, arena_id, account_id))
        self._tasks[addr] = task
        def done(future):
            if self._tasks.get(addr) is future:
                self._tasks.pop(addr, None)
        task.add_done_callback(done)
        return task

    async def _spawn(self, route, addr, client, arena_id, account_id):
        try:
            if not self._current(route, addr, client, arena_id, account_id, 'battle_owner_spawn'):
                return
            operation = 'reconnect' if client.reconnect_replay else 'spawn'
            result = await route.request(Command(operation, self.clock(), account_id))
            if not self._current(route, addr, client, arena_id, account_id, 'battle_owner_spawn'):
                return
            data = result.spawn
            if (result.arena_id != arena_id or type(data) is not SpawnData or
                    data.arena_id != arena_id or data.account_id != account_id):
                raise ValueError('spawn response identity mismatch')
            now = self.clock()
            client.phase = 'battle'
            client.last_sync_tick = -1
            client.load_grace_until = now + self.endpoint.ENTITY_LOAD_GRACE
            self.endpoint.send(addr, client, avatar_initial(data, now))
            await asyncio.sleep(self.delay)
            if not self._current(route, addr, client, arena_id, account_id, 'battle'):
                return
            required = sum((len(batch) + 899) // 900 for batch in data.batches)
            if client.reliable_window_used + required > 256:
                from .base import ChannelOverflow
                raise ChannelOverflow('Reliable spawn window exceeded')
            for batch in data.batches:
                self.endpoint.send(addr, client, batch)
            client.battle_spawned = True
            client.reconnect_replay = False
            client.battle_hints_at = self.clock() + 4.
            if self.on_spawn is not None:
                value = self.on_spawn(addr, client, arena_id)
                if inspect.isawaitable(value):
                    await value
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The callback decides whether the session still belongs to this
            # generation, and handles partial delivery/unknown outcomes.
            value = self.on_failure(addr, client, arena_id, exc)
            if inspect.isawaitable(value):
                await value

    async def close(self):
        self._closing = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
