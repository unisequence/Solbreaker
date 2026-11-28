from collections import Counter
import hashlib
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

from solbreaker import dvpl
from solbreaker import battle_setup, bot_roster
from solbreaker.bot_roster import select_room_bot_roster
from solbreaker.client_battle_settings import ACTIONS, FORM, upgrade_form
from solbreaker.client_ui import STRINGS_EN, STRINGS_RU
from solbreaker.mercury import packed_blob
from solbreaker.native_rooms import NativeRooms
from solbreaker.rooms import RoomManager
from solbreaker.training_config import RoomBattleSettings


class TrainingRoomBotsTests(unittest.TestCase):
    def setUp(self):
        clients = {i: SimpleNamespace(account_id=i, account_created=True, phase='account',
                                      arena_id=None) for i in (1, 2)}
        accounts = SimpleNamespace(get=lambda ident: SimpleNamespace(id=ident, nickname=f'Player {ident}'))
        self.endpoint = SimpleNamespace(
            clients=clients, send=Mock(), server=SimpleNamespace(accounts=accounts, event=Mock()),
            definition=SimpleNamespace(descriptor=b'vehicle'), map_catalog={6:'rudniki', 7:'fort'},
            map_spec=SimpleNamespace(map_id=6), arenas={}, start_shared=Mock(
                return_value=SimpleNamespace(result_id='battle')))
        self.endpoint.rooms = RoomManager(self.endpoint)
        self.native = NativeRooms(self.endpoint)

    def create(self, description):
        payload = (struct.pack('<IIHH', 0xFFFF0006, 420, 0, 0x3FF) + packed_blob(b'Training')
                   + packed_blob(description) + struct.pack('<BBI', 0, 1, 1))
        self.native.create(self.endpoint.clients[1], payload)
        return self.endpoint.rooms.current(1)

    def edit(self, description, client=1, *, map_selector=7):
        payload = (struct.pack('<HHB4qB', 300, 406, 4, map_selector, 0, 0, 0, 2)
                   + packed_blob(b'Training') + packed_blob(description))
        self.assertTrue(self.native.command(self.endpoint.clients[client], 149, payload, 300, 406))

    def test_room_creation_accepts_bot_counts_and_only_needs_ready_humans(self):
        room = self.create(b'[bots 7:2] Friends')
        self.assertEqual(room.bot_counts, (7, 2))
        self.assertEqual(room.active_bot_counts, (6, 2))
        self.assertEqual(self.native.settings(room)['comment'], '[bots 7:2] [notes] Friends')
        self.assertEqual(self.native.settings(room)['limits'][2]['minCount'], 0)
        self.assertEqual(self.native.listing(room)[1][9], '[bots 7:2] Friends')
        with self.assertRaisesRegex(ValueError, 'ready'):
            self.endpoint.rooms.start(1)
        self.endpoint.rooms.ready(1, True)
        self.endpoint.rooms.start(1)
        self.endpoint.start_shared.assert_called_once_with([1], [1], map_id=6, bot_counts=(6, 2),
                                                         room_settings=RoomBattleSettings())

    def test_large_teams_opt_in_is_scoped_to_rudniki_room(self):
        room = self.create(b'[room-form] [teams 15] [bots 14:14] [notes] Large room')
        self.assertTrue(room.battle_settings.large_teams)
        self.assertEqual(room.team_capacity, 15)
        self.assertEqual(room.active_bot_counts, (14, 14))
        self.assertEqual(self.native.settings(room)['limits'][2]['maxCount'], (15, 15, 15))
        self.assertIn('[teams 15]', self.native.settings(room)['comment'])
        with self.assertRaisesRegex(ValueError, 'Encounter on Rudniki'):
            self.endpoint.rooms.set_map(1, 7)
        self.endpoint.rooms.join(2, room.id, 2)
        self.endpoint.rooms.ready(1, True)
        self.endpoint.rooms.ready(2, True)
        self.endpoint.rooms.start(1)
        self.endpoint.start_shared.assert_called_once_with(
            [1, 2], [1, 2], map_id=6, bot_counts=(14, 14),
            room_settings=RoomBattleSettings(large_teams=True))

    def test_editing_large_room_back_to_standard_clamps_its_bot_counts(self):
        room = self.create(b'[teams 15] [bots 14:14] Large room')
        self.edit(b'[room-form] [bots 7:7] [notes] Standard room', map_selector=6)
        self.assertEqual(room.team_capacity, 7)
        self.assertEqual(room.active_bot_counts, (6, 7))
        self.assertNotIn('[teams 15]', self.native.settings(room)['comment'])

    def test_room_search_accepts_online_friend_ids(self):
        room = self.create(b'Lobby')
        self.native.reply = Mock()
        payload = struct.pack('<HHB5qB', 300, 524, 5, 0, 1, 3, 4, 5, 1) + packed_blob(b'*')
        client = self.endpoint.clients[2]
        self.assertTrue(self.native.command(client, 149, payload, 300, 524))
        self.native.reply.assert_called_once_with(client, 300, data=[self.native.listing(room)])

    def test_edit_and_join_keep_humans_ahead_of_bots(self):
        room = self.create(b'[bots 7:7] Room')
        self.endpoint.rooms.join(2, room.id, 1)
        self.assertEqual(room.active_bot_counts, (5, 7))
        self.edit(b'[bots 2:3] New notes', client=2)
        self.assertEqual((room.map_id, room.bot_counts), (6, (7, 7)))
        self.edit(b'[bots 2:8] Bad count')
        self.assertEqual((room.map_id, room.bot_counts), (6, (7, 7)))
        self.edit(b'[bots 2:3] New notes')
        self.assertEqual((room.map_id, room.bot_counts, room.description), (7, (2, 3), 'New notes'))
        self.edit(b'Human players only')
        self.assertEqual((room.bot_counts, room.description), ((0, 0), 'Human players only'))

    def test_custom_room_lineup_survives_edit_and_humans_claim_capacity(self):
        custom = battle_setup.RoomLineup(True, (2,)*14)
        room = self.create(custom.tag().encode() + b'[room-form] [bots 2:3] [notes] Room')
        self.assertEqual(room.lineup, custom)
        self.assertEqual(room.bot_counts, (2, 3))
        self.assertEqual(room.active_bot_counts, (6, 7))
        self.endpoint.rooms.join(2, room.id, 1)
        self.assertEqual(room.active_bot_counts, (5, 7))
        self.assertEqual(battle_setup.split_room_lineup(self.native.settings(room)['comment']),
                         ('[bots 2:3] [notes] Room', custom))
        self.assertEqual(self.native.listing(room)[1][9], '[bots 5:7] Room')
        self.edit(b'[room-options] [time 12]', map_selector=0xffffffff)
        self.assertEqual(room.lineup, custom)
        self.endpoint.rooms.ready(1, True)
        self.endpoint.rooms.ready(2, True)
        self.endpoint.rooms.start(1)
        self.endpoint.start_shared.assert_called_once_with(
            [1, 2], [1, 1], map_id=6, bot_counts=(5, 7), room_lineup=custom,
            room_settings=RoomBattleSettings(False, 12))

    def test_map_change_accepts_the_native_tagged_selector(self):
        self.endpoint.map_catalog[101] = 'rudniki_evening'
        room = self.create(b'[bots 7:7] Practice')
        self.edit(b'[room-form] [lives unlimited] [ammo unlimited] [time endless] '
                  b'[bots 7:7] [notes] Practice', map_selector=0xffff0065)
        self.assertEqual(room.map_id, 101)
        self.assertEqual(room.bot_counts, (7, 7))
        self.assertEqual(room.battle_settings, RoomBattleSettings(True, 0, True))
        self.edit(b'[room-options] [time 12]', map_selector=0xffffffff)
        self.assertEqual(room.map_id, 101)

    def test_room_rules_reach_the_shared_arena_and_are_saved_on_edit(self):
        room = self.create(b'[mode gravity] [bots 0:1] Wasteland')
        self.assertEqual((room.battle_mode, room.description), ('gravity', 'Wasteland'))
        self.assertEqual(self.native.settings(room)['comment'], '[mode gravity] [bots 0:1] [notes] Wasteland')
        self.edit(b'[mode elimination] [mode gravity] [bots 0:1] New rules')
        self.assertEqual((room.battle_mode, room.description), ('elimination', 'New rules'))
        self.endpoint.rooms.ready(1, True)
        self.endpoint.rooms.start(1)
        self.endpoint.start_shared.assert_called_once_with(
            [1], [1], map_id=7, bot_counts=(0, 1), room_mode='elimination',
            room_settings=RoomBattleSettings())

    def test_native_room_lives_and_time_survive_edits_and_reach_the_arena(self):
        room = self.create(b'[lives unlimited] [time 12] [bots 0:1] Practice')
        self.assertEqual(room.battle_settings, RoomBattleSettings(True, 12))
        self.assertEqual(self.native.settings(room)['comment'],
                         '[lives unlimited] [time 12] [bots 0:1] [notes] Practice')
        self.assertEqual(self.native.listing(room)[1][9], '[bots 0:1] Practice')
        self.edit(b'[time 61] [lives unlimited]')
        self.assertEqual(room.battle_settings, RoomBattleSettings(True, 12))
        self.edit(b'[room-options] [time endless]')
        self.assertEqual(room.battle_settings, RoomBattleSettings(True, 0))
        self.assertEqual((room.description, room.bot_counts), ('Practice', (0, 1)))
        self.endpoint.rooms.ready(1, True)
        self.endpoint.rooms.start(1)
        self.endpoint.start_shared.assert_called_once_with(
            [1], [1], map_id=7, bot_counts=(0, 1), room_settings=RoomBattleSettings(True, 0))

    def test_full_room_form_can_clear_notes_without_resetting_rules(self):
        room = self.create(b'[mode gravity] [lives unlimited] [time 12] [bots 0:1] Practice')
        self.edit(b'[room-form] [mode gravity] [lives unlimited] [time 12]')
        self.assertEqual((room.description, room.bot_counts), ('', (0, 0)))
        self.assertEqual(room.battle_mode, 'gravity')
        self.assertEqual(room.battle_settings, RoomBattleSettings(True, 12))

    def test_unlimited_ammo_round_trips_and_can_be_disabled(self):
        room = self.create(b'[room-form] [ammo unlimited] [bots 0:1] [notes] Practice')
        self.assertTrue(room.battle_settings.unlimited_ammo)
        self.assertIn('[ammo unlimited]', self.native.settings(room)['comment'])
        self.edit(b'[room-form] [bots 0:1] [notes] Edited with an older client')
        self.assertTrue(room.battle_settings.unlimited_ammo)
        self.edit(b'[room-form] [ammo limited] [bots 0:1] [notes] Practice')
        self.assertFalse(room.battle_settings.unlimited_ammo)

    def test_room_form_counts_are_independent_of_notes(self):
        room = self.create(b'[room-form] [mode gravity] [time endless] [bots 2:3] [notes] [bots 7:7] Notes')
        self.assertEqual((room.bot_counts, room.description), ((2, 3), '[bots 7:7] Notes'))
        self.edit(b'[room-form] [mode gravity] [time endless] [bots 0:1] [notes] [mode gravity] Notes')
        self.assertEqual((room.bot_counts, room.description), ((0, 1), '[mode gravity] Notes'))
        self.edit(b'[room-form] [mode gravity] [time endless] [bots 0:0] [notes]')
        self.assertEqual((room.bot_counts, room.description), ((0, 0), ''))

    def test_small_map_limits_the_combined_team_size(self):
        self.endpoint.map_catalog[60] = 'rudniki_01'
        room = self.endpoint.rooms.create(1, map_id=60, bot_counts=(7, 7))
        self.assertEqual(room.active_bot_counts, (4, 5))
        self.assertEqual(self.native.settings(room)['limits'][1]['maxCount'], (5, 5, 5))

    @staticmethod
    def _tank(name, tier):
        shell = SimpleNamespace(damage=250, penetration=(220, 180))
        weapon = SimpleNamespace(clip=1, burst_count=1, reload=7., pump_reload_times=(),
                                 interval=0., burst_interval=0., shells=(shell,))
        return SimpleNamespace(nation='test', name=name, tier=tier, health=1000, weapon=weapon)

    def test_bot_recipe_uses_free_slots_after_humans(self):
        first, second, third = (self._tank('first', 8), self._tank('second', 9),
                                self._tank('third', 9))
        with patch.object(bot_roster, '_catalog', return_value=()):
            rows = select_room_bot_roster('/unused', first, teams=(1, 1, 2), bot_counts=(1, 2),
                                          seed='game', human_definitions=(first, second, third))
        self.assertEqual([(row.team, row.slot) for row in rows], [(2, 1), (2, 2), (1, 2)])
        self.assertIs(rows[0].definition, second)
        with self.assertRaises(ValueError):
            select_room_bot_roster('/unused', first, teams=(1, 2), bot_counts=(0, 7), seed='game')

    def test_bot_roster_pairs_roles_without_repeating_models(self):
        player = self._tank('player', 8)
        classes = ('heavyTank', 'mediumTank', 'AT-SPG', 'lightTank')
        candidates = [bot_roster._Candidate('test', 'player', 8, classes[0])]
        candidates += [bot_roster._Candidate('test', f'{kind}{i}', 8, kind)
                       for kind in classes for i in range(7)]

        def load(_game, _nation, name, grade):
            definition = self._tank(name, 8)
            definition.grade = grade
            return definition

        with patch.object(bot_roster, '_catalog', return_value=candidates), patch.object(
                bot_roster, '_bot_vehicle', side_effect=load):
            def choose(seed):
                return bot_roster.select_bot_roster('/unused', player, enemies=7, allies=6, seed=seed)

            for seed in range(12):
                one = choose(seed)
                self.assertEqual(one, choose(seed))
                self.assertEqual(len({row.definition.name for row in one}), 13)
                self.assertNotIn('player', {row.definition.name for row in one})
                roles = [Counter(bot_roster._role(row.vehicle_class) for row in one if row.team == side)
                         for side in (1, 2)]
                roles[0]['heavyTank'] += 1
                self.assertEqual(roles[0], roles[1])
                grades = [Counter(row.definition.grade for row in one if row.team == side)
                          for side in (1, 2)]
                self.assertIn(bot_roster.TOP, grades[0])
                self.assertIn(bot_roster.TOP, grades[1])
                self.assertGreater(len(grades[0] | grades[1]), 1)
                self.assertTrue(all(abs(grades[0][i]-grades[1][i]) <= 1 for i in range(3)))

    def test_custom_lineup_and_preset_round_trip_together(self):
        from solbreaker.storage import AccountStore
        from solbreaker.training_config import TrainingConfig

        values = (10, 25, 50, 75, 100, 0, 0, 100, 0, 0, 0)
        presets = (battle_setup.Preset(values, True),) + (battle_setup.Preset(),)*3
        slots = (battle_setup.EMPTY, 0x492, 0x43211) + (0,)*10
        setup = battle_setup.BattleSetup(True, 1, slots, presets)
        encoded = setup.encode()
        self.assertEqual(battle_setup.split_description('settings/custom1/' + encoded.hex()),
                         ('settings', setup))
        self.assertEqual(battle_setup.random_filter(slots[1]), (9, 4))
        with self.assertRaises(ValueError):
            battle_setup.BattleSetup(True, 1, (0x5b2,)*13, presets).decode(
                battle_setup.BattleSetup(True, 1, (0x5b2,)*13, presets).encode())
        with tempfile.TemporaryDirectory() as directory:
            store = AccountStore(Path(directory))
            self.addCleanup(store.close)
            config = TrainingConfig(mode='bots')
            account_id = store.db.execute('SELECT id FROM accounts LIMIT 1').fetchone()['id']
            store.save_training_config(account_id, config, setup=setup)
            store.save_training_config(account_id, config)
            self.assertEqual(store.battle_setup(account_id), setup)

    def test_custom_lineup_leaves_empty_slots_unfilled(self):
        player = self._tank('player', 8)
        stock = self._tank('random', 9)
        exact = self._tank('custom', 8)
        exact.armor_states_controller = None
        target = SimpleNamespace(installed=True, nation='test', model='custom', vehicle_class='heavyTank')
        setup = battle_setup.BattleSetup(True, 0, (battle_setup.EMPTY,)*6
                                         + (0x492, 0x43211) + (battle_setup.EMPTY,)*5)
        candidate = bot_roster._Candidate('test', 'random', 9, 'lightTank')
        with patch.object(bot_roster, '_catalog', return_value=(candidate,)), patch.object(
                bot_roster, '_bot_vehicle', return_value=stock), patch(
                'solbreaker.practice_catalog.catalog', return_value={0x3211: target}), patch(
                'solbreaker.practice_catalog.definition', return_value=exact) as load, patch.object(
                bot_roster, '_geometry_supported', return_value=True):
            roster = bot_roster.custom_bot_roster('/unused', player, setup, capacity=5, seed=8)
            load.assert_called_once_with(Path('/unused'), target, 4)
        self.assertEqual([(row.team, row.slot, row.definition.name) for row in roster],
                         [(2, 0, 'random'), (2, 1, 'custom')])

    def test_upgrade_adds_hint_and_preserves_existing_locale_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = Path(directory).resolve()
            data = lab/'client/Data'
            description = {'name':'Description', 'children':[
                {'path':'Container/Caption','components':{'UITextComponent':{'text':'old caption'}}},
                {'path':'Container/TextFieldContainer/VerticalBox/Background/TextField/Hint',
                 'components':{'UITextComponent':{'text':'old hint'}}},
            ]}
            rules = {'name':'LocalRules', 'children':[
                {'path':'Caption', 'bindings':[['UITextComponent.text', 'old rules']]}]}
            documents = {FORM:{'Prototypes':[{'name':'Content','children':[description, rules]}]},
                         STRINGS_EN:{'Solbreaker':'custom name'}, STRINGS_RU:{'Custom':'Рудники'}}
            overrides = {}
            for relative, document in documents.items():
                target = data/relative
                target.parent.mkdir(parents=True, exist_ok=True)
                content = dvpl.encode(yaml.safe_dump(document).encode())
                target.write_bytes(content)
                overrides[relative] = hashlib.sha256(content).hexdigest()
            actions = data/ACTIONS
            actions.write_bytes(dvpl.encode(b'old actions'))
            overrides[ACTIONS] = hashlib.sha256(actions.read_bytes()).hexdigest()
            manifest = {'ui_overrides':overrides}
            (lab/'manifest.json').write_text(json.dumps(manifest))
            updated = upgrade_form(lab, manifest)
            self.assertIs(upgrade_form(lab, updated), updated)
            self.assertEqual(updated['room_bot_hint_version'], 1)
            self.assertEqual(yaml.safe_load(dvpl.read(data/STRINGS_EN))['Solbreaker'], 'custom name')
            self.assertEqual(yaml.safe_load(dvpl.read(data/STRINGS_RU))['Custom'], 'Рудники')
            self.assertIn(b'Local/BattleSettings/RoomBotsHint', dvpl.read(data/FORM))
            self.assertIn(b'Local/BattleSettings/Gravity', dvpl.read(data/FORM))
            self.assertIn(b'ChangeData(localRules, modePreview)', dvpl.read(actions))
