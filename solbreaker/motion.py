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
