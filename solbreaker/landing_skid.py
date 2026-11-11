"""Brief post-flight side skid for an intact tracked hull.

This is LOCAL tuning, not a recovered client value. A hull that returns to
its tracks after a real flight keeps the sideways part of its touchdown
velocity for a moment: track side grip starts low and blends smoothly back
to the ordinary limit. The episode only removes lateral momentum; it never
adds speed or yaw, and it ends once the motion follows the hull heading.
"""
from dataclasses import dataclass
import math


# Shorter flights are crest/contact chatter rather than a jump.
MIN_AIRTIME = .2
# Below this sideways touchdown speed the ordinary grip is indistinguishable.
MIN_LATERAL_SPEED = .35
# Side grip fraction at touchdown and the smooth return to full grip.
INITIAL_GRIP = .2
GRIP_RECOVERY = .5
# Full grip always ends the episode; this only bounds pathological input.
MAX_DURATION = 2.


@dataclass
class LandingSkid:
    velocity: tuple[float, float]
    elapsed: float = 0.


def lateral_speed(velocity, yaw):
    return velocity[0]*math.cos(yaw)-velocity[1]*math.sin(yaw)


def touchdown_skid(velocity, yaw, airtime):
    """Return a skid for a meaningful flight landing sideways, else ``None``."""
    if (airtime < MIN_AIRTIME or not all(math.isfinite(v) for v in velocity)
            or abs(lateral_speed(velocity, yaw)) < MIN_LATERAL_SPEED):
        return None
    return LandingSkid((float(velocity[0]), float(velocity[1])))


def grip_fraction(elapsed):
    """Smoothstep from INITIAL_GRIP to full side grip over GRIP_RECOVERY."""
    t = max(0., min(1., elapsed/GRIP_RECOVERY))
    return INITIAL_GRIP+(1.-INITIAL_GRIP)*t*t*(3.-2.*t)
