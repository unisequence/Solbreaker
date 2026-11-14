"""Collision startup and bounded static-ray batches."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from solbreaker.collision import CollisionWorld
from solbreaker.battle import destructible_state
from solbreaker.map_catalog import destruction_index_path


class NativeStartupTests(unittest.TestCase):
    def setUp(self):
        self.world = CollisionWorld.__new__(CollisionWorld)
        self.world._rapier = None
        self.world.build_timings = {}
        self.world._prepare_rapier_fixture = Mock(return_value='sealed.json')
        self.started = Event()
        self.rapier = Mock()
        owner_thread = get_ident()

        def load(path):
            self.assertNotEqual(get_ident(), owner_thread)
            self.assertEqual(path, 'sealed.json')
            self.started.set()
            return self.rapier, 10., 6.

        def ground():
            self.assertEqual(get_ident(), owner_thread)
            self.assertTrue(self.started.wait(2))
            yield

        self.world._load_rapier_world = load
        self.world._build_ground_physics = ground

    def test_installs_world_after_both_builders_finish(self):
        builder = self.world._build_native_parallel()
        next(builder)
        self.assertIsNone(self.world._rapier)
        with self.assertRaises(StopIteration):
            next(builder)
        self.assertIs(self.world._rapier, self.rapier)
        self.assertEqual(self.world.build_timings['rapier_worker_cpu_ms'], 6.)
        self.rapier.close.assert_not_called()

    def test_cancelled_arena_closes_the_constructed_rapier_world(self):
        builder = self.world._build_native_parallel()
        next(builder)
        builder.close()
        self.rapier.close.assert_called_once_with()
        self.assertIsNone(self.world._rapier)


class StaticRayBatchTests(unittest.TestCase):
    def setUp(self):
        self.world = CollisionWorld.__new__(CollisionWorld)
        self.world.cid = 17
        self.world._rapier = None
        self.world._ray_threads = 2

    def test_only_large_batches_use_two_workers_and_preserve_results(self):
        for count, workers in ((1, 1), (127, 1), (128, 2), (512, 2)):
            with self.subTest(count=count):
                starts = [(0., 1., 0.)] * count
                ends = [(0., 0., 0.)] * count
                expected = [(-1, -1, 1., end, (0., 1., 0.)) for end in ends]
                with patch('solbreaker.collision.bullet.rayTestBatch', return_value=expected) as batch:
                    self.assertIs(self.world._static_rays(starts, ends), expected)
                batch.assert_called_once_with(starts, ends, collisionFilterMask=1,
                                              numThreads=workers, physicsClientId=17)

    def test_single_ray_retains_the_scalar_entry_point(self):
        start, end = (0., 1., 0.), (0., 0., 0.)
        hit = (-1, -1, 1., end, (0., 1., 0.))
        with patch('solbreaker.collision.bullet.rayTest', return_value=[hit]) as scalar:
            with patch('solbreaker.collision.bullet.rayTestBatch') as batch:
                self.assertEqual(self.world._static_ray(start, end), hit)
                batch.assert_not_called()
        scalar.assert_called_once_with(start, end, collisionFilterMask=1, physicsClientId=17)

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


if __name__ == '__main__':
    unittest.main()
