"""Restore recorded mouse sensitivity into the native local options cache."""
import os
from pathlib import Path
import struct

from .connection import atomic_write
from .dava_scene import Archive
from .platform_io import open_private_file


_SENSITIVITY = ('MouseSensitivity', 'MouseSensitivityInSnipperMode')
_SAVED = struct.Struct('<IIIff')
_MAGIC = int.from_bytes(b'SBC1', 'little')
_ARCHIVES = ('game_options_local_options.dat', 'game_options_local_options.dat.bk')


def _read_private(directory, name, limit):
    try:
        fd = open_private_file(directory, name)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        raw = stream.read(limit + 1)
    return raw if len(raw) <= limit else None


def _sensitivity_archive(raw, overrides):
    """Replace native KA v1 string values in place, preserving unrelated entries."""
    reader = Archive(raw)
    if reader.take(4) != b'KA\x01\x00':
        raise ValueError('Unsupported local options archive')
    count = reader.number('I')
    if count > len(raw):
        raise ValueError('Invalid local options archive count')
    result = bytearray()
    copied = 0
    for _ in range(count):
        key = reader.value(reader.number('B'), 1)
        kind = reader.number('B')
        start = reader.pos
        reader.value(kind, 1)
        if isinstance(key, str) and kind == 4 and key in overrides:
            value = overrides[key]
            result += raw[copied:start] + struct.pack('<I', len(value)) + value
            copied = reader.pos
    if reader.pos != len(raw):
        raise ValueError('Trailing native local options data')
    return bytes(result) + raw[copied:]


def restore_mouse_sensitivity(documents):
    """Run while the prepared client is idle, inside its configuration lock."""
    documents = Path(documents)
    raw = _read_private(documents, 'solbreaker-controls.bin', _SAVED.size)
    if raw is None or len(raw) != _SAVED.size:
        return
    magic, valid, _, regular, sniper = _SAVED.unpack(raw)
    if magic != _MAGIC:
        return
    overrides = {
        key: f'{max(.05, min(1.5, value)):.6f}'.encode('ascii')
        for index, (key, value) in enumerate(zip(_SENSITIVITY, (regular, sniper)), 4)
        if valid & (1 << index) and .049 <= value <= 1.501
    }
    if not overrides:
        return
    cache = documents/'DAVAProject/cache'
    for name in _ARCHIVES:
        raw = _read_private(cache, name, 4 << 20)
        if raw is None:
            continue
        try:
            updated = _sensitivity_archive(raw, overrides)
        except ValueError:
            # Leave an unfamiliar cache for the native client to handle.
            continue
        if updated != raw:
            atomic_write(cache/name, updated)
