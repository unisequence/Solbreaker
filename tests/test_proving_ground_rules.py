"""Authoritative room isolation, range target ownership and collision layers."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import numpy as np
import pybullet as bullet

from solbreaker import proving_ground_rules as rules, sandbox
from solbreaker.arena import Arena
from solbreaker.ballistics import Projectile, advance
from solbreaker.vehicle_data import Shell
from solbreaker.battle import (allied_reload_message, arena_update, event_messages, pb_blob, pb_int,
                               reconnect_message_parts, team_kill_score)
from solbreaker.collision import CollisionWorld, Destructible, Pose
from solbreaker.consumables import COOLDOWN, READY
from solbreaker.training_config import RoomBattleSettings


class ProvingGroundTests(TestCase):
    def room(self, teams):
        spec = SimpleNamespace(key=rules.MAP_KEY, map_id=107,
                               spawn=lambda t, i: (10.*i, 20.*t), yaw=lambda t, i: 0.)
        arena = Arena([dict(id=i+1, nickname=f'Pilot{i+1}', team=t) for i, t in enumerate(teams)],
                      b'', SimpleNamespace(height=lambda x,z: 0.), map_spec=spec,
                      solo=False, now=1., room_settings=RoomBattleSettings())
        self.addCleanup(arena.close)
        for player in arena.players.values():
            arena.ready(player.account_id, 1.)
        return arena

    def test_target_capacity_depends_on_the_actual_room_roster(self):
        empty = self.room([1]*7+[2]*7)
        self.assertEqual(len(empty.vehicles), 14)
        self.assertFalse(empty.practice_slots)
        self.assertTrue(empty.practice)
        self.assertTrue(empty.sandbox_enabled)
        self.assertTrue(empty.endless)
        empty.world = Mock()
        empty._spawn_practice_target(empty.vehicle_for(1))
        self.assertEqual(empty.events[-1].data['notice'], sandbox.TARGET_SLOTS_FULL)
        single_team = self.room([1,1,1])
        self.assertEqual(len(single_team.practice_slots), 7)

    def test_solo_ability_slots_route_to_installed_items_and_keep_separate_cooldowns(self):
        spec = SimpleNamespace(key=rules.MAP_KEY, map_id=107,
                               spawn=lambda team, slot: (0., 0.), yaw=lambda team, slot: 0.)
        arena = Arena([dict(id=1, nickname='Pilot')], b'',
                      SimpleNamespace(height=lambda x, z: 0.), now=1., map_spec=spec)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        vehicle = arena.vehicle_for(1)
        self.assertEqual(vehicle.consumables[:3], (None, None, None))
        self.assertEqual(tuple(slot.spec for slot in vehicle.consumables[3:]), rules.ABILITIES)

        with patch.object(arena, 'use_consumable') as use:
            for slot, ability in enumerate(rules.ABILITIES):
                control = sandbox.Control.decode(sandbox.INPUT.pack(
                    sandbox.token(arena), slot + 1, sandbox.USE_ABILITY, slot, 0., 0., 0.))
                arena.sandbox_control(1, control)
                use.assert_called_with(1, ability.ident, now=1.)
        with self.assertRaises(ValueError):
            sandbox.Control.decode(sandbox.INPUT.pack(
                sandbox.token(arena), 4, sandbox.USE_ABILITY, 3, 0., 0., 0.))

        jump = vehicle.consumables[3]
        self.assertTrue(jump.activate(1.))
        payload = b''.join(reconnect_message_parts(arena, arena.players[1]))
        self.assertIn(sandbox.ability_slot_message(arena, 0, COOLDOWN, 20), payload)
        self.assertIn(sandbox.ability_slot_message(arena, 1, READY, 0), payload)
        self.assertIn(sandbox.ability_slot_message(arena, 2, READY, 0), payload)

    def test_reload_updates_ignore_uncreated_slots_and_follow_replacement_entity(self):
        import struct

        arena = self.room([1, 1, 2])
        player = arena.players[1]
        ally = arena.vehicle_for(2)
        target = next(arena.vehicles[entity] for entity in sorted(arena.practice_slots)
                      if arena.vehicles[entity].team == player.team)
        for vehicle in (ally, target):
            vehicle.reload_at = arena.simulation_time + 3.
            vehicle.clip = 1

        def expected(entity):
            row = pb_int(1, entity) + pb_int(2, 4) + b'\x1d' + struct.pack('<f', 3.) + pb_int(4, 1)
            return arena_update(15, pb_blob(14, pb_blob(1, row)))

        def reload_events():
            arena.drain_events()
            arena.emit('reload', target.entity_id, delay=3.)
            arena.emit('reload', ally.entity_id, delay=3.)
            return event_messages(arena, player, arena.drain_events())

        ally_message, original_id = expected(ally.entity_id), expected(target.entity_id)
        self.assertEqual(allied_reload_message(arena, player, ally), ally_message)
        ready = event_messages(arena, player, arena.drain_events())
        self.assertIn(ally_message, ready)
        for payload in (ready, reload_events(), b''.join(reconnect_message_parts(arena, player))):
            self.assertIn(ally_message, payload)
            self.assertNotIn(original_id, payload)

        arena.practice_spawned.add(target.entity_id)
        self.assertIn(original_id, reload_events())
        replaced = sandbox.PRACTICE_ENTITY_BASE
        arena.practice_client_ids[target.entity_id] = replaced
        for payload in (reload_events(), b''.join(reconnect_message_parts(arena, player))):
            self.assertIn(expected(replaced), payload)
            self.assertNotIn(original_id, payload)
            self.assertIn(ally_message, payload)

    def test_two_operators_spawn_and_manage_targets_independently(self):
        arena = self.room([1,2])
        first, second = arena.vehicle_for(1), arena.vehicle_for(2)
        first.gun.set_point((360., 0., 0.), False)
        second.gun.set_point((280., 0., 0.), False)
        world = Mock(assets=SimpleNamespace(bounds=(-500., -500., 500., 500.)))
        world.training_placement.side_effect = lambda entity, x, z, yaw: Pose((x, 0., z), yaw)
        arena.world = world

        def restore(entity, pose):
            vehicle = arena.vehicles[entity]
            vehicle.pose, vehicle.health = pose, vehicle.max_health
            return vehicle

        with patch.object(arena, '_restore_vehicle', side_effect=restore):
            arena._toggle_practice_return_fire(first)
            arena._spawn_practice_target(first)
            arena._spawn_practice_target(second)
            owned_a, = arena._owned_targets(first)
            owned_b, = arena._owned_targets(second)
            self.assertEqual(arena.vehicles[owned_a].team, 2)
            self.assertEqual(arena.vehicles[owned_b].team, 1)
            self.assertEqual(set(arena.practice_gunners), {owned_a})
            self.assertEqual(set(arena.proving_ground.routes), {owned_a})
            self.assertEqual(next(arena._practice_gunner_pairs())[2], first)
            with patch.object(arena, '_ability_ray', return_value=None), \
                 patch.object(arena, 'aim_hit', return_value=(owned_a, (360.,0.,0.))):
                arena._practice_menu_action(second, sandbox.MENU_REMOVE)
            self.assertEqual(arena._owned_targets(first), {owned_a})
            arena._clear_practice_targets(first)
            self.assertFalse(arena._owned_targets(first))
            self.assertFalse(arena.proving_ground.routes)
            self.assertTrue(arena.vehicles[owned_b].alive)
            arena._spawn_practice_target(first)
            self.assertEqual(arena._owned_targets(first), {owned_a})

    def test_shell_terminates_at_first_boundary_even_when_both_endpoints_are_outside(self):
        ground = rules.ProvingGround()
        ground.pvp_shots.add(12)
        # One integration substep crosses both sides of the 250 m diameter.
        shell = Shell(1, 100., 150., (100., 100.), 0., 1., 50000., 0., 1500.)
        tests = [(11, (120.,10.,-295.), (50000.,0.,0.), 175.),
                 (12, (300.,10.,-295.), (50000.,0.,0.), 425.),
                 (11, (176.,10.,-295.), (50000.,0.,0.), 176.)]
        for shot, origin, velocity, expected in tests:
            projectile = Projectile(shot, 200, shell, origin, velocity, 1.)
            impact = advance(projectile, None, {}, {}, 1./60., None, segment_limit=ground.shot_limit)
            self.assertAlmostEqual(impact.point[0], expected)
            self.assertFalse(impact.collision)
            self.assertIsNone(impact.obstacle)
        self.assertIsNone(ground.shot_limit(11, (120.,0.,-170.), (400.,0.,0.)))
        self.assertIsNone(ground.shot_limit(12, (300.,0.,-295.), (0.,10.,0.)))

    def test_cross_zone_projectiles_ram_kills_and_respawn(self):
        arena = self.room([1,2])
        a, b = arena.vehicle_for(1), arena.vehicle_for(2)
        a.pose = Pose((300., 0., -295.), 0.)
        self.assertFalse(arena.proving_ground.can_hit(a,b))
        self.assertEqual(arena._damage_health(b, 100, a.entity_id, 1., shell_hit=False, kinetic=True), 0)
        b.pose = Pose((300., 0., -300.), 0.)
        self.assertFalse(arena.proving_ground.can_hit(a,b,17))
        arena.proving_ground.pvp_shots.add(17)
        self.assertTrue(arena.proving_ground.can_hit(a,b,17))
        killed = arena._damage_health(b, b.health, a.entity_id, 1., shell_hit=True)
        self.assertGreater(killed, 0)
        self.assertEqual(arena.proving_ground.score, {1:1, 2:0})
        self.assertFalse(arena.finished_at)
        self.assertIn(b.entity_id, arena.respawn_at)
        score = team_kill_score(arena, arena.players[1])
        self.assertIn(b'TFS1FRAG\x01\0\0\0\0\0\0\0', score)
        arena.world = Mock()
        arena.world.training_placement.return_value = None
        arena._advance_respawns(5.)
        self.assertEqual(len(arena.world.training_placement.call_args_list), 7)
        for call in arena.world.training_placement.call_args_list:
            _, x, z, _ = call.args
            self.assertTrue(rules.in_pvp((x, 0., z)))

    def test_series_keeps_misses_and_counts_each_shell_contact_only_once(self):
        arena = self.room([1, 2])
        first = arena.vehicle_for(1)
        first.pose = Pose((280., 0., 0.), 0.)
        ground = arena.proving_ground
        for ident in (1001,1002,1003):
            ground.fired(first, ident)
        series = ground.series[first.entity_id]
        series.contact(1001, direct=True, penetrated=False, damage=0)  # Track then hull.
        series.contact(1001, direct=True, penetrated=True, damage=70)
        series.contact(1002, direct=False, penetrated=False, damage=13)  # Splash.
        ground.finish_frame({})
        arena.world = Mock()
        arena._clear_practice_targets(first)
        result = next(event for event in arena.events if event.kind == 'proving_range_series')
        self.assertEqual(result.actor, first.entity_id)
        self.assertEqual(result.data, dict(shots=3, hits=1, penetrations=1, damage=83))
        self.assertFalse(ground.series)
        self.assertFalse(series.live)

    def test_service_pad_requires_a_grounded_stop(self):
        arena = self.room([1,2])
        player = arena.vehicle_for(1)
        player.pose = Pose((69., 32., -45.), 0.)
        player.health -= 5
        arena.world = Mock(motion={v.entity_id: SimpleNamespace(grounded=False) for v in arena.vehicles.values()})
        arena._advance_practice(2.)
        arena._advance_practice(4.)
        self.assertLess(player.health, player.max_health)
        arena.world.motion[player.entity_id].grounded = True
        arena._advance_practice(4.5)
        self.assertLess(player.health, player.max_health)
        arena._advance_practice(6.5)
        self.assertEqual(player.health, player.max_health)
        self.assertEqual(arena.events[-1].kind, 'sandbox_restore_hp')

    def test_route_reverses_at_both_lane_ends(self):
        arena = self.room([1,2])
        shooter = arena.vehicle_for(1)
        target = arena.vehicles[min(arena.practice_slots)]
        target.pose = Pose((360., 0., -40.), 0.)
        arena.proving_ground.placed(target, shooter)
        route = arena.proving_ground.routes[target.entity_id]
        self.assertEqual(route.flags(target, 3.), route.driver.FORWARD)
        target.pose = Pose((360., 0., 46.), 0.)
        self.assertTrue(route.flags(target, 5.) & (route.driver.LEFT | route.driver.RIGHT))
        self.assertEqual(route.end_z, -48.)

    def test_sector_reset_requires_reload_and_clears_only_unoccupied_western_geometry(self):
        arena = self.room([1,2])
        world = CollisionWorld.__new__(CollisionWorld)
        cid = world.cid = bullet.connect(bullet.DIRECT)
        self.addCleanup(bullet.disconnect, cid)
        world.collision_zone = rules.in_pvp
        world._native_physics = None
        world.terrain = arena.terrain
        world.destroyed, world.destruction_events = set(), []
        world.object_bodies, world.breakable_bodies, world.body_entities = {}, {}, {}
        world.map_bodies, world.dynamic_bodies = set(), {}
        world.object_health, props = {}, {}
        for key, x in ((1, -290.), (2, 300.)):
            obj = Destructible(key, 'fence', 3, 15,
                               np.array(((x-2.,0.,-2.), (x+2.,4.,2.))), np.empty(0))
            props[key] = obj
            shape = bullet.createCollisionShape(bullet.GEOM_BOX, halfExtents=(2., 2., 2.),
                                                collisionFramePosition=(x, 2., 0.), physicsClientId=cid)
            body = bullet.createMultiBody(0, shape, physicsClientId=cid)
            world.object_bodies[key], world.object_health[key] = {body}, obj.health
            world.breakable_bodies[body] = key
            world.map_bodies.add(body)
        world.assets = SimpleNamespace(destructibles=props)
        for key in props:
            self.assertTrue(world.destroy(key, (1., 0.)))
            arena.destructions[key] = (0.,0.)
        self.assertLess(bullet.rayTest((-290,10,0), (-290,-5,0), physicsClientId=cid)[0][0], 0)
        with patch.object(arena, 'world', world):
            arena.hangar_players.add(1)
            arena._restore_range_for_reentry()
            self.assertEqual(world.destroyed, {1,2})
            arena.players[2].attached = False
            arena.players[2].departure_reason = 'disconnect'
            arena._restore_range_for_reentry()
            self.assertEqual(world.destroyed, {1,2})
            arena.players[2].attached = True
            arena.hangar_players.add(2)
            shape = bullet.createCollisionShape(bullet.GEOM_BOX, halfExtents=(1.,1.,1.), physicsClientId=cid)
            parked = bullet.createMultiBody(0, shape, basePosition=(-290.,1.,0.), physicsClientId=cid)
            world.body_entities[parked] = 201
            arena._restore_range_for_reentry()
            self.assertEqual(world.destroyed, {1,2})
            bullet.resetBasePositionAndOrientation(parked, (10,1,0), (0,0,0,1), physicsClientId=cid)
            arena._restore_range_for_reentry()
            self.assertEqual(world.destroyed, {2})
            self.assertEqual(set(arena.destructions), {2})
            self.assertEqual(world.object_health, {1:15, 2:0})
            self.assertGreaterEqual(bullet.rayTest((-290,10,0), (-290,-5,0), physicsClientId=cid)[0][0], 0)
            self.assertTrue(world.destroy(1, (1., 0.)))

    def test_bullet_separates_cross_boundary_bodies_but_preserves_in_zone_contact(self):
        world = CollisionWorld.__new__(CollisionWorld)
        world.cid = bullet.connect(bullet.DIRECT)
        self.addCleanup(bullet.disconnect, world.cid)
        world.collision_zone = rules.in_pvp
        world.suspended_entities = set()
        world._body_zones = {}
        shape = bullet.createCollisionShape(bullet.GEOM_BOX, halfExtents=(1.,1.,1.), physicsClientId=world.cid)
        inside, outside = (424.5, 1., -295.), (425.5, 1., -295.)
        hull = bullet.createMultiBody(0, shape, basePosition=inside, physicsClientId=world.cid)
        rolling = bullet.createMultiBody(1, shape, basePosition=outside, physicsClientId=world.cid)
        world.bodies = {1:hull, 2:rolling}
        world.poses = {1:Pose(inside, 0.), 2:Pose(outside, 0.)}
        world.dynamic_bodies = {2:SimpleNamespace(body=rolling)}
        world.refresh_collision_zones()
        bullet.performCollisionDetection(physicsClientId=world.cid)
        self.assertFalse(bullet.getContactPoints(hull, rolling, physicsClientId=world.cid))
        bullet.resetBasePositionAndOrientation(rolling, (424.8,1.,-295.), (0.,0.,0.,1.),
                                               physicsClientId=world.cid)
        world.refresh_collision_zones()
        bullet.performCollisionDetection(physicsClientId=world.cid)
        self.assertTrue(bullet.getContactPoints(hull, rolling, physicsClientId=world.cid))
