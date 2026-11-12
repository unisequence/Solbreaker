"""Immutable navigation inputs stored in the map cache's derived entries."""
from __future__ import annotations

import hashlib
import json

import numpy as np

# Bump when sampling, rasterisation, edge or landmark rules change.
FORMAT = 1


def sample_coordinates(bounds, cell, nx, nz):
    x0, z0 = bounds[:2]
    return np.meshgrid(x0+(np.arange(nx)+.5)*cell, z0+(np.arange(nz)+.5)*cell)


class NavigationCache:
    def __init__(self, prepared, *, may_evict=lambda: True):
        self.map = prepared
        self.may_evict = may_evict
        self.hits = self.misses = 0

    def key(self, purpose, *parameters):
        # The map key includes the source digests and exact terrain samples.
        payload = (self.map.path.name, FORMAT, purpose, parameters)
        return hashlib.sha256(json.dumps(payload, separators=(',', ':')).encode()).hexdigest()

    def get(self, key, build):
        arrays, hit = yield from self.map.cache.derive(key, build, may_evict=self.may_evict)
        self.hits += int(hit)
        self.misses += int(not hit)
        # The in-process arena loader can schedule a tick between entries.
        yield
        return arrays

    def heights(self, bounds, cell, nx, nz, sample):
        def build():
            xs, zs = sample_coordinates(bounds, cell, nx, nz)
            yield
            return {'heights': sample(xs.ravel(), zs.ravel()).reshape(nz, nx)}

        key = self.key('heights', cell, nx, nz)
        arrays = yield from self.get(key, build)
        return arrays['heights']


def prewarm(prepared, terrain, *, may_evict):
    from .bot_navigation import Navigation

    cache = NavigationCache(prepared, may_evict=may_evict)
    bounds = prepared.document['bounds']
    cell, nx, nz = Navigation.dimensions(bounds)
    if nx*nz > Navigation.MAX_CELLS or min(nx, nz) < 2:
        return {}
    grid = np.asarray(terrain.heights, dtype=float)
    build = cache.heights(bounds, cell, nx, nz, lambda x, z: terrain.sample_grid(x, z, grid))
    for _ in build:
        pass
    return dict(nav_cache_hits=cache.hits, nav_cache_misses=cache.misses)
