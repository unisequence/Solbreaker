from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from solbreaker.arena import Arena
from solbreaker.arena_owner import Command
from solbreaker.arena_owner_game import GameArenaHandler
from solbreaker.arena_owner_service import ArenaOwnerService
from solbreaker.base import BaseEndpoint, ClientSession
from solbreaker.sandbox import CHANGE_TANK, Control, INPUT


class TrainingReentryTests(TestCase):
    def setUp(self):
        self.endpoint = BaseEndpoint.__new__(BaseEndpoint)
        self.client = ClientSession(b'', 10, phase='battle', account_id=3, arena_id='training',
                                    account_created=True, battle_spawned=True)
        self.endpoint.clients = {1: self.client}
        self.endpoint.progression = object()
        self.endpoint.server = SimpleNamespace(event=Mock())
        self.endpoint.send = Mock()
        self.endpoint.flush_arena = Mock()
        self.endpoint.rooms = SimpleNamespace(sync=Mock(), leave=Mock())
        self.arena = SimpleNamespace(result_id='training', started=1., finished_at=0., closed=False,
                                     retiring=False, building=False, unlimited_lives=True, room_settings=object(),
                                     world=SimpleNamespace(_rapier=None), drive_workers=None,
                                     players={3: SimpleNamespace(attached=True, loaded=True)},
                                     reserve_training_seat=Mock(), leave=Mock(),
                                     training_tank_available=Mock(return_value=True))
        self.endpoint.arenas = SimpleNamespace(get=lambda _: self.arena, remove_if_empty=Mock())

    def test_exit_reserves_live_seat_and_expiry_releases_it(self):
        with patch('solbreaker.base.time.monotonic', return_value=10.):
            self.endpoint.training_hangar(1, self.client, self.arena)
        self.arena.reserve_training_seat.assert_called_once_with(3)
        self.assertEqual(self.client.phase, 'account_pending')
        self.assertIsNone(self.client.arena_id)
        self.endpoint.send.assert_called_once_with(1, self.client, b'\x04\0')
        self.assertTrue(self.endpoint.choosing_training_tank(3))
        self.assertFalse(self.endpoint._claim_reconnect(self.client, 11.))
        self.endpoint._expire_reconnects(129.)
        self.arena.leave.assert_not_called()
        self.endpoint._expire_reconnects(130.)
        self.arena.leave.assert_called_once_with(3, 130., reason='disconnect')
        self.assertFalse(self.endpoint.choosing_training_tank(3))
        self.endpoint.rooms.leave.assert_not_called()

    def test_request_is_scoped_to_training_with_respawns(self):
        self.arena.unlimited_lives = False
        self.endpoint.training_hangar(1, self.client, self.arena)
        self.arena.reserve_training_seat.assert_not_called()
        self.assertEqual(self.client.phase, 'battle')
        data = INPUT.pack(1, 2, CHANGE_TANK, 0, 0., 0., 0.)
        self.assertEqual(Control.decode(data).flags, CHANGE_TANK)
        with self.assertRaises(ValueError):
            Control.decode(INPUT.pack(1, 2, CHANGE_TANK, 0, 0., 0., 1.))

    def test_owner_leave_clears_the_previous_battles_swap_advertisement(self):
        self.client.training_swap_refresh_at = 25.
        self.client.owner_leave_pending = True
        service = ArenaOwnerService(self.endpoint)
        service._on_delivered(SimpleNamespace(arena=self.arena), Command('leave', 24., 3), None)

        self.assertIsNone(self.client.arena_id)
        self.assertEqual(self.client.phase, 'account_pending')
        self.assertEqual(self.client.training_swap_refresh_at, 0.)
        self.assertFalse(self.client.owner_leave_pending)

    def test_parked_tank_stays_alive_and_owner_ignores_late_control(self):
        arena = Arena.__new__(Arena)
        gun = SimpleNamespace(yaw=.2, pitch=.1, set_angles=Mock())
        pose = object()
        tank = SimpleNamespace(entity_id=10, account_id=3, alive=True, health=600, pose=pose,
                               flags=1, sandbox_god_mode=True, sandbox_boost=True, gun=gun)
        arena.players = {3: SimpleNamespace(account_id=3, vehicle_id=10, attached=True, loaded=True)}
        arena.vehicles = {10: tank}
        arena.hangar_players = set()
        arena.training_bots = {}
        arena.respawn_at = {}
        arena.boss_pending_roles = {}
        arena.big_boss = None
        arena._sandbox_inputs = {3: object()}
        arena._set_fast_reload = Mock()
        arena._cancel_burst = Mock()
        arena.room_settings = object()
        arena.unlimited_lives = True
        arena.started = 1.
        arena.finished_at = arena.closed = arena.retiring = False
        arena.world = SimpleNamespace(_rapier=None)
        arena.drive_workers = None
        arena.result_id = 'training'
        arena.drain_events = Mock(return_value=())
        arena.results = Mock(return_value=())
        handler = GameArenaHandler(arena, Mock())

        with patch('solbreaker.arena_owner_game.snapshot', return_value=b'state'):
            handler.apply(Command('training_park', 2., 3))
            handler.apply(Command('control', 2.1, 3, b'\x01\x00'))

        self.assertIn(3, arena.hangar_players)
        self.assertFalse(arena.players[3].loaded)
        self.assertTrue(arena._player_attached(tank))
        self.assertIs(tank.pose, pose)
        self.assertEqual(tank.health, 600)
        self.assertEqual(tank.flags, 0)
        self.assertFalse(tank.sandbox_god_mode)
        self.assertNotIn(3, arena._sandbox_inputs)
