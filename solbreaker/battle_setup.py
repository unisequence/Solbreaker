"""Custom bot lineups and sandbox vehicle presets."""
from dataclasses import dataclass
import struct

from .sandbox import TANK_STAT_NAMES

MARKER = '/custom1/'
ROOM_MARKER = '[lineup '
ROOM_HEADER = struct.Struct('<B14I')
CLASSES = ('heavyTank', 'mediumTank', 'AT-SPG', 'lightTank')
EMPTY = 0xffffffff
AUTOMATIC = 0
PRESET_COUNT = 4
SLOT_COUNT = 13
STAT_COUNT = len(TANK_STAT_NAMES)
PRESET = struct.Struct(f'<{STAT_COUNT}B?')
HEADER = struct.Struct(f'<BB{SLOT_COUNT}I')


@dataclass(frozen=True)
class Preset:
    values: tuple[int, ...] = (0,) * STAT_COUNT
    bots: bool = False


@dataclass(frozen=True)
class BattleSetup:
    custom: bool = False
    active: int = 0
    slots: tuple[int, ...] = (AUTOMATIC,) * SLOT_COUNT
    presets: tuple[Preset, ...] = (Preset(),) * PRESET_COUNT

    @property
    def preset(self):
        return self.presets[self.active-1] if self.active else Preset()

    def encode(self):
        return (HEADER.pack(self.custom, self.active, *self.slots)
                + b''.join(PRESET.pack(*row.values, row.bots) for row in self.presets))

    @classmethod
    def decode(cls, payload):
        if len(payload) != HEADER.size + PRESET.size * PRESET_COUNT:
            raise ValueError('Invalid custom battle settings size')
        custom, active, *slots = HEADER.unpack_from(payload)
        if custom > 1 or active > PRESET_COUNT:
            raise ValueError('Invalid custom battle settings')
        validate_slots(slots)
        presets = []
        for offset in range(HEADER.size, len(payload), PRESET.size):
            *values, bots = PRESET.unpack_from(payload, offset)
            if any(value > 100 for value in values) or payload[offset+STAT_COUNT] > 1:
                raise ValueError('Invalid tank preset')
            presets.append(Preset(tuple(values), bots))
        return cls(bool(custom), active, tuple(slots), tuple(presets))


@dataclass(frozen=True)
class RoomLineup:
    custom: bool = False
    slots: tuple[int, ...] = (EMPTY,) * 14

    def tag(self):
        payload = ROOM_HEADER.pack(self.custom, *self.slots)
        return ROOM_MARKER + payload.hex() + '] '

    def team_slots(self, team, available):
        start = 7 * (team - 1)
        return tuple(slot for slot in self.slots[start:start+7] if slot != EMPTY)[:max(available, 0)]


def validate_slots(slots):
    for slot in slots:
        if slot in (EMPTY, AUTOMATIC) or slot & 15 == 1:
            continue
        tier, kind = random_filter(slot)
        if slot & 0xfffff00f != 2 or tier > 10 or kind > len(CLASSES):
            raise ValueError('Invalid random tank filter')


def split_room_lineup(description):
    if not description.startswith(ROOM_MARKER):
        return description, None
    encoded, end, notes = description[len(ROOM_MARKER):].partition(']')
    if not end or len(encoded) != 2 * ROOM_HEADER.size:
        raise ValueError('Invalid room lineup size')
    payload = bytes.fromhex(encoded)
    if len(payload) != ROOM_HEADER.size:
        raise ValueError('Invalid room lineup encoding')
    custom, *slots = ROOM_HEADER.unpack(payload)
    if custom > 1:
        raise ValueError('Invalid room lineup mode')
    validate_slots(slots)
    return notes.removeprefix(' '), RoomLineup(bool(custom), tuple(slots))


def random_filter(slot):
    return (slot >> 4) & 15, (slot >> 8) & 15


def split_description(description):
    text, found, encoded = description.partition(MARKER)
    if not found:
        return description, None
    return text, BattleSetup.decode(bytes.fromhex(encoded))
