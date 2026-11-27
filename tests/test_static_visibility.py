"""Spotting and collision queries share the arena terrain and destructible bodies."""
from types import SimpleNamespace
import struct
import unittest
from unittest.mock import patch

import numpy as np
import pybullet as bullet

from solbreaker.arena import Arena
from solbreaker.arena_owner_game import GameArenaHandler
from solbreaker.battle import event_messages, reconnect_message_parts, recovery_warning_message
from solbreaker.collision import CollisionScene, CollisionWorld, Pose
from solbreaker.map_materials import MapEnvironment
from solbreaker.mesh_files import MeshFiles
from solbreaker.motion import vehicle_snapshot
from solbreaker.vehicle_data import SpottingProfile


class TeamSpottingTests(unittest.TestCase):
    def test_owner_motion_keeps_unspotted_teammates_visible(self):
        arena = Arena([dict(id=1, nickname='A', team=1), dict(id=2, nickname='B', team=2),
                       dict(id=3, nickname='C', team=1)],
                      b'', SimpleNamespace(height=lambda x, z: 0.), solo=False, now=1.)
        self.addCleanup(arena.close)
        snapshots = {}
        for account in (1, 2, 3):
            vehicle = arena.vehicle_for(account)
            vehicle.mad_unspotted = True
            pose = vehicle.pose
            snapshots[account] = vehicle_snapshot(vehicle.entity_id, pose.position,
                                                  pose.yaw, pose.pitch, pose.roll)
        arena._record_motion_frame(1.)
        owner = GameArenaHandler(arena, None)

        friendly, hostile = owner._motions((1, 2), arena.motion_frame)
        self.assertIn(snapshots[1], friendly.body)
        self.assertIn(snapshots[3], friendly.body)
        self.assertNotIn(snapshots[2], friendly.body)
        self.assertIn(snapshots[2], hostile.body)
        self.assertNotIn(snapshots[1], hostile.body)
        self.assertNotIn(snapshots[3], hostile.body)

    def test_visibility_rays_follow_range_and_refresh_after_fire_or_stop(self):
        arena = Arena([dict(id=1, nickname='A'), dict(id=2, nickname='B')],
                      b'', SimpleNamespace(height=lambda x, z: 0.), solo=False, now=1.)
        self.addCleanup(arena.close)
        observer, enemy = (arena.vehicle_for(account) for account in (1, 2))
        observer.definition = enemy.definition = SimpleNamespace(view_range=400.)
        observer.pose = Pose((0., 0., 0.), 0.)
        enemy.pose = Pose((0., 0., 320.), 0.)
        rays = []

        def trace(starts, ends):
            rays.extend(zip(starts, ends))
            return [(-1, -1, 1.)] * len(starts)

        with patch.object(arena, 'world', SimpleNamespace(_visibility_rays=trace)):
            for tick in range(20):
                arena._update_team_spotting(1. + tick / 10.)
            self.assertEqual(len(rays), 2)
            arena._update_team_spotting(3.)
            self.assertEqual(len(rays), 4)
            enemy.last_shot_at = 3.
            arena._update_team_spotting(3.1)
            self.assertEqual(len(rays), 5)
            enemy.spotting_last_motion_at = 3.2
            arena._update_team_spotting(3.3)
            self.assertEqual(len(rays), 6)
            enemy.pose = Pose((0., 0., 50.), 0.)
            arena._update_team_spotting(3.4)
            self.assertEqual(len(rays), 6)

    def test_vehicle_concealment_changes_spotting_range(self):
        arena = Arena([dict(id=1, nickname='A'), dict(id=2, nickname='B')],
                      b'', SimpleNamespace(height=lambda x, z: 0.), solo=False, now=1.)
        self.addCleanup(arena.close)
        observer, enemy = (arena.vehicle_for(account) for account in (1, 2))
        observer.pose = Pose((0., 0., 0.), 0.)
        enemy.pose = Pose((0., 0., 180.), 0.)
        observer.definition = SimpleNamespace(view_range=250., optional_devices=(),
                                              spotting=SpottingProfile(optics_bonus=.11))
        enemy.definition = SimpleNamespace(view_range=250., optional_devices=(115,),
                                           spotting=SpottingProfile(still=.30, moving=.15,
                                                                   at_shot=.25, net_bonus=.05))
        enemy.spotting_last_motion_at = 1.
        arena._update_team_spotting(4.)
        self.assertTrue(enemy.mad_unspotted)
        observer.definition.optional_devices = (114,)
        arena._update_team_spotting(5.)
        self.assertFalse(enemy.mad_unspotted)
        observer.definition.optional_devices = ()
        arena._update_team_spotting(15.)
        self.assertTrue(enemy.mad_unspotted)
        enemy.spotting_moving = True
        arena._update_team_spotting(16.)
        self.assertFalse(enemy.mad_unspotted)
        enemy.spotting_moving = False
        enemy.spotting_last_motion_at = 16.
        enemy.last_shot_at = 25.9
        arena._update_team_spotting(26.)
        self.assertAlmostEqual(enemy.mad_spotted_until, 36.)
        arena._update_team_spotting(36.)
        self.assertTrue(enemy.mad_unspotted)

    def test_foliage_changes_detection_after_firing_and_destruction(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='A'), dict(id=2, nickname='B')],
                      b'', terrain, solo=False, now=1.)
        self.addCleanup(arena.close)
        observer, enemy = (arena.vehicle_for(account) for account in (1, 2))
        observer.pose, enemy.pose = Pose((0., 0., 0.), 0.), Pose((0., 0., 215.), 0.)
        observer.definition = enemy.definition = SimpleNamespace(view_range=250.)
        assets = CollisionScene.__new__(CollisionScene)
        assets.environment = MapEnvironment(foliage=[((-2., .1, 208.), (2., 3., 210.), 7)])
        world = SimpleNamespace(assets=assets, destroyed=set(),
                                _visibility_rays=lambda starts, ends: [(-1, -1, 1.)] * len(starts))
        with patch.object(arena, 'world', world):
            arena._update_team_spotting(1.)
            self.assertTrue(enemy.mad_unspotted)
            enemy.last_shot_at = 2.
            arena._update_team_spotting(2.)
            self.assertFalse(enemy.mad_unspotted)
            arena._update_team_spotting(17.)
            self.assertTrue(enemy.mad_unspotted)
            world.destroyed.add(7)
            arena._update_team_spotting(19.)
            self.assertFalse(enemy.mad_unspotted)

    def test_drowning_starts_at_full_hull_and_requires_continuous_immersion(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='A'), dict(id=2, nickname='B')],
                      b'', terrain, solo=False, now=1.)
        self.addCleanup(arena.close)
        tank, opponent = (arena.vehicle_for(account) for account in (1, 2))
        environment = SimpleNamespace(water=True, water_height=lambda x, z: 3. if x == 0. else None)
        world = CollisionWorld.__new__(CollisionWorld)
        world.assets = SimpleNamespace(environment=environment, tank_shapes=(
            ('hull', np.array(((-1., .5, -2.), (1., 2.5, 2.)))),
            ('turret', np.array(((-.5, 2.5, -.5), (.5, 4., .5)))),
        ))
        world.vehicle_assets = {}
        world.primary_id = tank.entity_id
        world.terrain = terrain
        world.dynamic_bodies = {}
        world.poses = {tank.entity_id: Pose((0., 1.5, 0.), 0.),
                       opponent.entity_id: Pose((10., 0., 0.), 0.)}

        class Water:
            def is_submerged(self, entity):
                return world.is_submerged(entity)

        with patch.object(arena, 'world', Water()):
            arena._update_drowning(.9, None)
            self.assertIsNone(tank.submerged_since)
            world.poses[tank.entity_id] = Pose((0., .5, 0.), 0.)
            arena._update_drowning(1., None)
            self.assertEqual(tank.submerged_since, 1.)
            arena._update_drowning(10.9, None)
            self.assertTrue(tank.alive)
            world.poses[tank.entity_id] = Pose((0., .51, 0.), 0.)
            arena._update_drowning(11., None)
            self.assertIsNone(tank.submerged_since)
            # On its side, a side of the hull becomes its upper edge.
            world.poses[tank.entity_id] = Pose((0., 2.01, 0.), 0., roll=np.pi / 2)
            arena._update_drowning(11.9, None)
            self.assertIsNone(tank.submerged_since)
            world.poses[tank.entity_id] = Pose((0., 1.99, 0.), 0., roll=np.pi / 2)
            arena._update_drowning(12., None)
            self.assertEqual(tank.submerged_since, 12.)
            arena._update_drowning(21.9, None)
            self.assertTrue(tank.alive)
            arena._update_drowning(22., None)
            self.assertFalse(tank.alive)
            self.assertEqual(tank.killer, 0)
            self.assertEqual(opponent.kills, 0)

    def test_recovery_warning_stays_visible_when_a_tipped_hull_slides(self):
        world = CollisionWorld.__new__(CollisionWorld)
        body = SimpleNamespace(stationary_time=.1)
        world.dynamic_bodies = {1: body}
        world.poses = {1: Pose((0., 0., 0.), 0., roll=1.57)}

        self.assertIsNone(world.auto_recovery_remaining(1))
        body.stationary_time = .3
        self.assertAlmostEqual(world.auto_recovery_remaining(1), 2.2)

        # A new stationary anchor restarts the countdown without hiding it.
        body.stationary_time = 0.
        world.poses[1] = Pose((.6, 0., 0.), 0., roll=1.)
        self.assertEqual(world.auto_recovery_remaining(1, showing=True), 2.5)
        self.assertIsNone(world.auto_recovery_remaining(1))
        world.poses[1] = Pose((.6, 0., 0.), 0., roll=.2)
        self.assertIsNone(world.auto_recovery_remaining(1, showing=True))
        world.dynamic_bodies.clear()
        self.assertIsNone(world.auto_recovery_remaining(1, showing=True))

    def test_private_recovery_and_stock_drowning_hud(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='A'), dict(id=2, nickname='B')],
                      b'', terrain, solo=False, now=1.)
        self.addCleanup(arena.close)
        tank = arena.vehicle_for(1)
        world = CollisionWorld.__new__(CollisionWorld)
        world.dynamic_bodies = {tank.entity_id: SimpleNamespace(stationary_time=.25)}
        world.poses = {tank.entity_id: Pose((0., 0., 0.), 0., roll=1.57)}
        arena.drain_events()

        def check(now, expected, packet):
            arena._update_hazard_warnings(now, None)
            events = arena.drain_events()
            self.assertEqual(tank.hazard_warning, expected)
            self.assertEqual(event_messages(arena, arena.players[1], events), packet)
            self.assertEqual(event_messages(arena, arena.players[2], events), b'')

        def stock_timer(state, seconds):
            return b'\x13\x4e' + struct.pack('<IBIf', tank.entity_id, 4, state, seconds)

        with patch.object(arena, 'world', world):
            check(1., ('recovery', 3), recovery_warning_message(3))
            arena._update_hazard_warnings(1.1, None)
            self.assertEqual(arena.drain_events(), [])
            world.dynamic_bodies[tank.entity_id].stationary_time = 1.75
            check(2.5, ('recovery', 1), recovery_warning_message(1))
            world.dynamic_bodies[tank.entity_id].stationary_time = 0.
            check(2.6, ('recovery', 3), recovery_warning_message(3))
            tank.submerged_since = 2.6
            check(2.6, ('drowning', 10), recovery_warning_message() + stock_timer(2, 10.))
            check(3.6, ('drowning', 9), b'')
            tank.submerged_since = None
            world.dynamic_bodies.clear()
            check(4., None, stock_timer(0, 0.))

        tank.hazard_warning = ('drowning', 7)
        message = stock_timer(2, 7.)
        self.assertIn(message, reconnect_message_parts(arena, arena.players[1]))
        self.assertNotIn(message, reconnect_message_parts(arena, arena.players[2]))

    def test_encounter_spotting_uses_proximity_cover_and_retention(self):
        arena = Arena([dict(id=1, nickname='A'), dict(id=2, nickname='B')],
                      b'', SimpleNamespace(height=lambda x, z: 0.), solo=False, now=1.)
        self.addCleanup(arena.close)
        observer, enemy = (arena.vehicle_for(account) for account in (1, 2))
        observer.pose = Pose((0., 0., 0.), 0.)
        enemy.pose = Pose((0., 0., 100.), 0.)
        blocked = (42, -1, .5)
        world = SimpleNamespace(_visibility_rays=lambda starts, ends: [blocked] * len(starts))
        with patch.object(arena, 'world', world):
            arena._update_team_spotting(1.)
            self.assertTrue(enemy.mad_unspotted)
            enemy.pose = Pose((0., 0., 50.), 0.)
            arena._update_team_spotting(2.)
            self.assertFalse(enemy.mad_unspotted)
            enemy.pose = Pose((0., 0., 100.), 0.)
            arena._update_team_spotting(4.9)
            self.assertFalse(enemy.mad_observation_warning)
            arena._update_team_spotting(5.)
            self.assertTrue(enemy.mad_observation_warning)
            arena._update_team_spotting(11.9)
            self.assertFalse(enemy.mad_unspotted)
            arena._update_team_spotting(12.)
            self.assertTrue(enemy.mad_unspotted)
            self.assertFalse(enemy.mad_observation_warning)


class StaticVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.meshes = MeshFiles()
        self.addCleanup(self.meshes.close)
        wall = SimpleNamespace(
            vertices=np.array(((3., 0., -2.), (3., 4., -2.),
                               (3., 4., 2.), (3., 0., 2.))),
            triangles=np.array(((0, 1, 2), (0, 2, 3)), dtype=np.int32))
        self.assets = SimpleNamespace(vertices=np.empty((0, 3)),
            triangles=np.empty((0, 3), dtype=np.int32),
            destructibles={7: wall}, mesh_files=self.meshes)
        world = self.world = CollisionWorld.__new__(CollisionWorld)
        world.cid = bullet.connect(bullet.DIRECT)
        self.addCleanup(bullet.disconnect, world.cid)
        world._mesh_body_paths = {}
        world._blocked_launches = {}
        world._native_physics = None
        world._ray_threads = 1
        world.collision_zone = None
        self.wall_body = world._mesh_body(self.meshes.mesh(wall.vertices, wall.triangles))
        vertices = np.array(((-10., 0., -10.), (0., 0., -10.),
                             (-10., 0., 0.), (0., 0., 0.)))
        triangles = np.array(((0, 2, 1), (1, 2, 3)), dtype=np.int32)
        world._mesh_body(self.meshes.mesh(vertices, triangles))

    def wall_ray(self):
        return self.world._visibility_rays([(0., 2., 0.)], [(6., 2., 0.)])[0]

    def test_wall_and_terrain_hits_use_the_original_triangles(self):
        wall = self.wall_ray()
        self.assertEqual(wall[0], self.wall_body)
        self.assertEqual(wall[2], .5)
        self.assertEqual(wall[3], (3., 2., 0.))
        terrain = self.world._visibility_rays([(-5., 5., -5.)], [(-5., -1., -5.)])[0]
        self.assertAlmostEqual(terrain[2], 5/6)
        for actual, expected in zip(terrain[3], (-5., 0., -5.)):
            self.assertAlmostEqual(actual, expected)

    def test_sight_line_clears_only_when_damage_destroys_the_prop(self):
        world, body = self.world, self.wall_body
        world.assets = self.assets
        world.terrain = SimpleNamespace(height=lambda x, z: 0.)
        world.destroyed = set()
        world.object_health = {7: 10}
        world.object_bodies = {7: {body}}
        world.breakable_bodies = {body: 7}
        world.map_bodies = {body}
        world.destruction_events = []
        self.assertFalse(world.destroy(123, (1., 0.)))
        self.assertFalse(world.destroy(7, (1., 0.), damage=4))
        self.assertGreaterEqual(self.wall_ray()[0], 0)
        self.assertTrue(world.destroy(7, (1., 0.), damage=6))
        self.assertEqual(self.wall_ray()[0], -1)
        self.assertFalse(world.destroy(7, (1., 0.), damage=6))
        terrain = world._visibility_rays([(-5., 5., -5.)], [(-5., -1., -5.)])[0]
        self.assertGreaterEqual(terrain[0], 0)

    def test_visibility_dispatch_preserves_custom_samplers(self):
        world = self.world
        starts, ends = [(0., 2., 0.)], [(6., 2., 0.)]
        self.assertEqual(world._visibility_rays(starts, ends)[0][2], .5)
        expected = [(-1, -1, 1., ends[0], (0., 0., 0.))]
        world._static_rays = lambda a, b: expected
        self.assertIs(world._visibility_rays(starts, ends), expected)

    def test_vehicle_between_endpoints_does_not_occlude_static_visibility(self):
        from solbreaker.collision import _TRACKED_GROUP
        cid = self.world.cid
        shape = bullet.createCollisionShape(bullet.GEOM_BOX, halfExtents=(.5, .5, .5),
                                            physicsClientId=cid)
        vehicle = bullet.createMultiBody(baseCollisionShapeIndex=shape,
                                         basePosition=(1., 2., 0.), physicsClientId=cid)
        bullet.setCollisionFilterGroupMask(vehicle, -1, _TRACKED_GROUP, -1, physicsClientId=cid)
        self.assertEqual(self.wall_ray()[0], self.wall_body)
        bullet.removeBody(self.wall_body, physicsClientId=cid)
        self.assertEqual(self.wall_ray()[0], -1)


if __name__ == '__main__':
    unittest.main()


class SpottingFeedbackTests(unittest.TestCase):
    def arena(self, mode='encounter'):
        arena = Arena([dict(id=1, nickname='A'), dict(id=2, nickname='B')],
                      b'', SimpleNamespace(height=lambda x,z: 0.), now=1., solo=False, room_mode=mode)
        self.addCleanup(arena.close)
        return arena

    def test_human_boss_is_always_visible_without_detection_lamp(self):
        arena = self.arena('big_boss')
        boss = next(v for v in arena.vehicles.values() if arena._is_boss(v))
        boss.mad_observation_warning = True
        boss.mad_observation_due = 3.
        arena._set_mad_spotted(boss, False, 10.)
        arena._set_mad_spotted(boss, True, 15.)
        self.assertFalse(boss.mad_unspotted)
        self.assertFalse(boss.mad_observation_warning)
        self.assertIsNone(boss.mad_observation_due)
        self.assertFalse(any(e.kind == 'mad_observation_warning' and e.data['observed']
                             for e in arena.drain_events()))

    def test_blind_feedback_uses_visibility_at_impact_not_at_encoding(self):
        from solbreaker.battle import event_messages, shot_feedback
        arena = self.arena()
        shooter,target = arena.vehicle_for(1),arena.vehicle_for(2)
        target.mad_unspotted = True
        arena.emit('feedback',shooter.entity_id,target.entity_id,outcome='penetrated')
        events = arena.drain_events()
        target.mad_unspotted = False
        self.assertNotIn(shot_feedback(target.entity_id,'penetrated'),
                         event_messages(arena,arena.players[1],events))
        arena.emit('feedback',shooter.entity_id,target.entity_id,outcome='penetrated')
        events = arena.drain_events()
        target.mad_unspotted = True
        self.assertIn(shot_feedback(target.entity_id,'penetrated'),
                      event_messages(arena,arena.players[1],events))

    def test_blind_damage_keeps_victim_notice_and_refreshes_hp_on_reveal(self):
        from solbreaker.battle import event_messages, health_changed, received_damage_message
        arena = self.arena()
        shooter,target = arena.vehicle_for(1),arena.vehicle_for(2)
        target.mad_unspotted = True
        arena._damage_health(target,10,shooter.entity_id,2.,shell_hit=True)
        events = arena.drain_events()
        hp = health_changed(target.entity_id,target.health,shooter.entity_id)
        self.assertNotIn(hp,event_messages(arena,arena.players[1],events))
        self.assertIn(received_damage_message(target.entity_id,shooter.entity_id,10),
                      event_messages(arena,arena.players[2],events))
        arena._set_mad_spotted(target,True,3.)
        self.assertIn(health_changed(target.entity_id,target.health,0),
                      event_messages(arena,arena.players[1],arena.drain_events()))

    def test_hidden_death_reveals_wreck_before_health_in_all_spotting_modes(self):
        import struct
        from solbreaker.battle import event_messages, crew_voice_message, SOUND_ENEMY_SIGHTED
        for mode in ('encounter','big_boss','mad_games'):
            with self.subTest(mode=mode):
                arena = self.arena(mode)
                shooter,target = arena.vehicle_for(1),arena.vehicle_for(2)
                target.pose = Pose((30.,2.,40.),.3)
                target.mad_unspotted = target.mad_cloaked = True
                arena._damage_health(target,target.health,shooter.entity_id,2.,shell_hit=True)
                events = arena.drain_events()
                kinds = [e.kind for e in events]
                self.assertLess(kinds.index('mad_cloak'),kinds.index('health'))
                self.assertLess(kinds.index('health'),kinds.index('killed'))
                self.assertFalse(target.mad_unspotted or target.mad_cloaked)
                packet = event_messages(arena,arena.players[1],events)
                position = b'\x14'+struct.pack('<III6f',target.entity_id,arena.space_id,0,
                    *target.pose.position,target.pose.roll,target.pose.pitch,target.pose.yaw)
                self.assertIn(position,packet)
                self.assertNotIn(crew_voice_message(SOUND_ENEMY_SIGHTED),packet)

    def test_proximity_ignores_cover_and_high_concealment(self):
        arena = self.arena()
        observer,target = arena.vehicle_for(1),arena.vehicle_for(2)
        target.definition = SimpleNamespace(view_range=250., optional_devices=(),
                                           spotting=SpottingProfile(still=.99,moving=.99))
        observer.pose = Pose((0.,0.,0.),0.)
        target.pose = Pose((0.,0.,50.),0.)
        arena.world = SimpleNamespace(_visibility_rays=lambda *args: (_ for _ in ()).throw(
            AssertionError('Proximity must bypass sight rays')))
        self.addCleanup(setattr,arena,'world',None)
        target.mad_unspotted = True
        arena._update_team_spotting(2.)
        self.assertFalse(target.mad_unspotted)

    def test_partial_visibility_accepts_any_checkpoint_from_selected_viewport(self):
        arena = self.arena()
        observer,target = arena.vehicle_for(1),arena.vehicle_for(2)
        observer.pose = Pose((0.,0.,0.),0.)
        target.pose = Pose((0.,0.,60.),0.)
        observer.definition = target.definition = SimpleNamespace(view_range=250.)
        class World:
            ports = []
            def spotting_rays(self, source, destination, port):
                self.ports.append(port)
                return tuple(((0.,2.,0.),(i,2.,60.)) for i in range(6))
            def _visibility_rays(self, starts, ends):
                return [(99,-1,.4,(0,0,0),(0,1,0)) if i%6!=5
                        else (-1,-1,1.,(0,0,0),(0,0,0)) for i in range(len(starts))]
        arena.world = World()
        self.addCleanup(setattr,arena,'world',None)
        target.mad_unspotted = True
        arena._update_team_spotting(2.)
        self.assertFalse(target.mad_unspotted)
        arena._update_team_spotting(2.5)
        self.assertEqual(set(arena.world.ports),{0,1})

    def test_motion_timer_uses_the_same_immutable_frame_as_the_marker(self):
        from solbreaker.aiming import aim_time_remaining
        from solbreaker.battle import aim_time_message, motion_messages
        arena = self.arena()
        vehicle = arena.vehicle_for(1)
        config = SimpleNamespace(dispersion_radius=.3, aiming_time=2., after_shot_dispersion=3.)
        vehicle.definition = SimpleNamespace(aiming=config)
        vehicle.gun.movement_dispersion = 5.
        vehicle.last_shot_at = 1.75
        with patch.object(arena,'aim_marker',return_value=((0,0,0),(0,0,1),.03)):
            arena._record_motion_frame(2.)
        frame = arena.motion_frame
        expected = aim_time_remaining(config,shot_age=.25,movement_factor=5.)
        self.assertAlmostEqual(frame.samples[vehicle.entity_id].aim_remaining,expected)
        vehicle.gun.movement_dispersion = 1.
        vehicle.last_shot_at = None
        self.assertIn(aim_time_message(config,1.,0.,remaining=expected),
                      motion_messages(arena,arena.players[1],frame=frame))

    def test_reveal_uses_the_same_client_alias_as_health_and_death(self):
        import struct
        from solbreaker.battle import event_messages
        arena = self.arena()
        shooter,target = arena.vehicle_for(1),arena.vehicle_for(2)
        arena.practice_client_ids[target.entity_id] = 900
        target.mad_unspotted = True
        arena._damage_health(target,target.health,shooter.entity_id,2.,shell_hit=True)
        packet = event_messages(arena,arena.players[1],arena.drain_events())
        position = b'\x14'+struct.pack('<III6f',900,arena.space_id,0,
            *target.pose.position,target.pose.roll,target.pose.pitch,target.pose.yaw)
        self.assertIn(position,packet)

    def test_reveal_health_is_captured_at_the_visibility_event(self):
        from solbreaker.battle import event_messages, health_changed
        arena = self.arena()
        target = arena.vehicle_for(2)
        target.mad_unspotted = True
        target.health -= 10
        seen_health = target.health
        arena._set_mad_spotted(target,True,2.)
        events = arena.drain_events()
        target.health -= 30
        packet = event_messages(arena,arena.players[1],events)
        self.assertIn(health_changed(target.entity_id,seen_health,0),packet)
        self.assertNotIn(health_changed(target.entity_id,target.health,0),packet)
