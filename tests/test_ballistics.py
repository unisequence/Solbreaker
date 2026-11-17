"""Conservative target selection for unguided shell sweeps."""
from math import hypot
from random import Random
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from solbreaker.aiming import GunAim, disperse_shot
from solbreaker.arena import Arena
from solbreaker.armor import ArmorHit, ArmorModel, MovableArmorStateSnapshot, ShotResult
from solbreaker.ballistics import BodyState, Projectile, SweepCache, advance
from solbreaker.battle import event_messages, received_damage_message
from solbreaker.collision import Pose
from solbreaker.vehicle_data import Shell


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

    def test_aim_progress_uses_effective_aim_time_and_resets_on_motion(self):
        gun = GunAim()
        config = SimpleNamespace(dispersion_radius=.4, aiming_time=2.)
        pose = Pose((0., 0., 0.), 0.)
        gun.update_dispersion(config, pose, pose, 0., 3., 1.)
        self.assertEqual(gun.aim_progress, .5)
        gun.update_dispersion(config, pose, pose, 0., 2.1, .7)
        self.assertEqual(gun.aim_progress, 1.)
        moved = Pose((.1, 0., 0.), 0.)
        gun.update_dispersion(config, pose, moved, 0., 1., .7)
        self.assertEqual(gun.aim_progress, 0.)
        gun.update_dispersion(config, moved, moved, 0., 4.2, .7)
        self.assertEqual(gun.aim_progress, 1.)
        gun.pitch = .05
        gun.update_dispersion(config, moved, moved, 0., 1., .7, previous_pitch=0.)
        self.assertEqual(gun.aim_progress, 0.)


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
