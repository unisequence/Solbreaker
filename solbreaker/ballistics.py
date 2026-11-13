"""Flying shells and continuous relative-translation armor queries.

No future damage is stored. Targets are sampled over the same physics interval
as each shell. Translation is swept continuously; rigid hull attitude uses a
normalized shortest-path quaternion SLERP.  Rotation is sampled at the midpoint
of substeps no longer than 1/240 s, and is further divided by angular distance
so a high angular velocity cannot silently turn through a target between two
samples.  Turret yaw and gun pitch remain independent local joints.
Gravity's maximum chord deviation is under 0.022 mm per substep at 9.81 m/s².
"""
from dataclasses import InitVar, dataclass, field, replace
import math
import struct

import numpy as np
from .vector_math import vector_length as _vector_length

from .armor import (ArmorHit, ArmorModel, MovableArmorStateSnapshot, PenetrationState,
                    Shell, ShotResult, resolve_shell)
from .collision import Pose
from .orientation import matrix, quaternion


MAX_STEP = 1/240
MISSILE_TURN_MULTIPLIER = 2.4
# A target's curved surface can move several centimetres during a very fast
# free-flight tumble.  Bound the *attitude* increment separately from time: a
# midpoint ray sample stays within 0.75 degrees of either endpoint.  This is
# intentionally conservative and has no effect on normal tracked movement.
MAX_ROTATION_STEP = math.radians(1.5)


def _normalized_quaternion(value):
    """Return a finite unit (x, y, z, w) quaternion or fail closed."""
    value = np.asarray(value, dtype=float)
    if value.shape != (4,) or not np.all(np.isfinite(value)):
        raise ValueError('Invalid rigid-body orientation')
    length = _vector_length(value)
    if length <= 1e-12:
        raise ValueError('Zero rigid-body orientation')
    return tuple(float(item/length) for item in value)


def _quaternion_matrix(value):
    """Local-to-world rotation for the server's row-vector convention."""
    x,y,z,w = _normalized_quaternion(value)
    return np.array(((1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)),
                     (2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)),
                     (2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y))), dtype=float)


def _quaternion_slerp(left, right, fraction):
    """Shortest-path SLERP, including the numerically linear near-zero case."""
    if not 0 <= fraction <= 1:
        raise ValueError('Invalid interpolation fraction')
    left = np.asarray(_normalized_quaternion(left))
    right = np.asarray(_normalized_quaternion(right))
    dot = float(np.dot(left,right))
    if dot < 0:
        right = -right
        dot = -dot
    dot = min(1., max(-1., dot))
    if dot > .9995:
        return _normalized_quaternion(left+(right-left)*fraction)
    theta = math.acos(dot)
    sine = math.sin(theta)
    return _normalized_quaternion((math.sin((1-fraction)*theta)/sine)*left+
                                  (math.sin(fraction*theta)/sine)*right)


def _quaternion_angle(left, right):
    """Unsigned shortest rigid-attitude difference in radians."""
    left = _normalized_quaternion(left)
    right = _normalized_quaternion(right)
    return 2*math.acos(min(1., abs(sum(a*b for a,b in zip(left,right)))))


def _pose_from_rotation(position, rotation):
    """Compatibility Euler pose for systems that have not accepted a matrix.

    This does not participate in armor collision.  The full quaternion remains
    on ``BodyState.orientation`` and must be passed as ``rotation`` to armor.
    Euler coordinates are only a legacy carrier for network-facing callers.
    """
    # Matrix is the exact convention used by orientation.matrix().  This is a
    # canonical representative: unlike interpolation, a gimbal-lock choice is
    # harmless because the matrix itself stays attached to BodyState.
    pitch = math.asin(max(-1., min(1., -float(rotation[1,2]))))
    cp = math.cos(pitch)
    if abs(cp) > 1e-8:
        yaw = math.atan2(float(rotation[0,2]),float(rotation[2,2]))
        roll = math.atan2(float(rotation[1,0]),float(rotation[1,1]))
    else:
        yaw = math.atan2(float(-rotation[2,0]),float(rotation[0,0]))
        roll = 0.
    return Pose(tuple(float(item) for item in position),yaw,pitch,roll)


@dataclass(frozen=True)
class BodyState:
    pose: Pose
    turret: float = 0.
    pitch: float = 0.
    # A dynamic body may not have a stable Euler representation through a
    # flip.  Keep the original unit quaternion beside the legacy Pose.
    orientation: tuple | None = None
    # An immutable, complete observation of a movable-armor controller.  It
    # belongs to the physics frame rather than the mutable Vehicle so a shell
    # cannot accidentally query a later armor configuration.
    armor_state: MovableArmorStateSnapshot | None = None
    _defer_orientation: InitVar[bool] = False

    def __post_init__(self, defer_orientation):
        orientation = object.__getattribute__(self, 'orientation')
        if orientation is None and defer_orientation:
            if not all(math.isfinite(value) for value in
                       (self.pose.yaw, self.pose.pitch, self.pose.roll)):
                raise ValueError('Invalid rigid-body orientation')
        else:
            if orientation is None:
                orientation = quaternion(self.pose.yaw,self.pose.pitch,self.pose.roll)
            object.__setattr__(self,'orientation',_normalized_quaternion(orientation))
        if self.armor_state is not None and not isinstance(self.armor_state,MovableArmorStateSnapshot):
            raise ValueError('Invalid movable armor state')

    def __getattribute__(self, name):
        value = object.__getattribute__(self, name)
        if name == 'orientation' and value is None:
            pose = object.__getattribute__(self, 'pose')
            value = _normalized_quaternion(quaternion(pose.yaw,pose.pitch,pose.roll))
            object.__setattr__(self,'orientation',value)
        return value

    @property
    def rotation(self):
        return _quaternion_matrix(self.orientation)


def body_states(vehicles):
    return {e:BodyState(v.pose,v.gun.yaw,v.gun.pitch,
                        armor_state=getattr(v,'movable_armor_state',None),
                        _defer_orientation=True)
            for e,v in vehicles.items()}


class SweepCache:
    """Reuse immutable body motion within exactly one armor interval."""
    def __init__(self, before, after):
        self.before, self.after = before, after
        self._angles = {}
        self._translations = {}
        self._samples = {}

    def check(self, before, after):
        if self.before is not before or self.after is not after:
            raise ValueError('Armor sweep cache belongs to another interval')

    def angle(self, entity, old, new):
        entry = self._angles.get(entity)
        if entry is None or entry[0] is not old or entry[1] is not new:
            entry = (old, new, _quaternion_angle(old.orientation, new.orientation))
            self._angles[entity] = entry
        return entry[2]

    def sample(self, entity, old, new, fraction):
        key = (entity, id(old), id(new), type(fraction), fraction, struct.pack('<d',fraction))
        entry = self._samples.get(key)
        if entry is None:
            middle = interpolate(old,new,fraction)
            rotation = middle.rotation
            rotation.flags.writeable = False
            entry = (old,new,middle,rotation)
            self._samples[key] = entry
        # Keep the caller's historical ownership of its rotation argument.
        return entry[2],entry[3].copy()

    def translation(self, entity, old, new, left, right, fraction_key):
        key = (entity, id(old), id(new), fraction_key)
        entry = self._translations.get(key)
        if entry is None:
            a = tuple(x+(y-x)*left for x,y in zip(old.pose.position,new.pose.position))
            b = tuple(x+(y-x)*right for x,y in zip(old.pose.position,new.pose.position))
            center = np.asarray(a)
            displacement = np.asarray(b)-a
            center.flags.writeable = displacement.flags.writeable = False
            # Retain both states so their identity cannot be recycled while
            # this entry exists, even if a caller replaces a mapping entry.
            entry = (old, new, (a, center, displacement))
            self._translations[key] = entry
        return entry[2]


def angle_between(a,b,fraction):
    return a+((b-a+math.pi)%math.tau-math.pi)*fraction


def interpolate(a,b,fraction):
    p,q = a.pose,b.pose
    orientation = _quaternion_slerp(a.orientation,b.orientation,fraction)
    position = tuple(x+(y-x)*fraction for x,y in zip(p.position,q.position))
    # A collision segment cannot safely combine two controller configurations.
    # Passing ``None`` into a movable ArmorModel is deliberately rejected, so
    # a transition that crosses this ballistic frame fails closed instead of
    # silently raycasting either endpoint's plates.
    armor_state = a.armor_state if a.armor_state == b.armor_state else None
    return BodyState(_pose_from_rotation(position,_quaternion_matrix(orientation)),
                     angle_between(a.turret,b.turret,fraction),
                     angle_between(a.pitch,b.pitch,fraction),orientation,armor_state)


@dataclass(frozen=True)
class Impact:
    at: float
    shot: int
    result: ShotResult
    point: tuple
    obstacle: int | None
    direction: tuple
    shooter: int
    target: int | None
    shell: Shell | None = None
    collision: bool = False
    # Target-relative segment used by armor_contacts, separate from the world
    # tangent sent to native impact FX. None for terrain/range/external ends.
    relative_direction: tuple | None = None
    # Unit rigid-body attitude at the actual armor contact.  This is carried
    # separately from Pose so downstream HE/effect consumers can opt into the
    # exact dynamic orientation without treating canonical Euler fields as a
    # source of truth.  Static/map impacts deliberately keep it absent.
    target_orientation: tuple | None = None
    # Immutable movable-armor controller snapshot at the contact.  Kept
    # alongside the orientation so delayed HE/effect consumers do not read a
    # subsequently transitioned Vehicle.
    target_armor_state: MovableArmorStateSnapshot | None = None
    # Reuse the already interpolated contact frame for cosmetic hit encoding.
    # A later event consumer must not sample a tank after it has moved/turned.
    target_state: BodyState | None = None
    contact_direction: tuple | None = None

    def __post_init__(self):
        if (self.target_armor_state is not None and
                not isinstance(self.target_armor_state,MovableArmorStateSnapshot)):
            raise ValueError('Invalid target movable armor state')
        if self.target_state is not None and not isinstance(self.target_state,BodyState):
            raise ValueError('Invalid target contact frame')


@dataclass(frozen=True)
class TrackContact:
    """First contact of one shell with an external module: a track or the gun.

    The gun is the target definition's real gun collision geometry, never a
    breech or other internal volume.
    """
    at: float
    shot: int
    shooter: int
    target: int
    hit: ArmorHit
    shell: Shell
    penetrated: bool


@dataclass(frozen=True)
class ReconContact:
    """First external contact per target, collected only when recon is enabled."""
    at: float
    shot: int
    shooter: int
    target: int


@dataclass
class Projectile:
    shot: int
    shooter: int
    shell: Shell
    position: tuple
    velocity: tuple
    time: float
    distance: float = 0.
    penetration: PenetrationState = field(default_factory=PenetrationState)
    last_external: tuple | None = None
    _seen_tracks: set[tuple[int, str]] = field(default_factory=set)
    _pending_track_contacts: list[TrackContact] = field(default_factory=list)
    recon_tracking: bool = False
    _seen_recon_targets: set[int] = field(default_factory=set)
    _pending_recon_contacts: list[ReconContact] = field(default_factory=list)
    # Updated only at an actual target contact.  This preserves the exact
    # state that produced an Impact while the projectile itself remains live.
    target_armor_state: MovableArmorStateSnapshot | None = None
    last_external_frame: tuple | None = None

    def __post_init__(self):
        if (self.target_armor_state is not None and
                not isinstance(self.target_armor_state,MovableArmorStateSnapshot)):
            raise ValueError('Invalid target movable armor state')

    def drain_track_contacts(self):
        contacts = tuple(self._pending_track_contacts)
        self._pending_track_contacts.clear()
        return contacts

    def drain_recon_contacts(self):
        contacts = tuple(self._pending_recon_contacts)
        self._pending_recon_contacts.clear()
        return contacts

    def impact(self, point, direction, *, result=None, target=None, obstacle=None, at=None, collision=False,
               relative_direction=None, target_orientation=None, target_armor_state=None,
               target_state=None):
        contact_direction = None
        if result is None:
            if self.last_external:
                target,result = self.last_external
                if self.last_external_frame is not None:
                    target_state,contact_direction = self.last_external_frame
            else:
                result = ShotResult('miss',None)
        return Impact(self.time if at is None else at,self.shot,result,tuple(point),obstacle,
                      tuple(direction),self.shooter,target,self.shell,collision,
                      tuple(map(float,relative_direction)) if relative_direction is not None else None,
                      _normalized_quaternion(target_orientation) if target_orientation is not None else None,
                      self.target_armor_state if target_armor_state is None and target is not None
                      else target_armor_state, target_state, contact_direction)


# Restrict the optional rejection to finite binary64 game coordinates. Its
# padding widens only this preliminary test; every survivor still takes the
# original sphere test and precise armor query with the original radius.
_AXIS_F64 = np.dtype('float64')


def _axis_sweep_input_ok(start,delta,limit):
    return (type(start) is np.ndarray and type(delta) is np.ndarray
        and start.shape==delta.shape==(3,) and start.dtype==delta.dtype==_AXIS_F64
        and type(limit) in (float,int,np.float64,np.float32)
        and 0.<=limit<=1. and all(abs(float(x))<1e6 for x in (*start,*delta)))


def _axis_sweep_miss(cache,center,shift,start,delta,limit,model):
    # Do not newly evaluate a lazy radius property before the historical
    # zero-length check, or evaluate it twice on surviving targets.
    if (type(model) is not ArmorModel or 'radius' in ArmorModel.__dict__
            or ArmorModel.__getattribute__ is not object.__getattribute__):
        return False
    radius=model.__dict__.get('radius')
    if type(radius) not in (float,int,np.float64):return False
    if not 0.<=radius<1e6:return False
    radius,limit=float(radius),float(limit)
    memo=getattr(cache,'_axis_ranges',None)
    if memo is None:cache._axis_ranges=memo={}
    key=id(center),id(shift)
    data=memo.get(key)
    if data is None:
        good=(center.shape==shift.shape==(3,) and center.dtype==shift.dtype==_AXIS_F64
            and not center.flags.writeable and not shift.flags.writeable
            and all(abs(float(v))<1e6 for v in (*center,*shift)))
        data=(center,shift,tuple(map(float,center)) if good else (),
              tuple(map(float,shift)) if good else (),good)
        memo[key]=data
    if not data[4]:return False
    # Coordinates and displacements are bounded to 1e6; 10 micrometres
    # greatly exceeds accumulated binary64 endpoint/norm rounding here.
    # This widens only this optional prefilter; the old sphere test is
    # still evaluated for every survivor, with its original radius.
    bound=radius+1e-5
    c,d=data[2:4]
    for axis in (0,2,1):
        p=float(start[axis]);v=float(delta[axis])-d[axis]
        first=p-c[axis];last=(p+v*limit)-c[axis]
        if (first>bound and last>bound) or (first< -bound and last< -bound):
            return True
    return False


def armor_contacts(world, projectile, start, delta, before, after, left, right, limit,
                   *, sweep_cache=None):
    """Return sorted contacts as fractions of this shell segment, not ray range."""
    if not 0 <= left <= 1 or not 0 <= right <= 1:
        raise ValueError('Invalid interpolation fraction')
    if sweep_cache is not None:
        sweep_cache.check(before,after)
        fraction_key = (type(left),type(right),left,right,struct.pack('<2d',left,right))
    contacts = []
    axis_ok = type(sweep_cache) is SweepCache and _axis_sweep_input_ok(start,delta,limit)
    for entity,old in before.items():
        if entity == projectile.shooter:
            continue
        model = world.assets_for(entity).armor
        new = after[entity]
        # The spherical broadphase depends only on translation. Use the exact
        # same position arithmetic as interpolate, and defer SLERP/armor state
        # sampling until a shell segment actually approaches this target.
        if sweep_cache is None:
            a = tuple(x+(y-x)*left for x,y in zip(old.pose.position,new.pose.position))
            b = tuple(x+(y-x)*right for x,y in zip(old.pose.position,new.pose.position))
            displacement = np.asarray(b)-a
        else:
            a,center,displacement = sweep_cache.translation(entity,old,new,left,right,fraction_key)
        # Freeze the target at its initial position and subtract its translation
        # from the shell segment. This detects crossing even if neither endpoint
        # overlaps the tank. Use midpoint orientation to bound rotation error.
        if axis_ok and _axis_sweep_miss(sweep_cache,center,displacement,start,delta,limit,model):
            continue
        relative = delta-displacement
        length = _vector_length(relative)
        if length < 1e-9:
            continue
        if sweep_cache is None:
            center = np.asarray(a)
        fraction = min(limit,max(0.,float(np.dot(center-start,relative))/(length*length)))
        if _vector_length(start+relative*fraction-center) > model.radius:
            continue
        if sweep_cache is None:
            middle = interpolate(old,new,(left+right)/2)
            rotation = middle.rotation
        else:
            middle,rotation = sweep_cache.sample(entity,old,new,(left+right)/2)
        pose = replace(middle.pose,position=a)
        hits = model.intersections(start,relative/length,pose,middle.turret,middle.pitch,
                                   distance=length*limit,rotation=rotation,
                                   movable_state=middle.armor_state)
        actual_length = _vector_length(delta)
        for hit in hits:
            f = hit.distance/length
            contacts.append((f,entity,replace(hit,
                distance=projectile.distance+actual_length*f,
                point=tuple(map(float,start+delta*f))),relative/length))
    return sorted(contacts,key=lambda item:(item[0],item[1]))


def _missile_velocity(projectile, position, velocity, target, dt):
    flight = projectile.shell.missile
    speed = _vector_length(velocity)
    direction = velocity/speed
    turn = 0.
    if target is not None:
        offset = np.asarray(target) - position
        # A point behind the rocket cannot turn it around after an overshoot.
        ahead = float(np.dot(direction, offset))
        if ahead > 0:
            lateral = offset - direction*ahead
            side = _vector_length(lateral)
            if side > 1e-9:
                turn = min(math.atan2(side,ahead), flight.turn_speed*MISSILE_TURN_MULTIPLIER*dt)
                direction = direction*math.cos(turn) + lateral*(math.sin(turn)/side)
    speed = min(flight.max_speed, max(projectile.shell.speed,
        speed + flight.acceleration*dt - flight.rotational_deceleration*turn))
    return direction*speed


def advance(projectile, world, before, after, dt, rng, *, sweep_cache=None, missile_target=None):
    """Advance one shell over this world step; return only an actual contact/end."""
    # A free rigid body can rotate far faster than a tracked hull.  Keep the
    # same time cap for gravity and translational sweep, plus a strictly bound
    # angular cap for the midpoint armor samples.
    if sweep_cache is not None:
        sweep_cache.check(before,after)
    max_turn = max((sweep_cache.angle(entity,before[entity],after[entity]) if sweep_cache is not None
                    else _quaternion_angle(before[entity].orientation,after[entity].orientation)
                    for entity in before if entity != projectile.shooter and entity in after), default=0.)
    query_options = {'sweep_cache':sweep_cache} if sweep_cache is not None else {}
    count = max(1,math.ceil(dt/MAX_STEP),math.ceil(max_turn/MAX_ROTATION_STEP))
    h = dt/count
    acceleration = np.array((0.,-projectile.shell.gravity,0.))
    for i in range(count):
        start = np.asarray(projectile.position)
        velocity = np.asarray(projectile.velocity)
        if projectile.shell.missile is not None:
            velocity = _missile_velocity(projectile,start,velocity,missile_target,h)
        delta = velocity*h+acceleration*(h*h/2)
        length = _vector_length(delta)
        if length < 1e-12:
            projectile.time += h
            projectile.velocity = tuple(map(float,velocity+acceleration*h))
            continue
        remaining = projectile.shell.max_distance-projectile.distance
        range_fraction = min(1.,max(0.,remaining/length))
        end = start+delta*range_fraction
        body,wall_fraction,point = world.static_segment(tuple(start),tuple(end)) if world else (None,1.,tuple(end))
        limit = wall_fraction*range_fraction
        if world:
            for fraction,target,hit,relative_direction in armor_contacts(world,projectile,start,delta,before,after,
                                                      i/count,(i+1)/count,limit,**query_options):
                tangent = velocity+acceleration*(h*fraction)
                tangent /= np.linalg.norm(tangent)
                # Contact data keeps its historical compact tuple shape for
                # track observers.  Recover the exact target attitude here,
                # rather than leaking an Euler midpoint through Impact.
                target_state = (interpolate(before[target],after[target],(i+fraction)/count)
                                if target in before and target in after else None)
                projectile.target_armor_state = (target_state.armor_state if target_state else None)
                result = resolve_shell([hit],relative_direction,projectile.shell,rng,
                                    state=projectile.penetration,target=target)
                if projectile.recon_tracking and hit.external and target not in projectile._seen_recon_targets:
                    projectile._seen_recon_targets.add(target)
                    projectile._pending_recon_contacts.append(ReconContact(
                        projectile.time+h*fraction,projectile.shot,projectile.shooter,target))
                key = (target,hit.part)
                gun = getattr(getattr(world.assets_for(target),'definition',None),'gun',None)
                if (hit.external and (hit.part in ('chassis_track_L','chassis_track_R')
                        or gun is not None and hit.part == gun.geometry)
                        and key not in projectile._seen_tracks):
                    projectile._seen_tracks.add(key)
                    projectile._pending_track_contacts.append(TrackContact(
                        projectile.time+h*fraction,projectile.shot,projectile.shooter,
                        target,hit,projectile.shell,result.outcome in ('external','penetrated')))
                if projectile.shell.kind=='HIGH_EXPLOSIVE':
                    # HE detonates at the first surface, including a track or
                    # gun. Blast resolution happens at this actual contact time.
                    return projectile.impact(hit.point,tangent,result=result,target=target,
                                             at=projectile.time+h*fraction,collision=True,
                                             relative_direction=relative_direction,
                                             target_orientation=(target_state.orientation if target_state else None),
                                             target_armor_state=projectile.target_armor_state,
                                             target_state=target_state)
                if result.outcome == 'external':
                    projectile.last_external = (target,result)
                    projectile.last_external_frame = (target_state,tuple(map(float,tangent)))
                elif result.outcome != 'miss':
                    return projectile.impact(hit.point,tangent,result=result,target=target,
                                             at=projectile.time+h*fraction,collision=True,
                                             relative_direction=relative_direction,
                                             target_orientation=(target_state.orientation if target_state else None),
                                             target_armor_state=projectile.target_armor_state,
                                             target_state=target_state)
        if (body is not None and world is not None
                and body in world.breakable_bodies
                and projectile.shell.kind in ('ARMOR_PIERCING','ARMOR_PIERCING_CR')):
            # Remove the struck prop before tracing the rest of this same
            # substep. A tank immediately behind it must not be skipped.
            contact_velocity = velocity+acceleration*(h*limit)
            direction = contact_velocity/np.linalg.norm(contact_velocity)
            world.destroy(world.breakable_bodies[body],tuple(direction[::2]))
            projectile.penetration.prop_factor *= .95
            projectile.position = tuple(point)
            projectile.velocity = tuple(map(float,contact_velocity))
            projectile.distance += length*limit
            projectile.time += h*limit
            remainder = h*(1.-limit)
            if remainder > 1e-9:
                segment_before = {entity:interpolate(old,after[entity],(i+limit)/count)
                                  for entity,old in before.items() if entity in after}
                segment_after = {entity:interpolate(old,after[entity],(i+1)/count)
                                 for entity,old in before.items() if entity in after}
                impact = advance(projectile,world,segment_before,segment_after,remainder,rng,
                                 missile_target=missile_target)
                if impact is not None:
                    return impact
            continue
        if body is not None or remaining <= length+1e-9:
            tangent = velocity+acceleration*(h*limit)
            tangent /= np.linalg.norm(tangent)
            return projectile.impact(point,tangent,at=projectile.time+h*limit,
                obstacle=world.breakable_bodies.get(body) if world else None,collision=body is not None)
        projectile.position = tuple(map(float,end))
        projectile.velocity = tuple(map(float,velocity+acceleration*h))
        projectile.distance += length
        projectile.time += h
    return None
