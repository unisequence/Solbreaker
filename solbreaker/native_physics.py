"""Native ground physics owner, with setup and result marshalling."""
import ctypes as C
import json
import math
import struct
import sys
from pathlib import Path
import threading

import numpy as np
import pybullet as bullet

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
        ('latch', C.c_double*4), ('turret_yaw', C.c_double)]


class StepOutput(C.Structure):
    _fields_ = [('pose', NativePose), ('road', NativePose), ('motion', NativeMotion),
        ('lean', C.c_double*4), ('rest', C.c_double), ('spin', C.c_double*3),
        ('recoil', C.c_double*7), ('moved', C.c_uint32), ('grounded', C.c_uint32),
        ('blocked', C.c_uint32), ('carrying_supported', C.c_uint32),
        ('contact', C.c_int32), ('has_recoil', C.c_uint32), ('event_count', C.c_uint32),
        ('support_count', C.c_uint32), ('supports', CachedSupport*16),
        ('has_latch', C.c_uint32), ('latch', C.c_double*4)]


STEP_PACKER = struct.Struct('<II32dII8d')


def fixture(world):
    from .collision import _RAY_GROUP, _STATIC_GROUP
    static = []
    for body in sorted(world.map_bodies | world.terrain_bodies):
        key = world.breakable_bodies.get(body)
        static.append(dict(body=body, path=world._mesh_body_paths[body], key=key,
            kind='breakable' if key is not None else 'terrain' if body in world.terrain_bodies else 'map'))
    entities = [vehicle_fixture(world, entity) for entity in world.poses]
    terrain = world.terrain
    static_mask = _RAY_GROUP  # Restore a breakable filter if a native step fails.
    zone = dict(zone=tuple(world.collision_zone)) if world.collision_zone else {}
    return dict(static=static, entities=entities, **zone, static_group=_STATIC_GROUP, static_mask=static_mask,
        terrain=dict(size=terrain.size, origin=(terrain.MIN, terrain.MIN_Z),
            dimensions=(terrain.WIDTH, terrain.DEPTH), vertical=(terrain.HEIGHT_MIN, terrain.HEIGHT)))


def height_grid(terrain):
    """Expose contiguous native-endian uint16 samples for synchronous attachment."""
    heights = np.asarray(terrain.heights)
    if (not 1 <= terrain.size <= 8192 or heights.ndim != 1
            or heights.size != terrain.size * terrain.size or heights.dtype.kind not in 'iu'):
        raise ValueError('Invalid terrain grid')
    if heights.dtype != np.uint16 and (heights.min() < 0 or heights.max() > 65535):
        raise ValueError('Invalid terrain height')
    return np.require(heights, dtype=np.uint16, requirements=['C', 'A'])


def vehicle_fixture(world, entity):
    if not world._has_suspension(entity):
        raise ValueError(f'Native physics requires separate track colliders for vehicle {entity}')
    assets = world.assets_for(entity)
    layout = world._support_layout(entity)
    ground = dict(a=layout.a.tolist(), b=layout.b.tolist(), p=layout.p.tolist(),
        weights=layout.weight.tolist(), patch=layout.patch, clearance=world._hull_clearance(entity),
        slope=assets.max_slope, support_span=min(assets.support), bounds=tuple(assets.bounds),
        components=[(low.tolist(), high.tolist()) for _, low, high in world._component_bounds(entity)],
        track_links=list(world.track_links[entity]),
        tracks=[dict(span=(low, high), z=zs.tolist(), y=ys.tolist())
                for low, high, zs, ys in layout.tracks])
    body = world.bodies[entity]
    return dict(id=entity, body=body, links=bullet.getNumJoints(body, physicsClientId=world.cid), ground=ground)


def library_path():
    filename = ('solbreaker_physics_core.dll' if sys.platform == 'win32'
                else 'libsolbreaker_physics_core.so')
    return Path(__file__).with_name('native')/'physics_core'/filename


def load_library():
    """Load the required ground controller and validate its complete public ABI."""
    path = library_path()
    try:
        # PyBullet 3.2.7 holds the GIL for DIRECT commands and disconnect.
        # Keep that same exclusion while probing real bodies through ctypes.
        library = C.PyDLL(str(path))
        library.solbreaker_physics_abi_sizes.argtypes = []
        library.solbreaker_physics_abi_sizes.restype = C.c_uint64
        expected = (C.sizeof(StepInput) << 32) | C.sizeof(StepOutput)
        if (library.solbreaker_physics_abi_sizes() != expected
                or STEP_PACKER.size != StepInput.support_count.offset):
            raise RuntimeError('Incompatible native physics ABI')
        signatures = {
            'attach_grid': ([C.c_int32, C.c_char_p, C.c_char_p, C.POINTER(C.c_uint16), C.c_size_t], C.c_void_p),
            'session_live': ([C.c_int32], C.c_bool),
            'sensor_enabled': ([C.c_void_p, C.c_uint32, C.c_bool], C.c_int32),
            'query': ([C.c_void_p, C.c_char_p, C.c_void_p, C.c_size_t], C.c_int32),
            'close': ([C.c_void_p], None),
            'error': ([C.c_void_p, C.c_void_p, C.c_size_t], C.c_size_t),
            'step': ([C.c_void_p, C.POINTER(StepInput), C.POINTER(StepOutput)], C.c_int32),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(library, 'solbreaker_physics_'+name)
            function.argtypes, function.restype = arguments, result
    except (OSError, AttributeError, RuntimeError) as error:
        raise RuntimeError(f'Native ground physics library missing or incompatible: {path}. '
                           'Run python tools/build_native.py or install a complete native package. '
                           f'Cause: {error}') from error
    return library


class NativePhysics:
    def __init__(self, world):
        self.library = load_library()
        self._lock = threading.RLock()
        self._output = C.create_string_buffer(4*1024*1024)
        self.steps = 0
        self._inside_zone = ()
        self._limits = {}
        self.cid = world.cid
        self.handle = None
        grid = height_grid(world.terrain)
        payload = json.dumps(fixture(world), allow_nan=False).encode()
        path = library_path().resolve()
        # LoadLibraryA in PyBullet 3.2.7 accepts the ASCII module name when
        # ctypes has already loaded that module via its absolute Unicode path.
        plugin_path = path.name if sys.platform == 'win32' and not str(path).isascii() else str(path)
        self.plugin = bullet.loadPlugin(plugin_path, physicsClientId=self.cid)
        if self.plugin < 0:
            raise RuntimeError('Could not load the native physics plugin into the Bullet arena')
        self.token = bullet.executePluginCommand(self.plugin, physicsClientId=self.cid)
        if self.token <= 0:
            raise RuntimeError('Bullet arena already has a native physics owner')
        try:
            self.handle = self.library.solbreaker_physics_attach_grid(
                self.token, payload, str(Path(bullet.__file__).resolve()).encode(),
                grid.ctypes.data_as(C.POINTER(C.c_uint16)), grid.size)
            if not self.handle:
                raise RuntimeError(self.error())
        except BaseException:
            if self.arena_alive():
                bullet.unloadPlugin(self.plugin, physicsClientId=self.cid)
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
            if not 0 <= size <= len(self._output):
                raise RuntimeError(f'Native physics query failed ({size}): {self.error()}')
            # ctypes.raw copies the entire 4 MiB buffer even for a small reply.
            return json.loads(C.string_at(self._output, size))

    def replace_vehicle(self, world, entity):
        self.query(operation='replace_vehicle', vehicle=vehicle_fixture(world, entity))
        self._limits.pop(entity, None)

    def set_sensor_enabled(self, entity, enabled):
        with self._lock:
            if not self.handle:
                raise RuntimeError('Native physics world is closed')
            status = self.library.solbreaker_physics_sensor_enabled(self.handle, entity, enabled)
            if status:
                raise RuntimeError(f'Native sensor update failed ({status}): {self.error()}')

    def move(self, world, entity, distance, yaw_delta, dt, state, displacement):
        from .collision import Pose
        if world.collision_zone is not None:
            inside = tuple(world.bodies[e] for e, pose in world.poses.items()
                           if world.collision_zone(pose.position))
            if inside != self._inside_zone:
                self.query(operation='zone_members', bodies=inside)
                self._inside_zone = inside
        if world.movement_circle or world.open_map_borders or entity in self._limits:
            policy = {}
            if entity in world.open_map_borders:
                policy['bounds'] = world.sandbox_terrain_bounds()
            circle = world.movement_circle
            if circle and circle[0] == entity:
                policy['circle'] = circle[1:]
            if policy != self._limits.get(entity, {}):
                self.query(operation='movement_limits', entity=entity, **policy)
                if policy:
                    self._limits[entity] = policy
                else:
                    del self._limits[entity]
        command = StepInput()
        current, road = world.poses[entity], world._road_poses[entity]
        STEP_PACKER.pack_into(command, 0, 3, entity,
            *current.position, current.yaw, current.pitch, current.roll,
            *road.position, road.yaw, road.pitch, road.roll,
            *state.velocity, *state.suspension_velocity, *state.support_angular_rates,
            *world._lean_states.get(entity, (0.,)*4), *world._drive_efforts.get(entity, (0.,)*3),
            distance, yaw_delta, dt, *(displacement or (0., 0.)),
            int(displacement is not None), int(world._drive_blocked.get(entity, False)),
            world.SKIN, world.STEP, world.GRAVITY,
            world.SUPPORT_REACH, world.SUSPENSION_TRAVEL, world.SUSPENSION_OMEGA,
            world.SUSPENSION_ANGULAR_OMEGA, world._hull_clearance(entity))
        command.turret_yaw = world.gun_angles[entity][0]
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
        return result, bool(output.moved), output.rest

    def arena_alive(self):
        # Unlike PyBullet's small integer CID, this token is never recycled.
        return self.library.solbreaker_physics_session_live(self.token)

    def close(self):
        # The arena retains its identity plugin until Bullet disconnects.
        # Dropping an individual controller releases only its own Rust state.
        with self._lock:
            if self.handle:
                self.library.solbreaker_physics_close(self.handle)
                self.handle = None
