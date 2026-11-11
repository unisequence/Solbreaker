"""Raw immutable internal-module metadata from pinned vehicle XML.

This loader resolves the same component configuration as :func:`load_vehicle`,
but does not attach the data to battle state or choose any damage, repair,
fire, or crew behavior.  Per-component material declarations and the common
material catalog deliberately remain separate: a common record is never used
as a fallback here.
"""
from dataclasses import dataclass
from functools import lru_cache
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from .dvpl import read
from .vehicle_data import VehicleConfiguration, load_vehicle, overlay, resolve_gun


_RESOURCE_ROOT = Path('Data/XML/item_defs/vehicles')
_MODULE_FIELDS = (
    ('max_health', 'maxHealth', 0.0, None, True),
    ('max_regen_health', 'maxRegenHealth', 0.0, None, False),
    ('hysteresis_health', 'hysteresisHealth', 0.0, None, False),
    ('health_regen_per_sec', 'healthRegenPerSec', 0.0, None, False),
    ('repair_cost', 'repairCost', 0.0, None, False),
    ('fire_starting_chance', 'fireStartingChance', 0.0, 1.0, False),
)


@dataclass(frozen=True)
class XmlSource:
    """A game-relative XML location and its role in an overlay."""
    resource: str
    xpath: str
    layer: str


@dataclass(frozen=True)
class Provenance:
    """Original and shared inputs, in the order used to form selected data."""
    original: XmlSource | None
    shared: XmlSource | None
    inherited: XmlSource | None
    merged_selected: tuple[XmlSource, ...]


@dataclass(frozen=True)
class NumericField:
    """A scalar is absent when both value and source are ``None``."""
    value: float | None
    source: XmlSource | None


@dataclass(frozen=True)
class TextField:
    value: str | None
    source: XmlSource | None


@dataclass(frozen=True)
class BooleanField:
    value: bool | None
    source: XmlSource | None


@dataclass(frozen=True)
class ModuleMetadata:
    """One selected component or nested XML health record, without effects."""
    name: str
    tag: str | None
    provenance: Provenance
    max_health: NumericField
    max_regen_health: NumericField
    hysteresis_health: NumericField
    health_regen_per_sec: NumericField
    repair_cost: NumericField
    fire_starting_chance: NumericField


@dataclass(frozen=True)
class MaterialMetadata:
    """Raw local or common material fields.  No common record is applied here."""
    component: str
    name: str
    occurrence: int
    provenance: Provenance
    value: NumericField
    extra: TextField
    vehicle_damage_factor: NumericField
    chance_to_hit_by_projectile: NumericField
    chance_to_hit_by_explosion: NumericField
    increased_vulnerability_factor: NumericField
    continue_trace_if_no_hit: BooleanField
    damage_kind: TextField


@dataclass(frozen=True)
class ExtraIdentifier:
    name: str
    index: int
    definition: str | None
    source: XmlSource


@dataclass(frozen=True)
class CrewMember:
    holder: str
    roles: tuple[str, ...]
    index: int
    source: XmlSource
    trailing_text: str | None


@dataclass(frozen=True)
class VehicleModuleMetadata:
    nation: str
    name: str
    configuration: VehicleConfiguration
    modules: tuple[ModuleMetadata, ...]
    local_materials: tuple[MaterialMetadata, ...]
    common_materials: tuple[MaterialMetadata, ...]
    extras: tuple[ExtraIdentifier, ...]
    min_fire_starting_damage: NumericField
    crew: tuple[CrewMember, ...]

    def module(self, name):
        """Return one named raw record, or ``None`` if the name is unknown."""
        return next((item for item in self.modules if item.name == name), None)


@dataclass(frozen=True)
class _ResolvedNode:
    merged: ET.Element | None
    original: ET.Element | None
    original_source: XmlSource | None
    shared: ET.Element | None
    shared_source: XmlSource | None
    inherited: ET.Element | None
    inherited_source: XmlSource | None
    provenance: Provenance


def _resource(*parts):
    return str(_RESOURCE_ROOT.joinpath(*parts))


def _child_source(source, name):
    if source is None:
        return None
    return XmlSource(source.resource, f'{source.xpath}/{name}', source.layer)


def _provenance(original, shared, inherited=None):
    return Provenance(original, shared, inherited,
                      tuple(source for source in (shared, inherited, original) if source is not None))


def _children(node, tag):
    return () if node is None else tuple(child for child in node if child.tag == tag)


def _one_child(node, tag, context):
    values = _children(node, tag)
    if len(values) > 1:
        raise ValueError(f'Duplicate XML node: {context}/{tag}')
    return values[0] if values else None


def _effective_elements(original, original_source, shared, shared_source, tag, *,
                        inherited=None, inherited_source=None):
    """Apply the scalar part of ``overlay`` while retaining its source node."""
    selected, selected_source = (), None
    # This follows overlay's ``if local.text`` rule exactly: empty/None text
    # inherits, whitespace overrides, and ``shared`` inherits when a lower
    # layer exists.  The gun path has a third, turret-shared layer.
    for node, source in ((shared, shared_source), (inherited, inherited_source),
                         (original, original_source)):
        values = _children(node, tag)
        if not values:
            continue
        if not selected or any(item.text and item.text.strip() != 'shared' for item in values):
            selected, selected_source = values, source
    return selected, selected_source


def _leaf_text(element, context, *, allow_empty=False, allow_children=False):
    if element.attrib or (len(element) and not allow_children):
        raise ValueError(f'Invalid scalar XML field: {context}')
    value = (element.text or '').strip()
    if not value and not allow_empty:
        raise ValueError(f'Missing scalar XML value: {context}')
    return value


def _numeric_elements(elements, source, context, *, minimum=None, maximum=None,
                      strictly_positive=False, allow_empty=False, allow_children=False):
    if not elements:
        return NumericField(None, None)
    values = []
    for element in elements:
        text = _leaf_text(element, context, allow_empty=allow_empty, allow_children=allow_children)
        if not text:
            values.append(None)
            continue
        try:
            value = float(text)
        except ValueError as error:
            raise ValueError(f'Invalid numeric XML field: {context}') from error
        if not math.isfinite(value):
            raise ValueError(f'Non-finite numeric XML field: {context}')
        if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
            raise ValueError(f'Out-of-range numeric XML field: {context}')
        if strictly_positive and value <= 0:
            raise ValueError(f'Non-positive numeric XML field: {context}')
        values.append(value)
    if any(value is None for value in values):
        if any(value is not None for value in values):
            raise ValueError(f'Conflicting repeated XML field: {context}')
        return NumericField(None, None)
    if len(set(values)) != 1:
        raise ValueError(f'Conflicting repeated XML field: {context}')
    return NumericField(values[0], source)


def _numeric_field(original, original_source, shared, shared_source, tag, *,
                   minimum=None, maximum=None, strictly_positive=False, allow_empty=False,
                   inherited=None, inherited_source=None, merged=None):
    source_elements, parent_source = _effective_elements(
        original, original_source, shared, shared_source, tag,
        inherited=inherited, inherited_source=inherited_source)
    elements = _children(merged, tag) if merged is not None else source_elements
    return _numeric_elements(elements, _child_source(parent_source, tag), tag,
                             minimum=minimum, maximum=maximum,
                             strictly_positive=strictly_positive, allow_empty=allow_empty)


def _text_field(original, original_source, shared, shared_source, tag, *,
                inherited=None, inherited_source=None, merged=None):
    source_elements, parent_source = _effective_elements(
        original, original_source, shared, shared_source, tag,
        inherited=inherited, inherited_source=inherited_source)
    elements = _children(merged, tag) if merged is not None else source_elements
    if not elements:
        return TextField(None, None)
    values = tuple(_leaf_text(element, tag) for element in elements)
    if len(set(values)) != 1:
        raise ValueError(f'Conflicting repeated XML field: {tag}')
    return TextField(values[0], _child_source(parent_source, tag))


def _boolean_field(original, original_source, shared, shared_source, tag, *,
                   inherited=None, inherited_source=None, merged=None):
    source_elements, parent_source = _effective_elements(
        original, original_source, shared, shared_source, tag,
        inherited=inherited, inherited_source=inherited_source)
    elements = _children(merged, tag) if merged is not None else source_elements
    if not elements:
        return BooleanField(None, None)
    values = []
    for element in elements:
        text = _leaf_text(element, tag).lower()
        if text not in ('true', 'false'):
            raise ValueError(f'Invalid boolean XML field: {tag}')
        values.append(text == 'true')
    if len(set(values)) != 1:
        raise ValueError(f'Conflicting repeated XML field: {tag}')
    return BooleanField(values[0], _child_source(parent_source, tag))


def _nested(parent, tag):
    original = _one_child(parent.original, tag, parent.original_source.xpath if parent.original_source else tag)
    shared = _one_child(parent.shared, tag, parent.shared_source.xpath if parent.shared_source else tag)
    inherited = _one_child(parent.inherited, tag, parent.inherited_source.xpath if parent.inherited_source else tag)
    merged = _one_child(parent.merged, tag, tag)
    original_source = _child_source(parent.original_source, tag) if original is not None else None
    shared_source = _child_source(parent.shared_source, tag) if shared is not None else None
    inherited_source = _child_source(parent.inherited_source, tag) if inherited is not None else None
    return _ResolvedNode(merged, original, original_source, shared, shared_source,
                         inherited, inherited_source,
                         _provenance(original_source, shared_source, inherited_source))


def _module(name, resolved):
    fields = {}
    for attribute, tag, minimum, maximum, strictly_positive in _MODULE_FIELDS:
        fields[attribute] = _numeric_field(resolved.original, resolved.original_source,
                                            resolved.shared, resolved.shared_source, tag,
                                            minimum=minimum, maximum=maximum,
                                            strictly_positive=strictly_positive,
                                            inherited=resolved.inherited,
                                            inherited_source=resolved.inherited_source,
                                            merged=resolved.merged)
    return ModuleMetadata(name, resolved.merged.tag if resolved.merged is not None else None,
                          resolved.provenance, **fields)


def _material_record(component, name, occurrence, merged, original, original_source, shared, shared_source,
                     inherited=None, inherited_source=None):
    provenance = _provenance(original_source, shared_source, inherited_source)
    effective, source = _effective_material(original, original_source, shared, shared_source,
                                            inherited, inherited_source)
    value = _numeric_elements((merged,) if merged is not None else effective, source, name,
                              minimum=0.0, allow_empty=True,
                              allow_children=True)
    return MaterialMetadata(
        component, name, occurrence, provenance, value,
        _text_field(original, original_source, shared, shared_source, 'extra',
                    inherited=inherited, inherited_source=inherited_source, merged=merged),
        _numeric_field(original, original_source, shared, shared_source, 'vehicleDamageFactor',
                       inherited=inherited, inherited_source=inherited_source, merged=merged),
        _numeric_field(original, original_source, shared, shared_source, 'chanceToHitByProjectile',
                       minimum=0.0, maximum=1.0,
                       inherited=inherited, inherited_source=inherited_source, merged=merged),
        _numeric_field(original, original_source, shared, shared_source, 'chanceToHitByExplosion',
                       minimum=0.0, maximum=1.0,
                       inherited=inherited, inherited_source=inherited_source, merged=merged),
        _numeric_field(original, original_source, shared, shared_source, 'increasedVulnerabilityFactor',
                       inherited=inherited, inherited_source=inherited_source, merged=merged),
        _boolean_field(original, original_source, shared, shared_source, 'continueTraceIfNoHit',
                       inherited=inherited, inherited_source=inherited_source, merged=merged),
        _text_field(original, original_source, shared, shared_source, 'damageKind',
                    inherited=inherited, inherited_source=inherited_source, merged=merged))


def _effective_material(original, original_source, shared, shared_source, inherited, inherited_source):
    """Select a material text node exactly as overlay selects its text."""
    selected, selected_source = (), None
    for node, source in ((shared, shared_source), (inherited, inherited_source),
                         (original, original_source)):
        if node is None:
            continue
        if not selected or (node.text and node.text.strip() != 'shared'):
            selected, selected_source = (node,), source
    return selected, selected_source


def _materials_for_component(component, resolved):
    merged_armor = _one_child(resolved.merged, 'armor', f'{component}/merged')
    if merged_armor is None:
        return ()
    original_armor = _one_child(resolved.original, 'armor', f'{component}/original')
    shared_armor = _one_child(resolved.shared, 'armor', f'{component}/shared')
    inherited_armor = _one_child(resolved.inherited, 'armor', f'{component}/inherited')
    original_armor_source = (_child_source(resolved.original_source, 'armor')
                             if original_armor is not None else None)
    shared_armor_source = (_child_source(resolved.shared_source, 'armor')
                           if shared_armor is not None else None)
    inherited_armor_source = (_child_source(resolved.inherited_source, 'armor')
                              if inherited_armor is not None else None)
    occurrences = {}
    records = []
    for material in merged_armor:
        name = material.tag
        occurrence = occurrences.get(name, 0) + 1
        occurrences[name] = occurrence
        original_values = _children(original_armor, name)
        shared_values = _children(shared_armor, name)
        inherited_values = _children(inherited_armor, name)
        original = original_values[occurrence-1] if len(original_values) >= occurrence else None
        shared = shared_values[occurrence-1] if len(shared_values) >= occurrence else None
        inherited = inherited_values[occurrence-1] if len(inherited_values) >= occurrence else None
        original_source = (_child_source(original_armor_source,
                                         f'{name}[{occurrence}]' if len(original_values) > 1 else name)
                           if original is not None else None)
        shared_source = (_child_source(shared_armor_source,
                                       f'{name}[{occurrence}]' if len(shared_values) > 1 else name)
                         if shared is not None else None)
        inherited_source = (_child_source(inherited_armor_source,
                                          f'{name}[{occurrence}]' if len(inherited_values) > 1 else name)
                            if inherited is not None else None)
        records.append(_material_record(component, name, occurrence, material, original, original_source,
                                        shared, shared_source, inherited, inherited_source))
    return tuple(records)


def _common_materials(root, source):
    materials = _one_child(root, 'materials', 'common/vehicle')
    if materials is None:
        raise ValueError('Missing common material catalog')
    values = []
    seen = set()
    for material in materials:
        if material.tag in seen:
            raise ValueError(f'Duplicate common material: {material.tag}')
        seen.add(material.tag)
        material_source = _child_source(source, f'materials/{material.tag}')
        values.append(_material_record('common', material.tag, 1, material, material, material_source,
                                       None, None))
    return tuple(values)


def _extras(root, source):
    extras = _one_child(root, 'extras', 'common/vehicle')
    if extras is None:
        raise ValueError('Missing XML extras catalog')
    values, names, indexes = [], set(), set()
    for extra in extras:
        if extra.tag in names or set(extra.attrib) != {'index'}:
            raise ValueError(f'Invalid XML extra: {extra.tag}')
        try:
            index = int(extra.attrib['index'])
        except ValueError as error:
            raise ValueError(f'Invalid XML extra index: {extra.tag}') from error
        if index < 0 or index in indexes:
            raise ValueError(f'Duplicate or negative XML extra index: {extra.tag}')
        names.add(extra.tag)
        indexes.add(index)
        values.append(ExtraIdentifier(extra.tag, index, (extra.text or '').strip() or None,
                                      _child_source(source, f'extras/{extra.tag}')))
    return tuple(values)


def _crew(root, source):
    groups = _children(root, 'crew')
    if len(groups) != 1 or not len(groups[0]):
        raise ValueError('Missing or unsupported vehicle crew')
    crew = groups[0]
    if crew.attrib or (crew.text or '').strip():
        raise ValueError('Unsupported vehicle crew layout')
    known = {'commander', 'gunner', 'driver', 'loader', 'radioman'}
    members, occurrences = [], {}
    for index, member in enumerate(crew):
        if member.tag not in known or member.attrib or len(member):
            raise ValueError(f'Unsupported vehicle crew member: {member.tag}')
        roles = (member.tag, *(member.text or '').split())
        if any(role not in known for role in roles) or len(set(roles)) != len(roles):
            raise ValueError(f'Invalid vehicle crew roles: {member.tag}')
        occurrence = occurrences.get(member.tag, 0) + 1
        occurrences[member.tag] = occurrence
        trailing_text = member.tail if (member.tail or '').strip() else None
        members.append(CrewMember(member.tag, tuple(roles), index,
                                  _child_source(source, f'crew/{member.tag}[{occurrence}]'),
                                  trailing_text))
    return tuple(members)


def load_module_metadata(game: Path, nation='usa', name='M2_lt', configuration=None):
    """Load selected raw module metadata using ``load_vehicle`` configuration.

    ``configuration`` accepts the same explicit compatible tags as
    :func:`load_vehicle`.  The returned configuration is always the resolved
    selection, including stock defaults.
    """
    if configuration is not None and not isinstance(configuration, VehicleConfiguration):
        raise ValueError('Expected VehicleConfiguration')
    selected = load_vehicle(Path(game), nation, name, configuration)
    return _load_module_metadata(Path(game), nation, name, selected.configuration)


@lru_cache(maxsize=32)
def _load_module_metadata(game, nation, name, configuration):
    root = Path(game)/_RESOURCE_ROOT

    def xml(path):
        return ET.fromstring(read(path))

    vehicle_resource = _resource(nation, f'{name}.xml.dvpl')
    vehicle_source = XmlSource(vehicle_resource, '.', 'vehicle')
    vehicle = xml(root/nation/f'{name}.xml.dvpl')
    catalogs = {}

    def catalog(kind):
        if kind not in catalogs:
            catalogs[kind] = xml(root/nation/'components'/f'{kind}.xml.dvpl')
        return catalogs[kind]

    def configured(parent, path, tag):
        group = _one_child(parent, path, f'vehicle/{path}')
        item = _one_child(group, tag, f'vehicle/{path}')
        if item is None:
            raise ValueError(f'Incompatible vehicle component: {path}/{tag}')
        return item

    def resolve_component(kind, original, original_source):
        shared = None
        shared_source = None
        if (original.text or '').strip() == 'shared':
            shared = _one_child(catalog(kind).find('shared'), original.tag, f'{kind}/shared')
            if shared is None:
                raise ValueError(f'Missing shared component: {kind}/{original.tag}')
            shared_source = XmlSource(_resource(nation, 'components', f'{kind}.xml.dvpl'),
                                      f'shared/{original.tag}', 'shared')
        merged = resolve_gun(shared, original) if kind == 'guns' else overlay(shared, original)
        return _ResolvedNode(merged, original, original_source, shared, shared_source,
                             None, None, _provenance(original_source, shared_source))

    chassis_original = configured(vehicle, 'chassis', configuration.chassis)
    turret_original = configured(vehicle, 'turrets0', configuration.turret)
    engine_original = configured(vehicle, 'engines', configuration.engine)
    fuel_original = configured(vehicle, 'fuelTanks', configuration.fuel_tank)
    radio_original = configured(vehicle, 'radios', configuration.radio)
    hull_original = _one_child(vehicle, 'hull', 'vehicle')
    if hull_original is None:
        raise ValueError('Missing hull')

    chassis = resolve_component('chassis', chassis_original,
                                _child_source(vehicle_source, f'chassis/{configuration.chassis}'))
    turret = resolve_component('turrets', turret_original,
                               _child_source(vehicle_source, f'turrets0/{configuration.turret}'))
    engine = resolve_component('engines', engine_original,
                               _child_source(vehicle_source, f'engines/{configuration.engine}'))
    fuel = resolve_component('fuelTanks', fuel_original,
                             _child_source(vehicle_source, f'fuelTanks/{configuration.fuel_tank}'))
    radio = resolve_component('radios', radio_original,
                              _child_source(vehicle_source, f'radios/{configuration.radio}'))
    hull = _ResolvedNode(hull_original, hull_original, _child_source(vehicle_source, 'hull'),
                         None, None, None, None,
                         _provenance(_child_source(vehicle_source, 'hull'), None))

    local_guns = _one_child(turret_original, 'guns', 'vehicle/turret')
    inherited_guns = _one_child(turret.shared, 'guns', 'shared/turret')
    gun_original = _one_child(local_guns, configuration.gun, 'vehicle/turret/guns')
    gun_inherited = _one_child(inherited_guns, configuration.gun, 'shared/turret/guns')
    gun_original_source = (_child_source(_child_source(turret.original_source, 'guns'), configuration.gun)
                           if gun_original is not None else None)
    gun_inherited_source = (_child_source(_child_source(turret.shared_source, 'guns'), configuration.gun)
                            if gun_inherited is not None else None)
    if gun_original is None and gun_inherited is None:
        raise ValueError(f'Incompatible vehicle component: gun/{configuration.gun}')
    turret_gun = (overlay(gun_inherited, gun_original) if gun_original is not None
                  else overlay(None, gun_inherited))
    gun_shared = None
    gun_shared_source = None
    if (turret_gun.text or '').strip() == 'shared':
        gun_shared = _one_child(catalog('guns').find('shared'), configuration.gun, 'guns/shared')
        if gun_shared is None:
            raise ValueError(f'Missing shared component: guns/{configuration.gun}')
        gun_shared_source = XmlSource(_resource(nation, 'components', 'guns.xml.dvpl'),
                                      f'shared/{configuration.gun}', 'shared')
    gun = _ResolvedNode(resolve_gun(gun_shared, turret_gun), gun_original, gun_original_source,
                        gun_shared, gun_shared_source, gun_inherited, gun_inherited_source,
                        _provenance(gun_original_source, gun_shared_source, gun_inherited_source))

    common_resource = _resource('common', 'vehicle.xml.dvpl')
    common_source = XmlSource(common_resource, '.', 'common')
    common = xml(root/'common/vehicle.xml.dvpl')
    misc = _one_child(common, 'miscParams', 'common/vehicle')
    if misc is None:
        raise ValueError('Missing common miscParams')
    min_fire = _numeric_field(misc, _child_source(common_source, 'miscParams'), None, None,
                              'minFireStartingDamage')

    components = (('hull', hull), ('chassis', chassis), ('turret', turret), ('gun', gun),
                  ('engine', engine), ('fuelTank', fuel), ('radio', radio))
    modules = tuple(_module(component, resolved) for component, resolved in components)
    modules += (_module('ammoBay', _nested(hull, 'ammoBayHealth')),
                _module('turretRotator', _nested(turret, 'turretRotatorHealth')),
                _module('surveyingDevice', _nested(turret, 'surveyingDeviceHealth')))
    local_materials = tuple(record for component, resolved in components
                            for record in _materials_for_component(component, resolved))
    return VehicleModuleMetadata(nation, name, configuration, modules, local_materials,
                                 _common_materials(common, common_source), _extras(common, common_source),
                                 min_fire, _crew(vehicle, vehicle_source))


load_module_metadata.cache_clear = _load_module_metadata.cache_clear
