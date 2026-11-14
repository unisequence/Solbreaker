"""Local catalogs sent through the native synchronization resource streams.

No official shop, offers, clan memberships or battle history are imported.
The client still needs the catalog structures while loading its hangar.
"""
from copy import deepcopy
from collections import Counter
from functools import lru_cache
from pathlib import Path
import xml.etree.ElementTree as ET

from .dvpl import read
from .loadouts import CONSUMABLE, PROVISION, load_catalog
from .progression import item_cd
from .tech_tree import _price, load_tech_tree

# Local sandbox rule, not an imported official economy configuration.
SELL_PRICE_FACTOR = 0.5
# Gold -> credits rate for premium shells bought with credits. Equals the native
# service default (400) and is also sent as shop exchangeRate; no official value.
AMMO_EXCHANGE_RATE = 400
# The native inventory footer shows the old supply's full resale value when
# replacing it. Publish the same modifiers used by the slot transaction.
SUPPLY_SELL_PRICE_FACTOR = 1.0
# Advance whenever the local catalogue gains corrected client-visible entries.
# Native cache invalidation is revision based: old kind-10 caches fall back to
# INT_MAX in the ammunition editor when they lack a shell price.
SHOP_REVISION = 13

# Stock Blitz equipment unlock prices by vehicle tier and slot level.
# https://wargaming.net/support/en/products/wotb/article/15975/
_EQUIPMENT_SLOT_CREDITS = (
    (100, 200, 300),
    (200, 300, 400),
    (400, 500, 600),
    (4000, 5000, 6000),
    (40000, 50000, 60000),
    (100000, 120000, 140000),
    (150000, 170000, 190000),
    (200000, 225000, 250000),
    (250000, 275000, 300000),
    (300000, 350000, 400000),
)


def equipment_slot_credits(tier, level):
    """Price of unlocking one slot, with a zero-based equipment level."""
    if type(tier) is not int or not 1 <= tier <= len(_EQUIPMENT_SLOT_CREDITS):
        raise ValueError('Invalid vehicle tier for equipment pricing')
    if type(level) is not int or not 0 <= level < 3:
        raise ValueError('Invalid equipment level')
    return _EQUIPMENT_SLOT_CREDITS[tier - 1][level]

# Stock ServerConfigurationData category prices, in vehicle tier order.
# The pinned item XML can override these for a particular provision.
_SUPPLY_CATEGORY_CREDITS = {
    CONSUMABLE: {
        'free':    (0, 0, 0,   0,   0,    0,    0,    0,    0,    0),
        'radical': (0, 0, 0, 500, 750, 4900, 5600, 6300, 7000, 8400),
        'common':  (0, 0, 0, 150, 250, 1750, 2000, 2250, 2500, 3000),
        'potent':  (100, 125, 150, 250, 500, 2800, 3200, 3600, 4000, 5000),
    },
    PROVISION: {
        'radical': (0, 0, 0,   0,   0, 4900, 5600, 6300, 7000, 8400),
        'common':  (0, 0, 0, 250, 500, 1750, 2000, 2250, 2500, 3000),
        'potent':  (0, 0, 0,   0, 750, 2800, 3200, 3600, 4000, 5000),
    },
}
_TIER_KEYS = tuple(f'level{tier}' for tier in range(1, 11))


def _supply_override(item):
    fields = [(path.removeprefix('price/'), int(value)) for path, value in item.extras
              if path.startswith('price/')]
    if not fields:
        return {}
    prices = dict(fields)
    if (len(fields) != len(_TIER_KEYS) or prices.keys() != set(_TIER_KEYS)
            or any(not 0 <= value <= 0x7fffffff for value in prices.values())):
        raise ValueError(f'Invalid supply tier prices: {item.tag}')
    return prices


def supply_credits(item, tier):
    """Credits for an installed supply, using the same tables as the client."""
    if item is None:
        return 0
    if type(tier) is not int or not 1 <= tier <= len(_TIER_KEYS):
        raise ValueError('Invalid vehicle tier for supply pricing')
    override = _supply_override(item)
    if override:
        return override[_TIER_KEYS[tier - 1]]
    try:
        return _SUPPLY_CATEGORY_CREDITS[item.kind][item.category][tier - 1]
    except KeyError:
        raise ValueError(f'Unknown supply price category: {item.tag}') from None


def _loadout_prices(loadouts):
    # Native common-nation items index explicit tier overrides by full compact
    # descriptor. The default tier tables are separate kind-specific fields.
    overrides, tables = {}, {}
    for kind, code in ((CONSUMABLE, 11), (PROVISION, 13)):
        prices = {}
        for item in loadouts._items(kind):
            override = _supply_override(item)
            if override:
                prices[(item.ident << 8) | 0xf0 | code] = {
                    tier: (credits, 0) for tier, credits in override.items()}
            elif item.category not in _SUPPLY_CATEGORY_CREDITS[kind]:
                raise ValueError(f'Unknown supply price category: {item.tag}')
        overrides[code] = (prices,)
        tables[kind + 'Cost'] = {
            category: [(price, 0) for price in prices]
            for category, prices in _SUPPLY_CATEGORY_CREDITS[kind].items()}
    return overrides, tables


def sale_credits(credits):
    """Match Price::operator*= (86bab0): int32 -> float32, multiply, truncate."""
    import struct
    f32 = lambda value: struct.unpack('<f',struct.pack('<f',value))[0]
    return int(f32(f32(credits)*SELL_PRICE_FACTOR))


vehicle_sale_credits = sale_credits


def maps_configuration(map_name="rudniki"):
    # MapsConfig.proto in this build:
    # MapsConfig.progressionThreshold=1 (int64), mapList=2 (MapInfo).
    # MapInfo.name=1 (string), levels=2 (repeated int32),
    # newbieVehicleLevel=3 (int32), newbieBattleCount=4 (int64).
    # All values here fit in a one-byte protobuf varint.
    from .map_catalog import supported_maps
    names = (map_name,) if isinstance(map_name, str) else tuple(map_name)
    if (not names or len(set(names)) != len(names)
            or any(not isinstance(name, str) or name not in supported_maps()
                   for name in names)):
        raise ValueError("Unsupported configured map list")

    def varint(value):
        output = bytearray()
        while value > 127:
            output.append((value & 127) | 128)
            value >>= 7
        output.append(value)
        return bytes(output)

    levels = bytes(range(1, 11))
    entries = []
    for map_name in names:
        name = map_name.encode("ascii")
        entry = (b"\x0a" + varint(len(name)) + name
                 + b"\x12" + varint(len(levels)) + levels)
        entries.append(b"\x12" + varint(len(entry)) + entry)
    return b"".join(entries)


def research_prices(game):
    """cmd300 payload: {rev, items: {nation_id: {kind: ({id_or_cd: (credits, gold)},)}}}.

    Vehicle keys are the in-nation id, module/shell keys are full item CDs.
    Unknown module prices are omitted. A shell CD has one native catalog price;
    choose the most common compatible gun price, using the base shell price to
    break ties. Gun-specific overrides remain in the vehicle XML.
    """
    return deepcopy(_research_prices(str(Path(game).absolute())))


@lru_cache(maxsize=4)
def _research_prices(game):
    game = Path(game)
    root = ET.fromstring(read(game / 'Data/XML/item_defs/vehicles/common/nations.xml.dvpl'))
    items = {}
    for nation in root:
        if not (nation.findtext('id') or '').strip().isdigit():
            continue
        nation_id = int(nation.findtext('id'))
        vehicles, modules, conflicts = {}, {}, set()
        shell_votes = {}
        for vehicle in load_tech_tree(game, nation.tag):
            if vehicle.gold is not None:
                vehicles[vehicle.ident] = (0, vehicle.gold)
            elif vehicle.credits is not None:
                vehicles[vehicle.ident] = (vehicle.credits, 0)
            for module in vehicle.modules:
                if module.credits is None:
                    continue
                cd = item_cd(module.slot, module.ident, nation_id)
                price = (module.credits, 0)
                if modules.setdefault(cd, price) != price:
                    conflicts.add(cd)
            for cd, credits, gold in vehicle.shell_prices:
                shell_votes.setdefault(cd, Counter())[(credits, gold)] += 1
        shell_root = ET.fromstring(read(game/'Data/XML/item_defs/vehicles'/nation.tag/'components/shells.xml.dvpl'))
        base_shell_prices = {}
        for shell in shell_root:
            ident = shell.findtext('id')
            if ident is None:
                continue
            credits, gold = _price(shell)
            if credits is None and gold is None:
                raise ValueError(f'Missing shell catalog price: {nation.tag}/{shell.tag}')
            cd = (int(ident)<<8)|(nation_id<<4)|10
            if cd in base_shell_prices:
                raise ValueError(f'Duplicate shell catalog CD: {cd}')
            base_shell_prices[cd] = (credits or 0, gold or 0)
        if shell_votes.keys() != base_shell_prices.keys():
            raise ValueError(f'Shell catalog coverage differs from compatible guns: {nation.tag}')
        shells = {}
        for cd, votes in shell_votes.items():
            maximum = max(votes.values())
            tied = [price for price, count in votes.items() if count == maximum]
            base = base_shell_prices[cd]
            shells[cd] = base if base in tied else min(tied)
        kinds = {1: (vehicles,), 10: (shells,)}
        by_kind = {}
        for cd, price in modules.items():
            if cd not in conflicts:
                by_kind.setdefault(cd & 15, {})[cd] = price
        kinds.update((kind, (prices,)) for kind, prices in by_kind.items())
        items[nation_id] = kinds
    # BuyVehicle's native dialog always looks up mastery 75/100, even when the
    # player chooses the standard crew. Missing keys throw std::out_of_range
    # (1ab3960). None is the native unavailable-price value (1cc8080); retain it
    # for unimplemented crew training instead of inventing official prices.
    mastery = [dict(mastery=level, creditsByLevel=[None]*10, goldByLevel=[])
               for level in (75,100)]
    items[15], supply_tables = _loadout_prices(load_catalog(game))
    return {'rev': SHOP_REVISION, 'items': items, 'setMasteryCost': mastery,
            'sellPriceModifs': {None: SELL_PRICE_FACTOR, 'shell': SELL_PRICE_FACTOR,
                               11: SUPPLY_SELL_PRICE_FACTOR, 13: SUPPLY_SELL_PRICE_FACTOR},
            'exchangeRate': AMMO_EXCHANGE_RATE,
            **supply_tables,
            # ExtractOptionalDevicesSlotsConfig (0x1aa9c10), called with
            # optDevSlotsEconomics at 0x1ad45e6.
            'optDevSlotsEconomics': {'slotsByVehLevel': {
                tier: [dict(currency='credits', cost=(credits, 0)) for credits in levels]
                for tier, levels in enumerate(_EQUIPMENT_SLOT_CREDITS, 1)}}}


def synchronized_catalog(command):
    if command == 312:  # ArtefactsData: ID -> (prototype dictionary, metadata).
        return {}
    if command == 300:  # ServerConfigurationData.
        return {"rev": 1}
    if command == 600:  # Dossier: revision and sequence of records.
        return (1, [])
    if command == 601:  # Vehicle ranks and their counter configuration.
        return {"revision": 1,
                "rankSettings": {"rankPrices": {}, "rankGrades": {}},
                "counterSettings": {"economics": {"slotRank": {}, "slotPrices": {}},
                                    "counters": {}, "counters_view": {"levels": {}},
                                    "vehicles": {"counters": {}}}}
    return None
