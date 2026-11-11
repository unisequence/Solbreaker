"""Pure local fall-impact rule: impact class and max-HP damage fraction.

The coefficients below are a local, tunable gameplay rule. They are NOT the
exact client formula (which has not been recovered); they only give a
monotonic, energy-based (v^2) curve with a no-damage threshold.

Conventions (hull-local, BW axes: +y up): ``local_normal`` is the contact
surface normal pointing toward the hull (flat ground under the tank = +y).
``local_point`` is the contact point in the same frame.
"""
import math

BOTTOM, SIDE, ROOF = "bottom", "side", "roof"

# Classification: blend of normal verticality and contact height.
_NORMAL_WEIGHT = .5      # weight of normal.y; the rest is height score
_SCORE_LIMIT = .4        # |blend| needed for bottom/roof, else side
_MIN_VERTICAL = .3       # |normal.y| gate: a wall never counts as floor/roof
_UNIT_TOLERANCE = 1e-2   # accepted |normal| deviation from 1
_POINT_MARGIN = .05      # contact point may lie this fraction outside bounds

# Damage = scale * (v^2 - threshold^2) / REF_SPEED^2, clamped to 0..1.
# Thresholds (m/s) are lowest for the roof and highest for the tracked
# bottom, which is built to take landings; scales raise the fragile classes.
_REF_SPEED = 30.
_RULES = {
    BOTTOM: (9., 1.),
    SIDE: (7., 1.25),
    ROOF: (5., 1.5),
}


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a real number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def impact_damage_speed(point_closing, center_closing, effective_mass_fraction):
    """Return a local gameplay speed envelope for body-HP damage only.

    The isolated-contact energy estimate scales point closing speed by
    ``sqrt(effective_mass / body_mass)``. The positive COM closing speed
    preserves flat translational falls; the actual point closing speed bounds
    the result so rotation that cushions a contact cannot introduce new damage.

    This is a gameplay approximation, not exact multi-contact impact energy or
    the recovered client rule. It changes no physical velocity, force or track
    damage. Speeds are in m/s; point closing is nonnegative, COM closing may
    be signed, and the effective mass fraction must be in ``[0, 1]``.
    """
    point = _finite(point_closing, "point_closing")
    center = _finite(center_closing, "center_closing")
    fraction = _finite(effective_mass_fraction, "effective_mass_fraction")
    if point < 0.:
        raise ValueError("point_closing must be >= 0")
    if not 0. <= fraction <= 1.:
        raise ValueError("effective_mass_fraction must be in [0, 1]")
    return min(point, max(max(0., center), point * math.sqrt(fraction)))


def _vec3(value, name):
    try:
        items = tuple(value)
    except TypeError:
        raise TypeError(f"{name} must be a 3-sequence") from None
    if len(items) != 3:
        raise ValueError(f"{name} must have 3 components")
    return tuple(_finite(v, f"{name}[{i}]") for i, v in enumerate(items))


def _impact_class(point, normal, lo, hi):
    ny = normal[1]
    height = (point[1] - lo[1]) / (hi[1] - lo[1])
    low = 1. - 2. * min(1., max(0., height))  # +1 at hull bottom, -1 at top
    score = _NORMAL_WEIGHT * ny + (1. - _NORMAL_WEIGHT) * low
    if ny >= _MIN_VERTICAL and score >= _SCORE_LIMIT:
        return BOTTOM
    if ny <= -_MIN_VERTICAL and score <= -_SCORE_LIMIT:
        return ROOF
    return SIDE


def evaluate_fall_impact(closing_speed, local_point, local_normal, local_bounds):
    """Return ``(impact_class, hp_fraction)``.

    closing_speed: m/s along the normal, finite and >= 0.
    local_point / local_normal: hull-local 3-vectors; normal must be unit
    length (within 1%). local_bounds: ``(min_xyz, max_xyz)`` with
    min < max on every axis; the point may lie slightly outside it.
    impact_class is "bottom", "side" or "roof"; hp_fraction is in 0..1.
    """
    speed = _finite(closing_speed, "closing_speed")
    if speed < 0.:
        raise ValueError("closing_speed must be >= 0")
    point = _vec3(local_point, "local_point")
    normal = _vec3(local_normal, "local_normal")
    try:
        lo, hi = local_bounds
    except (TypeError, ValueError):
        raise TypeError("local_bounds must be a (min, max) pair") from None
    lo, hi = _vec3(lo, "local_bounds.min"), _vec3(hi, "local_bounds.max")
    for axis in range(3):
        if not lo[axis] < hi[axis]:
            raise ValueError("local_bounds must satisfy min < max per axis")
        slack = _POINT_MARGIN * (hi[axis] - lo[axis])
        if not lo[axis] - slack <= point[axis] <= hi[axis] + slack:
            raise ValueError("local_point lies outside local_bounds")
    length = math.sqrt(sum(c * c for c in normal))
    if abs(length - 1.) > _UNIT_TOLERANCE:
        raise ValueError("local_normal must be unit length")
    normal = tuple(c / length for c in normal)

    kind = _impact_class(point, normal, lo, hi)
    threshold, scale = _RULES[kind]
    excess = speed * speed - threshold * threshold
    fraction = 0. if excess <= 0. else scale * excess / (_REF_SPEED ** 2)
    return kind, min(1., fraction)
