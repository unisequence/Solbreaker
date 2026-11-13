"""Versioned, disposable static map assets shared by arena processes."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import time
import zipfile

import numpy as np

from .mesh_files import MeshFiles

LOG = logging.getLogger(__name__)
# Bump for changes to scene filtering, geometry, terrain, mesh partitioning or OBJ export.
FORMAT = 1
DEFAULT_BYTES = 1024 << 20
KEY = re.compile(r'[0-9a-f]{64}')
OBJ = re.compile(r'[0-9a-f]{64}\.obj')
MANIFEST = 'map.json'
ARRAYS = 'geometry.npz'
DERIVED_ARRAYS = 'derived.npz'
MAX_DERIVED_BYTES = 64 << 20
INVALID = (OSError, ValueError, KeyError, TypeError, OverflowError, EOFError, zipfile.BadZipFile)


def _try_lock(path, *, shared=False):
    """Return an open file holding an OS lock, or None on contention."""
    stream = path.open('a+b')
    try:
        if os.name == 'nt':
            from .map_cache_lock_win import try_lock
            if not try_lock(stream, shared):
                stream.close()
                return None
        else:
            import fcntl
            mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            try:
                fcntl.flock(stream, mode | fcntl.LOCK_NB)
            except BlockingIOError:
                stream.close()
                return None
    except BaseException:
        stream.close()
        raise
    return stream


def _digest(path):
    with path.open('rb') as source:
        size = os.fstat(source.fileno()).st_size
        return [size, hashlib.file_digest(source, 'sha256').hexdigest()]


def _fingerprint(spec, terrain):
    from .map_catalog import WEATHER_MAPS, _SUPPORTED

    source_files = (spec.scene_path, spec.geometry_path, spec.lka_path, spec.heightmap_path,
                    spec.mkm_path, spec.effects_path)
    # The caller may have decoded terrain before a local asset edit. Include
    # the actual samples used to export its collision mesh as well as the
    # source files that were present at the time of this lookup.
    heights = np.asarray(terrain.heights, dtype='<u2')
    key = WEATHER_MAPS[spec.key][1] if spec.key in WEATHER_MAPS else spec.key
    map_id = _SUPPORTED[key] if key != spec.key else spec.map_id
    identity = (FORMAT, map_id, key, spec.state_tag, spec.allow_empty_obstacles,
                hashlib.sha256(heights.tobytes()).hexdigest(),
                spec.bounds, spec.landscape_bbox, [_digest(Path(path)) for path in source_files])
    return hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()


@dataclass
class PreparedMap:
    path: Path
    lease: object
    files: dict
    document: dict
    size: int
    cache: MapCache

    def close(self):
        self.lease.close()

    def restore(self, scene, terrain):
        from .collision import Destructible

        data = self.document
        with np.load(self.path / ARRAYS, allow_pickle=False) as arrays:
            scene.vertices, scene.triangles = arrays['vertices'], arrays['triangles']
            vertices, triangles = arrays['object_vertices'], arrays['object_triangles']
        objects = {}
        for key, name, kind, health, v0, v1, f0, f1 in data['objects']:
            objects[key] = Destructible(key, name, kind, health, vertices[v0:v1], triangles[f0:f1])
        partitions = {bytes.fromhex(key): tuple(str(self.path / name) for name in names)
                      for key, names in data['partitions'].items()}
        terrain_paths = tuple(str(self.path / name) for name in data['terrain'])
        scene.bounds = tuple(data['bounds'])
        scene.object_names = data['names']
        scene.destructibles = objects
        scene.terrain_meshes[terrain] = terrain_paths
        scene.mesh_files.partitions.update(partitions)
        scene.mesh_files.prepared = self


@dataclass(frozen=True)
class MapCache:
    root: Path
    max_bytes: int = DEFAULT_BYTES

    def _lock(self, key, *, shared=False):
        # Lock files are never removed. Recreating one while another process
        # holds it would split the lock. These contain no asset data.
        return _try_lock(self.root / 'locks' / key, shared=shared)

    def _open(self, path, key, lease):
        with (path / MANIFEST).open('rb') as source:
            checksum = source.readline(66).rstrip(b'\n')
            raw = source.read(16 << 20)
            if source.read(1):
                raise ValueError('Map manifest exceeds its size limit')
        if hashlib.sha256(raw).hexdigest().encode() != checksum:
            raise ValueError('Map cache manifest checksum failed')
        data = json.loads(raw)
        if data['key'] != key or data['format'] != FORMAT:
            raise ValueError('Map cache identity mismatch')
        files = data['files']
        if not isinstance(files, dict) or ARRAYS not in files:
            raise ValueError('Map cache has no geometry')
        size = len(raw) + len(checksum) + 1
        for name, expected in files.items():
            if name != ARRAYS and not OBJ.fullmatch(name):
                raise ValueError('Invalid map cache filename')
            asset = path / name
            if asset.is_symlink() or _digest(asset) != expected:
                raise ValueError(f'Map cache checksum failed for {name}')
            size += expected[0]
        for digest, names in data['partitions'].items():
            if not KEY.fullmatch(digest) or not isinstance(names, list):
                raise ValueError('Invalid cached mesh partition')
            if any(name not in files or not OBJ.fullmatch(name) for name in names):
                raise ValueError('Missing cached partition mesh')
        if any(name not in files or not OBJ.fullmatch(name) for name in data['terrain']):
            raise ValueError('Missing cached terrain mesh')
        # Touch the directory for eviction order. Cache use remains valid on a
        # filesystem that permits reads but rejects this metadata update.
        try:
            os.utime(path)
        except OSError:
            pass
        return PreparedMap(path, lease, files, data, size, self)

    def _write(self, stage, key, spec, terrain):
        from .collision import CollisionScene
        from .static_visibility import mesh_inputs

        started = time.monotonic()
        scene = CollisionScene.map_only(spec, MeshFiles(stage))
        objects = []
        vertices, triangles = [], []
        v, f = 0, 0
        for obj in scene.destructibles.values():
            next_v, next_f = v + len(obj.vertices), f + len(obj.triangles)
            objects.append((obj.key, obj.name, obj.kind, obj.health, v, next_v, f, next_f))
            vertices.append(obj.vertices)
            triangles.append(obj.triangles)
            v, f = next_v, next_f
        np.savez(stage / ARRAYS, vertices=scene.vertices, triangles=scene.triangles,
                 object_vertices=np.concatenate(vertices) if vertices else np.empty((0, 3), float),
                 object_triangles=np.concatenate(triangles) if triangles else np.empty((0, 3), np.int32))
        geometry_ms = (time.monotonic() - started)*1000
        terrain_paths = scene.prepare_map(terrain)
        for _ in mesh_inputs(scene):
            pass
        files = {path.name: _digest(path) for path in stage.iterdir()}
        data = dict(key=key, format=FORMAT, bounds=scene.bounds, names=scene.object_names,
                    objects=objects, terrain=[Path(path).name for path in terrain_paths],
                    partitions={digest.hex(): [Path(path).name for path in paths]
                                for digest, paths in scene.mesh_files.partitions.items()},
                    files=files)
        payload = json.dumps(data, separators=(',', ':')).encode()
        encoded = hashlib.sha256(payload).hexdigest().encode() + b'\n' + payload
        (stage / MANIFEST).write_bytes(encoded)
        return len(encoded) + sum(size for size, _ in files.values()), geometry_ms

    def cleanup_stages(self, keep=None):
        try:
            entries = tuple(self.root.glob('.*.stage'))
            for entry in entries:
                key = entry.name[1:-6]
                if entry == keep or not KEY.fullmatch(key):
                    continue
                lease = self._lock(key)
                if lease:
                    with lease:
                        shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            # A read-only or unavailable cache never prevents server shutdown.
            pass

    def _publish(self, stage, path, size, may_evict):
        budget = None
        while budget is None:
            budget = self._lock('budget')
            if budget is None:
                time.sleep(.05)
        with budget:
            self.cleanup_stages(keep=stage)
            entries = []
            for entry in self.root.iterdir():
                name = entry.name
                if not KEY.fullmatch(name) or not entry.is_dir() or entry.is_symlink():
                    continue
                try:
                    with os.scandir(entry) as assets:
                        used = sum(asset.stat(follow_symlinks=False).st_size for asset in assets)
                    entries.append((entry.stat().st_mtime_ns, used, entry))
                except FileNotFoundError:
                    continue
            required = size + sum(used for _, used, _ in entries)
            if size > self.max_bytes:
                return 'oversize'
            if required > self.max_bytes and not may_evict():
                return 'budget'
            for _, used, entry in sorted(entries):
                if required <= self.max_bytes:
                    break
                lease = self._lock(entry.name)
                if lease is not None:
                    with lease:
                        try:
                            shutil.rmtree(entry)
                        except OSError:
                            continue
                        required -= used
            if required > self.max_bytes:
                return 'leased'
            stage.rename(path)
            return None

    def acquire(self, spec, terrain, *, may_evict=lambda: True):
        started = time.monotonic()
        cpu = time.thread_time()
        key = _fingerprint(spec, terrain)
        (self.root / 'locks').mkdir(parents=True, exist_ok=True)
        path = self.root / key
        stage = self.root / f'.{key}.stage'
        built = False
        geometry_ms = 0.
        unavailable = 'leased'
        while True:
            bad = False
            lease = self._lock(key, shared=True)
            if lease is not None:
                try:
                    if path.exists():
                        prepared = self._open(path, key, lease)
                        status = 'cache_miss' if built else 'cache_hit'
                        return prepared, dict(cache=status, cache_bytes=prepared.size,
                            cache_objects=len(prepared.document['objects']), geometry_ms=geometry_ms,
                            cache_cpu_ms=(time.thread_time()-cpu)*1000,
                            cache_ms=(time.monotonic()-started)*1000)
                except INVALID:
                    bad = True
                except BaseException:
                    lease.close()
                    raise
                lease.close()

            writer = self._lock(key)
            if writer is None:
                if bad:
                    # An earlier arena may still be using the old files.
                    # This arena can build private inputs while it drains.
                    break
                time.sleep(.05)
                continue
            with writer:
                if path.exists():
                    try:
                        self._open(path, key, writer)
                    except INVALID:
                        shutil.rmtree(path)
                    else:
                        # A writer won the race since our last read. Release
                        # this exclusive lock before opening a shared lease.
                        continue
                shutil.rmtree(stage, ignore_errors=True)
                stage.mkdir()
                try:
                    size, geometry_ms = self._write(stage, key, spec, terrain)
                    if key != _fingerprint(spec, terrain):
                        raise ValueError('Map resources changed during cache preparation')
                    unavailable = self._publish(stage, path, size, may_evict)
                    if unavailable is not None:
                        break
                    built = True
                finally:
                    if stage.exists():
                        shutil.rmtree(stage, ignore_errors=True)
        return None, dict(cache='cache_miss', cache_bytes=0, cache_objects=0, geometry_ms=geometry_ms,
                          cache_cpu_ms=(time.thread_time()-cpu)*1000,
                          cache_ms=(time.monotonic()-started)*1000, cache_unavailable=unavailable)

    def load(self, scene, terrain):
        try:
            prepared, metrics = self.acquire(scene.map_spec, terrain)
            if prepared is not None:
                try:
                    prepared.restore(scene, terrain)
                except BaseException:
                    prepared.close()
                    raise
            return metrics
        except INVALID as error:
            LOG.warning('Map cache unavailable for %s: %s', scene.map_spec.key, error)
            return dict(cache='cache_miss', cache_bytes=0, cache_objects=0)

    @staticmethod
    def _read_derived(path):
        file = path / DERIVED_ARRAYS
        if file.is_symlink():
            raise ValueError('Invalid derived cache path')
        with file.open('rb') as stream:
            size = os.fstat(stream.fileno()).st_size - 65
            if not 0 <= size <= MAX_DERIVED_BYTES:
                raise ValueError('Derived cache exceeds its size limit')
            checksum = stream.readline(66)
            raw = stream.read(size+1)
        digest = hashlib.sha256(path.name.encode())
        digest.update(raw)
        if len(raw) != size or digest.hexdigest().encode()+b'\n' != checksum:
            raise ValueError('Derived cache checksum failed')
        with np.load(io.BytesIO(raw), allow_pickle=False) as arrays:
            result = {name: arrays[name] for name in arrays.files}
        try:
            os.utime(path)
        except OSError:
            pass
        return result

    def derive(self, key, build, *, may_evict=lambda: True):
        """Reuse a map-derived array set under the same locks and disk budget.

        The builder yields at its usual cooperative boundaries. Readers copy
        arrays before releasing the lease, so these entries can be evicted
        while a battle is using the reconstructed data.
        """
        if not KEY.fullmatch(key):
            raise ValueError('Invalid derived cache key')
        path = self.root / key
        stage = self.root / f'.{key}.stage'
        while True:
            try:
                lease = self._lock(key, shared=True)
                if lease is not None:
                    with lease:
                        try:
                            return self._read_derived(path), True
                        except INVALID:
                            pass
                writer = self._lock(key)
            except OSError:
                return (yield from build()), False
            if writer is None:
                # An arena owner drives its generator in a tight loop.
                # Bound lock polling even outside the legacy async loader.
                time.sleep(.001)
                yield
                continue
            with writer:
                try:
                    return self._read_derived(path), True
                except INVALID:
                    pass
                arrays = yield from build()
                try:
                    if path.exists():
                        shutil.rmtree(path)
                    shutil.rmtree(stage, ignore_errors=True)
                    stage.mkdir()
                    buffer = io.BytesIO()
                    np.savez(buffer, **arrays)
                    raw = buffer.getbuffer()
                    if len(raw) <= MAX_DERIVED_BYTES:
                        digest = hashlib.sha256(key.encode())
                        digest.update(raw)
                        with (stage / DERIVED_ARRAYS).open('wb') as stream:
                            stream.write(digest.hexdigest().encode()+b'\n')
                            stream.write(raw)
                        self._publish(stage, path, len(raw)+65, may_evict)
                except OSError as error:
                    LOG.debug('Could not persist derived map data: %s', error)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage, ignore_errors=True)
                return arrays, False
