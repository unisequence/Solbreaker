"""Serializable player inputs for an arena owner.

Decoding and application are separate so a future arena process can consume
commands in their original order. The current server applies them immediately.
"""
from dataclasses import dataclass
import struct

from .mercury import read_blob


@dataclass(frozen=True)
class ArenaCommand:
    account_id: int
    action: str
    arguments: tuple
    now: float | None = None


def decode_input(account_id, kind, body, now=None):
    """Return a command for a supported RPC, or None for an ignored RPC."""
    if kind == 135 and not body:
        return ArenaCommand(account_id, 'ready', (), now)
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
    account, action, args, now = command.account_id, command.action, command.arguments, command.now
    if action == 'ready':
        arena.ready(account, now)
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
    elif action == 'select_shell':
        arena.select_shell(account, *args, now=now)
    else:
        raise ValueError('Unknown arena command')
