"""Neutral-base capture for Encounter battles, advanced by the arena clock."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class CapturePoint:
    position: tuple
    radius: float = 25.
    points_per_second: float = 1.
    max_points_per_second: float = 3.2
    base_id: int = 0

    def __post_init__(self):
        if len(self.position) != 3 or not all(math.isfinite(v) for v in self.position):
            raise ValueError('Invalid capture point position')
        for value in (self.radius, self.points_per_second, self.max_points_per_second):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError('Invalid capture point parameters')
        if type(self.base_id) is not int or not 0 <= self.base_id <= 255:
            raise ValueError('Invalid capture point ID')

    def contains(self, position):
        return ((position[0] - self.position[0]) ** 2
                + (position[2] - self.position[2]) ** 2 <= self.radius ** 2)


class BaseCapture:
    TARGET = 100.
    RATE_EXPONENT = 1. / 3.
    # Local confirmation interval for the documented final chance to reset.
    CONFIRM_SECONDS = 3.

    def __init__(self, point):
        self.point = point
        self.members = {}
        self.points = {}
        self.totals = (0., 0.)
        self.blocked = False
        self.pending_team = 0
        self.pending_since = None

    def view(self):
        return tuple(min(int(self.TARGET), math.floor(value + 1e-8)) for value in self.totals), self.blocked

    def _total(self):
        totals = [0., 0.]
        for entity, points in self.points.items():
            totals[self.members[entity] - 1] += points
        self.totals = tuple(totals)

    def reset(self, entity):
        dropped = self.points.get(entity, 0.)
        if dropped:
            self.points[entity] = 0.
            self.pending_team = 0
            self.pending_since = None
            self._total()
        return dropped

    def advance(self, members, dt, now):
        """Members are living attached tanks currently inside the circle.

        Cube-root scaling reproduces the published 100/79/69/63/58/55/52s
        capture times for 1..7 tanks. Each tank owns its share of the progress.
        """
        self.members = members
        self.points = {entity: self.points.get(entity, 0.) for entity in members}
        teams = set(members.values())
        self.blocked = len(teams) > 1
        self._total()
        if len(teams) != 1:
            self.pending_team = 0
            self.pending_since = None
            return 0
        team = next(iter(teams))
        total = self.totals[team - 1]
        if total < self.TARGET - 1e-8:
            rate = min(self.point.max_points_per_second,
                       self.point.points_per_second * len(members) ** self.RATE_EXPONENT)
            share = min(self.TARGET - total, rate * dt) / len(members)
            for entity in members:
                self.points[entity] += share
            self._total()
        if self.totals[team - 1] < self.TARGET - 1e-8:
            self.pending_team = 0
            self.pending_since = None
            return 0
        if self.pending_team != team:
            self.pending_team = team
            self.pending_since = now
        return team if now - self.pending_since >= self.CONFIRM_SECONDS - 1e-8 else 0
