"""Build client model pairs from a GLB with named tank meshes and pivots."""
from collections import defaultdict
from itertools import count
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

import numpy as np

from .. import dvpl
from .dava_export import Value, encode_table, legacy_archive
from .models import MAX_ASSET, TRACK_NAMES

BODY_PARTS = {'hull', 'turret_01', 'gun_01'}
TRACK_PARTS = set(TRACK_NAMES)


def string(value):
    return Value(4, value.encode('utf-8'))


def integer(value, kind=2):
    return Value(kind, struct.pack({2: '<i', 7: '<I', 10: '<Q'}[kind], value))


def vector(values):
    return Value({3: 12, 4: 13, 6: 19}[len(values)], struct.pack(f'<{len(values)}f', *values))


def archive(values):
    return Value(8, {name.encode(): value for name, value in values.items()})


def identity(value):
    return Value(6, struct.pack('<Q', value))


def data_header(node_type, identifier):
    return {b'##name': string(node_type), b'#id': identity(identifier)}


def transform():
    data = {'comp.typename': string('TransformComponent')}
    for frame in ('local', 'world'):
        data[f'tc.{frame}Rotation'] = vector((0., 0., 0., 1.))
        data[f'tc.{frame}Scale'] = vector((1., 1., 1.))
        data[f'tc.{frame}Translation'] = vector((0., 0., 0.))
    return archive(data)


def entity(name, identifier, components=(), children=(), *, hidden=False):
    comps = dict(count=integer(len(components)+1, 7))
    comps.update({f'{i:04d}': item for i, item in enumerate((transform(), *components))})
    return archive({'##name': string('Entity'), 'id': integer(identifier, 7), 'name': string(name),
                    'flags': integer(0 if hidden else 6146, 7), 'components': archive(comps),
                    '#hierarchy': Value(27, list(children))})


def render_component(batches):
    info = {'##name': string('Mesh'), 'ro.batchCount': integer(len(batches), 7),
            'ro.batches': archive({f'{index:04d}': item for index, item in enumerate(batches)}),
            'ro.flags': integer(1, 7)}
    for index in range(len(batches)):
        info[f'rb{index}.lodIndex'] = info[f'rb{index}.switchIndex'] = integer(-1)
    return archive({'comp.typename': string('RenderComponent'), 'rc.renderObj': archive(info)})


def make_group(ident, material, triangles, *, collision=False):
    verts = np.asarray(triangles, dtype='<f4').reshape(-1, 8)
    if not np.isfinite(verts).all() or np.abs(verts[:, :3]).max() > 100:
        raise ValueError('Model contains non-finite or out-of-scale geometry')
    if collision:
        # The runtime reads the armor material index as a float at byte 28.
        verts[:, 6:] = (0., 1.)
    xyz = verts[:, :3]
    low, high = xyz.min(0), xyz.max(0)
    group = data_header('PolygonGroup', ident)
    for key, val in (('indexCount', len(verts)), ('indexFormat', 0), ('packing', 0),
                     ('primitiveCount', len(triangles)), ('rhi_primitiveType', 1),
                     ('textureCoordCount', 0 if collision else 1), ('cubeTextureCoordCount', 0),
                     ('vertexCount', len(verts)), ('vertexFormat', 0x207 if collision else 0x0f)):
        group[key.encode()] = integer(val)
    group[b'vertices'] = Value(6, verts.tobytes())
    if not collision:
        # Visual VF 15 includes a packed RGBA8 color between normal and UV.
        packed = verts.copy().view('<u4')
        rgba = np.full((len(verts), 1), 0xffffffff, dtype='<u4')
        group[b'vertices'] = Value(6, np.hstack((packed[:, :6], rgba, packed[:, 6:])).tobytes())
    group[b'indices'] = Value(6, np.arange(len(verts), dtype='<u2').tobytes())
    batch = archive({'##name': string('RenderBatch'), 'rb.classname': string('RenderBatch'),
                     'rb.datasource': integer(ident, 10), 'rb.nmatname': integer(material, 10),
                     'rb.sortingKey': integer(248, 7), 'rb.aabbox': vector((*low, *high))})
    return legacy_archive(group), batch


def geometry_fingerprint(geometry_ids):
    seed, mask = 0, (1 << 64)-1
    for ident in geometry_ids:
        mixed = (((ident << 26) + seed) << 6) & mask
        seed ^= ((mixed + (seed >> 2) + 0x9e3779b99e3779b9) & mask) ^ ident
    return seed


def make_model(grouped, textures, points, *, collision, pivots=None):
    seq = count(1)
    data, meshes, geometry_ids, material_ids, children = [], [], [], {}, []
    for (part, material), triangles in sorted(grouped.items()):
        if material not in material_ids:
            parent, instance = next(seq), next(seq)
            material_ids[material] = instance
            reference = textures.get(material)
            fx = 'VertexColor' if collision or not reference else 'Textured'
            data.append(Value(8, {**data_header('NMaterial', parent),
                b'fxName': string(f'~res:/Materials/{fx}.material'),
                b'materialName': string(material),
                b'textures': archive({'albedo': string(reference)} if reference else {})}))
            data.append(Value(8, {**data_header('NMaterial', instance),
                b'materialName': string(f'Instance-{instance}'), b'parentMaterialKey': integer(parent, 10)}))
    by_part = defaultdict(list)
    for (part, material), triangles in sorted(grouped.items()):
        if collision and pivots is not None:
            triangles = np.asarray(triangles, dtype='<f4')
            triangles[:, :, :3] -= pivots[part]
        for offset in range(0, len(triangles), 20000):
            ident = next(seq)
            geometry_ids.append(ident)
            mesh, batch = make_group(ident, material_ids[material], triangles[offset:offset+20000],
                                     collision=collision)
            meshes.append(mesh)
            by_part[part].append(batch)
    if not collision:
        for name, point in points.items():
            if not name.startswith('HP_'):
                continue
            x, y, z = point
            triangle = [[[x, y+dy, z+dz, 1, 0, 0, 0, 0]
                         for dy, dz in ((-.005,-.005), (.005,-.005), (0,.005))]]
            ident = next(seq)
            geometry_ids.append(ident)
            mesh, batch = make_group(ident, next(iter(material_ids.values())), triangle)
            meshes.append(mesh)
            by_part[name].append(batch)
    collider = archive({'comp.typename': string('CollisionTypeComponent'), 'CollisionType': integer(6),
                        'MaterialKind': integer(1), 'FallingType': integer(0), 'Health': integer(5),
                        'Density': Value(3, struct.pack('<f', .9))})
    for part, batches in by_part.items():
        components = [render_component(batches)]
        if collision:
            components.append(collider)
        children.append(entity(part, next(seq), components, hidden=part.startswith('HP_')))
    if collision and 'gun_01' not in by_part:
        children.append(entity('gun_01', next(seq)))
    root = entity('Solbreaker tank', next(seq), children=children)
    seed = geometry_fingerprint(geometry_ids)
    fields = {b'#dataNodes': Value(27, data), b'#hierarchy': Value(27, [root]), b'#sceneComponents': archive({})}
    scene = b'SFV2' + struct.pack('<II', 35, 1) + legacy_archive({}) + struct.pack('<IIQ', 12, 1, seed)
    geometry = b'SCPG'+struct.pack('<III', 1, len(meshes), len(meshes)) + b''.join(meshes)
    if len(geometry) > MAX_ASSET or len(scene) > MAX_ASSET:
        raise ValueError('Converted model exceeds the supported file size')
    return scene+encode_table(fields), geometry


def import_glb(source, destination, *, blender=None):
    source, destination = Path(source), Path(destination)
    if source.stat().st_size > MAX_ASSET:
        raise ValueError('GLB exceeds 64 MiB')
    blender = blender or shutil.which('blender')
    if not blender:
        raise ValueError('Install Blender and add its executable to PATH to import GLB')
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with tempfile.TemporaryDirectory(prefix='solbreaker-glb-') as temp:
            extracted = Path(temp)/'model'
            result = subprocess.run([str(blender), '--background', '--factory-startup', '--disable-autoexec',
                '--python-exit-code', '1', '--python', str(Path(__file__).with_name('blender_import.py')),
                '--', str(source.resolve()), str(extracted)], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, check=False, text=True, timeout=150)
            if result.returncode:
                lines = [line for line in result.stdout.splitlines() if line.strip()]
                raise ValueError('Blender import failed: '+ ' '.join(lines[-6:]))
            if (extracted/'mesh.json').stat().st_size > 120*1024**2:
                raise ValueError('Converted GLB mesh data exceeds 120 MiB')
            data = json.loads((extracted/'mesh.json').read_text())
            visual, collision = defaultdict(list), defaultdict(list)
            for part, material, triangle in data['groups']:
                if part.startswith('collision_'):
                    collision[part.removeprefix('collision_'), 'Armor'].append(triangle)
                else:
                    visual[part, material].append(triangle)
            parts = {name for name, _ in visual}
            if not BODY_PARTS <= parts:
                raise ValueError('GLB requires hull, turret_01 and gun_01')
            if parts & TRACK_PARTS and not TRACK_PARTS <= parts:
                raise ValueError('Provide both track meshes or use Workshop running gear')
            if not {'hull', 'turret_01'} <= {name for name, _ in collision}:
                raise ValueError('GLB requires collision_hull and collision_turret_01 meshes')
            points = data['points']
            if 'HP_gunFire_01' not in points:
                raise ValueError('GLB requires an empty named HP_gunFire_01 at the barrel tip')
            for side in ('L', 'R'):
                if f'chassis_track_{side}' not in parts:
                    continue
                triangles = np.concatenate([np.asarray(mesh).reshape(-1, 8)[:, :3]
                    for (part, _), mesh in visual.items() if part == f'chassis_track_{side}'])
                lo, hi = triangles.min(0), triangles.max(0)
                mid = (lo + hi) / 2
                points.setdefault(f'HP_Track_{side}Front', [mid[0], hi[1], lo[2]])
                points.setdefault(f'HP_Track_{side}Rear', [mid[0], lo[1], lo[2]])
            refs = {}
            for material, texture in data['materials'].items():
                if texture:
                    path = Path(texture).with_suffix('.tex').as_posix()
                    shutil.copyfile(extracted/texture, destination/texture)
                    refs[material] = path
            pivots = {part: np.asarray(points.get(part, (0., 0., 0.))) for part in BODY_PARTS}
            for name, groups, textures, hit_mesh in (
                    ('visual', visual, refs, False), ('collision', collision, {}, True)):
                scene, geo = make_model(groups, textures, points, collision=hit_mesh,
                                        pivots=pivots if hit_mesh else None)
                (destination/f'{name}.sc2.dvpl').write_bytes(dvpl.encode(scene))
                (destination/f'{name}.scg.dvpl').write_bytes(dvpl.encode(geo))
    except BaseException:
        shutil.rmtree(destination)
        raise
    positions = {'hull': 'hull', 'turret': 'turret_01', 'gun': 'gun_01', 'muzzle': 'HP_gunFire_01'}
    return ({key: tuple(points[name][axis] for axis in (0, 2, 1))
             for key, name in positions.items() if name in points},
            {ref: str(Path(ref).with_suffix('.png')) for ref in refs.values()})
