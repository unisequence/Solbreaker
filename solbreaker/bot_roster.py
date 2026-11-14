"""Bounded, deterministic tank selection for a local bot battle.

This runs while preparing an arena. It reads national list metadata rather
than constructing every garage vehicle, and never changes a player's loadout.
The returned definitions are the recipe: parent and owner must share it instead
of drawing two independent rosters.
"""
from dataclasses import dataclass
from functools import lru_cache
import math
from pathlib import Path
import random
import re
import xml.etree.ElementTree as ET

from .dvpl import read
from .roster import TEAMS, TEAM_SIZE, validate_teams
from .vehicle_data import VehicleDefinition, load_vehicle


BOT_NICKNAMES = (
    'IronBadger_bot', 'SilentWolf_bot', 'SteelFalcon_bot', 'RustyFox_bot',
    'RedComet_bot', 'NightOwl_bot', 'SilverLynx_bot', 'StormRider_bot',
    'StoneBear_bot', 'CopperHawk_bot', 'WinterRaven_bot', 'DesertFox_bot',
    'AmberTiger_bot', 'BlueViper_bot', 'DustyEagle_bot', 'FrostWolf_bot',
    'WildBison_bot', 'IronCedar_bot', 'GoldenJackal_bot', 'QuietPanther_bot',
    'ThunderBoar_bot', 'GreyBadger_bot', 'ScarletHawk_bot', 'SwiftOtter_bot',
    'NorthernFox_bot', 'BraveRaven_bot', 'WanderingOwl_bot', 'SnowLeopard_bot',
    'CopperComet_bot', 'RockyFalcon_bot', 'MidnightLynx_bot', 'SilverBison_bot',
)

MAX_ALTERNATE_TYPES = 4
MAX_LOAD_ATTEMPTS = 24
BOT_SCENE_FIELDS = ('internal_geometry', 'engine_spec', 'ammo_rack_spec',
                    'fuel_tank_spec', 'fire_policy', 'fire_min_damage',
                    'crew_specs', 'crew_bindings')
_CLASSES = ('heavyTank', 'mediumTank', 'AT-SPG', 'lightTank')
_EXCLUDED = frozenset(('secret', 'deprecated', 'testTank', 'noRandom',
                       'event_battles', 'fictional'))
_NAME = re.compile(r'[A-Za-z0-9_-]+')


@dataclass(frozen=True, slots=True)
class BotVehicle:
    team: int
    slot: int
    definition: VehicleDefinition
    vehicle_class: str | None


@dataclass(frozen=True, slots=True)
class _Candidate:
    nation: str
    name: str
    tier: int
    vehicle_class: str
    available: bool = True

    @property
    def key(self):
        return self.nation, self.name


@lru_cache(maxsize=4)
def _catalog(game):
    """Only the small national lists; no module, armor or scene loading."""
    root = game/'Data/XML/item_defs/vehicles'
    nations = ET.fromstring(read(root/'common/nations.xml.dvpl'))
    result, seen = [], set()
    for nation in nations:
        if not _NAME.fullmatch(nation.tag):
            continue
        listing = ET.fromstring(read(root/nation.tag/'list.xml.dvpl'))
        for item in listing:
            tags = frozenset((item.findtext('tags') or '').split())
            kinds = tags.intersection(_CLASSES)
            level = (item.findtext('level') or '').strip()
            key = nation.tag, item.tag
            if (not _NAME.fullmatch(item.tag) or len(kinds) != 1 or tags & _EXCLUDED
                    or not level.isdecimal() or not 1 <= int(level) <= 10):
                continue
            if key in seen:
                raise ValueError('Duplicate bot vehicle listing')
            seen.add(key)
            # Keep its class/tier for a human-owned collectible, while avoiding
            # hidden event-only stock choices such as the Mark I. The pinned
            # lists do not give all of those the noRandom/event_battles tags.
            available = (item.findtext('notInShop') or '').strip().lower() not in ('true', '1')
            result.append(_Candidate(*key, int(level), next(iter(kinds)), available))
    return tuple(sorted(result, key=lambda item: item.key))


@lru_cache(maxsize=64)
def _geometry_supported(game, definition):
    """Check the exact stock configuration before sharing a frozen recipe.

    ArmorModel also validates collision materials, which asset existence alone
    cannot establish. Only its verdict is retained, never meshes or a scene.
    The selected human vehicle does not pass through this NPC admission path.
    """
    from .armor import ArmorModel
    try:
        ArmorModel(game, definition)
    except (OSError, ValueError):
        return False
    return True


@lru_cache(maxsize=64)
def stock_vehicle(game, nation, name):
    # Public loading refuses unsupported mechanics and validates the matching
    # descriptor, ammunition and existence of its own visual/collision assets.
    # Cache failures as well, so an unsupported candidate is not retried in
    # every subsequent battle. No CollisionScene or Bullet world is cached here.
    try:
        definition = load_vehicle(game, nation, name)
    except (OSError, ValueError, ET.ParseError):
        return None
    values = (definition.drive.forward, definition.drive.reverse,
              definition.drive.rotation, definition.health, definition.weapon.reload)
    if (any(not math.isfinite(value) or value <= 0 for value in values)
            or not definition.weapon.shells or definition.armor_states_controller is not None):
        return None
    if not _geometry_supported(game, definition):
        return None
    return definition


def _dpm(definition):
    weapon = definition.weapon
    # A stock gun's first loaded shell is its normal ammunition. Clip and
    # burst timing stay the XML values; this is a selection estimate only.
    shells = max(1, weapon.clip)
    burst = max(1, weapon.burst_count)
    groups = (shells+burst-1)//burst
    reload = sum(weapon.pump_reload_times) if weapon.pump_reload_times else weapon.reload
    cycle = reload + (groups-1)*weapon.interval + (shells-groups)*weapon.burst_interval
    return weapon.shells[0].damage*shells*60/cycle


def _counterpart(player, definition):
    """A conservative stock alternative for a one-opponent battle."""
    player_values = player.health, _dpm(player), player.weapon.shells[0].penetration[0]
    candidate_values = definition.health, _dpm(definition), definition.weapon.shells[0].penetration[0]
    if any(not math.isfinite(value) or value <= 0 for value in (*player_values, *candidate_values)):
        return False
    health, firepower, penetration = (a/b for a, b in zip(candidate_values, player_values))
    return (.75 <= health <= 4/3 and .5 <= firepower <= 2.
            and .65 <= penetration <= 1.5)


def vehicle_class(game, definition):
    if game is None or definition is None:
        return None
    try:
        classes = _vehicle_classes(Path(game), definition.nation)
    except (OSError, ValueError, ET.ParseError):
        return None
    return classes.get(definition.name)


@lru_cache(maxsize=32)
def _vehicle_classes(game, nation):
    path = game/'Data/XML/item_defs/vehicles'/nation/'list.xml.dvpl'
    result = {}
    for item in ET.fromstring(read(path)):
        classes = set((item.findtext('tags') or '').split()).intersection(_CLASSES)
        if len(classes) == 1:
            result[item.tag], = classes
    return result


def select_bot_roster(game, player_definition, *, enemies, allies, seed):
    """Return enemies first (slots0..), followed by allies (slots1..).

    Equal-sized teams get an exact counterpart of the selected player and
    matching pairs of sampled stock tanks. Their aggregate tiers, classes,
    health and gun definitions therefore match, including the player's actual
    installed modules. This is composition balance, not a claim of equal skill.

    A lone enemy instead prefers a different, comparable tank of the same tier
    and class. Unknown/custom players and unsupported catalogs keep the old
    clone behavior. Unequal team sizes remain the user's requested handicap.
    """
    if (type(enemies) is not int or not 1 <= enemies <= TEAM_SIZE
            or type(allies) is not int or not 0 <= allies < TEAM_SIZE
            or type(seed) not in (int, str, bytes)):
        raise ValueError('Invalid bot roster request')
    game = Path(game)
    try:
        catalog = _catalog(game)
    except (OSError, ValueError, ET.ParseError):
        catalog = ()
    key = player_definition.nation, player_definition.name
    own = next((item for item in catalog if item.key == key
                and item.tier == player_definition.tier), None)
    own_class = own.vehicle_class if own is not None else None
    randomizer = random.Random(seed)
    loaded = {}
    attempts = 0

    def load(item):
        nonlocal attempts
        if item.key not in loaded and attempts < MAX_LOAD_ATTEMPTS:
            attempts += 1
            definition = stock_vehicle(game, *item.key)
            # A mismatched/stale list must not silently put another tier or
            # identity into the roster, even when a custom loader is supplied.
            loaded[item.key] = (definition if definition is not None
                and (definition.nation, definition.name) == item.key
                and definition.tier == item.tier else None)
        return loaded.get(item.key)

    def candidates(tier):
        rows = [item for item in catalog if item.tier == tier and item.key != key and item.available]
        randomizer.shuffle(rows)
        return rows

    lone = enemies == 1 and allies == 0
    alternatives = []
    if own is not None and lone:
        rows = candidates(own.tier)
        # Stable sort preserves the seeded ordering within each class group.
        rows.sort(key=lambda item: item.vehicle_class != own_class)
        for item in rows:
            definition = load(item)
            if definition is not None and _counterpart(player_definition, definition):
                alternatives.append((definition, item.vehicle_class))
                break
            if attempts >= MAX_LOAD_ATTEMPTS:
                break
    elif own is not None:
        wanted = min(MAX_ALTERNATE_TYPES, max(enemies-1, allies))
        rows = candidates(own.tier)
        # Rotate classes rather than taking the first four shuffled lights or
        # heavies. At low tiers absent classes simply drop out of the cycle.
        queues = {kind: [item for item in rows if item.vehicle_class == kind]
                  for kind in _CLASSES}
        while len(alternatives) < wanted and attempts < MAX_LOAD_ATTEMPTS and any(queues.values()):
            for kind in _CLASSES:
                while queues[kind] and attempts < MAX_LOAD_ATTEMPTS:
                    item = queues[kind].pop()
                    definition = load(item)
                    if definition is not None:
                        alternatives.append((definition, kind))
                        break
                if len(alternatives) >= wanted:
                    break
        # Same-tier composition is preferred to increasing variety with a
        # weaker/stronger tank. No tier change is necessary for a safe fallback.

    own_choice = player_definition, own_class
    choices = alternatives or [own_choice]
    if lone:
        return (BotVehicle(2, 0, *choices[0]),)
    result = [BotVehicle(2, 0, *own_choice)]
    for slot in range(1, enemies):
        result.append(BotVehicle(2, slot, *choices[(slot-1) % len(choices)]))
    for slot in range(1, allies+1):
        result.append(BotVehicle(1, slot, *choices[(slot-1) % len(choices)]))
    return tuple(result)


def select_room_bot_roster(game, player_definition, *, teams, bot_counts, seed):
    """Fill slots after the human participants, using the same tank pool on both teams."""
    if (not isinstance(bot_counts, (tuple, list)) or len(bot_counts) != len(TEAMS)
            or any(type(count) is not int or not 0 <= count <= TEAM_SIZE for count in bot_counts)):
        raise ValueError('Invalid room bot counts')
    if not teams:
        raise ValueError('A room battle requires a human participant')
    validate_teams([*teams, *([1]*bot_counts[0]), *([2]*bot_counts[1])])
    if not any(bot_counts):
        return ()
    choices = select_bot_roster(game, player_definition, enemies=max(bot_counts), allies=0, seed=seed)
    roster = []
    for team, count in ((2, bot_counts[1]), (1, bot_counts[0])):
        first_slot = teams.count(team)
        roster.extend(BotVehicle(team, first_slot+index, item.definition, item.vehicle_class)
                      for index, item in enumerate(choices[:count]))
    return tuple(roster)
