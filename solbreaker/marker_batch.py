"""Frame-local aim queries sharing preparation and the existing armor ABI.

No geometry, numerical result or callback is cached between motion frames.
Full projectile/armor queries continue through their existing implementation.
"""
import ctypes as C
import numpy as np

from . import armor, armor_native as native
from .collision import CollisionWorld
from .aiming import GunAim
from .orientation import matrix

_WORLD = {name: getattr(CollisionWorld, name) for name in (
    'shot_path', '_armor_state_snapshot_map', '_dynamic_rotation',
    'assets_for', '_static_ray', '_static_rays')}
_INTERSECTIONS = armor.ArmorModel.intersections
_PARTS = armor.ArmorModel._parts_for_query
_ROTATION = armor._validated_rotation
_QUERY = native.query
_INVOKE = native._invoke
_MATRIX = armor.matrix
_CROSS = armor._ARMOR_NATIVE_CROSS
_GUN = {name: getattr(GunAim, name) for name in ('ray', 'ray_rotation')}


def try_markers(arena, vehicles, arena_methods):
    """Return complete exact markers, or None to retain the original path."""
    world = arena.world
    if (type(world) is not CollisionWorld or len(vehicles) < 2
            or any(getattr(getattr(arena, name), '__func__', None) is not source
                   for name, source in arena_methods.items())
            or any(getattr(getattr(world, name), '__func__', None) is not source
                   for name, source in _WORLD.items())
            or armor.matrix is not _MATRIX or armor._validated_rotation is not _ROTATION
            or armor._cross3 is not _CROSS or native.query is not _QUERY
            or native._invoke is not _INVOKE or not native._functions_unchanged()):
        return None
    if any(type(v.gun) is not GunAim
           or any(getattr(getattr(v.gun, name), '__func__', None) is not source
                  for name, source in _GUN.items()) for v in vehicles):
        return None
    if getattr(world, '_rapier', None) is not None and world._rapier_query_backend != 'bullet':
        return None
    lib = native.load_library()
    if lib is None:
        return None
    fn = lib.tfs_armor_query
    if (fn is not lib._query_source or lib._query_call is not fn
            or fn.argtypes is None or tuple(fn.argtypes) != lib._query_signature
            or fn.restype is not C.c_int32 or getattr(fn, 'errcheck', None) is not None):
        return None
    movable = {entity: target.movable_armor_state
               for entity, target in arena.vehicles.items()
               if target.movable_armor_state is not None}
    snapshots = world._armor_state_snapshot_map(movable or None)
    prepared = {}
    queries = []
    descriptors = []
    # Static map queries stay in their original ray order. Armor transforms
    # are pure, so preparing these inputs first does not change the map world.
    rays=[]
    for vehicle in vehicles:
        origin,direction=arena.shot_ray(vehicle)
        distance=arena.shell_for(vehicle).max_distance
        end=tuple(o+d*distance for o,d in zip(origin,direction))
        hit=world._static_ray(origin,end)
        limit=distance*hit[2] if hit[0]>=0 else distance
        rays.append((origin,direction,end,hit,limit))
    targets=list(world.poses.items())
    sphere_rows=[]
    for entity,pose in targets:
        model=world.assets_for(entity).armor
        if (type(model) is not armor.ArmorModel or type(model.radius) not in (float,np.float64)
                or type(pose.position) is not tuple
                or any(type(value) is not float for value in pose.position)):
            sphere_rows=[];break
        sphere_rows.append((*pose.position,(model.radius+1e-5)**2))
    coarse=None
    if sphere_rows:
        ray_rows=np.array([(*origin,*direction,limit) for origin,direction,_,_,limit in rays],dtype=float)
        sphere_rows=np.array(sphere_rows,dtype=float)
        coarse=native.marker_spheres(lib,ray_rows,sphere_rows)
    # Model validation is performed once, after the same sphere rejection as
    # intersections. Off-ray models never pay native wrapper guards.
    for ray_index,vehicle in enumerate(vehicles):
        origin,direction,end,hit,limit=rays[ray_index]
        query_index = len(queries)
        queries.append([vehicle, hit, end, None])
        for target_index,(entity,pose) in enumerate(targets):
            if entity == vehicle.entity_id:
                continue
            model = world.assets_for(entity).armor
            if (type(model) is not armor.ArmorModel
                    or getattr(model.intersections, '__func__', None) is not _INTERSECTIONS
                    or getattr(model._parts_for_query, '__func__', None) is not _PARTS):
                return None
            entry = prepared.get(entity)
            if entry is None:
                parts = tuple(model._parts_for_query(snapshots.get(entity)))
                entry = prepared[entity] = [parts, None]
            parts = entry[0]
            if coarse is not None and not coarse[ray_index,target_index]:
                continue
            delta = tuple(c-o for c, o in zip(pose.position, origin))
            length2 = sum(d*d for d in direction)
            if length2 <= 1e-16:
                continue
            t = max(0., min(limit, sum(c*d for c, d in zip(delta, direction))/length2))
            nearest2 = sum((c-d*t)**2 for c, d in zip(delta, direction))
            if nearest2 > (model.radius+1e-5)**2:
                continue
            if entry[1] is None:
                if not 1 <= len(parts) <= 64:
                    return None
                for name, triangles, ids, thickness, factors in parts:
                    if (type(name) is not str or type(triangles) is not np.ndarray
                            or triangles.dtype != native._F64 or triangles.ndim != 3
                            or triangles.shape[1:] != (3, 3) or not len(triangles)
                            or not triangles.flags.c_contiguous or not triangles.flags.aligned
                            or len(triangles) > 65536):
                        return None
                rot = armor._validated_rotation(pose, world._dynamic_rotation(entity))
                yaw, pitch = world.gun_angles[entity]
                turret_rot = matrix(yaw, 0, 0)
                transforms = []
                for name, triangles, *_ in parts:
                    if name.startswith('turret'):
                        local_rot, offset = turret_rot, model.turret_position
                    elif name.startswith('gun'):
                        local_rot = turret_rot @ matrix(0, pitch, 0)
                        offset = model.turret_position + turret_rot @ model.gun_position
                    else:
                        local_rot, offset = np.eye(3), np.zeros(3)
                    transforms.append((local_rot, offset))
                entry[1] = rot, transforms
            rot, transforms = entry[1]
            local_origin = (np.array(origin)-pose.position) @ rot
            local_direction = np.asarray(direction) @ rot
            pair = (query_index, local_origin, local_direction, rot, pose.position)
            for part, (local_rot, offset) in zip(parts, transforms):
                triangles = part[1]
                o = (local_origin-offset) @ local_rot
                d = local_direction @ local_rot
                descriptors.append((triangles, o, d, limit, pair))
    # Respect the unchanged ABI's part and triangle bounds. Each descriptor
    # owns its triangles through the synchronous call; chunks never share output.
    offset = 0
    calls = 0
    while offset < len(descriptors):
        stop = offset
        total = 0
        while stop < len(descriptors) and stop-offset < 64:
            n = len(descriptors[stop][0])
            if total+n > 65536:
                break
            total += n
            stop += 1
        chunk = descriptors[offset:stop]
        request = (native.Part*len(chunk))()
        for i, (triangles, o, d, limit, _) in enumerate(chunk):
            native._PART_PACK.pack_into(request, i*72, triangles.ctypes.data,
                                        len(triangles), *o, *d, limit)
        rows = native._invoke(lib, request, total)
        if rows is None:
            return None
        calls += 1
        for part_index, triangle, distance in rows:
            pair = chunk[part_index][4]
            query = queries[pair[0]]
            if query[3] is None or distance < query[3][0]:
                query[3] = distance, pair
        offset = stop
    result = {}
    for vehicle, hit, end, closest in queries:
        if closest is None:
            point = tuple(hit[3]) if hit[0] >= 0 else end
        else:
            distance, (_, origin, direction, rot, position) = closest
            point = tuple(map(float, (origin+direction*distance) @ rot.T+position))
        _, direction = arena.shot_ray(vehicle)
        result[vehicle.entity_id] = point, direction
    stats = getattr(arena, 'marker_batch_stats', None)
    if stats is None:
        stats = arena.marker_batch_stats = dict(frames=0, rays=0, native_calls=0)
    stats['frames'] += 1
    stats['rays'] += len(vehicles)
    stats['native_calls'] += calls
    if coarse is not None:
        stats['coarse_frames']=stats.get('coarse_frames',0)+1
        stats['coarse_rejected']=stats.get('coarse_rejected',0)+int(coarse.size-coarse.sum())
    return result
