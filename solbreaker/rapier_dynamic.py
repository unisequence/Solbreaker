"""Minimal JSON ABI for the optional native Rapier dynamic world."""
import ctypes as C
import json
import math
import sys
from functools import wraps
import threading
from pathlib import Path
import numpy as np


def library_path():
    name = 'tfs_rapier_dynamic.dll' if sys.platform == 'win32' else 'libtfs_rapier_dynamic.so'
    return Path(__file__).with_name('native')/'rapier_dynamic'/name


class _StaticRayHit(C.Structure):
    _fields_ = [('body', C.c_int64), ('fraction', C.c_double),
                ('point', C.c_double*3), ('normal', C.c_double*3)]


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class RapierDynamic:
    def __init__(self, fixture_path):
        self.library = C.CDLL(str(library_path()))
        self.library.tfs_rapier_create.argtypes = [C.c_char_p]
        self.library.tfs_rapier_create.restype = C.c_void_p
        self.library.tfs_rapier_activate.argtypes = [C.c_void_p, C.c_uint32,
            C.POINTER(C.c_double), C.POINTER(C.c_double),
            C.POINTER(C.c_double), C.POINTER(C.c_double)]
        self.library.tfs_rapier_activate.restype = C.c_int32
        self.library.tfs_rapier_set_mass.argtypes = [C.c_void_p, C.c_uint32, C.POINTER(C.c_double)]
        self.library.tfs_rapier_set_mass.restype = C.c_int32
        self.library.tfs_rapier_sync.argtypes = [C.c_void_p, C.c_char_p]
        self.library.tfs_rapier_sync.restype = C.c_int32
        self.library.tfs_rapier_step.argtypes = [C.c_void_p, C.c_double, C.c_char_p, C.c_size_t]
        self.library.tfs_rapier_step.restype = C.c_int32
        self.library.tfs_rapier_deactivate.argtypes = [C.c_void_p, C.c_uint32]
        self.library.tfs_rapier_deactivate.restype = C.c_int32
        self.library.tfs_rapier_destroy.argtypes = [C.c_void_p, C.c_char_p]
        self.library.tfs_rapier_destroy.restype = None
        self.library.tfs_rapier_close.argtypes = [C.c_void_p]
        self.library.tfs_rapier_close.restype = None
        self._static_rays = getattr(self.library, 'tfs_rapier_static_rays', None)
        if self._static_rays is not None:
            size = self.library.tfs_rapier_ray_hit_size
            size.argtypes, size.restype = [], C.c_size_t
            if size() != C.sizeof(_StaticRayHit):
                raise RuntimeError('Rapier static ray ABI mismatch')
            self._static_rays.argtypes = [C.c_void_p, C.POINTER(C.c_double),
                                          C.c_size_t, C.POINTER(_StaticRayHit)]
            self._static_rays.restype = C.c_int32
        self.handle = self.library.tfs_rapier_create(str(fixture_path).encode())
        if not self.handle:
            raise RuntimeError('Rapier dynamic world creation failed')
        self.closed = False
        # CDLL releases the GIL: protect the native mutable World and the
        # reusable output buffer, including close racing an in-flight step.
        self._lock = threading.RLock()
        self._output = C.create_string_buffer(4*1024*1024)
        self._ray_capacity = 0

    def _require_open(self):
        if self.closed or not self.handle:
            raise RuntimeError('Rapier dynamic world is closed')

    @staticmethod
    def _array(values, size=3):
        values = tuple(values)
        if len(values) != size or not all(math.isfinite(value) for value in values):
            raise ValueError('Invalid Rapier vector')
        return (C.c_double*size)(*values)

    @_serialized
    def activate(self, entity, position, orientation, velocity, angular):
        self._require_open()
        if type(entity) is not int or not 0 <= entity <= 0xffffffff:
            raise ValueError('Invalid Rapier entity')
        rotation = self._array(orientation, 4)
        if sum(value*value for value in rotation) < 1e-20:
            raise ValueError('Invalid Rapier orientation')
        result = self.library.tfs_rapier_activate(self.handle, entity,
            self._array(position), rotation,
            self._array(velocity), self._array(angular))
        if result != 0:
            raise RuntimeError(f'Rapier dynamic activation failed: {result}')

    @_serialized
    def set_mass(self, entity, mass, center, inertia):
        self._require_open()
        if type(entity) is not int or not 0 <= entity <= 0xffffffff:
            raise ValueError('Invalid Rapier entity')
        values = self._array((mass, *center, *inertia), 7)
        if values[0] <= 0 or any(values[index] <= 0 for index in (4, 5, 6)):
            raise ValueError('Invalid Rapier mass properties')
        result = self.library.tfs_rapier_set_mass(self.handle, entity, values)
        if result != 0:
            raise RuntimeError(f'Rapier mass update failed: {result}')

    @_serialized
    def sync(self, rows):
        self._require_open()
        if self.library.tfs_rapier_sync(self.handle, json.dumps(rows, allow_nan=False).encode()) != 0:
            raise RuntimeError('Rapier sensor sync failed')

    @_serialized
    def step(self, dt):
        self._require_open()
        if isinstance(dt, bool) or not isinstance(dt, (int, float)) or not math.isfinite(dt) or dt <= 0:
            raise ValueError('Invalid Rapier timestep')
        output = self._output
        capacity = len(output)
        result = self.library.tfs_rapier_step(self.handle, dt, output, capacity)
        if result <= 0 or result >= capacity:
            raise RuntimeError(f'Rapier dynamic step failed: {result}')
        return json.loads(C.string_at(C.addressof(output), result))

    @_serialized
    def deactivate(self, entity):
        self._require_open()
        if self.library.tfs_rapier_deactivate(self.handle, entity) != 0:
            raise RuntimeError('Rapier dynamic deactivation failed')

    @_serialized
    def static_rays(self, starts, ends):
        """Bullet-shaped mask=1 results; vehicles are excluded by native code."""
        self._require_open()
        starts, ends = tuple(starts), tuple(ends)
        if len(starts) != len(ends) or len(starts) > 65536:
            raise ValueError('Invalid Rapier ray batch')
        count = len(starts)
        if not count:
            return []
        if self._static_rays is None:
            raise RuntimeError('Rapier library lacks static ray ABI')
        if count > self._ray_capacity:
            self._ray_capacity = max(count, 64)
            self._rays_in = np.empty((self._ray_capacity, 6), dtype=np.float64)
            self._rays_in_ptr = self._rays_in.ctypes.data_as(C.POINTER(C.c_double))
            self._rays_out = (_StaticRayHit*self._ray_capacity)()
        try:
            if np.iscomplexobj(starts) or np.iscomplexobj(ends):
                raise ValueError('Invalid Rapier ray')
            starts, ends = np.asarray(starts, dtype=float), np.asarray(ends, dtype=float)
            if starts.shape != (count,3) or ends.shape != (count,3):
                raise ValueError('Invalid Rapier ray')
            self._rays_in[:count,:3] = starts
            self._rays_in[:count,3:] = ends
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('Invalid Rapier ray') from exc
        result = self._static_rays(self.handle, self._rays_in_ptr, count, self._rays_out)
        if result == -2:
            raise ValueError('Invalid Rapier ray')
        if result != 0:
            raise RuntimeError('Rapier static ray query failed')
        return [(int(row.body), -1, row.fraction, tuple(row.point), tuple(row.normal))
                for row in self._rays_out[:count]]

    @_serialized
    def destroy(self, key):
        self._require_open()
        self.library.tfs_rapier_destroy(self.handle, str(key).encode())

    @_serialized
    def close(self):
        if not self.closed:
            self.closed = True
            self.library.tfs_rapier_close(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
