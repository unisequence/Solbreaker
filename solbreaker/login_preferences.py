"""Apply the native client's automatic-login flag to its private device archive."""
import os
from pathlib import Path
import struct

from .connection import atomic_write
from .dava_scene import Archive
from .platform_io import open_private_file


def automatic_login_archive(raw, enabled):
    if type(enabled) is not bool:
        raise ValueError('Automatic login must be a boolean')
    reader = Archive(raw)
    if reader.take(4) != b'KA\x01\x00':
        raise ValueError('Unsupported saved-login archive format')
    count = reader.number('I')
    if count > len(raw):
        raise ValueError('Invalid saved-login archive count')
    offset = None
    seen = set()
    for _ in range(count):
        key = reader.value(reader.number('B'), 1)
        if not isinstance(key, str) or key in seen:
            raise ValueError('Invalid saved-login archive key')
        seen.add(key)
        kind = reader.number('B')
        position = reader.pos
        value = reader.value(kind, 1)
        if key == 'UseAutoLogin':
            if kind != 1 or value not in (0, 1):
                raise ValueError('Invalid automatic-login value')
            offset = position
    if reader.pos != len(raw):
        raise ValueError('Trailing saved-login archive data')
    result = bytearray(raw)
    if offset is None:
        struct.pack_into('<I', result, 4, count+1)
        key = b'UseAutoLogin'
        result += b'\x04'+struct.pack('<I', len(key))+key+b'\x01'+bytes([enabled])
    else:
        result[offset] = enabled
    return bytes(result)


def configure_automatic_login(documents, enabled):
    """Called only while the owning client's configuration lock is held and it is idle."""
    if type(enabled) is not bool:
        raise ValueError('Automatic login must be a boolean')
    cache = Path(documents)/'DAVAProject/cache'
    changed = 0
    pending = []
    for name in ('device_data.dat', 'device_data.dat.bk'):
        path = cache/name
        if not path.exists():
            continue
        descriptor = open_private_file(cache, name)
        with os.fdopen(descriptor, 'rb') as stream:
            if os.fstat(stream.fileno()).st_size > 4 << 20:
                raise ValueError('Saved-login archive is too large')
            raw = stream.read()
        updated = automatic_login_archive(raw, enabled)
        if updated != raw:
            pending.append((path, updated))
    for path, updated in pending:
        atomic_write(path, updated)
        changed += 1
    return changed
