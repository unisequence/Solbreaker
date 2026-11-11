"""Decode the local native form, using the existing create-room envelope."""
import re

from .battle_settings_wire import PREFIX, TITLE
from .training_config import TrainingConfig


def packed_recipe(config, catalog):
    map_id = config.map_id(catalog) if config.map is not None else 0
    return (map_id * 10000
            + ('elimination', 'encounter', 'supremacy').index(config.battle_mode) * 1000
            + ('easy', 'normal', 'hard').index(config.difficulty) * 100
            + config.enemies * 10 + config.allies)


def decode(title, description, map_id, catalog):
    if title != TITLE:
        return None
    match = re.fullmatch(re.escape(PREFIX)+r'([0-6])/([1-7])/([0-2])/([0-2])', description)
    if match is None:
        raise ValueError('Invalid native bot battle settings')
    allies, enemies, difficulty, capture = map(int, match.groups())
    if map_id != 0 and map_id not in catalog:
        raise ValueError('Bot battle map is unavailable')
    return TrainingConfig(mode='bots', allies=allies, enemies=enemies,
        difficulty=('easy', 'normal', 'hard')[difficulty], map=catalog[map_id] if map_id else None,
        battle_mode=('elimination', 'encounter', 'supremacy')[capture])
