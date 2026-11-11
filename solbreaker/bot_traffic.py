"""Bounded, local right-of-way for friendly NPCs in a single-width lane.

This only chooses ordinary drive inputs. Static clearance stays with the
director's navigation grid; vehicle clearance uses each bot's own knowledge.
"""
from dataclasses import dataclass
import math


@dataclass
class TrafficLease:
    owner: int
    yielder: int
    deadline: float
    origin: tuple
    reverse_axis: tuple
    lane_axis: tuple
    last_progress: float
    next_parking: float = 0.
    best_backout: float = 0.
    parking_goal: tuple | None = None
    best_distance: float = math.inf
    best_error: float = math.inf
    advanced: bool = False
    phase: str = 'backout'
    cooldown_until: float = 0.
    owner_origin: tuple | None = None
    owner_position: tuple | None = None


class TrafficController:
    MAX_LEASES = 7
    LEASE_SECONDS = 18.
    STALL_SECONDS = 3.
    COOLDOWN_SECONDS = 3.
    PARKING_INTERVAL = 1.
    MIN_BACKOUT = 3.
    BACKOUT_PARKING_DELAY = 1.
    MAX_BACKOUT = 20.
    PROGRESS_METRES = .4

    def __init__(self, director):
        self.director = director
        self.leases = {}
        self.by_entity = {}

    def immobilized(self, bot):
        engine=getattr(self.director.arena,'engines',{}).get(bot.entity_id)
        return (any(track.broken for track in getattr(bot,'tracks',{}).values())
                or engine is not None and engine.destroyed)

    def _pair_valid(self, first, second):
        arena = self.director.arena
        return (first is not None and second is not None and first.entity_id != second.entity_id
                and first.entity_id in arena.training_bots and second.entity_id in arena.training_bots
                and first.account_id is None and second.account_id is None
                and first.alive and second.alive and first.team == second.team
                and not self.immobilized(first) and not self.immobilized(second))

    def _remove(self, key):
        lease = self.leases.pop(key)
        for entity in (lease.owner, lease.yielder):
            if self.by_entity.get(entity) == key:
                self.by_entity.pop(entity)

    def _prune(self, now):
        vehicles = self.director.arena.vehicles
        for key, lease in tuple(self.leases.items()):
            if (not self._pair_valid(vehicles.get(lease.owner), vehicles.get(lease.yielder))
                    or lease.phase == 'cooldown' and now >= lease.cooldown_until):
                self._remove(key)

    @staticmethod
    def _reset_recovery(ai, now):
        ai.reverse_until = min(ai.reverse_until, now)
        ai.check_at = ai.check_position = ai.check_yaw = None
        ai.check_error = ai.check_heading = None
        ai.recovery_replan = False
        ai.passing = None
        ai.passing_until = 0.

    def request(self, bot, other, now):
        """Reserve stable roles for two living friendly NPCs, never a player."""
        self._prune(now)
        if self.director.navigation is None or not self._pair_valid(bot, other):
            return False
        key = tuple(sorted((bot.entity_id, other.entity_id)))
        if key in self.leases:
            return self.leases[key].phase != 'cooldown'
        if (len(self.leases) >= self.MAX_LEASES
                or bot.entity_id in self.by_entity or other.entity_id in self.by_entity):
            return False
        # Prefer the participant which can actually back away from its peer.
        # Equal cases use a stable ID, independent of decision order or speed.
        candidates = []
        for actor, peer in ((bot, other), (other, bot)):
            dx = actor.pose.position[0]-peer.pose.position[0]
            dz = actor.pose.position[2]-peer.pose.position[2]
            away = -math.sin(actor.pose.yaw)*dx-math.cos(actor.pose.yaw)*dz
            if away >= 0. and self.director._rear_clear(actor):
                candidates.append(actor.entity_id)
        yielding = max(candidates) if candidates else key[1]
        waiting = key[0] if yielding == key[1] else key[1]
        vehicle = self.director.arena.vehicles[yielding]
        owner = self.director.arena.vehicles[waiting]
        x, _, z = vehicle.pose.position
        ox, _, oz = owner.pose.position
        length = math.hypot(x-ox, z-oz)
        axis = ((x-ox)/length, (z-oz)/length) if length else (0., 1.)
        self.leases[key] = TrafficLease(waiting, yielding, now+self.LEASE_SECONDS,
            vehicle.pose.position, (-math.sin(vehicle.pose.yaw), -math.cos(vehicle.pose.yaw)),
            axis, now, owner_origin=owner.pose.position)
        for entity in key:
            self.by_entity[entity] = key
            self._reset_recovery(self.director.arena.training_bots[entity], now)
        return True

    def _cooldown(self, lease, now):
        lease.phase = 'cooldown'
        lease.cooldown_until = now+self.COOLDOWN_SECONDS
        for entity in (lease.owner, lease.yielder):
            self._reset_recovery(self.director.arena.training_bots[entity], now)

    def _vehicles_clear(self, bot, ai, goal):
        x, y, z = bot.pose.position
        dx, dz = goal[0]-x, goal[2]-z
        length2 = dx*dx+dz*dz
        clearance2 = (2*self.director.navigation.radius+.4)**2
        for other in self.director.arena.vehicles.values():
            if other.entity_id == bot.entity_id:
                continue
            if (other.team != bot.team and other.entity_id not in ai.visible
                    and other.entity_id not in ai.seen_obstacles):
                continue
            ox, oy, oz = other.pose.position
            if abs(oy-y) >= 4.:
                continue
            fraction = max(0., min(1., ((ox-x)*dx+(oz-z)*dz)/length2)) if length2 else 0.
            closest2 = (ox-x-fraction*dx)**2+(oz-z-fraction*dz)**2
            if closest2 < clearance2:
                initial2 = (ox-x)**2+(oz-z)**2
                end2 = (ox-goal[0])**2+(oz-goal[2])**2
                # Conservative circles can already overlap: permit only an
                # outward/tangent exit that finishes fully clear of this body.
                if (initial2 < clearance2 and closest2 >= initial2-1e-6
                        and end2 >= clearance2):
                    continue
                return False
        return True

    def _try_parking(self, bot, ai, lease, now):
        if now < lease.next_parking:
            return
        lease.next_parking = now+self.PARKING_INTERVAL
        nav = self.director.navigation
        x, y, z = bot.pose.position
        ax, az = lease.lane_axis
        distance = 2*nav.radius+2.
        preferred = 1. if bot.entity_id % 2 else -1.
        # At most two static corridor checks per attempt, at most once a second.
        for side in (preferred, -preferred):
            goal = (x+az*side*distance, y, z-ax*side*distance)
            if self._static_clear(bot, goal) and self._vehicles_clear(bot, ai, goal):
                lease.parking_goal = goal
                lease.phase = 'parking'
                lease.best_distance = distance
                lease.best_error = abs((math.atan2(goal[0]-x, goal[2]-z)-bot.pose.yaw+math.pi)%math.tau-math.pi)
                lease.last_progress = now
                lease.advanced = False
                self._reset_recovery(ai, now)
                return

    def _static_clear(self, bot, goal):
        nav=self.director.navigation
        mask=getattr(nav,'actor_climb_edges',{}).get(bot.entity_id)
        return (nav.clear(bot.pose.position,goal,climb_edges=mask) if mask is not None
                else nav.clear(bot.pose.position,goal))

    def controls(self, bot, ai, now):
        """Return a drive override, or None when ordinary AI may drive.

        A parked yielder stays still while its owner resumes ordinary route
        following; no navigation or gun logic is duplicated here.
        """
        self._prune(now)
        key = self.by_entity.get(bot.entity_id)
        if key is None:
            return None
        lease = self.leases[key]
        owner = self.director.arena.vehicles[lease.owner]
        yielding = self.director.arena.vehicles[lease.yielder]
        nav = self.director.navigation
        if nav is None:
            self._remove(key)
            return None
        if lease.phase == 'cooldown':
            return None
        if lease.phase == 'parking':
            x, _, z = yielding.pose.position
            ox, _, oz = lease.origin
            ax, az = lease.lane_axis
            lateral = abs((x-ox)*az-(z-oz)*ax)
            if (lateral >= 2*nav.radius+.4
                    and math.hypot(x-lease.parking_goal[0], z-lease.parking_goal[2]) < 1.5):
                lease.phase = 'parked'
                lease.owner_position = owner.pose.position
                lease.last_progress = now
        if lease.phase == 'parked':
            x, _, z = yielding.pose.position
            ox, _, oz = owner.pose.position
            passed = (ox-x)*lease.lane_axis[0]+(oz-z)*lease.lane_axis[1] > 2*nav.radius+.8
            dx, dz = ox-lease.owner_origin[0], oz-lease.owner_origin[2]
            ax, az = lease.lane_axis
            left_lane = (dx*ax+dz*az < -self.MIN_BACKOUT
                         or abs(dx*az-dz*ax) > 2*nav.radius+.8)
            separated = math.hypot(ox-x, oz-z) > 2*nav.radius+.8
            if passed or left_lane and separated or now >= lease.deadline:
                self._remove(key)
                return None
            px, _, pz = lease.owner_position
            if math.hypot(ox-px, oz-pz) >= self.PROGRESS_METRES:
                lease.owner_position = owner.pose.position
                lease.last_progress = now
            if now-lease.last_progress >= self.STALL_SECONDS:
                self._cooldown(lease, now)
                return None
            return None if bot.entity_id == lease.owner else 0
        if now >= lease.deadline or now-lease.last_progress >= self.STALL_SECONDS:
            self._cooldown(lease, now)
            return None
        if bot.entity_id == lease.owner:
            return 0
        if lease.phase == 'backout':
            x, _, z = bot.pose.position
            ox, _, oz = lease.origin
            travelled = (x-ox)*lease.reverse_axis[0]+(z-oz)*lease.reverse_axis[1]
            if travelled >= lease.best_backout+self.PROGRESS_METRES:
                lease.best_backout = travelled
                lease.last_progress = now
            rear_clear = self.director._rear_clear(bot)
            # A blocked rear must not make side parking unreachable forever.
            # Also try a side when a legal reverse input produced no progress.
            if (travelled >= self.MIN_BACKOUT or not rear_clear
                    or now-lease.last_progress >= self.BACKOUT_PARKING_DELAY):
                self._try_parking(bot, ai, lease, now)
            if lease.phase == 'backout':
                if travelled >= self.MAX_BACKOUT or not rear_clear:
                    return 0
                # Reversing towards the waiting peer cannot make passing room.
                px, _, pz = owner.pose.position
                away = (x-px)*lease.reverse_axis[0]+(z-pz)*lease.reverse_axis[1]
                return ai.REVERSE if away >= 0. else 0
        goal = lease.parking_goal
        if not self._static_clear(bot, goal) or not self._vehicles_clear(bot, ai, goal):
            return 0
        x, _, z = bot.pose.position
        distance = math.hypot(goal[0]-x, goal[2]-z)
        error = abs((math.atan2(goal[0]-x, goal[2]-z)-bot.pose.yaw+math.pi)%math.tau-math.pi)
        if (distance <= lease.best_distance-self.PROGRESS_METRES
                or not lease.advanced and error <= lease.best_error-.06):
            lease.last_progress = now
            lease.best_distance = min(lease.best_distance, distance)
            lease.best_error = min(lease.best_error, error)
        acceleration = self.director.arena._training_bot_braking_acceleration(bot)
        flags = ai.controls(bot.pose, goal, now, closing_speed=abs(bot.speed),
                            braking_acceleration=acceleration, stop_distance=1.)
        if flags & ai.REVERSE:
            self._cooldown(lease, now)
            return 0
        # Complete the initial pivot inside the validated turning footprint.
        # Starting a curved approach too early can leave a narrow pocket.
        if not lease.advanced and error > ai.TURN_DEADZONE:
            flags &= ~ai.FORWARD
        if flags & ai.FORWARD:
            lease.advanced = True
        return flags
