"""Sandbox negotiation and flight controls through the arena owner."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from solbreaker import sandbox
from solbreaker.arena import Arena, GRAVITY_JUMP_SPEC
from solbreaker.consumables import COOLDOWN, ConsumableState
from solbreaker.arena_owner import Command
from solbreaker.arena_owner_game import GameArenaHandler
from solbreaker.battle import present_vehicles, reconnect_message_parts, vehicle_property
from solbreaker.collision import Pose
from solbreaker.training_config import TrainingConfig


def report(token, sequence=1, flags=0, shot=0, direction=(0., 0., 0.)):
    return sandbox.PREFIX+sandbox.INPUT.pack(token, sequence, flags, shot, *direction)+b'\1'


class SandboxTests(TestCase):
    def test_gravity_jump_works_without_sandbox_and_rejects_heavies_and_repeats(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        config = TrainingConfig(mode='bots', battle_mode='gravity')
        arena = Arena([dict(id=1, nickname='Pilot')], b'', terrain, now=1., training=config)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        vehicle = arena.vehicle_for(1)
        vehicle.definition = SimpleNamespace(mass=25000.)
        world = Mock(motion={vehicle.entity_id: SimpleNamespace(grounded=True)})
        arena.world = world
        control = sandbox.Control(sandbox.token(arena), 1, sandbox.GRAVITY_JUMP, 0, 0., 0., 0.)
        arena.sandbox_control(1, control)
        world.add_flight_velocity.assert_not_called()
        vehicle.consumables = (ConsumableState(GRAVITY_JUMP_SPEC),)
        arena.sandbox_control(1, control)
        arena.sandbox_control(1, control)
        world.add_flight_velocity.assert_called_once_with(vehicle.entity_id, (0., 4.4, 2.5))
        self.assertEqual(vehicle.consumables[0].state_at(1.).state, COOLDOWN)
        self.assertEqual(arena.equipment_state(vehicle), ())

    def test_practice_starts_empty_and_keeps_running_past_the_clock(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='Pilot')], b'', terrain, now=1., sandbox_enabled=True)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        arena.drain_events()
        self.assertEqual([vehicle.entity_id for _, vehicle in present_vehicles(arena)], [200])
        self.assertEqual(len(arena.practice_slots), 7)
        reconnect = reconnect_message_parts(arena, arena.players[1])
        self.assertIn(vehicle_property(200, 1, b'\1'), reconnect)
        self.assertNotIn(vehicle_property(201, 1, b'\0'), reconnect)

        arena._fixed_step(1.+arena.battle_seconds)
        self.assertFalse(arena.finished_at)
        self.assertFalse(arena.solo_victory_team)
        self.assertEqual([event.data['period'] for event in arena.drain_events()
                          if event.kind == 'period'], [3])
        arena.leave(1, reason='leave')
        self.assertTrue(arena.finished_at)
        self.assertEqual(arena.results(), [])

    def test_negotiation_uses_current_arena_and_rejects_replayed_input(self):
        arena = Mock(unique_id=123, sandbox_enabled=False, owner_backed=True)
        client = SimpleNamespace(reconnect_replay=False, battle_spawned=True, sandbox_input=None,
                                 reliable_window_used=0, account_id=1)
        endpoint = Mock()
        address = ('127.0.0.1', 1234)
        self.assertTrue(sandbox.receive(endpoint, address, client, arena, report(0), 1.))
        endpoint.send.assert_not_called()
        arena.sandbox_enabled = True
        sandbox.receive(endpoint, address, client, arena, report(0), 1.1)
        endpoint.send.assert_called_once_with(address, client, sandbox.advertisement(arena))
        for stamp, token, sequence in ((1.2, 999, 2), (1.3, 123, 2), (1.4, 123, 2)):
            sandbox.receive(endpoint, address, client, arena, report(token, sequence, sandbox.BOOST), stamp)
        endpoint.owner_service.sandbox.assert_called_once()

    def test_owner_enables_boost_and_expires_lost_controls(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='Pilot')], b'', terrain, now=1., sandbox_enabled=True)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        handler = GameArenaHandler(arena, None)
        packet = report(sandbox.token(arena), flags=sandbox.BOOST)
        handler.apply(Command('sandbox', 1., 1, packet[len(sandbox.PREFIX):-1]))
        arena.step(1.02)
        self.assertTrue(arena.vehicle_for(1).sandbox_boost)
        for index in range(1, 7):
            arena.step(1.02+index/5)
        self.assertFalse(arena.vehicle_for(1).sandbox_boost)

    def test_flight_accelerates_and_banks_with_limited_response(self):
        flight = sandbox.Flight(5.)
        pose = Pose((0., 5., 0.), 0.)
        velocity, spin = sandbox.flight_velocity(flight, pose, (0.,0.,0.), (0.,0.,0.), 0.,
            9, sandbox.FLIGHT | sandbox.ASCEND, 9.81, 1/60, bounds=(-100.,-100.,100.,100.))
        self.assertGreater(flight.height, 5.)
        self.assertGreater(velocity[2], 0.)
        self.assertLess(velocity[2], .2)
        self.assertGreater(spin[1], 0.)
        self.assertLess(spin[2], 0.)

    def test_flight_brakes_at_map_edge_and_can_reverse(self):
        flight = sandbox.Flight(5.)
        pose = Pose((0., 5., 92.), 0.)
        for drive, speed in ((1, 10.), (2, 0.)):
            with self.subTest(drive=drive):
                velocity, _ = sandbox.flight_velocity(flight, pose, (0.,0.,speed), (0.,0.,0.),
                    0., drive, sandbox.FLIGHT, 9.81, 1/60, bounds=(-100.,-100.,100.,100.))
                self.assertLess(velocity[2], speed)
