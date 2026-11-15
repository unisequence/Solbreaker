"""Exercise branch movement and shell hits across independent Bullet worlds."""
from dataclasses import replace
import os
import pickle
import struct
import time
from unittest import TestCase
from unittest.mock import patch

import numpy as np

from solbreaker import big_boss
from solbreaker.arena import Arena
from solbreaker.arena_commands import ArenaCommand, apply_at_tick
from solbreaker.arena_owner import Command
from solbreaker.arena_owner_game import GameArenaHandler
from solbreaker.arena_time_stop import BattleTimeStop
from solbreaker.armor import ArmorHit, ArmorModel
from solbreaker.ballistics import Projectile, TrackContact, body_states
from solbreaker.battle import (
    avatar_method, destructible_state, event_messages, missile_message, motion_messages, present_vehicles,
)
from solbreaker.collision import CollisionWorld, Pose
from solbreaker.consumables import ConsumableState
from solbreaker.drive_replica import build_spec
from solbreaker.motion import vehicle_snapshot
from solbreaker.modules import TrackSpec, TrackState
from solbreaker.recon import ReconState
from solbreaker.sandbox import TIME_STOP, Control, token
from solbreaker.time_stop import (
    EffectLedger, Entity, Impulse, Level, Rules, Snapshot, TimelineSimulation, merge_frame, safe_return,
)
from solbreaker.time_stop_worker import Failure, Frame, Step, Worker
from solbreaker.vehicle_data import MissileFlight, Shell
from tests.test_native_physics import QueryAssets, terrain


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
        return replace(Snapshot.capture(self.arena, 1, 30.), clip_size=1)

    def fork(self, **options):
        branch = TimelineSimulation(self.snapshot(), self.geometry, **options)
        self.addCleanup(branch.close)
        return branch

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
            branch.tick(30. + tick*branch.STEP, distance=.2)
        self.assertGreater(branch.pose.position[2], .1)
        self.assertLessEqual(branch.pose.position[2], .6)
        self.assertEqual(branch.world.poses[self.enemy.entity_id], frozen)
        self.assertEqual(self.enemy.pose, relocated)
        self.assertEqual(self.owner.pose, branch.activation_pose)
        self.assertEqual(branch.ledger.energy, 50)
        self.assertFalse(branch.tick(45.))

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
        self.assertTrue(worker.submit(Step(distance=.2)))
        self.assertFalse(worker.submit(Step()))
        moved = self.reply(worker)
        self.assertIsInstance(moved, Frame)
        self.assertGreater(moved.pose.position[2], initialized.pose.position[2])
        self.assertEqual(self.owner.pose.position[2], initialized.pose.position[2])
        self.assertTrue(worker.submit(Step(finish=True)))
        completed = self.reply(worker)
        self.assertIsNotNone(completed.ledger)
        self.assertFalse(worker.submit(Step()))

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
        self.assertTrue(worker.submit(Step(distance=float('nan'))))
        self.assertIsInstance(self.reply(worker), Failure)
        self.arena.step(10.04)
        self.assertGreater(self.arena.step_count, 0)
        self.assertEqual(self.enemy.health, self.enemy.max_health)

    def test_worker_merge_transfers_pose_angles_ammo_and_remaining_reload(self):
        snapshot = self.snapshot()
        worker = self.worker(snapshot)
        self.assertIsInstance(self.reply(worker), Frame)
        self.assertTrue(worker.submit(Step(distance=.12, aim=(.1, .04),
                                           fire=(1, self.shell, 6.))))
        self.assertIsInstance(self.reply(worker), Frame)
        self.assertTrue(worker.submit(Step(finish=True)))
        frame = self.reply(worker)
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
        self.assertIn(frozen_wire, motion_messages(arena, arena.players[1]))
        self.assertIn(live_wire, motion_messages(arena, arena.players[2]))

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
