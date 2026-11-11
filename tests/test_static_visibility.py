"""Merged visibility meshes retain destructible lifetime and sampler overrides."""
from types import SimpleNamespace
import unittest

import numpy as np
import pybullet as bullet

from solbreaker.collision import CollisionWorld
from solbreaker.mesh_files import MeshFiles
from solbreaker.static_visibility import VisibilityWorld


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
        self.query = VisibilityWorld()
        self.addCleanup(self.query.close)
        for _ in self.query.build(self.assets):
            pass
        world = self.world = CollisionWorld.__new__(CollisionWorld)
        world.cid = bullet.connect(bullet.DIRECT)
        self.addCleanup(bullet.disconnect, world.cid)
        world._mesh_body_paths = {}
        world._rapier = None
        world._native_physics = None
        world._rapier_query_backend = 'bullet'
        world._ray_threads = 1
        world._visibility = self.query
        self.wall_body = world._mesh_body(self.meshes.mesh(wall.vertices, wall.triangles))
        vertices = np.array(((-10., 0., -10.), (0., 0., -10.),
                             (-10., 0., 0.), (0., 0., 0.)))
        triangles = np.array(((0, 2, 1), (1, 2, 3)), dtype=np.int32)
        world._mesh_body(self.meshes.mesh(vertices, triangles))

    def wall_ray(self):
        return self.query.rays([(0., 2., 0.)], [(6., 2., 0.)], 1)[0]

    def test_wall_and_terrain_hits_use_the_original_triangles(self):
        wall = self.wall_ray()
        self.assertGreaterEqual(wall[0], 0)
        self.assertEqual(wall[2], .5)
        self.assertEqual(wall[3], (3., 2., 0.))
        terrain = self.world._visibility_rays([(-5., 5., -5.)], [(-5., -1., -5.)])[0]
        self.assertAlmostEqual(terrain[2], 5/6)
        for actual, expected in zip(terrain[3], (-5., 0., -5.)):
            self.assertAlmostEqual(actual, expected)

    def test_destroy_removes_only_the_selected_obstacle_and_can_be_repeated(self):
        self.query.destroy(123)
        self.assertGreaterEqual(self.wall_ray()[0], 0)
        self.query.destroy(7)
        self.query.destroy(7)
        self.assertEqual(self.wall_ray()[0], -1)
        self.assertFalse(self.query.breakables)
        terrain = self.world._visibility_rays([(-5., 5., -5.)], [(-5., -1., -5.)])[0]
        self.assertGreaterEqual(terrain[0], 0)

    def test_main_world_destruction_is_mirrored_after_the_health_threshold(self):
        world, body = self.world, self.wall_body
        world.assets = self.assets
        world.terrain = SimpleNamespace(height=lambda x, z: 0.)
        world.destroyed = set()
        world.object_health = {7: 10}
        world.object_bodies = {7: {body}}
        world.breakable_bodies = {body: 7}
        world.map_bodies = {body}
        world.destruction_events = []
        self.assertFalse(world.destroy(7, (1., 0.), damage=4))
        self.assertGreaterEqual(self.wall_ray()[0], 0)
        self.assertTrue(world.destroy(7, (1., 0.), damage=6))
        self.assertEqual(self.wall_ray()[0], -1)
        self.assertFalse(world.destroy(7, (1., 0.), damage=6))

    def test_visibility_dispatch_preserves_custom_samplers_and_shadow_mode(self):
        world = self.world
        starts, ends = [(0., 2., 0.)], [(6., 2., 0.)]
        self.assertEqual(world._visibility_rays(starts, ends)[0][2], .5)
        expected = [(-1, -1, 1., ends[0], (0., 0., 0.))]
        world._static_rays = lambda a, b: expected
        self.assertIs(world._visibility_rays(starts, ends), expected)
        world._rapier_query_backend = 'shadow'
        self.assertIs(world._visibility_rays(starts, ends), expected)

    def test_close_releases_the_extra_connection_and_is_idempotent(self):
        cid = self.query.cid
        self.query.close()
        self.query.close()
        self.assertFalse(bullet.isConnected(cid))

    def test_empty_destructible_geometry_does_not_create_a_collision_body(self):
        query = VisibilityWorld()
        self.addCleanup(query.close)
        empty = SimpleNamespace(vertices=np.empty((0, 3)),
                                triangles=np.empty((0, 3), dtype=np.int32))
        assets = SimpleNamespace(vertices=empty.vertices, triangles=empty.triangles,
                                 destructibles={12: empty}, mesh_files=self.meshes)
        self.assertEqual(list(query.build(assets)), [])
        self.assertFalse(query.breakables)
        self.assertEqual(query.rays([(0., 1., 0.)], [(1., 1., 0.)], 1)[0][0], -1)


if __name__ == '__main__':
    unittest.main()
