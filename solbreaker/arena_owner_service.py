"""Production owner lifecycle around the serializable arena command boundary."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import pickle
import sqlite3
import struct
import time

from .arena_loading import ArenaLoad
from .arena_owner import AsyncArenaOwner, Command, CommandRejected, OwnerError
from .arena_owner_game import GameArenaFactory
from .arena_owner_registry import ArenaOwnerRegistry
from .arena_owner_settlement import commit
from .arena_owner_spawn import OwnerSpawnManager
from .arena_owner_transport import ArenaOwnerTransport
from .motion import SIMULATION_INTERVAL


# A full bot battle builds colliders, visibility and navigation before READY.
ARENA_STARTUP_TIMEOUT = 120.0

_INPUT_OPS = {
    135: 'ready', 145: 'control', 149: 'aim', 16: 'aim_entity',
    17: 'aim_entity_local', 138: 'fire', 13: 'autoaim',
    18: 'autoaim_point', 148: 'aim_angles', 20: 'consumable',
    147: 'select_shell',
}


def _consume(task):
    if not task.cancelled():
        task.exception()


@dataclass
class OwnerBattle:
    arena: object
    selected: tuple
    factory: GameArenaFactory
    started: float
    task: asyncio.Task | None = None
    owner: AsyncArenaOwner | None = None
    route: object | None = None
    transport: ArenaOwnerTransport | None = None
    spawner: OwnerSpawnManager | None = None
    closing: bool = False
    cleanup: asyncio.Task | None = None
    close_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    recon_tasks: dict = field(default_factory=dict)


class ArenaOwnerService:
    """Event-loop-confined supervisor; one child owns each authoritative world."""

    def __init__(self, endpoint, *, queue_limit=64, timeout=45.):
        self.endpoint = endpoint
        self.registry = ArenaOwnerRegistry(queue_limit=queue_limit)
        self.timeout = timeout
        self.jobs = {}
        self._closing = None

    def make_factory(self, arena, accounts, definitions, scenes, training):
        ids = tuple(account['id'] for account in accounts)
        if set(definitions) != set(ids) or set(scenes) != set(ids):
            raise ValueError('owner recipe does not match the arena roster')
        first = definitions[ids[0]]
        settings = []
        for account_id in ids:
            scene = scenes[account_id]
            vehicle = arena.vehicle_for(account_id)
            vehicle.definition = definitions[account_id]
            vehicle.max_health = vehicle.health = scene.armor.health
            settings.append((account_id, {
                name: getattr(scene, name, None)
                for name in ('internal_geometry', 'engine_spec', 'ammo_rack_spec',
                             'fuel_tank_spec', 'fire_policy', 'fire_min_damage',
                             'crew_specs', 'crew_bindings')
            }))
        return GameArenaFactory(
            self.endpoint.game, first.nation, first.name, arena.map_spec.key,
            tuple(accounts), arena.epoch, solo=arena.solo,
            definition=first, training=training, map_spec=arena.map_spec,
            player_definitions=tuple((account_id, definitions[account_id]) for account_id in ids),
            scene_settings=tuple(settings), arena_id=arena.result_id,
            space_id=arena.space_id, entity_base=arena.entity_base,
            recon_enabled=getattr(self.endpoint.server, 'recon_enabled', False),compact_inputs=True,
            bot_roster=arena.bot_roster, bot_scene_settings=arena.bot_scene_settings,
            prebattle_seconds=arena.prebattle_seconds)

    def start(self, arena, selected, factory):
        if arena.result_id in self.jobs or self._closing is not None:
            raise OwnerError('arena owner service is unavailable')
        arena.owner_backed = True
        arena.building = True
        arena.recon_client_aim = lambda account_id, target: self.recon_aim(arena, account_id, target)
        arena.recon_client_clear = lambda account_id: self.recon_clear(arena, account_id)
        for addr, client in selected:
            client.arena_id, client.phase = arena.result_id, 'battle_building'
            client.battle_spawned = False
            client.battle_hints_at = 0.
            client.result_sent = False
            client.reconnect_replay = False
            client.owner_leave_pending = False
        job = OwnerBattle(arena, tuple(selected), factory, time.monotonic())
        self.jobs[arena.result_id] = job
        job.task = asyncio.create_task(self._load(job))
        job.task.add_done_callback(_consume)
        return job.task

    async def _load(self, job):
        arena = job.arena
        try:
            job.owner = await AsyncArenaOwner.create(job.factory, timeout=self.timeout,
                                                     startup_timeout=ARENA_STARTUP_TIMEOUT)
            job.route = self.registry.register(arena.result_id, job.owner)
            account_id = job.factory.accounts[0]['id']
            initial = await job.route.request(Command('motion', time.monotonic(), account_id))
            self._observe(job, initial)
            for addr, client in job.selected:
                if (self.endpoint.clients.get(addr) is not client
                        or client.arena_id != arena.result_id or client.phase != 'battle_building'
                        or not client.account_created):
                    raise OwnerError('participant session changed during owner startup')
                if client.reliable_window_used >= 250:
                    raise OwnerError('participant channel is stalled')
            job.spawner = OwnerSpawnManager(
                self.endpoint, self.registry,
                on_failure=self._spawn_failed)
            job.transport = ArenaOwnerTransport(
                self.endpoint, job.route,
                on_result=lambda command, result: self._on_result(job, command, result),
                on_delivered=lambda command, result: self._on_delivered(job, command, result),
                on_fault=lambda arena_id, error: self._on_fault(job, error),
                simulation_debt=lambda: arena.accumulator,
                motion_tick=lambda: arena.owner_motion_tick)
            arena.building = False
            for addr, client in job.selected:
                client.queue_pending = False
                client.phase = 'battle_pending'
                client.load_grace_until = time.monotonic() + self.endpoint.ENTITY_LOAD_GRACE
            job.transport.start_ticker(SIMULATION_INTERVAL,
                                       step_budget=self.endpoint.SIMULATION_BUDGET)
            for addr, client in job.selected:
                self.endpoint.send(addr, client, b'\x04\0')
            self.endpoint.server.event(
                'arena_world_ready', arena_id=arena.result_id, owner=True,
                duration_ms=(time.monotonic()-job.started)*1000)
        except asyncio.CancelledError:
            await self._close_owner(job)
            raise
        except Exception as exc:
            await self._close_owner(job)
            self.jobs.pop(arena.result_id, None)
            if self.endpoint.arenas.get(arena.result_id) is arena:
                self.endpoint.server.event('arena_owner_start_failed',
                                           arena_id=arena.result_id, reason=str(exc))
                self.endpoint.fail_load(ArenaLoad(arena, job.selected, job.started),
                                        'Arena owner failed to initialize')

    async def _close_owner(self, job):
        async with job.close_lock:
            tasks = tuple(job.recon_tasks.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            job.recon_tasks.clear()
            if job.spawner is not None:
                await job.spawner.close()
                job.spawner = None
            if job.transport is not None:
                await job.transport.close()
                job.transport = None
            elif job.owner is not None:
                await job.owner.close()
            if job.route is not None:
                try:
                    await self.registry.remove(job.arena.result_id)
                except OwnerError:
                    pass
                job.route = None
            job.owner = None

    def _observe(self, job, result):
        arena = job.arena
        state = pickle.loads(result.state)
        if (result.arena_id != arena.result_id or state.get('version') not in (1,2)
                or state.get('arena_id') != arena.result_id
                or state['step'] < arena.step_count):
            raise OwnerError('invalid or stale arena observation')
        if state['version']==2:
            account=state.get('account_id')
            flags=state.get('flags')
            headers=('epoch','started','prebattle_end','simulation_time','accumulator','dropped_time',
                     'finished_at','finish_reason','winner')
            if (type(account) is not int or account not in arena.players
                    or state['step']!=arena.step_count or state['game_events']
                    or state['motion_tick']!=getattr(arena,'owner_motion_tick',None)
                    or any(state[key]!=getattr(arena,key) for key in headers)
                    or tuple(row[0] for row in state['players'])!=tuple(sorted(arena.players))
                    or tuple(row[0] for row in state['vehicles'])!=(arena.players[account].vehicle_id,)
                    or type(flags) is not tuple
                    or any(type(row) is not tuple or len(row)!=2 for row in flags)
                    or tuple(row[0] for row in flags)!=tuple(sorted(arena.vehicles))
                    or any(type(row[1]) is not int or not 0<=row[1]<=15 for row in flags)
                    or dict(flags)[arena.players[account].vehicle_id]!=state['vehicles'][0][5]):
                raise OwnerError('invalid compact arena observation')
        for name in ('epoch', 'started', 'simulation_time', 'accumulator',
                     'dropped_time', 'finished_at', 'finish_reason', 'winner'):
            setattr(arena, name, state[name])
        arena.prebattle_end = state.get('prebattle_end', 0.)
        arena.step_count = state['step']
        arena.owner_motion_tick = state['motion_tick']
        previous_worker_state = getattr(arena, 'owner_simulation', {}).get('worker_state')
        arena.owner_simulation = state['simulation']
        worker_state = arena.owner_simulation['worker_state']
        if worker_state != previous_worker_state and worker_state in ('ready', 'failed', 'closed'):
            details = {}
            if worker_state == 'failed':
                details['worker_stats'] = arena.owner_simulation.get('parallel_moves', {})
            self.endpoint.server.event('arena_drive_workers_'+worker_state,
                arena_id=arena.result_id,
                worker_processes=arena.owner_simulation['worker_processes'], **details)
        for account_id, attached, loaded, reason in state['players']:
            player = arena.players[account_id]
            player.attached, player.loaded, player.departure_reason = attached, loaded, reason
        for row in state['vehicles']:
            (entity_id, pose, speed, health, max_health, flags, shots, hits,
             damage, ammo, clip, shell, reload_at, reload_total, engine,
             ammo_by_shell, gun_yaw, gun_pitch) = row
            vehicle = arena.vehicles[entity_id]
            vehicle.pose, vehicle.speed = pose, speed
            vehicle.health, vehicle.max_health, vehicle.flags = health, max_health, flags
            vehicle.shots, vehicle.hits, vehicle.damage = shots, hits, damage
            vehicle.ammo, vehicle.clip, vehicle.selected_shell = ammo, clip, shell
            vehicle.reload_at, vehicle.reload_total, vehicle.engine_mode = reload_at, reload_total, engine
            vehicle.ammo_by_shell = dict(ammo_by_shell)
            vehicle.gun.yaw, vehicle.gun.pitch = gun_yaw, gun_pitch
        if state['version']==2:
            for entity_id,flags in state['flags']:
                arena.vehicles[entity_id].flags=flags
        else:
            capture = getattr(arena, 'base_capture', None)
            view = state.get('base_capture')
            if capture is not None and view is not None:
                points, capture.blocked = view
                capture.totals = tuple(points)
            supremacy = getattr(arena, 'supremacy', None)
            if supremacy is not None and state.get('supremacy') is not None:
                supremacy.observe(state['supremacy'])
            for entity, points, defense in state.get('capture_statistics', ()):
                vehicle = arena.vehicles[entity]
                vehicle.capture_points, vehicle.defense_points = points, defense
        events = state['game_events']
        if events:
            self.endpoint.log_arena_events(arena, events)

    def _on_result(self, job, command, result):
        if command.op != 'motion_light':
            self._observe(job, result)
        arena = job.arena
        attachments = {}
        if result.settlement is not None:
            pending = {item.account_id for item in result.settlement.results} - arena.recorded_accounts
            if pending:
                try:
                    reports = commit(self.endpoint, result.settlement)
                except (ValueError, sqlite3.Error) as exc:
                    self.endpoint.server.event('battle_settlement_failed',
                        arena_id=arena.result_id, reason=str(exc), owner=True)
                    return attachments
                for account_id, report in reports.items():
                    arena.reports[account_id] = report
                    if account_id not in arena.recorded_accounts:
                        arena.recorded_accounts.add(account_id)
                        self.endpoint.server.event('training_finished',
                            account_id=account_id, result=report,
                            simulation_steps=arena.step_count,
                            dropped_seconds=arena.dropped_time)
            if arena.finished_at:
                self.endpoint.rooms.sync()
            from .results import avatar_resource
            recipients = {client.account_id for client in self.endpoint.clients.values()
                          if client.arena_id == arena.result_id and not client.result_sent
                          and (arena.postbattle_complete or
                               command.op == 'leave' and command.account_id == client.account_id)}
            for account_id, report in arena.reports.items():
                if account_id in recipients:
                    attachments[account_id] = avatar_resource(report)
        return attachments

    def _on_delivered(self, job, command, result):
        arena = job.arena
        if command.op == 'leave':
            for addr, client in list(self.endpoint.clients.items()):
                if (self.endpoint.clients.get(addr) is client
                        and client.arena_id == arena.result_id
                        and client.account_id == command.account_id):
                    client.owner_leave_pending = False
                    client.arena_id = None
                    client.arena_spawn_at = 0
                    client.battle_spawned = False
                    client.battle_hints_at = 0.
                    client.reconnect_replay = False
                    client.phase = 'account_pending'
                    client.load_grace_until = time.monotonic() + self.endpoint.ENTITY_LOAD_GRACE
                    client.clock_start = time.monotonic()
                    client.last_sync_tick = -1
                    self.endpoint.send(addr, client, b'\x04\0')
        if not any(player.attached for player in arena.players.values()):
            self.endpoint.arenas.remove_if_empty(arena)
            if self.endpoint.arenas.get(arena.result_id) is not arena:
                self._schedule_close(job)

    def _schedule_close(self, job):
        if job.cleanup is None:
            async def cleanup():
                await self._close_owner(job)
                self.jobs.pop(job.arena.result_id, None)
            job.cleanup = asyncio.create_task(cleanup())
            job.cleanup.add_done_callback(_consume)
        return job.cleanup

    def _on_fault(self, job, error):
        arena = job.arena
        job.closing = True
        if self.endpoint.arenas.get(arena.result_id) is arena:
            self.endpoint._quarantine_arena(arena, 'arena_owner', error, time.monotonic())
        self._schedule_close(job)

    def _spawn_failed(self, addr, client, arena_id, error):
        job = self.jobs.get(arena_id)
        if job is not None:
            self._on_fault(job, OwnerError(f'arena spawn failed: {type(error).__name__}'))

    def submit(self, addr, client, kind, body):
        arena = self.endpoint.arenas.get(client.arena_id)
        job = self.jobs.get(arena.result_id) if arena is not None else None
        if job is None or job.transport is None or job.closing:
            raise OwnerError('arena owner is unavailable')
        operation = _INPUT_OPS.get(kind)
        if operation is None:
            return None
        return job.transport.submit_client(
            addr, client, Command(operation, time.monotonic(), client.account_id, bytes(body)))

    def leave(self, addr, client):
        job = self.jobs.get(client.arena_id)
        if job is None or job.transport is None:
            raise OwnerError('arena owner is unavailable')
        return job.transport.submit_client(
            addr, client, Command('leave', time.monotonic(), client.account_id))

    def disconnect(self, arena, account_id):
        job = self.jobs.get(arena.result_id)
        if job is None or job.transport is None or job.closing:
            return None
        return job.transport.submit_nowait(Command('disconnect', time.monotonic(), account_id))

    def stop_control(self, arena, account_id):
        job = self.jobs.get(arena.result_id)
        if job is not None and job.transport is not None and not job.closing:
            return job.transport.submit_nowait(
                Command('control', time.monotonic(), account_id, b'\0\0'))
        return None

    def recon_aim(self, arena, account_id, target):
        job = self.jobs.get(arena.result_id)
        if job is None or job.transport is None or job.closing:
            raise OwnerError('arena owner is unavailable')
        return job.transport.submit_nowait(Command(
            'recon_aim', time.monotonic(), account_id, struct.pack('<I', target or 0)))

    def recon_clear(self, arena, account_id):
        job = self.jobs.get(arena.result_id)
        if job is None or job.transport is None or job.closing:
            return None
        return job.transport.submit_nowait(Command('recon_clear', time.monotonic(), account_id))

    def recon_poll(self, addr, client, delivery):
        job = self.jobs.get(client.arena_id)
        if job is None or job.route is None or job.closing or client.account_id in job.recon_tasks:
            return
        task = asyncio.create_task(self._recon_poll(job, addr, client, delivery))
        job.recon_tasks[client.account_id] = task
        def done(completed):
            if job.recon_tasks.get(client.account_id) is completed:
                job.recon_tasks.pop(client.account_id, None)
            _consume(completed)
        task.add_done_callback(done)

    async def _recon_poll(self, job, addr, client, delivery):
        from .recon_binding import BindingUnavailable
        from .mercury import variable8
        from .recon_transport import METHOD
        from .recon_wire import decode
        bindings = self.endpoint.recon_transport.bindings
        try:
            sequence = bindings.next_sequence(delivery.token)
            result = await job.route.request(Command(
                'recon_snapshot', time.monotonic(), client.account_id,
                struct.pack('<I', sequence)))
            if result.arena_id != job.arena.result_id or type(result.recon) is not bytes:
                raise OwnerError('invalid owner recon response')
            bound = bindings.resolve(delivery.token)
            if bound.client is not client or bound.arena is not job.arena:
                return
            snapshot = decode(result.recon)
            state = snapshot.lamp, snapshot.markers
            now = time.monotonic()
            if state != delivery.state or now-delivery.last_sent >= 1.:
                self.endpoint.send(addr, client, b'\x13' + variable8(METHOD, result.recon))
                delivery.state, delivery.last_sent = state, now
        except (BindingUnavailable, CommandRejected):
            return
        except Exception as exc:
            self.endpoint.server.event('arena_owner_recon_failed',
                arena_id=job.arena.result_id, account_id=client.account_id,
                reason=str(exc))
            if isinstance(exc, OwnerError):
                self._on_fault(job, exc)
            else:
                self.endpoint.recon_transport.revoke(client)

    def spawn(self, addr, client):
        job = self.jobs.get(client.arena_id)
        if job is None or job.spawner is None:
            raise OwnerError('arena owner is unavailable')
        return job.spawner.start(addr, client)

    def cancel(self, arena, reason):
        job = self.jobs.get(arena.result_id)
        if job is None or job.closing:
            return
        job.closing = True
        if job.task is not None and not job.task.done():
            job.task.cancel()
        async def cancel_job():
            if job.task is not None:
                try:
                    await job.task
                except (asyncio.CancelledError, Exception):
                    pass
            await self._close_owner(job)
            self.jobs.pop(arena.result_id, None)
            if self.endpoint.arenas.get(arena.result_id) is arena:
                self.endpoint.fail_load(ArenaLoad(arena, job.selected, job.started), reason)
        job.cleanup = asyncio.create_task(cancel_job())
        job.cleanup.add_done_callback(_consume)

    async def close(self):
        if self._closing is None:
            async def close_all():
                jobs = tuple(self.jobs.values())
                for job in jobs:
                    job.closing = True
                    if job.task is not None and not job.task.done():
                        job.task.cancel()
                await asyncio.gather(*(self._close_owner(job) for job in jobs),
                                     return_exceptions=True)
                self.jobs.clear()
                await self.registry.close()
            self._closing = asyncio.create_task(close_all())
        await asyncio.shield(self._closing)
