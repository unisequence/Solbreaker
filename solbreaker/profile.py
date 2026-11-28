"""Minimal local account state for the pinned native client."""
from functools import lru_cache
from pathlib import Path

from .training_config import BATTLE_MODES, PROVING_GROUND_MODE
from .vehicles import stock_m2, stock_ammo

TRAINING_RETURN = 'Local/BattleSettings/ReturnToBattle'
# The abilities registry uses lunar, while the game-mode filter uses Lunar.
ARCADE_ABILITIES = {'uprising': 'hellgames', 'mad_games': 'arcade', 'gravity': 'lunar'}
BOTS_TITLE = 'Local/BotsTitle'
PROVING_GROUND_TITLE = 'Local/ProvingGround/Title'
PROVING_GROUND_DESCRIPTION = 'Local/ProvingGround/Description'
BOT_QUEUE_TITLES = {mode: f'Local/BotQueue/{mode}' for mode in BATTLE_MODES}


def game_mode_descriptions(training_return, rules=None):
    abilities = ARCADE_ABILITIES.get(rules, '')
    return {
        1: {'description': TRAINING_RETURN if training_return else 'Local/TargetDescription',
            'arcadeAbilities': abilities if training_return else ''},
        7: {'titleString': BOTS_TITLE if training_return else BOT_QUEUE_TITLES.get(rules, BOTS_TITLE),
            'description': TRAINING_RETURN if training_return else 'Local/BotsDescription',
            'arcadeAbilities': abilities},
    }


@lru_cache(maxsize=2)
def vehicle_descriptor(game):
    return stock_m2(Path(game))


def account_snapshot(previous_revision, game, progress, inventory=None, *, garage=None,
                     training_return=False, bot_rules=None, proving_ground=False,
                     vehicle_locks=(), cleared_vehicle_locks=()):
    """Full bootstrap when previous_revision is None; otherwise a cache delta."""
    if garage is not None:
        inventory = next((v for v in garage['vehicles'] if v['id'] == garage['selected_id']), None)
        if inventory is None:
            raise ValueError('Selected vehicle is not in the garage')
    # StatsData::Update reads cache.gameModes; its observer creates the
    # GameMode objects before the inventory/hangar asks for mode 1 (regular).
    # The installed eGameMode registry defines 1=regular, 2=training, 6=tutorial.
    slots = [(1, "regular"), (2, "training"), (6, "tutorial"), (7, "rating")]
    if proving_ground:
        slots.append((PROVING_GROUND_MODE, "arcade"))
    modes = {mode: {"name": name, "allowLevels": list(range(1, 11)),
                    "allowSquads": False, "squadCapacity": 1,
                    "activeStartTime": 0, "activeFinishTime": 4102444800,
                    "visibleStartTime": 0, "visibleFinishTime": 4102444800}
             for mode, name in slots}
    if proving_ground:
        modes[PROVING_GROUND_MODE].update(titleString=PROVING_GROUND_TITLE,
                                          description=PROVING_GROUND_DESCRIPTION)
    modes[1]['titleString'] = 'Local/TargetTitle'
    for mode, fields in game_mode_descriptions(training_return, bot_rules).items():
        modes[mode].update(fields)
    snapshot = {"prevRev": previous_revision, "rev": progress["revision"],
            "stats": {"credits": progress["credits"], "gold": progress.get("gold", 0), "freeXP": 0, "slots": 5,
                      "diary": bytes.fromhex(progress.get('diary', ''))},
            "cache": {"gameModes": modes,
                      # Blitz stores a pair (reason, timestamp) for each locked
                      # inventory vehicle. ON_ARENA=1 is the stock in-battle
                      # lock that drives the hangar card and its padlock icon.
                      "vehsLock": {**{vehicle_id: (1, 0) for vehicle_id in vehicle_locks},
                                   **{vehicle_id: None for vehicle_id in cleared_vehicle_locks}}},
            "garageTutorial": {"tutorialsCompleted": (1 << 63) - 1, "lastStep": 0, "completeStatus": 1},
            "inventory": {1: {"serialized": {1: bytes.fromhex(inventory['descriptor']) if inventory else vehicle_descriptor(game)},
                               "shells": {1: inventory['shells'] if inventory else stock_ammo(Path(game))}}}}
    if previous_revision is None:
        # Native AccountData treats the presence of an integer prevRev as a
        # delta, even at revision zero. Full initialization restores selection.
        snapshot.pop('prevRev')
    if garage is not None:
        vehicles = garage['vehicles']
        snapshot['inventory'][1] = {
            'serialized': {v['id']: bytes.fromhex(v['descriptor']) for v in vehicles},
            'shells': {v['id']: v['shells'] for v in vehicles},
            'settings': {v['id']: v['ammo_settings'] for v in vehicles if 'ammo_settings' in v},
            'camos': {v['id']: [*v.get('camouflage_slots', [v.get('camouflage', 0)] * 3), 0, 0, 0]
                      for v in vehicles if 'camouflage_slots' in v or 'camouflage' in v},
            # Native layout keys are typed compact turret/gun descriptors;
            # signed shell CDs retain the per-shell currency choice, even at0.
            'shellsLayout': {v['id']: {
                (entry['turret_cd'], entry['gun_cd']): list(entry['shells'])
                for entry in v['ammo_layouts']} for v in vehicles if 'ammo_layouts' in v}}
        snapshot['stats']['slots'] = max(5, len(vehicles))
        snapshot['stats']['freeXP'] = garage['free_xp']
        snapshot['stats']['vehTypeXP'] = {v['type_cd']: v['xp']
            for v in garage.get('type_xp', vehicles)}
        snapshot['stats']['unlocks'] = list(garage['researched'])
        # Local sandbox has no daily sale quota. The native default (missing
        # StatsData+0x158) is zero and prevents opening the sale dialog at all.
        snapshot['stats']['vehicleSellsLeft'] = (1 << 31)-1
        from .loadouts import native_inventory
        loadouts, slots = native_inventory(game, vehicles)
        snapshot['inventory'][1].update(loadouts)
        snapshot['stats']['optDeviceSlots'] = slots
    return snapshot


def account_profile_parts(snapshot, *, ammo_only=True):
    """Separate a bootstrap from complete per-vehicle inventory deltas.

    Prefer deferring just ammunition preferences: the established bootstrap
    still creates the entire garage before its observers receive the deltas.
    The fallback defers all inventory records. A vehicle's shellsLayout map
    (including every installed turret/gun key and its flat list) is indivisible.
    This copies the map layers it changes and never mutates the snapshot.
    """
    baseline = dict(snapshot)
    source = snapshot.get('inventory')
    if not isinstance(source, dict):
        return baseline, ()
    inventory = dict(source)
    baseline['inventory'] = inventory
    deltas = []
    for kind, fields in source.items():
        if not isinstance(fields, dict):
            continue
        kept = dict(fields)
        inventory[kind] = kept
        grouped = {}
        for field, records in fields.items():
            if ammo_only and field not in ('settings', 'shellsLayout'):
                continue
            if not isinstance(records, dict) or not records:
                continue
            del kept[field]
            for vehicle_id, record in records.items():
                grouped.setdefault(vehicle_id, {})[field] = {vehicle_id: record}
        deltas.extend({'inventory': {kind: record}} for record in grouped.values())
    return baseline, tuple(deltas)
