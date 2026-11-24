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

from solbreaker import collision, mad_games
from solbreaker.arena import Arena
from solbreaker.armor import ArmorHit, ShotResult
from solbreaker.ballistics import Impact
from solbreaker.big_boss import BigBoss, GravityTrap
from solbreaker.collision import CollisionWorld, Destructible, Motion, Pose, matrix, quaternion
from solbreaker.consumables import GRAVITY_JUMP_SPEC, ConsumableState
from solbreaker.mesh_files import MeshFiles
from solbreaker.native_physics import SensorState, StepInput, StepOutput, library_path, load_library
from solbreaker.physics_proxy import vehicle_proxies
from solbreaker.rapier_dynamic import (
    RapierStepRows, _decode_packed_step, _PACKED_CONTACT, _PACKED_HEADER, _PACKED_STATE,
)
from solbreaker.terrain import MapTerrain
from solbreaker.vehicle_data import MissileFlight, Shell
from solbreaker.vector_math import cross3_f64


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


class MassModelTests(unittest.TestCase):
    def test_cache_keeps_the_exact_mass_model_at_different_turret_angles(self):
        from solbreaker.mass_properties import VehicleMassModel, from_named_meshes
        from solbreaker.orientation import matrix

        assets = QueryAssets('flat')
        self.addCleanup(assets.mesh_files.close)
        model = VehicleMassModel(assets.definition, assets.tank_shapes, assets.turret_node)
        masses = {'hull': 5500., 'chassis_track_L': 1500., 'chassis_track_R': 1500.,
                  'turret': 1500.}
        for yaw in (0., .32, -1.7, 2.9, 0.):
            with self.subTest(yaw=yaw):
                pivot = np.asarray(assets.definition.aiming.turret_position)
                shapes = tuple((name, vertices @ matrix(yaw).T + pivot
                                if name == assets.turret_node else vertices)
                               for name, vertices in assets.tank_shapes)
                self.assertEqual(model.at_yaw(yaw), from_named_meshes(shapes, masses))
        self.assertIs(model.at_yaw(0.), model.at_yaw(0.))


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
    @staticmethod
    def flight_center_velocity(world, entity):
        pose = world.poses[entity]
        offset = matrix(pose.yaw, pose.pitch, pose.roll) @ world.dynamic_bodies[entity].center_of_mass
        return world._center_velocity(world.motion[entity].velocity, world.flight_spin(entity), offset)

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

    def test_ground_skid_builds_with_speed_and_recovers_after_steering(self):
        assets = self.assets('flat')
        assets.forward_speed = 60. / 3.6
        assets.rotation_speed = math.radians(35.)
        world = self.world(assets)
        arena = Arena([dict(id=1, nickname='Pilot'), dict(id=2, nickname='Peer')],
                      b'', world.terrain, now=1., solo=False, room_mode=mad_games.MODE)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        arena.started = 1.
        arena.world = world
        vehicle = arena.vehicle_for(1)
        entity = vehicle.entity_id
        self.assertTrue(arena._activate_consumable(vehicle, vehicle.consumables[3], 2.))

        def drive(start, kmh, turn):
            world.relocate_training_body(entity, Pose((0., 0., 0.), 0.))
            vehicle.pose = world.poses[entity]
            vehicle.speed = kmh / 3.6
            world.motion[entity].velocity = (0., 0., vehicle.speed)
            slips = []
            for frame in range(120):
                now = start + frame * arena.STEP
                arena.simulation_time = vehicle.last_input = now
                vehicle.flags = 1 | (turn if frame < 36 else 0)
                arena._drive(vehicle, arena.STEP, now)
                vx, _, vz = world.motion[entity].velocity
                slips.append(math.degrees(math.remainder(vehicle.pose.yaw - math.atan2(vx, vz), math.tau)))
            return slips

        slow = drive(2., 10., 8)
        fast = drive(4., 120., 8)
        mirrored = drive(6., 120., 4)
        self.assertGreater(fast[35], 1.5 * max(map(abs, slow)))
        self.assertGreater(fast[35], 10.)
        self.assertGreater(fast[35], fast[17] + 1.)
        self.assertGreater(fast[36], 1.)
        self.assertAlmostEqual(fast[35], -mirrored[35], delta=.2)
        self.assertLess(abs(fast[-1]), .25 * fast[35])

    def test_gravity_rearward_shot_pushes_hull_forward_and_air_control_preserves_velocity(self):
        assets = self.assets('flat')
        world = self.world(assets, gravity=2.5)
        arena = SimpleNamespace(world=world)
        vehicle = SimpleNamespace(entity_id=200, definition=assets.definition, pose=world.poses[200])
        shell = SimpleNamespace(caliber=100.)

        Arena._gravity_shell_impulse(arena, vehicle, shell, (0., 0., -1.), recoil=True)
        self.assertFalse(world.motion[200].grounded)
        self.assertGreater(world.motion[200].velocity[2], 4.)
        for _ in range(18):
            world.dynamic_commit(1/60)
        self.assertGreater(world.poses[200].position[2], 1.)

        self.assertTrue(world.add_flight_velocity(200, (0., 4., 5.)))
        vehicle.pose = world.poses[200]
        velocity = self.flight_center_velocity(world, 200)
        Arena._gravity_air_control(arena, vehicle, 1 | 8, 1/60)
        np.testing.assert_allclose(self.flight_center_velocity(world, 200), velocity, atol=1e-9)
        pitch_rate, yaw_rate, _ = world.flight_spin(200)
        self.assertGreater(pitch_rate, 0.)
        self.assertGreater(yaw_rate, 0.)
        world.dynamic_commit(1/60)
        self.assertLess(self.flight_center_velocity(world, 200)[1], velocity[1])

    def test_gravity_holding_forward_levels_a_tumbling_tank_without_thrust(self):
        world = self.world(self.assets('flat'), gravity=2.5)
        arena = SimpleNamespace(world=world)
        entity = 200
        world.relocate_training_body(entity, Pose((0., 20., 0.), 0., .9, -.8))
        self.assertTrue(world.set_flight_velocity(entity, (0., 0., 6.), (2., 0., -.8)))
        vehicle = SimpleNamespace(entity_id=entity, pose=world.poses[entity])

        for _ in range(120):
            vehicle.pose = world.poses[entity]
            velocity = self.flight_center_velocity(world, entity)
            Arena._gravity_air_control(arena, vehicle, 1, 1/60)
            np.testing.assert_allclose(self.flight_center_velocity(world, entity), velocity, atol=1e-9)
            world.dynamic_commit(1/60)

        pose = world.poses[entity]
        self.assertLess(abs(pose.pitch), .25)
        self.assertLess(abs(pose.roll), .1)
        self.assertLess(self.flight_center_velocity(world, entity)[1], -4.)
        self.assertLess(pose.position[1], 16.)

    def test_gravity_side_shot_rolls_away_and_scales_with_caliber_and_mass(self):
        assets = self.assets('flat')
        world = self.world(assets, gravity=2.5)
        arena = SimpleNamespace(world=world)
        entity = 200
        pose = world.poses[entity]
        vehicle = SimpleNamespace(entity_id=entity, pose=pose, definition=assets.definition)

        def roll_rate(direction, caliber, mass):
            world.relocate_training_body(entity, pose)
            vehicle.definition.mass = mass
            Arena._gravity_shell_impulse(arena, vehicle, SimpleNamespace(caliber=caliber),
                                         direction, recoil=True)
            return world.flight_spin(entity)[2]

        right = roll_rate((1., 0., 0.), 100., 40_000.)
        left = roll_rate((-1., 0., 0.), 100., 40_000.)
        self.assertGreater(right, 0.)
        self.assertAlmostEqual(left, -right)
        self.assertGreater(roll_rate((1., 0., 0.), 152., 40_000.), right)
        self.assertLess(roll_rate((1., 0., 0.), 100., 65_000.), right)
        self.assertAlmostEqual(roll_rate((0., 0., 1.), 100., 40_000.), 0.)

    def test_gravity_descent_aligns_to_terrain_without_changing_linear_momentum(self):
        world = self.world(self.assets('flat'), gravity=2.5)
        arena = SimpleNamespace(world=world)
        entity = 200
        vehicle = SimpleNamespace(entity_id=entity)
        world.relocate_training_body(entity, Pose((0., 2., 0.), 0.))
        self.assertTrue(world.set_flight_velocity(entity, (0., -.6, 0.), (0., 0., 0.)))

        with patch.object(world.terrain, 'height', side_effect=lambda x, z: .12*x + .2*z):
            pitch, roll, _ = world.landing_attitude(entity, world.poses[entity], 3.)
            self.assertLess(pitch, 0.)
            self.assertGreater(roll, 0.)
            self.assertIsNone(world.landing_attitude(entity, Pose((0., 6., 0.), 0.), 3.))
            for _ in range(40):
                vehicle.pose = world.poses[entity]
                velocity = self.flight_center_velocity(world, entity)
                Arena._gravity_air_control(arena, vehicle, 1, 1/60)
                np.testing.assert_allclose(self.flight_center_velocity(world, entity), velocity, atol=1e-9)
                world.dynamic_commit(1/60)

        pose = world.poses[entity]
        self.assertAlmostEqual(pose.pitch, pitch, delta=.07)
        self.assertAlmostEqual(pose.roll, roll, delta=.05)

    def test_gravity_thruster_adds_to_airborne_momentum_and_obeys_cooldown(self):
        for rigid in (False, True):
            with self.subTest(rigid=rigid):
                assets = self.assets('flat')
                world = self.world(assets, gravity=2.5)
                arena = Arena([dict(id=1, nickname='Pilot'), dict(id=2, nickname='Opponent')],
                              b'', world.terrain, now=1., solo=False, room_mode='gravity')
                self.addCleanup(arena.close)
                arena.ready(1, 1.)
                arena.ready(2, 1.)
                arena.world = world
                vehicle = arena.vehicle_for(1)
                entity = vehicle.entity_id
                vehicle.pose = Pose((0., 12., 0.), 0.)
                vehicle.definition = assets.definition
                state = ConsumableState(GRAVITY_JUMP_SPEC)
                vehicle.consumables = (state,)
                velocity, spin = (2., -4., 7.), (.2, .1, .3)
                world.relocate_training_body(entity, vehicle.pose, motion=Motion(velocity, False))
                if rigid:
                    self.assertTrue(world.set_flight_velocity(entity, velocity, spin))
                else:
                    world.short_flight_spins[entity] = spin
                arena.drain_events()

                self.assertTrue(arena.use_consumable(1, state.spec.ident, now=1.))
                delta = next(event.data['velocity'] for event in arena.drain_events()
                             if event.kind == 'gravity_jump')
                boosted = tuple(before + added for before, added in zip(velocity, delta))
                self.assertEqual(world.motion[entity].velocity, boosted)
                self.assertEqual(entity in world.dynamic_bodies, rigid)
                actual_spin = world.flight_spin(entity) if rigid else world.short_flight_spins[entity]
                np.testing.assert_allclose(actual_spin, spin, atol=1e-9)
                self.assertFalse(arena.use_consumable(1, state.spec.ident, now=1.))
                self.assertEqual(world.motion[entity].velocity, boosted)

    def test_gravity_incoming_shell_and_guided_rocket_move_a_braked_tank(self):
        assets = self.assets('flat')
        assets.definition.mass = 60_000.
        world = self.world(assets, gravity=2.5)
        arena = Arena([dict(id=1, nickname='Player'), dict(id=2, nickname='Enemy')],
                      b'', world.terrain, now=1., solo=False, room_mode='gravity')
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        arena.ready(2, 1.)
        arena.world = world
        target, shooter = (arena.vehicle_for(i) for i in (1, 2))
        target.definition = assets.definition
        entity = target.entity_id
        pose = world.poses[entity]
        hit_point = (0., .55, -1.8)
        contact = ArmorHit(0., hit_point, (0., 0., -1.), 'hull', 1, 200., 1.)
        result = ShotResult('not_penetrated', contact)
        health = target.health

        distances = []
        for shot, missile in enumerate((None, MissileFlight(400., 20., 2., 0.)), 1):
            world.relocate_training_body(entity, pose)
            shell = Shell(1, 152., 500., (150., 200.), None, None, 200., 0., 500., missile=missile)
            if missile is not None:
                shooter.definition = assets.definition
                with patch.object(arena, 'shot_dispersion', return_value=0.):
                    arena._launch(shooter, shell, 1.9)
                self.assertTrue(world.motion[shooter.entity_id].grounded)
                self.assertEqual(world.motion[shooter.entity_id].velocity, (0., 0., 0.))
                self.assertFalse(world.ground_motion[shooter.entity_id].active)
            arena._impact(Impact(2., shot, result, hit_point, None, (0., -.3, 1.),
                                 shooter.entity_id, entity, shell, collision=True))
            self.assertFalse(world.motion[entity].grounded)
            self.assertGreater(world.motion[entity].velocity[1], 0.)
            for _ in range(18):
                world.dynamic_commit(1/60)
            distances.append(world.poses[entity].position[2] - pose.position[2])
            self.assertEqual(target.health, health)

        self.assertGreater(distances[0], .7)
        self.assertGreater(distances[1], 1.7 * distances[0])

    def test_graviton_damps_fast_tanks_and_retains_ground_controls(self):
        assets = self.assets('flat')
        world = self.world(assets)
        arena = Arena([dict(id=1, nickname='Pilot')], b'', terrain(), now=1.)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        arena.world = world
        vehicle = arena.vehicle_for(1)
        vehicle.definition = assets.definition
        entity = vehicle.entity_id
        arena.big_boss = BigBoss({}, 'test')
        arena.big_boss.traps.append(GravityTrap(1, 201, 3-vehicle.team, (0., 0., 0.), 4, 8.))
        dt = arena.STEP

        def drive(flags, steps):
            vehicle.flags = flags
            for frame in range(steps):
                now = 1. + frame*dt
                vehicle.last_input = now
                arena._drive(vehicle, dt, now)

        world.relocate_training_body(entity, Pose((0., 0., 0.), 0.))
        vehicle.pose = world.poses[entity]
        drive(1 | 8, 30)
        self.assertGreater(vehicle.pose.position[2], .2)
        self.assertGreater(vehicle.pose.yaw, .2)
        self.assertLess(vehicle.pose.yaw, .4)

        world.relocate_training_body(entity, Pose((0., 0., 20.), 0.))
        vehicle.pose = world.poses[entity]
        vehicle.sandbox_boost = True
        vehicle.speed = 36.
        world.motion[entity].velocity = (0., 0., 36.)
        drive(1, 30)
        self.assertLess(vehicle.pose.position[2], 25.)
        self.assertLess(vehicle.speed, 0.)

        self.assertTrue(world.set_flight_velocity(entity, (0., 4., 36.), (0., 0., 0.)))
        drive(1, 1)
        _, vertical, forward = world.motion[entity].velocity
        self.assertAlmostEqual(vertical, 4.)
        self.assertLess(forward, 32.)

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
                    velocity = self.flight_center_velocity(world, entity)
                    Arena._gravity_air_control(arena, vehicle, 0, dt)
                    np.testing.assert_allclose(self.flight_center_velocity(world, entity), velocity, atol=1e-9)
                    world.dynamic_commit(dt)
                self.assertFalse(world.motion[entity].grounded)
                self.assertLess(abs(world.poses[entity].roll), .08)
                self.assertGreater(world.motion[entity].velocity[2], 7.)

    def test_ragdoll_recovery_skips_a_clear_pose_without_track_support(self):
        world = self.world(self.assets('flat'))
        entity = 200
        pose = world.poses[entity]
        self.assertTrue(world.set_flight_velocity(entity, (0., -.1, 0.), (0., 0., 0.)))
        with patch.object(world, '_can_carry_origin', side_effect=(False, True)) as can_carry:
            self.assertTrue(world._recover_dynamic(entity, world.dynamic_bodies[entity],
                                                   pose, matrix(pose.yaw, pose.pitch, pose.roll)))
        self.assertEqual(can_carry.call_count, 2)
        location = world.poses[entity].position
        self.assertAlmostEqual(math.dist(pose.position[::2], location[::2]), .75)
        self.assertTrue(world.motion[entity].grounded)
        self.assertNotIn(entity, world.dynamic_bodies)

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

    def test_oblique_wall_contact_turns_and_keeps_tangent_motion(self):
        assets = self.assets('wall')
        assets.vertices = box((-30.,0.,4.), (30.,3.,4.25))
        world = self.world(assets)
        yaw = math.radians(67.)
        start = world.training_placement(200, -4., 1., yaw, stationary=True)
        world.relocate_training_body(200, start)
        for _ in range(45):
            pose, _ = world.move(.15, 0., 200, dt=1/60)
        self.assertGreater(pose.yaw, math.radians(83.))
        self.assertGreater(pose.position[0]-start.position[0], 5.)
        self.assertLess(pose.position[2], 3.1)
        self.assertTrue(world.motion[200].grounded)

    def test_stationary_hull_can_pivot_around_a_wall_contact(self):
        world = self.world(self.assets('wall'))
        for _ in range(32):
            world.move(.08, 0., 200, dt=1/60)
        start = world.poses[200]
        for _ in range(20):
            pose, _ = world.move(0., math.radians(.65), 200, dt=1/60)
        self.assertAlmostEqual(math.degrees(pose.yaw-start.yaw), 13.)
        self.assertLess(pose.position[2],start.position[2]-.10)
        self.assertTrue(all(point[8] > -.03
                            for point in world._contacts(pose,200,suspension=True)))

    @unittest.skipUnless(getattr(collision._contact_queries, 'API_VERSION', None) == 2,
                         'requires contact collector API 2')
    def test_pivot_rejection_matches_full_wall_contact_validation(self):
        world = self.world(self.assets('wall'))
        entity = 200
        for _ in range(32):
            world.move(.08, 0., entity, dt=1/60)
        previous = world.poses[entity]
        x, height, z_wall = previous.position
        outcomes = set()
        for z in (z_wall - .8, z_wall, z_wall + .3, z_wall + .8):
            for yaw in (-.02, 0., .02):
                candidate = Pose((x, height, z), yaw)
                with self.subTest(z=z, yaw=yaw):
                    full, _ = world._valid(candidate, previous, entity=entity, suspension=True)
                    early, points = world._valid(candidate, previous, entity=entity,
                                                 suspension=True, rejection_only=True)
                    self.assertEqual(early, full)
                    self.assertEqual(points, [])
                    outcomes.add(full)
        self.assertEqual(outcomes, {False, True})

    @unittest.skipUnless(getattr(collision._contact_queries, 'API_VERSION', None) == 2,
                         'requires contact collector API 2')
    def test_pivot_cache_preserves_contacts_as_other_tank_moves(self):
        assets = self.assets('wall')
        world = self.world(assets)
        entity, peer = 200, 201
        pose = Pose((0., .001, 2.7), .07)
        cache = collision._PivotMapPairQueries(world, world.bodies[entity], assets)
        peer_contact = []
        for z in (10., 2.8, 10.):
            world.relocate_training_body(peer, Pose((.4, 0., z), 0.))
            for angle in (0., .37, 0.):
                world.set_turret(angle, entity=entity)
                cache.turret = world.gun_angles[entity][0]
                expected = world._contacts(pose, entity, suspension=True)
                actual = world._contacts(pose, entity, suspension=True, pair_queries=cache)
                with self.subTest(peer_z=z, turret_angle=angle):
                    self.assertEqual(actual, expected)
                    self.assertTrue(any(point[2] in world.map_bodies for point in actual))
                    peer_contact.append(any(point[2] == world.bodies[peer] for point in actual))
        self.assertEqual(peer_contact, [False] * 3 + [True] * 3 + [False] * 3)

    def test_ram_scrubs_speed_after_measuring_damage_energy(self):
        world = self.world(self.assets('flat'))
        a,b = 200,201
        pose_a, pose_b = Pose((0.,0.,0.),0.), Pose((0.,0.,4.),0.)
        contact = {'normal':(0.,0.,-1.), 'point_a':(0.,0.,2.), 'point_b':(0.,0.,2.),
                   'pose_a':pose_a, 'pose_b':pose_b, 'separation':0.}
        with patch.object(world,'_ground_contact_pairs',return_value={(a,b):contact}):
            updates = world.resolve_ground_impacts({a:((0.,10.),0.), b:((0.,0.),0.)})
        impact, = world.last_ground_impacts
        self.assertAlmostEqual(updates[a][0][1],impact['velocity_a'][1]*.65)
        self.assertAlmostEqual(updates[b][0][1],impact['velocity_b'][1]*.65)
        self.assertTrue(impact['ram'])
        self.assertGreater(impact['ram_energy'],240000.)

    def test_ram_rearms_after_a_sustained_gap(self):
        world = self.world(self.assets('flat'))
        a, b = 200, 201
        pair = (a, b)
        contact = {'normal': (0., 0., -1.), 'point_a': (0., 0., 2.), 'point_b': (0., 0., 2.),
                   'pose_a': Pose((0., 0., 0.), 0.), 'pose_b': Pose((0., 0., 4.), 0.),
                   'separation': 0.}
        states = {a: ((0., 10.), 0.), b: ((0., 0.), 0.)}
        with patch.object(world, '_ground_contact_pairs', return_value={pair: contact}) as collect:
            world.resolve_ground_impacts(states)
            self.assertTrue(world.last_ground_impacts[0]['ram'])
            collect.return_value = {}
            world.resolve_ground_impacts(states)
            collect.return_value = {pair: contact}
            updates = world.resolve_ground_impacts(states)
            self.assertFalse(world.last_ground_impacts[0]['ram'])
            self.assertAlmostEqual(updates[b][0][1], world.last_ground_impacts[0]['velocity_b'][1])
            collect.return_value = {}
            for _ in range(world.RAM_REARM_CLEAR_STEPS):
                world.resolve_ground_impacts(states)
            collect.return_value = {pair: contact}
            world.resolve_ground_impacts(states)
            self.assertTrue(world.last_ground_impacts[0]['ram'])

    def test_accelerator_adds_momentum_during_flight(self):
        for backend, flags, direction in (('bullet', 0, 1), ('rapier', 0, 1), ('swept', 2, -1)):
            dynamic_backend = 'rapier' if backend == 'rapier' else 'bullet'
            with self.subTest(backend=backend), patch.dict(os.environ, SOLBREAKER_DYNAMIC_BACKEND=dynamic_backend):
                world = self.world(self.assets('flat'))
                if backend == 'rapier' and world._rapier is None:
                    continue
                arena = Arena([dict(id=1, nickname='Pilot'), dict(id=2, nickname='Peer')],
                              b'', world.terrain, now=2., solo=False, room_mode=mad_games.MODE)
                self.addCleanup(arena.close)
                arena.ready(1, 2.)
                arena.ready(2, 2.)
                arena.started = 2.
                arena.world = world
                flyer = arena.vehicle_for(1)
                entity = flyer.entity_id
                with patch('solbreaker.mad_games.vehicle_abilities', return_value=('dash',)):
                    mad_games.install_ability(flyer, 'lightTank')
                state = flyer.consumables[3]
                flyer.flags, flyer.last_input = flags, 2.
                flyer.pose = Pose((0., 8., 0.), 0.)
                velocity, spin = (3., 2., 10.), (.1, .2, .3)
                world.relocate_training_body(entity, flyer.pose, motion=Motion(velocity, False))
                if backend == 'swept':
                    world.short_flight_spins[entity] = spin
                else:
                    self.assertTrue(world.set_flight_velocity(entity, velocity, spin))

                self.assertTrue(arena.use_consumable(1, state.spec.ident, now=2.))
                boosted = (3., 2., 10. + direction * mad_games.DASH_VELOCITY_BOOST)
                self.assertEqual(world.motion[entity].velocity, boosted)
                np.testing.assert_allclose(
                    world.short_flight_spins[entity] if backend == 'swept' else world.flight_spin(entity),
                    spin, atol=1e-6)
                self.assertFalse(arena.use_consumable(1, state.spec.ident, now=2.))
                self.assertEqual(state.uses, 1)
                self.assertEqual(world.motion[entity].velocity, boosted)

                arena._fixed_step(2. + arena.STEP)
                self.assertGreater(direction * flyer.pose.position[2], .25)
                self.assertLess(world.motion[entity].velocity[1], velocity[1])
                self.assertFalse(world.motion[entity].grounded)

    def test_airborne_ram_damages_both_tanks_once(self):
        for backend in ('bullet', 'rapier', 'swept'):
            dynamic_backend = 'rapier' if backend == 'rapier' else 'bullet'
            with self.subTest(backend=backend), patch.dict(os.environ, SOLBREAKER_DYNAMIC_BACKEND=dynamic_backend):
                assets = self.assets('flat')
                world = self.world(assets)
                if backend == 'rapier' and world._rapier is None:
                    continue
                arena = Arena([dict(id=1, nickname='Pilot'), dict(id=2, nickname='Peer')],
                              b'', world.terrain, now=1., solo=False)
                self.addCleanup(arena.close)
                arena.ready(1, 1.)
                arena.ready(2, 1.)
                arena.world = world
                flyer, target = (arena.vehicle_for(i) for i in (1, 2))
                target.pose = Pose((0., 0., 7.), 0.)
                flyer.pose = Pose((0., .30, 0.), 0.)
                for vehicle in (flyer, target):
                    vehicle.health = vehicle.max_health = 2000
                    world.relocate_training_body(vehicle.entity_id, vehicle.pose)
                if backend == 'swept':
                    world.relocate_training_body(flyer.entity_id, flyer.pose, motion=Motion((0., 0., 16.), False))
                else:
                    self.assertTrue(world.set_flight_velocity(flyer.entity_id, (0., 0., 16.), (0., 0., 0.)))

                for frame in range(45):
                    arena._fixed_step(2. + (frame + 1) * arena.STEP)
                hits = [event for event in arena.events if event.kind == 'ground_impact' and event.data['ram']]
                self.assertEqual(len(hits), 1)
                damage = hits[0].data
                self.assertTrue(damage['airborne'])
                self.assertGreater(damage['ram_damage_a'], 0)
                self.assertGreater(damage['ram_damage_b'], 0)
                self.assertEqual(target.health, target.max_health - damage['ram_damage_b'])
                self.assertEqual(flyer.damage, damage['ram_damage_b'])

    def test_destroyed_moving_tank_coasts_to_a_stop(self):
        assets = self.assets('flat')
        world = self.world(assets)
        arena = Arena([dict(id=1, nickname='Pilot'), dict(id=2, nickname='Peer')],
                      b'', world.terrain, now=1., solo=False)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        arena.ready(2, 1.)
        arena.world = world
        vehicle = arena.vehicle_for(1)
        entity = vehicle.entity_id
        vehicle.pose = world.poses[entity]
        vehicle.speed = 10.
        world.motion[entity].velocity = (0., 0., 10.)
        arena._damage_health(vehicle, vehicle.health, arena.vehicle_for(2).entity_id, 2., shell_hit=True)
        self.assertFalse(vehicle.alive)
        self.assertEqual(vehicle.speed, 10.)
        arena.finish(2, 'elimination', 2.)
        for frame in range(180):
            arena._fixed_step(2. + (frame + 1) * arena.STEP)
            if frame == 30:
                self.assertGreater(vehicle.speed, 0.)
                self.assertLess(vehicle.speed, 10.)
        self.assertAlmostEqual(vehicle.speed, 0.)
        self.assertGreater(vehicle.pose.position[2], 6.)
        self.assertLess(vehicle.pose.position[2], 12.)

    def test_steep_rock_contact_pitches_the_nose_without_a_launch(self):
        assets = self.assets('rock')
        assets.vertices = box((-5.,0.,4.), (5.,1.,6.))
        leading_top = (assets.vertices[:,1] == 1.) & (assets.vertices[:,2] == 4.)
        assets.vertices[leading_top,2] += .65
        world = self.world(assets)
        peak = 0.
        speed = 10.
        for _ in range(50):
            pose, _ = world.move(speed/60, 0., 200, dt=1/60)
            state = world.motion[200]
            speed = min(10., max(0.,state.velocity[2])+3.5/60)
            peak = min(peak,pose.pitch)
            self.assertTrue(state.grounded)
        self.assertLess(peak, math.radians(-2.))
        self.assertLess(pose.position[2], 3.5)

    def test_external_pose_and_turret_changes_reach_native_queries(self):
        world = self.world(self.assets('flat'))
        pose = Pose((0.,0.,2.), .3)
        world.poses[201] = pose
        world.set_pose(world.bodies[201], pose)
        world.set_turret(.7,201)
        world.move(.01,0.,200,dt=1/60)
        self.assertTrue(world._native_physics.query(operation='contacts',entity=200,skin=world.SKIN))

    def test_drive_batch_publishes_a_previous_python_hull_move(self):
        world = self.world(self.assets('flat'))
        physics = world._native_physics
        with physics.sensor_batch(world) as dirty:
            world.relocate_training_body(201, Pose((0., 0., 2.), .3))
            world.set_turret(.7, 201)
            dirty.add(201)
            world.move(.01, 0., 200, dt=1/60)
            self.assertFalse(dirty)
            self.assertTrue(physics.query(operation='contacts', entity=200, skin=world.SKIN))

    def test_suspended_sensor_exits_native_ground_contacts_without_relocation(self):
        world = self.world(self.assets('flat'))
        physics = world._native_physics
        if not hasattr(physics.library, 'solbreaker_physics_sensor_enabled'):
            self.skipTest('Rebuild physics_core with sensor presence support')
        world.relocate_training_body(201, Pose((0., 0., 2.), .3))
        physics.sync(world)
        self.assertTrue(physics.query(operation='contacts', entity=200, skin=world.SKIN))
        world.set_tracked_presence(201, False)
        self.assertFalse(physics.query(operation='contacts', entity=200, skin=world.SKIN))
        world.set_tracked_presence(201, True)
        self.assertTrue(physics.query(operation='contacts', entity=200, skin=world.SKIN))

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

    def test_grounded_tank_can_reverse_out_of_existing_rock_overlap(self):
        for native in (False, True):
            with self.subTest(native=native):
                assets = self.assets('rock')
                assets.vertices = box((-5., 0., 4.), (5., 3., 6.))
                world = self.world(assets, native)
                entity = 200
                start = Pose((0., .05, 2.32), 0.)
                world.relocate_training_body(entity, start)

                pose, moved = world.move(.024, 0., entity, dt=1/60)
                self.assertFalse(moved)
                self.assertEqual(pose, start)
                for _ in range(20):
                    previous = pose
                    pose, moved = world.move(-.024, 0., entity, dt=1/60)
                    self.assertTrue(moved)
                    self.assertLess(pose.position[2], previous.position[2])
                    self.assertTrue(world.motion[entity].grounded)
                self.assertFalse(world._contacts(pose, entity, suspension=True))

                for _ in range(30):
                    pose, _ = world.move(.024, 0., entity, dt=1/60)
                self.assertLessEqual(pose.position[2], 2.)

    def test_airborne_body_collides_with_static_cover_and_grounded_tanks(self):
        for obstacle, launch_z, tank_z, limit in (('wall', -3., 20., 2.15),
                                                 ('flat', -8., -2., -5.9)):
            with self.subTest(obstacle=obstacle):
                world = self.world(self.assets(obstacle))
                world.relocate_training_body(201, Pose((0., .001, tank_z), 0.))
                world.relocate_training_body(200, Pose((0., .6, launch_z), 0.))
                self.assertTrue(world.set_flight_velocity(200, (0., 0., 16.), (0., 0., 0.)))
                for _ in range(32):
                    world.dynamic_commit(1/60, piloted=(200,))
                    self.assertLess(world.poses[200].position[2], limit)
                self.assertLess(world.motion[200].velocity[2], 8.)

    def test_bullet_flight_handoff_queries_the_world_only_near_contact(self):
        with patch.dict(os.environ, SOLBREAKER_DYNAMIC_BACKEND='bullet'):
            world = self.world(self.assets('wall'))
        clear = Pose((0., .1, 1.8), 0.)
        world.relocate_training_body(200, clear)
        with patch('solbreaker.collision.bullet.performCollisionDetection',
                   side_effect=AssertionError('A clear spawn must not need a world pass')):
            self.assertTrue(world.set_flight_velocity(200, (0., 1., 0.), (0., 0., 0.)))
        self.assertAlmostEqual(world.poses[200].position[2], clear.position[2])

        shallow = Pose((0., .1, 2.08), 0.)
        world.relocate_training_body(200, shallow)
        self.assertTrue(world.set_flight_velocity(200, (0., 1., 0.), (0., 0., 0.)))
        self.assertLess(world.poses[200].position[2], 2.)
        self.assertAlmostEqual(world.poses[200].position[1], shallow.position[1])

        deep = Pose((0., .1, 2.22), 0.)
        world.relocate_training_body(200, deep)
        self.assertFalse(world.set_flight_velocity(200, (0., 1., 0.), (0., 0., 0.)))
        self.assertNotIn(200, world.dynamic_bodies)
        self.assertEqual(world.poses[200], deep)

    def test_red_border_preserves_ground_motion_along_the_wall(self):
        for native in (False, True):
            with self.subTest(native=native):
                assets = self.assets('flat')
                assets.bounds = (-20., -20., 20., 20.)
                world = self.world(assets, native)
                world.relocate_training_body(200, world.ground_pose(18.4, 0., 0., entity=200))
                for _ in range(24):
                    world.move(0., 0., 200, dt=1/60, planar_displacement=(.17, .12))
                x, _, z = world.poses[200].position
                self.assertGreater(x, 18.4)
                self.assertLess(x, 19.)
                self.assertGreater(z, 2.7)
                self.assertTrue(world.motion[200].grounded)
                self.assertIsNone(world.last_wall_recoil)

    def test_rapier_syncs_grounded_poses_once_per_tick_and_preserves_motion(self):
        def trajectory(cross, *, resync):
            with patch.dict(os.environ, SOLBREAKER_DYNAMIC_BACKEND='rapier'):
                world = self.world(self.assets('wall'))
            if world._rapier is None:
                self.skipTest('Optional Rapier library unavailable')
            world.relocate_training_body(200, Pose((0., .6, -3.), 0.))
            self.assertTrue(world.set_flight_velocity(200, (0., 0., 16.), (.07, -.1, .03)))
            sync = world._rapier.sync
            native_step = world._rapier.step
            motion = []
            frames = []

            def expected_rows():
                return [
                    dict(id=entity, position=pose.position,
                         orientation=quaternion(pose.yaw, pose.pitch, pose.roll))
                    for entity, pose in world.poses.items()]

            def check_rows(rows):
                self.assertEqual(rows, expected_rows())
                return sync(rows)

            def record_step(dt, *, rows=False):
                if resync and len(frames) % steps_per_tick:
                    sync(expected_rows())
                result = native_step(dt, rows=rows)
                if isinstance(result, RapierStepRows):
                    buffer = world._rapier._output
                    states, contacts, metadata = _PACKED_HEADER.unpack_from(buffer)
                    size = _PACKED_HEADER.size + states * _PACKED_STATE.size
                    size += contacts * _PACKED_CONTACT.size + metadata
                    frame = _decode_packed_step(buffer, size)
                    self.assertEqual(world._rapier_contacts(result.contacts, dt, result.metadata),
                                     world._rapier_contacts(frame['contacts'], dt))
                    frames.append(frame)
                else:
                    frames.append(result)
                return result

            tick_dt = 1/60
            steps_per_tick = math.ceil(tick_dt/world.DYNAMIC_MAX_STEP)
            packed = None if resync else world._rapier._step_packed
            with (patch('solbreaker.collision.cross3_f64', cross),
                  patch.object(world._rapier, '_step_packed', packed),
                  patch.object(world._rapier, 'step', side_effect=record_step),
                  patch.object(world._rapier, 'sync', side_effect=check_rows) as sensor_sync):
                for step in range(32):
                    # The other tank can move between ticks while the first is airborne.
                    world.relocate_training_body(201, Pose((step * .01, .2, 2.), step * .001))
                    world.dynamic_commit(tick_dt, piloted=(200,))
                    self.assertEqual(sensor_sync.call_count, step + 1)
                    motion.append((world.poses[200], world.motion[200].velocity, world.flight_spin(200)))
            self.assertTrue(any(point['other'].get('id') == 201 for result in frames
                                for point in result['contacts'] if point['other']))
            return motion, repr(frames)

        self.assertEqual(trajectory(np.cross, resync=True), trajectory(cross3_f64, resync=False))

    def test_red_border_removes_only_outward_air_velocity(self):
        for backend in ('bullet', 'rapier'):
            with self.subTest(backend=backend), patch.dict(os.environ, SOLBREAKER_DYNAMIC_BACKEND=backend):
                assets = self.assets('flat')
                assets.bounds = (-20., -20., 20., 20.)
                world = self.world(assets)
                world.relocate_training_body(200, Pose((17., 8., 0.), 0.))
                self.assertTrue(world.set_flight_velocity(200, (35., 0., 20.), (0., 0., 0.)))
                for _ in range(18):
                    world.dynamic_commit(1/60, piloted=(200,))
                x, y, z = world.poses[200].position
                vx, vy, vz = world.motion[200].velocity
                self.assertGreater(x, 17.)
                self.assertLess(x, 19.)
                self.assertGreater(y, 7.)
                self.assertGreater(z, 4.)
                self.assertAlmostEqual(vx, 0., places=5)
                self.assertLess(vy, 0.)
                self.assertGreater(vz, 14.)

    def test_dynamic_terrain_guard_extricates_a_deeply_buried_nose(self):
        from solbreaker.collision import bullet, matrix, quaternion
        with patch.dict(os.environ, SOLBREAKER_DYNAMIC_BACKEND='bullet'):
            world = self.world(self.assets('flat'))
        world.relocate_training_body(200, Pose((0., 8., 0.), 0.))
        self.assertTrue(world.set_flight_velocity(200, (4., -7., 8.), (0., 0., 0.)))
        dynamic = world.dynamic_bodies[200]
        pitch = .8
        center = np.asarray((0., .4, 0.)) + matrix(0., pitch, 0.) @ dynamic.center_of_mass
        bullet.resetBasePositionAndOrientation(dynamic.body, center, quaternion(0., pitch, 0.),
                                               physicsClientId=world.cid)
        bullet.resetBaseVelocity(dynamic.body, (4., -7., 8.), physicsClientId=world.cid)

        world.dynamic_commit(1/60, piloted=(200,))

        corrected = world.poses[200]
        vx, vy, vz = world.motion[200].velocity
        self.assertGreater(corrected.position[1], 1.)
        self.assertAlmostEqual(corrected.pitch, pitch, places=5)
        self.assertAlmostEqual(vy, 0., places=5)
        self.assertGreater(vx, 3.)
        self.assertGreater(vz, 7.)

    def test_released_sandbox_flight_clears_buried_tracks_without_braking(self):
        world = self.world(self.assets('flat'))
        entity = 200
        ground = world.terrain.height(0., 0.)
        pose = Pose((0., ground - .263, 0.), 0.)
        velocity = (1., -4., 20.)
        rotation = matrix(0.)
        self.assertTrue(world.set_flight_velocity(entity, velocity, (0., 0., 0.),
                                                 sandbox_landing=True))

        corrected, corrected_velocity = world._constrain_dynamic_pose(
            entity, pose, velocity, rotation)

        self.assertAlmostEqual(corrected.position[1], ground - world.SKIN)
        self.assertEqual(corrected_velocity, (1., 0., 20.))
        self.assertTrue(world.set_flight_velocity(entity, velocity, (0., 0., 0.),
                                                 sandbox_landing=False))
        self.assertEqual(world._constrain_dynamic_pose(entity, pose, velocity, rotation),
                         (pose, velocity))

    def test_sandbox_landing_clears_the_low_part_of_a_curved_track_nose(self):
        assets = self.assets('flat')
        outline = ((0., -1.), (0., .8), (.02, 2.6), (.8, 3.),
                   (1.4, 3.), (1.4, -1.))
        left = np.array([(x, y, z) for x in (-1.1, -.7) for y, z in outline])
        right = np.array([(x, y, z) for x in (.7, 1.1) for y, z in outline])
        hull, _, _, turret = assets.tank_shapes
        assets.tank_shapes = (hull, ('chassis_track_L', left),
                              ('chassis_track_R', right), turret)
        assets.contact_proxies = vehicle_proxies(assets.tank_shapes, assets.turret_node, assets.support)
        world = self.world(assets)
        velocity = (1., -3., 6.)
        self.assertTrue(world.set_flight_velocity(200, velocity, (0., 0., 0.), sandbox_landing=True))
        ground = world.terrain.height(0., 0.)
        pose = Pose((0., ground + .45, 0.), 0., .30)
        rotation = matrix(pose.yaw, pose.pitch)

        corrected, corrected_velocity = world._constrain_dynamic_pose(200, pose, velocity, rotation)

        track_floor = (right @ rotation.T)[:, 1].min() + corrected.position[1]
        self.assertAlmostEqual(track_floor, ground - world.SKIN)
        self.assertEqual(corrected_velocity, (1., 0., 6.))
        self.assertEqual(corrected.pitch, pose.pitch)

    def test_dynamic_terrain_guard_ignores_empty_space_below_a_bevelled_nose(self):
        from solbreaker.collision import bullet, matrix, quaternion
        assets = self.assets('flat')
        front = ((0., -1.), (0., 1.), (1., 3.), (1.4, 3.), (1.4, -1.))
        assets.tank_shapes = (
            ('hull', box((-.7, .45, -1.), (.7, 1.5, 1.))),
            ('chassis_track_L', np.array([(x, y, z) for x in (-1.1, -.7) for y, z in front])),
            ('chassis_track_R', np.array([(x, y, z) for x in (.7, 1.1) for y, z in front])),
            assets.tank_shapes[-1])
        assets.contact_proxies = vehicle_proxies(assets.tank_shapes, assets.turret_node, assets.support)
        with patch.dict(os.environ, SOLBREAKER_DYNAMIC_BACKEND='bullet'):
            world = self.world(assets)
        self.assertTrue(world.set_flight_velocity(200, (0., -1., 0.), (0., 0., 0.)))
        dynamic = world.dynamic_bodies[200]
        pitch = .8
        center = np.asarray((0., 1.2, 0.)) + matrix(0., pitch, 0.) @ dynamic.center_of_mass
        bullet.resetBasePositionAndOrientation(dynamic.body, center, quaternion(0., pitch, 0.),
                                               physicsClientId=world.cid)
        bullet.resetBaseVelocity(dynamic.body, (0., -1., 0.), physicsClientId=world.cid)

        world.dynamic_commit(1/60, piloted=(200,))

        self.assertLess(world.poses[200].position[1], 1.2)
        self.assertLess(world.motion[200].velocity[1], 0.)

    def test_low_gravity_catches_track_penetration_during_a_nose_down_landing(self):
        world = self.world(self.assets('flat'), gravity=2.5)
        self.assertTrue(world.set_flight_velocity(200, (0., -3., 6.), (0., 0., 0.)))
        pitch = .30
        pose = Pose((0., .30, 0.), 0., pitch)
        rotation = matrix(0., pitch, 0.)

        corrected, velocity = world._constrain_dynamic_pose(200, pose, (0., -3., 6.), rotation)

        self.assertGreater(corrected.position[1], pose.position[1])
        self.assertAlmostEqual(corrected.pitch, pitch)
        self.assertEqual(velocity, (0., 0., 6.))
        tracks = np.concatenate([vertices for name, vertices in world.assets_for(200).tank_shapes
                                 if name.startswith('chassis_track_')])
        self.assertGreaterEqual((tracks @ rotation.T)[:, 1].min() + corrected.position[1],
                                -world.LOW_GRAVITY_TERRAIN_OVERLAP)

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
