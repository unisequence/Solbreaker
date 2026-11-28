"""Bounded, deterministic tank selection for a local bot battle.

This runs while preparing an arena. It reads national list metadata rather
than constructing every garage vehicle, and never changes a player's loadout.
The returned definitions are the recipe: parent and owner must share it instead
of drawing two independent rosters.
"""
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
import math
from pathlib import Path
import random
import re
import xml.etree.ElementTree as ET

from .dvpl import read
from .roster import LARGE_TEAM_SIZE, TEAMS, TEAM_SIZE, validate_teams
from .vehicle_data import VehicleConfiguration, VehicleDefinition, load_vehicle


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

MAX_LOAD_ATTEMPTS = 40
TOP, MOBILITY, GUN = range(3)
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
    sandbox_preset: tuple[int, ...] = ()


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
    """Check the exact module configuration before sharing a frozen recipe.

    ArmorModel validates collision materials, which asset existence alone
    cannot establish. Only its verdict is retained, never meshes or a scene.
    The selected human vehicle does not pass through this NPC admission path.
    """
    from .armor import ArmorModel
    try:
        ArmorModel(game, definition)
    except (OSError, ValueError):
        return False
    return True


@lru_cache(maxsize=96)
def _supported_vehicle(game, nation, name, configuration=None):
    try:
        definition = load_vehicle(game, nation, name, configuration)
    except (OSError, ValueError, ET.ParseError):
        return None
    values = (definition.drive.forward, definition.drive.reverse,
              definition.drive.rotation, definition.health, definition.weapon.reload)
    if (any(not math.isfinite(value) or value <= 0 for value in values)
            or not definition.weapon.shells or definition.armor_states_controller is not None):
        return None
    return definition if _geometry_supported(game, definition) else None


def stock_vehicle(game, nation, name):
    return _supported_vehicle(game, nation, name)


def _module_index(modules, upgrade):
    # XML level breaks ties between alternate branch options. Declaration
    # order is the fallback for equal levels and older entries with no level.
    return max(range(len(modules)), key=lambda i: (modules[i].level, i)) if upgrade else 0


@lru_cache(maxsize=128)
def _bot_configuration(game, nation, name, grade):
    from .practice_catalog import Target, modules

    choices = modules(game, Target(0, nation, name, '', None, None, True))
    combat = grade != MOBILITY
    mobility = grade != GUN
    turret = _module_index(choices.turrets, combat)
    return VehicleConfiguration(
        chassis=choices.chassis[_module_index(choices.chassis, mobility)].tag,
        turret=choices.turrets[turret].tag,
        gun=choices.guns[turret][_module_index(choices.guns[turret], combat)].tag,
        engine=choices.engines[_module_index(choices.engines, mobility)].tag)


@lru_cache(maxsize=96)
def _bot_vehicle(game, nation, name, grade):
    for version in dict.fromkeys((grade, TOP)):
        try:
            configuration = _bot_configuration(game, nation, name, version)
        except (OSError, ValueError, IndexError, ET.ParseError):
            break
        tank = _supported_vehicle(game, nation, name, configuration)
        if tank is not None:
            return tank
    return stock_vehicle(game, nation, name)


def _dpm(definition):
    weapon = definition.weapon
    # The first loaded shell is the gun's normal ammunition. Clip and
    # burst timing stay the XML values; this is a selection estimate only.
    shells = max(1, weapon.clip)
    burst = max(1, weapon.burst_count)
    groups = (shells+burst-1)//burst
    reload = sum(weapon.pump_reload_times) if weapon.pump_reload_times else weapon.reload
    cycle = reload + (groups-1)*weapon.interval + (shells-groups)*weapon.burst_interval
    return weapon.shells[0].damage*shells*60/cycle


def _combat_difference(left, right):
    if left is None:
        return 0.
    lhs = left.health, _dpm(left), left.weapon.shells[0].penetration[0]
    rhs = right.health, _dpm(right), right.weapon.shells[0].penetration[0]
    return sum(weight * abs(math.log(a/b)) for a, b, weight in zip(lhs, rhs, (1., .7, .5))
               if a > 0 and b > 0)


def _role(vehicle_class):
    return 'mobile' if vehicle_class in ('mediumTank', 'lightTank') else vehicle_class


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


def _balanced_roster(game, humans, bot_counts, seed):
    game = Path(game)
    rng = random.Random(seed)
    try:
        catalog = _catalog(game)
    except (OSError, ValueError, ET.ParseError):
        catalog = ()
    classes = {item.key: item.vehicle_class for item in catalog}
    used = Counter((tank.nation, tank.name) for team in TEAMS for tank in humans[team])
    fallback = next(tank for team in TEAMS for tank in humans[team])
    anchor_tiers = {tank.tier for team in TEAMS for tank in humans[team]}
    pool = [item for item in catalog if item.available and
            any(abs(item.tier-tier) <= 1 for tier in anchor_tiers)]
    rng.shuffle(pool)
    loaded = {}
    failed = set()
    attempts = 0
    roles = {team: Counter(_role(classes.get((tank.nation, tank.name))) for tank in humans[team])
             for team in TEAMS}

    def pick(tier, kind, grade, opponent=None):
        nonlocal attempts
        desired_role = _role(kind)
        # Identity comes first so even a rare human vehicle does not force a
        # mirror. The search stays within the human tier band.
        def priority(item):
            return (used[item.key], abs(item.tier-tier),
                    _role(item.vehicle_class) != desired_role, item.vehicle_class != kind)

        candidates = sorted(pool, key=priority)
        best = None
        considered = 0
        for item in candidates:
            rank = priority(item)
            if best and rank != best[0]:
                break
            load_key = item.key, grade
            tank = loaded.get(load_key)
            if tank is None:
                if load_key in failed or attempts >= MAX_LOAD_ATTEMPTS:
                    continue
                attempts += 1
                tank = _bot_vehicle(game, *item.key, grade)
                if (tank is None or (tank.nation, tank.name) != item.key or tank.tier != item.tier):
                    failed.add(load_key)
                    continue
                loaded[load_key] = tank
            difference = _combat_difference(opponent, tank)
            if best is None or difference < best[1]:
                best = rank, difference, tank, item.vehicle_class
            if difference < .20 or attempts >= MAX_LOAD_ATTEMPTS:
                break
            # Exclude testing an unlimited number of near-identical tanks
            # even when the requested role has dozens of compatible models.
            considered += 1
            if considered >= 3:
                break
        if best is not None:
            _, _, tank, kind = best
        else:
            # Once the load budget is exhausted, recycle a verified definition
            # before falling back to the already-admitted human configuration.
            if loaded:
                tank = min(loaded.values(), key=lambda candidate: (
                    used[candidate.nation, candidate.name], abs(candidate.tier-tier),
                    _role(classes.get((candidate.nation, candidate.name))) != desired_role,
                    _combat_difference(opponent, candidate)))
            else:
                tank = opponent or fallback
            kind = classes.get((tank.nation, tank.name))
        used[tank.nation, tank.name] += 1
        return tank, kind

    roster = {team: [] for team in TEAMS}
    sizes = {team: len(humans[team])+bot_counts[team-1] for team in TEAMS}
    kinds = list(_CLASSES)
    rng.shuffle(kinds)
    groups = [(slot, vacancies) for slot in range(max(sizes.values()))
              if (vacancies := [team for team in TEAMS if len(humans[team]) <= slot < sizes[team]])]
    mixed = rng.randint(1, max(1, len(groups)//2)) if len(groups) >= 3 else 0
    grades = [TOP] * (len(groups)-mixed) + [rng.choice((MOBILITY, GUN)) for _ in range(mixed)]
    rng.shuffle(grades)

    def role_gap(team, kind):
        role = _role(kind)
        return roles[team][role] - roles[3-team][role]

    for (slot, vacancies), grade in zip(groups, grades):
        if len(vacancies) == 2 and rng.getrandbits(1):
            vacancies.reverse()
        reference = None
        for team in vacancies:
            other = 3-team
            if slot < len(humans[other]):
                reference = humans[other][slot]
                kind = classes.get((reference.nation, reference.name))
            elif reference is None:
                kind = min(kinds, key=lambda candidate: (
                    role_gap(team, candidate), roles[team][_role(candidate)]+roles[other][_role(candidate)]))
            else:
                need = min(kinds, key=lambda candidate: role_gap(team, candidate))
                if role_gap(team, need) < min(0, role_gap(team, kind)-1):
                    kind = need
            tier = reference.tier if reference is not None else fallback.tier
            tank, selected_class = pick(tier, kind, grade, reference)
            roles[team][_role(selected_class)] += 1
            roster[team].append(BotVehicle(team, slot, tank, selected_class))
            reference, kind = tank, selected_class
    return tuple(roster[2]+roster[1])


def select_bot_roster(game, player_definition, *, enemies, allies, seed, capacity=TEAM_SIZE):
    """Keep the configured counts and balance independently selected bot models."""
    if (type(capacity) is not int or not TEAM_SIZE <= capacity <= LARGE_TEAM_SIZE
            or type(enemies) is not int or not 1 <= enemies <= capacity
            or type(allies) is not int or not 0 <= allies < capacity
            or type(seed) not in (int, str, bytes)):
        raise ValueError('Invalid bot roster request')
    return _balanced_roster(game, {1: (player_definition,), 2: ()}, (allies, enemies), seed)


def select_room_bot_roster(game, player_definition, *, teams, bot_counts, seed, human_definitions=None, capacity=TEAM_SIZE):
    """Fill vacancies after the human participants using their real teams and tanks."""
    if (not isinstance(bot_counts, (tuple, list)) or len(bot_counts) != len(TEAMS)
            or any(type(count) is not int or not 0 <= count <= capacity for count in bot_counts)):
        raise ValueError('Invalid room bot counts')
    if not teams:
        raise ValueError('A room battle requires a human participant')
    validate_teams([*teams, *([1]*bot_counts[0]), *([2]*bot_counts[1])], capacity=capacity)
    if not any(bot_counts):
        return ()
    definitions = (player_definition,) * len(teams) if human_definitions is None else tuple(human_definitions)
    if len(definitions) != len(teams):
        raise ValueError('A tank definition is required for each room participant')
    humans = {team: tuple(tank for side, tank in zip(teams, definitions) if side == team) for team in TEAMS}
    return _balanced_roster(game, humans, bot_counts, seed)


def custom_bot_roster(game, player, setup, *, capacity, seed):
    slots = {1: setup.slots[:capacity-1], 2: setup.slots[6:6+capacity]}
    return _custom_roster(game, {1: (player,), 2: ()}, slots, seed)


def custom_room_bot_roster(game, human_definitions, teams, lineup, *, capacity, seed):
    humans = {team: tuple(tank for side, tank in zip(teams, human_definitions) if side == team)
              for team in TEAMS}
    slots = {team: lineup.team_slots(team, capacity-len(humans[team])) for team in TEAMS}
    return _custom_roster(game, humans, slots, seed)


def _custom_roster(game, humans, room_slots, seed):
    from . import battle_setup, practice_catalog
    game = Path(game)
    rng = random.Random(seed)
    catalogue = practice_catalog.catalog(game)
    regular = _catalog(game)
    slots = [(team, slot, value) for team in (2, 1)
             for slot, value in enumerate(room_slots[team], len(humans[team]))]
    automatic = {(row.team, row.slot): row for row in _balanced_roster(
        game, humans, tuple(len(room_slots[team]) for team in TEAMS), seed
    )} if any(value == battle_setup.AUTOMATIC for _, _, value in slots) else {}
    player = next(tank for team in TEAMS for tank in humans[team])
    used = Counter((tank.nation, tank.name) for team in TEAMS for tank in humans[team])
    definitions = {}
    for team, slot, value in slots:
        if value != battle_setup.EMPTY and value & 15 == 1:
            target = catalogue.get(value & 0xffff)
            if target is None or not target.installed:
                raise ValueError('An assigned bot tank is missing from the installed catalog')
            tank = practice_catalog.definition(game, target, value >> 16)
            if tank.armor_states_controller is not None or not _geometry_supported(game, tank):
                raise ValueError('An assigned bot tank has unsupported collision geometry')
            definitions[team, slot] = BotVehicle(team, slot, tank, target.vehicle_class)
            used[target.nation, target.model] += 1

    attempts = 0
    for team, slot, value in slots:
        if (team, slot) in definitions or value == battle_setup.EMPTY:
            continue
        row = automatic.get((team, slot))
        if row is not None:
            definitions[team, slot] = row
            used[row.definition.nation, row.definition.name] += 1
            continue
        tier, class_id = battle_setup.random_filter(value)
        anchor = humans[team][0] if humans[team] else player
        kind = battle_setup.CLASSES[class_id-1] if class_id else None
        candidates = [item for item in regular if item.available and
                      (item.tier == tier if tier else abs(item.tier-anchor.tier) <= 1)
                      and (kind is None or item.vehicle_class == kind)]
        rng.shuffle(candidates)
        candidates.sort(key=lambda item: (used[item.key], abs(item.tier-anchor.tier)))
        for item in candidates:
            if attempts >= MAX_LOAD_ATTEMPTS:
                break
            attempts += 1
            grade = rng.choice((TOP, TOP, TOP, MOBILITY, GUN))
            tank = _bot_vehicle(game, *item.key, grade)
            if tank is not None:
                definitions[team, slot] = BotVehicle(team, slot, tank, item.vehicle_class)
                used[item.key] += 1
                break
        else:
            raise ValueError('No compatible bot tank matches the selected level and class')
        if (team, slot) not in definitions:
            raise ValueError('No compatible bot tank found within the lineup load budget')
    # Empty slots control team size. The surviving vehicles use consecutive
    # spawn anchors so mixed small/large map layouts remain compatible.
    roster = []
    for team in (2, 1):
        side = [row for (row_team, _), row in sorted(definitions.items()) if row_team == team]
        roster.extend(BotVehicle(team, slot, row.definition, row.vehicle_class)
                      for slot, row in enumerate(side, len(humans[team])))
    return tuple(roster)
