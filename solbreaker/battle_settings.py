"""Decode the local native form, using the existing create-room envelope."""
import re
from dataclasses import replace

from .battle_settings_wire import LEGACY_PREFIX, PREFIX, RULES_SHIFT, TITLE
from .training_config import BATTLE_MODES, TrainingConfig


def packed_recipe(config, catalog, *, include_rules=True):
    map_id = config.map_id(catalog) if config.map is not None else 0
    duration = 0 if config.minutes is None else config.minutes + 1
    flags = duration * 4 + config.unlimited_lives + config.unlimited_ammo * 2 if include_rules else 0
    return ((flags << RULES_SHIFT) + map_id * 10000
            + BATTLE_MODES.index(config.battle_mode) * 1000
            + ('easy', 'normal', 'hard').index(config.difficulty) * 100
            + config.enemies * 10 + config.allies)


def decode(title, description, map_id, catalog):
    if title != TITLE:
        return None
    base_fields = r'([0-6])/([1-7])/([0-2])/([0-3])'
    match = re.fullmatch(re.escape(PREFIX) + base_fields + r'/([01])/(-1|[0-9]{1,2})/([01])', description)
    legacy = re.fullmatch(re.escape(LEGACY_PREFIX) + base_fields, description)
    if match is None and legacy is None:
        raise ValueError('Invalid native bot battle settings')
    allies, enemies, difficulty, capture = map(int, (match or legacy).groups()[:4])
    if map_id != 0 and map_id not in catalog:
        raise ValueError('Bot battle map is unavailable')
    config = TrainingConfig(mode='bots', allies=allies, enemies=enemies,
        difficulty=('easy', 'normal', 'hard')[difficulty], map=catalog[map_id] if map_id else None,
        battle_mode=BATTLE_MODES[capture])
    if match is None:
        return config
    lives, minutes, ammo = map(int, match.groups()[4:])
    return replace(config, unlimited_lives=bool(lives), minutes=None if minutes == -1 else minutes,
                   unlimited_ammo=bool(ammo))
