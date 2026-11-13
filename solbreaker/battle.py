"""Pinned client battle wire adapter; simulation lives in arena.py."""
import struct
import math
import time

from .account import pickle_document, variable16
from .mercury import packed_blob, read_blob, variable8
from .motion import vehicle_snapshot
from .destructibles import DestructibleState


AVATAR_ID = 101
VEHICLE_ID = 200
TARGET_ID = 201
SPACE_ID = 1
FINISH_REASONS = {'capture': 2, 'timeout': 3, 'supremacy': 6, 'supremacy_timeout': 7,
                  'leave': 5, 'disconnect': 5}


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def pb_int(field, value):
    return varint(field << 3) + varint(value)


def pb_blob(field, value):
    return varint((field << 3) | 2) + varint(len(value)) + value


def pb_double(field, value):
    return varint((field << 3) | 1) + struct.pack("<d", value)


def arena_update(opcode, message):
    # Avatar method 48: variable16, u8 operation and a packed protobuf.
    return b"\x13" + variable16(59 + 48, bytes([opcode]) + packed_blob(message))


def vehicle_info(entity_id, name, team, account_id, descriptor, *, alive=True):
    return (pb_int(1, entity_id) + pb_blob(2, descriptor)
            + pb_blob(3, name.encode()) + pb_int(4, team)
            + pb_int(5, int(alive)) + pb_int(6, 1) + pb_int(7, account_id)
            + varint((18 << 3) | 5) + struct.pack("<f", 1.0))


def participant_ids(identities):
    """Map vehicle/account pairs to distinct native participant keys.

    NPCs have no account, but native roster/results maps still need a unique
    nonzero key for each one. These IDs exist only in the battle wire format;
    they never become accounts or reward recipients. Sorting by entity keeps
    reconnects and saved reports stable regardless of row iteration order.
    """
    identities = tuple(identities)
    used = {account for _,account in identities if account}
    synthetic = 2**31-1
    result = {}
    for entity,account in sorted(identities):
        if account:
            result[entity] = account
        else:
            while synthetic in used:
                synthetic -= 1
            result[entity] = synthetic
            used.add(synthetic)
            synthetic -= 1
    return result


def device_vector(devices):
    """Vehicle vector<u8>: count u8 and raw XML module IDs (not a bit mask)."""
    devices = tuple(devices)
    if len(devices) > 255 or any(type(d) is not int or not 0 <= d <= 255 for d in devices):
        raise ValueError('Invalid vehicle device vector')
    return packed_blob(bytes(devices))


def create_vehicle(account, descriptor, position, health=280, entity_id=VEHICLE_ID, team=1,
                   alias=0, direction=(0.,0.,0.), max_health=None, optional_devices=(),
                   critical_devices=(), destroyed_devices=(), gun_angles=None, state_modifiers=()):
    # Vehicle type 2, readCellProperties 0x177b710; publicInfo reader
    # 0x174a210 contains name, descriptor, team and prebattle ID.
    # VehicleGameLogic::MakeConfigFromVehicleCompactDescr reads +0x98.
    public_info = (packed_blob(account["nickname"].encode()) + packed_blob(descriptor)
                   + struct.pack("<BI", team, 0))
    # BWEntity::initOtherEntity (0x2809ff0) reads a count and indexed
    # properties. readCellProperties is only used for the player entity.
    # Property4 is engineMode (2×u8), never a module critical mask.
    if gun_angles is None:
        gun_angles = struct.pack('<H',(512 << 6) | 46)
    if not isinstance(gun_angles,bytes) or len(gun_angles)!=2:
        raise ValueError('Invalid gun angle encoding')
    count = 6 + bool(optional_devices) + bool(critical_devices) + bool(destroyed_devices) + bool(state_modifiers)
    props = (bytes([count]) + b"\x01" + bytes([health>0]) + b"\x02" + gun_angles
             + b"\x03" + struct.pack("<h", health)
             + b"\x04\0\0" + b"\x05" + public_info
             + b"\x0d" + struct.pack("<h", health if max_health is None else max_health))
    if optional_devices:
        # Vehicle property11 -> +0x104, vector<uint8>, reader0x13c5b50.
        # These are raw XML device IDs, unlike full provision/consumable CDs.
        props += b'\x0b' + bytes([len(optional_devices)]) + bytes(optional_devices)
    # Property8 criticalDevices / property9 destroyedDevices, vector<uint8>.
    # Omitted when empty so a healthy spawn keeps the previous bytes.
    if critical_devices:
        props += b'\x08' + device_vector(critical_devices)
    if destroyed_devices:
        props += b'\x09' + device_vector(destroyed_devices)
    if state_modifiers:
        props += b'\x07' + device_vector(state_modifiers)
    body = b"\0" + struct.pack("<IH6f", entity_id, 2, *position, *direction) + props
    return b"\x0a" + struct.pack("<IB", entity_id, alias) + variable16(9, body)


def create_avatar(account, avatar_id=AVATAR_ID, arena_id=1, map_id=6):
    # Avatar::readBaseProperties, 0x177b1f0. arenaTypeID combines the
    # map in its low word and gameplay in its high word.
    properties = (packed_blob(account["nickname"].encode())
                  + struct.pack("<QI", arena_id, map_id)
                  + packed_blob(pickle_document({"mouseEnabled": True}))
                  + struct.pack("<BH", 0, 0)
                  + packed_blob(pickle_document({}))
                  # +0xd8 is observer mode, NOT arena bonus type. Nonzero
                  # makes AvatarGameLogic ignore ammo, reload and health RPCs.
                  + struct.pack("<BB", 0, 2))
    return variable16(5, struct.pack("<IH", avatar_id, 1) + properties)


def create_avatar_cell(space_id=SPACE_ID, team=1):
    # Client ServerConnection::createCellPlayer 0x2818240 uses the vanilla
    # header, unlike current desktop WoT: space, vehicle, position, scale, dir.
    header = struct.pack("<II7f", space_id, 0, 0, 0, 0, 0.0076444, 0, 0, 0)
    # Avatar::readCellProperties 0x177b450. Player vehicle remains unset
    # until the arena vehicle roster and Vehicle entity have been delivered.
    # First cell property is Avatar +0xda, the player's team. Native
    # 0x180d4a1 compares it with Vehicle.publicInfo.team at +0xb0.
    props = (struct.pack("<BIBBfHB", team, 0, 0, 0, 0.0, 0, 0)
             + b"\0" + struct.pack("<fB", 0.0, 0)
             + b"\0\0" + struct.pack("<4I", 0, 0, 0, 0))
    return variable16(6, header + props)


def avatar_method(method, payload=b""):
    if method in (35, 36, 38, 45):
        from .mercury import variable8
        return b"\x13" + variable8(59+method,payload)
    return b"\x13" + bytes([59 + method]) + payload


def missile_message(shooter, shot, effect, position, velocity):
    # Avatar30 / 0x177f175, moveATGM. TracerManager at 0x1bb9440 reads
    # the shooter, shot, first/third vectors and the effect. The other
    # fields are unused by the pinned client's tracer and player listeners.
    return avatar_method(30,struct.pack('<II10fBf',shooter,shot,
        *position,0.,0.,0.,*velocity,0.,effect,0.))


def provision_effect_message(entity_id, compact_id):
    """Avatar45: BW blob of ShortClientEffect{ExtrasEffect(entity, provisionCD)}.

    Native 177a6c0 group18 returns -1 for45, as for36/38. Decoder177f57d
    reads a BW blob; protobuf21ae7f0/21ae3a0 uses nested field1, then entity
    varint64 field1 and compactCD varint32 field2. Unknown field3 stays zero.
    AntiHE handler1d4e1e0 reads the visual duration from the pinned item itself.
    """
    if type(entity_id) is not int or not 0 < entity_id <= 0xffffffff or compact_id != 26109:
        raise ValueError('Unsupported short provision effect')
    child = pb_int(1, entity_id) + pb_int(2, compact_id)
    return avatar_method(45, packed_blob(pb_blob(1, child)))


def vehicle_property(entity_id, number, payload):
    return b"\x12" + struct.pack("<I", entity_id) + bytes([158 + number]) + payload


def health_changed(entity_id, health, attacker=VEHICLE_ID):
    # Vehicle method 1 / 0x177fcdc: i16 new HP, u32 attacker, u8 reason.
    # Send after property3: set_health caches old HP for the UI damage delta.
    return b'\x12'+struct.pack('<IBhIB',entity_id,60,health,attacker,0)


def received_damage_message(entity_id, attacker, damage, *, outcome=None, blocked_damage=0,
                            hit_effect=None):
    """Vehicle8: own incoming-hit feedback, including the native screen flash.

    Pinned decoder 0x177fe39: u32, u32, packed u64[], u8, u16, u8.
    hit_effect is the immutable (shell effect index, packed local ray) captured
    at impact. A real segment also reaches the native first-person hit sounds.
    Without geometry, retain the typed zero-length screen-feedback fallback.
    The separate HP delta must be actual applied damage; a zero-damage result
    needs an explicit outcome and never changes health. The same u16 is the
    displayed armor-block amount for a ricochet/nonpenetration; its HP flag
    and segment kind must still describe a non-damaging hit.
    """
    if (type(entity_id) is not int or not 0 < entity_id <= 0xffffffff or
            type(attacker) is not int or not 0 < attacker <= 0xffffffff or
            type(damage) is not int or not 0 <= damage <= 0xffff or
            type(blocked_damage) is not int or not 0 <= blocked_damage <= 0xffff or
            blocked_damage and (damage or outcome not in ('ricochet','not_penetrated')) or
            outcome not in (None,'penetrated','splash','ricochet','not_penetrated','external') or
            damage == 0 and outcome is None):
        raise ValueError('Invalid received damage')
    effect_index, segment = (0, 0) if hit_effect is None else hit_effect
    if (type(effect_index) is not int or not 0 <= effect_index <= 255
            or type(segment) is not int or not 0 <= segment < 1 << 64 or segment & 255):
        raise ValueError('Invalid received hit effect')
    kind = 3 if damage else {'ricochet':0,'not_penetrated':1,'external':4,
                            'penetrated':2,'splash':2}[outcome]
    body = struct.pack('<II', attacker, 0) + b'\1' + struct.pack('<Q',segment | kind)
    body += struct.pack('<BHB', effect_index, damage or blocked_damage, int(damage > 0))
    return b'\x12' + struct.pack('<I', entity_id) + variable8(67, body)


def shooting_message(entity_id, shot_count=1):
    # Vehicle method0, decoder0x177fd41 -> showShooting0x1845180: one u8.
    # Avatar29 creates the tracer; this separate RPC starts muzzle FX/audio.
    # The u8 is the whole muzzle/audio group size. Own-vehicle burst partners
    # cannot restart it after the native prediction flag has been consumed.
    if (type(entity_id) is not int or not 0 < entity_id <= 0xffffffff
            or type(shot_count) is not int or not 1 <= shot_count <= 255):
        raise ValueError('Invalid shooting effect')
    return b'\x12'+struct.pack('<IBB',entity_id,59,shot_count)


def shot_feedback(entity_id, outcome):
    # Avatar method 38: PrivateShotResult {entity, flags, u16[], u32[]}.
    # Verified on native 11.21: 8=ricochet, 0=not pierced, 16=HP damage.
    flags = {'penetrated':16, 'splash':16, 'ricochet':8}.get(outcome,0)
    return avatar_method(38,struct.pack('<II',entity_id,flags)+b'\0\0')


def targeting_info(vehicle=None, yaw_rate=None, pitch_rate=None):
    """Avatar36. Native 0x1e229a0 refreshes an active rotator's rates only.

    yaw_rate/pitch_rate are the event's snapshots of GunAim.yaw_rate and
    GunAim.pitch_rate, the same effective rates (base times the timed
    traverse factor, yaw plus the sandbox stabilizer) the server slews with.
    """
    config = vehicle.definition.aiming if vehicle and vehicle.definition else None
    if yaw_rate is None:
        yaw_rate = vehicle.gun.yaw_rate if vehicle else math.radians(45)
    if pitch_rate is None:
        pitch_rate = vehicle.gun.pitch_rate if vehicle else math.radians(45)
    regular = pb_blob(1, pb_double(1, .1)) + pb_blob(2, pb_double(1, .1))
    factors = pb_double(1, 1) + pb_double(2, .1) + pb_blob(3, pb_blob(1, regular))
    info = (pb_double(1, 0) + pb_double(2, 0) + pb_double(3, yaw_rate)
            + pb_double(4, pitch_rate)
            + pb_double(5, config.aiming_time if config else 1.7) + pb_blob(6, factors))
    return avatar_method(36, packed_blob(info))


def period_message(period, seconds=600, winner=0, battle_seconds=600, *, finish_reason=None):
    value = (pb_int(1, period) + b"\x11" + struct.pack("<d", seconds)
             + pb_int(3, battle_seconds) + pb_int(5, winner))
    prefix = b""
    if period == 4:
        # Field4 makes the native PeriodInfo consume winner and finish reason.
        reason = FINISH_REASONS.get(finish_reason, 1)
        value += pb_int(4, 1) + pb_int(6, reason)
        # Avatar4 stores RoundFinishedInfo before Period4 starts the native
        # AfterRoundFinishManager. Without it the client immediately leaves.
        prefix = avatar_method(4, struct.pack("<bB", winner, reason))
    return prefix + arena_update(3, pb_blob(3, value))


def base_points_message(team, points, blocked=False, base_id=0):
    # Pinned UpdateArena.proto, BasePoints field8. owning_team is the
    # defending side: ClientArena maps its progress to the opposite attacker.
    row = (pb_int(1, 3 - team) + pb_int(2, base_id)
           + pb_int(3, points) + pb_int(4, int(blocked)))
    return arena_update(8, pb_blob(8, row))


def base_capture_messages(arena):
    capture = getattr(arena, 'base_capture', None)
    if capture is None:
        return ()
    points, blocked = capture.view()
    return tuple(base_points_message(team, points[team - 1], blocked, capture.point.base_id)
                 for team in (1, 2))


def supremacy_messages(arena, player, state=None, previous=None):
    supremacy = getattr(arena, 'supremacy', None)
    if supremacy is None:
        return ()
    scores, statuses = supremacy.state() if state is None else state
    messages = []
    old_statuses = {row[0]: row for row in previous[1]} if previous is not None else {}
    for ident, owner, invader, progress, blocked, members in statuses:
        old = old_statuses.get(ident)
        if (old is not None and old[:5] == (ident, owner, invader, progress, blocked)
                and (player.vehicle_id in old[5]) == (player.vehicle_id in members)):
            continue
        row = (pb_int(1, ident) + pb_int(2, owner) + pb_int(3, invader)
               + pb_int(4, progress) + pb_int(5, int(blocked))
               + pb_int(6, int(player.vehicle_id in members)))
        messages.append(arena_update(12, pb_blob(11, row)))
    messages.extend(arena_update(13, pb_blob(12, pb_int(1, team) + pb_int(2, scores[team-1])))
                    for team in (1, 2)
                    if previous is None or previous[0][team-1] != scores[team-1])
    return tuple(messages)


def team_kill_score(arena, player):
    from .mercury import variable8
    totals = [0, 0]
    for vehicle in arena.vehicles.values():
        totals[vehicle.team != player.team] += vehicle.kills
    # Avatar37 vector sentinel for the Solbreaker HUD. Supremacy retains its
    # native capture points, which are independent of the kill totals.
    return b'\x13' + variable8(96, b'\x04TFS1FRAG' + struct.pack('<II', *totals) + b'\0')


def battle_hints_message():
    """Refresh the native battle HUD after its visibility bindings are ready.

    Avatar48 operation 23 selects UpdateArena.battle_hints_info (field 20).
    The pinned client's ClientArena listener recalculates visibility aspect 15
    from this update; round zero is the single-round sandbox's current round.
    """
    return arena_update(23, pb_blob(20, pb_int(1, 0)))


def initial_messages(arena, player):
    return create_avatar({'nickname':player.nickname},player.avatar_id,arena.unique_id,
                         getattr(arena,'client_map_id',getattr(arena,'map_id',6)))+create_avatar_cell(arena.space_id,player.team)


def destructible_state(arena):
    state = DestructibleState(arena.world.assets.destructibles,entity_base=arena.entity_base+300,
                             alias_base=len(arena.vehicles))
    for key,(yaw,angle) in arena.destructions.items():
        state.destroy(arena.world.assets.destructibles[key],yaw,angle)
    return state


def module_snapshot(arena, vehicle):
    """(critical, destroyed) module IDs; arenas without module state are healthy."""
    state = getattr(arena,'module_state',None)
    return state(vehicle) if state else ((),())


def present_vehicles(arena):
    slots = getattr(arena, 'practice_slots', ())
    for alias, vehicle in enumerate(arena.vehicles.values()):
        if vehicle.entity_id not in slots or vehicle.entity_id in arena.practice_spawned:
            yield alias, vehicle


def vehicle_spawn_parts(arena, vehicle, alias):
    pose = vehicle.pose
    critical, destroyed = module_snapshot(arena, vehicle)
    # Entity creation reads yaw/pitch/roll, detailed movement roll/pitch/yaw.
    yield create_vehicle({'nickname':vehicle.nickname}, vehicle.descriptor, pose.position,
        vehicle.health, vehicle.entity_id, vehicle.team, alias, (pose.yaw,pose.pitch,pose.roll),
        vehicle.max_health, vehicle.definition.optional_devices if vehicle.definition else (),
        critical, destroyed, gun_angles=vehicle.gun.packed(),
        state_modifiers=getattr(arena, 'state_modifiers', lambda vehicle: ())(vehicle))
    armor_state = movable_armor_states_message(vehicle)
    if armor_state:
        # Dynamic property16 is applied only after the Vehicle exists.
        yield armor_state


def spawn_message_parts(arena, player, *, include_owner_state=True):
    """Complete wire units; native Mercury cannot split an element header."""
    participants = participant_ids((v.entity_id,v.account_id) for v in arena.vehicles.values())
    present = tuple(present_vehicles(arena))
    rows = b''.join(pb_blob(1,vehicle_info(v.entity_id,v.nickname,v.team,participants[v.entity_id],v.descriptor,
                                         alive=v.alive or (getattr(arena, 'unlimited_lives', False)
                                                           and v.entity_id in arena.respawn_at)))
                    for _,v in present)
    stats = b''.join(pb_blob(1,pb_int(1,v.entity_id)+pb_int(2,v.kills)) for _,v in present)
    parts = [arena_update(1,pb_blob(1,rows)),arena_update(4,pb_blob(4,stats)),
             b'\x13\xa6'+struct.pack('<I',player.vehicle_id)]
    if getattr(arena, 'unlimited_lives', False) and arena.supremacy is None:
        parts.append(team_kill_score(arena, player))
    for alias,v in present:
        parts.extend(vehicle_spawn_parts(arena, v, alias))
    parts.append(b'\x14'+struct.pack('<III6f',player.avatar_id,arena.space_id,player.vehicle_id,0,0,0,0,0,0))
    # Creation/public vectors do not establish the owner's private crew
    # Health objects. Replay only that owner's wounds after control is set.
    own = arena.vehicles[player.vehicle_id]
    for person in (getattr(arena, 'injured_crew', lambda vehicle: ())(own) if include_owner_state else ()):
        parts.append(module_damage_message(own.entity_id, 0, person.spec.extra_health_index))
    if arena.world:
        parts.extend(destructible_state(arena).spawn_parts())
    return tuple(parts)


def spawn_messages(arena, player):
    return b''.join(spawn_message_parts(arena,player))


def spawn_batches(arena, player, limit=900, *, include_owner_state=True):
    """Coalesce whole units; an oversized payload keeps its own fragment group."""
    if type(limit) is not int or limit<=0:
        raise ValueError('Spawn batch limit must be a positive integer')
    batches = []
    pending = bytearray()
    for part in spawn_message_parts(arena,player,include_owner_state=include_owner_state):
        if pending and len(pending)+len(part)>limit:
            batches.append(bytes(pending))
            pending.clear()
        if len(part)>limit:
            batches.append(part)
        else:
            pending.extend(part)
    if pending:
        batches.append(bytes(pending))
    return tuple(batches)


def ammo_message(vehicle):
    counts = vehicle.ammo_by_shell or {vehicle.shell_id:vehicle.ammo}
    return b''.join(avatar_method(17,struct.pack('<BIHBhh',10,cd,
        vehicle.ammo if cd==vehicle.shell_id else count,
        vehicle.clip if cd==vehicle.shell_id else 0,0,0)) for cd,count in counts.items())


def allied_reload_message(arena, player, vehicle):
    """Public UpdateArena15 for a teammate, never the owner's gun RPC.

    Avatar13 updates the recipient's private gun regardless of its entity ID.
    Pinned opcode table0x4144240:15=RELOAD_TIME,14=PLAYER_NAME.
    The protobuf oneof field is14; ReloadTimeUpdate carries vehicle_id1, reload_state2,
    time_left3(float) and clip_size_left4 for team-panel/marker consumers.
    A pump with usable rounds is ready while its spare round is refilling.
    """
    if vehicle.entity_id == player.vehicle_id or vehicle.team != player.team:
        return b''
    now = arena.simulation_time
    remaining = max(0., vehicle.reload_at-now) if vehicle.reload_at else 0.
    if remaining:
        state, value = 4, remaining
    elif vehicle.clip == 0 and vehicle.pump_times and vehicle.pump_at > now:
        state, value = 6, vehicle.pump_at-now
    else:
        state, value = 0, 0.
    row = (pb_int(1,vehicle.entity_id) + pb_int(2,state)
           + varint((3 << 3) | 5) + struct.pack('<f',value) + pb_int(4,vehicle.clip))
    return arena_update(15,pb_blob(14,pb_blob(1,row)))


def _allied_reload_parts(arena, player):
    return tuple(allied_reload_message(arena,player,vehicle)
                 for _,vehicle in sorted(arena.vehicles.items())
                 if vehicle.team == player.team and vehicle.entity_id != player.vehicle_id)


def gun_reload_configuration(vehicle, *, full_reload_factor=None):
    """Avatar35 for pump guns only; b'' for ordinary guns.

    Decoder177de68: u32 (ignored by the own handler), floatA, floatB and a BW
    array<float> (u8 count below255). Handler18469e0 -> VehicleGun1bc0f10
    stores A at+50, B at+54 (primary nominal reload) and the vector at+58.
    A's meaning is unproved; the full reload is a provisional choice.
    """
    weapon = vehicle.definition.weapon if vehicle.definition else None
    times = getattr(weapon, 'pump_reload_times', ())
    if not times:
        return b''
    factor = vehicle.reload_speed_factor
    if full_reload_factor is None:
        # Preserve the original direct-call contract: before rack state exists,
        # full reload and pump work follow the vehicle's active boost rate.
        full_reload_factor = factor
    if (isinstance(full_reload_factor, bool) or not isinstance(full_reload_factor, (int, float))
            or not math.isfinite(full_reload_factor) or full_reload_factor <= 0):
        raise ValueError('Invalid full reload factor')
    # Rack damage changes full magazine/replenishment work only. The interval
    # stays on its existing boost/NewAutoloader timing path.
    values = (weapon.reload/full_reload_factor, weapon.interval/factor*vehicle.interval_factor,
              *(t/full_reload_factor for t in times))
    if len(times) > 254 or not all(math.isfinite(v) and v > 0 for v in values):
        raise ValueError('Invalid pump reload configuration')
    return avatar_method(35, struct.pack('<I2fB', vehicle.entity_id, *values[:2], len(times))
                         + struct.pack(f'<{len(times)}f', *values[2:]))


def consumable_message(compact_id, state, remaining=0, total=0):
    # Avatar17 / 0x177dfda -> updateVehicleAmmo. Kind11 is equipment;
    # slot state: 1 ready, 2 active, 3 cooldown. Durations are signed seconds.
    return avatar_method(17, struct.pack('<BIHBhh', 11, compact_id, 1 if compact_id else 0,
                                        state, remaining, total))


def provision_messages(vehicle):
    """Owner-only passive equipment slots for the native Tab HUD."""
    # Avatar17 kind13 dispatches to the provision panel. Full CDs use
    # common nation 15. CD0 advances the separate provision slot cursor
    # (0x184697e), so keep gaps. Raw XML ID0 is a real provision.
    messages = []
    for ident in getattr(vehicle.definition, 'provisions', ()):
        installed = int(ident is not None)
        compact_id = (ident << 8) | 0xfd if installed else 0
        messages.append(avatar_method(17, struct.pack('<BIHBhh', 13, compact_id,
                                                      installed, installed, 0, 0)))
    return tuple(messages)


def active_equipment_message(arena, vehicle, equipment=None):
    """Vehicle public property10: timed equipment indicators for every observer.

    Native UI uses BW gameTime (0x175e070 -> 0x28188c0), not the local
    position-filter clock. UI IDs are distinct from XML/activation IDs:
    0x53a166/0x53a184/0x53a2a3 map berserk/afterburn/freeburner to 9/10/189;
    0x53a240/0x53a244 (built at 0x53a2ae) map NewAutoloader script29 to 63
    (enum NEW_AUTOLOADER 0x868e37, icon new-autoloader case 0x1ba91ff);
    0x53a26c/0x53a270 map TungstenTip script45 to 105 (enum TUNGSTEN_TIP
    0x868ef1, icon case 0x1ba9379).
    0x53a24b/0x53a24f map improvedAfterburning script27 to 61
    (icon improved-afterburning at 0x1ba91ed).
    0x53a261/0x53a265 map ShieldKit script33 to public66.
    0x53a277/0x53a27b map ArmorMover script73 to public169.
    0x53a235/0x53a239 map FireControlSystem script28 to public62.
    The native indicator consumer removes an old indicator before checking
    for ACTIVE2 (0x1bc3cd3..0x1bc3cde); COOLDOWN3 therefore stops it.
    """
    public_ids = {18: 9, 14: 10, 80: 189, 29: 63, 45: 105, 27: 61, 33: 66, 28: 62, 73: 169}
    entries = []
    if equipment is None:
        equipment = arena.equipment_state(vehicle)
    for ident,state,started,duration in equipment:
        entries.append(struct.pack('<BBdf', public_ids[ident], state, started-arena.epoch, duration))
    payload = bytes([len(entries)]) + b''.join(entries)
    # Dynamic entity properties have a VARIABLE8 header outside createEntity.
    return b'\x12' + struct.pack('<I', vehicle.entity_id) + variable8(168, payload)


def armor_states_message(entity_id, states):
    """Encode a complete Vehicle ``armorsStates`` property-16 snapshot.

    The pinned Vehicle reader consumes a BigWorld dynamic property with kind
    174 (property 16).  Its value is an array of ``<u16 state_id, u16
    enabled>`` records.  The array length uses the same compact count form as
    a packed blob: one byte below 255, otherwise ``0xff`` plus a little-endian
    24-bit count.  This is deliberately just a wire encoder: it does not
    select states, mutate a vehicle, or opt any vehicle into movable armor.
    """
    if type(entity_id) is not int or not 0 < entity_id <= 0xffffffff:
        raise ValueError('Invalid armor-state entity ID')
    try:
        rows = tuple(states)
    except TypeError as exc:
        raise ValueError('Armor states must be iterable') from exc
    count = len(rows)
    if count > 0xffffff:
        raise ValueError('Armor-state count exceeds 24-bit wire limit')
    payload = bytearray(bytes([count]) if count < 255
                        else b'\xff' + count.to_bytes(3, 'little'))
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            raise ValueError('Armor state must be an (ID, enabled) pair')
        state_id, enabled = row
        if (type(state_id) is not int or not 0 <= state_id <= 0xffff
                or type(enabled) is not int or enabled not in (0, 1)):
            raise ValueError('Invalid armor state')
        payload += struct.pack('<HH', state_id, enabled)
    return b'\x12' + struct.pack('<I', entity_id) + variable8(174, bytes(payload))


def movable_armor_states_message(vehicle, states=None):
    """Return property16 only for a vehicle with an explicit armor snapshot.

    ``MovableArmorStateSnapshot`` keeps bool flags for collision validation,
    whereas the verified BigWorld value is a u16 flag.  Convert at this
    serialization boundary without relaxing ``armor_states_message``'s public
    wire validation.  ``states`` lets an immutable Arena event replay the
    observed transition instead of accidentally serializing later live state.
    """
    snapshot = getattr(vehicle, 'movable_armor_state', None) if states is None else states
    if snapshot is None:
        return b''
    rows = getattr(snapshot, 'states', snapshot)
    try:
        rows = tuple((ident, int(enabled)) for ident, enabled in rows)
    except (TypeError, ValueError) as exc:
        raise ValueError('Invalid movable armor state snapshot') from exc
    return armor_states_message(vehicle.entity_id, rows)


TRACK_DEVICES = (34, 35)  # left/right; RO mapping proves index == XML type
ENGINE_DEVICE = 31  # engineHealth
AMMO_RACK_DEVICE = 32  # ammoBayHealth
FUEL_TANK_DEVICE = 33  # fuelTankHealth: zero HP still uses the orange marker
GUN_DEVICE = 36  # gunHealth; native 0x847300/0x847340/0x847590 branches are generic
# commander/driver/gunner1/2/loader1/2 Health. Native 11.21 probes verified
# Avatar16 reason0 marker and reason19 healing for every listed extra. Geometry
# remains the separate authority for whether a person can be hit in simulation.
CREW_DEVICES = (39, 40, 41, 42, 43, 44)
MODULE_DEVICES = (ENGINE_DEVICE, AMMO_RACK_DEVICE, FUEL_TANK_DEVICE, *TRACK_DEVICES, GUN_DEVICE,
                  *CREW_DEVICES)
MODULE_REASONS = (0, 1, 18, 19)  # critical, destroyed, autorepair, full repair


def state_modifiers_message(entity_id, modifiers):
    """Full Vehicle publicStateModifiers snapshot (property7), not device HP.

    Native decoder0x178213c -> setter0x1842ab0 starts/stops extras by set
    difference. Extra4 is Fire; TrackHealthEntityExtra34/35 uses the same
    start/stop path for its left/right 3D torn-track visual. The transport is
    a set though BigWorld encodes a vector<u8>, so redundant IDs are removed.
    This message supplies no ignition, damage, repair timer or consumable use.
    Callers must preserve every active modifier in the complete snapshot.
    """
    modifiers = tuple(dict.fromkeys(modifiers))
    return b'\x12' + struct.pack('<I', entity_id) + variable8(165, device_vector(modifiers))


def module_state_messages(entity_id, critical, destroyed):
    """Vehicle public property8 criticalDevices and property9 destroyedDevices.

    Both are complete snapshots of vector<u8>, sent to every observer.
    """
    return b''.join(b'\x12' + struct.pack('<I', entity_id) + variable8(158 + number, device_vector(devices))
                    for number,devices in ((8, critical), (9, destroyed)))


def module_damage_message(entity_id, reason, extra):
    """Avatar client method16 (decoder0x177e56e -> showVehicleDamageInfo0x1845870).

    vehicle u32, reason u8, extraIndex u8, unknown u32=0. Reason18 restores
    the motion visual but keeps the critical UI; reason19 removes DeviceHealth.
    """
    if type(reason) is not int or type(extra) is not int or reason not in MODULE_REASONS or extra not in MODULE_DEVICES:
        raise ValueError('Unsupported module damage info')
    return avatar_method(16, struct.pack('<IBBI', entity_id, reason, extra, 0))


def module_repair_message(entity_id, extra, progress, duration):
    """Avatar19 / MiscStatus7 natural TrackHealth repair tween.

    Decoder 0x177e0d3 reads ``<u32,u8,u32,f32>``.  Code7 reaches
    DamagedModules::UpdateModuleRepair: packed bits0..7 select the
    TrackHealth EntityExtra, bits8..15 are completed natural-repair percent,
    and the f32 is the remaining native animation duration in seconds.  The
    controller interpolates the supplied start rectangle to the repaired end
    rectangle; percent 0 starts a full repair tween, while 100 is already at
    that end state.
    """
    if type(entity_id) is not int or not 0 < entity_id <= 0xffffffff:
        raise ValueError('Invalid repair entity ID')
    if type(extra) is not int or extra not in TRACK_DEVICES:
        raise ValueError('Natural repair only supports TrackHealth extras')
    if type(progress) is not int or not 0 <= progress <= 100:
        raise ValueError('Repair progress must be an integer percentage')
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or not 0 < duration <= 3.4e38):
        raise ValueError('Invalid repair duration')
    duration32 = struct.pack('<f', float(duration))
    if struct.unpack('<f', duration32)[0] <= 0:
        # DAVA::Animation divides elapsed time by this f32; reject values
        # that are positive in Python but underflow to zero on the wire.
        raise ValueError('Repair duration underflows native float32')
    return avatar_method(19, struct.pack('<IBI', entity_id, 7,
                                          extra | (progress << 8)) + duration32)


def marker_message(arena, vehicle):
    _,point = arena.aim_hit(vehicle)
    _,direction = arena.shot_ray(vehicle)
    return avatar_method(24,struct.pack('<7f',*point,*direction,0.))


def motion_messages(arena, player, frame=None):
    result = bytearray()
    for _,v in present_vehicles(arena):
        sample = frame.samples[v.entity_id] if frame is not None else None
        p = sample.pose if sample is not None else v.pose
        result += vehicle_snapshot(v.entity_id,p.position,p.yaw,p.pitch,p.roll)
        result += vehicle_property(v.entity_id,2,
                                   sample.gun_packed if sample is not None else v.gun.packed())
        # Property4 drives the native power/belt animation independently of
        # body velocity, including spinning tracks while pushing a wall.
        mode = sample.engine_mode if sample is not None else v.engine_mode
        result += vehicle_property(v.entity_id,4,bytes(mode))
    own = arena.vehicles[player.vehicle_id]
    sample = frame.samples[own.entity_id] if frame is not None else None
    result += b'\x13\xa7'+struct.pack('<f',sample.gun_yaw if sample is not None else own.gun.yaw)
    if own.alive:
        if sample is None:
            result += marker_message(arena,own)
        elif sample.marker is not None:
            point,direction = sample.marker
            result += avatar_method(24,struct.pack('<7f',*point,*direction,0.))
    return bytes(result)


def event_messages(arena, player, events):
    # A normal shell switch keeps the authoritative reload deadline.  Its
    # immediate ``shell_selected, ammo, reload`` group nevertheless used to
    # replay Avatar13 state4, which re-starts the native HUD transition.  Mark
    # only that immutable event shape before serializing: a burst's final
    # selected shell follows a shot and its reload is real, while a pump switch
    # is followed by pump_reload and deliberately starts a new pump timer.
    events = tuple(events)
    preserved_reload = set()
    for index in range(len(events)-2):
        selected, ammo, reload = events[index:index+3]
        if (selected.kind == 'shell_selected' and ammo.kind == 'ammo'
                and reload.kind == 'reload'
                and selected.actor == ammo.actor == reload.actor
                and not (index and events[index-1].kind == 'shot'
                         and events[index-1].actor == selected.actor)
                and not (index+3 < len(events) and events[index+3].kind == 'pump_reload'
                         and events[index+3].actor == selected.actor)):
            preserved_reload.add(index+2)
    result = bytearray()
    allied_reload = set()
    update_team_score = False
    for index,event in enumerate(events):
        actor = arena.vehicles.get(event.actor)
        own = event.actor == player.vehicle_id
        d = event.data
        if (event.kind in ('ready','reload','pump_reload') and not own
                and actor is not None and actor.team == player.team):
            allied_reload.add(event.actor)
        if event.kind == 'practice_spawn':
            entity = actor.entity_id
            participants = participant_ids((v.entity_id,v.account_id) for v in arena.vehicles.values())
            info = vehicle_info(entity, actor.nickname, actor.team, participants[entity], actor.descriptor)
            if d['first']:
                result += arena_update(2, pb_blob(2, pb_blob(1, info)))
                alias = tuple(arena.vehicles).index(entity)
                result += b''.join(vehicle_spawn_parts(arena, actor, alias))
            else:
                pose = actor.pose
                result += b'\x14'+struct.pack('<III6f',entity,arena.space_id,0,
                                               *pose.position,pose.roll,pose.pitch,pose.yaw)
                result += module_state_messages(entity, *module_snapshot(arena, actor))
                if own:
                    for extra in d['repaired']:
                        result += module_damage_message(entity, 19, extra)
                result += state_modifiers_message(entity, arena.state_modifiers(actor))
                result += movable_armor_states_message(actor)
                result += vehicle_property(entity,3,struct.pack('<h',actor.health))
                result += vehicle_property(entity,1,b'\1')
                # Update the roster alive flag as well as the world entity.
                # Native equipment input consults the arena's player record.
                result += arena_update(11, pb_blob(10, pb_blob(1, info)))
                result += arena_update(20, pb_blob(6, pb_int(1, entity)))
        elif event.kind=='ready' and own:
            allied_reload.update(v.entity_id for v in arena.vehicles.values()
                                 if v.team == player.team and v.entity_id != player.vehicle_id)
            result += ammo_message(actor)
            if not d.get('respawn'):
                result += b''.join(provision_messages(actor))
            for state in actor.consumables:
                if state:
                    view = state.state_at(arena.simulation_time)
                    result += consumable_message(state.spec.compact_id, view.state, view.remaining, view.total)
                elif not d.get('respawn'):
                    # Preserve empty slot positions: CD0 increments the native
                    # consumable slot cursor at AvatarGameLogic+0x5b4.
                    result += consumable_message(0, 0)
            result += avatar_method(7,struct.pack('<BI',0,actor.shell_id))+targeting_info(
                actor,d.get('yaw_rate'),d.get('pitch_rate'))
            full_factor = actor.reload_speed_factor * getattr(
                arena, 'applied_ammo_reload_factors', {}).get(actor.entity_id, 1.) * getattr(
                arena, 'applied_crew_reload_factors', {}).get(actor.entity_id, 1.)
            result += gun_reload_configuration(actor, full_reload_factor=full_factor)
            result += avatar_method(13,struct.pack('<IBf',actor.entity_id,0,actor.reload_total))
            result += marker_message(arena,actor)
        elif event.kind=='targeting' and own:
            result += targeting_info(actor,d['yaw_rate'],d.get('pitch_rate'))
        elif event.kind=='autoaim_lost' and own:
            # Avatar client method 0, decoder 0x177dcec: one bool byte.
            result += avatar_method(0,b'\0')
        elif event.kind in ('auto_recovered','orientation_rebased') and actor is not None:
            # Recovery already replaces the authoritative pose in one step.
            # Ordinary volatile updates interpolate that roll through terrain;
            # A handoff can also relabel an identical rotation by a full turn.
            # forcedPosition marks either numerical discontinuity for viewers.
            result += b'\x14'+struct.pack('<III6f',event.actor,arena.space_id,0,
                *d['position'],d['roll'],d['pitch'],d['yaw'])
        elif event.kind=='period':
            result += period_message(d['period'],d['seconds'],d['winner'],
                                     0 if getattr(arena, 'endless', False) else getattr(arena, 'battle_seconds', 600),
                                     finish_reason=getattr(arena, 'finish_reason', None))
        elif event.kind == 'base_points':
            for team in (1, 2):
                result += base_points_message(team, d['points'][team - 1],
                                              d['blocked'], d['base_id'])
        elif event.kind == 'base_captured':
            result += arena_update(9, pb_blob(9, pb_int(1, 3 - d['team'])
                                             + pb_int(2, d['base_id'])))
        elif event.kind == 'supremacy':
            result += b''.join(supremacy_messages(arena, player, d['state'], d.get('previous')))
        elif event.kind=='shot':
            count=d.get('shooting_count',1)
            if count:
                result += shooting_message(event.actor,count)
            if d.get('guided'):
                result += missile_message(event.actor,d['shot'],d['effect_index'],d['origin'],d['velocity'])
            else:
                result += avatar_method(29,struct.pack('<IIB7f',event.actor,d['shot'],d.get('effect_index',0),*d['origin'],*d['velocity'],d['gravity']))
        elif event.kind=='missile_move':
            result += missile_message(event.actor,d['shot'],d['effect_index'],d['origin'],d['velocity'])
        elif event.kind=='ammo' and own:
            result += ammo_message(actor)
        elif event.kind=='shell_selected' and own:
            result += avatar_method(7,struct.pack('<BI',0,d['shell']))
        elif event.kind=='reload' and own:
            if index in preserved_reload:
                continue
            full_factor = actor.reload_speed_factor * getattr(
                arena, 'applied_ammo_reload_factors', {}).get(actor.entity_id, 1.) * getattr(
                arena, 'applied_crew_reload_factors', {}).get(actor.entity_id, 1.)
            if d['delay'] and gun_reload_configuration(actor, full_reload_factor=full_factor):
                # Native 6->4 does not clear the pump flag or partial progress;
                # a known 0->4 does. Send state0 (nominal interval B) first.
                result += avatar_method(13,struct.pack('<IBf',event.actor,0,
                    actor.definition.weapon.interval/actor.reload_speed_factor*actor.interval_factor))
            result += avatar_method(13,struct.pack('<IBf',event.actor,4 if d['delay'] else 0,
                                                d['delay'] or d.get('total',0.)))
        elif event.kind=='pump_reload' and own:
            # remaining>0 is checked first: a nonempty refill may also have
            # readyGun=True and must start the state6 secondary timer.
            # Ready (remaining0) sends Avatar13 state0 with nominal B, but
            # UpdateArena time_left 0. A waiting intershot (remaining0, not
            # ready) sends nothing so it cannot override the reload gate.
            full_factor = actor.reload_speed_factor * getattr(
                arena, 'applied_ammo_reload_factors', {}).get(actor.entity_id, 1.) * getattr(
                arena, 'applied_crew_reload_factors', {}).get(actor.entity_id, 1.)
            config = gun_reload_configuration(actor, full_reload_factor=full_factor)
            if not config:
                continue
            if d['remaining'] > 0:
                state,value,nominal = 6,d['remaining'],d['remaining']
                result += config  # Refresh live consumable factors first.
            elif d['readyGun']:
                state,value = 0,0.
                nominal = actor.definition.weapon.interval/actor.reload_speed_factor*actor.interval_factor
            else:
                continue
            result += avatar_method(13,struct.pack('<IBf',event.actor,state,nominal))
            result += arena_update(15,pb_blob(14,pb_blob(1,pb_int(1,event.actor)+pb_int(2,state)
                +varint((3<<3)|5)+struct.pack('<f',value)+pb_int(4,d['clip']))))
        elif event.kind=='consumable_state':
            if own:
                result += consumable_message(d['compact_id'], d['state'], d['remaining'], d['total'])
            result += active_equipment_message(arena, actor, d['equipment'])
        elif event.kind=='module_state':
            # Immutable snapshots from the event, not the live vehicle state.
            result += module_state_messages(event.actor, d['critical'], d['destroyed'])
            if own:
                result += module_damage_message(event.actor, d['reason'], d['extra'])
        elif event.kind=='module_repair' and own:
            result += module_repair_message(event.actor, d['extra'], d['progress'], d['duration'])
        elif event.kind=='state_modifiers':
            result += state_modifiers_message(event.actor, d['modifiers'])
        elif event.kind=='armor_states':
            # Arena stores a complete immutable snapshot on the event.  Every
            # observer needs it because property16 controls public geometry.
            result += movable_armor_states_message(actor, d['states'])
        elif event.kind=='provision_effect':
            result += provision_effect_message(event.actor, d['compact_id'])
        elif event.kind=='explosion':
            # Avatar27 / decoder0x177eab3: shot, shell CD, material, position,
            # shot direction (not surface normal), unused byte. Keep stopTracer.
            result += avatar_method(27,struct.pack('<IIB6fB',d['shot'],d['shell_id'],d['material'],
                *d['point'],*d['direction'],0))
        elif event.kind in ('impact','shot_end','splash'):
            if event.kind != 'splash':
                if d.get('guided'):
                    # Avatar23 / 0x177f25b, stopATGM. The final byte is
                    # unused by the pinned client's tracer and player listeners.
                    result += avatar_method(23,struct.pack('<I3fB',d['shot'],*d['point'],0))
                else:
                    result += avatar_method(20,struct.pack('<I3f',d['shot'],*d['point']))
            # HP hits are reported once, immediately before their health
            # update below. Blocked/external hits have no health event but
            # still need the native hit/ricochet screen reaction.
            if (event.kind != 'shot_end' and event.target == player.vehicle_id
                    and event.target in arena.vehicles and actor is not None
                    and event.actor != event.target and not d.get('applied_damage')
                    and d['result'].hit is not None):
                result += received_damage_message(event.target,event.actor,0,
                    outcome=d['result'].outcome,blocked_damage=min(0xffff,d.get('blocked_damage',0)),
                    hit_effect=d.get('hit_effect'))
        elif event.kind=='feedback' and own:
            result += shot_feedback(event.target,d['outcome'])
        elif event.kind=='damage_statistics' and own:
            # Avatar cell property 10 (+0x118): cumulative damage dealt.
            result += b'\x13\xa8'+struct.pack('<I',d['damage'])
        elif event.kind=='blocked_statistics' and own:
            # Property 11 (+0x11c): cumulative damage blocked by armor.
            result += b'\x13\xa9'+struct.pack('<I',d['damage'])
        elif event.kind=='assisted_statistics' and own:
            # Property 12 (+0x120): damage assisted by immobilisation.
            result += b'\x13\xaa'+struct.pack('<I',d['damage'])
        elif event.kind=='health':
            # Private incoming hit notification. Send while the native vehicle
            # is still alive, including a lethal hit; HP state follows below.
            if event.target == player.vehicle_id and d.get('received_damage'):
                result += received_damage_message(event.target, event.actor, d['received_damage'],
                                                   hit_effect=d.get('hit_effect'))
            result += vehicle_property(event.target,3,struct.pack('<h',d['health']))
            result += health_changed(event.target,d['health'],event.actor)
        elif event.kind=='killed':
            result += vehicle_property(event.target,1,b'\0')
            # The vehicle still dies in the world. Keep its roster entry active
            # while it waits for the next life and update kills independently.
            if not d.get('keep_roster'):
                result += arena_update(6,pb_blob(6,pb_int(1,event.target)+pb_int(2,event.actor)))
            if actor is not None:
                result += arena_update(5,pb_blob(5,pb_blob(1,pb_int(1,event.actor)+pb_int(2,actor.kills))))
                update_team_score = update_team_score or (
                    getattr(arena, 'unlimited_lives', False) and arena.supremacy is None)
    if update_team_score:
        result += team_kill_score(arena, player)
    # Reload and pump edges can share one step. Send one final authoritative
    # readiness per ally so a pump refill cannot overwrite an intershot gate.
    for entity in sorted(allied_reload):
        result += allied_reload_message(arena,player,arena.vehicles[entity])
    changed = [e for e in events if e.kind=='destruction']
    if changed:
        state = destructible_state(arena)
        state.dirty = {(e.data['key']>>16,
            3 if arena.world.assets.destructibles[e.data['key']].kind==8 else
            2 if arena.world.assets.destructibles[e.data['key']].kind==5 else 1) for e in changed}
        result += state.updates()
    return bytes(result)


def _reconnect_reload_parts(arena, vehicle):
    """Current private gun timer state for a replacement Avatar.

    Reload events are edge-triggered in normal play, so a new session cannot
    reconstruct one that started before its transport disappeared.  Reuse the
    same known Avatar13/UpdateArena forms as ``event_messages`` and calculate
    the remaining time from the authoritative simulation clock.
    """
    now = arena.simulation_time
    full_factor = vehicle.reload_speed_factor * getattr(
        arena, 'applied_ammo_reload_factors', {}).get(vehicle.entity_id, 1.) * getattr(
        arena, 'applied_crew_reload_factors', {}).get(vehicle.entity_id, 1.)
    config = gun_reload_configuration(vehicle, full_reload_factor=full_factor)
    remaining = max(0., vehicle.reload_at-now) if vehicle.reload_at else 0.
    if vehicle.pump_times:
        if vehicle.pump_at:
            refill = max(0., vehicle.pump_at-now)
            return (config,
                    avatar_method(13, struct.pack('<IBf', vehicle.entity_id, 6, refill)),
                    arena_update(15, pb_blob(14, pb_blob(1, pb_int(1, vehicle.entity_id)
                        + pb_int(2, 6) + varint((3 << 3) | 5) + struct.pack('<f', refill)
                        + pb_int(4, vehicle.clip)))))
        if remaining:
            nominal = vehicle.definition.weapon.interval / vehicle.reload_speed_factor * vehicle.interval_factor
            return (config,
                    avatar_method(13, struct.pack('<IBf', vehicle.entity_id, 0, nominal)),
                    avatar_method(13, struct.pack('<IBf', vehicle.entity_id, 4, remaining)))
        nominal = vehicle.definition.weapon.interval / vehicle.reload_speed_factor * vehicle.interval_factor
        return (config, avatar_method(13, struct.pack('<IBf', vehicle.entity_id, 0, nominal)))
    # A normal ready gun deliberately receives state0 plus its nominal next
    # reload value, matching the existing ready/reload path.
    return (config,
            avatar_method(13, struct.pack('<IBf', vehicle.entity_id,
                                           4 if remaining else 0,
                                           remaining or vehicle.reload_total)))


def reconnect_message_parts(arena, player):
    """Authoritative owner/private state that is not retained by entity spawn."""
    own = arena.vehicles[player.vehicle_id]
    stats = b''.join(pb_blob(1, pb_int(1, v.entity_id)+pb_int(2, v.kills))
                     for _,v in present_vehicles(arena))
    parts = [arena_update(4, pb_blob(4, stats)),
             *base_capture_messages(arena), *supremacy_messages(arena, player),
             ammo_message(own), *provision_messages(own), b'\x13\xa8'+struct.pack('<I',own.damage),
             b'\x13\xa9'+struct.pack('<I',own.blocked_damage),
             b'\x13\xaa'+struct.pack('<I',own.assisted_damage)]
    if getattr(arena, 'unlimited_lives', False) and arena.supremacy is None:
        parts.append(team_kill_score(arena, player))
    battle_seconds = 0 if getattr(arena, 'endless', False) else arena.battle_seconds
    if arena.finished_at:
        parts.append(period_message(4, max(0., arena.finished_at+arena.POSTBATTLE_SECONDS-arena.simulation_time),
                                    arena.winner, battle_seconds, finish_reason=arena.finish_reason))
    elif arena.started and getattr(arena, 'endless', False):
        parts.append(period_message(3, 0, battle_seconds=0))
    elif arena.started:
        ends = getattr(arena, 'practice_timer_at', 0.) or arena.started+arena.battle_seconds
        parts.append(period_message(3, max(0., ends-arena.simulation_time),
                                    battle_seconds=battle_seconds))
    elif getattr(arena, 'prebattle_end', 0.):
        parts.append(period_message(2, max(0., arena.prebattle_end-arena.simulation_time),
                                    battle_seconds=battle_seconds))
    for state in own.consumables:
        if state:
            view = state.state_at(arena.simulation_time)
            parts.append(consumable_message(state.spec.compact_id, view.state, view.remaining, view.total))
        else:
            parts.append(consumable_message(0, 0))
    parts.extend((avatar_method(7, struct.pack('<BI', 0, own.shell_id)),
                  targeting_info(own), *_reconnect_reload_parts(arena, own)))
    parts.extend(_allied_reload_parts(arena, player))
    # Creation does carry these state vectors, but repeat full snapshots after
    # the new Avatar owns its vehicle.  This also covers client-side reset of
    # damaged-module visuals during a BaseApp replacement.
    for _, vehicle in present_vehicles(arena):
        parts.append(vehicle_property(vehicle.entity_id, 1, bytes([vehicle.alive])))
        parts.append(vehicle_property(vehicle.entity_id, 3, struct.pack('<h', vehicle.health)))
        critical, destroyed = module_snapshot(arena, vehicle)
        parts.append(module_state_messages(vehicle.entity_id, critical, destroyed))
        parts.append(state_modifiers_message(vehicle.entity_id,
                     getattr(arena, 'state_modifiers', lambda _: ())(vehicle)))
        if vehicle is own:
            # A reconnect creates new TrackHealth extras; let property7
            # register them before the owner-only Avatar19 lookup by index.
            for extra,progress,duration in getattr(
                    arena, 'track_repair_status', lambda _: ())(vehicle):
                parts.append(module_repair_message(vehicle.entity_id, extra, progress, duration))
        parts.append(active_equipment_message(arena, vehicle))
        armor_state = movable_armor_states_message(vehicle)
        if armor_state:
            parts.append(armor_state)
    for person in getattr(arena, 'injured_crew', lambda vehicle: ())(own):
        parts.append(module_damage_message(own.entity_id, 0, person.spec.extra_health_index))
    return tuple(part for part in parts if part)


def _message_batches(parts, limit=900):
    """Coalesce complete known protocol units without splitting headers."""
    if type(limit) is not int or limit <= 0:
        raise ValueError('Batch limit must be a positive integer')
    batches, pending = [], bytearray()
    for part in parts:
        if pending and len(pending)+len(part) > limit:
            batches.append(bytes(pending))
            pending.clear()
        if len(part) > limit:
            batches.append(part)
        else:
            pending.extend(part)
    if pending:
        batches.append(bytes(pending))
    return tuple(batches)


def reconnect_batches(arena, player, limit=900):
    """Packet-safe reconnect replay, separate from generic event delivery."""
    return _message_batches(reconnect_message_parts(arena, player), limit)


def dispatch_input(arena, account_id, kind, body, now=None):
    """Decode only validated RPCs; world state does not depend on wire IDs."""
    from .arena_commands import decode_input, apply_input
    apply_input(arena, decode_input(account_id, kind, body, now))
