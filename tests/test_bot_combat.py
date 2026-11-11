"""Bounded combat inputs on a real navigation grid, without a live server."""
import math
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from solbreaker.bot_ai import BotDirector, TrainingBot
from solbreaker.bot_navigation import Navigation
from solbreaker.collision import Pose


def vehicle(entity, team, position):
    return SimpleNamespace(entity_id=entity,team=team,pose=Pose(position,0.),
        speed=0.,flags=0,alive=True,account_id=None,reload_at=20.,burst_pending=False,
        tracks={},gun=SimpleNamespace(yaw_limits=(-math.pi,math.pi),set_point=Mock()))


class CombatManeuverTests(unittest.TestCase):
    def scene(self, entity=200, distance=24.):
        ai=TrainingBot(entity)
        bot=vehicle(entity,1,(.5,0.,.5))
        enemy=vehicle(300,2,(.5,0.,.5+distance))
        world=SimpleNamespace(motion={},destroyed=set(),
            _static_rays=Mock(side_effect=AssertionError('Combat jink cast a world ray')))
        arena=SimpleNamespace(vehicles={entity:bot,300:enemy},training_bots={entity:ai},
            world=world,players={},step_count=0,recon=None,
            _training_bot_braking_acceleration=lambda _:12.,
            _training_bot_closing_speed=lambda *_:0.)
        director=BotDirector(arena)
        director.navigation=Navigation((-20.,-20.,20.,180.),np.zeros((200,40)),
            np.zeros((200,40),dtype=bool),cell=1.,radius=1.8)
        director.navigation.footprints={entity:(.8,1.6),300:(.8,1.6)}
        ai.known={300:(enemy.pose.position,0.)};ai.visible={300}
        ai.next_cover_check=math.inf
        return director,bot,ai,enemy

    def move(self, director, bot, ai, enemy, now, eligible=True):
        return director._maneuver(bot,ai,enemy.pose.position,enemy.entity_id,eligible,now,12.)

    def start(self, director, bot, ai, enemy):
        self.assertIsNone(self.move(director,bot,ai,enemy,0.))
        flags=self.move(director,bot,ai,enemy,1.4)
        self.assertTrue(flags & (ai.FORWARD|ai.REVERSE))
        return flags

    def test_short_forward_reverse_inputs_at_close_and_distant_ranges(self):
        for entity in (200,201):
            for distance in (24.,80.,150.):
                with self.subTest(entity=entity,distance=distance):
                    director,bot,ai,enemy=self.scene(entity,distance)
                    pose=bot.pose
                    with patch.object(director.navigation,'request',side_effect=AssertionError('A*')):
                        flags=self.start(director,bot,ai,enemy)
                    self.assertIs(bot.pose,pose)
                    self.assertEqual(flags & (ai.FORWARD|ai.REVERSE),
                                     ai.FORWARD if entity%2 else ai.REVERSE)
                    self.assertGreaterEqual(math.dist(pose.position,ai.maneuver_goal),2.4-1e-9)
                    self.assertLessEqual(math.dist(pose.position,ai.maneuver_goal),3.6+1e-9)
                    self.assertLessEqual(abs(ai.maneuver_heading-pose.yaw),.18)
                    # The drivetrain reverses steering when backing.
                    physical_turn=(bool(flags&ai.RIGHT)-bool(flags&ai.LEFT))*ai.maneuver_direction
                    self.assertGreater(physical_turn*(ai.maneuver_heading-pose.yaw),0.)
                    self.assertEqual(director.stats['combat_moves'],1)

    def test_arrival_holds_aim_then_changes_translation_without_fast_oscillation(self):
        director,bot,ai,enemy=self.scene()
        self.start(director,bot,ai,enemy)
        first_direction=ai.maneuver_direction
        bot.pose=Pose(ai.maneuver_goal,ai.maneuver_heading)
        self.assertEqual(self.move(director,bot,ai,enemy,1.5),0)
        self.assertIsNone(ai.maneuver_goal)
        for now in (1.6,2.,2.7):
            self.assertEqual(self.move(director,bot,ai,enemy,now),0)
        self.assertIsNone(self.move(director,bot,ai,enemy,3.2))
        flags=self.move(director,bot,ai,enemy,4.4)
        self.assertTrue(flags & ai.FORWARD)
        self.assertEqual(ai.maneuver_direction,-first_direction)
        self.assertEqual(director.stats['combat_moves'],2)

    def test_braking_uses_real_speed_and_releases_before_destination(self):
        director,bot,ai,enemy=self.scene()
        self.start(director,bot,ai,enemy)
        heading=ai.maneuver_heading;direction=ai.maneuver_direction
        gx,gy,gz=ai.maneuver_goal
        bot.pose=Pose((gx-math.sin(heading)*direction,gy,gz-math.cos(heading)*direction),heading)
        bot.speed=2.*direction
        self.assertEqual(self.move(director,bot,ai,enemy,1.5),0)
        self.assertIsNone(ai.maneuver_goal)

    def test_reload_completion_and_burst_never_postpone_the_gun(self):
        for burst in (False,True):
            with self.subTest(burst=burst):
                director,bot,ai,enemy=self.scene()
                self.start(director,bot,ai,enemy)
                sighted=(enemy.entity_id,.1);aim=(enemy.entity_id,20.,(.1,0.,.1))
                ai.sighted=sighted;ai.aim=aim;ai.next_shot_check=1.6
                bot.reload_at=20. if burst else 2.
                bot.burst_pending=burst
                self.assertEqual(self.move(director,bot,ai,enemy,1.5),0)
                self.assertIs(ai.sighted,sighted);self.assertIs(ai.aim,aim)
                self.assertEqual(ai.next_shot_check,1.6)
                bot.reload_at=0.
                for now in (3.,6.,10.):
                    self.assertIsNone(self.move(director,bot,ai,enemy,now))
                self.assertEqual(director.stats['combat_moves'],1)

    def test_short_reload_and_fast_approach_do_not_start_a_jink(self):
        for reload_at,speed in ((0.,0.),(2.,0.),(20.,4.)):
            with self.subTest(reload_at=reload_at,speed=speed):
                director,bot,ai,enemy=self.scene()
                bot.reload_at=reload_at;bot.speed=speed
                self.move(director,bot,ai,enemy,0.)
                self.assertIsNone(self.move(director,bot,ai,enemy,1.4))
                self.assertEqual(director.stats['combat_moves'],0)

    def test_static_grid_climb_masks_and_learned_cells_all_reject_unsafe_moves(self):
        for barrier in ('static','climb','learned'):
            with self.subTest(barrier=barrier):
                director,bot,ai,enemy=self.scene()
                nav=director.navigation
                if barrier=='static':
                    nav.walkable[:]=False
                    nav.walkable.ravel()[nav.node(bot.pose.position)]=True
                elif barrier=='climb':
                    nav.actor_climb_edges[bot.entity_id]=np.zeros(nav.nx*nav.nz,dtype=np.uint8)
                else:
                    ai.route_bad_cells[nav.node(bot.pose.position)]=10.
                self.move(director,bot,ai,enemy,0.)
                self.assertIsNone(self.move(director,bot,ai,enemy,1.4))
                self.assertEqual(director.stats['combat_moves'],0)
                self.assertEqual(director.stats['combat_blocked'],1)
                checks=director.stats['combat_corridor_checks']
                self.assertLessEqual(checks,2)
                self.move(director,bot,ai,enemy,1.5)
                self.assertEqual(director.stats['combat_corridor_checks'],checks)

    def test_known_hulls_reject_reverse_without_reading_hidden_opponents(self):
        for knowledge in ('ally','visible','wreck','hidden'):
            with self.subTest(knowledge=knowledge):
                director,bot,ai,enemy=self.scene()
                obstacle=vehicle(400,1 if knowledge=='ally' else 2,(.5,0.,-4.5))
                director.arena.vehicles[400]=obstacle
                if knowledge=='visible':ai.visible.add(400)
                if knowledge=='wreck':
                    obstacle.alive=False;ai.seen_obstacles.add(400)
                flags=self.start(director,bot,ai,enemy)
                self.assertEqual(flags & (ai.FORWARD|ai.REVERSE),
                                 ai.REVERSE if knowledge=='hidden' else ai.FORWARD)

    def test_new_ally_in_cached_corridor_stops_on_the_next_decision(self):
        director,bot,ai,enemy=self.scene()
        self.start(director,bot,ai,enemy)
        checks=director.stats['combat_corridor_checks']
        director.arena.vehicles[400]=vehicle(400,1,(.5,0.,-4.5))
        self.assertEqual(self.move(director,bot,ai,enemy,1.41),0)
        self.assertEqual(director.stats['combat_corridor_checks'],checks)
        self.assertEqual(director.stats['combat_blocked'],1)

    def test_static_clearance_is_cached_and_invalidated_by_navigation_generation(self):
        director,bot,ai,enemy=self.scene()
        self.start(director,bot,ai,enemy)
        for now in (1.45,1.5,1.6,1.7):
            self.assertTrue(self.move(director,bot,ai,enemy,now)&ai.REVERSE)
        self.assertEqual(director.stats['combat_corridor_checks'],1)
        self.move(director,bot,ai,enemy,1.8)
        self.assertEqual(director.stats['combat_corridor_checks'],2)
        director.navigation.generation+=1
        self.move(director,bot,ai,enemy,1.81)
        self.assertEqual(director.stats['combat_corridor_checks'],3)
        self.assertEqual(director.navigation.stats['requests'],0)
        director.arena.world._static_rays.assert_not_called()

    def test_lost_or_changed_threat_resets_the_entire_impulse(self):
        director,bot,ai,enemy=self.scene()
        self.start(director,bot,ai,enemy)
        self.assertIsNone(self.move(director,bot,ai,enemy,1.5,eligible=False))
        self.assertIsNone(ai.maneuver_goal);self.assertIsNone(ai.maneuver_target)
        self.assertEqual(ai.maneuver_stable_until,0.)
        self.assertIsNone(self.move(director,bot,ai,enemy,1.6))
        replacement=vehicle(301,2,(.5,0.,30.))
        self.assertIsNone(self.move(director,bot,ai,replacement,2.5))
        self.assertEqual(ai.maneuver_target,301)
        self.assertEqual(director.stats['combat_moves'],1)

    def test_close_contact_far_range_and_broadside_do_not_start(self):
        for distance,yaw in ((8.,0.),(200.,0.),(24.,math.pi/2)):
            with self.subTest(distance=distance,yaw=yaw):
                director,bot,ai,enemy=self.scene(distance=distance)
                bot.pose=Pose(bot.pose.position,yaw)
                self.assertIsNone(self.move(director,bot,ai,enemy,0.))
                self.assertIsNone(self.move(director,bot,ai,enemy,2.))
                self.assertEqual(director.stats['combat_corridor_checks'],0)

    def test_drive_integrates_local_input_without_global_route_requests(self):
        director,bot,ai,enemy=self.scene(distance=80.)
        ai.order_kind='combat';ai.maneuver_target=enemy.entity_id;ai.next_maneuver=.5
        with patch.object(director.navigation,'request',side_effect=AssertionError('A*')):
            director._drive(bot,ai,1.4)
        self.assertTrue(bot.flags&ai.REVERSE)
        self.assertEqual(director.stats['combat_moves'],1)
        self.assertIsNone(ai.check_at)
        self.assertEqual(ai.route_watch_pause_at,1.4)

    def test_reload_cover_backs_out_without_routing_the_nose_toward_cover(self):
        director,bot,ai,enemy=self.scene(distance=20.)
        ai.next_cover_check=0.
        bot.reload_at=4.
        director.arena.world._static_rays.side_effect=lambda starts,ends:[(1,None,.5)]*len(starts)
        with patch.object(director.navigation,'request',side_effect=AssertionError('Cover requested A*')):
            director._drive(bot,ai,0.)
            self.assertEqual(bot.flags,ai.REVERSE)
            self.assertLess(ai.cover_goal[2],bot.pose.position[2])
            self.assertEqual(director.stats['cover_moves'],1)
            # Feed the next observed pose; physical motion is exercised by
            # the real-arena acceptance scenario, not synthesized here.
            bot.pose=Pose((.5,0.,-1.5),0.)
            bot.speed=-1.
            director._drive(bot,ai,2.2)
            self.assertEqual(bot.flags,ai.REVERSE)
            bot.speed=0.
            director._drive(bot,ai,4.1)
            self.assertIsNone(ai.cover_goal)
            self.assertEqual(bot.flags,0)
            self.assertEqual(bot.reload_at,4.)

    def test_lateral_cover_does_not_request_a_broadside_turn(self):
        director,bot,ai,enemy=self.scene(distance=20.)
        ai.next_cover_check=0.
        # Only the old lateral 12-m endpoints are hidden by this obstacle.
        director.arena.world._static_rays.side_effect=lambda starts,ends:[
            (1,None,.5) if abs(p[0]-.5)>5. else (-1,None,1.) for p in starts]
        with patch.object(director.navigation,'request',side_effect=AssertionError('Lateral cover route')):
            director._drive(bot,ai,0.)
        self.assertIsNone(ai.cover_goal)
        self.assertEqual(bot.flags,0)

    def test_loaded_close_combat_restores_front_with_hysteresis(self):
        for yaw in (math.pi/2,-math.pi/2,math.pi):
            with self.subTest(yaw=yaw):
                director,bot,ai,enemy=self.scene(distance=20.)
                ai.target_id=enemy.entity_id;ai.order_kind='combat'
                ai.cover_goal=(12.,0.,.5);ai.cover_until=1.
                bot.reload_at=1.
                bot.pose=Pose(bot.pose.position,yaw)
                director._drive(bot,ai,1.1)
                self.assertIsNone(ai.cover_goal)
                director._drive(bot,ai,1.2)
                self.assertTrue(bot.flags&(ai.LEFT|ai.RIGHT))
                self.assertFalse(bot.flags&(ai.FORWARD|ai.REVERSE))
                # An in-progress correction continues below its entry angle.
                bot.pose=Pose(bot.pose.position,math.radians(30.))
                director._drive(bot,ai,1.3)
                self.assertEqual(bot.flags,ai.LEFT)
                bot.pose=Pose(bot.pose.position,math.radians(18.))
                director._drive(bot,ai,1.4)
                self.assertEqual(bot.flags,0)
                bot.pose=Pose(bot.pose.position,math.radians(30.))
                director._drive(bot,ai,1.5)
                self.assertEqual(bot.flags,0)

    def test_rear_cover_checks_dynamic_hulls_and_changed_navigation(self):
        for obstacle in ('ally','terrain'):
            with self.subTest(obstacle=obstacle):
                director,bot,ai,enemy=self.scene(distance=20.)
                ai.next_cover_check=0.
                director.arena.world._static_rays.side_effect=lambda starts,ends:[(1,None,.5)]*len(starts)
                director._drive(bot,ai,0.)
                self.assertTrue(bot.flags&ai.REVERSE)
                if obstacle=='ally':
                    director.arena.vehicles[400]=vehicle(400,1,(.5,0.,-4.5))
                else:
                    director.navigation.walkable[:]=False
                    director.navigation.generation+=1
                director._drive(bot,ai,.1)
                self.assertEqual(bot.flags,0)
                self.assertIsNone(ai.cover_goal)

    def test_cover_stop_keeps_a_settling_pause_before_resuming_a_distant_route(self):
        for reason in ('expired','blocked'):
            with self.subTest(reason=reason):
                director,bot,ai,enemy=self.scene(distance=80.)
                ai.next_cover_check=0.
                director.arena.world._static_rays.side_effect=lambda starts,ends:[(1,None,.5)]*len(starts)
                director._drive(bot,ai,0.)
                if reason=='expired':ai.cover_until=.1
                else:director.arena.vehicles[400]=vehicle(400,1,(.5,0.,-4.5))
                with patch.object(director.navigation,'request',side_effect=AssertionError('No settling pause')):
                    for now in (.1,.2,.4,.8,1.2):
                        director._drive(bot,ai,now)
                        self.assertEqual(bot.flags,0)
                self.assertEqual(ai.maneuver_target,enemy.entity_id)

    def test_repairs_traffic_recovery_and_airborne_keep_priority(self):
        for gate in ('repair','traffic','recovery','airborne'):
            with self.subTest(gate=gate):
                director,bot,ai,enemy=self.scene()
                self.start(director,bot,ai,enemy)
                if gate=='repair':
                    bot.tracks={'left':SimpleNamespace(broken=True)}
                    director._drive(bot,ai,1.5)
                    self.assertEqual(bot.flags,0)
                elif gate=='traffic':
                    director.traffic.by_entity[bot.entity_id]=(200,400)
                    with patch.object(director.traffic,'controls',return_value=0):
                        director._drive(bot,ai,1.5)
                    self.assertEqual(bot.flags,0)
                elif gate=='recovery':
                    ai.reverse_until=3.;ai.recovery_turn=ai.LEFT
                    director._drive(bot,ai,1.5)
                    self.assertEqual(bot.flags,ai.REVERSE|ai.LEFT)
                else:
                    director.arena.world.motion[bot.entity_id]=SimpleNamespace(grounded=False)
                    with patch.object(director,'_perceive'):
                        director.step(1.5)
                    self.assertEqual(bot.flags,0)
                self.assertIsNone(ai.maneuver_goal)
                self.assertIsNone(ai.maneuver_target)
                self.assertEqual(director.stats['combat_moves'],1)


if __name__=='__main__':
    unittest.main()
