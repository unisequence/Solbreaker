"""Isolated ground-drive replicas with conservative authority-side admission.

Replica worlds borrow prepared OBJ paths. They never own the source scene's
temporary directory, Bullet connection, armor model, or destructive effects.
"""
from array import array
import copy
from dataclasses import dataclass, fields
import hashlib
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pybullet as bullet

from .collision import CollisionWorld, Motion, Pose
from .drive_jobs import DRIVE_STATE, DriveJob, DriveReply, capture_state, restore_state
from .terrain import MapTerrain


def _mesh_key(vertices, triangles=None):
    vertices = np.asarray(vertices, dtype=np.float64)
    if triangles is None:
        triangles = np.column_stack((np.zeros(len(vertices)-2, dtype=np.int32),
                                     np.arange(1, len(vertices)-1),
                                     np.arange(2, len(vertices))))
    triangles = np.asarray(triangles, dtype=np.int32)
    return hashlib.sha256(vertices.tobytes()+triangles.tobytes()).hexdigest()


def _partition_key(vertices, triangles):
    return hashlib.sha256(vertices.tobytes()+triangles.tobytes()).digest()


@dataclass(frozen=True)
class PreparedMeshes:
    """Read-only view of files whose lifetime remains with the owner scene."""
    meshes: dict
    partitions: dict
    closed: bool = False

    def mesh(self, vertices, triangles=None):
        key = _mesh_key(vertices, triangles)
        try:
            path = self.meshes[key]
        except KeyError as error:
            raise ValueError('Replica requested an unprepared mesh') from error
        if not Path(path).is_file():
            raise ValueError('Owner collision mesh is no longer available')
        return path

    def mesh_with_creation_status(self, vertices, triangles=None):
        return self.mesh(vertices, triangles), False

    def partition(self, vertices, triangles):
        try:
            return self.partitions[_partition_key(vertices, triangles)]
        except KeyError as error:
            raise ValueError('Replica requested an unprepared map partition') from error

    def close(self):
        # Deliberately no ownership of a TemporaryDirectory or its files.
        pass


@dataclass(frozen=True)
class ReplicaAssets:
    vertices: object
    triangles: object
    destructibles: dict
    tank_shapes: tuple
    turret_node: object
    hull_center: tuple
    support: tuple
    max_slope: float
    forward_speed: float
    reverse_speed: float
    rotation_speed: float
    definition: object
    bounds: tuple
    mesh_files: PreparedMeshes
    terrain_paths: tuple
    contact_proxies: object = None
    # Disabling flight-shape prewarming is intentional: every flight path is
    # rejected before it can construct or activate a dynamic body.
    _convex_shapes: object = None

    def prepare_terrain(self, terrain):
        return self.terrain_paths


@dataclass(frozen=True)
class ReplicaSpec:
    terrain: object
    assets: ReplicaAssets
    vehicle_assets: dict
    poses: dict
    gun_angles: dict
    destroyed: frozenset
    # Ordered source paths and owner IDs, not assumptions about native IDs.
    static_layout: tuple
    experimental_6dof: bool
    experimental_ground_impacts: bool
    wall_trampoline: bool
    sprung_tracks: bool
    trace_dynamic_contacts: bool


def _path(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8')
    return str(Path(value).resolve())


def _owner_static_layout(world, paths):
    available = {}
    for body in world.map_bodies | world.terrain_bodies:
        path = getattr(world, '_mesh_body_paths', {}).get(body)
        if path is None:
            shapes = bullet.getCollisionShapeData(body, -1, physicsClientId=world.cid)
            path = shapes[0][4] if len(shapes) == 1 else None
        if not path or path in ('unknown_file', b'unknown_file'):
            raise ValueError('Cannot identify an owner static collision mesh')
        available.setdefault(_path(path), []).append(body)
    for bodies in available.values():
        bodies.sort()
    layout = []
    for path, destroyed in paths:
        matches = available.get(_path(path), [])
        if matches:
            body = matches.pop(0)
        elif destroyed:
            body = None
        else:
            raise ValueError('Prepared mesh does not match the owner static world')
        layout.append((_path(path), body))
    if any(available.values()):
        raise ValueError('Owner has unrepresented static collision bodies')
    return tuple(layout)


def _definition(source):
    result = SimpleNamespace(aiming=SimpleNamespace(
        turret_position=tuple(source.aiming.turret_position)))
    if hasattr(source, 'mass'):
        result.mass = source.mass
    for name in ('hull', 'chassis', 'turret', 'gun', 'engine', 'fuel', 'radio'):
        component = getattr(source, name, None)
        if component is not None and hasattr(component, 'mass'):
            setattr(result, name, SimpleNamespace(mass=component.mass))
    return result


def build_spec(world):
    """Snapshot reusable geometry without serializing live native resources."""
    if world.closed or world.closing or not world.build_ready:
        raise ValueError('Drive replica requires a complete live collision world')
    assets_by_id, replicas = {}, {}
    terrain = copy.copy(world.terrain)
    if isinstance(terrain, MapTerrain):
        # Unlike a NumPy scalar, array indexing returns the same Python int
        # used by MapTerrain.height. Its operation order stays unchanged.
        terrain.heights = array('H', world.terrain.heights)
    else:
        terrain = copy.deepcopy(terrain)
    source_assets = [world.assets, *(world.assets_for(entity) for entity in world.poses)]
    for source in source_assets:
        if id(source) in assets_by_id:
            continue
        meshes, partitions = {}, {}

        def partition(vertices, triangles):
            paths = tuple(source.mesh_files.partition(vertices, triangles))
            partitions[_partition_key(vertices, triangles)] = paths
            return paths

        partition(source.vertices, source.triangles)
        for obj in source.destructibles.values():
            partition(obj.vertices, obj.triangles)
        terrain_paths = tuple(source.prepare_terrain(world.terrain))
        proxies = getattr(source, 'contact_proxies', None)
        if proxies:
            for part in proxies:
                if part.kind == 'prism':
                    meshes[_mesh_key(part.vertices)] = source.mesh_files.mesh(part.vertices)
        else:
            for name, vertices in source.tank_shapes:
                meshes[_mesh_key(vertices)] = source.mesh_files.mesh(vertices)
        replica = ReplicaAssets(source.vertices, source.triangles,
            dict(source.destructibles), tuple(source.tank_shapes), source.turret_node,
            tuple(source.hull_center), tuple(source.support), source.max_slope,
            source.forward_speed, source.reverse_speed, source.rotation_speed,
            _definition(source.definition), tuple(source.bounds),
            PreparedMeshes(meshes, partitions), terrain_paths, proxies)
        assets_by_id[id(source)] = replica
    for entity in world.poses:
        replicas[entity] = assets_by_id[id(world.assets_for(entity))]
    source = world.assets
    static_paths = [(path, False) for path in source.mesh_files.partition(source.vertices, source.triangles)]
    for key, obj in source.destructibles.items():
        static_paths.extend((path, key in world.destroyed)
                            for path in source.mesh_files.partition(obj.vertices, obj.triangles))
    static_paths.extend((path, False) for path in source.prepare_terrain(world.terrain))
    return ReplicaSpec(terrain, assets_by_id[id(world.assets)], replicas,
        dict(world.poses), dict(world.gun_angles), frozenset(world.destroyed),
        _owner_static_layout(world, static_paths), bool(world.experimental_6dof),
        bool(world.experimental_ground_impacts), bool(world.wall_trampoline),
        bool(world.sprung_tracks), bool(world.trace_dynamic_contacts))


class _RejectDrive(Exception):
    pass


class _ReplicaWorld(CollisionWorld):
    def __init__(self, *args, **kwargs):
        self._replica_active = False
        self._replica_footprint = None
        self._replica_validation_touched = False
        self._replica_static = []
        super().__init__(*args, **kwargs)

    def _init_rapier(self):
        # A replica answers ground queries only. Flight/activation reject the
        # job below; the authoritative owner alone integrates rigid bodies.
        # Keep the 6DOF drive rules, without duplicating the dynamic world.
        if self._rapier_query_backend != 'bullet':
            super()._init_rapier(static_only=True)

    def __setattr__(self, name, value):
        if name == '_last_validation_rejection' and getattr(self, '_replica_active', False):
            object.__setattr__(self, '_replica_validation_touched', True)
        super().__setattr__(name, value)

    def _mesh_body(self, path):
        body = super()._mesh_body(path)
        self._replica_static.append((_path(path), body))
        return body

    def _include_sensor(self, entity):
        body = self.bodies[entity]
        for link in range(-1, bullet.getNumJoints(body, physicsClientId=self.cid)):
            low, high = bullet.getAABB(body, link, physicsClientId=self.cid)
            rect = (low[0]-self.SKIN, low[2]-self.SKIN,
                    high[0]+self.SKIN, high[2]+self.SKIN)
            old = self._replica_footprint
            self._replica_footprint = rect if old is None else (
                min(old[0], rect[0]), min(old[1], rect[1]),
                max(old[2], rect[2]), max(old[3], rect[3]))

    def _contacts(self, pose, entity=None, *, suspension=False):
        entity = self.primary_id if entity is None else entity
        points = super()._contacts(pose, entity, suspension=suspension)
        if self._replica_active and any(
                p[2] in self.body_entities and self.body_entities[p[2]] != entity
                for p in points):
            raise _RejectDrive('vehicle_contact')
        return points

    def _contact_query_observer(self, entity, low, high):
        if self._replica_active:
            rect = (float(low[0]),float(low[2]),float(high[0]),float(high[2]))
            old = self._replica_footprint
            self._replica_footprint = rect if old is None else (
                min(old[0],rect[0]),min(old[1],rect[1]),
                max(old[2],rect[2]),max(old[3],rect[3]))

    def destroy(self, *args, **kwargs):
        if self._replica_active:
            raise _RejectDrive('destruction')
        return super().destroy(*args, **kwargs)

    def _begin_fall(self, *args, **kwargs):
        if self._replica_active:
            raise _RejectDrive('takeoff')
        return super()._begin_fall(*args, **kwargs)

    def _fly(self, *args, **kwargs):
        if self._replica_active:
            raise _RejectDrive('flight')
        return super()._fly(*args, **kwargs)

    def _queue_dynamic(self, *args, **kwargs):
        if self._replica_active:
            raise _RejectDrive('dynamic_queue')
        return super()._queue_dynamic(*args, **kwargs)

    def _activate_dynamic(self, *args, **kwargs):
        if self._replica_active:
            raise _RejectDrive('dynamic_activation')
        return super()._activate_dynamic(*args, **kwargs)

    def _dynamic_shape(self, *args, **kwargs):
        if self._replica_active:
            raise _RejectDrive('dynamic_shape')
        return super()._dynamic_shape(*args, **kwargs)

    def _record_ground_contacts(self, entity, points, candidate):
        if self._replica_active and any(
                p[2] in self.body_entities and self.body_entities[p[2]] != entity
                for p in points):
            raise _RejectDrive('ground_vehicle_contact')
        return super()._record_ground_contacts(entity, points, candidate)

    def _wall_recoil(self, *args, **kwargs):
        if self._replica_active:
            raise _RejectDrive('wall_recoil')
        return super()._wall_recoil(*args, **kwargs)


class DriveReplica:
    def __init__(self, spec):
        self.spec = spec
        self.world = _ReplicaWorld(spec.assets, spec.terrain, vehicles=spec.poses,
            vehicle_assets=spec.vehicle_assets, experimental_6dof=spec.experimental_6dof,
            experimental_ground_impacts=False, wall_trampoline=spec.wall_trampoline,
            sprung_tracks=spec.sprung_tracks)
        try:
            self.world.experimental_ground_impacts = spec.experimental_ground_impacts
            self.world.trace_dynamic_contacts = spec.trace_dynamic_contacts
            native = self.world._replica_static
            if len(native) != len(spec.static_layout):
                raise ValueError('Replica static mesh layout changed')
            self.owner_to_replica, self.replica_to_owner = {}, {}
            for (path, owner), (replica_path, replica) in zip(spec.static_layout, native):
                if path != replica_path:
                    raise ValueError('Replica static mesh order changed')
                if owner is not None:
                    self.owner_to_replica[owner] = replica
                    self.replica_to_owner[replica] = owner
            self._sync_poses, self._sync_angles = {}, {}
            self.sync(spec.poses, spec.gun_angles, spec.destroyed)
        except BaseException:
            self.world.close()
            raise

    def sync(self, poses, gun_angles, destroyed):
        world = self.world
        if world.closed:
            raise ValueError('Drive replica is closed')
        if set(poses) != set(world.bodies) or set(gun_angles) != set(world.bodies):
            raise ValueError('Replica roster changed')
        destroyed = set(destroyed)
        if not world.destroyed <= destroyed or not destroyed <= set(world.assets.destructibles):
            raise ValueError('Replica destruction must be monotonic')
        for key in sorted(destroyed-world.destroyed):
            CollisionWorld.destroy(world, key, (0., 0.))
        world.destruction_events.clear()
        self._sync_poses, self._sync_angles = dict(poses), dict(gun_angles)
        for entity, pose in poses.items():
            world.poses[entity] = pose
            world.set_pose(world.bodies[entity], pose)
            world.set_turret(gun_angles[entity][0], entity, gun_angles[entity][1])

    def _translate_state(self, state, mapping):
        state = copy.deepcopy(state)
        if '_contact_support_cache' in state:
            try:
                state['_contact_support_cache'] = [
                    (mapping[body], *row) for body, *row in state['_contact_support_cache']]
            except KeyError as error:
                raise _RejectDrive('unmapped_contact_support') from error
        return state

    def run(self, job):
        world = self.world
        entity = job.entity
        world._replica_footprint = None
        if (world.closed or entity not in world.bodies or not isinstance(job.motion, Motion)
                or not job.motion.grounded):
            return DriveReply(entity, False, reason='unsupported_motion')
        try:
            local = copy.deepcopy(job)
            state = self._translate_state(local.state, self.owner_to_replica)
            restore_state(world, entity, state)
            world.poses[entity], world.motion[entity] = local.pose, local.motion
            world._synced[entity] = (local.pose, local.motion)
            world._drive_efforts[entity] = tuple(local.effort)
            world.set_pose(world.bodies[entity], local.pose)
            world.set_turret(local.gun_angles[0], entity, local.gun_angles[1])
            world._replica_footprint = None
            world._replica_validation_touched = False
            world._replica_active = True
            world._include_sensor(entity)
            options = dict(local.options)
            if options.pop('entity', entity) != entity:
                raise _RejectDrive('mismatched_entity_option')
            pose, moved = world.move(local.distance, local.yaw_delta, entity, **options)
            if not world.motion[entity].grounded or world.dynamic_bodies or world.pending_dynamics:
                raise _RejectDrive('non_grounded_result')
            world._include_sensor(entity)
            diagnostics = (world.last_contact, world.last_wall_recoil, world.last_reject_reason,
                           world._replica_validation_touched,
                           world._last_validation_rejection
                           if world._replica_validation_touched else None)
            return DriveReply(entity, True, pose, moved, copy.deepcopy(world.motion[entity]),
                self._translate_state(capture_state(world, entity), self.replica_to_owner),
                world._replica_footprint, copy.deepcopy(diagnostics))
        except _RejectDrive as error:
            return DriveReply(entity, False, footprint=world._replica_footprint, reason=str(error))
        finally:
            world._replica_active = False
            # Another job in the same worker must see the synchronization
            # barrier's actor positions, never this speculative displacement.
            if not world.closed and entity in self._sync_poses:
                world.poses[entity] = self._sync_poses[entity]
                world.set_pose(world.bodies[entity], self._sync_poses[entity])
                angles = self._sync_angles[entity]
                world.set_turret(angles[0], entity, angles[1])

    def close(self):
        self.world.close()


def _finite_pose(pose):
    return (isinstance(pose, Pose) and len(pose.position) == 3
            and all(math.isfinite(v) for v in (*pose.position, pose.yaw, pose.pitch, pose.roll)))


def _finite_motion(motion):
    if type(motion.grounded) is not bool:
        return False
    for field in fields(Motion):
        value = getattr(motion, field.name)
        if isinstance(field.default, tuple):
            if (not isinstance(value, tuple) or len(value) != len(field.default)
                    or not all(math.isfinite(v) for v in value)):
                return False
    return True


def _sensor_inside_footprint(world, entity, footprint):
    body = world.bodies[entity]
    for link in range(-1, bullet.getNumJoints(body, physicsClientId=world.cid)):
        low, high = bullet.getAABB(body, link, physicsClientId=world.cid)
        if (low[0] < footprint[0] or low[2] < footprint[1]
                or high[0] > footprint[2] or high[2] > footprint[3]):
            return False
    return True


def sensor_rect(world, entity):
    """Current union of all sensor-link XZ bounds, without a pose estimate."""
    body = world.bodies[entity]
    rect = bounds = None
    for link in range(-1,bullet.getNumJoints(body,physicsClientId=world.cid)):
        low,high = bullet.getAABB(body,link,physicsClientId=world.cid)
        value = (low[0],low[2],high[0],high[2])
        rect = value if rect is None else (min(rect[0],value[0]),min(rect[1],value[1]),
                                          max(rect[2],value[2]),max(rect[3],value[3]))
        bounds = (low,high) if bounds is None else (
            tuple(min(a,b) for a,b in zip(bounds[0],low)),
            tuple(max(a,b) for a,b in zip(bounds[1],high)))
    cache = getattr(world, '_sensor_aabb_cache', None)
    if cache is not None:
        cache[entity] = bounds
    return rect


def accept_reply(world, reply, *, other_bounds=None, transfer=False):
    """Admit a map-only ground proposal after checking current actor geometry.

    Map revision and pool generation are checked by the owner before this
    function. Native sensor bounds here include all already-committed moves.
    """
    entity = reply.entity
    if (not reply.accepted or world.closed or world.closing or not world.build_ready
            or entity not in world.bodies or entity in world.dynamic_bodies
            or entity in world.pending_dynamics or entity not in world.motion
            or not world.motion[entity].grounded or not isinstance(reply.motion, Motion)
            or not reply.motion.grounded or not _finite_pose(reply.pose)
            or not _finite_motion(reply.motion)
            or not isinstance(reply.state, dict) or set(reply.state)-set(DRIVE_STATE)
            or not isinstance(reply.diagnostics, tuple) or len(reply.diagnostics) != 5
            or type(reply.diagnostics[3]) is not bool):
        return False
    footprint = reply.footprint
    if (not isinstance(footprint, tuple) or len(footprint) != 4
            or not all(math.isfinite(v) for v in footprint)
            or footprint[0] > footprint[2] or footprint[1] > footprint[3]):
        return False
    if not _sensor_inside_footprint(world, entity, footprint):
        return False
    gap = world.GROUND_CONTACT_GAP
    if other_bounds is not None and set(other_bounds) != set(world.bodies):
        return False
    for other in world.bodies:
        if other == entity:
            continue
        rect = sensor_rect(world,other) if other_bounds is None else other_bounds[other]
        if not (footprint[2] < rect[0]-gap or footprint[0] > rect[2]+gap
                or footprint[3] < rect[1]-gap or footprint[1] > rect[3]+gap):
            return False
    # A pool reply has exactly one consumer after take() removes it from the
    # inbox. Its deserialized arrays/lists can transfer to the owner directly;
    # ordinary callers retain the defensive-copy contract.
    state = reply.state if transfer else copy.deepcopy(reply.state)
    motion_values = {field.name: getattr(reply.motion, field.name)
                     for field in fields(Motion)}
    old_pose = world.poses[entity]
    try:
        world.set_pose(world.bodies[entity], reply.pose)
        if not _sensor_inside_footprint(world, entity, footprint):
            world.set_pose(world.bodies[entity], old_pose)
            return False
    except Exception:
        world.set_pose(world.bodies[entity], old_pose)
        return False
    motion = world.motion[entity]
    for name, value in motion_values.items():
        setattr(motion, name, value)
    restore_state(world, entity, state)
    world.poses[entity] = reply.pose
    world._synced[entity] = (reply.pose, motion)
    world.last_contact, world.last_wall_recoil, world.last_reject_reason = reply.diagnostics[:3]
    if reply.diagnostics[3]:
        world._last_validation_rejection = reply.diagnostics[4]
    return True


# Freeze the known replica overrides, rather than trusting arbitrary subclasses.
from .collision import _DRIVE_PREFIX_METHODS
_REPLICA_PREFIX_METHODS = {name: getattr(_ReplicaWorld, name) for name in (
    *_DRIVE_PREFIX_METHODS, '_contact_query_observer', '_include_sensor',
    'destroy', '_begin_fall', '_fly', '_queue_dynamic', '_activate_dynamic',
    '_dynamic_shape', '_record_ground_contacts', '_wall_recoil', '__setattr__')}


def _replica_prefix_contract(world):
    if type(world) is not _ReplicaWorld or not world._replica_active:
        return None
    return _REPLICA_PREFIX_METHODS
