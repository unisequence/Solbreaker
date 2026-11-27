"""Exercise airborne contact episodes through the authoritative arena tick."""
import math
import unittest

from solbreaker.arena import Arena
from solbreaker.collision import CollisionWorld, Pose
from tests.test_native_physics import QueryAssets, terrain


class UnifiedDynamicsTests(unittest.TestCase):
    def test_head_on_airborne_rams_rearm_only_after_separation(self):
        assets = QueryAssets('flat')
        self.addCleanup(assets.mesh_files.close)
        ground = terrain()
        arena = Arena([dict(id=1, nickname='North'), dict(id=2, nickname='South')],
                      b'', ground, now=1., solo=False)
        self.addCleanup(arena.close)
        for account in (1, 2):
            arena.ready(account, 1.)
        vehicles = [arena.vehicle_for(account) for account in (1, 2)]
        world = arena.world = CollisionWorld(assets, ground,
            vehicles={v.entity_id: v.pose for v in vehicles},
            experimental_6dof=True, experimental_ground_impacts=True, sprung_tracks=True)
        for vehicle, z, yaw in zip(vehicles, (5., -5.), (math.pi, 0.)):
            vehicle.health = vehicle.max_health = 5000
            vehicle.pose = Pose((0., 24., z), yaw)
            world.relocate_training_body(vehicle.entity_id, vehicle.pose)

        tick = 0
        hits = []

        def fly(speed, frames):
            nonlocal tick
            for vehicle, direction in zip(vehicles, (-1., 1.)):
                self.assertTrue(world.set_flight_velocity(
                    vehicle.entity_id, (0., 0., direction * speed), (0., 0., 0.)))
            for _ in range(frames):
                tick += 1
                arena._fixed_step(2. + tick * arena.STEP)
                hits.extend(event.data for event in arena.drain_events()
                            if event.kind == 'ground_impact' and event.data['ram'])
                self.assertTrue(all(not world.motion[v.entity_id].grounded for v in vehicles))

        fly(16., 23)
        self.assertEqual(len(hits), 1)
        fly(-6., 25)
        self.assertEqual(len(hits), 1)
        self.assertFalse(world.ground_contact_pairs)
        self.assertGreater(vehicles[0].pose.position[2] - vehicles[1].pose.position[2], 8.)
        fly(16., 52)
        self.assertEqual(len(hits), 2)
        for hit in hits:
            self.assertTrue(hit['airborne'])
            self.assertGreater(hit['ram_damage_a'], 0)
            self.assertGreater(hit['ram_damage_b'], 0)
        for vehicle, key in zip(vehicles, ('ram_damage_a', 'ram_damage_b')):
            self.assertEqual(vehicle.max_health - vehicle.health, sum(hit[key] for hit in hits))
