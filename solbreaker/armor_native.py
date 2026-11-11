"""Optional pure armor-triangle query; unsupported contracts retain NumPy."""
import ctypes as C
import os
from pathlib import Path
import struct
import numpy as np
from .orientation import matrix

class Part(C.Structure):
    _fields_=[('triangles',C.POINTER(C.c_double)),('count',C.c_uint32),
              ('origin',C.c_double*3),('direction',C.c_double*3),('distance',C.c_double)]

class Hit(C.Structure):
    _fields_=[('part',C.c_uint32),('triangle',C.c_uint32),('distance',C.c_double)]

_F64=np.dtype('float64')
_U8=np.dtype('uint8')
_HIT_DTYPE=np.dtype([('part','u4'),('triangle','u4'),('distance','f8')],align=True)
_PART_PACK=struct.Struct('@PI7d')
_FUNCTIONS=(np.einsum,np.cross,np.divide,np.zeros_like,np.array,np.asarray,
            np.eye,np.zeros,np.linalg.norm,np.multiply,np.empty,np.flatnonzero,np.isfinite)
_LIBRARY=None
_LOADED=False

def _library_path():
    suffix='.dll' if os.name=='nt' else '.so'
    return Path(__file__).with_name('native')/'armor_query'/('tfs_armor_query'+suffix)

def _functions_unchanged():
    return all(left is right for left,right in zip(_FUNCTIONS,
        (np.einsum,np.cross,np.divide,np.zeros_like,np.array,np.asarray,
         np.eye,np.zeros,np.linalg.norm,np.multiply,np.empty,np.flatnonzero,np.isfinite)))

def _reference(triangles,origin,direction,distance):
    a=triangles[:,0];e1=triangles[:,1]-a;e2=triangles[:,2]-a
    p=np.cross(direction,e2)
    det=np.einsum('ij,ij->i',e1,p)
    valid=det>1e-9
    inv=np.divide(1.,det,out=np.zeros_like(det),where=valid)
    delta=origin-a
    u=np.einsum('ij,ij->i',delta,p)*inv
    q=np.cross(delta,e1)
    v=q@direction*inv
    t=np.einsum('ij,ij->i',e2,q)*inv
    valid &= (u>=-1e-7)&(v>=-1e-7)&(u+v<=1.0000001)&(t>1e-5)&(t<=distance)
    return [(int(i),struct.pack('@d',t[i])) for i in np.flatnonzero(valid)]

def _invoke(lib,descriptors,total):
    output=np.empty(total,dtype=_HIT_DTYPE)
    count=C.c_uint32()
    status=lib._query_call(descriptors,len(descriptors),output.ctypes.data,
                           total,C.byref(count))
    if status:
        return None
    if count.value>total:
        raise RuntimeError('Invalid native armor hit count')
    result=[]
    previous=(-1,-1)
    for hit in output[:count.value]:
        part,triangle=int(hit['part']),int(hit['triangle'])
        distance=float(hit['distance'])
        if (not 0<=part<len(descriptors) or not 0<=triangle<descriptors[part].count
                or (part,triangle)<=previous or not np.isfinite(distance)
                or not 1e-5<distance<=descriptors[part].distance):
            raise RuntimeError('Invalid native armor hit')
        previous=part,triangle
        result.append((part,triangle,distance))
    return result

def _numerical_contract(lib):
    # Verify the installed reduction kernels instead of assuming that a NumPy
    # version or CPU architecture implies a particular floating-point order.
    rng=np.random.default_rng(37193)
    for size in (1,3,7,32,129):
        triangles=np.ascontiguousarray(rng.uniform(-1,1,(size,3,3)))
        origin=rng.uniform(-.5,.5,3)
        direction=rng.normal(size=3)
        descriptors=(Part*1)()
        _PART_PACK.pack_into(descriptors,0,triangles.ctypes.data,size,*origin,*direction,20.)
        actual=_invoke(lib,descriptors,size)
        if actual is None:
            return False
        if [(i,struct.pack('@d',t)) for p,i,t in actual]!=_reference(triangles,origin,direction,20.):
            return False
    return True

def load_library():
    global _LIBRARY,_LOADED
    if _LOADED:
        return _LIBRARY
    _LOADED=True
    if (not _functions_unchanged()
            or type(_FUNCTIONS[0]).__name__!='_ArrayFunctionDispatcher'
            or not _FUNCTIONS[0].__module__.startswith('numpy')
            or C.sizeof(C.c_void_p)!=8
            or C.sizeof(Part)!=72 or C.sizeof(Hit)!=16 or _PART_PACK.size!=72
            or _HIT_DTYPE.itemsize!=16
            or any(getattr(Part,k).offset!=v for k,v in
                (('triangles',0),('count',8),('origin',16),('direction',40),('distance',64)))
            or any(getattr(Hit,k).offset!=v or _HIT_DTYPE.fields[k][1]!=v
                   for k,v in (('part',0),('triangle',4),('distance',8)))):
        return None
    path=_library_path()
    if not path.is_file():
        return None
    try:
        lib=C.CDLL(str(path))
        for name,expected in (('tfs_armor_query_abi',1),('tfs_armor_part_size',72),
                              ('tfs_armor_hit_size',16)):
            fn=getattr(lib,name);fn.argtypes=[];fn.restype=C.c_uint32
            if fn()!=expected:
                return None
        fn=lib.tfs_armor_query
        fn.argtypes=[C.POINTER(Part),C.c_uint32,C.c_void_p,C.c_uint32,C.POINTER(C.c_uint32)]
        fn.restype=C.c_int32
        lib._query_signature=tuple(fn.argtypes)
        lib._query_source=fn
        lib._query_call=fn
        if _numerical_contract(lib):
            _LIBRARY=lib
            lib._marker_spheres_source=None
            marker=getattr(lib,'tfs_marker_spheres',None)
            version=getattr(lib,'tfs_marker_spheres_abi',None)
            if marker is not None and version is not None:
                version.argtypes=[];version.restype=C.c_uint32
                if version()==1:
                    marker.argtypes=[C.c_void_p,C.c_uint32,C.c_void_p,C.c_uint32,C.c_void_p]
                    marker.restype=C.c_int32
                    lib._marker_spheres_source=marker
                    lib._marker_spheres_signature=tuple(marker.argtypes)
                    if not _marker_spheres_contract(lib):
                        lib._marker_spheres_source=None
    except (AttributeError,OSError,TypeError,ValueError):
        pass
    return _LIBRARY


def marker_spheres(lib,rays,spheres):
    """Conservative misses only; all survivors retain the original predicate."""
    fn=getattr(lib,'_marker_spheres_source',None)
    if (not _functions_unchanged()
            or fn is None or lib.tfs_marker_spheres is not fn or fn.argtypes is None
            or tuple(fn.argtypes)!=lib._marker_spheres_signature
            or fn.restype is not C.c_int32 or getattr(fn,'errcheck',None) is not None
            or any(type(a) is not np.ndarray or a.dtype!=_F64 or a.ndim!=2
                   or not a.flags.c_contiguous or not a.flags.aligned for a in (rays,spheres))
            or rays.shape[1]!=7 or spheres.shape[1]!=4
            or not 1<=len(rays)<=64 or not 1<=len(spheres)<=256):
        return None
    output=np.empty((len(rays),len(spheres)),dtype=_U8)
    status=fn(rays.ctypes.data,len(rays),spheres.ctypes.data,len(spheres),output.ctypes.data)
    if status:
        return None
    if np.any(output>1):
        raise RuntimeError('Invalid native marker sphere result')
    return output


def _marker_spheres_contract(lib):
    rays=np.array(((0.,0.,0.,0.,0.,1.,10.),(0.,0.,0.,1e-8,0.,0.,10.),
                   (999000.,999000.,999000.,-1.,0.,0.,999000.)))
    spheres=np.array(((1.,0.,10.,1.),(0.,0.,11.,1.),(0.,0.,5.,1.),
                     (998000.,999001.,999000.,1.),(100.,100.,100.,1.)))
    flags=marker_spheres(lib,rays,spheres)
    if flags is None or not np.any(flags==0):
        return False
    for i,row in enumerate(rays):
        origin=tuple(map(float,row[:3]));direction=tuple(map(float,row[3:6]))
        length2=sum(d*d for d in direction)
        for j,target in enumerate(spheres):
            delta=tuple(c-o for c,o in zip(target[:3],origin))
            t=max(0.,min(float(row[6]),sum(c*d for c,d in zip(delta,direction))/length2))
            if not flags[i,j] and sum((c-d*t)**2 for c,d in zip(delta,direction))<=target[3]:
                return False
    return True

def query(parts,origin,direction,turret_rot,turret_position,gun_position,pitch,distance,*,matrix_fn):
    """Return prepared transforms and original-order hits, or None for NumPy."""
    if matrix_fn is not matrix or not _functions_unchanged():
        return None
    if (type(distance) not in (float,int,np.float64,np.float32)
            or any(type(v) is not np.ndarray or v.shape!=(3,) or v.dtype!=_F64
                   for v in (origin,direction))):
        return None
    if not 1<=len(parts)<=64:
        return None
    total=0
    for name,triangles,ids,thickness,factors in parts:
        if (type(name) is not str or type(triangles) is not np.ndarray
                or triangles.dtype!=_F64 or triangles.ndim!=3
                or triangles.shape[1:]!=(3,3) or not len(triangles)
                or not triangles.flags.c_contiguous or not triangles.flags.aligned):
            return None
        total+=len(triangles)
    if total>65536:
        return None
    lib=load_library()
    if lib is None:
        return None
    fn=lib.tfs_armor_query
    if (fn is not lib._query_source or fn.argtypes is None
            or tuple(fn.argtypes)!=lib._query_signature or fn.restype is not C.c_int32):
        return None
    descriptors=(Part*len(parts))()
    prepared=[]
    for index,part in enumerate(parts):
        name,triangles,ids,thickness,factors=part
        if name.startswith('turret'):
            local_rot,offset=turret_rot,turret_position
        elif name.startswith('gun'):
            local_rot=turret_rot@matrix(0,pitch,0)
            offset=turret_position+turret_rot@gun_position
        else:
            local_rot,offset=np.eye(3),np.zeros(3)
        o=(origin-offset)@local_rot
        d=direction@local_rot
        _PART_PACK.pack_into(descriptors,index*72,triangles.ctypes.data,len(triangles),
                             *o,*d,distance)
        prepared.append((part,local_rot))
    hits=_invoke(lib,descriptors,total)
    return (prepared,hits) if hits is not None else None
