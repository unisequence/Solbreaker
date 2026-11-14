"""Build a vehicle package without bundling the donor's catalog or game files."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path, PurePosixPath
from shutil import copyfileobj
import tempfile
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

import numpy as np
import yaml

from .. import dava_scene, dvpl
from ..build_info import BUILD_ID
from ..client_ui import _StringsLoader
from ..connection import atomic_write
from ..vehicle_data import overlay, resolve_gun
from .models import MAX_ASSET, POINT_NODE, check_model, model_preview, move_muzzle, move_visual_node, read_asset, texture_dds
from .project import MAX_PROJECT_BYTES, VEHICLE_CLASSES, TankProject, asset_path, local_asset

XML = Path('XML/item_defs/vehicles')
PARAMETERS = Path('3d/Tanks/Parameters')
MAX_PACKAGE = 320 * 1024**2


def vehicle_paths(project):
    name, nation = project.native_name, project.nation
    return Path(f'3d/Tanks/{nation.upper()}/{name}/{name}'), Path(f'3d/Tanks/CollisionMeshes/{nation}-{name}')


def stock_assets(game, key):
    from .running_gear import donor_parameters
    visual, parameters = donor_parameters(game, key)
    collision = PurePosixPath(parameters['resourcesPath']['collisionMesh'])
    if collision.parts[:1] != ('CollisionMeshes',) or '..' in collision.parts or collision.suffix != '.sc2':
        raise ValueError('Не найдены коллизии базового танка')
    return visual, game/'Data/3d/Tanks'/collision


def preview_pair(scene):
    return model_preview(scene.with_suffix('.sc2.dvpl'), scene.with_suffix('.scg.dvpl'))


def gun_node(gun):
    path = gun.findtext('models/undamaged')
    return PurePosixPath(path).stem.lower() if path else 'gun_01'


def stock_parts(game, key):
    from ..tech_tree import lookup_vehicle
    nation, name = key.split('/')
    stock = lookup_vehicle(game, nation, name).stock
    xml = ET.fromstring(dvpl.read(game/'Data'/XML/nation/(name+'.xml.dvpl')))
    turret = xml.find('turrets0/'+stock.turret)
    turret_model = PurePosixPath(turret.findtext('models/undamaged', 'Turret_01.model')).stem.lower()
    return turret_model, gun_node(turret.find('guns/'+stock.gun))


def stock_project(game, key, title, project_id):
    from ..armor import load_battle_vehicle
    from ..tech_tree import lookup_vehicle
    nation, name = key.split('/')
    research = lookup_vehicle(game, nation, name)
    model = load_battle_vehicle(game, nation, name)
    xml = ET.fromstring(dvpl.read(game/'Data'/XML/nation/(name+'.xml.dvpl')))
    hull = xml.find('hull')
    chassis = xml.find('chassis/'+research.stock.chassis)
    turret = xml.find('turrets0/'+research.stock.turret)
    chassis_point = np.fromstring(chassis.findtext('hullPosition'), sep=' ')
    turret_point = chassis_point+np.fromstring(hull.findtext('turretPositions/turret'), sep=' ')
    gun_point = turret_point+np.fromstring(turret.findtext('gunPosition'), sep=' ')
    visual, _ = stock_assets(game, key)
    _, markers = preview_pair(visual)
    muzzle = 'HP_gunFire_'+gun_node(turret.find('guns/'+research.stock.gun)).rsplit('_', 1)[-1]
    points = dict(zip(('hull','turret','gun','muzzle'), (tuple(map(float, p))
        for p in (chassis_point, turret_point, gun_point, markers[muzzle]))))
    row=ET.fromstring(dvpl.read(game/'Data'/XML/nation/'list.xml.dvpl')).find(name)
    tags=row.findtext('tags','').split()
    return TankProject(id=project_id, name=title, base_vehicle=key, stock_model=True, tier=model.tier,
        vehicle_class=next(tag for tag in VEHICLE_CLASSES if tag in tags),
        hit_points=model.health, forward_kmh=round(model.drive.forward*3.6, 2),
        reverse_kmh=round(model.drive.reverse*3.6, 2), engine_hp=round(model.drive.engine_power_hp),
        reload_seconds=model.weapon.reload,
        shell_damage=round(next(shell.damage for shell in model.weapon.shells if shell.tag==research.stock.shell)),
        points=points)


def xml_bytes(node):
    return ET.tostring(node, encoding='utf-8', xml_declaration=True)


def set_text(root, path, value):
    for key in path.split('/'):
        child = root.find(key)
        root = child if child is not None else ET.SubElement(root, key)
    root.text = str(value)


def clear_unlocks(root):
    for parent in root.iter():
        for child in parent.findall('unlocks'):
            parent.remove(child)


def replace_selected(parent, name, item):
    if parent is None or parent.find(name) is None:
        raise ValueError(f'Donor has no selected {name}')
    parent[:] = [item]


def model_bounds(parts, names):
    triangles = [parts[name].reshape(-1, 3) for name in names]
    lo = np.min([mesh.min(0) for mesh in triangles], axis=0)
    hi = np.max([mesh.max(0) for mesh in triangles], axis=0)
    return lo, hi


def patch_attachment(parameters, parts, project, *, native_turret='turret_01', native_gun='gun_01'):
    collision = parameters.setdefault('collision', {})
    point_map = {'hull': ('hull',), 'chassis': ('chassis_track_L', 'chassis_track_R'),
                 native_turret: (native_turret,), native_gun: (native_gun,)}
    for name, meshes in point_map.items():
        origin = np.asarray(project.points.get(name.split('_')[0], (0., 0., 0.)))
        lo, hi = model_bounds(parts, meshes)
        axes = [0, 2, 1]
        collision.setdefault(name, {}).update(points=(-origin[axes]).tolist(),
            bbox={'min': (lo-origin)[axes].tolist(), 'max': (hi-origin)[axes].tolist()})
    parameters.setdefault('suspension', {})['enabled'] = False
    parameters.setdefault('maskSlice', {}).setdefault(native_gun, {})['enabled'] = False


def compile_vehicle(project, game, root):
    """Return only the new row, derived definition and authored resources."""
    from ..tech_tree import lookup_vehicle
    project.validate(complete=True)
    if project.stock_model:
        stock_visual, stock_collision = stock_assets(game, project.base_vehicle)
        parts, markers = preview_pair(stock_visual)
    else:
        parts, markers = check_model(project, root)
    nation, donor_name = project.base_vehicle.split('/')
    name = project.native_name
    catalog = game/'Data'/XML/nation

    def xml(relative):
        return ET.fromstring(dvpl.read(catalog/relative))

    stock = lookup_vehicle(game, nation, donor_name).stock
    donor = xml(donor_name+'.xml.dvpl')
    listing = xml('list.xml.dvpl')
    if any(int(row.findtext('id', '-1')) == project.vehicle_id or row.tag == name for row in listing):
        raise ValueError(f'{nation} vehicle ID {project.vehicle_id} or name {name} is already in use')
    row = deepcopy(listing.find(donor_name))
    row.tag = name
    row.attrib.clear()
    row.tail = None
    for key, value in (('id', project.vehicle_id), ('level', project.tier),
                       ('userString', f'#{nation}_vehicles:{name}'),
                       ('shortUserString', f'#{nation}_vehicles:{name}_short'),
                       ('description', f'#{nation}_vehicles:{name}_descr'),
                       ('tags', project.vehicle_class+' collectible'), ('notInShop', 'true'),
                       ('enrichmentPermanentCost', 0)):
        set_text(row, key, value)
    for key in ('price', 'sellPrice'):
        current = row.find(key)
        if current is not None:
            row.remove(current)
        set_text(row, key, 0)

    def resolved(kind, node):
        if node is None:
            raise ValueError(f'Donor has no {kind} component')
        shared = None
        if (node.text or '').strip() == 'shared':
            shared = xml(f'components/{kind}.xml.dvpl').find('shared/'+node.tag)
            if shared is None:
                raise ValueError(f'Missing shared donor component {node.tag}')
        result = resolve_gun(shared, node) if kind == 'guns' else overlay(shared, node)
        result.text = None
        return result

    chassis = resolved('chassis', donor.find('chassis/'+stock.chassis))
    turret = resolved('turrets', donor.find('turrets0/'+stock.turret))
    gun = resolved('guns', turret.find('guns/'+stock.gun))
    engine = resolved('engines', donor.find('engines/'+stock.engine))
    fuel = resolved('fuelTanks', donor.find('fuelTanks/'+stock.fuel_tank))
    radio = resolved('radios', donor.find('radios/'+stock.radio))
    for group, tag, component in (('chassis', stock.chassis, chassis), ('turrets0', stock.turret, turret),
                                 ('engines', stock.engine, engine), ('fuelTanks', stock.fuel_tank, fuel),
                                 ('radios', stock.radio, radio)):
        replace_selected(donor.find(group), tag, component)
    replace_selected(turret.find('guns'), stock.gun, gun)
    clear_unlocks(donor)
    for child in list(donor):
        if child.tag.startswith('turrets') and child.tag != 'turrets0':
            donor.remove(child)
    hull = donor.find('hull')
    turret_hp = min(project.hit_points - 1, int(turret.findtext('maxHealth')))
    for target, field, value in (
        (donor, 'speedLimits/forward', project.forward_kmh),
        (donor, 'speedLimits/backward', project.reverse_kmh),
        (hull, 'maxHealth', project.hit_points-turret_hp),
        (turret, 'maxHealth', turret_hp),
        (engine, 'power', project.engine_hp),
        (gun, 'reloadTime', project.reload_seconds),
    ):
        set_text(target, field, value)
    p = {name: np.asarray(point) for name, point in project.points.items()}
    for node, path, point in ((chassis, 'hullPosition', p['hull']),
                              (hull, 'turretPositions/turret', p['turret']-p['hull']),
                              (turret, 'gunPosition', p['gun']-p['turret'])):
        set_text(node, path, ' '.join(format(value, '.7g') for value in point))
    shot = gun.find('shots/'+stock.shell)
    if shot is None:
        raise ValueError('Donor has no stock shell in its gun')
    set_text(shot, 'shell/damage/armor', project.shell_damage)

    visual, collision = vehicle_paths(project)
    parameters = yaml.safe_load(dvpl.read(game/'Data'/PARAMETERS/nation/(donor_name+'.yaml.dvpl')))
    parameters['solbreakerMuzzleOffset'] = (p['muzzle'] - p['gun']).tolist()
    if project.stock_model:
        visual = stock_visual.parent.relative_to(game/'Data')/name
        collision = stock_collision.relative_to(game/'Data').with_suffix('')
    resources = parameters['resourcesPath']
    if project.part_donors or project.stock_model:
        collision = vehicle_paths(project)[1]
    resources.update(blitzModelPath=visual.relative_to('3d').as_posix()+'.sc2',
                     collisionMesh=collision.relative_to('3d/Tanks').as_posix()+'.sc2')
    visual_pair = ((stock_visual.with_suffix('.sc2.dvpl'), stock_visual.with_suffix('.scg.dvpl'))
                   if project.stock_model else (local_asset(root, project.visual_scene),
                                               local_asset(root, project.visual_geometry)))
    native_turret, native_gun = stock_parts(game, project.base_vehicle) if project.stock_model else ('turret_01', 'gun_01')
    muzzle = 'HP_gunFire_'+native_gun.rsplit('_', 1)[-1]
    original_tip = markers[muzzle]
    visual_raw = tuple(read_asset(path) for path in visual_pair)
    if not project.stock_model and project.original_points:
        from .parts import authored_offsets
        for node, delta in authored_offsets(project).items():
            visual_raw = move_visual_node(*visual_raw, node, delta)
            parts[node] = parts[node]+delta
    collision_raw = None
    if project.part_donors or project.stock_model:
        from .parts import donor_parts as read_donor_parts, merge_body
        donors = dict.fromkeys(('hull', 'turret', 'gun'), project.base_vehicle) if project.stock_model else {}
        assembly = replace(project, part_donors=donors | project.part_donors)
        hit_pair = ((stock_collision.with_suffix('.sc2.dvpl'), stock_collision.with_suffix('.scg.dvpl'))
                    if project.stock_model else (local_asset(root, project.collision_scene),
                                                local_asset(root, project.collision_geometry)))
        collision_names = [Path(node.findtext('hitTester/collisionModel')).stem.lower()
                           for node in (hull, turret, gun)]
        keys = ('hull', 'turret', 'gun')
        visual_raw, collision_raw = merge_body(
            game, assembly, visual_raw, tuple(read_asset(path) for path in hit_pair),
            visual.parent.as_posix(), collision.parent.as_posix(),
            dict(zip(keys, ('hull', native_turret, native_gun))), dict(zip(keys, collision_names)),
            dict(zip(keys, (hull, turret, gun))))
        for part, key in assembly.part_donors.items():
            _, _, nodes, _, _, meshes, donor_points = read_donor_parts(game, key)
            target = dict(hull='hull', turret=native_turret, gun=native_gun)[part]
            parts[target] = meshes[nodes[part]] + p[part] - donor_points[part]
            if part == 'gun':
                original_tip = donor_points['muzzle'] + p['gun'] - donor_points['gun']
    model, geometry = move_muzzle(*visual_raw, original_tip, p['muzzle'], marker_name=muzzle)
    if project.running_gear:
        from .running_gear import apply_chassis_xml, donor_preview, fitted_parts, merge_running_gear
        donor_parts = donor_preview(game, project.running_gear)
        parts = fitted_parts(parts, donor_parts, project.gear_fit)
        model, geometry, suspension, chassis_parameters = merge_running_gear(
            game, project, model, geometry, visual.parent.as_posix())
        apply_chassis_xml(game, project, chassis, set_text)
    base_suspension_enabled = parameters.get('suspension', {}).get('enabled', False)
    patch_attachment(parameters, parts, project, native_turret=native_turret, native_gun=native_gun)
    if project.stock_model:
        parameters['suspension']['enabled'] = base_suspension_enabled
    if project.running_gear:
        parameters['suspension'] = suspension
        parameters['chassis'] = chassis_parameters
    for node, thickness in ((hull, project.armor_hull_mm), (turret, project.armor_turret_mm),
                            (gun, project.armor_gun_mm)):
        if thickness:
            set_text(node, 'armor/armor_1', thickness)
    data = {
        XML/nation/(name+'.xml.dvpl'): dvpl.encode(xml_bytes(donor)),
        PARAMETERS/nation/(name+'.yaml.dvpl'): dvpl.encode(yaml.safe_dump(parameters, sort_keys=False).encode()),
        Path(str(visual)+'.sc2.dvpl'): dvpl.encode(model),
        Path(str(visual)+'.scg.dvpl'): dvpl.encode(geometry),
    }
    if collision_raw is not None:
        for suffix, payload in zip(('.sc2.dvpl', '.scg.dvpl'), collision_raw):
            data[Path(str(collision)+suffix)] = dvpl.encode(payload)
    elif not project.stock_model:
        data[Path(str(collision)+'.sc2.dvpl')] = dvpl.encode(read_asset(local_asset(root, project.collision_scene)))
        data[Path(str(collision)+'.scg.dvpl')] = dvpl.encode(read_asset(local_asset(root, project.collision_geometry)))
    for ref, source in project.textures.items():
        relative = visual.parent/Path(ref).with_suffix('.dx11.dds.dvpl')
        data[relative] = dvpl.encode(texture_dds(local_asset(root, source)))
    strings = {f'#{nation}_vehicles:{name}{suffix}': project.name for suffix in ('', '_short', '_descr')}
    return data, row, strings


def export(project, root, output):
    project.validate(complete=True)
    if not project.stock_model:
        check_model(project, root)
    paths = {name: local_asset(root, name) for name in sorted(project.assets())}
    if sum(path.stat().st_size for path in paths.values()) > MAX_PACKAGE:
        raise ValueError('Package exceeds 320 MiB uncompressed')
    textures = {ref: f'compiled-textures/{i}.dds' for i, ref in enumerate(sorted(project.textures))}
    packaged = replace(project, textures=textures)
    output = Path(output)
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.tank-', delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with ZipFile(temporary, 'w', ZIP_DEFLATED, compresslevel=4) as archive:
            archive.writestr('tank.toml', packaged.to_bytes())
            archive.writestr('build.json', json.dumps({'build': BUILD_ID}))
            total = 0
            for name in sorted(project.assets() - set(project.textures.values())):
                path = paths[name]
                size = path.stat().st_size
                if name.casefold() in ('tank.toml', 'build.json') or size > MAX_ASSET:
                    raise ValueError(f'Invalid package asset: {name}')
                total += size
                archive.write(path, name)
            for ref, member in textures.items():
                texture = texture_dds(paths[project.textures[ref]])
                total += len(texture)
                if len(texture) > MAX_ASSET or total > MAX_PACKAGE:
                    raise ValueError('Compiled textures exceed the package size limit')
                archive.writestr(member, texture)
        if output.exists() or output.is_symlink():
            raise ValueError('Choose a new package filename')
        # Exclusive copy prevents replacing a file created during export.
        with output.open('xb') as stream, temporary.open('rb') as source:
            try:
                copyfileobj(source, stream)
            except BaseException:
                stream.close()
                output.unlink(missing_ok=True)
                raise
    finally:
        temporary.unlink(missing_ok=True)
    with output.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


@contextmanager
def unpack(package):
    with tempfile.TemporaryDirectory(prefix='solbreaker-tank-') as temporary, ZipFile(package) as archive:
        root = Path(temporary)
        infos = archive.infolist()
        names = {item.filename: item for item in infos}
        if (len(names) != len(infos) or len(names) > 70
                or len({name.casefold() for name in names}) != len(names)
                or sum(item.file_size for item in infos) > MAX_PACKAGE
                or any(item.file_size > MAX_ASSET or item.is_dir() for item in infos)):
            raise ValueError('Invalid tank package members or size')
        if 'tank.toml' not in names or names['tank.toml'].file_size > MAX_PROJECT_BYTES:
            raise ValueError('Tank package must include tank.toml')
        if 'build.json' not in names or names['build.json'].file_size > 512:
            raise ValueError('Tank package must include its client build')
        if json.loads(archive.read('build.json')) != {'build': BUILD_ID}:
            raise ValueError(f'Tank package requires client build {BUILD_ID}')
        project = TankProject.decode(archive.read('tank.toml'))
        project.validate(complete=True)
        if set(names) != {'tank.toml', 'build.json', *project.assets()}:
            raise ValueError('Tank package contains unexpected or missing resources')
        for name in project.assets():
            destination = root.joinpath(*asset_path(name).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, destination.open('xb') as target:
                copyfileobj(source, target)
        yield project, root


def overlay_changes(game, data_root, project, root, *, client_labels=False):
    data, row, strings = compile_vehicle(project, game, root)
    catalog = XML/project.nation/'list.xml.dvpl'
    listing = ET.fromstring(dvpl.read(data_root/catalog))
    existing = listing.find(row.tag)
    if existing is not None:
        if existing.findtext('id') != row.findtext('id'):
            raise ValueError('The installed tank uses a different ID')
        listing.remove(existing)
    if any(item.findtext('id') == row.findtext('id') for item in listing):
        raise ValueError('Vehicle ID is occupied by another installed tank')
    listing.append(row)
    data[catalog] = dvpl.encode(xml_bytes(listing))
    if client_labels:
        for locale in ('ru', 'en'):
            relative = Path(f'Strings/{locale}.yaml.dvpl')
            current = yaml.load(dvpl.read(data_root/relative), Loader=_StringsLoader)
            current.update(strings)
            data[relative] = dvpl.encode(yaml.safe_dump(current, allow_unicode=True, sort_keys=False,
                                                       width=1 << 30).encode())
    return data
