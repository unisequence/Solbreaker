"""Immutable stock definitions from pinned XML, without battle or wire state.

Single turret, AP, simple clips and pump autoreloaders only. Other layouts
fail explicitly.
Loading a definition is not a claim of native gameplay support.
"""
from copy import deepcopy
from dataclasses import dataclass, fields
from functools import lru_cache
import math
from pathlib import Path
import re
import struct
import xml.etree.ElementTree as ET

import yaml

from .dvpl import read
from .modules import GunSpec, ModuleProtection, TrackSpec

_ASSET_PATH = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9_. -]*(?:/[A-Za-z0-9_-][A-Za-z0-9_. -]*)+\.sc2')


def _asset_base(root, mapping, key, prefix):
    value = mapping.get(key)
    if not (isinstance(value, str) and _ASSET_PATH.fullmatch(value) and value.startswith(prefix+'/')
            and '..' not in value.split('/')):
        raise ValueError(f'Invalid vehicle resourcesPath.{key}')
    return root/value[:-len('.sc2')]


@dataclass(frozen=True)
class VehicleConfiguration:
    """XML tags selected within a vehicle's declared compatible slots."""
    chassis: str | None = None
    turret: str | None = None
    gun: str | None = None
    engine: str | None = None
    fuel_tank: str | None = None
    radio: str | None = None
    shell: str | None = None

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self,field.name)
            if value is not None and (not isinstance(value,str) or value in ('.','..')
                                      or not re.fullmatch(r'[A-Za-z0-9_.-]+',value)):
                raise ValueError(f'Invalid vehicle configuration: {field.name}')

    @classmethod
    def parse(cls, value):
        if value is None:
            return cls()
        if not isinstance(value,dict) or set(value)-{f.name for f in fields(cls)}:
            raise ValueError('Invalid vehicle configuration fields')
        return cls(**value)


@dataclass(frozen=True)
class Shell:
    compact_id: int
    caliber: float
    damage: float
    penetration: tuple
    normalization: float | None
    ricochet: float | None
    speed: float
    gravity: float
    max_distance: float
    kind: str = 'ARMOR_PIERCING'
    tag: str = ''
    penetration_loss_by_distance: float | None = None
    explosion_radius: float | None = None
    effect_index: int = 0
    damage_devices: float = 0.0
    # Uniform HP damage roll bounds, drawn at impact. Tungsten Rounds replaces
    # them on the shells it fires; nominal damage/devices stay unchanged.
    damage_factors: tuple = (.75, 1.25)

    def __post_init__(self):
        factors = self.damage_factors
        if (type(factors) is not tuple or len(factors)!=2
                or any(isinstance(f,bool) or not isinstance(f,(int,float)) or not math.isfinite(f) for f in factors)
                or not 0<factors[0]<=factors[1]):
            raise ValueError('Invalid shell damage factors')

    def power(self, distance):
        if self.kind=='HIGH_EXPLOSIVE':
            return self.penetration[0]
        fraction = min(1., max(0., (distance-100.)/400.))
        return self.penetration[0]*(1-fraction)+self.penetration[1]*fraction


@dataclass(frozen=True)
class ArmorMaterial:
    name: str
    thickness: float
    damage_factor: float
    # common/vehicle.xml materials/<name> useArmorHomogenization, then a local
    # child of the same material; None when neither declares it (unknown).
    use_armor_homogenization: bool | None = None


@dataclass(frozen=True)
class Component:
    name: str
    ident: int
    mass: float
    health: int
    geometry: str | None
    armor: tuple[ArmorMaterial, ...]

    def material(self, name):
        return next((a for a in self.armor if a.name == name), None)


@dataclass(frozen=True)
class ArmorStateCondition:
    """Static health threshold declared by an armorsStatesController state.

    ``health_percent`` is the XML percentage, not a current vehicle-health
    calculation.  The caller that eventually owns the controller transition
    must establish its exact boundary and wire timing against the client.
    """
    operation: str
    health_percent: float

    def __post_init__(self):
        if self.operation not in ('less', 'more'):
            raise ValueError('Invalid movable armor health operation')
        value = self.health_percent
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= 100):
            raise ValueError('Invalid movable armor health threshold')


@dataclass(frozen=True)
class ArmorState:
    """One static movable-armor state, without collision interpretation."""
    ident: int
    armors: tuple[str, ...]
    kind: str
    enabled: bool | None
    condition: ArmorStateCondition | None
    is_default: bool = False

    def __post_init__(self):
        if type(self.ident) is not int or not 0 <= self.ident < 256:
            raise ValueError('Invalid movable armor state ID')
        if (type(self.armors) is not tuple or not self.armors
                or len(set(self.armors)) != len(self.armors)
                or any(not _ARMOR_STATE_REFERENCE.fullmatch(value) for value in self.armors)):
            raise ValueError('Invalid movable armor references')
        if self.kind not in ('default', 'health'):
            raise ValueError('Invalid movable armor state type')
        if type(self.is_default) is not bool:
            raise ValueError('Invalid movable armor default marker')
        if self.kind == 'default':
            if type(self.enabled) is not bool or self.condition is not None:
                raise ValueError('Invalid default movable armor state')
        elif self.enabled is not None or not isinstance(self.condition, ArmorStateCondition):
            raise ValueError('Invalid health movable armor state')


@dataclass(frozen=True)
class ArmorStatesController:
    """Parsed XML/YAML movable-armor declaration.

    This is deliberately data only.  It does not inspect SC2 collision nodes,
    decide active geometry, evaluate health, or make a vehicle battle-ready.
    ``load_vehicle`` retains its explicit movable-armor refusal until those
    separate collision and network contracts are recovered.
    """
    states: tuple[ArmorState, ...]

    def __post_init__(self):
        if (type(self.states) is not tuple or len(self.states) < 2
                or any(not isinstance(state, ArmorState) for state in self.states)
                or len({state.ident for state in self.states}) != len(self.states)
                or sum(state.is_default for state in self.states) != 1):
            raise ValueError('Invalid movable armor controller')

    @property
    def default_state(self):
        return next(state for state in self.states if state.is_default)


_ARMOR_STATE_REFERENCE = re.compile(r'[A-Za-z0-9_]+\.armor_[1-9]\d*')


@dataclass(frozen=True)
class Drive:
    forward: float
    reverse: float
    rotation: float
    max_slope: float
    support: tuple
    terrain_resistance: tuple
    brake_force: float
    engine_power_hp: float
    power_bonus_factor: float = 1.0  # fuel metadata; runtime acceleration is wired by the arena


@dataclass(frozen=True)
class Aiming:
    turret_position: tuple
    gun_position: tuple
    pitch_limits: tuple
    yaw_rate: float
    pitch_rate: float
    aiming_time: float
    rear_limits: tuple | None
    yaw_limits: tuple = (-math.pi, math.pi)
    # extraPitchLimits/front (low,high,arc) radians and sector-to-base blend.
    # Native ctor 0x86326d defaults the blend to .4 rad; XML gives degrees.
    front_limits: tuple | None = None
    pitch_transition: float = .4
    # Native gun+16c/+170: envelope before equipment, then its own pair deltas.
    packed_limits: tuple | None = None

    @property
    def packed_pitch_limits(self):
        if self.packed_limits is not None:
            return self.packed_limits
        sectors = [self.pitch_limits] + [v[:2] for v in (self.front_limits,self.rear_limits) if v is not None]
        return min(v[0] for v in sectors),max(v[1] for v in sectors)


@dataclass(frozen=True)
class Weapon:
    ammo: int
    clip: int
    interval: float
    reload: float
    shells: tuple[Shell, ...]
    ammunition: tuple = ()
    # Pump autoreloader one-shell refill seconds in raw XML order, never
    # sorted: index = shells already loaded. Empty for every other gun.
    pump_reload_times: tuple = ()
    # Burst group: scheduled partners follow burst_interval after each prior
    # shell; interval is the separate group-to-group clip cooldown. burst_count
    # 1 (interval 0) is every ordinary gun.
    burst_count: int = 1
    burst_interval: float = 0.

    def __post_init__(self):
        if (type(self.burst_count) is not int or self.burst_count < 1
                or isinstance(self.burst_interval,bool) or not isinstance(self.burst_interval,(int,float))
                or not math.isfinite(self.burst_interval)
                or (self.burst_count > 1) != (self.burst_interval > 0)
                or self.burst_count > 1 and (self.pump_reload_times or type(self.clip) is not int
                                              or self.clip < self.burst_count)):
            raise ValueError('Invalid burst definition')
        times = self.pump_reload_times
        if type(times) is not tuple or (times and (
                type(self.clip) is not int or self.clip < 2 or len(times) != self.clip
                or any(isinstance(t,bool) or not isinstance(t,(int,float)) or not math.isfinite(t) or t <= 0
                       for t in times))):
            raise ValueError('Invalid pump reload times')

    @property
    def pump(self):
        return bool(self.pump_reload_times)

    @property
    def burst(self):
        return self.burst_count > 1


@dataclass(frozen=True)
class VehicleDefinition:
    nation: str
    nation_id: int
    name: str
    ident: int
    hull: Component
    chassis: Component
    turret: Component
    gun: Component
    engine: Component
    fuel: Component
    radio: Component
    hull_position: tuple
    drive: Drive
    aiming: Aiming
    weapon: Weapon
    collision_asset: Path
    visual_asset: Path
    configuration: VehicleConfiguration
    health_bonus: int = 0
    optional_devices: tuple[int, ...] = ()
    provisions: tuple[int, ...] = ()  # server metadata; no public wire field established
    crew_mastery: int | None = None  # None marks raw XML values, before battle calibration
    track_spec: TrackSpec | None = None  # installed chassis metadata; None if any field is absent
    # Selected gun HP and merged materialKinds chance; None without maxRegenHealth
    # or collision geometry. Gun armor/gun thickness is not a triangle thickness.
    gun_spec: GunSpec | None = None
    # Spall liner (provision101): final HE HP damage multiplier and its visual FX
    # seconds. Passive; the duration never limits the protection.
    he_damage_factor: float = 1.0
    he_effect_duration: float = 0.0
    # Raw108/109; HP-scaled tracks are held in track_spec, while the arena
    # derives independent internal-module specs from these chance factors.
    module_protection: ModuleProtection = ModuleProtection()
    # Present only on the battle-only, geometry-validated movable-armor load
    # path.  Public ``load_vehicle`` deliberately continues to refuse these
    # vehicles so callers cannot treat parsed XML alone as collision support.
    armor_states_controller: ArmorStatesController | None = None
    tier: int = 1  # Actual vehicle level from the national catalog, not component level.

    def __post_init__(self):
        if type(self.tier) is not int or not 1 <= self.tier <= 10:
            raise ValueError('Invalid vehicle tier')
        if not isinstance(self.module_protection, ModuleProtection):
            raise ValueError('Invalid module protection')
        if (self.armor_states_controller is not None
                and not isinstance(self.armor_states_controller, ArmorStatesController)):
            raise ValueError('Invalid movable armor controller')
        factor, duration = self.he_damage_factor, self.he_effect_duration
        if (any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v)
                for v in (factor,duration))
                or not 0<factor<=1 or duration<0 or (factor==1)!=(duration==0)):
            raise ValueError('Invalid HE damage protection')

    @property
    def descriptor(self):
        return struct.pack('<2B6HB', (self.nation_id << 4) | 1, self.ident,
            self.chassis.ident, self.engine.ident, self.fuel.ident, self.radio.ident,
            self.turret.ident, self.gun.ident, 0)

    @property
    def health(self):
        return self.hull.health + self.turret.health + self.health_bonus

    @property
    def mass(self):
        return sum(c.mass for c in (self.hull,self.chassis,self.turret,self.gun,
                                    self.engine,self.fuel,self.radio))

    @property
    def inventory(self):
        default = self.weapon.shells[0].compact_id
        ids = tuple(s.compact_id for s in self.weapon.ammunition) or (default,)
        return {'descriptor':self.descriptor.hex(),
                'shells':initial_ammunition(ids,self.weapon.ammo,default,
                    tuple(s.default_portion for s in self.weapon.ammunition))}


def initial_ammunition(shell_ids, capacity, default_shell, portions=()):
    """Fresh inventory from XML defaultPortion, in unchanged gun XML order.

    Local rounding policy, not recovered native behavior: floor the weighted
    counts, then assign the remaining shells by largest fractional remainder,
    breaking ties in XML order. Zero weights remain zero. When every portion
    is absent (legacy definitions), retain the
    existing one-default loadout; partial or invalid vectors fail explicitly.
    This helper never mutates a saved loadout or purchases ammunition.
    """
    ids, portions = tuple(shell_ids), tuple(portions)
    if (type(capacity) is not int or not 0 <= capacity < 65536 or not ids
            or any(type(cd) is not int or not 0 <= cd <= 2**31-1 for cd in ids)
            or len(set(ids)) != len(ids) or type(default_shell) is not int or default_shell not in ids):
        raise ValueError('Invalid initial ammunition layout')
    if portions and len(portions) != len(ids):
        raise ValueError('Invalid ammunition defaultPortion count')
    if not portions or all(value is None for value in portions):
        return [v for cd in ids for v in (cd,capacity if cd==default_shell else 0)]
    if any(type(value) not in (int,float) or value < 0 or value > 1 or not math.isfinite(value)
           for value in portions):
        raise ValueError('Invalid ammunition defaultPortion')
    total = sum(portions)
    if not math.isclose(total,1.,rel_tol=0.,abs_tol=1e-6):
        raise ValueError('Ammunition defaultPortion must sum to one')
    # Normalize only floating-point representation error within the tolerance.
    weighted = [capacity*value/total for value in portions]
    counts = [math.floor(value) for value in weighted]
    order = sorted((i for i in range(len(ids)) if portions[i]>0),
                   key=lambda i:(-(weighted[i]-counts[i]),i))
    remaining = capacity-sum(counts)
    if not 0 <= remaining <= len(order):
        raise ValueError('Initial ammunition rounding failed to conserve capacity')
    for index in order[:remaining]:
        counts[index] += 1
    return [v for cd,count in zip(ids,counts) for v in (cd,count)]


def overlay(shared, local):
    """Resolve shared components recursively; local leaves take precedence."""
    result = deepcopy(shared) if shared is not None else ET.Element(local.tag)
    result.tag = local.tag
    if local.text and (local.text.strip() != 'shared' or shared is None):
        result.text = local.text
    for child in local:
        previous = result.find(child.tag)
        merged = overlay(previous,child)
        if previous is not None:
            result.remove(previous)
        result.append(merged)
    return result


def resolve_gun(shared, local):
    """Resolve shared gun shots using the pinned native exact-name rule.

    OnReadGunLocals (11.21 VA 0x7fa770) clones the base gun and retains its
    shot order, flight values and defaultPortion. Local shots replace only a
    matching shot's shell; unmatched names do not add shots. Other gun fields
    still take their local overrides. Both input trees remain unchanged.
    """
    result = overlay(shared, local)
    if shared is None:
        return result
    previous = result.find('shots')
    if previous is not None:
        result.remove(previous)
    base_shots = shared.find('shots')
    if base_shots is None:
        return result
    shots = deepcopy(base_shots)
    by_name = {shot.tag: shot for shot in shots}
    local_shots = local.find('shots')
    for local_shot in (() if local_shots is None else local_shots):
        shot = by_name.get(local_shot.tag)
        if shot is None:
            continue
        # A local Shot initially references its named global Shell. A nested
        # shell supplies that pointer's local overrides, rather than patching
        # flight/portion fields or keeping an old base-shell override.
        shell = shot.find('shell')
        if shell is not None:
            shot.remove(shell)
        shell = local_shot.find('shell')
        if shell is not None:
            shot.append(deepcopy(shell))
    result.append(shots)
    return result


def numbers(node, path, count, *, radians=False):
    value = node.findtext(path)
    if value is None:
        raise ValueError(f'Missing vehicle field: {node.tag}/{path}')
    values = tuple(map(float,value.split()))
    if len(values) != count or not all(math.isfinite(v) for v in values):
        raise ValueError(f'Invalid vehicle field: {node.tag}/{path}')
    return tuple(map(math.radians,values)) if radians else values


def _movable_armor_text(parent, tag, source):
    nodes = parent.findall(tag)
    if len(nodes) != 1 or nodes[0].attrib or len(nodes[0]):
        raise ValueError(f'Invalid movable armor field: {source}/{tag}')
    text = (nodes[0].text or '').strip()
    if not text:
        raise ValueError(f'Invalid movable armor field: {source}/{tag}')
    return text


def _empty_movable_armor(node, source):
    if node.attrib or len(node) or (node.text or '').strip():
        raise ValueError(f'Invalid movable armor field: {source}')


def _parse_armor_states_controller(controller, source):
    """Parse the native declaration, deliberately without geometry checks."""
    if controller.attrib or (controller.text or '').strip():
        raise ValueError(f'Invalid movable armor controller: {source}')
    if any((child.tail or '').strip() for child in controller):
        raise ValueError(f'Invalid movable armor controller: {source}')
    states = []
    for index, node in enumerate(controller):
        state_source = f'{source}/state[{index}]'
        if node.tag != 'state' or node.attrib or (node.text or '').strip():
            raise ValueError(f'Invalid movable armor state: {state_source}')
        if any((child.tail or '').strip() for child in node):
            raise ValueError(f'Invalid movable armor state: {state_source}')
        tags = [child.tag for child in node]
        if len(tags) != len(set(tags)):
            raise ValueError(f'Duplicate movable armor state field: {state_source}')
        kind = _movable_armor_text(node, 'type', state_source)
        required = ({'id', 'armors', 'type', 'enabled', 'factorsModifiers'}
                    if kind == 'default' else
                    {'id', 'armors', 'type', 'activateCondition', 'factorsModifiers'})
        if not required <= set(tags) or set(tags) - required - {'default'}:
            raise ValueError(f'Invalid movable armor state schema: {state_source}')
        ident_text = _movable_armor_text(node, 'id', state_source)
        if not re.fullmatch(r'[0-9]+', ident_text):
            raise ValueError(f'Invalid movable armor state ID: {state_source}')
        armors = tuple(_movable_armor_text(node, 'armors', state_source).split())
        factors = node.find('factorsModifiers')
        _empty_movable_armor(factors, f'{state_source}/factorsModifiers')
        if kind == 'default':
            enabled = _movable_armor_text(node, 'enabled', state_source).lower()
            if enabled not in ('true', 'false'):
                raise ValueError(f'Invalid movable armor enabled flag: {state_source}')
            state = ArmorState(int(ident_text), armors, kind, enabled == 'true', None,
                               'default' in tags)
        elif kind == 'health':
            activate = node.find('activateCondition')
            if (activate.attrib or (activate.text or '').strip()
                    or len(activate) != 1 or activate[0].tag != 'healthActivateCondition'):
                raise ValueError(f'Invalid movable armor health condition: {state_source}')
            condition = activate[0]
            if set(condition.attrib) != {'op'} or len(condition) or (condition.tail or '').strip():
                raise ValueError(f'Invalid movable armor health condition: {state_source}')
            value = (condition.text or '').strip()
            try:
                threshold = float(value)
            except ValueError as error:
                raise ValueError(f'Invalid movable armor health condition: {state_source}') from error
            state = ArmorState(int(ident_text), armors, kind, None,
                               ArmorStateCondition(condition.attrib['op'], threshold), 'default' in tags)
        else:
            raise ValueError(f'Unsupported movable armor state type: {kind}')
        marker = node.find('default')
        if marker is not None:
            _empty_movable_armor(marker, f'{state_source}/default')
        states.append(state)
    return ArmorStatesController(tuple(states))


def load_armor_states(game: Path, nation='usa', name='M2_lt'):
    """Read an independent static movable-armor declaration, if present.

    XML and the corresponding Parameters YAML must agree that a vehicle has
    the ``MovableArmor`` mechanic.  The resulting object remains unsuitable
    for collision use by design; no SC2/SCG or state-node validation happens
    here.
    """
    if not all(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]+', value)
                   for value in (nation, name)):
        raise ValueError('Invalid vehicle resource name')
    return _load_armor_states(Path(game), nation, name)


@lru_cache(maxsize=32)
def _load_armor_states(game: Path, nation, name):
    root = game/'Data/XML/item_defs/vehicles'
    nations = ET.fromstring(read(root/'common/nations.xml.dvpl'))
    if nations.find(nation) is None:
        raise ValueError('Unknown nation')
    if ET.fromstring(read(root/nation/'list.xml.dvpl')).find(name) is None:
        raise ValueError('Unknown vehicle')
    vehicle = ET.fromstring(read(root/nation/(name+'.xml.dvpl')))
    controllers = vehicle.findall('extras/armorsStatesController')
    if len(vehicle.findall('.//armorsStatesController')) != len(controllers):
        raise ValueError('Unsupported movable armor controller location')
    if len(controllers) > 1:
        raise ValueError('Duplicate movable armor controller')
    try:
        parameters = yaml.safe_load(read(game/f'Data/3d/Tanks/Parameters/{nation}/{name}.yaml.dvpl'))
    except yaml.YAMLError as error:
        raise ValueError('Malformed vehicle parameters') from error
    if not isinstance(parameters, dict):
        raise ValueError('Malformed vehicle parameters')
    mechanics = parameters.get('mechanics', [])
    if not isinstance(mechanics, list):
        raise ValueError('Invalid vehicle mechanics')
    movable = []
    for item in mechanics:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str):
            raise ValueError('Invalid vehicle mechanic')
        if item['name'].lower() == 'movablearmor':
            if item != {'name': 'MovableArmor'}:
                raise ValueError('Invalid MovableArmor mechanic')
            movable.append(item)
    if len(movable) > 1:
        raise ValueError('Duplicate MovableArmor mechanic')
    if bool(controllers) != bool(movable):
        raise ValueError('Movable armor XML/YAML mismatch')
    return None if not controllers else _parse_armor_states_controller(
        controllers[0], f'{name}/armorsStatesController')


load_armor_states.cache_clear = _load_armor_states.cache_clear


def load_vehicle(game: Path, nation='usa', name='M2_lt', configuration=None):
    if configuration is not None and not isinstance(configuration,VehicleConfiguration):
        raise ValueError('Expected VehicleConfiguration')
    return _load_vehicle(Path(game),nation,name,configuration or VehicleConfiguration(),False)


def load_crew(game: Path, nation='usa', name='M2_lt'):
    """Ordered crew holders and their roles, including declared combined roles."""
    if not all(isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9_-]+',v) for v in (nation,name)):
        raise ValueError('Invalid vehicle resource name')
    return _load_crew(Path(game),nation,name)


@lru_cache(maxsize=1024)
def _load_crew(game: Path, nation, name):
    root = game/'Data/XML/item_defs/vehicles'
    nations = ET.fromstring(read(root/'common/nations.xml.dvpl'))
    if nations.find(nation) is None:
        raise ValueError('Unknown nation')
    listing = ET.fromstring(read(root/nation/'list.xml.dvpl'))
    if listing.find(name) is None:
        raise ValueError('Unknown vehicle')
    vehicle = ET.fromstring(read(root/nation/(name+'.xml.dvpl')))
    crews = vehicle.findall('crew')
    if len(crews) != 1 or not len(crews[0]):
        raise ValueError(f'Missing or unsupported vehicle crew: {nation}/{name}')
    crew = crews[0]
    if crew.attrib or (crew.text or '').strip():
        raise ValueError(f'Unsupported vehicle crew layout: {nation}/{name}')
    known_roles = {'commander','gunner','driver','loader','radioman'}
    holders = []
    for member in crew:
        if member.tag not in known_roles or member.attrib or len(member) or (member.tail or '').strip():
            raise ValueError(f'Unsupported vehicle crew member: {nation}/{name}')
        roles = (member.tag, *(member.text or '').split())
        if any(role not in known_roles for role in roles) or len(set(roles)) != len(roles):
            raise ValueError(f'Invalid vehicle crew roles: {nation}/{name}/{member.tag}')
        holders.append((member.tag,roles))
    return tuple(holders)


@lru_cache(maxsize=4)
def _shot_effects(game):
    root = ET.fromstring(read(Path(game)/'Data/XML/item_defs/vehicles/common/shot_effects.xml.dvpl'))
    effects = tuple((node.tag,node.findtext('tracerType')) for node in root)
    if len(effects)>256 or len({name for name,_ in effects})!=len(effects):
        raise ValueError('Invalid native shot-effect catalog')
    return effects


def _shell_effect(game, name):
    for index,(tag,tracer) in enumerate(_shot_effects(game)):
        if tag==name:
            # Native NativeShotEffectDict 0x7f3bb8 zeroes type+8 (enum 0x427705
            # shell=0); parser 0x7f2320 only overrides an explicit tracerType.
            # A projectile-only record (dangerousTracer) is therefore a shell.
            if tracer is not None and tracer!='shell':
                raise ValueError(f'Unsupported projectile tracer: {tracer}')
            return index
    raise ValueError(f'Unknown shell effect: {name}')


@lru_cache(maxsize=4)
def _gun_chance(game):
    """common/vehicle.xml materials/gun chanceToHitByProjectile (not gunBreech)."""
    root = ET.fromstring(read(Path(game)/'Data/XML/item_defs/vehicles/common/vehicle.xml.dvpl'))
    kind = root.find('materials/gun')
    if kind is None or kind.find('chanceToHitByProjectile') is None:
        raise ValueError('Missing common gun material chanceToHitByProjectile')
    return numbers(kind,'chanceToHitByProjectile',1)[0]


def _homogenization_flag(node, what):
    """Explicit useArmorHomogenization child of node: True/False, None if absent."""
    flags = node.findall('useArmorHomogenization')
    if not flags:
        return None
    if len(flags) != 1 or len(flags[0]) or flags[0].attrib:
        raise ValueError(f'Duplicate or nested useArmorHomogenization: {what}')
    text = (flags[0].text or '').strip().lower()
    if text not in ('true','false'):
        raise ValueError(f'Invalid useArmorHomogenization: {what}')
    return text == 'true'


@lru_cache(maxsize=4)
def _homogenization_defaults(game):
    """common/vehicle.xml materials -> declared flag; undeclared kinds are omitted.

    Pinned: armor_1..armor_16 and gunBreech true; gun, leftTrack, rightTrack false.
    The flag belongs to the material kind, not to the part that carries it.
    """
    root = ET.fromstring(read(Path(game)/'Data/XML/item_defs/vehicles/common/vehicle.xml.dvpl'))
    materials = root.findall('materials')
    if len(materials) > 1:
        raise ValueError('Duplicate common materials')
    kinds = tuple(materials[0]) if materials else ()
    if len({kind.tag for kind in kinds}) != len(kinds):
        raise ValueError('Duplicate common material kind')
    flags = ((kind.tag,_homogenization_flag(kind,'materials/'+kind.tag)) for kind in kinds)
    return tuple((name,flag) for name,flag in flags if flag is not None)


@lru_cache(maxsize=64)
def _load_vehicle(game: Path, nation, name, configuration, allow_movable=False):
    if type(allow_movable) is not bool:
        raise ValueError('Invalid movable armor load flag')
    if not all(isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9_-]+',v) for v in (nation,name)):
        raise ValueError('Invalid vehicle resource name')
    game = Path(game)
    root = game/'Data/XML/item_defs/vehicles'
    def xml(path):
        return ET.fromstring(read(path))
    national = xml(root/'common/nations.xml.dvpl').find(nation)
    if national is None:
        raise ValueError('Unknown nation')
    nation_id = int(national.findtext('id'))
    listing = xml(root/nation/'list.xml.dvpl').find(name)
    if listing is None:
        raise ValueError('Unknown vehicle')
    vehicle = xml(root/nation/(name+'.xml.dvpl'))
    if any(len(v) for v in vehicle if v.tag.startswith('turrets') and v.tag != 'turrets0'):
        raise ValueError('Multiple turrets are not supported')
    catalogs = {}
    def component(kind, local):
        if kind not in catalogs:
            catalogs[kind] = xml(root/nation/'components'/(kind+'.xml.dvpl'))
        catalog = catalogs[kind]
        shared = catalog.find('shared/'+local.tag) if (local.text or '').strip()=='shared' else None
        if (local.text or '').strip()=='shared' and shared is None:
            raise ValueError(f'Missing shared component: {kind}/{local.tag}')
        merged = resolve_gun(shared,local) if kind == 'guns' else overlay(shared,local)
        ident = catalog.findtext('ids/'+local.tag)
        if ident is None:
            raise ValueError(f'Missing component ID: {kind}/{local.tag}')
        return merged,int(ident)
    def select(node,path,slot):
        parent = node.find(path)
        if parent is None or not len(parent):
            raise ValueError(f'Missing vehicle component: {path}')
        tag = getattr(configuration,slot)
        if tag is None:
            return parent[0]
        selected = parent.find(tag)
        if selected is None:
            raise ValueError(f'Incompatible vehicle component: {slot}/{tag}')
        return selected
    chassis,ci = component('chassis',select(vehicle,'chassis','chassis'))
    turret,ti = component('turrets',select(vehicle,'turrets0','turret'))
    local_gun = select(turret,'guns','gun')
    gun,gi = component('guns',local_gun)
    engine,ei = component('engines',select(vehicle,'engines','engine'))
    fuel,fi = component('fuelTanks',select(vehicle,'fuelTanks','fuel_tank'))
    radio,ri = component('radios',select(vehicle,'radios','radio'))
    hull = vehicle.find('hull')
    if hull is None:
        raise ValueError('Missing hull')
    # Public loading remains fail-closed.  The private battle route may only
    # admit the one documented top-level controller after a separate SC2/SCG
    # completeness check; a controller carried by an installed component is
    # still unsupported even there.
    if not allow_movable:
        # Innermost first names the owner; the vehicle file is the final
        # catch-all.  Keep this branch byte-for-byte equivalent in behaviour
        # for public callers.
        for node in (gun,turret,chassis,engine,fuel,radio,vehicle):
            if node.find('.//armorsStatesController') is not None:
                raise ValueError(f'Movable armor is not implemented: {node.tag}/armorsStatesController')
    else:
        for node in (gun,turret,chassis,engine,fuel,radio):
            if node.find('.//armorsStatesController') is not None:
                raise ValueError(f'Movable armor is not implemented: {node.tag}/armorsStatesController')
        controllers = vehicle.findall('.//armorsStatesController')
        permitted = vehicle.findall('extras/armorsStatesController')
        if controllers and (len(controllers) != 1 or controllers != permitted):
            raise ValueError('Unsupported movable armor controller location')
    yaw_limits = numbers(turret,'yawLimits',2,radians=True)
    if not (-math.pi <= yaw_limits[0] <= 0 <= yaw_limits[1] <= math.pi):
        raise ValueError('Invalid turret traverse limits')
    burst = gun.find('burst')
    if gun.find('autoreload') is not None or burst is not None and (
            len(gun.findall('burst')) > 1 or len(burst.findall('count')) != 1
            or len(burst.findall('rate')) != 1 or len(burst) != 2):
        raise ValueError('Burst/autoreloader weapons are not implemented')
    # Pinned parser 0x868acf: bool pumpGunMode, vector pumpGunReloadTimes. The
    # mode and its list must agree; a half-declared pump layout is refused.
    if len(gun.findall('pumpGunMode')) > 1 or len(gun.findall('pumpGunReloadTimes')) > 1:
        raise ValueError('Duplicate pump autoreloader field')
    mode = gun.findtext('pumpGunMode')
    mode = 'false' if mode is None else mode.strip().lower()
    if mode not in ('true','1','false','0'):
        raise ValueError('Invalid pumpGunMode')
    pump = mode in ('true','1')
    if pump != (gun.find('pumpGunReloadTimes') is not None):
        raise ValueError('Inconsistent pump autoreloader definition')
    def scalar(node,path):
        return numbers(node,path,1)[0]
    defaults = dict(_homogenization_defaults(game))
    def homogenization(node,a):
        local = _homogenization_flag(a,f'{node.tag}/armor/{a.tag}')
        return defaults.get(a.tag) if local is None else local
    def describe(node,ident):
        materials = tuple(ArmorMaterial(a.tag,float((a.text or '').strip()),
                float(a.findtext('vehicleDamageFactor','1')),homogenization(node,a))
                for a in node.findall('armor/*')
                if re.fullmatch(r'armor_\d+|leftTrack|rightTrack',a.tag))
        model = node.findtext('hitTester/collisionModel')
        geometry = Path(model).stem.lower() if model else None
        return Component(node.tag,ident,scalar(node,'weight'),int(scalar(node,'maxHealth')),
                         geometry,materials)
    def device_damage(value):
        if not math.isfinite(value) or value<0:
            raise ValueError('Invalid shell module damage')
        return value
    shells = []
    shell_xml = xml(root/nation/'components/shells.xml.dvpl')
    local_shots = gun.find('shots')
    if local_shots is None:
        raise ValueError('Missing ammunition')
    if configuration.shell is not None:
        selected = local_shots.find(configuration.shell)
        if selected is None:
            raise ValueError(f'Incompatible vehicle ammunition: {configuration.shell}')
        # Explicit selection permits an AP sandbox loadout from a mixed XML
        # list. Unsupported shell kinds remain unselectable and fail below.
        local_shots = (selected,)
    for local_shot in local_shots:
        shot = gun.find('shots/'+local_shot.tag)
        source = shell_xml.find(local_shot.tag)
        if shot is None or source is None:
            raise ValueError('Incomplete shell definition')
        source = overlay(source,local_shot.find('shell')) if local_shot.find('shell') is not None else source
        kind = source.findtext('kind')
        if kind not in ('ARMOR_PIERCING','ARMOR_PIERCING_CR','HOLLOW_CHARGE','HIGH_EXPLOSIVE'):
            raise ValueError('Unsupported shell kind')
        shells.append(Shell((int(source.findtext('id'))<<8)|(nation_id<<4)|10,
            scalar(source,'caliber'),scalar(source,'damage/armor'),numbers(shot,'piercingPower',2),
            math.radians(scalar(source,'normalizationAngle')) if kind in ('ARMOR_PIERCING','ARMOR_PIERCING_CR') else None,
            math.radians(scalar(source,'ricochetAngle')) if source.find('ricochetAngle') is not None else None,
            scalar(shot,'speed'),scalar(shot,'gravity'),scalar(shot,'maxDistance'),
            kind,local_shot.tag,
            scalar(source,'piercingPowerLossFactorByDistance') if kind=='HOLLOW_CHARGE' else None,
            scalar(source,'explosionRadius') if kind=='HIGH_EXPLOSIVE' else None,
            _shell_effect(game,source.findtext('effects')),
            device_damage(scalar(source,'damage/devices'))))
    if not shells:
        raise ValueError('Missing ammunition')
    ammunition = load_ammunition(game,nation,name,configuration)
    for spec in ammunition:
        if (spec.kind in ('ARMOR_PIERCING','ARMOR_PIERCING_CR','HOLLOW_CHARGE','HIGH_EXPLOSIVE') and
                not any(s.compact_id==spec.compact_id for s in shells)):
            if spec.kind in ('ARMOR_PIERCING','ARMOR_PIERCING_CR') and (spec.normalization is None or spec.ricochet is None):
                raise ValueError('Missing kinetic shell angles')
            shells.append(Shell(spec.compact_id,spec.caliber,spec.damage_armor,spec.penetration,
                spec.normalization,spec.ricochet,spec.speed,spec.gravity,spec.max_distance,
                spec.kind,spec.tag,spec.penetration_loss_by_distance,spec.explosion_radius,
                _shell_effect(game,spec.effect_name),device_damage(spec.damage_devices)))
    if any(s.kind=='HOLLOW_CHARGE' and (s.penetration_loss_by_distance is None or
           not math.isfinite(s.penetration_loss_by_distance) or s.penetration_loss_by_distance<0)
           for s in shells):
        raise ValueError('Missing or invalid HEAT post-screen penetration loss')
    if any(s.kind=='HIGH_EXPLOSIVE' and (s.explosion_radius is None or
           not math.isfinite(s.explosion_radius) or s.explosion_radius<=0) for s in shells):
        raise ValueError('Missing or invalid HE explosion radius')
    hp = numbers(chassis,'hullPosition',3)
    tp = tuple(a+b for a,b in zip(hp,numbers(hull,'turretPositions/turret',3)))
    pitch = numbers(gun,'pitchLimits',2,radians=True)
    if pitch[0] > pitch[1]:
        raise ValueError('Invalid gun pitch limits')
    # Native parser 0x867ff4/0x868078: front/back are XML low,high,arc degrees;
    # 0x8680c5 replaces the .4 rad default blend with transition degrees.
    sectors,transition = {},.4
    extra = gun.find('extraPitchLimits')
    if extra is not None:
        tags = [n.tag for n in extra]
        if (any(t not in ('front','back','transition') for t in tags) or len(set(tags)) != len(tags)
                or extra.attrib or (extra.text or '').strip() or any(len(n) or n.attrib for n in extra)):
            raise ValueError('Unsupported additional gun pitch sector')
        for tag in ('front','back'):
            if tag in tags:
                low,high,arc = numbers(extra,tag,3,radians=True)
                if not (low <= high and 0 <= arc <= math.pi):
                    raise ValueError(f'Invalid gun pitch sector: {tag}')
                sectors[tag] = (low,high,arc)
        if 'transition' in tags:
            transition = math.radians(scalar(extra,'transition'))
            if transition < 0:
                raise ValueError('Invalid gun pitch transition')
    clip = int(gun.findtext('clip/count','1'))
    interval = 60/scalar(gun,'clip/rate') if clip > 1 else 0.
    # One time per magazine slot, kept in XML order (Lion 16,3.88,4.95). The
    # sum need not equal reloadTime, which stays the garage value only.
    pump_times = numbers(gun,'pumpGunReloadTimes',clip) if pump else ()
    if pump and (clip < 2 or any(t <= 0 for t in pump_times)):
        raise ValueError('Invalid pumpGunReloadTimes')
    burst_count,burst_interval = 1,0.
    if burst is not None:
        try:
            burst_count = int((burst.findtext('count') or '').strip())
        except ValueError as error:
            raise ValueError('Invalid burst count') from error
        # count=1 has no scheduled partner and remains the ordinary single-shot
        # path. A short final XML group is valid: the live scheduler only queues
        # shells that remain. Its own rate schedules partners; clip/rate is the
        # later group cooldown.
        rate = scalar(burst,'rate')
        if (burst_count < 1 or rate <= 0 or
                burst_count > 1 and (pump or clip < burst_count)):
            raise ValueError('Burst/autoreloader weapons are not implemented')
        burst_interval = 60/rate if burst_count > 1 else 0.
    track_fields = ('maxHealth','maxRegenHealth','hysteresisHealth','healthRegenPerSec')
    track_spec = (TrackSpec(*(scalar(chassis,f) for f in track_fields))
                  if all(chassis.findtext(f) is not None for f in track_fields) else None)
    gun_spec = None
    if gun.findtext('maxRegenHealth') is not None and gun.findtext('hitTester/collisionModel'):
        # Per-gun armor/gun chance (M2 20mm: 0) overrides the common gun kind (.33).
        local_kind = gun.find('armor/gun')
        chance = (scalar(local_kind,'chanceToHitByProjectile') if local_kind is not None
                  and local_kind.find('chanceToHitByProjectile') is not None else _gun_chance(game))
        gun_spec = GunSpec(scalar(gun,'maxHealth'),scalar(gun,'maxRegenHealth'),chance)
    try:
        parameters = yaml.safe_load(read(game/f'Data/3d/Tanks/Parameters/{nation}/{name}.yaml.dvpl'))
    except yaml.YAMLError as error:
        raise ValueError('Malformed vehicle parameters') from error
    mechanics = parameters.get('mechanics') if isinstance(parameters, dict) else None
    movable_mechanic = any(isinstance(m,dict) and str(m.get('name','')).lower()=='movablearmor'
                           for m in (mechanics if isinstance(mechanics,list) else ()))
    if movable_mechanic and not allow_movable:
        raise ValueError('Movable armor is not implemented: mechanics/MovableArmor')
    controller = None
    if allow_movable:
        # Re-read through the strict independent parser.  This establishes
        # that XML and Parameters YAML agree and preserves the controller on
        # the immutable definition for the arena/collision owner.
        controller = load_armor_states(game,nation,name)
        if controller is not None and not controllers:
            raise ValueError('Unsupported movable armor controller location')
    paths = parameters.get('resourcesPath') if isinstance(parameters, dict) else None
    if not isinstance(paths, dict):
        raise ValueError('Missing vehicle resourcesPath mapping')
    visual_asset = _asset_base(game/'Data/3d', paths, 'blitzModelPath', 'Tanks')
    collision_asset = _asset_base(game/'Data/3d/Tanks', paths, 'collisionMesh', 'CollisionMeshes')
    definition = VehicleDefinition(nation,nation_id,name,int(listing.findtext('id')),
        describe(hull,0),describe(chassis,ci),describe(turret,ti),describe(gun,gi),
        describe(engine,ei),describe(fuel,fi),describe(radio,ri),hp,
        Drive(scalar(vehicle,'speedLimits/forward')/3.6,scalar(vehicle,'speedLimits/backward')/3.6,
            math.radians(scalar(chassis,'rotationSpeed')),math.radians(scalar(chassis,'maxClimbAngle')),
            numbers(chassis,'topRightCarryingPoint',2),numbers(chassis,'terrainResistance',3),
            scalar(chassis,'brakeForce'),scalar(engine,'power')),
        Aiming(tp,numbers(turret,'gunPosition',3),pitch,
            math.radians(scalar(turret,'rotationSpeed')),math.radians(scalar(gun,'rotationSpeed')),
            scalar(gun,'aimingTime'),sectors.get('back'),yaw_limits,sectors.get('front'),transition),
        Weapon(int(scalar(gun,'maxAmmo')),clip,interval,scalar(gun,'reloadTime'),tuple(shells),ammunition,
               pump_times,burst_count,burst_interval),
        collision_asset,visual_asset,
        VehicleConfiguration(chassis.tag,turret.tag,gun.tag,engine.tag,fuel.tag,radio.tag,
                             next(iter(local_shots)).tag),
        track_spec=track_spec,gun_spec=gun_spec,armor_states_controller=controller,
        tier=int(listing.findtext('level')))
    for asset in (definition.collision_asset,definition.visual_asset):
        for suffix in ('.sc2.dvpl','.scg.dvpl'):
            if not Path(str(asset)+suffix).is_file():
                raise ValueError(f'Missing vehicle geometry: {asset.name}{suffix}')
    if not (0 <= nation_id < 16 and 0 <= definition.ident < 256 and
            0 < definition.health < 32768 and 0 < definition.weapon.ammo < 65536 and
            0 < clip < 256 and definition.weapon.reload > 0):
        raise ValueError('Vehicle values exceed the supported native format')
    return definition


load_vehicle.cache_clear = _load_vehicle.cache_clear


@dataclass(frozen=True)
class AmmunitionSpec:
    """Raw shell metadata for one compatible gun; angles in radians, no combat rules."""
    tag: str
    compact_id: int
    kind: str
    caliber: float
    damage_armor: float
    damage_devices: float
    penetration: tuple
    speed: float
    gravity: float
    max_distance: float
    normalization: float | None
    ricochet: float | None
    explosion_radius: float | None
    penetration_loss_by_distance: float | None
    price_credits: int
    price_gold: int
    effect_name: str | None = None
    default_portion: float | None = None  # resolved shared gun/shots weight


def load_ammunition(game: Path, nation='usa', name='M2_lt', configuration=None):
    if configuration is not None and not isinstance(configuration,VehicleConfiguration):
        raise ValueError('Expected VehicleConfiguration')
    return _load_ammunition(Path(game),nation,name,configuration or VehicleConfiguration())


@lru_cache(maxsize=32)
def _load_ammunition(game: Path, nation, name, configuration):
    if not all(isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9_-]+',v) for v in (nation,name)):
        raise ValueError('Invalid vehicle resource name')
    root = game/'Data/XML/item_defs/vehicles'
    def xml(path):
        return ET.fromstring(read(path))
    national = xml(root/'common/nations.xml.dvpl').find(nation)
    if national is None:
        raise ValueError('Unknown nation')
    nation_id = int(national.findtext('id'))
    if xml(root/nation/'list.xml.dvpl').find(name) is None:
        raise ValueError('Unknown vehicle')
    vehicle = xml(root/nation/(name+'.xml.dvpl'))
    def select(node,path,slot):
        parent = node.find(path)
        if parent is None or not len(parent):
            raise ValueError(f'Missing vehicle component: {path}')
        tag = getattr(configuration,slot)
        selected = parent[0] if tag is None else parent.find(tag)
        if selected is None:
            raise ValueError(f'Incompatible vehicle component: {slot}/{tag}')
        return selected
    for path,slot in (('chassis','chassis'),('engines','engine'),
                      ('fuelTanks','fuel_tank'),('radios','radio')):
        select(vehicle,path,slot)
    local_turret = select(vehicle,'turrets0','turret')
    turrets = xml(root/nation/'components/turrets.xml.dvpl')
    shared_turret = turrets.find('shared/'+local_turret.tag) if (local_turret.text or '').strip()=='shared' else None
    if (local_turret.text or '').strip()=='shared' and shared_turret is None:
        raise ValueError(f'Missing shared component: turrets/{local_turret.tag}')
    turret = overlay(shared_turret,local_turret)
    if turrets.findtext('ids/'+local_turret.tag) is None:
        raise ValueError(f'Missing component ID: turrets/{local_turret.tag}')
    local_gun = select(turret,'guns','gun')
    guns = xml(root/nation/'components/guns.xml.dvpl')
    shared = guns.find('shared/'+local_gun.tag) if (local_gun.text or '').strip()=='shared' else None
    if (local_gun.text or '').strip()=='shared' and shared is None:
        raise ValueError(f'Missing shared component: guns/{local_gun.tag}')
    gun = resolve_gun(shared,local_gun)
    local_shots = gun.find('shots')
    if local_shots is None or not len(local_shots):
        raise ValueError('Missing ammunition')
    if configuration.shell is not None and local_shots.find(configuration.shell) is None:
        raise ValueError(f'Incompatible vehicle ammunition: {configuration.shell}')
    shell_xml = xml(root/nation/'components/shells.xml.dvpl')
    def optional(node,path,convert=float):
        value = node.findtext(path)
        return None if value is None else convert(value)
    result = []
    for local_shot in local_shots:
        shot = gun.find('shots/'+local_shot.tag)
        source = shell_xml.find(local_shot.tag)
        if shot is None or source is None:
            raise ValueError('Incomplete shell definition')
        if local_shot.find('shell') is not None:
            source = overlay(source,local_shot.find('shell'))
        price = source.find('price')
        if price is None:
            raise ValueError('Missing shell price')
        text = (price.text or '').strip()
        if not re.fullmatch(r'\d+(\.0+)?',text):
            raise ValueError('Invalid shell price')
        amount = int(text.split('.')[0])
        gold = price.find('gold') is not None
        angle = lambda path: optional(source,path,lambda v:math.radians(float(v)))
        result.append(AmmunitionSpec(local_shot.tag,(int(source.findtext('id'))<<8)|(nation_id<<4)|10,
            source.findtext('kind'),float(source.findtext('caliber')),
            float(source.findtext('damage/armor')),float(source.findtext('damage/devices')),
            numbers(shot,'piercingPower',2),float(shot.findtext('speed')),
            float(shot.findtext('gravity')),float(shot.findtext('maxDistance')),
            angle('normalizationAngle'),angle('ricochetAngle'),
            optional(source,'explosionRadius'),optional(source,'piercingPowerLossFactorByDistance'),
            0 if gold else amount,amount if gold else 0,source.findtext('effects'),
            optional(shot,'defaultPortion')))
    return tuple(result)


load_ammunition.cache_clear = _load_ammunition.cache_clear
