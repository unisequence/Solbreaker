"""Experimental native physics owner, with setup and result marshalling."""
import ctypes as C
import json
import math
import os
import struct
from pathlib import Path
import threading

import pybullet as bullet

from .orientation import quaternion
from .sim_native import NativePose, NativeMotion


class CachedSupport(C.Structure):
    _fields_ = [('body', C.c_int32), ('x', C.c_double), ('z', C.c_double), ('height', C.c_double)]


class StepInput(C.Structure):
    _fields_ = [('abi_version', C.c_uint32), ('entity', C.c_uint32),
        ('pose', NativePose), ('road', NativePose), ('motion', NativeMotion),
        ('lean', C.c_double*4), ('effort', C.c_double*3),
        ('distance', C.c_double), ('yaw_delta', C.c_double), ('dt', C.c_double),
        ('displacement', C.c_double*2), ('has_displacement', C.c_uint32),
        ('was_blocked', C.c_uint32), ('constants', C.c_double*8),
        ('support_count', C.c_uint32), ('has_latch', C.c_uint32),
        ('carrying_supported', C.c_uint32), ('supports', CachedSupport*16),
        ('latch', C.c_double*4)]


class StepOutput(C.Structure):
    _fields_ = [('pose', NativePose), ('road', NativePose), ('motion', NativeMotion),
        ('lean', C.c_double*4), ('rest', C.c_double), ('spin', C.c_double*3),
        ('recoil', C.c_double*7), ('moved', C.c_uint32), ('grounded', C.c_uint32),
        ('blocked', C.c_uint32), ('carrying_supported', C.c_uint32),
        ('contact', C.c_int32), ('has_recoil', C.c_uint32), ('event_count', C.c_uint32),
        ('support_count', C.c_uint32), ('supports', CachedSupport*16),
        ('has_latch', C.c_uint32), ('latch', C.c_double*4)]


class SensorState(C.Structure):
    _fields_ = [('entity', C.c_uint32), ('pose', NativePose), ('turret', C.c_double)]


STEP_PACKER = struct.Struct('<II32dII8d')
SENSOR_PACKER = struct.Struct('<I4x7d')


def fixture(world):
    static = []
    for body in sorted(world.map_bodies | world.terrain_bodies):
        key = world.breakable_bodies.get(body)
        static.append(dict(body=body, path=world._mesh_body_paths[body], key=key,
            kind='breakable' if key is not None else 'terrain' if body in world.terrain_bodies else 'map'))
    entities = []
    for entity, pose in world.poses.items():
        if not world._has_suspension(entity):
            raise ValueError(f'Native physics requires separate track colliders for vehicle {entity}')
        assets = world.assets_for(entity)
        parts = []
        proxies = getattr(assets, 'contact_proxies', None)
        if proxies:
            for part in proxies:
                row = dict(name=part.name, kind=part.kind,
                    turret=part.name == assets.turret_node,
                    offset=tuple(assets.definition.aiming.turret_position)
                           if part.name == assets.turret_node else (0., 0., 0.))
                if part.kind == 'prism':
                    row['path'] = assets.mesh_files.mesh(part.vertices)
                elif part.kind == 'box':
                    row.update(center=part.center, half_extents=part.half_extents)
                else:
                    raise ValueError('Unsupported native query proxy')
                parts.append(row)
        else:
            parts = [dict(name=name, kind='prism', path=assets.mesh_files.mesh(vertices),
                turret=name == assets.turret_node,
                offset=tuple(assets.definition.aiming.turret_position)
                       if name == assets.turret_node else (0., 0., 0.))
                for name, vertices in assets.tank_shapes]
        layout = world._support_layout(entity)
        ground = dict(a=layout.a.tolist(), b=layout.b.tolist(), p=layout.p.tolist(),
            weights=layout.weight.tolist(), patch=layout.patch, clearance=world._hull_clearance(entity),
            slope=assets.max_slope, support_span=min(assets.support), bounds=tuple(assets.bounds),
            components=[(low.tolist(), high.tolist()) for _, low, high in world._component_bounds(entity)],
            track_links=list(world.track_links[entity]),
            tracks=[dict(span=(low, high), z=zs.tolist(), y=ys.tolist())
                    for low, high, zs, ys in layout.tracks])
        entities.append(dict(id=entity, body=world.bodies[entity],
            position=pose.position, orientation=quaternion(pose.yaw, pose.pitch, pose.roll),
            parts=parts, ground=ground))
    terrain = world.terrain
    return dict(static=static, entities=entities, terrain=dict(size=terrain.size,
        heights=list(terrain.heights), origin=(terrain.MIN, terrain.MIN_Z),
        dimensions=(terrain.WIDTH, terrain.DEPTH), vertical=(terrain.HEIGHT_MIN, terrain.HEIGHT)))


class NativePhysics:
    def __init__(self, world):
        filename = 'solbreaker_physics_core.dll' if os.name == 'nt' else 'libsolbreaker_physics_core.so'
        path = Path(__file__).with_name('native')/'physics_core'/filename
        self.library = C.CDLL(str(path))
        self.library.solbreaker_physics_abi_sizes.argtypes = []
        self.library.solbreaker_physics_abi_sizes.restype = C.c_uint64
        expected = (C.sizeof(StepInput) << 32) | (C.sizeof(StepOutput) << 16) | C.sizeof(SensorState)
        if (self.library.solbreaker_physics_abi_sizes() != expected
                or STEP_PACKER.size != StepInput.support_count.offset):
            raise RuntimeError('Incompatible native physics ABI')
        self.library.solbreaker_physics_create.argtypes = [C.c_char_p, C.c_char_p]
        self.library.solbreaker_physics_create.restype = C.c_void_p
        self.library.solbreaker_physics_query.argtypes = [C.c_void_p, C.c_char_p, C.c_void_p, C.c_size_t]
        self.library.solbreaker_physics_query.restype = C.c_int32
        self.library.solbreaker_physics_close.argtypes = [C.c_void_p]
        self.library.solbreaker_physics_close.restype = None
        self.library.solbreaker_physics_error.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t]
        self.library.solbreaker_physics_error.restype = C.c_size_t
        self.library.solbreaker_physics_step.argtypes = [C.c_void_p, C.POINTER(StepInput), C.POINTER(StepOutput)]
        self.library.solbreaker_physics_step.restype = C.c_int32
        self.library.solbreaker_physics_sync.argtypes = [C.c_void_p, C.POINTER(SensorState), C.c_size_t]
        self.library.solbreaker_physics_sync.restype = C.c_int32
        self._lock = threading.RLock()
        self._output = C.create_string_buffer(4*1024*1024)
        self.handle = self.library.solbreaker_physics_create(
            json.dumps(fixture(world), allow_nan=False).encode(), str(Path(bullet.__file__).resolve()).encode())
        if not self.handle:
            raise RuntimeError(self.error())
        self.steps = 0
        self._sensors = (SensorState*len(world.poses))()
        self._sensor_states = {}
        try:
            self.sync(world)
        except BaseException:
            self.close()
            raise

    def error(self):
        with self._lock:
            output = C.create_string_buffer(4096)
            self.library.solbreaker_physics_error(self.handle, output, len(output))
            return output.value.decode(errors='replace')

    def query(self, **request):
        with self._lock:
            if not self.handle:
                raise RuntimeError('Native physics world is closed')
            size = self.library.solbreaker_physics_query(self.handle,
                json.dumps(request, allow_nan=False).encode(), self._output, len(self._output))
            if size < 0:
                raise RuntimeError(f'Native physics query failed ({size}): {self.error()}')
            return json.loads(self._output.raw[:size])

    def sync(self, world):
        with self._lock:
            if not self.handle:
                raise RuntimeError('Native physics world is closed')
            if len(world.poses) != len(self._sensors):
                raise RuntimeError('Native physics roster changed')
            changes = {}
            for entity, pose in world.poses.items():
                turret = world.gun_angles[entity][0]
                value = pose, turret
                if self._sensor_states.get(entity) == value:
                    continue
                SENSOR_PACKER.pack_into(self._sensors, len(changes)*SENSOR_PACKER.size, entity,
                    *pose.position, pose.yaw, pose.pitch, pose.roll, turret)
                changes[entity] = value
            if not changes:
                return
            status = self.library.solbreaker_physics_sync(self.handle, self._sensors, len(changes))
            if status:
                raise RuntimeError(f'Native physics sync failed ({status}): {self.error()}')
            self._sensor_states.update(changes)

    def move(self, world, entity, distance, yaw_delta, dt, state, displacement):
        from .collision import Pose
        self.sync(world)
        command = StepInput()
        current, road = world.poses[entity], world._road_poses[entity]
        STEP_PACKER.pack_into(command, 0, 2, entity,
            *current.position, current.yaw, current.pitch, current.roll,
            *road.position, road.yaw, road.pitch, road.roll,
            *state.velocity, *state.suspension_velocity, *state.support_angular_rates,
            *world._lean_states.get(entity, (0.,)*4), *world._drive_efforts.get(entity, (0.,)*3),
            distance, yaw_delta, dt, *(displacement or (0., 0.)),
            int(displacement is not None), int(world._drive_blocked.get(entity, False)),
            world.SKIN, world.STEP, world.GRAVITY,
            world.SUPPORT_REACH, world.SUSPENSION_TRAVEL, world.SUSPENSION_OMEGA,
            world.SUSPENSION_ANGULAR_OMEGA, world._hull_clearance(entity))
        supports = [row for row in world._contact_support_cache.get(entity, ())
                    if row[0] in world.map_bodies and row[0] not in world.breakable_bodies]
        if len(supports) > len(command.supports):
            raise RuntimeError('Invalid native physics support count')
        command.support_count = len(supports)
        for index, row in enumerate(supports):
            command.supports[index] = CachedSupport(*row)
        latch = world._wall_recoil_latches.get(entity)
        command.has_latch = int(latch is not None)
        command.latch[:] = latch if latch is not None else (0.,)*4
        command.carrying_supported = int(world._carrying_supported.get(entity, True))
        output = StepOutput()
        with self._lock:
            if not self.handle:
                raise RuntimeError('Native physics world is closed')
            status = self.library.solbreaker_physics_step(self.handle, C.byref(command), C.byref(output))
            if status:
                raise RuntimeError(f'Native physics step failed ({status}): {self.error()}')
            self.steps += 1
            events = self.query(operation='events') if output.event_count else None
        def pose(value):
            return Pose((value.x, value.y, value.z), value.yaw, value.pitch, value.roll)
        result, road = pose(output.pose), pose(output.road)
        values = (*result.position, result.yaw, result.pitch, result.roll,
            *road.position, road.yaw, road.pitch, road.roll,
            *output.motion.velocity, *output.motion.suspension, *output.motion.angular,
            *output.lean, output.rest, *output.spin, *output.recoil)
        if not all(math.isfinite(value) for value in values):
            raise RuntimeError('Nonfinite native physics result')
        if output.support_count > 16 or output.contact not in range(4):
            raise RuntimeError('Invalid native physics result metadata')
        if events:
            for event in events['destroyed']:
                key = event['key']
                world.destroy(tuple(key) if isinstance(key, list) else key, event['direction'])
            for event in events['contacts']:
                value = event['pose']
                for point in event['points']:
                    for index in (5,6,7):
                        point[index] = tuple(point[index])
                world._record_ground_contacts(event['entity'], event['points'],
                    Pose(tuple(value[:3]), *value[3:]))
        state.velocity = tuple(output.motion.velocity)
        state.suspension_velocity = tuple(output.motion.suspension)
        state.support_angular_rates = tuple(output.motion.angular)
        state.grounded = bool(output.grounded)
        world._lean_states[entity] = tuple(output.lean)
        world._road_poses[entity] = road
        world._drive_blocked[entity] = bool(output.blocked)
        world._carrying_supported[entity] = bool(output.carrying_supported)
        world._contact_support_cache[entity] = [(row.body, row.x, row.z, row.height)
            for row in output.supports[:output.support_count]]
        if output.has_latch:
            world._wall_recoil_latches[entity] = tuple(output.latch)
        else:
            world._wall_recoil_latches.pop(entity, None)
        world.last_contact = (None, 'map', 'tank', 'slope_or_border')[output.contact]
        if output.has_recoil:
            nx, nz, closing, rebound, vx, vy, vz = output.recoil
            world.last_wall_recoil = dict(entity=entity, normal=(nx,nz),
                closing_speed=closing, rebound_speed=rebound, velocity=(vx,vy,vz))
        if not state.grounded:
            world._begin_fall(entity, result)
            world.short_flight_spins[entity] = tuple(output.spin)
            world._support_memory.pop(entity, None)
        world.poses[entity] = result
        world.set_pose(world.bodies[entity], result)
        self._sensor_states[entity] = result, world.gun_angles[entity][0]
        return result, bool(output.moved), output.rest

    def close(self):
        with self._lock:
            if self.handle:
                self.library.solbreaker_physics_close(self.handle)
                self.handle = None
