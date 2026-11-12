from dataclasses import replace
import multiprocessing as mp
from pathlib import Path
import struct
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from solbreaker.bot_navigation import Navigation
from solbreaker.collision import CollisionScene, CollisionWorld
from solbreaker.map_cache import DERIVED_ARRAYS, MapCache
from solbreaker.mesh_files import MeshFiles
from solbreaker.navigation_cache import prewarm
from solbreaker.terrain import MapTerrain
from tests.test_map_cache import TestCacheStore, fixture, map_only


def finish(generator):
    while True:
        try:
            next(generator)
        except StopIteration as result:
            return result.value


def read_derived(cache, gate, queue):
    gate.wait(10)

    def build():
        with (cache.root / 'derived-builds').open('a') as stream:
            stream.write('build\n')
        time.sleep(.1)
        yield
        return {'data': np.arange(16)}

    arrays, hit = finish(cache.derive('a'*64, build))
    queue.put((arrays['data'].tolist(), hit))


class NavigationCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = TestCacheStore(self.root / 'cache')
        self.spec = replace(fixture(self.root), bounds=(-12., -12., 12., 12.),
                            landscape_bbox=(-12., -12., -1., 12., 12., 1.))
        self.terrain = MapTerrain(self.root, self.spec)
        self.scene = CollisionScene.__new__(CollisionScene)
        self.scene._init_map(self.spec, MeshFiles())
        self.addCleanup(self.scene.close)
        self.store.load(self.scene, self.terrain)

    def build(self, *, cached=True, entity=201, width=.7, engine_power=380.):
        world = CollisionWorld.__new__(CollisionWorld)
        world.assets = self.scene
        world.terrain = self.terrain
        world._terrain_grid = None
        world.poses = {entity: SimpleNamespace(position=(-7., 0., -7.))}
        vehicle = SimpleNamespace(tank_shapes=(('hull', np.array(((-width, -.3, -1.), (width, 1., 1.)))),),
            turret_node=None, max_slope=.5, definition=SimpleNamespace(mass=25000.,
                drive=SimpleNamespace(engine_power_hp=engine_power, terrain_resistance=(1., 1.2, 1.4))))
        world.assets_for = lambda ident: vehicle
        prepared = self.scene.mesh_files.prepared
        if not cached:
            self.scene.mesh_files.prepared = None
        try:
            return finish(Navigation.build(world, (entity,), {entity: (7., 0., 7.)}))
        finally:
            self.scene.mesh_files.prepared = prepared

    def assert_nav_equal(self, first, second, first_actor=201, second_actor=201):
        for name in ('heights', 'base', 'counts', 'walkable', '_edges'):
            np.testing.assert_array_equal(getattr(first, name), getattr(second, name))
        self.assertEqual(first.actor_climb_costs[first_actor], second.actor_climb_costs[second_actor])
        self.assertEqual(first._landmark_fields, second._landmark_fields)
        a, b = first.searches[first_actor], second.searches[second_actor]
        self.assertEqual((a.start, a.goal, a.path, a.route), (b.start, b.goal, b.path, b.route))

    def test_prewarm_and_profile_reuse_preserve_paths_and_destruction(self):
        self.assertEqual(prewarm(self.scene.mesh_files.prepared, self.terrain, may_evict=lambda: False),
                         {'nav_cache_hits': 0, 'nav_cache_misses': 1})
        reference = self.build(cached=False)
        first = self.build()
        second = self.build(entity=213)
        self.assertEqual(first.build_timings['nav_cache_hits'], 1)
        self.assertEqual(second.build_timings['nav_cache_misses'], 0)
        self.assert_nav_equal(reference, first)
        self.assert_nav_equal(reference, second, second_actor=213)
        first.refresh({7})
        reference.refresh({7})
        self.assert_nav_equal(reference, first)
        self.assertEqual(second.removed, set())
        self.assertNotEqual(first.removed, second.removed)

    def test_exact_size_and_drivetrain_inputs_invalidate_independent_layers(self):
        self.build()
        wide = self.build(width=1.9)
        stronger = self.build(engine_power=550.)
        self.assertGreater(wide.build_timings['nav_cache_misses'], 0)
        self.assertGreater(wide.build_timings['nav_cache_hits'], 0)
        self.assertGreater(stronger.build_timings['nav_cache_misses'], 0)
        self.assertGreater(stronger.build_timings['nav_cache_hits'], 0)
        self.assert_nav_equal(self.build(cached=False, width=1.9), wide)
        self.assert_nav_equal(self.build(cached=False, engine_power=550.), stronger)

    def test_derived_corruption_rebuilds_exact_bytes(self):
        self.build()
        path = next(self.store.root.glob(f'*/{DERIVED_ARRAYS}'))
        expected = path.read_bytes()
        path.write_bytes(b'damaged')
        rebuilt = self.build()
        self.assertEqual(rebuilt.build_timings['nav_cache_misses'], 1)
        self.assertEqual(path.read_bytes(), expected)
        self.assert_nav_equal(self.build(cached=False), rebuilt)

    def test_cached_narrow_passage_keeps_width_gates(self):
        def walls(spec, meshes):
            scene = map_only(spec, meshes)
            scene.vertices = np.array([(0., y, z) for lo, hi in ((-12., -3.), (3., 12.))
                                       for y, z in ((-2., lo), (4., lo), (4., hi), (-2., hi))])
            scene.triangles = np.array(((0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)), dtype=np.int32)
            scene.destructibles = {}
            return scene

        self.scene = CollisionScene.__new__(CollisionScene)
        self.scene._init_map(self.spec, MeshFiles())
        self.addCleanup(self.scene.close)
        with patch.object(CollisionScene, 'map_only', side_effect=walls):
            MapCache(self.root/'walls').load(self.scene, self.terrain)
        small, wide = self.build(), self.build(width=1.9)
        self.assertTrue(small.searches[201].path)
        self.assertFalse(wide.searches[201].path)
        self.assert_nav_equal(self.build(cached=False), self.build())
        self.assert_nav_equal(self.build(cached=False, width=1.9), self.build(width=1.9))

    def test_cached_terrain_preserves_steep_slope_gates(self):
        flat = self.build()
        self.spec.heightmap_path.write_bytes(struct.pack('<II4H', 2, 1, 0, 65535, 0, 65535))
        spec = replace(self.spec, landscape_bbox=(-12., -12., -1., 12., 12., 18.))
        self.terrain = MapTerrain(self.root, spec)
        self.scene = CollisionScene.__new__(CollisionScene)
        self.scene._init_map(spec, MeshFiles())
        self.addCleanup(self.scene.close)
        self.store.load(self.scene, self.terrain)
        steep = self.build()
        self.assertGreater(np.count_nonzero(steep.base), np.count_nonzero(flat.base))
        self.assert_nav_equal(self.build(cached=False), self.build())

    def test_two_owners_publish_one_derived_entry(self):
        context = mp.get_context('spawn')
        gate, queue = context.Event(), context.Queue()
        workers = [context.Process(target=read_derived, args=(self.store, gate, queue)) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            gate.set()
            results = [queue.get(timeout=15) for _ in workers]
        finally:
            for worker in workers:
                if worker.pid is not None:
                    worker.join(2)
                    if worker.is_alive():
                        worker.terminate()
                        worker.join(2)
                    worker.close()
            queue.close()
        self.assertCountEqual(results, [(list(range(16)), False), (list(range(16)), True)])
        self.assertEqual((self.store.root / 'derived-builds').read_text().splitlines(), ['build'])
