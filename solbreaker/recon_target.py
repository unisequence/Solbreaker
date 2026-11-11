"""Experimental proximity candidates, not visibility or a camera ray.

An aim point close to armor does not prove what is under the crosshair: poses
may be stale and terrain or other vehicles may occlude it. No network or game
integration should treat this result as a confirmed target.
"""
from dataclasses import dataclass
import math

import numpy as np

from .armor import _closest_points, _validated_rotation
from .orientation import matrix


@dataclass(frozen=True)
class TargetGeometry:
    entity_id: int
    armor: object | None
    pose: object | None
    turret_yaw: float | None
    gun_pitch: float | None
    rotation: object | None = None
    movable_state: object | None = None


@dataclass(frozen=True)
class ProximityCandidate:
    entity_id: int
    distance: float
    point: tuple[float, float, float]
    part: str


@dataclass(frozen=True)
class ProximityResult:
    # All candidates within the requested tolerance, ordered by distance.
    candidates: tuple[ProximityCandidate, ...]
    # A missing target cannot be treated as a negative visibility result.
    unavailable: tuple[int, ...]


def candidates_near_aim_point(aim_point, targets, *, max_distance):
    """Compare world-space BW aim point to current armor, within <=5 m.

    The supplied pose and movable snapshot must be sampled by the caller; no
    interpolation, LOS check, occlusion test or target selection is performed.
    Returns every candidate, including ambiguous overlaps, and unavailable IDs.
    The tolerance is caller supplied and deliberately has no default.
    """
    try:
        point = np.asarray(aim_point, dtype=float)
        limit = float(max_distance)
    except (ValueError, TypeError):
        raise ValueError('Invalid aim point or distance') from None
    if point.shape != (3,) or not np.all(np.isfinite(point)):
        raise ValueError('Invalid aim point')
    if not math.isfinite(limit) or not 0 <= limit <= 5:
        raise ValueError('max_distance must be finite and between 0 and 5 metres')

    found, unavailable, seen = [], [], set()
    for target in targets:
        if target.entity_id in seen:
            raise ValueError('Duplicate target entity')
        seen.add(target.entity_id)
        if (target.armor is None or target.pose is None or
                target.turret_yaw is None or target.gun_pitch is None):
            unavailable.append(target.entity_id)
            continue
        try:
            position = np.asarray(target.pose.position, dtype=float)
            if position.shape != (3,) or not np.all(np.isfinite(position)):
                raise ValueError('Invalid pose')
            angles = (target.pose.yaw, target.pose.pitch, target.pose.roll,
                      target.turret_yaw, target.gun_pitch)
            if not all(math.isfinite(x) for x in angles):
                raise ValueError('Invalid angles')
            parts = target.armor._parts_for_query(target.movable_state)
            rot = _validated_rotation(target.pose, target.rotation)
            # ArmorModel.radius is a conservative bound for every turret,
            # gun and movable-armor state. Skip only when that bound proves
            # the point is too far away; synthetic models without it fall
            # back to the exact triangle scan.
            radius = getattr(target.armor, 'radius', None)
            if (isinstance(radius, (int, float)) and not isinstance(radius, bool)
                    and math.isfinite(radius) and radius >= 0
                    and np.linalg.norm(point-position) > radius + limit):
                continue
            local = (point-position) @ rot
            turret = matrix(target.turret_yaw, 0, 0)
            best = None
            for name, triangles, _ids, _thickness, _factors in parts:
                if name.startswith('turret'):
                    part_rot, offset = turret, target.armor.turret_position
                elif name.startswith('gun'):
                    part_rot = turret @ matrix(0, target.gun_pitch, 0)
                    offset = target.armor.turret_position + turret @ target.armor.gun_position
                else:
                    part_rot, offset = np.eye(3), np.zeros(3)
                part_point = (local-offset) @ part_rot
                closest = _closest_points(part_point, triangles)
                distances = np.linalg.norm(closest-part_point, axis=1)
                if not len(distances):
                    continue
                index = int(np.argmin(distances))
                distance = float(distances[index])
                if best is None or distance < best[0]:
                    world_point = (closest[index] @ part_rot.T + offset) @ rot.T + position
                    best = (distance, tuple(map(float, world_point)), name)
            if best is None:
                unavailable.append(target.entity_id)
            elif best[0] <= limit:
                found.append(ProximityCandidate(target.entity_id, *best))
        except (ValueError, TypeError, AttributeError):
            unavailable.append(target.entity_id)
    found.sort(key=lambda candidate: (candidate.distance, candidate.entity_id))
    return ProximityResult(tuple(found), tuple(unavailable))
