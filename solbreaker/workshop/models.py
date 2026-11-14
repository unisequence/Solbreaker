"""Read preview meshes and prepare the client's DAVA model and textures."""
from pathlib import Path
import struct

import numpy as np

from .. import dava_scene, dvpl
from ..collision import batches
from .project import asset_path, local_asset

MAX_ASSET = 64 * 1024**2
POINT_NODE = 'HP_gunFire_01'
TRACK_NAMES = ('chassis_track_L', 'chassis_track_R')
VISUAL_VERSIONS = (35, 41, 43, 47, 48)


def read_asset(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_ASSET + 1)
    if len(raw) > MAX_ASSET:
        raise ValueError(f'Asset exceeds 64 MiB: {Path(path).name}')
    return dvpl.decode(raw) if Path(path).suffix == '.dvpl' else raw


def texture_refs(document):
    refs = set()
    for node in document.get('#dataNodes', ()):
        if node.get('##name') == 'NMaterial':
            for value in node.get('textures', {}).values():
                if isinstance(value, str) and value.endswith('.tex'):
                    refs.add(asset_path(value).as_posix())
    return refs


def model_preview(scene, geometry):
    for path in (Path(scene), Path(geometry)):
        if path.stat().st_size > MAX_ASSET:
            raise ValueError(f'Asset exceeds 64 MiB: {path.name}')
    document, groups = dava_scene.scene(scene), dava_scene.geometry(geometry)
    parts, markers = {}, {}
    for node, comps in dava_scene.active_nodes(document['#hierarchy']):
        name = node.get('name', '')
        for batch in batches(comps):
            mesh = groups[batch['rb.datasource']]
            points = dava_scene.world_vertices(mesh.vertices, comps.get('TransformComponent', {}))
            if not np.all(np.isfinite(points)) or np.abs(points).max() > 100:
                raise ValueError(f'Invalid geometry in {name}')
            if name.startswith('HP_'):
                markers.setdefault(name, (points.min(0) + points.max(0)) / 2)
                continue
            parts.setdefault(name, []).append(points[mesh.triangles])
    if not parts:
        raise ValueError('Model contains no visible triangles')
    return {name: np.concatenate(meshes) for name, meshes in parts.items()}, markers


def validate_collision_geometry(path):
    reader = dava_scene.Archive(read_asset(path))
    if reader.take(8) != b'SCPG\x01\x00\x00\x00':
        raise ValueError('Unsupported collision geometry archive')
    count = reader.number('I')
    if count != reader.number('I') or count > 100000:
        raise ValueError('Invalid collision mesh count')
    for _ in range(count):
        group = reader.load()
        if group['indexFormat'] != 0 or group['vertexCount'] > 65535:
            raise ValueError('Split collision meshes into groups with 16-bit indices before importing')


def check_model(project, root):
    visual_scene, visual_geometry, collision_scene, collision_geometry = (
        local_asset(root, getattr(project, name)) for name in (
            'visual_scene', 'visual_geometry', 'collision_scene', 'collision_geometry'))
    parts, markers = model_preview(visual_scene, visual_geometry)
    validate_collision_geometry(collision_geometry)
    collision, _ = model_preview(collision_scene, collision_geometry)
    if not {'hull', 'turret_01', 'gun_01'} <= parts.keys():
        raise ValueError('Visual scene requires hull, turret_01 and gun_01')
    if not project.running_gear and not set(TRACK_NAMES) <= parts.keys():
        raise ValueError('Add both track meshes or choose running gear in Workshop')
    if 'hull' not in collision:
        raise ValueError('Collision scene requires a hull mesh')
    if POINT_NODE not in markers:
        raise ValueError(f'Visual scene requires the {POINT_NODE} muzzle marker')
    refs = texture_refs(dava_scene.scene(visual_scene))
    if refs != project.textures.keys():
        missing = sorted(refs - project.textures.keys())
        extra = sorted(project.textures.keys() - refs)
        raise ValueError(f'Material textures differ from the project (missing {missing}, extra {extra})')
    return parts, markers


def texture_dds(path):
    raw = read_asset(path)
    if raw.startswith(b'DDS '):
        if len(raw) < 128:
            raise ValueError('Truncated DDS texture')
        header, height, width = struct.unpack_from('<I4xII', raw, 4)
        if header != 124 or not (1 <= width <= 4096 and 1 <= height <= 4096):
            raise ValueError('Invalid DDS dimensions')
        return raw
    # Only the export workstation needs Qt. Packages carry the resulting DDS.
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QImage, QImageReader
    buffer = QBuffer()
    buffer.setData(QByteArray(raw))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    size = reader.size()
    width, height = size.width(), size.height()
    if bytes(reader.format()).lower() not in (b'png', b'jpeg', b'jpg'):
        raise ValueError('Expected a PNG, JPEG or DDS texture')
    if not (1 <= width <= 4096 and 1 <= height <= 4096):
        raise ValueError('Texture dimensions must be between 1 and 4096 px')
    current = reader.read().convertToFormat(QImage.Format.Format_RGBA8888)
    if current.isNull():
        raise ValueError(f'Cannot read texture: {reader.errorString()}')
    pixels = [bytes(current.constBits())]
    while (current.width(), current.height()) != (1, 1):
        current = current.scaled(max(1, current.width()//2), max(1, current.height()//2),
            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        pixels.append(bytes(current.constBits()))
    flags = 0x2100f if len(pixels) > 1 else 0x100f
    caps = 0x401008 if len(pixels) > 1 else 0x1000
    header = struct.pack(
        '<7I11I8I5I5I', 124, flags, height, width, width*4, 0, len(pixels), *([0]*11),
        32, 4, 0x30315844, 0, 0, 0, 0, 0, caps, 0, 0, 0, 0, 28, 3, 0, 1, 0)
    return b'DDS ' + header + b''.join(pixels)


def move_muzzle(scene_raw, geometry_raw, old_point, new_point, *, marker_name=POINT_NODE):
    """Translate the baked muzzle mesh and its AABB."""
    return move_visual_node(scene_raw, geometry_raw, marker_name, np.asarray(new_point)-old_point)


def move_visual_node(scene_raw, geometry_raw, name, world_delta):
    """Translate one baked node in a name-table SC2/SCG pair."""
    delta = world_delta[[0, 2, 1]]
    if np.max(np.abs(delta)) < 1e-5:
        return scene_raw, geometry_raw
    from .dava_export import ClientScene, Reader, legacy_archive
    scene = ClientScene(scene_raw, versions=VISUAL_VERSIONS)
    marker = scene.node(name)
    comps = {value.payload[b'comp.typename'].payload: value.payload
             for value in marker[b'components'].payload.values() if value.kind == 8}
    render = comps[b'RenderComponent'][b'rc.renderObj'].payload
    refs = set()
    for value in render[b'ro.batches'].payload.values():
        batch = value.payload
        refs.add(int.from_bytes(batch[b'rb.datasource'].payload, 'little'))
        bounds = np.array(struct.unpack('<6f', batch[b'rb.aabbox'].payload)).reshape(2, 3)
        batch[b'rb.aabbox'].payload = struct.pack('<6f', *(bounds + delta).flatten())
    reader = Reader(geometry_raw)
    header = reader.take(16)
    count = struct.unpack_from('<I', header, 8)[0]
    groups = []
    for _ in range(count):
        group = reader.archive()
        if int.from_bytes(group[b'#id'].payload, 'little') in refs:
            raw = bytearray(group[b'vertices'].payload)
            vertices = int.from_bytes(group[b'vertexCount'].payload, 'little')
            points = np.ndarray((vertices, 3), dtype='<f4', buffer=raw, strides=(len(raw)//vertices, 4))
            points += delta
            group[b'vertices'].payload = bytes(raw)
        groups.append(legacy_archive(group))
    reader.finish()
    return scene.encode(), header + b''.join(groups)
