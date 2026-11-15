"""Conservative target selection for unguided shell sweeps."""
from random import Random
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from solbreaker.arena import Arena
from solbreaker.armor import ArmorModel, MovableArmorStateSnapshot
from solbreaker.ballistics import BodyState, Projectile, SweepCache, advance
from solbreaker.collision import Pose


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
