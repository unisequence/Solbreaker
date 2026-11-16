"""Mad Games modifiers for the pinned Blitz build."""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import xml.etree.ElementTree as ET

import yaml

from .dvpl import read
from .consumables import ConsumableSpec, ConsumableState

MODE = 'mad_games'
CLIENT_GAME_MODE = 8
CLIENT_GAMEPLAY = b'arcade'
FUEL_ID = 24
HIGH_OCTANE = ConsumableSpec('afterburner_ability', FUEL_ID, (FUEL_ID << 8) | 0xfb,
                             15., 30., engine_factor=4., rotation_factor=.75)
DASH_ID = 21
ACCELERATOR = ConsumableSpec('dash', DASH_ID, (DASH_ID << 8) | 0xfb, 1.15, 15.)
CHAMELEON_ID = 25
CHAMELEON = ConsumableSpec('moonwalk', CHAMELEON_ID, (CHAMELEON_ID << 8) | 0xfb,
                           10., 45., engine_factor=4.)
CHAMELEON_SPEED_FACTOR = 1.4
DISCHARGE = ConsumableSpec('sacrifice', 22, (22 << 8) | 0xfb, 20., 35.)
DISCHARGE_HP_COST = .10
DISCHARGE_DAMAGE = 1.50
URANIUM_SHIELD = ConsumableSpec('shield', 23, (23 << 8) | 0xfb, 0., 15.)
SHIELD_THRESHOLD = .05
SHIELD_DAMAGE = .70
IRRADIATOR = ConsumableSpec('hunters_mark', 26, (26 << 8) | 0xfb, 10., 30.)
IRRADIATED_MODIFIER = 59
IRRADIATED_EFFECT = 4
IRRADIATED_DAMAGE = 1.60
COORDINATOR = 'coordinator'
SABOTEUR = 'saboteur'
SPOT_RETENTION = 10.
SABOTEUR_RETENTION_FACTOR = .015
OBSERVATION_WARNING_DELAY = 3.
SABOTEUR_WARNING_DELAY = .15
PROXIMITY_RANGE = 50.
COORDINATOR_MODIFIER = 52
COORDINATOR_RADIUS = 50.
COORDINATOR_BONUS = .20
COORDINATOR_LIMIT = 3
REFLECTOR = ConsumableSpec('reflectionAbility', 75, (75 << 8) | 0xfb, 12., 50.)
REFLECTOR_MODIFIER = 174
REFLECTED_DAMAGE = .35
TOXIC_STARE = 'toxic_stare'
TOXIC_STARE_MODIFIER = 175
REVENGE_DAMAGE = 1.20
REVENGE_DURATION = 30.
ACTIVE_SPECS = (HIGH_OCTANE, ACCELERATOR, CHAMELEON, DISCHARGE, IRRADIATOR, REFLECTOR)
DASH_SPEED = 50. / ACCELERATOR.duration
DASH_EXIT_SPEED = 15.
DASH_RAM_DAMAGE_FACTOR = .15
SPEED_FACTOR = 2.
TERRAIN_RESISTANCE_FACTOR = .4
HIGH_OCTANE_CHASSIS_HEALTH = 15.
RAM_OUTGOING = 1.65
RAM_INCOMING = .30
RAMMER = 'rammer'
REGENERATOR = 'armor_regenerator'
LIFESTEAL = .25
ALLY_HEAL = .60
HEALING_SHELLS = frozenset(('ARMOR_PIERCING', 'HIGH_EXPLOSIVE'))
# Raw IDs in the pinned common/provisions/madgames.xml.
PASSIVE_PROVISIONS = {
    REGENERATOR: 23,
    RAMMER: 24,
    COORDINATOR: 25,
    SABOTEUR: 26,
    TOXIC_STARE: 114,
}
REGENERATOR_EFFECT = (PASSIVE_PROVISIONS[REGENERATOR] << 8) | 0xfd


@dataclass(frozen=True)
class Irradiated:
    team: int
    start: float
    end: float


def irradiation_effects(mark):
    if mark is None:
        return ()
    # VehicleClientEffect.huntersMarkEffect, stable key on the marked vehicle.
    return ((IRRADIATED_EFFECT, IRRADIATED_EFFECT, mark.start, mark.end),)


@lru_cache(maxsize=4)
def _role_abilities(game):
    path = game/'Data/XML/item_defs/vehicles/common/combat_roles.yaml.dvpl'
    return {role: tuple(data['default_abilities']) for role, data in yaml.safe_load(read(path)).items()}


@lru_cache(maxsize=80)
def _vehicle_roles(game, nation, mode):
    path = game/'Data/XML/item_defs/vehicles'/nation/'list.xml.dvpl'
    return {item.tag: role.strip() for item in ET.fromstring(read(path))
            if (role := item.findtext(f'combatRole/{mode}'))}


def vehicle_abilities(game, definition, *, mode='arcade'):
    if game is None or definition is None:
        return ()
    path = Path(game)
    try:
        role = _vehicle_roles(path, definition.nation, mode).get(definition.name)
        return _role_abilities(path).get(role, ())
    except (OSError, ET.ParseError, yaml.YAMLError, ValueError):
        return ()


def install_ability(vehicle, vehicle_class, game=None, *, cooldown_limit=None):
    # Keep the first three loadout slots in their stock positions.
    slots = tuple(slot for slot in vehicle.consumables
                  if slot is None or (slot.spec not in ACTIVE_SPECS and slot.spec is not URANIUM_SHIELD))
    # The stock arcade catalog assigns roles even below the shared V–X
    # vehicle filter in the Mad Games consumables and provisions.
    tier = getattr(vehicle.definition, 'tier', None)
    if tier is not None and not 5 <= tier <= 10:
        vehicle.consumables = slots
        vehicle.mad_passive = None
        return
    light = vehicle_class == 'lightTank'
    abilities = vehicle_abilities(game, vehicle.definition)
    spec = next((item for item in ACTIVE_SPECS if item.tag in abilities),
                ACCELERATOR if light else HIGH_OCTANE)
    if abilities:
        vehicle.mad_passive = next((passive for tag, passive in (
            ('ramming_damage', RAMMER), ('lifesteal_heal', REGENERATOR),
            ('ally_buff', COORDINATOR), ('revengeAbility', TOXIC_STARE),
            ('stealth', SABOTEUR)) if tag in abilities), None)
    else:
        vehicle.mad_passive = REGENERATOR if light else RAMMER
    vehicle.consumables = slots + (None,) * max(0, 3-len(slots)) + (ConsumableState(spec, cooldown_limit=cooldown_limit),)
    if URANIUM_SHIELD.tag in abilities:
        vehicle.consumables += (ConsumableState(URANIUM_SHIELD, cooldown_limit=cooldown_limit),)


def concealed(vehicle, viewer_team):
    return viewer_team != vehicle.team and (getattr(vehicle, 'mad_cloaked', False)
                                            or getattr(vehicle, 'mad_unspotted', False))


def view_range(vehicle):
    radius = getattr(vehicle.definition, 'view_range', 250.)
    return radius * (1. + vehicle.mad_coordinator_allies * COORDINATOR_BONUS)


def irradiator_state(vehicle):
    return next((slot for slot in vehicle.consumables if slot and slot.spec is IRRADIATOR), None)


def active(vehicle, spec, now):
    return any(slot and slot.spec is spec and slot.uses and slot.activated_at <= now < slot.active_until
               for slot in vehicle.consumables)


def discharge_factor(vehicle, at):
    return DISCHARGE_DAMAGE if active(vehicle, DISCHARGE, at) else 1.


def track_damage(vehicle, damage, at):
    # Store track HP in base units across the temporary chassis HP bonus.
    # Hits still accumulate; entering or leaving the window does not repair a track.
    return damage / HIGH_OCTANE_CHASSIS_HEALTH if active(vehicle, HIGH_OCTANE, at) else damage


def charge_shield(vehicle, at):
    for slot in vehicle.consumables:
        if slot and slot.spec is URANIUM_SHIELD:
            slot.start_cooldown(at, slot.spec.cooldown)
