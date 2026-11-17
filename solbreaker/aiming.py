"""Server gun state and native point-aim RPCs."""
import math
import os
import struct
import numpy as np

from .orientation import matrix


# Starting bias before the gun has settled. 1.0 samples uniformly by area.
SHOT_DISPERSION_CENTER_BIAS = 1.50
UNSETTLED_CENTER_CHANCE = .5 ** (2. / SHOT_DISPERSION_CENTER_BIAS)
SETTLED_CENTER_CHANCE = .90  # Probability inside half of the dispersion radius.
FULL_SETTLE_TIME_CONSTANTS = 3.  # About 95% of exponential bloom has decayed.


def wrap(angle):
    return (angle + math.pi) % math.tau - math.pi


def settling_time(config, aim_factor):
    return max(1. / 60., config.aiming_time * aim_factor)


def dispersion_tangent(config, *, shot_age=None, factor=1., aim_factor=1.,
                       movement_factor=1., gun_damaged=False):
    """Return the cone radius per metre used by ballistics and the gun marker."""
    dispersion = getattr(config, 'dispersion_radius', 0.)
    if not dispersion:
        return 0.
    if gun_damaged:
        dispersion *= getattr(config, 'damaged_gun_dispersion', 1.)
    if shot_age is not None:
        settle = settling_time(config, aim_factor)
        dispersion *= 1. + (config.after_shot_dispersion - 1.) * math.exp(-max(0., shot_age) / settle)
    return dispersion * factor * movement_factor / 100.


def disperse_shot(direction, tangent, rng, aim_progress=0.):
    if not tangent:
        return direction
    center_chance = UNSETTLED_CENTER_CHANCE + (SETTLED_CENTER_CHANCE - UNSETTLED_CENTER_CHANCE) * aim_progress
    # For U**exponent, P(radius <= half) = .5**(1/exponent).
    radius = tangent * rng.random() ** (-1. / math.log2(center_chance))
    angle = rng.uniform(0., math.tau)
    dx, dy, dz = direction
    length = math.hypot(dx, dz)
    right = (dz / length, 0., -dx / length) if length else (1., 0., 0.)
    rx, _, rz = right
    up = (-rz * dy, rz * dx - rx * dz, rx * dy)
    azimuth, elevation = radius * math.cos(angle), radius * math.sin(angle)
    norm = math.hypot(1., radius)
    return tuple((axis + azimuth * r + elevation * u) / norm
                 for axis, r, u in zip(direction, right, up))


class GunAim:
    # M2_lt.xml: hull turretPosition + stock turret gunPosition, in BW axes.
    TURRET = (.0043, 1.6865, -.0804)
    GUN = (.0247, .2062, .796)
    MIN_PITCH, MAX_PITCH = map(math.radians, (-19, 7))
    RATE = math.radians(45)
    REAR_ARC = math.radians(55)
    LIMIT_BLEND = .4  # Gun pitch-limit component +0x2c, radians.

    def __init__(self, config=None):
        # Defaults remain available for resource-free protocol tests.
        self.PITCH_RATE = self.RATE
        self.rear_limits = (self.MIN_PITCH,0.,self.REAR_ARC)
        self.yaw_limits = (-math.pi,math.pi)
        self.front_limits = None
        self.PACKED_MIN, self.PACKED_MAX = self.MIN_PITCH,self.MAX_PITCH
        if config is not None:
            self.TURRET, self.GUN = config.turret_position, config.gun_position
            self.MIN_PITCH, self.MAX_PITCH = config.pitch_limits
            self.RATE, self.PITCH_RATE = config.yaw_rate, config.pitch_rate
            self.rear_limits = config.rear_limits
            self.yaw_limits = config.yaw_limits
            self.front_limits = config.front_limits
            self.LIMIT_BLEND = config.pitch_transition
            self.PACKED_MIN, self.PACKED_MAX = config.packed_pitch_limits
        self.yaw = self.pitch = 0.0
        self.movement_dispersion = 1.
        self.aim_progress = 0.
        self.desired_yaw = self.desired_pitch = 0.0
        self.point = None
        self.angles_requested = False
        self.local = True
        # Diagnostic switch for the hull counter-rotation used while turning
        # a world-space aim point into turret-local yaw/pitch. By default the
        # existing behavior is preserved; set the env var to 1 to bypass it.
        self.hull_aim_compensation = os.environ.get(
            'SOLBREAKER_DISABLE_AIM_HULL_COMPENSATION', '').strip().lower() not in {
                '1', 'true', 'yes', 'on'}
        # Timed consumable traverse multipliers, neutral until a battle sets them.
        self.yaw_factor = self.pitch_factor = 1.

    @property
    def yaw_rate(self):
        """Effective yaw rate shared by server slewing and TargetingInfo.

        The XML rate is the only base traverse rate. Timed loadout and crew
        factors apply multiplicatively and are reported through TargetingInfo.
        """
        return self.RATE*self.yaw_factor

    @property
    def pitch_rate(self):
        """Effective gun vertical traverse shared by slewing and TargetingInfo."""
        return self.PITCH_RATE*self.pitch_factor

    def set_factors(self, yaw, pitch):
        """Set the timed traverse multipliers; True when an effective rate changed."""
        if not all(not isinstance(f, bool) and isinstance(f, (int, float)) and math.isfinite(f) and f > 0
                   for f in (yaw, pitch)):
            raise ValueError('Invalid aiming rate factors')
        before = self.yaw_rate, self.pitch_rate
        self.yaw_factor, self.pitch_factor = float(yaw), float(pitch)
        return (self.yaw_rate, self.pitch_rate) != before

    def set_point(self, point, local):
        if not all(math.isfinite(v) and abs(v) < 10000 for v in point):
            return False
        self.point, self.local = tuple(point), local
        return True

    def set_angles(self, yaw, pitch):
        if not all(math.isfinite(v) for v in (yaw, pitch)):
            return False
        self.point = None
        self.angles_requested = True
        self.desired_yaw, self.desired_pitch = wrap(yaw), pitch
        return True

    def update_dispersion(self, config, before, after, previous_turret, dt, aim_factor, *, previous_pitch=None):
        if not getattr(config, 'dispersion_radius', 0.):
            return
        x, _, z = before.position
        dx, _, dz = after.position
        speed = 3.6 * math.hypot(dx-x, dz-z) / dt
        hull_rate = math.degrees(wrap(after.yaw - before.yaw)) / dt
        turret_rate = math.degrees(wrap(self.yaw - previous_turret)) / dt
        # Ignore tiny suspension and turret corrections while the gun settles.
        pitch_delta = 0. if previous_pitch is None else self.pitch - previous_pitch
        tilt = max(abs(pitch_delta), abs(wrap(after.pitch - before.pitch)),
                   abs(wrap(after.roll - before.roll)))
        if (speed > .3 or abs(after.position[1] - before.position[1]) > .10 * dt
                or max(abs(hull_rate), abs(turret_rate), math.degrees(tilt) / dt) > 1.):
            self.aim_progress = 0.
        else:
            duration = FULL_SETTLE_TIME_CONSTANTS * settling_time(config, aim_factor)
            self.aim_progress = min(1., self.aim_progress + dt / duration)
        # Use horizontal vehicle speed (km/h) and local traverse (degrees/s).
        steady = math.hypot(1., speed * getattr(config, 'movement_dispersion', 0.),
                            hull_rate * getattr(config, 'hull_rotation_dispersion', 0.),
                            turret_rate * getattr(config, 'turret_rotation_dispersion', 0.))
        if steady >= self.movement_dispersion:
            self.movement_dispersion = steady
            return
        settle = settling_time(config, aim_factor)
        remaining = 1. + (self.movement_dispersion - 1.) * math.exp(-dt / settle)
        self.movement_dispersion = max(steady, remaining)

    def tick(self, dt, position, hull_yaw, hull_pitch=0., hull_roll=0., hull_omega=0.):
        """Slew one fixed simulation step at the current configured rates."""
        if self.point is not None:
            x, y, z = self.point
            if not self.local:
                x, y, z = (v - o for v, o in zip(self.point, position))
            # Cell method 4 removes only vehicle translation, not rotation:
            # 0x1e0f0fb reconstructs its world point with vector addition.
            if self.hull_aim_compensation:
                x, y, z = matrix(hull_yaw,hull_pitch,hull_roll).T @ (x,y,z)
            x, y, z = x-self.TURRET[0], y-self.TURRET[1], z-self.TURRET[2]
            radius = math.hypot(x, z)
            if radius > .1:
                self.desired_yaw = wrap(math.atan2(x, z) - math.asin(
                    max(-1, min(1, self.GUN[0] / radius))))
                forward = math.sqrt(max(0, radius*radius-self.GUN[0]**2))-self.GUN[2]
                self.desired_pitch = math.atan2(self.GUN[1]-y, forward)
        if self.yaw_limits == (-math.pi,math.pi):
            yaw_error = wrap(self.desired_yaw-self.yaw)
        else:
            # A limited mount cannot take the shorter route through its rear
            # stop. Both the target and the slewed angle stay in its XML sector.
            low, high = self.yaw_limits
            target_yaw = max(low,min(high,self.desired_yaw))
            yaw_error = target_yaw-self.yaw
        low, high = self.pitch_limits(self.yaw)
        target_pitch = max(low, min(high, self.desired_pitch))
        pitch_error = target_pitch-self.pitch
        # Native 0x1e1cf5f coordinates the axes to arrive together. Turning
        # each at full rate diverges from local prediction on diagonal aims.
        duration = max(abs(yaw_error)/self.yaw_rate if self.RATE else 0.,
                       abs(pitch_error)/self.pitch_rate if self.PITCH_RATE else 0.)
        fraction = min(1.,max(0.,dt)/duration) if duration else 0.
        if self.RATE:self.yaw += yaw_error*fraction
        self.yaw = wrap(self.yaw) if self.yaw_limits == (-math.pi,math.pi) else max(
            self.yaw_limits[0],min(self.yaw_limits[1],self.yaw))
        if self.PITCH_RATE:self.pitch += pitch_error*fraction
        low, high = self.pitch_limits(self.yaw)
        self.pitch = max(low, min(high, self.pitch))
        return False

    def pitch_limits(self, yaw):
        # M2 extraPitchLimits/back: -19 0 55. Native 0x70e190 holds a sector
        # inside its arc, then blends linearly to the nominal limits over the
        # transition. Front (centre 0) wins whole, blend included; back
        # (centre +-pi) is consulted only when front is inactive.
        distance = abs(wrap(yaw))
        for sector,offset in ((self.front_limits,distance),(self.rear_limits,math.pi-distance)):
            if sector is None:
                continue
            low,high,arc = sector
            if offset < arc:
                return low,high
            if offset < arc+self.LIMIT_BLEND:
                blend = (offset-arc)/self.LIMIT_BLEND
                return low+(self.MIN_PITCH-low)*blend,high+(self.MAX_PITCH-high)*blend
        return self.MIN_PITCH,self.MAX_PITCH

    def packed(self):
        yaw = int((self.yaw+math.pi)*1024/math.tau) & 1023
        pitch = round((self.pitch-self.PACKED_MIN)*63/(self.PACKED_MAX-self.PACKED_MIN))
        return struct.pack('<H', (yaw << 6) | max(0, min(63, pitch)))

    def ray(self, position, hull_yaw, hull_pitch=0., hull_roll=0.):
        return self.ray_rotation(position, matrix(hull_yaw,hull_pitch,hull_roll))

    def ray_rotation(self, position, rot):
        """Gun ray in an exact hull rotation, including a flipped rigid body."""
        return self._ray_for_angles(position, rot, self.yaw, self.pitch)

    def target_ray(self, position, rot, point=None):
        """Ray toward the requested aim, independent of physical gun traverse."""
        if point is None and self.point is not None:
            point = tuple(a + b for a, b in zip(position, self.point)) if self.local else self.point
        if point is not None:
            origin = tuple(map(float, np.asarray(position) + rot @ self.TURRET))
            delta = tuple(b - a for a, b in zip(origin, point))
            distance = math.hypot(*delta)
            if distance > 1e-6:
                return origin, tuple(v / distance for v in delta)
        if self.angles_requested:
            return self._ray_for_angles(position, rot, self.desired_yaw, self.desired_pitch)
        return self.ray_rotation(position, rot)

    def _ray_for_angles(self, position, rot, yaw, pitch):
        c, s = math.cos(yaw), math.sin(yaw)
        gx, gy, gz = self.GUN
        tx, ty, tz = self.TURRET
        x, y, z = tx+c*gx+s*gz, ty+gy, tz-s*gx+c*gz
        origin = np.asarray(position)+rot @ (x,y,z)
        direction = rot @ (s * math.cos(pitch), -math.sin(pitch), c * math.cos(pitch))
        return tuple(map(float,origin)), tuple(map(float,direction))
