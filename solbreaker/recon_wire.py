"""Experimental recon snapshot payload, not connected to any game message.

The bytes encode the two packed u32 vectors read by pinned Avatar method 37.
This codec does not make that method safe to send: the receiver must first have
an independently verified sentinel hook, capability handshake and bounded
native decoder. In particular, vanilla clients must never receive this payload.

V1 first vector, little-endian u32 words: two magic words, version, 128-bit
battle UUID (four words), monotonically increasing sequence, flags, ID count,
IDs, CRC32. The second vector is empty. CRC detects accidental corruption; it
is not authentication. Each snapshot is complete for one recipient.
"""
from dataclasses import dataclass
import struct
import uuid
import zlib

MAGIC = (0x31534654, 0x4E4F4345)  # TFS1 ECON, two independent words
VERSION = 1
MAX_IDS = 49
MAX_PAYLOAD = 242  # 1 + 60*4 + 1, below Mercury variable8 extended form
_U32 = 0xffffffff


def _number(value, name, low=0):
    if type(value) is not int or not low <= value <= _U32:
        raise ValueError(f'Invalid {name}')
    return value


@dataclass(frozen=True)
class ReconSnapshot:
    battle_id: uuid.UUID
    sequence: int
    lamp: bool
    markers: tuple[int, ...]

    def __post_init__(self):
        if not isinstance(self.battle_id, uuid.UUID) or self.battle_id.int == 0:
            raise ValueError('Invalid battle ID')
        _number(self.sequence, 'sequence')
        if type(self.lamp) is not bool:
            raise ValueError('Invalid lamp flag')
        if not isinstance(self.markers, tuple) or len(self.markers) > MAX_IDS:
            raise ValueError('Invalid marker count')
        for entity in self.markers:
            _number(entity, 'entity ID', 1)
        if tuple(sorted(set(self.markers))) != self.markers:
            raise ValueError('Marker IDs must be unique and sorted')


def encode(snapshot):
    """Return method body only, never a framed or dispatched game message."""
    if not isinstance(snapshot, ReconSnapshot):
        raise ValueError('Invalid snapshot')
    battle = struct.unpack('<4I', snapshot.battle_id.bytes)
    words = (*MAGIC, VERSION, *battle, snapshot.sequence, int(snapshot.lamp),
             len(snapshot.markers), *snapshot.markers)
    content = struct.pack(f'<{len(words)}I', *words)
    crc = zlib.crc32(content)
    return bytes([len(words) + 1]) + content + struct.pack('<I', crc) + b'\0'


def decode(payload):
    """Decode and fully validate the bounded payload; malformed data raises ValueError."""
    if not isinstance(payload, bytes) or not 46 <= len(payload) <= MAX_PAYLOAD:
        raise ValueError('Invalid snapshot length')
    count = payload[0]
    if not 11 <= count <= 60 or len(payload) != 2 + count * 4 or payload[-1] != 0:
        raise ValueError('Invalid vector framing')
    content = payload[1:-5]
    if zlib.crc32(content) != struct.unpack_from('<I', payload, len(payload) - 5)[0]:
        raise ValueError('Snapshot checksum mismatch')
    words = struct.unpack(f'<{count - 1}I', content)
    if words[:2] != MAGIC or words[2] != VERSION:
        raise ValueError('Invalid magic or unsupported version')
    if words[8] not in (0, 1) or words[9] != len(words) - 10:
        raise ValueError('Invalid flags or marker count')
    battle_id = uuid.UUID(bytes=struct.pack('<4I', *words[3:7]))
    return ReconSnapshot(battle_id, words[7], bool(words[8]), tuple(words[10:]))


class SnapshotReceiver:
    """Per-battle ordering guard. Construct anew on battle/session reset.

    Sequence numbers cannot wrap within one battle. A new battle UUID requires
    a fresh receiver. Invalid and stale snapshots leave state untouched.
    """
    def __init__(self, battle_id):
        if not isinstance(battle_id, uuid.UUID) or battle_id.int == 0:
            raise ValueError('Invalid battle ID')
        self.battle_id = battle_id
        self.latest = None

    def accept(self, payload):
        snapshot = decode(payload)
        if snapshot.battle_id != self.battle_id:
            raise ValueError('Snapshot belongs to another battle')
        if self.latest is not None and snapshot.sequence <= self.latest.sequence:
            raise ValueError('Stale snapshot')
        self.latest = snapshot
        return snapshot
