"""Deterministic training AI; Arena retains physics, guns and damage.

Decisions run at 15 Hz, staggered across four physics ticks. Navigation is
shared, bounded work. Enemy positions require geometric sight or an active
team recon mark; lost targets leave a finite last-seen position.
"""
from dataclasses import dataclass
import math
import os
import random

from .aiming import wrap
from .orientation import matrix
from .bot_traffic import TrafficController
from .bot_orders import BotOrders


@dataclass(frozen=True)
class BotProfile:
    stop_distance: float
    shot_check_seconds: float
    # Continuous sight of the same target before the first shot after
    # acquiring it, sampled at the shot-check cadence.
    reaction_seconds: float
    # Radius of the bounded aim-point offset around the target centre, metres.
    aim_error: float
    aim_resample_seconds: float


# "normal" keeps the original single-bot stop and query cadence.
DIFFICULTIES = {
    'easy': BotProfile(32., .4, 1.2, 1.6, 1.4),
    'normal': BotProfile(24., .25, .6, .7, 1.),
    'hard': BotProfile(24., .15, .3, .25, .7),
}
DEFAULT_DIFFICULTY = 'normal'


def training_bot_requested(manifest, environ=None):
    """Resolve the optional local solo-bot setting without modifying the lab."""
    value = manifest.get('training_bot', False)
    if type(value) is not bool:
        raise ValueError('Invalid training bot setting')
    env = os.environ if environ is None else environ
    override = env.get('SOLBREAKER_TRAINING_BOT')
    if override is not None:
        if override not in ('0', '1'):
            raise ValueError('SOLBREAKER_TRAINING_BOT must be 0 or 1')
        return override == '1'
    return value


class TrainingBot:
    FORWARD, REVERSE, LEFT, RIGHT = 1, 2, 4, 8
    STOP_DISTANCE = 24.
    TURN_DEADZONE = .18
    FORWARD_ANGLE = .85
    STUCK_CHECK_SECONDS = 2.
    STUCK_DISTANCE = .5
    STUCK_YAW_PROGRESS = .06
    REVERSE_SECONDS = 1.5
    SHOT_CHECK_SECONDS = .25
    # Leave a margin for slope, tick cadence and model mismatch; this is bot
    # planning only and does not change the vehicle's actual braking force.
    BRAKING_SAFETY_FACTOR = .65
    BRAKING_REACTION_SECONDS = .25
    BRAKING_RELEASE_SPEED = .4
    AIM_HEIGHT = 1.
    # Keep the current target unless another is clearly closer.
    TARGET_SWITCH_RATIO = .75
    TRAVEL_LOOK_ANGLE = math.radians(12.)
    TRAVEL_LOOK_RATE = math.radians(18.)
    TRAVEL_LOOK_ENTER_SECONDS = .45
    TRAVEL_LOOK_RELEASE_SECONDS = .65

    def __init__(self, entity_id, difficulty=DEFAULT_DIFFICULTY):
        if type(entity_id) is not int or entity_id <= 0:
            raise ValueError('Invalid bot entity')
        if type(difficulty) is not str or difficulty not in DIFFICULTIES:
            raise ValueError('Invalid bot difficulty')
        self.entity_id = entity_id
        self.difficulty = difficulty
        self.profile = DIFFICULTIES[difficulty]
        self.STOP_DISTANCE = self.profile.stop_distance
        self.SHOT_CHECK_SECONDS = self.profile.shot_check_seconds
        # Per-bot deterministic stream: reproducible tests and replays.
        self.random = random.Random(entity_id)
        self.target_id = None
        self.sighted = None
        self.aim = None
        self.check_at = None
        self.check_position = None
        self.check_yaw = None
        self.check_error = self.check_heading = None
        self.reverse_until = 0.
        self.next_shot_check = 0.
        self.braking = False
        self.known = {}
        self.visible = set()
        self.visible_points = {}
        self.seen_obstacles = set()
        self.patrol_index = 0
        self.next_plan = 0.
        self.cover_until = 0.
        self.cover_goal = None
        self.next_cover_check = 0.
        self.cover_check_at = 0.
        self.cover_generation = None
        self.combat_align = False
        self.passing = None
        self.passing_until = 0.
        self.next_pass_check = 0.
        self.recoveries = 0
        self.recovery_replan = False
        self.recovery_turn = 0
        self.next_maneuver = 0.
        self.maneuver_goal = None
        self.maneuver_until = 0.
        self.maneuver_target = None
        self.maneuver_origin = None
        self.maneuver_heading = 0.
        self.maneuver_direction = 1
        self.maneuver_step = 0
        self.maneuver_check_at = 0.
        self.maneuver_generation = None
        self.maneuver_stable_until = 0.
        self.travel_look = 0.
        self.travel_look_at = None
        self.travel_look_active = False
        self.order_kind = 'patrol'
        self.route_kind = None
        self.order_goal = None
        self.route_bad_cells = {}
        self.route_bad_owners = {}
        self.route_watch_goal = None
        self.route_watch_at = None
        self.route_watch_pause_at = None
        self.route_watch_best = math.inf
        self.route_watch_origin = None
        self.route_watch_direction = None
        self.route_watch_obstacle = None
        self.route_watch_entity = None
        self.route_exit = None
        self.learned_corridors = 0
        self.margin_goal = None
        self.margin_until = self.next_margin_check = 0.

    def controls(self, pose, target_position, now, *, closing_speed=0., braking_acceleration=12.,
                 stop_distance=None, remaining_distance=None, align_at_stop=False):
        """Choose drive flags for the next fixed step, without moving the body."""
        x, _, z = pose.position
        tx, _, tz = target_position
        distance = math.hypot(tx-x, tz-z) if remaining_distance is None else remaining_distance
        stop = self.STOP_DISTANCE if stop_distance is None else stop_distance
        heading = math.atan2(tx-x, tz-z)
        error = wrap(heading-pose.yaw)
        if now < self.reverse_until:
            return self.REVERSE|self.recovery_turn
        if (not math.isfinite(closing_speed) or not math.isfinite(braking_acceleration)
                or braking_acceleration <= 0):
            raise ValueError('Invalid bot braking input')
        closing = max(0., closing_speed)
        deceleration = max(.5, braking_acceleration*self.BRAKING_SAFETY_FACTOR)
        braking_distance = closing*closing/(2*deceleration) + closing*self.BRAKING_REACTION_SECONDS
        if distance <= stop + braking_distance:
            self.braking = True
        elif self.braking and closing <= self.BRAKING_RELEASE_SPEED:
            self.braking = False
        stopped = self.braking or distance <= stop
        if stopped and not align_at_stop:
            # A full turret tracks the enemy independently. Repeated hull
            # commands while holding a firing position only rock the chassis
            # and reset the stuck detector, without making route progress.
            self.check_at = self.check_position = self.check_yaw = None
            return 0
        deadzone = .04 if stopped else self.TURN_DEADZONE
        turn = self.RIGHT if error > deadzone else self.LEFT if error < -deadzone else 0
        flags = turn | (self.FORWARD if not stopped and abs(error) < self.FORWARD_ANGLE else 0)
        if flags & (self.FORWARD|self.LEFT|self.RIGHT):
            if self.check_at is None or abs(wrap(heading-self.check_heading)) > .6:
                self.check_at, self.check_position = now, (x, z)
                self.check_yaw = pose.yaw
                self.check_error, self.check_heading = abs(error), heading
            elif now-self.check_at >= self.STUCK_CHECK_SECONDS:
                # Only useful alignment buys another turn interval. A blocked
                # hull oscillating between two yaw values is not advancing.
                stuck = (math.dist(self.check_position, (x, z)) < self.STUCK_DISTANCE
                         and self.check_error-abs(error) < self.STUCK_YAW_PROGRESS)
                self.check_at, self.check_position = now, (x, z)
                self.check_yaw = pose.yaw
                self.check_error, self.check_heading = abs(error), heading
                if stuck:
                    self.reverse_until = now + self.REVERSE_SECONDS
                    self.check_at = self.check_position = self.check_yaw = None
                    self.passing = None
                    self.recoveries += 1
                    self.recovery_replan = True
                    # Arena reverses steering while backing. Keep the input
                    # side of a blocked turn to rotate the nose away from it.
                    self.recovery_turn = turn
                    return self.REVERSE|self.recovery_turn
        else:
            self.check_at = self.check_position = self.check_yaw = None
        return flags

    def shot_check_due(self, now):
        """Limit expensive collision queries; this does not bypass gun reload."""
        if now < self.next_shot_check:
            return False
        self.next_shot_check = now + self.SHOT_CHECK_SECONDS
        return True

    def choose_target(self, position, candidates):
        """Pick the nearest enemy, with hysteresis, from public positions.

        ``candidates`` are ``(entity_id, position)`` pairs for living enemy
        vehicles. Returns the chosen entity ID or ``None``.
        """
        x, _, z = position
        ranked = sorted((math.hypot(p[0]-x, p[2]-z), e) for e, p in candidates)
        if not ranked:
            self.target_id = None
            return None
        nearest, chosen = ranked[0]
        current = next((d for d, e in ranked if e == self.target_id), None)
        if current is not None and nearest >= current*self.TARGET_SWITCH_RATIO:
            chosen = self.target_id
        self.target_id = chosen
        return chosen

    def aim_point(self, target_id, target_position, now):
        """Return a bounded, slowly varying aim point; Arena still traverses
        the gun at its normal rate and validates the shot by collision."""
        if self.aim is None or self.aim[0] != target_id or now >= self.aim[1]:
            error = self.profile.aim_error
            radius = error*math.sqrt(self.random.random())
            angle = self.random.random()*math.tau
            offset = (radius*math.sin(angle), (self.random.random()-.5)*error,
                      radius*math.cos(angle))
            self.aim = (target_id, now+self.profile.aim_resample_seconds, offset)
        x, y, z = target_position
        dx, dy, dz = self.aim[2]
        return (x+dx, y+self.AIM_HEIGHT+dy, z+dz)

    def sight_confirmed(self, target_id, visible, now):
        """Require continuous sight of one target for the reaction time."""
        if not visible:
            self.sighted = None
            return False
        if self.sighted is None or self.sighted[0] != target_id:
            self.sighted = (target_id, now)
        return now-self.sighted[1] >= self.profile.reaction_seconds


class BotDirector:
    """One scheduler and perception batch per arena, not one world per bot."""
    DECISION_TICKS = 4
    MEMORY_SECONDS = 10.
    VIEW_DISTANCE = 350.

    def __init__(self, arena):
        self.arena = arena
        self.navigation = None
        self.traffic = TrafficController(self)
        self.orders = BotOrders(self)
        self.phases = {eid:i%self.DECISION_TICKS for i,eid in enumerate(arena.training_bots)}
        # Spawn areas are public map objectives, captured once. Patrol never
        # reads the current position of an unseen opponent to follow it.
        self.objectives = {team:tuple(v.pose.position for v in arena.vehicles.values()
                                     if v.team!=team) for team in (1,2)}
        # Match opposite spawn lanes by their lateral position. Orders are
        # assigned once from public spawn geometry, never unseen live poses.
        for team,goals in self.objectives.items():
            bots=[e for e in arena.training_bots if arena.vehicles[e].team==team]
            own=[v.pose.position for v in arena.vehicles.values() if v.team==team]
            if not bots or not goals or not own:continue
            ax=sum(p[0] for p in own)/len(own);az=sum(p[2] for p in own)/len(own)
            bx=sum(p[0] for p in goals)/len(goals);bz=sum(p[2] for p in goals)/len(goals)
            dx,dz=bx-ax,bz-az
            if math.hypot(dx,dz)<1e-6:dx,dz=0.,1.
            def lateral(p):return p[0]*dz-p[2]*dx
            goals=tuple(sorted(goals,key=lambda p:(lateral(p),p)))
            homes=[arena.vehicles[e].pose.position for e in bots]
            patrol=[]
            for goal in goals:
                home=min(homes,key=lambda p:(abs(lateral(p)-lateral(goal)),p))
                patrol.extend((goal,home))
            self.objectives[team]=tuple(patrol)
            bots.sort(key=lambda e:(lateral(arena.vehicles[e].pose.position),e))
            for rank,eid in enumerate(bots):
                lane=(round(rank*(len(goals)-1)/(len(bots)-1))
                      if len(bots)>1 else len(goals)//2)
                arena.training_bots[eid].patrol_index=2*lane
        self.stats = dict(decisions=0,max_decisions_per_step=0,perception_rays=0,
                          avoidance=0,cover_moves=0,route_entries=0,combat_moves=0,
                          combat_forward_moves=0,combat_reverse_moves=0,
                          combat_drive_ticks=0,combat_hold_ticks=0,
                          combat_blocked=0,combat_corridor_checks=0)

    def patrol_goal(self, bot, ai):
        goals=self.objectives[bot.team]
        return goals[ai.patrol_index%len(goals)] if goals else None

    def _enemies(self, bot):
        a=self.arena
        return [v for v in a.vehicles.values() if v.alive and v.team!=bot.team
                and (v.account_id is None or a.players[v.account_id].attached)]

    def _visibility_points(self, target):
        world = self.arena.world
        if not hasattr(world, '_component_bounds'):
            return ()
        bounds = {name: (low, high) for name, low, high in world._component_bounds(target.entity_id)}
        if 'hull' not in bounds:
            return ()
        low, high = bounds['hull']
        center = (low + high) * .5
        height = low[1] + (high[1] - low[1]) * .65
        side = (high[0] - low[0]) * .35
        length = (high[2] - low[2]) * .35
        points = [(center[0] + x * side, height, center[2] + z * length)
                  for x, z in ((-1, 0), (1, 0), (0, -1), (0, 1),
                               (-1, -1), (-1, 1), (1, -1), (1, 1))]
        top = (center[0], high[1] - (high[1] - low[1]) * .2, center[2])
        assets = world.assets_for(target.entity_id) if hasattr(world, 'assets_for') else None
        turret = getattr(assets, 'turret_node', None)
        if turret in bounds:
            t_low, t_high = bounds[turret]
            point = (t_low + t_high) * .5
            point[1] = t_low[1] + (t_high[1] - t_low[1]) * .8
            top = (matrix(target.gun.yaw) @ point + assets.definition.aiming.turret_position)
        points.append(top)
        rotation = matrix(target.pose.yaw, target.pose.pitch, target.pose.roll)
        return tuple(tuple(float(a + b) for a, b in zip(target.pose.position, rotation @ point))
                     for point in points)

    def _perceive(self, due, now):
        a=self.arena;world=a.world;rows=[];starts=[];ends=[]
        rays = getattr(world, '_visibility_rays', None) or getattr(world, '_static_rays', None)
        for eid in due:
            bot=a.vehicles[eid];ai=a.training_bots[eid]
            ai.visible.clear()
            ai.visible_points.clear()
            ai.seen_obstacles.clear()
            enemies=self._enemies(bot);living={v.entity_id for v in enemies}
            ai.known={e:row for e,row in ai.known.items()
                      if e in living and now-row[1]<self.MEMORY_SECONDS}
            for target in enemies:
                if a.recon is not None and a.recon.team_visible(bot.team,target.entity_id,now):
                    ai.known[target.entity_id]=(target.pose.position,now)
                if math.dist(bot.pose.position,target.pose.position)>self.VIEW_DISTANCE:continue
                if world is None or not hasattr(world,'_static_rays'):
                    ai.visible.add(target.entity_id);ai.known[target.entity_id]=(target.pose.position,now)
                    continue
                start=bot.pose.position;end=target.pose.position
                starts.append((start[0],start[1]+1.4,start[2]))
                ends.append((end[0],end[1]+1.1,end[2]))
                rows.append((ai,target))
            # Wrecks remain physical obstacles, but do not become combat
            # targets or reveal their position through an occluding wall.
            if world is not None and hasattr(world,'_static_rays'):
                for target in a.vehicles.values():
                    if target.alive or target.team==bot.team or math.dist(bot.pose.position,target.pose.position)>50.:continue
                    start,end=bot.pose.position,target.pose.position
                    starts.append((start[0],start[1]+1.4,start[2]))
                    ends.append((end[0],end[1]+1.1,end[2]))
                    rows.append((ai,target))
        if rows:
            hits=rays(starts,ends)
            self.stats['perception_rays']+=len(rows)
            blocked=[]
            for (ai,target),hit in zip(rows,hits):
                if hit[0]<0 or hit[2]>=.999:
                    ai.seen_obstacles.add(target.entity_id)
                    if target.alive:
                        ai.visible.add(target.entity_id);ai.known[target.entity_id]=(target.pose.position,now)
                elif target.alive:
                    blocked.append((ai,target))
            # Keep the common clear-center path as one batch. Only occluded
            # living targets receive nine dimension-based fallback samples.
            starts,ends,samples=[],[],[]
            points={}
            for ai,target in blocked:
                observer=a.vehicles[ai.entity_id].pose.position
                if target.entity_id not in points:
                    points[target.entity_id]=self._visibility_points(target)
                for point in points[target.entity_id]:
                    starts.append((observer[0],observer[1]+1.4,observer[2]))
                    ends.append(point)
                    samples.append((ai,target,point))
            if samples:
                hits=rays(starts,ends)
                self.stats['perception_rays']+=len(samples)
                for (ai,target,point),hit in zip(samples,hits):
                    if target.entity_id not in ai.visible and (hit[0]<0 or hit[2]>=.999):
                        ai.visible.add(target.entity_id)
                        ai.seen_obstacles.add(target.entity_id)
                        ai.known[target.entity_id]=(target.pose.position,now)
                        ai.visible_points[target.entity_id]=point
        for eid in due:
            ai=a.training_bots[eid]
            if ai.target_id not in ai.visible:ai.sighted=None

    def _cover(self, bot, ai, threat, now):
        nav=self.navigation
        if nav is None or now<ai.next_cover_check:return
        ai.next_cover_check=now+2.
        if (bot.reload_at-now<3. or getattr(bot,'burst_pending',False)
                or ai.cover_goal is not None or now<ai.maneuver_stable_until):return
        x,y,z=bot.pose.position;tx,ty,tz=threat
        angle=math.atan2(tx-x,tz-z)
        if abs(wrap(angle-bot.pose.yaw))>math.radians(28.):return
        candidates=[]
        # Cover is a short reverse move, not a forward route to a point
        # beside/behind the enemy-facing hull. A distant hidden endpoint
        # alone never justifies exposing the flank while turning toward it.
        for offset in (0.,.18,-.18):
            heading=bot.pose.yaw+offset
            p=(x-math.sin(heading)*6.,y,z-math.cos(heading)*6.)
            node=nav.node(p)
            if node is not None:
                p=(p[0],nav.point(node)[1],p[2])
                if (self._traffic_corridor_clear(bot,p)
                        and self._maneuver_clear(bot,ai,p,now)):candidates.append(p)
        if not candidates:return
        ends=[(tx,ty+1.1,tz)]*len(candidates)
        hits=self.arena.world._static_rays([(p[0],p[1]+1.4,p[2]) for p in candidates],ends)
        self.stats['perception_rays']+=len(candidates)
        covered=[p for p,h in zip(candidates,hits) if h[0]>=0 and h[2]<.99]
        if covered:
            goal=min(covered,key=lambda p:math.dist(p,bot.pose.position))
            ai.cover_goal=goal
            ai.cover_until=min(now+3.,bot.reload_at-1.)
            ai.cover_check_at=now+.35;ai.cover_generation=nav.generation
            self.stats['cover_moves']+=1

    def _drive_cover(self, bot, ai, threat, now, braking):
        """Back into a checked corridor without turning the nose away."""
        goal=ai.cover_goal
        if goal is None:return None
        nav=self.navigation
        x,_,z=bot.pose.position
        dx,dz=goal[0]-x,goal[2]-z
        distance=math.hypot(dx,dz)
        heading=math.atan2(-dx,-dz)
        threat_angle=math.atan2(threat[0]-x,threat[2]-z)
        speed=max(0.,-bot.speed)
        stopping=speed*speed/(2*max(.5,braking*ai.BRAKING_SAFETY_FACTOR))+speed*ai.BRAKING_REACTION_SECONDS
        stop=(now>=ai.cover_until or bot.reload_at-now<=.75 or getattr(bot,'burst_pending',False)
              or distance<=.6+stopping or abs(wrap(heading-bot.pose.yaw))>.5
              or abs(wrap(threat_angle-bot.pose.yaw))>math.radians(35.)
              or not self._traffic_corridor_clear(bot,goal))
        if not stop and (now>=ai.cover_check_at or ai.cover_generation!=nav.generation):
            stop=not self._maneuver_clear(bot,ai,goal,now)
            ai.cover_check_at=now+.35;ai.cover_generation=nav.generation
        if stop:
            ai.cover_goal=None
            ai.route_kind=None;ai.next_plan=0.
            ai.maneuver_target=ai.target_id
            return self._finish_maneuver(ai,now)
        # Reverse steering has the opposite sign in Arena's drivetrain.
        error=wrap(heading-bot.pose.yaw)
        turn=ai.LEFT if error>.045 else ai.RIGHT if error<-.045 else 0
        return ai.REVERSE|turn

    @staticmethod
    def _needs_hull_aim(bot, ai, goal):
        low, high = bot.gun.yaw_limits
        x,_,z = bot.pose.position
        bearing=wrap(math.atan2(goal[0]-x,goal[2]-z)-bot.pose.yaw)
        if high-low >= math.tau-.01:
            # Restore the front after a route/target change, but retain a
            # useful hull angle instead of chasing the turret's every turn.
            ai.combat_align=abs(bearing)>math.radians(20. if ai.combat_align else 50.)
            return ai.combat_align
        # Stop once the gun has a useful traverse margin. Casemates retain
        # hull aiming; turreted tanks do not chase every target-bearing change.
        return not min(low+.05,0.) <= bearing <= max(high-.05,0.)

    @staticmethod
    def _reset_maneuver(ai):
        ai.maneuver_goal=ai.maneuver_origin=None
        ai.maneuver_target=None
        ai.next_maneuver=ai.maneuver_stable_until=0.

    def _finish_maneuver(self, ai, now):
        ai.maneuver_goal=ai.maneuver_origin=None
        # Release the tracks early enough for the physical chassis and gun
        # to settle. No aim/reaction state or gun reload timer is changed.
        ai.maneuver_stable_until=now+1.2+(ai.entity_id%3)*.15
        ai.next_maneuver=max(ai.next_maneuver,now+2.8+(ai.entity_id%4)*.2)
        self.stats['combat_hold_ticks']+=1
        return 0

    def _maneuver_clear(self, bot, ai, point, now):
        nav=self.navigation
        self.stats['combat_corridor_checks']+=1
        return nav.clear(bot.pose.position,point,
            blocked=tuple(node for node,until in ai.route_bad_cells.items() if now<until),
            climb_edges=nav.actor_climb_edges.get(bot.entity_id))

    def _maneuver(self, bot, ai, threat, chosen, eligible, now, braking):
        """Short reload-time track inputs, independent of the global route.

        Only the visible threat and our own gun state are used. Static grid
        checks are cached briefly; nearby known hulls are checked every drive
        decision. No rays, nearest-node searches or A* requests are added.
        """
        nav=self.navigation
        if (not eligible or nav is None or now<ai.reverse_until or ai.recovery_replan
                or ai.passing is not None or bot.entity_id in self.traffic.by_entity):
            self._reset_maneuver(ai)
            return None
        x,y,z=bot.pose.position;tx,_,tz=threat
        distance=math.hypot(tx-x,tz-z)
        bearing=math.atan2(tx-x,tz-z)
        if not 12.<=distance<=160. or abs(wrap(bearing-bot.pose.yaw))>math.radians(28.):
            self._reset_maneuver(ai)
            return None
        if chosen!=ai.maneuver_target:
            self._reset_maneuver(ai)
            ai.maneuver_target=chosen
            ai.next_maneuver=now+.8+(ai.entity_id%4)*.15
        reload_left=bot.reload_at-now
        burst=getattr(bot,'burst_pending',False)
        if ai.maneuver_goal is not None:
            ox,_,oz=ai.maneuver_origin
            dx=math.sin(ai.maneuver_heading)*ai.maneuver_direction
            dz=math.cos(ai.maneuver_heading)*ai.maneuver_direction
            remaining=(ai.maneuver_goal[0]-x)*dx+(ai.maneuver_goal[2]-z)*dz
            lateral=abs((x-ox)*dz-(z-oz)*dx)
            speed=max(0.,bot.speed*ai.maneuver_direction*math.cos(bot.pose.yaw-ai.maneuver_heading))
            deceleration=max(.5,braking*ai.BRAKING_SAFETY_FACTOR)
            stopping=speed*speed/(2*deceleration)+speed*ai.BRAKING_REACTION_SECONDS
            if (burst or reload_left<=.75 or now>=ai.maneuver_until
                    or remaining<=.35+stopping or lateral>.75):
                return self._finish_maneuver(ai,now)
            if not self._traffic_corridor_clear(bot,ai.maneuver_goal):
                self.stats['combat_blocked']+=1
                return self._finish_maneuver(ai,now)
            if now>=ai.maneuver_check_at or ai.maneuver_generation!=nav.generation:
                if not self._maneuver_clear(bot,ai,ai.maneuver_goal,now):
                    self.stats['combat_blocked']+=1
                    return self._finish_maneuver(ai,now)
                ai.maneuver_check_at=now+.35
                ai.maneuver_generation=nav.generation
        else:
            if now<ai.maneuver_stable_until:
                self.stats['combat_hold_ticks']+=1
                return 0
            # Moving approaches already provide evasive motion. Keep short
            # bursts and a loaded gun's firing opportunity free of new jinks.
            if burst or reload_left<1.8 or abs(bot.speed)>1.5 or now<ai.next_maneuver:
                return None
            ai.next_maneuver=now+2.8+(ai.entity_id%4)*.2
            travel=2.4+(ai.entity_id%4)*.4
            side=1. if (ai.entity_id+ai.maneuver_step//2)%2 else -1.
            change=max(-.18,min(.18,wrap(bearing+side*.2-bot.pose.yaw)))
            heading=bot.pose.yaw+change
            preferred=1 if (ai.entity_id+ai.maneuver_step)%2 else -1
            for direction in (preferred,-preferred):
                point=(x+math.sin(heading)*travel*direction,y,
                       z+math.cos(heading)*travel*direction)
                if (not self._traffic_corridor_clear(bot,point)
                        or not self._maneuver_clear(bot,ai,point,now)):continue
                ai.maneuver_goal=point;ai.maneuver_origin=bot.pose.position
                ai.maneuver_heading=heading;ai.maneuver_direction=direction
                ai.maneuver_until=min(now+2.4,bot.reload_at-.75)
                ai.maneuver_check_at=now+.35;ai.maneuver_generation=nav.generation
                ai.maneuver_step+=1
                self.stats['combat_moves']+=1
                self.stats['combat_forward_moves' if direction>0 else 'combat_reverse_moves']+=1
                break
            else:
                self.stats['combat_blocked']+=1
                return None
        # Arena reverses steering while backing: invert the turn input so
        # both directions preserve the same modest, front-facing hull angle.
        error=wrap(ai.maneuver_heading-bot.pose.yaw)*ai.maneuver_direction
        turn=ai.RIGHT if error>.045 else ai.LEFT if error<-.045 else 0
        self.stats['combat_drive_ticks']+=1
        return (ai.FORWARD if ai.maneuver_direction>0 else ai.REVERSE)|turn

    def _traffic_size(self, bot):
        nav=self.navigation
        return nav.footprints.get(bot.entity_id,(nav.radius,nav.radius))

    def _corridor_clear(self, bot, start, end):
        nav=self.navigation
        mask=getattr(nav,'actor_climb_edges',{}).get(bot.entity_id)
        return nav.clear(start,end,climb_edges=mask) if mask is not None else nav.clear(start,end)

    def _prune_route_obstacles(self, bot, ai, now):
        ai.route_bad_cells={node:until for node,until in ai.route_bad_cells.items() if now<until}
        for node,(entity,point,was_alive) in tuple(ai.route_bad_owners.items()):
            other=self.arena.vehicles.get(entity)
            known=other is not None and (other.team==bot.team or entity in ai.visible
                                         or entity in ai.seen_obstacles)
            moved=known and ((other.pose.position[0]-point[0])**2
                            +(other.pose.position[2]-point[2])**2>self.navigation.cell**2)
            if moved or was_alive and (not known or not other.alive):
                ai.route_bad_cells.pop(node,None)
            if node not in ai.route_bad_cells:
                ai.route_bad_owners.pop(node,None)

    def _watch_route(self, bot, ai, goal, waypoint, remaining, now, *, attempted_forward=False):
        """Learn one short failed corridor after repeated attempts, per bot.

        Track travel and replanned route distances can improve while retrying
        the same slope. Keep a physical highwater in the accepted local course.
        """
        lease=self.traffic.leases.get(self.traffic.by_entity.get(bot.entity_id))
        if lease is not None and (lease.phase in ('backout','parking')
                                  or lease.phase=='parked' and lease.yielder==bot.entity_id):
            if ai.route_watch_pause_at is None:ai.route_watch_pause_at=now
            return False
        if not bot.flags&ai.FORWARD and not attempted_forward:return False
        nav=self.navigation
        x,_,z=bot.pose.position;dx,dz=waypoint[0]-x,waypoint[2]-z
        length=math.hypot(dx,dz)
        if length<1e-6:return False
        direction=(dx/length,dz/length)
        if ai.route_watch_pause_at is not None:
            if ai.route_watch_at is not None:ai.route_watch_at+=now-ai.route_watch_pause_at
            ai.route_watch_pause_at=None
        fresh=ai.route_watch_at is None or ai.route_watch_origin is None
        if not fresh:
            ax,az=ai.route_watch_direction
            # Adjacent waypoint jitter is not a new attempt. A different
            # corridor is accepted once the hull has actually turned into it.
            fresh=(ax*direction[0]+az*direction[1]<math.cos(ai.FORWARD_ANGLE)
                   and abs(wrap(math.atan2(dx,dz)-bot.pose.yaw))<=ai.FORWARD_ANGLE
                   and abs(wrap(math.atan2(ax,az)-bot.pose.yaw))>ai.FORWARD_ANGLE)
        progress=0. if fresh else ((x-ai.route_watch_origin[0])*ai.route_watch_direction[0]
                                  +(z-ai.route_watch_origin[1])*ai.route_watch_direction[1])
        if fresh or progress>=1.:
            ai.route_watch_goal=nav.node(waypoint);ai.route_watch_at=now
            ai.route_watch_origin=(x,z);ai.route_watch_direction=direction
            ai.route_watch_best=0.
            return False
        ai.route_watch_best=max(ai.route_watch_best,progress)
        if now-ai.route_watch_at<10.:return False
        # A back-out or new lookahead can move away from the failed entry.
        ox,oz=ai.route_watch_origin;dx,dz=ai.route_watch_direction
        obstacle=ai.route_watch_obstacle or (ox+dx*nav.cell,0.,oz+dz*nav.cell)
        node=nav.node(obstacle)
        if node is None:return False
        z,x=divmod(node,nav.nx)
        for dz in (-1,0,1):
            for dx in (-1,0,1):
                xx,zz=x+dx,z+dz
                if 0<=xx<nav.nx and 0<=zz<nav.nz:
                    cell=zz*nav.nx+xx
                    ai.route_bad_cells[cell]=now+45.
                    entity=ai.route_watch_entity
                    if entity is None:
                        ai.route_bad_owners.pop(cell,None)
                    else:
                        ai.route_bad_owners[cell]=(entity,obstacle,self.arena.vehicles[entity].alive)
        while len(ai.route_bad_cells)>nav.MAX_BLOCKED_NODES:
            cell=next(iter(ai.route_bad_cells))
            ai.route_bad_cells.pop(cell)
            ai.route_bad_owners.pop(cell,None)
        ai.learned_corridors+=1;ai.route_watch_at=None
        ai.reverse_until=now+ai.REVERSE_SECONDS;ai.recovery_turn=0
        ai.recovery_replan=True;ai.recoveries+=1;ai.passing=None
        return True

    def _traffic_bounds(self, bot, other):
        width,length=self._traffic_size(bot);ow,ol=self._traffic_size(other)
        angle=other.pose.yaw-bot.pose.yaw
        sine,cosine=abs(math.sin(angle)),abs(math.cos(angle))
        return width+ow*cosine+ol*sine+.4,length+ol*cosine+ow*sine+.4

    def _traffic_corridor_clear(self, bot, goal):
        """Check two local detours against known hulls, not only the map."""
        nav=self.navigation;ai=self.arena.training_bots[bot.entity_id]
        x,y,z=bot.pose.position;dx,dz=goal[0]-x,goal[2]-z
        length2=dx*dx+dz*dz
        own=(math.hypot(*self._traffic_size(bot)) if bot.entity_id in nav.footprints else nav.radius)
        for other in self.arena.vehicles.values():
            if other.entity_id==bot.entity_id:continue
            if (other.team!=bot.team and other.entity_id not in ai.visible
                    and other.entity_id not in ai.seen_obstacles):continue
            ox,oy,oz=other.pose.position
            if abs(oy-y)>=4.:continue
            fraction=max(0.,min(1.,((ox-x)*dx+(oz-z)*dz)/length2)) if length2 else 0.
            radius=(math.hypot(*self._traffic_size(other)) if other.entity_id in nav.footprints else nav.radius)
            # Match the tangent's .2 m margin; roundoff at its exact point
            # of tangency must not reject both otherwise clear candidates.
            clearance=own+radius+.2-1e-6
            if (ox-x-fraction*dx)**2+(oz-z-fraction*dz)**2<clearance*clearance:return False
        return True

    def _rear_clear(self, bot, distance=5.):
        nav=self.navigation;ai=self.arena.training_bots[bot.entity_id]
        x,y,z=bot.pose.position;dx,dz=math.sin(bot.pose.yaw),math.cos(bot.pose.yaw)
        end=(x-dx*distance,y,z-dz*distance)
        if not self._corridor_clear(bot,bot.pose.position,end):
            # The rotating navigation disc can reject a cell occupied by a
            # valid oriented hull. Its grid cannot then also forbid every
            # recovery from that cell. Check the short rear corridor against
            # real geometry; the normal drivetrain still validates each move.
            node=nav.node(bot.pose.position)
            rays=getattr(self.arena.world,'_static_rays',None)
            if (node is None or nav.walkable.ravel()[node]
                    or nav.node(end) is None or rays is None):return False
            width=self._traffic_size(bot)[0]
            offsets=(-width,0.,width)
            hits=rays([(x+dz*d,y+.8,z-dx*d) for d in offsets],
                      [(end[0]+dz*d,y+.8,end[2]-dx*d) for d in offsets])
            self.stats['perception_rays']+=len(offsets)
            if any(h[0]>=0 and h[2]<.999 for h in hits):return False
        for other in self.arena.vehicles.values():
            if other.entity_id==bot.entity_id:continue
            if (other.team!=bot.team and other.entity_id not in ai.visible
                    and other.entity_id not in ai.seen_obstacles):continue
            ox,oy,oz=other.pose.position
            forward=(ox-x)*dx+(oz-z)*dz
            lateral=(ox-x)*dz-(oz-z)*dx
            width,length=self._traffic_bounds(bot,other)
            if abs(oy-y)<4. and -distance-length<forward<0 and abs(lateral)<width:return False
        return True

    def _margin_escape(self, bot, ai, now):
        """Leave a conservative disc margin along the hull's current heading."""
        if not ai.recoveries:return None
        nav=self.navigation;node=nav.node(bot.pose.position)
        if node is None or nav.walkable.ravel()[node]:
            ai.margin_goal=None
            return None
        x,y,z=bot.pose.position
        if ai.margin_goal is not None and now<ai.margin_until:
            return ai.margin_goal
        ai.margin_goal=None
        if now<ai.next_margin_check:return None
        ai.next_margin_check=now+.5
        rays=getattr(self.arena.world,'_static_rays',None)
        if rays is None:return None
        dx,dz=math.sin(bot.pose.yaw),math.cos(bot.pose.yaw)
        width=self._traffic_size(bot)[0];offsets=(-width,0.,width)
        # A straight exit needs less space than pivoting the enclosing disc.
        # Check two finite distances; never steer through an unavailable A*.
        for distance in (nav.radius*2.,nav.radius*3.):
            end=(x+dx*distance,y,z+dz*distance);target=nav.node(end)
            if target is None or not nav.walkable.ravel()[target]:continue
            end=(end[0],nav.point(target)[1],end[2])
            if not self._traffic_corridor_clear(bot,end):continue
            hits=rays([(x+dz*d,y+.8,z-dx*d) for d in offsets],
                [(end[0]+dz*d,end[1]+.8,end[2]-dx*d) for d in offsets])
            self.stats['perception_rays']+=len(offsets)
            if any(h[0]>=0 and h[2]<.999 for h in hits):continue
            ai.margin_goal=end;ai.margin_until=now+3.
            ai.reverse_until=now;ai.recovery_replan=False
            ai.check_at=ai.check_position=ai.check_yaw=None
            ai.next_plan=0.
            return end
        return None

    def _avoid(self, bot, waypoint, flags, now):
        nav=self.navigation
        if nav is None:return flags
        x,y,z=bot.pose.position;dx,dz=math.sin(bot.pose.yaw),math.cos(bot.pose.yaw)
        ai=self.arena.training_bots[bot.entity_id]
        if not flags&TrainingBot.REVERSE:
            ai.route_watch_obstacle=None
            ai.route_watch_entity=None
        if now>=ai.passing_until:ai.passing=None
        if flags&TrainingBot.REVERSE:
            ai.passing=None
            speed=max(0.,-bot.speed)
            deceleration=max(.5,self.arena._training_bot_braking_acceleration(bot)*ai.BRAKING_SAFETY_FACTOR)
            distance=max(1.,speed*ai.BRAKING_REACTION_SECONDS+speed*speed/(2*deceleration))
            return flags if self._rear_clear(bot,distance) else 0
        if not flags&TrainingBot.FORWARD and ai.passing is None:return flags
        radius=nav.radius
        deceleration=max(.5,self.arena._training_bot_braking_acceleration(bot)*ai.BRAKING_SAFETY_FACTOR)
        # A head-on pair needs turning room for both hulls, not merely one
        # stationary-obstacle braking distance. Begin the pass before the
        # combined closing speed consumes that room.
        braking_distance=abs(bot.speed)*ai.BRAKING_REACTION_SECONDS+bot.speed**2/(2*deceleration)
        horizon=max(radius*6.,braking_distance+radius*2)
        route_distance=math.hypot(waypoint[0]-x,waypoint[2]-z)
        threats=[]
        for other in self.arena.vehicles.values():
            if other.entity_id==bot.entity_id:continue
            if (other.team!=bot.team and other.entity_id not in ai.visible
                    and other.entity_id not in ai.seen_obstacles):continue
            ox,oy,oz=other.pose.position
            if abs(oy-y)>4.:continue
            forward=(ox-x)*dx+(oz-z)*dz
            lateral=(ox-x)*dz-(oz-z)*dx
            approaching=max(0.,-other.speed*(math.sin(other.pose.yaw)*dx+math.cos(other.pose.yaw)*dz))
            width,length=self._traffic_bounds(bot,other)
            # The route may turn before a distant hull. Do not demand a
            # straight passing corridor beyond the current route segment.
            # Retain room for both hulls, braking and an oncoming vehicle.
            reach=min(horizon,route_distance+length+braking_distance)
            if 0<forward<reach+approaching and abs(lateral)<width:
                threats.append((forward,other.entity_id))
        if not threats and ai.passing is None:return flags
        self.stats['avoidance']+=1
        if threats:
            ai.route_watch_entity=min(threats)[1]
            ai.route_watch_obstacle=self.arena.vehicles[ai.route_watch_entity].pose.position
        # Prefer the normal right-hand pass. A wall on that side must not
        # hide a usable left corridor around a stationary vehicle or wreck.
        if ai.passing is None:
            if now<ai.next_pass_check:return flags&~TrainingBot.FORWARD
            ai.next_pass_check=now+1.
            other=self.arena.vehicles[min(threats)[1]]
            obstacle=other.pose.position
            separation=math.hypot(obstacle[0]-x,obstacle[2]-z)
            sizes=self.navigation.footprints
            clearance=(math.hypot(*self._traffic_size(bot))+math.hypot(*self._traffic_size(other))+.2
                       if bot.entity_id in sizes and other.entity_id in sizes else radius*2+.8)
            if separation<=clearance:
                # A point behind the other hull cannot be reached safely
                # from inside this turning margin. Make room first.
                if self.traffic.request(bot,other,now):
                    override=self.traffic.controls(bot,ai,now)
                    if override is not None:return override
                ai.reverse_until=now+ai.REVERSE_SECONDS
                ai.recovery_turn=0
                ai.recovery_replan=True;ai.recoveries+=1
                return self._avoid(bot,waypoint,ai.REVERSE,now)
            bearing=math.atan2(obstacle[0]-x,obstacle[2]-z)
            travel=min(40.,separation+radius+2.)
            for direction in (1.,-1.):
                tangent=bearing+direction*math.asin(clearance/separation)
                side=(x+math.sin(tangent)*travel,y,z+math.cos(tangent)*travel)
                if self._corridor_clear(bot,bot.pose.position,side) and self._traffic_corridor_clear(bot,side):
                    # Taking the other lane against an oncoming ally would
                    # conflict with its own right-hand pass. Let one wait.
                    if (direction<0 and math.cos(other.pose.yaw-bot.pose.yaw)<-.5
                            and (abs(other.speed)>.3 or other.flags&TrainingBot.FORWARD)
                            and self.traffic.request(bot,other,now)):
                        override=self.traffic.controls(bot,ai,now)
                        if override is not None:return override
                    ai.passing=side;ai.passing_until=now+8.
                    break
            if ai.passing is None:
                if self.traffic.request(bot,other,now):
                    override=self.traffic.controls(bot,ai,now)
                    if override is not None:return override
                return flags&~TrainingBot.FORWARD
        side=ai.passing
        if math.hypot(side[0]-x,side[2]-z)<3.:
            ai.passing=None;return flags
        if self._corridor_clear(bot,bot.pose.position,side):
            error=wrap(math.atan2(side[0]-x,side[2]-z)-bot.pose.yaw)
            safe=not threats or min(threats)[0]>self._traffic_bounds(bot,self.arena.vehicles[min(threats)[1]])[1]+.6
            # The ordinary steering dead zone can stop a close tangent turn
            # just before it gains clearance, leaving both tracks idle.
            deadzone=.12 if safe else .02
            turn=TrainingBot.RIGHT if error>deadzone else TrainingBot.LEFT if error<-deadzone else 0
            return turn|(TrainingBot.FORWARD if safe and abs(error)<.65 else 0)
        # Yield before contact if the passing side has no terrain clearance.
        ai.passing=None
        return flags&~TrainingBot.FORWARD

    @staticmethod
    def _travel_aim(bot, ai, now):
        # Combat (including the finite last-seen point) owns the gun. Do not
        # change its aim sampling, reaction timer or normal traverse limits.
        low,high=bot.gun.yaw_limits
        if ai.target_id is not None or high-low < math.tau-.01:
            ai.travel_look=0.
            ai.travel_look_at=now
            ai.travel_look_active=False
            return
        reversing=bool(bot.flags&ai.REVERSE) or bot.speed < -.4
        turn=(bool(bot.flags&ai.RIGHT)-bool(bot.flags&ai.LEFT)) if not reversing else 0
        wanted=turn*ai.TRAVEL_LOOK_ANGLE
        dt=1/15 if ai.travel_look_at is None else max(0.,min(.25,now-ai.travel_look_at))
        ai.travel_look_at=now
        if not wanted and not ai.travel_look_active:return
        # Secondary motion follows the real drive intent. A damped command
        # eases in/out and cannot snap across when a route changes turn side;
        # GunAim.tick still performs all actual angular movement at XML rates.
        tau=ai.TRAVEL_LOOK_ENTER_SECONDS if wanted else ai.TRAVEL_LOOK_RELEASE_SECONDS
        change=(wanted-ai.travel_look)*dt/(tau+dt)
        limit=ai.TRAVEL_LOOK_RATE*dt
        ai.travel_look+=max(-limit,min(limit,change))
        if not wanted and abs(ai.travel_look)<.0005:ai.travel_look=0.
        ai.travel_look_active=bool(wanted or ai.travel_look)
        x,y,z=bot.pose.position
        angle=bot.pose.yaw+ai.travel_look
        bot.gun.set_point((x+math.sin(angle)*40.,y+bot.gun.TURRET[1]+bot.gun.GUN[1],
                           z+math.cos(angle)*40.),False)

    def _decide(self, bot, ai, now):
        self._drive(bot,ai,now)
        self._travel_aim(bot,ai,now)

    def _drive_route_exit(self, bot, ai, now):
        """Finish one accepted short detour before changing noncombat orders."""
        if ai.route_exit is None:return False
        search,point=ai.route_exit;nav=self.navigation
        distance=math.hypot(point[0]-bot.pose.position[0],point[2]-bot.pose.position[2])
        added=any(now<until and node not in search.blocked
                  for node,until in ai.route_bad_cells.items())
        if (not 1.<distance<=nav.cell*nav.JOIN_RINGS or search.generation!=nav.generation or added
                or ai.recovery_replan or now<ai.reverse_until
                or nav._segment_effort(bot.pose.position,point,blocked=search.blocked,
                    climb_edges=search.climb_edges,allow_blocked_start=True) is None):
            ai.route_exit=None
            return False
        flags=ai.controls(bot.pose,point,now,closing_speed=abs(bot.speed),
            braking_acceleration=self.arena._training_bot_braking_acceleration(bot),stop_distance=.5)
        bot.flags=self._avoid(bot,point,flags,now)
        if self._watch_route(bot,ai,ai.order_goal,point,distance,now,
                             attempted_forward=bool(flags&ai.FORWARD)):
            bot.flags=self._avoid(bot,point,ai.REVERSE,now)
        if ai.recovery_replan or ai.passing is not None:ai.route_exit=None
        return True

    def _drive(self, bot, ai, now):
        a=self.arena;nav=self.navigation
        ai.route_watch_obstacle=None
        ai.route_watch_entity=None
        patrol_changed=False
        visible=ai.visible.intersection(ai.known)
        candidates=[(eid,row[0]) for eid,row in sorted(ai.known.items())
                    if not visible or eid in visible]
        capture = getattr(a, 'base_capture', None)
        if capture is not None and capture.view()[0][2 - bot.team] > 0:
            capturers = [(eid, point) for eid, point in candidates
                         if eid in visible and capture.point.contains(point)]
            if capturers:
                candidates = capturers
        supremacy = getattr(a, 'supremacy', None)
        if supremacy is not None:
            threatened = [point for point, owner, cap in
                zip(supremacy.points, supremacy.owners, supremacy.captures)
                if owner == bot.team and (cap.totals[2-bot.team] > 0 or cap.blocked)]
            capturers = [(eid, position) for eid, position in candidates if eid in visible
                         and any(point.contains(position) for point in threatened)]
            if capturers:
                candidates = capturers
        previous_target=ai.target_id
        chosen=ai.choose_target(bot.pose.position,candidates)
        seen=chosen in ai.visible
        if not seen or chosen!=previous_target:
            ai.cover_goal=None
            ai.combat_align=False
        if (not seen or chosen!=ai.maneuver_target or now<ai.reverse_until
                or ai.recovery_replan or ai.passing is not None
                or bot.entity_id in self.traffic.by_entity):
            self._reset_maneuver(ai)
        if seen:ai.route_exit=None
        order=self.orders.select(bot,ai,now,chosen)
        kind='combat' if seen else order.kind if order is not None else 'patrol'
        if kind!=ai.order_kind:
            # Brief sight loss must not cancel the same approach search on
            # every visibility edge. Regroup/patrol changes own new routes.
            if {kind,ai.order_kind}!={'combat','investigate'}:
                ai.passing=None;ai.cover_goal=None;self._reset_maneuver(ai)
                ai.route_kind=None;ai.next_plan=0.
            ai.order_kind=kind
        if chosen is not None:
            aim_position=ai.known[chosen][0]
            exposed=ai.visible_points.get(chosen)
            if exposed is not None:
                aim_position=(exposed[0],exposed[1]-ai.AIM_HEIGHT,exposed[2])
            bot.gun.set_point(ai.aim_point(chosen,aim_position,now),False)
        if self.traffic.immobilized(bot):
            # Keep gun tracking during repairs and resume the same route afterward.
            bot.flags=0
            ai.check_at=ai.check_position=ai.check_yaw=None
            ai.reverse_until=min(ai.reverse_until,now)
            self._reset_maneuver(ai)
            if ai.route_watch_pause_at is None:ai.route_watch_pause_at=now
            return
        if order is not None and order.kind == 'defend':
            goal = order.goal
            ai.cover_goal = None
            self._reset_maneuver(ai)
        elif seen:
            goal=ai.known[chosen][0];self._cover(bot,ai,goal,now)
        elif order is not None:
            goal=order.goal
        else:
            goal=self.patrol_goal(bot,ai)
            if goal is None:
                bot.flags=0;return
            for _ in self.objectives[bot.team]:
                if math.hypot(goal[0]-bot.pose.position[0],goal[2]-bot.pose.position[2])>=8.:break
                ai.patrol_index+=1
                goal=self.patrol_goal(bot,ai)
                patrol_changed=True
            if patrol_changed:ai.route_kind=None;ai.next_plan=0.
        ai.order_goal=goal
        traffic_flags=self.traffic.controls(bot,ai,now)
        if traffic_flags is not None:
            ai.route_exit=None
            bot.flags=traffic_flags
            if not traffic_flags:ai.check_at=ai.check_position=ai.check_yaw=None
            if ai.route_watch_pause_at is None:ai.route_watch_pause_at=now
            return
        if self._drive_route_exit(bot,ai,now):return
        # Even an expired retreat goes through its stop/settle transition.
        cover=ai.cover_goal is not None
        distance=math.hypot(goal[0]-bot.pose.position[0],goal[2]-bot.pose.position[2])
        travelling=cover or order is not None
        if travelling:self._reset_maneuver(ai)
        align=seen and not travelling and self._needs_hull_aim(bot,ai,goal)
        target=a.vehicles.get(chosen) if seen else None
        closing=a._training_bot_closing_speed(bot,target) if target is not None else abs(bot.speed)
        braking=a._training_bot_braking_acceleration(bot)
        if nav is None:
            self._reset_maneuver(ai)
            bot.flags=ai.controls(bot.pose,goal,now,closing_speed=closing,
                braking_acceleration=braking,align_at_stop=align,
                stop_distance=order.stop_distance if order is not None else None)
            return
        escape=self._margin_escape(bot,ai,now)
        if escape is not None:
            self._reset_maneuver(ai)
            flags=ai.controls(bot.pose,escape,now,closing_speed=abs(bot.speed),
                braking_acceleration=braking,stop_distance=.5)
            bot.flags=self._avoid(bot,escape,flags,now)
            return
        if ai.recovery_replan:
            # A short back-out can still rejoin the current immutable route.
            # Changed learned cells trigger their own bounded replan below.
            ai.next_plan=ai.reverse_until
            ai.recovery_replan=False
        if now<ai.reverse_until:
            # Finish the physical back-out before asking for a path from the
            # new position. Waiting for a cancelled path must not swallow the
            # very reverse command which was meant to clear the obstruction.
            bot.flags=self._avoid(bot,goal,ai.REVERSE|ai.recovery_turn,now)
            return
        if ai.passing is not None and now<ai.passing_until:
            # Global route following must not steer back into the occupied
            # lane halfway through a validated local passing manoeuvre.
            passing=ai.passing
            flags=ai.controls(bot.pose,passing,now,closing_speed=abs(bot.speed),
                braking_acceleration=braking,stop_distance=.5)
            bot.flags=self._avoid(bot,passing,flags,now)
            if self._watch_route(bot,ai,goal,passing,math.dist(bot.pose.position,passing),now,
                                 attempted_forward=bool(flags&ai.FORWARD)):
                bot.flags=self._avoid(bot,goal,ai.REVERSE,now)
            return
        cover_flags=self._drive_cover(bot,ai,goal,now,braking)
        if cover_flags is not None:
            bot.flags=cover_flags
            ai.check_at=ai.check_position=ai.check_yaw=None
            if ai.route_watch_pause_at is None:ai.route_watch_pause_at=now
            return
        maneuver=self._maneuver(bot,ai,goal,chosen,
            seen and not travelling and not align
            and bot.gun.yaw_limits[1]-bot.gun.yaw_limits[0]>=math.tau-.01,now,braking)
        if maneuver is not None:
            bot.flags=maneuver
            ai.check_at=ai.check_position=ai.check_yaw=None
            if ai.route_watch_pause_at is None:ai.route_watch_pause_at=now
            return
        if seen and not travelling and distance<=ai.STOP_DISTANCE+max(0.,closing)**2/max(1.,braking):
            bot.flags=ai.controls(bot.pose,goal,now,closing_speed=closing,
                braking_acceleration=braking,align_at_stop=align)
            if ai.route_watch_pause_at is None:ai.route_watch_pause_at=now
            return
        self._prune_route_obstacles(bot,ai,now)
        blocked=frozenset(ai.route_bad_cells)
        search=nav.searches.get(bot.entity_id)
        if now>=ai.next_plan:
            moved=search is None or math.dist(nav.point(search.goal),goal)>8.
            retry=search is not None and search.done and not search.path
            changed=search is not None and (search.generation!=nav.generation
                                            or getattr(search,'blocked',frozenset())!=blocked)
            if retry:
                if kind in ('investigate','regroup'):self.orders.abandon(bot,now)
                if kind=='patrol' and not travelling:
                    ai.patrol_index+=1;goal=self.patrol_goal(bot,ai)
                nav.cancel(bot.entity_id);search=None
            if search is None or changed or ai.route_kind!=kind or moved and search.done:
                search=(nav.request(bot.entity_id,bot.pose.position,goal,blocked=blocked) if blocked
                        else nav.request(bot.entity_id,bot.pose.position,goal))
                ai.route_kind=kind
                if search is None and kind=='patrol' and not travelling and not retry:
                    ai.patrol_index+=1
            ai.next_plan=now+(2. if retry or search is None else .75)
        following=nav.waypoint(search,bot.pose.position,bot.speed)
        if following is None:
            bot.flags=0
            if align:
                # A casemate can aim its hull while the approach route is
                # pending or unavailable. Path availability must not disable
                # its gun against an already visible distant enemy.
                bot.flags=self._avoid(bot,goal,ai.controls(bot.pose,goal,now,
                    closing_speed=closing,braking_acceleration=braking,
                    remaining_distance=0.,align_at_stop=True),now)
            if search is not None and search.path:
                node=nav.node(bot.pose.position)
                # A ram or a conservative rotating-footprint margin can put
                # a valid oriented chassis just outside the coarse grid.
                # Enter only the nearby start of its real route, after a
                # physical geometry corridor check; never chase the enemy
                # directly through an unavailable route.
                entry=nav.point(search.start)
                if (node is not None and not nav.walkable.ravel()[node]
                        and math.dist(entry,bot.pose.position)<=nav.radius*2.):
                    x,y,z=bot.pose.position;ex,ey,ez=entry
                    distance=math.hypot(ex-x,ez-z)
                    if distance>.1:
                        rx,rz=(ez-z)/distance,-(ex-x)/distance
                        offsets=(-nav.radius*.4,0.,nav.radius*.4)
                        hits=a.world._static_rays([(x+rx*d,y+.8,z+rz*d) for d in offsets],
                            [(ex+rx*d,ey+.8,ez+rz*d) for d in offsets])
                        self.stats['perception_rays']+=len(offsets)
                        if all(h[0]<0 or h[2]>=.999 for h in hits):
                            flags=ai.controls(bot.pose,entry,now,closing_speed=abs(bot.speed),
                                braking_acceleration=braking,stop_distance=.4)
                            bot.flags=self._avoid(bot,entry,flags,now)
                            if self._watch_route(bot,ai,goal,entry,distance,now,
                                                 attempted_forward=bool(flags&ai.FORWARD)):
                                bot.flags=self._avoid(bot,goal,ai.REVERSE,now)
                            self.stats['route_entries']+=1
                            return
                if search.done:
                    nav.request(bot.entity_id,bot.pose.position,goal,blocked=blocked,restart=True)
                    ai.next_plan=now+.25
            # Reset only when waiting. A physically attempted grid-entry turn
            # still needs the normal stuck detector if the hull cannot rotate.
            if not align:ai.check_at=ai.check_position=ai.check_yaw=None
            if ai.route_watch_pause_at is None:ai.route_watch_pause_at=now
            return
        waypoint,remaining=following
        # Keep the chosen local exit while a slow hull turns into it. Expired
        # learned cells and a moving ally must not replace it with the old hill.
        if not seen and search.done and search.blocked and ai.passing is None:
            node=nav.node(bot.pose.position)
            outside=node is not None and nav.node(waypoint) not in search.blocked
            distance=math.hypot(waypoint[0]-bot.pose.position[0],waypoint[2]-bot.pose.position[2])
            if outside and 1.<distance<=nav.cell*nav.JOIN_RINGS:
                z,x=divmod(node,nav.nx)
                near=any(max(abs(n%nav.nx-x),abs(n//nav.nx-z))<=nav.JOIN_RINGS for n in search.blocked)
                if near:
                    ai.route_exit=(search,waypoint)
                    if self._drive_route_exit(bot,ai,now):return
        stop=order.stop_distance if order is not None else ai.STOP_DISTANCE if seen and not travelling else 1.
        # Route distance, not distance to the next tiny grid segment, governs
        # braking. The normal drivetrain still determines actual acceleration.
        flags=ai.controls(bot.pose,waypoint,now,closing_speed=abs(bot.speed),
            braking_acceleration=braking,stop_distance=stop,remaining_distance=remaining)
        bot.flags=self._avoid(bot,waypoint,flags,now)
        if self._watch_route(bot,ai,goal,waypoint,remaining,now,
                             attempted_forward=bool(flags&ai.FORWARD)):
            bot.flags=self._avoid(bot,goal,ai.REVERSE,now)

    def step(self, now):
        a=self.arena;nav=self.navigation
        if nav:
            nav.refresh(a.world.destroyed)
            nav.step()
        due=[]
        for eid,ai in a.training_bots.items():
            bot=a.vehicles[eid]
            if not bot.alive:
                bot.flags=0
                if nav:nav.cancel(eid)
                continue
            bot.last_input=now
            if a.step_count%self.DECISION_TICKS==self.phases[eid]:due.append(eid)
        self._perceive(due,now)
        for eid in due:
            bot=a.vehicles[eid]
            state=getattr(a.world,'motion',{}).get(eid)
            if state is not None and not state.grounded:
                bot.flags=0
                ai=a.training_bots[eid]
                ai.check_at=ai.check_position=ai.check_yaw=None
                self._reset_maneuver(ai)
                if ai.route_watch_pause_at is None:ai.route_watch_pause_at=now
                continue
            self._decide(bot,a.training_bots[eid],now)
        self.stats['decisions']+=len(due)
        self.stats['max_decisions_per_step']=max(self.stats['max_decisions_per_step'],len(due))
        return {eid:a.vehicles.get(ai.target_id) if ai.target_id in ai.visible else None
                for eid,ai in a.training_bots.items()}
