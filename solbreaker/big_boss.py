"""Big Boss roles, tickets and Blast Wave tuning."""
from dataclasses import replace
import math
import random
from typing import NamedTuple

from .consumables import ConsumableSpec


MODE = 'big_boss'
TICKETS = 30
LIVES = 3
HEALTH_MULTIPLIER = 4
RELOAD_RATE = 2.
SHELL_DAMAGE = 1.4
ENGINE_POWER = 1.3
TOP_SPEED = 1.3
COUNTDOWN_SECONDS = 10.
BLAST_RADIUS = 50.
BLAST_DAMAGE = .10
BLAST_REFERENCE_MASS = 25000.
BLAST_BASE_SPEED = 44.
LIGHT_BLAST_AIR_DAMPING = .0017
CLIENT_GAME_MODE = 27
# Vehicle.publicStateModifiers key. The client maps this to particle extra 23.
BOSS_AURA_MODIFIER = 129
# Per-level public vehicle extras. 128 is the Megashot charger.
HEAL_AURA_MODIFIERS = tuple(range(118, 123))
POISONED_SHELL_MODIFIERS = tuple(range(123, 128))
BLAST_WAVE = ConsumableSpec('gravityBurst', 46, (46 << 8) | 0xfb, 0., 40.)
MEGASHOT = ConsumableSpec('railgunCharger', 47, (47 << 8) | 0xfb, 0., 40.)
MEGASHOT_INITIAL_COOLDOWN = 30.
MEGASHOT_DAMAGE = 1.3
MEGASHOT_SPEED = 1000.
# Index in the pinned shot_effects.xml. The client uses its railgun tracer.
MEGASHOT_EFFECT = 22
BOSS_BOOST = (47 << 8) | 0xfd
GRAVITON, BOMBER = 'graviton', 'bomber'
REGENERATOR, CATALYST = 'regenerator', 'catalyst'
HEAL_RADIUS = 50.
HEAL_INTERVAL = 3.
HEAL_FRACTION = .04
HEAL_SHOT = (.40, .50, .50, .50, .60)
SHIELD_DURATION = (10., 15., 20., 25., 30.)
POISON_DAMAGE = (.05, .07, .07, .07, .10)
POISON_INTERVAL = 2.
POISON_TICKS = 3
CATALYST_DAMAGE = (1.15, 1.20, 1.25, 1.30, 1.35)
CATALYST_RELOAD = (.95, .90, .85, .80, .75)
CATALYST_AIM = (.75, .75, .75, .75, .50)
TRAP_DURATION = 7.
TRAP_RADIUS = (60., 65., 70., 75., 80.)
TRAP_MASS = (70., 75., 80., 90., 100.)
TRAP_SHIELD = (.10, .15, .20, .25, .30)
BOMB_RADIUS = 50.
BOMB_DAMAGE = (.20, .30, .30, .40, .50)
BOMB_MOBILITY = (1.15, 1.25, 1.25, 1.35, 1.40)
TRAP_MAX_SPEED = 18.
TRAP_EDGE_WIDTH = 10.
TRAP_RESPONSE = 8.
TRAP_DRIVE_SPEED_FACTOR = .35
TRAP_DRIVE_SPEED_CAP = 5.
TRAP_TURN_FACTOR = .55
# The pinned client defines per-level specs. Other modes do not load these slots.
ROLE_ACTIVE = {
    role: tuple(ConsumableSpec(f'{tag}L{level+1}', base+level,
                               ((base+level) << 8) | 0xfb, durations[level], cooldown)
                for level, cooldown in enumerate(cooldowns))
    for role, tag, base, durations, cooldowns in (
        (GRAVITON, 'gravityShot', 68, (0.,)*5, (45., 45., 40., 40., 35.)),
        (BOMBER, 'kamikazeAbility', 58, (0.,)*5, (40., 40., 35., 35., 30.)),
        (REGENERATOR, 'healingAura', 48, (6., 9., 12., 15., 18.), (45., 45., 40., 40., 35.)),
        (CATALYST, 'poisonedShell', 63, (10., 10., 10., 15., 20.), (35., 35., 30., 30., 25.)),
    )
}
SHIELD_SPECS = tuple(ConsumableSpec(f'shieldGiverAbilityL{level+1}', 53+level,
                                    ((53+level) << 8) | 0xfb, 0., cooldown)
                     for level, cooldown in enumerate((40., 35., 35., 30., 30.)))
ROLE_PROVISION = {GRAVITON: 48, BOMBER: 63, REGENERATOR: 53, CATALYST: 58}
ROLE_HUD = {GRAVITON: 6, BOMBER: 5, REGENERATOR: 2, CATALYST: 4}
BOSS = 'boss'
PICKABLE_ROLES = (BOSS, GRAVITON, BOMBER, REGENERATOR, CATALYST)
ROLE_LEVEL_COUNT = len(ROLE_ACTIVE[GRAVITON])
ACTIVE_SPECS = frozenset(spec for specs in ROLE_ACTIVE.values() for spec in specs)
ROLE_SPECS = ACTIVE_SPECS.union(SHIELD_SPECS)
ROLE_INPUT_IDS = frozenset(spec.ident for spec in ACTIVE_SPECS)
GRAVITON_INPUT_IDS = frozenset(spec.ident for spec in ROLE_ACTIVE[GRAVITON])
ROLE_EFFECTS = frozenset(spec.compact_id for role in (BOMBER, REGENERATOR)
                         for spec in ROLE_ACTIVE[role])
HEAL_SHOT_EFFECTS = tuple(((53+level) << 8) | 0xfd for level in range(5))


def role_shell(shell, role, level, *, poisoned=False):
    if role == REGENERATOR:
        return replace(shell, healing_level=level)
    if role == CATALYST:
        return replace(shell, damage=shell.damage*CATALYST_DAMAGE[level],
                       poison_level=level if poisoned else None)
    return shell


def megashot(shell):
    return replace(shell, kind='ARMOR_PIERCING', railgun=True,
                   damage=shell.damage*MEGASHOT_DAMAGE, speed=MEGASHOT_SPEED,
                   gravity=0., missile=None, effect_index=MEGASHOT_EFFECT)


class Poison(NamedTuple):
    damage: int
    next_tick: float
    remaining: int
    started: float


class GravityTrap(NamedTuple):
    ident: int
    owner: int
    team: int
    center: tuple[float, float, float]
    level: int
    expires_at: float


class BigBoss:
    def __init__(self, vehicles, seed, *, endless=False):
        rng = random.Random(seed + ':bosses')
        self.selected = {}
        self.bosses = set()
        for team in (1, 2):
            candidates = [v for v in vehicles.values() if v.team == team]
            humans = [v for v in candidates if v.account_id is not None]
            if candidates:
                self.bosses.add(rng.choice(humans or candidates).entity_id)
        self.lives = dict.fromkeys(self.bosses, LIVES)
        self.tickets = [] if endless else [TICKETS, TICKETS]
        self.impulses = {}
        self.blast_landings = {}
        self._role_rng = random.Random(seed + ':minions')
        self.roles = {}
        self.levels = {}
        self.experience = {}
        self.traps = []
        self.next_trap = 1
        self.heal_waves = {}
        self.shields = {}
        self.poison = {}
        for entity in sorted(vehicles):
            if entity not in self.bosses:
                self.assign_role(entity)

    def assign_role(self, entity):
        previous = self.roles.get(entity)
        choices = tuple(role for role in ROLE_ACTIVE if role != previous)
        self.roles[entity] = self.selected.get(entity) or self._role_rng.choice(choices)
        self.levels.setdefault(entity, 0)

    def advance_level(self, entity, damage, max_health):
        if entity not in self.roles:
            return False
        # Local progression pending the exact server thresholds. One level
        # per own max HP dealt, tracked across lives and role changes.
        progress = self.experience.get(entity, 0.) + damage / max_health
        self.experience[entity] = progress
        level = min(ROLE_LEVEL_COUNT - 1, int(progress))
        if level <= self.levels[entity]:
            return False
        self.levels[entity] = level
        return True

    def role_progress(self, entity, max_health):
        level = self.levels.get(entity, 0)
        if entity not in self.roles or level >= ROLE_LEVEL_COUNT - 1:
            return 0, 0
        current = round((self.experience.get(entity, 0.) - level) * max_health)
        return max(0, min(current, max_health - 1)), max_health

    def abilities(self, entity):
        if entity in self.bosses:
            return BOSS_BOOST, BLAST_WAVE.compact_id, MEGASHOT.compact_id
        role, level = self.roles[entity], self.levels[entity]
        cards = (((ROLE_PROVISION[role] + level) << 8) | 0xfd, ROLE_ACTIVE[role][level].compact_id)
        return (*cards, SHIELD_SPECS[level].compact_id) if role == REGENERATOR else cards

    def take_shield(self, entity, now):
        shield = self.shields.get(entity)
        if shield is None or not shield[0] <= now < shield[1]:
            return False
        del self.shields[entity]
        return True

    def vehicle_effects(self, entity):
        effects = []
        if entity in self.shields:
            start, expires = self.shields[entity]
            effects.append((1, 1, start, expires))
        poisons = self.poison.get(entity)
        if poisons:
            start = min(poison.started for poison in poisons.values())
            expires = max(poison.next_tick + (poison.remaining-1)*POISON_INTERVAL
                          for poison in poisons.values())
            effects.append((2, 2, start, expires))
        return tuple(effects)

    def mobility(self, entity):
        return BOMB_MOBILITY[self.levels[entity]] if self.roles.get(entity) == BOMBER else 1.

    def damage_factor(self, entity):
        return 1. - TRAP_SHIELD[self.levels[entity]] if self.roles.get(entity) == GRAVITON else 1.

    def pull(self, team, position, mass):
        return gravity_pull(self.traps, team, position, mass)

    def destroyed(self, vehicle):
        entity = vehicle.entity_id
        boss = entity in self.bosses
        self.impulses.pop(entity, None)
        self.blast_landings.pop(entity, None)
        if not self.tickets:
            return True
        self.tickets[vehicle.team-1] = max(0, self.tickets[vehicle.team-1] - (5 if boss else 1))
        if boss:
            self.lives[entity] -= 1
        return not boss or self.lives[entity] > 0

    def leading_team(self):
        first, second = self.tickets
        return 1 if first > second else 2 if second > first else 0


def gravity_pull(traps, team, position, mass):
    """Combine signed directions, then amplify overlapping allied fields."""
    dx = dz = strength = overlap = 0.
    for trap in traps:
        if trap.team == team:
            continue
        depth = TRAP_RADIUS[trap.level] - math.dist(trap.center, position)
        if depth <= 0.:
            continue
        x, z = trap.center[0]-position[0], trap.center[2]-position[2]
        distance = math.hypot(x, z)
        # Slow near the centre so the hull does not oscillate across it.
        speed = TRAP_MASS[trap.level] / 10. * min(1., distance / 4.)
        coverage = min(1., depth / TRAP_EDGE_WIDTH)
        speed *= coverage
        if distance > 0.:
            dx += speed * x / distance
            dz += speed * z / distance
        strength += speed
        overlap += coverage
    if not overlap:
        return None
    speed = min(TRAP_MAX_SPEED, strength * 1.7**max(0., overlap-1.) *
                min(1.5, math.sqrt(BLAST_REFERENCE_MASS / max(mass, 1.))))
    if not strength:
        return (0., 0.)
    return dx / strength * speed, dz / strength * speed


def blast_impulse(origin, position, mass, yaw, vehicle_class=None):
    dx, dy, dz = (target-source for source, target in zip(origin, position))
    distance = math.hypot(dx, dy, dz)
    if distance >= BLAST_RADIUS:
        return None
    horizontal = math.hypot(dx, dz)
    x, z = (dx/horizontal, dz/horizontal) if horizontal else (math.sin(yaw), math.cos(yaw))
    mass_factor = min(1.18, math.sqrt(BLAST_REFERENCE_MASS / max(mass, 1.)))
    speed = BLAST_BASE_SPEED * mass_factor * (1. - .70 * distance / BLAST_RADIUS)
    heavy = (vehicle_class == 'heavyTank'
             or vehicle_class not in ('lightTank', 'mediumTank') and mass >= 55000.)
    if heavy:
        speed *= 1.2
        return (x * speed, 0., z * speed), None
    # Light hulls carry much farther on a close hit. Keep rotation tied to
    # the original impulse so the extra range does not multiply tumble speed.
    spin = speed * .14
    if vehicle_class == 'lightTank':
        speed *= 2.45
        lift = .55
    else:
        speed *= 1.4
        lift = .42
    return (x * speed, speed * lift, z * speed), (z * spin, 0., -x * spin)
