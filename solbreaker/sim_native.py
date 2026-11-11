"""Rust grounded-drive kernel, with the accepted Bullet queries as callbacks.

No native proposal mutates authoritative drive caches. The owner applies a
complete successful result, or restores its sensor and uses the Python solver.
"""
from collections import Counter
import ctypes as C
import math
import os
from pathlib import Path
import sys
import struct

import numpy as np
try:
    from . import _support_checks
except (ImportError,OSError):
    _support_checks=None


ABI_VERSION = 1
DEFAULT_BACKEND = 'auto'
DoublePtr = C.POINTER(C.c_double)


class NativePose(C.Structure):
    _fields_ = [(name,C.c_double) for name in ('x','y','z','yaw','pitch','roll')]


class NativeMotion(C.Structure):
    _fields_ = [('velocity',C.c_double*3),('suspension',C.c_double*3),
                ('angular',C.c_double*2)]


class NativeTerrain(C.Structure):
    _fields_ = [('heights',C.POINTER(C.c_uint16)),('size',C.c_uint32),
                *[(name,C.c_double) for name in
                  ('min_x','min_z','width','depth','height_min','height_range')]]


class DriveInput(C.Structure):
    _fields_ = [('abi_version',C.c_uint32),('count',C.c_uint32),
                ('pose',NativePose),('road',NativePose),('motion',NativeMotion),
                ('lean',C.c_double*4),('effort',C.c_double*3),
                ('distance',C.c_double),('yaw_delta',C.c_double),('dt',C.c_double),
                ('displacement',C.c_double*2),
                *[(name,C.c_uint32) for name in
                  ('has_displacement','was_blocked','has_memory','hint_count')],
                ('hints',C.c_uint32*3),
                *[(name,DoublePtr) for name in
                  ('a','b','p','weight','memory_heights','memory_speeds')],
                ('constants',C.c_double*8),('terrain',NativeTerrain)]


class DriveOutput(C.Structure):
    _fields_ = [('pose',NativePose),('road',NativePose),('motion',NativeMotion),
                ('lean',C.c_double*4),
                *[(name,C.c_uint32) for name in
                  ('moved','blocked','carrying_supported','hint_count')],
                ('hints',C.c_uint32*3),('spring_pose',NativePose),('bounds_pose',NativePose),
                ('spring_velocity',C.c_double*3),('spring_lean',C.c_double*4),
                ('spring_angular',C.c_double*2),
                *[(name,DoublePtr) for name in
                  ('memory_heights','memory_speeds','lower_bounds')],
                ('reason',C.c_uint32),('support_queries',C.c_uint32),
                ('validation_queries',C.c_uint32)]


class SupportSliceInput(C.Structure):
    _fields_=[('x',C.c_double),('z',C.c_double),('yaw',C.c_double),
        ('reference',NativePose),('start',NativePose),('road',NativePose),
        ('motion',NativeMotion),('lean',C.c_double*4),('h',C.c_double),('centre',C.c_double),
        ('hint_count',C.c_uint32),('hints',C.c_uint32*3)]


class SupportSliceOutput(C.Structure):
    _fields_=[('candidate',NativePose),('road',NativePose),
        ('spring_velocity',C.c_double*3),('spring_lean',C.c_double*4),('spring_angular',C.c_double*2),
        ('hint_count',C.c_uint32),('hints',C.c_uint32*3),('stage',C.c_uint32),
        *[(name,DoublePtr) for name in ('lower','xs','zs','ground')]]


_SUPPORT_SLICE_PACKER=struct.Struct('@35d4I')
_SUPPORT_SLICE_RESULT=struct.Struct('@21d')
_SUPPORT_SLICE_POINTERS=struct.Struct('@4P')
if (_SUPPORT_SLICE_PACKER.size!=C.sizeof(SupportSliceInput)
        or SupportSliceInput.reference.offset!=24 or SupportSliceInput.motion.offset!=168
        or SupportSliceInput.hint_count.offset!=280 or C.sizeof(SupportSliceOutput)!=224
        or SupportSliceOutput.lower.offset!=192):
    _SUPPORT_SLICE_PACKER=None


class MeasuredSupportInput(C.Structure):
    _fields_ = [(name,C.c_uint32) for name in
                ('abi','count','extra_count','carrying','full_rank','supported')]+[
                (name,DoublePtr) for name in
                ('a','b','p','weights','xs','zs','ground','extra')]+[
                ('old_position',C.c_double*3),('up',C.c_double*3)]+[
                (name,C.c_double) for name in
                ('compression','reach','skin','centre','clearance')]+[
                ('hint_count',C.c_uint32),('hints',C.c_uint32*3)]


class MeasuredSupportOutput(C.Structure):
    _fields_ = [('plane',C.c_double*3),('active',C.c_uint32*3),
                ('active_count',C.c_uint32),('supported',C.c_uint32),('lower',DoublePtr)]


def _measured_request_packer():
    """Use native packing only when every field matches the ctypes ABI."""
    packer=struct.Struct('@6I8P11d4I')
    pointer_size=C.sizeof(C.c_void_p)
    vectors=24+8*pointer_size
    expected={name:4*i for i,name in enumerate(
        ('abi','count','extra_count','carrying','full_rank','supported'))}
    expected.update({name:24+pointer_size*i for i,name in enumerate(
        ('a','b','p','weights','xs','zs','ground','extra'))})
    expected.update(old_position=vectors,up=vectors+24,
        compression=vectors+48,reach=vectors+56,skin=vectors+64,
        centre=vectors+72,clearance=vectors+80,hint_count=vectors+88,hints=vectors+92)
    if (packer.size!=C.sizeof(MeasuredSupportInput)
            or any(getattr(MeasuredSupportInput,name).offset!=offset
                   for name,offset in expected.items())):
        return None
    return packer


_MEASURED_PACKER = _measured_request_packer()


def _drive_request_packers():
    if C.sizeof(C.c_void_p)!=8:
        return None
    header=struct.Struct('@2I32d7I')
    tail=struct.Struct('@2P8d')
    expected=dict(abi_version=0,count=4,pose=8,road=56,motion=104,
        lean=168,effort=200,distance=224,yaw_delta=232,dt=240,
        displacement=248,has_displacement=264,was_blocked=268,
        has_memory=272,hint_count=276,hints=280,a=296,b=304,p=312,
        weight=320,memory_heights=328,memory_speeds=336,constants=344,terrain=408)
    if (C.sizeof(DriveInput)!=472 or header.size!=292 or tail.size!=80
            or any(getattr(DriveInput,k).offset!=v for k,v in expected.items())
            or C.sizeof(NativePose)!=48 or C.sizeof(NativeMotion)!=64
            or C.sizeof(NativeTerrain)!=64
            or any(getattr(NativePose,k).offset!=8*i for i,k in enumerate(
                ('x','y','z','yaw','pitch','roll')))
            or any(getattr(NativeMotion,k).offset!=v for k,v in
                   (('velocity',0),('suspension',24),('angular',48)))
            or any(getattr(NativeTerrain,k).offset!=v for k,v in
                (('heights',0),('size',8),('min_x',16),('min_z',24),('width',32),
                 ('depth',40),('height_min',48),('height_range',56)))):
        return None
    expected_types=[C.c_uint32,C.c_uint32,NativePose,NativePose,NativeMotion,
        C.c_double*4,C.c_double*3,*([C.c_double]*3),C.c_double*2,
        *([C.c_uint32]*4),C.c_uint32*3,*([DoublePtr]*6),C.c_double*8,NativeTerrain]
    if ([kind for _,kind in DriveInput._fields_]!=expected_types
            or any(kind is not C.c_double for _,kind in NativePose._fields_)
            or [kind for _,kind in NativeMotion._fields_]
                !=[C.c_double*3,C.c_double*3,C.c_double*2]
            or [kind for _,kind in NativeTerrain._fields_]
                !=[C.POINTER(C.c_uint16),C.c_uint32,*([C.c_double]*6)]):
        return None
    return header,tail


_DRIVE_PACKERS = _drive_request_packers()


def _output_pointer_packers():
    if C.sizeof(C.c_void_p)!=8:
        return None,None
    expected = dict(pose=0,road=48,motion=96,lean=160,moved=192,blocked=196,
        carrying_supported=200,hint_count=204,hints=208,spring_pose=224,
        bounds_pose=272,spring_velocity=320,spring_lean=344,spring_angular=376,
        memory_heights=392,memory_speeds=400,lower_bounds=408,reason=416,
        support_queries=420,validation_queries=424)
    drive_types = [NativePose,NativePose,NativeMotion,C.c_double*4,
        *([C.c_uint32]*4),C.c_uint32*3,NativePose,NativePose,
        C.c_double*3,C.c_double*4,C.c_double*2,*([DoublePtr]*3),*([C.c_uint32]*3)]
    drive = (struct.Struct('@3P') if C.sizeof(DriveOutput)==432
        and [kind for _,kind in DriveOutput._fields_]==drive_types
        and all(getattr(DriveOutput,k).offset==v for k,v in expected.items()) else None)
    measured = (struct.Struct('@P') if C.sizeof(MeasuredSupportOutput)==56
        and [kind for _,kind in MeasuredSupportOutput._fields_]
            ==[C.c_double*3,C.c_uint32*3,C.c_uint32,C.c_uint32,DoublePtr]
        and all(getattr(MeasuredSupportOutput,k).offset==v for k,v in
            (('plane',0),('active',24),('active_count',36),('supported',40),('lower',48)))
        else None)
    return drive,measured


_DRIVE_OUTPUT_PACKER,_MEASURED_OUTPUT_PACKER = _output_pointer_packers()



SampleCallback = C.CFUNCTYPE(C.c_int32,C.c_void_p,C.c_uint32,DoublePtr,DoublePtr,
                             C.POINTER(NativePose),DoublePtr)
SamplePlanCallback = C.CFUNCTYPE(C.c_int32,C.c_void_p,C.c_uint32,DoublePtr,DoublePtr,
    C.POINTER(NativePose),DoublePtr,DoublePtr,DoublePtr,DoublePtr,
    C.POINTER(C.c_uint32),C.c_uint32,DoublePtr)
ValidateCallback = C.CFUNCTYPE(C.c_int32,C.c_void_p,C.POINTER(NativePose),
                               C.POINTER(NativePose))
_LIBRARY = None
_LOAD_ATTEMPTED = False


def library_path():
    suffix = '.dll' if sys.platform == 'win32' else '.dylib' if sys.platform == 'darwin' else '.so'
    return Path(__file__).with_name('native')/'sim_core'/('tfs_sim_core'+suffix)


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
        for name in ('tfs_sim_abi_version','tfs_sim_input_size','tfs_sim_output_size'):
            function = getattr(library,name)
            function.argtypes = []
            function.restype = C.c_uint32
        if (library.tfs_sim_abi_version() != ABI_VERSION
                or library.tfs_sim_input_size() != C.sizeof(DriveInput)
                or library.tfs_sim_output_size() != C.sizeof(DriveOutput)):
            raise ValueError('Rust simulation ABI does not match Python')
        library.tfs_ground_step.argtypes = [C.POINTER(DriveInput),SampleCallback,
            ValidateCallback,C.c_void_p,C.POINTER(DriveOutput)]
        library.tfs_ground_step.restype = C.c_int32
        planned = getattr(library,'tfs_ground_step_plan',None)
        if planned is not None:
            planned.argtypes = [C.POINTER(DriveInput),SamplePlanCallback,ValidateCallback,
                C.c_void_p,C.c_double,C.POINTER(DriveOutput)]
            planned.restype = C.c_int32
            library._planned_source = planned
            library._planned_signature = tuple(planned.argtypes)
        prefix = getattr(library,'tfs_ground_step_plan_prefix_v1',None)
        if prefix is not None:
            prefix.argtypes = [C.POINTER(DriveInput),SamplePlanCallback,ValidateCallback,
                C.c_void_p,C.c_double,C.POINTER(DriveOutput)]
            prefix.restype = C.c_int32
            library._prefix_source = prefix
            library._prefix_signature = tuple(prefix.argtypes)
        rotation = getattr(library,'tfs_ground_step_plan_prefix_v2',None)
        if rotation is not None:
            rotation.argtypes = [C.POINTER(DriveInput),SamplePlanCallback,ValidateCallback,
                C.c_void_p,C.c_double,C.POINTER(DriveOutput)]
            rotation.restype = C.c_int32
            library._rotation_source = rotation
            library._rotation_signature = tuple(rotation.argtypes)
        library._support_slice_source=None
        query=getattr(library,'tfs_ground_support_slice_v1',None)
        if query is not None and _SUPPORT_SLICE_PACKER is not None:
            try:
                for name in ('tfs_support_slice_abi','tfs_support_slice_input_size','tfs_support_slice_output_size'):
                    fn=getattr(library,name);fn.argtypes=[];fn.restype=C.c_uint32
                if (library.tfs_support_slice_abi()==1
                        and library.tfs_support_slice_input_size()==C.sizeof(SupportSliceInput)
                        and library.tfs_support_slice_output_size()==C.sizeof(SupportSliceOutput)):
                    query.argtypes=[C.POINTER(DriveInput),C.POINTER(SupportSliceInput),SamplePlanCallback,
                                    C.c_void_p,C.c_double,C.POINTER(SupportSliceOutput)]
                    query.restype=C.c_int32
                    library._support_slice_source=query
                    library._support_slice_signature=tuple(query.argtypes)
            except AttributeError:
                pass
        fit = getattr(library,'tfs_support_plane',None)
        if fit is not None:
            fit.argtypes = [C.c_uint32,DoublePtr,DoublePtr,DoublePtr,DoublePtr,
                C.c_uint32,C.POINTER(C.c_uint32),DoublePtr,C.POINTER(C.c_uint32),
                C.POINTER(C.c_uint32)]
            fit.restype = C.c_int32
        plan = getattr(library,'tfs_ground_plan',None)
        if plan is not None:
            plan.argtypes = [C.POINTER(NativeTerrain),C.c_uint32,DoublePtr,DoublePtr,
                C.c_uint32,C.POINTER(NativePose),C.c_double,C.c_double,C.c_double,
                DoublePtr,DoublePtr,DoublePtr,DoublePtr,C.POINTER(C.c_uint32),
                C.POINTER(C.c_uint32)]
            plan.restype = C.c_int32
            # A separate callable retains the typed public entry point.
            # All address arguments below refer to live local NumPy arrays.
            library._ground_plan_raw = None
            try:
                raw_types = list(plan.argtypes)
                for index in (2,3,9,10,11,13):
                    raw_types[index] = C.c_void_p
                raw = C.CFUNCTYPE(C.c_int32,*raw_types)(('tfs_ground_plan',library))
            except (AttributeError,OSError,TypeError,ValueError):
                pass
            else:
                library._ground_plan_source = plan
                library._ground_plan_argtypes = tuple(plan.argtypes)
                library._ground_plan_raw = raw
        # This optional pure-math extension does not change the ground-step
        # ABI. Older libraries retain the existing support-plane path.
        measured = getattr(library,'tfs_measured_support',None)
        library._tfs_measured_support = None
        if measured is not None:
            try:
                for name,expected in (('tfs_measured_input_size',C.sizeof(MeasuredSupportInput)),
                                      ('tfs_measured_output_size',C.sizeof(MeasuredSupportOutput))):
                    size = getattr(library,name)
                    size.argtypes, size.restype = [],C.c_uint32
                    if size() != expected:
                        raise ValueError('Measured support ABI mismatch')
            except (AttributeError,ValueError):
                pass
            else:
                measured.argtypes = [C.POINTER(MeasuredSupportInput),C.POINTER(MeasuredSupportOutput)]
                measured.restype = C.c_int32
                library._tfs_measured_support = measured
        _LIBRARY = library
    except (OSError,AttributeError,ValueError):
        _LIBRARY = None
    return _LIBRARY


def backend_status():
    mode = os.environ.get('SOLBREAKER_SIM_BACKEND',DEFAULT_BACKEND)
    if mode not in ('python','rust','auto'):
        raise ValueError('SOLBREAKER_SIM_BACKEND must be python, rust or auto')
    library = load_library() if mode != 'python' else None
    if mode == 'rust' and library is None:
        raise ValueError('Rust simulation library missing or incompatible; run tools/build_sim_core.py')
    return 'rust' if library is not None else 'python'


def runtime_status(world,pool=None):
    native = getattr(world,'_rust_drive',None)
    physics = getattr(world,'_native_physics',None)
    state = ('disabled' if pool is None else 'closed' if pool.closed else
             'failed' if pool.failed else 'closing' if pool.closing else
             'ready' if pool.ready else 'starting')
    live = 0
    for child in getattr(pool,'children',()):
        try:
            live += bool(child['process'].is_alive())
        except ValueError:  # Process handle already closed during retirement.
            pass
    return dict(backend='rust' if native is not None and native.library is not None else 'python',
                ground_physics='native' if physics is not None else 'hybrid',
                native_physics_steps=physics.steps if physics is not None else 0,
                vehicle_collision='proxy' if getattr(getattr(world,'assets',None),'contact_proxies',None) else 'mesh',
                native_moves=dict(native.stats) if native is not None else {},
                worker_processes=pool.count if pool is not None and pool.ready else 0,
                worker_state=state,live_workers=live,
                parallel_moves=dict(pool.stats) if pool is not None else {})


def _native_pose(pose):
    return NativePose(*pose.position,pose.yaw,pose.pitch,pose.roll)


def _pointer(values):
    return values.ctypes.data_as(DoublePtr)


def _callback_view(pointer, count):
    """Borrow a native buffer for this synchronous callback only.

    Rust retains the allocation until the callback returns. Constructing a
    view from its address avoids ctypes.cast's self-referencing _objects
    dictionaries, which otherwise accumulate until cyclic GC runs in a step.
    These views must not escape the callback or own the native allocation.
    """
    if count == 0:
        return np.empty(0,dtype=pointer._type_)
    buffer = (pointer._type_*count).from_address(C.addressof(pointer.contents))
    return np.asarray(buffer)


def try_support_plane(phi,target,weight,lower,hint):
    """The same active-set fit for contact/landing paths kept in Python."""
    if os.environ.get('SOLBREAKER_SIM_BACKEND',DEFAULT_BACKEND) not in ('rust','auto'):
        return None
    library = load_library()
    if library is None or not hasattr(library,'tfs_support_plane'):
        return None
    phi = np.ascontiguousarray(phi,dtype=np.float64)
    target,weight,lower = [np.ascontiguousarray(values,dtype=np.float64)
                           for values in (target,weight,lower)]
    count = len(phi)
    if (not 1 <= count <= 512 or phi.shape != (count,3)
            or any(values.shape != (count,) for values in (target,weight,lower))):
        return None
    if len(hint) > 3 or any(index < 0 or index >= count for index in hint):
        hint = ()
    indices = (C.c_uint32*3)(*hint)
    plane,active,active_count = (C.c_double*3)(),(C.c_uint32*3)(),C.c_uint32()
    status = library.tfs_support_plane(count,_pointer(phi),_pointer(target),_pointer(weight),
        _pointer(lower),len(hint),indices,plane,active,C.byref(active_count))
    if status != 0 or active_count.value > 3:
        return None
    return np.array(plane),tuple(active[:active_count.value])


class RustDrive:
    def __init__(self,world):
        from .collision import Pose
        self.world,self.Pose = world,Pose
        self.stats = Counter()
        self.support_stats = Counter()
        self.mode = os.environ.get('SOLBREAKER_SIM_BACKEND',DEFAULT_BACKEND)
        if self.mode not in ('python','rust','auto'):
            raise ValueError('SOLBREAKER_SIM_BACKEND must be python, rust or auto')
        self.library = load_library() if self.mode != 'python' else None
        if self.mode == 'rust' and self.library is None:
            raise ValueError('Rust simulation library missing or incompatible; run tools/build_sim_core.py')
        self._terrain = self._grid = None
        self._entity = None
        self._error = None
        self._layouts = {}
        self._drive_prefix = None
        self._prefix_enabled = os.environ.get('SOLBREAKER_DRIVE_PREFIX')=='1'
        self._rotation_enabled = os.environ.get('SOLBREAKER_ROTATION_PREFIX')=='1'
        self._pipeline_enabled=os.environ.get('SOLBREAKER_SUPPORT_PIPELINE')=='1'
        self._replica_pipeline_enabled=(os.environ.get('SOLBREAKER_REPLICA_PIPELINE')=='1'
            and os.environ.get('SOLBREAKER_DYNAMIC_BACKEND','bullet')=='rapier')
        self._capture_rotation_validation = False
        self._rotation_validation = None
        self._sample_fn = SampleCallback(self._sample)
        self._sample_plan_fn = SamplePlanCallback(self._sample_plan)
        self._validate_fn = ValidateCallback(self._validate)

    def _pose(self,native):
        return self.Pose((native.x,native.y,native.z),native.yaw,native.pitch,native.roll)

    def _sample(self,context,count,xs,zs,reference,output):
        try:
            heights = self.world._ground_heights(
                _callback_view(xs,count),
                _callback_view(zs,count),self._pose(reference.contents),self._entity)
            _callback_view(output,count)[:] = heights
            return 0
        except BaseException as error:
            self._error = error
            return 2

    def _validate(self,context,candidate,previous):
        try:
            # No impact argument: speculative validation cannot destroy props.
            candidate_pose,previous_pose=self._pose(candidate.contents),self._pose(previous.contents)
            clear,points = self.world._valid(candidate_pose,previous_pose,
                entity=self._entity,suspension=True)
            if self._capture_rotation_validation:
                self._capture_rotation_validation=False
                if not clear:
                    self._rotation_validation=(candidate_pose,previous_pose,points,
                                               self.world._last_validation_rejection)
            return 0 if clear else 1
        except BaseException as error:
            self._error = error
            return 2

    def _sample_plan(self,context,count,xs,zs,reference,terrain,tops,starts,bounds,raised,nraised,output):
        try:
            if nraised > count:
                raise ValueError('Invalid planned ground query')
            # Mirror the exact broadphase used by _ground_heights before
            # constructing NumPy views. Empty solid-map regions can copy the
            # already sampled terrain straight to the caller's output.
            import pybullet as bullet
            nearby = bullet.getOverlappingObjects(tuple(bounds[:3]),tuple(bounds[3:6]),
                                                  physicsClientId=self.world.cid) or ()
            if not any(body in self.world.map_bodies and body not in self.world.breakable_bodies
                       for body, _ in nearby):
                C.memmove(output, terrain, count*C.sizeof(C.c_double))
                return 0
            def view(ptr, length):
                return _callback_view(ptr,length)
            plan = (view(terrain,count),view(tops,count),view(starts,count),
                    tuple(bounds[:6]),view(raised,nraised))
            heights = self.world._ground_heights(view(xs,count),view(zs,count),
                self._pose(reference.contents),self._entity,_plan=plan,_nearby=nearby)
            view(output,count)[:] = heights
            return 0
        except BaseException as error:
            self._error = error
            return 2

    def _prepare_terrain(self):
        from .terrain import MapTerrain
        terrain = self.world.terrain
        if (not isinstance(terrain,MapTerrain)
                or getattr(terrain.height,'__func__',None) is not MapTerrain.height):
            return False
        if self._terrain is None:
            self._terrain = np.ascontiguousarray(terrain.heights,dtype=np.uint16)
            self._grid = NativeTerrain(self._terrain.ctypes.data_as(C.POINTER(C.c_uint16)),
                terrain.size,terrain.MIN,terrain.MIN_Z,terrain.WIDTH,terrain.DEPTH,
                terrain.HEIGHT_MIN,terrain.HEIGHT)
        return True

    def plan_ground(self,xs,zs,reference,entity):
        """Prepare identical terrain/plane ray starts, leaving Bullet in place."""
        if (self.library is None or not hasattr(self.library,'tfs_ground_plan')
                or xs.ndim != 1 or zs.shape != xs.shape or not 1 <= len(xs) <= 256
                or not self._prepare_terrain()):
            return None
        xs,zs = np.ascontiguousarray(xs,dtype=np.float64),np.ascontiguousarray(zs,dtype=np.float64)
        count = len(xs)
        if isinstance(reference,self.Pose):
            kind,ref = 2,_native_pose(reference)
        elif reference is None:
            kind,ref = 0,NativePose()
        else:
            try:
                kind,ref = 1,NativePose(0.,float(reference),0.,0.,0.,0.)
            except (ValueError,TypeError,OverflowError):
                return None
        terrain,tops,starts = np.empty(count),np.empty(count),np.empty(count)
        bounds,indices,raised_count = (C.c_double*6)(),np.empty(count,dtype=np.uint32),C.c_uint32()
        raw = getattr(self.library,'_ground_plan_raw',None)
        original = self.library.tfs_ground_plan
        if (raw is not None and original is getattr(self.library,'_ground_plan_source',None)
                and original.argtypes is not None
                and tuple(original.argtypes)==self.library._ground_plan_argtypes
                and original.restype is C.c_int32 and getattr(original,'errcheck',None) is None):
            status = raw(C.byref(self._grid),count,xs.ctypes.data,zs.ctypes.data,
                kind,C.byref(ref),self.world.assets_for(entity).max_slope,
                self.world.SUPPORT_REACH,self.world.SKIN,
                terrain.ctypes.data,tops.ctypes.data,starts.ctypes.data,bounds,
                indices.ctypes.data,C.byref(raised_count))
        else:
            status = self.library.tfs_ground_plan(C.byref(self._grid),count,_pointer(xs),_pointer(zs),
                kind,C.byref(ref),self.world.assets_for(entity).max_slope,
                self.world.SUPPORT_REACH,self.world.SKIN,_pointer(terrain),_pointer(tops),
                _pointer(starts),bounds,indices.ctypes.data_as(C.POINTER(C.c_uint32)),
                C.byref(raised_count))
        if status != 0 or raised_count.value > count:
            return None
        return terrain,tops,starts,tuple(bounds),indices[:raised_count.value]

    def _layout_pointers(self,layout):
        key = id(layout)
        cached = self._layouts.get(key)
        if cached is None or cached[0] is not layout:
            if len(self._layouts) >= 32:
                self._layouts.clear()
            arrays = tuple(np.ascontiguousarray(values,dtype=np.float64)
                           for values in (layout.a,layout.b,layout.p,layout.weight))
            cached = (layout,arrays,tuple(_pointer(values) for values in arrays))
            self._layouts[key] = cached
        return cached[2]

    def try_support_pose(self,layout,x,z,yaw,xs,zs,ground,carrying,compression,extra,floors,entity):
        """Prepare the same full-footprint fit after Python sampled geometry.

        Partial footprints and held stations retain the original implementation.
        No caches are published until the pure calculation succeeds in full.
        """
        function = getattr(self.library,'_tfs_measured_support',None)
        world = self.world
        from .terrain import MapTerrain
        if (function is None or floors is not None
                or os.environ.get('SOLBREAKER_SIM_BACKEND',DEFAULT_BACKEND) not in ('rust','auto')
                or not isinstance(world.terrain,MapTerrain)
                or getattr(world.terrain.height,'__func__',None) is not MapTerrain.height):
            return None
        count = len(layout.a)
        if not 1<=count<=256:
            return None
        layout_valid=(_support_checks.valid_layout(layout.a,layout.b,layout.p,layout.weight,count)
                      if _SUPPORT_OUTPUT_LAYOUT_OK and np.dtype is _SUPPORT_DTYPE_SOURCE else None)
        if layout_valid is None:
            layout_valid=not any(getattr(values,'shape',None)!=(count,)
                or getattr(values,'dtype',None)!=np.dtype('float64')
                for values in (layout.a,layout.b,layout.p,layout.weight))
        if not layout_valid:
            return None
        active,supported,rank = layout.full_support
        carry = bool(carrying and entity in world._road_poses)
        if carry and rank < 3:
            return None
        xs,zs,ground = (np.ascontiguousarray(values,dtype=np.float64) for values in (xs,zs,ground))
        if any(values.shape != (count,) for values in (xs,zs,ground)):
            return None
        extra_values = (np.asarray([(row[0],row[1],row[2],bool(len(row)<4 or row[3]))
                                   for row in extra],dtype=np.float64).reshape(-1,4)
                        if extra else None)
        extra_count = len(extra_values) if extra_values is not None else 0
        key = entity if entity is not None else world.primary_id
        hints = tuple(i for i in world._support_hints.get(key,()) if i < count)
        if len(hints)>3:
            hints = ()
        if any(i<0 for i in hints):
            return None
        old_position,up = (0.,0.,0.),(0.,1.,0.)
        if carry:
            old = carrying if isinstance(carrying,self.Pose) else world._road_poses[entity]
            lean = world._lean_states.get(entity,(0.,0.,0.,0.))
            old = self.Pose(old.position,old.yaw,old.pitch-lean[0],old.roll-lean[1])
            from .orientation import matrix
            old_position,up = old.position,matrix(old.yaw,old.pitch,old.roll)[:,1]
        request = MeasuredSupportInput()
        # Strong local references keep every input alive through the pure FFI
        # call. Fresh addresses also avoid relying on cached pointer lifetimes.
        if _MEASURED_PACKER is not None:
            layout_values = tuple(np.ascontiguousarray(v,dtype=np.float64)
                                  for v in (layout.a,layout.b,layout.p,layout.weight))
            _MEASURED_PACKER.pack_into(request,0,1,count,extra_count,int(carry),int(rank),int(supported),
                *(v.ctypes.data for v in layout_values),xs.ctypes.data,zs.ctypes.data,ground.ctypes.data,
                extra_values.ctypes.data if extra_values is not None else 0,
                *old_position,*up,compression,world.SUPPORT_REACH,world.SKIN,
                float(world._terrain_heights((x,),(z,))[0]),
                world._hull_clearance(entity) if compression else 0.,len(hints),*(hints+(0,)*(3-len(hints))))
        else:
            request.abi,request.count,request.extra_count=1,count,extra_count
            request.carrying,request.full_rank,request.supported=int(carry),int(rank),int(supported)
            for name,pointer in zip(('a','b','p','weights'),self._layout_pointers(layout)):
                setattr(request,name,pointer)
            for name,values in (('xs',xs),('zs',zs),('ground',ground)):
                setattr(request,name,_pointer(values))
            request.extra=_pointer(extra_values) if extra_values is not None else None
            request.old_position,request.up=(C.c_double*3)(*old_position),(C.c_double*3)(*up)
            request.compression,request.reach,request.skin=compression,world.SUPPORT_REACH,world.SKIN
            request.centre=float(world._terrain_heights((x,),(z,))[0])
            request.clearance=world._hull_clearance(entity) if compression else 0.
            request.hint_count,request.hints=len(hints),(C.c_uint32*3)(*hints)
        lower = np.empty(count+1+extra_count,dtype=np.float64)
        output = MeasuredSupportOutput()
        if _MEASURED_OUTPUT_PACKER is not None:
            _MEASURED_OUTPUT_PACKER.pack_into(output,MeasuredSupportOutput.lower.offset,lower.ctypes.data)
        else:
            output.lower = _pointer(lower)
        status = function(C.byref(request),C.byref(output))
        if status:
            self.support_stats['declined_'+str(status)] += 1
            return None
        output_valid=(_support_checks.valid_output(output,request.supported,lower)
                      if (_SUPPORT_OUTPUT_LAYOUT_OK and type(output) is _SUPPORT_OUTPUT_CLASS
                          and math.isfinite is _SUPPORT_FINITE_SOURCE
                          and np.isfinite is _SUPPORT_NUMPY_FINITE_SOURCE) else None)
        if output_valid is None:
            output_valid=not (output.active_count>3 or output.supported != request.supported
                or any(i>=len(lower) for i in output.active[:output.active_count])
                or not all(math.isfinite(v) for v in output.plane) or not np.isfinite(lower).all())
        if not output_valid:
            self.support_stats['invalid_output'] += 1
            return None
        y,slope_x,slope_z = output.plane
        pose = self.Pose((float(x),float(y),float(z)),yaw,-math.atan(slope_z),
                         math.atan(slope_x/math.sqrt(1+slope_z*slope_z)))
        phi = (np.vstack([layout.plane_rows,((1.,0.,0.),),
                          *((1.,row[0],row[1]) for row in extra)]) if extra else layout.fit_rows)
        if carry:
            world._carrying_supported[entity] = supported
            rank_key = (id(layout),active.tobytes())
            if rank_key not in world._support_ranks:
                if len(world._support_ranks)>=128:
                    world._support_ranks.clear()
                world._support_ranks[rank_key] = rank
        world._support_hints[key] = tuple(i for i in output.active[:output.active_count] if i<count)
        world._support_bounds[key] = (pose,phi,lower)
        self.support_stats['accepted'] += 1
        return pose


    def take_drive_prefix(self,entity,pose,distance,yaw_delta,dt,state,displacement):
        prefix,self._drive_prefix=self._drive_prefix,None
        if prefix is None:return None
        if (prefix[0]!=entity or prefix[1] is not pose or prefix[2]!=distance
                or prefix[3]!=yaw_delta or prefix[4]!=dt or prefix[5] is not state
                or prefix[6]!=displacement):
            self.stats['prefix_mismatch']+=1
            return None
        if prefix[7].validation_queries==1:
            travel=math.hypot(*displacement) if displacement is not None else abs(distance)
            steps=max(1,math.ceil(travel/self.world.STEP),
                      math.ceil(abs(yaw_delta)/math.radians(.75)))
            yaw=(pose.yaw+yaw_delta/steps+math.pi)%math.tau-math.pi
            if struct.pack('@d',yaw)!=struct.pack('@d',prefix[7].pose.yaw):
                self.stats['rotation_prefix_yaw_mismatch']+=1
                return None
        return prefix


    def restore_rotation_rejection(self,prefix,rotated):
        entity=prefix[0]
        _,_,points,reason=prefix[10]
        # A rejected contact query moved the sensor; early inside/climb
        # rejections return no points and never move it. Recreate that state.
        if points:self.world.set_pose(self.world.bodies[entity],rotated)
        self.world._last_validation_rejection=reason
        self.stats['rotation_prefix_consumed']+=1
        return False,points


    def _prefix_pose(self,value,yaw):
        return self.Pose((value.x,value.y,value.z),yaw,value.pitch,value.roll)


    def publish_drive_prefix(self,prefix,yaw):
        entity,_,_,_,_,_,_,output,lower,layout=prefix[:10]
        road=self._prefix_pose(output.road,yaw)
        candidate=self._prefix_pose(output.spring_pose,yaw)
        world=self.world
        active,supported,rank=layout.full_support
        rank_key=(id(layout),active.tobytes())
        if rank_key not in world._support_ranks:
            if len(world._support_ranks)>=128:world._support_ranks.clear()
            world._support_ranks[rank_key]=rank
        world._carrying_supported[entity]=supported
        world._support_hints[entity]=tuple(output.hints[:output.hint_count])
        world._support_bounds[entity]=(road,layout.fit_rows,lower)
        world._spring_rates[entity]=(candidate,tuple(output.spring_velocity),
                                    tuple(output.spring_lean),tuple(output.spring_angular))
        world._contact_support_cache[entity]=[]
        self.stats['prefix_consumed']+=1
        return road,candidate


    def try_support_pipeline(self,context,entity,x,z,yaw,reference,start,h,state):
        if not self._pipeline_enabled or self._entity is not None:
            return None
        world=self.world
        methods=_prefix_methods()
        if any(getattr(getattr(world,name),'__func__',None) is not methods[name]
               for name in ('_support_pose','_spring_pose','_ground_heights','_terrain_heights')):
            return None
        function=getattr(self.library,'_support_slice_source',None)
        if (function is None or self.library.tfs_ground_support_slice_v1 is not function
                or function.argtypes is None or tuple(function.argtypes)!=self.library._support_slice_signature
                or function.restype is not C.c_int32 or getattr(function,'errcheck',None) is not None
                or getattr(self._sample_plan,'__func__',None) is not _PREFIX_SAMPLE_IMPL):
            return None
        request=context[0]
        layout=world._support_layout(entity);count=len(layout.a)
        if request.count!=count:
            return None
        hints=tuple(i for i in world._support_hints.get(entity,()) if i<count)
        if len(hints)>3:hints=()
        if any(i<0 for i in hints):return None
        road=world._road_poses[entity]
        lean=world._lean_states.get(entity,(0.,0.,0.,0.))
        workspace=context[3]
        if not workspace:
            workspace.update(query=SupportSliceInput(),output=SupportSliceOutput(),
                             xs=np.empty(count),zs=np.empty(count),ground=np.empty(count))
        query=workspace['query'];output=workspace['output']
        xs,zs,ground=workspace['xs'],workspace['zs'],workspace['ground']
        _SUPPORT_SLICE_PACKER.pack_into(query,0,x,z,yaw,
            *reference.position,reference.yaw,reference.pitch,reference.roll,
            *start.position,start.yaw,start.pitch,start.roll,
            *road.position,road.yaw,road.pitch,road.roll,
            *state.velocity,*state.suspension_velocity,*state.support_angular_rates,
            *lean,h,float(world._terrain_heights((x,),(z,))[0]),len(hints),*(hints+(0,)*(3-len(hints))))
        # lower belongs to the published support cache: allocate it afresh.
        # The query/output structs and sample scratch live only in this drive.
        lower=np.empty(count+1)
        _SUPPORT_SLICE_POINTERS.pack_into(output,SupportSliceOutput.lower.offset,
            lower.ctypes.data,xs.ctypes.data,zs.ctypes.data,ground.ctypes.data)
        self._entity=entity;self._error=None
        try:
            status=function(C.byref(request),C.byref(query),self._sample_plan_fn,None,
                            world.assets_for(entity).max_slope,C.byref(output))
        finally:
            self._entity=None
        if self._error is not None:raise self._error
        self.stats['support_pipeline_calls']+=1
        if status==12:
            if output.stage!=1 or not all(np.isfinite(values).all() for values in (xs,zs,ground)):
                raise RuntimeError('Invalid native support sampling prefix')
            self.stats['support_pipeline_partial']+=1
            return None,None,(layout,xs,zs,ground)
        if status:
            raise RuntimeError('Native support pipeline failed: '+str(status))
        floats=_SUPPORT_SLICE_RESULT.unpack_from(output,0)
        if (output.stage!=2 or output.hint_count>3
                or any(i>=count for i in output.hints[:output.hint_count])
                or not all(math.isfinite(v) for v in floats) or not np.isfinite(lower).all()):
            raise RuntimeError('Invalid native support pipeline result')
        road=self.Pose(floats[6:9],yaw,floats[10],floats[11])
        candidate=self.Pose(floats[:3],yaw,floats[4],floats[5])
        active,supported,rank=layout.full_support
        key=(id(layout),active.tobytes())
        if key not in world._support_ranks:
            if len(world._support_ranks)>=128:world._support_ranks.clear()
            world._support_ranks[key]=rank
        world._carrying_supported[entity]=supported
        world._support_hints[entity]=tuple(output.hints[:output.hint_count])
        world._support_bounds[entity]=(road,layout.fit_rows,lower)
        world._spring_rates[entity]=(candidate,floats[12:15],floats[15:19],floats[19:21])
        self.support_stats['accepted']+=1
        self.stats['support_pipeline_completed']+=1
        return road,candidate,None


    def try_drive(self,entity,distance,yaw_delta,dt,state,planar_displacement):
        self._drive_prefix = None
        self._rotation_validation = None
        world = self.world
        if self.library is None:
            return None
        travel = math.hypot(*planar_displacement) if planar_displacement is not None else abs(distance)
        # Keep the accepted resting fast path (including its cache tombstone).
        if (travel == 0. and yaw_delta == 0.
                and max(abs(value) for value in state.suspension_velocity) < .001
                and not any(abs(value) > .01 for value in world._drive_efforts.get(entity,()))):
            return None
        if (not world.experimental_6dof or world.wall_trampoline or not world._has_suspension(entity)
                or not state.grounded or entity in world.dynamic_bodies or entity in world.pending_dynamics
                or entity not in world._road_poses or world._contact_support_cache.get(entity)
                or entity in world._wall_recoil_latches or dt is None or dt <= 0.
                or self._entity is not None or not self._prepare_terrain()):
            self.stats['unsupported'] += 1
            return None
        pose = world.poses[entity]
        layout = world._support_layout(entity)
        count = len(layout.a)
        if not 1 <= count <= 256:
            self.stats['unsupported'] += 1
            return None
        pointers = self._layout_pointers(layout)
        memory = world._support_memory.get(entity)
        has_memory = memory is not None and memory[0] == pose
        heights = np.ascontiguousarray(memory[1] if has_memory else np.zeros(count),dtype=np.float64)
        speeds = np.ascontiguousarray(memory[2] if has_memory else np.zeros(count),dtype=np.float64)
        hints = world._support_hints.get(entity,())
        if len(hints) > 3:
            hints = ()
        request = DriveInput()
        road = world._road_poses[entity]
        lean = world._lean_states.get(entity,(0.,0.,0.,0.))
        effort = world._drive_efforts.get(entity,(0.,0.,0.))
        displacement = planar_displacement or (0.,0.)
        rows = (pose.position,road.position,state.velocity,state.suspension_velocity,
                state.support_angular_rates,lean,effort,displacement)
        packed = (_DRIVE_PACKERS is not None and type(hints) is tuple
            and all(type(row) is tuple and len(row)==size
                    for row,size in zip(rows,(3,3,3,3,2,4,3,2)))
            and all(type(hint) is int and 0<=hint<=0xffffffff for hint in hints))
        if packed:
            header,tail = _DRIVE_PACKERS
            header.pack_into(request,0,ABI_VERSION,count,
                *pose.position,pose.yaw,pose.pitch,pose.roll,
                *road.position,road.yaw,road.pitch,road.roll,
                *state.velocity,*state.suspension_velocity,*state.support_angular_rates,
                *lean,*effort,distance,yaw_delta,dt,*displacement,
                int(planar_displacement is not None),int(world._drive_blocked.get(entity,False)),
                int(has_memory),len(hints),*(hints+(0,)*(3-len(hints))))
            # Keep the original owning layout pointers. Fresh local arrays
            # remain alive until the synchronous native call returns below.
            for name,pointer in zip(('a','b','p','weight'),pointers):
                setattr(request,name,pointer)
            tail.pack_into(request,DriveInput.memory_heights.offset,
                heights.ctypes.data,speeds.ctypes.data,
                world.SKIN,world.STEP,world.GRAVITY,world.SUPPORT_REACH,
                world.SUSPENSION_TRAVEL,world.SUSPENSION_OMEGA,
                world.SUSPENSION_ANGULAR_OMEGA,world._hull_clearance(entity))
            request.terrain = self._grid
        else:
            request.abi_version,request.count = ABI_VERSION,count
            request.pose,request.road = _native_pose(pose),_native_pose(world._road_poses[entity])
            request.motion = NativeMotion((C.c_double*3)(*state.velocity),
                (C.c_double*3)(*state.suspension_velocity),(C.c_double*2)(*state.support_angular_rates))
            request.lean = (C.c_double*4)(*world._lean_states.get(entity,(0.,0.,0.,0.)))
            request.effort = (C.c_double*3)(*world._drive_efforts.get(entity,(0.,0.,0.)))
            request.distance,request.yaw_delta,request.dt = distance,yaw_delta,dt
            request.displacement = (C.c_double*2)(*(planar_displacement or (0.,0.)))
            request.has_displacement = int(planar_displacement is not None)
            request.was_blocked,request.has_memory = int(world._drive_blocked.get(entity,False)),int(has_memory)
            request.hint_count = len(hints)
            request.hints = (C.c_uint32*3)(*hints)
            for name,pointer in zip(('a','b','p','weight'),pointers):
                setattr(request,name,pointer)
            request.memory_heights,request.memory_speeds = _pointer(heights),_pointer(speeds)
            request.constants = (C.c_double*8)(world.SKIN,world.STEP,world.GRAVITY,
                world.SUPPORT_REACH,world.SUSPENSION_TRAVEL,world.SUSPENSION_OMEGA,
                world.SUSPENSION_ANGULAR_OMEGA,world._hull_clearance(entity))
            request.terrain = self._grid
        out_heights,out_speeds,out_bounds = np.empty(count),np.empty(count),np.empty(count+1)
        output = DriveOutput()
        if _DRIVE_OUTPUT_PACKER is not None:
            _DRIVE_OUTPUT_PACKER.pack_into(output,DriveOutput.memory_heights.offset,
                out_heights.ctypes.data,out_speeds.ctypes.data,out_bounds.ctypes.data)
        else:
            output.memory_heights,output.memory_speeds,output.lower_bounds = (
                _pointer(out_heights),_pointer(out_speeds),_pointer(out_bounds))
        previous_validation = world._last_validation_rejection
        self._error,self._entity = None,entity
        try:
            from .collision import CollisionWorld
            planned = getattr(self.library,'tfs_ground_step_plan',None)
            prefix_function = getattr(self.library,'tfs_ground_step_plan_prefix_v1',None)
            prefix_methods = _prefix_methods() if self._prefix_enabled else {}
            prefix_world = (type(world) is CollisionWorld
                            and getattr(world,'_rapier',None) is not None)
            if self._replica_pipeline_enabled and not prefix_world:
                # A ground-only replica intentionally has no Rapier world.
                # Admit only the exact implementation retaining its original
                # contact observer and every side-effect rejection callback.
                from .drive_replica import _replica_prefix_contract
                replica_methods = _replica_prefix_contract(world)
                if replica_methods is not None:
                    prefix_world, prefix_methods = True, replica_methods
            prefix_allowed = (self._prefix_enabled and prefix_function is not None
                and prefix_world
                and all(getattr(getattr(world,name),'__func__',None) is original
                        for name,original in prefix_methods.items())
                and getattr(self._pose,'__func__',None) is _PREFIX_POSE_IMPL
                and getattr(self._sample_plan,'__func__',None) is _PREFIX_SAMPLE_IMPL
                and getattr(self._validate,'__func__',None) is _PREFIX_VALIDATE_IMPL
                and getattr(world._support_plane,'__func__',None) is _PREFIX_PLANE_IMPL()
                and planned is self.library._planned_source
                and planned.argtypes is not None
                and tuple(planned.argtypes)==self.library._planned_signature
                and planned.restype is C.c_int32
                and getattr(planned,'errcheck',None) is None
                and prefix_function is self.library._prefix_source
                and prefix_function.argtypes is not None
                and tuple(prefix_function.argtypes)==self.library._prefix_signature
                and prefix_function.restype is C.c_int32
                and getattr(prefix_function,'errcheck',None) is None)
            rotation_function=getattr(self.library,'tfs_ground_step_plan_prefix_v2',None)
            rotation_allowed=(prefix_allowed and self._rotation_enabled
                and rotation_function is not None
                and rotation_function is self.library._rotation_source
                and rotation_function.argtypes is not None
                and tuple(rotation_function.argtypes)==self.library._rotation_signature
                and rotation_function.restype is C.c_int32
                and getattr(rotation_function,'errcheck',None) is None)
            if rotation_allowed:
                self._capture_rotation_validation=True
                status = rotation_function(C.byref(request),self._sample_plan_fn,self._validate_fn,
                    None,world.assets_for(entity).max_slope,C.byref(output))
            elif prefix_allowed:
                status = prefix_function(C.byref(request),self._sample_plan_fn,self._validate_fn,
                    None,world.assets_for(entity).max_slope,C.byref(output))
            elif planned is not None and getattr(world._ground_heights,'__func__',None) is CollisionWorld._ground_heights:
                status = planned(C.byref(request),self._sample_plan_fn,self._validate_fn,
                    None,world.assets_for(entity).max_slope,C.byref(output))
            else:
                status = self.library.tfs_ground_step(C.byref(request),self._sample_fn,
                    self._validate_fn,None,C.byref(output))
        finally:
            self._capture_rotation_validation=False
            self._entity = None
            # Validation moves only the sensor. A declined native proposal
            # must leave the original solver at the authoritative start pose.
            world.set_pose(world.bodies[entity],pose)
        rotation_validation,self._rotation_validation=self._rotation_validation,None
        if self._error is not None:
            world._last_validation_rejection = previous_validation
            raise self._error
        if status != 0:
            world._last_validation_rejection = previous_validation
            if status==11 and prefix_allowed:
                values=(*self._pose(output.pose).position,output.pose.yaw,output.pose.pitch,output.pose.roll,
                    *self._pose(output.road).position,output.road.yaw,output.road.pitch,output.road.roll,
                    *self._pose(output.spring_pose).position,output.spring_pose.yaw,
                    output.spring_pose.pitch,output.spring_pose.roll,*output.spring_velocity,
                    *output.spring_lean,*output.spring_angular,*out_bounds)
                stage=output.validation_queries
                if (output.reason!=6 or stage not in ((1,2) if rotation_allowed else (2,))
                        or output.support_queries!=stage or output.hint_count>3
                        or any(i>=count for i in output.hints[:output.hint_count])
                        or not all(math.isfinite(v) for v in values)):
                    raise RuntimeError('Invalid native drive prefix')
                if stage==1:
                    if rotation_validation is None:
                        raise RuntimeError('Missing native rotation rejection')
                    observed,observed_previous,_,_=rotation_validation
                    pose_values=lambda value:(*value.position,value.yaw,value.pitch,value.roll)
                    native_values=lambda value:(value.x,value.y,value.z,value.yaw,value.pitch,value.roll)
                    if (struct.pack('@6d',*pose_values(observed))!=struct.pack('@6d',*native_values(output.pose))
                            or struct.pack('@6d',*pose_values(observed_previous))!=struct.pack('@6d',*pose_values(pose))
                            or struct.pack('@6d',*native_values(output.pose))!=struct.pack('@6d',*native_values(output.spring_pose))):
                        raise RuntimeError('Invalid native rotation checkpoint pose')
                self._drive_prefix=(entity,pose,distance,yaw_delta,dt,state,planar_displacement,
                                    output,out_bounds,layout,rotation_validation if stage==1 else None)
                if self._pipeline_enabled and getattr(self.library,'_support_slice_source',None) is not None:
                    self._drive_prefix+=((request,heights,speeds,{}),)
                self.stats['prefix_available']+=1
            self.stats['fallback_'+str(output.reason or status)] += 1
            return None
        result = self._pose(output.pose)
        values = (*result.position,result.yaw,result.pitch,result.roll,
                  *output.motion.velocity,*output.motion.suspension,*output.motion.angular,
                  *output.lean,*out_heights,*out_speeds,*out_bounds)
        if output.hint_count > 3 or not all(math.isfinite(value) for value in values):
            world._last_validation_rejection = previous_validation
            self.stats['invalid_output'] += 1
            return None
        world.set_pose(world.bodies[entity],result)
        world.poses[entity] = result
        state.velocity = tuple(output.motion.velocity)
        state.suspension_velocity = tuple(output.motion.suspension)
        state.support_angular_rates = tuple(output.motion.angular)
        road_pose = self._pose(output.road)
        bounds_pose = self._pose(output.bounds_pose)
        # The Python solver keeps the same Pose object for its final road
        # target and bounds; _bottomed_pose relies on that identity.
        if bounds_pose == road_pose:
            bounds_pose = road_pose
        world._road_poses[entity] = road_pose
        world._lean_states[entity] = tuple(output.lean)
        world._drive_blocked[entity] = bool(output.blocked)
        world._carrying_supported[entity] = bool(output.carrying_supported)
        world._support_hints[entity] = tuple(output.hints[:output.hint_count])
        world._support_bounds[entity] = (bounds_pose,layout.fit_rows,out_bounds)
        world._spring_rates[entity] = (self._pose(output.spring_pose),tuple(output.spring_velocity),
            tuple(output.spring_lean),tuple(output.spring_angular))
        world._support_memory[entity] = (result,out_heights,out_speeds)
        # The ordinary clean solver visits _cached_contact_supports even when
        # no measured obstacle rows remain, publishing this empty cache entry.
        world._contact_support_cache[entity] = []
        self.stats['accepted'] += 1
        return result,bool(output.moved),0.


_PREFIX_POSE_IMPL=RustDrive._pose
_PREFIX_SAMPLE_IMPL=RustDrive._sample_plan
_PREFIX_VALIDATE_IMPL=RustDrive._validate

def _PREFIX_PLANE_IMPL():
    from .collision import _SUPPORT_PLANE_IMPL
    return _SUPPORT_PLANE_IMPL

def _prefix_methods():
    # CollisionWorld finishes importing before the first movement request.
    from .collision import _DRIVE_PREFIX_METHODS
    return _DRIVE_PREFIX_METHODS


_SUPPORT_DTYPE_SOURCE=np.dtype
_SUPPORT_FINITE_SOURCE=math.isfinite
_SUPPORT_NUMPY_FINITE_SOURCE=np.isfinite
_SUPPORT_OUTPUT_CLASS=MeasuredSupportOutput
try:
    _SUPPORT_OUTPUT_LAYOUT_OK=(_support_checks is not None
        and callable(getattr(_support_checks,'valid_layout',None))
        and callable(getattr(_support_checks,'valid_output',None))
        and _support_checks.output_layout()==(
            C.sizeof(MeasuredSupportOutput),tuple(getattr(MeasuredSupportOutput,name).offset
            for name in ('plane','active','active_count','supported','lower'))))
except (AttributeError,TypeError,ValueError):
    _SUPPORT_OUTPUT_LAYOUT_OK=False
