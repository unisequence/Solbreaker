"""Small tactical orders from public allies and confirmed enemy snapshots.

This module does not request paths, query collision or read unseen enemy poses.
The director retains combat, movement and the normal patrol fallback.
"""
from dataclasses import dataclass, field
import math


@dataclass(frozen=True)
class Order:
    kind: str
    goal: tuple
    stop_distance: float


@dataclass
class _Sighting:
    goal: tuple
    stamp: float
    visited: bool = False
    blocked_until: float = 0.


@dataclass
class _State:
    sightings: dict = field(default_factory=dict)
    investigating: int | None = None
    isolated_since: float | None = None
    regrouping: bool = False
    regroup_goal: tuple | None = None
    regroup_ally: int | None = None
    next_regroup_check: float = 0.
    regroup_started: float | None = None
    regroup_progress_at: float | None = None
    regroup_best_distance: float = math.inf
    regroup_cooldown_until: float = 0.
    last_kind: str | None = None


class BotOrders:
    MEMORY_SECONDS = 60.
    ARRIVAL_RADIUS = 7.
    INVESTIGATE_STOP = 5.
    BLOCKED_COOLDOWN = 15.
    ISOLATION_DISTANCE = 90.
    ISOLATION_SECONDS = 2.
    REGROUP_RELEASE = 45.
    REGROUP_STOP = 35.
    REGROUP_UPDATE_SECONDS = 1.
    REGROUP_GOAL_MOVEMENT = 8.
    HOME_RADIUS = 60.
    HOME_RELEASE = 50.
    REGROUP_MAX_SECONDS = 45.
    REGROUP_STALL_SECONDS = 10.
    REGROUP_PROGRESS_METRES = 2.
    REGROUP_COOLDOWN = 15.

    def __init__(self, director):
        self.director = director
        self.states = {}
        self.retired = set()
        self._objective_at = None
        self._objective_teams = {}
        self._strategic_at = None
        self._strategic_teams = {}
        # Constructed once during arena loading: these friendly spawn poses
        # are public map information, never live positions of unseen enemies.
        arena=director.arena
        teams={arena.vehicles[e].team for e in getattr(arena,'training_bots',{})
               if e in arena.vehicles}
        self.home_centers={}
        for team in teams:
            points=[v.pose.position for v in arena.vehicles.values() if v.team==team]
            self.home_centers[team]=tuple(sum(p[i] for p in points)/len(points) for i in range(3))

    @staticmethod
    def _distance2(first, second):
        return (first[0]-second[0])**2+(first[2]-second[2])**2

    def _remember(self, state, ai, position, now):
        for entity, (point, stamp) in ai.known.items():
            if entity in self.retired:continue
            if now >= stamp+self.MEMORY_SECONDS:
                continue
            previous = state.sightings.get(entity)
            if previous is None or stamp > previous.stamp:
                state.sightings[entity] = _Sighting(tuple(point), stamp)
        expired = []
        for entity, sighting in state.sightings.items():
            if now >= sighting.stamp+self.MEMORY_SECONDS:
                expired.append(entity)
            elif self._distance2(position, sighting.goal) <= self.ARRIVAL_RADIUS**2:
                sighting.visited = True
        for entity in expired:
            state.sightings.pop(entity)

    def _ally_position(self, bot, other, state):
        if other is None or other.entity_id==bot.entity_id or other.team!=bot.team:
            return None
        if not other.alive:
            return None
        if other.account_id is not None:
            player=self.director.arena.players.get(other.account_id)
            if player is None or not player.attached:
                return None
        position=other.pose.position
        home=self.home_centers.get(bot.team)
        radius=(self.HOME_RELEASE if state.regrouping and other.entity_id==state.regroup_ally
                else self.HOME_RADIUS)
        if home is not None and self._distance2(position,home)<=radius**2:
            return None
        return tuple(position)

    def _nearest_ally(self, bot, state):
        arena = self.director.arena
        nearest = None
        for other in arena.vehicles.values():
            # Team identity is public; reject enemies before accessing any
            # mutable pose/health state. Hidden enemies never enter this loop.
            position=self._ally_position(bot,other,state)
            if position is None:continue
            rank = (self._distance2(bot.pose.position, position), other.entity_id)
            if nearest is None or rank < nearest[0]:
                nearest = rank, tuple(position)
        return nearest

    def _end_regroup(self, state, now, failed=False):
        state.isolated_since=None
        state.regrouping=False
        state.regroup_goal=None
        state.regroup_ally=None
        state.regroup_started=state.regroup_progress_at=None
        state.regroup_best_distance=math.inf
        if failed:state.regroup_cooldown_until=now+self.REGROUP_COOLDOWN

    def _regroup(self, state, nearest, now):
        if now<state.regroup_cooldown_until:
            state.isolated_since=None
            return None
        if nearest is None:
            self._end_regroup(state,now)
            return None
        (distance2, entity), position = nearest
        if state.regrouping:
            if distance2 <= self.REGROUP_RELEASE**2:
                self._end_regroup(state,now)
                return None
            if now-state.regroup_started>=self.REGROUP_MAX_SECONDS:
                self._end_regroup(state,now,failed=True)
                return None
            distance=math.sqrt(distance2)
            if distance<=state.regroup_best_distance-self.REGROUP_PROGRESS_METRES:
                state.regroup_best_distance=distance
                state.regroup_progress_at=now
            if now-state.regroup_progress_at>=self.REGROUP_STALL_SECONDS:
                self._end_regroup(state,now,failed=True)
                return None
        elif distance2 > self.ISOLATION_DISTANCE**2:
            if state.isolated_since is None:
                state.isolated_since = now
            if now-state.isolated_since >= self.ISOLATION_SECONDS:
                state.regrouping = True
                state.regroup_goal = position
                state.regroup_ally = entity
                state.regroup_started = state.regroup_progress_at = now
                state.regroup_best_distance = math.sqrt(distance2)
                state.next_regroup_check = now+self.REGROUP_UPDATE_SECONDS
        else:
            state.isolated_since = None
        if not state.regrouping:
            return None
        if now >= state.next_regroup_check:
            state.next_regroup_check = now+self.REGROUP_UPDATE_SECONDS
            if self._distance2(position, state.regroup_goal) > self.REGROUP_GOAL_MOVEMENT**2:
                state.regroup_goal = position
            state.regroup_ally=entity
        return Order('regroup', state.regroup_goal, self.REGROUP_STOP)

    def select(self, bot, ai, now, chosen=None):
        """Combat > public defence > regroup > investigation > capture > patrol."""
        if not bot.alive:
            self.states.pop(bot.entity_id, None)
            return None
        state = self.states.setdefault(bot.entity_id, _State())
        self._remember(state, ai, bot.pose.position, now)
        selected = chosen if chosen is not None else getattr(ai, 'target_id', None)
        objective = self.objective(bot, ai, now, selected)
        if (state.regrouping and self._ally_position(bot,
                self.director.arena.vehicles.get(state.regroup_ally),state) is None):
            self._end_regroup(state,now)
        regroup = self._regroup(state, self._nearest_ally(bot,state), now)
        if selected is not None and selected in ai.visible:
            state.last_kind = None
            return None
        if objective is not None and objective.kind == 'defend':
            self._end_regroup(state, now)
            state.last_kind = objective.kind
            return objective
        if regroup is not None:
            state.last_kind = regroup.kind
            return regroup
        def usable(entity):
            sighting = state.sightings.get(entity)
            return sighting is not None and not sighting.visited and now >= sighting.blocked_until
        if not usable(state.investigating):
            state.investigating = selected if usable(selected) else None
            if state.investigating is None:
                best = None
                for entity, sighting in state.sightings.items():
                    if not usable(entity):
                        continue
                    rank = (-sighting.stamp, self._distance2(bot.pose.position, sighting.goal), entity)
                    if best is None or rank < best[0]:
                        best = rank, entity
                if best is not None:
                    state.investigating = best[1]
        if state.investigating is None:
            state.last_kind = objective.kind if objective is not None else None
            return objective
        state.last_kind = 'investigate'
        return Order('investigate', state.sightings[state.investigating].goal, self.INVESTIGATE_STOP)

    def objective(self, bot, ai, now, selected=None):
        """Assign capturers and responders from public progress and allies."""
        arena = self.director.arena
        if getattr(arena, 'supremacy', None) is not None:
            return self.strategic_objective(bot, ai, now, selected)
        capture = getattr(arena, 'base_capture', None)
        if capture is None:
            return None
        point = capture.point
        if self._objective_at != now:
            self._objective_at = now
            self._objective_teams = {}
        if bot.team not in self._objective_teams:
            allies = [v for v in arena.vehicles.values() if v.team == bot.team and v.alive
                      and (v.account_id is None or arena.players[v.account_id].attached)]
            bots = sorted((v for v in allies if v.entity_id in arena.training_bots),
                key=lambda v: (not point.contains(v.pose.position),
                               self._distance2(v.pose.position, point.position), v.entity_id))
            self._objective_teams[bot.team] = bots
        bots = self._objective_teams[bot.team]
        progress, blocked = capture.view()
        threatened = progress[2 - bot.team] > 0 or blocked and progress[bot.team - 1] > 0
        if threatened:
            defenders = bots[:2] if progress[2 - bot.team] >= 70 else bots[:1]
            if any(v.entity_id == bot.entity_id for v in defenders):
                # Once inside, ordinary combat can hull-aim at a visible
                # capturer. Hidden enemies supply only the public base alert.
                known = ai.known.get(selected)
                if (selected in ai.visible and known is not None
                        and point.contains(known[0]) and point.contains(bot.pose.position)):
                    return None
                return Order('defend', point.position, point.radius * .35)
        capturers = bots[:1]
        for index, candidate in enumerate(capturers):
            if candidate.entity_id == bot.entity_id:
                # Spread hulls within the circle rather than parking them
                # on top of one another at its exact centre.
                angle = (index - (len(capturers) - 1) / 2.) * math.pi * .5
                home = self.home_centers.get(bot.team, point.position)
                facing = math.atan2(home[0] - point.position[0], home[2] - point.position[2])
                offset = point.radius * .4
                goal = (point.position[0] + math.sin(facing + angle) * offset,
                        point.position[1],
                        point.position[2] + math.cos(facing + angle) * offset)
                return Order('capture', goal, 2.)
        return None

    def strategic_objective(self, bot, ai, now, selected):
        arena = self.director.arena
        supremacy = arena.supremacy
        if self._strategic_at != now:
            self._strategic_at = now
            self._strategic_teams = {}
        if bot.team not in self._strategic_teams:
            available = [v for v in arena.vehicles.values() if v.team == bot.team and v.alive
                         and v.entity_id in arena.training_bots
                         and (v.account_id is None or arena.players[v.account_id].attached)]
            home = self.home_centers.get(bot.team, bot.pose.position)
            tasks = []
            for point, owner, capture in zip(supremacy.points, supremacy.owners, supremacy.captures):
                threatened = owner == bot.team and (
                    capture.totals[2-bot.team] > 0 or capture.blocked)
                if owner == bot.team and not threatened:
                    continue
                tasks.append((0 if threatened else 1 if not owner else 2,
                              self._distance2(home, point.position), point.base_id, point,
                              'defend' if threatened else 'capture'))
            assignments = {}
            # One capturer and one responder per team leave the majority
            # available for combat, pursuit and ordinary patrol routes.
            remaining = {'capture': 1, 'defend': 1}
            for _, _, _, point, kind in sorted(tasks):
                if not remaining[kind]:
                    continue
                available.sort(key=lambda v: (not point.contains(v.pose.position),
                    self._distance2(v.pose.position, point.position), v.entity_id))
                chosen, available = available[:1], available[1:]
                remaining[kind] -= len(chosen)
                for index, actor in enumerate(chosen):
                    angle = math.atan2(home[0]-point.position[0], home[2]-point.position[2])
                    angle += (index - (len(chosen)-1)/2) * math.pi * .5
                    goal = (point.position[0] + math.sin(angle)*point.radius*.4,
                            point.position[1],
                            point.position[2] + math.cos(angle)*point.radius*.4)
                    assignments[actor.entity_id] = (Order(kind, goal, 2.), point)
            self._strategic_teams[bot.team] = assignments
        assignment = self._strategic_teams[bot.team].get(bot.entity_id)
        if assignment is None:
            return None
        order, point = assignment
        known = ai.known.get(selected)
        if (order.kind == 'defend' and selected in ai.visible and known is not None
                and point.contains(known[0]) and point.contains(bot.pose.position)):
            return None
        return order

    def abandon(self, bot, now):
        """Cool down one failed investigation snapshot, without extending it
        on repeated failure callbacks or changing its original memory TTL."""
        state = self.states.get(bot.entity_id)
        if state is None:
            return
        if state.last_kind=='regroup':
            self._end_regroup(state,now,failed=True)
            state.last_kind=None
            return
        if state.last_kind!='investigate':return
        sighting = state.sightings.get(state.investigating)
        if sighting is not None:
            sighting.blocked_until = now+self.BLOCKED_COOLDOWN
        state.investigating = None
        state.last_kind = None

    def forget_enemy(self, entity):
        """Consume a public death ID; stale perception cannot revive it."""
        self.retired.add(entity)
        for state in self.states.values():
            state.sightings.pop(entity,None)
            if state.investigating==entity:
                state.investigating=None
                if state.last_kind=='investigate':state.last_kind=None
