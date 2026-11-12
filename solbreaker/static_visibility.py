"""Solid-map Bullet meshes for batched bot visibility queries.

One BVH covers the solid map. Destructible objects keep independent bodies
so their lifetime follows the main world. Clear segments need a terrain query.
Body identifiers in returned hits belong to this query world.
"""
import pybullet as bullet


def mesh_inputs(assets):
    if len(assets.triangles):
        yield None, assets.mesh_files.mesh(assets.vertices, assets.triangles)
    for key, obj in assets.destructibles.items():
        if len(obj.triangles):
            yield key, assets.mesh_files.mesh(obj.vertices, obj.triangles)


class VisibilityWorld:
    def __init__(self):
        self.cid = bullet.connect(bullet.DIRECT)
        if self.cid < 0:
            raise RuntimeError('Could not create visibility query world')
        self.breakables = {}
        self.shape_count = 0

    def _mesh(self, path):
        shape = bullet.createCollisionShape(bullet.GEOM_MESH, fileName=path,
            flags=bullet.GEOM_FORCE_CONCAVE_TRIMESH, physicsClientId=self.cid)
        body = bullet.createMultiBody(baseMass=0, baseCollisionShapeIndex=shape,
                                      physicsClientId=self.cid)
        bullet.setCollisionFilterGroupMask(body, -1, 1, -1, physicsClientId=self.cid)
        return body

    def build(self, assets):
        for key, path in mesh_inputs(assets):
            body = self._mesh(path)
            self.shape_count += 1
            if key is not None:
                self.breakables[key] = body
            yield

    def rays(self, starts, ends, threads):
        return bullet.rayTestBatch(starts, ends, collisionFilterMask=1,
                                   numThreads=threads, physicsClientId=self.cid)

    def destroy(self, key):
        body = self.breakables.get(key)
        if body is not None:
            bullet.removeBody(body, physicsClientId=self.cid)
            del self.breakables[key]

    def close(self):
        if self.cid is not None:
            bullet.disconnect(self.cid)
            self.cid = None
            self.breakables.clear()
