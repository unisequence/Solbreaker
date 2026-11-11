"""Pure local track fall rule: (impact speed, drop height) -> track HP fraction.

The constants below are a local, tunable gameplay approximation. They are NOT
the exact client formula (which has not been recovered).

Inputs describe one deduped contact episode of a single track, supplied by the
collision layer: ``speed`` is the normal impact speed in m/s and ``height`` the
drop height in m. Which track (left/right) was hit is decided upstream; this
module only maps numbers to a fraction of the track's max health.

Rule: ``fraction = height_term * speed_term`` clamped to ``0..1``.

* height_term is linear from 0 at ``free_height`` (ignorable undulation) to 1
  at ``break_height``.
* speed_term is a smoothstep from 0 at ``min_speed`` (cushioned or scraping
  contact) to 1 at ``full_speed``. A tall drop with a soft touchdown therefore
  cannot break the track, and a fast hit from a tiny height cannot either.
* results below ``min_fraction`` are road noise and become 0.

A fraction of 1.0 means the track is broken: the caller computes
``ceil(max_health * fraction)`` and applies it through ``TrackState.damage``,
which clamps at 0 HP and latches ``broken``. Lower fractions leave the track
mobile with reduced health (and unchanged repair/regen behavior).
"""
import math
from dataclasses import dataclass


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a real number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


@dataclass(frozen=True)
class TrackFallConfig:
    free_height: float = 1.5     # m; drops up to this never damage
    break_height: float = 7.    # m; height term reaches 1 (needs full speed)
    min_speed: float = 3.5       # m/s; at or below this the touchdown is soft
    full_speed: float = 10.      # m/s; speed term saturates (~5.1 m free fall)
    min_fraction: float = .05   # results below this are ignored as noise

    def __post_init__(self):
        for name in ('free_height', 'break_height', 'min_speed',
                     'full_speed', 'min_fraction'):
            value = _finite(getattr(self, name), name)
            if value < 0.:
                raise ValueError(f"{name} must be >= 0")
            object.__setattr__(self, name, value)
        if not self.free_height < self.break_height:
            raise ValueError("free_height must be < break_height")
        if not self.min_speed < self.full_speed:
            raise ValueError("min_speed must be < full_speed")
        if not self.min_fraction < 1.:
            raise ValueError("min_fraction must be < 1")


DEFAULT_TRACK_FALL_CONFIG = TrackFallConfig()


def evaluate_track_fall(speed, height, config=DEFAULT_TRACK_FALL_CONFIG):
    """Return the track HP damage as a fraction of max health, in ``0..1``.

    speed: normal impact speed in m/s, finite and >= 0.
    height: drop height in m, finite and >= 0.
    1.0 means the track breaks. Deterministic, monotonic non-decreasing in
    both arguments.
    """
    if not isinstance(config, TrackFallConfig):
        raise TypeError("config must be a TrackFallConfig")
    speed = _finite(speed, "speed")
    height = _finite(height, "height")
    if speed < 0.:
        raise ValueError("speed must be >= 0")
    if height < 0.:
        raise ValueError("height must be >= 0")

    height_term = ((height - config.free_height)
                   / (config.break_height - config.free_height))
    if height_term <= 0.:
        return 0.
    height_term = min(1.,height_term)
    x = (speed - config.min_speed) / (config.full_speed - config.min_speed)
    x = min(1., max(0., x))
    fraction = min(1., height_term * x * x * (3. - 2. * x))
    return 0. if fraction < config.min_fraction else fraction


def breaks_track(fraction):
    """True when a fraction from evaluate_track_fall breaks the track."""
    return _finite(fraction, "fraction") >= 1.
