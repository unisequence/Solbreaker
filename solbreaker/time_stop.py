"""Private physics and effect reconciliation for a Time Stop branch.

This module does not publish trajectories or effects to battle sessions. The
caller owns the simulation worker and schedules reconciliation on its arena tick.
"""
from copy import copy
from dataclasses import dataclass, replace
import math
import random
import uuid

from . import big_boss
from .aiming import GunAim
from .ballistics import BodyState, Projectile, TrackContact, advance, body_states
from .bot_roster import vehicle_class
from .collision import CollisionWorld
from .sandbox import BRANCH_SHOT_BASE


@dataclass(frozen=True)
class Level:
    energy: int
    cooldown_multiplier: float
    radius: float

    def __post_init__(self):
        if (type(self.energy) is not int or self.energy < 0
                or not all(math.isfinite(value) and value > 0.
                           for value in (self.cooldown_multiplier, self.radius))):
            raise ValueError('Invalid Time Stop level')


LEVELS = (Level(50, 1., 100.), Level(65, 1.5, 150.), Level(80, 2., 200.),
          Level(100, 3., 250.), Level(125, 4., 300.))

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

    def __post_init__(self):
        if not math.isfinite(self.duration) or self.duration <= 0.:
            raise ValueError('Invalid Time Stop duration')
        for cost in (self.shot_cost, self.explosion_cost, self.shockwave_cost,
                     self.graviton_cost, self.healing_cost, self.poison_cost):
            if type(cost) is not int or cost < 0:
                raise ValueError('Time Stop action costs must be nonnegative integers')
        if not 0. <= self.healing_refund <= 1.:
            raise ValueError('Invalid Time Stop healing refund')


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
    invulnerable: bool
    gun_packed: bytes
    health: int
    max_health: int
    attached: bool
    mass: float
    tank_class: str | None
    damage_factor: float
    anti_he_factor: float
    gun_chance: float


@dataclass(frozen=True, slots=True)
class ProjectileTrace:
    shot: int
    shooter: int
    position: tuple
    velocity: tuple
    gravity: float
    effect: int
    guided: bool

    @classmethod
    def capture(cls, projectile):
        shell = projectile.shell
        effect = shell.effect_index if projectile.trace_effect is None else projectile.trace_effect
        return cls(projectile.shot, projectile.shooter, projectile.position, projectile.velocity,
                   shell.gravity, effect, shell.missile is not None)


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
    object_health: tuple = ()
    consumables: tuple = ()
    heal_wave: tuple | None = None
    role: str | None = None
    role_level: int = 0
    infinite_ammo: bool = False
    gun_destroyed: bool = False
    suspended_shield: tuple | None = None
    suspended_poison: tuple = ()

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
        tanks = tuple(FrozenTank(Entity.of(v), bodies[v.entity_id], v.team,
                                v.alive, v.sandbox_god_mode, v.gun.packed(), v.health,
                                v.max_health, arena._player_attached(v),
                                getattr(v.definition, 'mass', big_boss.BLAST_REFERENCE_MASS),
                                vehicle_class(arena._boss_game, v.definition)
                                if boss is not None and v.definition is not None else None,
                                boss.damage_factor(v.entity_id) if boss is not None else 1.,
                                getattr(v.definition, 'he_damage_factor', 1.),
                                arena.guns[v.entity_id].spec.projectile_chance
                                if v.entity_id in arena.guns else 0.)
                      for v in arena.vehicles.values())
        projectiles = tuple(ProjectileTrace.capture(p) for p in arena.projectiles.values())
        shell = arena.shell_for(vehicle)
        now = arena.simulation_time
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
                shield = shield[0]-now, shield[1]-now
            poison = tuple((source, state._replace(started=state.started-now,
                                                   next_tick=state.next_tick-now))
                           for source, state in boss.poison.get(vehicle.entity_id, {}).items())
        return cls(uuid.uuid4().hex, arena.step_count, Entity.of(vehicle), wall_time,
                   tanks, projectiles, frozenset(arena.world.destroyed),
                   vehicle.ammo, vehicle.clip, vehicle.clip_size, vehicle.shell_id,
                   max(0., vehicle.reload_at-arena.simulation_time),
                   copy(vehicle.gun), shell.max_distance if shell is not None else 500.,
                   tuple(arena.world.object_health.items()), consumables, heal_wave,
                   role, level, arena.unlimited_ammo or arena.practice, arena.gun_destroyed(vehicle),
                   shield, poison)

    def restore_owner_status(self, arena):
        boss = arena.big_boss
        if boss is None:
            return
        entity, now = self.owner.ident, arena.simulation_time
        if self.suspended_shield is not None and self.suspended_shield[1] > 0.:
            start, expires = self.suspended_shield
            boss.shields[entity] = now+start, now+expires
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
class Effect:
    branch: str
    sequence: int
    at: float
    source: Entity
    target: Entity | int
    kind: str
    value: int | Impulse | PropImpact | TrackContact | PoisonContinuation
    action: int
    shell_hit: bool


@dataclass(frozen=True)
class Outcome:
    applied: tuple
    skipped: tuple
    refund: int
    energy: int


class EffectLedger:
    """Charge once per action and reconcile each ordered effect once."""

    def __init__(self, branch, source, energy):
        self.branch = branch
        self.source = source
        self.capacity = self.energy = energy
        self._charges = {}
        self._events = []
        self._sealed = False
        self._aborted = False

    def inherit_action(self):
        """An ability activated before Fork owes no energy in this branch."""
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
                (kind in ('damage', 'heal') and type(value) is int and value > 0)
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

    def merge(self, arena, *, refund_fraction=.5):
        """Run on the authoritative tick; references select the current life."""
        self._sealed = True
        applied = []
        skipped = []
        refunds = set()
        source = arena.vehicles.get(self.source.ident)
        if (self._aborted or self.branch in arena._resolved_time_branches
                or source is None or Entity.of(source) != self.source or not source.alive
                or arena.finished_at):
            return Outcome((), (), 0, self.energy)
        arena._resolved_time_branches.add(self.branch)
        for effect in self.events:
            if self._apply(arena, source, effect):
                applied.append(effect.sequence)
                continue
            skipped.append(effect.sequence)
            if effect.kind == 'heal':
                refunds.add(effect.action)
        refund = min(self.capacity-self.energy,
                     sum(int(self._charges[action]*refund_fraction) for action in refunds))
        self.energy += refund
        return Outcome(tuple(applied), tuple(skipped), refund, self.energy)

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
            return target.team == source.team and arena._heal_health(source, target, value) > 0
        if target.team == source.team:
            return False
        if effect.kind == 'damage':
            return arena._damage_health(target, value, source.entity_id, now,
                                        shell_hit=effect.shell_hit) > 0
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
        sprung_tracks=geometry.sprung_tracks, dynamic_backend='bullet')


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
        self.last_shot = None
        self.abilities_used = 0
        self.last_ability = None
        self.ammo, self.clip = snapshot.ammo, snapshot.clip
        self.gun = copy(snapshot.gun)
        self.reload_at = snapshot.reload_remaining/level.cooldown_multiplier
        self.rng = random.Random(snapshot.ident) if rng is None else rng
        self.ledger = EffectLedger(snapshot.ident, snapshot.owner, level.energy)
        self.tanks = {tank.identity.ident: tank for tank in snapshot.tanks}
        self.health = {entity: tank.health for entity, tank in self.tanks.items()}
        self.consumables = {slot.spec.ident: copy(slot) for slot in snapshot.consumables}
        for slot in self.consumables.values():
            slot.ready_at = max(0., slot.ready_at)/level.cooldown_multiplier
        self.heal_wave = (0, *snapshot.heal_wave) if snapshot.heal_wave is not None else None
        if self.heal_wave is not None:
            self.ledger.inherit_action()
        self.traps = {}
        self.poison = {}
        self.frozen = {entity: tank.body for entity, tank in self.tanks.items() if entity != self.entity}
        self.frozen_armor = {entity: body.armor_state for entity, body in self.frozen.items()
                             if body.armor_state is not None}
        initial = {entity: tank.body.pose for entity, tank in self.tanks.items()}
        self.activation_pose = initial[self.entity]
        self.projectiles = {}
        self.shot_ends = []
        self.world = prepare_world(geometry) if world is None else world
        try:
            if set(self.world.bodies) != set(initial):
                raise ValueError('Time Stop snapshot does not match prepared geometry')
            self.world.relocate_training_body(self.entity, initial[self.entity])
            for entity, tank in self.tanks.items():
                self.world.poses[entity] = tank.body.pose
                self.world.set_pose(self.world.bodies[entity], tank.body.pose)
                self.world.set_turret(tank.body.turret, entity, tank.body.pitch)
            for key in sorted(snapshot.destroyed):
                self.world.destroy(key, (0., 0.))
            self.world.object_health.update(snapshot.object_health)
            self.world.destruction_events.clear()
        except BaseException:
            self.world.close()
            raise

    @property
    def pose(self):
        return self.world.poses[self.entity]

    @property
    def time(self):
        return self.steps*self.STEP

    def aim(self, turret, pitch):
        return not self.ended and self.gun.set_angles(turret, pitch)

    def _shot_ray(self):
        pose = self.pose
        rotation = self.world._dynamic_rotation(self.entity)
        if rotation is not None:
            return self.gun.ray_rotation(pose.position, rotation)
        return self.gun.ray(pose.position, pose.yaw, pose.pitch, pose.roll)

    def aim_marker(self):
        origin, direction = self._shot_ray()
        _, point, _ = self.world.shot_path(origin, direction, self.snapshot.aim_range,
                                          ignore_entity=self.entity,
                                          movable_armor_states=self.frozen_armor)
        return point, direction

    def tick(self, wall_time, *, distance=0., yaw_delta=0.):
        if self.ended:
            return False
        if not all(math.isfinite(value) for value in (wall_time, distance, yaw_delta)):
            raise ValueError('Invalid Time Stop step')
        if wall_time >= self.deadline:
            self.finish()
            return False
        if wall_time-self.snapshot.captured_at+1e-9 < self.time+self.STEP:
            return True
        last = self.pose
        assets = self.world.assets_for(self.entity)
        distance = min(assets.forward_speed*self.STEP, max(-assets.reverse_speed*self.STEP, distance))
        yaw_delta = min(assets.rotation_speed*self.STEP, max(-assets.rotation_speed*self.STEP, yaw_delta))
        if distance or yaw_delta or not self.world.motion[self.entity].grounded:
            self.world.move(distance, yaw_delta, self.entity, dt=self.STEP)
        if self.world.dynamic_bodies or self.world.pending_dynamics:
            self.world.dynamic_commit(self.STEP)
        x, _, z = self.pose.position
        x0, _, z0 = self.activation_pose.position
        if math.hypot(x-x0, z-z0) > self.level.radius:
            self.world.relocate_training_body(self.entity, last)
        pose = self.pose
        self.gun.tick(self.STEP, pose.position, pose.yaw, pose.pitch, pose.roll)
        self.world.set_turret(self.gun.yaw, self.entity, self.gun.pitch)
        self.steps += 1
        if self.reload_at and self.time >= self.reload_at:
            self.reload_at = 0.
            self.clip = min(self.snapshot.clip_size, self.ammo)
        self._step_projectiles()
        self._advance_abilities()
        return True

    def activate(self, action, ident):
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
            start, direction = self._shot_ray()
            end = tuple(a+b*self.snapshot.aim_range for a, b in zip(start, direction))
            body, _, center = self.world.static_segment(start, end)
            if body is None:
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
                self._damage(action, self.time, target, damage)

    def _observe_trap(self, action, started, trap, at):
        strength = -math.expm1(-big_boss.TRAP_RESPONSE*(at-started))
        if not strength:
            return
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
                if amount > 0:
                    self.health[entity] += amount
                    self.ledger.record(action, due, target.identity, 'heal', amount)
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

    def fire(self, action, shell, *, cooldown):
        """Fire an authorized direct-fire round from a server-computed muzzle ray."""
        if (self.ended or self.reload_at or not self.ammo or not self.clip or shell.missile is not None
                or self.snapshot.gun_destroyed
                or self.snapshot.clip_size != 1
                or shell.compact_id != self.snapshot.selected_shell
                or shell.kind not in ('ARMOR_PIERCING', 'ARMOR_PIERCING_CR', 'HOLLOW_CHARGE',
                                     'HIGH_EXPLOSIVE')):
            return False
        if not math.isfinite(cooldown) or cooldown <= 0.:
            raise ValueError('Invalid Time Stop fire data')
        origin, direction = self._shot_ray()
        if not self.ledger.charge(action, self.rules.shot_cost):
            return False
        shell = self._decorate_shell(shell)
        if not self.snapshot.infinite_ammo:
            self.ammo -= 1
        self.shots_fired += 1
        self.clip = max(0, self.clip-1)
        self.reload_at = self.time+cooldown/self.level.cooldown_multiplier
        velocity = tuple(v*shell.speed for v in direction)
        self.projectiles[action] = Projectile(action, self.entity, shell, origin, velocity, self.time)
        self.last_shot = (action, origin, velocity, shell.gravity, shell.effect_index, shell.compact_id)
        return True

    def _decorate_shell(self, shell):
        role, level = self.snapshot.role, self.snapshot.role_level
        if role == big_boss.BOSS:
            shell = replace(shell, damage=shell.damage*big_boss.SHELL_DAMAGE)
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

    def _step_projectiles(self):
        if not self.projectiles:
            return
        contacts = []
        for action, projectile in self.projectiles.items():
            def prop_contact(key, direction, at):
                self.ledger.record(action, at, key, 'prop', PropImpact(direction))

            impact = advance(projectile, self.world, self.frozen, self.frozen, self.STEP, self.rng,
                             prop_contact=prop_contact)
            contacts.extend((contact.at, action, 0, contact) for contact in projectile.drain_track_contacts())
            if impact is not None:
                contacts.append((impact.at, action, 1, impact))
        for at, action, kind, contact in sorted(contacts, key=lambda item: item[:3]):
            target = self.tanks.get(contact.target)
            if kind == 0:
                if (self._enemy(target) and contact.penetrated and contact.shell.damage_devices > 0
                        and (contact.hit.part in ('chassis_track_L', 'chassis_track_R')
                             or target.gun_chance >= 1.
                             or 0. < target.gun_chance > self.rng.random())):
                    self.ledger.record(action, at, target.identity, 'module', contact)
                continue
            self._impact(action, contact, target)
            explosion = None
            shell = contact.shell
            if shell.kind == 'HIGH_EXPLOSIVE' and contact.collision:
                explosion = shell.compact_id, 3 if contact.result.hit else 0, contact.direction
            self.shot_ends.append((action, contact.point, explosion))
            del self.projectiles[action]

    def _enemy(self, target):
        return (target is not None and target.alive and not target.invulnerable
                and target.team != self.tanks[self.entity].team)

    def _damage(self, action, at, target, amount, *, shell_hit=False, high_explosive=False):
        factor = target.damage_factor * (target.anti_he_factor if high_explosive else 1.)
        rounded = int(amount*factor+.5)
        if rounded > 0:
            self.ledger.record(action, at, target.identity, 'damage', rounded, shell_hit=shell_hit)
        return rounded > 0

    def _shell_damage(self, action, at, target, amount, shell):
        if not self._damage(action, at, target, amount, shell_hit=True,
                            high_explosive=shell.kind == 'HIGH_EXPLOSIVE'):
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
            if amount > 0:
                self.health[entity] += amount
                self.ledger.record(action, impact.at, target.identity, 'heal', amount)
            return
        if impact.obstacle is not None:
            direction = impact.direction[::2]
            self.world.destroy(impact.obstacle, direction, damage=shell.damage)
            self.ledger.record(action, impact.at, impact.obstacle, 'prop', PropImpact(direction, shell.damage))
        he = shell.kind == 'HIGH_EXPLOSIVE'
        if he and impact.collision and impact.result.outcome != 'penetrated':
            for entity, state in self.frozen.items():
                tank = self.tanks[entity]
                if not self._enemy(tank):
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
        self.shot_ends.extend((action, projectile.position, None)
                              for action, projectile in self.projectiles.items())
        self.projectiles.clear()
        self.ledger.seal()

    def abort(self):
        self.finish()
        self.ledger.abort()

    def close(self):
        if not self.ended:
            self.abort()
        self.world.close()


def safe_return(world, entity, preferred, activation):
    """Find a valid landing in the current battle geometry, or defer return."""
    px, _, pz = preferred.position
    candidates = [(px, pz, preferred.yaw)]
    for radius in (1., 2., 4., 8., 12.):
        for index in range(16):
            angle = math.tau*index/16
            candidates.append((px+radius*math.cos(angle), pz+radius*math.sin(angle), preferred.yaw))
    candidates.append((*activation.position[::2], activation.yaw))
    for x, z, yaw in candidates:
        pose = world.training_placement(entity, x, z, yaw, stationary=True)
        if pose is not None:
            # The support solver owns pitch and roll; yaw remains player-selected.
            return pose
    return None


def merge_frame(arena, snapshot, frame, *, rules=Rules()):
    """Place the owner and apply a completed worker frame on an arena tick.

    If all checked locations are blocked, leave the result available for a
    later tick. The caller is responsible for keeping the owner parked.
    """
    ledger = frame.ledger
    if (ledger is None or ledger.branch != frame.branch or frame.branch != snapshot.ident
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
    pose = safe_return(arena.world, owner.entity_id, frame.pose, origin)
    if pose is None:
        return None
    arena.world.relocate_training_body(owner.entity_id, pose)
    arena.world.set_tracked_presence(owner.entity_id, True)
    owner.pose = pose
    owner.speed = owner.flags = 0
    owner.gun.set_angles(*frame.aim)
    owner.gun.yaw, owner.gun.pitch = frame.aim
    arena.world.set_turret(owner.gun.yaw, owner.entity_id, owner.gun.pitch)
    owner.ammo, owner.clip = frame.ammo, frame.clip
    owner.shots += frame.shots_fired
    owner.ammo_by_shell[owner.shell_id] = owner.ammo
    owner.reload_at = arena.simulation_time + frame.reload_remaining if frame.reload_remaining else 0.
    by_id = {slot.spec.ident: slot for slot in frame.consumables}
    now = arena.simulation_time
    for index, slot in enumerate(owner.consumables):
        staged = by_id.get(slot.spec.ident) if slot is not None else None
        if staged is not None and staged.spec == slot.spec:
            owner.consumables = (*owner.consumables[:index], _shift_consumable(staged, now-frame.seconds),
                                 *owner.consumables[index+1:])
    if arena.big_boss is not None and frame.heal_wave is not None:
        _, due, count, role_level = frame.heal_wave
        if arena.big_boss.roles.get(owner.entity_id) == big_boss.REGENERATOR:
            arena.big_boss.heal_waves[owner.entity_id] = (now + max(0., due-frame.seconds), count, role_level)
    snapshot.restore_owner_status(arena)
    owner.consumable_clock = now
    arena._consumables(owner, now, refresh_cooldowns=True)
    arena.emit('ammo', owner.entity_id)
    arena.emit('reload', owner.entity_id, delay=frame.reload_remaining, total=owner.reload_total)
    return ledger.merge(arena, refund_fraction=rules.healing_refund)
