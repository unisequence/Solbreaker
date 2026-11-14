"""DAVA model archive encoding adapted from the ZSU-57-2 prototype."""
from dataclasses import dataclass
import struct
import zlib


@dataclass(eq=True)
class Value:
    kind: int
    payload: object


FIXED = {0: 0, 1: 1, 2: 4, 3: 4, 7: 4, 9: 8, 10: 8, 11: 8,
         12: 12, 13: 16, 14: 16, 15: 36, 16: 64, 17: 16, 19: 24,
         21: 8, 22: 1, 23: 1, 24: 2, 25: 2, 29: 40}
MAX_FILE = 64 * 1024**2


class Reader:
    def __init__(self, data):
        if len(data) > MAX_FILE:
            raise ValueError('Model archive exceeds the 64 MiB conversion limit')
        self.data, self.pos = data, 0

    def take(self, count):
        if count < 0 or count > len(self.data) - self.pos:
            raise ValueError('Truncated model archive')
        result = self.data[self.pos:self.pos + count]
        self.pos += count
        return result

    def number(self, fmt='I'):
        return struct.unpack('<' + fmt, self.take(struct.calcsize('<' + fmt)))[0]

    def count(self, minimum=1):
        count = self.number()
        if count > 100000 or count > (len(self.data) - self.pos) // minimum:
            raise ValueError('Invalid model archive element count')
        return count

    def value(self, depth=0):
        if depth > 128:
            raise ValueError('Model archive nesting limit exceeded')
        kind = self.number('B')
        if kind in FIXED:
            return Value(kind, self.take(FIXED[kind]))
        if kind in (4, 6, 18):
            return Value(kind, self.take(self.number()))
        if kind == 8:
            child = Reader(self.take(self.number()))
            archive = child.archive(depth + 1)
            child.finish()
            return Value(kind, archive)
        if kind == 27:
            return Value(kind, [self.value(depth + 1) for _ in range(self.count())])
        raise ValueError(f'Cannot preserve legacy variant type {kind} in a name table')

    def archive(self, depth=0):
        if depth > 128 or self.take(4) != b'KA\x01\x00':
            raise ValueError('Expected a legacy KA v1 archive')
        result = {}
        for _ in range(self.count(2)):
            key = self.value(depth + 1)
            if key.kind != 4 or key.payload in result:
                raise ValueError('Invalid or duplicate legacy archive key')
            result[key.payload] = self.value(depth + 1)
        return result

    def finish(self):
        if self.pos != len(self.data):
            raise ValueError('Trailing model archive data')


def name_id(name):
    crc = zlib.crc32(name)
    return (crc & 0xFFFE0000) | ((crc & 0x7FFF) << 2)


def encode_table(top):
    names = set()

    def collect(archive, depth=0):
        if depth > 128:
            raise ValueError('Model archive nesting limit exceeded')
        for key, value in archive.items():
            names.add(key)
            collect_value(value, depth + 1)

    def collect_value(value, depth):
        if depth > 128:
            raise ValueError('Model archive nesting limit exceeded')
        if value.kind in (4, 18):
            names.add(value.payload)
        elif value.kind == 8:
            collect(value.payload, depth + 1)
        elif value.kind == 27:
            for element in value.payload:
                collect_value(element, depth + 1)

    collect(top)
    names = sorted(names)
    ids = {name: name_id(name) for name in names}
    if len(set(ids.values())) != len(names):
        raise ValueError('Name-table hash collision; refusing model export')
    if any(len(name) > 65535 for name in names):
        raise ValueError('Name-table string is too long')

    def items(archive):
        out = bytearray(struct.pack('<I', len(archive)))
        for key, value in sorted(archive.items()):
            out += struct.pack('<I', ids[key]) + variant(value)
        return bytes(out)

    def variant(value):
        kind, data = value.kind, value.payload
        prefix = bytes([kind])
        if kind in FIXED:
            if len(data) != FIXED[kind]:
                raise ValueError('Invalid fixed variant payload')
            return prefix + data
        if kind in (4, 18):
            return prefix + struct.pack('<I', ids[data])
        if kind == 6:
            return prefix + struct.pack('<I', len(data)) + data
        if kind == 8:
            raw = b'KA\x02\xff' if not data else b'KA\x02\x01' + items(data)
            return prefix + struct.pack('<I', len(raw)) + raw
        if kind == 27:
            return prefix + struct.pack('<I', len(data)) + b''.join(map(variant, data))
        raise ValueError(f'Unsupported name-table variant {kind}')

    table = b'KA\x02\x00' + struct.pack('<I', len(names))
    table += b''.join(struct.pack('<H', len(name)) + name for name in names)
    table += b''.join(struct.pack('<I', ids[name]) for name in names)
    return table + items(top)


def legacy_archive(values):
    """Encode only the small, explicitly generated marker polygon group."""
    def variant(value):
        raw = value.payload
        if value.kind in (4, 6):
            raw = struct.pack('<I', len(raw)) + raw
        elif value.kind not in FIXED:
            raise ValueError('Unsupported generated geometry value')
        return bytes([value.kind]) + raw
    return (b'KA\x01\x00' + struct.pack('<I', len(values)) +
            b''.join(variant(Value(4, key)) + variant(value) for key, value in values.items()))



class ClientScene:
    def __init__(self, raw, *, versions=(35,)):
        reader = Reader(raw)
        if reader.take(4) != b'SFV2' or reader.number() not in versions:
            raise ValueError('Unsupported DAVA scene version')
        reader.number()
        reader.archive()
        metadata_size = reader.number()
        self.geometry_fingerprint_offset = reader.pos + 4
        reader.take(metadata_size)
        self.header = raw[:reader.pos]
        if reader.take(4) != b'KA\x02\x00':
            raise ValueError('Editing a muzzle requires a name-table DAVA model')
        names = [reader.take(reader.number('H')) for _ in range(reader.count(2))]
        self.names = {reader.number(): name for name in names}
        self.fields = self.fields_from(reader)
        reader.finish()

    def fields_from(self, reader, depth=0):
        if depth > 128:
            raise ValueError('Model nesting limit exceeded')
        return {self.names[reader.number()]: self.value_from(reader, depth + 1)
                for _ in range(reader.count(5))}

    def value_from(self, reader, depth):
        if depth > 128:
            raise ValueError('Model nesting limit exceeded')
        kind = reader.number('B')
        if kind in FIXED:
            return Value(kind, reader.take(FIXED[kind]))
        if kind in (4, 18):
            return Value(kind, self.names[reader.number()])
        if kind == 6:
            return Value(kind, reader.take(reader.number()))
        if kind == 8:
            inner = Reader(reader.take(reader.number()))
            header = inner.take(4)
            if header == b'KA\x02\xff':
                inner.finish()
                return Value(8, {})
            if header != b'KA\x02\x01':
                raise ValueError('Unsupported nested model archive')
            fields = self.fields_from(inner, depth + 1)
            inner.finish()
            return Value(8, fields)
        if kind == 27:
            return Value(kind, [self.value_from(reader, depth + 1) for _ in range(reader.count())])
        raise ValueError(f'Unsupported model variant {kind}')

    def node(self, name):
        nodes = list(self.fields[b'#hierarchy'].payload)
        found = []
        while nodes:
            current = nodes.pop().payload
            if current.get(b'name') == Value(4, name.encode()):
                found.append(current)
            nodes.extend(current.get(b'#hierarchy', Value(27, [])).payload)
        if len(found) != 1:
            raise ValueError(f'Expected one model node {name}')
        return found[0]

    def encode(self):
        return self.header + encode_table(self.fields)
