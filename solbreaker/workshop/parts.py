"""Fit donor body meshes, armor and attachment points to a Workshop project."""
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np

from .. import dvpl
from ..tech_tree import lookup_vehicle
from ..vehicle_data import overlay, resolve_gun
from .project import DONOR_PARTS, PARTS
from .models import VISUAL_VERSIONS, read_asset
from .running_gear import ClientScene, name_of, scene_parts, transplant_nodes


@lru_cache(maxsize=16)
def donor_parts(game, key):
    from .package import XML, gun_node, preview_pair, stock_assets
    nation, vehicle = key.split('/')
    catalog = game/'Data'/XML/nation
    document = ET.fromstring(dvpl.read(catalog/(vehicle+'.xml.dvpl')))
    stock = lookup_vehicle(game, nation, vehicle).stock
    hull = document.find('hull')
    turret = document.find('turrets0/'+stock.turret)
    gun = turret.find('guns/'+stock.gun)
    chassis = document.find('chassis/'+stock.chassis)

    def resolved(node, kind):
        if (node.text or '').strip() != 'shared':
            return node
        definitions = ET.fromstring(dvpl.read(catalog/'components'/(kind+'.xml.dvpl')))
        shared = definitions.find('shared/'+node.tag)
        return resolve_gun(shared, node) if kind == 'guns' else overlay(shared, node)

    chassis = resolved(chassis, 'chassis')
    turret = resolved(turret, 'turrets')
    gun = resolved(gun, 'guns')
    visual, collision = stock_assets(game, key)
    shapes, markers = preview_pair(visual)
    turret_node = Path(turret.findtext('models/undamaged', 'turret_01')).stem.lower()
    barrel = gun_node(gun)
    muzzle = 'HP_gunFire_'+barrel.rsplit('_', 1)[-1]
    components = dict(zip(DONOR_PARTS, (hull, turret, gun)))
    nodes = dict(zip(DONOR_PARTS, ('hull', turret_node, barrel)))
    collision_nodes = {part: Path(node.findtext('hitTester/collisionModel', '')).stem.lower()
                       for part, node in components.items()}
    a = np.fromstring(chassis.findtext('hullPosition'), sep=' ')
    b = a + np.fromstring(hull.findtext('turretPositions/turret'), sep=' ')
    c = b + np.fromstring(turret.findtext('gunPosition'), sep=' ')
    return visual, collision, nodes, collision_nodes, components, shapes, dict(
        hull=a, turret=b, gun=c, muzzle=np.asarray(markers[muzzle]))


def select_part(game, project, part, key, fallback):
    """Re-seat downstream attachments with the selected part's mounting offsets."""
    if not project.stock_model and not project.original_points:
        project.original_points = fallback.copy()
    if key:
        project.part_donors[part] = key
    else:
        project.part_donors.pop(part, None)
    if key or project.stock_model:
        source = donor_parts(game, key or project.base_vehicle)[-1]
    else:
        source = {name: np.asarray(point) for name, point in project.original_points.items()}
    points = {name: np.asarray(point) for name, point in project.points.items()}
    index = PARTS.index(part)
    if part == 'hull':
        project.points['hull'] = tuple(map(float, source['hull']))
        points['hull'] = source['hull']
    child = PARTS[index+1]
    offset = points[part] + source[child] - source[part] - points[child]
    for name in PARTS[index+1:]:
        project.points[name] = tuple(map(float, points[name]+offset))


CATALOG = (
    ('ussr/T-54', 'Т-54', 'СССР'), ('ussr/T-44', 'Т-44', 'СССР'),
    ('ussr/T-34-85', 'Т-34-85', 'СССР'), ('ussr/R43_T-70_57', 'Т-70', 'СССР'),
    ('ussr/T-34', 'Т-34', 'СССР'), ('ussr/IS-3', 'ИС-3', 'СССР'),
    ('ussr/IS-7', 'ИС-7', 'СССР'), ('ussr/KV2', 'КВ-2', 'СССР'),
    ('ussr/Object_140', 'Объект 140', 'СССР'), ('usa/M4_Sherman', 'M4 Sherman', 'США'),
    ('usa/M48A1', 'M48 Patton', 'США'), ('usa/T29', 'T29', 'США'),
    ('germany/PzV', 'Panther', 'Германия'), ('germany/PzVI', 'Tiger I', 'Германия'),
    ('germany/E-50', 'E 50', 'Германия'), ('germany/E-100', 'E 100', 'Германия'),
    ('uk/GB19_Sherman_Firefly', 'Sherman Firefly', 'Великобритания'),
    ('uk/GB21_Cromwell', 'Cromwell', 'Великобритания'),
    ('china/Ch01_Type59', 'Type 59', 'Китай'), ('china/Ch22_113', '113', 'Китай'),
)


DONORS_EN = {
    'Т-54': 'T-54',
    'Т-44': 'T-44',
    'Т-34-85': 'T-34-85',
    'Т-70': 'T-70',
    'Т-34': 'T-34',
    'ИС-3': 'IS-3',
    'ИС-7': 'IS-7',
    'КВ-2': 'KV-2',
    'Объект 140': 'Object 140',
    'СССР': 'USSR',
    'США': 'USA',
    'Германия': 'Germany',
    'Великобритания': 'United Kingdom',
    'Китай': 'China',
}


def catalog_matches(query):
    alphabet = str.maketrans({'т': 't', 'к': 'k', 'в': 'v', 'и': 'i', 'с': 's', '‑': '-', '–': '-'})
    words = query.casefold().translate(alphabet).split()
    matches = []
    for key, title, nation in CATALOG:
        names = (key, title, nation, DONORS_EN.get(title, title), DONORS_EN.get(nation, nation))
        searchable = ' '.join(names).casefold().translate(alphabet)
        if all(word in searchable for word in words):
            matches.append((key, title, nation))
    return matches


def body_preview(game, project, visible, collision):
    visible, collision = visible.copy(), collision.copy()
    donors = dict.fromkeys(DONOR_PARTS, project.base_vehicle) if project.stock_model else {}
    for part, key in (donors | project.part_donors).items():
        _, hit, nodes, hit_nodes, _, meshes, points = donor_parts(game, key)
        target = 'hull' if part == 'hull' else part+'_01'
        delta = np.asarray(project.points[part])-points[part]
        visible[target] = meshes[nodes[part]]+delta
        hit_mesh = collision_preview(hit).get(hit_nodes[part])
        if hit_mesh is None:
            collision.pop(target, None)
        else:
            collision[target] = hit_mesh
    for target, delta in authored_offsets(project).items():
        if target in visible:
            visible[target] = visible[target]+delta
    return visible, collision


def authored_offsets(project):
    if project.stock_model or not project.original_points:
        return {}
    names = dict(hull='hull', turret='turret_01', gun='gun_01')
    return {names[part]: np.asarray(project.points[part])-project.original_points[part]
            for part in DONOR_PARTS if part not in project.part_donors}


@lru_cache(maxsize=16)
def collision_preview(scene):
    from .package import preview_pair
    return preview_pair(scene)[0]


def replace_armor(targets, source, part):
    armor = source.find('armor')
    if armor is None:
        raise ValueError('У выбранной детали нет карты брони')
    target = targets[part]
    previous = target.find('armor')
    if previous is not None:
        target.remove(previous)
    target.append(deepcopy(armor))


def merge_body(game, project, visual_pair, collision_pair, target_dir, hit_dir, visual_nodes,
               collision_nodes, targets):
    visual, hit = visual_pair, collision_pair
    for part in DONOR_PARTS:
        if part not in project.part_donors:
            continue
        source, hit_source, names, hit_names, components, _, points = donor_parts(game, project.part_donors[part])
        name, target = names[part], visual_nodes[part]
        delta = np.asarray(project.points[part])-points[part]
        donor_scene = ClientScene(read_asset(source.with_suffix('.sc2.dvpl')), versions=VISUAL_VERSIONS)
        available = {name_of(node) for node in scene_parts(donor_scene.fields[b'#hierarchy'].payload)}
        rename = {name: target}
        if part == 'gun':
            marker = 'HP_gunFire_'+name.rsplit('_', 1)[-1]
            rename[marker] = 'HP_gunFire_'+target.rsplit('_', 1)[-1]
        # Include common render layers for the selected body, while dropping
        # old overlays that would intersect the replacement.
        for candidate in sorted(available):
            if candidate.startswith(name+'_'):
                rename[candidate] = target+candidate[len(name):]
        visible_names = set(rename.values())
        visual = transplant_nodes(game, source, *visual, target_dir, rename,
                                  lambda node: node in visible_names or node.startswith(target+'_'),
                                  (np.ones(3), delta))
        native_hit = collision_nodes[part]
        hit = transplant_nodes(game, hit_source, *hit, hit_dir, {hit_names[part]: native_hit},
                               lambda node: node == native_hit, (np.ones(3), np.zeros(3)))
        replace_armor(targets, components[part], part)
    return visual, hit
