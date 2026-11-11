"""Pure local mobility policy: terrain resistance, engine and rolling limits.

``Drive.terrain_resistance`` is ordered (firm, medium, soft) and is already
modified by crew/loadout. The XML values are dimensionless resistance factors,
not friction or SI rolling coefficients. MKM0..3 are effect palette slots and
are never mobility classes; an unknown surface explicitly falls back to
medium. ``rolling_scale`` is a local calibration, not a recovered constant.
"""
import math
from dataclasses import dataclass

FIRM, MEDIUM, SOFT = 0, 1, 2


@dataclass(frozen=True)
class MobilityConfig:
    max_acceleration: float = 5.    # m/s^2 cap
    virtual_gear_speed: float = 4.  # m/s, floor on speed for P/(m*v)
    rolling_scale: float = .02      # local calibration

    def __post_init__(self):
        for name in ('max_acceleration', 'virtual_gear_speed', 'rolling_scale'):
            _positive(getattr(self, name), name)


@dataclass(frozen=True)
class TerrainMobility:
    resistance: float
    turn_factor: float
    surface_index: int
    source: str


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a real number')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f'{name} must be finite')
    return value


def _positive(value, name):
    value = _number(value, name)
    if value <= 0:
        raise ValueError(f'{name} must be positive')
    return value


DEFAULT_MOBILITY_CONFIG = MobilityConfig()


def resolve_terrain_mobility(resistances, surface_index=None):
    """Pick resistance for a (firm, medium, soft) surface index.

    ``surface_index`` None means unknown and explicitly selects medium.
    """
    try:
        values = tuple(resistances)
    except TypeError:
        raise ValueError('resistances must be a sequence of 3 numbers') from None
    if len(values) != 3:
        raise ValueError('resistances must contain exactly 3 values')
    values = tuple(_positive(v, 'resistance') for v in values)
    if surface_index is None:
        index, source = MEDIUM, 'fallback_medium'
    else:
        if isinstance(surface_index, bool) or not isinstance(surface_index, int):
            raise ValueError('surface_index must be an int or None')
        if not FIRM <= surface_index <= SOFT:
            raise ValueError('surface_index must be 0..2 or None')
        index, source = surface_index, 'explicit'
    resistance = values[index]
    return TerrainMobility(resistance, min(1., values[FIRM] / resistance),
                           index, source)


def engine_acceleration(power_watts, mass, surface_speed, resistance=1.,
                        config=DEFAULT_MOBILITY_CONFIG):
    """Unsigned engine acceleration limit in m/s^2.

    Resistance below 1 never yields more than the installed power.
    """
    power = _positive(power_watts, 'power_watts')
    mass = _positive(mass, 'mass')
    speed = abs(_number(surface_speed, 'surface_speed'))
    resistance = _positive(resistance, 'resistance')
    efficiency = min(1., 1. / resistance)
    reference = max(speed, config.virtual_gear_speed)
    return min(config.max_acceleration, power * efficiency / (mass * reference))


def rolling_acceleration(resistance, pitch, gravity=9.81,
                         config=DEFAULT_MOBILITY_CONFIG):
    """Positive horizontal rolling-resistance magnitude in m/s^2.

    The caller opposes motion and clamps at zero speed.
    """
    resistance = _positive(resistance, 'resistance')
    pitch = _number(pitch, 'pitch')
    gravity = _positive(gravity, 'gravity')
    return config.rolling_scale * resistance * gravity * math.cos(pitch) ** 2
