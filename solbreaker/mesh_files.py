"""Private, temporary OBJ meshes for PyBullet's file-loading API.

PyBullet 3.2.7's extractVertices/extractIndices leak Python references in the
vertices= API. File loading avoids that path. The original triangles and spatial
batches are retained; these files are disposable derivatives of local assets.
"""
import hashlib
from pathlib import Path
import tempfile

import numpy as np


class MeshFiles:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='solbreaker-collision-')
        self.partitions = {}
        self.closed = False

    def close(self):
        if not self.closed:
            self.directory.cleanup()
            self.closed = True

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
        path = Path(self.directory.name)/(key+'.obj')
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
