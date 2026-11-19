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
STEERING_GRIP_FACTOR = .56  # Local calibration for sustained track steering.


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


UNAVAILABLE_MOBILITY = TerrainMobility(1., 1., 1, 'unavailable')


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


def terrain_at(terrain, definition, position):
    drive = getattr(definition, 'drive', None)
    coefficients = getattr(drive, 'terrain_resistance', None)
    if coefficients is None:
        return UNAVAILABLE_MOBILITY
    sampler = getattr(terrain, 'mobility_index', None)
    index = sampler(position[0], position[2]) if callable(sampler) else None
    try:
        fallback = resolve_terrain_mobility(coefficients)
    except ValueError:
        return UNAVAILABLE_MOBILITY
    if index is None:
        return fallback
    try:
        return resolve_terrain_mobility(coefficients, index)
    except ValueError:
        return fallback


def powered_acceleration(definition, speed, resistance, factor):
    drive = getattr(definition, 'drive', None)
    mass = getattr(definition, 'mass', None)
    power_hp = getattr(drive, 'engine_power_hp', None)
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) and value > 0. for value in (mass, power_hp, factor)):
        return None
    return engine_acceleration(power_hp * 735.49875 * factor, mass, speed, resistance)


def braking_acceleration(definition):
    drive = getattr(definition, 'drive', None)
    mass = getattr(definition, 'mass', None)
    brake_force = getattr(drive, 'brake_force', None)
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) and value > 0. for value in (mass, brake_force)):
        return 12.
    # The pinned catalog also contains sentinel forces far outside this
    # range. Use the default brake response for those definitions.
    deceleration = 9.81 * brake_force / mass
    return deceleration if 1. <= deceleration <= 20. else 12.


def slope_grip_acceleration(assets, pitch, gravity, grip_factor=.8):
    limit = getattr(assets, 'max_slope', None)
    if (not isinstance(limit, (int, float)) or isinstance(limit, bool)
            or not math.isfinite(limit) or not 0 < limit < math.pi/2
            or not math.isfinite(pitch) or not math.isfinite(gravity) or gravity <= 0):
        return None
    grip = math.tan(limit) * grip_factor
    if abs(pitch) <= math.atan(grip):
        return None
    return gravity * grip * math.cos(pitch)**2


def advance_speed(speed, target, acceleration, slope, rolling_step, dt):
    """Return horizontal speed and the traction applied during this tick."""
    external = speed + slope * dt
    external -= math.copysign(min(abs(external), rolling_step), external)
    traction = max(-acceleration*dt, min(acceleration*dt, target-external))
    return external + traction, traction / dt


def skid_velocity(previous, yaw, speed, grip_step):
    """Apply lateral track grip while preserving the remaining momentum."""
    fx, fz = math.sin(yaw), math.cos(yaw)
    along = previous[0]*fx + previous[1]*fz
    lateral_x, lateral_z = previous[0]-along*fx, previous[1]-along*fz
    lateral = math.hypot(lateral_x, lateral_z)
    residual = max(0., lateral-grip_step)
    if lateral > 1e-8:
        factor = residual/lateral
        lateral_x, lateral_z = lateral_x*factor, lateral_z*factor
    vx, vz = speed*fx+lateral_x, speed*fz+lateral_z
    # Turning alone cannot increase the available speed.
    limit = max(math.hypot(*previous), abs(speed))
    magnitude = math.hypot(vx, vz)
    if magnitude > limit:
        vx, vz = vx*limit/magnitude, vz*limit/magnitude
    return (vx, vz), residual


def steering_skid(previous, yaw, yaw_delta, speed, grip_step, terrain_turn_factor):
    # Align momentum to the hull at the end of the step. Using half the turn
    # misses the lateral demand that causes slip at high speed.
    return skid_velocity(previous, yaw + yaw_delta, speed,
                         grip_step * STEERING_GRIP_FACTOR * terrain_turn_factor)
