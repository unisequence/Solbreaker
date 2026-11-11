"""A strict subset of the BigWorld Mercury off-channel protocol.

Wire layout cross-referenced with the MIT wg-toolkit implementation. This is
not a claim that the PC WoT entity protocol works with the pinned client.
"""
from dataclasses import dataclass, replace
import struct

REQUESTS = 0x01
PIGGYBACKS = 0x02
ACKS = 0x04
CHANNEL = 0x08
RELIABLE = 0x10
FRAGMENT = 0x20
SEQUENCE = 0x40
CHECKSUM = 0x100
CREATE_CHANNEL = 0x200
CUMULATIVE_ACK = 0x400
LAST_RELIABLE = 0x1000
CHANNEL_VERSION = 0x2000
SEQ_MASK = 0x0fffffff
SEQ_HALF = 0x08000000


def variable8(kind, body):
    """Mercury's oversized form moves the first four payload bytes to the end."""
    size = len(body)
    if size < 255:
        return bytes([kind,size])+body
    if size > 0xffffffff:
        raise ValueError('Message exceeds Mercury length limit')
    return bytes([kind,255])+struct.pack('<I',size)+body[4:]+body[:4]


def checksum(data: bytes) -> int:
    result = 0
    for offset in range(0, len(data) - 3, 4):
        result ^= struct.unpack_from("<I", data, offset)[0]
    return result


@dataclass(frozen=True)
class Packet:
    flags: int
    body: bytes
    first_request: int | None
    sequence: int | None
    prefix: bool
    acks: tuple[int, ...] = ()
    cumulative_ack: int | None = None
    fragment: tuple[int, int] | None = None
    piggybacks: tuple = ()
    last_reliable: int | None = None
    channel_version: int | None = None


def parse_packet(data: bytes, prefix: bool = False, *, depth=0) -> Packet:
    if depth > 8:
        raise ValueError("Too many nested piggybacks")
    if prefix:
        data = data[4:]
    if len(data) < 2:
        raise ValueError("Truncated packet header")
    flags = struct.unpack_from("<H", data)[0]
    # Accept only the footer fields recovered for this client build.
    if flags & ~(REQUESTS | ACKS | RELIABLE | SEQUENCE | CHECKSUM | CHANNEL | CREATE_CHANNEL | CUMULATIVE_ACK | LAST_RELIABLE | FRAGMENT | CHANNEL_VERSION | PIGGYBACKS):
        raise ValueError(f"Unsupported packet flags {flags:#x}")
    end = len(data)

    def pop(size):
        nonlocal end
        if end - size < 2:
            raise ValueError("Truncated packet footer")
        end -= size
        return data[end:end + size]

    if flags & CHECKSUM:
        expected = struct.unpack("<I", pop(4))[0]
        if checksum(data[:end]) != expected:
            raise ValueError("Packet checksum mismatch")
    piggybacks = []
    if flags & PIGGYBACKS:
        while True:
            length = struct.unpack("<h", pop(2))[0]
            final = length < 0
            size = ~length if final else length
            if size < 2 or len(piggybacks) >= 64:
                raise ValueError("Invalid piggyback length or count")
            piggybacks.append(parse_packet(pop(size), depth=depth + 1))
            if final:
                break
    version = struct.unpack('<I',pop(4))[0] if flags & CHANNEL_VERSION else None
    cumulative = struct.unpack("<I", pop(4))[0] if flags & CUMULATIVE_ACK else None
    acks = ()
    if flags & ACKS:
        count = pop(1)[0]
        if count == 0:
            raise ValueError("Empty acknowledgement footer")
        acks = struct.unpack(f"<{count}I", pop(count * 4))
    seq = struct.unpack("<I", pop(4))[0] if flags & SEQUENCE else None
    last_reliable = struct.unpack('<I',pop(4))[0] if flags & LAST_RELIABLE else None
    first = struct.unpack("<H", pop(2))[0] if flags & REQUESTS else None
    fragment = struct.unpack("<II", pop(8)) if flags & FRAGMENT else None
    if first is not None and not 2 <= first < end:
        raise ValueError("Request offset outside packet body")
    if flags & RELIABLE and seq is None:
        raise ValueError("Reliable packet without sequence number")
    if any(v is not None and not 0 <= v <= SEQ_MASK for v in (seq,cumulative,last_reliable,*acks)):
        raise ValueError('Sequence outside Mercury 28-bit range')
    if last_reliable is not None and (flags & RELIABLE or not flags & CHANNEL or seq is None):
        raise ValueError('Invalid last-reliable footer')
    return Packet(flags, data[2:end], None if first is None else first - 2, seq, prefix,
                  acks, cumulative, fragment, tuple(piggybacks),last_reliable,version)


def login_elements(packet: Packet):
    pos = 0
    request = packet.first_request
    body = packet.body
    while pos < len(body):
        start = pos
        kind = body[pos]
        pos += 1
        if kind in (0, 3):
            if pos + 2 > len(body):
                raise ValueError("Truncated element length")
            size = struct.unpack_from("<H", body, pos)[0]
            pos += 2
        elif kind == 2:
            size = 1
        elif kind in (1, 4):
            size = 0
        else:
            raise ValueError(f"Unknown LoginInterface element {kind}")
        request_id = None
        if start == request:
            if pos + 6 > len(body):
                raise ValueError("Truncated request header")
            request_id, next_request = struct.unpack_from("<IH", body, pos)
            pos += 6
            request = next_request - 2 if next_request >= 2 else None
            if request is not None and request <= start:
                raise ValueError("Request chain is not forward-only")
        if pos + size > len(body):
            raise ValueError("Truncated element body")
        yield kind, request_id, body[pos:pos + size]
        pos += size
    if request is not None:
        raise ValueError("Unresolved request offset")


def encode_reply(packet: Packet, request_id: int, payload: bytes) -> bytes:
    # A reply has reserved element ID 255, uint32 size, uint32 request ID.
    body = b"\xff" + struct.pack("<II", len(payload) + 4, request_id) + payload
    flags = CHECKSUM if packet.flags & CHECKSUM else 0
    footer = b""
    if packet.flags & RELIABLE:
        flags |= ACKS
        footer = struct.pack("<IB", packet.sequence, 1)
    data = struct.pack("<H", flags) + body + footer
    if flags & CHECKSUM:
        data += struct.pack("<I", checksum(data))
    # The observed pinned client prepends four bytes on transmission, but its
    # receiver consumes the flags directly; replies must omit that prefix.
    return data


def packed_blob(data: bytes) -> bytes:
    if len(data) < 255:
        return bytes([len(data)]) + data
    if len(data) > 0xffffff:
        raise ValueError("Blob exceeds 24-bit length")
    return b"\xff" + len(data).to_bytes(3, "little") + data


def read_blob(data: bytes, pos: int) -> tuple[bytes, int]:
    if pos >= len(data):
        raise ValueError("Missing packed blob length")
    size = data[pos]
    pos += 1
    if size == 255:
        if pos + 3 > len(data):
            raise ValueError("Truncated packed blob length")
        size = int.from_bytes(data[pos:pos + 3], "little")
        pos += 3
    if pos + size > len(data):
        raise ValueError("Truncated packed blob")
    return data[pos:pos + size], pos + size


def encode_packet(body=b"", *, sequence=None, acks=(), channel=True, fragment=None,
                  reliable=True,last_reliable=None):
    flags = CHANNEL if channel else 0
    footer = b""
    if fragment is not None:
        if not reliable or sequence is None:
            raise ValueError('Outgoing fragments require reliable delivery')
        flags |= FRAGMENT
        footer += struct.pack("<II", *fragment)
    if last_reliable is not None:
        if reliable or not channel or sequence is None:
            raise ValueError('Invalid last-reliable footer')
        flags |= LAST_RELIABLE
        footer += struct.pack('<I',last_reliable)
    if sequence is not None:
        flags |= SEQUENCE | (RELIABLE if reliable else 0)
        footer += struct.pack("<I", sequence)
    if acks:
        if len(acks) > 255:
            raise ValueError("Too many acknowledgements")
        flags |= ACKS
        footer += struct.pack(f"<{len(acks)}IB", *acks, len(acks))
    return struct.pack("<H", flags) + body + footer


class ReliableInbox:
    """Bounded, ordered Mercury reliable stream, including split bundles."""
    def __init__(self):
        self.expected = 0
        self.waiting = {}
        self.fragments = []
        self.fragment_range = None
        self.last_unreliable = None
        self.unreliable_dropped = 0

    def receive(self, packet):
        if not packet.flags & RELIABLE:
            if packet.fragment:
                raise ValueError("Unreliable fragmented bundle is unsupported")
            # Native 11.21: 0x284a909 compares against expected reliable - 1;
            # 0x2836ab0 filters stale unreliable sequence numbers separately.
            if packet.last_reliable is not None and packet.last_reliable != (self.expected-1)&SEQ_MASK:
                self.unreliable_dropped += 1
                return []
            if packet.sequence is not None:
                if self.last_unreliable is not None and not 0 < (packet.sequence-self.last_unreliable)&SEQ_MASK < SEQ_HALF:
                    self.unreliable_dropped += 1
                    return []
                self.last_unreliable = packet.sequence
            return [packet]
        distance = (packet.sequence - self.expected) & 0x0fffffff
        if distance >= 0x08000000:
            return []  # Already delivered; caller still acknowledges it.
        if distance >= 256:
            raise ValueError("Reliable receive window exceeded")
        self.waiting.setdefault(packet.sequence, packet)
        ready = []
        while self.expected in self.waiting:
            current = self.waiting.pop(self.expected)
            self.expected = (self.expected + 1) & 0x0fffffff
            if current.fragment:
                first, last = current.fragment
                count = ((last - first) & 0x0fffffff) + 1
                if not 2 <= count <= 128:
                    raise ValueError("Invalid fragment range")
                if not self.fragments:
                    if current.sequence != first:
                        raise ValueError("Missing first bundle fragment")
                    self.fragment_range = current.fragment
                if current.fragment != self.fragment_range:
                    raise ValueError("Interleaved bundle fragments")
                # Account commands use their own request IDs. Mercury request
                # chains across packet boundaries need a separate decoder.
                if current.first_request is not None:
                    raise ValueError("Fragmented Mercury request chain is unsupported")
                self.fragments.append(current)
                if sum(len(p.body) for p in self.fragments) > 131072:
                    raise ValueError("Fragmented bundle exceeds size limit")
                if current.sequence == last:
                    ready.append(replace(self.fragments[0],
                        body=b"".join(p.body for p in self.fragments),
                        flags=current.flags & ~FRAGMENT, fragment=None, piggybacks=()))
                    self.fragments.clear()
                    self.fragment_range = None
            elif self.fragments:
                raise ValueError("Unterminated fragmented bundle")
            else:
                ready.append(current)
        return ready


BASE_FIXED = {0: 5, 1: 4, 2: 19, 3: 24, 4: 22, 5: 31, 6: 0, 7: 4,
              9: 0, 10: 4, 11: 1, 12: 0}


def base_elements(packet):
    pos, request = 0, packet.first_request
    body = packet.body
    while pos < len(body):
        start = pos
        kind = body[pos]
        pos += 1
        if kind in BASE_FIXED:
            size = BASE_FIXED[kind]
        else:
            width = 4 if kind == 255 else 2
            if pos + width > len(body):
                raise ValueError("Truncated BaseApp element length")
            size = int.from_bytes(body[pos:pos + width], "little")
            pos += width
        request_id = None
        if start == request:
            if pos + 6 > len(body):
                raise ValueError("Truncated BaseApp request header")
            request_id, next_request = struct.unpack_from("<IH", body, pos)
            pos += 6
            request = next_request - 2 if next_request >= 2 else None
            if request is not None and request <= start:
                raise ValueError("Invalid BaseApp request chain")
        if pos + size > len(body):
            raise ValueError("Truncated BaseApp element body")
        yield kind, request_id, body[pos:pos + size]
        pos += size
    if request is not None:
        raise ValueError("Unresolved BaseApp request offset")
