"""Triangle armor queries, penetration and HE blast from installed tank assets."""
from dataclasses import dataclass, field, replace
from functools import lru_cache
import math
from pathlib import Path
import random

import numpy as np
from . import armor_native as _armor_native
from .vector_math import vector_length as _vector_length
import yaml

from .dava_scene import scene, geometry, active_nodes, components, world_vertices
from .dvpl import read
from .vehicle_data import (Shell, VehicleConfiguration, _asset_base, _load_vehicle,
                           load_armor_states, load_vehicle)
from .orientation import matrix


_CROSS_F64 = np.dtype('float64')


def _cross3(left,right):
    """Binary64 three-vector cross; other inputs retain NumPy dispatch."""
    if (type(left) is not np.ndarray or type(right) is not np.ndarray
            or left.dtype!=_CROSS_F64 or right.dtype!=_CROSS_F64
            or left.ndim not in (1,2) or right.ndim not in (1,2)
            or left.shape[-1]!=3 or right.shape[-1]!=3):
        return np.cross(left,right)
    if left.shape==right.shape:shape=left.shape
    elif left.ndim==1:shape=right.shape
    elif right.ndim==1:shape=left.shape
    else:return np.cross(left,right)
    cp=np.empty(shape,dtype=np.float64)
    a0,a1,a2=left[...,0],left[...,1],left[...,2]
    b0,b1,b2=right[...,0],right[...,1],right[...,2]
    cp0,cp1,cp2=cp[...,0],cp[...,1],cp[...,2]
    # Same separate multiply/subtract operations as installed numpy.cross.
    np.multiply(a1,b2,out=cp0)
    tmp=np.array(a2*b1)
    cp0-=tmp
    np.multiply(a2,b0,out=cp1)
    np.multiply(a0,b2,out=tmp)
    cp1-=tmp
    np.multiply(a0,b1,out=cp2)
    np.multiply(a1,b0,out=tmp)
    cp2-=tmp
    return cp


_ARMOR_NATIVE_CROSS = _cross3


def _validated_rotation(pose, override):
    """Return a finite proper local-to-world rigid rotation.

    ``Pose`` remains the public default because ordinary tracked motion is
    represented as yaw/pitch/roll.  A Bullet dynamic body instead supplies its
    quaternion-derived matrix so an armor query never has to interpolate Euler
    angles across a flip.  Refuse malformed transforms: accepting a scale or a
    reflection changes effective armor and is worse than missing a sandbox
    experimental impact.
    """
    if override is None:
        return matrix(pose.yaw,pose.pitch,pose.roll)
    rotation = np.asarray(override,dtype=float)
    if rotation.shape != (3,3) or not np.all(np.isfinite(rotation)):
        raise ValueError('Invalid armor rotation override')
    if not np.allclose(rotation @ rotation.T,np.eye(3),atol=2e-6,rtol=0.):
        raise ValueError('Non-rigid armor rotation override')
    if not math.isclose(float(np.linalg.det(rotation)),1.,abs_tol=2e-6,rel_tol=0.):
        raise ValueError('Reflected armor rotation override')
    return rotation


@dataclass(frozen=True)
class ArmorHit:
    distance: float
    point: tuple
    normal: tuple
    part: str
    material: int
    thickness: float
    damage_factor: float
    external: bool = False


@dataclass(frozen=True)
class ArmorMeshPart:
    """One collision mesh material with its full owner identity.

    A material number alone is deliberately not an identity: ``hull.armor_14``
    and ``turret_01.armor_14`` are unrelated armor declarations.  ``node`` is
    retained for auditing only; selecting a controller state is based on the
    complete ``owner``/``material`` signature, never the numerical suffix in a
    DAVA node name.

    The triangles are in the collision scene's local BigWorld coordinates and
    are read-only.  This data class does not decide transforms, active state,
    damage, or collision queries.
    """
    owner: str
    material: int
    node: str
    triangles: np.ndarray = field(repr=False, compare=False)

    def __post_init__(self):
        if (not isinstance(self.owner, str) or not self.owner or
                type(self.material) is not int or self.material <= 0 or
                not isinstance(self.node, str) or not self.node):
            raise ValueError('Invalid movable armor collision signature')
        triangles = np.asarray(self.triangles, dtype=float)
        if (triangles.ndim != 3 or triangles.shape[1:] != (3, 3) or
                not len(triangles) or not np.all(np.isfinite(triangles))):
            raise ValueError('Invalid movable armor collision mesh')
        triangles.setflags(write=False)
        object.__setattr__(self, 'triangles', triangles)

    @property
    def signature(self):
        return self.owner, self.material


@dataclass(frozen=True)
class ArmorStateMesh:
    """Supplemental collision parts selected by one parsed armor state."""
    ident: int
    parts: tuple[ArmorMeshPart, ...]

    def __post_init__(self):
        if (type(self.ident) is not int or self.ident < 0 or
                type(self.parts) is not tuple or not self.parts or
                any(not isinstance(part, ArmorMeshPart) for part in self.parts) or
                len({part.signature for part in self.parts}) != len(self.parts)):
            raise ValueError('Invalid movable armor state geometry')


@dataclass(frozen=True)
class MovableArmorStateSnapshot:
    """One explicit, immutable complete movable-armor state observation.

    The client property is a set of enabled flags rather than a bare selected
    state ID.  Preserve that distinction at the collision boundary: a caller
    supplies every controller row and this object canonicalizes it by ID.  It
    deliberately has no health input and cannot decide a transition itself.
    The geometry later verifies the controller-specific invariants.
    """
    states: tuple

    def __post_init__(self):
        try:
            rows = tuple(self.states)
        except TypeError as error:
            raise ValueError('Invalid movable armor state snapshot') from error
        parsed = []
        for row in rows:
            if not isinstance(row, (tuple, list)) or len(row) != 2:
                raise ValueError('Invalid movable armor state snapshot')
            ident, enabled = row
            if (type(ident) is not int or not 0 <= ident < 256 or
                    type(enabled) is not bool):
                raise ValueError('Invalid movable armor state snapshot')
            parsed.append((ident, enabled))
        if not parsed or len({ident for ident, _ in parsed}) != len(parsed):
            raise ValueError('Invalid movable armor state snapshot')
        object.__setattr__(self, 'states', tuple(sorted(parsed)))


@dataclass(frozen=True)
class MovableArmorGeometry:
    """Static base and supplemental collision records for a movable vehicle.

    It intentionally stops before runtime behavior.  In particular it does
    not remove ``load_vehicle``'s MovableArmor guard, evaluate health state
    transitions, attach a part to an ``ArmorModel``, or alter arena collision.
    The object gives a later, independently verified integration an exact
    geometry contract to consume.
    """
    base_parts: tuple[ArmorMeshPart, ...]
    states: tuple[ArmorStateMesh, ...]

    def __post_init__(self):
        if (type(self.base_parts) is not tuple or not self.base_parts or
                any(not isinstance(part, ArmorMeshPart) for part in self.base_parts) or
                len({part.signature for part in self.base_parts}) != len(self.base_parts) or
                type(self.states) is not tuple or len(self.states) < 2 or
                any(not isinstance(state, ArmorStateMesh) for state in self.states) or
                len({state.ident for state in self.states}) != len(self.states)):
            raise ValueError('Invalid movable armor geometry')

    def parts_for_state(self, ident):
        if type(ident) is not int:
            raise ValueError('Invalid movable armor state ID')
        return next((state.parts for state in self.states if state.ident == ident), None)

    def parts_for_snapshot(self, snapshot):
        """Return the one enabled state's collision parts after exact checks."""
        if not isinstance(snapshot, MovableArmorStateSnapshot):
            raise ValueError('Missing or invalid movable armor state snapshot')
        expected = {state.ident for state in self.states}
        observed = {ident for ident, _ in snapshot.states}
        enabled = tuple(ident for ident, value in snapshot.states if value)
        if observed != expected or len(enabled) != 1:
            raise ValueError('Invalid movable armor state snapshot')
        parts = self.parts_for_state(enabled[0])
        # ``observed == expected`` makes this unreachable for valid geometry,
        # but keep the query fail-closed if either implementation changes.
        if parts is None:
            raise ValueError('Invalid movable armor state snapshot')
        return parts


@dataclass(frozen=True)
class ShotResult:
    outcome: str
    hit: ArmorHit | None
    damage: int = 0
    penetration: float = 0.
    effective_armor: float = 0.
    angle: float = 0.


@dataclass
class PenetrationState:
    """Energy and external modules already crossed by one flying shell."""
    roll: float | None = None
    spent: float = 0.
    prop_factor: float = 1.
    external_parts: set = field(default_factory=set)
    chemical_remaining: float | None = None
    chemical_distance: float | None = None


def resolve_ap(hits, direction, shell, rng=None, *, state=None, target=0):
    """Resolve ordered surfaces; tracks/gun alone never subtract vehicle HP."""
    if shell.kind not in ('ARMOR_PIERCING','ARMOR_PIERCING_CR'):
        raise ValueError('Unsupported kinetic shell kind')
    rng = rng or random
    if not hits:
        return ShotResult('miss', None)
    state = state if state is not None else PenetrationState()
    if state.roll is None:
        state.roll = rng.uniform(.95, 1.05)
    last = None
    for hit in hits:
        key = (target,hit.part)
        if hit.external and key in state.external_parts:
            continue
        power = shell.power(hit.distance)*state.roll*state.prop_factor-state.spent
        cosine = min(1., max(0., -float(np.dot(direction, hit.normal))))
        angle = math.acos(cosine)
        if hit.external:
            # External modules have no ricochet or normalization check.
            state.external_parts.add(key)
            effective = hit.thickness
        elif hit.thickness == 0:
            # Pinned collision has zero-armor surfaces: no resistance, so no
            # ricochet (always overmatched) and no normalization to scale.
            effective = 0.
        else:
            if angle >= shell.ricochet and shell.caliber <= 3*hit.thickness:
                return ShotResult('ricochet',hit,0,power,hit.thickness,angle)
            normalization = shell.normalization
            if shell.caliber > 2*hit.thickness:
                normalization *= 1.4*shell.caliber/(2*hit.thickness)
            effective = hit.thickness/max(1e-8, math.cos(max(0.,angle-normalization)))
        if power <= effective:
            return ShotResult('not_penetrated',hit,0,power,effective,angle)
        last = ShotResult('external',hit,0,power,effective,angle)
        state.spent += effective
        if hit.damage_factor > 0:
            damage = max(1, int(shell.damage*hit.damage_factor*rng.uniform(*shell.damage_factors)+.5))
            return ShotResult('penetrated',hit,damage,last.penetration,effective,angle)
    return last or ShotResult('miss',None)


def resolve_heat(hits, direction, shell, rng=None, *, state=None, target=0):
    """HEAT jet: no normalization/overmatch; loss starts after the first screen.

    Use the documented client loss per 0.1 m, applied to power remaining
    after each penetrated layer. The client overlay uses a different distance
    scale; this is a server rule, not a reconstruction of that overlay.
    Only actual layer contacts advance the anchor,
    so dividing flight into more simulation steps cannot alter penetration.
    """
    if shell.kind!='HOLLOW_CHARGE':
        raise ValueError('Expected a HEAT shell')
    factor = shell.penetration_loss_by_distance
    if factor is None or not math.isfinite(factor) or factor<0:
        raise ValueError('Missing or invalid HEAT penetration loss')
    rng = rng or random
    state = state if state is not None else PenetrationState()
    if not hits:return ShotResult('miss',None)
    if state.roll is None:state.roll = rng.uniform(.95,1.05)
    last = None
    for hit in hits:
        key = (target,hit.part)
        if hit.external and key in state.external_parts:continue
        power = shell.power(hit.distance)*state.roll
        if state.chemical_distance is not None:
            gap = max(0.,hit.distance-state.chemical_distance)
            power = state.chemical_remaining*max(0.,1.-factor*10.*gap)
        cosine = min(1.,max(0.,-float(np.dot(direction,hit.normal))))
        angle = math.acos(cosine)
        # Client external modules subtract nominal armor, without normalization.
        effective = hit.thickness if hit.external else hit.thickness/max(1e-8,cosine)
        if power<=effective:
            return ShotResult('not_penetrated',hit,0,power,effective,angle)
        if hit.damage_factor>0:
            damage = max(1,int(shell.damage*hit.damage_factor*rng.uniform(*shell.damage_factors)+.5))
            return ShotResult('penetrated',hit,damage,power,effective,angle)
        if hit.external:state.external_parts.add(key)
        state.spent += effective
        state.chemical_remaining = power-effective
        state.chemical_distance = hit.distance
        last = ShotResult('external',hit,0,power,effective,angle)
    return last or ShotResult('miss',None)


def resolve_he(hits, direction, shell, rng=None, *, state=None, target=0):
    """HE direct hit: only the first surface counts, with no range falloff.

    Main armor resists with T/cos, external modules with nominal T. There is
    no normalization, ricochet or overmatch. Full HP damage needs the first
    surface to be a penetrated, non-external HP surface; anything else ends
    the shell without HP damage and is never traversed further.
    """
    if shell.kind!='HIGH_EXPLOSIVE':
        raise ValueError('Expected a HIGH_EXPLOSIVE shell')
    rng = rng or random
    if not hits:return ShotResult('miss',None)
    state = state if state is not None else PenetrationState()
    if state.roll is None:state.roll = rng.uniform(.95,1.05)
    hit = hits[0]
    power = shell.penetration[0]*state.roll
    cosine = min(1.,max(0.,-float(np.dot(direction,hit.normal))))
    angle = math.acos(cosine)
    effective = hit.thickness if hit.external else hit.thickness/max(1e-8,cosine)
    if power<=effective:
        return ShotResult('not_penetrated',hit,0,power,effective,angle)
    if hit.external or hit.damage_factor<=0:
        return ShotResult('external',hit,0,power,effective,angle)
    damage = max(1,int(shell.damage*hit.damage_factor*rng.uniform(*shell.damage_factors)+.5))
    return ShotResult('penetrated',hit,damage,power,effective,angle)


def resolve_shell(hits, direction, shell, rng=None, *, state=None, target=0):
    resolver = (resolve_heat if shell.kind=='HOLLOW_CHARGE' else
                resolve_he if shell.kind=='HIGH_EXPLOSIVE' else resolve_ap)
    return resolver(hits,direction,shell,rng,state=state,target=target)


# Sandbox approximation of HE splash, from the native client UI (0x70bf0c):
# max(0, 0.5*rolled_damage*(1-gap/radius) - K*sum(nominal armor layers)).
# K=1.3 is proven ONLY in that client overlay, not in any authoritative
# server code; treat the whole splash rule as unverified.
BLAST_ARMOR_COEFFICIENT = 1.3
_BLAST_SAMPLES = 32
_BLAST_SEPARATION = .05


def _closest_points(p, tri):
    """Exact closest point on each triangle (Ericson) to p, vectorized."""
    a,b,c = tri[:,0],tri[:,1],tri[:,2]
    ab,ac = b-a,c-a
    dot = lambda x,y:np.einsum('ij,ij->i',x,y)
    d1,d2 = dot(ab,p-a),dot(ac,p-a)
    d3,d4 = dot(ab,p-b),dot(ac,p-b)
    d5,d6 = dot(ab,p-c),dot(ac,p-c)
    vc,vb,va = d1*d4-d3*d2,d5*d2-d1*d6,d3*d6-d5*d4
    safe = lambda x:np.where(x==0,1.,x)
    den = safe(va+vb+vc)
    col = lambda x:x[:,None]
    res = a+col(vb/den)*ab+col(vc/den)*ac
    res = np.where(col((va<=0)&(d4-d3>=0)&(d5-d6>=0)),
                   b+col((d4-d3)/safe((d4-d3)+(d5-d6)))*(c-b),res)
    res = np.where(col((vb<=0)&(d2>=0)&(d6<=0)),a+col(d2/safe(d2-d6))*ac,res)
    res = np.where(col((d6>=0)&(d5<=d6)),c,res)
    res = np.where(col((vc<=0)&(d1>=0)&(d3<=0)),a+col(d1/safe(d1-d3))*ab,res)
    res = np.where(col((d3>=0)&(d4<=d3)),b,res)
    res = np.where(col((d1<=0)&(d2<=0)),a,res)
    return res


def _movable_collision_asset(game, nation, name):
    """Read only the checked collision-mesh path for a movable armor probe."""
    parameter_path = Path(game)/f'Data/3d/Tanks/Parameters/{nation}/{name}.yaml.dvpl'
    try:
        parameters = yaml.safe_load(read(parameter_path))
    except yaml.YAMLError as error:
        raise ValueError('Malformed vehicle parameters') from error
    if not isinstance(parameters, dict) or not isinstance(parameters.get('resourcesPath'), dict):
        raise ValueError('Missing vehicle resourcesPath mapping')
    return _asset_base(Path(game)/'Data/3d/Tanks', parameters['resourcesPath'],
                       'collisionMesh', 'CollisionMeshes')


def _collision_batches(comps):
    """Yield the native lowest-LOD collision batches with strict structure."""
    render = comps.get('RenderComponent', {}).get('rc.renderObj', {})
    if not isinstance(render, dict):
        raise ValueError('Invalid movable armor collision node')
    values = render.get('ro.batches', {})
    if not isinstance(values, dict):
        raise ValueError('Invalid movable armor collision batches')
    parsed = []
    for key, batch in values.items():
        if not isinstance(batch, dict):
            raise ValueError('Invalid movable armor collision batch')
        try:
            index = int(key)
        except (TypeError, ValueError) as error:
            raise ValueError('Invalid movable armor collision batch') from error
        lod = render.get(f'rb{index}.lodIndex', -1)
        source = batch.get('rb.datasource')
        if type(lod) is not int or type(source) is not int:
            raise ValueError('Invalid movable armor collision batch')
        parsed.append((lod, source))
    if not parsed:
        raise ValueError('Missing movable armor collision mesh')
    lod = min(value[0] for value in parsed)
    seen = set()
    for current, source in parsed:
        if current == lod and source not in seen:
            seen.add(source)
            yield source


def _collision_nodes(nodes):
    """Walk every rd0 collision node, retaining inactive state alternatives."""
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get('name'), str):
            raise ValueError('Invalid movable armor collision node')
        comps = components(node)
        labels = comps.get('LabelComponent', {}).get('lc.labels', [])
        if labels and (not isinstance(labels, list) or 'rd0' not in labels):
            continue
        yield node, comps
        children = node.get('#hierarchy', [])
        if not isinstance(children, list):
            raise ValueError('Invalid movable armor collision hierarchy')
        yield from _collision_nodes(children)


def _node_parts(node, comps, meshes, *, owner=None):
    """Split a collision node into its unique owner/material mesh records."""
    owner = node['name'] if owner is None else owner
    by_material = {}
    for source in _collision_batches(comps):
        mesh = meshes.get(source)
        if mesh is None or mesh.material_ids is None:
            raise ValueError('Missing movable armor material indices')
        ids = mesh.material_ids[mesh.triangles]
        if np.any(ids != ids[:, 0, None]):
            raise ValueError('Movable armor triangle crosses material indices')
        vertices = world_vertices(mesh.vertices, comps.get('TransformComponent', {}))
        triangles = vertices[mesh.triangles[:, (0, 2, 1)]]
        for material in sorted(map(int, np.unique(ids[:, 0]))):
            if material <= 0:
                raise ValueError('Invalid movable armor material index')
            selected = triangles[ids[:, 0] == material]
            by_material.setdefault(material, []).append(selected)
    if not by_material:
        raise ValueError('Missing movable armor collision mesh')
    return tuple(ArmorMeshPart(owner, material, node['name'],
                               np.concatenate(chunks, axis=0))
                 for material, chunks in sorted(by_material.items()))


def _state_signature(reference):
    owner, marker, material = reference.partition('.armor_')
    if not owner or marker != '.armor_' or not material.isdigit():
        raise ValueError('Invalid movable armor references')
    return owner, int(material)


def _load_movable_armor_geometry(game, nation, name):
    controller = load_armor_states(game, nation, name)
    if controller is None:
        return None
    asset = _movable_collision_asset(game, nation, name)
    collision_scene = scene(Path(str(asset)+'.sc2.dvpl'))
    meshes = geometry(Path(str(asset)+'.scg.dvpl'))
    hierarchy = collision_scene.get('#hierarchy')
    if not isinstance(hierarchy, list):
        raise ValueError('Invalid movable armor collision hierarchy')

    expected = {}
    owners = set()
    for state in controller.states:
        for reference in state.armors:
            signature = _state_signature(reference)
            if signature in expected:
                raise ValueError('Ambiguous movable armor controller signature')
            expected[signature] = state.ident
            owners.add(signature[0])

    base_nodes, supplemental_nodes = {}, []
    for node, comps in _collision_nodes(hierarchy):
        node_name = node['name']
        if node_name in owners:
            if node_name in base_nodes:
                raise ValueError(f'Ambiguous movable armor base node: {node_name}')
            base_nodes[node_name] = (node, comps)
            continue
        if '_state_' in node_name:
            matches = [owner for owner in owners if node_name.startswith(owner+'_state_')]
            if len(matches) != 1:
                raise ValueError(f'Unmatched movable armor collision node: {node_name}')
            supplemental_nodes.append((matches[0], node, comps))

    if set(base_nodes) != owners:
        missing = sorted(owners-set(base_nodes))[0]
        raise ValueError(f'Missing movable armor base node: {missing}')
    base_parts = tuple(part for owner in sorted(base_nodes)
                       for part in _node_parts(*base_nodes[owner], meshes))
    if len({part.signature for part in base_parts}) != len(base_parts):
        raise ValueError('Ambiguous movable armor base signature')

    selected, seen_nodes = {}, set()
    for owner, node, comps in supplemental_nodes:
        if node['name'] in seen_nodes:
            raise ValueError(f'Ambiguous movable armor collision node: {node["name"]}')
        seen_nodes.add(node['name'])
        node_parts = _node_parts(node, comps, meshes, owner=owner)
        state_ids = set()
        for part in node_parts:
            signature = owner, part.material
            state = expected.get(signature)
            if state is None:
                raise ValueError('Unmatched movable armor collision signature: '
                                 f'{owner}.armor_{part.material}')
            if signature in selected:
                raise ValueError('Ambiguous movable armor collision signature: '
                                 f'{owner}.armor_{part.material}')
            selected[signature] = part
            state_ids.add(state)
        if len(state_ids) != 1:
            raise ValueError(f'Ambiguous movable armor collision node: {node["name"]}')

    missing = [signature for signature in expected if signature not in selected]
    if missing:
        owner, material = sorted(missing)[0]
        raise ValueError(f'Missing movable armor collision signature: {owner}.armor_{material}')
    states = tuple(ArmorStateMesh(state.ident, tuple(
        selected[_state_signature(reference)] for reference in state.armors))
        for state in controller.states)
    return MovableArmorGeometry(base_parts, states)


@lru_cache(maxsize=16)
def _cached_movable_armor_geometry(game, nation, name):
    return _load_movable_armor_geometry(Path(game), nation, name)


def load_movable_armor_geometry(game: Path, nation='usa', name='M2_lt'):
    """Return static movable armor meshes, or ``None`` for ordinary vehicles.

    This loader is intentionally separate from ``ArmorModel`` and
    ``load_vehicle``.  Calling it cannot make a MovableArmor vehicle battle
    eligible; it validates only the independent controller-to-SC2 mesh mapping.
    """
    return _cached_movable_armor_geometry(Path(game), nation, name)


load_movable_armor_geometry.cache_clear = _cached_movable_armor_geometry.cache_clear


def load_battle_vehicle(game: Path, nation='usa', name='M2_lt', configuration=None):
    """Load a vehicle admitted to the server's battle collision path.

    Ordinary vehicles are returned by the public, guarded ``load_vehicle``
    path unchanged.  A MovableArmor vehicle gets no exception merely because
    its XML declares a controller: its complete SC2/SCG state geometry must
    first validate, then the internal loader may retain that exact controller
    on the immutable definition.  This is intentionally not a general escape
    hatch for loader guards.
    """
    if configuration is not None and not isinstance(configuration, VehicleConfiguration):
        raise ValueError('Expected VehicleConfiguration')
    game = Path(game)
    config = configuration or VehicleConfiguration()
    # Preserve the complete ordinary route, including its error order and
    # cache identity.  Only its explicit MovableArmor refusal enters the
    # narrow private path below.
    try:
        return load_vehicle(game, nation, name, config)
    except ValueError as public_error:
        if not str(public_error).startswith('Movable armor is not implemented:'):
            raise
    controller = load_armor_states(game, nation, name)
    if controller is None:
        raise public_error
    geometry = load_movable_armor_geometry(game, nation, name)
    if geometry is None:
        raise ValueError('Missing movable armor collision geometry')
    definition = _load_vehicle(game, nation, name, config, True)
    loaded = definition.armor_states_controller
    if loaded is None or loaded != controller:
        raise ValueError('Movable armor controller changed during battle load')
    controller_ids = {state.ident for state in loaded.states}
    geometry_ids = {state.ident for state in geometry.states}
    if controller_ids != geometry_ids:
        raise ValueError('Movable armor controller and geometry state IDs differ')
    return definition


class ArmorModel:
    def __init__(self, game, definition=None, *, movable_geometry=None):
        definition = definition or load_vehicle(Path(game))
        if movable_geometry is not None and not isinstance(movable_geometry, MovableArmorGeometry):
            raise ValueError('Invalid movable armor geometry')
        self.definition = definition
        self.shell = definition.weapon.shells[0]
        self.turret_position = np.array(definition.aiming.turret_position)
        self.gun_position = np.array(definition.aiming.gun_position)
        self.health = definition.health
        self.parts = []
        sc = scene(Path(str(definition.collision_asset)+'.sc2.dvpl'))
        meshes = geometry(Path(str(definition.collision_asset)+'.scg.dvpl'))
        selected = {c.geometry:c for c in (definition.hull,definition.turret,definition.gun)}
        empty_gun = False
        from .collision import batches, turretless
        # A casemate's turret has no armor surface; its HP and gun pivot remain.
        casemate = turretless(definition,sc['#hierarchy'])
        for node,comps in active_nodes(sc['#hierarchy']):
            name = node['name']
            if name not in selected:
                continue
            mesh_batches = tuple(batches(comps))
            # Some pinned vehicles (T1 Heavy) explicitly contain an empty gun
            # collision entity. There is no barrel surface to raycast; do not
            # substitute the visible model or ignore a missing entity.
            if (name == definition.gun.geometry and not mesh_batches
                    and 'RenderComponent' not in comps and not node.get('#hierarchy')):
                empty_gun = True
            for batch in mesh_batches:
                mesh = meshes[batch['rb.datasource']]
                if mesh.material_ids is None:
                    raise ValueError('Missing armor material indices')
                ids = mesh.material_ids[mesh.triangles]
                if np.any(ids != ids[:,0,None]):
                    raise ValueError('Armor triangle crosses material indices')
                armor = selected[name]
                ids = ids[:,0]
                thickness, factors = [],[]
                for ident in ids:
                    item = armor.material(f'armor_{ident}')
                    if item is None:
                        raise ValueError(f'No armor definition: {name}/{ident}')
                    thickness.append(item.thickness)
                    factors.append(item.damage_factor)
                vertices = world_vertices(mesh.vertices,comps.get('TransformComponent',{}))
                if name == definition.hull.geometry:
                    vertices = vertices + definition.hull_position
                triangles = vertices[mesh.triangles[:,(0,2,1)]]
                self.parts.append((name,triangles,ids,np.array(thickness),np.array(factors)))
        # Chassis has no separate armor mesh in this build. Use its visible
        # track surfaces as external modules, with armor from the vehicle XML.
        sc = scene(Path(str(definition.visual_asset)+'.sc2.dvpl'))
        meshes = geometry(Path(str(definition.visual_asset)+'.scg.dvpl'))
        for node,comps in active_nodes(sc['#hierarchy']):
            name = node['name']
            if name not in ('chassis_track_L','chassis_track_R'):
                continue
            material = definition.chassis.material('leftTrack' if name.endswith('L') else 'rightTrack')
            if material is None:
                raise ValueError('Missing track armor')
            armor = material.thickness
            for batch in batches(comps):
                mesh = meshes[batch['rb.datasource']]
                vertices = world_vertices(mesh.vertices,comps.get('TransformComponent',{}))
                triangles = vertices[mesh.triangles[:,(0,2,1)]]
                n = len(triangles)
                self.parts.append((name,triangles,np.zeros(n,dtype=int),np.full(n,armor),np.zeros(n)))
        required = set(selected)|{'chassis_track_L','chassis_track_R'}
        if empty_gun:
            required.remove(definition.gun.geometry)
        if casemate:
            required.remove(definition.turret.geometry)
        if {p[0] for p in self.parts} != required:
            raise ValueError('Incomplete armor geometry')
        # A sphere about the hull origin, conservative for every gun/turret
        # angle. Used only to reject distant projectile segments cheaply.
        self.radius = max(float(np.linalg.norm(p[1],axis=2).max()) +
            (_vector_length(self.turret_position) if p[0].startswith(('turret','gun')) else 0.) +
            (_vector_length(self.gun_position) if p[0].startswith('gun') else 0.)
            for p in self.parts)
        self._movable_geometry = movable_geometry
        self._movable_state_parts = None
        if movable_geometry is not None:
            # The static loader deliberately gives us collision triangles only.
            # Bind their material IDs to the supplied complete vehicle
            # definition here, without changing that loader's battle guard.
            def runtime_part(part):
                component = selected.get(part.owner)
                if component is None:
                    raise ValueError(f'Unknown movable armor owner: {part.owner}')
                material = component.material(f'armor_{part.material}')
                if material is None:
                    raise ValueError(f'No movable armor definition: {part.owner}/{part.material}')
                triangles = np.array(part.triangles, copy=True)
                if part.owner == definition.hull.geometry:
                    triangles += definition.hull_position
                triangles.setflags(write=False)
                count = len(triangles)
                return (part.owner, triangles, np.full(count, part.material, dtype=int),
                        np.full(count, material.thickness),
                        np.full(count, material.damage_factor))

            self.parts = tuple(runtime_part(part) for part in movable_geometry.base_parts)
            self._movable_state_parts = {
                state.ident: tuple(runtime_part(part) for part in state.parts)
                for state in movable_geometry.states
            }
            # A state is chosen per query, but the broad-phase bound must cover
            # every legal state.  Otherwise a valid transition could make a
            # shell or splash skip precise collision before it reaches a newly
            # exposed triangle.
            all_parts = tuple(self.parts) + tuple(
                part for parts in self._movable_state_parts.values() for part in parts)
            self.radius = max(float(np.linalg.norm(p[1],axis=2).max()) +
                (_vector_length(self.turret_position) if p[0].startswith(('turret','gun')) else 0.) +
                (_vector_length(self.gun_position) if p[0].startswith('gun') else 0.)
                for p in all_parts)

    def _parts_for_query(self, movable_state):
        """Select immutable movable geometry explicitly, or legacy parts.

        Ordinary vehicles retain the exact previous call contract.  Supplying
        a movable snapshot to one is an integration error; accepting it would
        silently make state handling dependent on vehicle type.
        """
        geometry = getattr(self, '_movable_geometry', None)
        if geometry is None:
            if movable_state is not None:
                raise ValueError('Ordinary armor model cannot accept movable armor state')
            return self.parts
        selected = geometry.parts_for_snapshot(movable_state)
        # Validation above establishes that exactly one declared ID is enabled.
        ident = next(ident for ident, enabled in movable_state.states if enabled)
        parts = self._movable_state_parts.get(ident)
        if parts is None or len(parts) != len(selected):
            raise ValueError('Invalid movable armor state snapshot')
        return self.parts + parts

    def blast(self, point, shell, pose, turret_yaw=0., gun_pitch=0., rng=None, *, contact=None,
              rotation=None, movable_state=None):
        """HE splash on this vehicle: best reduced HP damage over sample rays.

        Sandbox approximation (see BLAST_ARMOR_COEFFICIENT): no world
        occlusion, but modules/armor on the way resist. Up to _BLAST_SAMPLES
        surface points are chosen by exact closest-point distance and armor;
        each is traced from the blast point through the real transformed
        surfaces, stopping at the first main HP surface. External modules
        subtract nominal armor once per part; non-HP main layers each count,
        and so does the nominal armor of the HP surface that is reached.
        One damage roll is drawn per call, never per ray. No angles, no
        normalization, no ricochet. `contact` (a direct non-penetrating hit)
        sits at the blast point: an HP contact is evaluated at gap 0 (its own
        nominal armor still counts, returned hit.distance is 0). Any other
        contact only backs each ray's start a small epsilon out along its
        normal, so it is charged exactly when a ray really crosses it.
        """
        if shell.kind!='HIGH_EXPLOSIVE':
            raise ValueError('Expected a HIGH_EXPLOSIVE shell')
        radius = shell.explosion_radius
        if radius is None or not math.isfinite(radius) or radius<=0:
            raise ValueError('Missing or invalid explosion radius')
        rng = rng or random
        # Validate the explicit snapshot before the cheap geometry rejection.
        # A distant query must not silently hide a missing/invalid state.
        parts = self._parts_for_query(movable_state)
        origin = np.asarray(contact.point if contact is not None else point,dtype=float)
        if np.linalg.norm(origin-pose.position)>self.radius+radius:
            return ShotResult('miss',None)
        rot = _validated_rotation(pose,rotation)
        local = (origin-pose.position) @ rot
        turret_rot = matrix(turret_yaw,0,0)
        tris,scores,gaps,points,owners = [],[],[],[],[]
        for index,(name,triangles,ids,thickness,factors) in enumerate(parts):
            if name.startswith(('chassis','gun')) or not np.any(factors>0):
                continue
            if name.startswith('turret'):
                local_rot, offset = turret_rot,self.turret_position
            else:
                local_rot,offset = np.eye(3),np.zeros(3)
            hull = triangles @ local_rot.T + offset
            closest = _closest_points(local,hull)
            gap = np.linalg.norm(closest-local,axis=1)
            # Upper bound with the best damage roll, before any screens.
            bound = (.5*shell.damage*shell.damage_factors[1]*factors*(1-gap/radius)
                     - BLAST_ARMOR_COEFFICIENT*thickness)
            keep = np.flatnonzero((gap<radius)&(factors>0)&(bound>0))
            tris.append(hull)
            for i in keep:
                scores.append(bound[i]);gaps.append(gap[i]);points.append(closest[i])
                owners.append((len(tris)-1,index,i))
        direct = (contact is not None and not contact.external and contact.damage_factor>0)
        if not scores and not direct:
            return ShotResult('miss',None)
        roll = rng.uniform(*shell.damage_factors)
        base = .5*roll*shell.damage
        best = None  # (value, -gap, hit, nominal armor)
        def consider(value,gap,hit,armor):
            nonlocal best
            key = (value,-gap)
            if best is None or key>best[:2]:best = (value,-gap,hit,armor)
        if direct:
            armor = contact.thickness
            consider(max(0.,base*contact.damage_factor-BLAST_ARMOR_COEFFICIENT*armor),0.,
                     replace(contact,distance=0.),armor)
        start = origin
        if contact is not None and not direct:
            normal = np.asarray(contact.normal,dtype=float)
            start = origin+normal/max(1e-12,np.linalg.norm(normal))*_BLAST_SEPARATION*.01
        chosen = []
        for i in np.argsort(scores)[::-1][:512]:
            if len(chosen)>=_BLAST_SAMPLES:break
            if all(np.linalg.norm(points[i]-points[j])>=_BLAST_SEPARATION for j in chosen):
                chosen.append(i)
        for i in chosen:
            tri_index,part_index,t = owners[i]
            name,_,ids,thickness,factors = parts[part_index]
            gap = gaps[i]
            world = points[i] @ rot.T + pose.position
            if gap<1e-4:
                # Blast point on the surface: no ray direction; gap 0.
                tri = tris[tri_index][t]
                normal = np.cross(tri[1]-tri[0],tri[2]-tri[0])
                normal = normal/max(1e-12,np.linalg.norm(normal)) @ rot.T
                hit = ArmorHit(0.,tuple(map(float,world)),tuple(map(float,normal)),name,
                               int(ids[t]),float(thickness[t]),float(factors[t]))
                armor = hit.thickness
                consider(max(0.,base*hit.damage_factor-BLAST_ARMOR_COEFFICIENT*armor),0.,hit,armor)
                continue
            direction = (world-origin)/gap
            lead = _vector_length(start-origin)
            hits = self.intersections(start,direction,pose,turret_yaw,gun_pitch,
                                      min(radius,gap)+lead+1e-3,rotation=rot,
                                      movable_state=movable_state)
            crossed,total = set(),0.
            for hit in hits:
                # Gap is measured from the real explosion point, not the backed-off start.
                hit = replace(hit,distance=_vector_length(np.asarray(hit.point)-origin))
                if hit.distance>=radius:break
                if hit.external:
                    if hit.part in crossed:continue
                    crossed.add(hit.part)
                    total += hit.thickness
                elif hit.damage_factor<=0:
                    total += hit.thickness
                else:
                    total += hit.thickness
                    value = max(0.,base*hit.damage_factor*(1-hit.distance/radius)
                                - BLAST_ARMOR_COEFFICIENT*total)
                    consider(value,hit.distance,hit,total)
                    break
        if best is None:
            return ShotResult('miss',None)
        return ShotResult('splash',best[2],int(best[0]+.5),0.,best[3],0.)

    def intersections(self, origin, direction, pose, turret_yaw=0., gun_pitch=0., distance=500.,
                      *, rotation=None, movable_state=None):
        # Reject a finite ray segment outside the conservative hull-centred
        # sphere before allocating/transforming any triangle arrays. The same
        # bound already covers every turret/gun angle for flying projectiles.
        # State validation remains before that optimization so movable armor
        # never degrades into an ordinary broad-phase miss when a caller loses
        # the mandatory snapshot.
        parts = self._parts_for_query(movable_state)
        delta = tuple(c-o for c,o in zip(pose.position,origin))
        length2 = sum(d*d for d in direction)
        if length2<=1e-16:
            return []
        t = max(0.,min(distance,sum(c*d for c,d in zip(delta,direction))/length2))
        nearest2 = sum((c-d*t)**2 for c,d in zip(delta,direction))
        if nearest2>(self.radius+1e-5)**2:
            return []
        rot = _validated_rotation(pose,rotation)
        origin = (np.array(origin)-pose.position) @ rot
        direction = np.asarray(direction) @ rot
        turret_rot = matrix(turret_yaw,0,0)
        hits = []
        native = (_armor_native.query(parts,origin,direction,turret_rot,
            self.turret_position,self.gun_position,gun_pitch,distance,matrix_fn=matrix)
            if _cross3 is _ARMOR_NATIVE_CROSS else None)
        if native is not None:
            prepared,rows = native
            for part_index,i,t in rows:
                part,local_rot = prepared[part_index]
                name,triangles,ids,thickness,factors = part
                e1 = triangles[i,1]-triangles[i,0]
                e2 = triangles[i,2]-triangles[i,0]
                normal = _cross3(e1,e2); normal /= np.linalg.norm(normal)
                point = (origin+direction*t) @ rot.T + pose.position
                normal = normal @ local_rot.T @ rot.T
                hits.append(ArmorHit(t,tuple(map(float,point)),tuple(map(float,normal)),
                    name,int(ids[i]),float(thickness[i]),float(factors[i]),
                    name.startswith(('chassis','gun'))))
        else:
            for name,triangles,ids,thickness,factors in parts:
                if name.startswith('turret'):
                    local_rot, offset = turret_rot,self.turret_position
                elif name.startswith('gun'):
                    local_rot = turret_rot @ matrix(0,gun_pitch,0)
                    offset = self.turret_position+turret_rot @ self.gun_position
                else:
                    local_rot,offset = np.eye(3),np.zeros(3)
                o = (origin-offset) @ local_rot
                d = direction @ local_rot
                a = triangles[:,0]; e1=triangles[:,1]-a; e2=triangles[:,2]-a
                cross = _cross3(d,e2)
                det = np.einsum('ij,ij->i',e1,cross)
                valid = det > 1e-9  # Front-facing entries only; do not charge exit surfaces twice.
                inv = np.divide(1.,det,out=np.zeros_like(det),where=valid)
                delta = o-a
                u = np.einsum('ij,ij->i',delta,cross)*inv
                q = _cross3(delta,e1)
                v = q @ d*inv
                t = np.einsum('ij,ij->i',e2,q)*inv
                valid &= (u>=-1e-7)&(v>=-1e-7)&(u+v<=1.0000001)&(t>1e-5)&(t<=distance)
                for i in np.flatnonzero(valid):
                    normal = _cross3(e1[i],e2[i]); normal /= np.linalg.norm(normal)
                    point = (origin+direction*t[i]) @ rot.T + pose.position
                    normal = normal @ local_rot.T @ rot.T
                    hits.append(ArmorHit(float(t[i]),tuple(map(float,point)),tuple(map(float,normal)),
                        name,int(ids[i]),float(thickness[i]),float(factors[i]),
                        name.startswith(('chassis','gun'))))
        hits.sort(key=lambda h:h.distance)
        unique=[]
        for hit in hits:
            if unique and hit.part==unique[-1].part and abs(hit.distance-unique[-1].distance)<1e-5:
                continue
            unique.append(hit)
        return unique
