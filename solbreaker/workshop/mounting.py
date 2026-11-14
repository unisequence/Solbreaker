"""Estimate mounting clearance from the same armor meshes used in battle."""
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from .parts import collision_preview, donor_parts
from .project import PARTS

MOUNTS = {'turret': ('hull', 1, .55), 'gun': ('turret', 2, .18)}
MESH_NAMES = {'hull': 'hull', 'turret': 'turret_01', 'gun': 'gun_01'}
TOLERANCE = {'turret': .055, 'gun': .08}
MAX_CORRECTION = {'turret': .45, 'gun': .6}


@dataclass(frozen=True)
class MountFit:
    state: str
    position: tuple
    offset: float = 0.


def translate_mount(project, part, offset):
    """Move an attachment and the pivots mounted on it as one assembly."""
    points = {name: tuple(map(float, np.asarray(project.points[name])+offset))
              for name in PARTS[PARTS.index(part):]}
    if any(abs(value) > 50 for point in points.values() for value in point):
        return False
    project.points.update(points)
    return True


def surface(mesh, probes, axis, side):
    """Intersect parallel probe lines and return the nearest facing surface."""
    plane = [index for index in range(3) if index != axis]
    low, high = probes.min(axis=0), probes.max(axis=0)
    best = np.full(len(probes), -np.inf)
    for start_index in range(0, len(mesh), 2048):
        batch = mesh[start_index:start_index+2048]
        xy = batch[:, :, plane]
        start = xy[:, 0]
        u, v = xy[:, 1]-start, xy[:, 2]-start
        area = u[:, 0]*v[:, 1]-u[:, 1]*v[:, 0]
        valid = ((np.abs(area) > 1e-8) & (xy.max(axis=1) >= low).all(axis=1)
                 & (xy.min(axis=1) <= high).all(axis=1))
        start, u, v, area = start[valid], u[valid], v[valid], area[valid]
        depth = batch[valid, :, axis]
        delta = probes[:, None]-start
        beta = (delta[..., 0]*v[:, 1]-delta[..., 1]*v[:, 0])/area
        gamma = (delta[..., 1]*u[:, 0]-delta[..., 0]*u[:, 1])/area
        hits = (beta >= -1e-6) & (gamma >= -1e-6) & (beta+gamma <= 1.000001)
        values = depth[:, 0]+beta*(depth[:, 1]-depth[:, 0])+gamma*(depth[:, 2]-depth[:, 0])
        distance = np.max(np.where(hits, side*values, -np.inf), axis=1, initial=-np.inf)
        np.maximum(best, distance, out=best)
    return side*best


def joint_clearance(parent, child, origin, axis, extent):
    if parent is None or child is None or not len(parent) or not len(child):
        return None
    plane = [index for index in range(3) if index != axis]
    grid = np.linspace(-extent, extent, 7)
    probes = origin[plane]+np.stack(np.meshgrid(grid, grid), axis=-1).reshape(-1, 2)
    back = surface(child, probes, axis, -1)
    front = surface(parent, probes, axis, 1)
    valid = np.isfinite(back) & np.isfinite(front)
    if np.count_nonzero(valid) < 5:
        return None
    clearance = back[valid]-front[valid]
    # A low percentile describes the nearest broad contact, ignoring isolated triangles.
    rank = len(clearance)//4
    index = np.argsort(clearance)[rank]
    point = origin.copy()
    point[plane] = probes[valid][index]
    point[axis] = (back[valid][index]+front[valid][index])*.5
    return float(clearance[index]), tuple(point)


@lru_cache(maxsize=40)
def stock_clearance(game, key, part):
    host, axis, extent = MOUNTS[part]
    _, hit, _, names, _, _, stock = donor_parts(game, key)
    meshes = collision_preview(hit)
    if names[host] not in meshes or names[part] not in meshes:
        return None
    reference = joint_clearance(meshes[names[host]]+stock[host], meshes[names[part]]+stock[part],
                                stock[part], axis, extent)
    return reference[0] if reference is not None else None


def check_mount(game, project, part, collision):
    host, axis, extent = MOUNTS[part]
    points = {name: np.asarray(point) for name, point in project.points.items()}
    parent = collision.get(MESH_NAMES[host])
    child = collision.get(MESH_NAMES[part])
    if parent is None or child is None:
        return MountFit('unknown', tuple(points[part]))
    actual = joint_clearance(parent+points[host], child+points[part], points[part], axis, extent)
    key = project.part_donors.get(part) or (project.base_vehicle if project.stock_model else None)
    reference = stock_clearance(game, key, part) if game is not None and key else None
    if actual is None:
        return MountFit('unknown', tuple(points[part]))
    if reference is None:
        # Authored gun mounts can leave room for the mask, so no automatic
        # depth adjustment without a comparable stock reference.
        if part == 'gun':
            return MountFit('unknown', tuple(points[part]))
        reference = 0.
    gap, point = actual
    excess = gap-reference
    if abs(excess) <= TOLERANCE[part]:
        return MountFit('seated', point)
    return MountFit('gap' if excess > 0 else 'overlap', point, -excess)


def barrel_in_hull(project, visible, collision):
    hull, gun = collision.get('hull'), visible.get('gun_01')
    if hull is None or gun is None or not len(hull) or not len(gun):
        return None
    muzzle, pivot = (np.asarray(project.points[name]) for name in ('muzzle', 'gun'))
    reach = muzzle[2]-pivot[2]
    if reach < .6:
        return None
    # Use real barrel faces away from the masked breech. Sample a bounded
    # number so that dragging through a hull does not stall the viewport.
    barrel = gun.reshape(-1, 3)
    barrel = barrel[(barrel[:, 2] > pivot[2]+max(.45, reach*.22)) & (barrel[:, 2] < muzzle[2]+.15)]
    if len(barrel) < 3:
        return None
    probes = barrel[::max(1, (len(barrel)+63)//64)]
    hull = hull+project.points['hull']
    roof = surface(hull, probes[:, [0, 2]], 1, 1)
    floor = surface(hull, probes[:, [0, 2]], 1, -1)
    hits = probes[(probes[:, 1] < roof-.03) & (probes[:, 1] > floor+.02)]
    if len(hits) < 3:
        return None
    return tuple(hits[len(hits)//2])


def seat_mount(game, project, part, collision):
    fit = check_mount(game, project, part, collision)
    if fit.state in ('gap', 'overlap') and abs(fit.offset) <= MAX_CORRECTION[part]:
        shift = np.zeros(3)
        shift[MOUNTS[part][1]] = fit.offset
        return translate_mount(project, part, shift)
    return False
