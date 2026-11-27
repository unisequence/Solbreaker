"""Battle-scoped sandbox controls and damped flight targets."""
from dataclasses import dataclass
import math
import struct

from . import big_boss, mad_games
from .mercury import variable8

PREFIX = b'TFS-LAB-S'
INPUT = struct.Struct('<IIIIfff')  # battle token, sequence, flags, shot, direction
BOOST, FLIGHT, ASCEND, DESCEND, MISSILE_VIEW, SPAWN_TARGET = (1, 2, 4, 8, 16, 32)
GRAVITY_JUMP = 64
FAST_RELOAD = 128
TELEPORT = 256
NEXT_TARGET, TOGGLE_TARGET_FIRE, CLEAR_TARGETS = 512, 1024, 2048
RESTORE_HP = 4096
BOSS_PICK_ROLE = 8192
BOSS_RESPAWN = 16384
GOD_MODE = 32768
CHANGE_TANK = 65536
TIME_STOP = 1 << 17
REMOVE_TARGET = 1 << 18
PICK_TARGET = 1 << 19
USE_ABILITY = 1 << 21  # input only, separate from capability announcements
TARGET_AI = 1 << 22
TOGGLE_MAP_BORDER = 1 << 25
TANK_STATS = 1 << 29
(STAT_BOOST, STAT_RELOAD, STAT_AIM, STAT_ACCURACY, STAT_TURRET, STAT_HULL, STAT_DAMAGE, STAT_HEALTH,
 STAT_BRAKE, STAT_REVERSE, STAT_GRIP) = range(11)
TANK_STAT_NAMES = ('Boost', 'Reload', 'Aim', 'Accuracy', 'Turret', 'Hull', 'Damage', 'Health',
                   'Brake', 'Reverse', 'Grip')
PRACTICE_MENU = 1 << 30
PRACTICE_FEEDBACK = 1 << 31
MENU_SPAWN, MENU_HEAL, MENU_DESTROY, MENU_REMOVE, MENU_CLEAR, MENU_RETURN_FIRE = range(6)
MENU_MISS, MENU_HEALED, MENU_DESTROYED, MENU_REMOVED, MENU_FULL_HEALTH = range(5)
PRACTICE_ACTIONS = NEXT_TARGET | TOGGLE_TARGET_FIRE | CLEAR_TARGETS | REMOVE_TARGET
BRANCH_SHOT_BASE = 1 << 30
# These are client entity IDs, in a separate namespace from arena-body IDs.
PRACTICE_ENTITY_BASE = 1 << 29
PRACTICE_TANKS = (
    ('china', 'Ch01_Type59', 'Type 59'),
    ('ussr', 'KV1', 'KV-1'),
    ('germany', 'PzIV', 'Pz. IV'),
)
FAST_RELOAD_SECONDS = .5
SANDBOX_KEYS = 0x0f
GRAVITY_KEY = 0x10
FAST_RELOAD_KEY = 0x20
TARGET_PLACED = 0x40
TELEPORT_KEY = 0x80
TELEPORTED = 0x100
PRACTICE_KEYS = 0x200
PRACTICE_NOTICE = 0x400
RESTORE_HP_KEY = 0x4000
HP_RESTORED = 0x8000
GOD_MODE_KEY = 0x10000
TIME_STOP_KEY = 1 << 17
REMOVE_TARGET_KEY = 1 << 18
PICK_TARGET_KEY = 1 << 19
TARGET_SELECTED = 1 << 20
TARGET_UNAVAILABLE = 1 << 21
TARGET_AI_CHANGED = 1 << 23
TARGET_AI_MISSING = 1 << 24
MAP_BORDER_OPEN = 1 << 26
MAP_BORDER_CLOSED = 1 << 27
MAP_BORDER_OUTSIDE = 1 << 28
AI_LEVELS = (None, 'easy', 'normal', 'hard')
AI_APPLY_ALL = 4
TARGET_FIRE_OFF, TARGET_FIRE_ON, TARGETS_CLEARED, TARGET_SLOTS_FULL = range(4, 8)
INPUT_LEASE = .85
BOOST_POWER = 2.4
BOOST_SPEED = 1.75


def boost_factor(vehicle, maximum):
    strength = getattr(vehicle, 'sandbox_boost_strength', None)
    if strength is None:
        strength = 100 if getattr(vehicle, 'sandbox_boost', False) else 0
    return 1. + (maximum-1.) * strength/100.


def stat_percent(vehicle, stat):
    return getattr(vehicle, 'sandbox_stats', {}).get(stat, 0)


def stat_bonus(vehicle, stat, maximum=2.):
    return 1. + (maximum-1.)*stat_percent(vehicle, stat)/100.


def brake_assist(percent, speed, forward, target, acceleration, slope, dt):
    if not percent or speed*forward > 0. or (forward and not speed):
        return target, acceleration, False
    # Stop before applying reverse power. At maximum the longitudinal
    # contact cancels the whole tick's motion, including downhill gravity.
    stop_acceleration = abs(speed + slope*dt)/dt
    assist = min(1., percent/100.)
    acceleration += max(0., stop_acceleration-acceleration)*assist
    return 0., acceleration, percent == 100


def reload_limit(vehicle):
    if not vehicle.sandbox_fast_reload:
        return None
    return getattr(vehicle, 'sandbox_reload_override', None) or FAST_RELOAD_SECONDS


def token(arena):
    return arena.unique_id & 0xffffffff or 1


def vehicle_wire_id(arena, entity):
    ids = getattr(arena, 'practice_client_ids', None)
    return ids.get(entity, entity) if ids else entity


def vehicle_arena_id(arena, entity):
    ids = getattr(arena, 'practice_client_ids', None)
    if not ids:
        return entity
    for slot, client in ids.items():
        if client == entity:
            return slot
    if entity in ids or entity >= PRACTICE_ENTITY_BASE:
        return -1  # The old model no longer refers to a target.
    return entity


def advertisement(arena, *, notice=0, ability=False):
    # Avatar method 37 carries two vectors. The patched client consumes this
    # sentinel before the native method sees it.
    capabilities = (SANDBOX_KEYS | FAST_RELOAD_KEY if arena.sandbox_enabled else 0) | (
        GRAVITY_KEY if getattr(arena, 'gravity_mode', False) is True or ability else 0)
    if arena.sandbox_enabled:
        capabilities |= TELEPORT_KEY | RESTORE_HP_KEY | GOD_MODE_KEY | TOGGLE_MAP_BORDER | TANK_STATS
    if getattr(arena, 'practice', False):
        capabilities |= PRACTICE_KEYS | REMOVE_TARGET_KEY
        if arena.sandbox_enabled:
            capabilities |= PRACTICE_MENU
        if getattr(arena, 'practice_game', None) is not None:
            capabilities |= PICK_TARGET_KEY
    else:
        capabilities &= ~0x08
    if getattr(arena, 'time_stop_enabled', False):
        capabilities |= TIME_STOP_KEY
    if target_ai_available(arena):
        capabilities |= TARGET_AI
    capabilities |= notice
    second = b'\1' + struct.pack('<I', capabilities) if capabilities else b'\0'
    return b'\x13' + variable8(96, b'\x03TFS1SBOX' + struct.pack('<I', token(arena)) + second)


def preset_advertisement(arena, account_id):
    if not arena.sandbox_enabled:
        return b''
    vehicle = arena.vehicle_for(account_id)
    values = getattr(vehicle, 'sandbox_preset', ())
    if not isinstance(values, tuple) or len(values) != len(TANK_STAT_NAMES):
        return b''
    # The current preset is captured at battle creation. Subsequent live
    # slider edits use their ordinary incremental updates.
    data = bytes(values).ljust(12, b'\0')
    return b'\x13' + variable8(96, b'\x06TFS1STAT' + struct.pack('<I', token(arena)) + data + b'\0')


def ability_slot_message(arena, slot, state, remaining):
    return b'\x13' + variable8(96, b'\x04TFS1ABIL' + struct.pack(
        '<IBBH', token(arena), slot, state, remaining) + b'\0')


def ability_slot_snapshot(arena, vehicle):
    specs = getattr(arena, 'range_abilities', ())
    if not specs:
        return b''
    installed = {state.spec: state for state in vehicle.consumables if state}
    result = bytearray()
    for slot, spec in enumerate(specs):
        state = installed.get(spec)
        view = state.state_at(arena.simulation_time) if state else None
        result += ability_slot_message(arena, slot, view.state if view else 0,
                                       view.remaining if view else 0)
    return bytes(result)


def target_ai_available(arena):
    return arena.sandbox_enabled and (getattr(arena, 'practice', False) or bool(getattr(arena, 'training_bots', ())))


def decode_boss_pick(number):
    """The first block is the legacy role-only request; later blocks specify the level."""
    if number < 1:
        raise ValueError('Invalid Big Boss role')
    block, index = divmod(number - 1, len(big_boss.PICKABLE_ROLES))
    role = big_boss.PICKABLE_ROLES[index]
    if block > big_boss.ROLE_LEVEL_COUNT or (role == big_boss.BOSS and block):
        raise ValueError('Invalid Big Boss role level')
    return role, block - 1 if block else None


def boss_picker_message(arena, state):
    return b'\x13' + variable8(96, b'\x04TFS1ROLE' + struct.pack('<II', token(arena), state) + b'\0')


def change_tank_message(arena):
    return b'\x13' + variable8(96, b'\x03TFS1SWAP' + struct.pack('<I', token(arena)) + b'\0')


def training_roster_refresh(arena, entity):
    return b'\x13' + variable8(96, b'\x04TFS1TANK' + struct.pack('<II', token(arena), entity) + b'\0')


def training_reentry_enabled(arena):
    if ((getattr(arena, 'room_settings', None) is None and not getattr(arena, 'proving_ground', None))
            or not arena.started or arena.finished_at or arena.closed or arena.retiring):
        return False
    if getattr(arena, 'owner_backed', False):
        return getattr(arena, 'owner_training_reentry', False)
    return arena.world is not None


def receive(endpoint, addr, client, arena, body, now):
    """Consume sandbox reports on the authenticated current battle session."""
    if not body.startswith(PREFIX):
        return False
    boss = arena.big_boss if getattr(arena, 'big_boss_mode', False) is True else None
    mad = getattr(arena, 'mad_games_mode', False) is True
    player = arena.players.get(client.account_id) if boss is not None or mad else None
    ability = player is not None and (boss is not None
        or mad_games.irradiator_state(arena.vehicles[player.vehicle_id]) is not None)
    if (arena is None or client.reconnect_replay
            or not client.battle_spawned or body[-1:] != b'\x01'):
        return True
    raw = body[len(PREFIX):-1]
    try:
        control = Control.decode(raw)
    except ValueError:
        return True
    if control.flags == CHANGE_TANK:
        if control.token == token(arena):
            endpoint.training_hangar(addr, client, arena)
        return True
    if control.flags == TIME_STOP and not getattr(arena, 'time_stop_enabled', False):
        return True
    if not (arena.sandbox_enabled or getattr(arena, 'gravity_mode', False) is True or ability):
        return True
    previous = client.sandbox_input
    current = token(arena)
    if (previous is not None and previous[0] == current and now-previous[2] < .025
            and control.flags != TIME_STOP):
        return True
    if control.token == 0:
        if not control.flags and not control.shot and client.reliable_window_used < 200:
            client.sandbox_input = (current, None, now, 0)
            endpoint.send(addr, client, advertisement(arena, ability=ability)
                          + preset_advertisement(arena, client.account_id))
        return True
    if control.token != current:
        return True
    if previous is None or previous[0] != current:
        if control.flags not in (BOSS_PICK_ROLE, BOSS_RESPAWN):
            return True
        previous = (current, None, 0, 0)
    if previous[1] is not None:
        distance = (control.sequence-previous[1]) & 0xffffffff
        if not 0 < distance < (1 << 31):
            return True
    flags = previous[3] if control.flags in (PICK_TARGET, TARGET_AI, TOGGLE_MAP_BORDER,
                                            TANK_STATS, PRACTICE_MENU, USE_ABILITY) else control.flags
    client.sandbox_input = (current, control.sequence, now, flags)
    if (flags ^ previous[3]) & (BOOST | FLIGHT | MISSILE_VIEW | FAST_RELOAD | GOD_MODE):
        endpoint.server.event('sandbox_controls', account_id=client.account_id,
                              arena_id=arena.result_id, flags=flags)
    if getattr(arena, 'owner_backed', False):
        endpoint.owner_service.sandbox(addr, client, raw)
    else:
        from .arena_commands import ArenaCommand
        endpoint.queue_arena_input(addr, client, arena,
                                   ArenaCommand(client.account_id, 'sandbox', (control,)))
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
        if control.flags in (CHANGE_TANK, TIME_STOP, TOGGLE_MAP_BORDER) and not length and not control.shot:
            return control
        if control.flags == PICK_TARGET and not length and control.shot & 0xffff:
            return control
        if control.flags == TANK_STATS and not length and (control.shot >> 8) < len(TANK_STAT_NAMES) and (control.shot & 255) <= 100:
            return control
        if control.flags == USE_ABILITY and not length and control.shot < 3:
            return control
        if control.flags == PRACTICE_MENU and not length and control.shot <= MENU_RETURN_FIRE:
            return control
        if control.flags == TARGET_AI and not length and control.shot < 2 * AI_APPLY_ALL:
            return control
        if control.flags & (BOSS_PICK_ROLE | BOSS_RESPAWN):
            if length:
                raise ValueError('Invalid Big Boss request')
            if control.flags == BOSS_PICK_ROLE:
                decode_boss_pick(control.shot)
            elif control.flags != BOSS_RESPAWN or control.shot:
                raise ValueError('Invalid Big Boss request')
            return control
        if (control.flags & ~(511 | PRACTICE_ACTIONS | RESTORE_HP | GOD_MODE)
                or not math.isfinite(length) or length > 10000.
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
    return desired, flight_attitude(pose, velocity, spin, thrust, turn, dt)


def flight_attitude(pose, velocity, spin, thrust, turn, dt, *, angular_acceleration=5., landing=None):
    """Steer and level the hull with angular velocity only."""
    fx, fz = math.sin(pose.yaw), math.cos(pose.yaw)
    vx, _, vz = velocity
    ax, ay, az = spin
    pitch = math.remainder(pose.pitch, math.tau)
    roll = math.remainder(pose.roll, math.tau)
    forward_speed = vx*fx+vz*fz
    desired_pitch = .19*thrust if abs(forward_speed) < 8. else .14*thrust
    desired_roll = -.27*turn
    if landing is not None:
        pitch_on_ground, roll_on_ground, blend = landing
        desired_pitch += (pitch_on_ground - desired_pitch) * blend
        desired_roll += (roll_on_ground - desired_roll) * blend
    pitch_rate = 3.5*(desired_pitch-pitch) - .25*(ax*fz-az*fx)
    roll_rate = 3.5*(desired_roll-roll) - .25*(ax*fx+az*fz)
    angular = (pitch_rate*fz+roll_rate*fx, .87*turn, -pitch_rate*fx+roll_rate*fz)
    return tuple(_approach(rate, max(-1.5, min(1.5, goal)), angular_acceleration, dt)
                 for rate, goal in zip(spin, angular))
