"""Battle-scoped sandbox controls and damped flight targets."""
from dataclasses import dataclass
import math
import struct

from .mercury import variable8

PREFIX = b'TFS-LAB-S'
INPUT = struct.Struct('<IIIIfff')  # battle token, sequence, flags, shot, direction
BOOST, FLIGHT, ASCEND, DESCEND, MISSILE_VIEW, SPAWN_TARGET = (1, 2, 4, 8, 16, 32)
GRAVITY_JUMP = 64
INPUT_LEASE = .85
BOOST_POWER = 2.4
BOOST_SPEED = 1.75


def token(arena):
    return arena.unique_id & 0xffffffff or 1


def advertisement(arena):
    # Avatar method 37 carries two vectors. The patched client consumes this
    # sentinel before the native method sees it.
    second = (b'\1'+struct.pack('<I', 16 | (15 if arena.sandbox_enabled else 0))
              if getattr(arena, 'gravity_mode', False) is True else b'\0')
    return b'\x13' + variable8(96, b'\x03TFS1SBOX' + struct.pack('<I', token(arena)) + second)


def receive(endpoint, addr, client, arena, body, now):
    """Consume sandbox reports on the authenticated current battle session."""
    if not body.startswith(PREFIX):
        return False
    if (arena is None or not (arena.sandbox_enabled or getattr(arena, 'gravity_mode', False) is True)
            or client.reconnect_replay
            or not client.battle_spawned or body[-1:] != b'\x01'):
        return True
    raw = body[len(PREFIX):-1]
    try:
        control = Control.decode(raw)
    except ValueError:
        return True
    previous = client.sandbox_input
    current = token(arena)
    if previous is not None and previous[0] == current and now-previous[2] < .025:
        return True
    if control.token == 0:
        if not control.flags and not control.shot and client.reliable_window_used < 200:
            client.sandbox_input = (current, None, now, 0)
            endpoint.send(addr, client, advertisement(arena))
        return True
    if control.token != current or previous is None or previous[0] != current:
        return True
    if previous[1] is not None:
        distance = (control.sequence-previous[1]) & 0xffffffff
        if not 0 < distance < (1 << 31):
            return True
    client.sandbox_input = (current, control.sequence, now, control.flags)
    if (control.flags ^ previous[3]) & (BOOST | FLIGHT | MISSILE_VIEW):
        endpoint.server.event('sandbox_controls', account_id=client.account_id,
                              arena_id=arena.result_id, flags=control.flags)
    if getattr(arena, 'owner_backed', False):
        endpoint.owner_service.sandbox(addr, client, raw)
    else:
        arena.sandbox_control(client.account_id, control)
    return True


@dataclass(frozen=True)
class Control:
    token: int
    sequence: int
    flags: int
    shot: int
    x: float
    y: float
    z: float

    @classmethod
    def decode(cls, data):
        if len(data) != INPUT.size:
            raise ValueError('Invalid sandbox control size')
        control = cls(*INPUT.unpack(data))
        length = math.hypot(control.x, control.y, control.z)
        if (control.flags & ~127 or not math.isfinite(length) or length > 10000.
                or control.flags & MISSILE_VIEW and (not control.shot or length < .01)):
            raise ValueError('Invalid sandbox control')
        return control

    def direction(self):
        length = math.hypot(self.x, self.y, self.z)
        return self.x/length, self.y/length, self.z/length


@dataclass
class Flight:
    height: float


def _approach(value, target, rate, dt):
    return value + max(-rate*dt, min(rate*dt, target-value))


def _flight_speed(target, position, lower, upper):
    # Begin braking within the distance needed to stop at 10 m/s².
    forward = math.sqrt(20.*max(0., upper-position-7.))
    reverse = math.sqrt(20.*max(0., position-lower-7.))
    return max(-reverse, min(forward, target))


def flight_velocity(flight, pose, velocity, spin, ground, drive, flags, gravity, dt, *, bounds):
    """Acceleration limits, hover/landing and a spring-damped hull attitude."""
    airborne = bool(flags & FLIGHT)
    thrust = int(bool(drive & 1))-int(bool(drive & 2)) if airborne else 0
    turn = int(bool(drive & 8))-int(bool(drive & 4)) if airborne else 0
    climb = int(bool(flags & ASCEND))-int(bool(flags & DESCEND))
    if airborne:
        flight.height = max(ground+1., min(ground+95., flight.height+climb*9.*dt))
        vertical = max(-8., min(9., 1.4*(flight.height-pose.position[1])))
    else:
        vertical = -3.
    fx, fz = math.sin(pose.yaw), math.cos(pose.yaw)
    speed = thrust*(25. if thrust > 0 else 12.) * (1.25 if flags & BOOST else 1.)
    vx, vy, vz = velocity
    x, _, z = pose.position
    min_x, min_z, max_x, max_z = bounds
    target_x = _flight_speed(fx*speed, x, min_x, max_x)
    target_z = _flight_speed(fz*speed, z, min_z, max_z)
    desired = (_approach(vx, target_x, 10., dt),
               _approach(vy, vertical, 13., dt)+gravity*dt,
               _approach(vz, target_z, 10., dt))
    ax, ay, az = spin
    pitch = math.remainder(pose.pitch, math.tau)
    roll = math.remainder(pose.roll, math.tau)
    forward_speed = vx*fx+vz*fz
    desired_pitch = .19*thrust if abs(forward_speed) < 8. else .14*thrust
    desired_roll = -.27*turn
    pitch_rate = 3.5*(desired_pitch-pitch) - .25*(ax*fz-az*fx)
    roll_rate = 3.5*(desired_roll-roll) - .25*(ax*fx+az*fz)
    angular = (pitch_rate*fz+roll_rate*fx, .87*turn, -pitch_rate*fx+roll_rate*fz)
    rotation = tuple(_approach(rate, max(-1.5, min(1.5, goal)), 5., dt)
                     for rate, goal in zip(spin, angular))
    return desired, rotation
