"""Supremacy capture and score rules driven by the arena's fixed clock."""
from dataclasses import dataclass
import math

from .base_capture import BaseCapture, CapturePoint


@dataclass(frozen=True)
class StrategicPoint(CapturePoint):
    capture_points: int = 15
    income: int = 5
    income_seconds: float = 2.
    capture_bonus: int = 0
    loss_penalty: int = 0

    def __post_init__(self):
        super().__post_init__()
        for value in (self.capture_points, self.income, self.capture_bonus, self.loss_penalty):
            if type(value) is not int or value < 0:
                raise ValueError('Invalid strategic point score')
        if self.capture_points == 0 or not math.isfinite(self.income_seconds) or self.income_seconds <= 0:
            raise ValueError('Invalid strategic point timing')


class PointCapture(BaseCapture):
    CONFIRM_SECONDS = 0.
    RATE_EXPONENT = 1.

    def __init__(self, point):
        super().__init__(point)
        self.TARGET = point.capture_points


class Supremacy:
    TARGET = 1000
    INITIAL = 300
    KILL_GAIN = 40
    DEATH_LOSS = 30

    def __init__(self, points):
        self.points = tuple(sorted(points, key=lambda p: p.base_id))
        if not self.points or len({p.base_id for p in self.points}) != len(self.points):
            raise ValueError('Supremacy requires distinct strategic points')
        self.captures = [PointCapture(p) for p in self.points]
        self.owners = [0] * len(self.points)
        self.clocks = [0.] * len(self.points)
        self.inside = [{} for p in self.points]
        self.scores = [self.INITIAL, self.INITIAL]
        self.observed = None
        self.finished_winner = None

    def state(self):
        if self.observed is not None:
            return self.observed
        statuses = []
        for point, owner, capture, inside in zip(self.points, self.owners, self.captures, self.inside):
            totals, blocked = capture.view()
            invaders = set(inside.values()) - {owner}
            invading = next(iter(invaders)) if len(invaders) == 1 else (
                1 if totals[0] > totals[1] else 2 if totals[1] > totals[0] else 0)
            statuses.append((point.base_id, owner, invading,
                             totals[invading-1] if invading else 0, blocked, tuple(sorted(inside))))
        return tuple(self.scores), tuple(statuses)

    def observe(self, state):
        # Owner mirrors consume public snapshots and never advance this solver.
        self.observed = state
        self.scores[:] = state[0]
        self.owners[:] = [row[1] for row in state[1]]

    def winner(self):
        if self.finished_winner is not None:
            return self.finished_winner
        won = [team for team in (1, 2) if self.scores[team-1] >= self.TARGET]
        if won:
            self.finished_winner = won[0] if len(won) == 1 else 0
        return self.finished_winner

    def timeout_winner(self):
        return 1 if self.scores[0] > self.scores[1] else 2 if self.scores[1] > self.scores[0] else 0

    def reset(self, entity):
        return sum(capture.reset(entity) for capture in self.captures)

    def destroyed(self, team, enemy_kill):
        if self.finished_winner is not None:
            return
        self.scores[team-1] = max(0, self.scores[team-1] - self.DEATH_LOSS)
        if enemy_kill:
            self.scores[2-team] = min(self.TARGET, self.scores[2-team] + self.KILL_GAIN)

    def advance(self, members, dt, now):
        """Return contributions earned by tanks completing a capture."""
        earned = []
        if self.finished_winner is not None:
            return earned
        self.inside = members
        for index, (point, capture, inside) in enumerate(zip(self.points, self.captures, members)):
            owner = self.owners[index]
            teams = set(inside.values())
            active = inside if teams - {owner} else {}
            winner = capture.advance(active, dt, now)
            if winner:
                earned.extend(capture.points.items())
                self.owners[index] = winner
                self.clocks[index] = 0.
                self.scores[winner-1] = min(self.TARGET, self.scores[winner-1] + point.capture_bonus)
                if owner:
                    self.scores[owner-1] = max(0, self.scores[owner-1] - point.loss_penalty)
                capture.advance({}, 0., now)
                continue
            if owner and not teams - {owner}:
                self.clocks[index] += dt
                ticks = math.floor((self.clocks[index] + 1e-8) / point.income_seconds)
                if ticks:
                    self.clocks[index] -= ticks * point.income_seconds
                    self.scores[owner-1] = min(self.TARGET, self.scores[owner-1] + ticks * point.income)
        return earned
