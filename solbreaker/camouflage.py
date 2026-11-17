"""All-season paint from the player's pinned game resources."""
from functools import lru_cache
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from .dvpl import read
from .garage import GarageStore
from .loadouts import vehicle_key
from .storage import AccountStore, account_access

_VEHICLE_FIELDS = {'name', 'tags', 'minLevel', 'maxLevel'}


def _known_filter(block):
    if block.tag not in ('include', 'exclude'):
        return False
    for child in block:
        if child.tag == 'nations' and not len(child):
            continue
        if child.tag != 'vehicle' or not len(child):
            return False
        if any(len(field) or field.tag not in _VEHICLE_FIELDS for field in child):
            return False
    return bool(len(block))


def _matches(block, nation, name, tags, level):
    def field_matches(field):
        values = (field.text or '').split()
        if field.tag == 'name':
            return f'{nation}:{name}' in values
        if field.tag == 'tags':
            return bool(tags.intersection(values))
        if field.tag == 'minLevel':
            return level >= int(field.text)
        if field.tag == 'maxLevel':
            return level <= int(field.text)
        return False

    return all(
        nation in (child.text or '').split() if child.tag == 'nations'
        else all(field_matches(field) for field in child)
        for child in block
    )


@lru_cache(maxsize=4)
def _paint_catalog(game):
    root = ET.fromstring(read(game / 'Data/XML/item_defs/vehicles/common/camouflages.xml.dvpl'))
    paints = []
    for paint in root.find('camouflages'):
        if (paint.findtext('script', '').strip() != 'Camouflage'
                or set(paint.findtext('kind', '').split()) != {'summer', 'winter', 'desert'}
                or paint.findtext('notInShop', '').strip() != 'false'):
            continue
        restrictions = paint.findall('vehicleFilter/*')
        if any(not _known_filter(block) for block in restrictions):
            continue
        # ItemCompactDescr: kind 8 (camouflage), common nation 15.
        compact_id = (int(paint.findtext('id')) << 8) | 0xf8
        includes = tuple(block for block in restrictions if block.tag == 'include')
        excludes = tuple(block for block in restrictions if block.tag == 'exclude')
        paints.append((paint.tag, compact_id, includes, excludes))
    return tuple(sorted(paints, key=lambda paint: paint[0]))


def unlocked_paints(game):
    return [compact_id for _, compact_id, _, _ in _paint_catalog(game)]


def compatible_paints(game, type_cd):
    nation, name = vehicle_key(game, type_cd)
    vehicles = game / 'Data/XML/item_defs/vehicles'
    tank = ET.fromstring(read(vehicles / nation / 'list.xml.dvpl')).find(name)
    tags = set(tank.findtext('tags', '').split())
    tier = int(tank.findtext('level'))
    paints = {}
    for tag, compact_id, includes, excludes in _paint_catalog(game):
        if includes and not any(_matches(block, nation, name, tags, tier) for block in includes):
            continue
        if any(_matches(block, nation, name, tags, tier) for block in excludes):
            continue
        paints[tag] = compact_id
    return paints


def configure(state, account_id, inventory_id, *, paint=None, clear=False, vehicles=False):
    manifest = json.loads((state / 'manifest.json').read_text())
    private = state / 'private'
    with account_access(private):
        store = AccountStore(private)
        try:
            garage = GarageStore(store)
            if account_id is None:
                ids = [row[0] for row in store.db.execute(
                    'SELECT account_id FROM garage_profiles ORDER BY account_id')]
                if len(ids) != 1:
                    choices = ', '.join(map(str, ids)) or 'none'
                    raise ValueError(f'Specify --account-id. Initialized garages: {choices}')
                account_id = ids[0]
            snapshot = garage.snapshot(account_id)
            game = Path(manifest['game'])
            if vehicles:
                result = []
                for v in snapshot['vehicles']:
                    try:
                        name = '/'.join(vehicle_key(game, v['type_cd']))
                    except (KeyError, ValueError):
                        name = None
                    result.append({'vehicle': v['id'], 'name': name, 'type_cd': v['type_cd']})
                return result
            if inventory_id is None:
                inventory_id = snapshot['selected_id']
            vehicle = next((v for v in snapshot['vehicles'] if v['id'] == inventory_id), None)
            if vehicle is None:
                raise ValueError('Vehicle is not in the garage')
            paints = compatible_paints(game, vehicle['type_cd'])
            if paint is not None and paint not in paints:
                raise ValueError('Unknown or incompatible paint. Run the command without --set to list choices')
            cd = vehicle.get('camouflage', 0)
            if paint is not None or clear:
                cd = 0 if clear else paints[paint]
                garage.set_camouflage(account_id, inventory_id, cd)
            return {'account_id': account_id, 'vehicle': inventory_id,
                    'name': '/'.join(vehicle_key(game, vehicle['type_cd'])),
                    'camouflage': next((tag for tag, ident in paints.items() if ident == cd), None),
                    'available': list(paints)}
        finally:
            store.close()
