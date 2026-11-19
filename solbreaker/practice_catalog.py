"""Installed vehicles and stock definitions for the Polygon target picker."""
from dataclasses import dataclass
from functools import lru_cache
import math
import re
import xml.etree.ElementTree as ET

from .dvpl import read

CLASSES = ('lightTank', 'mediumTank', 'heavyTank', 'AT-SPG')
RESOURCE_NAME = re.compile(r'[A-Za-z0-9_-]+')


@dataclass(frozen=True, slots=True)
class Target:
    compact_id: int
    nation: str
    model: str
    name_key: str
    tier: int | None
    vehicle_class: str | None
    installed: bool


@lru_cache(maxsize=4)
def catalog(game):
    root = game/'Data/XML/item_defs/vehicles'
    nations = ET.fromstring(read(root/'common/nations.xml.dvpl'))
    result = {}
    for nation in nations:
        if not RESOURCE_NAME.fullmatch(nation.tag):
            continue
        nation_id = int(nation.findtext('id', '-1'))
        if not 0 <= nation_id < 16:
            raise ValueError('Invalid Polygon nation ID')
        listing = ET.fromstring(read(root/nation.tag/'list.xml.dvpl'))
        for item in listing:
            if not RESOURCE_NAME.fullmatch(item.tag) or item.find('id') is None:
                continue
            ident = int(item.findtext('id'))
            if not 0 <= ident < 256:
                raise ValueError('Invalid Polygon vehicle ID')
            compact_id = (ident << 8) | (nation_id << 4) | 1
            if compact_id in result:
                raise ValueError('Duplicate Polygon vehicle ID')
            level = item.findtext('level', '').strip()
            tags = (item.findtext('tags') or '').split()
            name = (item.findtext('userString') or f'#{nation.tag}_vehicles:{item.tag}').strip()
            result[compact_id] = Target(
                compact_id, nation.tag, item.tag, name,
                int(level) if level.isdecimal() else None,
                next((kind for kind in CLASSES if kind in tags), None),
                (root/nation.tag/(item.tag+'.xml.dvpl')).is_file())
    return result


@dataclass(frozen=True, slots=True)
class Module:
    tag: str
    name_key: str
    level: int = 0


@dataclass(frozen=True, slots=True)
class Modules:
    chassis: tuple[Module, ...]
    turrets: tuple[Module, ...]
    guns: tuple[tuple[Module, ...], ...]
    engines: tuple[Module, ...]

    def configuration(self, packed):
        from .vehicle_data import VehicleConfiguration

        chassis, turret, gun, engine = ((packed >> (4 * slot)) & 15 for slot in range(4))
        try:
            return VehicleConfiguration(
                chassis=self.chassis[chassis].tag, turret=self.turrets[turret].tag,
                gun=self.guns[turret][gun].tag, engine=self.engines[engine].tag)
        except IndexError as error:
            raise ValueError('Incompatible Polygon modules') from error


@lru_cache(maxsize=16)
def _component_catalogs(game, nation):
    root = game/'Data/XML/item_defs/vehicles'/nation/'components'
    return {kind: ET.fromstring(read(root/(kind+'.xml.dvpl')))
            for kind in ('chassis', 'turrets', 'guns', 'engines')}


@lru_cache(maxsize=2048)
def modules(game, target):
    """Read the battle loader's module order and turret/gun compatibility."""
    from .vehicle_data import overlay

    root = game/'Data/XML/item_defs/vehicles'/target.nation
    vehicle = ET.fromstring(read(root/(target.model+'.xml.dvpl')))
    components = _component_catalogs(game, target.nation)

    def resolve(local, kind):
        shared = (components[kind].find('shared/'+local.tag)
                  if (local.text or '').strip() == 'shared' else None)
        return overlay(shared, local)

    def choices(nodes, kind):
        result = []
        for local in nodes:
            item = resolve(local, kind)
            level = item.findtext('level', '').strip()
            result.append(Module(local.tag, (item.findtext('userString') or local.tag).strip(),
                                 int(level) if level.isdecimal() else 0))
        return tuple(result)

    def children(node, name):
        group = node.find(name)
        return () if group is None else tuple(child for child in group if RESOURCE_NAME.fullmatch(child.tag))

    turrets = children(vehicle, 'turrets0')
    return Modules(choices(children(vehicle, 'chassis'), 'chassis'),
                   choices(turrets, 'turrets'),
                   tuple(choices(children(resolve(turret, 'turrets'), 'guns'), 'guns') for turret in turrets),
                   choices(children(vehicle, 'engines'), 'engines'))


def definition(game, target, packed_modules=0):
    from .armor import load_battle_vehicle

    configuration = modules(game, target).configuration(packed_modules) if packed_modules else None
    entry = load_battle_vehicle(game, target.nation, target.model, configuration)
    weapon = entry.weapon
    values = (entry.drive.forward, entry.drive.reverse, entry.drive.rotation, entry.health, weapon.reload)
    if (int.from_bytes(entry.descriptor[:2], 'little') != target.compact_id
            or any(not math.isfinite(value) or value <= 0 for value in values) or not weapon.shells):
        raise ValueError('Unsupported Polygon vehicle')
    return entry
