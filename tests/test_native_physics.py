"""Exercise the experimental native owner through CollisionWorld.move."""
import ctypes as C
import math
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from solbreaker.collision import CollisionWorld, Destructible, Motion, Pose
from solbreaker.mesh_files import MeshFiles
from solbreaker.native_physics import StepInput, StepOutput
from solbreaker.physics_proxy import vehicle_proxies
from solbreaker.terrain import MapTerrain


def box(low, high):
    return np.asarray([(x,y,z) for x in (low[0],high[0])
        for y in (low[1],high[1]) for z in (low[2],high[2])], dtype=float)


FACES = np.asarray(((0,1,3),(0,3,2),(4,6,7),(4,7,5),
    (0,4,5),(0,5,1),(2,3,7),(2,7,6),(0,2,6),(0,6,4),(1,5,7),(1,7,3)), dtype=np.int32)
LIBRARY = Path(__file__).resolve().parents[1]/'solbreaker/native/physics_core'/(
    'solbreaker_physics_core.dll' if os.name == 'nt' else 'libsolbreaker_physics_core.so')


class QueryAssets:
    def __init__(self, scenario):
        self.mesh_files = MeshFiles()
        self.vertices = np.empty((0,3))
        self.triangles = np.empty((0,3), dtype=np.int32)
        if scenario in ('wall','rock'):
            self.vertices = box((-5.,0.,4.), (5.,3. if scenario == 'wall' else .2,4.25))
            self.triangles = FACES
        self.destructibles = {}
        if scenario == 'prop':
            self.destructibles[7] = Destructible(7, 'prop', 3, 100,
                box((-1.,0.,4.), (1.,2.,5.)), FACES)
        self.tank_shapes = (
            ('hull', box((-.7,.45,-1.8), (.7,1.5,1.8))),
            ('chassis_track_L', box((-1.1,0.,-2.), (-.7,.6,2.))),
            ('chassis_track_R', box((.7,0.,-2.), (1.1,.6,2.))),
            ('turret', box((-.55,0.,-.7), (.55,.55,.7))))
        self.turret_node = 'turret'
        self.hull_center = (0.,.975,0.)
        self.support, self.max_slope = (1.,2.), math.radians(35)
        self.forward_speed, self.reverse_speed, self.rotation_speed = 20.,8.,1.
        self.bounds = (-100.,-100.,100.,100.)
        self.definition = SimpleNamespace(mass=10000.,
            hull=SimpleNamespace(mass=3000.), chassis=SimpleNamespace(mass=3000.),
            turret=SimpleNamespace(mass=1000.), gun=SimpleNamespace(mass=500.),
            engine=SimpleNamespace(mass=1500.), fuel=SimpleNamespace(mass=500.),
            radio=SimpleNamespace(mass=500.), aiming=SimpleNamespace(turret_position=(.3,1.5,.2)))
        self.contact_proxies = vehicle_proxies(self.tank_shapes, self.turret_node, self.support)

    def prepare_terrain(self, terrain):
        return ()


def terrain():
    result = object.__new__(MapTerrain)
    result.size = 64
    result.MIN = result.MIN_Z = -64.
    result.WIDTH = result.DEPTH = 128.
    result.HEIGHT_MIN, result.HEIGHT = -8.,16.
    result.heights = [32768]*(result.size*result.size)
    return result


@unittest.skipUnless(LIBRARY.exists(), 'Build the experimental physics_core library first')
class NativePhysicsTests(unittest.TestCase):
    def world(self, assets, enabled=True):
        with patch.dict(os.environ, {'SOLBREAKER_NATIVE_PHYSICS':str(int(enabled))}):
            result = CollisionWorld(assets, terrain(),
                vehicles={200:Pose((0.,0.,0.),0.), 201:Pose((15.,0.,10.),0.)},
                experimental_6dof=True, experimental_ground_impacts=True,
                sprung_tracks=True, wall_trampoline=False)
        self.addCleanup(result.close)
        return result

    def assets(self, scenario):
        result = QueryAssets(scenario)
        self.addCleanup(result.mesh_files.close)
        return result

    def test_ground_movement_matches_existing_wall_and_prop_paths(self):
        for scenario in ('flat','wall','prop'):
            with self.subTest(scenario=scenario):
                assets = self.assets(scenario)
                baseline, native = self.world(assets, False), self.world(assets)
                for frame in range(180):
                    for world in (baseline,native):
                        world.begin_ground_step()
                        world.move(.05, .002 if frame>120 else 0., 200, dt=1/60)
                    a,b = baseline.poses[200], native.poses[200]
                    np.testing.assert_allclose((*a.position,a.yaw,a.pitch,a.roll),
                        (*b.position,b.yaw,b.pitch,b.roll), atol=1e-7, rtol=0.)
                    self.assertEqual(baseline.last_contact, native.last_contact)
                self.assertEqual(baseline.destroyed, native.destroyed)

    def test_ground_contact_loop_does_not_call_python_solver(self):
        world = self.world(self.assets('wall'))
        with patch.object(world, '_drive', side_effect=AssertionError('Python drive called')), \
             patch.object(world, '_contacts', side_effect=AssertionError('Python contacts called')), \
             patch.object(world, '_support_pose', side_effect=AssertionError('Python support called')):
            for _ in range(160):
                world.move(.05,0.,200,dt=1/60)
        self.assertEqual(world._native_physics.steps, 160)
        self.assertLess(world.poses[200].position[2], 3.)

    def test_external_pose_and_turret_changes_reach_native_queries(self):
        world = self.world(self.assets('flat'))
        pose = Pose((0.,0.,2.), .3)
        world.poses[201] = pose
        world.set_pose(world.bodies[201], pose)
        world.set_turret(.7,201)
        world.move(.01,0.,200,dt=1/60)
        self.assertTrue(world._native_physics.query(operation='contacts',entity=200,skin=world.SKIN))

    def test_invalid_command_preserves_world_and_can_be_retried(self):
        world = self.world(self.assets('flat'))
        physics = world._native_physics
        before = world.poses[200]
        command = StepInput()  # Deliberately rejected ABI/version.
        output = StepOutput()
        status = physics.library.solbreaker_physics_step(physics.handle, C.byref(command), C.byref(output))
        self.assertEqual(status, -3)
        self.assertEqual(world.poses[200], before)
        self.assertTrue(world.move(.05,0.,200,dt=1/60)[1])
        physics.close()
        physics.close()
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            physics.sync(world)

    def test_python_handoff_imports_support_recoil_and_carrying_state(self):
        world = self.world(self.assets('rock'))
        pose = Pose((0.,.1,2.5), 0.)
        world.poses[200] = pose
        world.motion[200] = Motion()
        world._road_poses[200] = pose
        world.set_pose(world.bodies[200], pose)
        supports = [(next(iter(world.map_bodies)), .8, 4., .2)]
        latch = (0.,2.5,0.,-1.)
        world._contact_support_cache[200] = supports[:]
        world._wall_recoil_latches[200] = latch
        world._carrying_supported[200] = False
        world.begin_ground_step()
        world.move(0.,0.,200,dt=1/60)
        self.assertEqual(world._contact_support_cache[200], supports)
        self.assertEqual(world._wall_recoil_latches[200], latch)
        self.assertFalse(world._carrying_supported[200])

        world._contact_support_cache[200] = []
        world._wall_recoil_latches.pop(200)
        world._carrying_supported[200] = True
        world.move(0.,0.,200,dt=1/60)
        self.assertEqual(world._contact_support_cache[200], [])
        self.assertNotIn(200, world._wall_recoil_latches)
        self.assertTrue(world._carrying_supported[200])

    def test_destroyed_support_is_removed_from_handoff(self):
        world = self.world(self.assets('prop'))
        body = next(iter(world.object_bodies[7]))
        world.destroy(7,(0.,1.))
        world._contact_support_cache[200] = [(body,.8,1.,.2)]
        world.move(0.,0.,200,dt=1/60)
        self.assertEqual(world._contact_support_cache[200], [])

    def test_oversized_subdivision_preserves_native_state_and_can_be_retried(self):
        world = self.world(self.assets('flat'))
        physics = world._native_physics
        for distance, yaw, displacement in (
                (world.STEP*513,0.,None),
                (0.,math.radians(.75)*513,None),
                (0.,0.,(world.STEP*513,0.))):
            with self.subTest(distance=distance, yaw=yaw, displacement=displacement):
                before = world.poses[200]
                native_before = physics.query(operation='state',entity=200)
                with self.assertRaisesRegex(RuntimeError, 'Invalid native movement subdivision'):
                    world.move(distance,yaw,200,dt=1/60,planar_displacement=displacement)
                self.assertEqual(world.poses[200], before)
                self.assertEqual(physics.query(operation='state',entity=200), native_before)
                self.assertTrue(world.move(.05,0.,200,dt=1/60)[1])

    def test_numerical_rock_contact_stays_finite(self):
        world = self.world(self.assets('rock'))
        for frame in range(360):
            world.move(.05 if frame<240 else -.05,
                .002 if 120<=frame<180 else 0.,200,dt=1/60)
            pose = world.poses[200]
            self.assertTrue(all(math.isfinite(v) for v in (*pose.position,pose.yaw,pose.pitch,pose.roll)))
            self.assertGreater(pose.position[1], -.12)
            self.assertLess(pose.position[1], .6)


if __name__ == '__main__':
    unittest.main()
