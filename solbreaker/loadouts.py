"""Immutable loadout catalog (optional devices, provisions, consumables) from pinned XML.

Data only: no battle effects, storage or wire serialization. Script values stay
strings exactly as written in the XML; compact descriptor kinds and prices are not
interpreted. Only the ``common`` item files are read (never boss/mad/etc. modes).
"""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import xml.etree.ElementTree as ET

from .dvpl import read
from .tech_tree import lookup_vehicle, load_tech_tree

OPTIONAL_DEVICE, PROVISION, CONSUMABLE = 'optional_device', 'provision', 'consumable'
COMPATIBLE, INCOMPATIBLE, UNSUPPORTED = 'compatible', 'incompatible', 'unsupported'

_SOURCES = {
    OPTIONAL_DEVICE: 'vehicles/common/optional_devices.xml.dvpl',
    PROVISION: 'vehicles/common/provisions/common.xml.dvpl',
    CONSUMABLE: 'vehicles/common/consumables/common.xml.dvpl',
}
_CORE = {'id', 'userString', 'description', 'icon', 'category', 'tags', 'vehicleFilter', 'script'}
_CATEGORIES = ('attack', 'defence', 'specialization')
# extendedTags=clip is additionally supported when the installed clip count is supplied.
# All other extended tags and fields outside this list stay unsupported.
FILTER_COVERED = ('nations', 'vehicle/name', 'vehicle/minLevel', 'vehicle/maxLevel')
FILTER_UNCOVERED = ('vehicle/tags', 'vehicle/extendedTags', 'geometryFilter')


def _node(element):
    """(tag, stripped text, children) of an element, recursively."""
    return (element.tag, (element.text or '').strip(), tuple(_node(c) for c in element))


def _leaves(element, prefix=''):
    """Ordered (path, text) for every leaf below element; repeated paths are kept."""
    out = []
    for child in element:
        path = prefix + child.tag
        if len(child):
            out.extend(_leaves(child, path + '/'))
        else:
            out.append((path, (child.text or '').strip()))
    return tuple(out)


@dataclass(frozen=True)
class ItemSpec:
    tag: str
    ident: int
    kind: str
    script: str | None  # script class name, e.g. 'Afterburning'
    tags: tuple[str, ...]
    category: str | None
    parameters: tuple[tuple[str, str], ...]  # (leaf path below <script>, text)
    filter_rules: tuple = ()  # normalized <vehicleFilter> children: (tag, text, children)
    user_string: str = ''
    description: str = ''
    icon: str = ''
    extras: tuple[tuple[str, str], ...] = ()  # other children (price, display_params, geometryFilter, ...)


@dataclass(frozen=True)
class PresetSpec:
    name: str
    slots: tuple[tuple[int | None, int | None], ...]  # 9 pairs: level0..2 x attack/defence/specialization


@dataclass(frozen=True)
class LoadoutCatalog:
    game: Path
    optional_devices: tuple[ItemSpec, ...]
    provisions: tuple[ItemSpec, ...]
    consumables: tuple[ItemSpec, ...]
    presets: tuple[PresetSpec, ...]

    def _items(self, kind):
        try:
            return {OPTIONAL_DEVICE: self.optional_devices, PROVISION: self.provisions,
                    CONSUMABLE: self.consumables}[kind]
        except KeyError:
            raise ValueError('Unknown loadout item kind') from None

    def lookup(self, kind, ident):
        for item in self._items(kind):
            if item.ident == ident:
                return item
        raise ValueError(f'Unknown {kind}: {ident!r}')

    def preset(self, name):
        for preset in self.presets:
            if preset.name == name:
                return preset
        raise ValueError(f'Unknown equipment preset: {name!r}')

    def equipment_slots(self, preset):
        """9 ordered (device, device) ident pairs; None marks an explicitly empty slot."""
        return self.preset(preset).slots

    def vehicle_slots(self, nation, name):
        """Exact (consumableSlots, provisionSlots) from this vehicle's XML."""
        return _vehicle_slots(self.game, nation, name)

    def vehicle_preset(self, nation, name):
        """optDevicePreset named by the vehicle XML; a missing entry is not defaulted."""
        lookup_vehicle(self.game, nation, name)
        vehicle = ET.fromstring(read(self.game/'Data/XML/item_defs/vehicles'/nation/(name+'.xml.dvpl')))
        preset = vehicle.findtext('optDevicePreset')
        if not preset or not preset.strip():
            raise ValueError(f'Missing optDevicePreset: {nation}/{name}')
        self.preset(preset.strip())
        return preset.strip()

    def compatibility(self, kind, ident, nation, name, *, clip=None, ignore_geometry=False):
        """COMPATIBLE / INCOMPATIBLE for explicit filters, else UNSUPPORTED (never a broad allow).

        Include blocks (all fields AND-ed; ``name`` is an OR over ``nation:name`` tokens;
        minLevel/maxLevel inclusive on the tech tree tier) must match and no exclude
        block may match. ``clip`` is the installed gun's resolved clip count,
        not the vehicle's equipment preset or any other compatible gun.
        Omitted clip counts leave extendedTags=clip undecidable.
        """
        if clip is not None and (type(clip) is not int or clip < 1):
            raise ValueError('Installed clip count must be a positive integer')
        item = self.lookup(kind, ident)
        level = lookup_vehicle(self.game, nation, name).tier
        if not ignore_geometry and any(k.split('/')[0] == 'geometryFilter' for k, _ in item.extras):
            return UNSUPPORTED
        if not item.filter_rules:
            return COMPATIBLE
        blocks = [(tag, _match(children, nation, name, level, clip))
                  for tag, _, children in item.filter_rules]
        if any(tag not in ('include', 'exclude') for tag, _ in blocks):
            return UNSUPPORTED
        includes = [m for tag, m in blocks if tag == 'include']
        excludes = [m for tag, m in blocks if tag == 'exclude']
        if True in excludes:
            return INCOMPATIBLE
        if len(includes) != 1:
            return UNSUPPORTED
        if includes[0] is False:
            return INCOMPATIBLE
        return UNSUPPORTED if includes[0] is None or None in excludes else COMPATIBLE

    def provision_compatibility(self, ident, nation, name, *, clip=None):
        """Permit the pinned inert desert provision without guessing its map effect."""
        item = self.lookup(PROVISION, ident)
        geometry = tuple((k, v) for k, v in item.extras if k.split('/')[0] == 'geometryFilter')
        inert_desert = (item.ident == 113 and item.tag == 'desertPower' and
                        geometry == (('geometryFilter/include', 'desert'),))
        return self.compatibility(PROVISION, ident, nation, name, clip=clip,
                                  ignore_geometry=inert_desert)


@lru_cache(maxsize=1024)
def _vehicle_slots(game, nation, name):
    lookup_vehicle(game, nation, name)
    vehicle = ET.fromstring(read(Path(game)/'Data/XML/item_defs/vehicles'/nation/(name+'.xml.dvpl')))
    counts = []
    for field in ('consumableSlots', 'provisionSlots'):
        text = vehicle.findtext(field)
        if text is None or not text.strip().isdigit() or not 0 <= int(text) <= 3:
            raise ValueError(f'Missing or invalid {field}: {nation}/{name}')
        counts.append(int(text))
    return tuple(counts)


def _match(children, nation, name, level, clip=None):
    """True/False when decided, None when a field is outside FILTER_COVERED. AND over fields."""
    results = []
    for tag, text, sub in children:
        if tag == 'nations' and not sub:
            results.append(nation in text.split())
        elif tag == 'vehicle' and sub:
            results.append(_match_vehicle(sub, nation, name, level, clip))
        else:
            results.append(None)
    if False in results:
        return False
    return None if None in results else True


def _match_vehicle(fields, nation, name, level, clip=None):
    results = []
    for tag, text, sub in fields:
        if sub:
            results.append(None)
        elif tag == 'name':
            results.append(f'{nation}:{name}' in text.split())
        elif tag == 'extendedTags' and text.split() == ['clip']:
            results.append(None if clip is None else clip > 1)
        elif tag in ('minLevel', 'maxLevel'):
            try:
                bound = int(text)
            except ValueError:
                results.append(None)
                continue
            results.append(level >= bound if tag == 'minLevel' else level <= bound)
        else:
            results.append(None)
    if False in results:
        return False
    return None if None in results else True


def _items(root, kind):
    items, idents, tags = [], set(), set()
    for el in root:
        if el.tag == 'nextAvailableId':
            continue
        ident_text = el.findtext('id')
        if ident_text is None or not ident_text.strip().isdigit():
            raise ValueError(f'Invalid {kind} id: {el.tag}')
        ident = int(ident_text)
        if ident in idents or el.tag in tags:
            raise ValueError(f'Duplicate {kind}: {el.tag}')
        idents.add(ident)
        tags.add(el.tag)
        script = el.find('script')
        filt = el.find('vehicleFilter')
        extras = tuple(leaf for c in el if c.tag not in _CORE for leaf in _leaves(_wrap(c)))
        items.append(ItemSpec(
            el.tag, ident, kind,
            (script.text or '').strip() or None if script is not None else None,
            tuple((el.findtext('tags') or '').split()),
            (el.findtext('category') or '').strip() or None,
            _leaves(script) if script is not None else (),
            tuple(_node(c) for c in filt) if filt is not None else (),
            (el.findtext('userString') or '').strip(), (el.findtext('description') or '').strip(),
            (el.findtext('icon') or '').strip(), extras))
    return tuple(items)


def _wrap(child):
    holder = ET.Element('x')
    holder.append(child)
    return holder


def _presets(root, devices):
    known = {d.tag: d.ident for d in devices}
    section = root.find('presets')
    if section is None:
        raise ValueError('Missing equipment presets')
    result = []
    for preset in section:
        if [c.tag for c in preset] != ['level0', 'level1', 'level2']:
            raise ValueError(f'Invalid equipment preset: {preset.tag}')
        slots = []
        for level in preset:
            if [c.tag for c in level] != list(_CATEGORIES):
                raise ValueError(f'Invalid equipment preset: {preset.tag}')
            for category in level:
                if [c.tag for c in category] != ['device0', 'device1']:
                    raise ValueError(f'Invalid equipment preset: {preset.tag}')
                pair = []
                for device in category:
                    tag = (device.text or '').strip()
                    if tag and tag not in known:
                        raise ValueError(f'Unknown optional device in {preset.tag}: {tag}')
                    pair.append(known[tag] if tag else None)
                slots.append(tuple(pair))
        result.append(PresetSpec(preset.tag, tuple(slots)))
    if len({p.name for p in result}) != len(result):
        raise ValueError('Duplicate equipment preset')
    return tuple(result)


@lru_cache(maxsize=4)
def _load(game: Path):
    root = game/'Data/XML/item_defs'
    docs = {kind: ET.fromstring(read(root/path)) for kind, path in _SOURCES.items()}
    devices = _items(docs[OPTIONAL_DEVICE], OPTIONAL_DEVICE)
    slots = ET.fromstring(read(root/'vehicles/common/optional_device_slots.xml.dvpl'))
    return LoadoutCatalog(game, devices, _items(docs[PROVISION], PROVISION),
                          _items(docs[CONSUMABLE], CONSUMABLE), _presets(slots, devices))


def load_catalog(game) -> LoadoutCatalog:
    return _load(Path(game))


def native_inventory(game, vehicles):
    """Installed IDs for InventoryData and StatsData, only for saved loadouts.

    Native common-nation items use nation15: provision kind13, consumable kind11.
    Equipment state is keyed by vehicle type CD, then row and group; -1 means
    locked, 0/1 chooses a device from that vehicle's XML preset (0x85d3f0).
    """
    catalog = load_catalog(game)
    inventory = dict(eqs={}, prov={})
    states = {}
    tags = {kind: {i.tag: i for i in catalog._items(kind)}
            for kind in (PROVISION, CONSUMABLE, OPTIONAL_DEVICE)}
    for vehicle in vehicles:
        loadout = vehicle.get('loadout')
        if loadout is None:
            continue
        for category, kind, key, low in (('provisions', PROVISION, 'prov', 0xfd),
                                         ('consumables', CONSUMABLE, 'eqs', 0xfb)):
            inventory[key][vehicle['id']] = [
                (tags[kind][tag].ident << 8) | low if tag else 0 for tag in loadout[category]]
        nation, name = vehicle_key(catalog.game, vehicle['type_cd'])
        preset = catalog.equipment_slots(catalog.vehicle_preset(nation, name))
        state = {}
        for slot in range(9):
            tag = loadout['devices'][slot]
            selected = (preset[slot].index(tags[OPTIONAL_DEVICE][tag].ident) if tag else 0)
            state.setdefault(slot // 3, {})[slot % 3] = (
                selected if loadout['unlocked'] >> slot & 1 else -1)
        states[vehicle['type_cd']] = state
    return inventory, states


@lru_cache(maxsize=4)
def _nation_names(game):
    root = ET.fromstring(read(Path(game)/'Data/XML/item_defs/vehicles/common/nations.xml.dvpl'))
    return {int(n.findtext('id')): n.tag for n in root if (n.findtext('id') or '').isdigit()}


def _nation_name(game, ident):
    return _nation_names(game)[ident]


def vehicle_key(game, type_cd):
    nation = _nation_name(game, (type_cd >> 4) & 15)
    for vehicle in load_tech_tree(game, nation):
        if vehicle.type_cd == type_cd:
            return nation, vehicle.key.split('/')[1]
    raise ValueError('Unknown vehicle type')
