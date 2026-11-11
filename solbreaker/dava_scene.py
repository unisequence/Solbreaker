"""Read the installed DAVA scene/geometry archives; no game assets are bundled.

KeyedArchive type IDs follow DAVA VariantType. The pinned scenes use versions
1, 2 and 258 (a nested archive sharing its parent's string table).
"""
from dataclasses import dataclass
from pathlib import Path
import struct

import numpy as np

from .dvpl import read


class Archive:
    def __init__(self, data, strings=None):
        self.data, self.pos, self.strings = data, 0, strings

    def take(self, length):
        if length < 0 or self.pos+length > len(self.data):
            raise ValueError('Truncated DAVA archive')
        result = self.data[self.pos:self.pos+length]
        self.pos += length
        return result

    def string(self, length):
        # Native strings are byte strings (std::string), not necessarily UTF-8.
        # surrogateescape keeps ordinary UTF-8 intact and round-trips other bytes.
        return self.take(length).decode('utf-8', 'surrogateescape')

    def number(self, fmt):
        return struct.unpack('<'+fmt, self.take(struct.calcsize('<'+fmt)))[0]

    def value(self, kind, version):
        formats = {1:'B', 2:'i', 3:'f', 7:'I', 9:'q', 10:'Q', 21:'d',
                   22:'b', 23:'B', 24:'h', 25:'H'}
        if kind == 0:
            return None
        if kind in formats:
            return self.number(formats[kind])
        if kind in (4, 5, 18, 20):
            if version != 1:
                return self.strings[self.number('I')]
            return self.string(self.number('I'))
        if kind == 6:
            return self.take(self.number('I'))
        if kind == 8:
            child = Archive(self.take(self.number('I')), self.strings)
            result = child.load()
            if child.pos != len(child.data):
                raise ValueError('Trailing nested archive data')
            return result
        lengths = {11:2, 12:3, 13:4, 14:4, 15:9, 16:16, 17:4, 19:6, 29:10}
        if kind in lengths:
            n = lengths[kind]
            return struct.unpack('<'+'f'*n, self.take(n*4))
        if kind == 27:
            count = self.number('I')
            if count > len(self.data)-self.pos:
                raise ValueError('Invalid archive array length')
            return [self.value(self.number('B'), version) for _ in range(count)]
        raise ValueError(f'Unsupported DAVA variant {kind}')

    def load(self):
        if self.take(2) != b'KA':
            raise ValueError('Invalid KeyedArchive magic')
        version = self.number('H')
        if version == 0xff02 and self.pos == len(self.data):
            return {}  # Native compact empty-archive marker KA 02 FF.
        if version not in (1, 2, 258):
            raise ValueError(f'Unsupported KeyedArchive version {version}')
        if self.pos == len(self.data):
            return {}  # Empty nested archives can contain only the header.
        count = self.number('I')
        if count > len(self.data):
            raise ValueError('Invalid KeyedArchive count')
        if version == 2:
            strings = [self.string(self.number('H')) for _ in range(count)]
            self.strings = {self.number('I'): value for value in strings}
            count = self.number('I')
        result = {}
        for _ in range(count):
            key = (self.value(self.number('B'), version) if version == 1
                   else self.strings[self.number('I')])
            result[key] = self.value(self.number('B'), version)
        return result


def scene(path):
    reader = Archive(read(Path(path)))
    if reader.take(4) != b'SFV2':
        raise ValueError('Invalid scene magic')
    version = reader.number('I')
    node_count = reader.number('I')
    # Pinned vehicle scenes share the archive envelope and world transforms.
    # 35/41/43 use 12-byte descriptors; 47/48 use 20-byte descriptors.
    if version not in (35, 41, 43, 47, 48):
        raise ValueError(f'Unsupported pinned scene version {version}')
    reader.load()  # Scene metadata.
    reader.take(reader.number('I'))  # Version-specific file descriptors.
    # Client scenes use the name-table layout (a KA v2 archive that contains
    # the string table and top-level fields). Resource Editor's SaveScene also
    # writes the older v35 layout: a count of inline data-node archives,
    # optional global-material archive, then depth-first entity archives.
    # Both have the same SFV2 header and descriptor, so select by the next KA
    # version instead of assuming every v35 scene is a client name-table file.
    if reader.data[reader.pos:reader.pos + 4] == b'KA\x02\x00':
        result = reader.load()
    else:
        data_node_count = reader.number('i')
        if data_node_count < 0 or data_node_count > (len(reader.data) - reader.pos) // 8:
            raise ValueError('Invalid legacy scene data-node count')
        for _ in range(data_node_count):
            reader.load()

        def is_archive(position):
            return reader.data[position:position + 4] in (b'KA\x01\x00', b'KA\x02\x00')

        # Older SaveScene versions inserted an opaque eight-byte prefix before
        # the global-material archive. Stock v22 editor meshes sometimes omit
        # it; recognize it only when it leads directly to a valid KA header.
        if node_count and not is_archive(reader.pos):
            if is_archive(reader.pos + 8):
                reader.take(8)
            else:
                raise ValueError('Invalid legacy scene archive prefix')

        if node_count:
            archive_start = reader.pos
            possible_global = reader.load()
            if possible_global.get('##name') != 'GlobalMaterial':
                reader.pos = archive_start

        def load_legacy_node(depth=0):
            if depth > 256:
                raise ValueError('Legacy scene hierarchy is too deep')
            node = reader.load()
            children = node.get('#childrenCount', 0)
            if type(children) is not int or children < 0 or children > (len(reader.data) - reader.pos) // 8:
                raise ValueError('Invalid legacy scene child count')
            node['#hierarchy'] = [load_legacy_node(depth + 1) for _ in range(children)]
            return node

        if node_count < 0 or node_count > (len(reader.data) - reader.pos) // 8:
            raise ValueError('Invalid legacy scene root count')
        result = {'#hierarchy': [load_legacy_node() for _ in range(node_count)]}
    if reader.pos != len(reader.data):
        raise ValueError('Trailing scene data')
    return result


@dataclass
class Mesh:
    vertices: np.ndarray
    triangles: np.ndarray
    material_ids: np.ndarray | None = None


def geometry(path):
    reader = Archive(read(Path(path)))
    if reader.take(4) != b'SCPG' or reader.number('I') != 1:
        raise ValueError('Unsupported geometry archive')
    count = reader.number('I')
    if count != reader.number('I') or count > 100000:
        raise ValueError('Invalid geometry count')
    result = {}
    for _ in range(count):
        group = reader.load()
        n = group['vertexCount']
        if n <= 0 or len(group['vertices']) % n or not group['vertexFormat'] & 1:
            raise ValueError('Invalid vertex buffer')
        stride = len(group['vertices']) // n
        vertices = np.ndarray((n, 3), dtype='<f4', buffer=group['vertices'],
                              strides=(stride, 4)).astype(float)
        dtype = {0:'<u2', 1:'<u4'}[group['indexFormat']]
        indices = np.frombuffer(group['indices'], dtype=dtype).astype(np.int32)
        if len(indices) != group['indexCount'] or np.any(indices >= n):
            raise ValueError('Invalid mesh indices')
        if group['rhi_primitiveType'] == 1:
            triangles = indices.reshape(-1, 3)
        elif group['rhi_primitiveType'] == 2:
            triangles = np.array([(indices[i], indices[i+1+(i%2)], indices[i+2-(i%2)])
                                  for i in range(len(indices)-2)], dtype=np.int32)
        else:
            raise ValueError('Unsupported mesh primitive')
        material_ids = None
        # Pinned armor meshes: position/normal, one auxiliary float and
        # a float material index. Keep the index per vertex, not per hull.
        if group['vertexFormat'] == 0x207 and stride == 32:
            ids = np.ndarray((n,), dtype='<f4', buffer=group['vertices'],
                             offset=28, strides=(stride,))
            if not np.all(np.isfinite(ids)) or not np.all(ids == np.floor(ids)):
                raise ValueError('Invalid collision material IDs')
            material_ids = ids.astype(np.int32)
        result[int.from_bytes(group['#id'], 'little')] = Mesh(vertices, triangles, material_ids)
    if reader.pos != len(reader.data):
        raise ValueError('Trailing geometry data')
    return result


def components(node):
    return {value['comp.typename']: value for value in node.get('components', {}).values()
            if isinstance(value, dict) and 'comp.typename' in value}


def rotation(quaternion):
    x, y, z, w = quaternion
    return np.array(((1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)),
                     (2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)),
                     (2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y))))


def world_vertices(vertices, transform):
    scale = np.array(transform.get('tc.worldScale', (1,1,1)))
    rot = rotation(transform.get('tc.worldRotation', (0,0,0,1)))
    translation = np.array(transform.get('tc.worldTranslation', (0,0,0)))
    # DAVA Z-up -> BigWorld Y-up.
    return ((vertices*scale) @ rot.T + translation)[:, (0,2,1)]


def state_names(switch):
    """Return (registered state child names, active child name or None)."""
    def integer(key):
        value = switch.get(key)
        if type(value) is not int:
            raise ValueError(f'StateSwitcher {key} must be an integer')
        return value
    count, active = integer('ssc.statesCount'), integer('ssc.activeState')
    if count < 0:
        raise ValueError('StateSwitcher ssc.statesCount is negative')
    names = []
    for i in range(count):
        name = switch.get(f'ssc.state{i}')
        if type(name) is not str:
            raise ValueError(f'StateSwitcher ssc.state{i} must be a string')
        names.append(name)
    return set(names), names[active] if 0 <= active < count else None


def active_nodes(nodes, tag='rd0'):
    for node in nodes:
        comps = components(node)
        labels = comps.get('LabelComponent', {}).get('lc.labels', [])
        if labels and tag not in labels:
            continue
        yield node, comps
        children = node.get('#hierarchy', [])
        if 'StateSwitcherComponent' in comps:
            # Native StateSwitcher: registered state children are shown only
            # when their index equals the active one; the owner and any
            # non-state children always stay. Out-of-range indices become -1,
            # which hides every registered state child.
            hidden, shown = state_names(comps['StateSwitcherComponent'])
            children = [child for child in children
                        if child['name'] not in hidden or child['name'] == shown]
        yield from active_nodes(children, tag)
