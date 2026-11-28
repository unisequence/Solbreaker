"""Detailed movement wire format for the pinned compatible build 11.21 client."""
import math
import struct


UPDATE_HZ = 30
SIMULATION_INTERVAL = 1 / (2 * UPDATE_HZ)


def game_tick(now, epoch):
    # Simulation times are epoch+n*STEP; at large epochs the subtraction
    # cancels and an exact label boundary lands a few ulps below the integer.
    # Floor, but snap up across that representational uncertainty only.
    ticks = (now - epoch) * UPDATE_HZ
    tick = math.floor(ticks)
    tolerance = 8 * UPDATE_HZ * math.ulp(max(abs(now), abs(epoch), abs(ticks)))
    return tick + 1 if tick + 1 - ticks <= tolerance else tick


def tick_sync(tick):
    return b"\x0d" + bytes([tick & 255])


def vehicle_snapshot(entity_id, position, yaw, pitch=0.0, roll=0.0):
    # ServerConnection::avatarUpdateNoAliasDetailed, 0x2816bd0.
    # Direction3 is stored as roll, pitch, yaw, NOT yaw, pitch, roll.
    # Time comes from the preceding tickSync; velocity is inferred by the
    # native filter from successive timestamped positions.
    values = (*position, roll, pitch, yaw)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Non-finite vehicle pose")
    return b"\x15" + struct.pack("<I6f", entity_id, *values)


# Leaves room for Mercury and cipher overhead on a 1280-byte path MTU.
MOTION_PACKET_BODY_LIMIT = 1152


def motion_records(body):
    """Iterate complete records in a tick bundle, including entity context."""
    if len(body) < 2 or body[0] != 0x0d:
        raise ValueError('Movement bundle must start with tickSync')
    offset = 2
    while offset < len(body):
        start = offset
        kind = body[offset]
        if kind == 0x15:
            offset += 29
            # Keep the pose and adjacent gun/engine properties together.
            for prop in (160, 162):
                if (offset + 8 <= len(body) and body[offset] == 0x12
                        and body[offset + 5] == prop
                        and body[offset + 1:offset + 5] == body[start + 1:start + 5]):
                    offset += 8
        elif kind == 0x13 and offset + 2 < len(body):
            method = body[offset + 1]
            if method == 0xa7:
                offset += 6
            elif method == 83:
                offset += 30
            elif method == 96:
                size = body[offset + 2]
                if size == 255:
                    if offset + 7 > len(body):
                        raise ValueError('Truncated extended movement length')
                    size = struct.unpack_from('<I', body, offset + 3)[0] + 4
                offset += 3 + size
            else:
                raise ValueError(f'Unsupported movement avatar message {method:#x}')
        else:
            raise ValueError(f'Unsupported movement message {kind:#x}')
        if offset > len(body):
            raise ValueError('Truncated movement record')
        yield start, offset


def motion_packet_bodies(body):
    """Build independent unreliable bundles, all carrying the same tick."""
    if len(body) <= MOTION_PACKET_BODY_LIMIT:
        return (body,)
    packets = []
    header = body[:2]
    packet_start = len(header)
    capacity = MOTION_PACKET_BODY_LIMIT - len(header)
    for start, end in motion_records(body):
        if end - start > capacity:
            raise ValueError('Movement record exceeds packet budget')
        if end - packet_start > capacity:
            packets.append(header + body[packet_start:start])
            packet_start = start
    packets.append(header + body[packet_start:])
    return tuple(packets)
