"""Conservative target selection for unguided shell sweeps."""
from math import hypot, exp, radians
from random import Random
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import numpy as np

from solbreaker.aiming import (GunAim, disperse_shot, aim_time_remaining, dispersion_tangent,
                               FULL_SETTLE_TIME_CONSTANTS, MAX_DISPERSION_BLOOM, settling_time)
from solbreaker.arena import Arena
from solbreaker.armor import ArmorHit, ArmorModel, MovableArmorStateSnapshot, ShotResult
from solbreaker.ballistics import BodyState, Projectile, SweepCache, advance
from solbreaker.battle import event_messages, received_damage_message
from solbreaker.collision import Pose
from solbreaker.dava_scene import Mesh
from solbreaker.vehicle_data import ArmorMaterial, Component, Shell


class ArmorMaterialTests(TestCase):
    def test_unassigned_faces_do_not_replace_indexed_vehicle_armor(self):
        def entity(name, mesh):
            return {'name': name, 'components': {'render': {
                'comp.typename': 'RenderComponent',
                'rc.renderObj': {'ro.batches': {'0': {'rb.datasource': mesh}}},
            }}}

        hull, turret, gun = (Component(name, 1, 1., 100, name, (ArmorMaterial('armor_1', 50., 1.),))
                             for name in ('hull', 'turret_02', 'gun_06'))
        tracks = Component('chassis', 1, 1., 100, None, (
            ArmorMaterial('leftTrack', 10., 0.), ArmorMaterial('rightTrack', 10., 0.)))
        definition = SimpleNamespace(hull=hull, turret=turret, gun=gun, chassis=tracks, health=100,
                                     hull_position=(0., 0., 0.), collision_asset='collision',
                                     visual_asset='visual', weapon=SimpleNamespace(shells=(None,)),
                                     aiming=SimpleNamespace(turret_position=(0., 0., 0.), gun_position=(0., 0., 0.)))
        collision = {'#hierarchy': [entity(part.geometry, i) for i, part in enumerate((hull, turret, gun))]}
        visual = {'#hierarchy': [entity(name, 0) for name in ('chassis_track_L', 'chassis_track_R')]}
        vertices = np.array([(0., 0., 0.), (1., 0., 0.), (0., 1., 0.),
                             (0., 0., 2.), (1., 0., 2.), (0., 1., 2.)])
        triangles = np.array([(0, 1, 2), (3, 4, 5)])
        indexed = Mesh(vertices, triangles[:1], np.ones(6, dtype=int))

        def load(gun_ids):
            meshes = {0: Mesh(vertices, triangles, np.array([1, 1, 1, -1, -1, -1])),
                      1: indexed, 2: Mesh(vertices, triangles, np.array(gun_ids))}
            with (patch('solbreaker.armor.scene', side_effect=(collision, visual)),
                  patch('solbreaker.armor.geometry', side_effect=(meshes, {0: indexed}))):
                return ArmorModel(None, definition)

        armor = load([1, 1, 1, -1, -1, -1])
        for name, faces, materials, thickness, _ in armor.parts:
            if name in (hull.geometry, gun.geometry):
                self.assertEqual(len(faces), 1)
                np.testing.assert_array_equal(materials, [1])
                np.testing.assert_array_equal(thickness, [50.])
                np.testing.assert_array_equal(faces[0], [[0., 0., 0.], [0., 0., 1.], [1., 0., 0.]])
        with self.assertRaisesRegex(ValueError, 'No armor definition: gun_06/99'):
            load([1, 1, 1, 99, 99, 99])
        with self.assertRaisesRegex(ValueError, 'Armor triangle crosses material indices'):
            load([1, 1, 1, -1, -1, 1])


class ShotDispersionTests(TestCase):
    def test_settling_increases_chance_inside_half_the_cone(self):
        tangent = .01
        for progress, center_chance in ((0., .397), (.5, .648), (1., .90)):
            with self.subTest(progress=progress):
                rng = Random(0)
                radii = []
                for _ in range(4096):
                    x, y, z = disperse_shot((0., 0., 1.), tangent, rng, progress)
                    radii.append(hypot(x, y) / z)
                self.assertLessEqual(max(radii), tangent)
                inner_fraction = sum(radius <= tangent / 2. for radius in radii) / len(radii)
                self.assertAlmostEqual(inner_fraction, center_chance, delta=.025)

    def test_shots_stay_inside_the_capped_circle_at_maximum_bloom(self):
        config = SimpleNamespace(dispersion_radius=.4, aiming_time=2., after_shot_dispersion=4.)
        tangent = dispersion_tangent(config, shot_age=0., movement_factor=30.)
        self.assertAlmostEqual(tangent, MAX_DISPERSION_BLOOM * config.dispersion_radius/100.)
        rng = Random(8)
        for _ in range(512):
            x, y, z = disperse_shot((0., 0., 1.), tangent, rng)
            self.assertLessEqual(hypot(x, y)/z, tangent)

    def test_settled_gun_has_no_timer_even_with_suspension_jitter(self):
        gun = GunAim()
        config = SimpleNamespace(dispersion_radius=.4, aiming_time=2.)
        pose = Pose((0., 0., 0.), 0.)
        self.assertEqual(gun.aim_progress, 1.)
        jittered = Pose((0., .002, 0.), 0., .001, -.001)
        gun.update_dispersion(config, pose, jittered, 0., 1/60, 1.)
        self.assertEqual(gun.movement_dispersion, 1.)
        self.assertEqual(gun.aim_progress, 1.)
        self.assertEqual(aim_time_remaining(config), 0.)

    def test_timer_tracks_combined_shot_and_movement_cone(self):
        config = SimpleNamespace(dispersion_radius=.4, aiming_time=2., after_shot_dispersion=4.)
        movement, age = 8., .3
        remaining = aim_time_remaining(config, shot_age=age, movement_factor=movement)
        self.assertGreater(remaining, FULL_SETTLE_TIME_CONSTANTS * settling_time(config, 1.))
        future_movement = 1. + (movement-1.) * exp(-remaining/settling_time(config, 1.))
        cone = dispersion_tangent(config, shot_age=age+remaining,
                                  movement_factor=future_movement)
        self.assertAlmostEqual(cone/(config.dispersion_radius/100.),
                               1. + exp(-FULL_SETTLE_TIME_CONSTANTS))
        self.assertEqual(aim_time_remaining(config, shot_age=age+remaining+.001,
            movement_factor=1.+(movement-1.)*exp(-(remaining+.001)/settling_time(config, 1.))), 0.)
        earlier = remaining-.01
        self.assertGreater(aim_time_remaining(config, shot_age=age+earlier,
            movement_factor=1.+(movement-1.)*exp(-earlier/settling_time(config, 1.))), 0.)

    def test_subunit_after_shot_preserves_actual_signed_bloom(self):
        # T49_Rocket's pinned afterShot is 0.0001. The shot initially narrows
        # the cone below base, including while moving; do not clamp that XML.
        config = SimpleNamespace(dispersion_radius=.4, aiming_time=2., after_shot_dispersion=.0001)
        for movement in (1., 1.01, 1.9, 2.):
            with self.subTest(movement=movement):
                self.assertEqual(aim_time_remaining(config, shot_age=0., movement_factor=movement), 0.)
        self.assertLess(dispersion_tangent(config, shot_age=0.), config.dispersion_radius/100.)

    def test_subunit_after_shot_counts_through_later_movement_bloom(self):
        config = SimpleNamespace(dispersion_radius=.4, aiming_time=2., after_shot_dispersion=.0001)
        movement = 11.
        self.assertLess(dispersion_tangent(config, shot_age=0., movement_factor=movement),
                        config.dispersion_radius/100.)
        remaining = aim_time_remaining(config, shot_age=0., movement_factor=movement)
        self.assertGreater(remaining, 0.)
        for offset, above in ((-.01, True), (.01, False)):
            elapsed = remaining+offset
            future = 1.+(movement-1.)*exp(-elapsed/settling_time(config, 1.))
            ratio = dispersion_tangent(config, shot_age=elapsed, movement_factor=future)/(config.dispersion_radius/100.)
            self.assertEqual(ratio > 1.+exp(-FULL_SETTLE_TIME_CONSTANTS), above)
        elapsed = 1.
        self.assertAlmostEqual(aim_time_remaining(config, shot_age=elapsed,
            movement_factor=1.+(movement-1.)*exp(-elapsed/settling_time(config, 1.))), remaining-elapsed)

    def test_aiming_factor_accelerates_the_same_cone_decay(self):
        config = SimpleNamespace(dispersion_radius=.4, aiming_time=2., after_shot_dispersion=4.)
        normal = aim_time_remaining(config, shot_age=0., movement_factor=5.)
        faster = aim_time_remaining(config, shot_age=0., movement_factor=5., aim_factor=.7)
        self.assertAlmostEqual(faster, .7 * normal)

    def test_movement_decay_and_timer_advance_together(self):
        gun = GunAim()
        config = SimpleNamespace(dispersion_radius=.4, aiming_time=2., movement_dispersion=.2)
        pose = Pose((0., 0., 0.), 0.)
        moved = Pose((0., 0., 10.), 0.)
        gun.update_dispersion(config, pose, moved, 0., 1., 1.)
        before = aim_time_remaining(config, movement_factor=gun.movement_dispersion)
        self.assertGreater(before, 0.)
        gun.update_dispersion(config, moved, moved, 0., .5, 1.)
        self.assertAlmostEqual(aim_time_remaining(config, movement_factor=gun.movement_dispersion), before-.5)
        for _ in range(100):
            gun.update_dispersion(config, moved, moved, 0., .1, 1.)
        self.assertEqual(gun.aim_progress, 1.)

    def test_vertical_gun_traverse_expands_the_same_reported_cone(self):
        gun = GunAim()
        config = SimpleNamespace(dispersion_radius=.4, aiming_time=2., turret_rotation_dispersion=.12)
        pose = Pose((0., 0., 0.), 0.)
        gun.pitch = radians(15.)
        gun.update_dispersion(config, pose, pose, 0., 1., 1., previous_pitch=0.)
        self.assertGreater(gun.movement_dispersion, 1.)
        self.assertGreater(aim_time_remaining(config, movement_factor=gun.movement_dispersion), 0.)
        self.assertLess(gun.aim_progress, 1.)

    def test_shot_bloom_keeps_progress_unsettled_while_standing(self):
        gun = GunAim()
        config = SimpleNamespace(dispersion_radius=.4, aiming_time=2., after_shot_dispersion=4.)
        pose = Pose((0., 0., 0.), 0.)
        gun.update_dispersion(config, pose, pose, 0., 1/60, 1., shot_age=1/60)
        self.assertLess(gun.aim_progress, 1.)
        gun.update_dispersion(config, pose, pose, 0., 1/60, 1., shot_age=20.)
        self.assertEqual(gun.aim_progress, 1.)


class IncomingHitTests(TestCase):
    def test_track_only_hit_displays_nominal_damage_to_the_target(self):
        arena = Arena([dict(id=1, nickname='Shooter', team=1), dict(id=2, nickname='Target', team=2)],
                      b'', SimpleNamespace(height=lambda x, z: 0.), now=1., solo=False)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        arena.ready(2, 1.)
        arena.drain_events()
        shooter, target = (arena.vehicle_for(i) for i in (1, 2))
        shell = Shell(1, 150., 640., (200., 200.), 0., 1., 750., 0., 500.)
        direction = (0., 0., 1.)
        point = (0., 0., 3.)
        health = target.health
        packet = received_damage_message(target.entity_id, shooter.entity_id, 0,
                                         outcome='not_penetrated', blocked_damage=640)
        for shot, part, outcome, displays_blocked in (
                (1, 'chassis_track_L', 'external', True),
                (2, 'chassis_track_R', 'not_penetrated', True),
                (3, 'gun', 'external', False)):
            with self.subTest(part=part, outcome=outcome):
                hit = ArmorHit(3., point, (0., 0., -1.), part, 0, 20., 0., external=True)
                projectile = Projectile(shot, shooter.entity_id, shell, point, direction, 1.)
                impact = projectile.impact(point, direction, result=ShotResult(outcome, hit),
                                           target=target.entity_id)
                arena._impact(impact)
                events = arena.drain_events()
                target_wire = event_messages(arena, arena.players[2], events)
                self.assertEqual(packet in target_wire, displays_blocked)
                self.assertNotIn(packet, event_messages(arena, arena.players[1], events))
                self.assertEqual(target.health, health)
                self.assertEqual(target.blocked_damage, 0)

        hit = ArmorHit(3., point, (0., 0., -1.), 'hull', 0, 20., 1.)
        projectile = Projectile(4, shooter.entity_id, shell, point, direction, 1.)
        arena._impact(projectile.impact(point, direction, result=ShotResult('penetrated', hit, 35),
                                        target=target.entity_id))
        target_wire = event_messages(arena, arena.players[2], arena.drain_events())
        self.assertIn(received_damage_message(target.entity_id, shooter.entity_id, 35), target_wire)
        self.assertNotIn(packet, target_wire)
        self.assertEqual(target.health, health - 35)


class BallisticSweepTests(TestCase):
    def setUp(self):
        armor = ArmorModel.__new__(ArmorModel)
        armor.radius = 3.
        armor.intersections = Mock(return_value=[])
        self.armor = armor
        self.world = SimpleNamespace(assets_for=lambda entity: SimpleNamespace(armor=armor),
                                     static_segment=Mock(side_effect=lambda a, b: (None, 1., b)))

    def test_only_nearby_rotating_hulls_increase_the_substep_count(self):
        dt = 1 / 60
        shell = SimpleNamespace(gravity=0., missile=None, max_distance=500.)
        for (first, last), count in (
                (((100., 0., 6.), (100., 0., 6.)), 4),
                (((-8., 0., 6.), (8., 0., 6.)), 30)):
            with self.subTest(first=first, last=last):
                self.world.static_segment.reset_mock()
                self.armor.intersections.reset_mock()
                before = {2: BodyState(Pose(first, 0., 0., -.38))}
                after = {2: BodyState(Pose(last, 0., 0., .38))}
                projectile = Projectile(1, 1, shell, (0., 0., 0.), (0., 0., 720.), 0.)
                self.assertIsNone(advance(projectile, self.world, before, after, dt, Random(0)))
                self.assertEqual(self.world.static_segment.call_count, count)
                self.assertEqual(self.armor.intersections.called, count > 4)

    def test_shell_apex_keeps_target_above_both_endpoints(self):
        shell = SimpleNamespace(gravity=80., missile=None, max_distance=500.)
        projectile = Projectile(1, 1, shell, (0., 0., 0.), (4., 40., 0.), 0.)
        frame = {2: BodyState(Pose((2., 12., 0.), 0.))}
        self.assertEqual(SweepCache(frame, frame).candidates(self.world, projectile, 1.), (2,))

    def test_motion_endpoints_reuse_pose_and_keep_interval_armor(self):
        active = MovableArmorStateSnapshot(((0, False), (1, True)))
        expired = MovableArmorStateSnapshot(((0, True), (1, False)))
        before = BodyState(Pose((1., 2., 3.), .1), armor_state=active)
        after = BodyState(Pose((2., 3., 4.), .2), armor_state=expired)
        arena = SimpleNamespace(_movable_armor_snapshot_at=Mock(return_value=active))
        self.assertIs(Arena._armor_frame_state(arena, None, before, after, 0., .5), before)
        end = Arena._armor_frame_state(arena, None, before, after, 1., .5)
        self.assertEqual(end.pose, after.pose)
        self.assertEqual(end.armor_state, active)
