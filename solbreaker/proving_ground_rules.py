"""Server-owned state for one instance of the Solbreaker proving ground."""

from dataclasses import dataclass, field
import math

from .arena_bounds import CircularZone
from .bot_ai import TrainingBot
from .consumables import GRAVITY_JUMP_SPEC
from .mad_games import ACCELERATOR, HIGH_OCTANE


MAP_KEY = 'solbreaker_proving_ground'
PVP_CENTER = (300., -295.)
PVP_RADIUS = 125.
in_pvp = CircularZone(*PVP_CENTER, PVP_RADIUS)
SERVICE_PUMPS = ((-91., -49.), (69., -50.))
LOOP_CENTER = (357., 275.)
LOOP_RADIUS = 18.
ABILITIES = (GRAVITY_JUMP_SPEC, ACCELERATOR, HIGH_OCTANE)


def loop_east(angle):
    fraction = angle / math.tau
    return LOOP_CENTER[0] + 7. * (2. * fraction * fraction * (3. - 2. * fraction) - 1.)


def in_range(position):
    x, _, z = position
    return 210. <= x <= 430. and -85. <= z <= 85.


def in_demolition_sector(position):
    x, _, z = position
    return -440. <= x <= -185. and -105. <= z <= 105.


def pvp_spawn(team, slot):
    side = 1 if team == 1 else -1
    return (PVP_CENTER[0] + (slot - 3) * 13.,
            PVP_CENTER[1] - side * 75., 0. if side == 1 else math.pi)


@dataclass
class TargetSelection:
    practice_reuse_slot: int | None = None
    practice_selection: int = 0
    practice_target: object = None
    practice_next_pick: float = 0.
    practice_next_spawn: float = 0.
    practice_return_fire: bool = False


@dataclass
class TargetRoute:
    x: float
    end_z: float
    driver: TrainingBot

    def flags(self, vehicle, now):
        x, _, z = vehicle.pose.position
        if math.hypot(x - self.x, z - self.end_z) < 7.:
            self.end_z = -self.end_z
        return self.driver.controls(vehicle.pose, (self.x, 0., self.end_z),
                                    now, stop_distance=1.)


@dataclass
class RangeSeries:
    shots: int = 0
    hits: int = 0
    penetrations: int = 0
    damage: int = 0
    live: dict = field(default_factory=dict)

    def fire(self, shot):
        self.shots += 1
        self.live[shot] = False

    def contact(self, shot, *, direct, penetrated, damage):
        if shot not in self.live:
            return
        self.damage += damage
        if direct and not self.live[shot]:
            self.hits += 1
            self.live[shot] = True
        if penetrated:
            self.penetrations += 1

    def results(self):
        return dict(shots=self.shots, hits=self.hits, penetrations=self.penetrations, damage=self.damage)


@dataclass
class LoopRide:
    entity: int
    entry_speed: float
    entry_offset: tuple
    position: tuple
    angle: float = 0.

    def advance(self, dt, base):
        previous = self.angle
        speed = self.entry_speed + max(0., 21. - self.entry_speed) * math.sin(previous / 2.)
        self.angle = angle = min(math.tau, previous + speed * dt / LOOP_RADIUS)
        # The hull's track ends extend beyond its pivot on tight curves. Fade
        # their clearance back to zero on the flat entry and exit tangents.
        clearance = .62 * min(1., angle / .3, (math.tau - angle) / .3)
        radius = LOOP_RADIUS - clearance
        track = (loop_east(angle), base + LOOP_RADIUS - radius * math.cos(angle),
                 LOOP_CENTER[1] + radius * math.sin(angle))
        blend = max(0., 1. - angle / .42)
        position = tuple(coordinate + blend * offset for coordinate, offset in zip(track, self.entry_offset))
        velocity = tuple((new - old) / dt for new, old in zip(position, self.position))
        self.position = position
        return position, velocity, math.remainder(-angle, math.tau), previous <= math.pi < angle


@dataclass
class ProvingGround:
    selections: dict = field(default_factory=dict)
    owners: dict = field(default_factory=dict)
    routes: dict = field(default_factory=dict)
    arena_deaths: set = field(default_factory=set)
    score: dict = field(default_factory=lambda: {1: 0, 2: 0})
    pvp_shots: set = field(default_factory=set)
    series: dict = field(default_factory=dict)
    service_since: dict = field(default_factory=dict)
    next_service_check: float = 0.
    loop_ride: LoopRide | None = None

    def service_due(self, vehicle, now, *, grounded):
        x, _, z = vehicle.pose.position
        parked = (grounded and vehicle.alive and abs(vehicle.speed) < .35
                  and any(math.hypot(x-px, z-pz) <= 13. for px, pz in SERVICE_PUMPS))
        if not parked:
            self.service_since.pop(vehicle.entity_id, None)
            return False
        return now - self.service_since.setdefault(vehicle.entity_id, now) >= 2.


    def fired(self, shooter, shot):
        if in_pvp(shooter.pose.position):
            self.pvp_shots.add(shot)
        elif shooter.account_id is not None and in_range(shooter.pose.position):
            self.series.setdefault(shooter.entity_id, RangeSeries()).fire(shot)

    def shot_limit(self, shot, start, delta):
        """First entry/exit of the duel circle in this actual ballistic substep."""
        x, z = start[0] - PVP_CENTER[0], start[2] - PVP_CENTER[1]
        dx, dz = delta[0], delta[2]
        offset = x*x + z*z - PVP_RADIUS**2
        inside = shot in self.pvp_shots
        if abs(offset) > 1e-8 and (offset < 0) != inside:
            # The muzzle may already extend beyond the shooter's boundary.
            return 0.
        length_sq = dx*dx + dz*dz
        if length_sq < 1e-18:
            return None
        projection = x*dx + z*dz
        discriminant = projection*projection - length_sq*offset
        if discriminant <= 0.:
            return None  # Tangential contact does not enter the other region.
        root = math.sqrt(discriminant)
        fraction = (-projection + (root if inside else -root)) / length_sq
        return float(fraction) if 0. <= fraction <= 1. else None

    def finish_frame(self, projectiles):
        self.pvp_shots.intersection_update(projectiles)
        for series in self.series.values():
            for shot in series.live.keys() - projectiles.keys():
                del series.live[shot]


    def selection(self, vehicle):
        return self.selections.setdefault(vehicle.entity_id, TargetSelection())

    def placed(self, target, player):
        self.owners[target.entity_id] = player.entity_id
        self.routes.pop(target.entity_id, None)
        x, _, z = target.pose.position
        if 345. <= x <= 385. and abs(z) <= 48.:
            self.routes[target.entity_id] = TargetRoute(x, 48. if z <= 0 else -48.,
                                                       TrainingBot(target.entity_id))

    def remove(self, entity):
        self.owners.pop(entity, None)
        self.routes.pop(entity, None)

    def can_hit(self, shooter, target, shot=None):
        if shooter is None or target is None:
            return True
        in_arena = in_pvp(shooter.pose.position)
        if in_pvp(target.pose.position) != in_arena or (shot is not None and (shot in self.pvp_shots) != in_arena):
            return False
        if shooter.account_id is None:
            return self.owners.get(shooter.entity_id) == target.entity_id
        if target.account_id is None:
            return self.owners.get(target.entity_id) == shooter.entity_id
        return shooter.team != target.team and in_arena
