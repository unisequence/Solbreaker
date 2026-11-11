"""Large static-ray batches share bounded Bullet workers without changing hits."""
import unittest
from unittest.mock import patch

from solbreaker.collision import CollisionWorld


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


if __name__ == '__main__':
    unittest.main()
