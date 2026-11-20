"""Private, temporary OBJ meshes for PyBullet's file-loading API.

PyBullet 3.2.7's extractVertices/extractIndices leak Python references in the
vertices= API. File loading avoids that path. The original triangles and spatial
batches are retained; these files are disposable derivatives of local assets.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile

import numpy as np


def _short_windows_path(path):
    import ctypes
    from ctypes import wintypes

    short_path = ctypes.WinDLL('kernel32').GetShortPathNameW
    short_path.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    short_path.restype = wintypes.DWORD
    length = short_path(path, None, 0)
    if not length:
        return path
    buffer = ctypes.create_unicode_buffer(length)
    if 0 < short_path(path, buffer, length) < length:
        return buffer.value
    return path


def _bullet_windows_path(path):
    if path.isascii():
        return path
    short = _short_windows_path(path)
    if short.isascii():
        return short
    try:
        relative = os.path.relpath(short)
    except ValueError:
        return None
    return relative if relative.isascii() else None


@contextmanager
def bullet_mesh_paths(paths):
    """Bridge Python's Unicode paths to Bullet 3.2.7's narrow Windows fopen."""
    if sys.platform != 'win32':
        yield paths
        return
    compatible = [_bullet_windows_path(path) for path in paths]
    if None not in compatible:
        yield compatible
        return

    # Some volumes disable short aliases. Stage only the files Bullet can't
    # open, in a directory owned for the duration of its synchronous reads.
    temp_root = tempfile.gettempdir()
    if _bullet_windows_path(temp_root) is None:
        temp_root = os.getcwd()
    with tempfile.TemporaryDirectory(prefix='solbreaker-bullet-', dir=temp_root) as root:
        for index, original in enumerate(paths):
            if compatible[index] is None:
                staged = Path(root) / f'{index}.obj'
                shutil.copyfile(original, staged)
                compatible[index] = _bullet_windows_path(str(staged))
                if compatible[index] is None:
                    raise OSError('Could not create a Windows-compatible Bullet mesh path')
        yield compatible


def create_bullet_mesh(bullet, path, **kwargs):
    with bullet_mesh_paths([path]) as (filename,):
        return bullet.createCollisionShape(bullet.GEOM_MESH, fileName=filename, **kwargs)


class MeshFiles:
    def __init__(self, output=None):
        self.directory = (tempfile.TemporaryDirectory(prefix='solbreaker-collision-')
                          if output is None else None)
        self.output = Path(self.directory.name) if self.directory else Path(output)
        self.partitions = {}
        self.prepared = None
        self.closed = False

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                if self.directory is not None:
                    self.directory.cleanup()
            finally:
                if self.prepared is not None:
                    self.prepared.close()

    def mesh(self, vertices, triangles=None):
        path, _ = self.mesh_with_creation_status(vertices, triangles)
        return path

    def mesh_with_creation_status(self, vertices, triangles=None):
        """Return ``(path, created)`` for a caller that owns temporary inputs.

        Shared cached meshes must not be unlinked by a short-lived Bullet
        shape builder. ``created`` is true only when this call wrote the file.
        """
        if self.closed:
            raise ValueError('Collision mesh cache is closed')
        vertices = np.asarray(vertices, dtype=np.float64)
        # Convex hull loading only needs the vertex cloud. A triangle fan makes
        # every vertex part of the OBJ mesh without changing the hull.
        if triangles is None:
            triangles = np.column_stack((np.zeros(len(vertices)-2, dtype=np.int32),
                                         np.arange(1,len(vertices)-1),
                                         np.arange(2,len(vertices))))
        triangles = np.asarray(triangles, dtype=np.int32)
        key = hashlib.sha256(vertices.tobytes()+triangles.tobytes()).hexdigest()
        name = key+'.obj'
        if self.prepared is not None and name in self.prepared.files:
            return str(self.prepared.path / name), False
        path = self.output / name
        created = not path.exists()
        if created:
            with path.open('x') as stream:
                for vertex in vertices:
                    stream.write('v '+' '.join(format(float(v),'.17g') for v in vertex)+'\n')
                for face in triangles:
                    stream.write('f '+' '.join(str(int(v)+1) for v in face)+'\n')
        return str(path), created

    def partition(self, vertices, triangles):
        key = hashlib.sha256(vertices.tobytes()+triangles.tobytes()).digest()
        if key not in self.partitions:
            centers = vertices[triangles].mean(axis=1)
            cells = np.floor(centers[:,(0,2)]/32).astype(np.int32)
            keys = cells[:,0]*4096+cells[:,1]
            order = np.argsort(keys)
            splits = np.r_[0,np.flatnonzero(np.diff(keys[order]))+1,len(order)]
            paths = []
            for left,right in zip(splits[:-1],splits[1:]):
                for start in range(left,right,8000):
                    faces = triangles[order[start:min(start+8000,right)]]
                    used,inverse = np.unique(faces,return_inverse=True)
                    paths.append(self.mesh(vertices[used],inverse.reshape(-1,3)))
            self.partitions[key] = tuple(paths)
        return self.partitions[key]
