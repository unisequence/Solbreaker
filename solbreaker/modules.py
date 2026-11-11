"""Local track, gun, engine, ammo-rack and fuel-tank state under sandbox rules.

This is not a reconstruction of the official server's module damage or repair
simulation. Nominal shell device damage is supplied by the caller after a real
penetrated track contact. Natural recovery uses XML regeneration values: a
track breaks at zero HP, regains traction at hysteresis HP, and recovers only
up to max_regen_health. A repair kit restores full HP. Partial HP stays critical.
restore_full_on_repair (set only by Enhanced Tracks, raw112) is a narrow LOCAL
policy, not the official module algorithm: a broken track that reaches the
hysteresis recovery threshold returns straight to max_health. HP limits, rates
and partial-damage regeneration are unchanged.

The gun (GunHealth, extra 36) follows the same nominal damage rule but has no
natural recovery: merged gun XML has no hysteresisHealth/healthRegenPerSec, and
maxRegenHealth alone is not a rate. Its status is local sandbox policy: full HP
intact, partial HP critical (1), zero HP destroyed (2). Only a repair kit
restores it, to full maxHealth. The official critical threshold, recovery and
chance/vulnerability composition are unknown and are not modelled.

Engine Health (extra 31 in the pinned XML) has no recovered server transition
model. The engine state below is a LOCAL sandbox policy: maxRegenHealth is a
critical threshold, critical power is one half, zero HP immobilizes, and a
repair restores full HP. It has no natural repair clock, ignition, fire tick,
or RNG behavior; fireStartingChance remains metadata for a later mechanism.

Ammo-rack health uses a similarly explicit local maxRegenHealth critical
boundary. Its live critical reload factor is one half; zero HP requests
catastrophic destruction through Arena. No automatic recovery is invented.

Fuel-tank health has the same explicit local critical boundary and no natural
recovery or fire simulation. Zero HP is a destroyed internal state; Arena
must keep its public damage marker orange rather than emit a red device.
"""
from dataclasses import dataclass
import math


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a finite nonnegative number')
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f'{name} must be a finite nonnegative number') from None
    if not math.isfinite(result) or result < 0:
        raise ValueError(f'{name} must be a finite nonnegative number')
    return result


@dataclass(frozen=True)
class TrackSpec:
    max_health: float
    max_regen_health: float
    hysteresis_health: float
    regen_per_sec: float
    restore_full_on_repair: bool = False

    def __post_init__(self):
        for name in ('max_health', 'max_regen_health', 'hysteresis_health', 'regen_per_sec'):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        if type(self.restore_full_on_repair) is not bool:
            raise ValueError('restore_full_on_repair must be a bool')
        if not 0 < self.hysteresis_health <= self.max_regen_health <= self.max_health:
            raise ValueError('Expected 0 < hysteresis_health <= max_regen_health <= max_health')
        if self.regen_per_sec <= 0:
            raise ValueError('regen_per_sec must be positive')


@dataclass(frozen=True)
class ModuleProtection:
    """Effects of the two ImprovedBattleCompartment devices.

    The track multiplier is already reflected in the derived TrackSpec. The
    three projectile factors are applied to copies of per-entity internal
    specs by Arena, never to a shared CollisionScene. Ramming absorption
    scales the local opt-in ram HP rule in Arena.
    """
    track_health_factor: float = 1.0
    ramming_absorption_percent: float = 0.0
    engine_chance_factor: float = 1.0
    ammo_rack_chance_factor: float = 1.0
    crew_chance_factor: float = 1.0

    def __post_init__(self):
        for name in ('track_health_factor', 'ramming_absorption_percent',
                     'engine_chance_factor', 'ammo_rack_chance_factor',
                     'crew_chance_factor'):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        if self.track_health_factor < 1:
            raise ValueError('track_health_factor must be at least 1')
        if self.ramming_absorption_percent >= 100:
            raise ValueError('ramming_absorption_percent must be below 100')
        if not all(0 < factor <= 1 for factor in (self.engine_chance_factor,
                                                   self.ammo_rack_chance_factor,
                                                   self.crew_chance_factor)):
            raise ValueError('Projectile chance factors must be in (0, 1]')

    @property
    def identity(self):
        return self == ModuleProtection()


class TrackState:
    """Independent HP, broken latch and monotonic simulation clock for one track.

    advance() reports status transitions, not incremental HP recovery. damage()
    and repair() report HP or status changes, including transitions caused by
    advancing first. The first advance, repair or positive-damage call anchors
    the clock; no recovery before its timestamp is invented. Zero damage is a
    strict no-op after validation, including for the clock and last_damage_at.
    """
    __slots__ = ('_spec', '_health', '_broken', '_last_time', '_last_damage_at')

    def __init__(self, spec):
        if not isinstance(spec, TrackSpec):
            raise ValueError('Expected TrackSpec')
        self._spec = spec
        self._health = spec.max_health
        self._broken = False
        self._last_time = None
        self._last_damage_at = None

    @property
    def spec(self):
        return self._spec

    @property
    def health(self):
        return self._health

    @property
    def broken(self):
        return self._broken

    @property
    def critical(self):
        return self._health < self._spec.max_health

    @property
    def last_damage_at(self):
        return self._last_damage_at

    def _time(self, now):
        now = _number(now, 'time')
        if self._last_time is not None and now < self._last_time:
            raise ValueError('Track clock cannot go backwards')
        return now

    def advance(self, now):
        now = self._time(now)
        previous = self._last_time
        self._last_time = now
        if previous is None:
            return False
        before = (self.broken, self.critical)
        if self._health < self._spec.max_regen_health:
            self._health = min(self._spec.max_regen_health,
                               self._health + (now - previous) * self._spec.regen_per_sec)
        if self._broken and self._health >= self._spec.hysteresis_health:
            self._broken = False
            if self._spec.restore_full_on_repair:
                # Local raw112 rule from "fully repairs the tracks automatically if
                # they're destroyed": only this latch release restores full HP.
                self._health = self._spec.max_health
        return before != (self.broken, self.critical)

    def damage(self, amount, now):
        amount = _number(amount, 'damage')
        now = self._time(now)
        if amount == 0:
            return False
        changed = self.advance(now)
        health = max(0., self._health - amount)
        changed = changed or health != self._health
        self._health = health
        if health == 0:
            changed = changed or not self._broken
            self._broken = True
        self._last_damage_at = now
        return changed

    def repair(self, now):
        now = self._time(now)
        changed = self.advance(now)
        changed = changed or self._health != self._spec.max_health or self._broken
        self._health = self._spec.max_health
        self._broken = False
        return changed


@dataclass(frozen=True)
class GunSpec:
    """Selected gun maxHealth/maxRegenHealth and merged materialKinds gun chance.

    projectile_chance is the per-gun armor/gun override if present, otherwise
    common/vehicle.xml materials/gun. max_regen_health is kept as metadata only.
    """
    max_health: float
    max_regen_health: float
    projectile_chance: float

    def __post_init__(self):
        for name in ('max_health', 'max_regen_health', 'projectile_chance'):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        if not 0 < self.max_regen_health <= self.max_health:
            raise ValueError('Expected 0 < max_regen_health <= max_health')
        if self.projectile_chance > 1:
            raise ValueError('projectile_chance must not exceed 1')


class GunState:
    """Independent HP of one vehicle's external gun; no clock, no recovery."""
    __slots__ = ('_spec', '_health')

    def __init__(self, spec):
        if not isinstance(spec, GunSpec):
            raise ValueError('Expected GunSpec')
        self._spec = spec
        self._health = spec.max_health

    @property
    def spec(self):
        return self._spec

    @property
    def health(self):
        return self._health

    @property
    def destroyed(self):
        return self._health == 0

    @property
    def critical(self):
        return 0 < self._health < self._spec.max_health

    @property
    def damaged(self):
        return self._health < self._spec.max_health

    def damage(self, amount):
        """Subtract nominal damage; return the HP actually removed."""
        amount = _number(amount, 'damage')
        applied = min(self._health, amount)
        self._health -= applied
        return applied

    def repair(self):
        changed = self._health != self._spec.max_health
        self._health = self._spec.max_health
        return changed


@dataclass(frozen=True)
class EngineSpec:
    """Raw engine health/chance metadata plus a narrow local critical policy.

    ``critical_health`` is XML maxRegenHealth used only as the local policy
    boundary. It is not a recovered official engine threshold. The chance
    fields and ``extra_index`` are retained for a future hit/fire integration;
    this class never rolls them or starts fire.
    """
    max_health: float
    critical_health: float
    projectile_chance: float
    fire_starting_chance: float
    extra_index: int

    def __post_init__(self):
        for name in ('max_health', 'critical_health', 'projectile_chance', 'fire_starting_chance'):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        if not 0 < self.critical_health <= self.max_health:
            raise ValueError('Expected 0 < critical_health <= max_health')
        if self.projectile_chance > 1 or self.fire_starting_chance > 1:
            raise ValueError('Engine chances must not exceed 1')
        if type(self.extra_index) is not int or not 0 <= self.extra_index <= 0xFF:
            raise ValueError('extra_index must be a uint8')


class EngineState:
    """Independent local engine HP; no natural repair, clock, fire, or RNG.

    This is not a vehicle movement implementation. At zero HP power_factor is
    zero; from one through the XML-derived local critical boundary it is one
    half while damaged; full HP is always intact. repair() restores full HP.
    """
    __slots__ = ('_spec', '_health')

    def __init__(self, spec):
        if not isinstance(spec, EngineSpec):
            raise ValueError('Expected EngineSpec')
        self._spec = spec
        self._health = spec.max_health

    @property
    def spec(self):
        return self._spec

    @property
    def health(self):
        return self._health

    @property
    def destroyed(self):
        return self._health == 0

    @property
    def critical(self):
        return 0 < self._health < self._spec.max_health and self._health <= self._spec.critical_health

    @property
    def power_factor(self):
        if self.destroyed:
            return 0.
        return .5 if self.critical else 1.

    def damage(self, amount):
        """Apply nominal device damage and return the HP actually removed."""
        amount = _number(amount, 'damage')
        if amount == 0:
            return 0.
        applied = min(self._health, amount)
        self._health -= applied
        return applied

    def repair(self):
        """Restore full local HP; repeat calls are idempotent."""
        changed = self._health != self._spec.max_health
        self._health = self._spec.max_health
        return changed


def engine_spec_from_metadata(metadata):
    """Build an EngineSpec from immutable XML metadata without battle effects.

    The import is local because vehicle_data imports this module while
    module_data imports vehicle_data. A single local hull/engine material may
    override the common projectile chance, including explicit zero. Repeated
    local declarations are refused rather than guessed.
    """
    from .module_data import VehicleModuleMetadata

    if not isinstance(metadata, VehicleModuleMetadata):
        raise ValueError('Expected VehicleModuleMetadata')
    engine = metadata.module('engine')
    if engine is None:
        raise ValueError('Missing selected engine metadata')
    if (engine.hysteresis_health.value is not None
            or engine.health_regen_per_sec.value is not None):
        raise ValueError('Unsupported engine natural-repair metadata')
    if engine.max_health.value is None or engine.max_regen_health.value is None:
        raise ValueError('Missing engine health metadata')
    if engine.fire_starting_chance.value is None:
        raise ValueError('Missing engine fire-starting metadata')

    common = [item for item in metadata.common_materials if item.name == 'engine']
    if len(common) != 1 or common[0].extra.value != 'engineHealth':
        raise ValueError('Missing or ambiguous common engine material metadata')
    extra = [item for item in metadata.extras if item.name == 'engineHealth']
    if len(extra) != 1:
        raise ValueError('Missing or ambiguous engineHealth XML extra')

    local = [item for item in metadata.local_materials
             if item.component in ('hull', 'engine') and item.name == 'engine']
    if len(local) > 1:
        raise ValueError('Ambiguous local engine material metadata')
    if local and local[0].chance_to_hit_by_projectile.value is not None:
        projectile_chance = local[0].chance_to_hit_by_projectile.value
    else:
        projectile_chance = common[0].chance_to_hit_by_projectile.value
    if projectile_chance is None:
        raise ValueError('Missing engine projectile chance metadata')

    return EngineSpec(engine.max_health.value, engine.max_regen_health.value,
                      projectile_chance, engine.fire_starting_chance.value,
                      extra[0].index)


@dataclass(frozen=True)
class AmmoRackSpec:
    """Selected hull ammoBayHealth and projectile material chance.

    maxRegenHealth supplies a LOCAL critical boundary, as with EngineSpec.
    It is not a regeneration rate. The rack has no natural recovery clock.
    """
    max_health: float
    critical_health: float
    projectile_chance: float
    extra_index: int

    def __post_init__(self):
        for name in ('max_health', 'critical_health', 'projectile_chance'):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        if not 0 < self.critical_health <= self.max_health:
            raise ValueError('Expected 0 < critical_health <= max_health')
        if self.projectile_chance > 1:
            raise ValueError('Ammo rack chance must not exceed 1')
        if type(self.extra_index) is not int or not 0 <= self.extra_index <= 0xFF:
            raise ValueError('extra_index must be a uint8')


class AmmoRackState:
    """Independent rack HP; zero HP must destroy the owning vehicle in Arena.

    A damaged live rack at/below the local boundary halves reload speed.
    At zero HP no reload factor is meaningful: Arena must resolve detonation
    and death, rather than feed a zero divisor to reload/pump timers. Repair
    of a live vehicle restores full HP; it never resurrects a vehicle.
    """
    __slots__ = ('_spec', '_health')

    def __init__(self, spec):
        if not isinstance(spec, AmmoRackSpec):
            raise ValueError('Expected AmmoRackSpec')
        self._spec, self._health = spec, spec.max_health

    @property
    def spec(self):
        return self._spec

    @property
    def health(self):
        return self._health

    @property
    def destroyed(self):
        return self._health == 0

    @property
    def damaged(self):
        return self._health < self._spec.max_health

    @property
    def critical(self):
        return 0 < self._health < self._spec.max_health and self._health <= self._spec.critical_health

    @property
    def reload_speed_factor(self):
        return .5 if self.critical else 1.

    def damage(self, amount):
        amount = _number(amount, 'damage')
        applied = min(self._health, amount)
        self._health -= applied
        return applied

    def repair(self):
        changed = self.damaged
        self._health = self._spec.max_health
        return changed


def ammo_rack_spec_from_metadata(metadata):
    """Resolve raw selected rack data; explicit local chance zero is retained."""
    from .module_data import VehicleModuleMetadata

    if not isinstance(metadata, VehicleModuleMetadata):
        raise ValueError('Expected VehicleModuleMetadata')
    rack = metadata.module('ammoBay')
    if rack is None or rack.max_health.value is None or rack.max_regen_health.value is None:
        raise ValueError('Missing selected ammo rack health metadata')
    if rack.hysteresis_health.value is not None or rack.health_regen_per_sec.value is not None:
        raise ValueError('Unsupported ammo rack natural-repair metadata')
    common = [item for item in metadata.common_materials if item.name == 'ammoBay']
    if len(common) != 1 or common[0].extra.value != 'ammoBayHealth':
        raise ValueError('Missing or ambiguous common ammo rack material metadata')
    extra = [item for item in metadata.extras if item.name == 'ammoBayHealth']
    if len(extra) != 1:
        raise ValueError('Missing or ambiguous ammoBayHealth XML extra')
    local = [item for item in metadata.local_materials
             if item.component in ('hull', 'ammoBay') and item.name == 'ammoBay']
    if len(local) > 1:
        raise ValueError('Ambiguous local ammo rack material metadata')
    chance = (local[0].chance_to_hit_by_projectile.value
              if local and local[0].chance_to_hit_by_projectile.value is not None
              else common[0].chance_to_hit_by_projectile.value)
    if chance is None:
        raise ValueError('Missing ammo rack projectile chance metadata')
    return AmmoRackSpec(rack.max_health.value, rack.max_regen_health.value,
                        chance, extra[0].index)


@dataclass(frozen=True)
class FuelTankSpec:
    """Selected fuelTank health; maxRegenHealth is a LOCAL critical boundary."""
    max_health: float
    critical_health: float
    projectile_chance: float
    extra_index: int

    def __post_init__(self):
        for name in ('max_health', 'critical_health', 'projectile_chance'):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        if not 0 < self.critical_health <= self.max_health:
            raise ValueError('Expected 0 < critical_health <= max_health')
        if self.projectile_chance > 1:
            raise ValueError('Fuel tank chance must not exceed 1')
        if type(self.extra_index) is not int or not 0 <= self.extra_index <= 0xFF:
            raise ValueError('extra_index must be a uint8')


class FuelTankState:
    """Independent fuel HP without ignition, fire ticks or natural repair.

    critical describes damaged positive HP at/below the local boundary.
    destroyed describes zero HP. Neither selects a public marker: Arena must
    represent both critical and destroyed fuel with orange extra33, not red.
    repair() restores full HP and is idempotent.
    """
    __slots__ = ('_spec', '_health')

    def __init__(self, spec):
        if not isinstance(spec, FuelTankSpec):
            raise ValueError('Expected FuelTankSpec')
        self._spec, self._health = spec, spec.max_health

    @property
    def spec(self):
        return self._spec

    @property
    def health(self):
        return self._health

    @property
    def damaged(self):
        return self._health < self._spec.max_health

    @property
    def critical(self):
        return 0 < self._health < self._spec.max_health and self._health <= self._spec.critical_health

    @property
    def destroyed(self):
        return self._health == 0

    def damage(self, amount):
        amount = _number(amount, 'damage')
        applied = min(self._health, amount)
        self._health -= applied
        return applied

    def repair(self):
        changed = self.damaged
        self._health = self._spec.max_health
        return changed


def fuel_tank_spec_from_metadata(metadata):
    """Resolve selected fuel and material chance, preserving explicit local0."""
    from .module_data import VehicleModuleMetadata

    if not isinstance(metadata, VehicleModuleMetadata):
        raise ValueError('Expected VehicleModuleMetadata')
    fuels = [item for item in metadata.modules if item.name == 'fuelTank']
    if len(fuels) != 1:
        raise ValueError('Missing or ambiguous selected fuel tank metadata')
    fuel = fuels[0]
    if fuel.max_health.value is None or fuel.max_regen_health.value is None:
        raise ValueError('Missing selected fuel tank health metadata')
    if fuel.hysteresis_health.value is not None or fuel.health_regen_per_sec.value is not None:
        raise ValueError('Unsupported fuel tank natural-repair metadata')
    common = [item for item in metadata.common_materials if item.name == 'fuelTank']
    if len(common) != 1 or common[0].extra.value != 'fuelTankHealth':
        raise ValueError('Missing or ambiguous common fuel tank material metadata')
    extra = [item for item in metadata.extras if item.name == 'fuelTankHealth']
    if len(extra) != 1:
        raise ValueError('Missing or ambiguous fuelTankHealth XML extra')
    local = [item for item in metadata.local_materials
             if item.component in ('hull', 'fuelTank') and item.name == 'fuelTank']
    if len(local) > 1:
        raise ValueError('Ambiguous local fuel tank material metadata')
    chance = (local[0].chance_to_hit_by_projectile.value
              if local and local[0].chance_to_hit_by_projectile.value is not None
              else common[0].chance_to_hit_by_projectile.value)
    if chance is None:
        raise ValueError('Missing fuel tank projectile chance metadata')
    return FuelTankSpec(fuel.max_health.value, fuel.max_regen_health.value,
                        chance, extra[0].index)


_CREW_ROLES = frozenset(('commander', 'gunner', 'driver', 'loader', 'radioman'))
# The common material name and its Health extra define the primary person.
# This is deliberately an explicit pinned mapping, rather than deriving an
# identity from the XML element text or from a crew role that a person shares.
_CREW_MATERIALS = {
    ('commander', 1): ('commander', 'commanderHealth'),
    ('driver', 1): ('driver', 'driverHealth'),
    ('gunner', 1): ('gunner_1', 'gunner1Health'),
    ('gunner', 2): ('gunner_2', 'gunner2Health'),
    ('loader', 1): ('loader_1', 'loader1Health'),
    ('loader', 2): ('loader_2', 'loader2Health'),
}


@dataclass(frozen=True)
class CrewSpec:
    """One raw crew person; roles never create additional people.

    The index is the immutable ``CrewMember`` identity from vehicle XML.  This
    stores hit metadata only.  A primary radioman has no matching common
    Health material in the pinned catalog, so both binding fields remain
    ``None`` and the person is not damageable through this metadata.  The
    class defines no crew penalty, HP, recovery clock, fire behavior, or RNG.
    ``material`` and ``continue_trace_if_no_hit`` are optional for
    backward-compatible construction; the metadata factory supplies the
    pinned common material name and its (possibly locally overridden)
    continueTraceIfNoHit value, ``None`` when XML declares none.
    """
    index: int
    holder: str
    roles: tuple[str, ...]
    projectile_chance: float | None
    extra_health_index: int | None
    material: str | None = None
    continue_trace_if_no_hit: bool | None = None

    def __post_init__(self):
        if type(self.index) is not int or self.index < 0:
            raise ValueError('Crew index must be a nonnegative integer')
        if not isinstance(self.holder, str) or self.holder not in _CREW_ROLES:
            raise ValueError('Crew holder is not recognized')
        if (not isinstance(self.roles, tuple) or not self.roles or any(
                not isinstance(role, str) or role not in _CREW_ROLES for role in self.roles)
                or len(set(self.roles)) != len(self.roles) or self.holder not in self.roles):
            raise ValueError('Crew roles are invalid')
        if (self.projectile_chance is None) != (self.extra_health_index is None):
            raise ValueError('Crew hit metadata must be both bound or absent')
        if self.projectile_chance is None and self.holder != 'radioman':
            raise ValueError('Only a primary radioman may lack crew hit metadata')
        if self.projectile_chance is not None:
            chance = _number(self.projectile_chance, 'projectile_chance')
            if chance > 1:
                raise ValueError('Crew projectile chance must not exceed 1')
            object.__setattr__(self, 'projectile_chance', chance)
        if self.extra_health_index is not None and (
                type(self.extra_health_index) is not int or not 0 <= self.extra_health_index <= 0xff):
            raise ValueError('Crew extra health index must be a uint8')
        if self.material is not None and (self.projectile_chance is None or self.material not in {
                material for material, _ in _CREW_MATERIALS.values()}):
            raise ValueError('Crew material must be a pinned bound crew material')
        if self.continue_trace_if_no_hit is not None and type(self.continue_trace_if_no_hit) is not bool:
            raise ValueError('continue_trace_if_no_hit must be a bool or None')

    @property
    def damageable(self):
        """Whether XML supplies both a projectile chance and Health extra."""
        return self.projectile_chance is not None


class CrewState:
    """Independent boolean injury for a metadata-bound CrewSpec, with no penalty."""
    __slots__ = ('_spec', '_injured')

    def __init__(self, spec):
        if not isinstance(spec, CrewSpec):
            raise ValueError('Expected CrewSpec')
        self._spec = spec
        self._injured = False

    @property
    def spec(self):
        return self._spec

    @property
    def injured(self):
        return self._injured

    def injure(self):
        """Mark a bound person injured; unbound or repeated calls are no-ops."""
        if not self._spec.damageable:
            return False
        if self._injured:
            return False
        self._injured = True
        return True

    def heal(self):
        """Clear injury only; no recovery clock or role effect is inferred."""
        if not self._injured:
            return False
        self._injured = False
        return True


def crew_specs_from_metadata(metadata):
    """Resolve one immutable CrewSpec per XML person.

    ``CrewMember.index`` supplies the person identity.  A combined gunner /
    loader remains one gunner-primary person and consumes only ``gunner_1``;
    no loader person is generated.  Primary radiomen have no Health material
    in the pinned common catalog and remain present as explicitly unbound
    people, rather than receiving invented hit metadata.
    """
    from .module_data import CrewMember, VehicleModuleMetadata

    if not isinstance(metadata, VehicleModuleMetadata):
        raise ValueError('Expected VehicleModuleMetadata')
    material_names = {material for material, _ in _CREW_MATERIALS.values()}
    common = {}
    for name in material_names:
        records = [item for item in metadata.common_materials if item.name == name]
        if len(records) != 1:
            raise ValueError('Missing or ambiguous common crew material metadata')
        common[name] = records[0]
    indexes, holder_counts, specs = set(), {}, []
    for member in metadata.crew:
        if not isinstance(member, CrewMember):
            raise ValueError('Invalid crew member metadata')
        if type(member.index) is not int or member.index < 0:
            raise ValueError('Invalid crew member index')
        if member.index in indexes:
            raise ValueError('Duplicate crew member index')
        indexes.add(member.index)
        if (not isinstance(member.holder, str) or member.holder not in _CREW_ROLES
                or not isinstance(member.roles, tuple) or not member.roles
                or any(not isinstance(role, str) or role not in _CREW_ROLES for role in member.roles)
                or len(set(member.roles)) != len(member.roles) or member.holder not in member.roles):
            raise ValueError('Unrecognized or malformed primary crew holder')
        if member.trailing_text is not None:
            raise ValueError('Unsupported trailing crew role text')
        holder_counts[member.holder] = holder_counts.get(member.holder, 0) + 1
        mapping = _CREW_MATERIALS.get((member.holder, holder_counts[member.holder]))
        if mapping is None:
            if member.holder == 'radioman':
                specs.append(CrewSpec(member.index, member.holder, member.roles, None, None))
                continue
            raise ValueError('Unrecognized primary crew holder')
        material_name, extra_name = mapping
        material = common[material_name]
        if material.extra.value != extra_name or material.chance_to_hit_by_projectile.value is None:
            raise ValueError('Invalid common crew material metadata')
        extra_records = [item for item in metadata.extras if item.name == extra_name]
        if len(extra_records) != 1:
            raise ValueError('Missing or ambiguous crew health XML extra')
        extra = extra_records[0]
        local = [item for item in metadata.local_materials if item.name == material_name]
        if len(local) > 1:
            raise ValueError('Ambiguous local crew material override')
        chance = material.chance_to_hit_by_projectile.value
        continue_trace = material.continue_trace_if_no_hit.value
        if local:
            override = local[0]
            if override.extra.value not in (None, extra_name):
                raise ValueError('Invalid local crew material override')
            if override.chance_to_hit_by_projectile.value is not None:
                chance = override.chance_to_hit_by_projectile.value
            if override.continue_trace_if_no_hit.value is not None:
                continue_trace = override.continue_trace_if_no_hit.value
        specs.append(CrewSpec(member.index, member.holder, member.roles, chance, extra.index,
                              material_name, continue_trace))
    if not specs:
        raise ValueError('No damageable crew members in metadata')
    return tuple(specs)


def crew_bindings_for_meshes(specs, meshes, supported_extras):
    """Explicit ``(mesh, person index)`` pairs for imported crew volumes.

    A legacy ``crew_<role>`` mesh binds only when exactly one person has that role
    as PRIMARY holder, the person is damageable and its Health extra is in
    ``supported_extras``.  Secondary roles never bind: a combined
    gunner/loader is reachable only through ``crew_gunner``.  Absent or
    ambiguous holders and unsupported extras are left unbound, so the mesh
    keeps blocking the internal trace as an unimplemented volume.

    Explicit ``crew_gunner_1/2`` and ``crew_loader_1/2`` identify the first or
    second PRIMARY holder in XML person-index order. Secondary roles never
    consume an occurrence. A missing occurrence stays unbound; aliases naming
    the same person twice are rejected before attaching any crew state.
    """
    if not isinstance(specs, tuple) or any(not isinstance(spec, CrewSpec) for spec in specs):
        raise ValueError('Expected a tuple of CrewSpec')
    if len({spec.index for spec in specs}) != len(specs):
        raise ValueError('Crew specs require unique person indices')
    supported = frozenset(supported_extras)
    bindings, seen, people = [], set(), set()
    for mesh in meshes:
        if not isinstance(mesh, str) or not mesh.startswith('crew_') or mesh in seen:
            raise ValueError('Invalid or repeated crew mesh name')
        seen.add(mesh)
        role = mesh[5:]
        primary, separator, number = role.rpartition('_')
        numbered = bool(separator and primary in ('gunner', 'loader') and number in ('1', '2'))
        holders = sorted((spec for spec in specs if spec.holder == (primary if numbered else role)),
                         key=lambda spec: spec.index)
        person = None
        if numbered and len(holders) >= int(number):
            person = holders[int(number)-1]
        elif not numbered and len(holders) == 1:
            person = holders[0]
        if (person is not None and numbered
                and person.material != _CREW_MATERIALS[(primary, int(number))][0]):
            person = None  # Inconsistent person order/material identity cannot be guessed.
        if (person is not None and person.damageable and person.material is not None
                and person.extra_health_index in supported):
            if person.index in people:
                raise ValueError('Multiple crew meshes name the same person')
            people.add(person.index)
            bindings.append((mesh, person.index))
    return tuple(bindings)
