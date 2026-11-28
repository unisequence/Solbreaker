"""Serializable game-arena adapter for the production owner service."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import pickle
import struct
import time

from . import mad_games
from .arena import Arena, TrainingSpawnUnavailable
from .training_config import RoomBattleSettings
from .arena_commands import apply_at_tick, decode_input
from .arena_owner import (AddressedPayload, Command, CommandRejected, HandlerOutput,
                          MotionPayload, SettlementData, SettlementRecord, SpawnData)
from .battle import (event_messages, initial_messages, motion_messages, present_vehicles,
                     spawn_batches, reconnect_batches, world_motion_messages)
from .collision import CollisionScene
from .map_catalog import load_map_spec
from .map_cache import MapCache
from .sim_native import runtime_status
from .sandbox import training_reentry_enabled
from .terrain import MapTerrain
from .vehicle_data import VehicleDefinition, load_vehicle

_KINDS = {'ready': 135, 'control': 145, 'aim': 149, 'aim_entity': 16, 'aim_entity_local': 17, 'fire': 138,
          'autoaim': 13, 'autoaim_point': 18, 'aim_angles': 148, 'consumable': 20, 'select_shell': 147,
          'uprising_select': 21, 'uprising_deselect': 22}


def snapshot(arena: Arena, events=(), *, account_id=None, training_error=None) -> bytes:
    """Versioned parent observation, not a resumable physics checkpoint."""
    vehicles = (sorted(arena.vehicles.items()) if account_id is None else
                ((arena.players[account_id].vehicle_id, arena.vehicle_for(account_id)),))
    state = {
        'version': 1 if account_id is None else 2,
        'arena_id': arena.result_id,
        'epoch': arena.epoch,
        'started': arena.started,
        'prebattle_end': arena.prebattle_end,
        'step': arena.step_count,
        'motion_tick': arena.motion_frame.tick if arena.motion_frame is not None else None,
        'simulation_time': arena.simulation_time,
        'accumulator': arena.accumulator,
        'dropped_time': arena.dropped_time,
        'finished_at': arena.finished_at,
        'finish_reason': arena.finish_reason,
        'winner': arena.winner,
        'players': tuple((aid, p.attached, p.loaded, p.departure_reason)
                         for aid, p in sorted(arena.players.items())),
        'vehicles': tuple((eid, v.pose, v.speed, v.health, v.max_health, v.flags,
                           v.shots, v.hits, v.damage, v.ammo, v.clip, v.selected_shell,
                           v.reload_at, v.reload_total, v.engine_mode,
                           tuple(v.ammo_by_shell.items()), v.gun.yaw, v.gun.pitch)
                          for eid, v in vehicles),
        'game_events': tuple(events),
        'simulation': runtime_status(arena.world, arena.drive_workers),
    }
    if account_id is not None:
        state['account_id'] = account_id
        # Parent-side reconnect/fault handling can change flags or player
        # attachment while an earlier input is still outstanding. Keep the
        # complete cheap player/flag rows so observation restores the same
        # mirror as a full snapshot at this boundary.
        state['flags'] = tuple((eid, v.flags) for eid, v in sorted(arena.vehicles.items()))
    else:
        state['training_reentry'] = training_reentry_enabled(arena)
        if training_error is not None:
            state['training_error'] = training_error
        if getattr(arena, 'scenario_editor', False):
            state['practice_spawned'] = tuple(sorted(arena.practice_spawned))
            state['practice_client_ids'] = dict(arena.practice_client_ids)
            if getattr(arena, 'proving_ground', None):
                state['range_targets'] = tuple((eid, number, arena.vehicles[eid].nickname,
                                                arena.vehicles[eid].descriptor)
                                               for eid, number in arena.practice_numbers.items())
                state['range_score'] = dict(arena.proving_ground.score)
        capture = getattr(arena, 'base_capture', None)
        state['base_capture'] = capture.view() if capture is not None else None
        supremacy = getattr(arena, 'supremacy', None)
        state['supremacy'] = supremacy.state() if supremacy is not None else None
        state['capture_statistics'] = tuple((eid, v.capture_points, v.defense_points)
            for eid, v in sorted(arena.vehicles.items()))
    return pickle.dumps(state, protocol=5)


@dataclass(frozen=True, slots=True)
class TrainingTank:
    inventory_id: int
    definition: VehicleDefinition
    shells: tuple
    consumables: tuple
    scene_settings: dict
    camouflage: int = 0


class GameArenaHandler:
    def __init__(self, arena: Arena, scene: CollisionScene, *, compact_inputs=False, game=None):
        if type(compact_inputs) is not bool:
            raise ValueError('invalid compact input observation setting')
        self.arena = arena
        self.scene = scene
        self.game = game
        self._joined = set(arena.players)
        self._reconnecting = set()
        self._closed = False
        self.compact_inputs = compact_inputs

    def _observation(self, command, events=()):
        compact = (self.compact_inputs and command.op in ('control','aim','aim_entity',
                   'aim_entity_local','recon_aim','recon_clear','sandbox') and not events and not self.arena.events
                   and self.arena.started and not self.arena.finished_at)
        return snapshot(self.arena,events,account_id=command.account_id if compact else None)

    def _motions(self, recipients, frame):
        if not recipients:
            return ()
        if frame is None:
            return tuple(MotionPayload(account, None, b'') for account in recipients)
        arena = self.arena
        timeline = getattr(arena, 'time_stop', None)
        if arena.team_spotting:
            by_team = {team: world_motion_messages(arena, frame, team=team)
                       for team in {arena.players[account].team for account in recipients}}
            return tuple(MotionPayload(account, frame.tick, motion_messages(
                arena, arena.players[account], frame, world_messages=by_team[arena.players[account].team]))
                for account in recipients)
        world_messages = (world_motion_messages(arena, frame)
                          if timeline is None or recipients != (timeline.account,) else None)
        return tuple(MotionPayload(account, frame.tick,
            motion_messages(arena, arena.players[account], frame, world_messages=world_messages))
            for account in recipients)

    def preflight(self, command: Command) -> None:
        """Validate membership before any Arena mutation or event draining."""
        if self._closed:
            raise RuntimeError('arena handler closed')
        command.validate()
        if command.op not in ('step', 'motions') and command.account_id not in self._joined:
            raise CommandRejected('account not attached to arena')
        if command.op == 'motions':
            recipients = struct.unpack(f'<{len(command.payload)//4}I', command.payload)
            if any(account not in self._joined for account in recipients):
                raise CommandRejected('motion account not attached to arena')
        if command.op in ('leave', 'disconnect'):
            player = self.arena.players[command.account_id]
            if player.inventory_id is not None and self.arena.vehicle_for(command.account_id).loadout_shells is None:
                raise CommandRejected('missing immutable ammunition snapshot')
        if command.op in ('training_park', 'training_swap'):
            account = command.account_id
            player = self.arena.players[account]
            parked = account in self.arena.hangar_players
            if (not training_reentry_enabled(self.arena) or not player.attached
                    or not self.arena.training_tank_available(account)):
                raise CommandRejected('Training return is unavailable')
            if (command.op == 'training_park' and (parked or not player.loaded)
                    or command.op == 'training_swap' and not parked):
                raise CommandRejected('Invalid training player state')
        if command.op.startswith('recon_'):
            player = self.arena.players[command.account_id]
            team_snapshot = command.op == 'recon_snapshot' and self.arena.team_spotting
            if self.arena.recon is None and not team_snapshot:
                raise CommandRejected('recon unavailable')
            prebattle = not self.arena.started and self.arena.prebattle_seconds > 0
            if (command.op != 'recon_clear'
                    and not (player.attached and (prebattle or player.loaded and self.arena.started)
                             and not self.arena.finished_at
                             and (team_snapshot or self.arena.vehicle_for(command.account_id).alive))):
                raise CommandRejected('recon player not ready')

    def apply(self, command: Command) -> HandlerOutput:
        from .diagnostics import command as record_command
        record_command(self.arena, command)
        self.preflight(command)
        if command.op == 'motions':
            recipients = struct.unpack(f'<{len(command.payload)//4}I', command.payload)
            frame = self.arena.motion_frame if self.arena.started else None
            return HandlerOutput(self.arena.result_id, b'', (), motions=self._motions(recipients, frame))
        active = sorted(self._joined - self.arena.hangar_players)
        if command.op in ('spawn', 'reconnect'):
            player = self.arena.players[command.account_id]
            reconnect = command.op == 'reconnect'
            if reconnect:
                player.loaded = False
                self._reconnecting.add(command.account_id)
            batches = spawn_batches(self.arena, player, include_owner_state=not reconnect)
            data = SpawnData(self.arena.result_id, command.account_id, self.arena.epoch,
                             initial_messages(self.arena, player), batches)
            return HandlerOutput(self.arena.result_id, snapshot(self.arena), (), spawn=data)
        if command.account_id in self._reconnecting and (command.op in _KINDS or command.op == 'sandbox'):
            if command.op == 'ready':
                # Native GunRotator/GameContext is bound only after world ready.
                # Sample the live timers here, after the loading interval.
                player = self.arena.players[command.account_id]
                player.loaded = True
                outgoing = tuple(AddressedPayload(command.account_id, batch)
                                 for batch in reconnect_batches(self.arena, player))
                self._reconnecting.remove(command.account_id)
                self.arena._start_if_ready(command.at)
                return HandlerOutput(self.arena.result_id, snapshot(self.arena), outgoing)
            return HandlerOutput(self.arena.result_id, self._observation(command), ())
        if command.account_id in self.arena.hangar_players and (command.op in _KINDS or command.op == 'sandbox'):
            return HandlerOutput(self.arena.result_id, self._observation(command), ())
        if command.op in ('motion', 'motion_light'):
            frame = self.arena.motion_frame if self.arena.started else None
            # Parent owns tick_sync, per-client cadence, spawn and transport.
            # No frame exists before the arena starts.
            motion = MotionPayload(command.account_id, frame.tick if frame is not None else None,
                motion_messages(self.arena, self.arena.players[command.account_id], frame) if frame is not None else b'')
            state = b'' if command.op == 'motion_light' else snapshot(self.arena)
            return HandlerOutput(self.arena.result_id, state, (), motion=motion)
        if command.op in ('recon_aim', 'recon_clear'):
            if command.op == 'recon_aim':
                target = struct.unpack('<I', command.payload)[0]
                self.arena.recon_client_aim(command.account_id, target or None)
            else:
                self.arena.recon_client_clear(command.account_id)
            return HandlerOutput(self.arena.result_id, self._observation(command), ())
        if command.op == 'recon_snapshot':
            import uuid
            from .recon_snapshot import (RosterVehicle, build_recon_snapshot, build_team_spotting_snapshot,
                                         encode_arena_recon_snapshot)
            sequence = struct.unpack('<I', command.payload)[0]
            player = self.arena.players[command.account_id]
            timeline = getattr(self.arena, 'time_stop', None)
            battle_id = uuid.UUID(self.arena.result_id)
            view = timeline.recon_snapshot(player.vehicle_id, battle_id, sequence) if timeline else None
            if view is None and self.arena.team_spotting:
                view = build_team_spotting_snapshot(
                    self.arena, player.vehicle_id, battle_id=battle_id, sequence=sequence)
            if view is None:
                roster = tuple(RosterVehicle(vehicle.entity_id, vehicle.team, vehicle.alive)
                               for _, vehicle in present_vehicles(self.arena, player)
                               if not mad_games.concealed(vehicle, player.team))
                view = build_recon_snapshot(
                    self.arena.recon, roster, player.vehicle_id,
                    battle_id=battle_id, sequence=sequence, now=self.arena.simulation_time)
            payload = encode_arena_recon_snapshot(self.arena, view, player.vehicle_id)
            return HandlerOutput(self.arena.result_id, snapshot(self.arena), (), recon=payload)
        if command.op == 'step':
            self.arena.step(command.at, deadline=command.deadline)
        elif command.op == 'training_park':
            self.arena.reserve_training_seat(command.account_id)
            active.remove(command.account_id)
        elif command.op == 'training_swap':
            error = self._swap_training_tank(command)
            if error is not None:
                return HandlerOutput(self.arena.result_id, snapshot(self.arena, training_error=error), ())
        elif command.op == 'flush':
            pass
        elif command.op in ('leave', 'disconnect'):
            self.arena.leave(command.account_id, now=command.at,
                             reason='leave' if command.op == 'leave' else 'disconnect')
        elif command.op == 'sandbox':
            from .sandbox import Control
            self.arena.sandbox_control(command.account_id, Control.decode(command.payload))
        elif command.op == 'time_stop_cancel':
            self.arena.cancel_time_stop(command.account_id)
            self.arena.vehicle_for(command.account_id).flags = 0
        elif command.op in _KINDS:
            input_at = command.at if command.op == 'ready' else self.arena.last_tick
            decoded = decode_input(command.account_id, _KINDS[command.op], command.payload, input_at)
            if decoded is None:
                raise ValueError('unsupported arena input')
            apply_at_tick(self.arena, decoded)
        else:
            raise ValueError('unsupported arena operation')
        events = self.arena.drain_events()
        outgoing = []
        if events:
            for account_id in active:
                if account_id in self._reconnecting:
                    continue
                body = event_messages(self.arena, self.arena.players[account_id], events)
                if body:
                    outgoing.append(AddressedPayload(account_id, body))
        if command.op in ('leave', 'disconnect'):
            if command.op == 'leave':
                outgoing.append(AddressedPayload(command.account_id, b'\x04\0'))
            self._joined.remove(command.account_id)
            self._reconnecting.discard(command.account_id)
        results = tuple(self.arena.results())
        if results:
            from .results import snapshot as result_snapshot
            records = []
            for result in results:
                vehicle = self.arena.vehicle_for(result.account_id)
                records.append(SettlementRecord(result, result_snapshot(self.arena, result.account_id),
                    vehicle.descriptor, tuple(vehicle.loadout_shells) if vehicle.loadout_shells is not None else None,
                    tuple(vehicle.ammo_by_shell.items()), vehicle.shell_id, vehicle.ammo))
            settlement = SettlementData(self.arena.result_id, results, tuple(records))
        else:
            settlement = None
        motions = ()
        if command.motion_viewers and self.arena.started and (frame := self.arena.motion_frame) is not None:
            recipients = tuple(account for account, last_tick in command.motion_viewers
                               if last_tick != frame.tick and account in self._joined)
            motions = self._motions(recipients, frame)
        return HandlerOutput(self.arena.result_id, self._observation(command,events),
                             tuple(outgoing), settlement=settlement, motions=motions)

    def _swap_training_tank(self, command):
        tank = pickle.loads(command.payload)
        if type(tank) is not TrainingTank or type(tank.inventory_id) is not int or tank.inventory_id <= 0:
            raise ValueError('Invalid training tank recipe')
        arena = self.arena
        account = command.account_id
        current = arena.world.assets_for(arena.players[account].vehicle_id)
        scene = current
        if current.definition != tank.definition:
            try:
                scene = current.for_vehicle(self.game, tank.definition)
                GameArenaFactory._configure(scene, tank.scene_settings)
                scene.prepare(arena.terrain)
            except (ValueError, OSError):
                if scene is not current:
                    scene.close()
                return 'The selected tank resources could not be loaded'
        try:
            arena.reenter_training(account, scene, tank.shells, tank.consumables,
                                   camouflage=tank.camouflage)
        except TrainingSpawnUnavailable as exc:
            if scene is not current:
                scene.close()
            return str(exc)
        arena.training_swappers.add(account)
        arena.players[account].inventory_id = tank.inventory_id
        self._reconnecting.add(account)
        return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.arena.close()
        finally:
            self.scene.close()


class SceneGroup:
    def __init__(self, scenes):
        self.scenes = scenes

    def close(self):
        for scene in self.scenes:
            scene.close()


@dataclass(frozen=True, slots=True)
class GameArenaFactory:
    """Spawn-safe complete arena recipe; no live Bullet object crosses IPC."""
    game: Path
    nation: str
    vehicle: str
    map_key: str
    accounts: tuple[dict, ...]
    now: float
    solo: bool = False
    definition: object | None = None
    training: object | None = None
    map_spec: object | None = None
    player_definitions: tuple[tuple[int, object], ...] = ()
    scene_settings: tuple[tuple[int, dict], ...] = ()
    arena_id: str | None = None
    space_id: int = 1
    entity_base: int = 0
    recon_enabled: bool = False
    compact_inputs: bool = False
    bot_roster: tuple | None = None
    bot_scene_settings: tuple = ()
    custom_bot_slots: bool = False
    prebattle_seconds: float = 0.
    map_cache: MapCache | None = None
    sandbox_enabled: bool = False
    scenario_editor_accounts: tuple[int, ...] | None = None
    room_mode: str = 'encounter'
    room_settings: RoomBattleSettings | None = None

    @staticmethod
    def _configure(scene, settings):
        for name in ('internal_geometry', 'engine_spec', 'ammo_rack_spec', 'fuel_tank_spec',
                     'fire_policy', 'fire_min_damage', 'crew_specs', 'crew_bindings'):
            setattr(scene, name, settings.get(name))

    def __call__(self) -> GameArenaHandler:
        from .diagnostics import install
        install()
        started = time.monotonic()
        compact=getattr(self,'compact_inputs',False)
        if type(compact) is not bool:
            raise ValueError('invalid compact input observation setting')
        if self.solo and len(self.accounts) != 1:
            raise ValueError('solo owner requires exactly one participant')
        definition = self.definition if self.definition is not None else load_vehicle(self.game, self.nation, self.vehicle)
        definitions = dict(self.player_definitions)
        ids = tuple(account['id'] for account in self.accounts)
        if self.player_definitions and (len(definitions) != len(ids) or set(definitions) != set(ids)
                                        or any(item is None for item in definitions.values())):
            raise ValueError('player definitions must match the arena roster')
        spec = self.map_spec if self.map_spec is not None else load_map_spec(self.game, self.map_key)
        settings = dict(self.scene_settings)
        terrain = MapTerrain(self.game, spec)
        scenes = []
        arena = None
        try:
            scene_started = time.monotonic()
            scene = CollisionScene(self.game, definitions.get(ids[0], definition), spec,
                                   map_cache=self.map_cache, terrain=terrain)
            scenes.append(scene)
            self._configure(scene, settings.get(ids[0], {}))
            scene.prepare(terrain)
            scene_ms = (time.monotonic()-scene_started)*1000
            vehicle_scenes = {}
            if self.player_definitions:
                for account_id in ids:
                    entry = definitions[account_id]
                    if account_id == ids[0]:
                        vehicle_scenes[account_id] = scene
                        continue
                    other = scene.for_vehicle(self.game, entry)
                    scenes.append(other)
                    self._configure(other, settings.get(account_id, {}))
                    other.prepare(terrain)
                    vehicle_scenes[account_id] = other
            roster_started = time.monotonic()
            arena = Arena(list(self.accounts), definition.descriptor, terrain, scene,
                          vehicle_scenes=vehicle_scenes or None, map_spec=spec, now=self.now,
                          solo=self.solo, training=self.training, result_id=self.arena_id,
                          space_id=self.space_id, entity_base=self.entity_base,
                          recon_enabled=self.recon_enabled, incremental=True,
                          recon_game=self.game if self.recon_enabled else None,
                          bot_roster=self.bot_roster, bot_game=self.game,
                          bot_scene_settings=self.bot_scene_settings, custom_bot_slots=self.custom_bot_slots,
                          prebattle_seconds=self.prebattle_seconds,
                          sandbox_enabled=self.sandbox_enabled, room_mode=self.room_mode,
                          room_settings=self.room_settings,
                          scenario_editor_accounts=self.scenario_editor_accounts)
            roster_ms = (time.monotonic()-roster_started)*1000
            while arena.building:
                arena.advance_build()
        except BaseException:
            if arena is not None:
                arena.close()
            for resource in scenes:
                resource.close()
            raise
        handler = GameArenaHandler(arena, SceneGroup(scenes), compact_inputs=compact, game=self.game)
        navigation = getattr(arena.bot_director, 'navigation', None)
        handler.startup_metrics = dict(scene_ms=scene_ms, roster_ms=roster_ms,
            navigation_ms=getattr(arena, 'navigation_build_ms', 0.),
            navigation_cpu_ms=getattr(arena, '_navigation_cpu_ms', 0.),
            arena_ms=(time.monotonic()-started)*1000, **scene.cache_metrics,
            **getattr(navigation, 'build_timings', {}),
            **arena.world.build_timings)
        return handler
