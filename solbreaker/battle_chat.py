"""Native battle chat relay. Lives with the network endpoint, outside arena IPC."""
import asyncio
from collections import deque
import math
import struct
import time

from .account import pickle_document, variable16
from .mercury import packed_blob, read_blob, variable8
from .sandbox import vehicle_arena_id, vehicle_wire_id


ACTION_KIND = 154
# Cell method 20 also carries the stock ground marker with entity zero.
MARKER_KIND = 20
# Native quick commands, including grid and entity-specific actions.
COMMANDS = frozenset(range(21, 63)) - {31, 38}
COMMON_CHANNEL = 540_000_001
TEAM_CHANNEL = 540_000_002
PUSH = (1 << 64) - 1
REQUEST_HEADER = struct.Struct('<QBIqiH')
TEXT_COMMAND = 47
# Native CHAT_ACTIONS index: enter=0, broadcast=1.
TEXT_ACTION = 1
MAX_TEXT_BYTES = 500
MAX_PENDING = 64
DELIVERIES_PER_TURN = 8


def ground_marker(body):
    """Validate the native E command's ArenaMarker.AttackHere position."""
    try:
        marker, end = read_blob(body, 4)
    except ValueError:
        return None
    if (end != len(body) or not 4 <= len(marker) <= 19
            or marker[:4] != bytes((10, len(marker) - 2, 10, len(marker) - 4))
            or (len(marker) - 4) % 5):
        return None
    axes = set()
    for axis, coordinate in struct.iter_unpack('<Bf', marker[4:]):
        if (axis not in (13, 21, 29) or axis in axes
                or not math.isfinite(coordinate) or abs(coordinate) >= 10000.):
            return None
        axes.add(axis)
    return marker


def chat_action(opcode, request, account, channel, nickname, arguments):
    stamp = time.time()
    data = (struct.pack('<QBBddIQ', request, opcode, 0, stamp, stamp, channel, account)
            + packed_blob(nickname.encode('utf-8')) + b'\0'
            + packed_blob(pickle_document(arguments)) + b'\0')
    return b'\x13' + variable16(106, data)


def channels(arena, client):
    player = arena.players[client.account_id]
    names = ('common', 'team')
    ids = (COMMON_CHANNEL, TEAM_CHANNEL)
    rows = [
        dict(id=channel, dbID=-channel, channelName=f'#chat:channels/battle/{name}',
             owner=arena.unique_id, ownerName='', greeting='', flags=flag,
             notifyFlags=0, isSystem=False, isReadOnly=False, isSecured=False)
        for channel, name, flag in zip(ids, names, (2, 3))
    ]
    return (chat_action(6, PUSH, PUSH, 0, '', rows)
            + b''.join(chat_action(7, PUSH, client.account_id, channel, player.nickname,
                                   (player.nickname, '')) for channel in ids))


class BattleChat:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.pending = deque()
        self.scheduled = False

    def receive(self, client, arena, body):
        if (arena is None or not client.battle_spawned or client.reconnect_replay
                or not REQUEST_HEADER.size + 2 <= len(body) <= REQUEST_HEADER.size + MAX_TEXT_BYTES + 5):
            return
        if time.monotonic() < client.battle_chat_after or len(self.pending) >= MAX_PENDING:
            return
        sender = arena.players.get(client.account_id)
        if sender is None:
            return
        _, command, channel, target, parameter, reserved = REQUEST_HEADER.unpack_from(body)
        if reserved:
            return
        try:
            message, offset = read_blob(body, REQUEST_HEADER.size)
            auxiliary, end = read_blob(body, offset)
        except ValueError:
            return
        if auxiliary or end != len(body):
            return
        team = sender.team
        if command == TEXT_COMMAND and message:
            if channel not in (TEAM_CHANNEL, COMMON_CHANNEL) or target or parameter:
                return
            try:
                arguments = message.decode('utf-8')
            except UnicodeDecodeError:
                return
            if len(message) > MAX_TEXT_BYTES or not arguments.strip() or '\0' in arguments:
                return
            opcode = TEXT_ACTION
            if channel == COMMON_CHANNEL:
                team = None
        elif not message and command in COMMANDS and channel == TEAM_CHANNEL:
            # A targeted command must refer to this battle. Minimap cells are
            # grid indices and do not use vehicle entity IDs.
            if command == 28:
                if not 0 <= target < 100:
                    return
            elif target not in (0, -1) and vehicle_arena_id(arena, target) not in arena.vehicles:
                return
            opcode = 20
            arguments = (command, max(0, target), parameter, '', '')
        else:
            return
        payload = chat_action(opcode, PUSH, client.account_id, channel, sender.nickname, arguments)
        self._enqueue(client, team, payload)

    def receive_marker(self, client, arena, body):
        if (arena is None or not client.battle_spawned or client.reconnect_replay
                or time.monotonic() < client.battle_chat_after or len(self.pending) >= MAX_PENDING):
            return
        marker = ground_marker(body)
        sender = arena.players.get(client.account_id)
        if marker is None or sender is None:
            return
        # Avatar 46, onArenaMarkerReceived: vehicle ID, account ID and ArenaMarker.
        payload = b'\x13' + variable8(59 + 46, struct.pack('<IQ',
            vehicle_wire_id(arena, sender.vehicle_id), client.account_id) + packed_blob(marker))
        self._enqueue(client, sender.team, payload)

    def _enqueue(self, client, team, payload):
        client.battle_chat_after = time.monotonic() + .5
        self.pending.append((client.arena_id, team, payload))
        if not self.scheduled:
            self.scheduled = True
            asyncio.get_running_loop().call_soon(self.drain)

    def drain(self):
        for _ in range(min(len(self.pending), DELIVERIES_PER_TURN)):
            arena_id, team, payload = self.pending.popleft()
            arena = self.endpoint.arenas.get(arena_id)
            if arena is None:
                continue
            for addr, recipient in self.endpoint.clients.items():
                if (recipient.phase != 'battle' or recipient.arena_id != arena_id
                        or not recipient.battle_spawned or recipient.reconnect_replay
                        or recipient.reliable_window_used >= 128):
                    continue
                player = arena.players.get(recipient.account_id)
                if player is not None and (team is None or player.team == team):
                    try:
                        self.endpoint.send(addr, recipient, payload)
                    except OSError as exc:
                        self.endpoint._report_fault('battle_chat', exc,
                                                    account_id=recipient.account_id)
        self.scheduled = bool(self.pending)
        if self.scheduled:
            asyncio.get_running_loop().call_soon(self.drain)
