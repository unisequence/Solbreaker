"""Pinned native cosmetic shot segments; no penetration or HP calculation."""
from dataclasses import dataclass
from functools import lru_cache
import math
from numbers import Real
from pathlib import Path
import struct

import numpy as np
import yaml

from .dvpl import read
from .orientation import matrix


def _finite(values):
    return all(isinstance(value, Real) and not isinstance(value, bool)
               and math.isfinite(value) for value in values)


def pack_segment(kind, component, bounds, point, direction):
    """Quantize a real surface point and its incoming ray in a component AABB.

    Native uses the two points to reconstruct impact position and direction.
    The second is a ray endpoint clipped to this box, not an armor exit.
    Coordinates and bounds use DAVA XYZ (Z up).
    """
    if (type(kind) is not int or not 0 <= kind <= 4 or type(component) is not int
            or not 0 <= component < 4 or len(bounds) != 6 or len(point) != 3
            or len(direction) != 3 or not _finite((*bounds,*point,*direction))):
        raise ValueError('Invalid cosmetic shot segment')
    low,high=bounds[:3],bounds[3:]
    size=tuple(b-a for a,b in zip(low,high))
    if min(size)<=0 or math.hypot(*direction)<1e-9:
        return None
    # A surface outside the part cannot be encoded faithfully. Permit only
    # half a quantization cell for floating-point/mesh boundary disagreement.
    if any(p<a-s/510 or p>b+s/510 for p,a,b,s in zip(point,low,high,size)):
        return None
    first=tuple(max(a,min(b,p)) for p,a,b in zip(point,low,high))
    limits=[((b-p)/d if d>0 else (a-p)/d) for p,d,a,b in zip(first,direction,low,high) if abs(d)>1e-12]
    distance=min(limits)
    if distance<=0:return None
    second=tuple(p+d*distance for p,d in zip(first,direction))
    def quantize(p):return tuple(max(0,min(255,int((v-a)/s*255+.5))) for v,a,s in zip(p,low,size))
    a,b=quantize(first),quantize(second)
    if a==b:return None
    # DecodeShotSegment uses x,z,y byte order for each DAVA point.
    return int.from_bytes(bytes((kind,component,a[0],a[2],a[1],b[0],b[2],b[1])),'little')


@dataclass(frozen=True, slots=True)
class PartFrame:
    name: str
    component: int
    bounds: tuple
    # Native model preparation recentres source geometry with these values.
    # The decoded bbox is already part-local; do not add this again at impact.
    points: tuple


@dataclass(frozen=True, slots=True)
class Frames:
    parts: tuple
    hull_position: tuple
    turret_position: tuple
    gun_position: tuple

    def encode(self, part, pointBW, directionBW, state):
        """Return geometry with a zero kind byte, from an impact-time BodyState.

        ``state.rotation`` carries the real dynamic attitude through flips.
        The clipped second point describes the incoming ray, never armor exit.
        """
        frame = next((entry for entry in self.parts if entry.name == part or
            entry.component == 0 and part in ('chassis_track_L','chassis_track_R')), None)
        if frame is None:
            return None
        if len(pointBW) != 3 or len(directionBW) != 3 or not _finite((*pointBW,*directionBW)):
            raise ValueError('Invalid cosmetic impact ray')
        rotation = state.rotation
        point = (np.asarray(pointBW)-state.pose.position) @ rotation
        direction = np.asarray(directionBW) @ rotation
        if frame.component == 1:
            point -= self.hull_position
        elif frame.component >= 2:
            turret = matrix(state.turret)
            point = (point-self.turret_position) @ turret
            direction = direction @ turret
            if frame.component == 3:
                pitch = matrix(0.,state.pitch)
                point = (point-self.gun_position) @ pitch
                direction = direction @ pitch
        return pack_segment(0,frame.component,frame.bounds,
                            point[[0,2,1]],direction[[0,2,1]])


def load_frames(game, definition):
    """Load immutable native bbox metadata once per installed configuration."""
    names = (definition.chassis.geometry or 'chassis', definition.hull.geometry,
             definition.turret.geometry, definition.gun.geometry)
    return _load_frames(Path(game), definition.nation, definition.name, names,
        tuple(definition.hull_position), tuple(definition.aiming.turret_position),
        tuple(definition.aiming.gun_position))


@lru_cache(maxsize=128)
def _load_frames(game, nation, vehicle, names, hull, turret, gun):
    # These are the four direct bbox fields observed in the pinned Vehicle;
    # SC2 visual/armor extents are different and are not substitutes.
    try:
        document = yaml.safe_load(read(game/f'Data/3d/Tanks/Parameters/{nation}/{vehicle}.yaml.dvpl'))
    except (OSError, yaml.YAMLError):
        return Frames((),hull,turret,gun)
    collision = document.get('collision', {}) if isinstance(document, dict) else {}
    frames = []
    for component, name in enumerate(names):
        entry = collision.get(name) if isinstance(collision, dict) else None
        if not isinstance(entry, dict) or not isinstance(entry.get('bbox'), dict):
            continue
        low,high = entry['bbox'].get('min'),entry['bbox'].get('max')
        points = entry.get('points')
        if (not all(isinstance(value,(list,tuple)) and len(value)==3 for value in (low,high,points))
                or not _finite((*low,*high,*points)) or any(a>=b for a,b in zip(low,high))):
            continue
        try:
            # The client parses these YAML fields as binary32. Keep its bbox
            # exactly, rather than the decimal parser's binary64 approximation.
            bounds = struct.unpack('<6f',struct.pack('<6f',*low,*high))
            offset = struct.unpack('<3f',struct.pack('<3f',*points))
        except (OverflowError,struct.error):
            continue
        if _finite((*bounds,*offset)) and all(a<b for a,b in zip(bounds[:3],bounds[3:])):
            frames.append(PartFrame(name,component,bounds,offset))
    return Frames(tuple(frames),hull,turret,gun)


load_frames.cache_clear = _load_frames.cache_clear
load_frames.cache_info = _load_frames.cache_info
