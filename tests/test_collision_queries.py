"""Collision startup and bounded static-ray batches."""
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from solbreaker.collision import CollisionWorld, Pose
from solbreaker.battle import destructible_state
from solbreaker.map_catalog import destruction_index_path


class StaticRayBatchTests(unittest.TestCase):
    def setUp(self):
        self.world = CollisionWorld.__new__(CollisionWorld)
        self.world.cid = 17
        self.world._ray_threads = 2

    def test_only_large_batches_use_two_workers_and_preserve_results(self):
        for count, workers in ((1, 1), (127, 1), (128, 2), (512, 2)):
            with self.subTest(count=count):
                starts = [(0., 1., 0.)] * count
                ends = [(0., 0., 0.)] * count
                expected = [(-1, -1, 1., end, (0., 1., 0.)) for end in ends]
                with patch('solbreaker.collision.bullet.rayTestBatch', return_value=expected) as batch:
                    self.assertIs(self.world._static_rays(starts, ends), expected)
                batch.assert_called_once_with(starts, ends, collisionFilterMask=8,
                                              numThreads=workers, physicsClientId=17)

    def test_single_ray_retains_the_scalar_entry_point(self):
        start, end = (0., 1., 0.), (0., 0., 0.)
        hit = (-1, -1, 1., end, (0., 1., 0.))
        with patch('solbreaker.collision.bullet.rayTest', return_value=[hit]) as scalar:
            with patch('solbreaker.collision.bullet.rayTestBatch') as batch:
                self.assertEqual(self.world._static_ray(start, end), hit)
                batch.assert_not_called()
        scalar.assert_called_once_with(start, end, collisionFilterMask=8, physicsClientId=17)

    def test_one_available_worker_and_uninitialised_fixture_stay_single_threaded(self):
        starts, ends = [(0., 1., 0.)] * 128, [(0., 0., 0.)] * 128
        self.world._ray_threads = 1
        for remove in (False, True):
            if remove:
                del self.world._ray_threads
            with patch('solbreaker.collision.bullet.rayTestBatch', return_value=[]) as batch:
                self.world._static_rays(starts, ends)
            self.assertEqual(batch.call_args.kwargs['numThreads'], 1)

    def test_unregistered_props_do_not_create_client_destruction_areas(self):
        assets = SimpleNamespace(destructibles={-42: None, 0x7f7f0001: None})
        arena = SimpleNamespace(world=SimpleNamespace(assets=assets), entity_base=0,
                                vehicles={200: None}, destructions={})
        self.assertEqual(destructible_state(arena).areas, {0x7f7f: 300})

    def test_map_index_prefers_the_active_scene_tag(self):
        with TemporaryDirectory() as directory:
            default = Path(directory) / 'map.lka.dvpl'
            tagged = default.with_name('map.am1.lka.dvpl')
            default.touch()
            self.assertEqual(destruction_index_path(default, 'am1'), default)
            tagged.touch()
            self.assertEqual(destruction_index_path(default, 'am1'), tagged)
            self.assertEqual(destruction_index_path(default, ''), default)



class SpottingGeometryTests(unittest.TestCase):
    @staticmethod
    def box(low, high):
        return np.array([(x, y, z) for x in (low[0], high[0])
                         for y in (low[1], high[1]) for z in (low[2], high[2])], dtype=float)

    def setUp(self):
        self.assets = SimpleNamespace(
            tank_shapes=(('hull', self.box((-2., 0., -3.), (2., 2., 3.))),
                         ('turret', self.box((-1., 0., -1.), (1., 1., 1.)))),
            turret_node='turret', definition=SimpleNamespace(aiming=SimpleNamespace(
                turret_position=(.3, 2., .2), gun_position=(.1, .4, .9))))
        self.world = CollisionWorld.__new__(CollisionWorld)
        self.world.assets = self.assets
        self.world.vehicle_assets = {}
        self.world._component_bound_cache = {}
        self.world.poses = {200: Pose((0., 0., 0.), 0.), 201: Pose((10., 0., 20.), 0.)}
        self.world.gun_angles = {200: (0., 0.), 201: (0., 0.)}

    def test_six_distinct_checkpoints_follow_selected_hull_and_turret_dimensions(self):
        rows = self.world.spotting_rays(200, 201)
        self.assertEqual(len(rows), 6)
        self.assertEqual(len({end for _, end in rows}), 6)
        self.assertTrue(all(start == (.3, 3., .2) for start, _ in rows))
        np.testing.assert_allclose([end for _, end in rows], (
            (10., 1., 23.), (10., 1., 17.), (8., 1., 20.), (12., 1., 20.),
            (10.3, 3., 20.2), (10.4, 2.4, 21.1)))

    def test_hull_yaw_pitch_and_roll_transform_surface_checkpoints(self):
        for angles, transformed in (((math.pi/2, 0., 0.), (3., 1., 0.)),
                                    ((0., math.pi/2, 0.), (0., -3., 1.)),
                                    ((0., 0., math.pi/2), (-1., 0., 3.))):
            with self.subTest(angles=angles):
                self.world.poses[201] = Pose((10., 20., 30.), *angles)
                start, front = self.world.spotting_rays(200, 201)[0]
                np.testing.assert_allclose(front, np.array((10., 20., 30.))+transformed, atol=1e-12)
                self.assertEqual(start, (.3, 3., .2))

    def test_dynamic_viewport_and_mantlet_follow_turret_but_static_roof_does_not(self):
        before = self.world.spotting_rays(200, 201, 0)
        self.world.gun_angles[200] = (math.pi/2, -.3)
        self.world.gun_angles[201] = (math.pi/2, .2)
        static = self.world.spotting_rays(200, 201, 0)
        dynamic = self.world.spotting_rays(200, 201, 1)
        self.assertEqual(static[:5], before[:5])
        np.testing.assert_allclose(dynamic[0][0], (1.2, 2.4, .1), atol=1e-12)
        np.testing.assert_allclose(static[5][1], (11.2, 2.4, 20.1), atol=1e-12)
        self.assertNotEqual(static[5][1], before[5][1])
        self.assertEqual(tuple(end for _, end in dynamic), tuple(end for _, end in static))

    def test_rear_mounted_turret_roof_is_on_component_and_rotates_only_for_target(self):
        self.assets.tank_shapes = (self.assets.tank_shapes[0],
            ('turret', self.box((-.8, 0., -2.5), (.6, 1.2, -.5))))
        neutral = self.world.spotting_rays(200, 201, 0)
        np.testing.assert_allclose(neutral[0][0], (.2, 3.2, -1.3), atol=1e-12)
        np.testing.assert_allclose(neutral[4][1], (10.2, 3.2, 18.7), atol=1e-12)
        self.world.gun_angles[200] = self.world.gun_angles[201] = (math.pi/2, 0.)
        turned = self.world.spotting_rays(200, 201, 0)
        self.assertEqual(turned[0][0], neutral[0][0])
        # Local roof (-.1, 1.2, -1.5) rotates to (-1.5, 1.2, .1)
        # before the XML turret pivot and the target's world origin are added.
        np.testing.assert_allclose(turned[4][1], (8.8, 3.2, 20.3), atol=1e-12)
        self.assertEqual(tuple(end for _, end in turned[:4]),
                         tuple(end for _, end in neutral[:4]))

    def test_viewport_uses_observers_full_hull_transform(self):
        self.world.poses[200] = Pose((5., 6., 7.), 0., 0., math.pi/2)
        for port, expected in ((0, (2., 6.3, 7.2)), (1, (2.6, 6.4, 8.1))):
            rows = self.world.spotting_rays(200, 201, port)
            np.testing.assert_allclose(rows[0][0], expected, atol=1e-12)

    def test_repeated_queries_reuse_cached_mesh_bounds(self):
        self.world.spotting_rays(200, 201)
        with patch('solbreaker.collision.np.min', side_effect=AssertionError('mesh rescan')), \
                patch('solbreaker.collision.np.max', side_effect=AssertionError('mesh rescan')):
            self.world.spotting_rays(200, 201, 1)

    def test_point_cache_reuses_both_roles_and_invalidates_selected_vehicle_assets(self):
        self.world.spotting_rays(200, 201)
        with patch('solbreaker.collision.matrix', side_effect=AssertionError('pose recomputed')):
            self.world.spotting_rays(200, 201, 1)
            self.world.spotting_rays(201, 200, 0)
        changed = SimpleNamespace(tank_shapes=(
            ('hull', self.box((-2., 0., -4.), (2., 2., 4.))), self.assets.tank_shapes[1]),
            turret_node=self.assets.turret_node, definition=self.assets.definition)
        self.world.vehicle_assets = {200: self.assets, 201: changed}
        rows = self.world.spotting_rays(200, 201)
        self.assertEqual(rows[0][1], (10., 1., 24.))
        self.assertEqual(len(self.world._spotting_point_cache), 2)

    def test_turretless_fixture_has_six_checks_and_invalid_port_is_rejected(self):
        self.assets.turret_node = None
        self.assets.definition = SimpleNamespace()
        rows = self.world.spotting_rays(200, 201, 1)
        self.assertEqual(len(set(end for _, end in rows)), 6)
        self.assertEqual(rows[0][0], (0., 1.5, 3.))
        for port in (-1, 2, True, .5):
            with self.subTest(port=port), self.assertRaises(ValueError):
                self.world.spotting_rays(200, 201, port)

if __name__ == '__main__':
    unittest.main()
