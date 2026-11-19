import math
import random
import struct
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from solbreaker import mad_games, uprising
from solbreaker.arena import Arena, TrainingBot
from solbreaker.armor import ShotResult
from solbreaker.ballistics import Impact, body_states
from solbreaker.arena_commands import decode_input, decode_uprising_target, apply_at_tick
from solbreaker.arena_owner import Command
from solbreaker.arena_owner_game import _KINDS
from solbreaker.arena_owner_service import _INPUT_OPS
from solbreaker.battle import (allied_reload_message, arena_update, consumable_message, create_avatar, create_avatar_cell,
                               enemy_reload_visibility_message, event_messages,
                               initial_messages, pb_blob, pb_int, provision_effect_message,
                               reconnect_message_parts,
                               uprising_elixir_message, uprising_offers_message, uprising_passive_message,
                               uprising_respawn_time_message, uprising_wait_respawn_message,
                               vehicle_client_effects_message, vehicle_property, health_changed)
from solbreaker.battle_settings import TITLE, decode, packed_recipe
from solbreaker.collision import Motion, Pose
from solbreaker.consumables import ACTIVE, COOLDOWN, READY, ConsumableState
from solbreaker.modules import TrackSpec, TrackState
from solbreaker.native_rooms import room_options
from solbreaker.profile import game_mode_descriptions
from solbreaker.training_config import RoomBattleSettings, TrainingConfig
from solbreaker.vehicle_data import Shell, Weapon
from solbreaker.uprising import BattleState, CHOICE_SECONDS, CLIENT_GAME_MODE, MODE, RESPAWN_CLOAK


class UprisingTests(TestCase):
    def test_hangar_advertises_selected_ability_catalog(self):
        for rules, catalog in ((MODE, 'hellgames'), ('mad_games', 'arcade'), ('regular', '')):
            with self.subTest(rules=rules):
                garage = game_mode_descriptions(False, rules)
                room = game_mode_descriptions(True, rules)
                self.assertEqual(garage[7]['arcadeAbilities'], catalog)
                self.assertEqual(garage[1]['arcadeAbilities'], '')
                self.assertEqual(room[1]['arcadeAbilities'], catalog)

    def test_room_and_solo_settings_accept_uprising(self):
        config = decode(TITLE, 'SolbreakerBots/2/0/1/1/6/0/-1/0', 0, {})
        self.assertEqual(config.battle_mode, MODE)
        self.assertEqual(packed_recipe(config, {}) // 1000, 6)
        self.assertEqual(room_options('[mode uprising] [bots 1:2] Notes')[:3],
                         ('Notes', (1, 2), MODE))

    def test_native_hellgames_mode_and_remaining_respawns_are_sent_on_entry(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='One'), dict(id=2, nickname='Two')],
                      b'', terrain, now=1., solo=False, room_mode=MODE)
        self.addCleanup(arena.close)
        player = arena.players[1]
        avatar = create_avatar({'nickname': player.nickname}, player.avatar_id, arena.unique_id,
                               arena.client_map_id, game_mode=CLIENT_GAME_MODE)
        mode = pb_int(4, 2) + pb_blob(5, b'hellgames') + pb_blob(7, b'hellgames') + pb_blob(8, b'hellgames') + pb_int(12, False)
        for remaining in (2, 1, 0):
            cell = create_avatar_cell(arena.space_id, player.team, remaining)
            self.assertEqual(initial_messages(arena, player),
                             avatar + cell + arena_update(18, pb_blob(17, mode)))
            arena.uprising.destroyed(player.vehicle_id)

    def test_elixir_uses_battle_time_and_clamped_enemy_damage(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='One'), dict(id=2, nickname='Two'),
                       dict(id=3, nickname='Three', team=1)],
                      b'', terrain, now=1., solo=False, room_mode=MODE)
        self.addCleanup(arena.close)
        for account in arena.players:
            arena.ready(account, 1.)
        owner, enemy, ally = (arena.vehicle_for(account) for account in (1, 2, 3))
        arena._advance_uprising_income(4.9)
        arena._advance_uprising_income(4.99)
        self.assertEqual(arena.uprising.elixir(owner.entity_id), 6)
        arena.drain_events()
        arena._damage_health(ally, 10, owner.entity_id, 5., shell_hit=True)
        self.assertEqual(arena.uprising.elixir(owner.entity_id), 6)
        arena._damage_health(enemy, 7, owner.entity_id, 5., shell_hit=True)
        self.assertEqual(arena.uprising.elixir(owner.entity_id), 6)
        enemy.health = 13
        arena._damage_health(enemy, 999, owner.entity_id, 5., shell_hit=True)
        self.assertEqual(arena.uprising.elixir(owner.entity_id), 38)
        self.assertEqual(arena.uprising.elixir(enemy.entity_id), 6)
        events = arena.drain_events()
        message = b'\x13\xb0\x26\x00'
        self.assertEqual(uprising_elixir_message(38), message)
        self.assertIn(message, event_messages(arena, arena.players[1], events))
        self.assertNotIn(message, event_messages(arena, arena.players[2], events))
        self.assertIn(message, reconnect_message_parts(arena, arena.players[1]))

    def test_fortune_increases_shell_damage_after_the_purchase_is_confirmed(self):
        state = BattleState(random.Random('0' * 32 + ':uprising'))
        entity = 200
        state.prepare_offers(entity)
        state.earn(entity, 200)
        self.assertEqual(state.select(entity, 0x70fd, True), 0)
        self.assertEqual(state.shell_damage_factor(entity, lambda: 0.), 1.)
        state.on_respawn(entity)
        self.assertEqual(state.upgrade(entity, 'fortune').grade, 2)
        self.assertEqual(state.shell_damage_factor(entity, lambda: .749), 1.25)
        self.assertEqual(state.shell_damage_factor(entity, lambda: .75), 1.)

    def test_upgrade_requests_reserve_elixir_until_respawn(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='One'), dict(id=2, nickname='Two')],
                      b'', terrain, now=1., solo=False, room_mode=MODE, result_id='0' * 32)
        self.addCleanup(arena.close)
        for account in arena.players:
            arena.ready(account, 1.)
        tank, opponent = (arena.vehicle_for(account) for account in (1, 2))
        player, peer = arena.players[1], arena.players[2]
        arena._award_elixir(tank, 199)
        with (patch.object(arena.uprising._rng, 'sample', return_value=['fortune', 'engine', 'reload']),
              patch.object(arena.uprising._rng, 'choice', side_effect=lambda grades: grades[-1])):
            arena._damage_health(tank, tank.health, opponent.entity_id, 1., shell_hit=False)
        cards = ((0x70fd, 2), (0x27fd, 2), (0x26fd, 2))
        self.assertEqual(arena.uprising.offers(tank.entity_id), cards)
        events = arena.drain_events()
        wait = uprising_wait_respawn_message(tank.entity_id, opponent.entity_id)
        owner_messages = event_messages(arena, player, events)
        self.assertIn(uprising_offers_message(cards) + uprising_respawn_time_message(CHOICE_SECONDS)
                      + wait, owner_messages)
        self.assertNotIn(uprising_offers_message(cards), event_messages(arena, peer, events))
        with patch.object(arena, 'world', Mock()):
            arena._advance_respawns(2.)
            arena._advance_respawns(2.05)
        clock_events = arena.drain_events()
        self.assertEqual([event.kind for event in clock_events], ['uprising_clock'])
        clock = uprising_respawn_time_message(9.)
        self.assertIn(clock, event_messages(arena, player, clock_events))
        self.assertNotIn(clock, event_messages(arena, peer, clock_events))


        def request(kind, compact_id, account=1, entity=None):
            if entity is None:
                entity = arena.players[account].vehicle_id
            payload = struct.pack('<II', entity, compact_id)
            op = _INPUT_OPS[kind]
            Command(op, 1., account, payload).validate()
            decoded = decode_input(account, _KINDS[op], payload, 1.)
            apply_at_tick(arena, decoded)

        request(21, 0x27fd)  # Too expensive.
        self.assertEqual(arena.uprising.elixir(tank.entity_id), 199)
        arena._award_elixir(tank, 201)
        self.assertIn(uprising_offers_message(tuple((compact, 0) for compact, _ in cards)),
                      event_messages(arena, player, arena.drain_events()))
        request(21, 0x27fd, account=2)
        request(21, 0x27fd, entity=opponent.entity_id)
        request(21, 0x1efd)  # Not in this offer.
        request(21, 0x27fd)
        request(21, 0x27fd)  # Reliable retry does not charge twice.
        self.assertEqual(arena.uprising.elixir(tank.entity_id), 200)
        self.assertEqual(arena.uprising.upgrade(tank.entity_id, 'engine').engine, 1.)
        self.assertIn(uprising_offers_message(((0x70fd, 0), (0x27fd, 1), (0x26fd, 0))),
                      reconnect_message_parts(arena, player))
        self.assertIn(wait, reconnect_message_parts(arena, player))
        request(22, 0x27fd)
        request(22, 0x27fd)
        self.assertEqual(arena.uprising.elixir(tank.entity_id), 400)
        request(21, 0x27fd)
        request(21, 0x26fd)

        world = Mock()
        world.training_placement.return_value = Pose((1., 0., 2.), 0.)
        weapon = SimpleNamespace(shells=(), ammunition=(), ammo=564, clip=4, reload=6.)
        world.assets_for.return_value = SimpleNamespace(armor=SimpleNamespace(health=tank.max_health),
                                                       definition=SimpleNamespace(aiming=None, weapon=weapon))
        with patch.object(arena, 'world', world):
            arena._advance_respawns(1. + CHOICE_SECONDS)
        restored = arena.vehicle_for(1)
        self.assertEqual(arena.uprising.offers(tank.entity_id), ())
        self.assertAlmostEqual(restored.engine_factor, 4. * 3.1)
        self.assertAlmostEqual(restored.forward_speed_bias, 40. / 3.6)
        self.assertAlmostEqual(restored.reload_speed_factor, 1. / .65)
        request(22, 0x27fd)  # Confirmed upgrades survive respawn.
        self.assertEqual(arena.uprising.elixir(tank.entity_id), 0)
        self.assertEqual(arena.uprising.upgrade(tank.entity_id, 'engine').grade, 2)
        self.assertIn(uprising_offers_message(()), event_messages(arena, player, arena.drain_events()))

    def test_tank_change_keeps_health_fraction_elixir_and_remaining_lives(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='One'), dict(id=2, nickname='Two')],
                      b'', terrain, now=1., solo=False, room_mode=MODE,
                      room_settings=RoomBattleSettings())
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        arena.ready(2, 1.)
        tank, enemy = arena.vehicle_for(1), arena.vehicle_for(2)
        state = arena.uprising
        state.destroyed(tank.entity_id)
        state.earn(tank.entity_id, 700)
        tank.health = tank.max_health // 2
        tank.damage = 45

        world = Mock()
        world.training_placement.return_value = Pose((1., 0., 2.), 0.)
        weapon = SimpleNamespace(shells=(), ammunition=(), ammo=564, clip=4, reload=6.)
        world.assets_for.return_value = SimpleNamespace(
            armor=SimpleNamespace(health=1600),
            definition=SimpleNamespace(aiming=None, weapon=weapon, descriptor=b'new'))
        scene = world.assets_for.return_value
        world.assets_for.return_value = object()
        workers = Mock(closing=False, closed=False)

        def replace_body(entity, assets, spawns):
            self.assertEqual(entity, tank.entity_id)
            self.assertIs(assets, scene)
            world.assets_for.return_value = assets
            return world.training_placement.return_value

        def release_workers():
            if workers.closing:
                workers.closed = True
            workers.closing = True
            return workers.closed

        world.replace_training_body.side_effect = replace_body
        workers.advance_close.side_effect = release_workers
        arena.drive_workers = workers
        arena._drive_workers_attempted = True
        with patch.object(arena, 'world', world):
            arena.reserve_training_seat(1)
            arena.reenter_training(1, scene, None, ())
            workers.advance_close.assert_called_once_with()
            self.assertTrue(workers.closing)
            self.assertFalse(arena._drive_workers_attempted)
            arena.step(1.)
            self.assertIsNone(arena.drive_workers)
            replacement = arena.vehicle_for(1)
            self.assertEqual(replacement.entity_id, tank.entity_id)
            self.assertEqual(replacement.health, 800)
            self.assertEqual(replacement.damage, 45)
            self.assertEqual(state.elixir(tank.entity_id), 700)
            self.assertEqual(state.remaining(tank.entity_id), 2)

            arena.reserve_training_seat(1)
            arena._damage_health(replacement, replacement.health, enemy.entity_id, 1., shell_hit=False)
            self.assertTrue(arena.training_tank_available(1))
            arena.reenter_training(1, scene, None, ())
            replacement = arena.vehicle_for(1)
            self.assertEqual(replacement.health, replacement.max_health)
            self.assertTrue(replacement.mad_cloaked)
            self.assertEqual(state.remaining(tank.entity_id), 1)

    def test_enemy_reload_upgrade_reveals_only_visible_enemy_reload(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='One'), dict(id=2, nickname='Two')],
                      b'', terrain, now=1., solo=False, room_mode=MODE)
        self.addCleanup(arena.close)
        player = arena.players[1]
        tank, enemy = arena.vehicle_for(1), arena.vehicle_for(2)
        state = arena.uprising
        with patch.object(state._rng, 'sample', return_value=['enemy_reload']):
            state.prepare_offers(tank.entity_id)
        state.earn(tank.entity_id, 100)
        state.select(tank.entity_id, uprising.ENEMY_RELOAD.compact_id, True)
        self.assertEqual(allied_reload_message(arena, player, enemy), b'')
        state.on_respawn(tank.entity_id)
        self.assertTrue(uprising_passive_message(state, tank.entity_id))
        enabled = b'\x13\xb4\x01'
        self.assertEqual(enemy_reload_visibility_message(True), enabled)
        self.assertIn(enabled, reconnect_message_parts(arena, player))
        self.assertNotIn(enabled, reconnect_message_parts(arena, arena.players[2]))
        cell = create_avatar_cell(arena.space_id, player.team, 2, enemy_reload=True)
        self.assertIn(cell, initial_messages(arena, player))
        arena.emit('ready', tank.entity_id, respawn=True)
        events = arena.drain_events()
        self.assertIn(enabled, event_messages(arena, player, events))
        self.assertNotIn(enabled, event_messages(arena, arena.players[2], events))
        enemy.reload_at = 10.
        self.assertTrue(allied_reload_message(arena, player, enemy))
        self.assertEqual(allied_reload_message(arena, arena.players[2], tank), b'')
        enemy.mad_cloaked = True
        self.assertEqual(allied_reload_message(arena, player, enemy), b'')
        enemy.mad_cloaked = False
        ability = ConsumableState(uprising.PLAY_DEAD)
        enemy.consumables = (ability,)
        self.assertTrue(ability.activate(arena.simulation_time))
        self.assertEqual(allied_reload_message(arena, player, enemy), b'')
        self.assertTrue(ability.stop(arena.simulation_time))
        enemy.reload_at = arena.simulation_time + 2.
        message = allied_reload_message(arena, player, enemy)
        arena.emit('uprising_play_dead_fx', enemy.entity_id, active=False, health=enemy.health)
        self.assertIn(message, event_messages(arena, player, arena.drain_events()))

    def test_low_tier_offers_follow_the_pinned_eligibility(self):
        state = BattleState(random.Random(1))
        state.prepare_offers(200, 4)
        self.assertEqual({compact for compact, _ in state.offers(200)}, {0x1efd, 0x22fd})

    def test_ram_upgrades_apply_both_directions_after_respawn(self):
        arena = Arena([dict(id=1, nickname='One'), dict(id=2, nickname='Two')], b'',
                      SimpleNamespace(height=lambda x, z: 0.), now=1., solo=False, room_mode=MODE)
        self.addCleanup(arena.close)
        tank, enemy = (arena.vehicle_for(account) for account in (1, 2))
        state = arena.uprising
        for grade, compact_id, added, absorbed in ((0, None, 50, 50), (1, 0x24fd, 80, 33), (2, 0x28fd, 105, 18)):
            if grade:
                with (patch.object(state._rng, 'sample', return_value=['ram']),
                      patch.object(state._rng, 'choice',
                                   side_effect=lambda rows: next(row for row in rows if row.grade == grade))):
                    state.prepare_offers(tank.entity_id)
                previous_balance = state.elixir(tank.entity_id)
                state.earn(tank.entity_id, grade * 100)
                self.assertEqual(state.select(tank.entity_id, compact_id, True), previous_balance)
                state.on_respawn(tank.entity_id)
            hp = tank.health, enemy.health
            arena._ground_impact(dict(entity_a=tank.entity_id, entity_b=enemy.entity_id, ram=True,
                                      ram_energy_a=100_000., ram_energy_b=100_000.), 1.)
            self.assertEqual((hp[0] - tank.health, hp[1] - enemy.health), (absorbed, added))

    def test_shot_upgrade_fast_cooldown_preserves_progress(self):
        state = BattleState(random.Random(1))
        entity = 200
        upgrade = uprising.INFERNO
        with patch.object(state._rng, 'sample', return_value=[upgrade.family]):
            state.prepare_offers(entity)
        state.earn(entity, upgrade.cost)
        state.select(entity, upgrade.compact_id, True)
        state.on_respawn(entity, now=1.)
        self.assertEqual(state.trigger_shot_upgrade(entity, upgrade.family, 6.), (upgrade, 30.))
        state.set_shot_upgrade_cooldown_limit(entity, 21., .5)
        self.assertEqual(tuple(state.shot_upgrade_statuses(entity, 21.)), ((upgrade.compact_id, .25, .5),))
        state.set_shot_upgrade_cooldown_limit(entity, 21., None)
        self.assertEqual(tuple(state.shot_upgrade_statuses(entity, 21.)), ((upgrade.compact_id, 15., 30.),))
        self.assertIsNone(state.trigger_shot_upgrade(entity, upgrade.family, 35.9, .5))
        self.assertEqual(state.trigger_shot_upgrade(entity, upgrade.family, 36., .5), (upgrade, .5))

    def test_shot_upgrades_use_pinned_damage_range_and_cooldowns(self):
        accounts = [dict(id=i, nickname=str(i), team=1 if i < 3 else 2) for i in range(1, 7)]
        shell = Shell(1, 100., 100., (100.,), 0., 0., 500., 0., 500.)
        for upgrade in (uprising.CHAIN_LIGHTNING, uprising.CHAIN_LIGHTNING_II, uprising.INFERNO):
            with self.subTest(upgrade=upgrade):
                arena = Arena(accounts, b'', SimpleNamespace(height=lambda x, z: 0.), now=1.,
                              solo=False, room_mode=MODE)
                self.addCleanup(arena.close)
                for account in arena.players:
                    arena.ready(account, 1.)
                tanks = [arena.vehicle_for(account) for account in arena.players]
                shooter, ally, primary, near, other, distant = tanks
                for tank, z in zip(tanks, (0., 71., 70., 84., 96., 110.)):
                    tank.pose = Pose((0., 0., z), 0.)
                shooter.health = 200
                state = arena.uprising
                with (patch.object(state._rng, 'sample', return_value=[upgrade.family]),
                      patch.object(state._rng, 'choice', return_value=upgrade)):
                    state.prepare_offers(shooter.entity_id)
                state.earn(shooter.entity_id, upgrade.cost)
                self.assertEqual(state.select(shooter.entity_id, upgrade.compact_id, True), 0)
                state.on_respawn(shooter.entity_id, now=1.)
                initial_health = {tank.entity_id: tank.health for tank in tanks}
                player, peer = arena.players[1], arena.players[3]
                starting_cooldown = consumable_message(upgrade.compact_id, COOLDOWN, 5, 5)
                self.assertIn(starting_cooldown, reconnect_message_parts(arena, player))
                self.assertNotIn(starting_cooldown, reconnect_message_parts(arena, peer))
                arena.drain_events()

                def impact(shot, now, outcome):
                    hit_tank = primary.entity_id if outcome == 'penetrated' else None
                    result = ShotResult(outcome, None, 100 if hit_tank else 0)
                    arena._impact(Impact(now, shot, result, primary.pose.position, None,
                                         (0., 0., 1.), shooter.entity_id, hit_tank, shell,
                                         collision=outcome != 'miss'))

                def loss(tank):
                    return initial_health[tank.entity_id] - tank.health

                impact(1, 5.9, 'not_penetrated')
                impact(2, 6., 'miss')
                self.assertEqual(loss(primary), 0)
                arena._advance_uprising_targeted(6., body_states(arena.vehicles))
                self.assertIn(consumable_message(upgrade.compact_id, READY),
                              event_messages(arena, player, arena.drain_events()))
                if upgrade is uprising.INFERNO:
                    impact(3, 6.1, 'not_penetrated')
                    self.assertEqual([loss(tank) for tank in (primary, near, other, distant)], [60, 60, 0, 0])
                    events = arena.drain_events()
                    splash = b'\x13\x6f' + struct.pack('<I3f', 0, *primary.pose.position)
                    cooldown = consumable_message(upgrade.compact_id, COOLDOWN, 30, 30)
                    self.assertIn(splash, event_messages(arena, peer, events))
                    self.assertIn(cooldown + splash, event_messages(arena, player, events))
                    self.assertNotIn(cooldown, event_messages(arena, peer, events))
                    impact(4, 7., 'not_penetrated')
                    self.assertEqual(loss(primary), 60)
                    impact(5, 36.1, 'not_penetrated')
                    self.assertEqual(loss(primary), 120)
                else:
                    impact(3, 6., 'not_penetrated')
                    self.assertEqual(loss(primary), 0)
                    impact(4, 6.1, 'penetrated')
                    a, b, c = (int(factor * 100) for factor in upgrade.damage_factors)
                    self.assertEqual(loss(primary), 100 + a)
                    before = body_states(arena.vehicles)
                    arena._advance_uprising_targeted(6.2, before)
                    self.assertEqual(loss(near), 0)
                    arena._advance_uprising_targeted(6.3, before)
                    self.assertEqual(loss(near), b)
                    self.assertEqual(loss(other), 0)
                    events = arena.drain_events()
                    arc = b'\x13\x6e' + struct.pack('<II6f', primary.entity_id, 0x50000011,
                                                     *primary.pose.position, 0., 1., 84.)
                    self.assertIn(arc, event_messages(arena, player, events))
                    near.mad_unspotted = True
                    self.assertNotIn(arc, event_messages(arena, player, events))
                    arena._advance_uprising_targeted(6.5, before)
                    self.assertEqual(loss(other), c)
                    self.assertEqual(loss(distant), 0)
                    impact(5, 7., 'penetrated')
                    self.assertEqual(loss(primary), 200 + a)
                self.assertEqual(loss(ally), 0)

    def test_ghost_shortens_spotting_and_keeps_its_warning_private(self):
        arena = Arena([dict(id=1, nickname='One'), dict(id=2, nickname='Two')],
                      b'', SimpleNamespace(height=lambda x, z: 0.), now=1.,
                      solo=False, room_mode=MODE)
        self.addCleanup(arena.close)
        tank, enemy = arena.vehicle_for(1), arena.vehicle_for(2)
        state = arena.uprising
        with patch.object(state._rng, 'sample', return_value=['ghost']):
            state.prepare_offers(tank.entity_id)
        state.earn(tank.entity_id, 100)
        state.select(tank.entity_id, uprising.GHOST.compact_id, True)
        state.on_respawn(tank.entity_id)
        self.assertTrue(uprising_passive_message(state, tank.entity_id))
        tank.pose, enemy.pose = Pose((0., 0., 0.), 0.), Pose((0., 0., 100.), 0.)
        arena._update_team_spotting(3.)
        self.assertAlmostEqual(tank.mad_spotted_until, 3.15)
        self.assertFalse(tank.mad_observation_warning)
        arena._update_team_spotting(3.16)
        self.assertTrue(tank.mad_observation_warning)
        events = arena.drain_events()
        warnings = [event for event in events if event.kind == 'mad_observation_warning']
        self.assertTrue(event_messages(arena, arena.players[1], warnings))
        self.assertFalse(event_messages(arena, arena.players[2], warnings))
        enemy.pose = Pose((0., 0., 500.), 0.)
        arena._update_team_spotting(3.20)
        self.assertFalse(tank.mad_unspotted)
        arena._update_team_spotting(3.32)
        self.assertTrue(tank.mad_unspotted)
        self.assertFalse(tank.mad_observation_warning)
        self.assertFalse(enemy.mad_unspotted)

    def test_scouting_awards_first_observer_once_per_enemy_life(self):
        arena = Arena([dict(id=1, nickname='One', team=1), dict(id=2, nickname='Two', team=1),
                       dict(id=3, nickname='Three', team=2)], b'',
                      SimpleNamespace(height=lambda x, z: 0.), now=1., solo=False, room_mode=MODE)
        self.addCleanup(arena.close)
        scout, ally, enemy = (arena.vehicle_for(account) for account in (1, 2, 3))
        for distance, tank in ((0., scout), (5., ally), (100., enemy)):
            tank.pose = Pose((0., 0., distance), 0.)
        world = Mock()

        def sample(at, blocked):
            hit = (8, -1, .5) if blocked else (-1, -1, 1.)
            world._visibility_rays.side_effect = lambda starts, ends: [hit] * len(starts)
            with patch.object(arena, 'world', world):
                arena._update_team_spotting(at)

        state = arena.uprising
        sample(1., blocked=True)
        self.assertTrue(enemy.mad_unspotted)
        self.assertEqual(state.elixir(scout.entity_id), 0)
        sample(2., blocked=False)
        self.assertFalse(enemy.mad_unspotted)
        self.assertEqual(state.elixir(scout.entity_id), uprising.ELIXIR_PER_SPOT)
        self.assertEqual(state.elixir(ally.entity_id), 0)
        sample(12.1, blocked=True)
        self.assertTrue(enemy.mad_unspotted)
        sample(13., blocked=False)
        self.assertEqual(state.elixir(scout.entity_id), uprising.ELIXIR_PER_SPOT)
        state.destroyed(enemy.entity_id)
        sample(14., blocked=False)
        self.assertEqual(state.elixir(scout.entity_id), 2 * uprising.ELIXIR_PER_SPOT)

    def test_duplet_spends_a_second_round_and_waits_for_the_original_group(self):
        shell = Shell(1, 100., 100., (100.,), 0., 0., 500., 0., 500.)
        loadouts = (
            (Weapon(6, 1, 1., 6., (shell,)), (1., 1.3), 4, 0),
            (Weapon(1, 1, 1., 6., (shell,)), (1.,), 0, 0),
            (Weapon(6, 2, 1., 6., (shell,), pump_reload_times=(3., 2.)), (1., 1.3), 4, 0),
            (Weapon(6, 2, 1., 6., (shell,), burst_count=2, burst_interval=.2), (1., 1.2, 1.5), 3, 0),
        )
        for weapon, times, ammo, clip in loadouts:
            with self.subTest(weapon=weapon):
                arena = Arena([dict(id=1, nickname='One'), dict(id=2, nickname='Two')],
                              b'', SimpleNamespace(height=lambda x, z: 0.), now=1.,
                              solo=False, room_mode=MODE)
                self.addCleanup(arena.close)
                for account in arena.players:
                    arena.ready(account, 1.)
                tank = arena.vehicle_for(1)
                tank.definition = SimpleNamespace(weapon=weapon, aiming=SimpleNamespace(
                    dispersion_radius=.4, aiming_time=2., after_shot_dispersion=4.))
                arena._configure_ammunition(tank)
                state = arena.uprising
                with patch.object(state._rng, 'sample', return_value=['duplet']):
                    state.prepare_offers(tank.entity_id)
                state.earn(tank.entity_id, 400)
                state.select(tank.entity_id, uprising.DUPLET.compact_id, True)
                state.on_respawn(tank.entity_id)
                arena._consumables(tank, 1.)
                self.assertEqual(tank.shot_dispersion_factor, 2.2)
                self.assertEqual(tank.aiming_time_factor, 1.6)
                arena._fire_vehicle(tank, 1.)
                for at in times[1:]:
                    self.assertAlmostEqual(tank.reload_at, at)
                    arena._fire_burst_next(tank, at)
                shots = [event for event in arena.drain_events() if event.kind == 'shot']
                self.assertEqual([event.data['at'] for event in shots], list(times))
                self.assertEqual([event.data.get('shooting_count', 1) for event in shots],
                                 [len(times)] + [0]*(len(times)-1))
                self.assertEqual((tank.ammo, tank.clip, tank.burst_pending), (ammo, clip, False))
                if weapon.pump_reload_times:
                    self.assertAlmostEqual(tank.pump_at, times[-1] + 3.)

    def test_targeted_ability_decodes_native_equipment_parameters(self):
        self.assertEqual(decode_uprising_target(42, bytes.fromhex('0a0308c901')), 201)
        for ident, field in ((42, 1), (36, 2), (43, 4)):
            for entity in (1, 129, 1200, 0xffffffff):
                self.assertEqual(decode_uprising_target(ident, pb_blob(field, pb_int(1, entity))), entity)
            for bad in (pb_blob(3, pb_int(1, 201)), pb_blob(field, pb_int(1, 0x100000000)),
                        pb_blob(field, pb_int(1, 1) + pb_int(1, 2)), b'\x0a\x02\x08\x80'):
                with self.assertRaises(ValueError):
                    decode_uprising_target(ident, bad)

    def test_hunters_mark_amplifies_allied_hits_and_clears_when_target_hides(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        accounts = [dict(id=1, nickname='Hunter', team=1), dict(id=2, nickname='Target', team=2),
                    dict(id=3, nickname='Ally', team=1)]
        arena = Arena(accounts, b'', terrain, now=1., solo=False, room_mode=MODE)
        self.addCleanup(arena.close)
        for account in arena.players:
            arena.ready(account, 1.)
        hunter, target, ally = tuple(arena.vehicle_for(account) for account in (1, 2, 3))
        slot = ConsumableState(uprising.HUNTERS_MARK)
        hunter.consumables = (slot,)
        with patch.object(arena, 'aim_hit', return_value=(target.entity_id, target.pose.position)):
            self.assertFalse(arena.use_consumable(1, slot.spec.ident, pb_blob(1, pb_int(1, ally.entity_id)), 1.))
            self.assertFalse(slot.uses)
            self.assertTrue(arena.use_consumable(1, slot.spec.ident, pb_blob(1, pb_int(1, target.entity_id)), 1.))
        effects = ((5, 5, 1., 11.),)
        message = vehicle_client_effects_message(arena, target.entity_id, effects)
        self.assertIn(message, event_messages(arena, arena.players[1], arena.drain_events()))

        shell = Shell(1, 100., 100., (200., 200.), 0., 70., 600., 0., 500.)
        impact = Impact(2., 1, ShotResult('penetrated', None, 100), target.pose.position,
                        None, (0., 0., 1.), ally.entity_id, target.entity_id, shell)
        hp = target.health
        arena._apply_impact(impact, impact.result, target.entity_id, direct=True, shell=shell)
        self.assertEqual(target.health, hp - 160)
        self.assertEqual(ally.damage, 160)
        self.assertEqual(hunter.damage, 0)

        target.mad_cloaked = True
        arena._advance_uprising_targeted(3., body_states(arena.vehicles))
        self.assertFalse(arena.uprising.marks)
        self.assertFalse(arena.uprising.exposes(hunter.entity_id, 3.))
        cleared = vehicle_client_effects_message(arena, target.entity_id, ())
        self.assertIn(cleared, event_messages(arena, arena.players[1], arena.drain_events()))

    def test_curse_validates_range_and_counts_path_distance_until_expiry(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='Hunter'), dict(id=2, nickname='Target')],
                      b'', terrain, now=1., solo=False, room_mode=MODE)
        self.addCleanup(arena.close)
        for account in arena.players:
            arena.ready(account, 1.)
        hunter, target = arena.vehicle_for(1), arena.vehicle_for(2)
        target.pose = Pose((0., 0., 0.), 0.)
        slot = ConsumableState(uprising.CURSE)
        hunter.consumables = (slot,)
        with patch.object(arena, '_ability_ray', return_value=((0., 0., -1.), (0., 0., 1.))):
            with patch.object(arena, 'aim_hit', return_value=(target.entity_id, (0., 0., 100.))):
                self.assertFalse(arena.use_consumable(1, slot.spec.ident, b'', 1.))
            self.assertEqual(slot.uses, 0)
            with patch.object(arena, 'aim_hit', return_value=(target.entity_id, target.pose.position)):
                self.assertTrue(arena.use_consumable(1, slot.spec.ident, b'', 1.))
        started = provision_effect_message(target.entity_id, slot.spec.compact_id)
        self.assertIn(started, event_messages(arena, arena.players[2], arena.drain_events()))

        hp = target.health
        pulse = int(target.max_health * .1 + .5)
        for now, x, expected in ((2., 6., hp), (3., 2., hp-pulse), (4., -2., hp-pulse),
                                 (5., -8., hp-2*pulse), (11., -20., hp-2*pulse)):
            before = body_states(arena.vehicles)
            target.pose = Pose((x, 0., 0.), 0.)
            arena._advance_uprising_targeted(now, before)
            self.assertEqual(target.health, expected)
        self.assertEqual(hunter.damage, 2*pulse)
        self.assertFalse(arena.uprising.curses)
        ended = provision_effect_message(target.entity_id, slot.spec.compact_id, argument=1)
        self.assertIn(ended, event_messages(arena, arena.players[2], arena.drain_events()))

    def test_dust_devil_turns_only_if_new_orientation_is_clear(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='Hunter'), dict(id=2, nickname='Target')],
                      b'', terrain, now=1., solo=False, room_mode=MODE)
        self.addCleanup(arena.close)
        for account in arena.players:
            arena.ready(account, 1.)
        hunter, target = arena.vehicle_for(1), arena.vehicle_for(2)
        original = Pose((12., 0., 25.), 0.)
        target.pose = original
        target.speed = 3.
        slot = ConsumableState(uprising.TORNADO)
        hunter.consumables = (slot,)
        motion = Motion(velocity=(0., 0., 3.))
        world = Mock(dynamic_bodies={}, motion={target.entity_id: motion})
        world._dynamic_rotation.return_value = None
        with patch.object(arena, 'world', world), patch.object(
                arena, 'aim_hit', return_value=(target.entity_id, target.pose.position)):
            world.training_placement.return_value = None
            self.assertFalse(arena.use_consumable(1, slot.spec.ident, b'', 1.))
            self.assertEqual(slot.uses, 0)
            self.assertEqual(target.pose, original)
            world.training_placement.side_effect = lambda entity, x, z, yaw, **_: Pose((x, 0., z), yaw)
            self.assertTrue(arena.use_consumable(1, slot.spec.ident, b'', 1.))
        self.assertEqual(target.pose.position, original.position)
        self.assertGreaterEqual(abs(target.pose.yaw), math.radians(120.))
        self.assertLessEqual(abs(target.pose.yaw), math.pi)
        world.relocate_training_body.assert_called_once_with(target.entity_id, target.pose, motion=Motion())
        self.assertEqual(target.speed, 0.)
        self.assertEqual(slot.ready_at, 41.)
        self.assertTrue(any(event.kind == 'orientation_rebased' for event in arena.drain_events()))

    def test_time_jump_restores_an_old_pose_health_and_track_state(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='Jumper'), dict(id=2, nickname='Opponent')],
                      b'', terrain, now=1., solo=False, room_mode=MODE)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        arena.ready(2, 1.)
        tank = arena.vehicle_for(1)
        original = Pose((12., 0., 25.), .1)
        tank.pose = original
        hp = tank.health
        slot = ConsumableState(uprising.TIME_JUMP)
        tank.consumables = (slot,)
        part = 'chassis_track_L'
        track = TrackState(TrackSpec(100., 60., 25., 10.))
        track.damage(25., 1.)
        tank.tracks[part] = track
        world = Mock(motion={tank.entity_id: Motion(velocity=(0., 0., 2.))})
        world.can_resume_pose.return_value = True
        with patch.object(arena, 'world', world):
            arena._record_uprising_rewinds(1.)
            tank.pose = Pose((14., 0., 31.), .6)
            tank.health -= 200
            track.damage(90., 3.)
            self.assertTrue(track.broken)
            self.assertFalse(arena._activate_uprising_time_jump(tank, slot, 4.9))
            world.can_resume_pose.return_value = False
            self.assertFalse(arena._activate_uprising_time_jump(tank, slot, 5.))
            self.assertEqual(slot.uses, 0)
            world.can_resume_pose.return_value = True
            self.assertTrue(arena._activate_uprising_time_jump(tank, slot, 5.))
        self.assertEqual(tank.pose, original)
        self.assertEqual(tank.health, hp)
        self.assertEqual(tank.tracks[part].health, 75.)
        self.assertFalse(tank.tracks[part].broken)
        self.assertEqual(tank.tracks[part].last_damage_at, 5.)
        world.relocate_training_body.assert_called_once_with(tank.entity_id, original, motion=world.motion[tank.entity_id])
        self.assertEqual(slot.ready_at, 45.)

    def test_play_dead_absorbs_one_shell_without_spending_a_life(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='Tank'), dict(id=2, nickname='Opponent')],
                      b'', terrain, now=1., solo=False, room_mode=MODE)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        arena.ready(2, 1.)
        tank, enemy = arena.vehicle_for(1), arena.vehicle_for(2)
        slot = ConsumableState(uprising.PLAY_DEAD)
        tank.consumables = (slot,)
        self.assertTrue(arena.use_consumable(1, slot.spec.ident, b'', 1.))
        false_death = vehicle_property(tank.entity_id, 1, b'\0')
        self.assertIn(false_death, reconnect_message_parts(arena, arena.players[2]))
        self.assertNotIn(false_death, reconnect_message_parts(arena, arena.players[1]))
        hp = tank.health
        self.assertTrue(arena._uprising_blocks_shell(tank, 5, 2.))  # First contact can be a track.
        self.assertEqual(slot.state_at(2.).state, COOLDOWN)
        shell = Shell(1, 100., 100., (200., 200.), 0., 70., 600., 0., 500.)
        result = ShotResult('penetrated', None, 100)
        for shot in (5, 6):
            event = Impact(2., shot, result, tank.pose.position, None, (0., 0., 1.),
                           enemy.entity_id, tank.entity_id, shell)
            arena._apply_impact(event, result, tank.entity_id, direct=True, shell=shell)
            self.assertEqual(tank.health, hp if shot == 5 else hp - 100)
        self.assertEqual(arena.uprising.remaining(tank.entity_id), 3)
        events = arena.drain_events()
        start = (vehicle_property(tank.entity_id, 3, b'\0\0')
                 + vehicle_property(tank.entity_id, 1, b'\0') + health_changed(tank.entity_id, 0, 0))
        end = (vehicle_property(tank.entity_id, 3, struct.pack('<h', hp))
               + vehicle_property(tank.entity_id, 1, b'\1') + health_changed(tank.entity_id, hp, 0))
        opponent_view = event_messages(arena, arena.players[2], events)
        self.assertIn(start, opponent_view)
        self.assertIn(end, opponent_view)
        self.assertNotIn(start, event_messages(arena, arena.players[1], events))

    def test_vehicle_abilities_apply_moonwalk_and_battle_trance(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='One'), dict(id=2, nickname='Two')],
                      b'', terrain, now=1., solo=False, room_mode=MODE)
        self.addCleanup(arena.close)
        for account in arena.players:
            arena.ready(account, 1.)
        tank, enemy = arena.vehicle_for(1), arena.vehicle_for(2)
        tank.definition = SimpleNamespace(tier=8)
        with patch.object(uprising, 'vehicle_abilities', return_value=('moonwalkHellgames',)) as roles:
            arena._install_uprising_ability(tank)
        roles.assert_called_once_with(None, tank.definition, mode='hellgames')
        tank.definition = None
        moonwalk = tank.consumables[3]
        self.assertIs(moonwalk.spec, uprising.MOONWALK)
        spawn_cloak = ConsumableState(RESPAWN_CLOAK)
        spawn_cloak.activate(1.)
        tank.consumables += (spawn_cloak,)
        self.assertTrue(arena._activate_consumable(tank, moonwalk, 2.))
        self.assertEqual(tank.engine_factor, 4.)
        self.assertAlmostEqual(tank.forward_speed_bias, 30. / 3.6)
        arena._consumables(tank, 11.)
        self.assertTrue(mad_games.concealed(tank, enemy.team))
        self.assertEqual(moonwalk.state_at(11.).state, ACTIVE)
        arena._end_vehicle_cloak(tank, 11.1)
        self.assertFalse(mad_games.concealed(tank, enemy.team))
        self.assertEqual(moonwalk.state_at(11.1).state, COOLDOWN)
        self.assertEqual(tank.engine_factor, 1.)

        tank.consumables = ()
        tank.definition = SimpleNamespace(tier=8)
        with patch.object(uprising, 'vehicle_abilities', return_value=('berserkHellgames',)):
            arena._install_uprising_ability(tank)
        tank.definition = None
        trance = tank.consumables[3]
        self.assertIs(trance.spec, uprising.BERSERK)
        self.assertTrue(arena._activate_consumable(tank, trance, 12.))
        self.assertAlmostEqual(tank.reload_speed_factor, 1. / .7)
        shell = Shell(1, 100., 100., (200., 200.), 0., 70., 600., 0., 500.)
        _, _, fired_shell = arena._launch(tank, shell, 12.)
        self.assertEqual(fired_shell.damage, shell.damage * 1.5)
        arena._consumables(tank, 27.)
        self.assertEqual(tank.reload_speed_factor, 1.)
        _, _, fired_shell = arena._launch(tank, shell, 27.)
        self.assertEqual(fired_shell.damage, shell.damage)

    def test_retreating_bot_uses_shadow_and_delays_fire_until_it_stops_retreating(self):
        arena = Arena([dict(id=1, nickname='One')], b'',
                      SimpleNamespace(height=lambda x, z: 0.), now=1., solo=True,
                      training=TrainingConfig(mode='bots', battle_mode=MODE))
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        opponent = arena.vehicle_for(1)
        bot = next(v for v in arena.vehicles.values() if v.team != opponent.team)
        slot = ConsumableState(uprising.MOONWALK)
        bot.consumables = (slot,)
        bot.health = bot.max_health // 2
        bot.reload_at = 5.
        arena._training_bot_uprising_mobility(bot, opponent, 2.)
        self.assertEqual(slot.uses, 0)
        bot.flags = TrainingBot.REVERSE
        arena._training_bot_uprising_mobility(bot, opponent, 2.)
        self.assertTrue(bot.mad_cloaked)
        self.assertEqual(slot.uses, 1)
        bot.reload_at = 0.
        ai = Mock(shot_check_due=Mock(return_value=True), sight_confirmed=Mock(return_value=True))
        with (patch.object(arena, 'world', object()),
              patch.object(arena, 'aim_hit', return_value=(opponent.entity_id, opponent.pose.position)),
              patch.object(arena, '_fire_vehicle') as fire):
            arena._training_bot_fire(bot, opponent, 3., ai)
            fire.assert_not_called()
            bot.flags = 0
            arena._training_bot_fire(bot, opponent, 3., ai)
            fire.assert_called_once_with(bot, 3.)

    def test_team_waiting_for_respawn_survives_until_the_third_death(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        for solo in (False, True):
            with self.subTest(solo=solo):
                accounts = [dict(id=1, nickname='One')]
                if not solo:
                    accounts.append(dict(id=2, nickname='Two'))
                settings = dict(training=TrainingConfig(mode='bots', battle_mode=MODE, minutes=0)) if solo else {
                    'room_mode': MODE, 'room_settings': RoomBattleSettings(unlimited_lives=True, minutes=0)}
                arena = Arena(accounts, b'', terrain, now=1., solo=solo, **settings)
                self.addCleanup(arena.close)
                for player in arena.players.values():
                    arena.ready(player.account_id, 1.)
                source = next(v for v in arena.vehicles.values() if v.team == 2)
                tank = arena.vehicle_for(1)
                self.assertEqual(arena.uprising.remaining(tank.entity_id), 3)
                self.assertFalse(tank.mad_cloaked)
                tank.damage = 33
                pose = Pose((1., 0., 2.), 0.)
                weapon = SimpleNamespace(shells=(), ammunition=(), ammo=564, clip=4, reload=6.)
                scene = SimpleNamespace(armor=SimpleNamespace(health=tank.max_health),
                                        definition=SimpleNamespace(aiming=None, weapon=weapon))
                world = Mock()
                world.training_placement.return_value = pose
                world.assets_for.return_value = scene
                for life in (2, 1, 0):
                    now = float(1 + 20 * (3 - life))
                    arena._damage_health(tank, tank.max_health, source.entity_id, now, shell_hit=False)
                    self.assertEqual(arena.uprising.remaining(tank.entity_id), life)
                    events = arena.drain_events()
                    killed = next(event for event in events if event.kind == 'killed')
                    self.assertEqual(killed.data['keep_roster'], life > 0)
                    self.assertEqual(arena.respawn_at.get(tank.entity_id), now + CHOICE_SECONDS if life else None)
                    player = arena.players[1]
                    timer = uprising_respawn_time_message(CHOICE_SECONDS)
                    self.assertEqual(timer in event_messages(arena, player, events), life > 0)
                    arena._fixed_step(now)
                    if life:
                        self.assertFalse(arena.finished_at)
                        generation = tank.generation
                        with patch.object(arena, 'world', world):
                            arena.simulation_time = now + CHOICE_SECONDS
                            arena._advance_respawns(arena.simulation_time)
                        tank = arena.vehicle_for(1)
                        self.assertEqual(tank.pose, pose)
                        self.assertEqual(tank.health, tank.max_health)
                        self.assertEqual(tank.generation, generation + 1)
                        self.assertEqual(tank.damage, 33)
                        self.assertEqual(arena.uprising.remaining(tank.entity_id), life)
                        events = arena.drain_events()
                        self.assertIn(uprising_respawn_time_message(0.),
                                      event_messages(arena, player, events))
                        effect = provision_effect_message(tank.entity_id, RESPAWN_CLOAK.compact_id,
                                                          argument=1)
                        indicator = struct.pack('<BBdf', 94, ACTIVE, arena.simulation_time - arena.epoch, 10.)
                        payload = event_messages(arena, player, events)
                        self.assertIn(effect, payload)
                        self.assertIn(indicator, payload)
                        if not solo:
                            other_payload = event_messages(arena, arena.players[2], events)
                            self.assertNotIn(effect, other_payload)
                            self.assertNotIn(indicator, other_payload)
                            reconnect = reconnect_message_parts(arena, player)
                            self.assertIn(effect, reconnect)
                            self.assertIn(indicator, b''.join(reconnect))
                            self.assertNotIn(effect, reconnect_message_parts(arena, arena.players[2]))
                        self.assertTrue(mad_games.concealed(tank, source.team))
                        self.assertFalse(mad_games.concealed(tank, tank.team))
                        self.assertAlmostEqual(tank.engine_factor, 4.)
                        self.assertAlmostEqual(tank.forward_speed_bias, 20. / 3.6)
                        self.assertEqual(arena._drive_speed_factor(tank), 1.)
                        self.assertEqual(sum(slot.spec is RESPAWN_CLOAK for slot in tank.consumables
                                             if slot), 1)
                        self.assertFalse(arena.equipment_state(tank, now + CHOICE_SECONDS))
                        if life == 2 and solo:
                            projectile = SimpleNamespace(trace_effect=0)
                            shell = SimpleNamespace(gravity=9.81, compact_id=1, missile=None)
                            with patch.dict(arena.projectiles, {arena.next_shot: projectile}):
                                arena._emit_shot(tank, shell, pose.position, (0., 0., 1.), now + CHOICE_SECONDS + .1, 1)
                        elif life == 2:
                            arena._consumables(tank, now + CHOICE_SECONDS + 9.99)
                            self.assertTrue(tank.mad_cloaked)
                            arena._consumables(tank, now + CHOICE_SECONDS + 10.)
                        else:
                            arena._damage_health(tank, 1, source.entity_id, now + CHOICE_SECONDS + .1, shell_hit=False)
                        self.assertFalse(tank.mad_cloaked)
                        self.assertEqual(tank.engine_factor, 1.)
                        self.assertEqual(tank.forward_speed_bias, 0.)
                        self.assertTrue(mad_games.concealed(tank, source.team))
                        source.pose = Pose((pose.position[0], pose.position[1], pose.position[2] + 20.), 0.)
                        arena._update_mad_spotting((source,), now + CHOICE_SECONDS + 10., (tank,))
                        events = arena.drain_events()
                        self.assertFalse(next(event for event in events
                                              if event.kind == 'mad_cloak').data['enabled'])
                        self.assertIn(provision_effect_message(tank.entity_id, RESPAWN_CLOAK.compact_id),
                                      event_messages(arena, player, events))
                        self.assertIn(struct.pack('<BB', 94, COOLDOWN), event_messages(arena, player, events))
                    else:
                        self.assertEqual(arena.winner, 2)
                        self.assertEqual(arena.finish_reason, 'elimination')

    def test_solo_victory_finishes_after_last_opponent_runs_out_of_lives(self):
        arena = Arena([dict(id=1, nickname='One')], b'',
                      SimpleNamespace(height=lambda x, z: 0.), now=1., solo=True,
                      training=TrainingConfig(mode='bots', battle_mode=MODE, minutes=0))
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        player = arena.vehicle_for(1)
        for bot in arena.vehicles.values():
            if bot.team != player.team:
                for _ in range(uprising.STARTING_LIVES - 1):
                    arena.uprising.destroyed(bot.entity_id)
                arena._damage_health(bot, bot.health, player.entity_id, 2., shell_hit=False)
        arena._fixed_step(2.)
        self.assertEqual((arena.winner, arena.finish_reason, arena.finished_at), (1, 'elimination', 2.))
