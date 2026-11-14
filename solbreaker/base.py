"""Native BaseApp handshake for the pinned compatible build."""
import asyncio
from dataclasses import dataclass, field, asdict
import secrets
import sqlite3
import struct
import time
import json
import math
import os
import traceback
from collections import deque
from functools import cached_property
from pathlib import Path

from . import diagnostics
from .cipher import PacketCipher, encrypt_packet, decrypt_packet
from .mercury import parse_packet, base_elements, encode_reply, encode_packet, RELIABLE, ReliableInbox
from .account import create_account, show_gui, account_method, pickle_document, command_header, command_response, local_http_response, resource_response
from .account import spa_token_response
from .account import random_queue_message
from .account import account_update, account_sync_response
from .mercury import packed_blob, read_blob
from .profile import account_snapshot
from .catalog import synchronized_catalog, maps_configuration
from .battle import (initial_messages, spawn_batches, reconnect_batches, motion_messages, event_messages,
                     dispatch_input, battle_hints_message, ammo_message, avatar_method)
from .arena import ArenaManager
from .vehicle_data import VehicleConfiguration
from .terrain import MapTerrain
from .map_catalog import WEATHER_MAPS, load_map_spec, training_map_catalog
from .dev_map import dev_map_specs_for_lab
from .motion import UPDATE_HZ, SIMULATION_INTERVAL, game_tick, tick_sync


class BattleRefused(ValueError):
    """The selected vehicle or arena cannot be prepared for a battle request."""


class ChannelOverflow(ValueError):
    """A stalled reliable channel must be disconnected, not silently skipped."""


class ChannelTimeout(ValueError):
    """Dropping a reliable packet would leave an unrecoverable sequence hole."""


@dataclass
class ClientSession:
    key: bytes
    session: int
    last_seen: float = field(default_factory=time.monotonic)
    last_sent: float = field(default_factory=time.monotonic)
    last_reliable_sent: float = field(default_factory=time.monotonic)
    last_sync_tick: int = -1
    sequence: int = 0
    unreliable_sequence: int = 0
    inbox: ReliableInbox = field(default_factory=ReliableInbox)
    pending: dict = field(default_factory=dict)
    account_created: bool = False
    clock_start: float = field(default_factory=time.monotonic)
    phase: str = "account"
    queue_pending: bool = False
    queue_handle: asyncio.TimerHandle | None = None
    arena_spawn_at: float = 0
    account_id: int = 1
    arena_id: str | None = None
    native_room_id: str | None = None
    native_room_version: tuple | None = None
    battle_spawned: bool = False
    battle_hints_at: float = 0.
    result_sent: bool = False
    load_grace_until: float = 0.
    account_revision: int | None = None
    report_scan_at: float = 0.
    report_ack: dict[int,tuple[int,...]] = field(default_factory=dict)
    smoothed_rtt: float | None = None
    rtt_variance: float = 0.
    rto: float = .25
    retransmissions: int = 0
    pending_peak: int = 0
    ammo_operation_nonce: str = field(default_factory=lambda: secrets.token_hex(16))
    ammo_operation_serial: int = 0
    ammo_last_request: tuple | None = None
    reconnect_replay: bool = False
    sandbox_input: tuple | None = None
    _packet_cipher: PacketCipher | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def packet_cipher(self):
        codec = self._packet_cipher
        if codec is None or codec.key != self.key:
            codec = self._packet_cipher = PacketCipher(self.key)
        return codec

    @property
    def reliable_window_used(self):
        return max(len(self.pending),max(((self.sequence-seq)&0x0fffffff
            for seq in self.pending),default=0))

    def acknowledge(self, sequence, now):
        packet = self.pending.pop(sequence,None)
        if packet is None or packet[2] != 1:
            return  # Karn: an ACK after a retry cannot identify the RTT sample.
        sample = max(0.,now-packet[1])
        if self.smoothed_rtt is None:
            self.smoothed_rtt,self.rtt_variance = sample,sample/2
        else:
            self.rtt_variance = .75*self.rtt_variance+.25*abs(self.smoothed_rtt-sample)
            self.smoothed_rtt = .875*self.smoothed_rtt+.125*sample
        self.rto = min(2.,max(.1,self.smoothed_rtt+max(.01,4*self.rtt_variance)))


@dataclass(frozen=True)
class ReconnectLease:
    """A short account-bound reservation of one already running arena."""
    account_id: int
    arena_id: str
    expires_at: float


# Kept for callers of the original transport-only class name.
Client = ClientSession


def _packet_cipher_for(client):
    codec = getattr(client, 'packet_cipher', None)
    return codec if isinstance(codec, PacketCipher) else None


def _ammo_operation_id(client, kind, body, request_id):
    """Scope adjacent native retries to this session, without treating u16 IDs as unique.

    Mercury already suppresses retransmitted reliable packets. Repeating the last
    exact logical ammunition request also reuses its receipt; a different request
    or an intervening Account command advances the serial, including reuse of an
    earlier native request ID/body.
    """
    request = (kind, request_id, bytes(body))
    if client.ammo_last_request != request:
        client.ammo_operation_serial += 1
        client.ammo_last_request = request
    return f'ammo:{client.ammo_operation_nonce}:{client.ammo_operation_serial}'


def _account_chunks(body, size=900):
    """Keep variable16 headers whole in a pure selected Account38/40 bundle.

    Native Mercury can continue a method payload across fragments, but cannot
    unpack a three-byte header split between them. Only recognize a complete
    selector13 followed by these two known variable16 methods; all other bundle
    formats retain the existing transport path. No protocol bytes are changed.
    """
    if type(size) is not int or size < 3:
        raise ValueError('Account fragment size must fit a variable16 header')
    if body[:1] != b'\x13':
        return None
    headers, offset = [], 1
    while offset < len(body):
        if offset + 3 > len(body) or body[offset] not in (97, 99):
            return None
        end = offset + 3 + struct.unpack_from('<H', body, offset + 1)[0]
        if end > len(body):
            return None
        headers.append(offset)
        offset = end
    if not headers:
        return None
    chunks, start, index = [], 0, 0
    while start < len(body):
        end = min(start + size, len(body))
        while index < len(headers) and headers[index] + 3 <= end:
            index += 1
        if index < len(headers) and headers[index] < end:
            end = headers[index]
        chunks.append(body[start:end])
        start = end
    return chunks


def _crew_metadata(model, metadata):
    """Immutable specs plus explicit bindings, or both None if nothing binds."""
    meshes = tuple(mesh.name for mesh in model.geometry.meshes if mesh.name.startswith('crew_'))
    if not meshes:
        return dict(crew_specs=None, crew_bindings=None)
    from .battle import CREW_DEVICES
    from .modules import crew_specs_from_metadata, crew_bindings_for_meshes
    specs = crew_specs_from_metadata(metadata)
    bindings = crew_bindings_for_meshes(specs, meshes, CREW_DEVICES)
    if not bindings:
        return dict(crew_specs=None, crew_bindings=None)
    return dict(crew_specs=specs, crew_bindings=bindings)


class BaseEndpoint(asyncio.DatagramProtocol):
    # Give HTTP/UDP callbacks a turn between batches of complete 60 Hz steps.
    # One individually expensive step can exceed this wall budget.
    SIMULATION_BUDGET = .008
    # Native ready can precede a blocking map/hangar render-resource load.
    # The bounded grace belongs to entity replacement, not regular traffic.
    ENTITY_LOAD_GRACE = 45.
    # A lost/replaced UDP session must not settle a running local battle before
    # its authenticated owner has had a chance to create a fresh BaseApp
    # session.  This is deliberately independent of packet retry timeouts.
    RECONNECT_GRACE = 90.
    FAULT_RETRY = 5.

    @cached_property
    def arena_faults(self):
        # Only arenas still owned by the manager live here. Retired worlds
        # belong to ArenaRetirement, not an ever-growing second object list.
        return {}

    @cached_property
    def fault_history(self):
        return deque(maxlen=32)

    def _report_fault(self, stage, exc, **context):
        self.server.event('base_fault', stage=stage, error_type=type(exc).__name__,
                          reason=str(exc), traceback=''.join(traceback.format_exception(exc)),
                          **context)

    def _detach_faulted_session(self, addr, client):
        recon_transport = getattr(self, 'recon_transport', None)
        if recon_transport is not None:
            recon_transport.revoke(client)
        if self.clients.get(addr) is client:
            self.clients.pop(addr)
        self._reconnects().pop(client.account_id, None)
        try:
            self.rooms.leave(client.account_id, disconnected=True)
        except Exception as exc:
            self._report_fault('session_detach', exc, account_id=client.account_id)

    def _disconnect_client(self, addr, reason):
        """Normal departure first; a broken departure must not poison the loop."""
        client = self.clients.get(addr)
        arena = self.arenas.get(client.arena_id) if client is not None else None
        try:
            self.drop_client(addr, reason)
        except Exception as exc:
            self._report_fault('client_cleanup', exc,
                               account_id=client.account_id if client else None)
            if client is not None:
                self._detach_faulted_session(addr, client)
            if arena is not None and self.arenas.get(arena.result_id) is arena:
                self._quarantine_arena(arena, 'client_cleanup', exc, time.monotonic())

    def _quarantine_arena(self, arena, stage, exc, now):
        """Never call simulation finish/leave while recovering simulation faults.

        An unfinished arena is aborted without a fabricated result. A finished
        local arena keeps its real winner and receipts, and retries only settlement.
        A failed owner is never reconstructed from its diagnostic parent mirror.
        """
        completed = bool(arena.finished_at) and not getattr(arena, 'owner_backed', False)
        fault = dict(stage=stage, error_type=type(exc).__name__, reason=str(exc),
                     completed=completed, retry_at=now+self.FAULT_RETRY,
                     recorded_accounts=sorted(arena.recorded_accounts),
                     unrecorded_accounts=sorted(set(arena.players)-arena.recorded_accounts))
        self.arena_faults[arena.result_id] = fault
        self.fault_history.append(dict(arena_id=arena.result_id, **fault))
        self._report_fault(stage, exc, arena_id=arena.result_id, completed=completed)
        if completed:
            retry = getattr(arena, '_settlement_retry', {})
            for account_id in arena.players:
                if account_id not in arena.recorded_accounts:
                    retry.setdefault(account_id, now+self.FAULT_RETRY)
            arena._settlement_retry = retry
        else:
            arena.retiring = True
        # Remove transport ownership before room cleanup. No leave/finish calls
        # here: those can re-enter the broken fire/physics code or award a win.
        for player in arena.players.values():
            player.attached = False
            self._reconnects().pop(player.account_id, None)
        connected = set()
        for addr, client in list(self.clients.items()):
            if client.arena_id == arena.result_id:
                connected.add(client.account_id)
                self._detach_faulted_session(addr, client)
        for account_id in set(arena.players)-connected:
            try:
                self.rooms.leave(account_id, disconnected=True)
            except Exception as cleanup:
                self._report_fault('session_detach', cleanup, account_id=account_id)
        if not completed:
            # Cancel incremental construction without invoking fail_load,
            # which performs ordinary room/session transitions of its own.
            loading = getattr(self, 'loading', None)
            if loading is not None:
                loading.jobs.pop(arena.result_id, None)
                try:loading.order.remove(arena.result_id)
                except ValueError:pass
            self._retire_faulted_arena(arena)

    def _retire_faulted_arena(self, arena):
        try:
            retire = self.arenas.retire
            if retire is not None:
                retire(arena)
            else:
                arena.close()
        except Exception as cleanup:
            # Manager ownership stays intact until handoff succeeds. The
            # quarantine loop retries cleanup without restarting simulation.
            self._report_fault('arena_cleanup', cleanup, arena_id=arena.result_id)
        else:
            self.arenas.arenas.pop(arena.result_id, None)
            self.arena_faults.pop(arena.result_id, None)

    def _retry_faulted_result(self, arena, fault, now):
        if now < fault['retry_at']:
            return
        fault['retry_at'] = now+self.FAULT_RETRY
        if not fault['completed']:
            self._retire_faulted_arena(arena)
            return
        try:
            self.flush_arena(arena)
            if not set(arena.players).issubset(arena.recorded_accounts):
                return
            arena._settlement_retry = {}
            self.arenas.remove_if_empty(arena)
        except Exception as exc:
            self._report_fault('arena_result_retry', exc, arena_id=arena.result_id)
        else:
            self.arena_faults.pop(arena.result_id, None)

    @cached_property
    def battle_captures(self):
        from .captures import BoundedCapture
        return BoundedCapture(self.server.lab/'private/battle-captures',4096)

    @cached_property
    def rpc_captures(self):
        from .captures import BoundedCapture
        return BoundedCapture(self.server.lab/'private/rpc-captures',256)

    @cached_property
    def owner_service(self):
        from .arena_owner_service import ArenaOwnerService
        return ArenaOwnerService(self)

    @cached_property
    def rooms(self):
        from .rooms import RoomManager
        return RoomManager(self)

    @cached_property
    def native_rooms(self):
        from .native_rooms import NativeRooms
        return NativeRooms(self)

    def __init__(self, server):
        self.server = server
        server.base = self
        self.clients = {}
        self.reconnects = {}
        self.transport = None
        owner_setting = os.environ.get('SOLBREAKER_ARENA_OWNERS', '0')
        if owner_setting not in ('0', '1'):
            raise ValueError('SOLBREAKER_ARENA_OWNERS must be 0 or 1')
        self.arena_owners_enabled = owner_setting == '1'
        self.max_arenas = int(os.environ.get('SOLBREAKER_MAX_ARENAS', '0'))
        if not 0 <= self.max_arenas <= 64:
            raise ValueError('SOLBREAKER_MAX_ARENAS must be between 0 and 64')
        game = Path(server.manifest["game"])
        self.game = game
        dev_specs = dev_map_specs_for_lab(server.manifest, server.lab)
        self.dev_map_specs = {spec.map_id: spec for spec in dev_specs}
        training_map = server.manifest.get('training_map', 'rudniki')
        self.map_spec = next((spec for spec in dev_specs
                              if spec.key == training_map), None)
        if self.map_spec is None:
            self.map_spec = load_map_spec(game, training_map)
        self.map_catalog = training_map_catalog(game)
        for spec in dev_specs:
            existing = self.map_catalog.get(spec.map_id)
            if existing is not None and existing != spec.key:
                raise ValueError('Development map ID conflicts with the client catalog')
            self.map_catalog[spec.map_id] = spec.key
        if self.map_catalog.get(self.map_spec.map_id) != self.map_spec.key:
            raise ValueError('Default map is not available to the training picker')
        self.map_specs = {self.map_spec.map_id: self.map_spec}
        self.map_spec_errors = {}
        if self.arena_owners_enabled:
            # Parent mirrors need small immutable descriptors. Parse them before
            # accepting clients so a cold map cannot pause another arena's IPC.
            started = time.monotonic()
            for map_id in self.map_catalog:
                try:
                    self.resolve_map_spec(map_id)
                except (OSError, ValueError) as exc:
                    self.map_spec_errors[map_id] = (type(exc), exc.args)
            server.event('map_metadata_ready', maps=len(self.map_specs),
                         unavailable=len(self.map_spec_errors),
                         duration_ms=(time.monotonic()-started)*1000)
        self.terrain = MapTerrain(game, self.map_spec)
        from .collision import CollisionScene
        selection = server.manifest.get('training_vehicle','usa/M2_lt').split('/')
        if len(selection) != 2:
            raise ValueError('training_vehicle must be nation/name')
        configuration = server.manifest.get('training_vehicle_configuration')
        # The manifest vehicle is used to build actual arena collision assets,
        # so it takes the same narrow battle-only route as a selected garage
        # vehicle.  Lobby/catalog decoding never reaches this loader.
        from .armor import load_battle_vehicle
        from .progression import ProgressionService
        self.definition = ProgressionService._require_battle_ready(load_battle_vehicle(
            Path(server.manifest['game']),*selection,
            VehicleConfiguration.parse(configuration) if configuration is not None else None))
        self.progression = None
        self.progression_catalog = None
        if server.manifest.get('vehicle_progression', True):
            self.progression = ProgressionService(server.accounts.store,
                Path(server.manifest['game']), self.definition,
                grant_all=server.manifest.get('grant_all_vehicles', True))
            from .catalog import research_prices
            self.progression_catalog = research_prices(Path(server.manifest['game']))
        for row in server.accounts.store.db.execute('SELECT inventory FROM accounts'):
            inventory = json.loads(row[0])
            if self.progression is None and inventory and (inventory['descriptor'] != self.definition.descriptor.hex()
                    or inventory.get('shells',[])[:1] != [self.definition.weapon.shells[0].compact_id]):
                raise ValueError('Existing inventory differs from training_vehicle; use a separate lab')
        from .loadout_effects import apply_definition
        self.definition = apply_definition(server.manifest['game'], self.definition, None)
        from .internal_geometry import load_experimental_models
        self.internal_module_models = load_experimental_models(server.manifest['game'], server.lab,
            server.manifest.get('experimental_internal_modules', []))
        # The official burn curve is not present in the pinned resources.
        # Enabling fire therefore requires an explicit, acknowledged LOCAL
        # sandbox policy; there are no implicit damage or duration defaults.
        self.experimental_fire = server.manifest.get('experimental_fire')
        if self.experimental_fire is not None:
            policy = self.experimental_fire
            fields = {'source_classification', 'hp_fraction_per_second',
                      'module_damage_per_second', 'duration'}
            if (not isinstance(policy, dict) or set(policy) != fields
                    or policy['source_classification'] != 'local_sandbox'):
                raise ValueError('Experimental fire requires an explicit local_sandbox policy')
            policy = dict(policy)
            for name in fields - {'source_classification'}:
                value = policy[name]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError('Invalid experimental fire parameter: ' + name)
                try:
                    value = float(value)
                except OverflowError:
                    raise ValueError('Invalid experimental fire parameter: ' + name) from None
                if not math.isfinite(value) or value < 0:
                    raise ValueError('Invalid experimental fire parameter: ' + name)
                policy[name] = value
            if not 0 < policy['hp_fraction_per_second'] <= 1 or not .5 <= policy['duration'] <= 2048:
                raise ValueError('Experimental fire requires a positive HP fraction and duration .5..2048 seconds')
            if not self.internal_module_models:
                raise ValueError('Experimental fire requires selected internal module models')
            from .fire import FirePolicy
            FirePolicy(policy['hp_fraction_per_second'], policy['module_damage_per_second'], policy['duration'])
            self.experimental_fire = policy
        from .map_prewarm import settings as map_cache_settings
        self.map_cache, self.map_prewarm_enabled = map_cache_settings(server.lab)
        if self.arena_owners_enabled:
            self.collision_scene = self.owner_vehicle_scene(self.definition)
        else:
            self.collision_scene = CollisionScene(game, self.definition, self.map_spec,
                                                  map_cache=self.map_cache, terrain=self.terrain)
            self.configure_internal_modules(self.collision_scene)
            self.collision_scene.prepare(self.terrain)
        self.map_contexts = {self.map_spec.map_id: dict(
            map_spec=self.map_spec, terrain=self.terrain,
            collision_scene=self.collision_scene, vehicle_scenes={})}
        self.vehicle_scenes = self.map_contexts[self.map_spec.map_id]['vehicle_scenes']
        from .arena_retirement import ArenaRetirement
        self.retirement = ArenaRetirement(lambda arena,reason:server.event(
            'arena_retirement_failed',arena_id=arena.result_id,reason=reason))
        self.arenas = ArenaManager(self.terrain,self.collision_scene,self.definition.descriptor,
                                  map_spec=self.map_spec,
                                  sandbox_enabled=getattr(server, 'sandbox', False),
                                  step_budget=self.SIMULATION_BUDGET,
                                  recon_enabled=getattr(server, 'recon_enabled', False),
                                  recon_game=server.manifest['game'],
                                  retire=self.retirement.begin,
                                  can_retire=lambda arena:not getattr(arena, "_settlement_retry", {}))
        from .arena_loading import ArenaLoader
        self.loading = ArenaLoader(self.validate_load,self.finish_load,self.fail_load)
        self.recon_transport = None
        if getattr(server, 'recon_enabled', False):
            from .recon_transport import ReconTransport
            self.recon_transport = ReconTransport(self, clock=time.monotonic)
        # Count existing diagnostics once, before serving gameplay packets.
        self.battle_captures
        self.rpc_captures

    def connection_made(self, transport):
        self.transport = transport
        self.matchmaking_enabled = True
        self.next_tick = time.monotonic() + SIMULATION_INTERVAL
        self.timer = asyncio.get_running_loop().call_later(SIMULATION_INTERVAL, self.tick)
        if self.map_cache is not None:
            from .map_prewarm import MapPrewarm
            specs = {**self.dev_map_specs, **self.map_specs}
            self.map_prewarm = MapPrewarm(
                self.map_cache, self.game, self.map_catalog, specs, self.map_spec.map_id,
                enabled=self.map_prewarm_enabled,
                busy=lambda: any(not arena.finished_at and not arena.closed
                                 for arena in self.arenas.arenas.values()),
                event=self.server.event)
            self.map_prewarm.start()

    def connection_lost(self, exc):
        self.matchmaking_enabled = False
        for client in self.clients.values():
            if client.queue_handle is not None:
                client.queue_handle.cancel()
                client.queue_handle = None
        self.timer.cancel()
        self.loading.close()
        prewarm = getattr(self, 'map_prewarm', None)
        owner = self.__dict__.get('owner_service')
        if prewarm is not None or owner is not None:
            async def close_workers():
                await asyncio.gather(*(worker.close() for worker in (prewarm, owner)
                                       if worker is not None))
            self.owner_close_task = asyncio.create_task(close_workers())
            self.owner_close_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None)
        try:
            if hasattr(self,'retirement'):
                self.retirement.close()
        finally:
            try:
                self.arenas.close()
            finally:
                for context in getattr(self, 'map_contexts', {}).values():
                    self._close_map_context(context)

    @staticmethod
    def _close_map_context(context):
        for scene in (*context['vehicle_scenes'].values(), context['collision_scene']):
            close = getattr(scene, 'close', None)
            if close is not None:
                close()
        context['vehicle_scenes'].clear()

    def _prune_map_contexts(self, keep=()):
        protected = set(keep) | {self.map_spec.map_id}
        arena_manager = getattr(self, 'arenas', None)
        for arena in getattr(arena_manager, 'arenas', {}).values():
            protected.add(arena.map_id)
        for map_id, context in tuple(self.map_contexts.items()):
            if map_id not in protected:
                self._close_map_context(context)
                del self.map_contexts[map_id]

    def map_runtime(self, map_id):
        spec = self.resolve_map_spec(map_id)
        context = self.map_contexts.get(map_id)
        if context is None:
            from .collision import CollisionScene
            terrain = MapTerrain(self.game, spec)
            if self.arena_owners_enabled:
                collision_scene = self.owner_vehicle_scene(self.definition)
            else:
                collision_scene = CollisionScene(self.game, self.definition, spec,
                    map_cache=getattr(self, 'map_cache', None), terrain=terrain)
                self.configure_internal_modules(collision_scene)
                collision_scene.prepare(terrain)
            context = dict(map_spec=spec, terrain=terrain,
                           collision_scene=collision_scene, vehicle_scenes={})
            self.map_contexts[map_id] = context
            self._prune_map_contexts((map_id,))
            detail = (dict(owner_recipe=True) if self.arena_owners_enabled
                      else dict(objects=len(collision_scene.object_names)))
            self.server.event('training_map_ready', map_id=map_id, map_key=spec.key, **detail)
        return context

    def resolve_map_spec(self, map_id):
        if type(map_id) is not int or map_id not in self.map_catalog:
            raise ValueError('Unsupported training map ID')
        failure = getattr(self, 'map_spec_errors', {}).get(map_id)
        if failure is not None:
            error_type, arguments = failure
            raise error_type(*arguments)
        spec = self.map_specs.get(map_id)
        if spec is None:
            spec = self.dev_map_specs.get(map_id)
            if spec is None:
                spec = load_map_spec(self.game, self.map_catalog[map_id])
            self.map_specs[map_id] = spec
        return spec

    def selected_map(self, map_id=None):
        # Native solo-queue requests use -1 for the server-selected default.
        if map_id is None:
            return self.map_spec
        if type(map_id) is not int:
            raise ValueError('Unsupported training map ID')
        if map_id in (-1, 0):
            return self.map_spec
        return self.resolve_map_spec(map_id)

    def scene_for_vehicle(self, definition, map_id=None):
        # Same modules can carry different loadouts; never alias effective HP,
        # reload, projectile or steering parameters between those participants.
        map_id = self.map_spec.map_id if map_id is None else map_id
        context = self.map_runtime(map_id)
        key = definition
        original = self.definition
        if key == original:
            return context['collision_scene']
        vehicle_scenes = context['vehicle_scenes']
        if key not in vehicle_scenes:
            started = time.monotonic()
            if self.arena_owners_enabled:
                scene = self.owner_vehicle_scene(definition)
            else:
                scene = context['collision_scene'].for_vehicle(self.game, definition)
                self.configure_internal_modules(scene)
                scene.prepare(context['terrain'])
            vehicle_scenes[key] = scene
            self.server.event('vehicle_scene_ready', vehicle=f'{definition.nation}/{definition.name}',
                              map_id=map_id, map_key=context['map_spec'].key,
                              duration_ms=(time.monotonic()-started)*1000,
                              optional_devices=list(definition.optional_devices),
                              max_health=definition.health, reload=definition.weapon.reload,
                              rotation_speed=definition.drive.rotation,
                              shells=[dict(cd=s.compact_id, penetration=s.penetration, speed=s.speed)
                                      for s in definition.weapon.shells])
        return vehicle_scenes[key]

    def owner_vehicle_scene(self, definition):
        """Exact vehicle data and module specs for a world-free parent mirror."""
        from types import SimpleNamespace
        scene = SimpleNamespace(definition=definition, armor=SimpleNamespace(
            shell=definition.weapon.shells[0], health=definition.health))
        self.configure_internal_modules(scene)
        return scene

    def configure_internal_modules(self, scene):
        """Attach only immutable specs/geometry; mutable HP belongs to the arena."""
        from .internal_geometry import GeometryConfiguration
        definition = scene.definition
        model = self.internal_module_models.get(GeometryConfiguration.from_definition(definition))
        # for_vehicle copies a scene dictionary. Always clear inherited model
        # and spec before selecting this vehicle's exact turret/gun layout.
        scene.internal_geometry = model
        scene.engine_spec = None
        scene.ammo_rack_spec = None
        scene.fuel_tank_spec = None
        scene.fire_policy = None
        scene.fire_min_damage = None
        scene.crew_specs = None
        scene.crew_bindings = None
        if model is None:
            return
        if (model.geometry.turret_position != tuple(definition.aiming.turret_position)
                or model.geometry.gun_position != tuple(definition.aiming.gun_position)):
            raise ValueError('Experimental internal module pivots do not match selected vehicle')
        from .module_data import load_module_metadata
        from .modules import engine_spec_from_metadata, ammo_rack_spec_from_metadata, fuel_tank_spec_from_metadata
        metadata = load_module_metadata(Path(self.server.manifest['game']),
            definition.nation, definition.name, definition.configuration)
        scene.engine_spec = engine_spec_from_metadata(metadata)
        scene.ammo_rack_spec = ammo_rack_spec_from_metadata(metadata)
        scene.fuel_tank_spec = fuel_tank_spec_from_metadata(metadata)
        crew = _crew_metadata(model, metadata)
        scene.crew_specs, scene.crew_bindings = crew['crew_specs'], crew['crew_bindings']
        policy = getattr(self, 'experimental_fire', None)
        if policy is not None:
            from .fire import FirePolicy
            scene.fire_policy = FirePolicy(
                initial_hp_damage_per_second=scene.armor.health * policy['hp_fraction_per_second'],
                initial_module_damage_per_second=policy['module_damage_per_second'],
                duration=policy['duration'])
            minimum = metadata.min_fire_starting_damage.value
            if minimum is None or not math.isfinite(minimum) or minimum <= 0:
                raise ValueError('Missing experimental engine ignition threshold metadata')
            # Using this XML value as a minimum *applied engine damage* is a
            # local ignition rule, not a recovered official comparison.
            scene.fire_min_damage = minimum

    def bot_scene_settings(self, roster, player_scene):
        """Freeze per-type module settings without map/vehicle scene caching."""
        from types import SimpleNamespace
        from .bot_roster import BOT_SCENE_FIELDS
        settings = []
        for row in roster:
            definition = row.definition
            if any(entry == definition for entry, _ in settings):
                continue
            if definition == player_scene.definition:
                source = player_scene
            else:
                source = SimpleNamespace(definition=definition,
                    armor=SimpleNamespace(health=definition.health))
                self.configure_internal_modules(source)
            settings.append((definition, {name: getattr(source, name, None)
                                         for name in BOT_SCENE_FIELDS}))
        return tuple(settings)

    def crew_metadata(self, definition):
        """resolve_loadout crew keywords for this definition's acknowledged geometry.

        Reads only the selected vehicle's module XML, and only when its exact
        experimental model has crew volumes; no collision scene or map loads.
        """
        from .internal_geometry import GeometryConfiguration
        models = getattr(self, 'internal_module_models', None)
        model = models.get(GeometryConfiguration.from_definition(definition)) if models else None
        if (model is None or model.geometry.turret_position != tuple(definition.aiming.turret_position)
                or model.geometry.gun_position != tuple(definition.aiming.gun_position)
                or not any(mesh.name.startswith('crew_') for mesh in model.geometry.meshes)):
            return dict(crew_specs=None, crew_bindings=None)
        from .module_data import load_module_metadata
        return _crew_metadata(model, load_module_metadata(Path(self.server.manifest['game']),
            definition.nation, definition.name, definition.configuration))

    def _reconnects(self):
        """Compatibility for focused endpoints constructed with ``__new__``."""
        if not hasattr(self, 'reconnects'):
            self.reconnects = {}
        return self.reconnects

    @staticmethod
    def _transport_loss(reason):
        return reason in {'idle timeout', 'reliable send window exceeded',
                          'reliable delivery timed out', 'replaced by a new login'}

    def _reserve_reconnect(self, client, arena, now):
        """Detach a transport while keeping the live arena participant intact."""
        player = arena.players.get(client.account_id)
        if (player is None or not player.attached or arena.building or arena.closed
                or arena.retiring or not arena.started or arena.finished_at):
            return False
        vehicle = arena.vehicle_for(client.account_id)
        # Stop receiving stale held keys but do not rewrite reload, damage,
        # burst, crew or vehicle state.  Those belong to the arena, not UDP.
        if getattr(arena, 'owner_backed', False):
            self.owner_service.stop_control(arena, client.account_id)
        vehicle.flags = 0
        vehicle.last_input = arena.simulation_time
        self._reconnects()[client.account_id] = ReconnectLease(
            client.account_id, arena.result_id, now + self.RECONNECT_GRACE)
        self.server.event('battle_reconnect_detached', account_id=client.account_id,
                          arena_id=arena.result_id, grace_seconds=self.RECONNECT_GRACE)
        return True

    def _claim_reconnect(self, client, now):
        """Bind a newly authenticated session to its still-live arena lease."""
        lease = self._reconnects().get(client.account_id)
        if lease is None:
            return False
        if now >= lease.expires_at:
            self._expire_reconnects(now, only_account=client.account_id)
            return False
        arena = self.arenas.get(lease.arena_id)
        player = arena.players.get(client.account_id) if arena is not None else None
        if (arena is None or arena.building or arena.closed or arena.retiring
                or player is None or not player.attached):
            self._reconnects().pop(client.account_id, None)
            return False
        self._reconnects().pop(client.account_id, None)
        client.arena_id = arena.result_id
        # A fresh native SessionContext needs Account initialization, including
        # its ReplayManager, before onBecomePlayer can create a battle Avatar.
        client.account_created = False
        client.account_revision = None
        client.phase = 'account_pending'
        player.loaded = False
        client.battle_spawned = False
        client.battle_hints_at = 0.
        client.result_sent = False
        client.reconnect_replay = True
        client.load_grace_until = now + self.ENTITY_LOAD_GRACE
        client.clock_start = arena.epoch
        client.last_sync_tick = -1
        self.server.event('battle_reconnect_claimed', account_id=client.account_id,
                          arena_id=arena.result_id)
        return True

    def _expire_reconnects(self, now, *, only_account=None):
        """Apply the existing departure path once when a lease expires."""
        for account_id, lease in list(self._reconnects().items()):
            if only_account is not None and account_id != only_account:
                continue
            if only_account is None and now < lease.expires_at:
                continue
            self._reconnects().pop(account_id, None)
            arena = self.arenas.get(lease.arena_id)
            if arena is not None and account_id in arena.players:
                try:
                    if getattr(arena, 'owner_backed', False):
                        self.owner_service.disconnect(arena, account_id)
                    else:
                        arena.leave(account_id, now, reason='disconnect')
                        self.flush_arena(arena)
                        if arena.finished_at:
                            self.rooms.sync()
                        self.arenas.remove_if_empty(arena)
                except Exception as exc:
                    self._quarantine_arena(arena, 'reconnect_expiry', exc, now)
            try:
                self.rooms.leave(account_id, disconnected=True)
            except Exception as exc:
                self._report_fault('reconnect_room_cleanup', exc, account_id=account_id)
            self.server.event('battle_reconnect_expired', account_id=account_id,
                              arena_id=lease.arena_id)

    def drop_client(self, addr, reason):
        client = self.clients.get(addr)
        if client is not None and client.queue_handle is not None:
            client.queue_handle.cancel()
            client.queue_handle = None
        recon_transport = getattr(self, 'recon_transport', None)
        if client is not None and recon_transport is not None:
            # Revocation resolves the live session to clear its reticle input.
            # Keep that trusted mapping until recon releases the observer.
            recon_transport.revoke(client)
        self.clients.pop(addr, None)
        arena = self.arenas.get(client.arena_id) if client else None
        held = False
        if arena:
            if arena.building:
                if arena.room_settings is not None:
                    self._withdraw_loading_player(arena, client)
                elif getattr(arena, 'owner_backed', False):
                    self.owner_service.cancel(
                        arena, 'Participant disconnected during owner startup')
                else:
                    self.loading.cancel(arena,'Participant disconnected during world construction')
            elif self._transport_loss(reason):
                held = self._reserve_reconnect(client, arena, time.monotonic())
            if not held and not arena.building:
                self._reconnects().pop(client.account_id, None)
                if getattr(arena, 'owner_backed', False):
                    self.owner_service.disconnect(arena, client.account_id)
                else:
                    arena.leave(client.account_id)
                    self.flush_arena(arena)
                    if arena.finished_at:
                        self.rooms.sync()
                    self.arenas.remove_if_empty(arena)
        if client and not held:
            try:
                self.rooms.leave(client.account_id,disconnected=True)
            except Exception as exc:
                # Room bookkeeping alone does not prove the arena is broken.
                self._report_fault('client_room_cleanup', exc, account_id=client.account_id)
        self.server.event("base_disconnected", reason=reason,
            account_id=client.account_id if client else None,
            retransmissions=client.retransmissions if client else 0,
            pending_peak=client.pending_peak if client else 0)

    def metrics(self):
        from .sim_native import runtime_status
        return {'rooms':len(self.rooms.rooms),
                'arena_faults':dict(self.arena_faults),
                'recent_arena_faults':list(self.fault_history),
                'retiring_arenas':len(self.retirement.jobs) if hasattr(self,'retirement') else 0,
                'retirement_failures':len(self.retirement.failures) if hasattr(self,'retirement') else 0,
                'reconnects':[dict(account_id=lease.account_id,arena_id=lease.arena_id,
                                    expires_at=lease.expires_at)
                              for lease in self._reconnects().values()],
                'sessions':[dict(account_id=c.account_id,peer=f'{addr[0]}:{addr[1]}',phase=c.phase,pending=len(c.pending),
                    pending_peak=c.pending_peak,rtt_ms=None if c.smoothed_rtt is None else c.smoothed_rtt*1000,
                    rto_ms=c.rto*1000,retransmissions=c.retransmissions,
                    receive_waiting=len(c.inbox.waiting),unreliable_dropped=c.inbox.unreliable_dropped)
                    for addr,c in self.clients.items()],
                'arenas':[dict(id=a.result_id,building=a.building,players=len(a.players),
                    attached_players=sum(p.attached for p in a.players.values()),
                    loaded_players=sum(p.loaded for p in a.players.values()),vehicles=len(a.vehicles),
                    projectiles=len(a.projectiles),steps=a.step_count,dropped_seconds=a.dropped_time,
                    simulation=(a.owner_simulation if getattr(a, 'owner_backed', False)
                                and hasattr(a, 'owner_simulation') else
                                runtime_status(getattr(a,'world',None),getattr(a,'drive_workers',None))))
                    for a in self.arenas.arenas.values()]}

    def tick(self):
        now = time.monotonic()
        if diagnostics.get_recorder() is not None:
            diagnostics.endpoint_sample(self, now)
        try:
            if now >= getattr(self, '_room_sync_retry_at', 0.):
                try:
                    self.rooms.sync()
                except Exception as exc:
                    self._room_sync_retry_at = now+self.FAULT_RETRY
                    self._report_fault('room_sync', exc)
            try:
                self._tick(now)
            except Exception as exc:
                if now >= getattr(self, '_tick_fault_log_at', 0.):
                    self._tick_fault_log_at = now+self.FAULT_RETRY
                    self._report_fault('tick', exc)
        finally:
            # A failing channel or research outbox must not stop the timer.
            deadline = getattr(self, "next_tick", now) + SIMULATION_INTERVAL
            finished = time.monotonic()
            pending = any(arena.started and not getattr(arena, 'owner_backed', False)
                          and not (arena.closed or arena.retiring or arena.building)
                          and arena.result_id not in self.arena_faults
                          and arena.accumulator+1e-9 >= arena.STEP
                          for arena in self.arenas.arenas.values())
            # Yield to queued HTTP/UDP callbacks, then continue any deferred
            # fixed steps. Sleeping another whole interval after an overrun
            # would reduce simulation throughput even when spare CPU exists.
            self.next_tick = finished if pending else max(deadline, finished)
            self.timer = asyncio.get_running_loop().call_later(self.next_tick-finished, self.tick)

    def _tick(self, now):
        arenas = list(self.arenas.arenas.values())
        if arenas:
            first = getattr(self, '_arena_tick_cursor', 0) % len(arenas)
            self._arena_tick_cursor = (first+1) % len(arenas)
            arenas = arenas[first:]+arenas[:first]
        deadline = time.monotonic()+self.SIMULATION_BUDGET
        for arena in arenas:
            if getattr(arena, 'owner_backed', False):
                continue
            fault = self.arena_faults.get(arena.result_id)
            if fault is not None:
                self._retry_faulted_result(arena, fault, now)
                continue
            try:
                arena.step(now, deadline=deadline)
                self.flush_arena(arena)
                if arena.result_id in self.arena_faults:
                    continue
                if hasattr(arena, "_settlement_retry") and not arena._settlement_retry:
                    self.arenas.remove_if_empty(arena)
            except Exception as exc:
                self._quarantine_arena(arena, 'arena_tick', exc, now)
        # A costly world step must not leave transport retries/leases using
        # the stale timestamp from before that step.
        now = time.monotonic()
        try:
            self._expire_reconnects(now)
        except Exception as exc:
            self._report_fault('reconnect_expiry', exc)
        for addr, client in list(self.clients.items()):
            if self.clients.get(addr) is not client:
                continue
            try:
                if now - client.last_seen > 60:
                    self._disconnect_client(addr, "idle timeout")
                    continue
                self.tick_client(addr, client, now)
            except ChannelOverflow:
                self._disconnect_client(addr, "reliable send window exceeded")
            except ChannelTimeout:
                self._disconnect_client(addr, 'reliable delivery timed out')
            except Exception as exc:
                self._report_fault('client_tick', exc, account_id=client.account_id)
                self._disconnect_client(addr, 'internal session failure')
        outbox = self.server.lab / "private/outbox"
        if (getattr(self.server, "developer_mode", False) and outbox.exists()
                and any(c.account_created for c in self.clients.values())):
            for path in sorted(outbox.glob("*.json"))[:8]:
                try:
                    if path.stat().st_size > 131072 or path.is_symlink():
                        raise ValueError("Invalid local outbox file")
                    item = json.loads(path.read_text())
                    if item.get("action") == "start_shared":
                        self.start_shared(item["accounts"])
                        path.rename(path.with_suffix(".sent"))
                        continue
                    if item.get("action") == "start_battle":
                        for addr, client in self.clients.items():
                            if client.phase == "account":
                                self.start_battle(addr, client)
                        path.rename(path.with_suffix(".sent"))
                        continue
                    if "bundle_hex" in item:
                        body = bytes.fromhex(item["bundle_hex"])
                        for addr, client in self.clients.items():
                            self.send(addr, client, body)
                        path.rename(path.with_suffix(".sent"))
                        continue
                    payload = (bytes.fromhex(item["payload_hex"]) if "payload_hex" in item
                               else packed_blob(pickle_document(item["pickle"])))
                    body = b"\x13" + account_method(int(item["method"]), payload)
                    for addr, client in self.clients.items():
                        if client.account_created:
                            self.send(addr, client, body)
                    path.rename(path.with_suffix(".sent"))
                    self.server.event("local_rpc_sent", method=item["method"], size=len(payload))
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    self.server.event("local_rpc_rejected", reason=str(exc))
                    path.rename(path.with_suffix(".rejected"))

    def flush_arena(self, arena):
        events = arena.drain_events()
        self.log_arena_events(arena, events)
        self._flush_arena_results(arena, events)

    def log_arena_events(self, arena, events):
        for event in events:
            if event.kind == 'period':
                self.server.event('arena_period', arena_id=arena.result_id,
                                  simulation_time=arena.simulation_time, step=arena.step_count,
                                  **event.data)
            elif event.kind == 'shot':
                v = arena.vehicles[event.actor]
                self.server.event('training_shot',entity_id=event.actor,shots=v.shots,
                    ammo=event.data.get('ammo_after',v.ammo),
                    shot=event.data['shot'],simulation_time=event.data['at'],
                    shell_id=event.data.get('shell_id'),effect_index=event.data.get('effect_index'),
                    origin=event.data['origin'],velocity=event.data['velocity'])
            elif event.kind == 'shell_selected':
                v = arena.vehicles[event.actor]
                self.server.event('training_shell_selected',entity_id=event.actor,
                    shell_id=event.data['shell'],clip=v.clip,ammo=v.ammo,
                    reload_remaining=max(0.,v.reload_at-arena.simulation_time))
            elif event.kind in ('impact','splash'):
                self.server.event('training_'+event.kind,entity_id=event.actor,target_id=event.target,
                    shot=event.data['shot'],**asdict(event.data['result']),
                    applied_damage=event.data['applied_damage'],simulation_time=event.data['at'],
                    **({'raw_damage':event.data['raw_damage']} if 'raw_damage' in event.data else {}))
            elif event.kind == 'explosion':
                self.server.event('training_explosion',entity_id=event.actor,**event.data)
            elif event.kind == 'health':
                self.server.event('training_hit',entity_id=event.target,health=event.data['health'],
                                  hits=(arena.vehicles[event.actor].hits
                                        if event.actor in arena.vehicles else 0))
            elif event.kind in ('recon_lock', 'recon_share'):
                self.server.event('training_'+event.kind, arena_id=arena.result_id,
                                  entity_id=event.actor, target_id=event.target, **event.data)
            elif event.kind == 'fall_impact':
                self.server.event('training_fall_impact',arena_id=arena.result_id,
                                  entity_id=event.actor,**event.data)
            elif event.kind == 'destruction':
                self.server.event('training_destructibles',destroyed=sorted(arena.destructions))
            elif event.kind in ('airborne','landed','auto_recovered','practice_spawn',
                                'sandbox_teleport','gravity_jump'):
                self.server.event('training_'+event.kind,entity_id=event.actor,**event.data)
            elif event.kind == 'ground_impact':
                self.server.event('training_ground_impact', arena_id=arena.result_id,
                    entity_a=event.actor, entity_b=event.target, **event.data)
            elif event.kind == 'drive_trace':
                self.server.event('training_drive_trace', arena_id=arena.result_id,
                                  entity_id=event.actor, **event.data)
            elif event.kind in ('consumable_used', 'consumable_state', 'consumable_rejected', 'armor_states',
                                 'module_hit', 'module_state', 'internal_module_deferred',
                                 'internal_module_trace', 'ammo_rack_detonated',
                                 'state_modifiers', 'fire_started', 'fire_stopped', 'fire_tick',
                                 'fire_rejected', 'provision_effect', 'crew_hit'):
                data = dict(event.data)
                if event.kind == 'armor_states':
                    # Logging must not retain the immutable collision object;
                    # property16 serialization still receives that exact object.
                    data['states'] = tuple(data['states'].states)
                self.server.event('training_'+event.kind, arena_id=arena.result_id,
                                  entity_id=event.actor,
                                  **({'target_id':event.target} if event.target else {}), **data)

    def _flush_arena_results(self, arena, events):
        for result in arena.results():
            if result.account_id in arena.recorded_accounts:
                continue
            retry = getattr(arena, '_settlement_retry', {})
            if time.monotonic() < retry.get(result.account_id, 0.):
                continue
            try:
                from .results import snapshot
                report = snapshot(arena,result.account_id)
                rewards = {}
                if result.inventory_id is not None:
                    rewards = dict(inventory_id=result.inventory_id,
                                   vehicle_xp=report['vehicle_xp'], free_xp=report['free_xp'])
                    if getattr(self, 'progression', None) is not None:
                        rewards['ammo'] = self.progression.battle_ammunition(
                            arena.vehicle_for(result.account_id))
                saved = self.server.accounts.store.record(result,report=report,**rewards)
                if saved is not None:
                    arena.reports[result.account_id] = report
                else:
                    existing = self.server.accounts.store.battle_report(result.account_id,arena.unique_id)
                    if existing is not None:
                        arena.reports[result.account_id] = existing
                arena.recorded_accounts.add(result.account_id)
                if saved:
                    self.server.event('training_finished',account_id=result.account_id,result=saved,
                        simulation_steps=arena.step_count,dropped_seconds=arena.dropped_time)
                retry.pop(result.account_id, None)
            except (ValueError, sqlite3.Error) as exc:
                retry[result.account_id] = time.monotonic() + 5.
                arena._settlement_retry = retry
                self.server.event('battle_settlement_failed', arena_id=arena.result_id,
                                  account_id=result.account_id, reason=str(exc))
        # Serialize the whole batch before disconnecting anyone. A malformed
        # shared event must not look like successive voluntary departures and
        # create a winner. Deferring cleanup also prevents nested flushes from
        # overtaking this batch's older events.
        outgoing, failures = [], []
        for addr,client in list(self.clients.items()):
            if (self.clients.get(addr) is not client or client.arena_id != arena.result_id
                    or client.phase != 'battle' or client.reconnect_replay):
                continue
            try:
                body = event_messages(arena,arena.players[client.account_id],events)
                result_ready = (not client.result_sent and client.account_id in arena.reports
                                and (arena.postbattle_complete or
                                     not arena.players[client.account_id].attached))
                if result_ready:
                    from .results import avatar_resource
                    body += avatar_resource(arena.reports[client.account_id])
                outgoing.append((addr,client,body,result_ready))
            except Exception as exc:
                failures.append((addr,client,exc))
        if failures and (len(failures)>1 or not outgoing):
            self._quarantine_arena(arena,'arena_events',failures[0][2],time.monotonic())
            return
        for addr,client,body,result_ready in outgoing:
            if self.clients.get(addr) is not client:
                continue
            try:
                if body:
                    self.send(addr,client,body)
                if result_ready:
                    client.result_sent = True
            except Exception as exc:
                failures.append((addr,client,exc))
        if sum(not isinstance(exc,ChannelOverflow) for _,_,exc in failures)>1:
            self._quarantine_arena(arena,'arena_events',failures[0][2],time.monotonic())
            return
        for addr,client,exc in failures:
            if self.clients.get(addr) is not client:
                continue
            if isinstance(exc,ChannelOverflow):
                self._disconnect_client(addr,'reliable send window exceeded')
            else:
                self._report_fault('client_events', exc, account_id=client.account_id,
                                   arena_id=arena.result_id)
                self._disconnect_client(addr,'internal session failure')

    def tick_client(self, addr, client, now):
        self.retry_pending(addr,client,now)
        self.sync_reports(addr,client,now)
        recon_transport = getattr(self, 'recon_transport', None)
        if recon_transport is not None:
            try:
                recon_transport.tick_client(addr, client, now)
            except Exception as exc:
                recon_transport.revoke(client)
                self._report_fault('recon_transport', exc, account_id=client.account_id)
        if client.native_room_id and client.phase == 'account':
            self.native_rooms.sync_client(addr,client)
        # The native client need not send anything while idle once snapshots are
        # unreliable. A sparse acknowledged probe keeps both peers alive and
        # provides RTT samples without turning every frame/ACK reliable.
        if not client.pending and now-client.last_reliable_sent>=2:
            self.send_packet(addr,client,b'',reliable=True)
        arena = self.arenas.get(client.arena_id)
        if getattr(arena, 'owner_backed', False):
            if client.battle_spawned:
                if not arena.started:
                    # Keep the native connection clock alive while the world
                    # is stationary. tickSync carries only8 bits; a loading
                    # screen plus countdown can span several complete wraps.
                    tick = game_tick(arena.simulation_time, arena.epoch)
                    if tick != client.last_sync_tick:
                        self.send(addr, client, tick_sync(tick), reliable=False)
                        client.last_sync_tick = tick
                if (client.battle_hints_at and arena.started and now >= client.battle_hints_at
                        and arena.players[client.account_id].loaded and not arena.finished_at):
                    vehicle = arena.vehicle_for(client.account_id)
                    self.send(addr, client, battle_hints_message() + ammo_message(vehicle)
                              + avatar_method(7, struct.pack('<BI', 0, vehicle.shell_id)))
                    client.battle_hints_at = 0.
                if (arena.postbattle_complete
                        and not getattr(client, 'owner_leave_pending', False)):
                    self.return_to_hangar(addr, client)
            return
        if arena and arena.building:
            arena = None  # Account keeps its own clock until the world is ready.
        frame = arena.motion_frame if arena and arena.started else None
        tick = (frame.tick if frame is not None else
                game_tick(arena.simulation_time if arena else now,
                          arena.epoch if arena else client.clock_start))
        motion_due = client.account_created and tick != client.last_sync_tick
        messages = bytearray()
        reliable = False
        if motion_due:
            client.last_sync_tick = tick
            messages += tick_sync(tick)
        if client.arena_spawn_at and now >= client.arena_spawn_at:
            client.arena_spawn_at = 0
            # Mercury can span a message body across fragments, but its native
            # iterator requires the whole message header in one packet. Keep
            # entity creation units intact when batching the spawn stream.
            player = arena.players[client.account_id]
            batches = spawn_batches(arena, player, include_owner_state=not client.reconnect_replay)
            required = sum((len(batch)+899)//900 for batch in batches)
            if client.reliable_window_used+required > 256:
                raise ChannelOverflow('Reliable spawn window exceeded')
            if messages:
                self.send(addr, client, bytes(messages), reliable=False)
                messages.clear()
            for batch in batches:
                self.send(addr, client, batch)
            client.battle_spawned = True
            # HUD bindings initialize after entity creation. A one-time
            # BattleHintsInfo refresh then exposes the native shell selector.
            client.battle_hints_at = now + 4.
        if arena and client.battle_spawned:
            if (client.battle_hints_at and arena.started and now >= client.battle_hints_at
                    and arena.players[client.account_id].loaded and not arena.finished_at):
                # Avatar17 arrives with READY, before the native HUD creates
                # its BattleHints bindings.  Replaying the current private
                # ammunition state immediately after BattleHintsInfo makes the
                # shell column attach to those bindings; Avatar7 selects the
                # matching row.  This is a snapshot only, so it neither
                # changes the rack nor restarts a reload timer.
                vehicle = arena.vehicle_for(client.account_id)
                self.send(addr, client, battle_hints_message() + ammo_message(vehicle)
                          + avatar_method(7, struct.pack('<BI', 0, vehicle.shell_id)))
                client.battle_hints_at = 0.
            vehicle = arena.vehicle_for(client.account_id)
            if motion_due and arena.started:
                messages += motion_messages(arena,arena.players[client.account_id],frame)
                p = frame.samples[vehicle.entity_id].pose if frame is not None else vehicle.pose
                self.server.event('training_motion',tick=tick,entity_id=vehicle.entity_id,
                    step_count=frame.step_count if frame is not None else arena.step_count,
                    label_residual_ms=1000*((frame.at if frame is not None else arena.simulation_time)
                                            -arena.epoch-tick/UPDATE_HZ),
                    position=p.position,yaw=p.yaw,pitch=p.pitch,roll=p.roll,
                    gun_yaw=vehicle.gun.yaw,gun_pitch=vehicle.gun.pitch,
                    contact=arena.world.last_contact if arena.world else None,
                    speed=vehicle.speed,flags=vehicle.flags)
            if arena.postbattle_complete:
                if messages:
                    self.send(addr,client,bytes(messages),reliable=reliable)
                    messages.clear()
                self.return_to_hangar(addr,client)
        if messages:
            self.send(addr, client, bytes(messages),reliable=reliable)

    def sync_reports(self, addr, client, now):
        store = None
        for unique_id,sequences in list(client.report_ack.items()):
            if not any(seq in client.pending for seq in sequences):
                store = self.server.accounts.store
                store.acknowledge_notification(client.account_id,unique_id)
                del client.report_ack[unique_id]
                self.server.event('result_notification_ack',account_id=client.account_id,unique_id=unique_id)
        if (client.phase!='account' or not client.account_created or client.account_revision is None
                or client.report_ack or now<client.report_scan_at):
            return
        client.report_scan_at = now+1
        store = store or self.server.accounts.store
        notification = store.pending_notification(client.account_id)
        if notification is None:
            return
        from .account import balance_update
        from .results import account_notification
        report,created = notification
        progress = store.get(client.account_id).progress
        body = b'\x13'
        if client.account_revision!=progress['revision']:
            garage = (self.progression.prepare(client.account_id)
                      if getattr(self, 'progression', None) is not None else None)
            body += balance_update(client.account_revision,progress,garage=garage)
        body += account_notification(report,created)
        first = client.sequence
        self.send(addr,client,body)
        client.report_ack[report['unique_id']] = tuple((first+i)&0x0fffffff
            for i in range((len(body)+899)//900))
        client.account_revision = progress['revision']
        self.server.event('result_notification_sent',account_id=client.account_id,unique_id=report['unique_id'])

    def retry_pending(self, addr, client, now):
        for seq, (data, sent, attempts) in list(client.pending.items()):
            interval = min(2.,client.rto*2**(attempts-1))
            if now - sent >= interval:
                if attempts>=8 and now>=client.load_grace_until:
                    raise ChannelTimeout('Reliable delivery timed out')
                self.transport.sendto(data, addr)
                client.pending[seq] = (data, now, attempts + 1)
                client.retransmissions += 1

    def send(self, addr, client, body, acks=(), *, reliable=True):
        if not body:
            reliable = False
        if len(body) > 900 * 128:
            raise ValueError("Outgoing bundle exceeds size limit")
        chunks = _account_chunks(body)
        if chunks is None:
            chunks = [body[n:n+900] for n in range(0, len(body), 900)] or [b""]
        if not reliable and len(chunks)>1:
            raise ValueError('Unreliable snapshots must fit one packet')
        if reliable and client.reliable_window_used+len(chunks)>256:
            raise ChannelOverflow("Reliable send window exceeded")
        first = client.sequence
        last = (first + len(chunks) - 1) & 0x0fffffff
        for index, chunk in enumerate(chunks):
            self.send_packet(addr, client, chunk, acks if index == 0 else (),
                             (first, last) if len(chunks) > 1 else None,reliable=reliable)

    def require_vehicle_available(self, account_id, inventory_id):
        # Leaving a running arena detaches the session, not its vehicle or ammo.
        for arena in self.arenas.arenas.values():
            player = arena.players.get(account_id)
            if (player is not None and player.inventory_id == inventory_id
                    and (not arena.finished_at or (arena.started
                         and account_id not in arena.recorded_accounts))):
                raise ValueError("This vehicle is still in a battle or awaiting its result")

    def require_arena_capacity(self):
        limit = getattr(self, 'max_arenas', 0)
        if not limit:
            return
        # A detached owner's world still occupies memory until close completes.
        occupied = set(self.arenas.arenas)
        occupied.update(getattr(getattr(self, 'retirement', None), 'jobs', {}))
        occupied.update(getattr(self.__dict__.get('owner_service'), 'jobs', {}))
        if len(occupied) >= limit:
            raise BattleRefused('Server arena capacity reached')

    def start_battle(self, addr, client, inventory_id=None, map_id=None, *, game_mode=None,
                     training=None):
        if not getattr(self, 'matchmaking_enabled', False):
            return self._prepare_battle(addr, client, inventory_id, map_id,
                                        game_mode=game_mode, training=training)
        if client.arena_id or client.phase != 'account' or client.queue_pending:
            return
        self.send(addr, client, b'\x13' + random_queue_message(True))
        client.phase = 'battle_queue'
        client.queue_pending = True

        def prepare():
            client.queue_handle = None
            if (not self.matchmaking_enabled or self.clients.get(addr) is not client
                    or client.phase != 'battle_queue' or not client.queue_pending):
                return
            client.phase = 'account'
            try:
                self._prepare_battle(addr, client, inventory_id, map_id,
                                     game_mode=game_mode, training=training)
            except Exception as exc:
                client.queue_pending = False
                self.send(addr, client, b'\x13' + random_queue_message(False))
                if not isinstance(exc, BattleRefused):
                    self._report_fault('battle_preparation', exc, account_id=client.account_id)
                self.server.event('battle_request_refused', account_id=client.account_id,
                                  inventory_id=inventory_id, reason=str(exc))
                self.send_join_failure(addr, client, self.JOIN_WRONG_VEHICLE)

        # Flush the queue notification before synchronous recipe/asset work.
        client.queue_handle = asyncio.get_running_loop().call_later(.1, prepare)

    def cancel_battle_queue(self, addr, client):
        if not client.queue_pending:
            return
        if client.phase not in ('battle_queue', 'battle_building'):
            return
        if client.queue_handle is not None:
            client.queue_handle.cancel()
            client.queue_handle = None
        arena = self.arenas.get(client.arena_id)
        client.queue_pending = False
        client.phase, client.arena_id = 'account', None
        self.send(addr, client, b'\x13' + random_queue_message(False))
        if arena is not None and arena.building:
            if getattr(arena, 'owner_backed', False):
                self.owner_service.cancel(arena, 'Queue cancelled')
            else:
                self.loading.cancel(arena, 'Queue cancelled')
        self.server.event('battle_queue_cancelled', account_id=client.account_id)

    def _prepare_battle(self, addr, client, inventory_id=None, map_id=None, *, game_mode=None,
                        training=None):
        if client.arena_id:
            return
        account = self.server.accounts.get(client.account_id).identity()
        try:
            self.require_arena_capacity()
            if training is None:
                training = getattr(self.server, 'training_config', None)
                store = getattr(self.server.accounts, 'store', None)
                if store is not None:
                    training = store.training_config(client.account_id, training)
            if game_mode is not None:
                from .training_config import TrainingConfig
                training = (training or TrainingConfig()).for_game_mode(game_mode)
            if training is not None and map_id in (None, -1, 0):
                if training.bots_enabled and training.map is None:
                    from secrets import choice
                    available = tuple(ident for ident, key in self.map_catalog.items()
                        if key not in WEATHER_MAPS and (training.battle_mode != 'supremacy'
                            or self.resolve_map_spec(ident).strategic_points))
                    if not available:
                        raise ValueError('No available maps support this battle mode')
                    map_id = choice(available)
                else:
                    map_id = training.map_id(self.map_catalog)
            spec = self.selected_map(map_id)
            if (training is not None and training.bots_enabled and training.battle_mode == 'supremacy'
                    and not getattr(spec, 'strategic_points', ())):
                raise ValueError('This map has no Supremacy capture points')
            if (prewarm := getattr(self, 'map_prewarm', None)) is not None:
                prewarm.selected(spec.map_id)
            runtime = self.map_runtime(spec.map_id)
            options = dict(terrain=runtime['terrain'], map_spec=spec,
                           collision_scene=runtime['collision_scene'], prebattle_seconds=7.)
            bot_enabled = getattr(self.server, 'training_bot',
                                  getattr(self.server, 'manifest', {}).get('training_bot', False))
            if type(bot_enabled) is not bool:
                raise ValueError('Invalid training bot setting')
            if bot_enabled:
                options['bot_enabled'] = True
            if training is not None:
                options['training'] = training
            definition = getattr(self, 'definition', None)
            if getattr(self, 'progression', None) is not None:
                self.progression.prepare(client.account_id)
                if inventory_id is not None:
                    self.progression.select(client.account_id, inventory_id)
                stored = self.progression.selected_inventory(client.account_id)
                definition = self.progression.battle_definition(stored['descriptor'], stored['shells'])
                self.require_vehicle_available(client.account_id, stored['id'])
                from .loadout_effects import apply_definition
                crew = self.crew_metadata(definition)
                definition = apply_definition(self.server.manifest['game'], definition,
                                              stored.get('loadout'), **crew)
                from .consumables import resolve_loadout
                account.update(inventory_id=stored['id'],shells=tuple(stored['shells']),
                               consumables=resolve_loadout(self.server.manifest['game'], definition, stored.get('loadout'),
                                                           **crew))
                options.update(collision_scene=self.scene_for_vehicle(definition, spec.map_id),
                               descriptor=definition.descriptor)
            bots = training.bots_enabled if training is not None else bot_enabled
            if not bots and definition is not None and self.arenas.sandbox_enabled:
                options['bot_game'] = self.game
            if bots and definition is not None:
                import uuid
                from .bot_roster import select_bot_roster
                from .training_config import TrainingConfig
                config = training or TrainingConfig(mode='bots')
                training = config
                identity = uuid.uuid4().hex
                from .map_catalog import map_team_capacity
                capacity = map_team_capacity(spec.map_id)
                recipe = select_bot_roster(Path(self.server.manifest['game']), definition,
                    enemies=min(capacity, config.enemies), allies=min(capacity-1, config.active_allies),
                    seed=identity)
                options.update(result_id=identity, bot_roster=recipe,
                    bot_game=Path(self.server.manifest['game']),
                    training=config,
                    bot_scene_settings=self.bot_scene_settings(recipe, options['collision_scene']))
            # Validate the selected vehicle before changing idle room membership.
            self.rooms.leave(client.account_id)
            if getattr(self, 'arena_owners_enabled', False):
                mirror_options = {key: value for key, value in options.items()
                                  if key not in ('collision_scene', 'descriptor')}
                arena = self.arenas.create([account], incremental=False,
                                           collision_scene=options['collision_scene'],
                                           descriptor=definition.descriptor, build_world=False,
                                           **mirror_options)
                factory = self.owner_service.make_factory(
                    arena, [account], {client.account_id: definition},
                    {client.account_id: options['collision_scene']}, training)
            else:
                arena = self.arenas.create([account],
                                          incremental=hasattr(self,'loading'), **options)
        except ValueError as exc:
            raise BattleRefused(str(exc)) from exc
        if getattr(self, 'arena_owners_enabled', False):
            self.owner_service.start(arena, [(addr, client)], factory)
        else:
            self.begin_load(arena,[(addr,client)])
        self.server.event("training_requested",arena_id=arena.result_id,
                          map_id=spec.map_id,map_key=spec.key)

    # Account.onJoinFailure (method 24): u8 reason + packed BWstring; the pinned
    # client releases the hangar queue wait and shows the localized dialog.
    JOIN_WRONG_VEHICLE, JOIN_WRONG_ARENA_STATE = 4, 8

    def send_join_failure(self, addr, client, reason):
        self.send(addr, client, b"\x13" + account_method(24, bytes([reason]) + packed_blob(b"")))

    def start_shared(self, account_ids, teams=(1,2), map_id=None, *, bot_counts=(0, 0),
                     room_mode='encounter', room_settings=None):
        """Attach independent sessions to one authoritative world."""
        self.require_arena_capacity()
        from .roster import validate_teams
        from .rooms import room_bot_counts
        from .map_catalog import map_team_capacity
        from .training_config import BATTLE_MODES
        if room_mode not in BATTLE_MODES:
            raise ValueError('Choose a supported room battle mode')
        bot_counts = room_bot_counts(bot_counts)
        validate_teams([*teams, *([1]*bot_counts[0]), *([2]*bot_counts[1])])
        if not account_ids or len(account_ids)!=len(teams) or len(set(account_ids))!=len(account_ids):
            raise ValueError('Every battle participant must have a distinct account and a team')
        selected = []
        for account_id in account_ids:
            matches = [(addr,c) for addr,c in self.clients.items() if c.account_id == account_id]
            if len(matches) != 1:
                raise ValueError('Every participant must have a connected game client')
            addr,client = matches[0]
            if client.phase != 'account' or not client.account_created or client.arena_id:
                raise ValueError('Every participant must be in the hangar')
            if client.reliable_window_used >= 250:
                raise ValueError('Participant channel is stalled')
            selected.append((addr,client))
        accounts = [dict(self.server.accounts.get(a).identity(), team=t) for a,t in zip(account_ids,teams)]
        spec = self.selected_map(map_id)
        capacity = map_team_capacity(spec.map_id)
        if any(teams.count(team) + bots > capacity for team, bots in enumerate(bot_counts, 1)):
            raise ValueError(f'This map supports up to {capacity} participants per team')
        if room_mode == 'supremacy' and not getattr(spec, 'strategic_points', ()):
            raise ValueError('This map has no Supremacy capture points')
        if (prewarm := getattr(self, 'map_prewarm', None)) is not None:
            prewarm.selected(spec.map_id)
        runtime = self.map_runtime(spec.map_id)
        options = dict(terrain=runtime['terrain'], map_spec=spec,
                       collision_scene=runtime['collision_scene'], prebattle_seconds=7.,
                       room_mode=room_mode, room_settings=room_settings,
                       bot_game=self.game if room_mode == 'gravity' else None)
        owners = getattr(self, 'arena_owners_enabled', False)
        definitions = ({account_id: self.definition for account_id in account_ids}
                       if owners and getattr(self, 'progression', None) is None else {})
        scenes = {account_id: runtime['collision_scene'] for account_id in account_ids}
        if getattr(self, 'progression', None) is not None:
            refreshed = set()
            for account_id in account_ids:
                room = self.rooms.current(account_id)
                if room is not None and not room.arena_id and room.id not in refreshed:
                    refreshed.add(room.id)
                    for member in room.members.values():
                        self.native_rooms.refresh_vehicle(member.account_id)
                    if not all(m.ready for m in room.members.values() if m.account_id in account_ids):
                        raise ValueError('Garage configuration changed; participants must be ready again')
            for account in accounts:
                account_id = account['id']
                stored = self.progression.selected_inventory(account_id)
                self.require_vehicle_available(account_id, stored['id'])
                account.update(inventory_id=stored['id'],shells=tuple(stored['shells']))
                definition = self.progression.battle_definition(stored['descriptor'], stored['shells'])
                from .loadout_effects import apply_definition
                crew = self.crew_metadata(definition)
                definition = apply_definition(self.server.manifest['game'], definition,
                                              stored.get('loadout'), **crew)
                from .consumables import resolve_loadout
                account['consumables'] = resolve_loadout(self.server.manifest['game'], definition, stored.get('loadout'),
                                                         **crew)
                definitions[account_id] = definition
                scenes[account_id] = self.scene_for_vehicle(definition, spec.map_id)
            options['vehicle_scenes'] = scenes
        if any(bot_counts):
            import uuid
            from .bot_roster import select_room_bot_roster
            identity = uuid.uuid4().hex
            definition = definitions.get(account_ids[0], self.definition)
            roster = select_room_bot_roster(self.game, definition, teams=teams,
                                           bot_counts=bot_counts, seed=identity)
            options.update(result_id=identity, bot_roster=roster, bot_game=self.game,
                           bot_scene_settings=self.bot_scene_settings(roster, scenes[account_ids[0]]))
        if owners:
            mirror_options = {key: value for key, value in options.items()
                              if key not in ('collision_scene', 'vehicle_scenes')}
            arena = self.arenas.create(accounts, solo=False, incremental=False,
                                       collision_scene=scenes[account_ids[0]], vehicle_scenes=scenes,
                                       build_world=False,
                                       descriptor=definitions[account_ids[0]].descriptor,
                                       **mirror_options)
            factory = self.owner_service.make_factory(
                arena, accounts, definitions, scenes, None)
            self.owner_service.start(arena, selected, factory)
        else:
            arena = self.arenas.create(accounts,solo=False,incremental=hasattr(self,'loading'), **options)
            self.begin_load(arena,selected)
        self.server.event('shared_requested',arena_id=arena.result_id,accounts=account_ids,
                          map_id=spec.map_id,map_key=spec.key,battle_mode=room_mode,
                          entities=list(arena.vehicles),teams=list(teams),bot_counts=list(bot_counts))
        return arena

    def progression_update(self, client, *, inventory=True, removed=(), removed_types=()):
        state = self.progression.prepare(client.account_id)
        progress = self.server.accounts.get(client.account_id).progress
        update = account_snapshot(client.account_revision,self.server.manifest['game'],
                                  progress,garage=state)
        # AccountData's recursive delta merge (146eab0) removes a key for None
        # (146edb8 -> 1481230); omission alone leaves old inventory entries intact.
        # A full snapshot already excludes removed vehicles; tombstones are
        # meaningful only when merging into an existing native cache.
        if client.account_revision is not None:
            for inventory_id in removed:
                for key in ('serialized','shells','eqs','prov','settings','shellsLayout'):
                    update['inventory'][1][key][inventory_id] = None
            for type_cd in removed_types:
                update['stats']['optDeviceSlots'][type_cd] = None
        # Even an early stats-only action needs to initialize the whole cache.
        if not inventory and client.account_revision is not None:
            update = {key:update[key] for key in ('prevRev','rev','stats')}
        encoded = account_update(update)
        client.account_revision = progress['revision']
        return encoded

    def vehicle_research(self, client, kind, body, request_id):
        """11.21 UnlockItemRequest: source vehicle int64, edge int32, empty tag."""
        try:
            if client.phase != 'account' or not client.account_created or client.arena_id:
                raise ValueError('Wait for your hangar before researching')
            if kind != 145 or len(body) != 17 or body[16:] != b'\0':
                raise ValueError('Unsupported vehicle research envelope')
            source, index = struct.unpack_from('<qi',body,4)
            self.progression.research_index(client.account_id,source,index)
            self.server.event('vehicle_researched',account_id=client.account_id,
                              source_type_cd=source,unlock_index=index)
            return self.progression_update(client,inventory=False)+command_response(request_id,0)
        except ValueError as exc:
            self.server.event('vehicle_research_rejected',account_id=client.account_id,reason=str(exc))
            return command_response(request_id,-1,message=str(exc))

    def vehicle_ammo(self, client, kind, body, request_id):
        """cmd109: atomically exchange a signed target layout at pinned server prices."""
        try:
            if client.phase != 'account' or not client.account_created or client.arena_id:
                raise ValueError('Wait for your hangar before changing ammunition')
            if (kind != 147 or len(body)<25 or body[4]<5 or body[4]>67
                    or len(body)!=5+4*body[4]
                    or struct.unpack_from('<HH',body) != (request_id,109)):
                raise ValueError('Unsupported ammunition loadout envelope')
            values = struct.unpack_from('<'+'i'*body[4],body,5)
            revision,inventory_id,length = values[:3]
            signed_pairs = values[3:]
            if revision != self.progression_catalog['rev'] or length!=len(signed_pairs) or length%2:
                raise ValueError('Unsupported ammunition revision or shell layout')
            self.require_vehicle_available(client.account_id, inventory_id)
            self.progression.exchange_ammo_loadout(client.account_id,inventory_id,list(signed_pairs),
                operation_id=_ammo_operation_id(client,kind,body,request_id))
            self.native_rooms.refresh_vehicle(client.account_id)
            self.server.event('vehicle_ammo_changed',account_id=client.account_id,
                              inventory_id=inventory_id,
                              shells=[abs(v) if i%2==0 else v for i,v in enumerate(signed_pairs)],
                              signed_shells=list(signed_pairs))
            return self.progression_update(client)+command_response(request_id,0)
        except ValueError as exc:
            self.server.event('vehicle_ammo_rejected',account_id=client.account_id,reason=str(exc))
            return command_response(request_id,-1,message=str(exc))

    def vehicle_ammo_setting(self, client, kind, body, request_id):
        """Native cmd107, Account base5: <HHqii>, per-vehicle auto-resupply flag4."""
        try:
            if client.phase != 'account' or not client.account_created or client.arena_id:
                raise ValueError('Wait for your hangar before changing ammunition settings')
            if (kind != 140 or len(body) != 20
                    or struct.unpack_from('<HH',body) != (request_id,107)):
                raise ValueError('Unsupported ammunition settings envelope')
            inventory_id, setting, enabled = struct.unpack_from('<qii',body,4)
            if setting != 4 or enabled not in (0,1):
                raise ValueError('Unsupported ammunition setting or enabled value')
            self.require_vehicle_available(client.account_id, inventory_id)
            self.progression.set_ammo_setting(client.account_id,inventory_id,setting,bool(enabled),
                operation_id=_ammo_operation_id(client,kind,body,request_id))
            self.server.event('vehicle_ammo_setting_changed',account_id=client.account_id,
                              inventory_id=inventory_id,setting=setting,enabled=bool(enabled))
            return self.progression_update(client)+command_response(request_id,0)
        except ValueError as exc:
            self.server.event('vehicle_ammo_setting_rejected',account_id=client.account_id,reason=str(exc))
            return command_response(request_id,-1,message=str(exc))

    def vehicle_loadout(self, client, kind, body, request_id, command_id):
        """Pinned native equipment unlock and provision/consumable slot requests."""
        try:
            from .loadouts import load_catalog, vehicle_key, OPTIONAL_DEVICE, PROVISION, CONSUMABLE, COMPATIBLE
            if client.phase != 'account' or not client.account_created or client.arena_id:
                raise ValueError('Wait for your hangar before changing loadout')
            if command_id == 133:
                if kind != 141 or len(body) != 24:
                    raise ValueError('Unsupported equipment selection envelope')
                inventory_id, a, b, option = struct.unpack_from('<q3i', body, 4)
            else:
                if kind != 147 or len(body) != 25 or body[4] != 5:
                    raise ValueError('Unsupported vehicle loadout envelope')
                revision, inventory_id, a, b, option = struct.unpack_from('<5i', body, 5)
                if revision != self.progression_catalog['rev']:
                    raise ValueError('Loadout catalog has changed')
            self.require_vehicle_available(client.account_id, inventory_id)
            state = self.progression.prepare(client.account_id)
            vehicle = next((v for v in state['vehicles'] if v['id'] == inventory_id), None)
            if vehicle is None:
                raise ValueError('Vehicle is not in the garage')
            catalog = load_catalog(self.server.manifest['game'])
            nation, name = vehicle_key(catalog.game, vehicle['type_cd'])
            garage = self.progression.garage
            if command_id in (130, 133):
                if not 0 <= a < 3 or not 0 <= b < 3 or option not in (0, 1):
                    raise ValueError('Invalid equipment slot or selection')
                slot = a * 3 + b
                ident = catalog.equipment_slots(catalog.vehicle_preset(nation, name))[slot][option]
                item = catalog.lookup(OPTIONAL_DEVICE, ident)
                if command_id == 130:
                    from .catalog import equipment_slot_credits
                    tier = self.progression.resolve_type(vehicle['type_cd']).tier
                    garage.unlock_device_slot(client.account_id, inventory_id, slot, device_tag=item.tag,
                                              credits_cost=equipment_slot_credits(tier, a),
                                              require_previous_levels=True)
                else:
                    current = garage.loadout(client.account_id, inventory_id)['devices']
                    current[slot] = item.tag
                    garage.set_loadout(client.account_id, inventory_id, 'devices', current,
                                       allowed_tags=[i.tag for i in catalog.optional_devices])
            else:
                from .catalog import SUPPLY_SELL_PRICE_FACTOR, supply_credits
                category, item_kind, low = (('provisions', PROVISION, 0xfd) if command_id == 110
                                             else ('consumables', CONSUMABLE, 0xfb))
                slot_counts = catalog.vehicle_slots(nation, name)
                slot_count = slot_counts[1 if command_id == 110 else 0]
                if not 0 <= a < slot_count or option not in (0, 1):
                    raise ValueError('Invalid loadout slot or purchase flag')
                if b and (b < 0 or b & 255 != low):
                    raise ValueError('Invalid loadout item descriptor')
                item = catalog.lookup(item_kind, b >> 8) if b else None
                definition = self.progression._decode(vehicle['descriptor'], vehicle['shells']) if item else None
                compatible = (catalog.provision_compatibility(item.ident, nation, name, clip=definition.weapon.clip)
                              if item and command_id == 110 else
                              catalog.compatibility(item_kind, item.ident, nation, name, clip=definition.weapon.clip)
                              if item else COMPATIBLE)
                if compatible != COMPATIBLE:
                    raise ValueError('This item is incompatible or requires an unsupported vehicle filter')
                from .consumables import BATTLE_CONSUMABLES, resolve_loadout
                previous = garage.loadout(client.account_id, inventory_id)[category]
                current = previous.copy()
                current[a] = item.tag if item else None
                if command_id == 110 and item:
                    from .loadout_effects import resolve_provisions
                    resolve_provisions(catalog.game, definition, dict(provisions=current))
                elif item:
                    if command_id != 111 or item.tag not in BATTLE_CONSUMABLES:
                        raise ValueError('Battle effect is not implemented yet: ' + item.tag)
                    resolve_loadout(catalog.game, definition, dict(consumables=current),
                                    **self.crew_metadata(definition))
                entries = {entry.tag: entry for entry in catalog._items(item_kind)}
                tier = self.progression.resolve_type(vehicle['type_cd']).tier
                refund = int(supply_credits(entries.get(previous[a]), tier) * SUPPLY_SELL_PRICE_FACTOR)
                garage.set_loadout(client.account_id, inventory_id, category, current, allowed_tags=entries,
                                   credits_cost=supply_credits(item, tier), credits_refund=refund,
                                   expected_values=previous)
            self.server.event('vehicle_loadout_changed', account_id=client.account_id,
                              inventory_id=inventory_id, command_id=command_id)
            return self.progression_update(client)+command_response(request_id, 0)
        except ValueError as exc:
            self.server.event('vehicle_loadout_rejected', account_id=client.account_id, reason=str(exc))
            return command_response(request_id, -1, message=str(exc))

    def vehicle_purchase(self, client, kind, body, request_id):
        """Observed stock BuyVehicle: shop revision, type CD, flags 0x11, empty tag."""
        try:
            if client.phase != 'account' or not client.account_created or client.arena_id:
                raise ValueError('Wait for your hangar before buying a vehicle')
            if kind != 146 or len(body) != 21 or body[20:] != b'\0':
                raise ValueError('Unsupported vehicle purchase envelope')
            shop_revision, target, flags = struct.unpack_from('<qii',body,4)
            if shop_revision != self.progression_catalog['rev'] or flags != 0x11:
                raise ValueError('Unsupported shop revision or vehicle purchase options')
            research = self.progression.resolve_type(target)
            state = self.progression.prepare(client.account_id)
            if len(state['vehicles']) >= 5 and not any(v['type_cd']==target for v in state['vehicles']):
                raise ValueError('No vacant garage slot; buying slots is not supported')
            self.progression.buy(client.account_id,research.key)
            self.server.event('vehicle_purchased',account_id=client.account_id,type_cd=target)
            return self.progression_update(client)+command_response(request_id,0)
        except ValueError as exc:
            self.server.event('vehicle_purchase_rejected',account_id=client.account_id,reason=str(exc))
            return command_response(request_id,-1,message=str(exc))

    def vehicle_sale(self, client, kind, body, request_id):
        """Observed SellVehicle cmd304: shop revision int64, inventory ID, flags0."""
        try:
            if client.phase != 'account' or not client.account_created or client.arena_id:
                raise ValueError('Wait for your hangar before selling a vehicle')
            if kind != 140 or len(body) != 20:
                raise ValueError('Unsupported vehicle sale envelope')
            shop_revision, inventory_id, flags = struct.unpack_from('<qii',body,4)
            if shop_revision != self.progression_catalog['rev'] or flags != 0:
                raise ValueError('Unsupported shop revision or vehicle sale options')
            self.require_vehicle_available(client.account_id, inventory_id)
            old = self.progression.prepare(client.account_id)
            vehicle = next((v for v in old['vehicles'] if v['id'] == inventory_id), None)
            if vehicle is None:
                raise ValueError('Vehicle is not in the garage')
            self.progression.sell(client.account_id,inventory_id)
            self.native_rooms.refresh_vehicle(client.account_id)
            self.server.event('vehicle_sold',account_id=client.account_id,inventory_id=inventory_id)
            return self.progression_update(client,removed=(inventory_id,),
                removed_types=(vehicle['type_cd'],))+command_response(request_id,0)
        except ValueError as exc:
            self.server.event('vehicle_sale_rejected',account_id=client.account_id,reason=str(exc))
            return command_response(request_id,-1,message=str(exc))

    def module_purchase(self, client, kind, body, request_id):
        """Observed BuyAndEquipItem: cmd308, vector(shopRev, itemCD, invID, pairedCD)."""
        try:
            if client.phase != 'account' or not client.account_created or client.arena_id:
                raise ValueError('Wait for your hangar before changing modules')
            if kind != 147 or len(body) != 21 or body[4] != 4:
                raise ValueError('Unsupported module purchase envelope')
            shop_revision, target, inventory_id, paired = struct.unpack_from('<4i',body,5)
            if shop_revision != self.progression_catalog['rev'] or paired != 0:
                raise ValueError('Unsupported shop revision or paired module request')
            state = self.progression.prepare(client.account_id)
            vehicle = next((v for v in state['vehicles'] if v['id']==inventory_id),None)
            if vehicle is None:
                raise ValueError('Vehicle is not in your garage')
            self.require_vehicle_available(client.account_id, inventory_id)
            from .progression import item_cd
            research = self.progression.resolve_type(vehicle['type_cd'])
            module = next((m for m in research.modules
                           if item_cd(m.slot,m.ident,research.nation_id)==target),None)
            if module is None:
                raise ValueError('Module does not belong to this vehicle')
            self.progression.research_and_install(client.account_id,inventory_id,module.slot,module.tag)
            self.native_rooms.refresh_vehicle(client.account_id)
            # Publish authoritative inventory/research before the request callback.
            update = self.progression_update(client)
            self.server.event('module_purchased',account_id=client.account_id,
                              inventory_id=inventory_id,item_cd=target,revision=client.account_revision)
            return update+command_response(request_id,0)
        except ValueError as exc:
            self.server.event('module_purchase_rejected',account_id=client.account_id,reason=str(exc))
            return command_response(request_id,-1,message=str(exc))

    def begin_load(self,arena,selected):
        for addr,client in selected:
            client.arena_id,client.phase = arena.result_id,'battle_building'
            client.battle_spawned = False
            client.battle_hints_at = 0.
            client.result_sent = False
            client.reconnect_replay = False
        if hasattr(self,'loading'):
            try:
                self.loading.begin(arena,selected)
            except Exception:
                from .arena_loading import ArenaLoad
                self.fail_load(ArenaLoad(arena,selected,time.monotonic()),'World loader is unavailable')
                raise
        else:
            # Synchronous callers without a network loop retain the same API.
            from .arena_loading import ArenaLoad
            self.finish_load(ArenaLoad(arena,selected,time.monotonic()))

    def validate_load(self,job):
        if self.arenas.get(job.arena.result_id) is not job.arena:
            raise ValueError('Arena reservation no longer exists')
        for addr,client in job.context:
            if (self.clients.get(addr) is not client or client.arena_id!=job.arena.result_id
                    or client.phase!='battle_building' or not client.account_created):
                raise ValueError('Participant session changed during world construction')
            if client.reliable_window_used>=250:
                raise ValueError('Participant channel is stalled')

    def finish_load(self,job):
        self.validate_load(job)
        for addr,client in job.context:
            client.queue_pending = False
            client.phase = 'battle_pending'
            client.load_grace_until = time.monotonic()+self.ENTITY_LOAD_GRACE
        for addr,client in job.context:
            self.send(addr,client,b'\x04\0')
        world = getattr(job.arena, 'world', None)
        detail = getattr(getattr(world, 'assets', None), 'cache_metrics', {})
        self.server.event('arena_world_ready',arena_id=job.arena.result_id,
                          cache=detail.get('cache', 'disabled'),
                          duration_ms=(time.monotonic()-job.started)*1000,
                          slices=job.slices,max_slice_ms=job.max_slice*1000,
                          work_ms=job.work_seconds*1000)
        if world is not None:
            navigation = getattr(getattr(job.arena, 'bot_director', None), 'navigation', None)
            self.server.event('arena_build_profile', arena_id=job.arena.result_id,
                              navigation_ms=getattr(job.arena, 'navigation_build_ms', 0.),
                              navigation_cpu_ms=getattr(job.arena, '_navigation_cpu_ms', 0.),
                              **getattr(navigation, 'build_timings', {}),
                              **detail, **getattr(world, 'build_timings', {}))

    def fail_load(self,job,reason):
        arena = job.arena
        refused = []
        for addr,client in job.context:
            if self.clients.get(addr) is client and client.arena_id==arena.result_id:
                if client.queue_pending:
                    client.queue_pending = False
                    self.send(addr, client, b'\x13' + random_queue_message(False))
                # Only sessions still waiting without an Avatar get a refusal.
                if client.phase=='battle_building' and client.account_created:
                    refused.append((addr,client))
                client.arena_id,client.phase = None,'account'
                client.load_grace_until = 0.
                client.reconnect_replay = False
        for player in arena.players.values():
            player.attached = False
        self.arenas.remove_if_empty(arena)
        self.rooms.sync()
        self.server.event('arena_world_cancelled',arena_id=arena.result_id,reason=reason)
        for addr,client in refused:
            if self.clients.get(addr) is client and client.phase=='account' and not client.arena_id:
                try:
                    self.send_join_failure(addr,client,self.JOIN_WRONG_ARENA_STATE)
                except ChannelOverflow:
                    self._disconnect_client(addr,'reliable send window exceeded')

    def _withdraw_loading_player(self, arena, client):
        loader = self.owner_service if getattr(arena, 'owner_backed', False) else self.loading
        loader.withdraw_loading_player(arena, client)

    def return_to_hangar(self, addr, client):
        recon_transport = getattr(self, 'recon_transport', None)
        if recon_transport is not None:
            recon_transport.revoke(client)
        arena = self.arenas.get(client.arena_id)
        if not arena:
            return
        if arena.building:
            if arena.room_settings is not None:
                client.arena_id, client.phase = None, 'account'
                client.load_grace_until = 0.
                self._withdraw_loading_player(arena, client)
            elif getattr(arena, 'owner_backed', False):
                self.owner_service.cancel(arena, 'Return requested during owner startup')
            else:
                self.loading.cancel(arena,'Return requested during world construction')
            return
        self._reconnects().pop(client.account_id, None)
        if getattr(arena, 'owner_backed', False):
            if not getattr(client, 'owner_leave_pending', False):
                client.owner_leave_pending = True
                self.owner_service.leave(addr, client)
            return
        arena.leave(client.account_id,reason='leave')
        self.flush_arena(arena)
        client.arena_id = None
        client.arena_spawn_at = 0
        client.battle_spawned = False
        client.battle_hints_at = 0.
        client.reconnect_replay = False
        client.phase = "account_pending"
        client.load_grace_until = time.monotonic()+self.ENTITY_LOAD_GRACE
        client.clock_start = time.monotonic()
        client.last_sync_tick = -1
        if arena.finished_at:
            self.rooms.sync()
        self.arenas.remove_if_empty(arena)
        self.send(addr, client, b"\x04\0")

    def send_packet(self, addr, client, body, acks=(), fragment=None, *, reliable=True):
        now = time.monotonic()
        sequence = client.sequence if reliable else client.unreliable_sequence
        last_reliable = None if reliable else (client.sequence-1)&0x0fffffff
        if reliable:
            client.sequence = (sequence+1)&0x0fffffff
            client.last_reliable_sent = now
        else:
            client.unreliable_sequence = (sequence+1)&0x0fffffff
        client.last_sent = now
        data = encrypt_packet(encode_packet(body,sequence=sequence,acks=acks,fragment=fragment,
            reliable=reliable,last_reliable=last_reliable),client.key,codec=_packet_cipher_for(client))
        if reliable:
            client.pending[sequence] = (data,now,1)
            client.pending_peak = max(client.pending_peak,len(client.pending))
        self.transport.sendto(data, addr)
        recorder = diagnostics.get_recorder()
        if recorder is not None:
            recorder.record('udp_send', account=client.account_id, arena_id=client.arena_id,
                            bytes=len(data), reliable=reliable, packet_sequence=sequence,
                            pending=len(client.pending), retransmissions=client.retransmissions,
                            fragment=fragment is not None)

    def datagram_received(self, data, addr):
        recorder = diagnostics.get_recorder()
        if recorder is not None:
            client = self.clients.get(addr)
            recorder.record('udp_receive', bytes=len(data),
                            account=client.account_id if client is not None else None,
                            arena_id=client.arena_id if client is not None else None,
                            phase=client.phase if client is not None else None)
        if not self.server.network.allows(addr[0]):
            self.server.event('base_address_denied')
            return
        if len(data) > 4096:
            return
        client = self.clients.get(addr)
        try:
            if client:
                # Native client's outgoing packets carry a four-byte prefix.
                packet = parse_packet(decrypt_packet(data[4:], client.key, codec=_packet_cipher_for(client)))
                client.last_seen = time.monotonic()
                self.receive_packet(addr, client, packet)
                return
            else:
                packet = parse_packet(data, prefix=True)
            self.handle_elements(addr, client, packet)
        except ChannelOverflow:
            self._disconnect_client(addr, "reliable send window exceeded")
        except (ValueError, struct.error) as exc:
            self.server.event("base_rejected", reason=str(exc), size=len(data))
        except Exception as exc:
            # Unknown peers have no session to retire. Do not log packet
            # contents or crypto keys; the traceback carries code locations.
            current = self.clients.get(addr)
            failed = current if client is None else client
            self._report_fault('packet', exc, account_id=failed.account_id if failed else None)
            if failed is not None and current is failed:
                self._disconnect_client(addr, 'internal packet failure')

    def receive_packet(self, addr, client, packet):
        if self.clients.get(addr) is not client:
            return
        for piggyback in packet.piggybacks:
            self.receive_packet(addr, client, piggyback)
            if self.clients.get(addr) is not client:
                return
        now = time.monotonic()
        for seq in packet.acks:
            client.acknowledge(seq,now)
        if packet.cumulative_ack is not None:
            for seq in list(client.pending):
                if 0 < (packet.cumulative_ack - seq) & 0x0fffffff < 0x08000000:
                    client.acknowledge(seq,now)
        if packet.flags & RELIABLE:
            self.send(addr, client, b"", acks=(packet.sequence,))
        if packet.body:
            self.server.event("base_packet", flags=packet.flags, size=len(packet.body),
                              sequence=packet.sequence, fragment=packet.fragment)
        for bundle in client.inbox.receive(packet):
            if self.clients.get(addr) is not client:
                return
            self.handle_elements(addr, client, bundle)

    def handle_elements(self, addr, client, packet):
        if client is not None and self.clients.get(addr) is not client:
            return
        elements = list(base_elements(packet))
        if client and any(kind == 1 and struct.unpack("<I", body)[0] != client.session
                          for kind, _, body in elements):
            raise ValueError("BaseApp session mismatch")
        for kind, request, body in elements:
            if client is not None and self.clients.get(addr) is not client:
                return
            if not client:
                if kind != 0 or request is None:
                    raise ValueError("BaseApp login required")
                login_key, attempt = struct.unpack("<IB", body)
                issued = self.server.logins.get(login_key)
                if issued is None or issued[1] < time.time():
                    raise ValueError("Unknown or expired BaseApp login")
                for previous, active in list(self.clients.items()):
                    if active.account_id == issued[2]:
                        self._disconnect_client(previous, "replaced by a new login")
                client = Client(key=issued[0], session=secrets.randbits(32), account_id=issued[2])
                self.clients[addr] = client
                reply = encode_reply(packet, request, struct.pack("<I", client.session))
                self.transport.sendto(encrypt_packet(reply, client.key, codec=_packet_cipher_for(client)), addr)
                self.server.event("base_login_accepted", attempt=attempt, account_id=client.account_id,
                                  peer=f'{addr[0]}:{addr[1]}')
                if self._claim_reconnect(client, time.monotonic()):
                    # Rebuild Account services first, then replace it with the
                    # retained battle Avatar after full profile synchronization.
                    self.send(addr, client, b"\x04\0")
            else:
                self.server.event("base_element", kind=kind, size=len(body), request=request,
                                  account_id=client.account_id)
                if kind == 11:
                    # ClientInterface disconnect; no more timers or retries
                    # should target this native session after its goodbye.
                    self._disconnect_client(addr, "client disconnect")
                    return
                if client.phase == "battle" and kind == 136 and not body:
                    # Avatar base method 1: native "Leave battle" confirmation,
                    # also sent when the client acknowledges the after-battle phase.
                    self.return_to_hangar(addr, client)
                    continue
                arena = self.arenas.get(client.arena_id)
                if client.phase == 'battle' and kind == 166 and request is None:
                    from .sandbox import receive
                    if receive(self, addr, client, arena, body, time.monotonic()):
                        continue
                recon_transport = getattr(self, 'recon_transport', None)
                if (recon_transport is not None and client.phase == 'battle' and
                        kind == 166 and request is None and
                        recon_transport.receive(addr, client, body, time.monotonic())):
                    continue
                if arena:
                    if (client.phase=='battle' and kind==135 and not body
                            and not arena.players[client.account_id].loaded):
                        # The client can acknowledge world load before its last
                        # blocking render-resource stage. Only the first ready
                        # is progress; duplicates cannot extend the deadline.
                        client.load_grace_until = time.monotonic()+self.ENTITY_LOAD_GRACE
                    if kind == 145 and len(body) == 2:
                        self.server.event("training_input",entity_id=arena.players[client.account_id].vehicle_id,
                                          flags=struct.unpack("<H",body)[0])
                    if getattr(arena, 'owner_backed', False):
                        if client.phase == 'battle':
                            self.owner_service.submit(addr, client, kind, body)
                    elif client.phase == 'battle':
                        if client.reconnect_replay:
                            if kind == 135 and not body and client.battle_spawned:
                                arena.players[client.account_id].loaded = True
                                for batch in reconnect_batches(arena, arena.players[client.account_id]):
                                    self.send(addr, client, batch)
                                client.reconnect_replay = False
                        else:
                            dispatch_input(arena,client.account_id,kind,body)
                            self.flush_arena(arena)
                    if self.clients.get(addr) is not client:
                        return
                if client.phase == "battle" and 2 <= kind <= 254:
                    self.battle_captures.write(kind,body)
                if 135 <= kind <= 254:
                    self.rpc_captures.write(kind,body)
                    self.server.event("account_request", method=kind - 135, size=len(body))
                    if client.phase == "battle":
                        self.server.event("avatar_request", method=kind - 135, size=len(body))
                        continue
                    if kind == 150:
                        self.native_rooms.create(client,body)
                        continue
                    if kind == 137 and len(body) == 3:
                        request_id, token_type = struct.unpack("<HB", body)
                        accounts = self.server.accounts
                        token = accounts.issue(accounts.web, client.account_id) if token_type in (1, 2) else None
                        self.send(addr, client, b"\x13" + spa_token_response(
                            request_id, token_type, client.account_id, token))
                        self.server.event("spa_token_response", request_id=request_id,
                                          token_type=token_type, available=token is not None)
                        continue
                    command = command_header(kind - 135, body)
                    if command is not None:
                        request_id, command_id = command
                        self.server.event("account_command", request_id=request_id, command_id=command_id)
                        if command_id not in (107,109):
                            client.ammo_last_request = None
                        if command_id == 701 and client.queue_pending:
                            self.cancel_battle_queue(addr, client)
                            continue
                        if self.native_rooms.command(client,kind,body,request_id,command_id):
                            continue
                        if command_id == 700 and client.phase == "account":
                            if kind != 141 or len(body) != 24:
                                raise ValueError('Unsupported vehicle queue request')
                            map_id, queue, requested_inventory, flags, game_mode = struct.unpack_from('<5i', body, 4)
                            self.server.event('solo_queue_requested', account_id=client.account_id,
                                              map_id=map_id, queue=queue, flags=flags, mode=game_mode)
                            if ((queue, flags) != (0, 0) or game_mode not in (1, 7)
                                    or map_id not in (-1, 0, *self.map_catalog)):
                                raise ValueError('Unsupported solo queue options')
                            # Legacy profiles retain their fixed vehicle; the
                            # native mode/map options still apply per request.
                            inventory_id = requested_inventory if self.progression is not None else None
                            try:
                                self.start_battle(addr, client, inventory_id, map_id,
                                                  game_mode=game_mode)
                            except BattleRefused as exc:
                                # Native NO_RESPONSE(202) waits until JOIN_FAILURE arrives.
                                self.server.event("battle_request_refused", account_id=client.account_id,
                                                  inventory_id=inventory_id, reason=str(exc))
                                self.send_join_failure(addr, client, self.JOIN_WRONG_VEHICLE)
                            continue
                        if request_id == 202:
                            # The native reserved ID 202 means NO_RESPONSE.
                            continue
                        if kind == 140 and command_id == 100:
                            previous = struct.unpack_from("<q", body, 4)[0]
                            inventory = self.server.accounts.store.inventory(
                                client.account_id, self.server.manifest["game"], self.definition)
                            garage = (self.progression.prepare(client.account_id)
                                      if self.progression is not None else None)
                            progress = self.server.accounts.get(client.account_id).progress
                            # A newly created native Account needs full-sync
                            # observers, including saved hangar selection.
                            # prevRev=0 still means delta in client 11.21.
                            response = account_sync_response(request_id,
                                account_snapshot(None if client.account_revision is None else previous,
                                    self.server.manifest["game"],
                                    progress,
                                    inventory, garage=garage))
                            client.account_revision = progress['revision']
                            client.report_scan_at = time.monotonic()+1
                        elif command_id == 109 and self.progression is not None:
                            response = self.vehicle_ammo(client,kind,body,request_id)
                        elif command_id == 107 and self.progression is not None:
                            response = self.vehicle_ammo_setting(client,kind,body,request_id)
                        elif command_id in (110,111,130,133) and self.progression is not None:
                            response = self.vehicle_loadout(client,kind,body,request_id,command_id)
                        elif command_id == 201 and self.progression is not None:
                            response = self.vehicle_research(client,kind,body,request_id)
                        elif command_id == 301 and self.progression is not None:
                            response = self.vehicle_purchase(client,kind,body,request_id)
                        elif command_id == 304 and self.progression is not None:
                            response = self.vehicle_sale(client,kind,body,request_id)
                        elif command_id == 308 and self.progression is not None:
                            response = self.module_purchase(client,kind,body,request_id)
                        elif command_id == 1900:
                            # DiarySync, writer 0x1462170: doCmdStr with a raw
                            # Diary.proto. Field 25 records the 7x7 notice.
                            if kind != 144:
                                raise ValueError('Unsupported diary command envelope')
                            data, end = read_blob(body, 4)
                            if end != len(body):
                                raise ValueError('Trailing diary command bytes')
                            self.server.accounts.store.save_diary(client.account_id, data)
                            response = command_response(request_id, 0)
                        elif kind == 149 and command_id == 1650 and len(body) >= 13 and body[4] == 1:
                            operation = struct.unpack_from("<q", body, 5)[0]
                            data = local_http_response(operation)
                            self.server.event("account_http_request", operation=operation)
                            response = command_response(request_id, 0, data) if data is not None else command_response(
                                request_id, -2, message="Local HTTP operation is not implemented")
                        elif command_id == 1500:
                            # The pinned requester uses doCmdInt3: arena UID
                            # followed by two zero int32 arguments (0x1447a29).
                            # Retain the former UID-only local request as well.
                            valid = len(body)==12 or (len(body)==20 and body[12:]==b'\0'*8)
                            unique_id = struct.unpack_from('<Q',body,4)[0] if valid else None
                            report = (self.server.accounts.store.battle_report(client.account_id,unique_id)
                                      if valid else None)
                            if report is None:
                                response = command_response(request_id,-2,message='Battle result not found')
                            else:
                                from .results import document
                                response = resource_response(request_id,document(report))
                        elif command_id == 11339:
                            response = command_response(request_id, 0, {"didDeviceIDChange": False, "isWinBack": False})
                        elif command_id == 11336:
                            # DataPrivacyConfigs protobuf, all six local
                            # privacy feature flags keep their false defaults.
                            response = command_response(request_id, 0, raw=b"")
                        elif command_id == 11335:
                            response = command_response(request_id, 0,
                                raw=maps_configuration(tuple(self.map_catalog.values())))
                        elif command_id in (11210, 1150):
                            # Client reports downloaded arenas and tutorial
                            # progress. Preserve the report in the private lab.
                            path = self.server.lab / "private" / f"report-{command_id}.bin"
                            from .platform_io import open_private_file
                            fd = open_private_file(path.parent, path.name, create=True,
                                                   writable=True, truncate=True)
                            with os.fdopen(fd, "wb") as output:
                                output.write(body[4:])
                            response = command_response(request_id, 0)
                        elif command_id in (1000, 1670):
                            # Locale and input mode updates have no payload response.
                            response = command_response(request_id, 0)
                        else:
                            response = self.research_response(request_id, command_id)
                        self.send(addr, client, b"\x13" + response)
                        # The map-download report follows native hangar initialization.
                        # Fullsync100 alone is too early to replace this Account.
                        if (command_id == 11210 and client.reconnect_replay
                                and client.phase == 'account' and client.account_revision is not None):
                            client.phase = 'battle_pending'
                            self.send(addr, client, b'\x04\0')
                if kind == 9 and client.phase == "battle_pending":
                    arena = self.arenas.get(client.arena_id)
                    if getattr(arena, 'owner_backed', False):
                        self.owner_service.spawn(addr, client)
                    else:
                        client.phase = "battle"
                        client.load_grace_until = time.monotonic()+self.ENTITY_LOAD_GRACE
                        client.last_sync_tick = -1
                        self.send(addr,client,b"\x02"+bytes([UPDATE_HZ])+b"\x03"
                            +struct.pack('<I',game_tick(time.monotonic(),arena.epoch))
                            +initial_messages(arena,arena.players[client.account_id]))
                        client.arena_spawn_at = time.monotonic() + 2
                    self.server.event("avatar_creation_sent")
                elif kind == 9 and (not client.account_created or client.phase == "account_pending"):
                    client.account_created = True
                    client.phase = "account"
                    client.account_revision = None
                    client.load_grace_until = time.monotonic()+self.ENTITY_LOAD_GRACE
                    # A new Account is created after battle; advertise its
                    # retained room and restore the corresponding roster.
                    client.native_room_id = client.native_room_version = None
                    account = self.server.accounts.get(client.account_id).identity()
                    room = None if client.reconnect_replay else self.rooms.current(client.account_id)
                    room_id = int(room.id, 16) if room else 0
                    self.send(addr, client, b"\x02" + bytes([UPDATE_HZ]) + b"\x03"
                              + struct.pack("<I", game_tick(time.monotonic(), client.clock_start))
                              + create_account(account) + b"\x13" + account_method(8, b"") + show_gui(account, room_id))
                    if room is not None:
                        self.native_rooms.attach(client, room)
                    self.server.event("account_creation_sent")

    def research_response(self, request_id, command_id):
        if command_id == 300 and getattr(self, 'progression_catalog', None) is not None:
            shell_counts = {nation: len(kinds[10][0])
                            for nation, kinds in self.progression_catalog['items'].items()
                            if nation != 15 and 10 in kinds}
            self.server.event('progression_catalog_sent', revision=self.progression_catalog['rev'],
                              shell_count=sum(shell_counts.values()), nations=shell_counts)
            return resource_response(request_id,self.progression_catalog)
        path = self.server.lab / "private/command-responses.json"
        if path.exists() and not path.is_symlink() and path.stat().st_size < 131072:
            responses = json.loads(path.read_text())
            item = responses.get(str(command_id))
            if item is not None:
                if "stream_pickle" in item:
                    return resource_response(request_id, item["stream_pickle"])
                raw = bytes.fromhex(item["raw_hex"]) if "raw_hex" in item else None
                return command_response(request_id, int(item.get("result", 0)),
                                        item.get("pickle"), raw=raw)
        catalog = synchronized_catalog(command_id)
        if catalog is not None:
            return resource_response(request_id, catalog)
        return command_response(request_id, -1,
                                message=f"Local PoC: command {command_id} is not implemented")
