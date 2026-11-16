"""Exercise branch movement and shell hits across independent Bullet worlds."""
from dataclasses import replace
import math
import os
import pickle
import signal
import struct
import time
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch

import numpy as np

from solbreaker import armor_native, big_boss, uprising
from solbreaker.arena import Arena, _MARKER_BATCH_ARENA_METHODS
from solbreaker.arena_commands import ArenaCommand, apply_at_tick, decode_graviton_aim
from solbreaker.arena_owner import Command
from solbreaker.arena_owner_game import GameArenaHandler
from solbreaker.arena_time_stop import BattleTimeStop
from solbreaker.armor import ArmorHit, ArmorModel
from solbreaker.ballistics import Projectile, TrackContact, body_states
from solbreaker.battle import (
    avatar_method, consumable_message, destructible_state, event_messages, gravity_traps_message, pb_blob,
    missile_message, motion_messages, present_vehicles,
    provision_effect_message, received_damage_message, reconnect_message_parts, time_stop_message, time_stop_roster_message,
    vehicle_property,
)
from solbreaker.collision import CollisionWorld, GroundMotion, Motion, Pose
from solbreaker.consumables import COOLDOWN, ConsumableSpec, ConsumableState
from solbreaker.drive_replica import build_spec
from solbreaker.entities import BattleEvent
from solbreaker.marker_batch import try_markers
from solbreaker.motion import vehicle_snapshot
from solbreaker.modules import GunSpec, GunState, TrackSpec, TrackState
from solbreaker.recon import ReconState
from solbreaker.sandbox import TIME_STOP, Control, token
from solbreaker.time_stop import (
    EffectLedger, Entity, Impulse, Level, MergeVisual, Rules, Snapshot, TimelineSimulation,
    merge_frame, safe_return,
)
from solbreaker.time_stop_worker import Failure, Frame, Prepared, Step, Worker, _frame
from solbreaker.vehicle_data import MissileFlight, Shell, Weapon
from tests.test_native_physics import FACES, QueryAssets, box, terrain


class TimeStopTests(TestCase):
    def setUp(self):
        flags = patch.dict(os.environ, SOLBREAKER_NATIVE_PHYSICS='0', SOLBREAKER_SIM_WORKERS='0')
        flags.start()
        self.addCleanup(flags.stop)
        self.shell = Shell(1, 100., 100., (200., 200.), 0., 70., 600., 0., 500.,
                           damage_factors=(1., 1.))
        self.arena = Arena([{'id': 1, 'nickname': 'Owner', 'team': 1},
                            {'id': 2, 'nickname': 'Enemy', 'team': 2},
                            {'id': 3, 'nickname': 'Ally', 'team': 1}],
                           b'', terrain(), solo=False, now=10., shell=self.shell)
        self.owner, self.enemy, self.ally = (self.arena.vehicle_for(i) for i in (1, 2, 3))
        self.owner.selected_shell = self.shell.compact_id
        assets = QueryAssets('flat')
        armor = ArmorModel.__new__(ArmorModel)
        armor.radius = 3.
        armor.turret_position = armor.gun_position = np.zeros(3)
        armor.parts = [('hull', np.array([[[-1., 0., -1.], [-1., 2., -1.], [1., 0., -1.]],
                                          [[1., 2., -1.], [1., 0., -1.], [-1., 2., -1.]]]),
                        np.array([1, 1]), np.array([20., 20.]), np.array([1., 1.]))]
        assets.armor = armor
        self.addCleanup(assets.mesh_files.close)
        self.addCleanup(self.arena.close)
        self.arena.world = CollisionWorld(assets, self.arena.terrain, vehicles={
            self.owner.entity_id: Pose((0., 0., 0.), 0.),
            self.enemy.entity_id: Pose((0., 0., 12.), 0.),
            self.ally.entity_id: Pose((10., 0., 5.), 0.),
        }, experimental_ground_impacts=False, dynamic_backend='bullet')
        for vehicle in (self.owner, self.enemy, self.ally):
            vehicle.pose = self.arena.world.poses[vehicle.entity_id]
        for account_id in (1, 2, 3):
            self.arena.ready(account_id, 10.)
        self.geometry = build_spec(self.arena.world, include_armor=True)

    def snapshot(self):
        snapshot = Snapshot.capture(self.arena, 1, 30.)
        return replace(snapshot, clip_size=1, clip=min(1, snapshot.clip))

    def fork(self, **options):
        branch = TimelineSimulation(self.snapshot(), self.geometry, **options)
        self.addCleanup(branch.close)
        return branch

    def test_shot_dispersion_is_shared_and_carries_bloom_into_time_stop(self):
        aiming = SimpleNamespace(dispersion_radius=.4, aiming_time=2., after_shot_dispersion=4.,
                                 movement_dispersion=.2, hull_rotation_dispersion=.1,
                                 turret_rotation_dispersion=.12, damaged_gun_dispersion=2.)
        self.owner.definition = SimpleNamespace(weapon=Weapon(12, 2, 1., 5., (self.shell,)),
                                                aiming=aiming)
        self.arena._configure_ammunition(self.owner)
        _, live_direction = self.arena.shot_ray(self.owner)
        with (patch.object(self.arena.dispersion_rng, 'random', return_value=1.),
              patch.object(self.arena.dispersion_rng, 'uniform', return_value=0.)):
            _, velocity, _ = self.arena._launch(self.owner, self.shell, self.arena.simulation_time)
        live_ratio = math.tan(math.acos(sum(a*b/self.shell.speed for a, b in zip(live_direction, velocity))))
        self.assertAlmostEqual(live_ratio, .004)
        start = Pose((0., 0., 0.), 0.)
        end = Pose((0., 0., 5.), math.radians(20.))
        self.owner.gun.yaw = math.radians(30.)
        self.owner.gun.update_dispersion(aiming, start, end, 0., 1., 1.)
        peak = math.hypot(1., 18.*.2, 20.*.1, 30.*.12)
        self.assertAlmostEqual(self.owner.gun.movement_dispersion, peak)
        self.owner.gun.update_dispersion(aiming, end, end, self.owner.gun.yaw, 1., 1.)
        settled = 1. + (peak - 1.) * math.exp(-.5)
        self.assertAlmostEqual(self.owner.gun.movement_dispersion, settled)
        gun_state = self.arena.guns[self.owner.entity_id] = GunState(GunSpec(100., 80., 0.))
        gun_state.damage(10.)
        branch = self.fork()
        self.assertEqual(branch.snapshot.gun_loadout.shot_age, 0.)
        self.assertAlmostEqual(branch.gun.movement_dispersion, settled)
        _, branch_direction = branch._shot_ray()
        with (patch.object(branch.dispersion_rng, 'random', return_value=1.),
              patch.object(branch.dispersion_rng, 'uniform', return_value=0.)):
            self.assertTrue(branch.fire(1))
        velocity = branch.projectiles[1].velocity
        ratio = math.tan(math.acos(sum(a*b/self.shell.speed for a, b in zip(branch_direction, velocity))))
        self.assertAlmostEqual(ratio, .016 * 2. * settled)
        self.assertAlmostEqual(_frame(branch).movement_dispersion, settled)

    def test_frozen_motion_and_autoaim_preserve_team_spotting(self):
        self.enemy.mad_unspotted = True
        branch = self.fork()
        hidden = self.enemy.entity_id
        self.assertIn(hidden, branch.world.bodies)
        self.assertFalse(branch.autoaim(hidden))
        timeline = SimpleNamespace(account=1, snapshot=branch.snapshot, frame=None)
        with patch.object(self.arena, 'time_stop', timeline):
            result = motion_messages(self.arena, self.arena.players[1])
        for tank in branch.snapshot.tanks:
            pose = tank.body.pose
            message = vehicle_snapshot(tank.identity.ident, pose.position, pose.yaw, pose.pitch, pose.roll)
            self.assertEqual(message in result, tank.identity.ident != hidden)

    def test_server_rules_override_action_cost_and_duration(self):
        with (patch.dict(os.environ, SOLBREAKER_TIME_STOP_RULES='{"shot_cost":11,"duration":12}'),
              patch.object(BattleTimeStop, '_warm')):
            timeline = BattleTimeStop(self.arena)
        branch = self.fork(rules=timeline.rules)
        self.assertEqual(timeline.rules.shockwave_cost, Rules().shockwave_cost)
        self.assertEqual(branch.deadline, branch.snapshot.captured_at+12.)
        self.assertTrue(branch.fire(1))
        self.assertEqual(branch.ledger.energy, 39)

    def test_server_rules_reject_malformed_and_unknown_values(self):
        for raw in ('null', '{', '{"unexpected": 1}', '{"shot_cost": true}',
                    '{"duration": NaN}', '{"healing_refund": -1}',
                    '{"starting_level": 0}', '{"starting_level": 6}', '{"starting_level": true}'):
            with (self.subTest(raw=raw), patch.dict(os.environ, SOLBREAKER_TIME_STOP_RULES=raw),
                  self.assertRaisesRegex(ValueError, 'SOLBREAKER_TIME_STOP_RULES')):
                Rules.from_environment()

    def map_geometry(self, scenario):
        assets = QueryAssets(scenario)
        assets.armor = self.arena.world.assets.armor
        self.addCleanup(assets.mesh_files.close)
        world = self.arena.world
        poses = dict(world.poses)
        world.close()
        self.arena.world = CollisionWorld(assets, self.arena.terrain, vehicles=poses, dynamic_backend='bullet')
        self.geometry = build_spec(self.arena.world, include_armor=True)

    def worker(self, snapshot=None, *, rules=Rules()):
        worker = Worker(snapshot or self.snapshot(), self.geometry,
                        level=Level(50, 1., 100.), rules=rules)
        self.addCleanup(worker.close)
        return worker

    def reply(self, worker):
        deadline = time.monotonic() + 10.
        while time.monotonic() < deadline:
            if (value := worker.poll()) is not None:
                return value
            time.sleep(.005)
        self.fail('Time Stop worker produced no reply')

    def test_separate_physics_free_movement_radius_and_wall_clock(self):
        branch = self.fork(level=Level(50, 1., .6))
        self.assertNotEqual(branch.world.cid, self.arena.world.cid)
        frozen = self.enemy.pose
        relocated = replace(frozen, position=(22., frozen.position[1], 12.))
        self.arena.world.relocate_training_body(self.enemy.entity_id, relocated)
        self.enemy.pose = relocated
        for tick in range(1, 40):
            branch.tick(30. + tick*branch.STEP, flags=1)
        self.assertGreater(branch.pose.position[2], .1)
        self.assertLessEqual(branch.pose.position[2], .6)
        self.assertEqual(branch.world.poses[self.enemy.entity_id], frozen)
        self.assertEqual(self.enemy.pose, relocated)
        self.assertEqual(self.owner.pose, branch.activation_pose)
        self.assertEqual(branch.ledger.energy, 50)
        self.assertFalse(branch.tick(45.))

    def test_tracks_accelerate_and_brake_like_the_main_arena(self):
        branch = self.fork()
        entity = self.owner.entity_id
        dt, now = branch.STEP, self.arena.simulation_time
        positions = []
        for tick in range(1, 61):
            flags = 1 if tick <= 40 else 0
            self.owner.flags, self.owner.last_input = flags, now
            self.arena._drive(self.owner, dt, now)
            branch.tick(30. + tick*dt, flags=flags)
            positions.append(branch.pose.position[2])
            self.assertAlmostEqual(branch.speed, self.owner.speed, places=6)
            self.assertAlmostEqual(branch.pose.position[2], self.owner.pose.position[2], places=6)
            self.assertEqual(branch.engine_mode, (3, 1) if flags else (1, 0))
        self.assertGreater(positions[30]-positions[29], positions[1]-positions[0])
        self.assertEqual(branch.world.motion[entity].grounded, True)
        self.assertAlmostEqual(branch.speed, 0.)

    def test_fork_while_driving_preserves_grip_and_suspension(self):
        world, owner = self.arena.world, self.owner
        entity, now, dt = owner.entity_id, self.arena.simulation_time, self.arena.STEP
        world.experimental_ground_impacts = True
        self.geometry = build_spec(world, include_armor=True)
        owner.flags, owner.last_input = 1, now
        for _ in range(100):
            self.arena._drive(owner, dt, now)
        branch = self.fork()
        self.assertGreater(branch.speed, 1.)
        self.assertEqual(branch.pose, owner.pose)
        self.assertEqual(branch.world.motion[entity], world.motion[entity])
        owner.flags = 9
        for tick in range(1, 31):
            self.arena._drive(owner, dt, now)
            branch.tick(30. + tick*dt, flags=9)
            self.assertEqual(branch.pose, owner.pose)
            self.assertAlmostEqual(branch.speed, owner.speed, places=6)

    def test_turning_at_radius_preserves_tangential_movement(self):
        branch = self.fork(level=Level(50, 1., 1.2))
        for tick in range(1, 121):
            flags = 1 if tick < 70 else 1 | 8
            branch.tick(30. + tick*branch.STEP, flags=flags)
            x, _, z = branch.pose.position
            self.assertLessEqual(math.hypot(x, z), 1.2 + 1e-9)
            if tick == 69:
                self.assertAlmostEqual(z, 1.2)
                self.assertAlmostEqual(branch.speed, 0.)
        self.assertGreater(x, .35)
        self.assertGreater(branch.pose.yaw, .7)

    def test_default_level_allows_travel_beyond_100m_and_keeps_the_map_border(self):
        world = self.arena.world
        start = Pose((-25., self.owner.pose.position[1], -85.), 0.)
        world.relocate_training_body(self.owner.entity_id, start)
        self.owner.pose = start
        branch = self.fork()
        for tick in range(1, 900):
            branch.tick(30. + tick*branch.STEP, flags=1)
        self.assertGreater(branch.pose.position[2]-start.position[2], 100.)
        self.assertLess(branch.pose.position[2], world.assets.bounds[3])
        self.assertEqual(branch.ledger.energy, 50)
        self.assertFalse(branch.tick(45., flags=1))

    def test_touching_the_radius_in_flight_does_not_ground_the_tank(self):
        branch = self.fork(level=Level(50, 1., 2.))
        world, entity = branch.world, branch.entity
        start = Pose((1.95, 8., 0.), 0.)
        world.relocate_training_body(entity, start)
        world.motion[entity] = Motion((6., -1., 3.), False)
        for tick in range(1, 7):
            branch.tick(30. + tick*branch.STEP)
            x, y, z = branch.pose.position
            self.assertLessEqual(math.hypot(x, z), 2. + 1e-9)
            self.assertLess(y, start.position[1])
            self.assertFalse(world.motion[entity].grounded)
        self.assertGreater(z, .15)
        self.assertLess(world.motion[entity].velocity[1], -1.)

    def test_accelerated_reload_while_stationary_and_discard_round_at_deadline(self):
        self.owner.reload_at = self.arena.simulation_time + 8.
        branch = self.fork(level=Level(50, 4., 100.))
        shell = Shell(1, 100., 100., (200., 200.), 0., None, 30., 0., 500.)
        self.assertEqual(branch.reload_at, 2.)
        self.assertFalse(branch.fire(1, shell, cooldown=8.))
        for tick in range(1, 121):
            branch.tick(30. + tick*branch.STEP)
        self.assertTrue(branch.fire(1, shell, cooldown=8.))
        self.assertEqual(branch.ledger.energy, 45)
        self.assertEqual(branch.ammo, self.owner.ammo-1)
        self.assertEqual(branch.reload_at, 4.)
        self.assertFalse(branch.tick(45.))
        self.assertFalse(branch.projectiles)
        self.assertFalse(branch.ledger.events)

    def missile_shell(self):
        return replace(self.shell, kind='HOLLOW_CHARGE', speed=24., effect_index=29,
                       penetration_loss_by_distance=.01,
                       missile=MissileFlight(48., 10., 2., 1.))

    def test_missile_steers_in_camera_and_stops_at_static_geometry(self):
        self.map_geometry('wall')
        arena = self.arena
        arena.shell = self.missile_shell()
        branch = self.fork()
        self.assertTrue(branch.fire(1))
        missile = branch.projectiles[1]
        self.assertTrue(branch.aim_point((-8., 1., 30.)))
        branch.tick(30.+branch.STEP)
        self.assertLess(missile.velocity[0], 0.)
        for tick in range(2, 20):
            branch.tick(30.+tick*branch.STEP, missile_camera=(1, (.22, 0., .9755)))
            if not branch.projectiles:
                break
        self.assertGreater(missile.velocity[0], 0.)
        frame = _frame(branch)
        self.assertTrue(frame.shots[0][0].guided)
        self.assertEqual(frame.energy, 45)
        self.assertFalse(frame.projectiles)
        shot, point, guided, explosion = frame.shot_ends[0]
        self.assertEqual(shot, 1)
        self.assertTrue(guided)
        self.assertAlmostEqual(point[2], 4., places=2)
        self.assertEqual(explosion[:2], (arena.shell.compact_id, 0))
        self.assertFalse(branch.ledger.events)

    def test_guided_missile_keeps_frozen_lock_and_applies_hit_once(self):
        arena, enemy = self.arena, self.enemy
        arena.shell = self.missile_shell()
        self.assertTrue(arena.autoaim(1, enemy.entity_id, (0., 1., 12.)))
        branch = self.fork()
        frozen = enemy.pose
        moved = replace(frozen, position=(15., frozen.position[1], 16.))
        arena.world.relocate_training_body(enemy.entity_id, moved)
        enemy.pose = moved
        self.assertTrue(branch.fire(1))
        for tick in range(1, 45):
            branch.tick(30.+tick*branch.STEP)
            if not branch.projectiles:
                break
        self.assertEqual(branch.world.poses[enemy.entity_id], frozen)
        self.assertEqual(enemy.health, enemy.max_health)
        branch.finish()
        frame = _frame(branch)
        self.assertIsNotNone(merge_frame(arena, branch.snapshot, frame))
        self.assertEqual(enemy.health, enemy.max_health-100)
        self.assertEqual(enemy.pose, moved)
        self.assertEqual(self.owner.autoaim_target, enemy.entity_id)
        self.assertIsNone(merge_frame(arena, branch.snapshot, frame))
        self.assertEqual(enemy.health, enemy.max_health-100)

    def test_magazine_uses_shot_interval_and_advances_queued_ammunition(self):
        secondary = replace(self.shell, compact_id=2)
        base = self.snapshot()
        gun = replace(base.gun_loadout, shells=(self.shell, secondary), reserve=((1, 7), (2, 5)),
                      interval=2., full_reload=8.)
        snapshot = replace(base, ammo=7, clip=3, clip_size=3, gun_loadout=gun)
        branch = TimelineSimulation(snapshot, self.geometry, level=Level(50, 2., 100.))
        self.addCleanup(branch.close)

        self.assertTrue(branch.fire(1))
        self.assertEqual((branch.clip, branch.ammo), (2, 6))
        self.assertEqual((branch.reload_kind, branch.reload_total, branch.reload_at), ('interval', 1., 1.))
        self.assertTrue(branch.select_shell(1, secondary.compact_id))
        self.assertFalse(branch.fire(2))
        for tick in range(1, 61):
            branch.tick(30. + tick*branch.STEP)
        self.assertEqual((branch.shell_id, branch.clip, branch.ammo), (1, 2, 6))
        self.assertTrue(branch.select_shell(17, 0))
        self.assertEqual((branch.clip, branch.reload_kind, branch.reload_at), (0, 'reload', 5.))
        for tick in range(61, 301):
            branch.tick(30. + tick*branch.STEP)
        self.assertEqual((branch.shell_id, branch.clip, branch.ammo), (2, 3, 5))
        self.assertEqual(branch.ammo_by_shell, {1: 6, 2: 5})
        self.assertEqual(branch.ledger.energy, 45)

    def test_return_keeps_the_changed_shell_count_and_remaining_interval(self):
        secondary = replace(self.shell, compact_id=2)
        base = self.snapshot()
        gun = replace(base.gun_loadout, shells=(self.shell, secondary), reserve=((1, base.ammo), (2, 5)),
                      interval=2., full_reload=8.)
        snapshot = replace(base, clip=3, clip_size=3, gun_loadout=gun)
        worker = self.worker(snapshot)
        self.assertIsInstance(self.reply(worker), Frame)
        self.assertTrue(worker.submit(Step(actions=((1, 'select_shell', (0, 2)), (2, 'fire', ())))))
        firing = self.reply(worker)
        self.assertEqual((firing.selected_shell, firing.clip, firing.ammo), (2, 2, 4))
        self.assertEqual(firing.shots[-1][0].shell_id, 2)
        self.assertTrue(worker.submit(Step(finish=True)))
        completed = self.reply(worker)
        self.assertIsNotNone(merge_frame(self.arena, snapshot, completed))
        self.assertEqual((self.owner.shell_id, self.owner.clip, self.owner.ammo), (2, 2, 4))
        self.assertEqual(self.owner.ammo_by_shell[2], 4)
        self.assertEqual((self.owner.reload_kind, self.owner.reload_total), ('interval', 2.))
        self.assertAlmostEqual(self.owner.reload_at, self.arena.simulation_time+completed.reload_remaining)
        self.assertIsNone(merge_frame(self.arena, snapshot, completed))

    def test_changing_shells_in_time_stop_preserves_partial_pump_refill(self):
        secondary = replace(self.shell, compact_id=2)
        base = self.snapshot()
        gun = replace(base.gun_loadout, shells=(self.shell, secondary), reserve=((1, 7), (2, 5)),
                      pump_times=(4., 6., 8.), pump_total=4., pump_remaining=1.)
        snapshot = replace(base, ammo=7, clip=0, clip_size=3, gun_loadout=gun)
        branch = TimelineSimulation(snapshot, self.geometry, level=Level(50, 2., 100.))
        self.addCleanup(branch.close)
        for tick in range(1, 16):
            branch.tick(30. + tick*branch.STEP)
        self.assertTrue(branch.select_shell(0, 2))
        self.assertEqual((branch.pump_at, branch.pump_total), (.5, 2.))
        for tick in range(16, 31):
            branch.tick(30. + tick*branch.STEP)
        self.assertEqual((branch.shell_id, branch.ammo, branch.clip), (2, 5, 1))
        self.assertAlmostEqual(branch.pump_at, 3.5)

    def test_burst_keeps_its_rate_and_applies_later_shell_change(self):
        secondary = replace(self.shell, compact_id=2)
        base = self.snapshot()
        gun = replace(base.gun_loadout, shells=(self.shell, secondary), reserve=((1, 7), (2, 5)),
                      interval=2., burst_count=3, burst_interval=.15)
        snapshot = replace(base, ammo=7, clip=4, clip_size=4, gun_loadout=gun)
        branch = TimelineSimulation(snapshot, self.geometry, level=Level(50, 2., 100.))
        self.addCleanup(branch.close)
        self.assertTrue(branch.fire(1))
        self.assertTrue(branch.select_shell(0, 2))
        self.assertEqual((branch.shell_id, branch.burst_remaining, branch.reload_at), (1, 2, .15))
        for tick in range(1, 18):
            branch.tick(30. + tick*branch.STEP)
        self.assertEqual((branch.shell_id, branch.shots_fired, branch.ledger.energy), (1, 2, 40))
        branch.tick(30. + 18*branch.STEP)
        self.assertEqual((branch.shell_id, branch.clip, branch.ammo), (2, 1, 5))
        self.assertEqual(branch.ammo_by_shell[1], 4)
        self.assertEqual(branch.ledger.energy, 35)
        self.assertAlmostEqual(branch.reload_at, 1.3)
        shots = _frame(branch).shots
        self.assertEqual([count for _, count in shots], [3, 0, 0])
        self.assertEqual([shot.shell_id for shot, _ in shots], [1, 1, 1])
        self.assertEqual(len({shot.shot for shot, _ in shots}), 3)

    def test_live_burst_stops_at_fork_and_resumes_with_remaining_rounds(self):
        arena = self.arena
        weapon = Weapon(12, 4, 2., 8., (self.shell,), burst_count=3, burst_interval=.15)
        self.owner.definition = SimpleNamespace(weapon=weapon)
        arena._configure_ammunition(self.owner)
        at = arena.simulation_time
        arena._fire_vehicle(self.owner, at)
        self.assertEqual(self.owner.burst_remaining, 2)
        with patch.object(BattleTimeStop, '_warm'):
            timeline = arena.time_stop = BattleTimeStop(arena)
        timeline.ready, timeline.worker = True, Mock()
        self.assertTrue(timeline.toggle(1))
        snapshot = timeline.snapshot
        branch = TimelineSimulation(snapshot, self.geometry, level=Level(50, 3., 100.))
        self.addCleanup(branch.close)
        arena.step(at+.2)
        self.assertEqual(self.owner.shots, 1)
        self.assertEqual(self.owner.burst_remaining, 2)
        self.assertAlmostEqual(branch.reload_at, .15)
        for tick in range(1, 10):
            branch.tick(snapshot.captured_at + tick*branch.STEP)
        self.assertEqual((branch.shots_fired, branch.burst_remaining), (1, 1))
        branch.finish()
        frame = _frame(branch)
        self.assertIsNotNone(merge_frame(arena, snapshot, frame))
        timeline._end(refund=0, aborted=False)
        self.assertEqual((self.owner.shots, self.owner.burst_remaining), (2, 1))
        self.assertAlmostEqual(self.owner.reload_at-arena.simulation_time, .15)
        arena.step(arena.simulation_time+.16)
        self.assertEqual((self.owner.shots, self.owner.burst_remaining, self.owner.clip), (3, 0, 1))

    def test_ordered_merge_uses_current_health_and_life_once(self):
        branch = self.fork()
        self.assertTrue(branch.fire(1, self.shell, cooldown=6.))
        self.assertFalse(branch.fire(1, self.shell, cooldown=6.))
        self.enemy.health = 190
        self.enemy.pose = replace(self.enemy.pose, position=(4., 0., -14.))
        self.arena.world.relocate_training_body(self.enemy.entity_id, self.enemy.pose)
        for tick in range(1, 6):
            branch.tick(30. + tick*branch.STEP)
        self.assertEqual(self.enemy.health, 190)
        branch.finish()
        ledger = branch.ledger
        self.assertEqual(len(ledger.events), 1)
        duplicate = pickle.loads(pickle.dumps(ledger))
        self.assertEqual(ledger.merge(self.arena).applied, (1,))
        self.assertEqual(self.enemy.health, 90)
        self.assertFalse(duplicate.merge(self.arena).applied)
        self.assertEqual(self.enemy.health, 90)

    def test_heal_conflicts_refund_once_per_action_without_resurrection(self):
        ledger = EffectLedger('test', Entity.of(self.owner), 50)
        self.assertTrue(ledger.charge(1, 20))
        self.assertFalse(ledger.charge(2, 31))
        ledger.record(1, .1, Entity.of(self.ally), 'heal', 100)
        ledger.record(1, .2, Entity.of(self.ally), 'heal', 100)
        self.ally.generation += 1
        self.ally.health = 0
        result = ledger.merge(self.arena)
        self.assertEqual(result.skipped, (1, 2))
        self.assertEqual((result.refund, result.energy), (10, 40))
        self.assertEqual(ledger.merge(self.arena).refund, 0)
        self.assertFalse(self.ally.alive)

    def test_frozen_armor_equipment_reduces_shell_damage_using_the_fork_state(self):
        now = self.arena.simulation_time
        shield = ConsumableState(ConsumableSpec('shieldKit', 33, 8699, 5., 30., damage_taken_factor=.7))
        armor = ConsumableState(ConsumableSpec('armorMover', 73, 18939, 5., 30., all_shell_damage_factor=.8))
        self.enemy.consumables = (shield, armor)
        for slot in self.enemy.consumables:
            self.assertTrue(slot.activate(now))
        branch = self.fork()
        for slot in self.enemy.consumables:
            self.assertTrue(slot.stop(now + 1.))
        self.assertEqual(self.arena._shield_factor(self.enemy, now + 2.), 1.)
        self.assertEqual(self.arena._armor_mover_factor(self.enemy, now + 2.), 1.)
        target = branch.tanks[self.enemy.entity_id]
        for action in (1, 2, 3):
            self.assertTrue(branch.ledger.charge(action, 5))
        branch._shell_damage(1, 2., target, 100, replace(self.shell, kind='ARMOR_PIERCING'))
        branch._shell_damage(2, 3., target, 100, replace(self.shell, kind='HIGH_EXPLOSIVE'))
        branch._damage(3, 4., target, 100)
        self.assertEqual([effect.value for effect in branch.ledger.events], [56, 80, 100])
        health = self.enemy.health
        branch.finish()
        self.assertEqual(branch.ledger.merge(self.arena).applied, (1, 2, 3))
        self.assertEqual(self.enemy.health, health - 236)

    def test_time_stop_consumes_the_frozen_regenerator_shield_once(self):
        arena = self.arena
        self.ally.team = self.enemy.team
        arena.big_boss = boss = big_boss.BigBoss(arena.vehicles, 'shields')
        boss.bosses.discard(self.owner.entity_id)
        bomber = big_boss.ROLE_ACTIVE[big_boss.BOMBER][0]
        self.owner.consumables = (ConsumableState(bomber),)
        now = arena.simulation_time
        for target in (self.enemy, self.ally):
            boss.grant_shield(target.entity_id, now, 10.)
        branch = self.fork()
        self.assertTrue(branch.fire(1, replace(self.shell, poison_level=0), cooldown=1.))
        for tick in range(1, 7):
            branch.tick(30. + tick*branch.STEP)
        self.assertFalse(branch.poison)
        self.assertEqual([effect.kind for effect in branch.ledger.events], ['shield'])
        self.assertTrue(branch.activate(2, bomber.ident))
        branch.finish()
        effects = branch.ledger.events
        self.assertEqual([(e.kind, e.target) for e in effects], [
            ('shield', Entity.of(self.enemy)), ('damage', Entity.of(self.enemy)),
            ('shield', Entity.of(self.ally))])

        # Identical start/expiry can occur when two shields are granted on one tick.
        boss.grant_shield(self.ally.entity_id, now, 10.)
        replacement = boss.shields[self.ally.entity_id]
        health = self.enemy.health
        self.assertEqual(branch.ledger.merge(arena).applied, (1, 2))
        self.assertNotIn(self.enemy.entity_id, boss.shields)
        self.assertEqual(self.enemy.health, health - effects[1].value)
        self.assertEqual(boss.shields[self.ally.entity_id], replacement)
        self.assertEqual(self.ally.health, self.ally.max_health)
        self.assertFalse(branch.ledger.merge(arena).applied)

    def test_shockwave_and_bomb_use_frozen_targets_and_merge_damage_even_if_push_is_unsafe(self):
        shockwave = big_boss.BLAST_WAVE
        bomber = big_boss.ROLE_ACTIVE[big_boss.BOMBER][0]
        self.owner.consumables = ConsumableState(shockwave), ConsumableState(bomber)
        self.ally.team = self.enemy.team
        branch = self.fork()
        self.assertTrue(branch.activate(1, shockwave.ident))
        self.assertTrue(branch.activate(2, bomber.ident))
        self.assertFalse(branch.activate(3, bomber.ident))
        self.assertEqual(branch.ledger.energy, 15)
        self.assertEqual(branch.world.poses[self.enemy.entity_id], self.enemy.pose)
        self.assertEqual(self.enemy.health, self.enemy.max_health)
        self.assertEqual([effect.kind for effect in branch.ledger.events],
                         ['impulse', 'damage', 'impulse', 'damage', 'damage', 'damage'])
        world = self.arena.world
        world.relocate_training_body(self.ally.entity_id, self.enemy.pose)
        self.ally.pose = self.enemy.pose
        branch.finish()
        duplicate = pickle.loads(pickle.dumps(branch.ledger))
        outcome = branch.ledger.merge(self.arena)
        self.assertEqual(outcome.skipped, (1, 3))
        self.assertEqual(outcome.applied, (2, 4, 5, 6))
        self.assertEqual(self.enemy.health, self.enemy.max_health-84)
        self.assertEqual(self.owner.hits, 0)
        self.assertFalse(duplicate.merge(self.arena).applied)

    def test_god_mode_blocks_shells_but_not_area_abilities_in_time_stop(self):
        shockwave = big_boss.BLAST_WAVE
        bomber = big_boss.ROLE_ACTIVE[big_boss.BOMBER][0]
        self.owner.consumables = ConsumableState(shockwave), ConsumableState(bomber)
        self.enemy.sandbox_god_mode = True
        branch = self.fork()
        self.assertTrue(branch.fire(1, self.shell))
        for tick in range(1, 6):
            branch.tick(30. + tick * branch.STEP)
        shot, = branch.shot_ends
        self.assertAlmostEqual(shot[1][2], self.enemy.pose.position[2] - 1.)
        self.assertFalse(branch.ledger.events)

        self.assertTrue(branch.activate(2, shockwave.ident))
        self.assertTrue(branch.activate(3, bomber.ident))
        branch.finish()
        effects = branch.ledger.events
        self.assertEqual([(effect.action, effect.kind, effect.target) for effect in effects],
                         [(2, 'impulse', Entity.of(self.enemy)),
                          (2, 'damage', Entity.of(self.enemy)),
                          (3, 'damage', Entity.of(self.enemy))])
        branch.ledger.merge(self.arena)
        self.assertEqual(self.enemy.health, self.enemy.max_health - 84)
        self.assertEqual(self.owner.hits, 0)

    def test_deferred_impulse_caps_velocity_at_live_pose(self):
        world = self.arena.world
        world.experimental_ground_impacts = True
        current = replace(self.enemy.pose, position=(15., 0., 18.))
        world.relocate_training_body(self.enemy.entity_id, current)
        self.enemy.pose = current
        ledger = EffectLedger('impulse', Entity.of(self.owner), 50)
        self.assertTrue(ledger.charge(1, 20))
        ledger.record(1, .1, Entity.of(self.enemy), 'impulse', Impulse((100., 0., 0.), max_speed=12.))
        self.assertEqual(ledger.merge(self.arena).applied, (1,))
        self.assertEqual(world.ground_motion[self.enemy.entity_id].velocity, (12., 0.))
        self.assertEqual(self.enemy.pose, current)

    def test_ability_hud_uses_accelerated_cooldown_and_refreshes_after_reuse(self):
        arena = self.arena
        wave = big_boss.BLAST_WAVE
        slot = ConsumableState(wave)
        slot.start_cooldown(arena.simulation_time, 12.)
        self.owner.consumables = (slot,)
        branch = self.fork(level=Level(50, 4.))
        with patch.object(BattleTimeStop, '_warm'):
            timeline = arena.time_stop = BattleTimeStop(arena)
        timeline.snapshot, timeline.account = branch.snapshot, 1
        initial = _frame(branch)
        timeline._present_abilities(initial, None)
        events = arena.drain_events()
        expected = consumable_message(wave.compact_id, COOLDOWN, 3, 3)
        self.assertIn(expected, event_messages(arena, arena.players[1], events))
        self.assertNotIn(expected, event_messages(arena, arena.players[2], events))
        branch.steps = round(3. / branch.STEP)
        self.assertTrue(branch.activate(1, wave.ident))
        used = _frame(branch)
        timeline._present_abilities(used, initial)
        self.assertIn(consumable_message(wave.compact_id, COOLDOWN, 10, 10),
                      event_messages(arena, arena.players[1], arena.drain_events()))
        branch.steps += round(10. / branch.STEP)
        self.assertTrue(branch.activate(2, wave.ident))
        timeline._present_abilities(_frame(branch), used)
        self.assertIn(consumable_message(wave.compact_id, COOLDOWN, 10, 10),
                      event_messages(arena, arena.players[1], arena.drain_events()))

    def test_heal_wave_ticks_in_private_clock_and_refunds_if_ally_respawns(self):
        aura = big_boss.ROLE_ACTIVE[big_boss.REGENERATOR][0]
        self.owner.consumables = (ConsumableState(aura),)
        self.owner.health = self.ally.health = 100
        branch = self.fork(level=Level(50, 4., 100.))
        self.assertTrue(branch.activate(1, aura.ident))
        self.assertEqual(branch.consumables[aura.ident].ready_at, aura.cooldown/4.)
        for tick in range(1, 370):
            branch.tick(30. + tick*branch.STEP)
        self.assertEqual(len(branch.ledger.events), 4)
        self.assertEqual(self.owner.health, 100)
        self.ally.generation += 1
        branch.finish()
        outcome = branch.ledger.merge(self.arena)
        self.assertEqual(outcome.refund, 10)
        self.assertEqual(self.owner.health, 112)
        self.assertEqual(self.ally.health, 100)
        self.assertEqual(len(outcome.skipped), 2)

    def test_regenerator_healing_shot_merges_cleanse_and_shield_at_full_hp(self):
        arena = self.arena
        arena.big_boss = boss = big_boss.BigBoss(arena.vehicles, 'regenerator')
        boss.bosses.discard(self.owner.entity_id)
        boss.roles[self.owner.entity_id] = big_boss.REGENERATOR
        boss.levels[self.owner.entity_id] = 0
        spec = big_boss.SHIELD_SPECS[0]
        self.owner.consumables = (ConsumableState(spec),)
        self.ally.pose, self.enemy.pose = self.enemy.pose, self.ally.pose
        for vehicle in (self.ally, self.enemy):
            arena.world.relocate_training_body(vehicle.entity_id, vehicle.pose)
        branch = self.fork(level=Level(50, 2.))
        self.assertTrue(branch.fire(1, self.shell, cooldown=5.))
        for tick in range(1, 7):
            branch.tick(30. + tick*branch.STEP)
        effect, = branch.ledger.events
        self.assertEqual((effect.kind, effect.target, effect.value.amount), ('heal', Entity.of(self.ally), 0))
        self.assertTrue(effect.value.grant_shield)
        self.assertAlmostEqual(branch.consumables[spec.ident].ready_at, effect.at + spec.cooldown/2.)
        self.assertFalse(boss.shields)

        now = arena.simulation_time
        arena._boss_poison(self.enemy, self.ally, 0, now)
        branch.finish()
        frame = _frame(branch)
        self.assertEqual(merge_frame(arena, branch.snapshot, frame).applied, (1,))
        self.assertNotIn(self.ally.entity_id, boss.poison)
        self.assertEqual(self.ally.health, self.ally.max_health)
        shield = boss.active_shield(self.ally.entity_id, now)
        self.assertEqual(shield.expires, now + big_boss.SHIELD_DURATION[0])
        self.assertAlmostEqual(self.owner.consumables[0].ready_at,
                               now + branch.consumables[spec.ident].ready_at - frame.seconds)
        self.assertIsNone(merge_frame(arena, branch.snapshot, frame))
        self.assertEqual(boss.active_shield(self.ally.entity_id, now), shield)

    def test_regenerator_aura_cleanses_a_poisoned_full_health_ally(self):
        arena = self.arena
        arena.big_boss = boss = big_boss.BigBoss(arena.vehicles, 'regenerator aura')
        aura = big_boss.ROLE_ACTIVE[big_boss.REGENERATOR][0]
        self.owner.consumables = (ConsumableState(aura),)
        arena._boss_poison(self.enemy, self.ally, 0, arena.simulation_time)
        branch = self.fork()
        self.assertTrue(branch.activate(1, aura.ident))
        for tick in range(1, round(big_boss.HEAL_INTERVAL / branch.STEP) + 2):
            branch.tick(30. + tick*branch.STEP)
        effect, = branch.ledger.events
        self.assertEqual((effect.kind, effect.target, effect.value.amount), ('heal', Entity.of(self.ally), 0))
        self.assertIn(self.ally.entity_id, boss.poison)
        branch.finish()
        self.assertEqual(branch.ledger.merge(arena).applied, (1,))
        self.assertNotIn(self.ally.entity_id, boss.poison)

    def test_graviton_uses_native_camera_target_with_the_gun_aimed_elsewhere(self):
        arena = self.arena
        self.map_geometry('wall')
        arena.big_boss = big_boss.BigBoss(arena.vehicles, 'graviton reticle')
        arena.big_boss.bosses.discard(self.owner.entity_id)
        arena.big_boss.roles[self.owner.entity_id] = big_boss.GRAVITON
        arena.big_boss.levels[self.owner.entity_id] = 0
        spec = big_boss.ROLE_ACTIVE[big_boss.GRAVITON][0]
        self.owner.consumables = (ConsumableState(spec),)
        self.owner.gun.yaw = math.pi / 2
        self.assertTrue(arena.aim_point(1, (-8., 1., 12.)))
        camera, target = (0., 1., 0.), (2., 1., 4.)

        def vector(coords):
            return b''.join(struct.pack('<Bf', tag, value)
                            for tag, value in zip((13, 21, 29), coords) if value)

        payload = pb_blob(3, pb_blob(1, vector(camera)) + pb_blob(2, vector(target)))
        self.assertEqual(decode_graviton_aim(payload), (camera, target))
        with self.assertRaises(ValueError):
            decode_graviton_aim(payload[:-1])
        now = arena.simulation_time
        self.assertFalse(arena.use_consumable(1, spec.ident, payload[:-1], now))
        branch = self.fork()
        self.assertTrue(arena.use_consumable(1, spec.ident, payload, now))
        trap, = arena.big_boss.traps
        np.testing.assert_allclose(trap.center, target, atol=1e-5)

        with patch.object(BattleTimeStop, '_warm'):
            timeline = arena.time_stop = BattleTimeStop(arena)
        timeline.snapshot, timeline.account = branch.snapshot, 1
        self.assertTrue(timeline.accept(ArenaCommand(
            1, 'consumable', (self.owner.entity_id, spec.ident, payload), now)))
        action, _, args = timeline.actions[0]
        self.assertTrue(branch.activate(action, *args))
        _, private_trap = branch.traps[action]
        np.testing.assert_allclose(private_trap.center, target, atol=1e-5)

    def test_graviton_and_mark_can_follow_a_fresh_aim_point(self):
        self.owner.gun.yaw = math.pi / 2
        arena = self.arena
        arena.uprising = uprising.BattleState(arena.rng)
        mark = ConsumableState(uprising.HUNTERS_MARK)
        self.owner.consumables = (mark,)
        self.assertIsNone(arena.aim_hit(self.owner)[0])
        self.assertTrue(arena.aim_point(1, (0., 1., 12.)))
        self.assertTrue(arena.use_consumable(1, mark.spec.ident, now=arena.simulation_time))
        self.assertIn(self.enemy.entity_id, arena.uprising.marks)

        self.map_geometry('wall')
        spec = big_boss.ROLE_ACTIVE[big_boss.GRAVITON][0]
        self.owner.consumables = (ConsumableState(spec),)
        branch = self.fork()
        self.assertTrue(branch.aim_point((2., 1., 4.)))
        self.assertTrue(branch.activate(1, spec.ident))
        _, trap = branch.traps[1]
        np.testing.assert_allclose(trap.center, (2., 1., 4.), atol=1e-5)

    def test_graviton_does_not_charge_for_the_sky_and_keeps_frozen_hulls_still(self):
        self.map_geometry('wall')
        spec = big_boss.ROLE_ACTIVE[big_boss.GRAVITON][0]
        self.owner.consumables = (ConsumableState(spec),)
        branch = self.fork()
        branch.gun.pitch = -.7
        self.assertFalse(branch.activate(1, spec.ident))
        self.assertEqual(branch.ledger.energy, 50)
        branch.gun.pitch = .05
        self.assertTrue(branch.activate(2, spec.ident))
        self.assertEqual(branch.ledger.energy, 30)
        for tick in range(1, 62):
            branch.tick(30. + tick*branch.STEP)
        self.assertEqual(branch.world.poses[self.enemy.entity_id], self.enemy.pose)
        self.assertFalse(branch.ledger.events)
        branch.finish()
        effect, = branch.ledger.events
        self.assertEqual((effect.kind, effect.target, effect.action), ('impulse', Entity.of(self.enemy), 2))
        self.assertLess(effect.value.velocity[2], 0.)

    def test_graviton_merge_replaces_the_private_effect_with_a_public_visual_only(self):
        arena = self.arena
        self.map_geometry('wall')
        arena.big_boss = boss = big_boss.BigBoss(arena.vehicles, 'graviton')
        spec = big_boss.ROLE_ACTIVE[big_boss.GRAVITON][0]
        self.owner.consumables = (ConsumableState(spec),)
        branch = self.fork()
        with patch.object(BattleTimeStop, '_warm'):
            timeline = arena.time_stop = BattleTimeStop(arena)
        timeline.snapshot, timeline.account = branch.snapshot, 1
        branch.gun.pitch = .05
        self.assertTrue(branch.activate(1, spec.ident))
        branch.tick(30. + branch.STEP)
        active = _frame(branch)
        timeline._present_abilities(active, None)
        events = arena.drain_events()
        private = gravity_traps_message(active.traps)
        self.assertIn(private, event_messages(arena, arena.players[1], events))
        self.assertNotIn(private, event_messages(arena, arena.players[2], events))

        branch.finish()
        timeline.frame = _frame(branch)
        now = arena.simulation_time
        timeline._merge_final_frame(now, Mock())
        events = arena.drain_events()
        self.assertIn(gravity_traps_message(()), event_messages(arena, arena.players[1], events))
        self.assertFalse(boss.traps)
        self.assertFalse(timeline.merge_traps)

        live = big_boss.GravityTrap(boss.next_trap, self.enemy.entity_id, self.enemy.team,
                                    (5., 0., 15.), 0, now+7.)
        boss.next_trap += 1
        boss.traps.append(live)
        velocity = arena.world.motion[self.enemy.entity_id].velocity
        timeline._show_merge_visuals(now+.05)
        visual, = timeline.merge_traps
        self.assertEqual(visual.center, active.traps[0].center)
        self.assertNotIn(visual.ident, (live.ident, active.traps[0].ident))
        self.assertEqual(boss.traps, [live])
        self.assertEqual(arena.world.motion[self.enemy.entity_id].velocity, velocity)
        events = arena.drain_events()
        self.assertIn(gravity_traps_message((live, visual)), event_messages(arena, arena.players[2], events))
        timeline._show_merge_visuals(visual.expires_at)
        self.assertFalse(timeline.merge_traps)
        self.assertIn(gravity_traps_message((live,)), event_messages(arena, arena.players[2], arena.drain_events()))

    def test_projectile_records_prop_and_keeps_tracing_in_branch_only(self):
        self.map_geometry('prop')
        branch = self.fork()
        self.assertTrue(branch.fire(1, self.shell, cooldown=6.))
        for tick in range(1, 5):
            branch.tick(30. + tick*branch.STEP)
        self.assertIn(7, branch.world.destroyed)
        self.assertNotIn(7, self.arena.world.destroyed)
        self.assertEqual([effect.kind for effect in branch.ledger.events], ['prop', 'damage'])
        branch.finish()
        self.assertEqual(branch.ledger.merge(self.arena).applied, (1, 2))
        self.assertIn(7, self.arena.world.destroyed)

    def test_driving_breaks_a_prop_in_the_live_world_only_on_merge(self):
        self.map_geometry('prop')
        branch = self.fork()
        for tick in range(1, 121):
            branch.tick(30. + tick * branch.STEP, flags=1)
        self.assertIn(7, branch.world.destroyed)
        self.assertNotIn(7, self.arena.world.destroyed)
        self.assertEqual(branch.ledger.energy, branch.level.energy)
        effect, = branch.ledger.events
        self.assertEqual((effect.kind, effect.target), ('prop', 7))
        branch.finish()
        self.assertEqual(branch.ledger.merge(self.arena).applied, (1,))
        self.assertIn(7, self.arena.world.destroyed)
        self.assertFalse(branch.ledger.merge(self.arena).applied)
        self.arena.drain_events()
        self.arena.step(self.arena.started + self.arena.STEP)
        events = self.arena.drain_events()
        destroyed, = (event for event in events if event.kind == 'destruction')
        self.assertEqual(destroyed.data['key'], 7)
        self.assertIn(destructible_state(self.arena).updates(),
                      event_messages(self.arena, self.arena.players[2], events))

    def test_deferred_module_contact_changes_only_current_life_on_merge(self):
        part = 'chassis_track_L'
        track = self.enemy.tracks[part] = TrackState(TrackSpec(40., 20., 10., 4.))
        shell = replace(self.shell, damage_devices=60.)
        hit = ArmorHit(5., self.enemy.pose.position, (0., 0., -1.), part, 1, 20., 0., external=True)
        contact = TrackContact(.1, 1, self.owner.entity_id, self.enemy.entity_id, hit, shell, True)
        ledger = EffectLedger('module', Entity.of(self.owner), 50)
        self.assertTrue(ledger.charge(1, 5))
        ledger.record(1, .1, Entity.of(self.enemy), 'module', contact)
        self.assertEqual(track.health, 40.)
        self.assertEqual(ledger.merge(self.arena).applied, (1,))
        self.assertTrue(track.broken)
        self.assertEqual(self.enemy.health, self.enemy.max_health)
        self.assertFalse(ledger.merge(self.arena).applied)

    def test_poison_keeps_pending_ticks_across_merge_without_a_second_charge(self):
        spec = big_boss.ROLE_ACTIVE[big_boss.CATALYST][0]
        self.owner.consumables = (ConsumableState(spec),)
        self.arena.big_boss = boss = big_boss.BigBoss(self.arena.vehicles, 'time stop')
        boss.bosses.discard(self.owner.entity_id)
        boss.roles[self.owner.entity_id] = big_boss.CATALYST
        boss.levels[self.owner.entity_id] = 0
        branch = self.fork()
        self.assertTrue(branch.activate(1, spec.ident))
        self.assertTrue(branch.fire(2, self.shell, cooldown=6.))
        for tick in range(1, 145):
            branch.tick(30. + tick*branch.STEP)
        self.assertEqual(branch.ledger.energy, 30)
        self.assertFalse(boss.poison)
        branch.finish()
        self.assertEqual([effect.kind for effect in branch.ledger.events], ['damage', 'damage', 'poison'])
        result = branch.ledger.merge(self.arena)
        poison = boss.poison[self.enemy.entity_id][self.owner.entity_id]
        self.assertEqual(result.applied, (1, 2, 3))
        self.assertEqual(poison.remaining, 2)
        self.assertEqual(self.owner.hits, 1)
        self.assertGreater(poison.next_tick, self.arena.simulation_time)
        self.assertFalse(branch.ledger.merge(self.arena).applied)

    def test_safe_return_avoids_a_hull_occupying_the_preferred_pose(self):
        pose = safe_return(self.arena.world, self.owner.entity_id, self.enemy.pose, self.owner.pose)
        self.assertIsNotNone(pose)
        self.assertNotEqual(pose.position[::2], self.enemy.pose.position[::2])

    def test_blocked_merge_uses_a_free_team_spawn(self):
        arena = self.arena
        arena.map_spec = SimpleNamespace(
            spawn=lambda team, slot: self.enemy.pose.position[::2] if slot == 0 else (8., -10.),
            yaw=lambda team, slot: .4)
        branch = self.fork()
        branch.finish()
        arena.world.set_tracked_presence(branch.entity, False)
        with patch('solbreaker.time_stop.safe_return', return_value=None):
            result = merge_frame(arena, branch.snapshot, _frame(branch))
        self.assertIsNotNone(result)
        self.assertEqual(self.owner.pose.position[::2], (8., -10.))
        self.assertNotIn(branch.entity, arena.world.suspended_entities)

    def test_blocked_merge_retries_without_rechecking_on_each_tick(self):
        arena = self.arena
        with patch.object(BattleTimeStop, '_warm'):
            timeline = arena.time_stop = BattleTimeStop(arena)
        branch = self.fork()
        branch.finish()
        timeline.snapshot, timeline.account = branch.snapshot, 1
        timeline.frame = _frame(branch)
        timeline.worker = Mock(poll=Mock(return_value=None))
        arena.world.set_tracked_presence(branch.entity, False)
        now = arena.simulation_time
        with (patch('solbreaker.time_stop.safe_return', side_effect=(None, branch.pose)) as search,
              patch('solbreaker.time_stop.return_spawn', return_value=None)):
            timeline.tick(now)
            timeline.tick(now + arena.STEP)
            self.assertEqual(search.call_count, 1)
            self.assertIn(branch.entity, arena.world.suspended_entities)
            self.assertNotIn(branch.snapshot.ident, arena._resolved_time_branches)
            timeline.tick(now + timeline.RETURN_RETRY)
            self.assertEqual(search.call_count, 2)
        self.assertIn(branch.snapshot.ident, arena._resolved_time_branches)
        self.assertNotIn(branch.entity, arena.world.suspended_entities)

    def test_merge_launch_starts_moving_on_the_following_step(self):
        arena = self.arena
        with patch.object(BattleTimeStop, '_warm'):
            timeline = arena.time_stop = BattleTimeStop(arena)
        branch = self.fork()
        self.assertTrue(branch.ledger.charge(1, 5))
        branch.ledger.record(1, .1, Entity.of(self.enemy), 'impulse',
                             Impulse((0., 4., 0.), (0., .2, 0.)))
        branch.gun.movement_dispersion = 1.5
        branch.finish()
        timeline.snapshot, timeline.account = branch.snapshot, 1
        timeline.frame = _frame(branch)
        timeline.worker = Mock(poll=Mock(return_value=None))
        arena.world.set_tracked_presence(branch.entity, False)
        entity = self.enemy.entity_id
        height = arena.world.poses[entity].position[1]
        start = arena.started

        arena.step(start + arena.STEP)
        self.assertIsNone(timeline.entity)
        self.assertEqual(self.owner.gun.movement_dispersion, 1.5)
        self.assertIn(entity, arena.world.dynamic_bodies)
        self.assertAlmostEqual(arena.world.poses[entity].position[1], height)
        self.assertAlmostEqual(arena.world.motion[entity].velocity[1], 4.)

        timeline.rearm = False
        arena.step(start + 2 * arena.STEP)
        self.assertGreater(arena.world.poses[entity].position[1], height)

    def test_native_aim_batch_keeps_world_ray_and_ignores_unseen_hulls(self):
        if armor_native.load_library() is None:
            self.skipTest('Native armor library is unavailable')
        arena, enemy = self.arena, self.enemy
        world = arena.world
        enemy.pose = replace(enemy.pose, yaw=.3)
        world.relocate_training_body(enemy.entity_id, enemy.pose)
        observers = self.owner, self.ally
        direction = arena.shot_ray(self.owner)[1]
        for present, spotted in ((True, True), (False, True), (True, False)):
            world.set_tracked_presence(enemy.entity_id, present)
            enemy.mad_unspotted = not spotted
            markers = try_markers(arena, observers, _MARKER_BATCH_ARENA_METHODS)
            self.assertIsNotNone(markers)
            entity, point = arena.aim_hit(self.owner)
            self.assertEqual(entity, enemy.entity_id if present and spotted else None)
            self.assertEqual(markers[self.owner.entity_id], (point, direction))

    def test_safe_return_searches_the_same_level_as_the_occupied_pose(self):
        assets = QueryAssets('flat')
        assets.vertices = box((-10., 2.75, 5.), (10., 3., 20.))
        assets.triangles = FACES
        self.addCleanup(assets.mesh_files.close)
        world = self.arena.world
        poses = dict(world.poses)
        world.close()
        world = self.arena.world = CollisionWorld(assets, self.arena.terrain, vehicles=poses,
                                                  dynamic_backend='bullet')
        entity, other = self.owner.entity_id, self.enemy.entity_id
        target = world.training_placement(other, 0., 12., 0., stationary=True, reference=3.)
        self.assertIsNotNone(target)
        self.assertGreater(target.position[1], 2.9)
        world.relocate_training_body(other, target)
        returned = safe_return(world, entity, target, self.owner.pose)
        self.assertIsNotNone(returned)
        self.assertNotEqual(returned.position[::2], target.position[::2])
        self.assertAlmostEqual(returned.position[1], target.position[1], places=3)

    def test_airborne_merge_keeps_height_attitude_and_fall_velocity(self):
        branch = self.fork()
        pose = Pose((2., 8., 3.), .27, .16, -.13)
        motion = Motion((1., -2., 4.), False)
        branch.world.relocate_training_body(branch.entity, pose, motion=motion)
        branch.finish()
        frame = _frame(branch)
        self.assertIsNotNone(merge_frame(self.arena, branch.snapshot, frame))
        world = self.arena.world
        self.assertEqual(self.owner.pose, pose)
        self.assertEqual(world.motion[branch.entity], motion)
        after, moved = world.move(0., 0., branch.entity, dt=self.arena.STEP)
        self.assertTrue(moved)
        self.assertGreater(after.position[2], pose.position[2])
        self.assertLess(after.position[1], pose.position[1])
        self.assertFalse(world.motion[branch.entity].grounded)

    def test_grounded_merge_restores_slip_and_the_latest_control_input(self):
        arena = self.arena
        with patch.object(BattleTimeStop, '_warm'):
            timeline = arena.time_stop = BattleTimeStop(arena)
        branch = self.fork()
        branch.ground_motion = GroundMotion((3., 2.), .4, True)
        branch.world.motion[branch.entity].velocity = (3., 0., 2.)
        arena.world.experimental_ground_impacts = True
        arena.world.set_tracked_presence(branch.entity, False)
        timeline.snapshot, timeline.account = branch.snapshot, 1
        branch.finish()
        timeline.frame = pickle.loads(pickle.dumps(_frame(branch)))
        timeline.worker = Mock(poll=Mock(return_value=None))
        now = arena.simulation_time
        self.assertTrue(timeline.toggle(1))
        apply_at_tick(arena, ArenaCommand(1, 'fire', (), now))
        self.assertFalse(timeline.actions)
        apply_at_tick(arena, ArenaCommand(1, 'control', (9,), now))
        timeline.tick(now + arena.STEP)
        self.assertIsNone(timeline.entity)
        self.assertEqual(arena.world.ground_motion[branch.entity], branch.ground_motion)
        self.assertEqual(self.owner.flags, 9)
        self.assertEqual(self.owner.last_input, now)

    def test_merge_does_not_restore_expired_or_released_controls(self):
        arena = self.arena
        now = arena.simulation_time
        for flags, age in ((0, 0.), (1, arena.CONTROL_INPUT_TIMEOUT)):
            with (self.subTest(flags=flags, age=age), patch.object(BattleTimeStop, '_warm')):
                timeline = arena.time_stop = BattleTimeStop(arena)
                branch = self.fork()
                branch.finish()
                timeline.snapshot, timeline.account = branch.snapshot, 1
                timeline.frame = _frame(branch)
                timeline.worker = Mock(poll=Mock(return_value=None))
                apply_at_tick(arena, ArenaCommand(1, 'control', (flags,), now))
                timeline.tick(now + age)
                self.assertIsNone(timeline.entity)
                self.assertEqual(self.owner.flags, 0)

    def test_airborne_merge_resumes_rigid_rotation_in_the_live_world(self):
        branch = self.fork()
        entity = branch.entity
        world = self.arena.world
        world.set_tracked_presence(entity, False)
        pose = Pose((2., 8., 3.), .27, .16, -.13)
        velocity, spin = (1., -2., 4.), (.4, -.3, .2)
        branch.world.relocate_training_body(entity, pose, motion=Motion(velocity, False))
        self.assertTrue(branch.world.set_flight_velocity(entity, velocity, spin))
        branch.finish()
        frame = pickle.loads(pickle.dumps(_frame(branch)))
        self.assertEqual(frame.return_motion.rigid_spin, spin)
        self.assertIsNotNone(merge_frame(self.arena, branch.snapshot, frame))
        self.assertIn(entity, world.dynamic_bodies)
        self.assertEqual(world.motion[entity].velocity, velocity)
        self.assertEqual(world.flight_spin(entity), spin)
        self.assertEqual(self.owner.pose, world.poses[entity])
        self.assertLess(math.dist(self.owner.pose.position, frame.pose.position), 1e-6)

        world.dynamic_commit(self.arena.STEP)
        after = world.poses[entity]
        self.assertNotEqual(after.roll, pose.roll)
        self.assertLess(after.position[1], pose.position[1])

    def test_airborne_merge_lands_safely_when_live_hull_occupies_the_exit(self):
        branch = self.fork()
        entity = branch.entity
        world = self.arena.world
        world.set_tracked_presence(entity, False)
        pose = Pose((2., 8., 3.), .27, .16, -.13)
        velocity, spin = (1., -2., 4.), (.4, -.3, .2)
        branch.world.relocate_training_body(entity, pose, motion=Motion(velocity, False))
        self.assertTrue(branch.world.set_flight_velocity(entity, velocity, spin))
        branch.finish()
        frame = _frame(branch)
        world.relocate_training_body(self.enemy.entity_id, pose, motion=Motion(velocity, False))

        self.assertIsNotNone(merge_frame(self.arena, branch.snapshot, frame))
        self.assertLess(self.owner.pose.position[1], pose.position[1])
        self.assertNotIn(entity, world.dynamic_bodies)
        self.assertTrue(world.motion[entity].grounded)
        self.assertEqual(world.motion[entity].velocity, (0., 0., 0.))
        self.assertEqual(world.poses[self.enemy.entity_id], pose)

    def test_worker_merge_preserves_live_boss_ability_dispatch(self):
        arena = self.arena
        arena.big_boss = boss = big_boss.BigBoss(arena.vehicles, 'worker-merge')
        boss.bosses.add(self.owner.entity_id)
        boss.roles.pop(self.owner.entity_id, None)
        self.enemy.definition = SimpleNamespace(mass=30000.)
        wave, megashot = big_boss.BLAST_WAVE, big_boss.MEGASHOT
        self.owner.consumables = (ConsumableState(wave), ConsumableState(megashot))
        now = arena.simulation_time
        self.owner.consumables[1].start_cooldown(now, 3.)
        branch = self.fork(level=Level(50, 2.))
        branch.steps = round(2. / branch.STEP)
        branch.finish()
        completed = pickle.loads(pickle.dumps(_frame(branch)))
        self.assertIsNot(completed.consumables[0].spec, wave)
        self.assertIsNotNone(merge_frame(arena, branch.snapshot, completed))
        arena.drain_events()

        before = self.enemy.health
        self.assertTrue(arena.use_consumable(1, wave.ident, now=now))
        self.assertLess(self.enemy.health, before)
        self.assertGreater(arena.world.motion[self.enemy.entity_id].velocity[2], 0.)
        self.assertTrue(any(event.kind == 'boss_blast' for event in arena.drain_events()))
        arena.fire(1, now=now)
        projectile, = arena.projectiles.values()
        self.assertEqual(projectile.shell.effect_index, big_boss.MEGASHOT_EFFECT)
        self.assertEqual(self.owner.consumables[1].uses, 1)

    def test_merge_updates_hp_and_reports_hits_on_separate_ticks(self):
        arena = self.arena
        with patch.object(BattleTimeStop, '_warm'):
            timeline = arena.time_stop = BattleTimeStop(arena)
        self.owner.consumables = (ConsumableState(big_boss.BLAST_WAVE),)
        branch = self.fork()
        self.assertTrue(branch.activate(1, big_boss.BLAST_WAVE.ident))
        self.assertTrue(branch.ledger.charge(2, 5))
        branch.ledger.record(2, .2, Entity.of(self.enemy), 'damage', 13)
        branch.ledger.record(2, .4, Entity.of(self.enemy), 'damage', 17)
        explosion = BattleEvent('explosion', self.owner.entity_id, 0, {
            'shot': 100, 'shell_id': self.shell.compact_id, 'material': 3,
            'point': self.enemy.pose.position, 'direction': (0., 0., 1.), 'skip_owner': True,
        })
        branch.merge_visuals.append(MergeVisual(.2, 2, explosion))
        branch.finish()
        self.enemy.health = sum(effect.value for effect in branch.ledger.events
                                if effect.target == Entity.of(self.enemy) and effect.kind == 'damage')
        timeline.snapshot, timeline.account = branch.snapshot, 1
        timeline.frame = pickle.loads(pickle.dumps(_frame(branch)))
        timeline.worker = Mock(poll=Mock(return_value=None))
        arena.drain_events()
        now = arena.simulation_time
        target, source = self.enemy.entity_id, self.owner.entity_id
        first_hit = received_damage_message(target, source, 13)
        last_hit = received_damage_message(target, source, 17)
        ability = provision_effect_message(source, big_boss.BLAST_WAVE.compact_id)
        burst = event_messages(arena, arena.players[2], (explosion,))
        self.assertTrue(burst)

        timeline.tick(now)
        self.assertIsNone(timeline.entity)
        self.assertEqual(self.enemy.health, 30)
        events = arena.drain_events()
        immediate = event_messages(arena, arena.players[2], events)
        self.assertIn(vehicle_property(target, 3, struct.pack('<h', 30)), immediate)
        self.assertIn(ability, immediate)
        self.assertNotIn(burst, immediate)
        self.assertNotIn(ability, event_messages(arena, arena.players[1], events))
        self.assertNotIn(first_hit, immediate)
        self.assertNotIn(last_hit, immediate)

        arena.simulation_time = now + 5 * arena.STEP
        timeline.tick(arena.simulation_time)
        self.assertEqual(self.enemy.health, 30)
        events = arena.drain_events()
        self.assertNotIn(ability, event_messages(arena, arena.players[2], events))
        arena.simulation_time = now + 6 * arena.STEP
        timeline.tick(arena.simulation_time)
        self.assertEqual(self.enemy.health, 17)
        events = arena.drain_events()
        incoming = event_messages(arena, arena.players[2], events)
        self.assertIn(first_hit, incoming)
        self.assertIn(burst, incoming)
        self.assertNotIn(burst, event_messages(arena, arena.players[1], events))
        self.assertNotIn(last_hit, incoming)
        arena.simulation_time = now + 12 * arena.STEP
        timeline.tick(arena.simulation_time)
        self.assertEqual(self.enemy.health, 0)
        events = arena.drain_events()
        incoming = event_messages(arena, arena.players[2], events)
        self.assertIn(last_hit, incoming)
        self.assertNotIn(first_hit, incoming)
        self.assertEqual(sum(event.kind == 'killed' for event in events), 1)
        timeline.tick(now+.4)
        self.assertEqual(self.owner.kills, 1)

    def test_queued_time_stop_damage_does_not_hit_a_new_life(self):
        arena = self.arena
        with patch.object(BattleTimeStop, '_warm'):
            timeline = arena.time_stop = BattleTimeStop(arena)
        branch = self.fork()
        self.assertTrue(branch.ledger.charge(1, 5))
        for at in (.1, .2):
            branch.ledger.record(1, at, Entity.of(self.enemy), 'damage', 13)
        branch.finish()
        self.assertIsNotNone(merge_frame(arena, branch.snapshot, _frame(branch),
                                         defer_hit=timeline._queue_merge_hit))
        self.enemy.generation += 1
        self.enemy.health = self.enemy.max_health
        timeline.tick(arena.simulation_time+.1)
        self.assertEqual(self.enemy.health, self.enemy.max_health)
        self.assertEqual(self.owner.hits, 1)
        self.assertFalse(timeline.merge_hits)

    def test_committed_time_stop_hits_survive_source_tank_replacement(self):
        arena = self.arena
        with patch.object(BattleTimeStop, '_warm'):
            timeline = arena.time_stop = BattleTimeStop(arena)
        branch = self.fork()
        self.assertTrue(branch.ledger.charge(1, 5))
        for at in (.1, .2):
            branch.ledger.record(1, at, Entity.of(self.enemy), 'damage', 13)
        branch.finish()
        health = self.enemy.health
        self.assertIsNotNone(merge_frame(arena, branch.snapshot, _frame(branch),
                                         defer_hit=timeline._queue_merge_hit))
        self.assertEqual(self.enemy.health, health - 13)
        arena.unlimited_lives = True
        with patch.object(arena, '_configure_battle_vehicle'):
            replacement = arena._restore_vehicle(self.owner.entity_id, self.owner.pose)
        self.assertEqual(replacement.account_id, self.owner.account_id)
        self.assertNotEqual(Entity.of(replacement), branch.snapshot.owner)
        arena.simulation_time += 6 * arena.STEP
        timeline.tick(arena.simulation_time)
        self.assertEqual(self.enemy.health, health - 26)
        self.assertEqual((replacement.hits, replacement.damage), (2, 26))
        self.assertFalse(timeline.merge_hits)

    def test_mass_wave_spreads_launches_without_moving_damage_or_lethal_push(self):
        arena = self.arena
        with patch.object(BattleTimeStop, '_warm'):
            timeline = arena.time_stop = BattleTimeStop(arena)
        targets = [replace(self.enemy, entity_id=400 + i, health=100, account_id=None) for i in range(9)]
        targets[-1].health = 20
        ledger = EffectLedger('mass-wave', Entity.of(self.owner), 50)
        self.assertTrue(ledger.charge(1, 20))
        push = Impulse((20., 8., 0.), (1., 0., 0.))
        for target in targets:
            arena.vehicles[target.entity_id] = target
            arena.world.motion[target.entity_id] = Motion()
            identity = Entity.of(target)
            ledger.record(1, .1, identity, 'impulse', push)
            ledger.record(1, .1, identity, 'damage', 20, shell_hit=False)

        def launch(entity, *args, **kwargs):
            self.assertTrue(arena.vehicles[entity].alive)
            return True

        now = arena.simulation_time
        with (patch.object(arena, '_player_attached', return_value=True),
              patch.object(arena.world, 'deferred_impulse', side_effect=launch) as launches):
            outcome = ledger.merge(arena, defer_hit=timeline._queue_merge_hit)
            self.assertEqual(outcome.damage_ticks, {(.1, 1): 0})
            self.assertEqual([target.health for target in targets], [80]*8 + [0])
            self.assertEqual([call.args[0] for call in launches.call_args_list],
                             [target.entity_id for target in (*targets[:5], targets[-1])])
            self.assertEqual(len(timeline.merge_hits), 3)
            targets[6].generation += 1
            self.assertFalse(ledger.merge(arena, defer_hit=timeline._queue_merge_hit).applied)
            arena.simulation_time = now + arena.STEP
            timeline.tick(arena.simulation_time)
            self.assertEqual(launches.call_count, 8)
            self.assertEqual(launches.call_args_list[-2].args[0], targets[5].entity_id)
            self.assertEqual(launches.call_args.args[0], targets[7].entity_id)
            self.assertFalse(timeline.merge_hits)

    def test_time_stop_hit_spacing_compresses_a_long_volley(self):
        target, other = Entity.of(self.enemy), Entity.of(self.ally)
        ledger = EffectLedger(42, Entity.of(self.owner), 50)
        self.assertTrue(ledger.charge(1, 5))
        ledger.record(1, .1, other, 'damage', 1)
        for index in range(15):
            ledger.record(1, .1 + index * .2, target, 'damage', 1)
        ticks = ledger._hit_ticks(ledger.events, self.arena.STEP)
        self.assertNotIn(other, ticks)
        frames = list(ticks[target].values())
        self.assertEqual((frames[0], frames[-1]), (0, 24))
        self.assertEqual(len(set(frames)), 15)

    def test_active_consumable_rejection_uses_the_owners_battle_notice(self):
        arena = self.arena
        gear = ConsumableState(ConsumableSpec('afterburning', 14, 3835, 15., 45., engine_factor=1.2))
        gear.activate(arena.simulation_time)
        self.owner.consumables = (gear,)
        with patch.object(BattleTimeStop, '_warm'):
            timeline = arena.time_stop = BattleTimeStop(arena)
        self.assertFalse(timeline.toggle(1))
        events = arena.drain_events()
        notice = time_stop_message(7, (0., 0., 0.))
        self.assertIn(notice, event_messages(arena, arena.players[1], events))
        self.assertNotIn(notice, event_messages(arena, arena.players[2], events))

    def test_queued_activation_uses_new_hulls_and_survives_worker_warmup(self):
        arena = self.arena
        timeline = arena.time_stop = BattleTimeStop(arena)
        self.assertTrue(timeline.toggle(1))
        self.assertTrue(timeline.toggle(1))
        self.assertFalse(timeline.toggle(2))
        self.assertEqual(sum(event.kind == 'time_stop_queued' for event in arena.events), 1)
        world = arena.world
        new_hull = QueryAssets('flat')
        new_hull.armor = world.assets.armor
        new_hull.definition.mass += 2000.
        self.addCleanup(new_hull.mesh_files.close)
        entity = self.enemy.entity_id
        x, _, z = self.enemy.pose.position
        placed = world.replace_training_body(entity, new_hull, ((x+12., z, 0.),))
        self.assertIsNotNone(placed)
        self.enemy.pose = placed
        timeline.refresh_geometry()
        deadline = time.monotonic()+8.
        while timeline.frame is None and time.monotonic() < deadline:
            timeline.tick(arena.simulation_time)
            time.sleep(.005)
        self.assertIsNotNone(timeline.frame)
        self.assertEqual(timeline.account, 1)
        self.assertIsNone(timeline.pending)
        self.assertEqual(timeline.geometry.vehicle_assets[entity].definition.mass, new_hull.definition.mass)

    def test_leaving_cancels_the_activation_waiting_for_the_worker(self):
        arena = self.arena
        timeline = arena.time_stop = BattleTimeStop(arena)
        self.assertTrue(timeline.toggle(1))
        arena.cancel_time_stop(1)
        self.assertIsNone(timeline.pending)
        deadline = time.monotonic()+8.
        while not timeline.ready and time.monotonic() < deadline:
            timeline.tick(arena.simulation_time)
            time.sleep(.005)
        self.assertTrue(timeline.ready)
        self.assertIsNone(timeline.account)

    def test_suspended_hull_is_absent_even_when_its_pose_overlaps_live_physics(self):
        arena, owner, enemy = self.arena, self.owner, self.enemy
        world = arena.world
        entity, body = owner.entity_id, world.bodies[owner.entity_id]
        overlap = replace(owner.pose, position=(0., owner.pose.position[1], .5))
        ray = ((0., 1., -5.), (0., 0., 1.), 10.)
        health = owner.health
        self.assertTrue(any(point[2] == body for point in world._contacts(overlap, enemy.entity_id)))
        self.assertEqual(world.shot_path(*ray, ignore_entity=enemy.entity_id)[0], body)

        world.set_tracked_presence(entity, False)
        self.assertFalse(any(point[2] == body for point in world._contacts(overlap, enemy.entity_id)))
        self.assertNotEqual(world.shot_path(*ray, ignore_entity=enemy.entity_id)[0], body)
        states = body_states(arena.vehicles)
        self.assertNotIn(entity, arena._armor_frame_states(states, states, 0., arena.simulation_time))
        self.assertEqual(arena._damage_health(owner, 500, enemy.entity_id, arena.simulation_time,
                                             shell_hit=True), 0)
        self.assertEqual(owner.health, health)

        world.set_tracked_presence(entity, True)
        self.assertTrue(any(point[2] == body for point in world._contacts(overlap, enemy.entity_id)))
        self.assertEqual(world.shot_path(*ray, ignore_entity=enemy.entity_id)[0], body)

    def test_abort_discards_paid_but_unmerged_effects(self):
        branch = self.fork()
        self.assertTrue(branch.ledger.charge(1, 5))
        branch.ledger.record(1, .1, Entity.of(self.enemy), 'damage', 250)
        branch.abort()
        self.assertFalse(branch.ledger.merge(self.arena).applied)
        self.assertEqual(self.enemy.health, self.enemy.max_health)

    def test_worker_steps_without_running_branch_physics_in_arena(self):
        worker = self.worker()
        self.assertNotEqual(worker.process.pid, os.getpid())
        self.assertFalse(worker.submit(Step()))
        self.arena.step(10.02)
        self.assertGreater(self.arena.step_count, 0)
        initialized = self.reply(worker)
        self.assertIsInstance(initialized, Frame)
        self.assertTrue(worker.submit(Step(flags=1)))
        self.assertFalse(worker.submit(Step()))
        moved = self.reply(worker)
        self.assertIsInstance(moved, Frame)
        self.assertGreater(moved.pose.position[2], initialized.pose.position[2])
        self.assertEqual(self.owner.pose.position[2], initialized.pose.position[2])
        self.assertTrue(worker.submit(Step(finish=True)))
        completed = self.reply(worker)
        self.assertIsNotNone(completed.ledger)
        self.assertFalse(worker.submit(Step()))

    def test_warmed_worker_accepts_airborne_owner_and_resumes_the_fall(self):
        arena, owner = self.arena, self.owner
        world, entity = arena.world, owner.entity_id
        timeline = arena.time_stop = BattleTimeStop(arena)

        def wait_for(condition):
            deadline = time.monotonic() + 6.
            while not condition() and time.monotonic() < deadline:
                timeline.tick(arena.simulation_time)
                time.sleep(.008)
            self.assertTrue(condition())

        wait_for(lambda: timeline.ready)
        pose = Pose((2., 8., 3.), .27, .16, -.13)
        velocity, spin = (1., -2., 4.), (.4, -.3, .2)
        world.relocate_training_body(entity, pose, motion=Motion(velocity, False))
        self.assertTrue(world.set_flight_velocity(entity, velocity, spin))
        owner.pose = world.poses[entity]
        self.assertTrue(timeline.toggle(1))
        self.assertIn(entity, world.suspended_entities)
        self.assertNotIn(entity, world.dynamic_bodies)
        wait_for(lambda: timeline.frame is not None and timeline.frame.seconds >= .05)
        self.assertLess(timeline.frame.pose.position[1], pose.position[1])
        self.assertGreater(timeline.frame.pose.position[2], pose.position[2])
        self.assertNotEqual(timeline.frame.pose.roll, pose.roll)

        self.assertTrue(timeline.toggle(1))
        wait_for(lambda: timeline.entity is None)
        self.assertNotIn(entity, world.suspended_entities)
        self.assertIn(entity, world.dynamic_bodies)
        self.assertLess(world.motion[entity].velocity[1], 0.)
        self.assertGreater(owner.pose.position[1], 6.)
        self.assertTrue(any(event.kind == 'time_stop_end' and not event.data['aborted']
                            for event in arena.events))

    def test_abort_restores_original_airborne_motion(self):
        arena, owner = self.arena, self.owner
        world, entity = arena.world, owner.entity_id
        with patch.object(BattleTimeStop, '_warm'):
            timeline = arena.time_stop = BattleTimeStop(arena)
        timeline.worker, timeline.ready = Mock(), True
        pose = Pose((2., 8., 3.), .27, .16, -.13)
        velocity, spin = (1., -2., 4.), (.4, -.3, .2)
        world.relocate_training_body(entity, pose, motion=Motion(velocity, False))
        self.assertTrue(world.set_flight_velocity(entity, velocity, spin))
        owner.pose = origin = world.poses[entity]

        self.assertTrue(timeline.toggle(1))
        self.assertTrue(timeline.cancel(1))
        self.assertNotIn(entity, world.suspended_entities)
        self.assertIn(entity, world.dynamic_bodies)
        self.assertEqual(world.motion[entity].velocity, velocity)
        self.assertEqual(world.flight_spin(entity), spin)
        self.assertLess(math.dist(owner.pose.position, origin.position), 1e-6)

    def test_disconnect_waits_for_a_safe_return_after_aborting(self):
        arena, owner = self.arena, self.owner
        world, entity = arena.world, owner.entity_id
        with patch.object(BattleTimeStop, '_warm'):
            timeline = arena.time_stop = BattleTimeStop(arena)
        worker = Mock(advance_close=Mock(return_value=True))
        timeline.worker, timeline.ready = worker, True
        origin = owner.pose
        self.assertTrue(timeline.toggle(1))
        parked = owner.pose
        with (patch('solbreaker.arena_time_stop.safe_return', side_effect=(None, origin)) as search,
              patch('solbreaker.arena_time_stop.return_spawn', return_value=None),
              patch.object(timeline, '_try_warm')):
            arena.leave(1)
            self.assertFalse(arena.players[1].attached)
            self.assertEqual(owner.pose, parked)
            self.assertIn(entity, world.suspended_entities)
            self.assertFalse(any(event.kind == 'time_stop_end' for event in arena.events))
            arena.step(arena.last_tick + arena.STEP)
            self.assertEqual(search.call_count, 1)
            self.assertIsNone(timeline.worker)
            arena.step(arena.last_tick + timeline.RETURN_RETRY)
            self.assertEqual(search.call_count, 2)
        self.assertEqual(owner.pose, origin)
        self.assertNotIn(entity, world.suspended_entities)
        worker.poll.assert_not_called()
        self.assertIsNone(timeline.account)
        end = [event for event in arena.events if event.kind == 'time_stop_end']
        self.assertEqual(len(end), 1)
        self.assertTrue(end[0].data['aborted'])

    def test_battle_end_clears_a_blocked_time_stop_without_restoring_the_hull(self):
        arena, owner = self.arena, self.owner
        with patch.object(BattleTimeStop, '_warm'):
            timeline = arena.time_stop = BattleTimeStop(arena)
        timeline.worker, timeline.ready = Mock(advance_close=Mock(return_value=True)), True
        self.assertTrue(timeline.toggle(1))
        parked = owner.pose
        with (patch('solbreaker.arena_time_stop.safe_return', return_value=None),
              patch('solbreaker.arena_time_stop.return_spawn', return_value=None)):
            arena.leave(1)
            self.assertIsNotNone(timeline.snapshot)
            arena.finish()
        self.assertIsNone(timeline.snapshot)
        self.assertEqual(owner.pose, parked)
        self.assertIn(owner.entity_id, arena.world.suspended_entities)
        end = [event for event in arena.events if event.kind == 'time_stop_end']
        self.assertEqual(len(end), 1)
        self.assertTrue(end[0].data['aborted'])

    def test_winning_time_stop_hit_finishes_battle_after_return(self):
        arena = self.arena
        with patch.object(BattleTimeStop, '_warm'):
            timeline = arena.time_stop = BattleTimeStop(arena)
        self.ally.team = self.enemy.team
        branch = self.fork()
        exit_pose = replace(self.owner.pose, position=(3., self.owner.pose.position[1], 1.))
        branch.world.relocate_training_body(branch.entity, exit_pose)
        self.assertTrue(branch.ledger.charge(1, 5))
        branch.ledger.record(1, .1, Entity.of(self.enemy), 'damage', self.enemy.health)
        branch.ledger.record(1, .2, Entity.of(self.ally), 'damage', 15)
        branch.finish()
        timeline.snapshot, timeline.account = branch.snapshot, 1
        timeline.frame = _frame(branch)
        timeline.worker = Mock(poll=Mock(return_value=None))
        arena.world.set_tracked_presence(branch.entity, False)
        arena.drain_events()
        damage = arena._damage_health

        def award_victory(target, *args, **kwargs):
            amount = damage(target, *args, **kwargs)
            if target is self.enemy and not target.alive:
                arena.finish(1, 'supremacy')
            return amount

        with patch.object(arena, '_damage_health', side_effect=award_victory):
            timeline.tick(arena.simulation_time)

        self.assertIsNone(timeline.snapshot)
        self.assertEqual(self.owner.pose, exit_pose)
        self.assertEqual(self.ally.health, self.ally.max_health - 15)
        self.assertEqual(arena.winner, 1)
        events = arena.drain_events()
        returned, = (event for event in events if event.kind == 'time_stop_end')
        finished, = (event for event in events if event.kind == 'period' and event.data['period'] == 4)
        self.assertFalse(returned.data['aborted'])
        self.assertLess(events.index(returned), events.index(finished))

    def test_warmed_worker_uses_the_activating_players_level(self):
        arena = self.arena
        timeline = arena.time_stop = BattleTimeStop(arena, rules=Rules(starting_level=2))

        def wait_for(condition):
            deadline = time.monotonic() + 6.
            while not condition() and time.monotonic() < deadline:
                timeline.tick(arena.simulation_time)
                time.sleep(.008)
            self.assertTrue(condition())

        wait_for(lambda: timeline.ready)
        worker = timeline.worker
        self.owner.reload_at = arena.simulation_time + 8.
        timeline.set_player_level(1, 3)
        self.assertTrue(timeline.toggle(1))
        wait_for(lambda: timeline.frame is not None)
        first_branch = timeline.frame.branch
        self.assertEqual(timeline.frame.energy, 80)
        self.assertEqual(timeline.frame.reload_at, 4.)
        self.assertTrue(any(event.kind == 'time_stop_begin' and event.data['energy'] == 80
                            for event in arena.events))

        timeline.set_player_level(1, 5)
        self.assertEqual(timeline.level.energy, 80)
        self.assertTrue(timeline.toggle(1))
        wait_for(lambda: timeline.ready)
        self.assertIs(timeline.worker, worker)
        self.assertTrue(timeline.toggle(2))
        wait_for(lambda: timeline.frame is not None)
        self.assertNotEqual(timeline.frame.branch, first_branch)
        self.assertEqual(timeline.frame.seconds, 0.)
        self.assertEqual(timeline.frame.energy, 65)
        self.assertTrue(timeline.toggle(2))
        wait_for(lambda: timeline.ready)
        self.assertIs(timeline.worker, worker)
        self.assertTrue(timeline.toggle(1))
        wait_for(lambda: timeline.frame is not None)
        self.assertEqual(timeline.frame.energy, 125)

    def test_same_tick_fire_uses_energy_before_later_ability(self):
        arena = self.arena
        self.owner.consumables = (ConsumableState(big_boss.BLAST_WAVE),)
        arena.time_stop = timeline = BattleTimeStop(arena, rules=Rules(shot_cost=40))

        def wait_for(condition):
            deadline = time.monotonic() + 6.
            while not condition() and time.monotonic() < deadline:
                timeline.tick(arena.simulation_time)
                time.sleep(.008)
            self.assertTrue(condition())

        wait_for(lambda: timeline.ready)
        with patch.object(type(self.owner), 'clip_size', 1):
            self.assertTrue(timeline.toggle(1))
        apply_at_tick(arena, ArenaCommand(1, 'fire', (), arena.simulation_time))
        args = (self.owner.entity_id, big_boss.BLAST_WAVE.ident, b'')
        apply_at_tick(arena, ArenaCommand(1, 'consumable', args, arena.simulation_time))
        wait_for(lambda: timeline.frame is not None and timeline.frame.energy < 50)
        self.assertEqual(timeline.frame.energy, 10)
        self.assertEqual(timeline.frame.shots_fired, 1)
        self.assertEqual(timeline.frame.abilities_used, 0)

    def test_worker_fault_leaves_main_arena_running(self):
        worker = self.worker()
        self.assertIsInstance(self.reply(worker), Frame)
        self.assertTrue(worker.submit(Step(flags=-1)))
        self.assertIsInstance(self.reply(worker), Failure)
        self.arena.step(10.04)
        self.assertGreater(self.arena.step_count, 0)
        self.assertEqual(self.enemy.health, self.enemy.max_health)

    def test_active_worker_failure_restores_owner_and_observer_roster(self):
        arena, owner, enemy = self.arena, self.owner, self.enemy
        self.assertTrue(arena.autoaim(1, enemy.entity_id, (0., 1., 12.)))
        arena.time_stop = timeline = BattleTimeStop(arena)

        def wait_for(condition):
            deadline = time.monotonic() + 6.
            while not condition() and time.monotonic() < deadline:
                arena.step(arena.last_tick + arena.STEP)
                time.sleep(.008)
            self.assertTrue(condition())

        wait_for(lambda: timeline.ready)
        origin = owner.pose.position[::2]
        self.assertTrue(timeline.toggle(1))
        wait_for(lambda: timeline.frame is not None and timeline.frame.aim_lock is not None)
        apply_at_tick(arena, ArenaCommand(1, 'autoaim', (owner.entity_id, 0)))
        wait_for(lambda: timeline.frame is not None and timeline.frame.aim_lock is None)
        arena.drain_events()
        timeline.worker.process.terminate()
        wait_for(lambda: timeline.account is None)

        events = arena.drain_events()
        self.assertTrue(any(e.kind == 'time_stop_worker_error' for e in events))
        self.assertTrue(any(e.kind == 'time_stop_end' and e.data['aborted'] for e in events))
        self.assertEqual(owner.pose.position[::2], origin)
        self.assertEqual(owner.autoaim_target, enemy.entity_id)
        self.assertNotIn(owner.entity_id, arena.world.suspended_entities)
        self.assertIn(time_stop_roster_message(0), event_messages(arena, arena.players[2], events))
        before = arena.step_count
        arena.step(arena.last_tick+arena.STEP)
        self.assertGreater(arena.step_count, before)

    @skipUnless(hasattr(signal, 'SIGSTOP'), 'Requires a POSIX process suspension signal')
    def test_prepare_timeout_replaces_stalled_worker_on_the_next_press(self):
        arena, owner = self.arena, self.owner
        arena.time_stop = timeline = BattleTimeStop(arena)
        stalled = timeline.worker
        os.kill(stalled.process.pid, signal.SIGSTOP)
        origin = owner.pose

        def wait_for(condition):
            deadline = time.monotonic() + 6.
            while not condition() and time.monotonic() < deadline:
                timeline.tick(arena.simulation_time)
                time.sleep(.008)
            self.assertTrue(condition())

        with (patch.object(timeline, 'PREPARE_WAIT', .08),
              patch.object(stalled, 'poll', return_value=None)):
            self.assertTrue(timeline.toggle(1))
            wait_for(lambda: timeline.retiring)

        self.assertIsNone(timeline.account)
        self.assertEqual(owner.pose, origin)
        self.assertTrue(any(event.kind == 'time_stop_rejected' and event.data['reason'] == 'prepare_timeout'
                            for event in arena.events))
        self.assertTrue(timeline.toggle(1))
        wait_for(lambda: timeline.frame is not None)
        self.assertTrue(stalled.closed)
        self.assertIsNot(timeline.worker, stalled)
        self.assertEqual(timeline.account, 1)

    @skipUnless(hasattr(signal, 'SIGSTOP'), 'Requires a POSIX process suspension signal')
    def test_stalled_worker_restores_the_owner_and_is_reaped(self):
        arena, owner = self.arena, self.owner
        arena.time_stop = timeline = BattleTimeStop(arena)

        def wait_for(condition):
            deadline = time.monotonic() + 6.
            while not condition() and time.monotonic() < deadline:
                arena.step(arena.last_tick + arena.STEP)
                time.sleep(.008)
            self.assertTrue(condition())

        wait_for(lambda: timeline.ready)
        origin = owner.pose.position[::2]
        self.assertTrue(timeline.toggle(1))
        wait_for(lambda: timeline.frame is not None)
        worker = timeline.worker
        os.kill(worker.process.pid, signal.SIGSTOP)
        self.assertTrue(worker.process.is_alive())
        arena.drain_events()
        before = arena.step_count
        with patch.object(timeline, 'REPLY_TIMEOUT', .12):
            timeline.reply_deadline = time.monotonic() + timeline.REPLY_TIMEOUT
            wait_for(lambda: timeline.worker is None)

        events = arena.drain_events()
        self.assertTrue(any(e.kind == 'time_stop_worker_error' and e.data['reason'] == 'reply_timeout'
                            for e in events))
        self.assertTrue(any(e.kind == 'time_stop_end' and e.data['aborted'] for e in events))
        self.assertTrue(worker.closed)
        self.assertGreater(arena.step_count, before)
        self.assertEqual(owner.pose.position[::2], origin)
        self.assertNotIn(owner.entity_id, arena.world.suspended_entities)
        self.assertTrue(timeline.toggle(1))
        wait_for(lambda: timeline.frame is not None)

    def test_worker_merge_transfers_pose_angles_ammo_and_remaining_reload(self):
        snapshot = self.snapshot()
        worker = self.worker(snapshot)
        self.assertIsInstance(self.reply(worker), Frame)
        self.assertTrue(worker.submit(Step(flags=1, aim=(.1, .04), finish=True,
                                           actions=((1, 'fire', ()),))))
        frame = self.reply(worker)
        self.assertEqual(frame.energy, 45)
        self.assertEqual(self.owner.ammo, snapshot.ammo)
        self.assertIsNotNone(merge_frame(self.arena, snapshot, frame))
        self.assertEqual(self.owner.pose.position[::2], frame.pose.position[::2])
        self.assertEqual((self.owner.gun.yaw, self.owner.gun.pitch), frame.aim)
        self.assertEqual(self.owner.ammo, snapshot.ammo-1)
        self.assertEqual(self.owner.shots, 1)
        self.assertAlmostEqual(self.owner.reload_at, self.arena.simulation_time+frame.reload_remaining)
        self.assertIsNone(merge_frame(self.arena, snapshot, frame))

    def test_idle_worker_advances_and_delivers_its_own_expiry(self):
        worker = self.worker(rules=Rules(duration=1.5))
        self.assertIsInstance(self.reply(worker), Frame)
        completed = self.reply(worker)
        self.assertIsNotNone(completed.ledger)
        self.assertGreater(completed.seconds, 1.)

    def test_rearmed_worker_discards_input_queued_after_expiry(self):
        worker = Worker(None, self.geometry, rules=Rules(duration=.15))
        self.addCleanup(worker.close)
        self.assertIsInstance(self.reply(worker), Prepared)
        self.assertTrue(worker.activate(self.snapshot(), level=Level(50, 1., 100.)))
        self.assertIsInstance(self.reply(worker), Frame)
        deadline = time.monotonic() + 6.
        while worker._incoming.empty() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertFalse(worker._incoming.empty())
        # Until the arena polls the terminal frame, input can still enter the pipe.
        self.assertTrue(worker.submit(Step(actions=((1, 'fire', ()),))))
        completed = self.reply(worker)
        self.assertIsNotNone(completed.ledger)
        self.assertTrue(worker.prepare_next())
        self.assertIsInstance(self.reply(worker), Prepared)
        self.assertTrue(worker.activate(self.snapshot(), level=Level(65, 1.5, 150.)))
        self.assertEqual(self.reply(worker).energy, 65)
        completed = self.reply(worker)
        self.assertIsNotNone(completed.ledger)
        self.assertEqual(completed.shots_fired, 0)
        self.assertEqual(completed.energy, 65)

    def test_arena_routes_frozen_motion_and_recon_to_the_owner_only(self):
        arena = self.arena
        self.map_geometry('prop')
        self.owner.consumables = (ConsumableState(big_boss.BLAST_WAVE),)
        arena.time_stop = timeline = BattleTimeStop(arena)

        def wait_for(condition):
            deadline = time.monotonic() + 6.
            while not condition():
                if time.monotonic() >= deadline:
                    self.fail('Arena time branch did not make progress')
                timeline.tick(arena.simulation_time)
                time.sleep(.008)

        wait_for(lambda: timeline.ready)
        initial = self.owner.pose
        frozen = self.enemy.pose
        active = Projectile(17, self.enemy.entity_id, self.shell, (1., 2., 3.),
                            (4., 5., 6.), arena.simulation_time, trace_effect=23)
        missile = replace(self.shell, speed=12., effect_index=29,
                          missile=MissileFlight(20., 1., 1., 1.))
        retired = Projectile(18, self.enemy.entity_id, missile, (7., 8., 9.),
                             (10., 11., 12.), arena.simulation_time)
        arena.projectiles.update({17: active, 18: retired})
        control = Control(token(arena), 1, TIME_STOP, 0, 0., 0., 0.)
        arena.sandbox_control(1, control)
        self.assertEqual(timeline.account, 1)
        self.assertNotEqual(self.owner.pose, initial)
        self.assertIn(self.owner.entity_id, arena.world.suspended_entities)
        self.assertFalse(timeline.toggle(2))

        events = arena.drain_events()
        begin_owner = event_messages(arena, arena.players[1], events)
        begin_peer = event_messages(arena, arena.players[2], events)
        self.assertIn(b'TFS1TIME'+struct.pack('<I', 1), begin_owner)
        self.assertIn(b'TFS1TIME'+struct.pack('<I', 2), begin_peer)
        hidden_row = time_stop_roster_message(self.owner.entity_id)
        self.assertNotIn(hidden_row, begin_owner)
        self.assertIn(hidden_row, begin_peer)
        reconnect = reconnect_message_parts(arena, arena.players[2], refresh_period=False)
        self.assertIn(hidden_row, reconnect)
        # The hull can still be waiting for a safe return beyond the clock.
        with patch('solbreaker.arena_time_stop.time.monotonic',
                   return_value=timeline.snapshot.captured_at+timeline.rules.duration+2.):
            reconnect = reconnect_message_parts(arena, arena.players[2], refresh_period=False)
        self.assertIn(hidden_row, reconnect)
        self.assertIn(time_stop_message(6, initial.position, seconds=0.), reconnect)
        ballistic = avatar_method(29, struct.pack('<IIB7f', active.shooter, 17, 23,
                              *active.position, *active.velocity, active.shell.gravity))
        guided = missile_message(retired.shooter, 18, 29, retired.position, retired.velocity)
        self.assertIn(ballistic, begin_owner)
        self.assertIn(guided, begin_owner)
        self.assertNotIn(ballistic, begin_peer)
        self.assertNotIn(guided, begin_peer)
        active.position = (14., 15., 16.)
        del arena.projectiles[18]
        self.assertTrue(arena.world.destroy(7, (1., 0.)))
        _, yaw, angle = arena.world.destruction_events.pop()
        arena.destructions[7] = (yaw, angle)
        arena.emit('destruction', key=7, yaw=yaw, angle=angle)
        destruction = destructible_state(arena).updates()
        self.assertTrue(destruction)
        events = arena.drain_events()
        self.assertNotIn(destruction, event_messages(arena, arena.players[1], events))
        self.assertIn(destruction, event_messages(arena, arena.players[2], events))
        parked = self.owner.pose
        self.assertIn(b'\x14'+struct.pack('<III6f', self.owner.entity_id, arena.space_id, 0,
                                         *parked.position, parked.roll, parked.pitch, parked.yaw), begin_peer)
        self.assertNotIn(self.owner, [v for _, v in present_vehicles(arena, arena.players[2])])

        actual = replace(frozen, position=(16., frozen.position[1], 19.))
        arena.world.relocate_training_body(self.enemy.entity_id, actual)
        self.enemy.pose = actual
        apply_at_tick(arena, ArenaCommand(1, 'control', (1,), arena.simulation_time))
        wait_for(lambda: timeline.frame is not None and timeline.frame.pose != initial)
        frozen_wire = vehicle_snapshot(self.enemy.entity_id, frozen.position,
                                       frozen.yaw, frozen.pitch, frozen.roll)
        live_wire = vehicle_snapshot(self.enemy.entity_id, actual.position,
                                     actual.yaw, actual.pitch, actual.roll)
        owner_wire = motion_messages(arena, arena.players[1])
        self.assertIn(frozen_wire, owner_wire)
        self.assertIn(vehicle_property(self.owner.entity_id, 4, bytes((3, 1))), owner_wire)
        self.assertIn(vehicle_property(self.enemy.entity_id, 4, bytes((0, 0))), owner_wire)
        self.assertIn(live_wire, motion_messages(arena, arena.players[2]))

        arena.motion_frame = None
        arena._record_motion_frame(arena.simulation_time)
        transport = GameArenaHandler(arena, None)
        frame = arena.motion_frame
        viewers = (2, 1, 3)
        batch = transport.apply(Command('motions', arena.simulation_time, 0, struct.pack('<3I', *viewers)))
        self.assertEqual([row.account_id for row in batch.motions], list(viewers))
        self.assertEqual([row.body for row in batch.motions],
                         [motion_messages(arena, arena.players[account], frame) for account in viewers])
        self.assertIn(frozen_wire, batch.motions[1].body)
        self.assertNotIn(frozen_wire, batch.motions[0].body)
        combined = transport.apply(Command('step', arena.last_tick, 0,
            motion_viewers=((2, frame.tick), (1, None), (3, None), (999, None))))
        self.assertEqual(combined.motions, batch.motions[1:])

        args = (self.owner.entity_id, big_boss.BLAST_WAVE.ident, b'')
        apply_at_tick(arena, ArenaCommand(1, 'consumable', args, arena.simulation_time))
        wait_for(lambda: timeline.frame.abilities_used == 1)
        self.assertEqual(timeline.frame.energy, 30)
        private_effects = arena.drain_events()
        self.assertTrue(any(event.kind == 'time_stop_ability' for event in private_effects))
        self.assertTrue(event_messages(arena, arena.players[1], private_effects))
        self.assertFalse(event_messages(arena, arena.players[2], private_effects))

        arena.recon = ReconState()
        self.addCleanup(setattr, arena, 'recon', None)
        handler = GameArenaHandler(arena, None)
        for account in (1, 2):
            message = handler.apply(Command('recon_snapshot', arena.simulation_time,
                                            account, struct.pack('<I', 1)))
            self.assertIsNotNone(message.recon)

        arena.sandbox_control(1, control)
        wait_for(lambda: timeline.account is None)
        events = arena.drain_events()
        resumed = avatar_method(29, struct.pack('<IIB7f', active.shooter, 17, 23,
                            *active.position, *active.velocity, active.shell.gravity))
        stopped = avatar_method(23, struct.pack('<I3fB', 18, *retired.position, 0))
        self.assertIn(time_stop_roster_message(0), event_messages(arena, arena.players[2], events))
        self.assertNotIn(time_stop_roster_message(0), event_messages(arena, arena.players[1], events))
        self.assertIn(resumed, event_messages(arena, arena.players[1], events))
        self.assertIn(stopped, event_messages(arena, arena.players[1], events))
        self.assertNotIn(resumed, event_messages(arena, arena.players[2], events))
        self.assertNotIn(stopped, event_messages(arena, arena.players[2], events))
        self.assertIn(destruction, event_messages(arena, arena.players[1], events))
        self.assertNotIn(destruction, event_messages(arena, arena.players[2], events))
        self.assertNotEqual(self.owner.pose, initial)
        self.assertGreater(self.owner.consumables[0].ready_at, arena.simulation_time)
        self.assertNotIn(self.owner.entity_id, arena.world.suspended_entities)
        self.assertIn(self.owner, [v for _, v in present_vehicles(arena, arena.players[2])])
