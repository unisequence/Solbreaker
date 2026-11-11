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
    low, high = vertices.min(axis=0), vertices.max(axis=0)
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
    total = sum(part.mass for part in parts)
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Vehicle must have positive finite total mass")
    center = sum((part.mass * np.asarray(part.center) for part in parts), np.zeros(3)) / total
    inertia = np.zeros(3)
    for part in parts:
        delta = np.asarray(part.center) - center
        inertia += np.asarray(part.inertia_diagonal) + part.mass * np.array((
            delta[1] * delta[1] + delta[2] * delta[2],
            delta[0] * delta[0] + delta[2] * delta[2],
            delta[0] * delta[0] + delta[1] * delta[1],
        ))
    if not np.isfinite(center).all() or not np.isfinite(inertia).all():
        raise ValueError("Non-finite combined mass properties")
    return MassProperties(float(total), tuple(float(value) for value in center),
                          tuple(float(value) for value in inertia), parts)


def vehicle_mass_properties(definition, tank_shapes, turret_node):
    """Build the fallback from :class:`CollisionScene`'s actual mesh arrays.

    The collision scene exposes hull, two tracks and optionally a turret.  A
    chassis is split equally between the two actual track meshes.  Engine,
    fuel and radio are hull-mounted; the gun is turret-mounted.  Casemates
    have no separate turret mesh, so their declared turret and gun masses are
    assigned to their hull.  This is a documented mounting allocation, not a
    claim about centre-of-mass data from the client.
    """
    shapes = tuple(tank_shapes)
    names = tuple(name for name, _ in shapes)
    required = {"hull", "chassis_track_L", "chassis_track_R"}
    if turret_node is not None:
        required.add(turret_node)
    if set(names) != required or len(names) != len(required):
        raise ValueError("Unexpected selected vehicle collision shapes")
    components = definition
    masses = {
        "hull": (components.hull.mass + components.engine.mass + components.fuel.mass
                 + components.radio.mass),
        "chassis_track_L": components.chassis.mass * .5,
        "chassis_track_R": components.chassis.mass * .5,
    }
    adjusted = []
    for name, vertices in shapes:
        vertices = _vertices(vertices, name)
        if turret_node is not None and name == turret_node:
            masses[name] = components.turret.mass + components.gun.mass
            # CollisionWorld places this link at turret_position too.
            vertices = vertices + np.asarray(components.aiming.turret_position, dtype=float)
        adjusted.append((name, vertices))
    if turret_node is None:
        masses["hull"] += components.turret.mass + components.gun.mass
    return from_named_meshes(adjusted, masses)
