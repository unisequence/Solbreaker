"""Small collision primitives derived from vehicle component dimensions.

Armor geometry and mass inputs stay with their original owners. These parts
contain newly generated contact geometry, independent of source tessellation.
Prism vertices use the existing component-local frame, including their centre;
box centres are explicit. A turret's XML joint offset is applied by the caller.
"""
from dataclasses import dataclass
import math

import numpy as np


@dataclass(frozen=True)
class ProxyPart:
    name: str
    kind: str
    center: tuple
    half_extents: tuple
    vertices: np.ndarray | None = None
    radius: float = 0.
    length: float = 0.


def _vector(values, length, message):
    try:
        if np.iscomplexobj(values):
            raise ValueError(message)
        result = np.asarray(values, dtype=float)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(message) from error
    if result.shape != (length,) or not np.isfinite(result).all():
        raise ValueError(message)
    return result


def _component(vertices):
    try:
        if np.iscomplexobj(vertices):
            raise ValueError('Invalid vehicle proxy geometry')
        vertices = np.asarray(vertices, dtype=float)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError('Invalid vehicle proxy geometry') from error
    if (vertices.ndim != 2 or vertices.shape[1:] != (3,) or len(vertices) < 4
            or not np.isfinite(vertices).all()):
        raise ValueError('Invalid vehicle proxy geometry')
    low, high = vertices.min(axis=0), vertices.max(axis=0)
    with np.errstate(over='ignore', invalid='ignore'):
        extent = high-low
        center = low+extent*.5
        half = extent*.5
    if (not np.isfinite(extent).all() or np.any(extent <= 0.)
            or not np.isfinite(center).all() or np.any(half <= 0.)):
        raise ValueError('Degenerate vehicle proxy geometry')
    return vertices, low, high, tuple(float(v) for v in center), tuple(float(v) for v in half)


def _lower_envelope(vertices):
    """Return the convex lower longitudinal outline, without mesh faces."""
    order = np.lexsort((vertices[:, 1], vertices[:, 2]))
    points = vertices[order][:, (2, 1)]
    # At a vertical end, only the lowest vertex belongs to the floor.
    points = points[np.r_[True, points[1:, 0] != points[:-1, 0]]]
    z_scale = float(np.ptp(points[:, 0]))
    y_scale = float(np.ptp(points[:, 1])) or 1.
    tolerance = 64.*np.finfo(float).eps
    lower = []
    for point in points:
        while len(lower) >= 2:
            a, b = lower[-2], lower[-1]
            cross = ((b[0]-a[0])/z_scale)*((point[1]-a[1])/y_scale)-((b[1]-a[1])/y_scale)*((point[0]-a[0])/z_scale)
            if cross > tolerance:
                break
            lower.pop()
        lower.append(point)
    return np.asarray(lower)


def _prism(vertices, low, high):
    lower = _lower_envelope(vertices)
    floor = float(low[1])
    tolerance = 64.*np.finfo(float).eps*max(1., float(high[1]-low[1]))
    flat = lower[np.abs(lower[:, 1]-floor) <= tolerance]
    if not len(flat):
        raise ValueError('Vehicle proxy has no lower carrying outline')
    # End height, beginning/end of the lowest floor, and end height retain
    # front/rear upsweep with at most four lower points. The two upper corners
    # close one convex polygon, extruded across the original component width.
    candidates = [lower[0], (flat[0, 0], floor), (flat[-1, 0], floor), lower[-1],
                  (float(high[2]), float(high[1])), (float(low[2]), float(high[1]))]
    profile = []
    for point in candidates:
        point = tuple(float(value) for value in point)
        if not profile or point != profile[-1]:
            profile.append(point)
    if profile[-1] == profile[0]:
        profile.pop()
    result = np.asarray([(float(x), y, z) for x in (low[0], high[0]) for z, y in profile], dtype=float)
    if len(result) > 12 or len(profile) < 3 or not np.isfinite(result).all():
        raise ValueError('Invalid generated vehicle proxy')
    result.setflags(write=False)
    return result


def vehicle_proxies(tank_shapes, turret_node, support):
    """Create a prism for hull/tracks and a local box for each other part.

    Input order, component names and every component's min/max dimensions are
    preserved. Flat-bottomed source components produce rectangular prisms;
    rising ends produce bevels. ``support`` is the existing positive pair of
    carrying half-width/half-length, validated without changing its geometry.
    """
    support = _vector(support, 2, 'Invalid vehicle proxy support')
    if np.any(support <= 0.):
        raise ValueError('Invalid vehicle proxy support')
    if turret_node is not None and (not isinstance(turret_node, str) or not turret_node):
        raise ValueError('Invalid vehicle proxy turret node')
    parts, names = [], set()
    try:
        shapes = tuple(tank_shapes)
    except TypeError as error:
        raise ValueError('Invalid vehicle proxy components') from error
    for entry in shapes:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise ValueError('Invalid vehicle proxy component')
        name, source = entry
        if not isinstance(name, str) or not name or name in names:
            raise ValueError('Invalid vehicle proxy component name')
        names.add(name)
        vertices, low, high, center, half_extents = _component(source)
        if name == 'hull' or name.startswith('chassis_track_'):
            parts.append(ProxyPart(name, 'prism', center, half_extents, _prism(vertices, low, high)))
        else:
            parts.append(ProxyPart(name, 'box', center, half_extents))
    if 'hull' not in names or turret_node is not None and turret_node not in names:
        raise ValueError('Missing vehicle proxy component')
    return tuple(parts)


def swept_turret(part):
    """Bound every yaw of a local turret box by one Y-axis cylinder.

    The cylinder is centred on the local yaw pivot XZ=(0,0), even when the
    source box is offset. Its caller adds the turret's XML joint position.
    ``length`` is the full Y height; ``half_extents`` bounds the cylinder.
    """
    if not isinstance(part, ProxyPart) or part.kind != 'box':
        raise ValueError('Swept turret requires a box proxy')
    center = _vector(part.center, 3, 'Invalid turret proxy center')
    half = _vector(part.half_extents, 3, 'Invalid turret proxy dimensions')
    if np.any(half <= 0.):
        raise ValueError('Invalid turret proxy dimensions')
    radius = math.hypot(abs(float(center[0]))+float(half[0]),
                        abs(float(center[2]))+float(half[2]))
    length = float(half[1])*2.
    if not (math.isfinite(radius) and radius > 0. and math.isfinite(length) and length > 0.):
        raise ValueError('Invalid swept turret proxy')
    return ProxyPart(part.name, 'cylinder', (0., float(center[1]), 0.),
                     (radius, float(half[1]), radius), radius=radius, length=length)
