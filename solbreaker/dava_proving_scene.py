"""Compile the proving ground using resources from the player's pinned client.

Keep the source material and polygon IDs so instanced local props still refer to
their own textures, animation and destruction states. Only the new level layout
and the author's meshes are shipped with Solbreaker.
"""
import copy
import math
import random
import struct
import zlib

import numpy as np

from .dava_scene import Archive
from .proving_ground_landscape_detail import (
    QUARRY_YARD, SETTLEMENT_HOUSES, SETTLEMENT_WINDMILL, ground_cover, vegetation,
)


AIRCRAFT_ALTITUDE_OFFSET = 82.


# KA2 values carry their original type even where Python sees the same number.
_FIXED = {1: 1, 2: 4, 3: 4, 7: 4, 9: 8, 10: 8, 11: 8, 12: 12, 13: 16,
          14: 16, 15: 36, 16: 64, 17: 16, 19: 24, 21: 8, 22: 1, 23: 1,
          24: 2, 25: 2, 29: 40}
_STRING = (4, 5, 18, 20)


class _TypedReader(Archive):
    def value(self, kind, version):
        if kind in _FIXED:
            val = self.take(_FIXED[kind])
        elif kind == 8:
            child = _TypedReader(self.take(self.number('I')), self.strings)
            val = child.load()
            if child.pos != len(child.data):
                raise ValueError('Incomplete nested map archive')
        else:
            val = super().value(kind, version)
        return kind, val


def _u32(value):
    return struct.pack('<I', value)


def _i(value):
    return 2, struct.pack('<i', value)


def _u(value):
    return 7, _u32(value)


def _q(value):
    return 10, struct.pack('<Q', value)


def _name_id(text):
    crc = zlib.crc32(text.encode('utf-8', 'surrogateescape'))
    return (crc & 0xfffe0000) | ((crc & 0x7fff) << 2)


class _Table:
    def __init__(self):
        self.ids = {}

    def name(self, text):
        if text not in self.ids:
            self.ids[text] = _u32(_name_id(text))
        return self.ids[text]

    def items(self, fields):
        return _u32(len(fields)) + b''.join(self.name(key) + self.field(fields[key])
                                             for key in sorted(fields))

    def field(self, field):
        kind, value = field
        if kind in _STRING:
            value = self.name(value)
        elif kind == 8:
            value = b'KA\x02\x01' + self.items(value) if value else b'KA\x02\xff'
            value = _u32(len(value)) + value
        elif kind == 27:
            value = _u32(len(value)) + b''.join(map(self.field, value))
        elif kind == 6:
            value = _u32(len(value)) + value
        elif kind == 0:
            value = b''
        return bytes([kind]) + value

    def encode(self, top):
        body = self.items(top)
        names = sorted((text.encode('utf-8','surrogateescape'), ident)
                       for text, ident in self.ids.items())
        if len({ident for _, ident in names}) != len(names):
            raise ValueError('Native scene string IDs collide')
        lookup = b''.join(struct.pack('<H',len(name)) + name for name, _ in names)
        return b'KA\x02\x00' + _u32(len(names)) + lookup + b''.join(i for _, i in names) + body


def _archive1(fields):
    """Legacy inline archive used for polygon streams and destructible indices."""
    def blob(value):
        return _u32(len(value)) + value
    parts = []
    for name, (kind, value) in sorted(fields.items()):
        if kind == 4:
            value = blob(value.encode())
        elif kind == 6:
            value = blob(value)
        parts.append(b'\x04' + blob(name.encode()) + bytes([kind]) + value)
    return b'KA\x01\x00' + _u32(len(parts)) + b''.join(parts)


def _components(node):
    return {part['comp.typename'][1]: part for kind, part in node['components'][1].values()
            if kind == 8}


def _entity(name, ident, components):
    components = [dict(fields, **{'comp.typename': (4, key)}) for key, fields in components]
    values = {f'{i:04}': (8, part) for i, part in enumerate(components)}
    values['count'] = _u(len(values))
    return 8, {'##name': (4, 'Entity'), 'name': (4, name), 'flags': _u(6146),
               'id': _u(ident), 'components': (8, values)}


def _mesh(obj, index, transform, water_parent=None):
    coords = np.asarray(obj['vertices'], dtype='<f4')
    origin = np.asarray(obj.get('origin', (0., 0., 0.)), dtype='<f4')
    faces = np.asarray(obj['triangles'], dtype=int)
    if faces.ndim != 2 or faces.shape[1] != 3 or not 0 < faces.size <= 65535:
        raise ValueError('Invalid proving ground mesh')
    if np.any(faces < 0) or np.any(faces >= len(coords)) or not np.isfinite(coords).all():
        raise ValueError('Invalid proving ground vertices')
    points = coords[faces]
    normals = np.cross(points[:, 1] - points[:, 0], points[:, 2] - points[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    if np.any(lengths < 1e-6):
        raise ValueError('Degenerate triangle in ' + obj['name'])
    normals /= lengths[:, None]
    vertices = np.empty(faces.size, dtype=[('position','<3f'), ('normal','<3f'),
                                           ('color','4u1'), ('uv','<2f')])
    vertices['position'] = points.reshape(-1,3) - origin
    vertices['normal'] = np.repeat(normals, 3, axis=0)
    # The pinned vertex-color shader is unlit. Bake a directional key and sky
    # fill so that the roof, end walls and steelwork remain readable in motion.
    light = np.array([-.49,.32,.81])
    if 'vertex_colors' in obj:
        # The distant ridges share their normals and colours across faces.
        # A flat tint per triangle is visible even through the map fog.
        adjacency = np.zeros_like(coords)
        for corner in faces.T:
            np.add.at(adjacency, corner, normals)
        adjacency /= np.maximum(np.linalg.norm(adjacency, axis=1)[:, None], 1e-6)
        vertices['normal'] = adjacency[faces].reshape(-1, 3)
        rgba = np.asarray(obj['vertex_colors'], dtype=float)[faces].reshape(-1, 4) * 255
    else:
        rgba = np.asarray(obj['color']) * 255
    sun = np.maximum(0, vertices['normal'] @ light)
    shade = .65 + .35 * sun
    rgba = np.clip(rgba, 0, 255)
    vertices['color'][:, :3] = (shade[:, None] * rgba[..., :3]).astype(np.uint8)
    vertices['color'][:, 3] = rgba[..., 3].astype(np.uint8)
    # Project onto the dominant plane per face so vertical walls have height
    # in UV space, and mitred roof sections can share the same sheet stock.
    vertical = np.abs(normals).argmax(axis=1)
    uv = np.empty((len(faces),3,2),dtype='<f4')
    for axis, axes in enumerate(((1,2),(0,2),(0,1))):
        select = vertical == axis
        uv[select] = points[select][:,:,axes]
    vertices['uv'] = uv.reshape(-1,2) * .25
    if 'sign' in obj:
        vertices['uv'] = np.asarray(((0,1),(1,1),(1,0),(0,0)),dtype='<f4')[faces.reshape(-1)]
    is_water = obj.get('water', False)
    if is_water:
        if water_parent is None:
            raise ValueError('Missing local water material')
        stream = np.empty(faces.size,dtype=[('position','<3f'),('normal','<3f'),
                                            ('uv','<2f'),('decal','<2f'),('tangent','<3f')])
        stream['position'],stream['normal'] = vertices['position'],vertices['normal']
        stream['uv'] = vertices['position'][:,:2]*.05
        stream['decal'] = stream['uv']
        stream['tangent'] = (1,0,0)
        vertices = stream
    name = obj['name']
    mat_id, pg_id = 1000000 + index*2, 1000001 + index*2
    pg = _archive1({'##name': (4, 'PolygonGroup'), '#id': (6, struct.pack('<Q', pg_id)),
        'cubeTextureCoordCount': _i(0), 'indexCount': _i(faces.size), 'indexFormat': _i(0),
        'indices': (6, np.arange(faces.size,dtype='<u2').tobytes()), 'packing': _i(0),
        'primitiveCount': _i(len(faces)), 'rhi_primitiveType': _i(1), 'textureCoordCount': _i(2 if is_water else 1),
        'vertexCount': _i(faces.size), 'vertexFormat': _i(0x9b if is_water else 15), 'vertices': (6, vertices.tobytes())})
    material = (8, {'##name': (4,'NMaterial'), '#id': (6,struct.pack('<Q',mat_id)),
        'fxName': (4,'~res:/Materials/VertexColor.material'), 'materialName': (4,name+'_material'),
        'flags': (8,{}), 'properties': (8,{}), 'textures': (8,{})})
    for suffixes, finish in (
        (('_sage','_galvanized'), 'sheet'),
        (('_sage_light','_roof'), 'roof'),
        (('_concrete','_plinth','_mortar','_shale'), 'aggregate'),
        (('_steel','_rust','_ochre'), 'plate'),
    ):
        if name.endswith(suffixes):
            fields = material[1]
            fields['fxName'] = 4,'~res:/Materials/Textured.material'
            fields['flags'] = 8,{'VERTEX_COLOR':_i(1), 'SHADOW_RECEIVER':_i(1)}
            fields['textures'] = 8,{'albedo':(4, f'landscape/solbreaker_range_{finish}.tex')}
            fields['textureSampleStates'] = 8,{'albedo':_u(2103616)}
            break
    if 'sign' in obj:
        fields = material[1]
        fields['fxName'] = 4,'~res:/Materials/Textured.material'
        fields['textures'] = 8,{'albedo':(4,'landscape/solbreaker_sign_'+obj['sign']+'.tex')}
        fields['textureSampleStates'] = 8,{'albedo':_u(2103616)}
    if is_water:
        # A ford is a bounded mesh. Use the pinned client's cubemap and
        # animated normal maps without the ocean's camera-following renderer.
        material[1].update({
            'parentMaterialKey': water_parent,
            'fxName': (4,'~res:/Materials/WaterPerPixelCubemapAlphablend.material'),
            'flags': (8,{'WATER_RENDER_OBJECT':_i(0)}),
        })
    batch = {'##name': (4,'RenderBatch'), 'rb.classname': (4,'RenderBatch'), 'rb.sortingKey': _u(248),
        'rb.datasource': _q(pg_id), 'rb.nmatname': _q(mat_id),
        'rb.aabbox': (19,struct.pack('<6f',*(coords.min(axis=0)-origin),*(coords.max(axis=0)-origin)))}
    render = {'##name': (4,'Mesh'), 'rb0.lodIndex': _i(-1), 'rb0.switchIndex': _i(-1),
        'ro.flags': _u(1), 'ro.debugflags': _u(0), 'ro.sOclIndex': _u(65535),
        'ro.batchCount': _u(1), 'ro.batches': (8,{'0000': (8,batch)})}
    collision = {'CollisionType': _i(6), 'Density': (3,struct.pack('<f',.9)),
                 'FallingType': _i(0),'Health': _i(5),'MaterialKind': _i(1)}
    parts = [('RenderComponent',{'rc.renderObj': (8,render)}), ('TransformComponent',transform)]
    if obj.get('solid', True):
        parts.insert(0, ('CollisionTypeComponent',collision))
    return _entity(name, 50000 + index, parts), material, pg_id, pg


def _shift(node, delta, ids, *, root=True):
    """DAVA stores both local and world translations for every nested prop."""
    components = _components(node)
    if 'SpeedTreeComponent' in components and 'LodTransitionComponent' in components:
        # Clones share the donor batch materials. The native crossfade writes
        # lodTransitionThreshold into an instance-local material property;
        # ordinary LodComponent switching needs no per-tree mutable material.
        group = node['components'][1]
        retained = [value for key, value in sorted(group.items()) if key != 'count'
                    and value[1]['comp.typename'][1] != 'LodTransitionComponent']
        node['components'] = 8, {**{f'{i:04}': value for i, value in enumerate(retained)},
                                 'count': _u(len(retained))}
    render = components.get('RenderComponent', {}).get('rc.renderObj')
    if render is not None and 'ro.sOclIndex' in render[1]:
        # The original PVS describes placements on Mines, not on this map.
        render[1]['ro.sOclIndex'] = _u(65535)
    part = components['TransformComponent']
    for key in (('tc.worldTranslation','tc.localTranslation') if root else ('tc.worldTranslation',)):
        old = struct.unpack('<3f',part[key][1])
        part[key] = 12, struct.pack('<3f', *(p+d for p,d in zip(old,delta)))
    node['id'] = _u(next(ids))
    for _, child in node.get('#hierarchy',(27,[]))[1]:
        _shift(child, delta, ids, root=False)


def _merge_batch(target, source):
    count = struct.unpack('<I',target['ro.batchCount'][1])[0]
    target[f'rb{count}.lodIndex'] = source['rb0.lodIndex']
    target[f'rb{count}.switchIndex'] = source['rb0.switchIndex']
    target['ro.batches'][1][f'{count:04}'] = source['ro.batches'][1]['0000']
    target['ro.batchCount'] = _u(count+1)


def _props():
    # Position intact, native state-switching assets inside the western course.
    for y in (-32,-10,12,34):
        yield 'env_rd_stonefence.sc2', -316, y, True
        yield 'env_rd_woodfence.sc2', -295, y, True
    yield 'env_rd_mine_enterance.sc2', -340, 42, True
    yield 'bld_rd_barnsmall_decrepit.sc2', -330, -45, True
    yield 'bld_rd_barnsmall_decrepit.sc2', -274, 39, True
    yield 'bld_rd_barnsmall_decrepit.sc2', -374, 23, True
    yield 'bld_rd_barnsmall_decrepit.sc2', -294, -46, True
    yield 'bld_pl_villagehouse_02.sc2', -370, -20, False
    yield 'bld_pl_villagehouse_03.sc2', -338, 18, False
    yield 'bld_rd_cowshed.sc2', -274, -20, False
    for x,y in ((-396,-26),(-371,10),(-327,48),(-281,-48),(-266,18)):
        yield 'env_rd_40_RuralCart.sc2', x,y, True
    for x,y in ((-387,-20),(-365,-31),(-343,3),(-343,31),(-303,-67),(-261,6)):
        yield 'env_rd_woodfence.sc2', x,y, True
    yield 'env_rd_budka.sc2', -341, -19, True
    yield 'bld_rd_watch_tower.sc2', -345, -45, False
    yield 'env_rd_kolodets.sc2', -363, -34, True
    yield 'env_rd_logStack_2.sc2', -335, 31, True
    yield 'env_rd_telega01.sc2', -281, -28, True
    yield 'env_rd_mineCar.sc2', -258, 244, True
    yield 'env_rd_mineCar.sc2', -327, 249, True
    yield 'env_rd_mine_reels.sc2', -349, 260, False
    yield 'env_rd_mine_reels.sc2', -363, 288, False
    yield 'env_rd_logStack_2.sc2', -230, 302, True
    yield 'env_rd_plane_03.sc2', -122, 369, False
    yield 'env_rd_plane_03_1.sc2', 219, 374, False
    yield 'env_rd_budka.sc2', -192, 374, True
    for x,y in ((-88,-63),(-69,-55),(110,37),(-181,314)):
        yield 'env_rd_bench.sc2', x,y, True
    yield 'bld_rd_mine_shaft.sc2', -399, 355, False
    yield 'bld_rd_mine_tower.sc2', -334, 269, True
    yield 'env_rd_mineCar.sc2', -302, 250, True
    yield 'env_rd_logStack_2.sc2', -378, 370, True
    yield 'env_rd_mine_reels.sc2', -390, 371, False
    # Native roadside props stand beyond the shoulder and the access mouths.
    for x,y in ((-199,23),(-154,12),(161,12),(201,-17),(-2,324),(19,380)):
        yield 'env_13_electropole.sc2', x,y, False
    for x,y in ((-180,13),(205,-22),(132,-266)):
        yield 'env_rd_woodfence.sc2', x,y, True
    yield 'env_rd_budka.sc2', 137,-235, True
    for name, x, y, _, _, _ in SETTLEMENT_HOUSES:
        yield name, x, y, False
    yield 'bld_pl_windmill.sc2', *SETTLEMENT_WINDMILL, False
    yield 'env_rd_kolodets.sc2', -307., 150., True
    yield 'env_rd_40_RuralCart.sc2', -317., 148., True
    yield 'env_rd_woodfence.sc2', -341., 143., True
    yield 'env_rd_stonefence.sc2', -299., 163., True
    yield 'env_13_electropole.sc2', -245., 144., False
    yield 'env_13_electropole.sc2', -345., 122., False
    x, y = QUARRY_YARD
    yield 'env_rd_mineCar.sc2', x + 4., y - 10., True
    yield 'env_rd_mineCar.sc2', x + 11., y - 10., True
    yield 'env_rd_mine_reels.sc2', x + 17., y + 14.5, False
    yield 'env_rd_logStack_2.sc2', x + 17., y - 4., True


def _ambience():
    # Positional native sounds make the ford, groves and service areas
    # audible from their own locations, in addition to the map's sound bed.
    yield 'ambient/water/river_weak', -404.7, -214.1
    yield 'ambient/wind/wind_and_leaves', -165., 122.
    yield 'ambient/wind/wind_and_leaves', 89., -147.
    yield 'ambient/rudniki/crickets', -358., -126.
    yield 'ambient/rudniki/crickets', 137., 126.
    yield 'ambient/birds/sparrow', 96., 77.
    yield 'ambient/rudniki/birds', -171., 269.
    yield 'ambient/rudniki/birds', 122., -310.
    yield 'ambient/birds/sparrow', -311., 155.
    yield 'ambient/wind/wind_and_leaves', *SETTLEMENT_WINDMILL


def compile_scene(scene_bytes, geometry_bytes, objects, height, surface):
    reader = _TypedReader(scene_bytes)
    if reader.take(20) != b'SFV2' + _u32(48) + scene_bytes[8:12] + b'KA\x01\x00\x00\x00\x00\x00':
        raise ValueError('The proving ground requires the pinned v48 scene')
    descriptor = bytearray(reader.take(reader.number('I')))
    top = reader.load()
    if reader.pos != len(scene_bytes) or len(descriptor) != 20:
        raise ValueError('Unexpected v48 scene envelope')
    hierarchy = top['#hierarchy'][1]
    sources = {}
    for _, node in hierarchy:
        sources.setdefault(node['name'][1], []).append(node)
    root = [(8,sources[key][0]) for key in ('Landscape','SkySphereDeepHorizon.sc2','MapSettings')]
    land = _components(root[0][1])
    render = land['RenderComponent']['rc.renderObj'][1]
    render['hmap'] = (4,'landscape/solbreaker_proving_ground.heightmap')
    render['bbox'] = (6,struct.pack('<6f',-512,-512,0,512,512,100))
    transform = land['TransformComponent']
    root.append(_entity('sb_range_bounds',49999,[
        ('MapBorderComponent',{'mbc.rect': (6,struct.pack('<4f',-504,-504,504,504))}),
        ('TransformComponent',transform)]))
    settings = _components(root[2][1])['TerrainDataComponent']
    settings['tdc.lkaPaths'][1][''] = (4,'blitz/solbreaker_proving_ground.lka')
    settings['tdc.mkmPath'] = (4,'blitz/solbreaker_proving_ground.mkm')
    settings['tdc.mapEffectsPath'] = (4,'blitz/solbreaker_proving_ground.yaml')
    data_nodes = top['#dataNodes'][1]
    terrain_mat = next(n for _,n in data_nodes if n.get('materialName') == (4,'Landscape_TileMask_Material'))
    textures = terrain_mat['textures'][1]
    for key,suffix in [('colorTexture','color'),('tileMask','mask'),('tileTexture0','detail')]:
        textures[key] = (textures[key][0], f'landscape/solbreaker_range_{suffix}.tex')
    terrain_mat['flags'][1].pop('LANDSCAPE_HEIGHT_BLEND',None)
    # A repeat covers 4.9 m. The ortho map supplies large-scale coloration.
    tiling = terrain_mat['properties'][1]['textureTiling'][1]
    terrain_mat['properties'][1]['textureTiling'] = 6,tiling[:5]+struct.pack('<2f',208.,208.)
    # Keep the soft mountain haze beyond the central plaza and its buildings.
    for _, config in top['#sceneComponentSets'][1]['Default'][1]:
        if 'fogStart' in config and 'fogEnd' in config:
            config['fogStart'] = 3,struct.pack('<f',205.)
            config['fogEnd'] = 3,struct.pack('<f',910.)
    water_parent = next(_q(struct.unpack('<Q',n['#id'][1])[0]) for _,n in data_nodes
                        if n.get('materialName') == (4,'sea'))
    for key in ('tileColor0','tileColor1','tileColor2','tileColor3'):
        v = terrain_mat['properties'][1][key][1]
        terrain_mat['properties'][1][key] = 6, v[:5]+struct.pack('<3f',1.,1.,1.)
    if geometry_bytes[:8] != b'SCPG\x01\x00\x00\x00':
        raise ValueError('Unexpected native polygon stream')
    count,duplicate = struct.unpack_from('<II',geometry_bytes,8)
    if count != duplicate:
        raise ValueError('Inconsistent polygon count')
    groups = [geometry_bytes[16:]]
    hash_value = struct.unpack_from('<Q',descriptor,4)[0]
    assemblies = {}
    objects = [*objects, *ground_cover(surface, height)]
    for i, obj in enumerate(objects):
        node, material, ident, chunk = _mesh(obj, i, transform, water_parent)
        building = obj.get('assembly')
        if building is None:
            root.append(node)
        else:
            render = _components(node[1])['RenderComponent']['rc.renderObj'][1]
            if building in assemblies:
                _merge_batch(assemblies[building],render)
            else:
                node[1]['name'] = (4,building)
                _shift(node[1],obj['origin'],iter((50000+i,)))
                assemblies[building] = render
                root.append(node)
        data_nodes.append(material)
        groups.append(chunk)
        hash_value ^= (((((ident << 26) + hash_value) << 6) + (hash_value >> 2)
                        + 0x9e3779b99e3779b9) ^ ident) & 0xffffffffffffffff
    count += len(objects)
    polygons = b'SCPG' + struct.pack('<3I',1,count,count) + b''.join(groups)
    struct.pack_into('<QQ',descriptor,4,hash_value,zlib.crc32(polygons))
    index, counts = {}, {}
    ids = iter(range(100000,200000))
    rng = random.Random(514)
    ambience = ((name, x, y, False) for name, x, y in _ambience())
    for name,x,y,breakable in (*_props(), *vegetation(surface), *ambience):
        source_nodes = sources[name]
        template = rng.choice(source_nodes) if 'tree' in name or 'bush' in name or 'flowers' in name else source_nodes[0]
        node = copy.deepcopy(template)
        part = _components(node)['TransformComponent']
        previous = struct.unpack('<3f',part['tc.worldTranslation'][1])
        altitude = AIRCRAFT_ALTITUDE_OFFSET if name.startswith('env_rd_plane_03') else 0.
        delta = tuple(val-old for val,old in zip((x,y,height(x,y)+altitude),previous))
        _shift(node,delta,ids)
        if breakable:
            area = ((math.floor(x/100)+127)<<8) | (math.floor(y/100)+127)
            local = counts.get(area,0)
            counts[area] = local+1
            entity_id = struct.unpack('<I',node['id'][1])[0]
            index[str(entity_id)] = _u((area<<16)|local)
        root.append((8,node))
    top['#hierarchy'] = 27,root
    header = b'SFV2'+struct.pack('<II',48,len(root))+scene_bytes[12:20]+_u32(20)+descriptor
    return header+_Table().encode(top), polygons, _archive1(index)
