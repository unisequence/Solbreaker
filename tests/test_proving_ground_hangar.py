from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from solbreaker.base import BaseEndpoint, BattleRefused
from solbreaker.profile import account_snapshot, PROVING_GROUND_TITLE
from solbreaker.rooms import RoomManager
from solbreaker.training_config import (PROVING_GROUND_ID, PROVING_GROUND_KEY, PROVING_GROUND_MODE,
                                       TrainingConfig)


class ProvingGroundHangarTests(unittest.TestCase):
    def endpoint(self):
        base = BaseEndpoint.__new__(BaseEndpoint)
        base.game = Path('prepared-client')
        base.map_spec = SimpleNamespace(map_id=6, key='rudniki')
        ground = SimpleNamespace(map_id=PROVING_GROUND_ID, key=PROVING_GROUND_KEY)
        base.map_specs = {6: base.map_spec, PROVING_GROUND_ID: ground}
        base.map_catalog = {i: spec.key for i, spec in base.map_specs.items()}
        base.server = SimpleNamespace(
            event=Mock(), training_config=TrainingConfig(mode='bots', map='rudniki', battle_mode='gravity'),
            accounts=SimpleNamespace(get=lambda i: SimpleNamespace(identity=lambda: {'id': i})))
        base.clients = {i: SimpleNamespace(account_id=i, phase='account', account_created=True,
                        arena_id=None, reliable_window_used=0) for i in (1, 2)}
        base.arenas = Mock(sandbox_enabled=False)
        base.arenas.create.return_value = SimpleNamespace(result_id='test', vehicles={})
        base.map_runtime = Mock(return_value={'terrain': None, 'collision_scene': None})
        base.begin_load = Mock()
        return base

    def test_card_presence_is_conditional(self):
        state = {'revision': 1, 'credits': 0}
        inventory = {'descriptor': '01', 'shells': []}
        for installed in (True, False):
            modes = account_snapshot(None, None, state, inventory,
                                     proving_ground=installed)['cache']['gameModes']
            self.assertEqual(PROVING_GROUND_MODE in modes, installed)
            if installed:
                self.assertEqual(modes[PROVING_GROUND_MODE]['name'], 'arcade')
                self.assertEqual(modes[PROVING_GROUND_MODE]['titleString'], PROVING_GROUND_TITLE)

    def test_snapshot_uses_native_vehicle_lock_and_delta_tombstone(self):
        state = {'revision': 1, 'credits': 0}
        inventory = {'descriptor': '01', 'shells': []}
        locked = account_snapshot(None, None, state, inventory,
                                  vehicle_locks=(42,))['cache']['vehsLock']
        cleared = account_snapshot(1, None, state, inventory,
                                   cleared_vehicle_locks=(42,))['cache']['vehsLock']
        self.assertEqual(locked, {42: (1, 0)})
        self.assertEqual(cleared, {42: None})

    def test_arena_vehicle_is_locked_until_its_result_is_recorded(self):
        base = self.endpoint()
        player = SimpleNamespace(inventory_id=42)
        arena = SimpleNamespace(players={1: player}, finished_at=None, started=True,
                                recorded_accounts=set())
        base.arenas = SimpleNamespace(arenas={'battle': arena})
        self.assertEqual(base.locked_vehicle_ids(1), {42})
        with self.assertRaisesRegex(ValueError, 'still in a battle'):
            base.require_vehicle_available(1, 42)
        arena.finished_at = 10.
        arena.recorded_accounts.add(1)
        self.assertEqual(base.locked_vehicle_ids(1), set())

    def test_card_overrides_previous_map_and_rules(self):
        base = self.endpoint()
        base.start_battle(1, base.clients[1], map_id=6, game_mode=PROVING_GROUND_MODE)
        options = base.arenas.create.call_args.kwargs
        self.assertEqual(options['map_spec'].map_id, PROVING_GROUND_ID)
        self.assertFalse(options['training'].bots_enabled)
        self.assertEqual((options['training'].minutes, options['training'].unlimited_lives), (0, True))
        self.assertEqual((options['bot_game'], options['bot_roster']), (base.game, ()))
        self.assertEqual(base.server.training_config.map, 'rudniki')
        base.begin_load.assert_called_once()

    def test_missing_map_refuses_stale_selection(self):
        base = self.endpoint()
        del base.map_catalog[PROVING_GROUND_ID]
        with self.assertRaisesRegex(BattleRefused, 'not installed'):
            base.start_battle(1, base.clients[1], map_id=-1, game_mode=PROVING_GROUND_MODE)
        base.arenas.create.assert_not_called()

    def test_random_bots_omit_proving_ground(self):
        base = self.endpoint()
        base.server.training_config = TrainingConfig(mode='bots')
        with patch('secrets.choice', side_effect=lambda rows: rows[0]) as choice:
            base.start_battle(1, base.clients[1], map_id=-1, game_mode=7)
        self.assertEqual(choice.call_args.args[0], (6,))

    def test_friends_can_start_on_the_same_team(self):
        base = self.endpoint()
        base.rooms = RoomManager(base)
        room = base.rooms.create(1, map_id=PROVING_GROUND_ID)
        base.rooms.join(2, room.id, team=1)
        base.rooms.ready(1, True)
        base.rooms.ready(2, True)
        base.rooms.start(1)
        accounts, = base.arenas.create.call_args.args
        self.assertEqual([a['team'] for a in accounts], [1, 1])
        options = base.arenas.create.call_args.kwargs
        self.assertEqual((options['map_spec'].map_id, options['bot_game'], options['bot_roster']),
                         (PROVING_GROUND_ID, base.game, ()))
        self.assertFalse(options['solo'])
        base.begin_load.assert_called_once()
