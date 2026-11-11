"""Passive broken-belt contact policy for the local tracked controller.

The native client proves the broken-module state, not these force constants.
This is LOCAL tuning: a torn belt disables powered drive, but ground contact
must dissipate momentum rather than deleting it. One-sided friction couples
longitudinal motion and yaw; it cannot supply kinetic energy or spin at rest.
The 6DOF solver remains the sole owner of airborne/roll/pitch motion.
"""
from dataclasses import dataclass
import math


# Metres and seconds. The effective lever/inertia describe a modest tank;
# they deliberately do not pretend to recover the native chassis controller.
LEVER = 1.2
YAW_RADIUS_SQUARED = 4.
SIDE_DRAG_RATE = .65
MAX_SIDE_DECELERATION = 3.25
COAST_DRAG_RATE = .18
BOTH_DRAG_RATE = .85
YAW_DAMPING = 1.5
REPAIRED_YAW_DAMPING = 6.
MAX_CONTACT_STEP = 1. / 240.


@dataclass
class TrackDriveMotion:
    velocity: tuple[float, float]
    yaw_rate: float


@dataclass(frozen=True)
class TrackDriveStep:
    speed: float
    yaw_rate: float
    yaw_delta: float


def passive_track_step(speed, yaw_rate, left, right, dt):
    """Integrate passive longitudinal/yaw contact, with bounded side force.

    A left contact's slip is ``speed + LEVER*yaw_rate`` (positive yaw turns
    right). Its rank-one drag has an exact exponential update and decreases
    ``speed**2 + YAW_RADIUS_SQUARED*yaw_rate**2``. Symmetric damping is split
    around that update; small fixed slices keep the force cap independent of
    the caller's usual 30/60/120 Hz tick. Repair keeps yaw inertia but removes
    all extra longitudinal drag immediately.
    """
    if (not all(math.isfinite(v) for v in (speed, yaw_rate, dt))
            or not 0. < dt <= 1.):
        raise ValueError('Track contact requires finite motion and 0 < dt <= 1')
    if left == right:
        rate = BOTH_DRAG_RATE if left else 0.
        damping = YAW_DAMPING if left else REPAIRED_YAW_DAMPING
        angular_factor = math.exp(-damping*dt)
        return TrackDriveStep(speed*math.exp(-rate*dt), yaw_rate*angular_factor,
                              yaw_rate*(-math.expm1(-damping*dt))/damping)
    lever = LEVER if left else -LEVER
    coupling = 1.+lever*lever/YAW_RADIUS_SQUARED
    steps = max(1, math.ceil(dt/MAX_CONTACT_STEP-1e-10))
    h = dt/steps
    linear_factor = math.exp(-COAST_DRAG_RATE*h*.5)
    angular_factor = math.exp(-YAW_DAMPING*h*.5)
    angle = 0.
    for _ in range(steps):
        old_yaw_rate = yaw_rate
        speed *= linear_factor
        yaw_rate *= angular_factor
        slip = speed+lever*yaw_rate
        rate = min(SIDE_DRAG_RATE, MAX_SIDE_DECELERATION/max(abs(slip), 1e-12))
        delta = slip*math.expm1(-rate*coupling*h)/coupling
        speed += delta
        yaw_rate += lever/YAW_RADIUS_SQUARED*delta
        speed *= linear_factor
        yaw_rate *= angular_factor
        angle += (old_yaw_rate+yaw_rate)*.5*h
    return TrackDriveStep(speed, yaw_rate, angle)
