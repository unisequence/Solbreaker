"""The first Mad Games loadout uses ordinary room, HUD and drive paths."""
from dataclasses import replace
import pickle
import struct
from types import SimpleNamespace
from unittest import TestCase
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from solbreaker import client_battle_settings, dvpl, mad_games, sandbox
from solbreaker.arena import Arena
from solbreaker.armor import ShotResult
from solbreaker.ballistics import Impact
from solbreaker.battle import (SOUND_ENEMY_SIGHTED, SOUND_PLAYER_SIGHTED, active_equipment_message,
                               chameleon_pose, consumable_message, create_avatar, crew_voice_message,
                               event_messages, health_changed, initial_messages, observed_status_message,
                               provision_effect_message, provision_messages, reconnect_message_parts,
                               revenge_targets_message, spawn_message_parts, vehicle_client_effects_message,
                               world_motion_messages)
from solbreaker.motion import vehicle_snapshot
from solbreaker.battle_settings import TITLE, decode, packed_recipe
from solbreaker.consumables import ACTIVE, COOLDOWN, READY, ConsumableSpec, ConsumableState
from solbreaker.native_rooms import room_options
from solbreaker.modules import TrackSpec, TrackState
from solbreaker.mercury import read_blob
from solbreaker.training_config import TrainingConfig


class MadGamesTests(TestCase):
    def arena(self, mode):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='Pilot'), dict(id=2, nickname='Peer')],
                      b'', terrain, now=1., solo=False, room_mode=mode)
        self.addCleanup(arena.close)
        return arena

    def test_room_and_solo_share_the_same_mode(self):
        config = decode(TITLE, 'SolbreakerBots/2/0/1/1/5/0/-1/0', 0, {})
        self.assertEqual(config.battle_mode, mad_games.MODE)
        self.assertEqual(packed_recipe(config, {}) // 1000, 5)
        self.assertEqual(room_options('[mode mad_games] [bots 1:2] Notes')[:3],
                         ('Notes', (1, 2), mad_games.MODE))
        self.assertEqual(config, TrainingConfig.from_dict(config.to_dict()))
        self.assertEqual(client_battle_settings.MODE_ART[5], 'madgames')

    def test_mad_games_owns_spotting_with_server_recon_enabled(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        accounts = [dict(id=1, nickname='Pilot'), dict(id=2, nickname='Peer')]
        for solo, mode in ((False, 'encounter'), (False, mad_games.MODE), (True, mad_games.MODE)):
            with self.subTest(solo=solo, mode=mode):
                training = TrainingConfig(mode='bots', battle_mode=mode) if solo else None
                arena = Arena(accounts[:1] if solo else accounts, b'', terrain, now=1.,
                              solo=solo, room_mode=mode, training=training, recon_enabled=True)
                self.addCleanup(arena.close)
                self.assertEqual(arena.recon is None, mode == mad_games.MODE)

    def test_spotted_markers_follow_the_team_in_both_battle_paths(self):
        for mode in ('encounter', mad_games.MODE, 'uprising'):
            with self.subTest(mode=mode):
                self.check_team_spotting_transport(mode)

    def test_bot_boss_remains_spotted_outside_range(self):
        from uuid import UUID
        from solbreaker.recon_snapshot import build_team_spotting_snapshot

        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='Pilot')], b'', terrain, now=1.,
                      training=TrainingConfig(mode='bots', allies=0, enemies=2, battle_mode='big_boss'))
        self.addCleanup(arena.close)
        observer = arena.vehicle_for(1)
        boss, ordinary = sorted((v for v in arena.vehicles.values() if v.team != observer.team),
                                key=lambda v: not arena._is_boss(v))
        for vehicle in boss, ordinary:
            vehicle.pose = replace(vehicle.pose, position=(2000., 0., 2000.))
        for now in (2., 40.):
            arena._update_team_spotting(now)
            self.assertFalse(mad_games.concealed(boss, observer.team))
            self.assertTrue(mad_games.concealed(ordinary, observer.team))
            snapshot = build_team_spotting_snapshot(
                arena, observer.entity_id, battle_id=UUID(arena.result_id), sequence=int(now))
            self.assertEqual(snapshot.markers, (boss.entity_id,))

    def check_team_spotting_transport(self, mode):
        from solbreaker.arena_owner_game import GameArenaHandler
        from solbreaker.base import ClientSession
        from solbreaker.arena_owner import Command
        from solbreaker.recon_transport import AIM_PREFIX, HELLO_PREFIX, ReconTransport
        from solbreaker.recon_wire import decode as decode_snapshot

        arena = self.arena(mode)
        observer, target = arena.vehicle_for(1), arena.vehicle_for(2)
        arena.started = 1.
        arena.players[1].loaded = True
        addr = ('127.0.0.1', 20000)
        client = ClientSession(b'', 1, account_created=True, account_id=1,
                               arena_id=arena.result_id, phase='battle', battle_spawned=True)
        endpoint = SimpleNamespace(send=Mock(), clients={addr: client},
                                   arenas={arena.result_id: arena})
        bridge = ReconTransport(endpoint, clock=lambda: 2.)
        hello = HELLO_PREFIX + struct.pack('<IB', 7, 1)
        self.assertTrue(bridge.receive(addr, client, hello, 2.))
        self.assertTrue(endpoint.send.called)
        delivery = bridge._deliveries[id(client)]
        aim = AIM_PREFIX + struct.pack('<IIIB', 7, 0, target.entity_id, 1)
        self.assertTrue(bridge.receive(addr, client, aim, 2.))
        self.assertFalse(arena._recon_client_aims)

        handler = GameArenaHandler(arena, None)
        for now, is_spotted, lamp in ((2., False, False), (3., True, False),
                                      (6., True, True), (7., False, False)):
            arena._set_mad_spotted(target, is_spotted, now)
            arena._set_mad_spotted(observer, is_spotted, now)
            local = decode_snapshot(bridge.bindings.snapshot(delivery.token))
            command = Command('recon_snapshot', 2., 1, struct.pack('<I', local.sequence))
            self.assertEqual(local, decode_snapshot(handler.apply(command).recon))
            self.assertEqual(local.markers, (target.entity_id,) if is_spotted else ())
            self.assertEqual(local.lamp, lamp)
            body = event_messages(arena, arena.players[1], arena.drain_events())
            self.assertEqual(crew_voice_message(SOUND_ENEMY_SIGHTED) in body, now == 3.)
            self.assertEqual(crew_voice_message(SOUND_PLAYER_SIGHTED) in body, now == 6.)

    def test_mad_games_selects_the_native_arcade_gameplay(self):
        arena = self.arena(mad_games.MODE)
        player = arena.players[1]
        arena.base_capture = object()
        message = initial_messages(arena, player)
        avatar = create_avatar({'nickname': player.nickname}, player.avatar_id, arena.unique_id,
                               arena.client_map_id, game_mode=8)
        self.assertTrue(message.startswith(avatar))
        self.assertTrue(message.endswith(
            b'\x8a\x01\x1c\x2a\x06arcade\x3a\x06arcade\x42\x06arcade\x48\x00\x60\x01'))

    def test_full_loadout_sends_passive_after_active_ability(self):
        arena = self.arena(mad_games.MODE)
        player, other = arena.players[1], arena.players[2]
        tank = arena.vehicle_for(1)
        tank.definition = SimpleNamespace(provisions=(0, 19, 11), aiming=None, weapon=None)
        normal = b''.join(provision_messages(tank))
        ability = tank.consumables[3].spec
        active = consumable_message(ability.compact_id, 1)
        for passive, ident in ((mad_games.REGENERATOR, 23), (mad_games.RAMMER, 24),
                               (mad_games.COORDINATOR, 25), (mad_games.SABOTEUR, 26),
                               (mad_games.TOXIC_STARE, 114)):
            with self.subTest(passive=passive):
                tank.mad_passive = passive
                packet = b'\x13\x4c' + struct.pack('<BIHBhh', 13, (ident << 8) | 0xfd, 1, 1, 0, 0)
                arena.emit('ready', tank.entity_id)
                events = arena.drain_events()
                ready = event_messages(arena, player, events)
                reconnect = b''.join(reconnect_message_parts(arena, player))
                for hud in (ready, reconnect):
                    self.assertIn(normal, hud)
                    self.assertIn(active + packet, hud)
                    self.assertEqual(hud.count(packet), 1)
                self.assertNotIn(packet, event_messages(arena, other, events))
                arena.emit('ready', tank.entity_id, respawn=True)
                self.assertIn(active + packet, event_messages(arena, player, arena.drain_events()))

    def test_bot_activates_discharge_before_a_confirmed_shot(self):
        arena = self.arena(mad_games.MODE)
        bot, target = arena.vehicle_for(1), arena.vehicle_for(2)
        target.mad_unspotted = False
        arena.started = 1.
        arena.simulation_time = 2.
        bot.ammo = 1
        with patch('solbreaker.mad_games.vehicle_abilities', return_value=('sacrifice',)):
            mad_games.install_ability(bot, 'heavyTank')
        ai = Mock()
        ai.sight_confirmed.side_effect = lambda entity, hit, now: hit
        state = bot.consumables[3]
        first_health = bot.health
        with (patch.object(arena, 'world', Mock()),
              patch.object(arena, 'aim_hit', return_value=(0, None)) as aim,
              patch.object(arena, '_fire_vehicle') as fire):
            arena._training_bot_fire(bot, target, 2., ai)
            self.assertEqual(state.uses, 0)
            aim.return_value = (target.entity_id, target.pose.position)
            fire.side_effect = lambda *_: self.assertEqual(state.state_at(2.).state, ACTIVE)
            arena._training_bot_fire(bot, target, 2., ai)
        self.assertEqual(state.uses, 1)
        self.assertEqual(bot.health, first_health - round(bot.max_health * .10))
        fire.assert_called_once_with(bot, 2.)

    def test_bot_uses_fuel_on_a_straight_approach(self):
        arena = self.arena(mad_games.MODE)
        bot = arena.vehicle_for(1)
        arena.started = 1.
        arena.simulation_time = 2.
        state = bot.consumables[3]
        bot.speed = 5.
        bot.flags = 1 | 4
        arena._training_bot_mad_mobility(bot, None, 2.)
        self.assertEqual(state.uses, 0)
        bot.flags = 1
        arena._training_bot_mad_mobility(bot, None, 2.)
        self.assertEqual(state.state_at(2.).state, ACTIVE)
        self.assertEqual(bot.engine_factor, 6.)

    def test_vehicle_loadout_reads_pinned_arcade_role(self):
        with TemporaryDirectory() as temporary:
            game = Path(temporary)
            vehicles = game/'Data/XML/item_defs/vehicles'
            (vehicles/'common').mkdir(parents=True)
            (vehicles/'usa').mkdir()
            (vehicles/'common/combat_roles.yaml.dvpl').write_bytes(
                dvpl.encode(b'long_support: {default_abilities: [lifesteal_heal, afterburner_ability]}'))
            (vehicles/'usa/list.xml.dvpl').write_bytes(dvpl.encode(
                b'<list><Tank><combatRole><hellgames>unknown</hellgames>'
                b'<arcade>long_support</arcade></combatRole></Tank></list>'))
            vehicle = SimpleNamespace(definition=SimpleNamespace(nation='usa', name='Tank', tier=5),
                                      consumables=())
            mad_games.install_ability(vehicle, 'lightTank', game)
            self.assertIs(vehicle.consumables[3].spec, mad_games.HIGH_OCTANE)
            self.assertEqual(vehicle.mad_passive, mad_games.REGENERATOR)
            vehicle.definition.tier = 4
            mad_games.install_ability(vehicle, 'lightTank', game)
            self.assertEqual(vehicle.consumables, (None, None, None))
            self.assertIsNone(vehicle.mad_passive)

    def test_fuel_uses_the_battle_consumable_clock(self):
        arena = self.arena(mad_games.MODE)
        owner = arena.vehicle_for(1)
        peer = arena.vehicle_for(2)
        state = owner.consumables[3]
        self.assertIs(state.spec, mad_games.HIGH_OCTANE)
        self.assertIsNot(state, peer.consumables[3])
        arena.started = 1.
        arena.simulation_time = 2.
        self.assertTrue(arena._activate_consumable(owner, state, 2.))
        self.assertEqual((owner.engine_factor, owner.rotation_factor), (6., 1.60))
        self.assertEqual(arena._drive_speed_factor(owner), 3.)
        self.assertEqual(arena._terrain_mobility(owner).resistance, .4)
        self.assertEqual(arena.equipment_state(owner)[0][:2], (24, ACTIVE))
        # Opponents receive the native state ID, while the owner uses raw24.
        self.assertIn(struct.pack('<BBdf', 55, ACTIVE, 2.-arena.epoch, 15.),
                      active_equipment_message(arena, owner))
        self.assertFalse(arena._activate_consumable(owner, state, 3.))
        arena.simulation_time = 17.
        arena._consumables(owner, 17.)
        self.assertEqual((owner.engine_factor, owner.rotation_factor), (1., 1.))
        self.assertEqual(arena._drive_speed_factor(owner), 1.)
        self.assertEqual(arena.equipment_state(owner)[0][:2], (24, COOLDOWN))
        self.assertTrue(arena._activate_consumable(owner, state, 32.))

    def test_timed_boost_clears_the_public_indicator_even_when_already_ready(self):
        arena = self.arena(mad_games.MODE)
        player = arena.players[1]
        tank = arena.vehicle_for(1)
        regular = ConsumableSpec('afterburning', 14, (14 << 8) | 0xfb, 5., 1.)
        regular_state = ConsumableState(regular)
        fuel_state = tank.consumables[3]
        tank.consumables = (regular_state, fuel_state)
        arena.started = 1.
        self.assertTrue(arena._activate_consumable(tank, regular_state, 2.))
        self.assertTrue(arena._activate_consumable(tank, fuel_state, 2.))
        arena.simulation_time = 7.
        arena._consumables(tank, 7.)
        ended = next(event for event in reversed(arena.events)
                     if event.kind == 'consumable_state' and event.data['ident'] == 14)
        self.assertEqual(ended.data['state'], READY)
        stop = active_equipment_message(arena, tank, ((14, READY, 2., 5.),))
        fuel = active_equipment_message(arena, tank)
        self.assertIn(struct.pack('<BBdf', 10, COOLDOWN, 2.-arena.epoch, 0.), stop)
        self.assertIn(struct.pack('<BBdf', 55, ACTIVE, 2.-arena.epoch, 15.), fuel)
        self.assertTrue(event_messages(arena, player, (ended,)).endswith(stop + fuel))

    def test_high_octane_protects_existing_track_health_during_the_active_window(self):
        arena = self.arena(mad_games.MODE)
        vehicle, enemy = arena.vehicle_for(1), arena.vehicle_for(2)
        arena.started = 1.
        arena.simulation_time = 2.
        part = 'chassis_track_L'
        track = vehicle.tracks[part] = TrackState(TrackSpec(150., 50., 50., 1.))
        track.damage(20., 1.)
        state = vehicle.consumables[3]
        self.assertTrue(arena._activate_consumable(vehicle, state, 2.))
        self.assertEqual(track.health, 130.)

        contact = SimpleNamespace(target=vehicle.entity_id, shooter=enemy.entity_id,
                                  penetrated=True, hit=SimpleNamespace(part=part),
                                  shell=SimpleNamespace(damage_devices=150.), shot=1, at=3.)
        self.assertTrue(arena._track_contact(contact, 3.))
        self.assertEqual(track.health, 120.)
        arena._track_fall_impacts([dict(entity=vehicle.entity_id, track=part, speed=10., height=7.)], 4.)
        self.assertEqual(track.health, 110.)
        self.assertFalse(track.broken)

        contact.at = 17.
        self.assertTrue(arena._track_contact(contact, 17.))
        self.assertTrue(track.broken)

    def dash_arena(self):
        arena = self.arena(mad_games.MODE)
        vehicle = arena.vehicle_for(1)
        arena.ready(1, 1.)
        arena.started = 1.
        arena.simulation_time = 2.
        mad_games.install_ability(vehicle, 'lightTank')
        vehicle.pose = replace(vehicle.pose, position=(0., 0., 0.), yaw=0.)
        motion = SimpleNamespace(grounded=True, velocity=(0., 0., 0.))

        def move(distance, yaw_delta, *, entity, dt):
            self.assertEqual((yaw_delta, entity), (0., vehicle.entity_id))
            x, y, z = vehicle.pose.position
            motion.velocity = (0., 0., distance/dt)
            return replace(vehicle.pose, position=(x, y, z+distance)), True

        arena.world = Mock(spec=['motion', 'poses', 'ground_motion', 'assets_for', 'move', 'close',
                                 'advance_close'],
                           motion={vehicle.entity_id: motion}, poses={vehicle.entity_id: vehicle.pose},
                           ground_motion={})
        arena.world.assets_for.return_value = None
        arena.world.move.side_effect = move
        return arena, vehicle, vehicle.consumables[3]

    def test_accelerator_adds_one_impulse_and_coasts_after_the_effect(self):
        for flags, direction in ((0, 1), (2, -1)):
            with self.subTest(flags=flags):
                arena, vehicle, state = self.dash_arena()
                vehicle.flags, vehicle.last_input = flags, 2.
                vehicle.speed = 5. * direction
                arena.world.motion[vehicle.entity_id].velocity = (0., 0., vehicle.speed)
                self.assertIs(state.spec, mad_games.ACCELERATOR)
                self.assertTrue(arena._activate_mad_dash(vehicle, state, 2.))
                self.assertAlmostEqual(vehicle.speed, (5. + mad_games.DASH_VELOCITY_BOOST) * direction)
                self.assertIn(struct.pack('<BBdf', 49, ACTIVE, 2.-arena.epoch, 1.15),
                              active_equipment_message(arena, vehicle))
                for tick in range(1, 72):
                    previous_speed = abs(vehicle.speed)
                    arena._drive(vehicle, 1./60, 2.+tick/60)
                    self.assertLess(abs(vehicle.speed), previous_speed)
                    self.assertLess(previous_speed - abs(vehicle.speed), 1.)
                self.assertGreater(direction * vehicle.pose.position[2], 30.)
                self.assertEqual(state.state_at(3.16).state, COOLDOWN)
                self.assertGreater(abs(vehicle.speed), 15.)

    def test_accelerator_stops_at_a_blocked_drive_step(self):
        arena, vehicle, state = self.dash_arena()
        arena.world.move.side_effect = None
        arena.world.move.return_value = (vehicle.pose, False)
        self.assertTrue(arena._activate_mad_dash(vehicle, state, 2.))
        self.assertFalse(arena._drive(vehicle, 1./60, 2.+1./60))
        self.assertEqual(vehicle.speed, 0.)
        self.assertEqual(vehicle.pose.position[2], 0.)
        self.assertEqual(state.state_at(2.1).state, ACTIVE)

    def test_accelerator_protects_against_the_ram_that_stops_it(self):
        arena = self.arena(mad_games.MODE)
        source, enemy = arena.vehicle_for(1), arena.vehicle_for(2)
        source.mad_passive = enemy.mad_passive = None
        arena.started = 1.
        with patch('solbreaker.mad_games.vehicle_abilities', return_value=('dash',)):
            mad_games.install_ability(source, 'lightTank')
        state = source.consumables[3]
        self.assertTrue(arena._activate_consumable(source, state, 2.))
        state.stop(2.5)

        energy = 100_000 / arena.RAM_DAMAGE_PER_KILOJOULE
        impact = dict(entity_a=source.entity_id, entity_b=enemy.entity_id, ram=True,
                      ram_energy_a=energy, ram_energy_b=energy)
        arena._ground_impact(impact, 2.5)
        self.assertEqual(source.health, source.max_health - 15)
        self.assertEqual(enemy.health, enemy.max_health - 100)
        arena._ground_impact(impact, 2.6)
        self.assertEqual(source.health, source.max_health - 115)

    def test_only_a_high_energy_lethal_ram_detonates_and_pushes(self):
        for closing_speed, health, detonation in ((-55., 400, True), (-12., 400, False), (-55., 10_000, False)):
            with self.subTest(closing_speed=closing_speed, health=health):
                arena = self.arena(mad_games.MODE)
                source, victim = arena.vehicle_for(1), arena.vehicle_for(2)
                for account in (1, 2):
                    arena.ready(account, 1.)
                source.mad_passive = victim.mad_passive = None
                source.health = source.max_health = 10_000
                victim.health = victim.max_health = health
                source.pose = replace(source.pose, position=(0., 0., 6.))
                victim.pose = replace(victim.pose, position=(0., 0., 0.))
                arena.world = Mock()
                arena.world.add_ground_velocity.return_value = True
                arena.drain_events()
                impact = dict(entity_a=source.entity_id, entity_b=victim.entity_id, ram=True,
                              closing_speed=closing_speed, airborne=True,
                              ram_energy_a=1_000_000., ram_energy_b=9_000_000.)
                arena._ground_impact(impact, 2.)
                events = arena.drain_events()
                kinds = [event.kind for event in events]
                self.assertEqual('ram_detonated' in kinds, detonation)
                self.assertEqual('ram_shockwave' in kinds, detonation)
                if detonation:
                    self.assertEqual(victim.killer, source.entity_id)
                    self.assertLess(kinds.index('ram_detonated'), kinds.index('killed'))
                    self.assertLess(kinds.index('killed'), kinds.index('ram_turret_created'))
                    pushed, vx, vz = arena.world.add_ground_velocity.call_args.args
                    self.assertEqual((pushed, vx), (source.entity_id, 0.))
                    self.assertGreater(vz, 0.)
                    wire = event_messages(arena, arena.players[1], events)
                    self.assertIn(b'\x12' + struct.pack('<IBII', victim.entity_id, 61, source.entity_id, 0), wire)
                    turret = arena.ram_turrets[victim.entity_id]
                    self.assertIn(struct.pack('<IH', turret.entity, 8), wire)
                    self.assertGreater(turret.pose_at(3.)[0][1], turret.origin[1])
                    arena._retire_ram_turret(victim.entity_id)
                    self.assertEqual(arena.drain_events()[-1].data['turret_id'], turret.entity)
                    self.assertNotIn(victim.entity_id, arena.ram_turrets)
                    self.assertIn(victim.entity_id, arena.ram_detached_hulls)
                    arena.emit('ram_turret_restored', victim.entity_id)
                    respawn = arena.drain_events()
                    for account in (1, 2):
                        wire = event_messages(arena, arena.players[account], respawn)
                        self.assertIn(b'TFS1TRRT' + struct.pack('<I', victim.entity_id), wire)
                else:
                    arena.world.add_ground_velocity.assert_not_called()

    def test_accelerator_preserves_momentum_if_the_tank_leaves_the_ground(self):
        arena, vehicle, state = self.dash_arena()
        motion = arena.world.motion[vehicle.entity_id]
        move = arena.world.move.side_effect

        def launch(*args, **kwargs):
            pose, moved = move(*args, **kwargs)
            motion.grounded = False
            return pose, moved

        arena.world.move.side_effect = launch
        self.assertTrue(arena._activate_mad_dash(vehicle, state, 2.))
        self.assertTrue(arena._drive(vehicle, 1./60, 2.+1./60))
        self.assertGreater(vehicle.speed, mad_games.DASH_VELOCITY_BOOST - 1.)
        self.assertTrue(any(event.kind == 'airborne' for event in arena.drain_events()))

    def test_accelerator_rejects_critical_tracks_without_consuming_cooldown(self):
        arena, vehicle, state = self.dash_arena()
        track = vehicle.tracks['chassis_track_L'] = TrackState(TrackSpec(150., 50., 50., 1.))
        track.damage(20., 2.)
        self.assertTrue(track.critical)
        self.assertFalse(track.broken)
        self.assertFalse(arena._activate_mad_dash(vehicle, state, 2.))
        self.assertEqual(state.uses, 0)
        track.damage(150., 2.)
        self.assertTrue(track.broken)
        self.assertFalse(arena._activate_mad_dash(vehicle, state, 2.))
        self.assertEqual(state.uses, 0)

    def test_chameleon_hides_only_from_enemies_and_reveals_on_damage(self):
        arena = self.arena(mad_games.MODE)
        source, enemy = arena.vehicle_for(1), arena.vehicle_for(2)
        source.pose = replace(source.pose, position=(123., 12., 101.))
        enemy.pose = replace(enemy.pose, position=(123., 12., 151.))
        arena.ready(1, 1.)
        arena.ready(2, 1.)
        arena.started = 1.
        arena.simulation_time = 2.
        with patch('solbreaker.mad_games.vehicle_abilities', return_value=('moonwalk', 'ramming_damage')):
            mad_games.install_ability(source, 'lightTank')
        arena._set_mad_spotted(source, True)
        enemy.autoaim_target = source.entity_id
        state = source.consumables[-1]
        self.assertIs(state.spec, mad_games.CHAMELEON)
        self.assertTrue(arena._activate_consumable(source, state, 2.))
        self.assertEqual(enemy.autoaim_target, 0)
        self.assertFalse(arena.autoaim(2, source.entity_id))
        self.assertEqual((source.engine_factor, arena._drive_speed_factor(source)), (4., 1.4))

        pose = source.pose
        snapshot = vehicle_snapshot(source.entity_id, pose.position, pose.yaw, pose.pitch, pose.roll)
        self.assertIn(snapshot, world_motion_messages(arena, team=source.team))
        self.assertNotIn(snapshot, world_motion_messages(arena, team=enemy.team))
        parked = chameleon_pose(pose)
        forced = b'\x14' + struct.pack('<III6f', source.entity_id, arena.space_id, 0,
                                        *parked.position, parked.roll, parked.pitch, parked.yaw)
        events = arena.drain_events()
        hostile_view = event_messages(arena, arena.players[2], events)
        friendly_view = event_messages(arena, arena.players[1], events)
        activate_fx = provision_effect_message(source.entity_id, mad_games.CHAMELEON.compact_id, argument=1)
        self.assertIn(activate_fx, friendly_view)
        self.assertIn(activate_fx, hostile_view)
        self.assertLess(hostile_view.index(activate_fx), hostile_view.index(forced))
        self.assertNotIn(forced, friendly_view)
        joined = b''.join(spawn_message_parts(arena, arena.players[2]))
        self.assertIn(struct.pack('<3f', *parked.position), joined)
        self.assertNotIn(struct.pack('<3f', *pose.position), joined)

        self.assertEqual(arena._damage_health(source, 10, enemy.entity_id, 3., shell_hit=True), 10)
        self.assertFalse(source.mad_cloaked)
        self.assertEqual((source.engine_factor, state.ready_at), (1., 47.))
        self.assertIn(snapshot, world_motion_messages(arena, team=enemy.team))
        expire_fx = provision_effect_message(source.entity_id, mad_games.CHAMELEON.compact_id)
        events = arena.drain_events()
        self.assertIn(expire_fx, event_messages(arena, arena.players[1], events))
        self.assertIn(expire_fx, event_messages(arena, arena.players[2], events))

    def test_chameleon_expires_after_ten_seconds(self):
        arena = self.arena(mad_games.MODE)
        source = arena.vehicle_for(1)
        arena.started = 1.
        arena.simulation_time = 2.
        with patch('solbreaker.mad_games.vehicle_abilities', return_value=('moonwalk',)):
            mad_games.install_ability(source, 'lightTank')
        state = source.consumables[-1]
        self.assertTrue(arena._activate_consumable(source, state, 2.))
        activation = provision_effect_message(source.entity_id, mad_games.CHAMELEON.compact_id, argument=1)
        events = arena.drain_events()
        self.assertIn(activation, event_messages(arena, arena.players[1], events))
        self.assertNotIn(activation, event_messages(arena, arena.players[2], events))
        arena._consumables(source, 11.99)
        self.assertTrue(source.mad_cloaked)
        arena._consumables(source, 12.)
        self.assertFalse(source.mad_cloaked)
        self.assertEqual((source.engine_factor, state.ready_at), (1., 47.))

    def test_chameleon_ends_on_a_real_shot_and_keeps_own_lock(self):
        arena = self.arena(mad_games.MODE)
        source, enemy = arena.vehicle_for(1), arena.vehicle_for(2)
        source.pose = replace(source.pose, position=(0., 0., 0.))
        enemy.pose = replace(enemy.pose, position=(0., 0., 25.))
        arena._update_mad_games(2.)
        arena.started = 1.
        arena.simulation_time = 2.
        with patch('solbreaker.mad_games.vehicle_abilities', return_value=('moonwalk',)):
            mad_games.install_ability(source, 'lightTank')
        source.autoaim_target = enemy.entity_id
        self.assertTrue(arena._activate_consumable(source, source.consumables[-1], 2.))
        self.assertEqual(source.autoaim_target, enemy.entity_id)
        arena.drain_events()
        arena.projectiles[arena.next_shot] = SimpleNamespace(trace_effect=0)
        shell = SimpleNamespace(gravity=9.81, compact_id=1, missile=None)
        arena._emit_shot(source, shell, source.pose.position, (0., 0., 1.), 3., 1)
        events = arena.drain_events()
        kinds = [event.kind for event in events]
        self.assertLess(kinds.index('mad_cloak'), kinds.index('shot'))
        self.assertFalse(source.mad_cloaked)
        self.assertEqual(source.consumables[-1].ready_at, 47.)

    def test_regenerator_heals_from_actual_enemy_damage(self):
        arena = self.arena(mad_games.MODE)
        source, target = arena.vehicle_for(1), arena.vehicle_for(2)
        mad_games.install_ability(source, 'lightTank')
        source.health -= 100
        target.health = 40
        self.assertEqual(arena._damage_health(target, 200, source.entity_id, 2., shell_hit=True), 40)
        self.assertEqual(source.health, source.max_health-90)
        self.assertEqual(source.damage, 40)
        events = arena.drain_events()
        effect = provision_effect_message(source.entity_id, mad_games.REGENERATOR_EFFECT)
        self.assertIn(effect, event_messages(arena, arena.players[1], events))
        self.assertNotIn(effect, event_messages(arena, arena.players[2], events))
        arena._set_mad_spotted(source, True)
        self.assertIn(effect, event_messages(arena, arena.players[2], events))

    def test_regenerator_heals_allies_with_a_direct_he_hit_or_ram(self):
        accounts = [dict(id=i, nickname=str(i), team=team)
                    for i, team in enumerate((1, 1, 2), 1)]
        arena = Arena(accounts, b'', SimpleNamespace(height=lambda x, z: 0.),
                      now=1., solo=False, room_mode=mad_games.MODE)
        self.addCleanup(arena.close)
        shooter, ally = arena.vehicle_for(1), arena.vehicle_for(2)
        mad_games.install_ability(shooter, 'lightTank')
        ally.health -= 200
        shell = SimpleNamespace(healing_level=None, kind='HIGH_EXPLOSIVE', missile=None, damage=100)
        impact = Impact(2., 1, ShotResult('not_penetrated', object()), (0., 0., 0.), None,
                        (0., 0., 1.), shooter.entity_id, ally.entity_id, shell)
        arena._impact(impact)
        self.assertEqual(ally.health, ally.max_health-140)
        self.assertEqual(shooter.damage, 0)
        events = arena.drain_events()
        self.assertFalse(any(e.kind == 'explosion' for e in events))
        notice = b'TFS1HEAL' + struct.pack('<I', 60)
        marker = health_changed(ally.entity_id, ally.health, shooter.entity_id, healing_marker=True)
        payloads = {account: event_messages(arena, player, events)
                    for account, player in arena.players.items()}
        self.assertIn(marker, payloads[1])
        self.assertIn(notice, payloads[1])
        self.assertIn(notice, payloads[2])
        self.assertNotIn(marker, payloads[2])
        self.assertNotIn(marker, payloads[3])
        self.assertNotIn(notice, payloads[3])
        self.assertIn(health_changed(ally.entity_id, ally.health, shooter.entity_id), payloads[3])
        effect = provision_effect_message(ally.entity_id, mad_games.REGENERATOR_EFFECT,
                                          argument=shooter.entity_id)
        self.assertIn(effect, payloads[1])
        self.assertIn(effect, payloads[2])
        self.assertNotIn(effect, payloads[3])
        arena._set_mad_spotted(ally, True)
        self.assertNotIn(effect, event_messages(arena, arena.players[3], events))
        arena._set_mad_spotted(shooter, True)
        opponent = event_messages(arena, arena.players[3], events)
        self.assertIn(effect, opponent)
        self.assertNotIn(notice, opponent)

        arena._ground_impact(dict(entity_a=shooter.entity_id, entity_b=ally.entity_id, ram=True,
                                  ram_energy_a=100000, ram_energy_b=100000), 2.)
        healed = round(arena.RAM_DAMAGE_PER_KILOJOULE * 100 * mad_games.ALLY_HEAL)
        self.assertEqual(ally.health, ally.max_health-140+healed)
        self.assertEqual(shooter.health, shooter.max_health)

    def test_discharge_costs_hp_and_strengthens_shells_during_the_active_window(self):
        arena = self.arena(mad_games.MODE)
        attacker, target = arena.vehicle_for(1), arena.vehicle_for(2)
        arena.started = 1.
        arena.simulation_time = 2.
        attacker.health = 100
        attacker.max_health = 1000
        target.health = target.max_health = 1000
        with patch('solbreaker.mad_games.vehicle_abilities', return_value=('sacrifice',)):
            mad_games.install_ability(attacker, 'heavyTank')
        state = attacker.consumables[3]
        effect = provision_effect_message(attacker.entity_id, state.spec.compact_id)
        arena.drain_events()
        self.assertFalse(arena._activate_consumable(attacker, state, 2.))
        self.assertEqual(state.uses, 0)
        self.assertNotIn(effect, event_messages(arena, arena.players[1], arena.drain_events()))
        attacker.health = 600
        self.assertTrue(arena._activate_consumable(attacker, state, 2.))
        self.assertEqual((attacker.health, attacker.max_health, attacker.damage_received), (500, 1000, 0))
        events = arena.drain_events()
        self.assertIn(effect, event_messages(arena, arena.players[1], events))
        self.assertNotIn(effect, event_messages(arena, arena.players[2], events))
        shell = SimpleNamespace(kind='ARMOR_PIERCING', healing_level=None, missile=None, poison_level=None)
        result = ShotResult('penetrated', None, 100)
        impact = Impact(3., 1, result, (0., 0., 0.), None, (0., 0., 1.),
                        attacker.entity_id, target.entity_id, shell)
        arena._impact(impact)
        self.assertEqual(target.health, 850)
        arena._impact(replace(impact, at=22., shot=2))
        self.assertEqual(target.health, 750)
        self.assertEqual(state.ready_at, 37.)
        arena._set_mad_spotted(attacker, True)
        arena.drain_events()
        self.assertTrue(arena._activate_consumable(attacker, state, 37.))
        events = arena.drain_events()
        for player in arena.players.values():
            self.assertEqual(event_messages(arena, player, events).count(effect), 1)

    def test_uranium_shield_starts_charging_and_only_consumes_a_qualifying_hit(self):
        arena = self.arena(mad_games.MODE)
        target = arena.vehicle_for(1)
        target.health = target.max_health = 1000
        with patch('solbreaker.mad_games.vehicle_abilities', return_value=('sacrifice', 'shield')):
            mad_games.install_ability(target, 'heavyTank')
        state = target.consumables[4]
        self.assertIs(state.spec, mad_games.URANIUM_SHIELD)
        arena.ready(1, 1.)
        arena.ready(2, 1.)
        self.assertEqual(state.ready_at, 16.)
        self.assertEqual(arena._mad_shield_damage(target, 100, 15.), 100)
        self.assertEqual(arena._mad_shield_damage(target, 50, 16.), 50)
        self.assertEqual(arena._mad_shield_damage(target, 100, 16.), 70)
        self.assertEqual(arena._mad_shield_damage(target, 100, 17.), 100)
        self.assertEqual(arena._mad_shield_damage(target, 100, 31.), 70)
        effect = provision_effect_message(target.entity_id, state.spec.compact_id)
        events = arena.drain_events()
        for player in arena.players.values():
            self.assertIn(effect, event_messages(arena, player, events))
        self.assertFalse(arena._activate_consumable(target, state, 46.))

    def test_ram_damage_combines_discharge_and_the_defenders_shield(self):
        arena = self.arena(mad_games.MODE)
        attacker, target = arena.vehicle_for(1), arena.vehicle_for(2)
        arena.started = 1.
        arena.simulation_time = 2.
        attacker.health = attacker.max_health = target.health = target.max_health = 1000
        with patch('solbreaker.mad_games.vehicle_abilities', return_value=('sacrifice', 'shield')):
            mad_games.install_ability(attacker, 'heavyTank')
            mad_games.install_ability(target, 'heavyTank')
        self.assertTrue(arena._activate_consumable(attacker, attacker.consumables[3], 2.))
        raw_energy = 100_000 / arena.RAM_DAMAGE_PER_KILOJOULE
        arena._ground_impact(dict(entity_a=attacker.entity_id, entity_b=target.entity_id, ram=True,
                                  ram_energy_a=raw_energy, ram_energy_b=raw_energy), 3.)
        self.assertEqual((attacker.health, target.health), (830, 895))
        self.assertEqual(attacker.damage, 105)
        self.assertEqual(target.damage, 70)

    def test_irradiator_requires_the_aimed_enemy_and_expires_on_loss_of_sight(self):
        arena = self.arena(mad_games.MODE)
        source, enemy = arena.vehicle_for(1), arena.vehicle_for(2)
        source.pose = replace(source.pose, position=(0., 0., 0.))
        enemy.pose = replace(enemy.pose, position=(0., 0., 60.))
        enemy.mad_passive = mad_games.SABOTEUR
        arena._update_mad_games(2.)
        arena.started = 1.
        arena.simulation_time = 2.
        with patch('solbreaker.mad_games.vehicle_abilities', return_value=('hunters_mark',)):
            mad_games.install_ability(source, 'heavyTank')
        state = source.consumables[3]
        with patch.object(arena, 'aim_hit', return_value=(enemy.entity_id, enemy.pose.position)):
            self.assertFalse(arena._activate_mad_irradiator(source, state, 2., source.entity_id))
            self.assertEqual(state.uses, 0)
            self.assertTrue(arena._activate_mad_irradiator(source, state, 2., enemy.entity_id))
        self.assertIn(mad_games.IRRADIATED_MODIFIER, arena.state_modifiers(enemy))
        effects = ((4, 4, 2., 12.),)
        packet = vehicle_client_effects_message(arena, enemy.entity_id, effects)
        blob, end = read_blob(packet, 8)
        self.assertEqual(end, len(packet))
        self.assertEqual(pickle.loads(blob, encoding='bytes'),
                         (4, b'\x12\x0c\x22\x0a\x0d' + struct.pack('<f', 2.-arena.epoch)
                          + b'\x15' + struct.pack('<f', 40.)))
        events = arena.drain_events()
        for player in arena.players.values():
            self.assertIn(packet, event_messages(arena, player, events))
            self.assertIn(packet, spawn_message_parts(arena, player))
        self.assertEqual(arena._mad_outgoing_factor(source, enemy, 3.), 1.6)
        self.assertEqual(arena._mad_outgoing_factor(enemy, enemy, 3.), 1.)
        self.assertEqual(arena._mad_outgoing_factor(source, enemy, 12.), 1.)
        with patch.object(arena, 'world', Mock()):
            arena.world._visibility_rays.return_value = [(1, -1, .25)] * 2
            arena._update_mad_games(3.)
        self.assertNotIn(mad_games.IRRADIATED_MODIFIER, arena.state_modifiers(enemy))
        clear = vehicle_client_effects_message(arena, enemy.entity_id, ())
        self.assertIn(clear, event_messages(arena, arena.players[1], arena.drain_events()))
        self.assertEqual(state.ready_at, 32.)

    def test_irradiator_accepts_ability_one_with_sandbox_disabled(self):
        arena = self.arena(mad_games.MODE)
        source, enemy = arena.vehicle_for(1), arena.vehicle_for(2)
        arena.ready(1, 1.)
        arena.ready(2, 1.)
        arena.started = 1.
        arena.simulation_time = 2.
        with patch('solbreaker.mad_games.vehicle_abilities', return_value=('hunters_mark',)):
            mad_games.install_ability(source, 'heavyTank')
        control = sandbox.Control(sandbox.token(arena), 1, sandbox.GRAVITY_JUMP, 0, 0., 0., 0.)
        with patch.object(arena, 'aim_hit', return_value=(enemy.entity_id, enemy.pose.position)):
            arena.sandbox_control(1, control)
        self.assertIn(enemy.entity_id, arena.mad_irradiated)

    def test_coordinator_scales_reload_and_aiming_with_allies_in_range(self):
        arena = self.arena(mad_games.MODE)
        source, ally = arena.vehicle_for(1), arena.vehicle_for(2)
        ally.team = source.team
        source.pose = replace(source.pose, position=(0., 0., 0.))
        ally.pose = replace(ally.pose, position=(0., 0., 50.))
        source.mad_passive = mad_games.COORDINATOR
        source.reload_at, source.reload_total = 8., 6.
        arena._update_mad_games(2.)
        arena._consumables(source, 2.)
        self.assertEqual((source.mad_coordinator_allies, source.reload_speed_factor), (1, 1.2))
        self.assertAlmostEqual(source.aiming_time_factor, 1 / 1.2)
        self.assertAlmostEqual(source.reload_at, 7.)
        self.assertIn(mad_games.COORDINATOR_MODIFIER, arena.state_modifiers(source))
        ally.pose = replace(ally.pose, position=(0., 0., 50.01))
        arena._update_mad_games(3.)
        arena._consumables(source, 3.)
        self.assertEqual((source.mad_coordinator_allies, source.reload_speed_factor, source.aiming_time_factor), (0, 1., 1.))
        self.assertAlmostEqual(source.reload_at, 7.8)

    def test_saboteur_drops_spotting_early_after_line_of_sight_is_blocked(self):
        arena = self.arena(mad_games.MODE)
        saboteur, opponent = arena.vehicle_for(1), arena.vehicle_for(2)
        saboteur.mad_passive = mad_games.SABOTEUR
        saboteur.pose = replace(saboteur.pose, position=(0., 0., 0.))
        opponent.pose = replace(opponent.pose, position=(0., 0., 80.))
        world = Mock()
        with patch.object(arena, 'world', world):
            world._visibility_rays.return_value = [(-1, -1, 1.)] * 2
            arena._update_mad_games(2.)
            self.assertFalse(mad_games.concealed(saboteur, opponent.team))
            opponent.autoaim_target = saboteur.entity_id

            world._visibility_rays.return_value = [(1, -1, .25)] * 2
            arena._update_mad_games(2.1)
            self.assertFalse(mad_games.concealed(saboteur, opponent.team))
            arena._update_mad_games(2.2)
            self.assertTrue(mad_games.concealed(saboteur, opponent.team))
            self.assertFalse(mad_games.concealed(saboteur, saboteur.team))
            self.assertFalse(mad_games.concealed(opponent, saboteur.team))
            self.assertEqual(opponent.autoaim_target, 0)

            opponent.pose = replace(opponent.pose, position=(0., 0., 50.))
            arena._update_mad_games(2.3)
            self.assertFalse(mad_games.concealed(saboteur, opponent.team))

    def test_coordinator_view_bonus_can_reveal_a_distant_enemy(self):
        arena = self.arena(mad_games.MODE)
        observer, enemy = arena.vehicle_for(1), arena.vehicle_for(2)
        observer.pose = replace(observer.pose, position=(0., 0., 0.))
        enemy.pose = replace(enemy.pose, position=(0., 0., 270.))
        observer.mad_passive = mad_games.COORDINATOR
        enemy.mad_passive = mad_games.SABOTEUR
        living = (observer, enemy)
        arena._update_mad_spotting(living, 2.)
        self.assertTrue(mad_games.concealed(enemy, observer.team))
        observer.mad_coordinator_allies = 1
        arena._update_mad_spotting(living, 3.)
        self.assertFalse(mad_games.concealed(enemy, observer.team))
        self.assertTrue(mad_games.concealed(observer, enemy.team))
        observer.mad_coordinator_allies = 0
        arena._update_mad_spotting(living, 3.2)
        self.assertTrue(mad_games.concealed(enemy, observer.team))

    def test_spotting_status_is_sent_only_to_the_detected_player(self):
        arena = self.arena(mad_games.MODE)
        target, enemy = arena.vehicle_for(1), arena.vehicle_for(2)
        visible = bytes.fromhex('13 6b 0b00 10 09 7a 07 08 c8 01 10 01 18 08')
        self.assertEqual(observed_status_message(target.entity_id, True), visible)

        arena._set_mad_spotted(target, True, 2.)
        self.assertFalse(mad_games.concealed(target, enemy.team))
        self.assertNotIn(visible, event_messages(arena, arena.players[1], arena.drain_events()))
        self.assertNotIn(visible, spawn_message_parts(arena, arena.players[1]))
        arena._set_mad_spotted(target, True, 4.99)
        self.assertNotIn(visible, event_messages(arena, arena.players[1], arena.drain_events()))
        arena._set_mad_spotted(target, True, 5.)
        events = arena.drain_events()
        self.assertIn(visible, event_messages(arena, arena.players[1], events))
        self.assertNotIn(visible, event_messages(arena, arena.players[2], events))
        self.assertIn(visible, spawn_message_parts(arena, arena.players[1]))

        arena._set_mad_spotted(target, False)
        hidden = observed_status_message(target.entity_id, False)
        events = arena.drain_events()
        self.assertIn(hidden, event_messages(arena, arena.players[1], events))
        self.assertNotIn(hidden, event_messages(arena, arena.players[2], events))
        self.assertNotIn(visible, spawn_message_parts(arena, arena.players[1]))

        target.mad_passive = mad_games.SABOTEUR
        arena._set_mad_spotted(target, True, 6.)
        arena._set_mad_spotted(target, False, 6.1)
        arena._set_mad_spotted(target, True, 6.2)
        arena._set_mad_spotted(target, True, 6.34)
        self.assertNotIn(visible, event_messages(arena, arena.players[1], arena.drain_events()))
        arena._set_mad_spotted(target, True, 6.36)
        self.assertIn(visible, event_messages(arena, arena.players[1], arena.drain_events()))
        arena._damage_health(target, target.health, enemy.entity_id, 6.4, shell_hit=False)
        self.assertIn(hidden, event_messages(arena, arena.players[1], arena.drain_events()))

    def reflector_battle(self):
        arena = self.arena(mad_games.MODE)
        first, second = arena.vehicle_for(1), arena.vehicle_for(2)
        arena.started = 1.
        arena.simulation_time = 2.
        for vehicle in (first, second):
            vehicle.health = vehicle.max_health = 1000
        with patch('solbreaker.mad_games.vehicle_abilities',
                   return_value=('reflectionAbility', 'revengeAbility')):
            for vehicle in (first, second):
                mad_games.install_ability(vehicle, 'heavyTank')
        return arena, first, second

    def hit(self, arena, shooter, target, damage, at, *, shot=1, connected=True):
        shell = SimpleNamespace(kind='ARMOR_PIERCING', healing_level=None, missile=None,
                                poison_level=None, damage=200)
        contact = SimpleNamespace(external=False) if connected else None
        result = ShotResult('penetrated' if damage else 'not_penetrated', contact, damage)
        arena._impact(Impact(at, shot, result, (0., 0., 0.), None, (0., 0., 1.),
                             shooter.entity_id, target.entity_id, shell))

    def test_reflector_returns_damage_and_non_penetrations_without_recursion(self):
        arena, attacker, defender = self.reflector_battle()
        for vehicle in (attacker, defender):
            state = vehicle.consumables[3]
            self.assertIs(state.spec, mad_games.REFLECTOR)
            self.assertTrue(arena._activate_consumable(vehicle, state, 2.))
            self.assertIn(mad_games.REFLECTOR_MODIFIER, arena.state_modifiers(vehicle))
        self.hit(arena, attacker, defender, 100, 3.)
        self.assertEqual((attacker.health, defender.health), (965, 900))
        self.assertFalse(attacker.mad_revenge)
        self.hit(arena, attacker, defender, 0, 4., shot=2, connected=False)
        self.assertEqual(attacker.health, 965)
        self.hit(arena, attacker, defender, 0, 5., shot=3)
        self.assertEqual(attacker.health, 895)
        self.hit(arena, attacker, defender, 0, 14., shot=4)
        self.assertEqual(attacker.health, 895)

    def test_bot_reflector_reacts_to_an_incoming_hit_even_while_reloading(self):
        arena, attacker, bot = self.reflector_battle()
        arena.training_bots[bot.entity_id] = Mock()
        state = bot.consumables[3]
        arena._training_bot_mad_attack(bot, attacker, 2.)
        self.assertEqual(state.uses, 0)
        bot.reload_at = 8.
        self.hit(arena, attacker, bot, 0, 2., connected=False)
        self.assertEqual(state.uses, 0)
        self.hit(arena, attacker, bot, 100, 3., shot=2)
        self.assertEqual((attacker.health, bot.health, state.uses), (1000, 900, 1))
        self.hit(arena, attacker, bot, 100, 4., shot=3)
        self.assertEqual((attacker.health, bot.health, state.uses), (965, 800, 1))

    def test_reflector_hit_carries_both_ids_once_for_viewers_who_can_see_it(self):
        arena, attacker, defender = self.reflector_battle()
        arena._set_mad_spotted(attacker, True)
        arena._set_mad_spotted(defender, True)
        arena.drain_events()
        hit_effect = (1, 0x100)
        arena.emit('health', attacker.entity_id, defender.entity_id, health=900,
                   received_damage=100, hit_effect=hit_effect, reflector_hit=True)
        (hit,) = arena.drain_events()
        packet = (b'\x12' + struct.pack('<I', defender.entity_id) + b'\x43\x15'
                  + struct.pack('<IIBQBHB', attacker.entity_id, defender.entity_id,
                                1, hit_effect[1] | 3, hit_effect[0], 100, 1))
        for player in arena.players.values():
            self.assertEqual(event_messages(arena, player, (hit,)).count(packet), 1)
        arena._set_mad_spotted(defender, False)
        self.assertNotIn(packet, event_messages(arena, arena.players[1], (hit,)))
        arena._set_mad_spotted(attacker, False)
        feedback = packet[:11] + bytes(4) + packet[15:]
        self.assertIn(feedback, event_messages(arena, arena.players[2], (hit,)))

    def test_toxic_stare_tracks_one_return_hit_against_the_same_life(self):
        arena, attacker, defender = self.reflector_battle()
        self.hit(arena, attacker, defender, 100, 3.)
        self.assertIn(mad_games.TOXIC_STARE_MODIFIER, arena.state_modifiers(defender))
        self.assertEqual(revenge_targets_message(arena, defender), b'\x13\xb6\x01\x00')
        arena.drain_events()
        arena._set_mad_spotted(attacker, True)
        events = arena.drain_events()
        marked = revenge_targets_message(arena, defender)
        self.assertNotEqual(marked, b'\x13\xb6\x01\x00')
        self.assertIn(marked, event_messages(arena, arena.players[2], events))
        self.assertNotIn(marked, event_messages(arena, arena.players[1], events))
        self.hit(arena, defender, attacker, 100, 4., shot=2)
        self.assertEqual(attacker.health, 880)
        self.assertNotIn(mad_games.TOXIC_STARE_MODIFIER, arena.state_modifiers(defender))
        self.assertIn(b'\x13\xb6\x01\x00',
                      event_messages(arena, arena.players[2], arena.drain_events()))
        self.hit(arena, defender, attacker, 100, 5., shot=3)
        self.assertEqual(attacker.health, 780)
        self.hit(arena, attacker, defender, 100, 9., shot=4)
        self.hit(arena, defender, attacker, 0, 10., shot=5)
        self.assertFalse(defender.mad_revenge)
        self.hit(arena, attacker, defender, 100, 11., shot=6)
        attacker.generation += 1
        self.hit(arena, defender, attacker, 100, 12., shot=7)
        self.assertEqual(attacker.health, 680)
        self.hit(arena, attacker, defender, 100, 13., shot=8)
        arena._update_mad_games(43.)
        self.assertFalse(defender.mad_revenge)

    def test_mirrored_rammers_apply_each_modifier_once(self):
        normal = self.arena('encounter')
        modified = self.arena(mad_games.MODE)
        for arena in (normal, modified):
            arena._damage_health = Mock()
            a, b = arena.vehicle_for(1), arena.vehicle_for(2)
            arena._ground_impact(dict(entity_a=a.entity_id, entity_b=b.entity_id, ram=True,
                                     ram_energy_a=100000, ram_energy_b=100000), 2.)
            self.assertEqual(arena._damage_health.call_count, 2)
        normal_damage = normal._damage_health.call_args.args[1]
        mad_damage = modified._damage_health.call_args.args[1]
        self.assertAlmostEqual(mad_damage / normal_damage, .495, delta=.03)
