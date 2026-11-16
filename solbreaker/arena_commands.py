"""Decode and apply player inputs for both arena schedulers."""
from dataclasses import dataclass
import math
import struct

from .mercury import read_blob


@dataclass(frozen=True)
class ArenaCommand:
    account_id: int
    action: str
    arguments: tuple
    now: float | None = None


# EquipmentParameters oneof in the pinned client. Each nested message has
# one uint32 field, entity_id, encoded as an unsigned protobuf varint.
_UPRISING_TARGET_FIELDS = {42: 1, 36: 2, 43: 4}


def decode_uprising_target(ident, data):
    if not data:
        return None
    tag = (_UPRISING_TARGET_FIELDS[ident] << 3) | 2
    if (not 4 <= len(data) <= 8 or data[0] != tag
            or data[1] != len(data) - 2 or data[2] != 8):
        raise ValueError('Invalid Uprising ability target')
    entity = 0
    for index, byte in enumerate(data[3:]):
        entity |= (byte & 0x7f) << (7 * index)
        if byte < 0x80:
            if index == len(data) - 4 and 0 < entity <= 0xffffffff:
                return entity
            break
    raise ValueError('Invalid Uprising ability target')


def decode_graviton_aim(data):
    """EquipmentParameters.gravityShot, with two sparse Vector3 messages."""
    if not data:
        return None
    invalid = 'Invalid Graviton aim'
    if not 6 <= len(data) <= 36 or data[:2] != bytes((26, len(data) - 2)):
        raise ValueError(invalid)
    points = {}
    offset = 2
    while offset + 2 <= len(data):
        field, size = data[offset:offset + 2]
        offset += 2
        end = offset + size
        if field not in (10, 18) or field in points or size % 5 or end > len(data):
            raise ValueError(invalid)
        values = {}
        while offset < end:
            axis, value = struct.unpack_from('<Bf', data, offset)
            if axis not in (13, 21, 29) or axis in values or not math.isfinite(value) or abs(value) >= 10000.:
                raise ValueError(invalid)
            values[axis] = value
            offset += 5
        points[field] = tuple(values.get(axis, 0.) for axis in (13, 21, 29))
    if offset != len(data) or len(points) != 2 or points[10] == points[18]:
        raise ValueError(invalid)
    return points[10], points[18]


def decode_input(account_id, kind, body, now=None):
    """Return a command for a supported RPC, or None for an ignored RPC."""
    if kind == 135 and not body:
        return ArenaCommand(account_id, 'ready', (), now)
    # Native cell requests address the controlled vehicle.
    if kind in (21, 22) and len(body) == 8:
        entity, compact_id = struct.unpack('<II', body)
        return ArenaCommand(account_id, 'uprising_upgrade', (entity, compact_id, kind == 21), now)
    if kind == 145 and len(body) == 2:
        return ArenaCommand(account_id, 'control', struct.unpack('<H', body), now)
    if kind == 13 and len(body) == 8:
        return ArenaCommand(account_id, 'autoaim', struct.unpack('<II', body))
    if kind == 18 and len(body) == 20:
        entity, target, *point = struct.unpack('<II3f', body)
        return ArenaCommand(account_id, 'autoaim_point', (entity, target, tuple(point)))
    if kind in (16, 17) and len(body) == 16:
        entity, *point = struct.unpack('<I3f', body)
        return ArenaCommand(account_id, 'aim_point_entity', (entity, tuple(point), kind == 17))
    if kind == 149 and len(body) == 12:
        return ArenaCommand(account_id, 'aim_point', (struct.unpack('<3f', body),))
    if kind == 148 and len(body) == 8:
        return ArenaCommand(account_id, 'aim_angles', struct.unpack('<2f', body))
    if kind == 138 and not body:
        return ArenaCommand(account_id, 'fire', (), now)
    if kind == 20 and len(body) >= 7:
        entity, ident = struct.unpack_from('<IH', body)
        extra, end = read_blob(body, 6)
        if end == len(body):
            return ArenaCommand(account_id, 'consumable', (entity, ident, extra), now)
    if kind == 147 and len(body) == 5:
        return ArenaCommand(account_id, 'select_shell', struct.unpack('<BI', body), now)
    return None


def apply_input(arena, command):
    """Apply one decoded command using the established Arena methods."""
    if command is None:
        return
    timeline = getattr(arena, 'time_stop', None)
    if timeline is not None and timeline.accept(command):
        return
    account, action, args, now = command.account_id, command.action, command.arguments, command.now
    if action == 'ready':
        arena.ready(account, now)
    elif action == 'sandbox':
        arena.sandbox_control(account, *args)
    elif action == 'control':
        arena.control(account, *args, now)
    elif action in ('autoaim', 'autoaim_point'):
        entity, target, *point = args
        if entity in (0, arena.players[account].vehicle_id):
            if point:
                arena.autoaim(account, target, list(point[0]))
            else:
                arena.autoaim(account, target)
    elif action == 'aim_point_entity':
        entity, point, local = args
        if entity == arena.players[account].vehicle_id:
            arena.aim_point(account, list(point), local=local)
    elif action == 'aim_point':
        arena.aim_point(account, *args)
    elif action == 'aim_angles':
        arena.aim_angles(account, *args)
    elif action == 'fire':
        arena.fire(account, now)
    elif action == 'consumable':
        entity, ident, extra = args
        if entity == arena.players[account].vehicle_id:
            arena.use_consumable(account, ident, extra, now)
    elif action == 'uprising_upgrade':
        entity, compact_id, selected = args
        if entity == arena.players[account].vehicle_id:
            arena.select_uprising_upgrade(account, compact_id, selected, now=now)
    elif action == 'select_shell':
        arena.select_shell(account, *args, now=now)
    else:
        raise ValueError('Unknown arena command')


def apply_at_tick(arena, command):
    """Apply input on the current simulation clock without running physics."""
    previous = arena._input_only
    arena._input_only = True
    try:
        apply_input(arena, command)
    finally:
        arena._input_only = previous
