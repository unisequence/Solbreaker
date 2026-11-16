"""Deterministic approximate mass properties from the collision meshes loaded.

This module has no Bullet dependency. Its results feed the opt-in experimental
airborne rigid body; the grounded tracked drive still uses its separate
longitudinal model.

Vehicle ``.sc2`` collision meshes are surfaces.  The loader used by the
server supplies named vertex arrays, but not a proof that a mesh is closed and
manifold.  Treating an arbitrary surface as a volume would make mass depend
on triangle winding and on holes.  We therefore *always* use the explicit
``aabb_uniform`` fallback: each named part is a uniform solid whose bounds
come from the vertices of that part.  The selected method is retained on
every result so replacing it with a verified watertight-volume calculation is
an intentional, observable change rather than an implicit physics guess.

Only declared XML component masses are used, allocated across hull, tracks,
and turret using the documented mounting assumptions below. In particular,
this module does not look at DAVA ``physicsShape`` data, which has not been
established as the pinned client's vehicle mass representation.
"""
from dataclasses import dataclass
import math
from typing import Iterable, Mapping

import numpy as np

from .orientation import matrix


AABB_UNIFORM = "aabb_uniform"


@dataclass(frozen=True)
class PartMassProperties:
    """Mass approximation for one named collision-mesh part.

    ``method`` is deliberately per part.  It records the fallback selected
    for open or nonmanifold input rather than concealing it behind a vehicle
    wide flag.
    """
    name: str
    mass: float
    center: tuple[float, float, float]
    inertia_diagonal: tuple[float, float, float]
    bounds: tuple[tuple[float, float, float], tuple[float, float, float]]
    method: str = AABB_UNIFORM


@dataclass(frozen=True)
class MassProperties:
    """Combined local-frame approximation, around its centre of mass."""
    mass: float
    center_of_mass: tuple[float, float, float]
    inertia_diagonal: tuple[float, float, float]
    parts: tuple[PartMassProperties, ...]


def _finite_mass(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Invalid mass for {name}")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Invalid mass for {name}")
    return value


def _vertices(value, name):
    vertices = np.asarray(value, dtype=float)
    if vertices.ndim != 2 or vertices.shape[1:] != (3,) or not len(vertices):
        raise ValueError(f"Invalid vertices for {name}")
    if not np.isfinite(vertices).all():
        raise ValueError(f"Non-finite vertices for {name}")
    return vertices


def aabb_part(name, vertices, mass):
    """Approximate a named mesh as a uniform solid axis-aligned box.

    This is intentionally public so call sites with a separately known mesh
    layout cannot accidentally pick a different fallback.  Degenerate bounds
    are accepted: their corresponding cuboid inertia terms are zero.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("Invalid mass-property part name")
    vertices, mass = _vertices(vertices, name), _finite_mass(mass, name)
    # Store each axis contiguously so both reductions can scan whole rows.
    # Rotated turret meshes can have thousands of vertices per launch.
    axes = np.ascontiguousarray(vertices.T)
    low, high = axes.min(axis=1), axes.max(axis=1)
    center = (low + high) * .5
    dx, dy, dz = high - low
    inertia = mass / 12. * np.array((dy * dy + dz * dz,
                                     dx * dx + dz * dz,
                                     dx * dx + dy * dy))
    return PartMassProperties(
        name=name,
        mass=mass,
        center=tuple(float(value) for value in center),
        inertia_diagonal=tuple(float(value) for value in inertia),
        bounds=(tuple(float(value) for value in low),
                tuple(float(value) for value in high)),
    )


def from_named_meshes(named_meshes: Iterable[tuple[str, object]], component_masses: Mapping[str, float]):
    """Combine named actual mesh arrays and their declared masses.

    Names must match exactly, preventing a newly introduced collision part
    from silently losing mass.  Zero-mass parts remain represented in
    ``parts`` for auditability, but the complete vehicle must have positive
    declared mass.
    """
    if not isinstance(component_masses, Mapping):
        raise ValueError("Component masses must be a mapping")
    meshes = tuple(named_meshes)
    names = tuple(name for name, _ in meshes)
    if (not meshes or len(set(names)) != len(names)
            or set(names) != set(component_masses)):
        raise ValueError("Mesh names and component masses must match exactly")
    parts = tuple(aabb_part(name, vertices, component_masses[name]) for name, vertices in meshes)
    return _combine_parts(parts)


def _combine_parts(parts):
    total = sum(part.mass for part in parts)
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Vehicle must have positive finite total mass")
    cx = cy = cz = 0.
    for part in parts:
        x, y, z = part.center
        cx += part.mass * x
        cy += part.mass * y
        cz += part.mass * z
    center = cx / total, cy / total, cz / total
    ix = iy = iz = 0.
    for part in parts:
        x, y, z = part.center
        dx, dy, dz = x - center[0], y - center[1], z - center[2]
        px, py, pz = part.inertia_diagonal
        ix += px + part.mass * (dy * dy + dz * dz)
        iy += py + part.mass * (dx * dx + dz * dz)
        iz += pz + part.mass * (dx * dx + dy * dy)
    inertia = ix, iy, iz
    if not all(map(math.isfinite, (*center, *inertia))):
        raise ValueError("Non-finite combined mass properties")
    return MassProperties(total, center, inertia, parts)


class VehicleMassModel:
    """Build the fallback from :class:`CollisionScene`'s actual mesh arrays.

    The collision scene exposes hull, two tracks and optionally a turret.  A
    chassis is split equally between the two actual track meshes.  Engine,
    fuel and radio are hull-mounted; the gun is turret-mounted.  Casemates
    have no separate turret mesh, so their declared turret and gun masses are
    assigned to their hull.  This is a documented mounting allocation, not a
    claim about centre-of-mass data from the client.
    """

    def __init__(self, definition, tank_shapes, turret_node):
        shapes = tuple(tank_shapes)
        names = tuple(name for name, _ in shapes)
        required = {"hull", "chassis_track_L", "chassis_track_R"}
        if turret_node is not None:
            required.add(turret_node)
        if set(names) != required or len(names) != len(required):
            raise ValueError("Unexpected selected vehicle collision shapes")
        masses = {
            "hull": (definition.hull.mass + definition.engine.mass + definition.fuel.mass
                     + definition.radio.mass),
            "chassis_track_L": definition.chassis.mass * .5,
            "chassis_track_R": definition.chassis.mass * .5,
        }
        turret_mass = definition.turret.mass + definition.gun.mass
        self._turret = None
        if turret_node is None:
            masses["hull"] += turret_mass
        parts = []
        for name, vertices in shapes:
            if name == turret_node:
                pivot = np.asarray(definition.aiming.turret_position, dtype=float)
                vertices = _vertices(vertices, name)
                self._turret = (len(parts), name, vertices, pivot, turret_mass)
                parts.append(aabb_part(name, vertices + pivot, turret_mass))
            else:
                parts.append(aabb_part(name, vertices, masses[name]))
        self._parts = tuple(parts)
        self._aligned = _combine_parts(self._parts)

    def at_yaw(self, yaw):
        if self._turret is None or yaw == 0.:
            return self._aligned
        index, name, vertices, pivot, mass = self._turret
        part = aabb_part(name, vertices @ matrix(yaw).T + pivot, mass)
        return _combine_parts((*self._parts[:index], part, *self._parts[index+1:]))


def vehicle_mass_properties(definition, tank_shapes, turret_node):
    return VehicleMassModel(definition, tank_shapes, turret_node).at_yaw(0.)
