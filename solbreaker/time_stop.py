"""Private physics and effect reconciliation for a Time Stop branch.

This module does not publish trajectories or effects to battle sessions. The
caller owns the simulation worker and schedules reconciliation on its arena tick.
"""
from collections import Counter
from copy import copy
from dataclasses import dataclass, replace
import json
import math
import os
import random
import uuid

from . import big_boss, mad_games
from .aiming import GunAim, disperse_shot, dispersion_tangent
from .ammunition import (advance_pump, apply_burst_shell_switch, apply_queued_shell,
                         finish_magazine_reload, magazine_reload_allowed, spend_round,
                         start_pump_refill, switch_shell)
from .ballistics import BodyState, Projectile, TrackContact, advance, body_states, missile_guidance_target
from .bot_roster import vehicle_class
from .collision import CollisionWorld, GroundMotion, Motion, Pose
from .consumables import READY, SHIELDED_SHELL_KINDS
from .entities import BattleEvent
from .map_catalog import map_team_capacity
from .mobility import (advance_speed, braking_acceleration, powered_acceleration,
                       rolling_acceleration, skid_velocity, slope_grip_acceleration, terrain_at)
from .orientation import matrix
from .roster import training_spawn, training_yaw
from .sandbox import BOOST_POWER, BRANCH_SHOT_BASE
from .vehicle_data import Shell

# A 15-second branch can never receive this many player input commands.
BURST_ACTION_BASE = 1 << 20


@dataclass(frozen=True)
class Level:
    energy: int
    cooldown_multiplier: float
    radius: float | None = None

    def __post_init__(self):
        if (type(self.energy) is not int or self.energy < 0
                or not math.isfinite(self.cooldown_multiplier) or self.cooldown_multiplier <= 0.
                or self.radius is not None and (not math.isfinite(self.radius) or self.radius <= 0.)):
            raise ValueError('Invalid Time Stop level')


LEVELS = (Level(50, 1.), Level(65, 1.5), Level(80, 2.),
          Level(100, 3.), Level(125, 4.))

ABILITY_RECIPES = {big_boss.BLAST_WAVE.ident: ('shockwave', 0)}
for _role, _kind in ((big_boss.BOMBER, 'explosion'), (big_boss.GRAVITON, 'graviton'),
                     (big_boss.REGENERATOR, 'healing'), (big_boss.CATALYST, 'poison')):
    ABILITY_RECIPES.update((spec.ident, (_kind, level))
                           for level, spec in enumerate(big_boss.ROLE_ACTIVE[_role]))


@dataclass(frozen=True)
class Rules:
    duration: float = 15.
    shot_cost: int = 5
    explosion_cost: int = 15
    shockwave_cost: int = 20
    graviton_cost: int = 20
    healing_cost: int = 20
    poison_cost: int = 15
    healing_refund: float = .5
    starting_level: int = 1

    def __post_init__(self):
        # The client receives the duration as unsigned 16-bit deciseconds.
        if type(self.duration) not in (int, float) or not 0. < self.duration <= 6553.5:
            raise ValueError('Invalid Time Stop duration')
        for cost in (self.shot_cost, self.explosion_cost, self.shockwave_cost,
                     self.graviton_cost, self.healing_cost, self.poison_cost):
            if type(cost) is not int or cost < 0:
                raise ValueError('Time Stop action costs must be nonnegative integers')
        if type(self.healing_refund) not in (int, float) or not 0. <= self.healing_refund <= 1.:
            raise ValueError('Invalid Time Stop healing refund')
        if type(self.starting_level) is not int or not 1 <= self.starting_level <= len(LEVELS):
            raise ValueError('Invalid Time Stop starting level')

    @classmethod
    def from_environment(cls):
        name = 'SOLBREAKER_TIME_STOP_RULES'
        try:
            overrides = json.loads(os.environ.get(name, '{}'))
            if not isinstance(overrides, dict):
                raise ValueError('expected a JSON object')
            return cls(**overrides)
        except (TypeError, ValueError) as error:
            raise ValueError(f'{name}: {error}') from error


@dataclass(frozen=True)
class Entity:
    ident: int
    generation: int

    @classmethod
    def of(cls, vehicle):
        return cls(vehicle.entity_id, vehicle.generation)


@dataclass(frozen=True)
class FrozenTank:
    identity: Entity
    body: BodyState
    team: int
    alive: bool
    sandbox_god_mode: bool
    gun_packed: bytes
    health: int
    max_health: int
    attached: bool
    mass: float
    tank_class: str | None
    damage_factor: float
    anti_he_factor: float
    shield_factor: float
    armor_mover_factor: float
    gun_chance: float
    regenerator_shield: big_boss.RegeneratorShield | None
    visible: bool = True


@dataclass(frozen=True, slots=True)
class ProjectileTrace:
    shot: int
    shooter: int
    position: tuple
    velocity: tuple
    gravity: float
    effect: int
    guided: bool
    shell_id: int

    @classmethod
    def capture(cls, projectile):
        shell = projectile.shell
        effect = shell.effect_index if projectile.trace_effect is None else projectile.trace_effect
        return cls(projectile.shot, projectile.shooter, projectile.position, projectile.velocity,
                   shell.gravity, effect, shell.missile is not None, shell.compact_id)


@dataclass(frozen=True)
class DriveSnapshot:
    speed: float
    motion: Motion
    ground_motion: GroundMotion
    road: Pose
    lean: tuple | None
    support_memory: tuple | None
    carrying_supported: bool
    speed_factor: float
    power_factor: float
    rotation_factor: float
    speed_bias: tuple
    broken_track: bool
    engine_destroyed: bool
    rigid_spin: tuple[float, float, float] | None = None

    @classmethod
    def capture(cls, arena, vehicle):
        world, entity = arena.world, vehicle.entity_id
        engine = arena.engines.get(entity)
        power = vehicle.engine_factor * (1.5 if arena.gravity_mode else 1.)
        if getattr(vehicle, 'sandbox_boost', False):
            power *= BOOST_POWER
        support = world._support_memory.get(entity)
        if support is not None:
            pose, heights, speeds = support
            support = (pose, heights.copy(), speeds.copy())
        return cls(vehicle.speed, copy(world.motion[entity]), copy(world.ground_motion[entity]),
                   world._road_poses[entity], world._lean_states.get(entity),
                   support, world._carrying_supported.get(entity, True),
                   arena._drive_speed_factor(vehicle), power,
                   vehicle.rotation_factor, (vehicle.forward_speed_bias, vehicle.reverse_speed_bias),
                   any(track.broken for track in vehicle.tracks.values()), bool(engine and engine.destroyed),
                   world.flight_spin(entity) if entity in world.dynamic_bodies else None)


@dataclass(frozen=True)
class ReturnMotion:
    motion: Motion
    road: Pose
    lean: tuple | None
    rigid_spin: tuple[float, float, float] | None = None
    ground_motion: GroundMotion | None = None


@dataclass(frozen=True)
class BurstState:
    shell: Shell | None
    effect: int
    remaining: int
    switch: int

    @classmethod
    def capture(cls, gun):
        return cls(gun.burst_shell, gun.burst_effect, gun.burst_remaining, gun.burst_switch_shell)

    def restore(self, gun):
        gun.burst_shell, gun.burst_effect = self.shell, self.effect
        gun.burst_remaining, gun.burst_switch_shell = self.remaining, self.switch


@dataclass(frozen=True)
class GunSnapshot:
    shells: tuple
    reserve: tuple
    next_shell: int
    reload_kind: str
    reload_total: float
    full_reload: float
    interval: float
    burst_count: int
    burst_interval: float
    burst: BurstState
    pump_times: tuple
    pump_remaining: float
    pump_total: float
    pump_waiting: bool
    aiming: object = None
    aiming_time_factor: float = 1.
    dispersion_factor: float = 1.
    shot_age: float | None = None
    gun_damaged: bool = False

    @classmethod
    def capture(cls, arena, vehicle, selected):
        shells = vehicle.definition.weapon.shells if vehicle.definition else (selected,) if selected else ()
        stock = dict(vehicle.ammo_by_shell)
        stock[vehicle.shell_id] = vehicle.ammo
        refill_times = tuple(arena._reload_duration(vehicle, 'reload', nominal) for nominal in vehicle.pump_times)
        burst_count = vehicle.definition.weapon.burst_count if vehicle.burst_gun else 1
        gun_state = arena.guns.get(vehicle.entity_id)
        return cls(shells, tuple(stock.items()), vehicle.next_shell, vehicle.reload_kind, vehicle.reload_total,
                   arena._magazine_duration(vehicle, 'reload'), arena._magazine_duration(vehicle, 'interval'),
                   burst_count, arena._burst_duration(vehicle) if burst_count > 1 else 0.,
                   BurstState.capture(vehicle), refill_times, _remaining(vehicle.pump_at, arena.simulation_time),
                   vehicle.pump_total, vehicle.pump_waiting, getattr(vehicle.definition, 'aiming', None),
                   vehicle.aiming_time_factor, vehicle.shot_dispersion_factor,
                   None if vehicle.last_shot_at is None else arena.simulation_time - vehicle.last_shot_at,
                   gun_state is not None and gun_state.damaged)


def _remaining(deadline, now):
    # A due timer must still complete on the branch's first step.
    return max(1e-9, deadline-now) if deadline else 0.


@dataclass(frozen=True)
class Snapshot:
    ident: str
    tick: int
    owner: Entity
    captured_at: float
    tanks: tuple
    projectiles: tuple
    destroyed: frozenset
    ammo: int
    clip: int
    clip_size: int
    selected_shell: int
    reload_remaining: float
    gun: GunAim
    aim_range: float
    drive: DriveSnapshot
    gun_loadout: GunSnapshot
    object_health: tuple = ()
    consumables: tuple = ()
    heal_wave: tuple | None = None
    role: str | None = None
    role_level: int = 0
    infinite_ammo: bool = False
    gun_destroyed: bool = False
    suspended_shield: big_boss.RegeneratorShield | None = None
    suspended_poison: tuple = ()
    aim_lock: tuple[Entity, tuple] | None = None
    poisoned_targets: frozenset[int] = frozenset()

    @classmethod
    def capture(cls, arena, account_id, wall_time):
        if (not math.isfinite(wall_time) or not arena.started or arena.finished_at
                or arena.world is None or not arena.world.build_ready
                or arena.retiring or arena.closed):
            raise ValueError('Time Stop requires a live battle')
        player = arena.players.get(account_id)
        if not player or not player.loaded or not player.attached or account_id in arena.hangar_players:
            raise ValueError('Time Stop requires an attached player')
        vehicle = arena.vehicle_for(account_id)
        if not vehicle.alive or vehicle.entity_id in arena.respawn_at:
            raise ValueError('Time Stop owner is unavailable')
        bodies = body_states(arena.vehicles)
        boss = arena.big_boss
        now = arena.simulation_time
        tanks = tuple(FrozenTank(Entity.of(v), bodies[v.entity_id], v.team,
                                v.alive, v.sandbox_god_mode, v.gun.packed(), v.health,
                                v.max_health, arena._player_attached(v),
                                getattr(v.definition, 'mass', big_boss.BLAST_REFERENCE_MASS),
                                vehicle_class(arena._boss_game, v.definition)
                                if boss is not None and v.definition is not None else None,
                                boss.damage_factor(v.entity_id) if boss is not None else 1.,
                                getattr(v.definition, 'he_damage_factor', 1.),
                                arena._shield_factor(v, now), arena._armor_mover_factor(v, now),
                                arena.guns[v.entity_id].spec.projectile_chance
                                if v.entity_id in arena.guns else 0.,
                                boss.active_shield(v.entity_id, now) if boss is not None else None,
                                not mad_games.concealed(v, vehicle.team))
                      for v in arena.vehicles.values())
        projectiles = tuple(ProjectileTrace.capture(p) for p in arena.projectiles.values())
        shell = arena.shell_for(vehicle)
        consumables = tuple(_shift_consumable(slot, -now) for slot in vehicle.consumables if slot is not None)
        heal_wave = boss.heal_waves.get(vehicle.entity_id) if boss is not None else None
        if heal_wave is not None:
            due, count, level = heal_wave
            heal_wave = max(0., due-now), count, level
        role, level = None, 0
        shield, poison = None, ()
        if boss is not None:
            role = big_boss.BOSS if arena._is_boss(vehicle) else boss.roles.get(vehicle.entity_id)
            level = boss.levels.get(vehicle.entity_id, 0)
            shield = boss.shields.get(vehicle.entity_id)
            if shield is not None:
                shield = shield._replace(started=shield.started-now, expires=shield.expires-now)
            poison = tuple((source, state._replace(started=state.started-now,
                                                   next_tick=state.next_tick-now))
                           for source, state in boss.poison.get(vehicle.entity_id, {}).items())
        target = arena.vehicles.get(vehicle.autoaim_target)
        aim_lock = (Entity.of(target), vehicle.autoaim_offset) if (
            target is not None and target.alive and arena._player_attached(target)
            and not mad_games.concealed(target, vehicle.team)
            and vehicle.autoaim_offset is not None) else None
        return cls(uuid.uuid4().hex, arena.step_count, Entity.of(vehicle), wall_time,
                   tanks, projectiles, frozenset(arena.world.destroyed),
                   vehicle.ammo, vehicle.clip, vehicle.clip_size, vehicle.shell_id,
                   _remaining(vehicle.reload_at, arena.simulation_time),
                   copy(vehicle.gun), shell.max_distance if shell is not None else 500.,
                   DriveSnapshot.capture(arena, vehicle), GunSnapshot.capture(arena, vehicle, shell),
                   tuple(arena.world.object_health.items()), consumables, heal_wave,
                   role, level, arena.unlimited_ammo or arena.practice, arena.gun_destroyed(vehicle),
                   shield, poison, aim_lock, frozenset(boss.poison if boss is not None else ()))

    def restore_owner_status(self, arena):
        boss = arena.big_boss
        if boss is None:
            return
        entity, now = self.owner.ident, arena.simulation_time
        shield = self.suspended_shield
        if shield is not None and shield.expires > 0.:
            boss.shields[entity] = shield._replace(started=now+shield.started, expires=now+shield.expires)
        if self.suspended_poison:
            boss.poison[entity] = {source: state._replace(started=now+state.started,
                                                        next_tick=now+max(0., state.next_tick))
                                   for source, state in self.suspended_poison}
        if entity in boss.shields or entity in boss.poison:
            arena._emit_boss_vehicle_effects(arena.vehicles[entity])


def _shift_consumable(slot, offset):
    shifted = copy(slot)
    shifted.activated_at += offset
    shifted.active_until += offset
    shifted.ready_at += offset
    return shifted


@dataclass(frozen=True)
class Impulse:
    velocity: tuple[float, float, float]
    spin: tuple[float, float, float] | None = None
    light: bool = False
    max_speed: float = 130.


@dataclass(frozen=True)
class PropImpact:
    direction: tuple[float, float]
    damage: float | None = None


@dataclass(frozen=True)
class PoisonContinuation:
    damage: int
    delay: float
    remaining: int


@dataclass(frozen=True)
class RegeneratorHeal:
    amount: int
    level: int
    shot: bool = False
    grant_shield: bool = False


@dataclass(frozen=True)
class Effect:
    branch: str
    sequence: int
    at: float
    source: Entity
    target: Entity | int
    kind: str
    value: int | Impulse | PropImpact | TrackContact | PoisonContinuation | RegeneratorHeal
    action: int
    shell_hit: bool


@dataclass(frozen=True)
class Outcome:
    applied: tuple
    skipped: tuple
    refund: int
    energy: int
    damage_ticks: dict[tuple[float, int], int]


@dataclass(frozen=True)
class MergeVisual:
    at: float
    action: int
    event: BattleEvent


class EffectLedger:
    """Charge once per action and reconcile each ordered effect once."""

    RIGID_LAUNCHES_PER_TICK = 6

    def __init__(self, branch, source, energy):
        self.branch = branch
        self.source = source
        self.capacity = self.energy = energy
        self._charges = {}
        self._events = []
        self._sealed = False
        self._aborted = False

    def reserve_free_action(self):
        """Reserve action zero for effects that do not spend branch energy."""
        self._charges[0] = 0

    @property
    def events(self):
        return tuple(sorted(self._events, key=lambda effect: (effect.at, effect.sequence)))

    @property
    def aborted(self):
        return self._aborted

    def charge(self, action, cost):
        if (self._sealed or type(action) is not int or action < 1
                or action in self._charges or type(cost) is not int
                or not 0 <= cost <= self.energy):
            return False
        self._charges[action] = cost
        self.energy -= cost
        return True

    def record(self, action, at, target, kind, value, *, shell_hit=True):
        if self._sealed or action not in self._charges:
            raise ValueError('Time Stop effect must belong to an active paid action')
        if kind == 'prop':
            valid = type(target) is int and isinstance(value, PropImpact)
        else:
            valid = isinstance(target, Entity) and (
                (kind in ('damage', 'heal', 'shield') and type(value) is int and value > 0)
                or (kind == 'heal' and isinstance(value, RegeneratorHeal)
                    and value.amount >= 0 and 0 <= value.level < big_boss.ROLE_LEVEL_COUNT)
                or (kind == 'impulse' and isinstance(value, Impulse))
                or (kind == 'module' and isinstance(value, TrackContact))
                or (kind == 'poison' and isinstance(value, PoisonContinuation)))
        if not valid or not math.isfinite(at) or at < 0.:
            raise ValueError('Invalid Time Stop effect')
        self._events.append(Effect(self.branch, len(self._events)+1, at, self.source,
                                   target, kind, value, action, shell_hit))

    def abort(self):
        self._sealed = True
        self._aborted = True
        self._events.clear()

    def seal(self):
        self._sealed = True

    def merge(self, arena, *, refund_fraction=.5, defer_hit=None):
        """Run on the authoritative tick; references select the current life."""
        self._sealed = True
        applied = []
        skipped = []
        refunds = set()
        source = arena.vehicles.get(self.source.ident)
        if (self._aborted or self.branch in arena._resolved_time_branches
                or source is None or Entity.of(source) != self.source or not source.alive
                or arena.finished_at):
            return Outcome((), (), 0, self.energy, {})
        arena._resolved_time_branches.add(self.branch)
        events = self.events
        hit_ticks = self._hit_ticks(events, arena.STEP) if defer_hit is not None else {}
        timed = tuple(self._time_hits(events, hit_ticks))
        spread_targets, launches_per_tick = (self._spread_targets(arena, timed) if defer_hit is not None
                                            else (set(), Counter()))
        last_impulse_tick = {}
        damage_ticks = {}
        for effect, tick in timed:
            impact = effect.at, effect.action
            if effect.kind == 'impulse' and effect.target in spread_targets:
                tick = max(tick, last_impulse_tick.get(effect.target, 0))
                if effect.value.spin is not None:
                    while launches_per_tick[tick] >= self.RIGID_LAUNCHES_PER_TICK:
                        tick += 1
                    launches_per_tick[tick] += 1
                last_impulse_tick[effect.target] = tick
            if tick:
                defer_hit(tick, effect)
            elif self._apply(arena, source, effect):
                applied.append(effect.sequence)
            else:
                skipped.append(effect.sequence)
                if effect.kind == 'heal':
                    refunds.add(effect.action)
                continue
            if effect.kind == 'damage':
                damage_ticks[impact] = min(damage_ticks.get(impact, tick), tick)
        refund = min(self.capacity-self.energy,
                     sum(int(self._charges[action]*refund_fraction) for action in refunds))
        self.energy += refund
        return Outcome(tuple(applied), tuple(skipped), refund, self.energy, damage_ticks)

    @staticmethod
    def _time_hits(events, hit_ticks):
        target_ticks = {}
        for effect in events:
            tick = 0
            if effect.target in hit_ticks and effect.kind not in ('prop', 'heal'):
                impact = effect.at, effect.action
                tick = max(target_ticks.get(effect.target, 0), hit_ticks[effect.target].get(impact, 0))
                target_ticks[effect.target] = tick
            yield effect, tick

    def _spread_targets(self, arena, timed):
        """Find hulls whose rigid launches can move independently of HP timing."""
        if arena.world is None:
            return set(), Counter()
        launches = [(effect.target, tick) for effect, tick in timed
                    if effect.kind == 'impulse' and effect.value.spin is not None
                    and effect.target.ident not in arena.world.dynamic_bodies
                    and (target := arena.vehicles.get(effect.target.ident)) is not None
                    and target.alive and Entity.of(target) == effect.target]
        if len(launches) <= self.RIGID_LAUNCHES_PER_TICK:
            return set(), Counter()
        damage = Counter()
        for effect, _ in timed:
            if effect.kind == 'damage':
                damage[effect.target] += effect.value
        spread = {identity for identity, _ in launches if arena.vehicles[identity.ident].health > damage[identity]}
        # Reserve room for launches that must precede the ordinary death path.
        fixed = Counter(tick for identity, tick in launches if identity not in spread)
        return spread, fixed

    @staticmethod
    def _hit_ticks(events, step):
        """Separate impacts on the same life while keeping an area hit simultaneous."""
        hits = {}
        for effect in events:
            if effect.kind == 'damage':
                hits.setdefault(effect.target, {})[(effect.at, effect.action)] = 0
        # Normally leave 100 ms between hits. Compress long volleys to about
        # 400 ms, with at least one fixed step between successive hits.
        for target, impacts in hits.items():
            if len(impacts) > 1:
                gaps = len(impacts) - 1
                span = max(gaps, min(gaps * round(.1 / step), round(.4 / step)))
                hits[target] = {impact: index * span // gaps for index, impact in enumerate(impacts)}
        return {target: impacts for target, impacts in hits.items() if len(impacts) > 1}

    @classmethod
    def apply_committed_hit(cls, arena, effect):
        # A merge authorizes the source life once. The arena keeps its entity
        # assigned to the same player across respawns and training tank swaps.
        # Queued impacts still go through the target's life check in _apply.
        source = arena.vehicles.get(effect.source.ident)
        if source is not None and not arena.finished_at:
            cls._apply(arena, source, effect)

    @staticmethod
    def _apply(arena, source, effect):
        now, value = arena.simulation_time, effect.value
        if effect.kind == 'prop':
            world = arena.world
            if world is None or effect.target not in world.object_health or effect.target in world.destroyed:
                return False
            world.destroy(effect.target, value.direction, damage=value.damage)
            return True
        target = arena.vehicles.get(effect.target.ident)
        if target is None or Entity.of(target) != effect.target or not target.alive:
            return False
        if effect.kind == 'heal':
            if target.team != source.team or target.entity_id in arena._suspended_entities():
                return False
            if not isinstance(value, RegeneratorHeal):
                return arena._heal_health(source, target, value) > 0
            if arena.big_boss is None:
                return arena._heal_health(source, target, value.amount) > 0
            applied = arena._boss_heal(source, target, value.amount)
            if value.shot or applied:
                arena.emit('boss_role_effect', target.entity_id, compact_id=big_boss.HEAL_SHOT_EFFECTS[value.level])
            if value.grant_shield:
                arena._grant_regenerator_shield(target, value.level, now)
            return applied or value.shot
        if target.team == source.team:
            return False
        if effect.kind == 'damage':
            return arena._damage_health(target, value, source.entity_id, now,
                                        shell_hit=effect.shell_hit) > 0
        if effect.kind == 'shield':
            return arena._consume_boss_shield(target, now, ident=value)
        if effect.kind == 'module':
            return arena._track_contact(replace(value, at=now, shot=BRANCH_SHOT_BASE+effect.action),
                                        now, probability_checked=True)
        if effect.kind == 'poison':
            if arena.big_boss is None:
                return False
            poisons = arena.big_boss.poison.setdefault(target.entity_id, {})
            existing = poisons.get(source.entity_id)
            poisons[source.entity_id] = big_boss.Poison(
                existing.damage if existing else value.damage,
                min(existing.next_tick, now+value.delay) if existing else now+value.delay,
                max(existing.remaining, value.remaining) if existing else value.remaining,
                existing.started if existing else now)
            arena._emit_boss_vehicle_effects(target)
            return True
        world, entity = arena.world, target.entity_id
        if world is None or entity not in world.motion or not arena._player_attached(target):
            return False
        airborne = not world.motion[entity].grounded
        applied = world.deferred_impulse(entity, value.velocity, value.spin, dt=arena.STEP,
                                         max_speed=value.max_speed,
                                         damping=big_boss.LIGHT_BLAST_AIR_DAMPING if value.light else None)
        if applied and arena.big_boss is not None:
            arena.big_boss.impulses[entity] = source.entity_id, now + (12. if value.light else 4.)
            if entity in world.dynamic_bodies:
                arena.big_boss.blast_landings.setdefault(entity, target.pose if not airborne else None)
        return applied


def prepare_world(geometry):
    """Build an independent world before any player asks to fork."""
    return CollisionWorld(geometry.assets, geometry.terrain, vehicles=geometry.poses,
        vehicle_assets=geometry.vehicle_assets, experimental_6dof=geometry.experimental_6dof,
        experimental_ground_impacts=False, wall_trampoline=geometry.wall_trampoline,
        sprung_tracks=geometry.sprung_tracks, gravity=geometry.gravity, dynamic_backend='bullet')


class TimelineSimulation:
    """One independent Bullet connection. Only the owner advances."""

    STEP = 1 / 60

    def __init__(self, snapshot, geometry, *, level=LEVELS[0], rules=Rules(), rng=None, world=None):
        self.snapshot = snapshot
        self.rules, self.level = rules, level
        self.entity = snapshot.owner.ident
        self.steps = 0
        self.deadline = snapshot.captured_at+rules.duration
        self.ended = False
        self.shots_fired = 0
        self.shots = []
        self.abilities_used = 0
        self.last_ability = None
        self.ammo, self.clip = snapshot.ammo, snapshot.clip
        self.gun = copy(snapshot.gun)
        loadout = snapshot.gun_loadout
        self.clip_size = snapshot.clip_size
        self.selected_shell, self.next_shell = snapshot.selected_shell, loadout.next_shell
        self.ammo_by_shell = dict(loadout.reserve)
        self.shells = {shell.compact_id: shell for shell in loadout.shells}
        loadout.burst.restore(self)
        reload_rate = 1. if self.burst_pending else level.cooldown_multiplier
        self.reload_at = snapshot.reload_remaining/reload_rate
        self.reload_total = loadout.reload_total/reload_rate
        self.reload_kind = loadout.reload_kind
        self.pump_times = tuple(value/level.cooldown_multiplier for value in loadout.pump_times)
        self.pump_at = loadout.pump_remaining/level.cooldown_multiplier
        self.pump_total = loadout.pump_total/level.cooldown_multiplier
        self.pump_waiting = loadout.pump_waiting
        self.gun_revision = 0
        self.rng = random.Random(snapshot.ident) if rng is None else rng
        self.dispersion_rng = random.Random(snapshot.ident + ':dispersion')
        self.last_shot_at = None if loadout.shot_age is None else -loadout.shot_age
        self.ledger = EffectLedger(snapshot.ident, snapshot.owner, level.energy)
        self.tanks = {tank.identity.ident: tank for tank in snapshot.tanks}
        self.hidden_targets = frozenset(entity for entity, tank in self.tanks.items() if not tank.visible)
        self.shields = {entity: tank.regenerator_shield.ident for entity, tank in self.tanks.items()
                        if tank.regenerator_shield is not None}
        self.poisoned_targets = set(snapshot.poisoned_targets)
        self.aim_lock, self.autoaim_lost = snapshot.aim_lock, False
        self.health = {entity: tank.health for entity, tank in self.tanks.items()}
        self.consumables = {slot.spec.ident: copy(slot) for slot in snapshot.consumables}
        for slot in self.consumables.values():
            slot.ready_at = max(0., slot.ready_at)/level.cooldown_multiplier
        self.heal_wave = (0, *snapshot.heal_wave) if snapshot.heal_wave is not None else None
        if self.heal_wave is not None:
            self.ledger.reserve_free_action()
        self.traps = {}
        self.poison = {}
        self.frozen = {entity: tank.body for entity, tank in self.tanks.items() if entity != self.entity}
        self.frozen_armor = {entity: body.armor_state for entity, body in self.frozen.items()
                             if body.armor_state is not None}
        initial = {entity: tank.body.pose for entity, tank in self.tanks.items()}
        self.activation_pose = initial[self.entity]
        self.drive = snapshot.drive
        self.speed = self.drive.speed
        # Reuse the arena's lateral grip without letting contacts impart
        # motion to any of the frozen vehicles.
        self.ground_motion = copy(self.drive.ground_motion) if geometry.experimental_ground_impacts else None
        self.engine_mode = (0, 0) if self.drive.engine_destroyed else (1, 0)
        self.projectiles = {}
        self.shot_ends = []
        self.merge_visuals = []
        self.world = prepare_world(geometry) if world is None else world
        try:
            if set(self.world.bodies) != set(initial):
                raise ValueError('Time Stop snapshot does not match prepared geometry')
            self.world.relocate_training_body(self.entity, initial[self.entity],
                motion=copy(self.drive.motion), road=self.drive.road, lean=self.drive.lean)
            self.world._carrying_supported[self.entity] = self.drive.carrying_supported
            if self.drive.support_memory is not None:
                self.world._support_memory[self.entity] = self.drive.support_memory
            if self.level.radius is not None:
                x, _, z = self.activation_pose.position
                self.world.movement_circle = self.entity, x, z, self.level.radius
            for entity, tank in self.tanks.items():
                self.world.poses[entity] = tank.body.pose
                self.world.set_pose(self.world.bodies[entity], tank.body.pose)
                self.world.set_turret(tank.body.turret, entity, tank.body.pitch)
            for key in sorted(snapshot.destroyed):
                self.world.destroy(key, (0., 0.))
            self.world.object_health.update(snapshot.object_health)
            self.world.destruction_events.clear()
            if (self.drive.rigid_spin is not None and not self.world.set_flight_velocity(
                    self.entity, self.drive.motion.velocity, self.drive.rigid_spin)):
                raise ValueError('Time Stop could not place the airborne hull in the branch')
        except BaseException:
            self.world.close()
            raise

    @property
    def pose(self):
        return self.world.poses[self.entity]

    @property
    def time(self):
        return self.steps*self.STEP

    @property
    def burst_pending(self):
        return self.burst_remaining > 0

    def aim(self, turret, pitch):
        return not self.ended and self.gun.set_angles(turret, pitch)

    def autoaim(self, entity, point=None):
        if not entity:
            self.aim_lock, self.autoaim_lost = None, False
            return self.gun.set_angles(self.gun.yaw, self.gun.pitch)
        target = self.tanks.get(entity)
        if (target is None or not target.alive or not target.attached or not target.visible
                or target.team == self.tanks[self.entity].team):
            self.autoaim(0)
            self.autoaim_lost = True
            return False
        body = target.body
        if point is None:
            offset = self.world.assets_for(entity).hull_center
        else:
            if (len(point) != 3 or not all(math.isfinite(v) and abs(v) < 10000 for v in point)
                    or math.dist(point, body.pose.position) > 15.):
                return False
            offset = body.rotation.T @ tuple(a-b for a, b in zip(point, body.pose.position))
        self.aim_lock, self.autoaim_lost = (target.identity, tuple(map(float, offset))), False
        self._aim_at_lock()
        return True

    def aim_point(self, point, local=False):
        if self.aim_lock is None:
            return self.gun.set_point(point, local)
        if local:
            point = tuple(a+b for a, b in zip(self.pose.position, point))
        return self.autoaim(self.aim_lock[0].ident, point)

    def _aim_at_lock(self):
        identity, offset = self.aim_lock
        body = self.frozen[identity.ident]
        return self.gun.set_point(tuple(a+b for a, b in zip(body.pose.position, body.rotation @ offset)), False)

    def _shot_ray(self):
        pose = self.pose
        rotation = self.world._dynamic_rotation(self.entity)
        if rotation is not None:
            return self.gun.ray_rotation(pose.position, rotation)
        return self.gun.ray(pose.position, pose.yaw, pose.pitch, pose.roll)

    def _ability_ray(self):
        pose = self.pose
        rotation = self.world._dynamic_rotation(self.entity)
        if rotation is None:
            rotation = matrix(pose.yaw, pose.pitch, pose.roll)
        if self.aim_lock is not None:
            self._aim_at_lock()
        return self.gun.target_ray(pose.position, rotation)

    @property
    def shell_id(self):
        return self.selected_shell

    @property
    def aim_range(self):
        shell = self.shells.get(self.shell_id)
        return shell.max_distance if shell is not None else self.snapshot.aim_range

    def aim_marker(self):
        origin, direction = self._shot_ray()
        shell = self.shells.get(self.shell_id)
        speed = shell.speed if shell is not None else 0.
        if self.snapshot.role == big_boss.BOSS:
            charge = self.consumables.get(big_boss.MEGASHOT.ident)
            if charge is not None and charge.state_at(self.time).state == READY:
                speed = big_boss.MEGASHOT_SPEED
        return origin, tuple(axis * speed for axis in direction), self.shot_dispersion()

    def shot_dispersion(self):
        loadout = self.snapshot.gun_loadout
        shot_age = None if self.last_shot_at is None else self.time - self.last_shot_at
        return dispersion_tangent(loadout.aiming, shot_age=shot_age,
                                  factor=loadout.dispersion_factor, aim_factor=loadout.aiming_time_factor,
                                  movement_factor=self.gun.movement_dispersion, gun_damaged=loadout.gun_damaged)

    def _drive(self, flags):
        world, entity = self.world, self.entity
        drive = self.drive
        state = world.motion[entity]
        ground = self.ground_motion
        residual_slip = 0.
        if drive.broken_track or drive.engine_destroyed:
            flags = 0
        forward = int(bool(flags & 1)) - int(bool(flags & 2))
        turn = int(bool(flags & 8)) - int(bool(flags & 4))
        if forward < 0:
            turn = -turn
        mask = flags if forward or turn else 0
        self.engine_mode = (0, 0) if drive.engine_destroyed else (3, mask) if mask else (1, 0)
        if state.grounded:
            assets = world.assets_for(entity)
            definition = assets.definition
            pitch = world.drive_pitch(entity)
            cos_pitch = max(.05, math.cos(pitch))
            mobility = terrain_at(world.terrain, definition, self.pose.position)
            cap = (assets.forward_speed + drive.speed_bias[0] if forward > 0
                   else assets.reverse_speed + drive.speed_bias[1])
            target = forward * cap * drive.speed_factor * cos_pitch
            if not forward or self.speed*forward < 0.:
                acceleration = braking_acceleration(definition)
            else:
                acceleration = powered_acceleration(definition, self.speed/cos_pitch,
                                                    mobility.resistance, drive.power_factor)
                if acceleration is None:
                    fuel = getattr(getattr(definition, 'drive', None), 'power_bonus_factor', 1.)
                    acceleration = 5. * fuel * drive.power_factor
            acceleration *= cos_pitch
            grip = slope_grip_acceleration(assets, pitch, world.GRAVITY)
            if grip is not None:
                acceleration = min(acceleration, grip)
            slope = world.GRAVITY * math.sin(pitch) * math.cos(pitch)
            rolling = rolling_acceleration(mobility.resistance, pitch, world.GRAVITY) * self.STEP
            if drive.engine_destroyed:
                self.speed = 0.
            speed, effort = advance_speed(self.speed, target, acceleration, slope, rolling, self.STEP)
            yaw_delta = turn * assets.rotation_speed * drive.rotation_factor * mobility.turn_factor * self.STEP
            displacement = None
            if ground is not None:
                yaw_rate = ground.yaw_rate * math.exp(-world.GROUND_YAW_DAMPING*self.STEP)
                yaw_delta += yaw_rate*self.STEP
                yaw = self.pose.yaw
                previous = (ground.velocity if ground.active and ground.velocity is not None else
                            (self.speed*math.sin(yaw), self.speed*math.cos(yaw)))
                velocity, residual_slip = skid_velocity(previous, yaw+yaw_delta*.5,
                                                         speed, world.GROUND_LATERAL_GRIP*self.STEP)
                displacement = tuple(axis*self.STEP for axis in velocity)
                ground.yaw_rate = yaw_rate
            world.set_drive_effort(entity, effort, speed*yaw_delta/self.STEP, forward)
            distance = speed*self.STEP
            heading = self.pose.yaw+yaw_delta
            fx, fz = math.sin(heading), math.cos(heading)
            x, _, z = self.pose.position
            cx, _, cz = self.activation_pose.position
            if (self.level.radius is not None and forward
                    and math.hypot(x-cx+fx*distance, z-cz+fz*distance) > self.level.radius):
                # The boundary supplies a radial reaction only. Do not aim
                # the previous tangential momentum back into the boundary.
                vx, _, vz = state.velocity
                push = speed - (vx*fx+vz*fz)
                vx, vz = vx+push*fx, vz+push*fz
                magnitude = math.hypot(vx, vz)
                step = self.STEP * (min(1., abs(target)/magnitude) if magnitude else 1.)
                displacement = vx*step, vz*step
            world.move(distance, yaw_delta, entity, dt=self.STEP, planar_displacement=displacement)
        else:
            world.set_drive_effort(entity, 0., 0., 0)
            world.move(0., 0., entity, dt=self.STEP)
        if world.dynamic_bodies or world.pending_dynamics:
            world.dynamic_commit(self.STEP)
        state = world.motion[entity]
        vx, _, vz = state.velocity
        if ground is not None:
            if state.grounded:
                ground.velocity = (vx, vz)
                ground.active = residual_slip > .001 or abs(ground.yaw_rate) > .01
            else:
                ground.active = False
                ground.yaw_rate = 0.
        self.speed = vx*math.sin(self.pose.yaw) + vz*math.cos(self.pose.yaw)

    def tick(self, wall_time, *, flags=0, missile_camera=None):
        if self.ended:
            return False
        if not math.isfinite(wall_time) or type(flags) is not int or not 0 <= flags <= 15:
            raise ValueError('Invalid Time Stop step')
        if wall_time >= self.deadline:
            self.finish()
            return False
        if wall_time-self.snapshot.captured_at+1e-9 < self.time+self.STEP:
            return True
        previous, previous_turret, previous_pitch = self.pose, self.gun.yaw, self.gun.pitch
        self._drive(flags)
        pose = self.pose
        if self.aim_lock is not None:
            self._aim_at_lock()
        self.gun.tick(self.STEP, pose.position, pose.yaw, pose.pitch, pose.roll)
        loadout = self.snapshot.gun_loadout
        self.gun.update_dispersion(loadout.aiming, previous, pose, previous_turret, self.STEP,
                                   loadout.aiming_time_factor, previous_pitch=previous_pitch)
        self.world.set_turret(self.gun.yaw, self.entity, self.gun.pitch)
        self.steps += 1
        if self.world.destruction_events:
            self.ledger.reserve_free_action()
            for obj, yaw, _ in self.world.destruction_events:
                self.ledger.record(0, self.time, obj.key, 'prop', PropImpact((math.sin(yaw), math.cos(yaw))))
        if self.pump_times:
            previous_refill = self.pump_at
            for _ in advance_pump(self, self.time, self._pump_segment):
                pass
            if self.pump_at != previous_refill:
                self.gun_revision += 1
        if self.reload_at and self.time >= self.reload_at and not self.burst_pending:
            if self.pump_times:
                self.reload_at = 0.
            else:
                finish_magazine_reload(self)
            self.gun_revision += 1
        self._step_projectiles(missile_camera)
        # Projectile impacts record their own timed changes in the ledger.
        self.world.destruction_events.clear()
        self._advance_abilities()
        if self.burst_pending and self.time + 1e-9 >= self.reload_at:
            self._fire_burst_next()
        return True

    def activate(self, action, ident, view=None):
        slot = self.consumables.get(ident)
        recipe = ABILITY_RECIPES.get(ident)
        if self.ended or slot is None or recipe is None:
            return False
        kind, level = recipe
        cost = getattr(self.rules, kind+'_cost')
        if self.ledger.energy < cost:
            return False
        origin = self.pose.position
        if kind == 'graviton':
            ray = self._ability_ray() if view is None else None
            center = big_boss.graviton_center(self.world, origin, ray, self.aim_range, view)
            if center is None:
                return False
        staged = copy(slot)
        if not staged.activate(self.time) or not self.ledger.charge(action, cost):
            return False
        staged.ready_at = self.time+(staged.ready_at-self.time)/self.level.cooldown_multiplier
        self.consumables[ident] = staged
        self.abilities_used += 1
        self.last_ability = kind, level, slot.spec.compact_id
        if kind == 'healing':
            self.heal_wave = (action, self.time+big_boss.HEAL_INTERVAL, level+2, level)
        elif kind == 'graviton':
            trap = big_boss.GravityTrap(action | 0x40000000, self.entity, self.tanks[self.entity].team,
                                       tuple(center), level, self.time+big_boss.TRAP_DURATION)
            self.traps[action] = self.time, trap
        elif kind in ('explosion', 'shockwave'):
            event = BattleEvent('provision_effect', self.entity, 0,
                {'compact_id': slot.spec.compact_id, 'skip_owner': True,
                 'generation': self.snapshot.owner.generation})
            self.merge_visuals.append(MergeVisual(self.time, action, event))
            for target in self.tanks.values():
                if self._enemy(target) and target.attached:
                    self._area_hit(action, kind, level, origin, target)
        return True

    def _area_hit(self, action, kind, level, origin, target):
        position = target.body.pose.position
        if kind == 'shockwave':
            impulse = big_boss.blast_impulse(origin, position, target.mass, self.pose.yaw, target.tank_class)
            if impulse is not None:
                self.ledger.record(action, self.time, target.identity, 'impulse',
                                   Impulse(*impulse, light=target.tank_class == 'lightTank'))
                self._damage(action, self.time, target, max(1, round(target.max_health*big_boss.BLAST_DAMAGE)))
        elif math.dist(origin, position) <= big_boss.BOMB_RADIUS:
            start = origin[0], origin[1]+1., origin[2]
            end = position[0], position[1]+1., position[2]
            if self.world.static_segment(start, end)[0] is None:
                damage = max(1, round(self.health[self.entity]*big_boss.BOMB_DAMAGE[level]))
                self._damage(action, self.time, target, damage, blocks_on_shield=True)

    def _observe_trap(self, action, started, trap, at):
        strength = -math.expm1(-big_boss.TRAP_RESPONSE*(at-started))
        if not strength:
            return
        event = BattleEvent('time_stop_graviton_merge', self.entity, 0, {'trap': trap})
        self.merge_visuals.append(MergeVisual(at, action, event))
        for target in self.tanks.values():
            if not self._enemy(target) or not target.attached:
                continue
            pull = big_boss.gravity_pull((trap,), target.team, target.body.pose.position, target.mass)
            if pull and math.hypot(*pull) > .01:
                self.ledger.record(action, at, target.identity, 'impulse',
                    Impulse((pull[0]*strength, 0., pull[1]*strength), max_speed=big_boss.TRAP_MAX_SPEED))

    def _advance_abilities(self):
        if self.heal_wave is not None and self.time >= self.heal_wave[1]:
            action, due, count, level = self.heal_wave
            self.heal_wave = (action, due+big_boss.HEAL_INTERVAL, count-1, level) if count > 1 else None
            origin = self.pose.position
            team = self.tanks[self.entity].team
            for entity, target in self.tanks.items():
                if not target.alive or not target.attached or target.team != team:
                    continue
                own = entity == self.entity
                if not own and math.dist(origin, target.body.pose.position) > big_boss.HEAL_RADIUS:
                    continue
                amount = min(target.max_health-self.health[entity],
                             round(target.max_health*big_boss.HEAL_FRACTION*(.5 if own else 1.)))
                if amount > 0 or entity in self.poisoned_targets:
                    self.poisoned_targets.discard(entity)
                    self.health[entity] += amount
                    self.ledger.record(action, due, target.identity, 'heal', RegeneratorHeal(amount, level))
        for action, (started, trap) in tuple(self.traps.items()):
            if self.time >= trap.expires_at:
                self._observe_trap(action, started, trap, trap.expires_at)
                del self.traps[action]
        for entity, (action, poison) in tuple(self.poison.items()):
            if self.time >= poison.next_tick:
                self.ledger.record(action, poison.next_tick, self.tanks[entity].identity,
                                   'damage', poison.damage, shell_hit=False)
                if poison.remaining > 1:
                    self.poison[entity] = action, poison._replace(
                        next_tick=poison.next_tick+big_boss.POISON_INTERVAL, remaining=poison.remaining-1)
                else:
                    del self.poison[entity]

    def _start_reload(self, kind, cooldown):
        self.reload_kind = kind
        self.reload_total = cooldown/self.level.cooldown_multiplier
        self.reload_at = self.time+self.reload_total
        self.gun_revision += 1

    def _start_burst_interval(self):
        self.reload_kind = 'interval'
        self.reload_total = self.snapshot.gun_loadout.burst_interval
        self.reload_at = self.time+self.reload_total
        self.gun_revision += 1

    def _pump_segment(self, at):
        start_pump_refill(self, at)

    def select_shell(self, setting, shell_id):
        if self.ended:
            return False
        if (setting == 17 and shell_id == 0 and not self.pump_times
                and not self.burst_pending and magazine_reload_allowed(self)):
            self.clip = 0
            self._start_reload('reload', self.snapshot.gun_loadout.full_reload)
            return True
        if setting not in (0, 1) or shell_id not in self.shells or self.ammo_by_shell.get(shell_id, 0) <= 0:
            return False
        if setting == 1:
            self.next_shell = shell_id
            return True
        self.next_shell = 0
        if self.burst_pending:
            self.burst_switch_shell = shell_id if shell_id != self.shell_id else 0
            return True
        if switch_shell(self, shell_id):
            if self.pump_times:
                if not (self.reload_at or self.pump_waiting or self.pump_at):
                    self._pump_segment(self.time)
            else:
                self.clip = min(self.clip or (self.clip_size if not self.reload_at else 0), self.ammo)
            self.gun_revision += 1
        return True

    def fire(self, action, shell=None, *, cooldown=None):
        """Fire an authorized direct-fire round from a server-computed muzzle ray."""
        loadout = self.snapshot.gun_loadout
        if shell is None:
            shell = self.shells.get(self.shell_id)
        if (self.ended or self.reload_at or self.burst_pending or not self.ammo or not self.clip
                or shell is None
                or self.snapshot.gun_destroyed
                or shell.compact_id != self.shell_id
                or shell.kind not in ('ARMOR_PIERCING', 'ARMOR_PIERCING_CR', 'HOLLOW_CHARGE',
                                     'HIGH_EXPLOSIVE')):
            return False
        if cooldown is None:
            cooldown = loadout.full_reload
        if not math.isfinite(cooldown) or cooldown <= 0.:
            raise ValueError('Invalid Time Stop fire data')
        if not self.ledger.charge(action, self.rules.shot_cost):
            return False
        if self.snapshot.role == big_boss.BOSS:
            shell = replace(shell, damage=shell.damage*big_boss.SHELL_DAMAGE)
        following = min(loadout.burst_count-1, self.clip-1,
                        self.ammo-int(not self.snapshot.infinite_ammo))
        visible_count = following+1
        if self.rules.shot_cost:
            visible_count = min(visible_count, 1+self.ledger.energy//self.rules.shot_cost)
        self._launch_round(action, shell, shell.effect_index, visible_count)
        if following:
            self.burst_shell, self.burst_effect = shell, shell.effect_index
            self.burst_remaining = following
            self._start_burst_interval()
            return True

        if self.pump_times:
            self.pump_at = self.pump_total = 0.
            if self.clip:
                self._start_reload('interval', loadout.interval)
                self.pump_waiting = True
            else:
                apply_queued_shell(self)
                self._pump_segment(self.time)
                self.gun_revision += 1
        else:
            kind = 'interval' if self.clip else 'reload'
            self._start_reload(kind, loadout.interval if self.clip else cooldown)
        return True

    def _fire_burst_next(self):
        loadout = self.snapshot.gun_loadout
        action = BURST_ACTION_BASE+self.shots_fired
        if self.ledger.charge(action, self.rules.shot_cost):
            self._launch_round(action, self.burst_shell, self.burst_effect, 0)
            self.burst_remaining -= 1
            if self.burst_remaining:
                self._start_burst_interval()
                return
        self.burst_remaining = 0
        self.burst_shell, self.burst_effect = None, 0
        kind = 'interval' if self.clip else 'reload'
        self._start_reload(kind, loadout.interval if self.clip else loadout.full_reload)
        apply_burst_shell_switch(self)

    def _launch_round(self, action, shell, effect, shooting_count):
        origin, direction = self._shot_ray()
        direction = disperse_shot(direction, self.shot_dispersion(), self.dispersion_rng, self.gun.aim_progress)
        self.gun.aim_progress = 0.
        self.last_shot_at = self.time
        decorated = self._decorate_shell(shell)
        if decorated.effect_index != shell.effect_index:
            effect = decorated.effect_index
        shell = decorated
        spend_round(self, self.snapshot.infinite_ammo)
        self.shots_fired += 1
        velocity = tuple(v*shell.speed for v in direction)
        projectile = Projectile(action, self.entity, shell, origin, velocity, self.time, trace_effect=effect)
        self.projectiles[action] = projectile
        self.shots.append((ProjectileTrace.capture(projectile), shooting_count))

    def _decorate_shell(self, shell):
        role, level = self.snapshot.role, self.snapshot.role_level
        if role == big_boss.BOSS:
            charge = self.consumables.get(big_boss.MEGASHOT.ident)
            if charge is not None and charge.activate(self.time):
                charge.ready_at = self.time+(charge.ready_at-self.time)/self.level.cooldown_multiplier
                return big_boss.megashot(shell)
        poisoned = False
        if role == big_boss.CATALYST:
            spec = big_boss.ROLE_ACTIVE[role][level]
            slot = self.consumables.get(spec.ident)
            poisoned = slot is not None and slot.active_until > self.time
        return big_boss.role_shell(shell, role, level, poisoned=poisoned)

    def _step_projectiles(self, missile_camera):
        if not self.projectiles:
            return
        contacts = []
        for action, projectile in self.projectiles.items():
            def prop_contact(key, direction, at):
                self.ledger.record(action, at, key, 'prop', PropImpact(direction))

            guidance = None
            if projectile.shell.missile is not None:
                direction = missile_camera[1] if missile_camera and missile_camera[0] == action else None
                guidance = missile_guidance_target(projectile, self.gun, self.pose.position, self._shot_ray,
                                                   locked=self.aim_lock is not None, camera=direction)
            impact = advance(projectile, self.world, self.frozen, self.frozen, self.STEP, self.rng,
                             prop_contact=prop_contact, missile_target=guidance)
            contacts.extend((contact.at, action, 0, contact) for contact in projectile.drain_track_contacts())
            if impact is not None:
                contacts.append((impact.at, action, 1, impact))
        for at, action, kind, contact in sorted(contacts, key=lambda item: item[:3]):
            target = self.tanks.get(contact.target)
            if kind == 0:
                if (self._enemy(target) and not target.sandbox_god_mode
                        and contact.penetrated and contact.shell.damage_devices > 0
                        and (contact.hit.part in ('chassis_track_L', 'chassis_track_R')
                             or target.gun_chance >= 1.
                             or 0. < target.gun_chance > self.rng.random())):
                    self.ledger.record(action, at, target.identity, 'module', contact)
                continue
            self._impact(action, contact, target)
            explosion = None
            shell = contact.shell
            guided = shell.missile is not None
            if contact.collision and (shell.kind == 'HIGH_EXPLOSIVE' or guided and contact.result.hit is None):
                explosion = shell.compact_id, 3 if contact.result.hit else 0, contact.direction
                event = BattleEvent('explosion', self.entity, 0, {
                    'shot': BRANCH_SHOT_BASE+action, 'shell_id': explosion[0],
                    'material': explosion[1], 'point': contact.point,
                    'direction': explosion[2], 'skip_owner': True,
                })
                self.merge_visuals.append(MergeVisual(at, action, event))
            self.shot_ends.append((action, contact.point, guided, explosion))
            del self.projectiles[action]

    def _enemy(self, target):
        return target is not None and target.alive and target.team != self.tanks[self.entity].team

    def _damage(self, action, at, target, amount, *, shell=None, blocks_on_shield=False):
        factor = target.damage_factor
        if shell is not None:
            if target.sandbox_god_mode:
                return False
            factor *= target.armor_mover_factor
            if shell.kind == 'HIGH_EXPLOSIVE':
                factor *= target.anti_he_factor
            elif shell.kind in SHIELDED_SHELL_KINDS:
                factor *= target.shield_factor
        rounded = int(amount*factor+.5)
        if rounded > 0 and (shell is not None or blocks_on_shield):
            shield = self.shields.pop(target.identity.ident, None)
            if shield is not None:
                self.ledger.record(action, at, target.identity, 'shield', shield, shell_hit=False)
                return False
        if rounded > 0:
            self.ledger.record(action, at, target.identity, 'damage', rounded, shell_hit=shell is not None)
        return rounded > 0

    def _shell_damage(self, action, at, target, amount, shell):
        if not self._damage(action, at, target, amount, shell=shell):
            return
        if shell.poison_level is not None:
            entity = target.identity.ident
            previous = self.poison.get(entity)
            if previous:
                _, poison = previous
                state = poison._replace(remaining=big_boss.POISON_TICKS)
            else:
                damage = max(1, round(target.max_health*big_boss.POISON_DAMAGE[shell.poison_level]))
                state = big_boss.Poison(damage, at+big_boss.POISON_INTERVAL, big_boss.POISON_TICKS, at)
            self.poison[entity] = action, state

    def _impact(self, action, impact, target):
        shell = impact.shell
        if (shell.healing_level is not None and impact.result.hit is not None and target is not None
                and target.alive and target.team == self.tanks[self.entity].team):
            heal = big_boss.HEAL_SHOT[shell.healing_level]+self.rng.uniform(-.15, .15)
            entity = target.identity.ident
            amount = min(target.max_health-self.health[entity], round(shell.damage*heal))
            self.health[entity] += amount
            self.poisoned_targets.discard(entity)
            shield = next((slot for slot in self.consumables.values() if slot.spec in big_boss.SHIELD_SPECS), None)
            grant_shield = shield is not None and shield.activate(impact.at)
            if grant_shield:
                shield.ready_at = impact.at + (shield.ready_at-impact.at)/self.level.cooldown_multiplier
            self.ledger.record(action, impact.at, target.identity, 'heal',
                               RegeneratorHeal(amount, shell.healing_level, shot=True, grant_shield=grant_shield))
            return
        if impact.obstacle is not None:
            direction = impact.direction[::2]
            self.world.destroy(impact.obstacle, direction, damage=shell.damage)
            self.ledger.record(action, impact.at, impact.obstacle, 'prop', PropImpact(direction, shell.damage))
        he = shell.kind == 'HIGH_EXPLOSIVE'
        if he and impact.collision and impact.result.outcome != 'penetrated':
            for entity, state in self.frozen.items():
                tank = self.tanks[entity]
                if not self._enemy(tank) or tank.sandbox_god_mode:
                    continue
                direct = entity == impact.target
                blast = self.world.assets_for(entity).armor.blast(
                    impact.point, shell, state.pose, state.turret, state.pitch, self.rng,
                    contact=impact.result.hit if direct else None, rotation=state.rotation,
                    movable_state=state.armor_state)
                damage = blast.damage or (impact.result.damage if direct else 0)
                self._shell_damage(action, impact.at, tank, damage, shell)
        elif self._enemy(target) and impact.result.damage > 0:
            self._shell_damage(action, impact.at, target, impact.result.damage, shell)

    def finish(self):
        if self.ended:
            return
        self.ended = True
        for action, (started, trap) in self.traps.items():
            self._observe_trap(action, started, trap, self.time)
        self.traps.clear()
        for entity, (action, poison) in self.poison.items():
            status = PoisonContinuation(poison.damage, max(0., poison.next_tick-self.time), poison.remaining)
            self.ledger.record(action, self.time, self.tanks[entity].identity, 'poison', status)
        self.shot_ends.extend((action, projectile.position, projectile.shell.missile is not None, None)
                              for action, projectile in self.projectiles.items())
        self.projectiles.clear()
        self.ledger.seal()

    def abort(self):
        self.finish()
        self.ledger.abort()
        self.merge_visuals.clear()

    def close(self):
        if not self.ended:
            self.abort()
        self.world.close()


def safe_return(world, entity, preferred, activation, *, grounded=True, activation_grounded=True):
    """Find a valid landing in the current battle geometry, or defer return."""
    if world.can_resume_pose(entity, preferred, grounded=grounded):
        return preferred
    px, height, pz = preferred.position
    pose = world.training_placement(entity, px, pz, preferred.yaw, stationary=True, reference=height)
    if pose is not None:
        return pose
    candidates = []
    for radius in (1., 2., 4., 8., 12.):
        for index in range(16):
            angle = math.tau*index/16
            candidates.append((px+radius*math.cos(angle), pz+radius*math.sin(angle)))
    pose = world.first_stationary_placement(entity, candidates, preferred.yaw, height)
    if pose is not None:
        return pose
    if preferred == activation and grounded == activation_grounded:
        return None
    if world.can_resume_pose(entity, activation, grounded=activation_grounded):
        return activation
    ax, ay, az = activation.position
    return world.training_placement(entity, ax, az, activation.yaw, stationary=True, reference=ay)


def return_spawn(arena, owner):
    """Use a validated team spawn if both points on the battle map are blocked."""
    for slot in range(map_team_capacity(arena.map_id)):
        x, z = training_spawn(arena.map_spec, owner.team, slot)
        yaw = training_yaw(arena.map_spec, owner.team, slot)
        pose = arena.world.training_placement(owner.entity_id, x, z, yaw, stationary=True)
        if pose is not None:
            return pose
    return None


def restore_aim_lock(arena, owner, lock):
    if lock is None:
        return
    identity, offset = lock
    target = arena.vehicles.get(identity.ident)
    if (target is not None and target.alive and target.team != owner.team
            and Entity.of(target) == identity and arena._player_attached(target)):
        owner.autoaim_target, owner.autoaim_offset = identity.ident, offset
    else:
        arena.emit('autoaim_lost', owner.entity_id, identity.ident)


def place_returning_body(world, entity, pose, motion=None):
    """Restore the live collider and any motion belonging to the selected pose."""
    if motion is None:
        world.relocate_training_body(entity, pose)
    else:
        world.relocate_training_body(entity, pose, motion=copy(motion.motion),
                                     road=motion.road, lean=motion.lean)
        if motion.motion.grounded and motion.ground_motion is not None and world.experimental_ground_impacts:
            world.ground_motion[entity] = copy(motion.ground_motion)
    world.set_tracked_presence(entity, True)
    if motion is not None and motion.rigid_spin is not None and world.supports_flight:
        # An obstructed rigid hull can still fall from the validated sensor pose.
        world.set_flight_velocity(entity, motion.motion.velocity, motion.rigid_spin)
    return world.poses[entity]


def merge_frame(arena, snapshot, frame, *, rules=Rules(), defer_hit=None):
    """Place the owner and apply a completed worker frame on an arena tick.

    If all checked locations are blocked, leave the result available for a
    later tick. The caller is responsible for keeping the owner parked.
    """
    ledger = frame.ledger
    if (ledger is None or frame.burst is None or ledger.branch != frame.branch or frame.branch != snapshot.ident
            or ledger.source != snapshot.owner):
        raise ValueError('Time Stop merge requires the completed frame for this branch')
    if frame.branch in arena._resolved_time_branches:
        return None
    owner = arena.vehicles.get(snapshot.owner.ident)
    player = arena.players.get(owner.account_id) if owner is not None else None
    if (ledger.aborted or arena.finished_at or owner is None or not owner.alive
            or Entity.of(owner) != snapshot.owner or arena.world is None
            or player is None or not player.attached
            or owner.shell_id != snapshot.selected_shell):
        ledger.abort()
        return None
    origin = next(tank.body.pose for tank in snapshot.tanks if tank.identity == snapshot.owner)
    world, entity = arena.world, owner.entity_id
    return_motion = frame.return_motion
    pose = safe_return(world, entity, frame.pose, origin,
                       grounded=return_motion is None or return_motion.motion.grounded,
                       activation_grounded=snapshot.drive.motion.grounded)
    if pose is None:
        pose = return_spawn(arena, owner)
    if pose is None:
        return None
    owner.flags = 0
    owner.gun.set_angles(*frame.aim)
    owner.gun.yaw, owner.gun.pitch = frame.aim
    owner.gun.movement_dispersion = frame.movement_dispersion
    owner.gun.aim_progress = frame.aim_progress
    world.set_turret(owner.gun.yaw, entity, owner.gun.pitch)
    motion = return_motion if pose is frame.pose else None
    if pose is origin and not snapshot.drive.motion.grounded:
        motion = snapshot.drive
    owner.pose = pose = place_returning_body(world, entity, pose, motion)
    vx, _, vz = world.motion[entity].velocity
    owner.speed = vx*math.sin(pose.yaw) + vz*math.cos(pose.yaw)
    restore_aim_lock(arena, owner, frame.aim_lock)
    previous_shell = owner.shell_id
    owner.selected_shell, owner.next_shell = frame.selected_shell, frame.next_shell
    owner.ammo, owner.clip = frame.ammo, frame.clip
    owner.shots += frame.shots_fired
    owner.last_shot_at = None if frame.shot_age is None else arena.simulation_time - frame.shot_age
    owner.ammo_by_shell.update(frame.ammo_by_shell)
    owner.reload_at = arena.simulation_time + frame.reload_remaining if frame.reload_remaining else 0.
    owner.reload_kind, owner.reload_total = frame.reload_kind, frame.reload_total
    owner.pump_at = arena.simulation_time + frame.pump_remaining if frame.pump_remaining else 0.
    owner.pump_total, owner.pump_waiting = frame.pump_total, frame.pump_waiting
    frame.burst.restore(owner)
    by_id = {slot.spec.ident: slot for slot in frame.consumables}
    now = arena.simulation_time
    for index, slot in enumerate(owner.consumables):
        staged = by_id.get(slot.spec.ident) if slot is not None else None
        if staged is not None and staged.spec == slot.spec:
            restored = _shift_consumable(staged, now-frame.seconds)
            # The worker's serialized spec compares equal but is a new object.
            # Live ability dispatch selects the arena's installed spec by identity.
            restored.spec = slot.spec
            owner.consumables = (*owner.consumables[:index], restored, *owner.consumables[index+1:])
    if arena.big_boss is not None and frame.heal_wave is not None:
        _, due, count, role_level = frame.heal_wave
        if arena.big_boss.roles.get(owner.entity_id) == big_boss.REGENERATOR:
            arena.big_boss.heal_waves[owner.entity_id] = (now + max(0., due-frame.seconds), count, role_level)
    snapshot.restore_owner_status(arena)
    owner.consumable_clock = now
    arena._consumables(owner, now, refresh_cooldowns=True)
    if owner.shell_id != previous_shell:
        arena.emit('shell_selected', owner.entity_id, shell=owner.shell_id)
    arena.emit('ammo', owner.entity_id)
    arena.emit('reload', owner.entity_id, delay=frame.reload_remaining, total=owner.reload_total)
    if owner.pump_times:
        arena._emit_pump(owner, now)
    return ledger.merge(arena, refund_fraction=rules.healing_refund, defer_hit=defer_hit)
