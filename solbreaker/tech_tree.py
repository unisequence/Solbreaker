"""Immutable research catalog read from pinned vehicle XML.

Describes modules, prices and unlock edges only. A stock configuration here is
the first declared choice per slot and says nothing about native battle support.
"""
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
import re
import struct
import xml.etree.ElementTree as ET

from .dvpl import read
from .vehicle_data import VehicleConfiguration, initial_ammunition, overlay, resolve_gun

# Vehicle XML section -> (component catalog, slot name used by the API)
_SECTIONS = (('chassis','chassis','chassis'),('turrets','turrets','turret'),
             ('engines','engines','engine'),('fuelTanks','fuelTanks','fuel_tank'),
             ('radios','radios','radio'))
_UNLOCK_SLOTS = {'chassis':'chassis','turret':'turret','gun':'gun','engine':'engine',
                 'fuelTank':'fuel_tank','radio':'radio','vehicle':'vehicle'}


class _NativeUnknown(Exception):
    """Unlock target the native index order cannot be derived for."""


@dataclass(frozen=True)
class ModuleResearch:
    slot: str
    tag: str
    ident: int
    credits: int | None


@dataclass(frozen=True)
class Unlock:
    """source is the owning module (slot, tag), or None for the vehicle itself."""
    source: tuple[str,str] | None
    target: tuple[str,str]
    xp: int


@dataclass(frozen=True)
class VehicleResearch:
    nation: str
    nation_id: int
    name: str
    ident: int
    tier: int
    credits: int | None
    gold: int | None
    modules: tuple[ModuleResearch,...]
    unlocks: tuple[Unlock,...]
    stock: VehicleConfiguration
    unsupported_unlocks: tuple[str,...] = ()  # kind:target:cost for non-XP edges
    # Edges in the client's (11.21) native index order; None keeps the slot of a
    # non-XP edge. An empty tuple means the native mapping is unconfirmed.
    native_unlocks: tuple[Unlock|None,...] = ()
    # (full shell CD, credits, gold) for each compatible turret/gun shot. Keep
    # the observations because a shell CD can have gun-specific price overrides.
    shell_prices: tuple[tuple[int,int,int], ...] = ()

    @property
    def key(self):
        return f'{self.nation}/{self.name}'

    @property
    def type_cd(self):
        return (self.ident<<8)|(self.nation_id<<4)|1


@dataclass(frozen=True)
class InventoryVehicle:
    """Native garage data from XML, independent of supported battle mechanics."""
    key: str
    type_cd: int
    descriptor: bytes
    shell_cd: int
    ammo: int
    configuration: VehicleConfiguration
    researched: tuple[int, ...]
    compatible_shells: tuple[int, ...] = ()  # gun XML order, independent of default shell
    default_portions: tuple[float | None, ...] = ()  # resolved gun XML weights

    @property
    def inventory(self):
        shells = initial_ammunition(self.compatible_shells or (self.shell_cd,),self.ammo,
                                     self.shell_cd,self.default_portions)
        return dict(descriptor=self.descriptor.hex(), shells=shells)


@lru_cache(maxsize=64)
def _component(game: Path, nation: str, kind: str):
    return ET.fromstring(read(game/'Data/XML/item_defs/vehicles'/nation/'components'/(kind+'.xml.dvpl')))


def _resolve(game: Path, nation: str, kind: str, local):
    if local is None:
        raise ValueError(f'Missing inventory component: {kind}')
    shared = None
    if (local.text or '').strip() == 'shared':
        shared = _component(game, nation, kind).find('shared/'+local.tag)
        if shared is None:
            raise ValueError(f'Missing shared component: {kind}/{local.tag}')
    return resolve_gun(shared, local) if kind == 'guns' else overlay(shared, local)


def _ammo_layout(game: Path, nation: str, nation_id: int, key: str, vehicle, configuration, stock):
    """(shell CDs in XML order, capacity, selected tag, selected CD, XML portions).

    Shell kind and price are deliberately not validated.
    """
    turret = _resolve(game, nation, 'turrets', vehicle.find('turrets0/'+(configuration.turret or stock.turret)))
    local_gun = turret.find('guns/'+(configuration.gun or stock.gun))
    gun = _resolve(game, nation, 'guns', local_gun)
    shots = gun.find('shots')
    if shots is None or not len(shots):
        raise ValueError(f'Missing inventory ammunition: {key}')
    selected = configuration.shell
    default = shots.find(selected) if selected is not None else shots[0]
    if default is None:
        raise ValueError(f'Missing stock shell: {key}')
    shells = _component(game, nation, 'shells')
    ids, selected_cd, portions = [], None, []
    for shot in shots:
        shell = shells.find(shot.tag)
        if shell is None:
            raise ValueError(f'Unknown inventory shell: {key}/{shot.tag}')
        if shot.find('shell') is not None:
            shell = overlay(shell, shot.find('shell'))
        ids.append((int(shell.findtext('id'))<<8)|(nation_id<<4)|10)
        merged_shot = gun.find('shots/'+shot.tag)
        portion = merged_shot.findtext('defaultPortion') if merged_shot is not None else None
        portions.append(float(portion) if portion is not None else None)
        if shot is default:selected_cd = ids[-1]
    capacity = int(float(gun.findtext('maxAmmo')))
    if (len(set(ids)) != len(ids) or len(ids) > 32 or not 0 < capacity < 65536
            or not all(0 <= cd <= 2**31-1 for cd in ids)):
        raise ValueError(f'Inventory values exceed the native format: {key}')
    # Validate the complete vector without changing this lightweight API's order.
    initial_ammunition(ids,capacity,selected_cd,portions)
    return tuple(ids), capacity, default.tag, selected_cd, tuple(portions)


@lru_cache(maxsize=4096)
def _load_ammo_layout(game: Path, nation: str, name: str, configuration: VehicleConfiguration):
    research = lookup_vehicle(game, nation, name)
    vehicle = ET.fromstring(read(game/'Data/XML/item_defs/vehicles'/nation/(name+'.xml.dvpl')))
    ids, capacity, _, _, _ = _ammo_layout(game, nation, research.nation_id, research.key, vehicle,
                                    configuration, research.stock)
    return ids, capacity


def load_ammo_layout(game: Path, nation: str, name: str, configuration: VehicleConfiguration):
    """Immutable ((shell CDs in gun XML order), capacity) without ballistics."""
    if not isinstance(configuration, VehicleConfiguration):
        raise ValueError('Invalid vehicle configuration')
    return _load_ammo_layout(Path(game), nation, name, configuration)


@lru_cache(maxsize=4)
def load_inventory_catalog(game: Path):
    """Every listed stock vehicle, including non-AP and tutorial variants.

    Only module IDs, compatible ammunition and capacity are needed by the
    native garage. Do not require collision meshes or invent battle stats.
    """
    game = Path(game)
    root = game/'Data/XML/item_defs/vehicles'
    nations = ET.fromstring(read(root/'common/nations.xml.dvpl'))
    result, seen = [], set()
    slots = ('chassis', 'engine', 'fuel_tank', 'radio', 'turret', 'gun')
    kinds = dict(vehicle=1, chassis=2, turret=3, gun=4, engine=5, fuel_tank=6, radio=7)
    for nation in nations:
        for research in load_tech_tree(game, nation.tag):
            if research.type_cd in seen:
                raise ValueError('Duplicate vehicle type in inventory catalog')
            vehicle = ET.fromstring(read(root/nation.tag/(research.name+'.xml.dvpl')))
            shell_ids, ammo, shell_tag, shell_cd, portions = _ammo_layout(game, nation.tag, research.nation_id,
                                                      research.key, vehicle, research.stock, research.stock)
            modules = [next(m for m in research.modules
                            if m.slot == slot and m.tag == getattr(research.stock, slot)) for slot in slots]
            descriptor = struct.pack('<2B6HB', (research.nation_id<<4)|1, research.ident,
                                     *(module.ident for module in modules), 0)
            installed = {research.type_cd} | {
                (m.ident<<8)|(research.nation_id<<4)|kinds[m.slot] for m in modules}
            result.append(InventoryVehicle(research.key, research.type_cd, descriptor, shell_cd, ammo,
                replace(research.stock, shell=shell_tag), tuple(sorted(installed)), shell_ids, portions))
            seen.add(research.type_cd)
    return tuple(result)


def _price(node):
    """(credits, gold) from a price node; gold and credits are never mixed."""
    price = node.find('price') if node is not None else None
    if price is None:
        return None,None
    text = (price.text or '').strip()
    if not re.fullmatch(r'\d+(\.0+)?',text):
        raise ValueError('Invalid research price')
    amount = int(text.split('.')[0])
    if price.find('gold') is not None:
        return None,amount
    return amount,None


def load_tech_tree(game: Path, nation: str):
    if not isinstance(nation,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',nation):
        raise ValueError('Invalid vehicle resource name')
    return _load_tech_tree(Path(game),nation)


def lookup_vehicle(game: Path, nation: str, name: str):
    for vehicle in load_tech_tree(game,nation):
        if vehicle.name == name:
            return vehicle
    raise ValueError('Unknown vehicle')


@lru_cache(maxsize=16)
def _load_tech_tree(game: Path, nation):
    root = game/'Data/XML/item_defs/vehicles'
    def xml(path):
        return ET.fromstring(read(path))
    national = xml(root/'common/nations.xml.dvpl').find(nation)
    if national is None:
        raise ValueError('Unknown nation')
    nation_id = int(national.findtext('id'))
    catalogs = {}
    def catalog(kind):
        if kind not in catalogs:
            catalogs[kind] = xml(root/nation/'components'/(kind+'.xml.dvpl'))
        return catalogs[kind]
    def resolve(kind, local):
        shared = None
        if (local.text or '').strip() == 'shared':
            shared = catalog(kind).find('shared/'+local.tag)
            if shared is None:
                raise ValueError(f'Missing shared component: {kind}/{local.tag}')
        ident = catalog(kind).findtext('ids/'+local.tag)
        if ident is None:
            raise ValueError(f'Missing component ID: {kind}/{local.tag}')
        return (resolve_gun(shared,local) if kind == 'guns' else overlay(shared,local)),int(ident)
    def edges(source, node, unsupported=None):
        found = []
        for group in node.findall('unlocks'):
            for child in group:
                slot = _UNLOCK_SLOTS.get(child.tag)
                cost = (child.findtext('cost') or '').strip()
                target = (child.text or '').strip()
                if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*:\d+',cost):
                    if unsupported is not None and target:
                        unsupported.append(f'{child.tag}:{target}:{cost}')
                    continue  # non-XP currency (season points); reported, never an XP edge
                if slot is None or not target or not re.fullmatch(r'\d+',cost):
                    raise ValueError(f'Invalid unlock: {child.tag}')
                found.append(Unlock(source,(slot,target),int(cost)))
        return found
    def native_edges(source, node):
        found = []
        for group in node.findall('unlocks'):
            for child in group:
                slot = _UNLOCK_SLOTS.get(child.tag)
                target = (child.text or '').strip()
                if slot is None or not target:
                    raise _NativeUnknown
                cost = (child.findtext('cost') or '').strip()
                if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*:\d+',cost):
                    found.append(None)
                elif re.fullmatch(r'\d+',cost):
                    found.append(Unlock(source,(slot,target),int(cost)))
                else:
                    raise ValueError(f'Invalid unlock: {child.tag}')
        return found
    shell_xml = None
    result = []
    for listing in xml(root/nation/'list.xml.dvpl'):
        vehicle = xml(root/nation/(listing.tag+'.xml.dvpl'))
        modules,seen,unsupported = [],set(),[]
        shell_prices = []
        unlocks = edges(None,vehicle,unsupported)
        def add(slot, tag, ident, node):
            if (slot,tag) not in seen:
                seen.add((slot,tag))
                modules.append(ModuleResearch(slot,tag,ident,_price(node)[0]))
            unlocks.extend(edges((slot,tag),node,unsupported))
        stock = {}
        turret_resolved = []
        native = {'chassis':[],'engine':[],'fuel_tank':[],'turret':[]}
        try:
            native_own = native_edges(None,vehicle)
        except _NativeUnknown:
            native_own,native = None,None
        for section,kind,slot in _SECTIONS:
            for parent in (n for n in vehicle if re.fullmatch(section+r'\d*',n.tag)):
                for local in parent:
                    node,ident = resolve(kind,local)
                    add(slot,local.tag,ident,node)
                    stock.setdefault(slot,local.tag)
                    if native is not None and slot != 'radio':
                        try:
                            if slot == 'chassis':
                                # Shared chassis emit no edges at all, local ones included.
                                if (local.text or '').strip() != 'shared':
                                    native[slot].extend(native_edges((slot,local.tag),local))
                            elif slot != 'turret':
                                native[slot].extend(native_edges((slot,local.tag),node))
                            elif not native_edges(None,local) and native_edges(None,node):
                                native = None  # inherited turret edges: mapping unconfirmed
                        except _NativeUnknown:
                            native = None
                    if slot == 'turret':
                        turret_resolved.append((local,node))
                        # Resolved node carries shared guns; local order stays first.
                        local_guns = local.find('guns')
                        local_tags = [] if local_guns is None else [g.tag for g in local_guns]
                        resolved_guns = node.find('guns')
                        by_tag = {} if resolved_guns is None else {g.tag:g for g in resolved_guns}
                        order = local_tags+[t for t in by_tag if t not in local_tags]
                        for local_gun in (by_tag[t] for t in order):
                            gun,gun_ident = resolve('guns',local_gun)
                            add('gun',local_gun.tag,gun_ident,gun)
                            if shell_xml is None:
                                shell_xml = xml(root/nation/'components/shells.xml.dvpl')
                            shots = gun.find('shots')
                            if shots is None:
                                raise ValueError(f'Missing research ammunition: {nation}/{listing.tag}/{local_gun.tag}')
                            for shot in shots:
                                source = shell_xml.find(shot.tag)
                                if source is None:
                                    raise ValueError(f'Unknown research shell: {nation}/{shot.tag}')
                                local_shell = shot.find('shell')
                                if local_shell is not None:
                                    source = overlay(source,local_shell)
                                credits,gold = _price(source)
                                if credits is None and gold is None:
                                    raise ValueError(f'Missing research shell price: {nation}/{shot.tag}')
                                shell_prices.append(((int(source.findtext('id'))<<8)|(nation_id<<4)|10,
                                                     credits or 0,gold or 0))
                            if native is not None:
                                try:
                                    gun_edges = native_edges(('gun',local_gun.tag),gun)
                                except _NativeUnknown:
                                    native = None
                                else:
                                    if gun_edges and local_gun.tag not in local_tags:
                                        native = None  # inherited gun edges, no local guns
                                    else:
                                        native['turret'].extend(gun_edges)
                            if len(turret_resolved) == 1:
                                stock.setdefault('gun',local_gun.tag)
                        if native is not None:
                            try:
                                native['turret'].extend(native_edges(('turret',local.tag),node))
                            except _NativeUnknown:
                                native = None
        # Prefer AP, then APCR/HEAT; HE-only guns may use their first HE shot.
        shell = None
        first_he = None
        if turret_resolved and 'gun' in stock:
            local_gun = turret_resolved[0][1].find('guns/'+stock['gun'])
            gun,_ = resolve('guns',local_gun)
            shots = gun.find('shots')
            if shots is not None:
                if shell_xml is None:
                    shell_xml = xml(root/nation/'components/shells.xml.dvpl')
                for shot in shots:
                    source = shell_xml.find(shot.tag)
                    if source is None:
                        continue
                    if shot.find('shell') is not None:
                        source = overlay(source,shot.find('shell'))
                    if source.findtext('kind') == 'ARMOR_PIERCING':
                        shell = shot.tag
                        break
                    if shell is None and source.findtext('kind') in ('ARMOR_PIERCING_CR','HOLLOW_CHARGE'):
                        shell = shot.tag
                    if first_he is None and source.findtext('kind')=='HIGH_EXPLOSIVE':
                        first_he = shot.tag
        shell = shell or first_he
        credits,gold = _price(listing)
        result.append(VehicleResearch(nation,nation_id,listing.tag,int(listing.findtext('id')),
            int(listing.findtext('level')),credits,gold,tuple(modules),tuple(unlocks),
            VehicleConfiguration(stock.get('chassis'),stock.get('turret'),stock.get('gun'),
                stock.get('engine'),stock.get('fuel_tank'),stock.get('radio'),shell),
            tuple(unsupported),
            () if native is None else tuple(native_own+native['chassis']+native['engine']
                +native['fuel_tank']+native['turret']),tuple(shell_prices)))
    return tuple(result)


load_tech_tree.cache_clear = _load_tech_tree.cache_clear
