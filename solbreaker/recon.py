"""Authoritative recon timers, independent of client rendering and hit detection.

The caller identifies the vehicle under the reticle and calls ``aim`` on every
simulation step, including ``None`` when the lock is lost. It must also call
``hit`` once per actual enemy vehicle impact, including ricochets and impacts
without damage. Entity motion and visibility information are not stored here:
active markers follow the ordinary live vehicle state.
"""
from dataclasses import dataclass
import math
import random


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Invalid recon timestamp')
    return float(value)


def _entity(value):
    if type(value) is not int or value <= 0:
        raise ValueError('Invalid recon entity ID')
    return value


def _team(value):
    if type(value) is not int or value <= 0:
        raise ValueError('Invalid recon team ID')
    return value


@dataclass(frozen=True)
class ReconRules:
    lock_seconds: float = 1.5
    marker_seconds: float = 10.0
    base_chance: float = 0.20
    light_bonus: float = 0.10
    optics_bonus: float = 0.10
    camouflage_penalty: float = 0.10
    min_chance: float = 0.05
    max_chance: float = 0.80

    def __post_init__(self):
        for value in (self.lock_seconds, self.marker_seconds, self.base_chance,
                      self.light_bonus, self.optics_bonus, self.camouflage_penalty,
                      self.min_chance, self.max_chance):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('Invalid recon rules')
        if (self.lock_seconds <= 0 or self.marker_seconds <= 0 or
                not 0 <= self.min_chance <= self.base_chance <= self.max_chance <= 1 or
                min(self.light_bonus, self.optics_bonus, self.camouflage_penalty) < 0):
            raise ValueError('Invalid recon rules')

    def transmission_chance(self, *, light=False, optics=False, camouflage=False):
        if any(type(x) is not bool for x in (light, optics, camouflage)):
            raise ValueError('Recon modifiers must be boolean')
        value = (self.base_chance + self.light_bonus * light + self.optics_bonus * optics
                 - self.camouflage_penalty * camouflage)
        return max(self.min_chance, min(self.max_chance, value))


def server_test_requested(environ=None):
    """Enable authoritative recon only for an explicit server-only test.

    This flag does not advertise native client support or send marker data.
    """
    import os
    value = (os.environ if environ is None else environ).get('SOLBREAKER_RECON_SERVER_TEST', '0')
    if value not in ('0', '1'):
        raise ValueError('SOLBREAKER_RECON_SERVER_TEST must be 0 or 1')
    return value == '1'


class ReconState:
    """One battle's personal locks, team transmissions and recipient alerts.

    IDs are positive server entity/team IDs. All times are simulation seconds.
    Callers must pass monotonically nondecreasing aim times for each observer,
    and process hits in time order. Hit random samples are injectable for tests.
    """

    def __init__(self, rules=None, *, random_sample=None):
        self.rules = ReconRules() if rules is None else rules
        if not isinstance(self.rules, ReconRules):
            raise ValueError('Invalid recon rules')
        self._random = random.random if random_sample is None else random_sample
        if not callable(self._random):
            raise ValueError('Invalid recon random source')
        self._aims = {}              # observer -> (target | None, start, last sample)
        self._personal = {}          # (observer, target) -> expiry
        self._team_marks = {}        # (team, target) -> expiry

    def aim(self, observer_id, target_id, now):
        """Update the current lock; return True while a personal mark is refreshed."""
        observer = _entity(observer_id)
        target = None if target_id is None else _entity(target_id)
        if target == observer:
            raise ValueError('Cannot aim recon at self')
        now = _timestamp(now)
        previous = self._aims.get(observer)
        if previous is not None and now < previous[2]:
            raise ValueError('Recon aim time moved backwards')
        if previous is None or previous[0] != target:
            self._aims[observer] = (target, now, now)
            return False
        self._aims[observer] = (target, previous[1], now)
        if target is None or now - previous[1] + 1e-9 < self.rules.lock_seconds:
            return False
        self._personal[(observer, target)] = now + self.rules.marker_seconds
        return True

    def hit(self, observer_id, observer_team, target_id, target_team, now, *,
            light=False, optics=False, camouflage=False):
        """Attempt team transmission for an actual impact; return whether it succeeded."""
        observer, target = _entity(observer_id), _entity(target_id)
        team, enemy_team = _team(observer_team), _team(target_team)
        now = _timestamp(now)
        chance = self.rules.transmission_chance(light=light, optics=optics, camouflage=camouflage)
        if observer == target or team == enemy_team:
            return False
        sample = self._random()
        if isinstance(sample, bool) or not isinstance(sample, (int, float)) or not math.isfinite(sample) or not 0 <= sample < 1:
            raise ValueError('Invalid recon random sample')
        if sample >= chance:
            return False
        key = (team, target)
        # Out-of-order calls cannot shorten an already active transmission.
        self._team_marks[key] = max(self._team_marks.get(key, now), now + self.rules.marker_seconds)
        return True

    def personal_visible(self, observer_id, target_id, now):
        return _timestamp(now) < self._personal.get((_entity(observer_id), _entity(target_id)), -math.inf)

    def team_visible(self, observer_team, target_id, now):
        return _timestamp(now) < self._team_marks.get((_team(observer_team), _entity(target_id)), -math.inf)

    def visible(self, observer_id, observer_team, target_id, now):
        """Whether an enemy marker is enabled from either source for this viewer."""
        return (self.personal_visible(observer_id, target_id, now) or
                self.team_visible(observer_team, target_id, now))

    def alert_active(self, target_id, now):
        """The target's lamp is active while any team transmission is active."""
        target, now = _entity(target_id), _timestamp(now)
        return any(key[1] == target and now < expiry for key, expiry in self._team_marks.items())

    def expire(self, now):
        """Discard elapsed markers; never delete an observer's in-progress lock."""
        now = _timestamp(now)
        self._personal = {key: expiry for key, expiry in self._personal.items() if now < expiry}
        self._team_marks = {key: expiry for key, expiry in self._team_marks.items() if now < expiry}

    def remove_vehicle(self, entity_id):
        """Forget a removed/dead entity and any locks targeting it."""
        entity = _entity(entity_id)
        self._aims = {observer: state for observer, state in self._aims.items()
                      if observer != entity and state[0] != entity}
        self._personal = {key: expiry for key, expiry in self._personal.items() if entity not in key}
        self._team_marks = {key: expiry for key, expiry in self._team_marks.items() if key[1] != entity}
