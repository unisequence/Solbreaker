from dataclasses import replace
import asyncio
import multiprocessing as mp
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np

from solbreaker.collision import CollisionScene, Destructible
from solbreaker.map_cache import MapCache, MANIFEST
from solbreaker.map_prewarm import MapPrewarm
from solbreaker.map_catalog import MapSpec
from solbreaker.mesh_files import MeshFiles
from solbreaker.terrain import MapTerrain


def fixture(root, map_id=6):
    base = root / str(map_id)
    base.mkdir()
    files = [base / name for name in ('scene', 'geometry', 'lka', 'height', 'mkm', 'effects')]
    for path in files:
        path.write_bytes(b'fixture')
    files[3].write_bytes(struct.pack('<II4H', 2, 1, 0, 1, 2, 3))
    files[4].write_bytes(struct.pack('<4sIIIII', b'kkm\0', 1, 2, 1, 0, 0) + b'\0')
    return MapSpec(f'map{map_id}', map_id, '', '', base, *files,
                   (-4, -4, 4, 4), (-4, -4, -1, 4, 4, 1), ((), ()), 'fixture')


def map_only(spec, files):
    scene = CollisionScene.__new__(CollisionScene)
    scene._init_map(spec, files)
    scene.bounds = spec.bounds
    scene.vertices = np.array(((0., 0., 0.), (0., 2., 0.), (0., 0., 2.)))
    scene.triangles = np.array(((0, 1, 2),), dtype=np.int32)
    scene.object_names = ['wall']
    scene.destructibles = {7: Destructible(7, 'wall', 3, 12, scene.vertices+1, scene.triangles.copy())}
    return scene


class TestCacheStore(MapCache):
    def _write(self, stage, key, spec, terrain):
        with (self.root / 'builds').open('a') as stream:
            stream.write(f'{spec.map_id}\n')
        with patch.object(CollisionScene, 'map_only', side_effect=map_only):
            return super()._write(stage, key, spec, terrain)


class SlowStore(TestCacheStore):
    def _write(self, stage, key, spec, terrain):
        (self.root / f'entered-{spec.map_id}').touch()
        if spec.map_id == 6:
            while not (self.root / 'continue-6').exists():
                time.sleep(.05)
        if spec.map_id == 8:
            raise ValueError('invalid map')
        return super()._write(stage, key, spec, terrain)


def acquire(cache, spec, gate, queue):
    gate.wait(10)
    prepared, metrics = cache.acquire(spec, MapTerrain(spec.map_root, spec))
    try:
        queue.put(metrics['cache'])
    finally:
        prepared.close()


class MapCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cache = TestCacheStore(self.root / 'cache')
        self.spec = fixture(self.root)
        self.terrain = MapTerrain(self.root, self.spec)

    def test_reuses_exact_arrays_and_obj_after_reopen(self):
        prepared, cold = self.cache.acquire(self.spec, self.terrain)
        path = prepared.path
        prepared.close()
        scene = CollisionScene.__new__(CollisionScene)
        scene._init_map(self.spec, MeshFiles())
        self.addCleanup(scene.close)
        metrics = replace(self.cache).load(scene, self.terrain)
        self.assertEqual((cold['cache'], metrics['cache']), ('cache_miss', 'cache_hit'))
        self.assertEqual(scene.mesh_files.prepared.path, path)
        reference = map_only(self.spec, MeshFiles())
        self.addCleanup(reference.close)
        np.testing.assert_array_equal(scene.vertices, reference.vertices)
        np.testing.assert_array_equal(scene.destructibles[7].triangles, reference.destructibles[7].triangles)
        expected = [Path(name).read_bytes() for name in reference.prepare_map(self.terrain)]
        actual = [Path(name).read_bytes() for name in scene.prepare_map(self.terrain)]
        self.assertEqual(actual, expected)
        self.assertEqual(list(scene.mesh_files.output.iterdir()), [])
        self.assertEqual((self.cache.root / 'builds').read_text().splitlines(), ['6'])

    def test_damaged_obj_and_manifest_rebuild_and_source_change_invalidates(self):
        entry, _ = self.cache.acquire(self.spec, self.terrain)
        path = entry.path
        entry.close()
        for damaged in (next(path.glob('*.obj')), path / MANIFEST):
            damaged.write_bytes(b'bad')
            rebuilt, metrics = self.cache.acquire(self.spec, self.terrain)
            self.assertEqual(metrics['cache'], 'cache_miss')
            self.assertEqual(rebuilt.path, path)
            rebuilt.close()
        self.spec.geometry_path.write_bytes(b'updated source')
        replaced, metrics = self.cache.acquire(self.spec, self.terrain)
        self.assertEqual(metrics['cache'], 'cache_miss')
        self.assertNotEqual(replaced.path, path)
        replaced.close()

    def test_limit_evicts_idle_maps_and_preserves_active_leases(self):
        active, metrics = self.cache.acquire(self.spec, self.terrain)
        size = metrics['cache_bytes']
        active_path = active.path
        other = fixture(self.root, 8)
        terrain = MapTerrain(self.root, other)
        small = replace(self.cache, max_bytes=size+64)
        try:
            refused, _ = small.acquire(other, terrain)
            self.assertIsNone(refused)
            self.assertTrue(active_path.exists())
        finally:
            active.close()
        prepared, _ = small.acquire(other, terrain)
        self.assertIsNotNone(prepared)
        prepared.close()
        self.assertFalse(active_path.exists())

    def test_speculative_preparation_never_evicts_a_ready_map(self):
        prepared, metrics = self.cache.acquire(self.spec, self.terrain)
        path = prepared.path
        prepared.close()
        other = fixture(self.root, 8)
        small = replace(self.cache, max_bytes=metrics['cache_bytes']+64)
        refused, detail = small.acquire(other, MapTerrain(self.root, other), may_evict=lambda: False)
        self.assertIsNone(refused)
        self.assertEqual(detail['cache_unavailable'], 'budget')
        self.assertTrue(path.exists())

    def test_two_processes_share_one_builder(self):
        context = mp.get_context('spawn')
        gate, queue = context.Event(), context.Queue()
        children = [context.Process(target=acquire, args=(self.cache, self.spec, gate, queue))
                    for _ in range(2)]
        try:
            for child in children:
                child.start()
            gate.set()
            self.assertCountEqual((queue.get(timeout=15), queue.get(timeout=15)), ('cache_miss', 'cache_hit'))
        finally:
            for child in children:
                if child.pid is None:
                    continue
                child.join(2)
                if child.is_alive():
                    child.terminate()
                    child.join(2)
                child.close()
            queue.close()
        self.assertEqual((self.cache.root / 'builds').read_text().splitlines(), ['6'])


class MapPrewarmTests(unittest.IsolatedAsyncioTestCase):
    async def await_request(self, queue, spec):
        demand = queue.request(spec)
        try:
            await asyncio.wait_for(asyncio.shield(demand.ready), 15)
        finally:
            queue.release(demand)

    async def test_active_battle_priority_failure_isolation_and_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            maps = {i: fixture(root, i) for i in (6, 8, 9)}
            cache = SlowStore(root / 'cache')
            active = False
            events = []
            queue = MapPrewarm(cache, root, {i: spec.key for i, spec in maps.items()},
                maps, 6, enabled=True, busy=lambda: active,
                event=lambda name, **fields: events.append(fields) if name == 'map_preparation' else None)

            async def until(predicate):
                async with asyncio.timeout(15):
                    while not predicate():
                        await asyncio.sleep(.025)

            queue.start()
            try:
                await until(lambda: (cache.root / 'entered-6').exists())
                active = True
                first = asyncio.create_task(self.await_request(queue, maps[9]))
                second = asyncio.create_task(self.await_request(queue, maps[9]))
                await asyncio.wait_for(asyncio.gather(first, second), 15)
                self.assertEqual(queue.jobs[9].state, 'ready')
                self.assertEqual(queue.jobs[6].state, 'queued')
                self.assertEqual(queue.jobs[8].state, 'queued')
                # Run an explicitly requested failing map while the battle is
                # active. Its failure must release both the caller and worker.
                await self.await_request(queue, maps[8])
                self.assertEqual(queue.jobs[8].state, 'failed')
                self.assertEqual((cache.root / 'builds').read_text().splitlines(), ['9'])
                active = False
                await until(lambda: queue.current is queue.jobs[6])
            finally:
                await queue.close()
            self.assertIsNone(queue.process)
            self.assertEqual(list(cache.root.glob('.*.stage')), [])
            self.assertEqual([row['phase'] for row in events[:2]], ['ready', 'failed'])

    async def test_promotes_the_worker_already_preparing_the_requested_map(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = fixture(root)
            cache = SlowStore(root / 'cache')
            active = False
            queue = MapPrewarm(cache, root, {6: spec.key}, {6: spec}, 6, enabled=True,
                               busy=lambda: active, event=lambda *a, **kw: None)
            queue.start()
            try:
                async with asyncio.timeout(15):
                    while not (cache.root / 'entered-6').exists():
                        await asyncio.sleep(.025)
                active = True
                process = queue.process
                first, second = queue.request(spec), queue.request(spec)
                self.assertIs(first, second)
                await asyncio.sleep(.1)
                self.assertIs(queue.process, process)
                self.assertTrue(process.is_alive())
                (cache.root / 'continue-6').touch()
                self.assertEqual(await asyncio.wait_for(first.ready, 15), 'ready')
                queue.release(first)
                queue.release(second)
            finally:
                await queue.close()
            self.assertEqual((cache.root / 'builds').read_text().splitlines(), ['6'])


if __name__ == '__main__':
    unittest.main()
