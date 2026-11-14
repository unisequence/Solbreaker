"""Exercise the default native owner through CollisionWorld.move."""
import ctypes as C
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from solbreaker.arena import Arena
from solbreaker.collision import CollisionWorld, Destructible, Motion, Pose
from solbreaker.mesh_files import MeshFiles
from solbreaker.native_physics import SensorState, StepInput, StepOutput, library_path, load_library
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


class NativeLibraryTests(unittest.TestCase):
    def test_invalid_backend_setting_is_rejected(self):
        with patch.dict(os.environ, SOLBREAKER_NATIVE_PHYSICS='auto'):
            with self.assertRaisesRegex(ValueError, 'SOLBREAKER_NATIVE_PHYSICS must be 0 or 1'):
                CollisionWorld(None, None)

    def test_platform_selection(self):
        with patch('solbreaker.native_physics.sys.platform', 'win32'):
            self.assertEqual(library_path().name, 'solbreaker_physics_core.dll')
        with patch('solbreaker.native_physics.sys.platform', 'linux'):
            self.assertEqual(library_path().name, 'libsolbreaker_physics_core.so')

    def test_missing_library_reports_required_build(self):
        with patch('solbreaker.native_physics.C.CDLL', side_effect=OSError('missing library')):
            with self.assertRaisesRegex(RuntimeError, 'build_native.py'):
                load_library()

    def test_incompatible_abi_and_missing_export_are_rejected(self):
        library = MagicMock()
        library.solbreaker_physics_abi_sizes.return_value = 0
        with patch('solbreaker.native_physics.C.CDLL', return_value=library):
            with self.assertRaisesRegex(RuntimeError, 'Incompatible native physics ABI'):
                load_library()
            library.solbreaker_physics_abi_sizes.return_value = (
                C.sizeof(StepInput) << 32) | (C.sizeof(StepOutput) << 16) | C.sizeof(SensorState)
            del library.solbreaker_physics_sync
            with self.assertRaisesRegex(RuntimeError, 'solbreaker_physics_sync'):
                load_library()


@unittest.skipUnless(LIBRARY.exists(), 'Build the physics_core library first')
class NativePhysicsTests(unittest.TestCase):
    def world(self, assets, enabled=None, *, gravity=CollisionWorld.GRAVITY):
        with patch.dict(os.environ):
            if enabled is None:
                os.environ.pop('SOLBREAKER_NATIVE_PHYSICS', None)
            else:
                os.environ['SOLBREAKER_NATIVE_PHYSICS'] = str(int(enabled))
            result = CollisionWorld(assets, terrain(),
                vehicles={200:Pose((0.,0.,0.),0.), 201:Pose((15.,0.,10.),0.)},
                experimental_6dof=True, experimental_ground_impacts=True,
                sprung_tracks=True, wall_trampoline=False, gravity=gravity)
        self.addCleanup(result.close)
        return result

    def test_no_flag_uses_native_and_explicit_zero_uses_legacy(self):
        assets = self.assets('flat')
        native, legacy = self.world(assets), self.world(assets, False)
        self.assertIsNotNone(native._native_physics)
        self.assertIsNone(legacy._native_physics)
        native.move(.05, 0., 200, dt=1/60)
        self.assertEqual(native._native_physics.steps, 1)

    def test_gravity_recoil_slides_on_flat_ground_and_air_control_adds_no_lift(self):
        assets = self.assets('flat')
        world = self.world(assets, gravity=2.5)
        arena = SimpleNamespace(world=world)
        vehicle = SimpleNamespace(entity_id=200, definition=assets.definition, pose=world.poses[200])
        shell = SimpleNamespace(caliber=100.)

        Arena._gravity_shell_impulse(arena, vehicle, shell, (0., 0., 1.), recoil=True)
        self.assertTrue(world.motion[200].grounded)
        self.assertAlmostEqual(world.ground_motion[200].velocity[1], -4.2)

        self.assertTrue(world.add_flight_velocity(200, (0., 4., 5.)))
        vehicle.pose = world.poses[200]
        vx, vy, _ = world.motion[200].velocity
        Arena._gravity_air_control(arena, vehicle, 1 | 8, 1/60)
        self.assertGreater(world.motion[200].velocity[0], vx)
        self.assertAlmostEqual(world.motion[200].velocity[1], vy)
        pitch_rate, yaw_rate, _ = world.flight_spin(200)
        self.assertGreater(pitch_rate, 0.)
        self.assertGreater(yaw_rate, 0.)
        world.dynamic_commit(1/60)
        self.assertLess(world.motion[200].velocity[1], vy)

    def test_gravity_damps_takeoff_bank_with_no_input(self):
        for mass in (10_000., 60_000.):
            with self.subTest(mass=mass):
                assets = self.assets('flat')
                definition = assets.definition
                for name in ('hull', 'chassis', 'turret', 'gun', 'engine', 'fuel', 'radio'):
                    getattr(definition, name).mass *= mass / definition.mass
                definition.mass = mass
                world = self.world(assets, gravity=2.5)
                arena = SimpleNamespace(world=world)
                entity = 200
                world.relocate_training_body(entity, Pose((0., 2., 0.), 0., 0., .35))
                self.assertTrue(world.set_flight_velocity(entity, (0., 0., 8.), (0., 0., 1.5)))
                self.assertAlmostEqual(world.dynamic_bodies[entity].mass, mass)
                vehicle = SimpleNamespace(entity_id=entity)
                dt = 1/60
                for _ in range(48):
                    vehicle.pose = world.poses[entity]
                    velocity = world.motion[entity].velocity
                    Arena._gravity_air_control(arena, vehicle, 0, dt)
                    self.assertEqual(world.motion[entity].velocity, velocity)
                    world.dynamic_commit(dt)
                self.assertFalse(world.motion[entity].grounded)
                self.assertLess(abs(world.poses[entity].roll), .08)
                self.assertGreater(world.motion[entity].velocity[2], 7.)

    def test_default_does_not_fall_back_when_native_library_is_missing(self):
        with TemporaryDirectory() as temporary:
            with patch('solbreaker.native_physics.library_path', return_value=Path(temporary)/LIBRARY.name):
                with self.assertRaisesRegex(RuntimeError, 'Native ground physics library missing'):
                    self.world(self.assets('flat'))

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

    def test_native_tracks_cross_a_low_riser_but_stop_at_a_wall(self):
        for height, can_cross in ((.18, True), (.20, True), (.27, True), (.50, False), (1., False)):
            with self.subTest(height=height):
                assets = self.assets('rock')
                assets.vertices = box((-5., 0., 4.), (5., height, 8.))
                world = self.world(assets)
                for _ in range(140):
                    world.move(.09, 0., 200, dt=1/60)
                pose = world.poses[200]
                if can_cross:
                    self.assertGreater(pose.position[2], 10.)
                else:
                    self.assertLess(pose.position[2], 2.1)

    def test_native_tracks_cross_a_low_riser_one_track_first(self):
        for height, can_cross in ((.18, True), (.50, False)):
            for angle in (-math.pi/4, math.pi/4):
                with self.subTest(height=height, angle=angle):
                    assets = self.assets('rock')
                    assets.vertices = box((-20., 0., 4.), (20., height, 20.))
                    world = self.world(assets)
                    pose = world.training_placement(200, 0., 0., angle, stationary=True)
                    world.relocate_training_body(200, pose)
                    for _ in range(350):
                        pose, _ = world.move(.035, 0., 200, dt=1/60)
                    if can_cross:
                        self.assertGreater(pose.position[2], 8.)
                    else:
                        self.assertLess(pose.position[2], 2.5)

    def test_native_tracks_remove_unregistered_breakable_from_all_queries(self):
        assets = self.assets('prop')
        prop = assets.destructibles.pop(7)
        assets.destructibles[-42] = Destructible(-42, prop.name, prop.kind, prop.health,
                                                 prop.vertices, prop.triangles)
        world = self.world(assets)
        start, end = (0., 1., 3.), (0., 1., 6.)
        hit, _, _ = world.static_segment(start, end)
        self.assertEqual(world.breakable_bodies[hit], -42)
        for _ in range(140):
            world.move(.09, 0., 200, dt=1/60)
        self.assertIn(-42, world.destroyed)
        self.assertIsNone(world.static_segment(start, end)[0])
        self.assertGreater(world.poses[200].position[2], 8.)
        self.assertFalse(world.destruction_events[0][0].client_registered)


if __name__ == '__main__':
    unittest.main()
