"""Sandbox negotiation and flight controls through the arena owner."""
import math
import struct
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from solbreaker import sandbox
from solbreaker.battle_settings import decode, packed_recipe
from solbreaker.battle_settings_wire import RULES_SHIFT, TITLE
from solbreaker.arena import Arena, GRAVITY_JUMP_SPEC
from solbreaker.consumables import ACTIVE, COOLDOWN, READY, ConsumableSpec, ConsumableState
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

    def test_god_mode_converts_shell_damage_to_non_penetration(self):
        from solbreaker.armor import ShotResult

        terrain = SimpleNamespace(height=lambda x, z: 0.)
        for enabled in (False, True):
            with self.subTest(sandbox=enabled):
                arena = Arena([dict(id=1, nickname='One'), dict(id=2, nickname='Two')],
                              b'', terrain, now=1., solo=False, sandbox_enabled=enabled)
                self.addCleanup(arena.close)
                arena.ready(1, 1.)
                arena.ready(2, 1.)
                shooter, target = (arena.vehicle_for(i) for i in (1, 2))
                control = sandbox.Control(sandbox.token(arena), 1, sandbox.GOD_MODE, 0, 0., 0., 0.)
                arena.sandbox_control(2, sandbox.Control.decode(
                    sandbox.INPUT.pack(control.token, 1, control.flags, 0, 0., 0., 0.)))
                health = target.health
                event = SimpleNamespace(shooter=shooter.entity_id, target=target.entity_id,
                                        shot=1, at=1., point=(0., 0., 0.), target_state=None)
                hit = SimpleNamespace(external=False)
                shell = SimpleNamespace(kind='ARMOR_PIERCING', damage=100, missile=None, poison_level=None)
                arena._apply_impact(event, ShotResult('penetrated', hit, 100), target.entity_id,
                                    direct=True, shell=shell)
                self.assertEqual(target.health, health if enabled else health - 100)
                feedback = [row.data['outcome'] for row in arena.events if row.kind == 'feedback']
                self.assertEqual(feedback, ['not_penetrated' if enabled else 'penetrated'])

    def test_fast_reload_rescales_equipment_and_boss_cooldowns(self):
        from solbreaker import big_boss

        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='One'), dict(id=2, nickname='Two')], b'', terrain,
                      now=1., solo=False, sandbox_enabled=True, room_mode='big_boss')
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        arena.ready(2, 1.)
        vehicle = arena.vehicle_for(1)
        charge = next(slot for slot in vehicle.consumables if slot and slot.spec is big_boss.MEGASHOT)
        self.assertEqual(charge.ready_at, 31.)
        equipment = ConsumableState(ConsumableSpec('boost', 99, 0x63fb, 3., 20.))
        vehicle.consumables = (equipment, *vehicle.consumables[1:])
        self.assertTrue(equipment.activate(1.))
        token = sandbox.token(arena)
        arena.sandbox_control(1, sandbox.Control(token, 1, sandbox.FAST_RELOAD, 0, 0., 0., 0.))
        self.assertEqual((equipment.ready_at, charge.ready_at), (1.5, 1.5))
        self.assertTrue(any(event.kind == 'consumable_state' and event.data['ident'] == charge.spec.ident
                            and event.data['total'] == 1 for event in arena.events))
        self.assertTrue(charge.activate(1.5))
        self.assertEqual(charge.ready_at, 2.)
        self.assertEqual(equipment.state_at(1.5).state, ACTIVE)
        self.assertFalse(equipment.activate(1.5))
        self.assertEqual(equipment.state_at(4.).state, READY)
        arena.simulation_time = 1.75
        arena.sandbox_control(1, sandbox.Control(token, 2, 0, 0, 0., 0., 0.))
        self.assertEqual(charge.ready_at, 21.75)

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

    def test_big_boss_mode_decodes_in_both_battle_forms(self):
        from solbreaker.native_rooms import room_options
        config = decode(TITLE, 'SolbreakerBots/2/1/7/1/4/0/-1/0', 0, {})
        self.assertEqual(config.battle_mode, 'big_boss')
        self.assertEqual(packed_recipe(config, {}), 4171)
        _, _, mode, _ = room_options('[mode big_boss] [bots 1:2] [notes] Test')
        self.assertEqual(mode, 'big_boss')

    def test_big_boss_wave_passes_through_cover_and_respects_mass_and_cooldown(self):
        from solbreaker.big_boss import BLAST_WAVE, GRAVITON, blast_impulse
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        accounts = [dict(id=i, nickname=str(i), team=team) for i, team in enumerate((1, 1, 2, 2), 1)]
        arena = Arena(accounts, b'', terrain, now=1., solo=False, room_mode='big_boss',
                      room_settings=RoomBattleSettings())
        self.addCleanup(arena.close)
        for account in accounts:
            arena.ready(account['id'], 1.)
        by_team = {team: [v for v in arena.vehicles.values() if v.team == team] for team in (1, 2)}
        boss = next(v for v in by_team[1] if v.entity_id in arena.big_boss.bosses)
        ally = next(v for v in by_team[1] if v is not boss)
        target, covered = by_team[2]
        minion = next(v for v in by_team[2] if not arena._is_boss(v))
        arena.big_boss.roles[minion.entity_id] = GRAVITON
        arena._set_boss_role_ability(minion)
        for vehicle, x in ((boss, 0.), (ally, 6.), (target, 8.), (covered, 12.)):
            vehicle.pose = Pose((x, 0., 0.), 0.)
            vehicle.definition = SimpleNamespace(mass=25000., weapon=SimpleNamespace())
        world = Mock()
        world._static_ray.side_effect = lambda _start, end: (100 if end[0] >= 10. else -1,)
        world.motion = {enemy.entity_id: SimpleNamespace(grounded=True)
                        for enemy in (target, covered)}
        arena.world = world
        world.advance_close.return_value = True
        self.assertFalse(arena._boss_blast(ally))
        old_hp = [v.health for v in (boss, ally, target, covered)]
        self.assertTrue(arena._boss_blast(boss))
        self.assertFalse(arena._boss_blast(boss))
        self.assertEqual([v.health for v in (boss, ally, target, covered)],
                         [old_hp[0], old_hp[1],
                          *(hp - int(round(enemy.max_health * .1) *
                                      arena.big_boss.damage_factor(enemy.entity_id) + .5)
                            for hp, enemy in zip(old_hp[2:], (target, covered)))])
        impacts = world.add_flight_velocity.call_args_list
        self.assertEqual([impact.args[0] for impact in impacts], [target.entity_id, covered.entity_id])
        for impact in impacts:
            self.assertGreater(impact.args[1][1], 0.)
            self.assertGreater(math.hypot(*impact.kwargs['angular_delta']), 0.)
        state = next(s for s in boss.consumables if s and s.spec is BLAST_WAVE)
        self.assertEqual(state.ready_at, 41.)
        self.assertEqual(arena.equipment_state(boss), ())
        origin, near = (0., 0., 0.), (8., 0., 0.)
        close, light_spin = blast_impulse(origin, near, 25000., 0., 'lightTank')
        medium, medium_spin = blast_impulse(origin, near, 45000., 0., 'mediumTank')
        heavy, heavy_spin = blast_impulse(origin, near, 45000., 0., 'heavyTank')
        edge, _ = blast_impulse(origin, (49., 0., 0.), 25000., 0.)
        self.assertGreater(close[0], 90.)
        self.assertGreater(heavy[0], 28.)
        self.assertEqual(heavy[1], 0.)
        self.assertIsNone(heavy_spin)
        self.assertGreater(medium[1], 10.)
        self.assertGreater(math.hypot(*light_spin), math.hypot(*medium_spin))
        self.assertGreater(close[0], heavy[0])
        self.assertLess(edge[0], heavy[0])
        self.assertIsNone(blast_impulse(origin, (50., 0., 0.), 25000., 0.))
        from solbreaker.battle import provision_effect_message
        wave_visual = provision_effect_message(boss.entity_id, BLAST_WAVE.compact_id)
        wave_event, = (event for event in arena.events if event.kind == 'boss_blast')
        self.assertEqual(wave_event.data['targets'], (target.entity_id, covered.entity_id))
        for player in arena.players.values():
            self.assertEqual(event_messages(arena, player, (wave_event,)), wave_visual)

    def test_blast_landing_recovers_a_hull_thrown_over_the_map_edge(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        config = TrainingConfig(mode='bots', battle_mode='big_boss', enemies=1)
        arena = Arena([dict(id=1, nickname='Pilot')], b'', terrain, now=1., training=config)
        self.addCleanup(arena.close)
        hull = arena.vehicle_for(1)
        entity = hull.entity_id
        outside = Pose((52., 5., 0.), 0.)
        inside = Pose((45., 0., 0.), 0.)
        world = arena.world = Mock(assets=SimpleNamespace(bounds=(-50., -50., 50., 50.)),
                                   poses={entity: outside}, dynamic_recoveries=[],
                                   motion={entity: SimpleNamespace(grounded=False)})
        world.advance_close.return_value = True
        world.training_placement.return_value = inside
        arena.big_boss.blast_landings[entity] = hull.pose
        arena._recover_blast_landings()
        world.training_placement.assert_called_once_with(entity, 45., 0., 0., stationary=True)
        world.relocate_training_body.assert_called_once_with(entity, inside)
        self.assertEqual(world.dynamic_recoveries, [(entity, outside.position, inside.position)])
        self.assertNotIn(entity, arena.big_boss.blast_landings)

    def test_big_boss_has_three_lives_and_weighted_tickets(self):
        from solbreaker.battle import boss_lives_message, team_points_message
        from solbreaker.big_boss import BOSS_AURA_MODIFIER
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        config = TrainingConfig(mode='bots', battle_mode='big_boss', enemies=2)
        arena = Arena([dict(id=1, nickname='Pilot')], b'', terrain, now=1., training=config)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        boss = next(v for v in arena.vehicles.values() if v.team == 2 and v.entity_id in arena.big_boss.bosses)
        regular = next(v for v in arena.vehicles.values() if v.team == 2 and v is not boss)
        player = arena.vehicle_for(1)
        self.assertEqual(player.max_health, 1120)
        self.assertIn(BOSS_AURA_MODIFIER, arena.state_modifiers(boss))
        self.assertNotIn(BOSS_AURA_MODIFIER, arena.state_modifiers(regular))
        for life in (2, 1, 0):
            arena._damage_health(boss, boss.max_health, player.entity_id, 1., shell_hit=False)
            self.assertEqual(arena.big_boss.lives[boss.entity_id], life)
            self.assertNotIn(BOSS_AURA_MODIFIER, arena.state_modifiers(boss))
            self.assertEqual(boss.entity_id in arena.respawn_at, life > 0)
            if life:
                arena.respawn_at.pop(boss.entity_id)
                boss.health = boss.max_health
        arena._damage_health(regular, regular.max_health, player.entity_id, 1., shell_hit=False)
        self.assertIn(regular.entity_id, arena.respawn_at)
        self.assertEqual(arena.big_boss.tickets, [30, 14])
        data = event_messages(arena, arena.players[1], arena.events)
        self.assertIn(team_points_message(1, 30), data)
        self.assertIn(team_points_message(2, 14), data)
        self.assertNotIn(boss_lives_message(2), data)
        arena._damage_health(player, player.max_health, None, 1., shell_hit=False)
        lives_event, = (event for event in arena.events
                        if event.kind == 'boss_lives' and event.actor == player.entity_id)
        self.assertEqual(event_messages(arena, arena.players[1], (lives_event,)), b'\x13\xb1\x01')
        for _ in range(14):
            regular.health = regular.max_health
            arena._damage_health(regular, regular.max_health, player.entity_id, 1., shell_hit=False)
        arena._fixed_step(2.)
        self.assertEqual((arena.winner, arena.finish_reason), (1, 'boss_score'))

    def test_endless_big_boss_has_no_ticket_limit(self):
        from solbreaker.battle import initial_messages, spawn_message_parts, team_points_message

        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='One', team=1), dict(id=2, nickname='Two', team=2)],
                      b'', terrain, now=1., solo=False, room_mode='big_boss',
                      room_settings=RoomBattleSettings(minutes=0))
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        arena.ready(2, 1.)
        player = arena.players[1]
        attacker, boss = arena.vehicle_for(1), arena.vehicle_for(2)
        for _ in range(7):
            arena._damage_health(boss, boss.health, attacker.entity_id, 1., shell_hit=False)
            self.assertIn(boss.entity_id, arena.respawn_at)
            arena.respawn_at.pop(boss.entity_id)
            boss.health = boss.max_health
        arena._fixed_step(2.)
        self.assertFalse(arena.finished_at)
        self.assertFalse(arena.big_boss.tickets)
        self.assertFalse(any(event.kind == 'boss_score' for event in arena.events))
        self.assertNotIn(pb_int(10, 30) + pb_int(11, 30), initial_messages(arena, player))
        for team in (1, 2):
            score = team_points_message(team, 30)
            self.assertNotIn(score, spawn_message_parts(arena, player))
            self.assertNotIn(score, reconnect_message_parts(arena, player))
        arena.leave(1, now=3.)
        self.assertFalse(arena.finished_at)
        arena.leave(2, now=4.)
        self.assertEqual(arena.finished_at, 4.)

    def test_big_boss_cards_and_performance_match_the_active_role(self):
        from solbreaker import big_boss
        from solbreaker.battle import initial_messages
        from solbreaker.big_boss import BLAST_WAVE, BOSS_BOOST, MEGASHOT
        from solbreaker.vehicle_data import Shell

        terrain = SimpleNamespace(height=lambda x, z: 0.)
        accounts = [dict(id=i, nickname=str(i), team=team, ammo=1)
                    for i, team in enumerate((1, 1, 2), 1)]
        shell = Shell(1, 76., 200., (100., 80.), None, None, 500., 0., 500.)
        arena = Arena(accounts, b'', terrain, now=1., solo=False, room_mode='big_boss', shell=shell)
        self.addCleanup(arena.close)
        cards = b'\x03' + struct.pack('<IBIBIB', BOSS_BOOST, 0, BLAST_WAVE.compact_id, 0,
                                      MEGASHOT.compact_id, 0)
        for account in accounts:
            arena.ready(account['id'], 1.)
        for player in arena.players.values():
            vehicle = arena.vehicles[player.vehicle_id]
            boss = vehicle.entity_id in arena.big_boss.bosses
            self.assertEqual(cards in initial_messages(arena, player), boss)
            arena._consumables(vehicle, 1.)
            mobility = 1.3 if boss else arena.big_boss.mobility(vehicle.entity_id)
            self.assertEqual(arena._drive_speed_factor(vehicle), mobility)
            self.assertEqual(vehicle.engine_factor, mobility)
            arena._fire_vehicle(vehicle, 1.)
            catalyst = arena.big_boss.roles.get(vehicle.entity_id) == big_boss.CATALYST
            self.assertAlmostEqual(vehicle.reload_at - 1., 3. if boss else 5.7 if catalyst else 6.)
            self.assertAlmostEqual(arena.projectiles[arena.next_shot].shell.damage,
                                   280. if boss else 230. if catalyst else 200.)
        self.assertEqual(shell.damage, 200.)

    def test_training_boss_picker_allows_duplicate_roles_and_changes_on_respawn(self):
        from solbreaker import big_boss

        terrain = SimpleNamespace(height=lambda x, z: 0.)
        accounts = [dict(id=i, nickname=str(i), team=1 if i <= 7 else 2) for i in range(1, 15)]
        arena = Arena(accounts, b'', terrain, now=1., solo=False, room_mode='big_boss',
                      room_settings=RoomBattleSettings(), prebattle_seconds=10.)
        self.addCleanup(arena.close)
        token = sandbox.token(arena)
        for account in accounts:
            arena.ready(account['id'], 1.)
        for account in accounts:
            arena.sandbox_control(account['id'], sandbox.Control(token, 1, sandbox.BOSS_PICK_ROLE, 1, 0, 0, 0))
        self.assertEqual(arena.big_boss.bosses, set(arena.vehicles))
        self.assertEqual(sum(e.kind == 'boss_picker' and e.data['state'] == 2
                             for e in arena.events), len(accounts))
        arena._start_if_ready(2.)
        self.assertEqual(sum(e.kind == 'boss_picker' and e.data['state'] == 2
                             for e in arena.events), len(accounts))
        arena._start_if_ready(11.)
        player = arena.players[1]
        tank = arena.vehicle_for(1)
        arena.sandbox_control(1, sandbox.Control(token, 2, sandbox.BOSS_PICK_ROLE, 2, 0, 0, 0))
        self.assertEqual(arena.big_boss.selected[tank.entity_id], big_boss.BOSS)

        before = tuple(arena.big_boss.tickets)
        arena.world = Mock()
        try:
            arena.sandbox_control(1, sandbox.Control(token, 3, sandbox.BOSS_RESPAWN, 0, 0, 0, 0))
        finally:
            arena.world = None
        self.assertFalse(tank.alive)
        self.assertEqual(tuple(arena.big_boss.tickets), before)
        self.assertEqual(arena.big_boss.lives[tank.entity_id], big_boss.LIVES)
        # Role index two plus the fifth explicit level block.
        packet = report(token, 4, sandbox.BOSS_PICK_ROLE, 27)
        arena.sandbox_control(1, sandbox.Control.decode(packet[len(sandbox.PREFIX):-1]))
        self.assertEqual(arena.boss_pending_roles, {player.vehicle_id: (big_boss.GRAVITON, 4)})
        arena._pin_boss_role(tank, *arena.boss_pending_roles.pop(player.vehicle_id))
        arena.big_boss.assign_role(player.vehicle_id)
        self.assertEqual(arena.big_boss.roles[player.vehicle_id], big_boss.GRAVITON)
        self.assertNotIn(player.vehicle_id, arena.big_boss.bosses)
        self.assertEqual(arena.big_boss.levels[player.vehicle_id], 4)
        self.assertEqual(tank.consumables[-1].spec, big_boss.ROLE_ACTIVE[big_boss.GRAVITON][4])
        for invalid in (0, 6, 31):
            with self.assertRaises(ValueError):
                sandbox.decode_boss_pick(invalid)

    def test_megashot_charges_once_per_physical_round_and_bypasses_armor(self):
        from solbreaker.armor import ArmorHit, resolve_shell
        from solbreaker.big_boss import MEGASHOT, MEGASHOT_EFFECT
        from solbreaker.consumables import READY
        from solbreaker.vehicle_data import Shell

        terrain = SimpleNamespace(height=lambda x, z: 0.)
        shell = Shell(1, 76., 200., (100., 80.), 0., math.radians(70), 500., 9.81, 500.,
                      damage_factors=(1., 1.))
        arena = Arena([dict(id=1, nickname='Boss', ammo=10), dict(id=2, nickname='Enemy')],
                      b'', terrain, now=1., solo=False, room_mode='big_boss', shell=shell)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        arena.ready(2, 1.)
        boss = arena.vehicle_for(1)
        charge = next(s for s in boss.consumables if s and s.spec is MEGASHOT)
        self.assertEqual(charge.state_at(1.).remaining, 30)
        self.assertEqual(charge.state_at(31.).state, READY)
        self.assertFalse(arena.use_consumable(1, MEGASHOT.ident, now=1.))

        arena._fire_vehicle(boss, 30.)
        self.assertFalse(arena.projectiles[arena.next_shot].shell.railgun)
        boss.reload_at = 0.
        arena._fire_vehicle(boss, 31.)
        charged = arena.projectiles[arena.next_shot].shell
        self.assertTrue(charged.railgun)
        self.assertEqual((charged.damage, charged.speed, charged.gravity), (364., 1000., 0.))
        shot = next(event for event in reversed(arena.events) if event.kind == 'shot')
        self.assertEqual(shot.data['effect_index'], MEGASHOT_EFFECT)
        self.assertEqual(charge.ready_at, 71.)
        boss.reload_at = 0.
        arena._fire_vehicle(boss, 32.)
        self.assertFalse(arena.projectiles[arena.next_shot].shell.railgun)

        plate = ArmorHit(10., (0., 0., 10.), (0., 0., -1.), 'hull', 0, 1000., 1.)
        self.assertEqual(resolve_shell([plate], (0., 0., 1.), shell).outcome, 'not_penetrated')
        self.assertEqual(resolve_shell([plate], (0., 0., 1.), charged).damage, 364)

    def test_bomber_cover_death_blast_and_role_level_reset(self):
        from solbreaker import big_boss
        from solbreaker.consumables import READY
        terrain = SimpleNamespace(height=lambda x,z: 0.)
        accounts = [dict(id=i, nickname=str(i), team=team)
                    for i, team in enumerate((1, 1, 2, 2), 1)]
        arena = Arena(accounts, b'', terrain, now=1., solo=False, room_mode='big_boss')
        self.addCleanup(arena.close)
        for account in accounts:
            arena.ready(account['id'], 1.)
        bomber = next(v for v in arena.vehicles.values() if v.team == 1 and not arena._is_boss(v))
        enemy, covered = (v for v in arena.vehicles.values() if v.team == 2)
        for v, x in ((bomber, 0.), (enemy, 6.), (covered, 12.)):
            v.pose = Pose((x, 0., 0.), 0.)
        for v in (enemy, covered):
            v.health = v.max_health = 3000
            if not arena._is_boss(v):
                arena.big_boss.roles[v.entity_id] = big_boss.BOMBER
                arena._set_boss_role_ability(v)
        arena.big_boss.roles[bomber.entity_id] = big_boss.BOMBER
        arena._set_boss_role_ability(bomber)
        world = arena.world = Mock()
        world.advance_close.return_value = True
        world.static_segment.side_effect = lambda _start, end: (99 if end[0] > 10 else None, 1., end)
        state = bomber.consumables[-1]
        stale_graviton_id = big_boss.ROLE_ACTIVE[big_boss.GRAVITON][0].ident
        self.assertTrue(arena.use_consumable(bomber.account_id, stale_graviton_id, bytes(36), now=1.))
        self.assertEqual(enemy.health, 3000 - round(bomber.health * .20))
        self.assertEqual(covered.health, 3000)
        world.add_flight_velocity.assert_not_called()
        self.assertEqual(arena.equipment_state(bomber), ())
        ability_events = (event for event in arena.events
                          if event.kind in ('bomber_blast', 'consumable_state'))
        self.assertTrue(event_messages(arena, arena.players[bomber.account_id], ability_events))

        arena._damage_health(enemy, bomber.max_health, bomber.entity_id, 1., shell_hit=False)
        self.assertEqual(arena.big_boss.levels[bomber.entity_id], 1)
        self.assertIsNot(bomber.consumables[-1], state)
        self.assertEqual(bomber.consumables[-1].state_at(1.).state, READY)
        hp = enemy.health
        arena._damage_health(bomber, bomber.health, enemy.entity_id, 1., shell_hit=False)
        self.assertEqual(enemy.health, hp - round(bomber.max_health * .30))
        self.assertEqual(covered.health, 3000)
        blasts = sum(event.kind == 'bomber_blast' for event in arena.events)
        bomber.health = bomber.max_health
        arena._damage_health(bomber, bomber.health, enemy.entity_id, 2., shell_hit=False)
        self.assertEqual(sum(event.kind == 'bomber_blast' for event in arena.events), blasts)

    def test_gravitrap_stacks_without_pulling_allies_and_caps_speed(self):
        from solbreaker import big_boss
        state = big_boss.BigBoss({}, 'test')
        trap = big_boss.GravityTrap(1, 200, 1, (10., 0., 0.), 4, 7.)
        state.traps.append(trap)
        self.assertIsNone(state.pull(1, (0., 0., 0.), 25000.))
        self.assertIsNone(state.pull(2, (-70., 0., 0.), 25000.))
        self.assertEqual(state.pull(2, (-69., 0., 0.), 25000.), (1., 0.))
        self.assertEqual(state.pull(2, (0., 0., 0.), 25000.), (10., 0.))
        state.traps.extend([trap] * 3)
        self.assertEqual(state.pull(2, (0., 0., 0.), 25000.), (big_boss.TRAP_MAX_SPEED, 0.))

    def test_regenerator_zone_and_healing_shot_clear_poison_and_grant_shield(self):
        from solbreaker import big_boss
        from solbreaker.vehicle_data import Shell

        accounts = [dict(id=i, nickname=str(i), team=team)
                    for i, team in enumerate((1, 1, 2, 2), 1)]
        arena = Arena(accounts, b'', SimpleNamespace(height=lambda x, z: 0.),
                      now=1., solo=False, room_mode='big_boss')
        self.addCleanup(arena.close)
        boss = arena.big_boss
        healer = next(v for v in arena.vehicles.values() if v.team == 1 and not arena._is_boss(v))
        ally = next(v for v in arena.vehicles.values() if v.team == 1 and v is not healer)
        enemy = next(v for v in arena.vehicles.values() if v.team == 2)
        for v in (healer, ally, enemy):
            v.pose = Pose((0., 0., 0.), 0.)
            v.health, v.max_health = 500, 1000
        boss.roles[healer.entity_id] = big_boss.REGENERATOR
        arena._set_boss_role_ability(healer)
        arena.world = Mock()
        arena.world.advance_close.return_value = True
        self.assertTrue(arena._activate_boss_role_ability(healer, healer.consumables[-2], 1.))
        self.assertIn(big_boss.HEAL_AURA_MODIFIERS[0], arena.state_modifiers(healer))
        arena._boss_poison(enemy, ally, 0, 2.)
        arena._advance_boss_roles(4.)
        self.assertEqual((healer.health, ally.health, enemy.health), (520, 540, 500))
        healed, = (e for e in arena.events if e.kind == 'health' and e.target == ally.entity_id)
        payload = b'TFS1HEAL' + struct.pack('<I', 40)
        self.assertIn(payload, event_messages(arena, arena.players[healer.account_id], (healed,)))
        self.assertIn(payload, event_messages(arena, arena.players[ally.account_id], (healed,)))
        self.assertNotIn(payload, event_messages(arena, arena.players[enemy.account_id], (healed,)))
        from solbreaker.battle import health_changed
        marker = health_changed(ally.entity_id, 540, healer.entity_id, healing_marker=True)
        self.assertIn(marker, event_messages(arena, arena.players[healer.account_id], (healed,)))
        self.assertNotIn(marker, event_messages(arena, arena.players[ally.account_id], (healed,)))
        self.assertNotIn(marker, event_messages(arena, arena.players[enemy.account_id], (healed,)))
        self.assertNotIn(ally.entity_id, boss.poison)
        arena._boss_poison(enemy, ally, 0, 5.)
        shell = Shell(1, 76., 200., (100., 80.), None, None, 500., 0., 500., healing_level=0)
        arena._boss_healing_shot(healer, ally, shell, 5.)
        self.assertGreater(ally.health, 540)
        self.assertNotIn(ally.entity_id, boss.poison)
        self.assertTrue(arena._consume_boss_shield(ally, 6.))
        self.assertFalse(arena._consume_boss_shield(ally, 6.))
        arena._advance_boss_roles(7.)
        self.assertNotIn(big_boss.HEAL_AURA_MODIFIERS[0], arena.state_modifiers(healer))

    def test_catalyst_active_extra_and_role_progress_wire(self):
        from solbreaker import big_boss

        accounts = [dict(id=i, nickname=str(i), team=team)
                    for i, team in enumerate((1, 1, 2, 2), 1)]
        arena = Arena(accounts, b'', SimpleNamespace(height=lambda x, z: 0.),
                      now=1., solo=False, room_mode='big_boss')
        self.addCleanup(arena.close)
        catalyst = next(v for v in arena.vehicles.values() if v.team == 1 and not arena._is_boss(v))
        enemy = next(v for v in arena.vehicles.values() if v.team == 2)
        arena.big_boss.roles[catalyst.entity_id] = big_boss.CATALYST
        arena._set_boss_role_ability(catalyst)
        arena.world = Mock()
        arena.world.advance_close.return_value = True
        active = catalyst.consumables[-1]
        self.assertTrue(arena._activate_boss_role_ability(catalyst, active, 1.))
        self.assertEqual(arena.state_modifiers(catalyst), (123,))

        damage = catalyst.max_health // 2
        enemy.health = damage + 1
        arena._damage_health(enemy, damage, catalyst.entity_id, 2., shell_hit=False)
        progress, = (event for event in arena.events if event.kind == 'boss_role_progress')
        expected = (pb_int(1, catalyst.entity_id) + pb_int(2, 0x14)
                    + pb_int(3, damage) + pb_int(4, catalyst.max_health))
        self.assertEqual(event_messages(arena, arena.players[catalyst.account_id], (progress,)),
                         arena_update(24, pb_blob(21, expected)))
        arena._consumables(catalyst, active.active_until)
        self.assertEqual(arena.state_modifiers(catalyst), ())
        self.assertTrue(any(event.kind == 'state_modifiers' and event.actor == catalyst.entity_id
                            and event.data['modifiers'] == () for event in arena.events))

    def test_catalyst_poison_refreshes_per_source_and_stacks_across_sources(self):
        from solbreaker import big_boss

        accounts = [dict(id=i, nickname=str(i), team=team)
                    for i, team in enumerate((1, 1, 2), 1)]
        arena = Arena(accounts, b'', SimpleNamespace(height=lambda x, z: 0.),
                      now=1., solo=False, room_mode='big_boss')
        self.addCleanup(arena.close)
        one, two, target = [arena.vehicle_for(i) for i in (1, 2, 3)]
        target.health = target.max_health = 1000
        arena._boss_poison(one, target, 0, 1.)
        arena._boss_poison(one, target, 4, 2.)
        arena._boss_poison(two, target, 4, 2.)
        self.assertEqual(len(arena.big_boss.vehicle_effects(target.entity_id)), 1)
        for now in range(3, 9):
            arena._advance_boss_roles(float(now))
        self.assertEqual(target.health, 550)
        self.assertFalse(arena.big_boss.vehicle_effects(target.entity_id))
