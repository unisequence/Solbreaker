"""One Bullet world owns an arena's terrain, obstacles, tanks and rigid-body dynamics.

Tracked motion uses bounded sweeps and suspension. Airborne vehicles use rigid
bodies in the same world, with the arena owner advancing the fixed simulation step.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property, partial
import math
import os
from pathlib import Path
import struct
import time

import numpy as np
from .vector_math import vector_length as _vector_length

# Keep replaced numerical callbacks on their original path.
_CONTACT_MEAN, _CONTACT_NORM = np.mean, np.linalg.norm
_CONTACT_NUMPY = (type(_CONTACT_MEAN).__name__ == '_ArrayFunctionDispatcher'
                  and type(_CONTACT_NORM).__name__ == '_ArrayFunctionDispatcher')
import pybullet as bullet
try:
    from . import _contact_queries
except (ImportError, OSError):
    _contact_queries = None
if getattr(_contact_queries, 'API_VERSION', None) not in (1, 2):
    _contact_queries = None

from .arena_bounds import CircularZone
from .dava_scene import scene, geometry, components, active_nodes, world_vertices, Archive
from .dvpl import read
from .mass_properties import VehicleMassModel
from .orientation import matrix, quaternion
from .mesh_files import MeshFiles, bullet_mesh_paths, create_bullet_mesh
from .map_catalog import load_map_spec
from .terrain import MapTerrain
from .fall_damage import MIN_HULL_DAMAGE_SPEED, evaluate_fall_impact, impact_damage_speed
from .map_materials import (MapEnvironment, foliage_material, material_chain,
                            permeable_water_plant, water_material)
_BREAKABLE_TYPES = (3, 4, 5, 8)
_RAY_GROUP = 1  # Bullet's default ray query group.
_TRACKED_GROUP = 2
_DYNAMIC_GROUP = 4
_STATIC_GROUP = 8
_ZONE_TRACKED_GROUP = 16
_ZONE_DYNAMIC_GROUP = 32


# Pinned New Bay blocks split solid facades into untyped render parts. The
# parent assemblies also omit CollisionTypeComponent in the 11.21 resources.
_FORGECITY_BUILDING_ASSEMBLIES = frozenset(
    [f'district_{number}.sc2' for number in range(1, 13)]
    + [f'residetial_ditrict_{number:02d}.sc2' for number in range(2, 8)]
    + ['residetial_ditrict_01_01.sc2', 'residetial_ditrict_01_02.sc2'])


def batches(comps):
    obj = comps.get('RenderComponent', {}).get('rc.renderObj', {})
    values = obj.get('ro.batches', {})
    lods = [obj.get(f'rb{int(k)}.lodIndex', -1) for k in values]
    lod = min(lods, default=-1)
    seen = set()
    for key, value in values.items():
        if obj.get(f'rb{int(key)}.lodIndex', -1) != lod:
            continue
        pg = value['rb.datasource']
        if pg not in seen:
            seen.add(pg)
            yield value


def turretless(definition, hierarchy):
    """Whether the selected turret legitimately has no collision armor.

    Casemate tank destroyers keep an XML turret for HP, the gun pivot and
    traverse limits, but their superstructure is hull armor: the selected
    turret entity is absent (SU-85, Hetzer) or empty (T110E3). Only that
    layout is accepted, with limited traverse and the gun as the sole
    rotating armor; a missing turret anywhere else is incomplete data.
    """
    turret,gun = definition.turret.geometry,definition.gun.geometry
    nodes = list(active_nodes(hierarchy))
    def meshed(name):
        return any(n['name']==name and next(batches(c),None) is not None for n,c in nodes)
    if meshed(turret):
        return False
    low,high = definition.aiming.yaw_limits
    if turret is None or not -math.pi < low <= high < math.pi:
        raise ValueError(f'Missing turret collision geometry: {turret}')
    if gun is None or not meshed(gun):
        raise ValueError(f'Turretless vehicle lacks gun collision geometry: {gun}')
    for node,_ in nodes:
        if node['name'] != turret:
            continue
        for child,comps in active_nodes(node.get('#hierarchy',[])):
            if child['name'] != gun and next(batches(comps),None) is not None:
                raise ValueError(f'Unassigned geometry under turret: {turret}/{child["name"]}')
    return True


@dataclass(frozen=True)
class Pose:
    position: tuple
    yaw: float
    pitch: float = 0.
    roll: float = 0.


@dataclass
class Motion:
    """Kinematic state of one vehicle; CollisionWorld.poses keeps its position."""
    velocity: tuple = (0.,0.,0.)
    grounded: bool = True
    suspension_velocity: tuple = (0.,0.,0.)
    # Pitch/roll momentum sampled while the suspension can exert a positive
    # support force. Unloaded visual road fitting cannot add takeoff torque.
    support_angular_rates: tuple = (0.,0.)


@dataclass
class GroundMotion:
    """Optional planar and yaw momentum carried between ground steps."""
    velocity: tuple | None = None
    yaw_rate: float = 0.
    active: bool = False


@dataclass
class DynamicBody:
    """Opt-in Bullet body used while a released hull is genuinely airborne.

    The normal tracked solver deliberately remains kinematic.  A dynamic body
    exists only for the experimental cliff/drop path, and is mirrored back to
    the existing collision sensor body so armor, turret and network code keep
    their established entity identity.
    """
    body: int
    mass: float
    center_of_mass: tuple
    inertia_diagonal: tuple
    settled_steps: int = 0
    stationary_time: float = 0.
    stationary_anchor: tuple = None
    contact_friction: float = .8
    restore_air_damping: bool = False
    sandbox_landing: bool = False


@dataclass(frozen=True)
class Destructible:
    key: int
    name: str
    kind: int
    health: int
    vertices: np.ndarray
    triangles: np.ndarray

    @property
    def client_registered(self):
        # Negative keys are local to physics. The client has no entry for
        # them in the map's .lka and cannot play a networked destruction.
        return self.key >= 0


class CollisionScene:
    """Immutable local asset data shared between successive training battles."""
    def __init__(self, game, definition=None, map_spec=None, *, map_cache=None, terrain=None):
        from .vehicle_data import load_vehicle
        game = Path(game)
        definition = definition or load_vehicle(game)
        self._init_map(map_spec or load_map_spec(game, 'rudniki'), MeshFiles())
        try:
            self.cache_metrics = (map_cache.load(self, terrain) if map_cache is not None else {})
            if self.mesh_files.prepared is None:
                started = time.monotonic()
                self._load_map()
                self.cache_metrics['geometry_ms'] = (self.cache_metrics.get('geometry_ms', 0.)
                                                    + (time.monotonic()-started)*1000)
            self._configure_vehicle(game, definition)
        except BaseException:
            self.close()
            raise

    def _init_map(self, spec, mesh_files):
        self.map_spec = spec
        self.mesh_files = mesh_files
        self.terrain_meshes = {}
        self.water_triangles = []
        self.foliage_bounds = []
        self._owns_mesh_files = True

    @classmethod
    def map_only(cls, spec, mesh_files):
        result = cls.__new__(cls)
        result._init_map(spec, mesh_files)
        result._load_map()
        return result

    @cached_property
    def environment(self):
        return MapEnvironment(self.water_triangles, self.foliage_bounds)

    def _load_map(self):
        data = scene(self.map_spec.scene_path)
        meshes = geometry(self.map_spec.geometry_path)
        materials = {int.from_bytes(n['#id'], 'little'): n
                     for n in data['#dataNodes'] if n.get('##name') == 'NMaterial'}
        traits = {}
        for ident in materials:
            chain = tuple(material_chain(materials, ident))
            traits[ident] = (water_material(chain), foliage_material(chain),
                             any('trunk' in m.get('materialName', '').lower() for m in chain))
        ids = Archive(read(self.map_spec.lka_path)).load()
        # State 0 meshes inherit their parent's .lka ID. Alternative ruined
        # states are excluded by active_nodes, as in the original collision map.
        owners, collision_types = {}, {}
        def assign(nodes, owner=None, local_owner=None, inherited_collision=None):
            for node in nodes:
                key = ids.get(str(node['id']), owner)
                comps = components(node)
                local_key = -node['id']-1 if 'StateSwitcherComponent' in comps else local_owner
                owners[node['id']] = key if key is not None else local_key
                own_collision = comps.get('CollisionTypeComponent')
                if (own_collision is None and self.map_spec.key == 'forgecity'
                        and node['name'] in _FORGECITY_BUILDING_ASSEMBLIES
                        and not any(batches(comps))):
                    own_collision = {'CollisionType': 6}
                effective = own_collision if own_collision is not None else inherited_collision
                if effective is not None:
                    collision_types[node['id']] = effective
                child_collision = inherited_collision
                if own_collision is not None:
                    # Static assemblies own their render descendants. Explicit
                    # child types, including 0, terminate that inherited scope.
                    child_collision = (own_collision if
                        own_collision.get('CollisionType') in (6, 7) and not any(batches(comps))
                        else None)
                assign(node.get('#hierarchy',[]), key, local_key, child_collision)
        assign(data['#hierarchy'])
        vertices, triangles, offset = [], [], 0
        parts = {}
        self.destructibles = {}
        self.object_names = []
        self.bounds = None
        for node, comps in active_nodes(data['#hierarchy'], self.map_spec.state_tag):
            if 'MapBorderComponent' in comps:
                self.bounds = struct.unpack('<4f', comps['MapBorderComponent']['mbc.rect'])
            kind = collision_types.get(node['id'], {}).get('CollisionType', 0)
            key = owners[node['id']] if kind in _BREAKABLE_TYPES else None
            if kind in _BREAKABLE_TYPES and key is None:
                key = -node['id']-1
            if key is not None and key >= 0 and key & 65535 > 255:
                raise ValueError('Destructible ID exceeds native 8-bit index')
            added = False
            canopy = []
            for batch in batches(comps):
                material_id = batch['rb.nmatname']
                water, foliage, trunk = traits.get(material_id, (False, False, False))
                cane = permeable_water_plant(node, kind, material_id, materials)
                solid = kind in (3, 4, 5, 6, 7, 8) and not cane and (kind != 8 or trunk)
                if not (water or foliage or solid):
                    continue
                mesh = meshes[batch['rb.datasource']]
                verts = world_vertices(mesh.vertices, comps.get('TransformComponent', {}))
                if not len(verts):
                    continue
                if water:
                    self.water_triangles.extend(verts[mesh.triangles].tolist())
                    continue
                if foliage:
                    canopy.append((verts.min(axis=0), verts.max(axis=0)))
                if not solid:
                    continue
                faces = mesh.triangles[:, (0,2,1)]
                if key is None:
                    vertices.append(verts)
                    triangles.append(faces+offset)
                    offset += len(verts)
                else:
                    parts.setdefault(key,[]).append((node,comps,verts,faces))
                added = True
            if canopy:
                low = np.min([bounds[0] for bounds in canopy], axis=0).tolist()
                high = np.max([bounds[1] for bounds in canopy], axis=0).tolist()
                self.foliage_bounds.append((low, high, key))
            if added:
                self.object_names.append(node['name'])
        if self.bounds is None or (not vertices and not getattr(self.map_spec, 'allow_empty_obstacles', False)):
            raise ValueError(f'{self.map_spec.key} collision data is incomplete')
        self.vertices = np.concatenate(vertices) if vertices else np.empty((0, 3), dtype=float)
        self.triangles = np.concatenate(triangles) if triangles else np.empty((0, 3), dtype=np.int32)
        for key, groups in parts.items():
            verts,faces,offset = [],[],0
            for node,comps,v,t in groups:
                verts.append(v);faces.append(t+offset);offset+=len(v)
            ct = comps['CollisionTypeComponent']
            self.destructibles[key] = Destructible(key,node['name'],ct['CollisionType'],
                    max(1,int(ct.get('Health',1))),np.concatenate(verts),np.concatenate(faces))

        self.foliage_bounds = [(low, high, key if key in parts else None)
                               for low, high, key in self.foliage_bounds]

    def for_vehicle(self, game, definition):
        """Share immutable map assets/cache; load only this vehicle's geometry."""
        if self.mesh_files.closed:
            raise ValueError('Collision scene is closed')
        result = object.__new__(CollisionScene)
        result.__dict__ = self.__dict__.copy()
        result._owns_mesh_files = False
        result._configure_vehicle(Path(game), definition)
        return result

    def _configure_vehicle(self, game, definition):
        self.definition = definition
        self.tank_shapes = []
        self._convex_shapes = None
        ts = scene(Path(str(definition.collision_asset)+'.sc2.dvpl'))
        tg = geometry(Path(str(definition.collision_asset)+'.scg.dvpl'))
        # A casemate has no turret shape or joint; its gun is not a physical shape.
        self.turret_node = None if turretless(definition,ts['#hierarchy']) else definition.turret.geometry
        for node, comps in active_nodes(ts['#hierarchy']):
            if node['name'] not in ('hull', self.turret_node):
                continue
            parts = [world_vertices(tg[b['rb.datasource']].vertices,
                                    comps.get('TransformComponent', {})) for b in batches(comps)]
            if not parts:
                continue
            verts = np.concatenate(parts)
            if node['name']=='hull':
                verts = verts + definition.hull_position
            self.tank_shapes.append((node['name'], verts))
        # Tracks have no armor collision mesh. Their visible dimensions and
        # lower outline supply the support layout and procedural contact prism.
        vs = scene(Path(str(definition.visual_asset)+'.sc2.dvpl'))
        vg = geometry(Path(str(definition.visual_asset)+'.scg.dvpl'))
        for node, comps in active_nodes(vs['#hierarchy']):
            if node['name'] not in ('chassis_track_L', 'chassis_track_R'):
                continue
            parts = [world_vertices(vg[b['rb.datasource']].vertices,
                                    comps.get('TransformComponent', {})) for b in batches(comps)]
            if parts:
                self.tank_shapes.append((node['name'], np.concatenate(parts)))
        required = {'hull','chassis_track_L','chassis_track_R'}|({self.turret_node} if self.turret_node else set())
        if {name for name,_ in self.tank_shapes} != required:
            raise ValueError('Missing vehicle collision shapes')
        from .physics_proxy import vehicle_proxies
        collision_mode = os.environ.get('SOLBREAKER_VEHICLE_COLLISION', 'proxy').strip().lower()
        if collision_mode not in ('proxy', 'mesh'):
            raise ValueError('SOLBREAKER_VEHICLE_COLLISION must be proxy or mesh')
        # Detailed arrays remain dimensional/support and mass inputs only.
        # Bullet receives bounded procedural shapes; armor has its independent
        # triangle model below. Keep mesh mode solely for measured A/B checks.
        self.contact_proxies = (vehicle_proxies(self.tank_shapes, self.turret_node,
                                               definition.drive.support)
                                if collision_mode == 'proxy' else None)
        hull = next(vertices for name,vertices in self.tank_shapes if name=='hull')
        self.hull_center = tuple((hull.min(axis=0)+hull.max(axis=0))*.5)
        self.support = definition.drive.support
        self.max_slope = definition.drive.max_slope
        self.forward_speed = definition.drive.forward
        self.reverse_speed = definition.drive.reverse
        self.rotation_speed = definition.drive.rotation
        from .armor import ArmorModel, load_movable_armor_geometry
        controller = definition.armor_states_controller
        movable_geometry = None
        if controller is not None:
            # ``load_battle_vehicle`` has already admitted this definition
            # through the independent XML-to-SC2 validation route.  Repeat
            # the identity check at the consumer boundary: accepting a
            # geometry cache for only a subset (or another ordering) of this
            # vehicle's controller would let a later state packet select a
            # collision plate which was never part of this vehicle.
            movable_geometry = load_movable_armor_geometry(game, definition.nation,
                                                            definition.name)
            if movable_geometry is None:
                raise ValueError('Missing movable armor collision geometry')
            controller_ids = tuple(state.ident for state in controller.states)
            geometry_ids = tuple(state.ident for state in movable_geometry.states)
            if controller_ids != geometry_ids:
                raise ValueError('Movable armor controller and geometry state IDs differ')
        # State-specific plates exist only for armor ray/HE queries. They must
        # not change the fixed dimensional proxy or its support/mass model.
        self.armor = ArmorModel(game,definition,movable_geometry=movable_geometry)
        from .hit_effects import load_frames
        self.hit_effects = load_frames(game, definition)

    def prepare_map(self, terrain):
        self.mesh_files.partition(self.vertices,self.triangles)
        for obj in self.destructibles.values():
            self.mesh_files.partition(obj.vertices,obj.triangles)
        return self.prepare_terrain(terrain)

    def prepare(self, terrain):
        """Prepare collision meshes and validate the vehicle armor backend."""
        self.prepare_map(terrain)
        if self.contact_proxies is None:
            for _,vertices in self.tank_shapes:
                self.mesh_files.mesh(vertices)
        else:
            for part in self.contact_proxies:
                if part.kind == 'prism':
                    self.mesh_files.mesh(part.vertices)
        # ABI/numerical validation can lazily import NumPy kernels. Keep that
        # first-use cost in scene loading rather than the first live armor ray.
        from .armor_native import load_library as load_armor_library
        load_armor_library()

    def prepare_terrain(self, terrain):
        if terrain not in self.terrain_meshes:
            n = terrain.size
            x,z = np.meshgrid(np.arange(n)*terrain.WIDTH/n+terrain.MIN,
                             np.arange(n)*terrain.DEPTH/n+terrain.MIN_Z)
            y = terrain.HEIGHT_MIN + np.asarray(terrain.heights, dtype=float).reshape(n,n)*terrain.HEIGHT/65535
            vertices = np.column_stack((x.ravel(),y.ravel(),z.ravel()))
            base = (np.arange(n-1)[:,None]*n+np.arange(n-1)).ravel()
            indices = np.concatenate((np.column_stack((base,base+n,base+1)),
                                      np.column_stack((base+1,base+n,base+n+1))))
            self.terrain_meshes[terrain] = self.mesh_files.partition(vertices,indices)
        return self.terrain_meshes[terrain]

    def close(self):
        if self._owns_mesh_files:
            self.mesh_files.close()
            self.terrain_meshes.clear()


@dataclass(frozen=True)
class SupportLayout:
    """Track contact stations in the hull's yaw frame.

    Each station is a lateral/longitudinal offset (a, b) on one track and the
    height ``p`` of that track's lower collision surface above the carrying
    plane. Contact-patch stations (weight 1) set the compliant support plane;
    ramp stations (weight 0) only keep the sloped track ends out of the ground.
    """
    a: np.ndarray
    b: np.ndarray
    p: np.ndarray
    weight: np.ndarray
    # Per track: lateral extent and the lower (z, y) envelope above the plane.
    tracks: tuple
    patch: float

    @cached_property
    def plane_rows(self):
        return np.column_stack((np.ones_like(self.a), self.a, self.b))

    @cached_property
    def fit_rows(self):
        return np.vstack((self.plane_rows, ((1., 0., 0.),)))

    @cached_property
    def full_support(self):
        active = np.flatnonzero(self.weight > 0.)
        supported = (len(active)>0 and self.a[active].min()<=.05 and self.a[active].max()>=-.05
                     and self.b[active].min()<=.05 and self.b[active].max()>=-.05)
        rank = (np.linalg.matrix_rank(np.column_stack((np.ones(len(active)),
                        self.a[active],self.b[active]))) if len(active)>=3 else 0)
        return active,supported,rank

    def track_profile(self, a, b, margin=0.):
        """Track surface height above the carrying plane at (a, b), or None."""
        for low, high, zs, ys in self.tracks:
            if low-margin <= a <= high+margin:
                if b < zs[0]-margin or b > zs[-1]+margin:
                    return None
                return 0. if abs(b) <= self.patch else max(0., float(np.interp(b, zs, ys)))
        return None


class _RecoveryClosestQueries:
    """Cache immutable map results while the arena searches for one hull pose."""

    def __init__(self, map_bodies, reuse_filtered):
        self.getNumJoints = bullet.getNumJoints
        self.getAABB = bullet.getAABB
        self.getOverlappingObjects = bullet.getOverlappingObjects
        self.map_bodies = map_bodies
        self.cache = {}
        self.filtered = {} if reuse_filtered else None
        self.pose = None

    def getClosestPoints(self, sensor, body, distance, *, physicsClientId):
        if body not in self.map_bodies:
            return _RECOVERY_CLOSEST(sensor,body,distance,physicsClientId=physicsClientId)
        key = (sensor, body, self.pose, distance)
        points = self.cache.get(key)
        if points is None:
            points = _RECOVERY_CLOSEST(sensor,body,distance,physicsClientId=physicsClientId)
            self.cache[key] = points
        return points


class _PivotMapPairQueries:
    """Exact raw Bullet contacts with static meshes for one sensor shape."""

    def __init__(self, world, body, assets):
        self.world, self.body, self.assets = world, body, assets
        self.cache = {}
        self.cached_points = 0
        self.turret = 0.
        self.pose = None
        self.getNumJoints = bullet.getNumJoints
        self.getAABB = bullet.getAABB
        self.getOverlappingObjects = bullet.getOverlappingObjects

    def getClosestPoints(self, sensor, target, distance, *, physicsClientId):
        if target not in self.world.map_bodies or target in self.world.breakable_bodies:
            return _PIVOT_CLOSEST(sensor,target,distance,physicsClientId=physicsClientId)
        key = (self.pose, target, distance)
        rows = self.cache.get(key)
        if rows is None:
            rows = _PIVOT_CLOSEST(sensor,target,distance,physicsClientId=physicsClientId)
            count = len(rows)
            if count <= 1024:
                if len(self.cache) >= 256 or self.cached_points + count > 1024:
                    self.cache.clear()
                    self.cached_points = 0
                self.cache[key] = rows
                self.cached_points += count
        return rows


class CollisionWorld:
    collision_zone = None
    # A private timeline can bound its active hull without modifying map assets.
    movement_circle = None  # (entity, center_x, center_z, radius)
    open_map_borders = frozenset()
    SKIN = .008
    STEP = .06
    GRAVITY = 9.81
    # Longest gap between support stations along one contact patch. The
    # carrying points remain the outermost stations, so a uniform grade gives
    # the same pose as before while shorter features are carried by the
    # stations under them instead of by whichever corner meets them first.
    SUPPORT_SPACING = .5
    # Head-room above the hull reference within which a map surface can carry
    # a track. Taller surfaces are obstacles, never support.
    SUPPORT_REACH = .28
    # Track contact can compress relative to the hull. The visible belt stays
    # rigid for now; its small overlap with a drivable surface represents the
    # suspension travel of the road wheels inside it, never a wall contact.
    SUSPENSION_TRAVEL = .10
    # Give heave and hull attitude time to follow abrupt changes in grade.
    SUSPENSION_OMEGA = 26.
    SUSPENSION_ANGULAR_OMEGA = 12.
    # Vertical speed by which the ground may fall away from the gravity-bound
    # hull before it separates. Smaller crest/kink hops (under ~5 cm) are
    # absorbed as track travel rather than becoming flights of a few frames.
    RELEASE_SPEED = 1.
    DYNAMIC_SETTLE_SPEED = 1.2
    # A quiet landing need not lose all horizontal momentum before returning
    # to the tracked controller. The tangent component is preserved at handoff.
    DYNAMIC_SETTLE_TANGENT_SPEED = 2.
    DYNAMIC_SETTLE_ANGULAR_SPEED = 1.2
    DYNAMIC_SETTLE_STEPS = 2
    # Maximum disagreement between Bullet's settled pose and the support fit.
    # This permits a tank resting on a steep hill without accepting a side or
    # roof rest as ordinary tracked ground motion.
    DYNAMIC_GROUND_ALIGNMENT = math.cos(math.radians(35))
    DYNAMIC_HANDOFF_DISTANCE = .20
    # A newly handed-off body may penetrate a terrain triangle by a few cm
    # because the grounded solver samples a finite number of support points.
    # Resolve only such small measured overlaps; larger conflicts stay in the
    # swept fallback instead of being teleported out of geometry.
    DYNAMIC_MAX_START_CORRECTION = .20
    # Ground motion samples orientation once per solver slice. Retain the
    # measured rotation when that support is lost, but reject an impossible
    # one-frame terrain-fit jump from becoming an explosive Bullet launch.
    # Ordinary tracked movement never reads this value: it is Phase-A-only.
    DYNAMIC_MAX_LAUNCH_ANGULAR_SPEED = math.tau
    # The sandbox can deliberately launch from steep map faces. Normal
    # tracked driving should not carry a one-slice support-fit spike into
    # Bullet as a large upward or tumbling impulse.
    TRACKED_MAX_UPWARD_SPEED = 3.
    TRACKED_MAX_LAUNCH_ANGULAR_SPEED = 2.
    DYNAMIC_MIN_CLEARANCE = .40
    # Bullet's broad phase is discrete.  The arena can ask for a 60 Hz tick,
    # while a released hull can retain a large horizontal speed from the track
    # solver.  Integrate every experimental dynamic tick in deterministic
    # 240 Hz slices so two flying vehicles cannot swap sides between one
    # broad-phase update and the next.  This remains opt-in with 6DOF; the
    # established kinematic drive does not call Bullet stepping.
    DYNAMIC_MAX_STEP = 1. / 240.
    DYNAMIC_LINEAR_DAMPING = .003
    DYNAMIC_ANGULAR_DAMPING = .05
    DYNAMIC_DEEP_TERRAIN_OVERLAP = .75
    LOW_GRAVITY_TERRAIN_OVERLAP = .12
    # A later collision after a real gap starts a new fall-impact episode.
    DYNAMIC_FALL_GAP_STEPS = 60
    # Explicit local impact-layer parameters. They are arena-wide sandbox
    # rules, not inferred from MKM codes or the unlabelled XML resistance rows.
    GROUND_IMPACT_RESTITUTION = 0.
    GROUND_IMPACT_FRICTION = .50
    GROUND_IMPACT_MIN_CLOSING_SPEED = .05
    RAM_TRACK_SPEED_RETENTION = .65
    RAM_TRACK_YAW_RETENTION = .50
    RAM_REARM_CLEAR_STEPS = 15
    # Track side grip is stronger than hull-on-hull impact friction.
    GROUND_LATERAL_GRIP = 1.80 * GRAVITY
    DYNAMIC_RECOVER_AFTER = 2.5
    # Local contact calibration for the free rigid body only. Track-supported
    # belly contacts retain the previous grip; a sliding roof/side has less
    # static friction, without changing inertia, impulses or ordinary driving.
    DYNAMIC_TRACK_FRICTION = .8
    DYNAMIC_HULL_FRICTION = .35
    DYNAMIC_GRIP_BLEND_RATE = 16.
    DYNAMIC_STUCK_RADIUS = .5
    GROUND_YAW_DAMPING = 1.5
    # Proximity alone must not trigger an impact before a swept contact.
    # Swept blocked moves already contribute their candidate contact patch.
    GROUND_CONTACT_GAP = SKIN
    GROUND_IMPACT_TRACE_LIMIT = 16
    # Local ram rule: only a pair entering contact (absent from the previous
    # resolved tick) at this normal closing speed is a ram. Sustained pushing
    # and tangential scraping therefore never qualify.
    RAM_MIN_CLOSING_SPEED = 1.5
    # Local wall-recoil rule for the tracked 6DOF solver. A grounded
    # hull meeting a solid map face (taller than track reach) at a real normal
    # closing speed leaves the tick with a small outward velocity along the
    # measured face normal. Arena's drivetrain then brakes it within a few
    # ticks, every backward increment swept by the ordinary contact solver.
    # Pushing, scraping and a hull still near its last recoil never qualify.
    WALL_RECOIL_MIN_CLOSING_SPEED = 1.5
    # Normal closing speed over approach speed: at least 30 deg off the face.
    WALL_RECOIL_MIN_INCIDENCE = .5
    WALL_RECOIL_RESTITUTION = .15
    WALL_RECOIL_MAX_SPEED = 1.
    WALL_RECOIL_REARM_DISTANCE = .25
    WALL_GLANCE_YAW_RATE = math.radians(125.)
    ROCK_IMPACT_MIN_SPEED = 3.

    def __init__(self, assets, terrain, player_position=None, target_position=None, *, vehicles=None,
                 incremental=False, vehicle_assets=None, experimental_6dof=None,
                 experimental_ground_impacts=None, wall_trampoline=None,
                 sprung_tracks=None, visibility=False, gravity=9.81, collision_zone=None):
        self.collision_zone = collision_zone
        self._body_zones = {}
        self.assets, self.terrain = assets, terrain
        if not math.isfinite(gravity) or gravity <= 0.:
            raise ValueError('World gravity must be positive and finite')
        self.GRAVITY = gravity
        self.GROUND_LATERAL_GRIP = 1.8 * math.sqrt(gravity * CollisionWorld.GRAVITY)
        self.vehicle_assets = dict(vehicle_assets or {})
        available_cpus = (len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity')
                          else os.cpu_count() or 1)
        self._ray_threads = min(2, available_cpus)
        # Accepted driving model. Keep the historical flag names for launch
        # compatibility; explicit 0 still selects the legacy solver.
        self.experimental_6dof = (os.environ.get('SOLBREAKER_EXPERIMENTAL_6DOF', '1') == '1'
                                   if experimental_6dof is None else bool(experimental_6dof))
        self.experimental_ground_impacts = (
            os.environ.get('SOLBREAKER_EXPERIMENTAL_GROUND_IMPACTS', '1') == '1'
            if experimental_ground_impacts is None else bool(experimental_ground_impacts))
        self.wall_trampoline = (os.environ.get('SOLBREAKER_WALL_TRAMPOLINE') == '1'
                                if wall_trampoline is None else bool(wall_trampoline))
        self.sprung_tracks = (self.experimental_6dof and not self.wall_trampoline
                             and os.environ.get('SOLBREAKER_SPRUNG_TRACKS', '1') == '1'
                             if sprung_tracks is None else bool(sprung_tracks))
        native = os.environ.get('SOLBREAKER_NATIVE_PHYSICS', '1')
        if native not in ('0', '1'):
            raise ValueError('SOLBREAKER_NATIVE_PHYSICS must be 0 or 1')
        self._native_physics_enabled = native == '1' and (
            collision_zone is None or isinstance(collision_zone, CircularZone))
        self.cid = None
        self.closed = False
        self.closing = False
        self._closer = None
        self.build_ready = False
        self.last_contact = None
        self.last_wall_recoil = None
        self._wall_recoil_latches = {}
        self._wall_glances = {}
        self.trace_dynamic_contacts = os.environ.get('SOLBREAKER_DRIVE_TRACE') == '1'
        self.dynamic_contact_trace = {}
        self.last_reject_reason = None
        self._last_validation_rejection = None
        self.motion = {}
        self.dynamic_bodies = {}
        self.pending_dynamics = {}
        self._separating_launches = {}
        self._blocked_launches = {}
        self.suspended_entities = set()
        self.dynamic_shapes = {}
        self._shared_dynamic_shapes = {}
        self._mass_models = {}
        self._terrain_probes = {}
        self._kinematic_shapes = {}
        self.dynamic_bounds = {}
        self.dynamic_fall_peak = {}
        self.dynamic_fall_gap = {}
        self.fall_impacts = []
        self.track_impacts = []
        self._fall_episodes = {}
        self._fall_peaks = {}
        self._track_impact_peaks = {}
        self.dynamic_mass_yaws = {}
        self.dynamic_launch_angular_velocities = {}
        self.short_flight_spins = {}
        self.dynamic_recoveries = []
        self.dynamic_orientation_resets = []
        self.body_entities = {}
        self.ground_motion = {}
        self.ground_commands = {}
        self.ground_track_rates = {}
        self.ground_contacts = {}
        self.ground_mass_properties = {}
        self.last_ground_impacts = ()
        self.ground_contact_pairs = frozenset()
        self._ground_ram_clear_steps = {}
        self.air_ram_impacts = {}
        self._air_tank_contacts = set()
        self._synced = {}
        self._support_layouts = {}
        self._support_ranks = {}
        self._support_systems = {}
        self._chassis_bounds = {}
        self._component_bound_cache = {}
        # Explicit provenance for process-local static IDs. The pinned Bullet
        # build reports "unknown_file" from getCollisionShapeData for OBJ
        # triangle meshes, so a replica cannot recover this mapping later.
        self._mesh_body_paths = {}
        self._hull_clearances = {}
        self.track_links = {}
        self._support_memory = {}
        self._support_hints = {}
        self._support_bounds = {}
        self._terrain_grid = None
        self._road_poses = {}
        self._drive_efforts = {}
        self._spring_rates = {}
        self._lean_states = {}
        self._drive_blocked = {}
        self._carrying_supported = {}
        self._contact_support_cache = {}
        self._rust_drive = None
        self._native_physics = None
        self.build_timings = {}
        self._builder = self._build(assets,terrain,player_position,target_position,vehicles)
        if not incremental:
            while not self.advance_build():
                pass

    def advance_build(self):
        if self.closed or self.closing:
            raise ValueError('Collision world is closed')
        if self.build_ready:
            return True
        try:
            next(self._builder)
            return False
        except StopIteration:
            self._builder = None
            self.build_ready = True
            return True
        except BaseException:
            self.close()
            raise

    def _build(self,assets,terrain,player_position,target_position,vehicles):
        started = time.monotonic()
        started_cpu = time.thread_time()
        self.cid = bullet.connect(bullet.DIRECT)
        if self.experimental_6dof:
            bullet.setGravity(0, -self.GRAVITY, 0, physicsClientId=self.cid)
        yield
        self.map_bodies = set()
        for path in assets.mesh_files.partition(assets.vertices,assets.triangles):
            self.map_bodies.add(self._mesh_body(path))
            yield
        self.breakable_bodies = {}
        self.object_bodies = {}
        self.object_health = {k:o.health for k,o in assets.destructibles.items()}
        self.destroyed = set()
        self.destruction_events = []
        for key,obj in assets.destructibles.items():
            bodies = set()
            for path in assets.mesh_files.partition(obj.vertices,obj.triangles):
                bodies.add(self._mesh_body(path, breakable=True))
                yield
            self.object_bodies[key] = bodies
            self.breakable_bodies.update((b,key) for b in bodies)
            self.map_bodies.update(bodies)
        self.terrain_bodies = set()
        for path in assets.prepare_terrain(terrain):
            self.terrain_bodies.add(self._mesh_body(path))
            yield
        # Query-only worlds do not get Bullet's regular broadphase upkeep
        # from simulation steps. Prepare the static tree before adding tanks.
        for _ in range(10):
            bullet.performCollisionDetection(physicsClientId=self.cid)
            yield
        physical_started = time.monotonic()
        physical_cpu = time.thread_time()
        self.build_timings['collision_ms'] = (physical_started-started)*1000
        self.build_timings['collision_cpu_ms'] = (physical_cpu-started_cpu)*1000
        self.build_timings['static_bodies'] = len(self._mesh_body_paths)
        self.bodies = {}
        self.poses = {}
        self.turret_links = {}
        self.gun_angles = {}
        self.motion = {}
        self.dynamic_bodies = {}
        self.pending_dynamics = {}
        self.dynamic_shapes = {}
        self.dynamic_mass_yaws = {}
        self.dynamic_launch_angular_velocities = {}
        self.short_flight_spins = {}
        self._synced = {}
        vehicles = vehicles or {200:Pose(player_position,0),201:Pose(target_position,0)}
        if self.vehicle_assets and set(self.vehicle_assets) != set(vehicles):
            raise ValueError('Vehicle assets must match the world entities')
        self.primary_id, self.target_id = list(vehicles)[:2]
        for entity,pose in vehicles.items():
            body,link = self._tank(pose.position, group=_TRACKED_GROUP, entity=entity)
            self.bodies[entity],self.turret_links[entity] = body,link
            self.track_links[entity] = frozenset(i for i,(name,_) in enumerate(
                (pair for pair in self.assets_for(entity).tank_shapes if pair[0] != 'hull'))
                if name.startswith('chassis_track_'))
            self.body_entities[body] = entity
            self.poses[entity] = self.ground_pose(*pose.position[::2],pose.yaw,entity=entity)
            self._road_poses[entity] = self.poses[entity]
            self.gun_angles[entity] = (0.,0.)
            self.motion[entity] = Motion()
            self.ground_motion[entity] = GroundMotion()
            self._synced[entity] = (self.poses[entity],self.motion[entity])
            self.set_pose(body,self.poses[entity])
            yield
            if self.experimental_6dof and (getattr(self.assets_for(entity), '_convex_shapes', None) is not None
                    or (isinstance(self.assets_for(entity), CollisionScene)
                        and getattr(self.assets_for(entity), 'contact_proxies', None))):
                # Prepare the yaw-independent flight shape while the world
                # is loading. Identical vehicles share its immutable Bullet
                # shape instead of rebuilding a dense turret sweep on a fall.
                vehicle_assets = self.assets_for(entity)
                cached = self._shared_dynamic_shapes.get(id(vehicle_assets))
                if cached is None or cached[0] is not vehicle_assets:
                    shape = self._dynamic_shape(entity)
                    cached = (vehicle_assets, shape, self.dynamic_bounds[entity])
                    self._shared_dynamic_shapes[id(vehicle_assets)] = cached
                self.dynamic_shapes[entity] = cached[1]
                self.dynamic_bounds[entity] = cached[2]
                yield
        yield from self._build_ground_physics()
        self.build_timings['physics_ms'] = (time.monotonic()-physical_started)*1000
        self.build_timings['physics_cpu_ms'] = (time.thread_time()-physical_cpu)*1000
        self.build_timings['vehicle_bodies'] = len(self.bodies)

    def _build_ground_physics(self):
        if self.experimental_ground_impacts:
            self.ground_mass_properties = {
                entity:self._dynamic_mass_properties(entity,self.gun_angles[entity][0])
                for entity in self.bodies}
        from .sim_native import RustDrive
        self._rust_drive = RustDrive(self)
        if self._rust_drive.library is not None:
            # Copy the compact immutable height grid during incremental
            # loading, rather than adding an allocation to the first input.
            self._rust_drive._prepare_terrain()
            yield
        if self._native_physics_enabled:
            if not self.sprung_tracks or self.wall_trampoline:
                raise ValueError('Native physics requires sprung tracked physics; '
                                 'set SOLBREAKER_NATIVE_PHYSICS=0 for legacy diagnostics')
            from .native_physics import NativePhysics
            native_started, native_cpu = time.monotonic(), time.thread_time()
            self._native_physics = NativePhysics(self)
            self.build_timings['native_ground_ms'] = (time.monotonic()-native_started)*1000
            self.build_timings['native_ground_cpu_ms'] = (time.thread_time()-native_cpu)*1000
            yield

    def _static_rays(self, starts, ends, *, _single=False):
        """Map/terrain/breakable rays. Vehicle armor keeps its exact model."""
        if _single:
            return (bullet.rayTest(starts[0], ends[0], collisionFilterMask=_STATIC_GROUP,
                                   physicsClientId=self.cid)[0],)
        # Large visibility batches amortize a second Bullet worker.
        return bullet.rayTestBatch(starts, ends, collisionFilterMask=_STATIC_GROUP,
                                   numThreads=(getattr(self, '_ray_threads', 1)
                                               if len(starts) >= 128 else 1),
                                   physicsClientId=self.cid)

    def _static_ray(self, start, end):
        return self._static_rays([start], [end], _single=True)[0]

    def _visibility_rays(self, starts, ends):
        """Trace sight lines against the same terrain and live props as movement."""
        return self._static_rays(starts, ends)

    def assets_for(self, entity=None):
        entity = self.primary_id if entity is None else entity
        return self.vehicle_assets[entity] if self.vehicle_assets else self.assets

    def _component_bounds(self, entity):
        """Immutable source dimensions, without rescanning dense tracks on impact."""
        assets = self.assets_for(entity)
        cached = self._component_bound_cache.get(id(assets))
        if cached is None or cached[0] is not assets:
            bounds = tuple((name, np.min(vertices,axis=0), np.max(vertices,axis=0))
                           for name,vertices in assets.tank_shapes)
            cached = self._component_bound_cache[id(assets)] = (assets,bounds)
        return cached[1]

    def auto_recovery_remaining(self, entity, *, showing=False):
        dynamic = self.dynamic_bodies.get(entity)
        if dynamic is None:
            return None
        # The anchor can reset in a single step when a tipped tank slides.
        # Wait for a settled onset, then keep the warning through such resets
        # and through small oscillations around the entry angle.
        if not showing and dynamic.stationary_time < .2:
            return None
        pose = self.poses[entity]
        upright = math.cos(pose.pitch) * math.cos(pose.roll)
        if upright >= (.6 if showing else .5):
            return None
        return max(0., self.DYNAMIC_RECOVER_AFTER - dynamic.stationary_time)

    def is_submerged(self, entity):
        environment = getattr(self.assets, 'environment', None)
        if environment is None or not environment.water:
            return False
        pose = self.poses[entity]
        x, y, z = pose.position
        level = environment.water_height(x, z)
        if level is None or self.terrain.height(x, z) >= level:
            return False
        rotation = self._dynamic_rotation(entity)
        if rotation is None:
            rotation = matrix(pose.yaw, pose.pitch, pose.roll)
        hull = next(vertices for name, vertices in self.assets_for(entity).tank_shapes
                    if name == 'hull')
        return bool(y + np.max(hull @ rotation[1]) <= level)

    def spotting_rays(self, observer_entity, target_entity, port=0):
        """Six visibility checks from one of two estimated vehicle viewports.

        This local approximation uses selected hull/turret bounds and the XML
        gun pivot. These are not recovered native visibility-point coordinates.
        Port 0 uses the neutral roof location fixed to the hull; port 1 follows
        turret yaw at the mantlet. One cached point set per entity is replaced
        whenever its selected assets, hull pose or turret yaw changes.
        """
        if type(port) is not int or port not in (0, 1):
            raise ValueError('Spotting viewport must be 0 or 1')
        cache = getattr(self, '_spotting_point_cache', None)
        if cache is None:
            cache = self._spotting_point_cache = {}

        def points(entity):
            assets = self.assets_for(entity)
            pose = self.poses[entity]
            yaw = self.gun_angles.get(entity, (0., 0.))[0]
            cached = cache.get(entity)
            if (cached is not None and cached[0] is assets
                    and cached[1] == pose and cached[2] == yaw):
                return cached[3], cached[4]
            bounds = {name: (low, high) for name, low, high in self._component_bounds(entity)}
            if 'hull' not in bounds:
                raise ValueError('Spotting geometry requires hull bounds')
            low, high = bounds['hull']
            middle = (low + high) * .5
            aiming = getattr(assets.definition, 'aiming', None)
            pivot = np.asarray(getattr(aiming, 'turret_position', (0., 0., 0.)), dtype=float)
            turret = bounds.get(assets.turret_node)
            neutral_roof = roof = (float(middle[0]), float(high[1]), float(middle[2]))
            if turret is not None and pivot[1] + turret[1][1] > high[1]:
                turret_low, turret_high = turret
                turret_middle = (turret_low + turret_high) * .5
                turret_roof = (turret_middle[0], turret_high[1], turret_middle[2])
                # The static viewport keeps the neutral top-component center.
                # A target's checkpoint remains on its actual rotating roof.
                neutral_roof = tuple(pivot + turret_roof)
                roof = tuple(pivot + matrix(yaw) @ turret_roof)
            gun = getattr(aiming, 'gun_position', None)
            if gun is None:
                mantlet = (float(middle[0]), float(low[1] + (high[1]-low[1])*.75), float(high[2]))
            else:
                mantlet = tuple(pivot + matrix(yaw) @ np.asarray(gun, dtype=float))
            local = ((middle[0], middle[1], high[2]),
                     (middle[0], middle[1], low[2]),
                     (low[0], middle[1], middle[2]),
                     (high[0], middle[1], middle[2]), roof, mantlet)
            rotation = matrix(pose.yaw, pose.pitch, pose.roll)
            origin = np.asarray(pose.position, dtype=float)

            def world(point):
                return tuple(float(value) for value in origin + rotation @ point)

            checkpoints = tuple(world(point) for point in local)
            ports = (world(neutral_roof), checkpoints[5])
            cache[entity] = (assets, pose, yaw, ports, checkpoints)
            return ports, checkpoints

        observer, _ = points(observer_entity)
        _, target = points(target_entity)
        return tuple((observer[port], end) for end in target)

    def drive_pitch(self, entity=None):
        """Physical road pitch for traction, independent of hull suspension lean."""
        entity = self.primary_id if entity is None else entity
        return self._road_poses.get(entity,self.poses[entity]).pitch

    def _has_suspension(self, entity):
        links = self.track_links.get(entity,())
        return (self.sprung_tracks and bool(links) and
                bullet.getNumJoints(self.bodies[entity],physicsClientId=self.cid) > max(links))

    def _landing_support(self, x, z, yaw, reference, entity, *, contact_result=None):
        """The same compliant contact envelope used while driving."""
        suspension = self._has_suspension(entity)
        extra = self._cached_contact_supports(entity,x,z,yaw)
        candidate = self._support_pose(x,z,yaw,reference,entity=entity,extra=extra,
            compression=self.SUSPENSION_TRAVEL if suspension else 0.)
        if suspension:
            body = self.bodies[entity]
            saved_position,saved_orientation = bullet.getBasePositionAndOrientation(
                body,physicsClientId=self.cid)
            contacts = self._contacts
            if getattr(contacts, '__func__', None) is _CONTACTS_IMPL:
                contacts = partial(contacts, landing_probe=True)
            try:
                for _ in range(3):
                    points = contacts(candidate,entity,suspension=True)
                    found = self._contact_supports(entity,candidate,points,reference)
                    if not found:
                        if contact_result is not None:
                            contact_result.append(points)
                        break
                    extra += found
                    candidate = self._support_pose(x,z,yaw,reference,entity=entity,
                        extra=extra,compression=self.SUSPENSION_TRAVEL)
            finally:
                # _contacts temporarily places the sensor at its candidate.
                # A support query must never leave the authoritative sensor
                # on the road while the logical hull is still airborne.
                bullet.resetBasePositionAndOrientation(body,saved_position,saved_orientation,
                                                       physicsClientId=self.cid)
        return candidate

    def set_drive_effort(self, entity, longitudinal_accel, lateral_accel, throttle):
        """Set signed horizontal accelerations and effort for visual suspension.

        These values only deflect the sprung hull. They never alter the road
        normal, traction or free-body momentum.
        """
        values = tuple(float(value) for value in (longitudinal_accel,lateral_accel,throttle))
        if not all(math.isfinite(value) for value in values):
            raise ValueError('Invalid drive effort')
        self._drive_efforts[entity] = values

    def _can_carry_origin(self, entity, pose):
        """Use the drive solver's actual droop footprint for every landing."""
        self._support_pose(pose.position[0],pose.position[2],pose.yaw,
            pose.position[1],entity=entity,
            extra=self._cached_contact_supports(entity,*pose.position[::2],pose.yaw),
            compression=self.SUSPENSION_TRAVEL,carrying=pose)
        return self._carrying_supported.get(entity,True)

    @property
    def player(self):
        return self.bodies[self.primary_id]

    @property
    def target(self):
        return self.bodies[self.target_id]

    @property
    def pose(self):
        return self.poses[self.primary_id]

    @pose.setter
    def pose(self,value):
        self.poses[self.primary_id] = value

    @property
    def target_pose(self):
        return self.poses[self.target_id]

    def _mesh_body(self, path, *, breakable=False):
        shape = create_bullet_mesh(bullet, path,
                    flags=bullet.GEOM_FORCE_CONCAVE_TRIMESH,
                    physicsClientId=self.cid)
        # Map chunks have no joints. A fixed rigid body keeps them out of the
        # articulated-body solver's per-substep pass over the whole arena.
        body = bullet.createMultiBody(baseMass=0, baseCollisionShapeIndex=shape,
                                      useMaximalCoordinates=True, physicsClientId=self.cid)
        # Native consumers open this same file from the arena's asset lease.
        # An absolute name suffices and avoids filesystem traversal per body.
        self._mesh_body_paths[body] = os.path.abspath(path)
        # Bullet can recycle a removed body's id, even for different geometry
        # created at the same transform. Recheck any blocked launches.
        self._blocked_launches.clear()
        # Keep breakable meshes queryable, without applying solver impulses.
        mask = _RAY_GROUP
        if not breakable:
            mask |= _DYNAMIC_GROUP
            if self.collision_zone:
                mask |= _ZONE_DYNAMIC_GROUP
        bullet.setCollisionFilterGroupMask(body, -1, _STATIC_GROUP, mask,
                                           physicsClientId=self.cid)
        return body

    def _mesh_bodies(self, vertices, triangles):
        return {self._mesh_body(path) for path in self.assets.mesh_files.partition(vertices,triangles)}

    def _tank(self, position, group, entity=None):
        assets = self.assets_for(entity)
        # Bullet retains shapes until the world disconnects. Polygon targets
        # reuse their stock shapes by descriptor while releasing scene meshes.
        key = getattr(assets, 'practice_shape_key', None)
        owner = key if key is not None else assets
        cache_key = key if key is not None else id(assets)
        cached = self._kinematic_shapes.get(cache_key)
        if cached is None:
            shapes, names = [], []
            proxies = getattr(assets, 'contact_proxies', None)
            if proxies:
                for part in proxies:
                    names.append(part.name)
                    shapes.append(self._proxy_shape(assets, part))
            else:
                for name, verts in assets.tank_shapes:
                    names.append(name)
                    shapes.append(create_bullet_mesh(bullet, assets.mesh_files.mesh(verts),
                                                     physicsClientId=self.cid))
            cached = self._kinematic_shapes[cache_key] = (owner, shapes, names)
        _, shapes, names = cached
        hull = names.index('hull')
        links = [(name, shape) for name,shape in zip(names,shapes) if name != 'hull']
        count = len(links)
        body = bullet.createMultiBody(baseMass=0, baseCollisionShapeIndex=shapes[hull],
            basePosition=position, linkMasses=[0]*count,
            linkCollisionShapeIndices=[s for _,s in links], linkVisualShapeIndices=[-1]*count,
            linkPositions=[assets.definition.aiming.turret_position if name==assets.turret_node else (0,0,0) for name,_ in links],
            linkOrientations=[(0,0,0,1)]*count, linkInertialFramePositions=[(0,0,0)]*count,
            linkInertialFrameOrientations=[(0,0,0,1)]*count, linkParentIndices=[0]*count,
            linkJointTypes=[bullet.JOINT_REVOLUTE if name==assets.turret_node else bullet.JOINT_FIXED
                            for name,_ in links], linkJointAxis=[(0,1,0)]*count,
            physicsClientId=self.cid)
        dynamic_mask = _DYNAMIC_GROUP
        if self.collision_zone and self.collision_zone(position) and group == _TRACKED_GROUP:
            group, dynamic_mask = _ZONE_TRACKED_GROUP, _ZONE_DYNAMIC_GROUP
        for index in range(-1,count):
            bullet.setCollisionFilterGroupMask(body, index, group, dynamic_mask,
                                                physicsClientId=self.cid)
            bullet.changeDynamics(body, index, collisionMargin=.001, physicsClientId=self.cid)
        turret_link = next((i for i,(name,_) in enumerate(links) if name==assets.turret_node),None)
        if (isinstance(assets, CollisionScene) and assets._convex_shapes is None
                and not getattr(assets, 'contact_proxies', None)):
            # These are Bullet's actual convex sensor hulls, already reduced
            # from the installed vertex clouds. Convex-hulling their rotated
            # union gives the same swept convex model without expanding a
            # 100k-vertex visual turret sixteen times in the simulation loop.
            hulls = []
            for index, name in [(-1, 'hull'), *enumerate(name for name,_ in links)]:
                count, vertices = bullet.getMeshData(body, index,
                    flags=bullet.MESH_DATA_SIMULATION_MESH, physicsClientId=self.cid)[:2]
                if count < 4:
                    raise ValueError('Incomplete vehicle convex collision hull')
                hulls.append((name, np.asarray(vertices, dtype=float)))
            assets._convex_shapes = tuple(hulls)
        return body,turret_link

    def _proxy_shape(self, assets, part):
        if part.kind == 'box':
            return bullet.createCollisionShape(bullet.GEOM_BOX,
                halfExtents=part.half_extents, collisionFramePosition=part.center,
                physicsClientId=self.cid)
        if part.kind == 'prism':
            return create_bullet_mesh(bullet, assets.mesh_files.mesh(part.vertices),
                                      physicsClientId=self.cid)
        raise ValueError(f'Unsupported grounded vehicle proxy: {part.kind}')

    def _sensor_filter(self, entity, group, mask):
        """Enable or disable the established kinematic sensor body."""
        body = self.bodies[entity]
        if (group == _TRACKED_GROUP and self.collision_zone
                and self.collision_zone(self.poses[entity].position)):
            group, mask = _ZONE_TRACKED_GROUP, _ZONE_DYNAMIC_GROUP
        for link in range(-1, bullet.getNumJoints(body, physicsClientId=self.cid)):
            bullet.setCollisionFilterGroupMask(body, link, group, mask,
                                                physicsClientId=self.cid)

    def _set_dynamic_zone(self, body, inside):
        tracked, dynamic = ((_ZONE_TRACKED_GROUP, _ZONE_DYNAMIC_GROUP) if inside
                            else (_TRACKED_GROUP, _DYNAMIC_GROUP))
        bullet.setCollisionFilterGroupMask(body, -1, dynamic, _STATIC_GROUP | tracked | dynamic,
                                           physicsClientId=self.cid)

    def refresh_collision_zones(self):
        # Explicit hull queries use positions; Bullet's substep solver needs
        # matching masks too. Only change filters on a membership transition.
        for entity, pose in self.poses.items():
            if entity in self.suspended_entities:
                continue
            dynamic = self.dynamic_bodies.get(entity)
            position = (bullet.getBasePositionAndOrientation(dynamic.body, physicsClientId=self.cid)[0]
                        if dynamic else pose.position)
            inside = self.collision_zone(position)
            if self._body_zones.get(entity) == inside:
                continue
            self._body_zones[entity] = inside
            if dynamic:
                self._set_dynamic_zone(dynamic.body, inside)
            else:
                self._sensor_filter(entity, _TRACKED_GROUP, _DYNAMIC_GROUP)

    def set_tracked_presence(self, entity, present):
        """Remove or restore a stationary hull for contact queries."""
        if present == (entity not in self.suspended_entities):
            return
        if entity in self.dynamic_bodies or entity in self.pending_dynamics:
            raise ValueError('Changing vehicle presence requires a stationary sensor')
        if self._native_physics is not None:
            self._native_physics.set_sensor_enabled(entity, present)
        body = self.bodies[entity]
        if present:
            self._sensor_filter(entity, _TRACKED_GROUP, _DYNAMIC_GROUP)
            self.body_entities[body] = entity
            self.suspended_entities.remove(entity)
        else:
            self._sensor_filter(entity, 0, 0)
            del self.body_entities[body]
            self.suspended_entities.add(entity)

    def _dynamic_shape(self, entity):
        """Compound convex hulls from the selected vehicle's collision mesh.

        Dynamic concave triangle meshes are static-only in the pinned Bullet
        build. Passing the same mesh vertex clouds as GEOM_MESH children of a
        positive-mass body makes Bullet build convex hulls instead. This keeps
        component cavities/angles that an AABB fit would fill, while retaining
        hull and track pieces as separate convex shapes. The turret is a
        convex hull of its full yaw sweep, so live aim cannot create another
        native collision shape each frame.
        """
        assets = self.assets_for(entity)
        if getattr(assets, 'contact_proxies', None):
            return self._dynamic_proxy_shape(entity, assets)
        file_names, temporary_files, positions, orientations = [], [], [], []
        shape_lows, shape_highs = [], []
        for name, vertices in (getattr(assets, '_convex_shapes', None) or assets.tank_shapes):
            position, orientation = (0., 0., 0.), (0., 0., 0., 1.)
            if name == assets.turret_node:
                pivot = np.asarray(assets.definition.aiming.turret_position, dtype=float)
                vertices = np.array(vertices, dtype=float, copy=True)
                # Scale the 16-gon beyond its circumradius so the polygon
                # circumscribes each vertex's continuous yaw circle.
                vertices[:,(0,2)] *= 1. / math.cos(math.pi / 16.)
                vertices = np.concatenate([vertices @ matrix(yaw).T + pivot
                    for yaw in np.linspace(0., math.tau, 16, endpoint=False)],axis=0)
            if (np.asarray(vertices).ndim != 2 or np.asarray(vertices).shape[1:] != (3,)
                    or not len(vertices) or not np.isfinite(vertices).all()):
                raise ValueError('Invalid vehicle dynamic collision geometry')
            extent = np.ptp(vertices, axis=0)
            if not np.isfinite(extent).all() or np.any(extent <= 0):
                raise ValueError('Degenerate vehicle dynamic collision geometry')
            shape_lows.append(np.min(vertices,axis=0))
            shape_highs.append(np.max(vertices,axis=0))
            filename, created = assets.mesh_files.mesh_with_creation_status(vertices)
            file_names.append(filename)
            if created:
                temporary_files.append(filename)
            positions.append(position)
            orientations.append(orientation)
        if not file_names:
            raise ValueError('Vehicle has no dynamic collision geometry')
        self.dynamic_bounds[entity] = (
            tuple(float(value) for value in np.min(shape_lows,axis=0)),
            tuple(float(value) for value in np.max(shape_highs,axis=0)))
        try:
            with bullet_mesh_paths(file_names) as compatible:
                shape = bullet.createCollisionShapeArray(shapeTypes=[bullet.GEOM_MESH]*len(file_names),
                    fileNames=compatible, meshScales=[(1.,1.,1.)]*len(file_names),
                    collisionFramePositions=positions,
                    collisionFrameOrientations=orientations,
                    physicsClientId=self.cid)
        finally:
            # PyBullet reads OBJ files synchronously. Remove these temporary
            # convex-mesh inputs; Bullet keeps the loaded shape until the
            # bounded collision world is disconnected.
            for filename in temporary_files:
                Path(filename).unlink(missing_ok=True)
        return shape

    def _dynamic_proxy_shape(self, entity, assets):
        """Four small parts, including one analytic full-yaw turret cylinder."""
        from .physics_proxy import swept_turret
        kinds, files, halves, radii, lengths, positions, orientations = [], [], [], [], [], [], []
        lows, highs = [], []
        for source in assets.contact_proxies:
            part = swept_turret(source) if source.name == assets.turret_node else source
            offset = (np.asarray(assets.definition.aiming.turret_position, dtype=float)
                      if source.name == assets.turret_node else np.zeros(3))
            if part.kind == 'prism':
                kinds.append(bullet.GEOM_MESH)
                files.append(assets.mesh_files.mesh(part.vertices))
                position = offset
                low, high = np.min(part.vertices, axis=0), np.max(part.vertices, axis=0)
            else:
                kinds.append(bullet.GEOM_CYLINDER if part.kind == 'cylinder' else bullet.GEOM_BOX)
                files.append('')
                position = np.asarray(part.center)+offset
                extent = (np.asarray((part.radius,part.length*.5,part.radius))
                          if part.kind == 'cylinder' else np.asarray(part.half_extents))
                low, high = np.asarray(part.center)-extent, np.asarray(part.center)+extent
            halves.append(part.half_extents)
            radii.append(part.radius)
            lengths.append(part.length)
            positions.append(tuple(position))
            # Bullet cylinders are Z-up; all TFS vehicle dimensions are Y-up.
            orientations.append((math.sqrt(.5),0.,0.,math.sqrt(.5))
                                if part.kind == 'cylinder' else (0.,0.,0.,1.))
            lows.append(low+offset);highs.append(high+offset)
        with bullet_mesh_paths(files) as compatible:
            shape = bullet.createCollisionShapeArray(shapeTypes=kinds, fileNames=compatible,
                halfExtents=halves, radii=radii, lengths=lengths,
                meshScales=[(1.,1.,1.)]*len(kinds), collisionFramePositions=positions,
                collisionFrameOrientations=orientations, physicsClientId=self.cid)
        self.dynamic_bounds[entity] = (tuple(np.min(lows,axis=0)),tuple(np.max(highs,axis=0)))
        return shape

    def _dynamic_mass_properties(self, entity, turret_yaw):
        """Return the explicit local mass model for an airborne hull.

        The tracked solver never uses this.  Experimental Bullet bodies use
        exactly the named collision meshes and XML component masses that are
        already selected for this vehicle, so a body is never given a generic
        cube's inertia merely because it has left the ground.
        """
        assets = self.assets_for(entity)
        cached = self._mass_models.get(id(assets))
        if cached is None:
            model = VehicleMassModel(assets.definition, assets.tank_shapes, assets.turret_node)
            self._mass_models[id(assets)] = (assets, model)
        else:
            _, model = cached
        return model.at_yaw(turret_yaw)

    @staticmethod
    def _pose_from_quaternion(position, orientation, reference=None, *, rotation=None):
        """Convert Bullet's quaternion to the project's Y-up angle order.

        One orientation has infinitely many Euler descriptions.  Picking the
        usual principal values on every Bullet tick makes an airborne hull
        appear to jump by π in yaw and roll as pitch crosses ±π/2.  ``reference``
        is the previous mirrored pose, so choose the equivalent description
        nearest to it.  The result still recreates Bullet's rotation matrix;
        this is continuity selection, not an approximation of the attitude.
        """
        if rotation is None:
            rotation = np.asarray(bullet.getMatrixFromQuaternion(orientation), dtype=float).reshape(3,3)
        sine = max(-1., min(1., -float(rotation[1,2])))
        cosine = math.hypot(float(rotation[0,2]), float(rotation[2,2]))

        def nearest_turn(value, previous):
            return value + math.tau*round((previous-value)/math.tau)

        # At a gimbal singularity the matrix determines yaw-roll (pitch +π/2)
        # or yaw+roll (pitch -π/2), rather than either angle independently.
        # Solve that constraint for the pair nearest to the old pair.  Merely
        # applying atan2 to zero entries loses a real rotation here.
        if cosine <= 1e-7:
            pitch = math.copysign(math.pi/2, sine)
            if sine >= 0.:
                combined = math.atan2(float(rotation[0,1]), float(rotation[0,0]))
                if reference is None:
                    yaw, roll = combined, 0.
                else:
                    combined = nearest_turn(combined, reference.yaw-reference.roll)
                    yaw = (combined+reference.yaw+reference.roll)*.5
                    roll = yaw-combined
            else:
                combined = math.atan2(-float(rotation[0,1]), float(rotation[0,0]))
                if reference is None:
                    yaw, roll = combined, 0.
                else:
                    combined = nearest_turn(combined, reference.yaw+reference.roll)
                    yaw = (combined+reference.yaw-reference.roll)*.5
                    roll = combined-yaw
            if reference is not None:
                pitch = nearest_turn(pitch, reference.pitch)
            return Pose(tuple(float(value) for value in position), yaw, pitch, roll)

        # ``orientation.matrix`` has pitch at -m[1,2], yaw at m[0,2]/m[2,2]
        # and roll at m[1,0]/m[1,1].  The second branch has the same matrix:
        # (yaw+π, π-pitch, roll+π).  Compare both after unwrapping by full
        # turns relative to the old pose.
        pitch = math.atan2(sine, cosine)
        yaw = math.atan2(float(rotation[0,2]), float(rotation[2,2]))
        roll = math.atan2(float(rotation[1,0]), float(rotation[1,1]))
        if reference is None:
            return Pose(tuple(float(value) for value in position), yaw, pitch, roll)
        candidates = []
        for candidate in ((yaw, pitch, roll),
                          (yaw+math.pi, math.pi-pitch, roll+math.pi)):
            unwrapped = tuple(nearest_turn(value, previous) for value,previous in zip(
                candidate, (reference.yaw, reference.pitch, reference.roll)))
            distance = sum((value-previous)**2 for value,previous in zip(
                unwrapped, (reference.yaw, reference.pitch, reference.roll)))
            candidates.append((distance, unwrapped))
        _, (yaw, pitch, roll) = min(candidates, key=lambda item: item[0])
        return Pose(tuple(float(value) for value in position), yaw, pitch, roll)

    def _dynamic_start_contact(self, body, obstacles):
        """Use the contact manifold only if a nearby collider overlaps."""
        low, high = bullet.getAABB(body, physicsClientId=self.cid)
        low = tuple(value-self.SKIN for value in low)
        high = tuple(value+self.SKIN for value in high)
        nearby = {other for other, _ in bullet.getOverlappingObjects(
            low, high, physicsClientId=self.cid) or () if other in obstacles}
        # A hull corrected away from a surface can still be within the query
        # skin. Only actual overlap needs another full-world manifold pass.
        if not any(bullet.getClosestPoints(body, other, 0., physicsClientId=self.cid)
                   for other in nearby):
            return None
        # Pair queries can report a different normal at a mesh edge. Preserve
        # the original depenetration direction whenever there is contact.
        bullet.performCollisionDetection(physicsClientId=self.cid)
        points = bullet.getContactPoints(bodyA=body, physicsClientId=self.cid)
        return min((point for point in points if point[2] in obstacles and point[8] < -self.SKIN),
                   key=lambda point: point[8], default=None)

    def _launch_environment(self, entity, bounds):
        """Fingerprint the actual bodies a stationary handoff could touch."""
        nearby = {body for body, _ in bullet.getOverlappingObjects(*bounds, physicsClientId=self.cid) or ()}
        nearby.discard(self.bodies[entity])
        static = frozenset((body, bullet.getBasePositionAndOrientation(body, physicsClientId=self.cid),
                            self.breakable_bodies.get(body))
                           for body in nearby if body in self.map_bodies or body in self.terrain_bodies)
        peers = nearby - {row[0] for row in static}
        placement = frozenset((body, bullet.getBasePositionAndOrientation(body, physicsClientId=self.cid),
                               self.gun_angles.get(self.body_entities.get(body)),
                               body in self.body_entities)
                              for body in peers)
        return (static, placement, self.gun_angles.get(entity), self.dynamic_mass_yaws.get(entity),
                self.dynamic_shapes.get(entity), tuple(self.movement_bounds(entity)),
                self.movement_circle, tuple(self._contact_support_cache.get(entity, ())))

    def _rises_clear_of_terrain(self, body, delta):
        """Prove that every point gains height relative to the traversed terrain triangles."""
        dx, dy, dz = delta
        if not (dx or dz):
            return dy > 0.
        low, high = bullet.getAABB(body, physicsClientId=self.cid)
        terrain = self.terrain
        cell_x, cell_z = terrain.WIDTH / terrain.size, terrain.DEPTH / terrain.size
        def cells(lo, hi, shift, origin, cell):
            first = math.floor((lo + min(0., shift) - origin) / cell)
            last = math.floor((hi + max(0., shift) - origin) / cell)
            return slice(max(0, min(terrain.size-2, first)), max(0, min(terrain.size-2, last)) + 2)
        xs = cells(low[0], high[0], dx, terrain.MIN, cell_x)
        zs = cells(low[2], high[2], dz, terrain.MIN_Z, cell_z)
        heights = np.asarray(terrain.heights).reshape(terrain.size, terrain.size)[zs, xs].astype(float)
        heights *= terrain.HEIGHT / 65535.
        # The two triangles of a cell share its (x+1,z)–(x,z+1) edge.
        # Their directional slopes bound the rise everywhere along this slice.
        bottom, left = heights[:-1, 1:], heights[1:, :-1]
        rise_a = (bottom - heights[:-1, :-1])*(dx/cell_x) + (left - heights[:-1, :-1])*(dz/cell_z)
        rise_b = (heights[1:, 1:] - left)*(dx/cell_x) + (heights[1:, 1:] - bottom)*(dz/cell_z)
        return dy - max(rise_a.max(), rise_b.max()) > 1e-9

    def _separate_blocked_launch(self, entity, pose, body, contact, obstacles):
        """Move one swept slice away from a static face that blocks rigid takeoff."""
        terrain = contact[2] in self.terrain_bodies
        # Start above the heightfield or on the occupied side of the wall.
        # The neighboring contact planes can constrain this initial direction.
        normal = np.asarray((0., 1., 0.) if terrain else contact[7], dtype=float)
        if not terrain and (contact[2] not in self.map_bodies or contact[2] in self.breakable_bodies
                or not np.isfinite(normal).all() or abs(normal[1]) > .50
                or np.dot(normal, np.subtract(pose.position, contact[6])) <= 0.):
            return None
        previous = bullet.getContactPoints(bodyA=body, physicsClientId=self.cid)
        nearby_map = {p[2] for p in previous if p[2] in self.map_bodies and p[8] < self.STEP}
        # Find a direction tangent to adjacent faces. A vertical lift by a
        # sloping wall, or a sideward retreat on a ramp, must separate both.
        normals = np.asarray([p[7] for other in nearby_map
            for p in bullet.getClosestPoints(body, other, 0., physicsClientId=self.cid)])
        if normals.size:
            for _ in range(12):
                speeds = normals @ normal
                i = int(speeds.argmin())
                if speeds[i] >= -1e-7:
                    break
                normal -= speeds[i] * normals[i]
        # The full-yaw rigid turret can be wider than the tracked sensor. A
        # large side overlap cannot be resolved in a single handoff. Retain a
        # small separating movement only if both collider representations allow it.
        step = min(self.STEP, self.DYNAMIC_MAX_START_CORRECTION)
        delta = normal * step
        if terrain and not self._rises_clear_of_terrain(body, delta):
            return None
        candidate = Pose(tuple(map(float, pose.position + delta)), pose.yaw, pose.pitch, pose.roll)
        if self.collision_zone and self.collision_zone(pose.position) != self.collision_zone(candidate.position):
            return None
        if not self._valid_retreat(candidate, pose, entity)[0]:
            self.set_pose(self.bodies[entity], pose)
            return None
        center, orientation = bullet.getBasePositionAndOrientation(body, physicsClientId=self.cid)
        bullet.resetBasePositionAndOrientation(body, tuple(center + delta), orientation, physicsClientId=self.cid)
        next_contact = self._dynamic_start_contact(body, obstacles)
        if next_contact is not None:
            contacts = bullet.getContactPoints(bodyA=body, physicsClientId=self.cid)
            # Do not trade this overlap for another one behind the hull. A
            # Bullet map body can contain several nearby, outward-facing walls.
            for point in contacts:
                if point[2] not in obstacles or point[8] >= -self.SKIN:
                    continue
                if terrain and point[2] in self.terrain_bodies:
                    continue
                if point[2] in nearby_map and np.dot(normal, point[7]) >= -1e-7:
                    continue
                if not any(old[2:5] == point[2:5] and old[8] <= point[8] + 1e-6
                           and np.dot(old[7], point[7]) >= .95
                           and abs(np.dot(old[7], np.subtract(old[6], point[6]))) <= self.SKIN
                           for old in previous):
                    break
            else:
                # The heightfield is solid below its upper surface. Bullet's
                # double-sided triangles can report a larger back-face depth
                # during a proven upward escape. Only a non-terrain blocker
                # needs a decreasing penetration in the rigid manifold.
                if terrain or next_contact[8] > contact[8] + 1e-5:
                    return candidate
            self.set_pose(self.bodies[entity], pose)
            return None
        return candidate

    @staticmethod
    def _center_velocity(velocity, angular, offset):
        vx, vy, vz = velocity
        wx, wy, wz = angular
        x, y, z = offset
        return vx + (wy*z - wz*y), vy + (wz*x - wx*z), vz + (wx*y - wy*x)

    def _activate_dynamic(self, entity, pose, velocity, angular_velocity=(0.,0.,0.),
                          *, initialize_bullet_velocity=True, allow_separation=False):
        """Transfer a released hull to Bullet, returning its corrected pose."""
        if not self.experimental_6dof or entity in self.dynamic_bodies:
            return None
        if entity not in self._fall_peaks or self.motion.get(entity,Motion()).grounded:
            self._begin_fall(entity,pose)
        angular_velocity = tuple(float(value) for value in angular_velocity)
        if (len(angular_velocity) != 3
                or not all(math.isfinite(value) for value in angular_velocity)):
            raise ValueError('Invalid dynamic angular velocity')
        turret_yaw = self.dynamic_mass_yaws.setdefault(
            entity, float(self.gun_angles.get(entity, (0.,0.))[0]))
        launch_angular = self.dynamic_launch_angular_velocities.setdefault(
            entity, angular_velocity)
        shape = self.dynamic_shapes.get(entity)
        if shape is None:
            shape = self.dynamic_shapes[entity] = self._dynamic_shape(entity)
        self.dynamic_fall_peak[entity] = 0.
        self.dynamic_fall_gap[entity] = 0
        properties = self._dynamic_mass_properties(entity,turret_yaw)
        launch_orientation = quaternion(pose.yaw, pose.pitch, pose.roll)
        body = bullet.createMultiBody(baseMass=properties.mass, baseCollisionShapeIndex=shape,
                    basePosition=pose.position,
                    baseOrientation=launch_orientation,
                    baseInertialFramePosition=properties.center_of_mass,
                    baseInertialFrameOrientation=(0., 0., 0., 1.),
                    physicsClientId=self.cid)
        self._sensor_filter(entity, 0, 0)
        inside = bool(self.collision_zone and self.collision_zone(pose.position))
        self._set_dynamic_zone(body, inside)
        contact_friction = self._ragdoll_friction(math.cos(pose.pitch)*math.cos(pose.roll))
        bullet.changeDynamics(body, -1, collisionMargin=.001, lateralFriction=contact_friction, restitution=.02,
                              rollingFriction=.01, spinningFriction=.01,
                              linearDamping=self.DYNAMIC_LINEAR_DAMPING, angularDamping=self.DYNAMIC_ANGULAR_DAMPING,
                              localInertiaDiagonal=properties.inertia_diagonal,
                              physicsClientId=self.cid)
        obstacles = (self.map_bodies - self.breakable_bodies.keys()) | self.terrain_bodies
        # Explicit pair queries do not check the collision masks. Include each
        # flying tank through its rigid hull and exclude its disabled sensor.
        obstacles.update(body_id for body_id, other in self.body_entities.items()
                         if other != entity and other not in self.dynamic_bodies
                         and (not self.collision_zone or self.collision_zone(self.poses[other].position) == inside))
        obstacles.update(dynamic.body for other, dynamic in self.dynamic_bodies.items()
                         if not self.collision_zone or self.collision_zone(self.poses[other].position) == inside)
        corrected_by = 0.
        contact = self._dynamic_start_contact(body, obstacles)
        start_center = None
        for _ in range(8):
            if contact is None:
                break
            if contact[2] in self.terrain_bodies and contact[7][1] <= 0.:
                break
            depth = -float(contact[8]) + self.SKIN
            normal = np.asarray(contact[7], dtype=float)
            length = _vector_length(normal)
            if (not math.isfinite(depth) or not math.isfinite(length) or length < 1e-8
                    or corrected_by + depth > self.DYNAMIC_MAX_START_CORRECTION):
                break
            normal /= length
            correction = normal * depth
            center, orientation = bullet.getBasePositionAndOrientation(body, physicsClientId=self.cid)
            if start_center is None:
                start_center = (center, orientation)
            center = np.asarray(center, dtype=float)
            center += correction
            bullet.resetBasePositionAndOrientation(body, tuple(center), orientation,
                                                    physicsClientId=self.cid)
            corrected_by += depth
            contact = self._dynamic_start_contact(body, obstacles)
        if contact is not None:
            if allow_separation and math.isfinite(contact[8]):
                if start_center is not None:
                    # The full correction failed. Try a single safe slice
                    # from the authoritative pose, not from a discarded trial.
                    bullet.resetBasePositionAndOrientation(body, *start_center, physicsClientId=self.cid)
                    contact = self._dynamic_start_contact(body, obstacles)
                separated = (self._separate_blocked_launch(entity, pose, body, contact, obstacles)
                             if contact is not None else None)
                if separated is not None:
                    self._separating_launches[entity] = separated
                    self.poses[entity] = separated
                    self._synced[entity] = (separated, self.motion[entity])
            stationary = (allow_separation and not self.collision_zone and not self.trace_dynamic_contacts
                          and not getattr(self, '_contact_query_observer', None)
                          and self.poses[entity] == pose and velocity[0] == velocity[2] == 0.)
            if stationary:
                # The temporary body may have moved within the correction
                # budget. Include every position a future attempt may query.
                low, high = bullet.getAABB(body, physicsClientId=self.cid)
                margin = 2*self.DYNAMIC_MAX_START_CORRECTION + self.STEP + self.SKIN
                bounds = (tuple(x-margin for x in low), tuple(x+margin for x in high))
            bullet.removeBody(body, physicsClientId=self.cid)
            self._sensor_filter(entity, _TRACKED_GROUP, _DYNAMIC_GROUP)
            if stationary:
                self._blocked_launches[entity] = (pose, velocity, angular_velocity, bounds,
                                                  self._launch_environment(entity, bounds))
            return None
        if initialize_bullet_velocity:
            launch_rotation = np.asarray(bullet.getMatrixFromQuaternion(launch_orientation)).reshape(3, 3)
            offset = launch_rotation @ properties.center_of_mass
            com_velocity = self._center_velocity(velocity, launch_angular, offset)
            bullet.resetBaseVelocity(body, linearVelocity=com_velocity,
                                     angularVelocity=launch_angular, physicsClientId=self.cid)
        center, orientation = bullet.getBasePositionAndOrientation(
            body, physicsClientId=self.cid)
        rotation = np.asarray(bullet.getMatrixFromQuaternion(orientation),
                              dtype=float).reshape(3,3)
        origin = np.asarray(center, dtype=float) - rotation @ np.asarray(
            properties.center_of_mass, dtype=float)
        active_pose = Pose(tuple(float(value) for value in origin),
                           pose.yaw, pose.pitch, pose.roll)
        self.dynamic_bodies[entity] = DynamicBody(
            body, properties.mass, properties.center_of_mass,
            properties.inertia_diagonal, contact_friction=contact_friction)
        self.dynamic_launch_angular_velocities.pop(entity,None)
        self.set_pose(self.bodies[entity], active_pose)
        return active_pose

    def _queue_dynamic(self, entity, pose, velocity, angular_velocity=None):
        """Defer a kinematic-to-rigid handoff until this arena tick has ended.

        CollisionWorld.move may spend part or all of the current fixed step
        advancing the swept fallback. Creating a Bullet body immediately would
        make dynamic_commit integrate it again for that same interval. Keep the
        final measured state here; dynamic_commit activates it after stepping
        bodies that were already live at the start of the tick.
        """
        if not self.experimental_6dof or entity in self.dynamic_bodies:
            return False
        if angular_velocity is None:
            previous = self.pending_dynamics.get(entity)
            angular_velocity = previous[2] if previous is not None else (0.,0.,0.)
        if (len(velocity) != 3 or len(angular_velocity) != 3
                or not all(math.isfinite(value) for value in (*velocity,*angular_velocity))):
            raise ValueError('Invalid pending dynamic state')
        self.pending_dynamics[entity] = (
            pose, tuple(float(value) for value in velocity),
            tuple(float(value) for value in angular_velocity))
        return True

    def _activate_pending_dynamics(self):
        pending, self.pending_dynamics = self.pending_dynamics, {}
        moved = False
        for entity, (pose, velocity, angular_velocity) in pending.items():
            if entity in self.dynamic_bodies:
                continue
            blocked = self._blocked_launches.get(entity)
            if blocked is not None:
                old_pose, old_velocity, old_spin, bounds, environment = blocked
                if (not self.collision_zone and not self.trace_dynamic_contacts
                        and not getattr(self, '_contact_query_observer', None)
                        and (pose, velocity, angular_velocity) == (old_pose, old_velocity, old_spin)
                        and self._launch_environment(entity, bounds) == environment):
                    continue
                del self._blocked_launches[entity]
            active = self._activate_dynamic(entity, pose, velocity, angular_velocity,
                                            allow_separation=True)
            if active is None:
                moved |= self.poses[entity] != pose
                continue
            state = self.motion[entity]
            state.velocity, state.grounded = velocity, False
            self.short_flight_spins.pop(entity,None)
            self.poses[entity] = active
            self._synced[entity] = (active,state)
            moved = True
        return moved

    @staticmethod
    def _ground_handoff_pose(pose):
        """Keep the physical attitude in the Euler branch used by ground springs.

        Free flight deliberately preserves complete turns and both equivalent
        Euler branches. The ground solver's road fits use principal pitch/roll;
        handing it a roll near 2*pi makes an ordinary spring unwind a full turn.
        This only relabels the same rotation, without moving or uprighting it.
        """
        if -math.pi/2 <= pose.pitch <= math.pi/2 and -math.pi <= pose.roll <= math.pi:
            return pose
        pitch = math.remainder(pose.pitch, math.tau)
        roll = math.remainder(pose.roll, math.tau)
        yaw = pose.yaw
        if pitch > math.pi/2:
            yaw, pitch, roll = yaw+math.pi, math.pi-pitch, roll+math.pi
        elif pitch < -math.pi/2:
            yaw, pitch, roll = yaw+math.pi, -math.pi-pitch, roll+math.pi
        return Pose(pose.position, yaw, pitch, math.remainder(roll, math.tau))

    def _commit_dynamic_handoff(self, entity, handoff, pose, previous,
                                velocity, yaw_rate, dt):
        ground_pose = self._ground_handoff_pose(pose)
        rebased = ground_pose is not pose
        rates_pose, rates_previous = pose, previous
        if self.sprung_tracks and (rebased
                or self._ground_handoff_pose(previous) is not previous
                or abs(pose.pitch-previous.pitch) > math.pi
                or abs(pose.roll-previous.roll) > math.pi):
            # Align both ends before taking differences: changing Euler branch
            # is not angular velocity. Preserve the existing finite-difference
            # convention without introducing a 2*pi/dt spring impulse.
            rates_pose = ground_pose
            rates_previous = self._pose_from_quaternion(previous.position,
                quaternion(previous.yaw, previous.pitch, previous.roll), ground_pose)
        self._deactivate_dynamic(entity, handoff, velocity, yaw_rate)
        if self.sprung_tracks:
            self.motion[entity].suspension_velocity = (
                float(velocity[1]), float((rates_pose.pitch-rates_previous.pitch)/dt),
                float((rates_pose.roll-rates_previous.roll)/dt))
            self.motion[entity].support_angular_rates = self.motion[entity].suspension_velocity[1:]
        if rebased:
            self._spring_rates.pop(entity, None)
            self.dynamic_orientation_resets.append((entity, handoff))

    def _deactivate_dynamic(self, entity, pose, velocity=None, yaw_rate=0.):
        dynamic = self.dynamic_bodies.pop(entity)
        self.dynamic_fall_peak.pop(entity,None)
        self.dynamic_fall_gap.pop(entity,None)
        if velocity is None:
            center, orientation = bullet.getBasePositionAndOrientation(
                dynamic.body, physicsClientId=self.cid)
            linear, angular = bullet.getBaseVelocity(dynamic.body,
                                                       physicsClientId=self.cid)
            rotation = np.asarray(bullet.getMatrixFromQuaternion(orientation),
                                  dtype=float).reshape(3, 3)
            offset = rotation @ np.asarray(dynamic.center_of_mass, dtype=float)
            velocity = np.asarray(linear, dtype=float) - np.cross(
                np.asarray(angular, dtype=float), offset)
        bullet.removeBody(dynamic.body, physicsClientId=self.cid)
        self.dynamic_launch_angular_velocities.pop(entity,None)
        self.short_flight_spins.pop(entity,None)
        self.dynamic_mass_yaws.pop(entity,None)
        self._sensor_filter(entity, _TRACKED_GROUP, _DYNAMIC_GROUP)
        self.poses[entity] = pose
        # Keep the small residual velocity that qualified this quiet landing.
        # Removing the Bullet body must not turn touchdown into a one-frame
        # brake. Ground motion stores a scalar along the hull, so retain the
        # in-plane component and remove its normal component rather than
        # carrying a bounce into the tracked solver.
        road = self._road_poses.get(entity,pose)
        up = matrix(road.yaw,road.pitch,road.roll)[:,1]
        residual = np.asarray(velocity, dtype=float)
        tangent = residual-up*float(np.dot(residual,up))
        self.motion[entity] = Motion(tuple(float(value) for value in tangent), True)
        self._seed_support_memory(entity,pose,tangent[1])
        if self.experimental_ground_impacts:
            planar = (float(tangent[0]),float(tangent[2]))
            active = (math.hypot(*planar) > self.GROUND_IMPACT_MIN_CLOSING_SPEED
                      or abs(yaw_rate) > .01)
            self.ground_motion[entity] = GroundMotion(
                planar if active else (0.,0.), float(yaw_rate) if active else 0., active)
        self.set_pose(self.bodies[entity], pose)
        self._synced[entity] = (pose, self.motion[entity])

    def _dynamic_can_handoff(self, entity, dynamic, pose, velocity, angular_velocity, up, contacts):
        """Return a quiet, terrain-supported pose that matches its support fit."""
        spin = math.sqrt(sum(value*value for value in angular_velocity))
        if spin > self.DYNAMIC_SETTLE_ANGULAR_SPEED:
            dynamic.settled_steps = 0
            return None
        if not any((point[2] in self.map_bodies or point[2] in self.terrain_bodies)
                   and point[7][1] > .35
                   and point[8] <= self.SKIN and point[9] > 0.
                   for point in contacts):
            dynamic.settled_steps = 0
            return None
        # A terrain support fit has an upward normal. For an overturned hull,
        # its horizontal up-vector component bounds the best possible alignment
        # with any such normal, even on a vertical slope.
        if up[1] < 0. and math.hypot(up[0], up[2]) + 1e-12 < self.DYNAMIC_GROUND_ALIGNMENT:
            dynamic.settled_steps = 0
            return None
        ground_pose = self._ground_handoff_pose(pose)
        candidate = self._landing_support(pose.position[0],pose.position[2],ground_pose.yaw,
                                         pose.position[1],entity)
        candidate_up = matrix(candidate.yaw,candidate.pitch,candidate.roll)[:,1]
        normal_speed = abs(float(np.dot(velocity,candidate_up)))
        if normal_speed > self.DYNAMIC_SETTLE_SPEED:
            # A hull still falling or bouncing into its support must remain
            # dynamic even when its total speed is mostly along the ground.
            dynamic.settled_steps = 0
            return None
        if (math.dist(candidate.position, pose.position) > self.DYNAMIC_HANDOFF_DISTANCE
                or float(np.dot(up, candidate_up)) < self.DYNAMIC_GROUND_ALIGNMENT):
            # A low-speed body can be wedged against a wall or resting on its
            # side. Only return it to the tracked solver when both its origin
            # and its up direction agree with the terrain-supported fit.
            dynamic.settled_steps = 0
            return None
        if self._has_suspension(entity) and not self._can_carry_origin(entity,ground_pose):
            dynamic.settled_steps = 0
            return None
        dynamic.settled_steps += 1
        if dynamic.settled_steps < self.DYNAMIC_SETTLE_STEPS:
            return None
        self._sensor_filter(entity, _TRACKED_GROUP, _DYNAMIC_GROUP)
        handoff = ground_pose if self.sprung_tracks else candidate
        valid, _ = self._valid(handoff, pose, entity=entity, climb=False,
                               suspension=self._has_suspension(entity))
        self._sensor_filter(entity, 0, 0)
        if not valid:
            dynamic.settled_steps = 0
            return None
        self._road_poses[entity] = candidate
        # Keep the actual settled attitude/height and let the sprung hull
        # converge over the next drive ticks. The contact normal and endpoint
        # tangent velocity already belong to the road fit.
        return handoff

    def _recover_dynamic(self, entity, dynamic, pose, rotation):
        """Find a clear supported track pose after a rigid hull stays stuck."""
        forward = rotation[:,2]
        yaw = (math.atan2(float(forward[0]),float(forward[2]))
               if math.hypot(float(forward[0]),float(forward[2])) > .1
               else pose.yaw)
        x, _, z = pose.position
        offsets = [(0.,0.)]
        for radius in (.75,1.5,2.5,3.5):
            offsets.extend((radius*math.sin(i*math.tau/12),
                            radius*math.cos(i*math.tau/12)) for i in range(12))
        suspension = self._has_suspension(entity)
        reuse_contacts = (suspension
            and getattr(self._landing_support, '__func__', None) is _LANDING_SUPPORT_IMPL
            and getattr(self._contact_supports, '__func__', None) is _CONTACT_SUPPORTS_IMPL
            and getattr(self._valid, '__func__', None) is _VALID_IMPL
            and getattr(self._contacts, '__func__', None) is _CONTACTS_IMPL
            and getattr(self, '_contact_query_observer', None) is None
            and getattr(self, 'collision_zone', None) is None
            and not self.trace_dynamic_contacts)
        self._sensor_filter(entity,_TRACKED_GROUP,_DYNAMIC_GROUP)
        prepare_contacts = (reuse_contacts
            and getattr(self._compressed_track_contact, '__func__', None) is _COMPRESSED_CONTACT_IMPL
            and getattr(self._track_incline, '__func__', None) is _TRACK_INCLINE_IMPL)
        if prepare_contacts:
            rotations = {}
            self._compressed_track_contact = partial(self._recovery_track_contact, rotations)
        batch_contacts = prepare_contacts and self._can_batch_incline_rays()
        if batch_contacts:
            self._recovery_batch_active = True
        cache_raw = (reuse_contacts and _contact_queries is not None
                     and bullet.getClosestPoints is _RECOVERY_CLOSEST)
        if cache_raw:
            self._recovery_closest = _RecoveryClosestQueries(
                self.map_bodies, CollisionWorld._blocking_contacts is _RECOVERY_BLOCKING_CONTACTS)
        try:
            for dx,dz in offsets:
                contacts = []
                landing_options = {'contact_result': contacts} if reuse_contacts else {}
                candidate = self._landing_support(x+dx,z+dz,yaw,pose.position[1],entity,
                                                  **landing_options)
                validation = {'measured_contacts': contacts[0]} if contacts else {}
                if (abs(candidate.position[1]-pose.position[1]) > 4.
                        or not self._valid(candidate,pose,entity=entity,climb=False,
                                           suspension=suspension, **validation)[0]
                        or (suspension and not self._can_carry_origin(entity,candidate))):
                    continue
                self._road_poses[entity] = candidate
                self._deactivate_dynamic(entity,candidate,(0.,0.,0.))
                self.dynamic_recoveries.append((entity,pose.position,candidate.position))
                return True
        finally:
            if cache_raw:
                del self._recovery_closest
            if batch_contacts:
                del self._recovery_batch_active
            if prepare_contacts:
                del self._compressed_track_contact
            if entity in self.dynamic_bodies:
                self._sensor_filter(entity,0,0)
        return False

    def _ragdoll_friction(self, alignment):
        """Continuous roof/side → track-supported grip, without an added force."""
        t = max(0., min(1., (alignment-.15)/.55))
        t = t*t*(3.-2.*t)
        return self.DYNAMIC_HULL_FRICTION + t*(self.DYNAMIC_TRACK_FRICTION-self.DYNAMIC_HULL_FRICTION)

    def _update_ragdoll_friction(self, dynamic, basis, blend, contacts=None):
        ux, uy, uz = basis[1], basis[4], basis[7]
        if contacts is None:
            contacts = bullet.getContactPoints(bodyA=dynamic.body, physicsClientId=self.cid)
        total_weight = weighted_alignment = 0.
        for point in contacts:
            nx, ny, nz = point[7]
            if ny > .15 and point[8] <= self.SKIN and (
                    point[2] in self.map_bodies or point[2] in self.terrain_bodies):
                weight = max(1., point[9])
                total_weight += weight
                weighted_alignment += weight * (ux*nx + uy*ny + uz*nz)
        # Before first touchdown, use gravity-up as the contact prediction.
        # Once supported, the actual slope normal owns the alignment: a hull
        # following a hill on its tracks keeps ordinary track contact grip.
        alignment = weighted_alignment / total_weight if total_weight else uy
        target = self._ragdoll_friction(alignment)
        value = dynamic.contact_friction + (target-dynamic.contact_friction)*blend
        if abs(value-dynamic.contact_friction) > 1e-5:
            dynamic.contact_friction = value
            # Sliding grip is independent of the existing rotational contact
            # resistance; reducing both would prolong a roof/side wobble.
            bullet.changeDynamics(dynamic.body, -1, lateralFriction=value,
                physicsClientId=self.cid)

    @property
    def supports_flight(self):
        return self.experimental_6dof

    def flight_spin(self, entity):
        dynamic = self.dynamic_bodies.get(entity)
        if dynamic is None:
            return (0., 0., 0.)
        return bullet.getBaseVelocity(dynamic.body, physicsClientId=self.cid)[1]

    def set_flight_velocity(self, entity, velocity, angular, *, sandbox_landing: bool | None = None):
        pose, state = self.poses[entity], self.motion[entity]
        if entity not in self.dynamic_bodies:
            # Bullet receives the requested velocity below before it can step.
            active = self._activate_dynamic(entity, pose, state.velocity, initialize_bullet_velocity=False)
            if active is None:
                return False
            pose = self.poses[entity] = active
            self.pending_dynamics.pop(entity, None)
            self.short_flight_spins.pop(entity, None)
        dynamic = self.dynamic_bodies[entity]
        if sandbox_landing is not None:
            dynamic.sandbox_landing = sandbox_landing
        offset = matrix(pose.yaw, pose.pitch, pose.roll) @ dynamic.center_of_mass
        com_velocity = self._center_velocity(velocity, angular, offset)
        bullet.resetBaseVelocity(dynamic.body, com_velocity, angular, physicsClientId=self.cid)
        state.velocity, state.grounded = velocity, False
        self._wall_glances.pop(entity,None)
        self._synced[entity] = (pose, state)
        return True

    def add_flight_velocity(self, entity, delta, *, angular_delta=None, linear_damping=None):
        if not self.supports_flight:
            return False
        state = self.motion[entity]
        velocity = tuple(current+extra for current, extra in zip(state.velocity, delta))
        angular = self.flight_spin(entity)
        if angular_delta is not None:
            angular = tuple(current+extra for current, extra in zip(angular, angular_delta))
        if not self.set_flight_velocity(entity, velocity, angular):
            return False
        if linear_damping is not None:
            dynamic = self.dynamic_bodies[entity]
            bullet.changeDynamics(dynamic.body, -1, linearDamping=linear_damping, physicsClientId=self.cid)
            dynamic.restore_air_damping = True
        return True

    def set_flight_spin(self, entity, angular):
        """Change the spin without changing the rigid body's linear momentum."""
        pose = self.poses[entity]
        offset = matrix(pose.yaw, pose.pitch, pose.roll) @ self.dynamic_bodies[entity].center_of_mass
        spin_delta = tuple(old - new for old, new in zip(self.flight_spin(entity), angular))
        origin_velocity = self._center_velocity(self.motion[entity].velocity, spin_delta, offset)
        return self.set_flight_velocity(entity, origin_velocity, angular)

    def add_airborne_velocity(self, entity, delta, *, angular_delta=None):
        state = self.motion[entity]
        if state.grounded:
            return False
        if entity in self.dynamic_bodies:
            return self.add_flight_velocity(entity, delta, angular_delta=angular_delta)
        # Keep swept flight and its stored spin until the usual dynamic handoff.
        state.velocity = tuple(current + extra for current, extra in zip(state.velocity, delta))
        pending = self.pending_dynamics.get(entity)
        angular = pending[2] if pending is not None else self.short_flight_spins.get(entity, (0., 0., 0.))
        if angular_delta is not None:
            angular = tuple(before + added for before, added in zip(angular, angular_delta))
            self.short_flight_spins[entity] = angular
        if pending is not None:
            self.pending_dynamics[entity] = pending[0], state.velocity, angular
        return True

    def _terrain_probe_points(self, entity):
        assets = self.assets_for(entity)
        cached = self._terrain_probes.get(id(assets))
        if cached is not None and cached[0] is assets:
            return cached[1]
        proxies = getattr(assets, 'contact_proxies', None)
        if proxies:
            components = []
            for part in proxies:
                if part.name == assets.turret_node:
                    continue
                if part.kind == 'box':
                    cx, cy, cz = part.center
                    hx, hy, hz = part.half_extents
                    components.append([(x, y, z) for x in (cx-hx, cx+hx)
                                       for y in (cy-hy, cy+hy) for z in (cz-hz, cz+hz)])
                else:
                    components.append(part.vertices)
        else:
            shapes = getattr(assets, '_convex_shapes', None) or assets.tank_shapes
            components = [verts for name, verts in shapes if name != assets.turret_node]
        points = np.concatenate(components)
        points.setflags(write=False)
        self._terrain_probes[id(assets)] = assets, points
        return points

    def _terrain_penetration(self, pose, rotation, points):
        world_points = points @ rotation.T + pose.position
        heights = self._terrain_heights(world_points[:, 0], world_points[:, 2])
        return float(np.max(heights - world_points[:, 1]))

    def _constrain_dynamic_pose(self, entity, pose, velocity, rotation):
        """Keep dynamic hulls inside the red line and above terrain overlaps."""
        bounds = self.dynamic_bounds[entity]
        clipped = self._border_pose(pose, entity, flight_bounds=bounds, rotation=rotation)
        x, y, z = clipped.position
        vx, vy, vz = velocity
        if (x - pose.position[0]) * vx < 0.:
            vx = 0.
        if (z - pose.position[2]) * vz < 0.:
            vz = 0.
        clipped, (vx, vy, vz) = self._clip_circle(entity, clipped, (vx, vy, vz))
        x, y, z = clipped.position
        low, high = bounds
        up = rotation[1]
        down = -float(np.minimum(up * low, up * high).sum())
        # The box is only a broad-phase bound. Its corners can stick out from
        # a sloped hull or track nose and must not trigger depenetration.
        if y - down < self.terrain.height(x, z) + 2.:
            dynamic = self.dynamic_bodies[entity]
            penetration = self._terrain_penetration(clipped, rotation, self._terrain_probe_points(entity))
            overlap = (self.LOW_GRAVITY_TERRAIN_OVERLAP if self.GRAVITY < CollisionWorld.GRAVITY
                       else self.DYNAMIC_DEEP_TERRAIN_OVERLAP)
            if dynamic.sandbox_landing:
                # Keep the tracks within the compression allowed by the ground handoff.
                overlap = self.SUSPENSION_TRAVEL
            if penetration > overlap:
                # Leave a small contact overlap so the solver can still settle
                # the landing through its contact manifold.
                residual = min(overlap, self.SKIN)
                clipped = Pose((x, y + penetration - residual, z), pose.yaw, pose.pitch, pose.roll)
                vy = max(0., vy)
        return clipped, (vx, vy, vz)

    def add_ground_velocity(self, entity, x, z):
        if not self.experimental_ground_impacts or not self.motion[entity].grounded:
            return False
        state = self.ground_motion[entity]
        vx, _, vz = self.motion[entity].velocity
        if state.active and state.velocity is not None:
            vx, vz = state.velocity
        self.ground_motion[entity] = GroundMotion((vx+x, vz+z), state.yaw_rate, True)
        return True

    def deferred_impulse(self, entity, delta, spin, *, dt, max_speed, damping=None):
        """Test a queued impulse against the live hull and its next swept pose."""
        if entity not in self.bodies or entity in self.pending_dynamics:
            return False
        pose = self.poses[entity]
        state = self.motion[entity]
        slides = spin is None and state.grounded and self.experimental_ground_impacts
        current = state.velocity
        ground = self.ground_motion[entity]
        if slides and ground.active and ground.velocity is not None:
            current = ground.velocity[0], 0., ground.velocity[1]
        velocity = tuple(a+b for a, b in zip(current, delta))
        if not all(map(math.isfinite, velocity)):
            return False
        length = math.hypot(*velocity)
        if length > max_speed:
            velocity = tuple(v*max_speed/length for v in velocity)
        ahead = Pose(tuple(p+v*dt for p, v in zip(pose.position, velocity)),
                     pose.yaw, pose.pitch, pose.roll)
        try:
            if (not self._inside(pose, entity) or not self._inside(ahead, entity)
                    or self._contacts(pose, entity, suspension=state.grounded)
                    or self._contacts(ahead, entity, suspension=slides)):
                return False
        finally:
            self.set_pose(self.bodies[entity], pose)
        if slides:
            return self.add_ground_velocity(entity, velocity[0]-current[0], velocity[2]-current[2])
        angular = self.flight_spin(entity)
        omega = tuple(a+b for a, b in zip(angular, spin)) if spin is not None else angular
        angular_speed = math.hypot(*omega)
        limit = self.DYNAMIC_MAX_LAUNCH_ANGULAR_SPEED
        if angular_speed > limit:
            omega = tuple(v*limit/angular_speed for v in omega)
        return self.add_flight_velocity(entity, tuple(v-c for v, c in zip(velocity, current)),
                                        angular_delta=tuple(v-c for v, c in zip(omega, angular)),
                                        linear_damping=damping)

    def _break_props_at_rigid_body(self, body, direction):
        if not self.breakable_bodies:
            return
        low, high = bullet.getAABB(body, physicsClientId=self.cid)
        nearby = bullet.getOverlappingObjects(
            tuple(v-self.SKIN for v in low), tuple(v+self.SKIN for v in high),
            physicsClientId=self.cid) or ()
        keys = set()
        for other, _ in nearby:
            if other in self.breakable_bodies and bullet.getClosestPoints(
                    body, other, self.SKIN, physicsClientId=self.cid):
                keys.add(self.breakable_bodies[other])
        for key in sorted(keys):
            self.destroy(key, direction)

    def dynamic_commit(self, dt, *, piloted=()):
        """Advance live bodies and then start newly released hulls.

        Arena calls this exactly once after it has queued every vehicle's
        controls. Bodies that were already dynamic receive the full fixed
        interval. Newly released hulls are activated only after that step, so
        a swept kinematic portion of this tick cannot be integrated twice.
        """
        if not (math.isfinite(dt) and dt > 0):
            raise ValueError('Invalid dynamic motion interval')
        moved = False
        previous_contacts = {}
        if self.dynamic_bodies:
            steps = max(1, math.ceil(dt / self.DYNAMIC_MAX_STEP))
            sub_dt = dt / steps
            blend = -math.expm1(-self.DYNAMIC_GRIP_BLEND_RATE * sub_dt)
            bullet.setTimeStep(sub_dt, physicsClientId=self.cid)
            for _ in range(steps):
                before = {}
                for entity,dynamic in self.dynamic_bodies.items():
                    center,orientation = bullet.getBasePositionAndOrientation(
                        dynamic.body,physicsClientId=self.cid)
                    linear,angular = bullet.getBaseVelocity(dynamic.body,
                                                            physicsClientId=self.cid)
                    basis = bullet.getMatrixFromQuaternion(orientation)
                    self._update_ragdoll_friction(dynamic, basis, blend,
                                                  contacts=previous_contacts.get(entity))
                    before[entity] = (center,basis,linear,angular)
                if self.collision_zone:
                    self.refresh_collision_zones()
                bullet.stepSimulation(physicsClientId=self.cid)
                # Breakable meshes remain available for explicit contact and
                # ray queries, but cannot exert a rigid-body solver impulse.
                current_contacts = {}
                for entity,dynamic in tuple(self.dynamic_bodies.items()):
                    linear = before[entity][2]
                    self._break_props_at_rigid_body(dynamic.body, (linear[0], linear[2]))
                    contacts = bullet.getContactPoints(bodyA=dynamic.body,
                                                       physicsClientId=self.cid)
                    current_contacts[entity] = contacts
                    if contacts:
                        if dynamic.restore_air_damping and any(point[8] <= 0. for point in contacts):
                            bullet.changeDynamics(dynamic.body, -1, linearDamping=self.DYNAMIC_LINEAR_DAMPING,
                                                  physicsClientId=self.cid)
                            dynamic.restore_air_damping = False
                    self._record_fall_contacts(entity,dynamic,before[entity],contacts)
                self._record_air_rams(before, current_contacts, bullet_bodies=True)
                previous_contacts = current_contacts
        for entity, dynamic in list(self.dynamic_bodies.items()):
            contacts = previous_contacts.get(entity)
            if contacts is None:
                contacts = bullet.getContactPoints(bodyA=dynamic.body, physicsClientId=self.cid)
            if self.trace_dynamic_contacts:
                self.dynamic_contact_trace[entity] = tuple(
                    dict(body_id=int(point[2]),
                         kind=self._dynamic_contact_kind(point[2]),
                         point_on_vehicle=tuple(float(value) for value in point[5]),
                         point_on_other=tuple(float(value) for value in point[6]),
                         normal=tuple(float(value) for value in point[7]),
                         distance=float(point[8]),
                         normal_force=float(point[9]))
                    for point in contacts[:8])
            center, orientation = bullet.getBasePositionAndOrientation(dynamic.body,
                                                                          physicsClientId=self.cid)
            velocity, angular = bullet.getBaseVelocity(dynamic.body, physicsClientId=self.cid)
            rotation = np.asarray(bullet.getMatrixFromQuaternion(orientation),
                                  dtype=float).reshape(3, 3)
            ox, oy, oz = rotation @ dynamic.center_of_mass
            # Bullet reports the inertial (centre-of-mass) frame.  The rest
            # of the server, collision sensor and Mercury pose retain the
            # hull origin, so mirror back both the origin and its velocity.
            # v_origin = v_com - omega x r_com.
            origin = (center[0]-ox, center[1]-oy, center[2]-oz)
            self._fall_peaks[entity] = max(self._fall_peaks.get(entity, origin[1]), origin[1])
            wx, wy, wz = angular
            origin_velocity = (velocity[0] - (wy*oz-wz*oy),
                               velocity[1] - (wz*ox-wx*oz),
                               velocity[2] - (wx*oy-wy*ox))
            pose = self._pose_from_quaternion(origin, orientation, self.poses[entity],
                                              rotation=rotation)
            if (not all(math.isfinite(value) for value in (*pose.position, pose.yaw, pose.pitch, pose.roll))
                    or not all(math.isfinite(value) for value in (*origin_velocity, *angular))):
                raise ValueError('Non-finite experimental dynamic body state')
            clipped, clipped_velocity = self._constrain_dynamic_pose(entity, pose, origin_velocity, rotation)
            if clipped.position != pose.position:
                center = tuple(current + new - old for current, new, old
                               in zip(center, clipped.position, pose.position))
                velocity = tuple(current + new - old for current, new, old
                                 in zip(velocity, clipped_velocity, origin_velocity))
                bullet.resetBasePositionAndOrientation(dynamic.body, center, orientation, physicsClientId=self.cid)
                bullet.resetBaseVelocity(dynamic.body, velocity, angular, physicsClientId=self.cid)
                pose, origin_velocity = clipped, clipped_velocity
            previous = self.poses[entity]
            self.poses[entity] = pose
            self.motion[entity] = Motion(tuple(map(float, origin_velocity)), False)
            # The sensor mirrors the rigid body's exact position and rotation.
            bullet.resetBasePositionAndOrientation(self.bodies[entity], pose.position,
                                                   orientation, physicsClientId=self.cid)
            self._synced[entity] = (pose, self.motion[entity])
            moved |= math.dist(previous.position, pose.position) > 1e-7
            if entity in piloted:
                dynamic.settled_steps = 0
                dynamic.stationary_time = 0.
                continue
            handoff = self._dynamic_can_handoff(entity, dynamic, pose, velocity, angular,
                                                 rotation[:,1], contacts)
            if handoff is not None:
                # Heading is atan2(forward.x, forward.z). Its derivative
                # differs from world omega.y on a pitched/rolled landing.
                forward = rotation[:,2]
                forward_rate = np.cross(np.asarray(angular),forward)
                yaw_rate = float((forward[2]*forward_rate[0]
                                  -forward[0]*forward_rate[2])
                                 / max(1e-8,forward[0]**2+forward[2]**2))
                self._commit_dynamic_handoff(entity, handoff, pose, previous,
                                              origin_velocity, yaw_rate, dt)
                continue
            if dynamic.stationary_anchor is None or math.dist(
                    dynamic.stationary_anchor,pose.position) > self.DYNAMIC_STUCK_RADIUS:
                dynamic.stationary_anchor = pose.position
                dynamic.stationary_time = 0.
            else:
                dynamic.stationary_time += dt
            if dynamic.stationary_time >= self.DYNAMIC_RECOVER_AFTER:
                if not self._recover_dynamic(entity,dynamic,pose,rotation):
                    dynamic.stationary_time = 0.
        return self._activate_pending_dynamics() or moved

    def _record_fall_contacts(self, entity, dynamic, before, contacts):
        """Use pre-impulse point velocity; one HP peak per contact episode."""
        center,basis,linear,angular = before
        cx,cy,cz = dynamic.center_of_mass
        origin_y = center[1] - (basis[3]*cx + basis[4]*cy + basis[5]*cz)
        peak_height = max(self._fall_peaks.get(entity,origin_y),origin_y)
        self._fall_peaks[entity] = peak_height
        contacts = [point for point in contacts
                    if point[8] <= self.SKIN and
                    (point[2] in self.map_bodies or point[2] in self.terrain_bodies)]
        if not contacts:
            gap = self.dynamic_fall_gap.get(entity,0)+1
            self.dynamic_fall_gap[entity] = gap
            if gap >= self.DYNAMIC_FALL_GAP_STEPS:
                self.dynamic_fall_peak[entity] = 0.
            return
        self.dynamic_fall_gap[entity] = 0
        low,high = self.dynamic_bounds[entity]
        best = None
        mass_ratios = None
        rotation = np.asarray(basis).reshape(3,3)
        origin = center-rotation @ np.asarray(dynamic.center_of_mass,dtype=float)
        wx,wy,wz = angular
        for contact in contacts:
            point = contact[5]
            length = math.hypot(*contact[7])
            if not .99 <= length <= 1.01 or not all(map(math.isfinite,point)):
                continue
            nx,ny,nz = normal = tuple(value/length for value in contact[7])
            rx,ry,rz = (value-com for value,com in zip(point,center))
            center_closing = -(linear[0]*nx + linear[1]*ny + linear[2]*nz)
            speed = (center_closing - (wy*rz-wz*ry)*nx
                     - (wz*rx-wx*rz)*ny - (wx*ry-wy*rx)*nz)
            if speed <= 0. or not math.isfinite(speed):
                continue
            local_point = rotation.T @ (point-origin)
            local_normal = rotation.T @ normal
            self._record_track_impact(entity,local_point,local_normal,speed,
                                      peak_height-origin_y)
            if speed <= MIN_HULL_DAMAGE_SPEED:
                continue
            # Bullet contact points may lie a little outside the fitted mesh
            # due to penetration. Clamp only to the measured proxy bounds.
            witness_local = local_point
            local_point = np.clip(local_point,low,high)
            kind,fraction = evaluate_fall_impact(speed,tuple(local_point),
                tuple(local_normal),(low,high))
            damage_speed = speed
            if fraction > 0.:
                # A rotating corner does not carry the whole vehicle mass.
                # Use the backend's actual COM/inertia and the UNCLIPPED
                # witness lever. This changes gameplay HP only; belt damage
                # above and all physical velocities retain the raw closing.
                if mass_ratios is None:
                    mass_ratios = tuple(dynamic.mass/value for value in dynamic.inertia_diagonal)
                rx,ry,rz = (float(witness_local[i])-dynamic.center_of_mass[i] for i in range(3))
                nx,ny,nz = (float(value) for value in local_normal)
                kx,ky,kz = ry*nz-rz*ny,rz*nx-rx*nz,rx*ny-ry*nx
                effective_mass_fraction = 1./(1.+kx*kx*mass_ratios[0]
                    +ky*ky*mass_ratios[1]+kz*kz*mass_ratios[2])
                damage_speed = impact_damage_speed(speed,center_closing,effective_mass_fraction)
                kind,fraction = evaluate_fall_impact(damage_speed,tuple(local_point),
                    tuple(local_normal),(low,high))
            if best is None or fraction > best['fraction']:
                best = dict(entity=entity,kind=kind,fraction=fraction,
                            speed=speed,damage_speed=damage_speed,
                            point=tuple(float(value) for value in point),
                            normal=tuple(float(value) for value in normal))
        if best is None:
            return
        peak = self.dynamic_fall_peak.get(entity,0.)
        if best['fraction'] > peak + 1e-9:
            best['fraction'] -= peak
            self.dynamic_fall_peak[entity] = peak+best['fraction']
            self.fall_impacts.append(best)

    def _ram_body_snapshot(self, entity, pose, velocity, angular):
        props = self.ground_mass_properties[entity]
        basis = matrix(pose.yaw, pose.pitch, pose.roll)
        offset = basis @ props.center_of_mass
        return (np.asarray(pose.position) + offset, basis,
                np.asarray(velocity) + np.cross(angular, offset), angular)

    def _record_air_rams(self, before, contacts, *, bullet_bodies=False, swept=False):
        """Measure damage from hull contact before the flight solver dissipates it."""
        if not self.experimental_ground_impacts:
            return
        dynamic_ids = ({body.body: entity for entity, body in self.dynamic_bodies.items()}
                       if bullet_bodies else {})
        grounded = {}

        def contact_motion(entity, point, normal):
            if entity in before:
                center, basis, linear, angular = before[entity]
                props = self.dynamic_bodies.get(entity) or self.ground_mass_properties[entity]
            else:
                props = self.ground_mass_properties[entity]
                if entity not in grounded:
                    omega = self.ground_commands.get(entity, (None, 0.))[1]
                    grounded[entity] = self._ram_body_snapshot(entity, self.poses[entity],
                                                                 self.motion[entity].velocity,
                                                                 (0., omega, 0.))
                center, basis, linear, angular = grounded[entity]
            arm = np.asarray(point) - center
            angular_arm = np.cross(arm, normal) @ np.asarray(basis).reshape(3, 3)
            inverse_mass = 1. / props.mass + float(np.sum(angular_arm**2 / props.inertia_diagonal))
            normal_speed = float(np.dot(linear, normal) + np.dot(angular, np.cross(arm, normal)))
            return normal_speed, inverse_mass

        for entity, rows in contacts.items():
            for contact in rows:
                body = contact[2]
                other = dynamic_ids.get(body, self.body_entities.get(body))
                if (other is None or other == entity or contact[8] > self.SKIN
                        or (not swept and contact[9] <= 0.)):
                    continue
                pair = (min(entity, other), max(entity, other))
                self._air_tank_contacts.add(pair)
                if pair in self.ground_contact_pairs:
                    continue
                normal = np.asarray(contact[7])
                speed_a, inverse_a = contact_motion(entity, contact[5], normal)
                speed_b, inverse_b = contact_motion(other, contact[6], normal)
                closing = speed_a - speed_b
                if not math.isfinite(closing) or closing > -self.RAM_MIN_CLOSING_SPEED:
                    continue
                energy = .5 * closing**2 / (inverse_a + inverse_b)
                previous = self.air_ram_impacts.get(pair)
                if previous is not None and previous['ram_energy'] >= energy:
                    continue
                forward = entity == pair[0]
                share = (inverse_a if forward else inverse_b) / (inverse_a + inverse_b)
                self.air_ram_impacts[pair] = dict(
                    entity_a=pair[0], entity_b=pair[1], airborne=True, ram=True,
                    closing_speed=closing, separation=contact[8],
                    point_a=tuple(contact[5 if forward else 6]),
                    point_b=tuple(contact[6 if forward else 5]),
                    ram_energy=energy, ram_energy_a=energy*share, ram_energy_b=energy*(1.-share))

    def _begin_fall(self, entity, pose):
        self._fall_episodes[entity] = self._fall_episodes.get(entity,0)+1
        self._fall_peaks[entity] = float(pose.position[1])
        self._track_impact_peaks.pop(entity,None)
        self._lean_states.pop(entity,None)
        self._drive_blocked.pop(entity,None)

    def _record_track_impact(self, entity, point, normal, speed, height):
        """Record a contact with a lower track surface, with one peak per side."""
        if normal[1] < .25 or speed <= 0.:
            return
        candidates = []
        for name,low,high in self._component_bounds(entity):
            if not name.startswith('chassis_track_'):
                continue
            # Compound Bullet shapes do not report the child index. Match
            # the contact to the actual belt footprint in the hull frame.
            if (low[0]-.12 <= point[0] <= high[0]+.12
                    and low[2]-.12 <= point[2] <= high[2]+.12
                    and point[1] <= low[1]+max(.2,(high[1]-low[1])*.55)):
                candidates.append((abs(float(point[0])-(low[0]+high[0])*.5),name))
        if not candidates:
            return
        name = min(candidates)[1]
        peaks = self._track_impact_peaks.setdefault(entity,{})
        old_speed,old_height = peaks.get(name,(0.,0.))
        if speed <= old_speed+1e-6 and height <= old_height+1e-6:
            return
        peaks[name] = max(old_speed,speed),max(old_height,height)
        self.track_impacts.append(dict(entity=entity,track=name,speed=float(speed),
            height=float(height),episode=self._fall_episodes.get(entity,0)))

    def _swept_track_landing(self, entity, pose, velocity):
        """Contact-side data for short ballistic hops that never need Bullet."""
        rotation = matrix(pose.yaw,pose.pitch,pose.roll)
        normal = rotation[:,1]
        speed = max(0.,-float(np.dot(velocity,normal)))
        height = max(0.,self._fall_peaks.get(entity,pose.position[1])-pose.position[1])
        for name,low,high in self._component_bounds(entity):
            if not name.startswith('chassis_track_'):
                continue
            local = np.array(((low[0]+high[0])*.5,low[1],(low[2]+high[2])*.5))
            point = rotation @ local+pose.position
            ground = self.ground_height(float(point[0]),float(point[2]),pose.position[1])
            if point[1]-ground <= self.SUSPENSION_TRAVEL+.08:
                self._record_track_impact(entity,local,(0.,1.,0.),speed,height)

    def _dynamic_contact_kind(self, body_id):
        if body_id in self.breakable_bodies:
            return 'breakable'
        if body_id in self.terrain_bodies:
            return 'terrain'
        if body_id in self.map_bodies:
            return 'map'
        if body_id in self.bodies.values():
            return 'vehicle'
        return 'other'

    def close(self):
        while not self.advance_close():
            pass

    def advance_close(self):
        """Release one native body at a time, retaining ownership until done."""
        if self.closed:
            return True
        if self._closer is None:
            self.closing = True
            if self._builder is not None:
                self._builder.close()
                self._builder = None
            self._closer = self._close_steps()
        try:
            next(self._closer)
            return False
        except StopIteration:
            self._closer = None
            return True
        except BaseException:
            # Keep the CID. A later synchronous close can retry cleanup rather
            # than silently losing an owned native connection after an error.
            self._closer = None
            raise

    def _close_steps(self):
        if self._native_physics is not None:
            self._native_physics.close()
            yield
        def connected():
            return (self.cid is not None
                    and (self._native_physics is None or self._native_physics.arena_alive())
                    and bullet.isConnected(self.cid))

        if connected():
            ids = [bullet.getBodyUniqueId(i,physicsClientId=self.cid)
                   for i in range(bullet.getNumBodies(physicsClientId=self.cid))]
            yield
            for body in ids:
                if not connected():
                    break
                bullet.removeBody(body,physicsClientId=self.cid)
                yield
            if connected():
                bullet.disconnect(self.cid)
        self._native_physics = None
        self.cid = None
        self.dynamic_bodies.clear()
        self.dynamic_shapes.clear()
        self._shared_dynamic_shapes.clear()
        self._mass_models.clear()
        self._terrain_probes.clear()
        self._kinematic_shapes.clear()
        self.dynamic_bounds.clear()
        self.dynamic_fall_peak.clear()
        self.dynamic_fall_gap.clear()
        self.fall_impacts.clear()
        self.track_impacts.clear()
        self._fall_episodes.clear()
        self._fall_peaks.clear()
        self._track_impact_peaks.clear()
        self.dynamic_contact_trace.clear()
        self.pending_dynamics.clear()
        self._separating_launches.clear()
        self._blocked_launches.clear()
        self.dynamic_mass_yaws.clear()
        self.dynamic_launch_angular_velocities.clear()
        self.short_flight_spins.clear()
        self.dynamic_recoveries.clear()
        self.dynamic_orientation_resets.clear()
        self.body_entities.clear()
        self.suspended_entities.clear()
        self.ground_motion.clear()
        self.ground_commands.clear()
        self.ground_track_rates.clear()
        self.ground_contacts.clear()
        self.ground_mass_properties.clear()
        self._support_memory.clear()
        self._support_hints.clear()
        self._support_bounds.clear()
        self._support_layouts.clear()
        self._support_ranks.clear()
        self._support_systems.clear()
        self._chassis_bounds.clear()
        self._component_bound_cache.clear()
        self._mesh_body_paths.clear()
        if hasattr(self, '_pivot_map_pairs'):
            self._pivot_map_pairs.clear()
        self._hull_clearances.clear()
        self.track_links.clear()
        self._road_poses.clear()
        self._drive_efforts.clear()
        self._spring_rates.clear()
        self._lean_states.clear()
        self._drive_blocked.clear()
        self._wall_recoil_latches.clear()
        self._wall_glances.clear()
        self.last_wall_recoil = None
        self._carrying_supported.clear()
        self._contact_support_cache.clear()
        self._rust_drive = None
        self.last_ground_impacts = ()
        self.closed = True
        self.closing = False

    def set_pose(self, body, pose):
        bullet.resetBasePositionAndOrientation(body, pose.position,
                    quaternion(pose.yaw, pose.pitch, pose.roll), physicsClientId=self.cid)

    def training_placement(self, entity, x, z, yaw, *, stationary=False, reference=None):
        """Find a supported placement without leaving a speculative sensor pose."""
        low_x, low_z, high_x, high_z = self.assets.bounds
        if not (low_x < x < high_x and low_z < z < high_z):
            return None
        previous = self.poses[entity]
        pose = self.ground_pose(x, z, yaw, reference, entity=entity)
        try:
            if stationary:
                valid = self._inside(pose, entity) and not self._contacts(pose, entity, suspension=True)
            else:
                valid, _ = self._valid(pose, previous, entity=entity, climb=False, suspension=True)
        finally:
            self.set_pose(self.bodies[entity], previous)
        return pose if valid else None

    def first_stationary_placement(self, entity, positions, yaw, reference):
        """Sample several nearby footprints together, then check poses in order."""
        low_x, low_z, high_x, high_z = self.assets.bounds
        positions = [(x, z) for x, z in positions if low_x < x < high_x and low_z < z < high_z]
        if not positions:
            return None
        layout = self._support_layout(entity)
        # Keep the ray plan within the native sampler's 256-station limit.
        batch_size = max(1, min(8, 256 // len(layout.a)))
        c, s = math.cos(yaw), math.sin(yaw)
        previous = self.poses[entity]
        try:
            for offset in range(0, len(positions), batch_size):
                group = positions[offset:offset+batch_size]
                centers = np.asarray(group)
                xs = centers[:, 0, None] + c*layout.a + s*layout.b
                zs = centers[:, 1, None] - s*layout.a + c*layout.b
                ground = self._ground_heights(xs.ravel(), zs.ravel(), reference, entity).reshape(xs.shape)
                # Support hints and the first unobstructed pose depend on this order.
                for (x, z), row_x, row_z, heights in zip(group, xs, zs, ground):
                    pose = self._support_pose(x, z, yaw, reference, entity=entity,
                                              _sample=(layout, row_x, row_z, heights))
                    if self._inside(pose, entity) and not self._placement_blocked(pose, entity):
                        return pose
        finally:
            self.set_pose(self.bodies[entity], previous)
        return None

    def can_resume_pose(self, entity, pose, *, grounded):
        """Validate the exact attitude and altitude against the live map."""
        if not self._inside(pose, entity):
            return False
        x, y, z = pose.position
        floor = self.held_pose(x, z, pose.yaw, pose.pitch, pose.roll, y, entity=entity)
        suspension = grounded and self._has_suspension(entity)
        gap = y-floor.position[1]
        compression = self.SUSPENSION_TRAVEL if suspension else 0.
        if gap < -compression-self.SKIN or grounded and gap > self.SUPPORT_REACH:
            return False
        previous = self.poses[entity]
        try:
            return not self._contacts(pose, entity, suspension=suspension)
        finally:
            self.set_pose(self.bodies[entity], previous)

    def relocate_training_body(self, entity, pose, *, motion=None, road=None, lean=None):
        """Move an existing body and start contact from the supplied physical state."""
        self.pending_dynamics.pop(entity, None)
        self._separating_launches.pop(entity, None)
        self._blocked_launches.pop(entity, None)
        road = pose if road is None else road
        self._road_poses[entity] = road
        if entity in self.dynamic_bodies:
            self._deactivate_dynamic(entity, pose, (0., 0., 0.))
        self.poses[entity] = pose
        self.motion[entity] = state = Motion() if motion is None else motion
        self.ground_motion[entity] = GroundMotion()
        self._fall_peaks.pop(entity, None)
        self._spring_rates.pop(entity, None)
        self._lean_states.pop(entity, None)
        self._drive_blocked.pop(entity, None)
        self._wall_recoil_latches.pop(entity, None)
        self._wall_glances.pop(entity, None)
        if state.grounded:
            self._seed_support_memory(entity, road, state.velocity[1])
            if lean is not None:
                self._lean_states[entity] = lean
        else:
            self._begin_fall(entity, pose)
        self.set_pose(self.bodies[entity], pose)
        self._synced[entity] = (pose, self.motion[entity])

    def replace_training_body(self, entity, assets, spawns):
        """Fit the replacement hull at a team spawn before committing it."""
        previous = self.assets_for(entity)
        self._blocked_launches.clear()
        pose = self.poses[entity]
        if not self.vehicle_assets:
            self.vehicle_assets.update(dict.fromkeys(self.bodies, self.assets))
        old_body, old_turret = self.bodies[entity], self.turret_links[entity]
        old_tracks = self.track_links[entity]
        self.vehicle_assets[entity] = assets
        body = None
        placed = None
        try:
            body, turret = self._tank(pose.position, _TRACKED_GROUP, entity)
            self.bodies[entity], self.turret_links[entity] = body, turret
            self.track_links[entity] = frozenset(
                index for index, name in enumerate(
                    part for part, _ in assets.tank_shapes if part != 'hull')
                if name.startswith('chassis_track_'))
            for x, z, yaw in spawns:
                placed = self.training_placement(entity, x, z, yaw, stationary=True)
                if placed is not None:
                    break
            if placed is None:
                return None
            if entity in self.dynamic_bodies:
                # The old ragdoll owns its previous dimensions and mass.
                self.bodies[entity] = old_body
                self._deactivate_dynamic(entity, pose, (0., 0., 0.))
                self.bodies[entity] = body
            self.gun_angles[entity] = (0., 0.)
            getattr(self, '_pivot_map_pairs', {}).pop(entity, None)
            self.dynamic_shapes.pop(entity, None)
            self.dynamic_bounds.pop(entity, None)
            self._contact_support_cache.pop(entity, None)
            self._support_hints.pop(entity, None)
            self._support_bounds.pop(entity, None)
            self._carrying_supported.pop(entity, None)
            self._drive_efforts.pop(entity, None)
            if self.experimental_ground_impacts:
                self.ground_mass_properties[entity] = self._dynamic_mass_properties(entity, 0.)
            if self._native_physics is not None:
                self._native_physics.replace_vehicle(self, entity)
            self.relocate_training_body(entity, placed)
            bullet.removeBody(old_body, physicsClientId=self.cid)
            del self.body_entities[old_body]
            self.body_entities[body] = entity
            if all(current is not previous for current in self.vehicle_assets.values()):
                self._mass_models.pop(id(previous), None)
                self._terrain_probes.pop(id(previous), None)
            return placed
        finally:
            if placed is None:
                self.vehicle_assets[entity] = previous
                self.bodies[entity], self.turret_links[entity] = old_body, old_turret
                self.track_links[entity] = old_tracks
                if body is not None:
                    bullet.removeBody(body, physicsClientId=self.cid)

    def release_practice_scene(self, assets):
        self._support_layouts.pop(id(assets), None)
        self._mass_models.pop(id(assets), None)
        self._terrain_probes.pop(id(assets), None)
        assets.close()

    def set_turret(self, yaw, entity=None, pitch=0.):
        entity = self.primary_id if entity is None else entity
        # Without a turret link the angles still rotate the armor gun about the XML pivot.
        if self.turret_links[entity] is not None:
            bullet.resetJointState(self.bodies[entity],self.turret_links[entity],yaw,physicsClientId=self.cid)
        self.gun_angles[entity] = (yaw,pitch)

    def _terrain_heights(self, xs, zs):
        """Vectorised ``terrain.height`` for the installed bilinear heightmap.

        Any other sampler (or a replaced ``height``) is called per point.
        """
        xs, zs = np.asarray(xs, dtype=float), np.asarray(zs, dtype=float)
        terrain = self.terrain
        if xs.shape == zs.shape == (1,):
            return np.array((terrain.height(float(xs[0]), float(zs[0])),))
        if getattr(terrain.height, '__func__', None) is not MapTerrain.height:
            return np.array([terrain.height(float(x), float(z)) for x, z in zip(xs, zs)])
        if not (np.isfinite(xs).all() and np.isfinite(zs).all()):
            raise ValueError("Invalid terrain coordinate")
        if self._terrain_grid is None or self._terrain_grid[0] is not terrain.heights:
            self._terrain_grid = (terrain.heights, np.asarray(terrain.heights, dtype=float))
        return terrain.sample_grid(xs, zs, self._terrain_grid[1])

    def _support_references(self, reference, xs, zs, entity=None):
        """Height from which support reach is measured at each point.

        A scalar is the hull origin height, as for a landing. A carrying
        pose also measures reach from its own plane where that plane is
        higher: on a mesh ramp the leading stations sit well above the hull
        origin, so a ray started only SUPPORT_REACH above the origin begins
        inside the deck and returns its underside or misses it entirely.
        The extended reach never rises above the vehicle's climb angle from
        the hull origin, so a steeper face or a nose-up attitude against a
        wall cannot carry a station further up it than before.
        """
        if not isinstance(reference, Pose):
            return reference
        x0, y0, z0 = reference.position
        up = matrix(reference.yaw, reference.pitch, reference.roll)[:, 1]
        dx = np.asarray(xs, dtype=float)-x0
        dz = np.asarray(zs, dtype=float)-z0
        rise = -(up[0]*dx+up[2]*dz)/max(.05, float(up[1]))
        limit = np.hypot(dx, dz)*math.tan(self.assets_for(entity).max_slope)-self.SUPPORT_REACH
        return y0+np.clip(rise, 0., np.maximum(0., limit))

    def _ground_heights(self, xs, zs, reference=None, entity=None, *, _plan=None, _nearby=None):
        """Terrain or a walkable map top under each point, in one ray batch.

        A carrying pose is first measured from its origin height, exactly as
        a scalar reference. Where its plane reaches higher, a second ray from
        that plane replaces the result only with a top above the first ray's
        start: the first ray began beneath that surface and could not see it.
        Any top the origin ray already starts above keeps its measurement.
        """
        xs, zs = np.asarray(xs, dtype=float), np.asarray(zs, dtype=float)
        native = getattr(self,'_rust_drive',None)
        plan = _plan if _plan is not None else (native.plan_ground(xs,zs,reference,entity) if native else None)
        if plan is None:
            terrain = self._terrain_heights(xs, zs)
            plane = None
            if isinstance(reference, Pose):
                plane = self._support_references(reference, xs, zs, entity)
                reference = reference.position[1]
            tops = (np.maximum(terrain, reference) if reference is not None
                    else terrain) + self.SUPPORT_REACH
            starts = np.maximum(terrain, plane) + self.SUPPORT_REACH if plane is not None else tops
            low = (float(xs.min())-self.SKIN,float(terrain.min())-.4-self.SKIN,
                   float(zs.min())-self.SKIN)
            high = (float(xs.max())+self.SKIN,float(np.maximum(tops,starts).max())+self.SKIN,
                    float(zs.max())+self.SKIN)
        else:
            terrain,tops,starts,bounds,raised = plan
            plane = True if isinstance(reference,Pose) else None
            low,high = bounds[:3],bounds[3:]
        # Most track samples are over terrain alone. The heightmap above is
        # authoritative for that case; casting through its triangle bodies
        # cannot contribute a map top. Reject only an entirely empty solid-map
        # broadphase, keeping the original rays and occlusion near any object.
        nearby = (_nearby if _nearby is not None else
                  bullet.getOverlappingObjects(low,high,physicsClientId=self.cid) or ())
        if not any(body in self.map_bodies and body not in self.breakable_bodies
                   for body, _ in nearby):
            return terrain
        x_values,z_values = xs.tolist(),zs.tolist()
        ray_starts = list(zip(x_values,tops.tolist(),z_values))
        ray_ends = list(zip(x_values,(terrain-.4).tolist(),z_values))
        raised_hits = None
        fuse = (os.environ.get('SOLBREAKER_GROUND_RAY_BATCH') == '1'
                and bullet.rayTestBatch is _GROUND_RAY_BATCH_SOURCE
                and getattr(self._static_rays,'__func__',None) is _GROUND_RAYS_IMPL)
        if plane is not None:
            if plan is None:
                raised = np.flatnonzero(starts > tops)
            if fuse and len(raised):
                raised_x,raised_z = xs[raised].tolist(),zs[raised].tolist()
                extra_starts = list(zip(raised_x,starts[raised].tolist(),raised_z))
                extra_ends = list(zip(raised_x,(terrain[raised]-.4).tolist(),raised_z))
                all_hits = self._static_rays(ray_starts+extra_starts,ray_ends+extra_ends)
                hits,raised_hits = all_hits[:len(xs)],all_hits[len(xs):]
                self._batched_ground_queries = getattr(self,'_batched_ground_queries',0)+1
            else:
                hits = self._static_rays(ray_starts,ray_ends)
        else:
            hits = self._static_rays(ray_starts,ray_ends)
        heights = terrain.copy()
        for index, hit in enumerate(hits):
            if (hit[0] in self.map_bodies and hit[0] not in self.breakable_bodies
                    and hit[4][1] > .65):
                heights[index] = max(terrain[index], hit[3][1])
        if plane is not None and len(raised):
            if raised_hits is None:
                raised_x,raised_z = xs[raised].tolist(),zs[raised].tolist()
                raised_hits = self._static_rays(
                    list(zip(raised_x,starts[raised].tolist(),raised_z)),
                    list(zip(raised_x,(terrain[raised]-.4).tolist(),raised_z)))
            for index, hit in zip(raised, raised_hits):
                if (hit[0] in self.map_bodies and hit[0] not in self.breakable_bodies
                        and hit[4][1] > .65 and hit[3][1] > tops[index]):
                    heights[index] = max(terrain[index], hit[3][1])
        return heights

    def ground_height(self, x, z, reference=None):
        return float(self._ground_heights((x,), (z,), reference)[0])

    def landing_attitude(self, entity, pose, reach):
        """Fit the local terrain under the tracks, excluding steep faces and ledges."""
        x, y, z = pose.position
        center = self.terrain.height(x, z)
        clearance = y - center
        if not -.5 <= clearance < reach:
            return None
        assets = self.assets_for(entity)
        width, length = assets.support
        c, s = math.cos(pose.yaw), math.sin(pose.yaw)
        a, b = c * width, s * length
        d, e = -s * width, c * length
        lf, lr, rf, rr = self._terrain_heights(
            (x-a+b, x-a-b, x+a+b, x+a-b), (z-d+e, z-d-e, z+d+e, z+d-e))
        if abs((lf + lr + rf + rr) * .25 - center) > .45 or abs(lf + rr - lr - rf) > 1.4:
            return None
        slope_x = (rf + rr - lf - lr) / (4. * width)
        slope_z = (lf + rf - lr - rr) / (4. * length)
        if math.hypot(slope_x, slope_z) > math.tan(assets.max_slope):
            return None
        blend = min(1., 2.5 * (1. - clearance / reach))
        pitch = -math.atan(slope_z)
        roll = math.atan(slope_x / math.hypot(1., slope_z))
        return pitch, roll, blend

    @staticmethod
    def _lower_profile(vertices):
        """Lower convex (z, y) envelope: the part of a convex track that meets ground."""
        points = np.unique(np.round(np.asarray(vertices, dtype=float)[:, (2, 1)], 6), axis=0)
        lower = []
        for point in points:
            while len(lower) >= 2 and (
                    (lower[-1][0]-lower[-2][0])*(point[1]-lower[-2][1])
                    - (lower[-1][1]-lower[-2][1])*(point[0]-lower[-2][0])) <= 0:
                lower.pop()
            lower.append(point)
        # A vertical end face belongs to the side of the track, not its floor.
        lower = np.asarray([point for index, point in enumerate(lower)
                            if index == 0 or point[0] > lower[index-1][0]])
        return lower[:, 0], lower[:, 1]

    def _support_layout(self, entity=None):
        assets = self.assets_for(entity)
        cached = self._support_layouts.get(id(assets))
        if cached is None or cached[0] is not assets:
            cached = self._support_layouts[id(assets)] = (assets, self._build_support_layout(assets))
        return cached[1]

    @classmethod
    def _build_support_layout(cls, assets):
        """Stations along both tracks, taken from the vehicle's own geometry.

        The XML carrying points bound each contact patch. The track collision
        meshes, which Bullet tests for contact, provide the lateral footprint
        and the rise of their sloped ends. A vehicle without track meshes
        keeps flat patches.
        """
        width, length = (float(value) for value in assets.support)
        spans = max(1, math.ceil(2*length/cls.SUPPORT_SPACING - 1e-9))
        patch = np.linspace(-length, length, spans+1)
        shapes = list(getattr(assets, 'tank_shapes', ()))
        rows, tracks = [], []
        for sign in (-1., 1.):
            lateral = sign*width
            rows.extend((lateral, b, 0., 1.) for b in patch)
            track = next((np.asarray(vertices, dtype=float) for name, vertices in shapes
                          if name.startswith('chassis_track_')
                          and float(np.mean(np.asarray(vertices, dtype=float)[:, 0]))*sign > 0), None)
            if track is None:
                tracks.append((lateral, lateral, np.array((-length, length)), np.zeros(2)))
                continue
            zs, ys = cls._lower_profile(track)
            floor = float(np.min(np.interp(patch, zs, ys)))
            ys = ys - floor
            tracks.append((min(float(track[:, 0].min()), lateral),
                           max(float(track[:, 0].max()), lateral), zs, ys))
            for end, limit in ((1., float(zs[-1])), (-1., float(zs[0]))):
                reach = end*limit - length
                if reach <= 1e-6:
                    continue
                count = max(1, math.ceil(reach/cls.SUPPORT_SPACING - 1e-9))
                for k in range(1, count+1):
                    b = end*(length + reach*k/count)
                    rows.append((lateral, b, max(0., float(np.interp(b, zs, ys))), 0.))
        a, b, p, weight = (np.array(column, dtype=float) for column in zip(*rows))
        return SupportLayout(a, b, p, weight, tuple(tracks), length)

    @staticmethod
    def _small_solve(matrix, vector):
        try:
            solution = np.linalg.solve(matrix, vector)
            if np.isfinite(solution).all():
                return solution
        except np.linalg.LinAlgError:
            pass
        return np.linalg.lstsq(matrix, vector, rcond=None)[0]

    @classmethod
    def _support_plane(cls, phi, target, weight, lower, hint=(), *, system=None):
        """Stiffest-fit support plane that keeps every station above its bound.

        Minimises sum(weight*(phi@u-target)**2) subject to phi@u >= lower with
        a primal active-set method. Every iterate is feasible, so stopping at
        the iteration bound still never leaves a track inside the ground.
        ``hint`` is a previous active set; it is used only as a starting point
        when it is feasible here. Returns the plane and its active set.
        """
        from .sim_native import try_support_plane
        native = try_support_plane(phi,target,weight,lower,hint)
        if native is not None:
            return native
        if system is None:
            weighted = phi.T*weight
            hessian = weighted @ phi + 1e-9*np.eye(3)
            inverse = np.linalg.inv(hessian)
        else:
            weighted, hessian, inverse = system
        linear = weighted @ target
        u, active = None, []
        rows = phi[list(hint)] if 0 < len(hint) <= 3 else None
        # Only an independent working set is a valid start (its multipliers
        # are then unique); collinear stations on one track are not.
        if rows is not None and np.linalg.det(rows @ rows.T) > 1e-6:
            projected = rows @ inverse
            multipliers = cls._small_solve(projected @ rows.T, lower[list(hint)] - projected @ linear)
            guess = inverse @ (linear + rows.T @ multipliers)
            if float(np.min(phi @ guess - lower)) >= -1e-9:
                u, active = guess, list(hint)
        if u is None:
            u = inverse @ linear
            slack = phi @ u - lower
            worst = int(np.argmin(slack))
            if slack[worst] < 0:
                u[0] -= slack[worst]
                active.append(worst)
        free = np.ones(len(phi), dtype=bool)
        for _ in range(16):
            gradient = hessian @ u - linear
            if active:
                # Range-space step on the active rows: H p = A'm - g with
                # A p = 0, so (A H^-1 A') m = A H^-1 g. A blocking row is never
                # a combination of active rows (its rate would be zero), so
                # this system is regular; lstsq is only a numerical fallback.
                rows = phi[active]
                projected = rows @ inverse
                multipliers = cls._small_solve(projected @ rows.T, projected @ gradient)
                step = inverse @ (rows.T @ multipliers - gradient)
            else:
                step, multipliers = -(inverse @ gradient), None
            if float(np.abs(step).max()) < 1e-10:
                if multipliers is None or float(multipliers.min()) >= -1e-10:
                    break
                active.pop(int(np.argmin(multipliers)))
                continue
            rate = phi @ step
            free[:] = True
            free[active] = False
            closing = np.flatnonzero(free & (rate < -1e-12))
            alpha, blocking = 1., None
            if len(closing):
                limits = np.maximum(0., (phi[closing] @ u - lower[closing]))/-rate[closing]
                nearest = int(np.argmin(limits))
                if limits[nearest] < 1.:
                    alpha, blocking = float(limits[nearest]), int(closing[nearest])
            u = u + alpha*step
            if blocking is not None:
                active.append(blocking)
        return u, tuple(active)

    def _hull_clearance(self, entity):
        entity = self.primary_id if entity is None else entity
        if entity not in self._hull_clearances:
            hull = next(vertices for name,vertices in self.assets_for(entity).tank_shapes
                        if name == 'hull')
            self._hull_clearances[entity] = max(0.,float(np.min(hull[:,1])))
        return self._hull_clearances[entity]

    def _support_pose(self, x, z, yaw, reference=None, *, entity=None, floors=None,
                      extra=(), compression=0., carrying=False, _sample=None):
        """Supported pose on all track stations, optionally with held stations.

        ``floors`` are the lowest carrying-plane heights each station can
        fall to in this slice (see _support_floors). ``extra`` adds measured
        contact supports as (a, b, lowest plane height) rows. A fourth false
        item marks a rigid belly constraint rather than a compliant belt.
        """
        if _sample is None:
            layout = self._support_layout(entity)
            c, s = math.cos(yaw), math.sin(yaw)
            xs = x + c*layout.a + s*layout.b
            zs = z - s*layout.a + c*layout.b
            ground = self._ground_heights(xs, zs, reference, entity)
        else:
            layout,xs,zs,ground=_sample
        native = getattr(self,'_rust_drive',None)
        native_support = getattr(native,'try_support_pose',None)
        if (native_support is not None
                and getattr(self._support_plane,'__func__',None) is _SUPPORT_PLANE_IMPL
                and getattr(self._terrain_heights,'__func__',None) is _TERRAIN_HEIGHTS_IMPL):
            measured = native_support(layout,x,z,yaw,xs,zs,ground,
                                      carrying,compression,extra,floors,entity)
            if measured is not None:
                return measured
        weight = layout.weight
        if carrying and entity in self._road_poses:
            # Droop is measured from the actual carrying hull, not the road
            # target it has not reached yet. A blocked stone can raise the
            # proposed fit far above the body; using that target as the next
            # reference incorrectly discards all ordinary ground contacts.
            old = carrying if isinstance(carrying,Pose) else self._road_poses[entity]
            lean = self._lean_states.get(entity,(0.,0.,0.,0.))
            old = Pose(old.position,old.yaw,old.pitch-lean[0],old.roll-lean[1])
            up = matrix(old.yaw,old.pitch,old.roll)[:,1]
            prediction = (old.position[1]-(up[0]*(xs-old.position[0])
                           +up[2]*(zs-old.position[2]))/max(.05,float(up[1])))
            # A station beyond maximum droop is not a tensile spring. Its
            # lower road sample cannot pitch the still-supported tank down
            # before the centre reaches a genuine ledge.
            reachable = ground-layout.p >= prediction-self.SUPPORT_REACH
            relief = np.concatenate((ground-layout.p,
                                      np.asarray([row[2] for row in extra],dtype=float)))
            if float(np.ptp(relief)) <= self.SUPPORT_REACH+self.SKIN:
                # An isolated stone inside the ordinary chassis reach is
                # still a supported road feature. A temporarily raised hull
                # must not discard the surrounding ground and extrapolate
                # its old nose-up plane into a ramp over that short stone.
                reachable[:] = True
            weight = layout.weight*reachable
            active = np.flatnonzero(weight > 0.)
            supported = (len(active) > 0 and
                         layout.a[active].min() <= .05 and layout.a[active].max() >= -.05 and
                         layout.b[active].min() <= .05 and layout.b[active].max() >= -.05)
            self._carrying_supported[entity] = supported
            if len(active) >= 3:
                # Rank depends on these immutable stations, not the pose.
                # Distinct partial footprints at ledges get distinct keys.
                rank_key = (id(layout), active.tobytes())
                rank = self._support_ranks.get(rank_key)
                if rank is None:
                    if len(self._support_ranks) >= 128:
                        self._support_ranks.clear()
                    rank = np.linalg.matrix_rank(np.column_stack((np.ones(len(active)),
                                                   layout.a[active],layout.b[active])))
                    self._support_ranks[rank_key] = rank
            else:
                rank = 0
            if len(active) < 3 or rank < 3:
                y = old.position[1]-(up[0]*(x-old.position[0])
                                     +up[2]*(z-old.position[2]))/max(.05,float(up[1]))
                self._support_bounds.pop(entity if entity is not None else self.primary_id,None)
                return Pose((float(x),float(y),float(z)),yaw,old.pitch,old.roll)
        centre = self._terrain_heights((x,), (z,))[0]
        lower = ground - layout.p - compression
        if floors is not None:
            # A station whose ground falls away faster than gravity lets it
            # follow is held on that bound, so the hull pivots over a rock or
            # crest edge instead of snapping down onto the next support. On a
            # steady grade each station's remembered speed already matches
            # the ground's, so the bound is inactive there. Ground beyond
            # reach below the bound is a ledge: the station lets go and
            # _release decides whether the hull flies.
            held = floors - lower <= self.SUPPORT_REACH
            lower = np.where(held, np.maximum(lower, floors), lower)
        phi = [layout.plane_rows, ((1., 0., 0.),)]
        # The terrain below the centre is a hull clearance check, not a
        # third track. On a narrow rock between tracks, the belly may pass
        # above it without lifting the entire vehicle by the rock's height.
        centre_floor = centre - self._hull_clearance(entity) + self.SKIN if compression else centre
        bounds = [lower, (centre_floor,)]
        for row in extra:
            a,b,height = row[:3]
            compliant = len(row) < 4 or row[3]
            phi.append(((1., a, b),))
            bounds.append((height-(compression if compliant else 0.),))
        phi = np.vstack(phi) if extra else layout.fit_rows
        count = len(phi) - len(layout.a)
        # Consecutive solves for one hull usually share their contact
        # stations; the last active set saves most active-set iterations.
        stations = len(layout.a)
        key = entity if entity is not None else self.primary_id
        fit_weight = np.concatenate((weight, np.zeros(count)))
        system = None
        if not extra:
            system_key = (id(layout), fit_weight.tobytes())
            system = self._support_systems.get(system_key)
            if system is None:
                if len(self._support_systems) >= 128:
                    self._support_systems.clear()
                weighted = phi.T*fit_weight
                hessian = weighted @ phi + 1e-9*np.eye(3)
                system = (weighted, hessian, np.linalg.inv(hessian))
                self._support_systems[system_key] = system
        (y, slope_x, slope_z), active = self._support_plane(
            phi, np.concatenate((ground, np.zeros(count))),
            fit_weight, np.concatenate(bounds),
            tuple(index for index in self._support_hints.get(key, ()) if index < stations),
            system=system)
        self._support_hints[key] = tuple(index for index in active if index < stations)
        pitch = -math.atan(slope_z)
        roll = math.atan(slope_x/math.sqrt(1+slope_z*slope_z))
        pose = Pose((float(x), float(y), float(z)), yaw, pitch, roll)
        # The hard limits this fit honoured, for a bottomed suspension.
        self._support_bounds[key] = (pose, phi, np.concatenate(bounds))
        return pose

    def _spring_pose(self, previous, target, dt, state, entity=None, supports=()):
        """Critically damp heave/attitude toward the compressed support fit.

        A grade's vertical velocity is feed-forward from the old support
        normal, so the spring only absorbs a sudden change in the road shape.
        There is no positional jump or rigid-body impulse from a single new
        track station. Collision validation below still owns hard obstacles.
        """
        if not self.sprung_tracks or state is None or dt is None:
            return target
        vx = (target.position[0]-previous.position[0])/dt
        vz = (target.position[2]-previous.position[2])/dt
        road = self._road_poses.get(entity,previous)
        up = matrix(road.yaw,road.pitch,road.roll)[:,1]
        grade_speed = (-(up[0]*vx+up[2]*vz)/up[1] if up[1] > .5 else 0.)
        omega = self.SUSPENSION_OMEGA
        def step(value, goal, velocity, feed=0., frequency=omega):
            decay = math.exp(-frequency*dt)
            old_goal = goal-feed*dt
            error = value-old_goal
            relative = velocity-feed
            c = relative+frequency*error
            result = goal+(error+c*dt)*decay
            rate = feed+(relative-frequency*c*dt)*decay
            return float(result),float(rate)
        longitudinal,lateral,throttle = self._drive_efforts.get(entity,(0.,0.,0.))
        pitch_lean = max(-.045,min(.045,-longitudinal*.004))
        roll_lean = max(-.045,min(.045,-lateral*.004))
        old_lp,old_lr,old_vp,old_vr = self._lean_states.get(entity,(0.,0.,0.,0.))
        lp,lvp = step(old_lp,pitch_lean,old_vp,frequency=self.SUSPENSION_ANGULAR_OMEGA)
        lr,lvr = step(old_lr,roll_lean,old_vr,frequency=self.SUSPENSION_ANGULAR_OMEGA)
        vy,vpitch,vroll = state.suspension_velocity
        y,vy = step(previous.position[1],target.position[1],vy,grade_speed)
        free_y = previous.position[1]+state.suspension_velocity[0]*dt-.5*self.GRAVITY*dt*dt
        free_vy = state.suspension_velocity[0]-self.GRAVITY*dt
        unloaded = y < free_y
        if unloaded:
            # Normal suspension forces can push the hull upward. They cannot
            # pull it down toward a road that is dropping away faster than
            # gravity; retain that heave momentum until contact is regained.
            y,vy = free_y,max(vy,free_vy)
        pitch,vpitch = step(previous.pitch-old_lp,target.pitch,vpitch-old_vp)
        roll,vroll = step(previous.roll-old_lr,target.roll,vroll-old_vr)
        pitch,roll = pitch+lp,roll+lr
        vpitch,vroll = vpitch+lvp,vroll+lvr
        rotation = matrix(target.yaw,pitch,roll)
        unleaned = matrix(target.yaw,pitch-lp,roll-lr)
        for row in supports:
            a,b,height = row[:3]
            compliant = len(row) < 4 or row[3]
            profile = (self._support_layout(entity).track_profile(a,b,self.SKIN)
                       if compliant else self._hull_clearance(entity))
            if profile is None:
                continue
            offset = rotation @ np.array((a,profile,b))
            base_offset = unleaned @ np.array((a,profile,b))
            # Effort lean must not consume more than the belt's actual
            # compression travel against a measured rock. Shift the sprung
            # hull by the required clearance, without adding launch momentum.
            needed = height+profile-offset[1]-(self.SUSPENSION_TRAVEL if compliant else 0.)-y
            y += min(max(0.,needed),max(0.,base_offset[1]-offset[1]))
        pose = Pose((target.position[0],y,target.position[2]),target.yaw,pitch,roll)
        launch_rates = (state.support_angular_rates if unloaded else
                        ((pitch-previous.pitch)/dt,(roll-previous.roll)/dt))
        self._spring_rates[entity] = (pose,(vy,vpitch,vroll),(lp,lr,lvp,lvr),launch_rates)
        return pose

    def _station_heights(self, pose, entity=None):
        """Carrying-plane height under each support station of a pose."""
        layout = self._support_layout(entity)
        slope_z = -math.tan(pose.pitch)
        slope_x = math.tan(pose.roll)*math.sqrt(1+slope_z*slope_z)
        return pose.position[1] + layout.a*slope_x + layout.b*slope_z

    def ground_pose(self, x, z, yaw, reference=None, *, entity=None):
        return self._support_pose(x, z, yaw, reference, entity=entity)

    def held_pose(self, x, z, yaw, pitch, roll, reference=None, *, entity=None):
        """Lowest supported pose at x,z that keeps the given attitude."""
        layout = self._support_layout(entity)
        support = matrix(yaw,pitch,roll) @ np.vstack((layout.a, layout.p, layout.b))
        ground = self._ground_heights(x+support[0], z+support[2], reference)
        y = float(np.max(ground-support[1]))
        y = max(y, float(self._terrain_heights((x,), (z,))[0]))
        return Pose((float(x),float(y),float(z)),yaw,pitch,roll)

    def _contacts(self, pose, entity=None, *, suspension=False, pair_queries=None, landing_probe=False):
        entity = self.primary_id if entity is None else entity
        player = self.bodies[entity]
        self.set_pose(player, pose)
        if _contact_queries is not None:
            recovery = None if pair_queries is not None else getattr(self, '_recovery_closest', None)
            if pair_queries is not None:
                pair_queries.pose = _PIVOT_POSE_BYTES(*pose.position, pose.yaw, pose.pitch,
                                                     pose.roll, pair_queries.turret)
                queries = pair_queries
            elif recovery is None:
                queries = bullet
            else:
                recovery.pose = _RECOVERY_CONTACT_POSE(*pose.position,pose.yaw,pose.pitch,pose.roll)
                queries = recovery
                if recovery.filtered is not None:
                    supports = self._contact_support_cache.get(entity, ())
                    filter_key = (entity, recovery.pose, suspension,
                                  tuple(_RECOVERY_SUPPORT_KEY(*support) for support in supports))
                    saved = recovery.filtered.get(filter_key)
                    if saved is not None:
                        return saved
            points = _contact_queries.gather(queries, self.cid, player, self.SKIN,
                self.map_bodies, self.body_entities,
                getattr(self, '_contact_query_observer', None), entity)
        else:
            points = []
            low,high = [float('inf')]*3,[float('-inf')]*3
            for link in range(-1,bullet.getNumJoints(player,physicsClientId=self.cid)):
                a,b = bullet.getAABB(player,link,physicsClientId=self.cid)
                for axis in range(3):
                    low[axis] = min(low[axis],a[axis])
                    high[axis] = max(high[axis],b[axis])
            query_low = [value-self.SKIN for value in low]
            query_high = [value+self.SKIN for value in high]
            observer = getattr(self,'_contact_query_observer',None)
            if observer is not None:
                observer(entity,query_low,query_high)
            near = bullet.getOverlappingObjects(query_low,query_high,
                                                physicsClientId=self.cid) or []
            other_tanks = self.body_entities.keys() - {player}
            bodies = {b for b,_ in near if b in self.map_bodies or b in other_tanks}
            for body in bodies:
                points.extend(bullet.getClosestPoints(player, body, self.SKIN,
                                                       physicsClientId=self.cid))
        # Landing probes against a wall can return many track points on the
        # same mesh. Their independent incline rays share one fixed pose.
        if (landing_probe and suspension and len(points) >= 12
                and self.experimental_6dof and not self.wall_trampoline
                and CollisionWorld._blocking_contacts is _RECOVERY_BLOCKING_CONTACTS
                and not getattr(self, 'collision_zone', None) and not self.trace_dynamic_contacts
                and getattr(self, '_contact_query_observer', None) is None
                and getattr(self._compressed_track_contact, '__func__', None) is _COMPRESSED_CONTACT_IMPL
                and getattr(self._track_incline, '__func__', None) is _TRACK_INCLINE_IMPL
                and self._can_batch_incline_rays()):
            filtered = self._batch_track_contacts(pose, entity, points)
        else:
            filtered = CollisionWorld._blocking_contacts(self, pose, entity, suspension, points)
        if _contact_queries is not None and recovery is not None and recovery.filtered is not None:
            recovery.filtered[filter_key] = filtered
        return filtered

    def _placement_blocked(self, pose, entity):
        if (getattr(_contact_queries, 'API_VERSION', None) != 2
                or getattr(self._contacts, '__func__', None) is not _CONTACTS_IMPL):
            return bool(self._contacts(pose, entity, suspension=True))
        player = self.bodies[entity]
        self.set_pose(player, pose)
        return _contact_queries.gather(bullet, self.cid, player, self.SKIN,
            self.map_bodies, self.body_entities,
            getattr(self, '_contact_query_observer', None), entity,
            partial(self._blocking_contacts, pose, entity, True))

    def _blocking_contacts(self, pose, entity, suspension, points):
        if suspension and len(points) >= 12 and getattr(self, '_recovery_batch_active', False):
            return self._batch_track_contacts(pose, entity, points)
        if getattr(self, 'collision_zone', None):
            inside = self.collision_zone(pose.position)
            points = (p for p in points if p[2] not in self.body_entities or
                      self.collision_zone(self.poses[self.body_entities[p[2]]].position) == inside)
        return [p for p in points if p[8] < self.SKIN
                and not (p[2] in self.map_bodies and p[7][1]>.65 and p[8]>-.025)
                and not (suspension and p[3] in self.track_links.get(entity,())
                         and p[2] in self.map_bodies
                         and p[2] not in self.breakable_bodies
                         and p[7][1] > .65
                         and p[8] > -self.SUSPENSION_TRAVEL-self.SKIN)
                and not (suspension and self._compressed_track_contact(entity,pose,p))]

    def _compressed_track_contact(self, entity, pose, point):
        """A verified map top can consume belt travel at its front/side edge.

        A rigid track sensor can report a horizontal face while its lower
        surface is only compressed into a low rock. Match a nearby measured
        top, rather than permitting generic side penetration into map walls.
        Inclined triangle edges use their measured face and track clearance,
        since their GJK normal need not point out of the road surface.
        """
        if (point[3] not in self.track_links.get(entity,())
                or point[2] not in self.map_bodies or point[2] in self.breakable_bodies):
            return False
        local = matrix(pose.yaw,pose.pitch,pose.roll).T @ (np.asarray(point[5])-pose.position)
        profile = self._support_layout(entity).track_profile(float(local[0]),float(local[2]),self.SKIN)
        if profile is None:
            return False
        floor = (matrix(pose.yaw,pose.pitch,pose.roll) @
                 np.array((local[0],profile,local[2]))+pose.position)[1]
        incline = self._track_incline(entity,pose,point)
        if incline is not None and incline[2] <= self.SUSPENSION_TRAVEL+self.SKIN:
            return True
        for body,x,z,height in self._contact_support_cache.get(entity,()):
            if (body == point[2] and math.hypot(x-point[5][0],z-point[5][2]) <= self.STEP*2+self.SKIN
                    and -self.SKIN <= height-floor <= self.SUSPENSION_TRAVEL+self.SKIN
                    and point[5][1] <= height+self.SKIN):
                return True
        return False

    def _can_batch_incline_rays(self):
        return (getattr(self._static_ray, '__func__', None) is _RECOVERY_SCALAR_RAY
                and getattr(self._static_rays, '__func__', None) is _GROUND_RAYS_IMPL
                and bullet.rayTest is _RECOVERY_RAY_TEST
                and bullet.rayTestBatch is _GROUND_RAY_BATCH_SOURCE)

    def _batch_track_contacts(self, pose, entity, points):
        # The pose, static world and support cache remain fixed throughout this call.
        tracks = self.track_links.get(entity,())
        supports = self._contact_support_cache.get(entity,())
        rotation = matrix(pose.yaw,pose.pitch,pose.roll)
        layout = self._support_layout(entity)
        ordinary = []
        prepared = {}
        for index, point in enumerate(points):
            if not point[8] < self.SKIN:
                continue
            body = point[2]
            map_body = body in self.map_bodies
            if map_body and point[7][1] > .65 and point[8] > -.025:
                continue
            ordinary.append(index)
            if point[3] not in tracks or not map_body or body in self.breakable_bodies:
                continue
            if point[7][1] > .65 and point[8] > -self.SUSPENSION_TRAVEL-self.SKIN:
                ordinary.pop()
                continue
            local = rotation.T @ (np.asarray(point[5])-pose.position)
            profile = layout.track_profile(float(local[0]),float(local[2]),self.SKIN)
            if profile is not None:
                prepared[index] = (local, profile)
        if len(prepared) < 8 or not self.experimental_6dof or self.wall_trampoline:
            return [points[index] for index in ordinary if index not in prepared
                    or not self._compressed_track_contact(entity,pose,points[index])]
        pending = list(prepared)
        surfaces = {}
        hx,hz = math.sin(pose.yaw)*self.SKIN,math.cos(pose.yaw)*self.SKIN
        limit = math.cos(self.assets_for(entity).max_slope)
        for ox,oz in ((0.,0.),(-hx,-hz),(hx,hz)):
            if not pending:
                break
            starts,ends = [],[]
            for index in pending:
                px,py,pz = points[index][6]
                starts.append((px+ox,py+2*self.STEP,pz+oz))
                ends.append((px+ox,py-2*self.STEP,pz+oz))
            rejected = []
            for index, hit in zip(pending, self._static_rays(starts, ends), strict=True):
                normal = np.asarray(hit[4])
                ny = normal[1]
                if (hit[0] not in self.map_bodies or hit[0] in self.breakable_bodies
                        or hit[2] <= 0. or ny < limit or ny >= .9999
                        or abs(np.dot(np.asarray(points[index][6])-hit[3],normal)) > self.SKIN):
                    rejected.append(index)
                else:
                    surfaces[index] = (hit,normal)
            pending = rejected
        result = []
        reach = self.SUSPENSION_TRAVEL+self.SKIN
        for index in ordinary:
            point = points[index]
            if index not in prepared:
                result.append(point)
                continue
            local,profile = prepared[index]
            floor = None
            if index in surfaces:
                hit,normal = surfaces[index]
                floor = rotation @ np.array((local[0],profile,local[2]))+pose.position
                if float(np.dot(np.asarray(hit[3])-floor,normal)) <= reach:
                    continue
            floor_height = None if floor is None else floor[1]
            for body,x,z,height in supports:
                if (body != point[2]
                        or not math.hypot(x-point[5][0],z-point[5][2]) <= self.STEP*2+self.SKIN):
                    continue
                if floor_height is None:
                    floor_height = (rotation @ np.array((local[0],profile,local[2]))+pose.position)[1]
                if (-self.SKIN <= height-floor_height <= reach
                        and point[5][1] <= height+self.SKIN):
                    break
            else:
                result.append(point)
        return result

    def _recovery_track_contact(self, rotations, entity, pose, point):
        if (point[3] not in self.track_links.get(entity,())
                or point[2] not in self.map_bodies or point[2] in self.breakable_bodies):
            return False
        key = _RECOVERY_ROTATION_KEY(pose.yaw, pose.pitch, pose.roll)
        rotation = rotations.get(key)
        if rotation is None:
            rotation = rotations[key] = matrix(pose.yaw,pose.pitch,pose.roll)
        local = rotation.T @ (np.asarray(point[5])-pose.position)
        profile = self._support_layout(entity).track_profile(float(local[0]),float(local[2]),self.SKIN)
        if profile is None:
            return False
        incline = self._track_incline(entity,pose,point,prepared=(rotation,local,profile))
        if incline is not None and incline[2] <= self.SUSPENSION_TRAVEL+self.SKIN:
            return True
        floor = None if incline is None else incline[1][1]
        for body,x,z,height in self._contact_support_cache.get(entity,()):
            if (body != point[2]
                    or not math.hypot(x-point[5][0],z-point[5][2]) <= self.STEP*2+self.SKIN):
                continue
            if floor is None:
                floor = (rotation @ np.array((local[0],profile,local[2]))+pose.position)[1]
            if (-self.SKIN <= height-floor <= self.SUSPENSION_TRAVEL+self.SKIN
                    and point[5][1] <= height+self.SKIN):
                return True
        return False

    def _track_incline(self, entity, pose, point, *, crest=False, prepared=None):
        """Measure a track's clearance to the actual inclined mesh face.

        Concave triangle queries can return an internal edge normal rather
        than the road normal. A vertical ray identifies the face, but only
        when the reported map contact lies on that same plane. A wall below
        a ramp or a riser therefore cannot borrow the ramp's support.
        ``crest`` also measures a horizontal continuation for the bump stop;
        ordinary edge compliance retains the existing flat-rock rules.
        The static map is stored as 32 m Bullet partitions, so a deck split
        by a cell seam reports its contact on one body and the coplanar face
        on the next; any unbreakable map body qualifies, the plane decides.
        A contact exactly on a crest edge can return the riser beside the
        deck; rays one skin either way along the heading resolve the edge.
        """
        if (not self.experimental_6dof or self.wall_trampoline
                or point[3] not in self.track_links.get(entity,())
                or point[2] not in self.map_bodies or point[2] in self.breakable_bodies):
            return None
        px,py,pz = point[6]
        hx,hz = math.sin(pose.yaw)*self.SKIN,math.cos(pose.yaw)*self.SKIN
        for ox,oz in ((0.,0.),(-hx,-hz),(hx,hz)):
            face = self._static_ray((px+ox,py+2*self.STEP,pz+oz),
                                    (px+ox,py-2*self.STEP,pz+oz))
            normal = np.asarray(face[4])
            ny = normal[1]
            if not (face[0] not in self.map_bodies or face[0] in self.breakable_bodies
                    or face[2] <= 0.
                    or ny < math.cos(self.assets_for(entity).max_slope)
                    or (ny >= .9999 and not crest)
                    or abs(np.dot(np.asarray(point[6])-face[3],normal)) > self.SKIN):
                break
        else:
            return None
        if prepared is None:
            rotation = matrix(pose.yaw,pose.pitch,pose.roll)
            local = rotation.T @ (np.asarray(point[5])-pose.position)
            profile = self._support_layout(entity).track_profile(float(local[0]),float(local[2]),self.SKIN)
            if profile is None:
                return None
        else:
            rotation, local, profile = prepared
        floor = rotation @ np.array((local[0],profile,local[2]))+pose.position
        depth = float(np.dot(np.asarray(face[3])-floor,normal))
        return normal,floor,depth

    def _separating_map_contacts(self, points, dx, dz):
        return bool(points) and all(
            point[2] in self.map_bodies and point[2] not in self.breakable_bodies
            and dx*point[7][0]+dz*point[7][2] > 1e-8 for point in points)

    def _retreat_from_map_contact(self, entity, previous, points, dx, dz):
        """Preserve attitude for a short retreat while the ground still supports the hull."""
        if (not self.experimental_6dof or self.wall_trampoline
                or not self._separating_map_contacts(points,dx,dz)):
            return None
        x,y,z = previous.position
        held = Pose((x+dx,y,z+dz),previous.yaw,previous.pitch,previous.roll)
        if not self._valid_retreat(held,previous,entity)[0]:
            return None
        layout = self._support_layout(entity)
        stations = matrix(held.yaw,held.pitch,held.roll) @ np.vstack((layout.a,layout.p,layout.b))
        ground = self._ground_heights(x+dx+stations[0],z+dz+stations[2],held,entity)
        active = np.flatnonzero((layout.weight > 0.) &
                               (ground >= y+stations[1]-self.SUPPORT_REACH))
        if (len(active) and layout.a[active].min() <= .05 and layout.a[active].max() >= -.05
                and layout.b[active].min() <= .05 and layout.b[active].max() >= -.05):
            return held
        return None

    def _valid_retreat(self, candidate, previous, entity):
        clear, points = self._valid(candidate,previous,entity=entity,climb=False,suspension=True)
        dx = candidate.position[0]-previous.position[0]
        dz = candidate.position[2]-previous.position[2]
        if not self._separating_map_contacts(points,dx,dz):
            return clear, points
        # Back up through an existing overlap one slice at a time.
        # Each remaining contact must be shallower on the same face.
        prior = self._contacts(previous,entity,suspension=True)
        self.set_pose(self.bodies[entity],candidate)
        if all(any(old[2:4] == point[2:4] and old[8] < point[8]-1e-6
                   and sum(a*b for a,b in zip(old[7],point[7])) >= .95
                   and abs(sum(n*(a-b) for n,a,b in zip(old[7],point[6],old[6]))) <= self.SKIN
                   for old in prior) for point in points):
            if self.trace_dynamic_contacts:
                self._last_validation_rejection = None
            return True, []
        return False, points

    def sandbox_terrain_bounds(self):
        terrain = self.terrain
        # The terrain mesh ends at its last sample, one cell before MAX_X/Z.
        return (terrain.MIN, terrain.MIN_Z, terrain.MAX_X - terrain.WIDTH / terrain.size,
                terrain.MAX_Z - terrain.DEPTH / terrain.size)

    def movement_bounds(self, entity):
        if entity in self.open_map_borders:
            return self.sandbox_terrain_bounds()
        return self.assets.bounds

    def _inside(self, pose, entity=None, *, bounds=None):
        assets = self.assets_for(entity)
        lo_x,lo_z,hi_x,hi_z = bounds if bounds is not None else self.movement_bounds(
            self.primary_id if entity is None else entity)
        # Use the physical chassis vertices, not the entity's center, at the map edge.
        cached = self._chassis_bounds.get(id(assets))
        if cached is None or cached[0] is not assets:
            bounds = tuple((verts.min(axis=0), verts.max(axis=0))
                           for name, verts in assets.tank_shapes
                           if name != assets.turret_node)
            radius = max(math.hypot(max(abs(low[0]),abs(high[0])),
                                    max(abs(low[2]),abs(high[2]))) for low,high in bounds)+1e-7
            cached = self._chassis_bounds[id(assets)] = (assets, bounds, radius)
        x,_,z = pose.position
        circle = self.movement_circle
        if circle is not None and (self.primary_id if entity is None else entity) == circle[0]:
            _, cx, cz, radius = circle
            if math.hypot(x-cx, z-cz) > radius + 1e-9:
                return False
        margin = self.SKIN+cached[2]
        if (all(math.isfinite(value) for value in (*pose.position,pose.yaw,pose.pitch,pose.roll))
                and lo_x+margin <= x <= hi_x-margin and lo_z+margin <= z <= hi_z-margin):
            # Rotation preserves this enclosing radius. In the interior all
            # twelve original corners are inside, regardless of attitude.
            return True
        rot = matrix(pose.yaw,pose.pitch,pose.roll)
        for minimum, maximum in cached[1]:
            for x in (minimum[0],maximum[0]):
                for z in (minimum[2],maximum[2]):
                    v = rot @ np.array((x,0,z))+pose.position
                    if not (lo_x+self.SKIN <= v[0] <= hi_x-self.SKIN and
                            lo_z+self.SKIN <= v[2] <= hi_z-self.SKIN):
                        return False
        return True

    def _clip_circle(self, entity, pose, velocity=(0., 0., 0.)):
        circle = self.movement_circle
        if circle is None or entity != circle[0]:
            return pose, velocity
        _, cx, cz, radius = circle
        x, y, z = pose.position
        distance = math.hypot(x-cx, z-cz)
        if distance <= radius:
            return pose, velocity
        nx, nz = (x-cx)/distance, (z-cz)/distance
        vx, vy, vz = velocity
        outward = max(0., vx*nx+vz*nz)
        return (Pose((cx+nx*radius, y, cz+nz*radius), pose.yaw, pose.pitch, pose.roll),
                (vx-outward*nx, vy, vz-outward*nz))

    def _border_pose(self, pose, entity, *, flight_bounds=None, rotation=None):
        """Project the hull onto the playable rectangle, independently on both axes."""
        x, y, z = pose.position
        low_x, low_z, high_x, high_z = self.movement_bounds(entity)
        if flight_bounds is None:
            if self._inside(pose, entity):
                return pose
            _, chassis, _ = self._chassis_bounds[id(self.assets_for(entity))]
            bounds = tuple(((lo[0], 0., lo[2]), (hi[0], 0., hi[2])) for lo, hi in chassis)
        else:
            lo, hi = flight_bounds
            radius = sum(max(abs(a), abs(b)) for a, b in zip(lo, hi)) + self.SKIN
            if low_x + radius <= x <= high_x - radius and low_z + radius <= z <= high_z - radius:
                return pose
            bounds = (flight_bounds,)
        if rotation is None:
            rotation = matrix(pose.yaw, pose.pitch, pose.roll)

        def limits(axis, low, high):
            row = rotation[axis]
            edges = [(np.minimum(row * lo, row * hi).sum(),
                      np.maximum(row * lo, row * hi).sum()) for lo, hi in bounds]
            margin = self.SKIN + .001
            return (low + margin - min(edge[0] for edge in edges),
                    high - margin - max(edge[1] for edge in edges))

        left, right = limits(0, low_x, high_x)
        near, far = limits(2, low_z, high_z)
        return Pose((min(right, max(left, x)), y, min(far, max(near, z))),
                    pose.yaw, pose.pitch, pose.roll)

    def _border_drive(self, entity, distance, yaw_delta, displacement=None):
        if displacement is None and not distance:
            return None
        pose = self.poses[entity]
        x, y, z = pose.position
        yaw = pose.yaw + yaw_delta
        dx, dz = ((math.sin(yaw) * distance, math.cos(yaw) * distance)
                  if displacement is None else displacement)
        candidate = Pose((x + dx, y, z + dz), yaw, pose.pitch, pose.roll)
        limited, _ = self._clip_circle(entity, candidate)
        if limited is not candidate:
            dx, dz = limited.position[0]-x, limited.position[2]-z
            displacement = dx, dz
            candidate = limited
        clipped = self._border_pose(candidate, entity)
        if clipped is candidate:
            return displacement
        # Rotation can grow the footprint at rest. A border must not pull the
        # tank backwards when it was trying to drive into the wall.
        bx, _, bz = clipped.position
        return (min(max(0., dx), max(min(0., dx), bx - x)),
                min(max(0., dz), max(min(0., dz), bz - z)))

    def destroy(self, key, direction, damage=None):
        if key in self.destroyed or key not in self.object_health:
            return False
        self.object_health[key] -= self.object_health[key] if damage is None else damage
        if self.object_health[key] > 0:
            return False
        obj = self.assets.destructibles[key]
        dx,dz = direction
        yaw = math.atan2(dx,dz)
        center = (obj.vertices.min(axis=0)+obj.vertices.max(axis=0))/2
        length = max(1.,float(np.ptp(obj.vertices[:,1])))
        x,z = center[0],center[2]
        slope = math.atan2(self.terrain.height(x+math.sin(yaw)*length,z+math.cos(yaw)*length)
                          - self.terrain.height(x,z),length)
        angle = math.pi/2-slope
        for body in self.object_bodies[key]:
            self.map_bodies.remove(body)
            del self.breakable_bodies[body]
            if self.collision_zone:
                # The dedicated range can restore this mesh after fresh scene loads.
                bullet.setCollisionFilterGroupMask(body, -1, 0, 0, physicsClientId=self.cid)
                bullet.resetBasePositionAndOrientation(body, (0., -8192., 0.), (0., 0., 0., 1.),
                                                       physicsClientId=self.cid)
            else:
                bullet.removeBody(body,physicsClientId=self.cid)
        self.destroyed.add(key)
        if self._native_physics is not None:
            self._native_physics.query(operation='destroy', key=key)
        self.destruction_events.append((obj,yaw,angle))
        return True

    def restore_range_sector(self, contains):
        """Restore the range's parked Bullet props while every participant reloads."""
        if not self.collision_zone:
            return ()
        keys = []
        for key, obj in self.assets.destructibles.items():
            if self.object_health[key] >= obj.health:
                continue
            low, high = obj.vertices.min(axis=0), obj.vertices.max(axis=0)
            if not contains((low + high) * .5):
                continue
            # Even a parked participant or a fallen wreck may occupy the gap.
            # Query all sensor links and dynamic hulls with a clearance margin.
            bodies = self.body_entities.keys() | {d.body for d in self.dynamic_bodies.values()}
            near = bullet.getOverlappingObjects((low - .5).tolist(), (high + .5).tolist(),
                                                 physicsClientId=self.cid) or ()
            if any(body in bodies for body, _ in near):
                return ()
            keys.append(key)
        for key in keys:
            self.object_health[key] = self.assets.destructibles[key].health
            if key not in self.destroyed:
                continue
            for body in self.object_bodies[key]:
                bullet.resetBasePositionAndOrientation(body, (0., 0., 0.), (0., 0., 0., 1.),
                                                       physicsClientId=self.cid)
                bullet.setCollisionFilterGroupMask(body, -1, _STATIC_GROUP,
                    _RAY_GROUP, physicsClientId=self.cid)
                self.map_bodies.add(body)
                self.breakable_bodies[body] = key
            if self._native_physics is not None:
                self._native_physics.query(operation='restore_prop', key=key)
            self.destroyed.remove(key)
        if keys:
            self.destruction_events[:] = [event for event in self.destruction_events if event[0].key not in keys]
        return tuple(keys)

    def _valid(self, candidate, previous, impact=None, entity=None, *, climb=True,
               suspension=False, rejection_only=False, measured_contacts=None, pair_queries=None):
        if not self._inside(candidate,entity):
            if self.trace_dynamic_contacts:
                self._last_validation_rejection = 'outside_map_bounds'
            return False, []
        rise = candidate.position[1]-previous.position[1]
        if climb and rise > .0001:
            horizontal = math.hypot(candidate.position[0]-previous.position[0],
                                    candidate.position[2]-previous.position[2])
            assets = self.assets_for(entity)
            # The fitted hull centre can rise when a leading support point
            # first touches a shallow terrain feature. Dividing that rise by
            # the 6 cm integration slice makes even a passable mound look
            # steeper than maxClimbAngle. Use the shorter of the physical
            # support span and one heightfield cell to reject only abrupt
            # steps; sustained steep grades are handled by the drivetrain's
            # gravity/traction balance instead of freezing the hull here.
            support_span = min(assets.support)
            terrain_cell = (getattr(self.terrain, 'WIDTH', None)
                            and getattr(self.terrain, 'size', None))
            if terrain_cell:
                support_span = min(support_span,
                                   self.terrain.WIDTH/self.terrain.size)
            rise_span = max(horizontal, support_span*.5)
            if rise > math.tan(assets.max_slope)*rise_span:
                if self.trace_dynamic_contacts:
                    self._last_validation_rejection = 'support_step_height'
                return False, []
        if rejection_only:
            if pair_queries is None:
                return not self._pivot_overlaps(candidate, previous, entity), []
            return not self._pivot_overlaps(candidate, previous, entity, pair_queries=pair_queries), []
        if measured_contacts is None:
            points = self._contacts(candidate,entity,suspension=suspension)
        else:
            # The landing probe restored the previous sensor pose. Validation
            # still leaves the sensor at this candidate, as a full query would.
            self.set_pose(self.bodies[entity], candidate)
            points = measured_contacts
        if points:
            dx = candidate.position[0]-previous.position[0]
            dz = candidate.position[2]-previous.position[2]
            distance = math.hypot(dx,dz)
            overlap_tolerance = self.SKIN*3
            approach_tolerance = max(1e-6,distance*.1)
            # Collision meshes are triangle surfaces, so the swept body can
            # remain inside Bullet's query skin while travelling beside
            # a wall. Treat up to 3x the query skin as a seam only when the
            # motion is nearly tangent. A real approach or deeper overlap
            # still blocks movement.
            points = [point for point in points
                if not (point[8] >= -overlap_tolerance
                    and dx*point[7][0]+dz*point[7][2] >= -approach_tolerance
                    and math.hypot(point[7][0],point[7][2]) > .1)
                # When the rear of a compressed track leaves a low rock, its
                # convex sensor can still overlap the rock's far face. A
                # separating move must be able to finish clearing it; an
                # approaching move into the same face remains blocked.
                and not (suspension and point[3] in self.track_links.get(entity,())
                    and point[2] in self.map_bodies
                    and point[2] not in self.breakable_bodies
                    and point[8] >= -self.SUSPENSION_TRAVEL-self.SKIN
                    and dx*point[7][0]+dz*point[7][2] > 1e-8)]
        if impact is not None:
            keys = {self.breakable_bodies[p[2]] for p in points if p[2] in self.breakable_bodies}
            for key in keys:
                self.destroy(key,impact)
            if keys:
                points = self._contacts(candidate,entity,suspension=suspension)
        if self.trace_dynamic_contacts:
            self._last_validation_rejection = 'geometry_overlap' if points else None
        return not points, points

    def move(self, distance, yaw_delta, entity=None, *, dt=None,
             planar_displacement=None, planar_velocity=None,
             track_yaw_rate=None):
        """Advance one vehicle by a drive increment.

        Without dt this is the stateless ground solver. With dt a grounded
        hull uses the same solver, but separates where the ground falls away
        faster than gravity allows; an airborne hull ignores distance and
        yaw_delta, follows its stored velocity and lands back on the solver.
        The optional planar displacement is used by the opt-in skid state;
        legacy callers keep the scalar distance API.
        """
        entity = self.primary_id if entity is None else entity
        self.last_contact = None
        self.last_wall_recoil = None
        self.last_reject_reason = None
        if planar_displacement is not None:
            if (len(planar_displacement) != 2
                    or not all(math.isfinite(value) for value in planar_displacement)):
                raise ValueError('Invalid planar displacement')
            planar_displacement = tuple(float(value) for value in planar_displacement)
        if planar_velocity is not None:
            if (len(planar_velocity) != 2
                    or not all(math.isfinite(value) for value in planar_velocity)):
                raise ValueError('Invalid planar velocity')
            planar_velocity = tuple(float(value) for value in planar_velocity)
        if track_yaw_rate is not None and not math.isfinite(track_yaw_rate):
            raise ValueError('Invalid track yaw rate')
        if dt is None:
            if planar_displacement is not None or planar_velocity is not None:
                raise ValueError('Planar motion requires a fixed-step interval')
            pose,moved,_ = self._drive(entity, distance, yaw_delta,
                                      planar_displacement=self._border_drive(entity, distance, yaw_delta))
            state = self.motion.setdefault(entity,Motion())
            state.velocity,state.grounded = (0.,0.,0.),True
        else:
            if not (math.isfinite(dt) and dt > 0):
                raise ValueError('Invalid motion interval')
            state = self._motion_state(entity)
            start = self.poses[entity]
            incoming = state.velocity
            pitching = state.suspension_velocity[1]
            if state.grounded and entity not in self.dynamic_bodies:
                clipped = self._border_drive(entity, distance, yaw_delta, planar_displacement)
                if clipped != planar_displacement:
                    planar_velocity = tuple(axis / dt for axis in clipped)
                    planar_displacement = clipped
            if (self.experimental_ground_impacts and state.grounded
                    and entity not in self.dynamic_bodies):
                if planar_velocity is None:
                    if planar_displacement is not None:
                        planar_velocity = tuple(value/dt for value in planar_displacement)
                    else:
                        yaw = self.poses[entity].yaw+yaw_delta
                        speed = distance/dt
                        planar_velocity = (math.sin(yaw)*speed,math.cos(yaw)*speed)
                self.ground_commands[entity] = (
                    planar_velocity, float(yaw_delta/dt))
                self.ground_track_rates[entity] = (
                    float(yaw_delta/dt if track_yaw_rate is None else track_yaw_rate))
            if entity in self.dynamic_bodies:
                # ``dynamic_commit`` owns the one Bullet integration for the
                # entire arena tick.  Inputs during this phase intentionally
                # add no track force; Phase A covers ballistic drops only.
                pose,moved = self.poses[entity],False
            else:
                moved,rest = False,dt
                if state.grounded:
                    if self._native_physics is not None:
                        native = self._native_physics.move(self,entity,distance,yaw_delta,dt,state,
                                                          planar_displacement)
                    elif self.movement_circle or entity in self.open_map_borders or self.collision_zone:
                        native = None
                    else:
                        if self._rust_drive is None:
                            from .sim_native import RustDrive
                            self._rust_drive = RustDrive(self)
                        native = self._rust_drive.try_drive(entity,distance,yaw_delta,dt,state,
                                                            planar_displacement)
                    if native is None:
                        pose,moved,rest = self._drive(entity,distance,yaw_delta,dt,state,
                                                       planar_displacement)
                    else:
                        pose,moved,rest = native
                    if (state.grounded and self.experimental_6dof
                            and not self.wall_trampoline and self._has_suspension(entity)):
                        missing_yaw = math.remainder(start.yaw+yaw_delta-pose.yaw,math.tau)
                        requested_yaw = math.copysign(min(abs(yaw_delta),abs(missing_yaw)),yaw_delta)
                        travel = (abs(distance) if planar_displacement is None
                                  else math.hypot(*planar_displacement))
                        if (travel <= 1.5*dt and abs(requested_yaw) > 1e-6
                                and missing_yaw*yaw_delta > 0.):
                            pose, pivoted = self._pivot_at_map_contact(entity,pose,requested_yaw)
                            moved |= pivoted
                        if self.last_contact == 'map' or entity in self._wall_glances:
                            pose, slipped = self._map_contact_response(
                                entity, start, pose, distance, yaw_delta, dt, state,
                                incoming, pitching, planar_displacement)
                            moved |= slipped
                if not state.grounded:
                    self._wall_glances.pop(entity,None)
                if not state.grounded and rest > 1e-9 and entity not in self.dynamic_bodies:
                    moved |= self._fly(entity,rest,state)
                pose = self.poses[entity]
        self._synced[entity] = (pose,state)
        return pose,moved

    def _pivot_overlaps(self, candidate, previous, entity, *, pair_queries=None):
        # A pivot uses only the validity decision and never destroys an item.
        dx = candidate.position[0] - previous.position[0]
        dz = candidate.position[2] - previous.position[2]
        approach_tolerance = max(1e-6, math.hypot(dx, dz) * .1)
        overlap_tolerance = self.SKIN * 3
        track_links = self.track_links.get(entity, ())
        player = self.bodies[entity]

        def blocked(points):
            for point in points:
                separation = point[8]
                if not separation < self.SKIN:
                    continue
                body = point[2]
                is_map = body in self.map_bodies
                nx, ny, nz = point[7]
                if is_map and ny > .65 and separation > -.025:
                    continue
                track = is_map and point[3] in track_links and body not in self.breakable_bodies
                if track and (ny > .65 and separation > -self.SUSPENSION_TRAVEL-self.SKIN):
                    continue
                approach = dx*nx + dz*nz
                if (separation >= -overlap_tolerance and approach >= -approach_tolerance
                        and math.hypot(nx, nz) > .1):
                    continue
                if track and separation >= -self.SUSPENSION_TRAVEL-self.SKIN and approach > 1e-8:
                    continue
                if track and self._compressed_track_contact(entity, candidate, point):
                    continue
                return True
            return False

        self.set_pose(player, candidate)
        if pair_queries is not None:
            pair_queries.pose = _PIVOT_POSE_BYTES(*candidate.position,candidate.yaw,candidate.pitch,
                                                 candidate.roll,pair_queries.turret)
        return _contact_queries.gather(bullet if pair_queries is None else pair_queries,
                                       self.cid, player, self.SKIN,
                                       self.map_bodies, self.body_entities, None, entity, blocked)

    def _pivot_at_map_contact(self, entity, pose, turn):
        """A blocked low-speed turn can rotate about the corner touching the map."""
        origin = pose
        steps = math.ceil(abs(turn)/math.radians(.75))
        step = turn/steps
        c,s = math.cos(step),math.sin(step)
        road = self._road_poses[entity]
        validation = {}
        query_option = {}
        if (getattr(_contact_queries, 'API_VERSION', None) == 2
                and getattr(self, '_contact_query_observer', None) is None
                and getattr(self, 'collision_zone', None) is None
                and not self.trace_dynamic_contacts
                and getattr(self._valid, '__func__', None) is _VALID_IMPL
                and getattr(self._contacts, '__func__', None) is _CONTACTS_IMPL):
            validation = {'rejection_only': True}
            if all(getattr(bullet, name) is original for name, original in _PIVOT_BULLET_APIS):
                caches = getattr(self, '_pivot_map_pairs', None)
                if caches is None:
                    caches = self._pivot_map_pairs = {}
                body, assets = self.bodies[entity], self.assets_for(entity)
                cache = caches.get(entity)
                if cache is None or cache.body != body or cache.assets is not assets:
                    cache = caches[entity] = _PivotMapPairQueries(self, body, assets)
                cache.turret = self.gun_angles[entity][0]
                query_option = {'pair_queries': cache}
                validation.update(query_option)
        for _ in range(steps):
            x,y,z = pose.position
            yaw = math.remainder(pose.yaw+step,math.tau)
            rotated = Pose(pose.position,yaw,pose.pitch,pose.roll)
            points = self._contacts(rotated,entity,suspension=True, **query_option)
            if not points or any(p[2] not in self.map_bodies or p[2] in self.breakable_bodies
                                 for p in points):
                break
            # Try the closest corners first. Rotating the centre around the
            # vehicle-side contact keeps that part of the hull at its fulcrum.
            for point in sorted(points,key=lambda p:p[8])[:4]:
                ax,_,az = point[5]
                dx,dz = ax+c*(x-ax)+s*(z-az)-x,az-s*(x-ax)+c*(z-az)-z
                nx,_,nz = point[7]
                if math.hypot(nx,nz) < .25 or dx*nx+dz*nz < -self.SKIN:
                    continue
                for clearance in (0.,self.SKIN,2*self.SKIN):
                    sx,sz = dx+clearance*nx,dz+clearance*nz
                    _,rise,_ = self._road_velocity(road,sx,sz)
                    candidate = Pose((x+sx,y+rise,z+sz),yaw,pose.pitch,pose.roll)
                    if (self._clip_circle(entity,candidate)[0] is candidate
                            and self._valid(candidate,pose,entity=entity,suspension=True,
                                            **validation)[0]):
                        pose = candidate
                        break
                if pose is candidate:
                    break
            else:
                break
        self.set_pose(self.bodies[entity],pose)
        self.poses[entity] = pose
        return pose,pose is not origin

    def _map_contact_response(self, entity, start, pose, distance, yaw_delta, dt, state,
                              incoming, pitching, displacement):
        """Recover the surface limiting the ground solver's last move."""
        yaw = start.yaw + yaw_delta
        dx,dz = displacement if displacement is not None else (
            math.sin(yaw)*distance,math.cos(yaw)*distance)
        travel = math.hypot(dx,dz)
        guide = self._wall_glances.pop(entity,None)
        if travel < 1e-6:
            return pose,False
        fx,fz = dx/travel,dz/travel
        x,y,z = pose.position
        native = self._native_physics
        if native is not None:
            # A wall can briefly clear the nose as the hull turns. Keep the
            # same measured face in range across that gap and mesh seams.
            skin = max(.45 if guide else 0.,min(.35,travel)+self.SKIN)
            points = native.query(operation='contacts',entity=entity,skin=skin)
        else:
            sx,sz = (fx*travel,fz*travel) if guide is None else (-.16*guide[0],-.16*guide[2])
            probe = Pose((x+sx,y,z+sz),pose.yaw,pose.pitch,pose.roll)
            points = self._contacts(probe,entity,suspension=True)
            self.set_pose(self.bodies[entity],pose)
        if not points or any(p[2] not in self.map_bodies or p[2] in self.breakable_bodies
                             for p in points):
            return pose,False
        walls = []
        for p in sorted(points,key=lambda point:point[8])[:8]:
            px,py,pz = p[6]
            normal = p[7]
            # At the foot of a boulder the closest point can be on its bottom
            # edge. Probe just beyond that edge, close enough to use the same face.
            for offset in (0.,2*self.SKIN):
                rx,rz = px+fx*offset,pz+fz*offset
                face = self._static_ray((rx,py+.12,rz),(rx,py-.12,rz))
                if (face[0] in self.map_bodies and face[0] not in self.breakable_bodies
                        and .18 < face[4][1] < .78
                        and abs(sum((a-b)*n for a,b,n in zip(p[6],face[3],face[4]))) <= self.SKIN*3):
                    normal = face[4]
                    break
            nx,ny,nz = normal
            close = -(fx*nx+fz*nz)
            if (guide is not None and abs(ny) <= .18 and close >= -.08
                    and nx*guide[0]+nz*guide[2] >= .95):
                return self._glance_along_wall(entity,start,pose,state,guide[:3],dx,dz,
                                               dt,distance,guide[3],yaw_delta)
            if self.last_contact == 'map' and .18 < ny < .78 and close >= .35:
                speed = min(travel/dt,incoming[0]*fx+incoming[2]*fz)
                if pitching > -.4 and speed*close >= self.ROCK_IMPACT_MIN_SPEED:
                    pose = self._pitch_against_rock(entity,pose,state,p[6],speed*close,ny)
                return pose,False
            if self.last_contact == 'map' and abs(ny) <= .18 and .02 <= close <= .65:
                walls.append(normal)
        if walls:
            return self._glance_along_wall(entity,start,pose,state,walls[0],dx,dz,
                                           dt,distance,0.,yaw_delta)
        return pose,False

    def _glance_along_wall(self, entity, start, pose, state, normal, dx, dz, dt, distance,
                          previous_rate, requested_yaw):
        nx,_,nz = normal
        length = math.hypot(nx,nz)
        nx,nz = nx/length,nz/length
        tx,tz = -nz,nx
        along = dx*tx+dz*tz
        if along < 0.:
            tx,tz,along = -tx,-tz,-along
        if along < 1e-6:
            return pose,False
        target = math.atan2(tx,tz) + (math.pi if distance < 0. else 0.)
        angle = math.remainder(target-pose.yaw,math.tau)
        if abs(angle) > math.pi/3 or requested_yaw*angle < -1e-5:
            return pose,False
        target_rate = min(self.WALL_GLANCE_YAW_RATE,abs(angle)*24.)
        rate = target_rate+(abs(previous_rate)-target_rate)*math.exp(-28.*dt)
        turn = math.copysign(min(abs(angle),rate*dt),angle)
        covered = max(0.,(pose.position[0]-start.position[0])*tx
                        +(pose.position[2]-start.position[2])*tz)
        blocked = self.last_contact == 'map'
        remaining = max(0.,along-covered) if blocked else 0.
        steps = max(1,math.ceil(remaining/self.STEP),math.ceil(abs(turn)/math.radians(.75)))
        road = self._road_poses[entity]
        resolved = pose
        for _ in range(steps):
            x,y,z = pose.position
            sx,sz = tx*remaining/steps,tz*remaining/steps
            _,rise,_ = self._road_velocity(road,sx,sz)
            yaw = math.remainder(pose.yaw+turn/steps,math.tau)
            candidate = Pose((x+sx,y+rise,z+sz),yaw,pose.pitch,pose.roll)
            if (self._clip_circle(entity,candidate)[0] is not candidate
                    or not self._valid(candidate,pose,entity=entity,suspension=True)[0]):
                break
            pose = candidate
        self.set_pose(self.bodies[entity],pose)
        self._wall_glances[entity] = (nx,0.,nz,
            math.remainder(pose.yaw-resolved.yaw,math.tau)/dt)
        if pose is resolved:
            return pose,False
        self.poses[entity] = pose
        travelled = math.hypot(pose.position[0]-resolved.position[0],pose.position[2]-resolved.position[2])
        if blocked and covered+travelled:
            speed = min(along,covered+travelled)/dt
            state.velocity = self._road_velocity(road,tx*speed,tz*speed)
        return pose, bool(travelled)

    def _pitch_against_rock(self, entity, pose, state, point, closing, ny):
        x,y,z = pose.position
        px,py,pz = point
        nose = (px-x)*math.sin(pose.yaw)+(pz-z)*math.cos(pose.yaw)
        if (nose < self.assets_for(entity).support[1]*.55
                or py < y-self.SKIN or py-y > self.assets_for(entity).support[1]):
            return pose
        # A forward-facing inclined contact pushes up at the nose. Validate
        # the first part of the pitch with the rear support kept in place.
        lift = math.radians(min(3.5,closing*ny*.8))
        tail = self._support_layout(entity).patch
        for factor in (1.,.5,.25):
            pitch = pose.pitch-lift*factor
            rise = tail*(math.sin(pose.pitch)-math.sin(pitch))
            candidate = Pose((x,y+rise,z),pose.yaw,pitch,pose.roll)
            if self._valid(candidate,pose,entity=entity,suspension=True)[0]:
                hy,pr,rr = state.suspension_velocity
                state.suspension_velocity = (hy,min(pr,-lift*14.),rr)
                self.poses[entity] = pose = candidate
                break
        self.set_pose(self.bodies[entity],pose)
        return pose

    def begin_ground_step(self):
        """Reset per-tick inputs for the opt-in planar contact solver."""
        if self.experimental_ground_impacts:
            self.ground_commands.clear()
            self.ground_track_rates.clear()
            self.ground_contacts.clear()
            self.last_ground_impacts = ()
            self.air_ram_impacts.clear()
            self._air_tank_contacts.clear()
            self._sensor_aabb_cache = {}

    def _record_ground_contacts(self, entity, points, candidate):
        if (not self.experimental_ground_impacts or
                not self.motion.get(entity,Motion()).grounded):
            return
        grouped = {}
        for point in points:
            other = self.body_entities.get(point[2])
            if (other is None or other == entity
                    or not self.motion.get(other,Motion()).grounded
                    or other in self.dynamic_bodies or other in self.pending_dynamics):
                continue
            grouped.setdefault(other,[]).append(point)
        for other, contacts in grouped.items():
            if entity < other:
                a,b = entity,other
                reverse = False
                pose_a,pose_b = candidate,self.poses[other]
            else:
                a,b = other,entity
                reverse = True
                pose_a,pose_b = self.poses[other],candidate
            point_a,point_b,normal,separation = self._ground_contact_record(
                contacts,reverse=reverse)
            key = (a,b)
            previous = self.ground_contacts.get(key)
            if previous is None or separation < previous['separation']:
                self.ground_contacts[key] = {
                    'point_a':point_a,
                    'point_b':point_b,
                    'normal':normal,
                    'separation':separation,
                    'pose_a':pose_a,
                    'pose_b':pose_b,
                }

    @staticmethod
    def _ground_contact_record(points, *, reverse=False):
        """Collapse the measured near-simultaneous contact patch to one point."""
        minimum = min(float(point[8]) for point in points)
        patch = [point for point in points if float(point[8]) <= minimum+.025]
        if (_CONTACT_NUMPY and np.mean is _CONTACT_MEAN and np.linalg.norm is _CONTACT_NORM
                and len(patch)==1 and type(reverse) is bool):
            point=patch[0]
            a,b=(point[6],point[5]) if reverse else (point[5],point[6])
            n=point[7]
            if (type(a) is tuple and type(b) is tuple and type(n) is tuple
                    and len(a)==len(b)==len(n)==3 and
                    all(type(v) is float and math.isfinite(v) and abs(v)<1e100 for row in (a,b,n) for v in row)):
                raw=np.asarray(n,dtype=float)
                if reverse:raw=-raw
                normal=raw+0.
                length=_vector_length(normal)
                if not math.isfinite(length) or length<=1e-8:
                    normal=raw
                    length=_vector_length(normal)
                if not math.isfinite(length) or length<=1e-8:
                    raise ValueError('Invalid ground contact normal patch')
                normal/=length
                return (tuple(v+0. for v in a),tuple(v+0. for v in b),
                        tuple(float(v) for v in normal),minimum)
        point_a = np.asarray([point[6] if reverse else point[5] for point in patch],
                             dtype=float)
        point_b = np.asarray([point[5] if reverse else point[6] for point in patch],
                             dtype=float)
        normals = np.asarray([(-np.asarray(point[7],dtype=float) if reverse
                               else np.asarray(point[7],dtype=float)) for point in patch])
        normal = np.mean(normals,axis=0)
        length = _vector_length(normal)
        if not math.isfinite(length) or length <= 1e-8:
            normal = normals[0]
            length = _vector_length(normal)
        if not math.isfinite(length) or length <= 1e-8:
            raise ValueError('Invalid ground contact normal patch')
        normal /= length
        return (tuple(float(value) for value in np.mean(point_a,axis=0)),
                tuple(float(value) for value in np.mean(point_b,axis=0)),
                tuple(float(value) for value in normal), minimum)

    @staticmethod
    def _ground_point_velocity(velocity, yaw_rate, arm):
        return (velocity[0] + yaw_rate*arm[2],
                velocity[1] - yaw_rate*arm[0])

    @staticmethod
    def _ground_cross(arm, direction):
        return arm[2]*direction[0] - arm[0]*direction[1]

    def _ground_contact_pairs(self):
        """Collect one measured closest point for each nearby grounded pair."""
        distance = self.GROUND_CONTACT_GAP
        pairs = dict(self.ground_contacts)
        for entity in sorted(self.body_entities.values()):
            if (entity in self.dynamic_bodies or entity in self.pending_dynamics
                    or not self.motion[entity].grounded):
                continue
            body = self.bodies[entity]
            cached = getattr(self, '_sensor_aabb_cache', {}).get(entity)
            if cached is None:
                low, high = bullet.getAABB(body, physicsClientId=self.cid)
                for link in range(bullet.getNumJoints(body, physicsClientId=self.cid)):
                    link_low, link_high = bullet.getAABB(body, link, physicsClientId=self.cid)
                    low = tuple(map(min, low, link_low))
                    high = tuple(map(max, high, link_high))
            else:
                low, high = cached
            near = bullet.getOverlappingObjects(tuple(v-distance for v in low),
                tuple(v+distance for v in high), physicsClientId=self.cid) or ()
            other_entities = sorted({other_entity for other, _ in near
                if (other_entity := self.body_entities.get(other)) is not None and other_entity > entity})
            for other_entity in other_entities:
                if ((entity, other_entity) in pairs or other_entity in self.dynamic_bodies
                        or other_entity in self.pending_dynamics
                        or not self.motion[other_entity].grounded):
                    continue
                points = bullet.getClosestPoints(bodyA=body,
                    bodyB=self.bodies[other_entity],distance=distance,
                    physicsClientId=self.cid)
                if not points:
                    continue
                if min(point[8] for point in points) <= distance:
                    point_a,point_b,normal,separation = self._ground_contact_record(points)
                    pairs[(entity,other_entity)] = {
                        'point_a':point_a,
                        'point_b':point_b,
                        'normal':normal,
                        'separation':separation,
                        'pose_a':self.poses[entity],
                        'pose_b':self.poses[other_entity],
                    }
        return pairs

    def resolve_ground_impacts(self, states):
        """Apply one mass/inertia-based planar impulse per closing tank pair.

        `states` maps entity IDs to `(world_xz_velocity, yaw_rate)` at this
        tick. Swept movement commands take precedence, so a hull stopped at
        contact retains its pre-impact velocity for the impulse calculation.
        This layer never acts on map/material contacts or airborne Bullet bodies.
        """
        if not self.experimental_ground_impacts:
            return ()
        if set(self.ground_mass_properties) != set(self.bodies):
            raise ValueError('Missing ground-impact mass properties')

        state = {}
        for entity, value in states.items():
            if (entity not in self.bodies or len(value) != 2 or len(value[0]) != 2
                    or not all(math.isfinite(component) for component in (*value[0],value[1]))):
                raise ValueError('Invalid ground-impact state')
            state[entity] = (tuple(float(component) for component in value[0]),
                             float(value[1]))
        for entity, (velocity,yaw_rate) in self.ground_commands.items():
            state[entity] = (velocity,yaw_rate)

        # A tank can hand back to the track solver in the tick it was hit.
        # That pair must not generate a second ram on the ground.
        if self.air_ram_impacts:
            self.ground_contact_pairs = self.ground_contact_pairs.union(self.air_ram_impacts)
        pairs = sorted(self._ground_contact_pairs().items())
        if getattr(self, 'collision_zone', None):
            pairs = [(pair, contact) for pair, contact in pairs
                     if self.collision_zone(self.poses[pair[0]].position)
                     == self.collision_zone(self.poses[pair[1]].position)]
        from .impact_native import try_ground_impacts
        native = try_ground_impacts(self,state,pairs)
        if native is not None:
            return self._commit_ground_impacts(*native)

        def geometry(entity, point, pose):
            props = self.ground_mass_properties[entity]
            rotation = matrix(pose.yaw,pose.pitch,pose.roll)
            center = np.asarray(pose.position,dtype=float) + rotation @ np.asarray(
                props.center_of_mass,dtype=float)
            arm = np.asarray(point,dtype=float)-center
            inertia = np.asarray(props.inertia_diagonal,dtype=float)
            inertia_y = float(np.dot(rotation[1]*rotation[1],inertia))
            if not math.isfinite(inertia_y) or inertia_y <= 1e-6:
                raise ValueError('Invalid vertical ground-impact inertia')
            return props.mass,inertia_y,arm

        updates = {}
        impacts = []
        touching = set()
        for (a,b), contact in pairs:
            if (a in self.dynamic_bodies or b in self.dynamic_bodies
                    or a in self.pending_dynamics or b in self.pending_dynamics
                    or not self.motion[a].grounded or not self.motion[b].grounded):
                continue
            touching.add((a,b))
            if a not in state or b not in state:
                impacts.append(dict(entity_a=a,entity_b=b,
                    separation=contact['separation'],skipped='missing_velocity_state'))
                continue
            va,wa = state[a]
            vb,wb = state[b]
            normal = np.asarray(contact['normal'],dtype=float)
            normal_xz = np.asarray((normal[0],normal[2]),dtype=float)
            normal_length = _vector_length(normal_xz)
            if not math.isfinite(normal_length) or normal_length < .25:
                continue
            normal_xz /= normal_length
            point_a = np.asarray(contact['point_a'],dtype=float)
            point_b = np.asarray(contact['point_b'],dtype=float)
            mass_a,inertia_a,arm_a = geometry(a,point_a,contact['pose_a'])
            mass_b,inertia_b,arm_b = geometry(b,point_b,contact['pose_b'])
            relative = (np.asarray(self._ground_point_velocity(va,wa,arm_a))
                        - np.asarray(self._ground_point_velocity(vb,wb,arm_b)))
            closing = float(np.dot(relative,normal_xz))
            if not math.isfinite(closing) or closing >= -self.GROUND_IMPACT_MIN_CLOSING_SPEED:
                continue
            q_a = self._ground_cross(arm_a,normal_xz)
            q_b = self._ground_cross(arm_b,normal_xz)
            effective = 1/mass_a+1/mass_b+q_a*q_a/inertia_a+q_b*q_b/inertia_b
            if not math.isfinite(effective) or effective <= 0:
                raise ValueError('Invalid ground-impact effective mass')
            normal_impulse = -(1+self.GROUND_IMPACT_RESTITUTION)*closing/effective
            # Relative normal kinetic energy, 0.5*m_eff*v_n**2.  In the pair's
            # momentum frame each body carries the share k_i/effective of it, so
            # the lighter (or more lever-armed) participant absorbs more.
            ram_energy = .5*closing*closing/effective
            share_a = (1/mass_a+q_a*q_a/inertia_a)/effective
            ram = ((a,b) not in self.ground_contact_pairs
                   and -closing >= self.RAM_MIN_CLOSING_SPEED)
            impulse = normal_impulse*normal_xz
            va = (va[0]+impulse[0]/mass_a,va[1]+impulse[1]/mass_a)
            wa += q_a*normal_impulse/inertia_a
            vb = (vb[0]-impulse[0]/mass_b,vb[1]-impulse[1]/mass_b)
            wb -= q_b*normal_impulse/inertia_b

            tangent = np.asarray((-normal_xz[1],normal_xz[0]))
            relative = (np.asarray(self._ground_point_velocity(va,wa,arm_a))
                        - np.asarray(self._ground_point_velocity(vb,wb,arm_b)))
            tangent_speed = float(np.dot(relative,tangent))
            t_a = self._ground_cross(arm_a,tangent)
            t_b = self._ground_cross(arm_b,tangent)
            tangent_effective = (1/mass_a+1/mass_b+t_a*t_a/inertia_a+t_b*t_b/inertia_b)
            tangent_impulse = 0.
            if tangent_effective > 0 and math.isfinite(tangent_effective):
                tangent_impulse = max(-self.GROUND_IMPACT_FRICTION*normal_impulse,
                    min(self.GROUND_IMPACT_FRICTION*normal_impulse,
                        -tangent_speed/tangent_effective))
                friction = tangent_impulse*tangent
                va = (va[0]+friction[0]/mass_a,va[1]+friction[1]/mass_a)
                wa += t_a*tangent_impulse/inertia_a
                vb = (vb[0]-friction[0]/mass_b,vb[1]-friction[1]/mass_b)
                wb -= t_b*tangent_impulse/inertia_b

            state[a],state[b] = (va,wa),(vb,wb)
            updates[a],updates[b] = (va,wa),(vb,wb)
            impacts.append(dict(entity_a=a,entity_b=b,
                point_a=tuple(float(x) for x in point_a),
                point_b=tuple(float(x) for x in point_b),
                normal=tuple(float(x) for x in normal_xz),
                separation=contact['separation'],closing_speed=closing,
                normal_impulse=normal_impulse,tangent_impulse=tangent_impulse,
                mass_a=mass_a,mass_b=mass_b,
                mass_model='aabb_uniform',
                velocity_a=va,velocity_b=vb,yaw_rate_a=wa,yaw_rate_b=wb,
                ram=ram,ram_energy=ram_energy,ram_energy_a=ram_energy*share_a,
                ram_energy_b=ram_energy*(1-share_a)))

        return self._commit_ground_impacts(updates,impacts,touching)

    def _commit_ground_impacts(self, updates, impacts, touching):
        # Both sets of tracks scrub speed on a fresh ram. Apply it once per
        # tank across the contact batch, after measuring the energy for HP.
        rams = {(impact['entity_a'], impact['entity_b']) for impact in impacts if impact.get('ram')}
        rammed = {entity for pair in rams for entity in pair}
        for entity,(velocity,yaw_rate) in updates.items():
            if entity in rammed:
                velocity = tuple(v*self.RAM_TRACK_SPEED_RETENTION for v in velocity)
                track_rate = self.ground_track_rates.get(entity,0.)
                yaw_rate = track_rate+(yaw_rate-track_rate)*self.RAM_TRACK_YAW_RETENTION
                updates[entity] = velocity,yaw_rate
            self.ground_motion[entity] = GroundMotion(velocity,yaw_rate,True)
        self.ground_commands.clear()
        # A swept contact can disappear for a few ticks as the hulls shift
        # and immediately touch again. Resolve those impulses, but rearm ram
        # damage and track braking only after sustained separation.
        touching = touching | self._air_tank_contacts
        for pair in rams | self.air_ram_impacts.keys():
            self._ground_ram_clear_steps[pair] = 0
        for pair, clear in tuple(self._ground_ram_clear_steps.items()):
            if pair in touching:
                self._ground_ram_clear_steps[pair] = 0
            elif clear+1 >= self.RAM_REARM_CLEAR_STEPS:
                del self._ground_ram_clear_steps[pair]
            else:
                self._ground_ram_clear_steps[pair] = clear+1
        self.ground_contact_pairs = frozenset(touching | self._ground_ram_clear_steps.keys())
        limit = self.GROUND_IMPACT_TRACE_LIMIT
        # The trace is bounded, but a ram carries HP consequences downstream.
        self.last_ground_impacts = (tuple(impacts[:limit])
                                    + tuple(impact for impact in impacts[limit:]
                                            if impact.get('ram')))
        return updates

    def _motion_state(self, entity):
        """State for poses[entity], restarted when the pose was placed externally.

        A pose written without a new Motion starts at rest: grounded when it
        lies on its ground pose, otherwise falling from where it was placed.
        Assigning motion[entity] as well keeps that explicit state.
        """
        pose,state = self.poses[entity],self.motion.get(entity)
        seen = self._synced.get(entity)
        if state is None or seen is None or seen[1] is state and seen[0] != pose:
            ground = self.ground_pose(*pose.position[::2],pose.yaw,pose.position[1],entity=entity)
            state = self.motion[entity] = Motion((0.,0.,0.),
                        pose.position[1] <= ground.position[1]+self.SKIN)
            self._road_poses[entity] = ground
            self._wall_recoil_latches.pop(entity,None)
            if not state.grounded:
                self._begin_fall(entity,pose)
            self.short_flight_spins.pop(entity,None)
        elif not state.grounded and seen is not None and seen[1] is not state:
            self._begin_fall(entity,pose)
        return state

    @staticmethod
    def _slide(dx, dz, points):
        """Project a horizontal increment onto the tangent of its contacts."""
        for point in sorted(points,key=lambda p:p[8])[:4]:
            nx,_,nz = point[7]
            length2 = nx*nx+nz*nz
            toward = dx*nx+dz*nz
            if length2 > .01 and toward < 0:
                dx -= nx*toward/length2
                dz -= nz*toward/length2
        return dx,dz

    def _bottomed_pose(self, entity, candidate, points, dx, dz, road):
        """Sprung pose carried by a climbable incline its tracks bottomed on.

        The support fit already lets every station compress the full track
        travel, and the critically damped spring then lags that fit. At a
        ramp toe the two together press the belt deeper than its travel, and
        the overlap would discard all forward motion against a face the hull
        can drive up. Like a bump stop, the incline then carries the hull
        rigidly: the smallest change of station heights brings each such
        contact back to the travel, without making any bound of the fit worse.

        Only track contacts within one further travel qualify, on a static
        map surface whose own measured face lies inside the climb angle and
        is approached by this increment. Bullet's penetration normal at a
        triangle edge can be the pitched track's own floor; the face is
        therefore measured at the map contact itself. At a horizontal crest
        the approaching carrying plane supplies the uphill direction. Walls,
        risers, steeper faces, tanks and props remain obstacles. The spring
        keeps its rates: no launch velocity is added.
        """
        bounds = self._support_bounds.get(entity)
        rates = self._spring_rates.get(entity)
        if (not self.experimental_6dof or self.wall_trampoline
                or not points or bounds is None or bounds[0] is not road
                or rates is None or rates[0] != candidate):
            return None
        x0,y0,z0 = candidate.position
        c,s = math.cos(candidate.yaw),math.sin(candidate.yaw)
        slope_z = -math.tan(candidate.pitch)
        slope_x = math.tan(candidate.roll)*math.sqrt(1+slope_z*slope_z)
        u = np.array((y0,slope_x,slope_z))
        up = matrix(candidate.yaw,candidate.pitch,candidate.roll)[:,1]
        rows,lower = [],[]
        for point in points:
            incline = self._track_incline(entity,candidate,point,crest=True)
            if incline is None:
                return None
            (nx,ny,nz),floor,depth = incline
            excess = depth-self.SUSPENSION_TRAVEL
            if excess <= 0.:
                continue
            approach = dx*nx+dz*nz if ny < .9999 else dx*up[0]+dz*up[2]
            if excess > self.SUSPENSION_TRAVEL or approach >= -1e-8:
                return None
            vx,_,vz = floor
            row = (1.,c*(vx-x0)-s*(vz-z0),s*(vx-x0)+c*(vz-z0))
            rows.append(row)
            # Lifting the plane by excess/ny here moves the face apart by excess.
            lower.append(float(np.dot(row,u))+excess/ny)
        if not rows:
            return None
        layout = self._support_layout(entity)
        phi,limits = bounds[1],bounds[2]
        heights = phi @ u
        weight = np.zeros(len(phi)+len(rows))
        weight[:len(layout.a)] = layout.weight
        (y,slope_x,slope_z),_ = self._support_plane(
            np.vstack((phi,rows)),np.concatenate((heights,np.zeros(len(rows)))),weight,
            np.concatenate((np.minimum(limits,heights),lower)))
        pose = Pose((x0,float(y),z0),candidate.yaw,-math.atan(slope_z),
                    math.atan(slope_x/math.sqrt(1+slope_z*slope_z)))
        self._spring_rates[entity] = (pose,*rates[1:])
        return pose

    def _contact_kind(self, points):
        return 'tank' if any(p[2] in self.bodies.values() for p in points) else 'map'

    def _drive(self, entity, distance, yaw_delta, dt=None, state=None,
               planar_displacement=None):
        if self.trace_dynamic_contacts:
            self.last_reject_reason = None
        pose = self.poses[entity]
        native=getattr(self,'_rust_drive',None)
        take_prefix=getattr(native,'take_drive_prefix',None)
        prefix=(take_prefix(entity,pose,distance,yaw_delta,dt,state,planar_displacement)
                if take_prefix is not None else None)
        support_context=prefix[11] if prefix is not None and len(prefix)>11 else None
        # Even a long frame cannot step through a thin wall or another tank.
        travel = (math.hypot(*planar_displacement) if planar_displacement is not None
                  else abs(distance))
        # A scene whose collision sensor has no separate track links cannot
        # safely permit suspension compression: there is only a hull collider.
        sprung = state is not None and self._has_suspension(entity)
        recoil = state is not None and self.experimental_6dof and not self.wall_trampoline
        latch = self._wall_recoil_latches.get(entity)
        if latch is not None and math.hypot(pose.position[0]-latch[0],
                pose.position[2]-latch[1]) > self.WALL_RECOIL_REARM_DISTANCE:
            del self._wall_recoil_latches[entity]
        wall_hit = None
        steps = max(1, math.ceil(travel/self.STEP),
                    math.ceil(abs(yaw_delta)/math.radians(.75)))
        moved = False
        # This solver has static map support and rejects overlap before moving;
        # a resting hull needs no repeated ground rays or contact queries. Its
        # Bullet body remains present for other vehicles and shell queries.
        if (travel==0. and yaw_delta==0. and
                (not sprung or (max(abs(v) for v in state.suspension_velocity) < .001
                  and not any(abs(v) > .01 for v in self._drive_efforts.get(entity,()))))):
            if state is not None:
                state.velocity = (0.,0.,0.)
                state.support_angular_rates = (0.,0.)
            self._support_memory.pop(entity,None)
            return pose,False,0.
        h = dt/steps if state is not None else None
        curvature_scale = 1.
        # Only the tracked 6DOF solver measures support reach per point from
        # its carrying plane; the legacy and trampoline solvers keep the hull
        # origin height they have always used.
        plane_reach = state is not None and self.experimental_6dof and not self.wall_trampoline
        def carried(pose):
            if not plane_reach:
                return pose.position[1]
            # Support reach follows the hull's own carrying plane, not the
            # visual effort lean superimposed on it.
            lean = self._lean_states.get(entity,(0.,0.,0.,0.))
            return Pose(pose.position,pose.yaw,pose.pitch-lean[0],pose.roll-lean[1])
        for step in range(steps):
            start = previous = pose
            old_road = self._road_poses.get(entity,start)
            # A track that has lost the ground under it falls toward it no
            # faster than gravity allows from its previous height and speed.
            floors = None if sprung else self._support_floors(entity,start,h)
            road_pose = None
            def support(x, z, yaw, reference, extra=()):
                nonlocal road_pose
                extra = self._cached_contact_supports(entity,x,z,yaw)+extra
                sample=None
                if support_context is not None and sprung and plane_reach and not extra:
                    result=native.try_support_pipeline(support_context,entity,x,z,yaw,reference,start,h,state)
                    if result is not None:
                        road_pose,candidate,sample=result
                        if road_pose is not None:
                            return candidate
                if sample is None:
                    target = self._support_pose(x,z,yaw,reference,entity=entity,
                        floors=floors,extra=extra,
                        compression=self.SUSPENSION_TRAVEL if sprung else 0.,
                        carrying=start if plane_reach else None)
                else:
                    target = self._support_pose(x,z,yaw,reference,entity=entity,
                        floors=floors,extra=extra,
                        compression=self.SUSPENSION_TRAVEL if sprung else 0.,
                        carrying=start if plane_reach else None,_sample=sample)
                road_pose = target
                return self._spring_pose(start,target,h,state,entity,extra) if sprung else target
            def bottom(candidate, points, dx, dz):
                """Let a bottomed suspension carry the hull up a climbable incline."""
                pose,more = candidate,points
                for _ in range(2):
                    pose = self._bottomed_pose(entity,pose,more,dx,dz,road_pose) if sprung else None
                    if pose is None:
                        break
                    clear,more = self._valid(pose,previous,entity=entity,suspension=sprung)
                    if clear:
                        return pose,True,[]
                return candidate,False,points
            yaw = (previous.yaw+yaw_delta/steps+math.pi) % math.tau-math.pi
            x,_,z = previous.position
            # Resolve rotation separately so a blocked turn cannot rotate a
            # long corner through a wall while the center remains stationary.
            if prefix is not None:
                if prefix[7].validation_queries==1:
                    road_pose,rotated=native.publish_drive_prefix(prefix,yaw)
                    valid,points=native.restore_rotation_rejection(prefix,rotated)
                    prefix=None
                else:
                    rotated=Pose((prefix[7].pose.x,prefix[7].pose.y,prefix[7].pose.z),
                                 yaw,prefix[7].pose.pitch,prefix[7].pose.roll)
                    valid,points=True,[]
            else:
                rotated = support(x,z,yaw,carried(previous))
                valid, points = self._valid(rotated,previous,entity=entity,
                                            suspension=sprung)
            if valid:
                previous = rotated
            else:
                self._record_ground_contacts(entity,points,rotated)
                yaw = previous.yaw
            if planar_displacement is None:
                dx,dz = (math.sin(yaw)*distance/steps*curvature_scale,
                         math.cos(yaw)*distance/steps*curvature_scale)
            else:
                dx,dz = (planar_displacement[0]/steps*curvature_scale,
                         planar_displacement[1]/steps*curvature_scale)
            incoming = (self._road_velocity(old_road,dx/h,dz/h)
                        if state is not None else None)
            impact = (dx,dz) if math.hypot(dx,dz)>1e-8 else None
            reference = carried(previous)
            if prefix is not None:
                road_pose,candidate=native.publish_drive_prefix(prefix,yaw)
                prefix=None
            else:
                candidate = support(x+dx,z+dz,yaw,reference)
            valid, points = self._valid(candidate,previous,impact,entity=entity,
                                        suspension=sprung)
            extra = ()
            for _ in range(2):
                if valid or not points:
                    break
                # A track meeting a low map surface is carried by it, as
                # by any other support station; taller faces stay obstacles.
                found = self._contact_supports(entity,candidate,points,reference)
                if not found:
                    break
                extra += found
                stepped = support(x+dx,z+dz,yaw,reference,extra)
                clear, more = self._valid(stepped,previous,impact,entity=entity,
                                          suspension=sprung)
                if clear:
                    candidate,valid,points = stepped,True,[]
                elif more:
                    candidate,points = stepped,more
            if not valid and points:
                candidate,valid,points = bottom(candidate,points,dx,dz)
            retreating = False
            if not valid and points and sprung:
                held = self._retreat_from_map_contact(entity,previous,points,dx,dz)
                if held is not None:
                    candidate,valid,points = held,True,[]
                    road_pose = Pose(held.position,held.yaw,old_road.pitch,old_road.roll)
                    retreating = True
            if not valid and points:
                self._record_ground_contacts(entity,points,candidate)
                # Project this increment onto the wall's tangent. There is
                # no corrective position jump; a candidate must remain clear.
                self.last_contact = self._contact_kind(points)
                if recoil and wall_hit is None:
                    wall_hit = self._wall_impact(points,dx/h,dz/h,previous.position[1])
                dx,dz = self._slide(dx,dz,points)
                if math.hypot(dx,dz) < travel/steps*.02:
                    dx = dz = 0.0
                candidate = support(x+dx,z+dz,yaw,reference,extra)
                valid,points = self._valid(candidate,previous,entity=entity,
                                           suspension=sprung)
            if not valid and not points and state is not None:
                # At a sharp edge the fitted plane can tip the hull up as its
                # support points cross over. Keep the present attitude while
                # it is still carried and not climbing; release follows below.
                held = self.held_pose(x+dx,z+dz,yaw,previous.pitch,previous.roll,
                                      previous.position[1],entity=entity)
                if held.position[1] <= previous.position[1]+self.SKIN:
                    valid,_ = self._valid(held,previous,entity=entity,climb=False,
                                          suspension=sprung)
                    if valid:
                        candidate = held
                        road_pose = held
            if valid:
                if state is not None:
                    # Wall projection may have removed most of this slice's
                    # commanded motion. Curvature may redirect only the
                    # surviving velocity, not restore dissipated contact
                    # energy by dividing by the shorter slide displacement.
                    incoming = self._road_velocity(old_road,dx/h,dz/h)
                if (state is not None and self.experimental_6dof and not self.wall_trampoline
                        and road_pose is not None and math.hypot(dx,dz) > 1e-8
                        and not self._support_detached(start,candidate,road_pose,h,
                                                       incoming[1],entity)):
                    # Redirect existing speed into the new road tangent.
                    # Keeping horizontal speed while adding a vertical
                    # component would manufacture energy at every ramp.
                    tangent = self._road_velocity(road_pose,dx/h,dz/h)
                    factor = math.sqrt(sum(v*v for v in incoming) /
                                       max(1e-12,sum(v*v for v in tangent)))
                    curvature_scale *= factor
                    dx,dz = dx*factor,dz*factor
                    adjusted = (Pose((x+dx,previous.position[1],z+dz),
                                     previous.yaw,previous.pitch,previous.roll)
                                if retreating else support(x+dx,z+dz,yaw,reference,extra))
                    if retreating:
                        clear,more = self._valid_retreat(adjusted,previous,entity)
                    else:
                        clear,more = self._valid(adjusted,previous,impact,entity=entity,
                                                 suspension=sprung)
                    if not clear and more:
                        adjusted,clear,_ = bottom(adjusted,more,dx,dz)
                    if clear:
                        candidate = adjusted
                    else:
                        dx = dz = 0.
                        candidate = previous
                        road_pose = old_road
                if self.trace_dynamic_contacts:
                    self.last_reject_reason = None
                pose = candidate
                moved |= math.hypot(dx,dz) > max(.00001,travel/steps*.02)
            else:
                if self.trace_dynamic_contacts:
                    self.last_reject_reason = self._last_validation_rejection or 'blocked'
                self.last_contact = self.last_contact or 'slope_or_border'
                pose = previous
            if state is not None:
                if sprung:
                    rates = self._spring_rates.get(entity)
                    state.suspension_velocity = (rates[1] if rates and rates[0] == pose
                                                   else (0.,0.,0.))
                    if rates and rates[0] == pose:
                        self._lean_states[entity] = rates[2]
                        state.support_angular_rates = rates[3]
                    else:
                        state.support_angular_rates = (0.,0.)
                    blocked = travel > 0. and math.hypot(dx,dz) < 1e-9
                    if self._drive_blocked.get(entity,False) and not blocked:
                        # Carrying height can respond while pushing a stone,
                        # but this blocked clearance response is not a free
                        # launch velocity when the tracks start moving again.
                        _,vp,vr = state.suspension_velocity
                        state.suspension_velocity = (0.,vp,vr)
                    self._drive_blocked[entity] = blocked
                if self.experimental_6dof and not self.wall_trampoline:
                    air = self._tracked_release(start,previous,pose,h,state,entity,
                                               valid,road_pose or pose,incoming)
                else:
                    air = self._release(start,previous,pose,h,state,entity,valid,
                                        road_pose if sprung else None)
                if air is not None:
                    pose = air
                    state.suspension_velocity = (0.,0.,0.)
                    state.support_angular_rates = (0.,0.)
                    self._support_memory.pop(entity,None)
                    self._wall_recoil_latches.pop(entity,None)
                    self.set_pose(self.bodies[entity],pose)
                    self.poses[entity] = pose
                    return pose,moved,dt-h*(step+1)
                self._remember_support(entity,start,pose,h)
                if valid and road_pose is not None:
                    self._road_poses[entity] = road_pose
            self.set_pose(self.bodies[entity],pose)
        if wall_hit is not None:
            self._wall_recoil(entity,pose,state,wall_hit)
        self.poses[entity] = pose
        return pose,moved,0.

    def _wall_impact(self, points, vx, vz, reference):
        """Measured face normal and closing speed of a solid map-wall block.

        Only static, unbroken map faces rising above track reach qualify;
        a tank, a low rock the tracks could carry, a shallow face or a
        glancing approach returns None.
        """
        if any(p[2] not in self.map_bodies or p[2] in self.breakable_bodies
               for p in points):
            return None
        speed = math.hypot(vx,vz)
        if speed < self.WALL_RECOIL_MIN_CLOSING_SPEED:
            return None
        nx = nz = 0.
        for p in points:
            if math.hypot(p[7][0],p[7][2]) > .7 and vx*p[7][0]+vz*p[7][2] < 0:
                nx += p[7][0]
                nz += p[7][2]
        length = math.hypot(nx,nz)
        if length < 1e-6:
            return None
        nx,nz = nx/length,nz/length
        closing = -(vx*nx+vz*nz)
        if (closing < self.WALL_RECOIL_MIN_CLOSING_SPEED
                or closing < self.WALL_RECOIL_MIN_INCIDENCE*speed):
            return None
        # The same head-room above which _contact_supports never carries a
        # track. A closest-point query may report one arbitrary point of a
        # face, so also probe the face horizontally just above that reach.
        nearest = sorted(points,key=lambda p:p[8])[:4]
        terrain = self._terrain_heights([p[6][0] for p in nearest],
                                        [p[6][2] for p in nearest])
        for p,ground in zip(nearest,terrain):
            floor = max(float(ground),reference)+self.SUPPORT_REACH
            if p[6][1] > floor+self.SKIN:
                return nx,nz,closing,speed
            y = floor+self.SKIN
            hit = self._static_ray((p[6][0]+nx*.3,y,p[6][2]+nz*.3),
                                   (p[6][0]-nx*.1,y,p[6][2]-nz*.1))
            if hit[0] in self.map_bodies and hit[0] not in self.breakable_bodies:
                return nx,nz,closing,speed
        return None

    def _wall_recoil(self, entity, pose, state, hit):
        """Replace the blocked normal velocity by a small capped rebound.

        The tangent and vertical components the solver left are kept. The
        result is velocity only: the next tick's ordinary sweep validates
        each backward increment. A hull that has not moved away from its
        last recoil against the same face does not rebound again.
        """
        nx,nz,closing,speed = hit
        latch = self._wall_recoil_latches.get(entity)
        if latch is not None and latch[2]*nx+latch[3]*nz > .7:
            return
        self._wall_recoil_latches[entity] = (pose.position[0],pose.position[2],nx,nz)
        vx,vy,vz = state.velocity
        along = vx*nx+vz*nz
        rebound = min(self.WALL_RECOIL_RESTITUTION*closing,self.WALL_RECOIL_MAX_SPEED)
        vx,vz = vx-along*nx+rebound*nx,vz-along*nz+rebound*nz
        planar = math.hypot(vx,vz)
        if planar > speed:
            vx,vz = vx*speed/planar,vz*speed/planar
        state.velocity = (float(vx),vy,float(vz))
        self.last_wall_recoil = dict(entity=entity,normal=(nx,nz),
            closing_speed=closing,rebound_speed=rebound,velocity=(float(vx),float(vz)))

    def _support_floors(self, entity, start, h):
        """Lowest station heights reachable from ``start`` in one slice.

        Each station falls from its own remembered downward speed. There is
        deliberately no extra descent allowance for a drivable grade: letting
        a held station drop by a grade's height in one slice is exactly the
        vertical snap these floors prevent, and _release would read it as a
        separation from the ground.
        """
        memory = self._support_memory.get(entity)
        if h is None or memory is None or memory[0] != start:
            return None
        _, heights, speeds = memory
        return heights-(speeds+.5*self.GRAVITY*h)*h

    def _remember_support(self, entity, start, pose, h):
        """Keep each station's height and downward speed for the next slice."""
        heights = self._station_heights(pose,entity)
        memory = self._support_memory.get(entity)
        speeds = (np.maximum(0.,(memory[1]-heights)/h)
                  if memory is not None and memory[0] == start else np.zeros_like(heights))
        self._support_memory[entity] = (pose,heights,speeds)

    def _seed_support_memory(self, entity, pose, vy):
        """Start station memory at a touchdown's own tangent descent rate.

        Fresh memory has zero station speeds, so the next slice's floors
        would hold every station on a descending grade as if it were a crest.
        """
        heights = self._station_heights(pose,entity)
        self._support_memory[entity] = (pose,heights,
                                        np.full_like(heights,max(0.,-float(vy))))

    def _contact_supports(self, entity, candidate, points, reference):
        """Support rows for track contacts with a walkable map top in reach.

        Bullet reports where the track's own convex surface meets a map body.
        Probing one integration slice into that body finds its top. A top that
        faces up, lies under a track and within SUPPORT_REACH of the hull is a
        support station at that point, with the track profile height there.
        Tank contacts, breakable props and tall or steep faces are excluded;
        they remain obstacles for the ordinary contact response.
        """
        layout = self._support_layout(entity)
        x0,_,z0 = candidate.position
        c,s = math.cos(candidate.yaw),math.sin(candidate.yaw)
        found = []
        for point in sorted(points,key=lambda p:p[8])[:8]:
            body = point[2]
            if body not in self.map_bodies or body in self.breakable_bodies:
                continue
            # The station sits where the track surface itself was met.
            vx,_,vz = point[5]
            a = c*(vx-x0)-s*(vz-z0)
            b = s*(vx-x0)+c*(vz-z0)
            profile = layout.track_profile(a,b,self.SKIN)
            compliant = profile is not None
            if profile is None:
                # A short stone can catch the hull between the two tracks
                # before either track station reaches it. Treat a low,
                # upward-facing top under the chassis as another carrying
                # point so the hull rises with the tracks instead of stopping
                # on its belly. The bounded ray below still rejects tall
                # stones and vertical faces.
                inner = min(min(abs(track[0]),abs(track[1]))
                            for track in layout.tracks)
                if abs(a) >= inner or abs(b) > layout.patch+self.SKIN:
                    continue
                profile = self._hull_clearance(entity)
            px,_,pz = point[6]
            nx,_,nz = point[7]
            length = math.hypot(nx,nz)
            if length > 1e-6:
                px -= nx/length*self.STEP
                pz -= nz/length*self.STEP
            terrain = float(self._terrain_heights((px,),(pz,))[0])
            origin = reference.position[1] if isinstance(reference,Pose) else reference
            top = max(terrain,origin)+self.SUPPORT_REACH
            hit = self._static_ray((px,top,pz),(px,terrain-.4,pz))
            if isinstance(reference,Pose):
                # As in _ground_heights: the carrying plane only adds a top
                # the origin ray started beneath.
                start = max(terrain,float(self._support_references(reference,px,pz,entity)))
                start += self.SUPPORT_REACH
                if start > top:
                    higher = self._static_ray((px,start,pz),(px,terrain-.4,pz))
                    if (higher[0] in self.map_bodies and higher[0] not in self.breakable_bodies
                            and higher[4][1] > .65 and higher[2] > 0. and higher[3][1] > top):
                        hit = higher
            if (hit[0] not in self.map_bodies or hit[0] in self.breakable_bodies
                    or hit[4][1] <= .65 or hit[2] <= 0.):
                continue
            row = (a,b,float(hit[3][1])-profile,compliant)
            if all(math.dist(row[:2],other[:2]) > 1e-3 for other in found):
                found.append(row)
                wx,wz = x0+c*a+s*b,z0-s*a+c*b
                cache = self._contact_support_cache.setdefault(entity,[])
                entry = (body,float(wx),float(wz),float(hit[3][1]))
                cache[:] = [old for old in cache
                            if old[0] != body or math.hypot(old[1]-wx,old[2]-wz) > .10]
                cache.append(entry)
                del cache[:-16]
        return tuple(found)

    def _cached_contact_supports(self, entity, x, z, yaw):
        """Measured rock tops remain support while inside the track footprint.

        Sampling stations alone can miss a narrow rock. These world-space
        contact points are shared by drive and landing, and expire by actual
        geometry/footprint rather than a timer.
        """
        layout = self._support_layout(entity)
        c,s = math.cos(yaw),math.sin(yaw)
        rows,keep = [],[]
        inner = min(min(abs(track[0]),abs(track[1])) for track in layout.tracks)
        for body,wx,wz,height in self._contact_support_cache.get(entity,()):
            if body not in self.map_bodies or body in self.breakable_bodies:
                continue
            a,b = c*(wx-x)-s*(wz-z),s*(wx-x)+c*(wz-z)
            profile = layout.track_profile(a,b,self.SKIN)
            compliant = profile is not None
            if profile is None:
                if abs(a) >= inner or abs(b) > layout.patch+self.SKIN:
                    continue
                profile = self._hull_clearance(entity)
            rows.append((a,b,height-profile,compliant))
            keep.append((body,wx,wz,height))
        self._contact_support_cache[entity] = keep
        return tuple(rows)

    def _release(self, start, oriented, pose, h, state, entity, translated,
                 road_pose=None):
        """Separate from ground that falls away faster than gravity.

        The ground may push the hull up at any rate, but can pull it down no
        faster than gravity. When the solved ground pose drops below that
        ballistic height, the hull leaves it at the substep's instantaneous
        velocity with the attitude it last held on the ground.
        """
        if not translated:
            # A blocked increment stops the hull rather than launching it.
            state.velocity = (0.,0.,0.)
            return None
        g = self.GRAVITY
        x,y,z = start.position
        vx,vz = (pose.position[0]-x)/h,(pose.position[2]-z)/h
        vy0 = (state.velocity[1] if self.wall_trampoline else
               min(state.velocity[1],self.TRACKED_MAX_UPWARD_SPEED))
        vy = vy0-g*h
        fall = y+(vy0-.5*g*h)*h
        # The spring may consume a small height change while the road is
        # still within track travel. Once the road drops farther away, it
        # cannot pull the hull down. Use the road fit, rather than the lagging
        # visual hull pose, to decide whether that support is gone.
        road_gap = fall-(road_pose.position[1] if road_pose is not None
                         else pose.position[1])
        support_lost = road_pose is None
        if road_pose is not None and road_gap > self.SUSPENSION_TRAVEL+self.RELEASE_SPEED*h:
            # Coming off a low stone may briefly remove a fitted station,
            # while the hull remains within the track's ordinary reach of
            # the terrain. Keep that short event in the supported solver.
            terrain = float(self._terrain_heights((pose.position[0],),
                                                  (pose.position[2],))[0])
            nominal_ride = -float(np.min(self._support_layout(entity).p))
            clearance = pose.position[1]-(terrain+nominal_ride)
            old_terrain = float(self._terrain_heights((start.position[0],),
                                                      (start.position[2],))[0])
            horizontal = math.hypot(pose.position[0]-start.position[0],
                                    pose.position[2]-start.position[2])
            descending_ground = old_terrain-terrain > .15*horizontal+self.SKIN
            support_lost = clearance > self.SUPPORT_REACH or descending_ground
        if (support_lost and
                pose.position[1] < fall-self.RELEASE_SPEED*h):
            air = Pose((pose.position[0],fall,pose.position[2]),pose.yaw,oriented.pitch,oriented.roll)
            if self._valid(air,pose,entity=entity,climb=False)[0]:
                state.velocity,state.grounded = (vx,vy,vz),False
                if self.experimental_6dof:
                    spin = self._launch_angular_velocity(start, oriented, h,
                        self.DYNAMIC_MAX_LAUNCH_ANGULAR_SPEED if self.wall_trampoline
                        else self.TRACKED_MAX_LAUNCH_ANGULAR_SPEED)
                    if self.wall_trampoline:
                        self._queue_dynamic(entity, air, state.velocity, spin)
                    else:
                        self.short_flight_spins[entity] = spin
                return air
        # On support the vertical speed follows the hull's plane, but may only
        # decay at gravity's rate. The lag is what carries a hull off a crest.
        # It never exceeds the hull's measured rise: a support plane that is
        # still tilting (a seam crossing) or a station lifted by a step moves
        # the hull's position, but is not vertical momentum it can keep.
        up = matrix(pose.yaw,pose.pitch,pose.roll)[:,1]
        surface = -(up[0]*vx+up[2]*vz)/up[1]
        rise = (pose.position[1]-y)/h
        lift = max(vy, min(rise,max(surface,vy)))
        if not self.wall_trampoline:
            lift = min(lift,self.TRACKED_MAX_UPWARD_SPEED)
        state.velocity = (vx,float(lift),vz)
        return None

    @staticmethod
    def _road_velocity(road, vx, vz):
        up = matrix(road.yaw,road.pitch,road.roll)[:,1]
        vy = -(up[0]*vx+up[2]*vz)/max(.05,float(up[1]))
        return float(vx),float(vy),float(vz)

    def _support_detached(self, start, pose, road, h, vy, entity):
        """A carrying surface can push upward, but cannot pull a tank down."""
        ballistic = start.position[1]+vy*h-.5*self.GRAVITY*h*h
        gap = max(ballistic,pose.position[1])-road.position[1]
        terrain = float(self._terrain_heights((pose.position[0],),
                                              (pose.position[2],))[0])
        if (not self._carrying_supported.get(entity,True)
                and start.position[1]-terrain > self.SUPPORT_REACH):
            return True
        separation = self.SUSPENSION_TRAVEL+self.SKIN if self._has_suspension(entity) else self.SKIN
        if gap <= separation:
            return False
        old_terrain = float(self._terrain_heights((start.position[0],),
                                                  (start.position[2],))[0])
        if terrain > old_terrain+1e-4:
            # A finite station entering an uphill patch can momentarily
            # flatten the fitted plane. The rising road is still carrying
            # the vehicle; that fit change is not a crest/takeoff.
            return False
        horizontal = math.hypot(pose.position[0]-start.position[0],
                                pose.position[2]-start.position[2])
        # A small isolated rock changes the fitted plane between stations;
        # its back edge stays inside track travel/reach. A descending terrain
        # crest or a real ledge removes the support instead.
        return (start.position[1]-terrain > self.SUPPORT_REACH or
                old_terrain-terrain > .15*horizontal+self.SKIN)

    def _tracked_release(self, start, oriented, pose, h, state, entity, valid,
                         road, incoming):
        dx,dz = pose.position[0]-start.position[0],pose.position[2]-start.position[2]
        if not valid or math.hypot(dx,dz) < 1e-9:
            state.velocity = (0.,0.,0.)
            return None
        if self._support_detached(start,pose,road,h,incoming[1],entity):
            vx,vy,vz = incoming
            if self._has_suspension(entity):
                vy = state.suspension_velocity[0]
                budget = math.sqrt(sum(v*v for v in incoming))
                speed = math.sqrt(vx*vx+vy*vy+vz*vz)
                scale = min(1.,budget/speed) if speed else 1.
                vx,vy,vz = vx*scale,vy*scale,vz*scale
                y = pose.position[1]
            else:
                y = start.position[1]+vy*h-.5*self.GRAVITY*h*h
                vy -= self.GRAVITY*h
            air = Pose((start.position[0]+vx*h,y,start.position[2]+vz*h),
                       pose.yaw,oriented.pitch,oriented.roll)
            if self._valid(air,pose,entity=entity,climb=False,
                           suspension=self.sprung_tracks)[0]:
                self._begin_fall(entity,air)
                state.velocity = (vx,vy,vz)
                state.grounded = False
                momentum_pose = oriented
                if self._has_suspension(entity):
                    # The visual spring still follows terrain for contact
                    # clearance. Once heave is gravity-bound its fitted tilt
                    # is not an applied torque: bound the additional rotation
                    # against loaded momentum and passive suspension droop.
                    # Yaw remains the current commanded hull rotation.
                    layout = self._support_layout(entity)
                    droop_speed = math.sqrt(2.*self.GRAVITY*self.SUSPENSION_TRAVEL)
                    def release_rate(actual, loaded, arm):
                        # Ordinary small settling rotations remain intact.
                        # Extra unloaded rotation is bounded by gravity over
                        # the available suspension stroke about the opposite
                        # supported end (full contact span), not spring omega.
                        allowance = droop_speed/max(.1,2.*arm)
                        carried = abs(loaded) if actual*loaded > 0. else 0.
                        return math.copysign(min(abs(actual),max(carried,allowance)),actual)
                    vpitch = release_rate((oriented.pitch-start.pitch)/h,
                        state.support_angular_rates[0],layout.patch)
                    vroll = release_rate((oriented.roll-start.roll)/h,
                        state.support_angular_rates[1],max(abs(float(v)) for v in layout.a))
                    momentum_pose = Pose(oriented.position,oriented.yaw,
                        start.pitch+vpitch*h,start.roll+vroll*h)
                self.short_flight_spins[entity] = self._launch_angular_velocity(
                    start,momentum_pose,h,self.TRACKED_MAX_LAUNCH_ANGULAR_SPEED)
                return air
        tangent = self._road_velocity(road,dx/h,dz/h)
        # Contact correction/solver tolerances may dissipate momentum, never
        # create it. Motor work and downhill gravity were already integrated
        # by Arena before this call.
        speed = math.sqrt(sum(v*v for v in tangent))
        budget = math.sqrt(sum(v*v for v in incoming))
        scale = min(1.,budget/speed) if speed else 1.
        state.velocity = tuple(v*scale for v in tangent)
        return None

    @classmethod
    def _launch_angular_velocity(cls, previous, current, dt, limit=None):
        """World-space angular velocity sampled from the final support slice.

        The tracked solver has no persistent angular state. At release it
        does, however, have two actual orientations from the same finite
        collision slice. Passing their bounded matrix delta to Bullet keeps
        an ordinary hull turn or a crest pitch from disappearing at the
        kinematic-to-dynamic boundary. This is not an invented suspension
        torque; its only input is the solver's own measured rotation.
        """
        if not isinstance(dt, (int, float)) or not math.isfinite(dt) or dt <= 0:
            return (0.,0.,0.)
        before,after = matrix(previous.yaw,previous.pitch,previous.roll), matrix(
            current.yaw,current.pitch,current.roll)
        relative = after @ before.T
        vector = np.array((relative[2,1]-relative[1,2],
                           relative[0,2]-relative[2,0],
                           relative[1,0]-relative[0,1]), dtype=float)
        sine = .5*_vector_length(vector)
        cosine = max(-1.,min(1.,(float(np.trace(relative))-1.)*.5))
        angle = math.atan2(sine,cosine)
        if not math.isfinite(angle) or angle <= 1e-10 or sine <= 1e-10:
            return (0.,0.,0.)
        velocity = vector*(angle/(2.*sine*dt))
        magnitude = _vector_length(velocity)
        if not math.isfinite(magnitude):
            return (0.,0.,0.)
        limit = cls.DYNAMIC_MAX_LAUNCH_ANGULAR_SPEED if limit is None else limit
        if magnitude > limit:
            velocity *= limit/magnitude
        return tuple(float(value) for value in velocity)

    def _fly(self, entity, dt, state):
        """Ballistic flight without control, swept in short 3D increments."""
        pose = self.poses[entity]
        # Continue a verified partial release next tick even inside the
        # short-hop clearance. Otherwise gravity can undo the same lift
        # repeatedly before the swept hull asks for rigid motion again.
        separating = self._separating_launches.pop(entity, None) == pose
        if entity not in self._fall_peaks:
            self._begin_fall(entity,pose)
        assets = self.assets_for(entity)
        suspension = self._has_suspension(entity)
        g = self.GRAVITY
        vx,vy,vz = state.velocity
        span = math.hypot(vx*dt,vz*dt,abs(vy)*dt+.5*g*dt*dt)
        steps = max(1,math.ceil(span/self.STEP))
        h = dt/steps
        moved = False
        landing_velocity = None
        for _ in range(steps):
            x,y,z = pose.position
            self._fall_peaks[entity] = max(self._fall_peaks.get(entity,y),y)
            dx,dz,dy = vx*h,vz*h,(vy-.5*g*h)*h
            vy -= g*h
            candidate = Pose((x+dx, y+dy, z+dz), pose.yaw, pose.pitch, pose.roll)
            clipped = self._border_pose(candidate, entity)
            if clipped.position[0] != candidate.position[0]:
                dx, vx = clipped.position[0] - x, 0.
            if clipped.position[2] != candidate.position[2]:
                dz, vz = clipped.position[2] - z, 0.
            limited, (vx, vy, vz) = self._clip_circle(entity, clipped, (vx, vy, vz))
            if limited is not clipped:
                dx, dz = limited.position[0] - x, limited.position[2] - z
            for attempt in range(3):
                ground = self._landing_support(x+dx,z+dz,pose.yaw,y,entity)
                landing = y+dy <= ground.position[1]
                if landing and (dx or dz) and ground.position[1]-y > math.hypot(dx,dz)*math.tan(assets.max_slope)+self.SKIN:
                    # Ground above the climbable rise is a face, not a landing.
                    valid,points = False,[]
                else:
                    candidate = ground if landing else Pose((x+dx,y+dy,z+dz),pose.yaw,pose.pitch,pose.roll)
                    valid,points = self._valid(candidate,pose,(dx,dz) if dx or dz else None,
                                               entity=entity,climb=False,suspension=suspension)
                if (not valid and self.experimental_ground_impacts
                        and any(point[2] in self.body_entities for point in points)):
                    snapshot = self._ram_body_snapshot(entity, pose, (vx, vy, vz), (0., 0., 0.))
                    self._record_air_rams({entity: snapshot}, {entity: points}, swept=True)
                if valid or not (dx or dz):
                    break
                self.last_contact = self._contact_kind(points) if points else 'slope_or_border'
                # Lose the velocity component into the obstacle and retry.
                dx,dz = self._slide(dx,dz,points) if points else (0.,0.)
                if math.hypot(dx,dz) < 1e-7:
                    dx = dz = 0.
                vx,vz = dx/h,dz/h
            if not valid:
                # Nothing clears even vertically: an obstacle caps or carries
                # the hull. Falling onto it is not support: the hull stays
                # airborne without velocity into the obstacle. Retry gravity
                # next tick so it falls when a supporting vehicle moves away.
                # Classify this final failed landing even when horizontal
                # motion was already zero. A prior rise rejection can leave
                # slope_or_border here and suppress a real map handoff forever.
                self.last_contact = self._contact_kind(points) if points else 'slope_or_border'
                impact_velocity = (vx,vy,vz)
                vx = vz = 0.
                if vy < 0:
                    # A shallow map-mesh seam can reject the swept landing
                    # pose even though the exact ground support pose is clear.
                    # Reconcile only when it is within this substep's measured
                    # fall distance and the support pose has no real overlap.
                    support = self._landing_support(x,z,pose.yaw,y,entity)
                    fall_gap = y-support.position[1]
                    max_drop = max(abs(dy)+self.SKIN,.02)
                    support_up = matrix(support.yaw,support.pitch,support.roll)[:,1]
                    if (-self.SKIN <= fall_gap <= max_drop
                            and support_up[1] >= self.DYNAMIC_GROUND_ALIGNMENT
                            and self._valid(support,pose,entity=entity,climb=False,
                                            suspension=suspension)[0]
                            and (not suspension or self._can_carry_origin(entity,support))):
                        pose = support
                        self._swept_track_landing(entity,pose,(vx,vy,vz))
                        self._road_poses[entity] = support
                        state.grounded = True
                        up = matrix(support.yaw,support.pitch,support.roll)[:,1]
                        velocity = np.asarray((vx,vy,vz))
                        landing_velocity = velocity-up*float(np.dot(velocity,up))
                        break
                    if self.experimental_6dof and self.last_contact == 'map':
                        # An airborne hull touching static geometry without
                        # a clear compliant support must let Bullet resolve
                        # the actual contact. Zeroing gravity here leaves it
                        # suspended forever a few centimetres above a stone.
                        self._queue_dynamic(entity,pose,impact_velocity,
                            self.short_flight_spins.get(entity,(0.,0.,0.)))
                    vy = 0.
                    break
                vy = 0.
                continue
            if landing and suspension and not self._can_carry_origin(entity,candidate):
                # A geometrically clear road fit may still have every
                # reachable contact behind the centre of mass. It is not a
                # carrying landing: let the free body pivot/fall rather than
                # reporting one grounded tick that drive immediately undoes.
                ballistic = Pose((x+dx,y+dy,z+dz),pose.yaw,pose.pitch,pose.roll)
                # The terrain mesh is excluded from swept obstacle queries.
                # Put the lowest actual chassis point on the heightfield so
                # Bullet can accept this body even on an unsupported edge.
                rotation = matrix(pose.yaw, pose.pitch, pose.roll)
                penetration = self._terrain_penetration(ballistic, rotation,
                                                        self._terrain_probe_points(entity))
                if penetration > self.SKIN:
                    bx, by, bz = ballistic.position
                    ballistic = Pose((bx, by + penetration - self.SKIN, bz),
                                     pose.yaw, pose.pitch, pose.roll)
                if self._valid(ballistic,pose,entity=entity,climb=False,suspension=True)[0]:
                    pose = ballistic
                    moved |= math.hypot(dx,dz) > 1e-5
                self._queue_dynamic(entity,pose,(vx,vy,vz),
                    self.short_flight_spins.get(entity,(0.,0.,0.)))
                break
            pose = candidate
            moved |= math.hypot(dx,dz) > 1e-5
            if landing:
                # The rest of this interval is not carried into the drive; the
                # base's outgoing speed follows the slope on the next step.
                self._swept_track_landing(entity,pose,(vx,vy,vz))
                self._road_poses[entity] = ground
                up = matrix(ground.yaw,ground.pitch,ground.roll)[:,1]
                velocity = np.asarray((vx,vy,vz))
                landing_velocity = velocity-up*float(np.dot(velocity,up))
                state.grounded = True
                break
        state.velocity = (tuple(float(value) for value in landing_velocity)
                          if landing_velocity is not None else (vx,vy,vz))
        self.set_pose(self.bodies[entity],pose)
        self.poses[entity] = pose
        if state.grounded:
            if self.experimental_6dof:
                # The landing velocity has already lost its normal component.
                # Its remaining vertical part is the hull following the slope,
                # not a bounce. A zero heave rate would let the hull descend
                # only from rest under gravity while the road keeps falling at
                # that rate; the growing gap then reads as a crest and relaunches
                # the tank with its descent erased.
                state.suspension_velocity = (state.velocity[1],0.,0.)
                self._seed_support_memory(entity,pose,state.velocity[1])
            else:
                state.suspension_velocity = (0.,0.,0.)
                self._support_memory.pop(entity,None)
            state.support_angular_rates = (0.,0.)
            # The swept fallback may complete a landing in the unused part
            # of a fixed step after _release queued the takeoff state.
            self.pending_dynamics.pop(entity, None)
            self.dynamic_launch_angular_velocities.pop(entity,None)
            self.dynamic_mass_yaws.pop(entity,None)
            self.short_flight_spins.pop(entity,None)
        if self.experimental_6dof and not state.grounded and entity not in self.dynamic_bodies:
            # Minor hops stay swept and upright. Once there is enough space
            # below the hull for a genuine fall, hand off to Bullet for impact
            # and rollover. The sandbox retains immediate rigid takeoff.
            ground = self._landing_support(pose.position[0],pose.position[2],pose.yaw,
                                          pose.position[1],entity)
            if self.wall_trampoline:
                # Keep the spin measured at takeoff when updating the queued
                # pose after the swept remainder of this fixed step.
                self._queue_dynamic(entity, pose, state.velocity)
            elif separating or pose.position[1]-ground.position[1] > self.DYNAMIC_MIN_CLEARANCE:
                self._queue_dynamic(entity, pose, state.velocity,
                                    self.short_flight_spins.get(entity,(0.,0.,0.)))
        return moved

    def raycast(self, origin, direction, distance=500, *, movable_armor_states=None):
        """Return the first direct sight contact.

        ``movable_armor_states`` is an optional frame-local entity-to-state
        mapping. It remains input data rather than CollisionWorld state, so
        aiming observes the same armor configuration as the asking frame.
        """
        body,point,_ = self.shot_path(origin,direction,distance,
                                      movable_armor_states=movable_armor_states)
        return body,point

    def static_segment(self, start, end):
        """First map/terrain contact; vehicle armor is queried separately."""
        hit = self._static_ray(start, end)
        return (hit[0],float(hit[2]),tuple(hit[3])) if hit[0]>=0 else (None,1.,tuple(end))

    def _dynamic_rotation(self, entity):
        """Return an active body's exact local-to-world Bullet rotation.

        ``poses`` carries Euler angles for the pre-existing sensor and network
        paths.  Those angles are deliberately continuous through a flip, but
        an instantaneous armor ray needs the rigid body's own matrix: it must
        never choose a different Euler representative at a singularity.
        """
        dynamic = self.dynamic_bodies.get(entity)
        if dynamic is None:
            return None
        _, orientation = bullet.getBasePositionAndOrientation(
            dynamic.body, physicsClientId=self.cid)
        return np.asarray(bullet.getMatrixFromQuaternion(orientation), dtype=float).reshape(3, 3)

    def _armor_state_snapshot_map(self, movable_armor_states):
        """Validate and locally freeze direct-query armor observations.

        Copying the input items once prevents a mutable Mapping implementation
        from changing which armor configuration one query uses. Snapshot
        values are already immutable. Unknown entity IDs fail closed rather
        than concealing an Arena/CollisionWorld frame mismatch.
        """
        if movable_armor_states is None:
            return {}
        if not isinstance(movable_armor_states, Mapping):
            raise ValueError('Invalid movable armor state mapping')
        from .armor import MovableArmorStateSnapshot

        snapshots = {}
        for entity, snapshot in tuple(movable_armor_states.items()):
            if (type(entity) is not int or entity not in self.poses or
                    not isinstance(snapshot, MovableArmorStateSnapshot)):
                raise ValueError('Invalid movable armor state mapping')
            snapshots[entity] = snapshot
        return snapshots

    def shot_path(self, origin, direction, distance=500, *, ignore_entity=None,
                  movable_armor_states=None, ignore_entities=()):
        armor_states = self._armor_state_snapshot_map(movable_armor_states)
        end = tuple(o+d*distance for o,d in zip(origin,direction))
        # Driving convex hulls are deliberately excluded from shell queries.
        # Armor uses the actual triangles/materials; world obstacles still occlude it.
        hit = self._static_ray(origin, end)
        limit = distance*hit[2] if hit[0]>=0 else distance
        ignore_entity = self.primary_id if ignore_entity is None else ignore_entity
        closest = None
        for entity,pose in self.poses.items():
            if entity == ignore_entity or entity in self.suspended_entities or entity in ignore_entities:
                continue
            yaw,pitch = self.gun_angles[entity]
            rotation = self._dynamic_rotation(entity)
            kwargs = {'rotation': rotation} if rotation is not None else {}
            if entity in armor_states:
                kwargs['movable_state'] = armor_states[entity]
            armor_hits = self.assets_for(entity).armor.intersections(
                origin,direction,pose,yaw,pitch,distance=limit,**kwargs)
            if armor_hits and (closest is None or armor_hits[0].distance < closest[1][0].distance):
                closest = entity,armor_hits
        if closest:
            entity,armor_hits = closest
            return self.bodies[entity],armor_hits[0].point,armor_hits
        return hit[0],tuple(hit[3]) if hit[0]>=0 else end,[]

    def ray(self, origin, direction, distance=500, *, movable_armor_states=None):
        body,point = self.raycast(origin,direction,distance,
                                  movable_armor_states=movable_armor_states)
        return body == self.target,point


# Keep replaced samplers and fit callbacks on their original Python path.
_SUPPORT_PLANE_IMPL = CollisionWorld._support_plane.__func__
_TERRAIN_HEIGHTS_IMPL = CollisionWorld._terrain_heights
_CONTACTS_IMPL = CollisionWorld._contacts
_VALID_IMPL = CollisionWorld._valid
_LANDING_SUPPORT_IMPL = CollisionWorld._landing_support
_CONTACT_SUPPORTS_IMPL = CollisionWorld._contact_supports


_DRIVE_PREFIX_METHODS={name:getattr(CollisionWorld,name) for name in (
    '_drive','_support_pose','_spring_pose','_valid','_cached_contact_supports',
    '_ground_heights','_terrain_heights','_support_layout','_hull_clearance',
    '_contacts','_inside','_has_suspension')}


_GROUND_RAY_BATCH_SOURCE = bullet.rayTestBatch
_GROUND_RAYS_IMPL = CollisionWorld._static_rays

_COMPRESSED_CONTACT_IMPL = CollisionWorld._compressed_track_contact
_TRACK_INCLINE_IMPL = CollisionWorld._track_incline
_RECOVERY_ROTATION_KEY = struct.Struct('=3d').pack

_RECOVERY_SCALAR_RAY = CollisionWorld._static_ray
_RECOVERY_RAY_TEST = bullet.rayTest

_RECOVERY_CLOSEST = bullet.getClosestPoints
_RECOVERY_CONTACT_POSE = struct.Struct('=6d').pack

_RECOVERY_BLOCKING_CONTACTS = CollisionWorld._blocking_contacts
_RECOVERY_SUPPORT_KEY = struct.Struct('=i3d').pack

_PIVOT_CLOSEST = bullet.getClosestPoints
_PIVOT_POSE_BYTES = struct.Struct('=7d').pack
_PIVOT_BULLET_APIS = tuple((name, getattr(bullet, name)) for name in
                           ('getClosestPoints','getNumJoints','getAABB','getOverlappingObjects'))
