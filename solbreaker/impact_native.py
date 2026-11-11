"""Optional isolated Rust ground-impact solver with a Python fallback."""
import ctypes as C
import os
from pathlib import Path
import sys

import numpy as np

from .sim_native import DEFAULT_BACKEND


class GroundVehicle(C.Structure):
    _fields_ = [('entity',C.c_uint32),('changed',C.c_uint32),('state',C.c_double*3)]


class GroundPair(C.Structure):
    _fields_ = [('a',C.c_uint32),('b',C.c_uint32),
                ('point_a',C.c_double*3),('point_b',C.c_double*3),
                ('normal',C.c_double*3),('separation',C.c_double),
                ('was_touching',C.c_uint32),('mass_a',C.c_double),('mass_b',C.c_double),
                ('inertia_a',C.c_double),('inertia_b',C.c_double),
                ('center_a',C.c_double*3),('center_b',C.c_double*3)]


class GroundParams(C.Structure):
    _fields_ = [(name,C.c_double) for name in
                ('restitution','friction','min_closing','ram_min')]


class GroundImpact(C.Structure):
    _fields_ = [('entity_a',C.c_uint32),('entity_b',C.c_uint32),
                ('point_a',C.c_double*3),('point_b',C.c_double*3),
                ('normal',C.c_double*2),('separation',C.c_double),
                ('closing_speed',C.c_double),('normal_impulse',C.c_double),
                ('tangent_impulse',C.c_double),('mass_a',C.c_double),('mass_b',C.c_double),
                ('velocity_a',C.c_double*2),('velocity_b',C.c_double*2),
                ('yaw_rate_a',C.c_double),('yaw_rate_b',C.c_double),('ram',C.c_uint32),
                ('ram_energy',C.c_double),('ram_energy_a',C.c_double),('ram_energy_b',C.c_double)]


_LIBRARY = None
_LOAD_ATTEMPTED = False


def library_path():
    suffix = '.dll' if os.name == 'nt' else '.dylib' if sys.platform == 'darwin' else '.so'
    return Path(__file__).with_name('native')/'impact_core'/('tfs_impact_core'+suffix)


def load_library():
    global _LIBRARY,_LOAD_ATTEMPTED
    if _LOAD_ATTEMPTED:
        return _LIBRARY
    _LOAD_ATTEMPTED = True
    path = library_path()
    if not path.is_file():
        return None
    try:
        library = C.CDLL(str(path))
        library.tfs_ground_impacts.argtypes = [C.c_uint32,C.POINTER(GroundVehicle),
            C.c_uint32,C.POINTER(GroundPair),C.POINTER(GroundParams),
            C.POINTER(GroundImpact),C.POINTER(C.c_uint32)]
        library.tfs_ground_impacts.restype = C.c_int32
        _LIBRARY = library
    except (OSError,AttributeError):
        _LIBRARY = None
    return _LIBRARY


def try_ground_impacts(world,state,pairs):
    mode = os.environ.get('SOLBREAKER_SIM_IMPACTS','python')
    if mode not in ('python','rust'):
        raise ValueError('SOLBREAKER_SIM_IMPACTS must be python or rust')
    if mode != 'rust' or os.environ.get('SOLBREAKER_SIM_BACKEND',DEFAULT_BACKEND) not in ('rust','auto'):
        return None
    library = load_library()
    if library is None:
        return None
    filtered = []
    touching = set()
    for (a,b),contact in pairs:
        if (a in world.dynamic_bodies or b in world.dynamic_bodies
                or a in world.pending_dynamics or b in world.pending_dynamics
                or not world.motion[a].grounded or not world.motion[b].grounded):
            continue
        touching.add((a,b))
        if a not in state or b not in state:
            return None
        filtered.append(((a,b),contact))
    if not filtered:
        return {},[],touching
    entities = sorted({entity for (a,b),_ in filtered for entity in (a,b)})
    if len(entities) > 128 or len(filtered) > 512:
        return None
    indices = {entity:index for index,entity in enumerate(entities)}
    from .orientation import matrix
    vehicles = (GroundVehicle*len(entities))()
    for index,entity in enumerate(entities):
        velocity,yaw_rate = state[entity]
        vehicles[index] = GroundVehicle(entity,0,(*velocity,yaw_rate))
    def geometry(entity,pose):
        props = world.ground_mass_properties[entity]
        rotation = matrix(pose.yaw,pose.pitch,pose.roll)
        center = np.asarray(pose.position,dtype=float) + rotation @ np.asarray(
            props.center_of_mass,dtype=float)
        inertia = np.asarray(props.inertia_diagonal,dtype=float)
        inertia_y = float(np.dot(rotation[1]*rotation[1],inertia))
        return props.mass,inertia_y,tuple(center)
    native_pairs = (GroundPair*len(filtered))()
    for index,((a,b),contact) in enumerate(filtered):
        mass_a,inertia_a,center_a = geometry(a,contact['pose_a'])
        mass_b,inertia_b,center_b = geometry(b,contact['pose_b'])
        native_pairs[index] = GroundPair(indices[a],indices[b],tuple(contact['point_a']),
            tuple(contact['point_b']),tuple(contact['normal']),contact['separation'],
            (a,b) in world.ground_contact_pairs,mass_a,mass_b,inertia_a,inertia_b,
            center_a,center_b)
    params = GroundParams(world.GROUND_IMPACT_RESTITUTION,world.GROUND_IMPACT_FRICTION,
        world.GROUND_IMPACT_MIN_CLOSING_SPEED,world.RAM_MIN_CLOSING_SPEED)
    impacts = (GroundImpact*len(filtered))()
    count = C.c_uint32()
    status = library.tfs_ground_impacts(len(entities),vehicles,len(filtered),native_pairs,
        C.byref(params),impacts,C.byref(count))
    if status != 0 or count.value > len(filtered):
        return None
    def vector(values):
        return tuple(np.float64(value) for value in values)
    updates = {vehicle.entity:(vector(vehicle.state[:2]),np.float64(vehicle.state[2]))
               for vehicle in vehicles if vehicle.changed}
    events = [dict(entity_a=impact.entity_a,entity_b=impact.entity_b,
        point_a=tuple(impact.point_a),point_b=tuple(impact.point_b),
        normal=tuple(impact.normal),separation=impact.separation,
        closing_speed=impact.closing_speed,
        normal_impulse=np.float64(impact.normal_impulse),
        tangent_impulse=np.float64(impact.tangent_impulse),
        mass_a=impact.mass_a,mass_b=impact.mass_b,
        mass_model='aabb_uniform',velocity_a=vector(impact.velocity_a),
        velocity_b=vector(impact.velocity_b),yaw_rate_a=np.float64(impact.yaw_rate_a),
        yaw_rate_b=np.float64(impact.yaw_rate_b),ram=bool(impact.ram),
        ram_energy=np.float64(impact.ram_energy),
        ram_energy_a=np.float64(impact.ram_energy_a),
        ram_energy_b=np.float64(impact.ram_energy_b))
        for impact in impacts[:count.value]]
    native = getattr(world,'_rust_drive',None)
    if native is not None:
        native.stats['ground_impact_batches'] += 1
    return updates,events,touching
