import itertools
import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    import pybullet as bullet
    from solbreaker import _contact_queries
except ImportError:
    bullet = _contact_queries = None


def reference(api, cid, player, skin, maps, vehicles, observer=None, entity=1):
    low, high = [float('inf')]*3, [float('-inf')]*3
    for link in range(-1, api.getNumJoints(player, physicsClientId=cid)):
        a, b = api.getAABB(player, link, physicsClientId=cid)
        for axis in range(3):
            low[axis] = min(low[axis], a[axis])
            high[axis] = max(high[axis], b[axis])
    low = [value-skin for value in low]
    high = [value+skin for value in high]
    if observer is not None:
        observer(entity, low, high)
    near = api.getOverlappingObjects(low, high, physicsClientId=cid) or []
    other = set(vehicles)-{player}
    bodies = {body for body, _ in near if body in maps or body in other}
    points = []
    for body in bodies:
        points.extend(api.getClosestPoints(player, body, skin, physicsClientId=cid))
    return points


@unittest.skipUnless(bullet is not None and _contact_queries is not None, 'Optional native contact queries unavailable')
class ContactQueriesTests(unittest.TestCase):
    def setUp(self):
        self.cid = bullet.connect(bullet.DIRECT)
        self.addCleanup(bullet.disconnect, self.cid)
        self.skin = .025
        box = bullet.createCollisionShape(bullet.GEOM_BOX, halfExtents=[1., .5, 1.5],
                                          physicsClientId=self.cid)
        self.player = bullet.createMultiBody(baseMass=0, baseCollisionShapeIndex=box,
            linkMasses=[0., 0.], linkCollisionShapeIndices=[box, box],
            linkVisualShapeIndices=[-1, -1], linkPositions=[[-1.2, -.3, 0], [1.2, -.3, 0]],
            linkOrientations=[[0, 0, 0, 1]]*2, linkInertialFramePositions=[[0, 0, 0]]*2,
            linkInertialFrameOrientations=[[0, 0, 0, 1]]*2, linkParentIndices=[0, 0],
            linkJointTypes=[bullet.JOINT_FIXED]*2, linkJointAxis=[[0, 0, 0]]*2,
            physicsClientId=self.cid)
        self.wall = bullet.createMultiBody(baseMass=0, baseCollisionShapeIndex=box,
            basePosition=[3., 0, 0], physicsClientId=self.cid)
        self.other = bullet.createMultiBody(baseMass=0, baseCollisionShapeIndex=box,
            basePosition=[-3., 0, 0], physicsClientId=self.cid)
        self.ignored = bullet.createMultiBody(baseMass=0, baseCollisionShapeIndex=box,
            basePosition=[0., 0, 3], physicsClientId=self.cid)
        self.maps = {self.wall}
        self.vehicles = {10: self.player, 20: self.other}

    def check_pose(self, x, y, yaw):
        bullet.resetBasePositionAndOrientation(self.player, [x, y, 0],
            bullet.getQuaternionFromEuler([.15, yaw, -.2]), physicsClientId=self.cid)
        old_seen, new_seen = [], []
        args = (bullet, self.cid, self.player, self.skin, self.maps, self.vehicles.values())
        expected = reference(*args, lambda *row: old_seen.append(row), 10)
        actual = _contact_queries.gather(*args, lambda *row: new_seen.append(row), 10)
        self.assertEqual(actual, expected)
        self.assertEqual(new_seen, old_seen)
        self.assertNotIn(self.ignored, {row[2] for row in actual})
        return len(actual)

    def test_multilink_contacts_and_normals_match_exactly(self):
        points = sum(self.check_pose(x, y, angle) for x, y, angle in itertools.product(
            [-4., -2., 0., 2., 4.], [-.3, 0., .3, 10.], [-1.1, 0., .7]))
        self.assertGreater(points, 100)

    def test_removed_map_and_live_vehicle_view(self):
        self.check_pose(2., 0., 0.)
        bullet.removeBody(self.wall, physicsClientId=self.cid)
        self.maps.remove(self.wall)
        self.check_pose(2., 0., 0.)
        self.vehicles.pop(20)
        self.check_pose(-2., 0., 0.)

    def test_query_error_and_observer_error_propagate(self):
        args = (bullet, self.cid, self.player, self.skin, self.maps, self.vehicles.values())
        def fail(*unused):
            raise RuntimeError('observer failure')
        with self.assertRaisesRegex(RuntimeError, 'observer failure'):
            _contact_queries.gather(*args, fail, 10)
        with self.assertRaises(bullet.error):
            _contact_queries.gather(bullet, 12345, self.player, self.skin,
                self.maps, self.vehicles.values(), None, 10)
        self.check_pose(2., 0., 0.)

    def test_none_broadphase_and_closest_failure(self):
        functions = {name: getattr(bullet, name) for name in
                     ['getNumJoints', 'getAABB', 'getOverlappingObjects', 'getClosestPoints']}
        api = SimpleNamespace(**functions)
        api.getOverlappingObjects = lambda *a, **kw: None
        args = (api, self.cid, self.player, self.skin, self.maps, self.vehicles.values(), None, 10)
        self.assertEqual(_contact_queries.gather(*args), [])
        api.getOverlappingObjects = lambda *a, **kw: [(self.wall, -1)]
        def fail(*a, **kw):
            raise RuntimeError('closest failure')
        api.getClosestPoints = fail
        with self.assertRaisesRegex(RuntimeError, 'closest failure'):
            _contact_queries.gather(*args)

    def test_world_filter_matches_without_accelerator(self):
        from solbreaker.collision import CollisionWorld, Pose
        world = SimpleNamespace(primary_id=10, bodies=self.vehicles, cid=self.cid,
            SKIN=self.skin, SUSPENSION_TRAVEL=.2, map_bodies=self.maps,
            track_links={10: (0, 1)}, breakable_bodies={},
            _compressed_track_contact=lambda *args: False)
        world.set_pose = lambda body, pose: bullet.resetBasePositionAndOrientation(
            body, pose.position, bullet.getQuaternionFromEuler([0., pose.yaw, 0.]),
            physicsClientId=self.cid)
        for suspension in (False, True):
            pose = Pose((2., 0., 0.), .3)
            actual = CollisionWorld._contacts(world, pose, suspension=suspension)
            with patch('solbreaker.collision._contact_queries', None):
                expected = CollisionWorld._contacts(world, pose, suspension=suspension)
            self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
