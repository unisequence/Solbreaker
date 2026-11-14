"""Sandbox negotiation and flight controls through the arena owner."""
import struct
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from solbreaker import sandbox
from solbreaker.battle_settings import decode, packed_recipe
from solbreaker.battle_settings_wire import RULES_SHIFT, TITLE
from solbreaker.arena import Arena, GRAVITY_JUMP_SPEC
from solbreaker.consumables import COOLDOWN, ConsumableState
from solbreaker.arena_owner import Command
from solbreaker.arena_owner_game import GameArenaHandler
from solbreaker.battle import (VOICE_ENEMY_KILLED, VOICE_TRACK_DESTROYED, arena_update,
                               crew_voice_message, event_messages, health_changed, pb_blob, pb_int,
                               present_vehicles, reconnect_message_parts, team_kill_score,
                               vehicle_info, vehicle_property)
from solbreaker.collision import Pose
from solbreaker.training_config import RoomBattleSettings, TrainingConfig


def report(token, sequence=1, flags=0, shot=0, direction=(0., 0., 0.)):
    return sandbox.PREFIX+sandbox.INPUT.pack(token, sequence, flags, shot, *direction)+b'\1'


class SandboxTests(TestCase):
    def test_solo_battle_recipe_carries_the_room_rules(self):
        catalog = {101: 'rudniki_evening'}
        config = decode(TITLE, 'SolbreakerBots/2/1/2/1/0/1/0/1', 101, catalog)
        self.assertEqual(config.battle_settings, RoomBattleSettings(True, 0, True))
        self.assertEqual(packed_recipe(config, catalog) >> RULES_SHIFT, 7)
        self.assertEqual(packed_recipe(config, catalog, include_rules=False), 1010121)
        self.assertEqual(TrainingConfig.from_dict(config.to_dict()), config)
        self.assertEqual(TrainingConfig.from_dict({'mode': 'bots'}).battle_settings,
                         RoomBattleSettings())
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='Pilot')], b'', terrain, now=1., training=config)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        self.assertTrue(arena.endless)
        self.assertTrue(arena.unlimited_lives)
        self.assertTrue(arena.unlimited_ammo)
        self.assertFalse(arena._combat_can_finish())

    def test_teleport_requires_sandbox_and_a_clear_destination(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='Pilot')], b'', terrain, now=1., sandbox_enabled=True)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        arena.drain_events()
        vehicle = arena.vehicle_for(1)
        world = Mock()
        arena.world = world
        pose = Pose((20., 1., 30.), vehicle.pose.yaw, 0., 0.)
        world.training_placement.side_effect = (None, pose)
        control = sandbox.Control(sandbox.token(arena), 1, sandbox.TELEPORT, 0, 0., 0., 0.)
        with patch.object(arena, 'aim_hit', return_value=(None, (20., 0., 30.))):
            arena.practice = False
            arena.sandbox_enabled = False
            arena.sandbox_control(1, control)
            world.training_placement.assert_not_called()
            arena.sandbox_enabled = True
            arena.sandbox_control(1, control)
            world.relocate_training_body.assert_not_called()
            arena.sandbox_control(1, control)
        world.training_placement.assert_called_with(
            vehicle.entity_id, 20., 30., pose.yaw, stationary=True)
        world.relocate_training_body.assert_called_once_with(vehicle.entity_id, pose)
        self.assertEqual(vehicle.pose, pose)
        self.assertIn(sandbox.advertisement(arena, notice=sandbox.TELEPORTED),
                      event_messages(arena, arena.players[1], arena.drain_events()))

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

    def test_restore_hp_updates_players_and_requires_sandbox_and_a_living_tank(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='One'), dict(id=2, nickname='Two')],
                      b'', terrain, now=1., solo=False, sandbox_enabled=False)
        self.addCleanup(arena.close)
        arena.gravity_mode = True
        arena.ready(1, 1.)
        arena.ready(2, 1.)
        arena.drain_events()
        vehicle = arena.vehicle_for(1)
        vehicle.health = 1
        body = report(sandbox.token(arena), flags=sandbox.RESTORE_HP)
        control = sandbox.Control.decode(body[len(sandbox.PREFIX):-1])
        arena.sandbox_control(1, control)
        self.assertEqual(vehicle.health, 1)
        self.assertEqual(arena.drain_events(), [])

        arena.sandbox_enabled = True
        arena.sandbox_control(1, control)
        self.assertEqual(vehicle.health, vehicle.max_health)
        events = arena.drain_events()
        self.assertEqual([event.kind for event in events], ['sandbox_restore_hp'])
        notice = sandbox.advertisement(arena, notice=sandbox.HP_RESTORED)
        for player_id in (1, 2):
            result = event_messages(arena, arena.players[player_id], events)
            self.assertIn(vehicle_property(vehicle.entity_id, 3, struct.pack('<h', vehicle.health)), result)
            self.assertIn(health_changed(vehicle.entity_id, vehicle.health, 0), result)
            self.assertEqual(notice in result, player_id == 1)

        arena.sandbox_control(1, control)
        self.assertEqual(arena.drain_events(), [])
        vehicle.health = 0
        arena.sandbox_control(1, control)
        self.assertEqual(vehicle.health, 0)
        self.assertEqual(arena.drain_events(), [])

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

    def test_practice_target_selection_fire_and_clear_reuse_the_same_slot(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='Pilot')], b'', terrain, now=1., sandbox_enabled=True)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        arena.drain_events()
        player = arena.vehicle_for(1)
        entity = min(arena.practice_slots)
        alternate = entity + 1
        arena.practice_types = {0: [entity], 1: [alternate]}
        world = Mock(assets=SimpleNamespace(bounds=(0., 0., 100., 100.)))
        arena.world = world
        pose = Pose((30., 1., 35.), 0.)
        world.training_placement.return_value = pose

        def action(flags):
            arena.sandbox_control(1, sandbox.Control(sandbox.token(arena), 1, flags, 0, 0., 0., 0.))

        def restore(ident, placement):
            vehicle = arena.vehicles[ident]
            vehicle.pose = placement
            vehicle.health = 280

        with patch.object(arena, 'aim_hit', return_value=(None, pose.position)), \
             patch.object(arena, '_restore_vehicle', side_effect=restore):
            action(sandbox.NEXT_TARGET | sandbox.TOGGLE_TARGET_FIRE | sandbox.SPAWN_TARGET)
            self.assertEqual(arena.practice_selection, 1)
            self.assertEqual(set(arena.practice_gunners), {alternate})
            self.assertTrue(arena.vehicles[alternate].alive)
            self.assertFalse(arena.vehicles[entity].alive)
            self.assertTrue(next(e for e in arena.drain_events() if e.kind == 'practice_spawn').data['first'])

            action(sandbox.CLEAR_TARGETS)
            self.assertFalse(arena.vehicles[alternate].alive)
            self.assertFalse(arena.practice_gunners)
            self.assertNotEqual(arena.vehicles[alternate].pose, pose)
            self.assertIn('practice_clear', [e.kind for e in arena.drain_events()])
            self.assertEqual(arena.vehicle_for(1), player)

            action(sandbox.TOGGLE_TARGET_FIRE | sandbox.SPAWN_TARGET)
            self.assertTrue(arena.vehicles[alternate].alive)
            self.assertFalse(arena.practice_gunners)
            self.assertFalse(next(e for e in arena.drain_events() if e.kind == 'practice_spawn').data['first'])

    def test_training_room_waits_for_last_player_even_if_one_team_leaves(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='One'), dict(id=2, nickname='Two')],
                      b'', terrain, now=1., solo=False, room_settings=RoomBattleSettings())
        self.addCleanup(arena.close)
        owner = GameArenaHandler(arena, None)
        owner.apply(Command('ready', 1., 1))
        owner.apply(Command('ready', 1., 2))
        owner.apply(Command('leave', 2., 1))
        arena._fixed_step(2.)
        self.assertFalse(arena.finished_at)
        owner.apply(Command('leave', 3., 2))
        self.assertEqual((arena.finished_at, arena.winner, arena.finish_reason), (3., 0, 'leave'))

    def test_training_room_lives_and_clock_are_independent(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        for minutes in (0, 1):
            with self.subTest(minutes=minutes):
                arena = Arena([dict(id=1, nickname='One'), dict(id=2, nickname='Two')],
                              b'', terrain, now=1., solo=False,
                              room_settings=RoomBattleSettings(True, minutes))
                self.addCleanup(arena.close)
                arena.ready(1, 1.)
                arena.ready(2, 1.)
                self.assertEqual(next(e.data['seconds'] for e in arena.events if e.kind == 'period'),
                                 60 * minutes)
                vehicle = arena.vehicle_for(1)
                arena._damage_health(vehicle, vehicle.health, None, 2., shell_hit=False)
                arena._fixed_step(2.)
                self.assertEqual(arena.respawn_at, {vehicle.entity_id: 5.})
                self.assertFalse(arena.finished_at)
                arena._fixed_step(700.)
                self.assertEqual(arena.finish_reason, 'timeout' if minutes else '')

    def test_respawn_restores_the_native_roster_before_notifying_the_avatar(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='Pilot')], b'', terrain, now=1., sandbox_enabled=True)
        self.addCleanup(arena.close)
        player = arena.players[1]
        vehicle = arena.vehicle_for(1)
        arena.emit('practice_spawn', vehicle.entity_id, first=False, repaired=[])
        data = event_messages(arena, player, arena.drain_events())
        info = vehicle_info(vehicle.entity_id, 'Pilot', 1, 1, b'')
        roster_update = arena_update(11, pb_blob(10, pb_blob(1, info)))
        resurrection = arena_update(20, pb_blob(6, pb_int(1, vehicle.entity_id)))
        self.assertIn(health_changed(vehicle.entity_id, vehicle.health, 0) +
                      roster_update + resurrection, data)
        notice = sandbox.advertisement(arena, notice=sandbox.TARGET_PLACED)
        self.assertNotIn(notice, data)
        arena.emit('practice_spawn', vehicle.entity_id, first=False, repaired=[],
                   placed_by=player.vehicle_id)
        events = arena.drain_events()
        self.assertIn(notice, event_messages(arena, player, events))
        self.assertNotIn(notice, event_messages(
            arena, SimpleNamespace(vehicle_id=999, team=2), events))

    def test_fast_reload_is_separate_from_room_ammunition(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        for enabled in (False, True):
            with self.subTest(sandbox=enabled):
                arena = Arena([dict(id=1, nickname='One', ammo=1),
                               dict(id=2, nickname='Two')], b'', terrain,
                              now=1., solo=False, sandbox_enabled=enabled,
                              room_settings=RoomBattleSettings(unlimited_ammo=True))
                self.addCleanup(arena.close)
                arena.ready(1, 1.)
                arena.ready(2, 1.)
                vehicle = arena.vehicle_for(1)
                self.enterContext(patch.object(arena, 'shell_for', return_value=SimpleNamespace(
                    effect_index=0, speed=500., gravity=0., compact_id=vehicle.shell_id,
                    missile=None, max_distance=100.)))
                arena._fire_vehicle(vehicle, 1.)
                self.assertEqual((vehicle.clip, vehicle.ammo, vehicle.reload_at), (0, 1, 7.))
                control = sandbox.Control(sandbox.token(arena), 1, sandbox.FAST_RELOAD, 0, 0., 0., 0.)
                arena.sandbox_control(1, control)
                self.assertEqual(vehicle.reload_at, 1.5 if enabled else 7.)
                if enabled:
                    arena._fixed_step(1.5)
                    arena._fire_vehicle(vehicle, 1.5)
                    self.assertEqual((vehicle.clip, vehicle.ammo, vehicle.reload_at), (0, 1, 2.))
                    arena.sandbox_control(1, sandbox.Control(sandbox.token(arena), 2, 0, 0, 0., 0., 0.))
                    self.assertEqual(vehicle.reload_total, 6.)

    def test_unlimited_lives_keep_roster_active_and_send_kill_statistics(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        for unlimited in (False, True):
            with self.subTest(unlimited=unlimited):
                arena = Arena([dict(id=1, nickname='One'), dict(id=2, nickname='Two')],
                              b'', terrain, now=1., solo=False,
                              room_settings=RoomBattleSettings(unlimited))
                self.addCleanup(arena.close)
                killer, target = arena.vehicle_for(1), arena.vehicle_for(2)
                arena._damage_health(target, target.health, killer.entity_id, 2., shell_hit=False)
                events = arena.drain_events()
                data = event_messages(arena, arena.players[1], events)
                victim, source = pb_int(1, target.entity_id), pb_int(2, killer.entity_id)
                roster_death = arena_update(6, pb_blob(6, victim + source))
                kills = arena_update(5, pb_blob(5, pb_blob(1, pb_int(1, killer.entity_id) + pb_int(2, 1))))
                self.assertIn(vehicle_property(target.entity_id, 1, b'\0'), data)
                self.assertIn(kills, data)
                self.assertEqual(roster_death in data, not unlimited)
                self.assertEqual(team_kill_score(arena, arena.players[1]) in data, unlimited)
                cue = crew_voice_message(VOICE_ENEMY_KILLED)
                self.assertEqual(data.count(cue), 1)
                self.assertNotIn(cue, event_messages(arena, arena.players[2], events))

    def test_track_voice_goes_to_the_driver_once_per_break(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='One'), dict(id=2, nickname='Two')],
                      b'', terrain, now=1., solo=False)
        self.addCleanup(arena.close)
        vehicle = arena.vehicle_for(1)
        for broken in (True, False):
            arena.emit('module_state', vehicle.entity_id, critical=(), destroyed=(34,),
                       reason=1, extra=34, track_just_broke=broken)
        events = arena.drain_events()
        cue = crew_voice_message(VOICE_TRACK_DESTROYED)
        self.assertEqual(event_messages(arena, arena.players[1], events).count(cue), 1)
        self.assertNotIn(cue, event_messages(arena, arena.players[2], events))

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
