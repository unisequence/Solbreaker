"""Guided projectile flight and the pinned client's ATGM messages."""
import math
from random import Random
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from solbreaker.arena import Arena
from solbreaker.arena_commands import apply_input, decode_input
from solbreaker.ballistics import Projectile, advance
from solbreaker.battle import avatar_method, event_messages, missile_message, shooting_message
from solbreaker.collision import Pose
from solbreaker.entities import BattleEvent
from solbreaker.vehicle_data import MissileFlight, Shell, _missile_flight, _shell_effect


class MissileTests(unittest.TestCase):
    def shell(self, *, distance=720., speed=85., max_speed=85., acceleration=0.):
        return Shell(57370,152.,560.,(185.,185.),None,math.radians(85),
                     speed,0.,distance,kind='HOLLOW_CHARGE',effect_index=17,
                     missile=MissileFlight(max_speed,acceleration,.6,0.))

    def projectile(self, shell):
        return Projectile(1,200,shell,(0.,0.,0.),(0.,0.,shell.speed),0.)

    def test_xml_marks_guidance_without_treating_it_as_a_damage_kind(self):
        shot = ET.fromstring('''<shot><isATGM>true</isATGM><maxSpeed>85</maxSpeed>
            <acceleration>0</acceleration><turnSpeed>0.6</turnSpeed>
            <rotationalDeceleration>0</rotationalDeceleration></shot>''')
        flight = _missile_flight(shot)
        self.assertEqual(flight, MissileFlight(85.,0.,.6,0.))
        with patch('solbreaker.vehicle_data._shot_effects',
                   return_value=(('normal','shell'),('rocket','atgm'),('torpedo','torpedo'))):
            self.assertEqual(_shell_effect(None,'normal'),0)
            self.assertEqual(_shell_effect(None,'rocket',flight),1)
            with self.assertRaises(ValueError):
                _shell_effect(None,'rocket')
            with self.assertRaises(ValueError):
                _shell_effect(None,'torpedo',flight)

    def test_reticle_steers_in_flight_with_a_turn_limit_and_no_uturn(self):
        rocket = self.projectile(self.shell())
        self.assertIsNone(advance(rocket,None,{},{},.25,Random(0),missile_target=(100.,0.,200.)))
        angle = math.atan2(rocket.velocity[0],rocket.velocity[2])
        self.assertAlmostEqual(angle,.6*2.4*.25)
        self.assertAlmostEqual(math.hypot(*rocket.velocity),85.)
        previous = rocket.velocity
        advance(rocket,None,{},{},.25,Random(0),missile_target=(-200.,0.,-200.))
        for first,second in zip(previous,rocket.velocity):
            self.assertAlmostEqual(first,second)

    def test_accelerating_missile_is_capped_and_stops_at_range(self):
        rocket = self.projectile(self.shell(distance=300.,speed=45.,max_speed=100.,acceleration=45.))
        self.assertIsNone(advance(rocket,None,{},{},.5,Random(0)))
        self.assertAlmostEqual(rocket.velocity[2],67.5)
        self.assertIsNone(advance(rocket,None,{},{},1.,Random(0)))
        self.assertAlmostEqual(rocket.velocity[2],100.)
        impact = advance(rocket,None,{},{},3.,Random(0))
        self.assertAlmostEqual(impact.point[2],300.)
        self.assertFalse(impact.collision)

    def test_world_cover_stops_a_rocket_before_it_can_curve_around_it(self):
        def static_segment(start, end):
            if end[2] >= 10.:
                fraction = (10.-start[2])/(end[2]-start[2])
                point = tuple(a+(b-a)*fraction for a,b in zip(start,end))
                return 9,fraction,point
            return None,1.,end

        world = SimpleNamespace(static_segment=static_segment,breakable_bodies={})
        rocket = self.projectile(self.shell())
        impact = advance(rocket,world,{},{},.5,Random(0),missile_target=(20.,0.,60.))
        self.assertTrue(impact.collision)
        self.assertAlmostEqual(impact.point[2],10.)
        self.assertLess(impact.point[0],1.)

    def test_arena_guides_from_manual_aim_and_loses_guidance_with_the_shooter(self):
        terrain = SimpleNamespace(height=lambda x,z: 0.)
        arena = Arena([dict(id=1,nickname='Shooter')],b'',terrain,now=1.,shell=self.shell())
        self.addCleanup(arena.close)
        vehicle = arena.vehicle_for(1)
        arena.ready(1,1.)
        arena.drain_events()
        arena.fire(1,1.)
        rocket = next(iter(arena.projectiles.values()))
        px,py,pz = vehicle.pose.position
        arena.aim_point(1,(30.,2.,150.),local=True)
        self.assertEqual(arena._missile_target(rocket),(px+30.,py+2.,pz+150.))
        arena.step(1.2)
        self.assertGreater(rocket.velocity[0],0.)
        events = arena.drain_events()
        self.assertTrue(events[0].data['guided'])
        self.assertEqual([event.kind for event in events].count('missile_move'),2)
        arena.players[1].attached = False
        self.assertIsNone(arena._missile_target(rocket))
        arena.finish(now=1.2)
        ends = [event for event in arena.drain_events() if event.kind=='shot_end']
        self.assertEqual(len(ends),1)
        self.assertTrue(ends[0].data['guided'])

    def test_rocket_follows_a_moving_autoaim_target_after_launch(self):
        terrain = SimpleNamespace(height=lambda x,z: 0.)
        arena = Arena([dict(id=1,nickname='Shooter')],b'',terrain,now=1.,shell=self.shell())
        self.addCleanup(arena.close)
        shooter = arena.vehicle_for(1)
        target = next(vehicle for vehicle in arena.vehicles.values() if vehicle.team==2)
        x,y,z = shooter.pose.position
        target.pose = Pose((x+20.,y,z+85.),0.)
        arena.ready(1,1.)
        arena.fire(1,1.)
        rocket = next(iter(arena.projectiles.values()))
        launch_velocity = rocket.velocity

        lock = decode_input(1,13,struct.pack('<II',shooter.entity_id,target.entity_id))
        apply_input(arena,lock)
        arena.step(1.1)
        self.assertEqual(shooter.autoaim_target,target.entity_id)
        self.assertGreater(rocket.velocity[0],launch_velocity[0])
        first_point = arena._missile_target(rocket)
        target.pose = Pose((x+23.,y,z+87.),0.)
        arena.step(1.2)
        moved_point = arena._missile_target(rocket)
        self.assertEqual((moved_point[0]-first_point[0],moved_point[2]-first_point[2]),(3.,2.))
        target.health = 0
        arena.step(1.3)
        self.assertEqual(shooter.autoaim_target,0)

    def test_manual_guidance_still_works_when_cover_is_between_shooter_and_rocket(self):
        terrain = SimpleNamespace(height=lambda x,z: 0.)
        arena = Arena([dict(id=1,nickname='Shooter')],b'',terrain,now=1.,shell=self.shell())
        self.addCleanup(arena.close)
        shooter = arena.vehicle_for(1)
        origin,_ = arena.shot_ray(shooter)
        x,y,z = origin
        rocket = self.projectile(self.shell())
        rocket.position = (x-12.,y,z+40.)

        def static_segment(start, end):
            wall_z = z+20.
            if start[2] < wall_z <= end[2]:
                fraction = (wall_z-start[2])/(end[2]-start[2])
                point = tuple(a+(b-a)*fraction for a,b in zip(start,end))
                if abs(point[0]-x) <= 8.:
                    return 9,fraction,point
            return None,1.,end

        world = SimpleNamespace(static_segment=static_segment,breakable_bodies={})
        self.assertEqual(world.static_segment(origin,rocket.position)[0],9)
        arena.aim_point(1,(x+6.,y,z+20.))
        point = arena._missile_target(rocket)
        self.assertEqual(point,(x-6.,y,z+60.))
        self.assertIsNone(advance(rocket,world,{},{},.1,Random(0),missile_target=point))
        self.assertGreater(rocket.velocity[0],0.)

    def test_launch_update_and_stop_use_atgm_rpc_framing(self):
        player = SimpleNamespace(vehicle_id=200,team=1)
        arena = SimpleNamespace(vehicles={})
        start = (2.,3.,4.)
        velocity = (0.,0.,85.)
        data = dict(shot=1,effect_index=17,origin=start,velocity=velocity)
        events = (
            BattleEvent('shot',200,0,dict(data,guided=True)),
            BattleEvent('missile_move',200,0,data),
            BattleEvent('shot_end',200,0,dict(shot=1,point=start,guided=True)),
        )
        move = missile_message(200,1,17,start,velocity)
        self.assertEqual(len(move),55)
        self.assertEqual(struct.unpack('<II10fBf',move[2:]),
                         (200,1,*start,0.,0.,0.,*velocity,0.,17,0.))
        self.assertEqual(event_messages(arena,player,events),
                         shooting_message(200)+move+move+avatar_method(23,struct.pack('<I3fB',1,*start,0)))
