"""Fit a stock running gear assembly to an authored tank."""
import base64
from copy import deepcopy
from functools import lru_cache
from pathlib import Path, PurePosixPath
import posixpath
import struct
import xml.etree.ElementTree as ET

import numpy as np
import yaml

from .. import dvpl
from ..build_info import BUILD_ID, VERSION
from .dava_export import ClientScene, Reader, Value, legacy_archive
from .glb import geometry_fingerprint
from .models import TRACK_NAMES, VISUAL_VERSIONS, model_preview, read_asset

@lru_cache(maxsize=4)
def donor_catalog(game):
    check_build(game)
    candidates = (('ussr/T-54', 'Т-54'), ('ussr/T-44', 'Т-44'),
                  ('ussr/T-34-85', 'Т-34-85'), ('ussr/R43_T-70_57', 'Т-70'))
    result = []
    for key, title in candidates:
        try:
            model, _ = donor_parameters(game, key)
        except (OSError, ValueError):
            continue
        if model.with_suffix('.sc2.dvpl').is_file() and model.with_suffix('.scg.dvpl').is_file():
            result.append((key, title))
    return tuple(result)


def is_running_gear(name):
    return name.startswith(('chassis_track_', 'chassis_wheel_', 'chassis_chassis_', 'HP_Track'))


def fit_vectors(settings):
    return (np.array([settings[k] for k in ('width', 'height', 'length')]),
            np.array([0., settings['lift'], settings['shift']]))


def fit_to_model(original, donor):
    def limits(parts):
        points = np.concatenate([parts[key].reshape(-1, 3) for key in TRACK_NAMES])
        return points.min(0), points.max(0)
    low, high = limits(original)
    donor_low, donor_high = limits(donor)
    span = donor_high - donor_low
    if np.any(span <= 0):
        raise ValueError('Ходовая донора имеет нулевой размер')
    scale = (high-low)/span
    offset = (low+high)/2 - ((donor_low+donor_high)/2)*scale
    return dict(zip(('width', 'height', 'length', 'lift', 'shift'),
                    map(float, (*scale, offset[1], offset[2]))))


def fitted_parts(parts, donor, settings):
    scale, offset = fit_vectors(settings)
    return ({key: value for key, value in parts.items() if not is_running_gear(key)} |
            {key: value*scale+offset for key, value in donor.items()})


def donor_parameters(game, key):
    nation, vehicle = key.split('/')
    path = game/'Data/3d/Tanks/Parameters'/nation/(vehicle+'.yaml.dvpl')
    data = yaml.safe_load(dvpl.read(path))
    if not data.get('suspension', {}).get('enabled'):
        raise ValueError(f'This build has no procedural suspension for {key}')
    stem = PurePosixPath(data['resourcesPath']['blitzModelPath'])
    if (stem.is_absolute() or '..' in stem.parts or stem.parts[0] != 'Tanks' or stem.suffix != '.sc2'):
        raise ValueError('Не удалось найти модель ходовой')
    return game/'Data/3d'/stem, data


def check_build(game):
    if dvpl.read(game/'Data/version.txt.dvpl').decode().strip() != VERSION:
        raise ValueError(f'Choose the game folder for build {BUILD_ID}')


def donor_preview(game, key):
    check_build(game)
    model, _ = donor_parameters(game, key)
    parts, _ = model_preview(Path(str(model)+'.dvpl'), model.with_suffix('.scg.dvpl'))
    return {name: mesh for name, mesh in parts.items()
            if is_running_gear(name) and 'crash' not in name}


def track_loops(game, key, parts):
    _, parameters = donor_parameters(game, key)
    suspension = parameters['suspension']
    loops = []
    for side, name in zip(('left', 'right'), TRACK_NAMES):
        chains = suspension.get(side+'TrackChains')
        chain = chains[min(chains)] if chains else suspension[side+'TrackChain']
        raw = base64.b64decode(chain.rstrip('\f'), validate=True)
        tread = parts[name].reshape(-1, 3)[:, 0]
        center = (tread.min()+tread.max())/2
        points = np.array([(center, y, z) for z, y, _ in struct.iter_unpack('<ffI', raw)])
        if len(points) < 3:
            raise ValueError('Контур гусеницы должен содержать минимум три точки')
        loops.append(points)
    return loops


def apply_chassis_xml(game, project, chassis, set_text):
    nation, name = project.running_gear.split('/')
    donor = ET.fromstring(dvpl.read(game/'Data/XML/item_defs/vehicles'/nation/(name+'.xml.dvpl')))
    source = donor.find('chassis')[0]
    for tag in ('tracks', 'wheels', 'drivingWheels'):
        node = source.find(tag)
        if node is None:
            raise ValueError(f'The donor running gear does not define {tag}')
        current = chassis.find(tag)
        if current is not None:
            chassis.remove(current)
        chassis.append(node)
    for radius in chassis.findall('wheels/*/radius'):
        radius.text = format(float(radius.text)*project.gear_fit['height'], '.7g')
    width, length = map(float, source.findtext('topRightCarryingPoint').split())
    set_text(chassis, 'topRightCarryingPoint', ' '.join(map(str, (
        width*project.gear_fit['width'], length*project.gear_fit['length']))))


def geometry_groups(data):
    reader = Reader(data)
    if reader.take(8) != b'SCPG\x01\x00\x00\x00':
        raise ValueError('Неизвестный формат геометрии ходовой')
    count = reader.count(8)
    if count != reader.number():
        raise ValueError('Неверное количество групп геометрии')
    items = [reader.archive() for _ in range(count)]
    reader.finish()
    return items


def native_id(value):
    return int.from_bytes(value.payload, 'little')


def number(value, ident):
    value.payload = int(ident).to_bytes(len(value.payload), 'little')


def name_of(node):
    return node.payload.get(b'name', Value(4, b'')).payload.decode()


def entries(node):
    return node.payload.get(b'#hierarchy', Value(27, [])).payload


def scene_parts(nodes):
    return entries(nodes[0]) if len(nodes) == 1 and entries(nodes[0]) else nodes


def walk(nodes):
    for node in nodes:
        yield node
        yield from walk(entries(node))


def transform_suspension(parameters, settings):
    result = deepcopy(parameters['suspension'])
    _, height, length = fit_vectors(settings)[0]

    def scale_chain(value):
        raw = base64.b64decode(value.rstrip('\f'), validate=True)
        out = b''.join(struct.pack('<ffI', z*length+settings['shift'], y*height+settings['lift'], flag)
                       for z, y, flag in struct.iter_unpack('<ffI', raw))
        return base64.b64encode(out).decode() + ('\f' if value.endswith('\f') else '')

    for side in ('left', 'right'):
        if (key := side+'TrackChains') in result:
            result[key] = {f'{index:02d}': scale_chain(value) for index, value in result[key].items()}
        elif (key := side+'TrackChain') in result:
            result[key] = scale_chain(result[key])
        else:
            raise ValueError(f'The donor has no {side} track path')
    raw = base64.b64decode(result['wheels'].rstrip('\f'), validate=True)
    wheels = b''.join(struct.pack('<Iff', flag, up*height, down*height)
                     for flag, up, down in struct.iter_unpack('<Iff', raw))
    result['wheels'] = base64.b64encode(wheels).decode()+ ('\f' if result['wheels'].endswith('\f') else '')
    return result


def transplant_nodes(game, model, scene_raw, geometry_raw, target_dir, names, remove, fit):
    """Copy selected DAVA nodes, resolving their geometry and material references locally."""
    source = ClientScene(read_asset(Path(str(model)+'.dvpl')), versions=VISUAL_VERSIONS)
    target = ClientScene(scene_raw, versions=VISUAL_VERSIONS)
    source_nodes = source.fields[b'#hierarchy'].payload
    target_nodes = target.fields[b'#hierarchy'].payload
    new_nodes = [node for node in scene_parts(source_nodes) if name_of(node) in names]
    if not new_nodes:
        raise ValueError('В модели донора нет выбранной детали')
    target_children = scene_parts(target_nodes)
    target_children[:] = [node for node in target_children if not remove(name_of(node))]
    material_bucket = target.fields[b'#dataNodes'].payload
    donor_materials = {native_id(node.payload[b'#id']): node for node in source.fields[b'#dataNodes'].payload}
    donor_geometry = {native_id(group[b'#id']): group for group in geometry_groups(
        read_asset(model.with_suffix('.scg.dvpl')))}
    geometry = geometry_groups(geometry_raw)
    next_id = max([native_id(n.payload[b'#id']) for n in material_bucket]
                  + [native_id(g[b'#id']) for g in geometry], default=0) + 1
    next_entity = max(native_id(n.payload[b'id']) for n in walk(target_nodes)) + 1
    mats, groups = {}, {}
    scale, offset = (value[[0, 2, 1]] for value in fit)
    model_dir = PurePosixPath(model.parent.relative_to(game/'Data').as_posix())

    def material_id(old):
        nonlocal next_id
        if old == 0:
            return 0
        if old in mats:
            return mats[old]
        row = donor_materials[old]
        ident = mats[old] = next_id
        next_id += 1
        data = row.payload
        number(data[b'#id'], ident)
        if b'parentMaterialKey' in data:
            parent = data[b'parentMaterialKey']
            number(parent, material_id(native_id(parent)))
        configs = [data] + [value.payload for key, value in data.items() if key.startswith(b'configArchive_')]
        for config in configs:
            if b'textures' in config:
                for texture in config[b'textures'].payload.values():
                    value = texture.payload.decode()
                    if value.startswith('~res:/'):
                        path = PurePosixPath(value.removeprefix('~res:/'))
                    else:
                        path = model_dir/value
                    resource = posixpath.normpath(path.as_posix())
                    if not resource.startswith('3d/'):
                        raise ValueError('Ссылка материала ходовой выходит за папку 3d')
                    texture.payload = posixpath.relpath(resource, target_dir).encode()
        material_bucket.append(row)
        return ident

    def geometry_id(old):
        nonlocal next_id
        if old in groups:
            return groups[old]
        group = donor_geometry[old]
        size = native_id(group[b'vertexCount'])
        raw = bytearray(group[b'vertices'].payload)
        stride = len(raw)//size
        if native_id(group[b'packing']) != 0 or stride < 24:
            raise ValueError('Неподдерживаемый формат вершин ходовой')
        points = np.ndarray((size, 3), dtype='<f4', buffer=raw, strides=(stride, 4))
        points[:] = points*scale+offset
        normals = np.ndarray((size, 3), dtype='<f4', buffer=raw, offset=12, strides=(stride, 4))
        normals[:] = normals/scale
        normals[:] /= np.maximum(1e-8, np.linalg.norm(normals, axis=1, keepdims=True))
        group[b'vertices'].payload = bytes(raw)
        ident = groups[old] = next_id
        next_id += 1
        number(group[b'#id'], ident)
        geometry.append(group)
        return ident

    for node in new_nodes:
        node.payload[b'name'].payload = names[name_of(node)].encode()
    for node in walk(new_nodes):
        number(node.payload[b'id'], next_entity)
        next_entity += 1
        for value in node.payload[b'components'].payload.values():
            if value.kind != 8:
                continue
            component = value.payload
            kind = component[b'comp.typename'].payload
            if kind == b'TransformComponent':
                for frame in (b'local', b'world'):
                    for stem, expected in ((b'Translation', (0., 0., 0.)), (b'Scale', (1., 1., 1.)),
                                           (b'Rotation', (0., 0., 0., 1.))):
                        item = component[b'tc.'+frame+stem]
                        if not np.allclose(struct.unpack('<'+'f'*len(expected), item.payload), expected, atol=1e-5):
                            raise ValueError(f'{name_of(node)} has an unbaked transform')
            if kind != b'RenderComponent':
                continue
            render = component[b'rc.renderObj'].payload
            for value in render[b'ro.batches'].payload.values():
                batch = value.payload
                ref = batch[b'rb.datasource']
                number(ref, geometry_id(native_id(ref)))
                mat = batch[b'rb.nmatname']
                number(mat, material_id(native_id(mat)))
                bounds = batch[b'rb.aabbox']
                xyz = np.array(struct.unpack('<6f', bounds.payload)).reshape(2, 3)
                bounds.payload = struct.pack('<6f', *(xyz*scale+offset).flatten())
    target_children.extend(new_nodes)
    fingerprint = geometry_fingerprint(native_id(group[b'#id']) for group in geometry)
    start = target.geometry_fingerprint_offset
    target.header = target.header[:start] + struct.pack('<Q', fingerprint) + target.header[start+8:]
    geometry_raw = b'SCPG' + struct.pack('<III', 1, len(geometry), len(geometry)) + b''.join(
        legacy_archive(group) for group in geometry)
    return target.encode(), geometry_raw


def merge_running_gear(game, project, scene_raw, geometry_raw, target_dir):
    model, parameters = donor_parameters(game, project.running_gear)
    source = ClientScene(read_asset(model.with_suffix('.sc2.dvpl')), versions=VISUAL_VERSIONS)
    names = {name_of(node): name_of(node) for node in scene_parts(source.fields[b'#hierarchy'].payload)
             if is_running_gear(name_of(node))}
    if not set(TRACK_NAMES) <= names.keys():
        raise ValueError('У донора отсутствует пара гусениц')
    scene, geometry = transplant_nodes(game, model, scene_raw, geometry_raw, target_dir, names,
                                       is_running_gear, fit_vectors(project.gear_fit))
    return scene, geometry, transform_suspension(parameters, project.gear_fit), parameters['chassis']
