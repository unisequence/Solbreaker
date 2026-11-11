"""DAVA DVPL wrapper; assets stay in the user's installation or private lab."""
from pathlib import Path
import struct
import zlib

import lz4.block

FOOTER = struct.Struct("<IIII4s")
MAX_RESOURCE_SIZE = 128 * 1024 * 1024


def decode(data: bytes) -> bytes:
    if len(data) < FOOTER.size:
        raise ValueError("Truncated DVPL footer")
    raw_size, size, crc, compression, magic = FOOTER.unpack(data[-FOOTER.size:])
    payload = data[:-FOOTER.size]
    if magic != b"DVPL" or size != len(payload):
        raise ValueError("Invalid DVPL magic or stored size")
    if raw_size > MAX_RESOURCE_SIZE:
        raise ValueError("Resource exceeds the configured size limit")
    if zlib.crc32(payload) != crc:
        raise ValueError("DVPL CRC mismatch")
    if compression == 0:
        result = payload
    elif compression in (1, 2):
        result = lz4.block.decompress(payload, uncompressed_size=raw_size)
    else:
        raise ValueError(f"Unsupported DVPL compression {compression}")
    if len(result) != raw_size:
        raise ValueError("DVPL uncompressed size mismatch")
    return result


def encode(data: bytes) -> bytes:
    """Uncompressed DVPL is also used by the installed client (version.txt)."""
    if len(data) > MAX_RESOURCE_SIZE:
        raise ValueError("Resource exceeds the configured size limit")
    return data + FOOTER.pack(len(data), len(data), zlib.crc32(data), 0, b"DVPL")


def read(path: Path) -> bytes:
    return decode(path.read_bytes()) if path.suffix == ".dvpl" else path.read_bytes()
