"""Pinned client battle wire adapter; simulation lives in arena.py."""
from dataclasses import replace
from functools import lru_cache
import struct
import math
import time

from .account import pickle_document, variable16
from . import big_boss, mad_games, uprising
from .mercury import packed_blob, read_blob, variable8
from .motion import vehicle_snapshot
from .destructibles import DestructibleState
from .consumables import ACTIVE, READY, COOLDOWN
from .sandbox import (BRANCH_SHOT_BASE, reload_limit, HP_RESTORED, PRACTICE_NOTICE,
                      TARGET_PLACED, TARGET_SELECTED, TARGET_UNAVAILABLE, TELEPORTED,
                      advertisement as sandbox_advertisement, boss_picker_message, vehicle_wire_id)


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


def vehicle_info(entity_id, name, team, account_id, descriptor, *, alive=True, camouflage=0):
    return (pb_int(1, entity_id) + pb_blob(2, descriptor)
            + pb_blob(3, name.encode()) + pb_int(4, team)
            + pb_int(5, int(alive)) + pb_int(6, 1) + pb_int(7, account_id)
            + (pb_int(13, camouflage) if camouflage else b'')
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


def create_avatar(account, avatar_id=AVATAR_ID, arena_id=1, map_id=6, *, game_mode=2):
    # Avatar::readBaseProperties, 0x177b1f0. arenaTypeID combines the
    # map in its low word and gameplay in its high word.
    properties = (packed_blob(account["nickname"].encode())
                  + struct.pack("<QI", arena_id, map_id)
                  + packed_blob(pickle_document({"mouseEnabled": True}))
                  + struct.pack("<BH", 0, 0)
                  + packed_blob(pickle_document({}))
                  # +0xd8 is observer mode, NOT arena bonus type. Nonzero
                  # makes AvatarGameLogic ignore ammo, reload and health RPCs.
                  # +0xd9 selects the native game mode and its battle effects.
                  + struct.pack("<BB", 0, game_mode))
    return variable16(5, struct.pack("<IH", avatar_id, 1) + properties)


def boss_ability_cards(abilities):
    return bytes([len(abilities)]) + b''.join(struct.pack('<IB', ident, grade)
                                             for ident, grade in abilities)


def create_avatar_cell(space_id=SPACE_ID, team=1, respawns_left=0, abilities=(), *, enemy_reload=False):
    # Client ServerConnection::createCellPlayer 0x2818240 uses the vanilla
    # header, unlike current desktop WoT: space, vehicle, position, scale, dir.
    header = struct.pack("<II7f", space_id, 0, 0, 0, 0, 0.0076444, 0, 0, 0)
    # Avatar::readCellProperties 0x177b450. Player vehicle remains unset
    # until the arena vehicle roster and Vehicle entity have been delivered.
    # First cell property is Avatar +0xda, the player's team. Native
    # 0x180d4a1 compares it with Vehicle.publicInfo.team at +0xb0.
    # Avatar +0xec: array of (item compact descriptor, ability grade).
    props = (struct.pack("<BIBBfHB", team, 0, 0, 0, 0.0, 0, respawns_left)
             + boss_ability_cards(abilities) + struct.pack("<fB", 0.0, enemy_reload)
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


def provision_effect_message(entity_id, compact_id, *, argument=0):
    """Avatar45: BW blob of ShortClientEffect{ExtrasEffect(entity, itemCD)}.

    Native 177a6c0 group18 returns -1 for45, as for36/38. Decoder177f57d
    reads a BW blob; protobuf21ae7f0/21ae3a0 uses nested field1, then entity
    varint64 field1, compactCD varint32 field2 and int32 argument field3.
    AntiHE handler1d4e1e0 reads the visual duration from the pinned item itself.
    """
    if (type(entity_id) is not int or not 0 < entity_id <= 0xffffffff
            or (compact_id not in (26109, big_boss.BLAST_WAVE.compact_id,
                                   mad_games.URANIUM_SHIELD.compact_id, mad_games.CHAMELEON.compact_id,
                                   mad_games.REFLECTOR.compact_id, mad_games.DISCHARGE.compact_id,
                                   mad_games.REGENERATOR_EFFECT, uprising.CURSE.compact_id,
                                   *(spec.compact_id for spec in uprising.CLOAKS))
                and compact_id not in big_boss.ROLE_EFFECTS
                and compact_id not in big_boss.HEAL_SHOT_EFFECTS)):
        raise ValueError('Unsupported short provision effect')
    max_argument = {mad_games.CHAMELEON.compact_id: 1, mad_games.REFLECTOR.compact_id: 1,
                    mad_games.REGENERATOR_EFFECT: 0x7fffffff, uprising.CURSE.compact_id: 1,
                    **{spec.compact_id: 1 for spec in uprising.CLOAKS}}.get(compact_id, 0)
    if type(argument) is not int or not 0 <= argument <= max_argument:
        raise ValueError('Unsupported short provision effect argument')
    child = pb_int(1, entity_id) + pb_int(2, compact_id)
    if argument:
        child += pb_int(3, argument)
    return avatar_method(45, packed_blob(pb_blob(1, child)))


@lru_cache(maxsize=512)
def _gravity_trap_effect(trap):
    position = b''.join(bytes([(field << 3) | 5]) + struct.pack('<f', value)
                        for field, value in enumerate(trap.center, 1))
    gravity_shot = pb_blob(1, position) + pb_int(2, trap.owner)
    effect = pb_blob(1, pb_blob(1, gravity_shot))
    return packed_blob(pickle_document((trap.ident, effect)))


def gravity_traps_message(traps):
    # Avatar property 24 diffs effects by entry. Reuse each trap's wire
    # representation across both successive snapshots and recipients.
    size = len(traps)
    count = bytes([size]) if size < 255 else b'\xff' + size.to_bytes(3, 'little')
    return b'\x13' + variable8(158 + 24, count + b''.join(map(_gravity_trap_effect, traps)))


@lru_cache(maxsize=512)
def _revenge_target_effect(entity_id):
    # ContinuousClientEffect.avatar -> AvatarClientEffect.revengeEffect.
    # Give each target its own ID so removing one mark leaves the others active.
    effect = pb_blob(1, pb_blob(2, pb_int(1, entity_id)))
    return packed_blob(pickle_document((entity_id, effect)))


def revenge_targets_message(arena, owner):
    targets = sorted(entity for (entity, generation), until in owner.mad_revenge.items()
                     if (target := arena.vehicles.get(entity)) is not None
                     and target.alive and target.generation == generation
                     and arena.simulation_time < until and not mad_games.concealed(target, owner.team))
    return b'\x13' + variable8(182, bytes([len(targets)]) + b''.join(map(_revenge_target_effect, targets)))


def public_gravity_traps(arena):
    timeline = getattr(arena, 'time_stop', None)
    visuals = getattr(timeline, 'merge_traps', ())
    boss = getattr(arena, 'big_boss', None)
    return (*boss.traps, *visuals) if boss is not None else ()


def vehicle_client_effects_message(arena, entity_id, effects):
    # Vehicle property 17 contains pickled continuous effects, just like
    # Avatar.clientEffects. Effect removal is driven by the authoritative
    # snapshot. The client extrapolates its clock during loading and can be
    # a few tick-sync wraps ahead, so leave room in its local expiry timer.
    entries = []
    for key, kind, start, end in effects:
        times = b'\x0d' + struct.pack('<f', start-arena.epoch) + b'\x15' + struct.pack('<f', end-start+30.)
        entries.append(packed_blob(pickle_document((key, pb_blob(2, pb_blob(kind, times))))))
    return b'\x12' + struct.pack('<I', entity_id) + variable8(158+17, bytes([len(entries)])+b''.join(entries))


def vehicle_property(entity_id, number, payload):
    return b"\x12" + struct.pack("<I", entity_id) + bytes([158 + number]) + payload


def health_changed(entity_id, health, attacker=VEHICLE_ID, *, healing_marker=False):
    # Vehicle method 1 / 0x177fcdc: i16 new HP, u32 attacker, u8 reason.
    # Send after property3: set_health caches old HP for the UI damage delta.
    # The remote VehicleUIObject displays a positive delta only for reason 6 or 7.
    return b'\x12'+struct.pack('<IBhIB',entity_id,60,health,attacker,6 if healing_marker else 0)


def received_damage_message(entity_id, attacker, damage, *, outcome=None, blocked_damage=0,
                            hit_effect=None, reflector_hit=False):
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
    # The scene's HitExtraEffectEvent takes both these IDs from Vehicle8.
    # The pinned Reflector listener looks for modifier 174 on the second ID.
    body = struct.pack('<II', attacker, entity_id if reflector_hit else 0)
    body += b'\1' + struct.pack('<Q',segment | kind)
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

def ammo_bay_explosion_message(entity_id, attacker):
    # Vehicle method2 sends two u32s to showAmmoBayExplosion. The pinned
    # visual handler uses the victim entity and leaves both arguments unused.
    return b'\x12' + struct.pack('<IBII', entity_id, 61, attacker, 0)

def detached_turret_message(turret, parent, at):
    position, yaw, pitch, roll = turret.pose_at(at)
    # DetachedTurret type8 has one initial public property, parent vehicle ID.
    body = b'\0' + struct.pack('<IH6fBBI', turret.entity, 8, *position, yaw, pitch, roll,
                               1, 0, parent)
    return b'\x0a' + struct.pack('<IB', turret.entity, 255) + variable16(9, body)

def restore_turret_message(entity):
    return b'\x13' + variable8(96, b'\x03TFS1TRRT' + struct.pack('<I', entity) + b'\0')


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
    factors = (pb_double(1, vehicle.effective_dispersion_factor if vehicle else 1.)
               + pb_double(2, .1) + pb_blob(3, pb_blob(1, regular)))
    aiming_time = config.aiming_time if config else 1.7
    if vehicle:
        aiming_time *= vehicle.effective_aiming_time_factor
    info = (pb_double(1, 0) + pb_double(2, 0) + pb_double(3, yaw_rate)
            + pb_double(4, pitch_rate)
            + pb_double(5, aiming_time) + pb_blob(6, factors))
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


def observed_status_message(entity_id, observed):
    # UpdateArena::OBSERVED_STATUS (16), protobuf field 15. The pinned
    # ClientArena maps encoded observation type 8 to internal type 1.
    row = pb_int(1, entity_id) + pb_int(2, int(observed)) + pb_int(3, 8)
    return arena_update(16, pb_blob(15, row))


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
    messages.extend(team_points_message(team, scores[team-1])
                    for team in (1, 2)
                    if previous is None or previous[0][team-1] != scores[team-1])
    return tuple(messages)


def team_kill_score(arena, player):
    totals = [0, 0]
    for vehicle in arena.vehicles.values():
        totals[vehicle.team != player.team] += vehicle.kills
    # Avatar37 vector sentinel for the Solbreaker HUD. Supremacy retains its
    # native capture points, which are independent of the kill totals.
    return b'\x13' + variable8(96, b'\x04TFS1FRAG' + struct.pack('<II', *totals) + b'\0')


def team_points_message(team, score):
    return arena_update(13, pb_blob(12, pb_int(1, team) + pb_int(2, score)))


SOUND_ENEMY_SIGHTED = 2
SOUND_PLAYER_SIGHTED = 3
VOICE_ENEMY_KILLED = 0x22
VOICE_TRACK_DESTROYED = 0x4f


def crew_voice_message(cue):
    # Avatar37 routes cues to the pinned client's existing battle sound queue.
    return b'\x13' + variable8(96, b'\x03TFS1VOIC' + struct.pack('<I', cue) + b'\0')


def battle_hints_message():
    """Refresh the native battle HUD after its visibility bindings are ready.

    Avatar48 operation 23 selects UpdateArena.battle_hints_info (field 20).
    The pinned client's ClientArena listener recalculates visibility aspect 15
    from this update; round zero is the single-round sandbox's current round.
    """
    return arena_update(23, pb_blob(20, pb_int(1, 0)))


def initial_messages(arena, player):
    boss = getattr(arena, 'big_boss', None)
    arcade = getattr(arena, 'mad_games_mode', False)
    resurrection = getattr(arena, 'uprising', None)
    game_mode, lives = 2, 1
    if boss is not None:
        game_mode = big_boss.CLIENT_GAME_MODE
        lives = boss.lives.get(player.vehicle_id, 1)
    elif arcade:
        game_mode = mad_games.CLIENT_GAME_MODE
    elif resurrection is not None:
        game_mode = uprising.CLIENT_GAME_MODE
        lives = resurrection.remaining(player.vehicle_id)
    result = create_avatar({'nickname':player.nickname},player.avatar_id,arena.unique_id,
                           getattr(arena,'client_map_id',getattr(arena,'map_id',6)),
                           game_mode=game_mode)
    abilities = ()
    if boss is not None:
        abilities = tuple((compact_id, 0) for compact_id in boss.abilities(player.vehicle_id))
    enemy_reload = resurrection is not None and resurrection.has(player.vehicle_id, uprising.ENEMY_RELOAD)
    result += create_avatar_cell(arena.space_id, player.team, future_respawns(lives), abilities,
                                 enemy_reload=enemy_reload)
    if boss is not None:
        # The pinned client selects the BossMode HUD and effect handlers from
        # GameModeData.gameplayBehavior when constructing the battle UI.
        mode = pb_int(4, big_boss.LIVES) + pb_blob(5, b'bossmode') + pb_blob(7, b'bossmode')
        if boss.tickets:
            mode += pb_int(10, big_boss.TICKETS) + pb_int(11, big_boss.TICKETS)
        result += arena_update(18, pb_blob(17, mode))
    elif arcade:
        # Native mode table 0x707771 maps arcade to 8. GameModeData's
        # gameplayBehaviour and arcadeAbilities identify the effect rules.
        arcade_name = mad_games.CLIENT_GAMEPLAY
        mode = (pb_blob(5, arcade_name) + pb_blob(7, arcade_name) + pb_blob(8, arcade_name)
                + pb_int(9, arena.supremacy is not None) + pb_int(12, arena.base_capture is not None))
        result += arena_update(18, pb_blob(17, mode))
    elif resurrection is not None:
        # Native mode table at 0x7077af maps hellgames to enum 23.
        name = uprising.CLIENT_GAMEPLAY
        mode = (pb_int(4, future_respawns(uprising.STARTING_LIVES)) + pb_blob(5, name) + pb_blob(7, name)
                + pb_blob(8, name) + pb_int(12, arena.base_capture is not None))
        result += arena_update(18, pb_blob(17, mode))
    return result


def destructible_state(arena):
    assets = arena.world.assets.destructibles
    state = DestructibleState((key for key in assets if key >= 0),entity_base=arena.entity_base+300,
                             alias_base=len(arena.vehicles))
    for key,(yaw,angle) in arena.destructions.items():
        state.destroy(assets[key],yaw,angle)
    return state


def module_snapshot(arena, vehicle):
    """(critical, destroyed) module IDs; arenas without module state are healthy."""
    state = getattr(arena,'module_state',None)
    return state(vehicle) if state else ((),())


def present_vehicles(arena, player=None, *, include_phased=False):
    slots = getattr(arena, 'practice_slots', ())
    timeline = getattr(arena, 'time_stop', None)
    hidden = timeline.entity if not include_phased and timeline is not None and (
        player is None or player.account_id != timeline.account) else None
    for alias, vehicle in enumerate(arena.vehicles.values()):
        if vehicle.entity_id != hidden and (
                vehicle.entity_id not in slots or vehicle.entity_id in arena.practice_spawned):
            yield alias, vehicle


def battle_nickname(arena, vehicle):
    boss = getattr(arena, 'big_boss', None)
    return '★ ' + vehicle.nickname if boss is not None and vehicle.entity_id in boss.bosses else vehicle.nickname


def boss_role_message(entity_id, role=None, level=0, progress=(0, 0)):
    # BossmodeData stores the role in the low nibble (1 = boss). Its high
    # nibble is the role level, unused for bosses.
    value = 1 if role is None else big_boss.ROLE_HUD[role] | ((level+1) << 4)
    current, required = progress
    info = pb_int(1, entity_id) + pb_int(2, value)
    if required:
        info += pb_int(3, current) + pb_int(4, required)
    return arena_update(24, pb_blob(21, info))


def future_respawns(lives):
    return max(0, lives - 1)


def boss_lives_message(lives):
    # Private Avatar property 19 (respawnsLeft). The native HUD shows one
    # heart for the current tank, with additional hearts for future spawns.
    return b'\x13\xb1' + bytes([future_respawns(lives)])


def uprising_elixir_message(balance):
    # Private Avatar property 18 dispatches to the uint16 battlePoints slot
    # at Avatar+0xe8 (0x1780cfc).
    return b'\x13\xb0' + struct.pack('<H', balance)


def uprising_respawn_time_message(seconds):
    # Private Avatar property 21, timeRespawnLeft (Avatar+0xf8).
    return b'\x13\xb3' + struct.pack('<f', seconds)


def enemy_reload_visibility_message(enabled):
    # Private Avatar property 22, canSeeEnemyReload (Avatar+0xfc).
    return b'\x13\xb4' + bytes([enabled])


def uprising_offers_message(offers):
    # Private Avatar property 20, a vector of (compact item ID, status).
    return b'\x13' + variable8(158 + 20, boss_ability_cards(offers))


def uprising_wait_respawn_message(entity, killer):
    # ClientArena::OnVehicleWaitRespawn 0x1d60840 consumes VehicleKilled,
    # the same pair of vehicle IDs as the regular death update.
    return arena_update(19, pb_blob(6, pb_int(1, entity) + pb_int(2, killer)))


def boss_role_messages(arena):
    boss = arena.big_boss
    return tuple(boss_role_message(entity_id, boss.roles.get(entity_id), boss.levels.get(entity_id, 0),
                                   boss.role_progress(entity_id, arena.vehicles[entity_id].max_health))
                 for entity_id in sorted(arena.vehicles))


def chameleon_pose(pose):
    # Keep the native entity and roster alias, but place the enemy copy
    # beyond the playable area until real poses are allowed again.
    return replace(pose, position=(-4096., 0., -4096.), yaw=0., pitch=0., roll=0.)


def vehicle_spawn_parts(arena, vehicle, alias, *, pose=None, health=None):
    pose = vehicle.pose if pose is None else pose
    critical, destroyed = module_snapshot(arena, vehicle)
    entity = vehicle_wire_id(arena, vehicle.entity_id)
    # Entity creation reads yaw/pitch/roll, detailed movement roll/pitch/yaw.
    yield create_vehicle({'nickname':battle_nickname(arena, vehicle)}, vehicle.descriptor, pose.position,
        vehicle.health if health is None else health, entity, vehicle.team, alias,
        (pose.yaw,pose.pitch,pose.roll),
        vehicle.max_health, vehicle.definition.optional_devices if vehicle.definition else (),
        critical, destroyed, gun_angles=vehicle.gun.packed(),
        # The client's particle tree is still being built for new entities.
        # Apply the boss aura at battle start or in the reconnect snapshot.
        state_modifiers=tuple(modifier for modifier in getattr(arena, 'state_modifiers', lambda _: ())(vehicle)
                              if modifier != big_boss.BOSS_AURA_MODIFIER))
    armor_state = movable_armor_states_message(vehicle, entity_id=entity)
    if armor_state:
        # Dynamic property16 is applied only after the Vehicle exists.
        yield armor_state
    boss = getattr(arena, 'big_boss', None)
    resurrection = getattr(arena, 'uprising', None)
    if boss:
        effects = boss.vehicle_effects(vehicle.entity_id)
    elif resurrection is not None:
        effects = resurrection.vehicle_effects(vehicle.entity_id)
        if vehicle.entity_id in resurrection.curses and pose is vehicle.pose:
            yield provision_effect_message(vehicle.entity_id, uprising.CURSE.compact_id)
        if pose is vehicle.pose and vehicle.mad_cloaked:
            if cloak := uprising.active_cloak(vehicle, arena.simulation_time):
                yield provision_effect_message(entity, cloak.compact_id, argument=1)
    else:
        effects = mad_games.irradiation_effects(
            getattr(arena, 'mad_irradiated', {}).get(vehicle.entity_id))
    if effects:
        yield vehicle_client_effects_message(arena, entity, effects)


def spawn_message_parts(arena, player, *, include_owner_state=True):
    """Complete wire units; native Mercury cannot split an element header."""
    participants = participant_ids((v.entity_id,v.account_id) for v in arena.vehicles.values())
    present = tuple(present_vehicles(arena, player, include_phased=True))
    rows = b''.join(pb_blob(1,vehicle_info(vehicle_wire_id(arena, v.entity_id),battle_nickname(arena,v),v.team,participants[v.entity_id],v.descriptor,
                                         alive=v.alive or ((getattr(arena, 'unlimited_lives', False)
                                                            or getattr(arena, 'uprising', None) is not None)
                                                           and v.entity_id in arena.respawn_at),
                                         camouflage=v.camouflage))
                    for _,v in present)
    stats = b''.join(pb_blob(1,pb_int(1,vehicle_wire_id(arena, v.entity_id))+pb_int(2,v.kills)) for _,v in present)
    parts = [arena_update(1,pb_blob(1,rows)),arena_update(4,pb_blob(4,stats)),
             b'\x13\xa6'+struct.pack('<I',player.vehicle_id)]
    boss = getattr(arena, 'big_boss', None)
    if boss is not None:
        parts.extend(team_points_message(team, score) for team, score in enumerate(boss.tickets, 1))
    elif getattr(arena, 'unlimited_lives', False) and arena.supremacy is None:
        parts.append(team_kill_score(arena, player))
    for alias,v in present:
        hidden = mad_games.concealed(v, player.team)
        health = _observer_health(arena, player, v)
        parts.extend(vehicle_spawn_parts(arena, v, alias, health=health,
                                         pose=chameleon_pose(v.pose) if hidden else None))
    for parent, turret in getattr(arena, 'ram_turrets', {}).items():
        parts.append(detached_turret_message(turret, vehicle_wire_id(arena, parent), arena.simulation_time))
    parts.append(b'\x14'+struct.pack('<III6f',player.avatar_id,arena.space_id,player.vehicle_id,0,0,0,0,0,0))
    if boss is not None:
        parts.extend(boss_role_messages(arena))
    # Creation/public vectors do not establish the owner's private crew
    # Health objects. Replay only that owner's wounds after control is set.
    own = arena.vehicles[player.vehicle_id]
    if own.mad_observation_warning:
        parts.append(observed_status_message(own.entity_id, True))
    for person in (getattr(arena, 'injured_crew', lambda vehicle: ())(own) if include_owner_state else ()):
        parts.append(module_damage_message(own.entity_id, 0, person.spec.extra_health_index))
    if arena.world:
        parts.extend(destructible_state(arena).spawn_parts())
    return tuple(parts)


def _observer_health(arena, player, vehicle):
    if (getattr(arena, 'uprising', None) is not None and vehicle.team != player.team
            and uprising.active(vehicle, uprising.PLAY_DEAD, arena.simulation_time)):
        return 0
    return vehicle.health


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


def _ammo_rows(counts, selected, ammo, clip):
    return b''.join(avatar_method(17, struct.pack('<BIHBhh', 10, shell,
        ammo if shell == selected else count, clip if shell == selected else 0, 0, 0))
        for shell, count in counts)


def ammo_message(vehicle):
    counts = vehicle.ammo_by_shell or {vehicle.shell_id:vehicle.ammo}
    return _ammo_rows(counts.items(), vehicle.shell_id, vehicle.ammo, vehicle.clip)


def _can_view_reload(arena, player, vehicle):
    if vehicle.entity_id == player.vehicle_id:
        return False
    if vehicle.team == player.team:
        return True
    state = getattr(arena, 'uprising', None)
    return (state is not None and vehicle.alive and not mad_games.concealed(vehicle, player.team)
            and state.has(player.vehicle_id, uprising.ENEMY_RELOAD)
            and not uprising.active(vehicle, uprising.PLAY_DEAD, arena.simulation_time))


def allied_reload_message(arena, player, vehicle):
    """Public UpdateArena15 for a visible reload marker.

    Avatar13 updates the recipient's private gun regardless of its entity ID.
    Pinned opcode table0x4144240:15=RELOAD_TIME,14=PLAYER_NAME.
    The protobuf oneof field is14; ReloadTimeUpdate carries vehicle_id1, reload_state2,
    time_left3(float) and clip_size_left4 for team-panel/marker consumers.
    A pump with usable rounds is ready while its spare round is refilling.
    """
    if not _can_view_reload(arena, player, vehicle):
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
                 if _can_view_reload(arena, player, vehicle))


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
    if vehicle.sandbox_fast_reload:
        values = (reload_limit(vehicle),) * len(values)
    return _gun_reload_config(vehicle.entity_id, values)


def _gun_reload_config(entity, values):
    count = len(values)-2
    if count > 254 or not all(math.isfinite(v) and v > 0 for v in values):
        raise ValueError('Invalid pump reload configuration')
    return avatar_method(35, struct.pack('<I2fB', entity, *values[:2], count)
                         + struct.pack(f'<{count}f', *values[2:]))


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
    provisions = tuple(getattr(vehicle.definition, 'provisions', ()))
    messages = []
    for ident in provisions:
        installed = int(ident is not None)
        compact_id = (ident << 8) | 0xfd if installed else 0
        messages.append(avatar_method(17, struct.pack('<BIHBhh', 13, compact_id,
                                                      installed, installed, 0, 0)))
    return tuple(messages)


def mad_games_passive_message(vehicle):
    ident = mad_games.PASSIVE_PROVISIONS.get(getattr(vehicle, 'mad_passive', None))
    if ident is None:
        return b''
    # Ability provisions use the separate AbilitiesStatusPanel above the HP
    # bar. They share the native slot cursor with active abilities.
    compact_id = (ident << 8) | 0xfd
    return _passive_ability_message(compact_id)


def _passive_ability_message(compact_id):
    return avatar_method(17, struct.pack('<BIHBhh', 13, compact_id, 1, 1, 0, 0))


def uprising_passive_message(state, entity):
    return b''.join(_passive_ability_message(upgrade.compact_id)
                    for upgrade in (uprising.ENEMY_RELOAD, uprising.DUPLET, uprising.GHOST)
                    if state.has(entity, upgrade))


def uprising_shot_upgrade_messages(state, entity, now):
    return b''.join(consumable_message(compact_id, COOLDOWN if remaining else READY,
                                       math.ceil(remaining), math.ceil(total))
                    for compact_id, remaining, total in state.shot_upgrade_statuses(entity, now))


def active_equipment_message(arena, vehicle, equipment=None, *, entity_id=None):
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
    0x53a1c8/0x53a1cf map High-Octane Fuel script24 to public55.
    0x53a195/0x53a19c map Accelerator script21 to public49.
    Vehicle extras index 57 is the Mad Games Moonwalk (script25).
    Respawn Moonwalk script40 maps to public indicator94 (0x53a21f).
    The native indicator consumer removes an old indicator before checking
    for ACTIVE2 (0x1bc3cd3..0x1bc3cde); COOLDOWN3 therefore stops it.
    """
    public_ids = {18: 9, 14: 10, 80: 189, 29: 63, 45: 105, 27: 61,
                  33: 66, 28: 62, 73: 169, 21: 49, 22: 53, 23: 54, 24: 55, 25: 57, 26: 58,
                  uprising.RESPAWN_CLOAK.ident: 94, 75: mad_games.REFLECTOR_MODIFIER}
    entries = []
    if equipment is None:
        equipment = tuple(row for row in arena.equipment_state(vehicle) if row[1] == ACTIVE)
    for ident,state,started,duration in equipment:
        # Public effects stop on COOLDOWN, even when the owning slot can
        # already return to READY under the sandbox cooldown limit.
        state = ACTIVE if state == ACTIVE else COOLDOWN
        entries.append(struct.pack('<BBdf', public_ids[ident], state, started-arena.epoch,
                                   duration if state == ACTIVE else 0.))
    payload = bytes([len(entries)]) + b''.join(entries)
    # Dynamic entity properties have a VARIABLE8 header outside createEntity.
    entity = vehicle_wire_id(arena, vehicle.entity_id) if entity_id is None else entity_id
    return b'\x12' + struct.pack('<I', entity) + variable8(168, payload)


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


def movable_armor_states_message(vehicle, states=None, *, entity_id=None):
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
    return armor_states_message(vehicle.entity_id if entity_id is None else entity_id, rows)


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
    return gun_marker(*arena.aim_marker(vehicle, arena.simulation_time))


def gun_marker(origin, velocity, dispersion):
    # Avatar24 traces from the muzzle using the unspread shell velocity.
    # The tangent times twice the traced distance gives the circle diameter.
    return avatar_method(24, struct.pack('<7f', *origin, *velocity, dispersion))


def aim_time_message(aiming, aim_factor, progress):
    from .aiming import FULL_SETTLE_TIME_CONSTANTS, settling_time
    seconds = ((1.-progress) * FULL_SETTLE_TIME_CONSTANTS * settling_time(aiming, aim_factor)
               if getattr(aiming, 'dispersion_radius', 0.) else 0.)
    tenths = min(999, math.ceil(max(0.,seconds-1e-6)*10.))
    return b'\x13' + variable8(96, b'\x03TFS1AIMT' + struct.pack('<I', tenths) + b'\0')


def world_motion_messages(arena, frame=None, *, team=None):
    """Encode the live vehicles visible to the receiving team."""
    result = bytearray()
    for _,v in present_vehicles(arena):
        if mad_games.concealed(v, team):
            continue
        sample = frame.samples[v.entity_id] if frame is not None else None
        p = sample.pose if sample is not None else v.pose
        entity = vehicle_wire_id(arena, v.entity_id)
        result += vehicle_snapshot(entity,p.position,p.yaw,p.pitch,p.roll)
        result += vehicle_property(entity,2,
                                   sample.gun_packed if sample is not None else v.gun.packed())
        # Property4 drives the native power/belt animation independently of
        # body velocity, including spinning tracks while pushing a wall.
        mode = sample.engine_mode if sample is not None else v.engine_mode
        result += vehicle_property(entity,4,bytes(mode))
    if getattr(arena, 'practice_client_ids', None):
        result += practice_roster_message(arena)
    for turret in getattr(arena, 'ram_turrets', {}).values():
        at = frame.at if frame is not None else arena.simulation_time
        result += vehicle_snapshot(turret.entity, *turret.pose_at(at))
    return bytes(result)


def practice_roster_message(arena):
    from .sandbox import token

    # Keep the legacy marker vector empty. The second vector lists the
    # currently assigned roster IDs so obsolete rows can be hidden.
    roster = tuple(vehicle_wire_id(arena, entity) for entity in arena.practice_numbers)
    payload = b'\x04TFS1DUMY' + struct.pack('<II', token(arena), 0)
    return b'\x13' + variable8(96, payload + bytes([len(roster)])
                               + struct.pack(f'<{len(roster)}I', *roster))


def motion_messages(arena, player, frame=None, *, world_messages=None):
    timeline = getattr(arena, 'time_stop', None)
    if timeline is not None and timeline.account == player.account_id:
        return temporal_motion(arena, player, timeline)
    if world_messages is None:
        world_messages = world_motion_messages(arena, frame, team=player.team)
    result = bytearray(world_messages)
    own = arena.vehicles[player.vehicle_id]
    sample = frame.samples[own.entity_id] if frame is not None else None
    result += b'\x13\xa7'+struct.pack('<f',sample.gun_yaw if sample is not None else own.gun.yaw)
    if frame is None or frame.tick % 3 == 0:
        aiming = getattr(getattr(own, 'definition', None), 'aiming', None) if own.alive else None
        result += aim_time_message(aiming, own.effective_aiming_time_factor, own.gun.aim_progress)
    if own.alive:
        if sample is None:
            result += marker_message(arena,own)
        elif sample.marker is not None:
            result += gun_marker(*sample.marker)
    return bytes(result)


def temporal_motion(arena, player, timeline):
    """Frozen target snapshots and fresh owner motion share the normal aliases."""
    result = bytearray()
    present = {v.entity_id for _, v in present_vehicles(arena, player)}
    local = timeline.frame
    for tank in timeline.snapshot.tanks:
        entity = tank.identity.ident
        if entity not in present or not tank.visible:
            continue
        live = local is not None and entity == player.vehicle_id
        pose = local.pose if live else tank.body.pose
        entity = vehicle_wire_id(arena, entity)
        result += vehicle_snapshot(entity, pose.position, pose.yaw, pose.pitch, pose.roll)
        result += vehicle_property(entity, 2, local.gun_packed if live else tank.gun_packed)
        result += vehicle_property(entity, 4, bytes(local.engine_mode if live else (0, 0)))
    if local is not None:
        result += b'\x13\xa7'+struct.pack('<f', local.aim[0])
        if local.marker is not None:
            loadout = timeline.snapshot.gun_loadout
            result += aim_time_message(loadout.aiming, loadout.aiming_time_factor, local.aim_progress)
            result += gun_marker(*local.marker)
    if getattr(arena, 'practice_client_ids', None):
        result += practice_roster_message(arena)
    return bytes(result)


def time_stop_message(status, position, *, seconds=0., energy=0):
    seconds_tenths = max(0, min(65535, round(seconds*10)))
    return b'\x13' + variable8(96, b'\x04TFS1TIME'
        + struct.pack('<II', status, seconds_tenths | energy << 16)
        + b'\x03' + struct.pack('<3f', *position))


def time_stop_roster_message(entity):
    return b'\x13' + variable8(96, b'\x03TFS1ROST' + struct.pack('<I', entity) + b'\0')


def projectile_trace_message(trace, *, shot_offset=0, shooter=None):
    shot = trace.shot+shot_offset
    shooter = trace.shooter if shooter is None else shooter
    if trace.guided:
        return missile_message(shooter, shot, trace.effect, trace.position, trace.velocity)
    return avatar_method(29, struct.pack('<IIB7f', shooter, shot, trace.effect,
                                         *trace.position, *trace.velocity, trace.gravity))


def stop_trace_message(shot, point, *, guided=False):
    # Avatar23 adds a final unused byte to the wire layout for missiles.
    if guided:
        return avatar_method(23, struct.pack('<I3fB', shot, *point, 0))
    return avatar_method(20, struct.pack('<I3f', shot, *point))


def event_messages(arena, player, events):
    events = tuple(events)
    timeline = getattr(arena, 'time_stop', None)
    if timeline is not None and timeline.account == player.account_id:
        events = tuple(event for event in events if event.kind.startswith('time_stop_'))
    result = bytearray()
    allied_reload = set()
    changed_destructibles = set()
    update_gravity_traps = False
    update_revenge_targets = False
    update_team_score = False
    enemy_sighted = False
    for event in events:
        actor = arena.vehicles.get(event.actor)
        own = event.actor == player.vehicle_id
        client_actor = event.actor if event.client_actor is None else event.client_actor
        client_target = event.target if event.client_target is None else event.client_target
        d = event.data
        reflector_hit = (d.get('reflector_hit') and actor is not None
                         and not mad_games.concealed(actor, player.team)
                         and not mad_games.concealed(arena.vehicles[event.target], player.team))
        if (event.kind in ('ready','reload','pump_reload') and not own
                and actor is not None and _can_view_reload(arena, player, actor)):
            allied_reload.add(event.actor)
        if event.kind == 'time_stop_begin':
            result += time_stop_message(1 if own else 2, d['position'],
                                        seconds=d['seconds'], energy=d['energy'])
            if own:
                for trace in d['traces']:
                    result += projectile_trace_message(trace, shooter=vehicle_wire_id(arena, trace.shooter))
            else:
                # Special-mode HUD and effect controllers retain VehicleGameLogic
                # references. Move the same session entity out of the scene.
                result += time_stop_roster_message(event.actor)
                pose = d['parked']
                result += b'\x14'+struct.pack('<III6f', event.actor, arena.space_id, 0,
                                              *pose.position, pose.roll, pose.pitch, pose.yaw)
        elif event.kind == 'time_stop_rejected' and own and d['reason'] == 'active_equipment':
            result += time_stop_message(7, (0., 0., 0.))
        elif event.kind == 'time_stop_energy' and own:
            result += time_stop_message(5, (0., 0., 0.), energy=d['energy'])
        elif event.kind == 'time_stop_shot' and own:
            if d['shooting_count']:
                result += shooting_message(event.actor, d['shooting_count'])
            result += projectile_trace_message(d['shot'], shot_offset=BRANCH_SHOT_BASE)
        elif event.kind == 'time_stop_missile_move' and own:
            result += projectile_trace_message(d['trace'], shot_offset=BRANCH_SHOT_BASE)
        elif event.kind == 'time_stop_autoaim_lost' and own:
            result += avatar_method(0, b'\0')
        elif event.kind == 'time_stop_gun_state' and own:
            shell = d['shell']
            if d['selected_changed']:
                result += avatar_method(7, struct.pack('<BI', 0, shell))
            counts = d['counts']
            result += _ammo_rows(counts, shell, dict(counts)[shell], d['clip'])
            if d['remaining'] is not None:
                remaining = d['remaining']
                pump = d['pump']
                if pump:
                    result += _gun_reload_config(event.actor, pump)
                    result += b''.join(_pump_reload_parts(event.actor, pump[1], remaining,
                                                          d['refill'], d['clip']))
                else:
                    result += avatar_method(13, struct.pack('<IBf', event.actor,
                                                            4 if remaining else 0, remaining or d['total']))
        elif event.kind == 'time_stop_shot_end' and own:
            shot = BRANCH_SHOT_BASE+d['shot']
            if d['explosion'] is not None:
                shell, material, direction = d['explosion']
                result += avatar_method(27, struct.pack('<IIB6fB', shot, shell, material,
                                                        *d['point'], *direction, 0))
            result += stop_trace_message(shot, d['point'], guided=d['guided'])
        elif event.kind == 'time_stop_consumable' and own:
            result += consumable_message(d['compact_id'], d['state'], d['remaining'], d['total'])
        elif event.kind == 'time_stop_ability' and own and d['kind_name'] in ('shockwave', 'explosion'):
            result += provision_effect_message(client_actor, d['compact_id'])
        elif event.kind == 'time_stop_traps' and own:
            result += gravity_traps_message(d['traps'])
        elif event.kind == 'time_stop_modifiers' and own:
            result += state_modifiers_message(client_actor, d['modifiers'])
        elif event.kind == 'time_stop_end':
            result += time_stop_message(4 if d['aborted'] else 3, d['position'])
            if own:
                for trace in d['retired_traces']:
                    result += stop_trace_message(trace.shot, trace.position, guided=trace.guided)
                for trace in d['live_traces']:
                    result += projectile_trace_message(trace, shooter=vehicle_wire_id(arena, trace.shooter))
                changed_destructibles.update(d['missed_destructions'])
                result += b''.join(reconnect_message_parts(arena, player, refresh_period=False))
            else:
                result += time_stop_roster_message(0)
                result += module_state_messages(event.actor, *module_snapshot(arena, actor))
                result += state_modifiers_message(event.actor, arena.state_modifiers(actor))
                result += active_equipment_message(arena, actor)
            pose = actor.pose
            result += b'\x14'+struct.pack('<III6f', event.actor, arena.space_id, 0,
                                          *pose.position, pose.roll, pose.pitch, pose.yaw)
        elif event.kind == 'practice_retire':
            # Move the dead hull away before dropping its client entity. Its
            # replacement enters with a fresh ID and the same movement alias.
            result += b'\x14'+struct.pack('<III6f', client_actor, arena.space_id, 0,
                                          -4096., 0., -4096., 0., 0., 0.)
            result += variable16(12, struct.pack('<I', client_actor))
        elif event.kind == 'practice_spawn':
            entity = client_actor
            participants = participant_ids((v.entity_id,v.account_id) for v in arena.vehicles.values())
            info = vehicle_info(entity, battle_nickname(arena, actor), actor.team,
                                participants[actor.entity_id], actor.descriptor)
            if d['first']:
                result += arena_update(2, pb_blob(2, pb_blob(1, info)))
                alias = tuple(arena.vehicles).index(actor.entity_id)
                result += b''.join(vehicle_spawn_parts(arena, actor, alias))
                result += health_changed(entity, actor.health, 0)
            else:
                pose = chameleon_pose(actor.pose) if mad_games.concealed(actor, player.team) else actor.pose
                result += b'\x14'+struct.pack('<III6f',entity,arena.space_id,0,
                                               *pose.position,pose.roll,pose.pitch,pose.yaw)
                result += module_state_messages(entity, *module_snapshot(arena, actor))
                if own:
                    for extra in d['repaired']:
                        result += module_damage_message(entity, 19, extra)
                result += movable_armor_states_message(actor, entity_id=entity)
                if getattr(arena, 'boss_role_picker', False):
                    result += vehicle_property(entity,13,struct.pack('<h',actor.max_health))
                result += vehicle_property(entity,3,struct.pack('<h',actor.health))
                result += vehicle_property(entity,1,b'\1')
                # Refresh observers' health/appearance listeners after reviving the model.
                result += health_changed(entity, actor.health, 0)
                # Update the roster alive flag as well as the world entity.
                # Native equipment input consults the arena's player record.
                result += arena_update(11, pb_blob(10, pb_blob(1, info)))
                result += arena_update(20, pb_blob(6, pb_int(1, entity)))
                result += state_modifiers_message(entity, arena.state_modifiers(actor))
            if d.get('placed_by') == player.vehicle_id:
                result += sandbox_advertisement(arena, notice=TARGET_PLACED)
        elif event.kind == 'practice_clear':
            entity = client_actor
            result += b'\x14'+struct.pack('<III6f', entity, arena.space_id, 0, *d['position'], 0., 0., 0.)
            result += vehicle_property(entity, 3, b'\0\0')
            result += vehicle_property(entity, 1, b'\0')
            result += health_changed(entity, 0, 0)
            result += arena_update(6, pb_blob(6, pb_int(1, entity)+pb_int(2, 0)))
        elif event.kind == 'sandbox_restore_hp':
            result += vehicle_property(event.actor, 3, struct.pack('<h', d['health']))
            result += health_changed(event.actor, d['health'], 0)
            if own:
                result += sandbox_advertisement(arena, notice=HP_RESTORED)
        elif event.kind == 'practice_notice' and own:
            result += sandbox_advertisement(arena, notice=PRACTICE_NOTICE | (d['notice'] << 11))
        elif event.kind == 'practice_feedback' and own:
            from .sandbox import PRACTICE_FEEDBACK
            result += sandbox_advertisement(arena, notice=PRACTICE_FEEDBACK | (d['notice'] << 11))
        elif event.kind == 'practice_catalog_status' and own:
            result += sandbox_advertisement(arena,
                notice=TARGET_SELECTED if d['selected'] else TARGET_UNAVAILABLE)
        elif event.kind == 'sandbox_ai' and own:
            from .sandbox import TARGET_AI_CHANGED, TARGET_AI_MISSING
            result += sandbox_advertisement(arena, notice=TARGET_AI_CHANGED if d['count'] else TARGET_AI_MISSING)
        elif event.kind == 'sandbox_map_border' and own:
            result += sandbox_advertisement(arena, notice=d['notice'])
        elif event.kind == 'boss_score':
            result += b''.join(team_points_message(team, score) for team, score in enumerate(d['tickets'], 1))
        elif event.kind == 'boss_picker' and own:
            result += boss_picker_message(arena, d['state'])
        elif event.kind in ('boss_health', 'max_health'):
            result += vehicle_property(event.actor, 13, struct.pack('<h', d['max_health']))
            result += vehicle_property(event.actor, 3, struct.pack('<h', d['health']))
            result += health_changed(event.actor, d['health'], 0)
        elif event.kind == 'boss_lives' and own:
            result += boss_lives_message(d['lives'])
        elif event.kind in ('boss_role', 'boss_role_progress'):
            result += boss_role_message(event.actor, d['role'], d['level'], d['progress'])
            if own and event.kind == 'boss_role':
                # Avatar property 20 uses the same card array as cell creation.
                result += b'\x13' + variable8(158 + 20, boss_ability_cards(d['abilities']))
                for compact_id in reversed(d['previous']):
                    # Native equipment state 255 removes its indexed slot. State 0
                    # leaves its icon in the slot and only clears its activity.
                    result += consumable_message(compact_id, 255)
        elif event.kind=='ready' and own:
            boss = getattr(arena, 'big_boss', None)
            if boss is not None or getattr(arena, 'mad_games_mode', False):
                ability = boss is not None or mad_games.irradiator_state(actor) is not None
                result += sandbox_advertisement(arena, ability=ability)
            if boss is not None and not d.get('respawn'):
                result += b''.join(boss_role_messages(arena))
            allied_reload.update(v.entity_id for v in arena.vehicles.values()
                                 if _can_view_reload(arena, player, v))
            result += ammo_message(actor)
            if not d.get('respawn'):
                result += b''.join(provision_messages(actor))
            if (resurrection := getattr(arena, 'uprising', None)) is not None:
                result += boss_lives_message(resurrection.remaining(actor.entity_id))
                result += uprising_elixir_message(resurrection.elixir(actor.entity_id))
                if d.get('respawn'):
                    result += uprising_offers_message(())
                    if resurrection.has(actor.entity_id, uprising.ENEMY_RELOAD):
                        result += enemy_reload_visibility_message(True)
                result += uprising_respawn_time_message(0.)
            for state in actor.consumables:
                if state and state.spec is uprising.RESPAWN_CLOAK:
                    continue
                if state:
                    view = state.state_at(arena.simulation_time)
                    result += consumable_message(state.spec.compact_id, view.state, view.remaining, view.total)
                elif not d.get('respawn'):
                    # Preserve empty slot positions: CD0 increments the native
                    # consumable slot cursor at AvatarGameLogic+0x5b4.
                    result += consumable_message(0, 0)
            result += mad_games_passive_message(actor)
            if resurrection is not None:
                result += uprising_passive_message(resurrection, actor.entity_id)
                result += uprising_shot_upgrade_messages(resurrection, actor.entity_id, arena.simulation_time)
            result += avatar_method(7,struct.pack('<BI',0,actor.shell_id))+targeting_info(
                actor,d.get('yaw_rate'),d.get('pitch_rate'))
            full_factor = actor.reload_speed_factor * getattr(
                arena, 'applied_ammo_reload_factors', {}).get(actor.entity_id, 1.) * getattr(
                arena, 'applied_crew_reload_factors', {}).get(actor.entity_id, 1.)
            result += gun_reload_configuration(actor, full_reload_factor=full_factor)
            result += avatar_method(13,struct.pack('<IBf',actor.entity_id,0,actor.reload_total))
            result += marker_message(arena,actor)
        elif event.kind == 'training_reentry' and not own:
            # The observer keeps the same avatar. Replace the observed hull
            # in one reliable message so motion never targets a missing ID.
            entity = event.actor
            info = vehicle_info(entity, battle_nickname(arena, actor), actor.team,
                                actor.account_id, actor.descriptor, camouflage=actor.camouflage)
            result += variable16(12, struct.pack('<I', entity))
            result += arena_update(11, pb_blob(10, pb_blob(1, info)))
            alias = tuple(arena.vehicles).index(entity)
            pose = chameleon_pose(actor.pose) if mad_games.concealed(actor, player.team) else actor.pose
            result += b''.join(vehicle_spawn_parts(arena, actor, alias, pose=pose))
            result += arena_update(20, pb_blob(6, pb_int(1, entity)))
            result += state_modifiers_message(entity, arena.state_modifiers(actor))
            if getattr(arena, 'big_boss', None) is not None:
                boss = arena.big_boss
                result += boss_role_message(entity, boss.roles.get(entity), boss.levels.get(entity, 0),
                                            boss.role_progress(entity, actor.max_health))
        elif event.kind == 'destruction':
            changed_destructibles.add(d['key'])
        elif event.kind=='targeting' and own:
            result += targeting_info(actor,d['yaw_rate'],d.get('pitch_rate'))
        elif event.kind=='autoaim_lost' and own:
            # Avatar client method 0, decoder 0x177dcec: one bool byte.
            result += avatar_method(0,b'\0')
        elif event.kind in ('auto_recovered','orientation_rebased','sandbox_teleport') and actor is not None:
            if mad_games.concealed(actor, player.team):
                continue
            # forcedPosition snaps viewers to an instantaneous pose change.
            result += b'\x14'+struct.pack('<III6f',client_actor,arena.space_id,0,
                *d['position'],d['roll'],d['pitch'],d['yaw'])
            if own and event.kind == 'sandbox_teleport':
                result += sandbox_advertisement(arena, notice=TELEPORTED)
        elif event.kind=='period':
            if d['period'] == 4 and getattr(arena, 'boss_role_picker', False):
                result += boss_picker_message(arena, 0)
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
                result += shooting_message(client_actor,count)
            if d.get('guided'):
                result += missile_message(client_actor,d['shot'],d['effect_index'],d['origin'],d['velocity'])
            else:
                result += avatar_method(29,struct.pack('<IIB7f',client_actor,d['shot'],d.get('effect_index',0),*d['origin'],*d['velocity'],d['gravity']))
        elif event.kind=='missile_move':
            result += missile_message(client_actor,d['shot'],d['effect_index'],d['origin'],d['velocity'])
        elif event.kind=='ammo' and own:
            result += ammo_message(actor)
        elif event.kind=='shell_selected' and own:
            result += avatar_method(7,struct.pack('<BI',0,d['shell']))
        elif event.kind=='reload' and own:
            full_factor = actor.reload_speed_factor * getattr(
                arena, 'applied_ammo_reload_factors', {}).get(actor.entity_id, 1.) * getattr(
                arena, 'applied_crew_reload_factors', {}).get(actor.entity_id, 1.)
            if d['delay'] and gun_reload_configuration(actor, full_reload_factor=full_factor):
                # Native 6->4 does not clear the pump flag or partial progress;
                # a known 0->4 does. Send state0 (nominal interval B) first.
                result += avatar_method(13,struct.pack('<IBf',event.actor,0,
                    reload_limit(actor) if actor.sandbox_fast_reload else
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
            elif d['readyGun']:
                state,value = 0,0.
                nominal = (reload_limit(actor) if actor.sandbox_fast_reload else
                           actor.definition.weapon.interval/actor.reload_speed_factor*actor.interval_factor)
            else:
                continue
            result += config
            result += avatar_method(13,struct.pack('<IBf',event.actor,state,nominal))
            result += arena_update(15,pb_blob(14,pb_blob(1,pb_int(1,event.actor)+pb_int(2,state)
                +varint((3<<3)|5)+struct.pack('<f',value)+pb_int(4,d['clip']))))
        elif event.kind == 'uprising_elixir' and own:
            result += uprising_elixir_message(d['balance'])
        elif event.kind == 'uprising_clock' and own:
            result += uprising_respawn_time_message(d['seconds'])
        elif event.kind == 'uprising_offers' and own:
            result += uprising_offers_message(d['offers'])
        elif event.kind == 'uprising_shot_upgrade':
            if own:
                seconds = math.ceil(d['cooldown'])
                result += consumable_message(d['compact_id'], COOLDOWN, seconds, seconds)
            if d['compact_id'] == uprising.INFERNO.compact_id:
                # A nonzero vehicle ID overrides the point with that vehicle's
                # center in showSplash. Zero leaves the world point intact.
                result += avatar_method(52, struct.pack('<I3f', 0, *d['point']))
        elif event.kind == 'uprising_shot_upgrade_ready' and own:
            result += consumable_message(d['compact_id'], READY)
        elif event.kind == 'uprising_shot_upgrade_cooldown' and own:
            result += consumable_message(d['compact_id'], COOLDOWN,
                                         math.ceil(d['remaining']), math.ceil(d['total']))
        elif event.kind == 'uprising_lightning_fx':
            # Avatar51 dispatches showLightning(vehicle ID, tracer ID, start, end).
            start = arena.vehicles.get(d['start_entity'])
            target = arena.vehicles.get(event.target)
            if start and target and not (mad_games.concealed(start, player.team)
                                         or mad_games.concealed(target, player.team)):
                result += avatar_method(51, struct.pack('<II6f', start.entity_id, d['arc'],
                                                      *d['start'], *d['end']))
        elif event.kind == 'uprising_respawn':
            if own:
                result += uprising_offers_message(d['offers'])
                result += uprising_respawn_time_message(d['seconds'])
            result += uprising_wait_respawn_message(event.actor, event.target)
        elif event.kind == 'mad_observation_warning' and own:
            result += observed_status_message(event.actor, d['observed'])
            if d['observed']:
                result += crew_voice_message(SOUND_PLAYER_SIGHTED)
        elif event.kind == 'mad_cloak':
            if actor.team != player.team:
                viewer = arena.vehicles[player.vehicle_id]
                update_revenge_targets |= (event.actor, actor.generation) in viewer.mad_revenge
                pose = chameleon_pose(d['pose']) if d['enabled'] else d['pose']
                result += b'\x14' + struct.pack('<III6f', event.actor, arena.space_id, 0,
                                                *pose.position, pose.roll, pose.pitch, pose.yaw)
                enemy_sighted |= not d['enabled'] and actor.alive
                if not d['enabled'] and _can_view_reload(arena, player, actor):
                    allied_reload.add(event.actor)
        elif event.kind == 'uprising_cloak_fx' and actor.team == player.team:
            result += provision_effect_message(event.actor, d['compact_id'], argument=int(d['active']))
            if d['compact_id'] == uprising.RESPAWN_CLOAK.compact_id:
                spec = uprising.RESPAWN_CLOAK
                status = ACTIVE if d['active'] else COOLDOWN
                result += active_equipment_message(arena, actor,
                    ((spec.ident, status, d['at'], spec.duration),), entity_id=client_actor)
        elif event.kind == 'mad_revenge_targets' and own:
            update_revenge_targets = True
        elif event.kind == 'mad_reflector_fx':
            if actor.team == player.team or d['spotted']:
                result += provision_effect_message(
                    event.actor, mad_games.REFLECTOR.compact_id, argument=int(not d['active']))
        elif event.kind == 'mad_chameleon_fx':
            if actor.team == player.team or d['spotted']:
                # MadgamesMoonwalkEffectHandler consumes argument 1 on
                # activation and 0 on exit, using the pinned client assets.
                result += provision_effect_message(
                    event.actor, mad_games.CHAMELEON.compact_id, argument=int(d['active']))
        elif event.kind == 'mad_regenerator_fx':
            healer = arena.vehicles[event.target]
            if not (mad_games.concealed(actor, player.team) or mad_games.concealed(healer, player.team)):
                # The pinned LifestealHeal handler uses zero for self-healing
                # and the healer's entity ID for a heal on another vehicle.
                result += provision_effect_message(event.actor, mad_games.REGENERATOR_EFFECT,
                                                   argument=0 if actor is healer else healer.entity_id)
        elif event.kind=='consumable_state':
            if own:
                result += consumable_message(d['compact_id'], d['state'], d['remaining'], d['total'])
            equipment = d['equipment']
            active = tuple(row for row in equipment if row[1] == ACTIVE)
            stopped = tuple(row for row in equipment if row[0] == d['ident'] and row[1] != ACTIVE)
            if stopped:
                result += active_equipment_message(arena, actor, stopped, entity_id=client_actor)
            if stopped or any(row[0] == d['ident'] for row in active):
                result += active_equipment_message(arena, actor, active, entity_id=client_actor)
        elif event.kind=='module_state':
            # Immutable snapshots from the event, not the live vehicle state.
            result += module_state_messages(client_actor, d['critical'], d['destroyed'])
            if own:
                result += module_damage_message(event.actor, d['reason'], d['extra'])
                if d.get('track_just_broke'):
                    result += crew_voice_message(VOICE_TRACK_DESTROYED)
        elif event.kind=='module_repair' and own:
            result += module_repair_message(event.actor, d['extra'], d['progress'], d['duration'])
        elif event.kind=='state_modifiers':
            result += state_modifiers_message(client_actor, d['modifiers'])
        elif event.kind=='armor_states':
            # Arena stores a complete immutable snapshot on the event.  Every
            # observer needs it because property16 controls public geometry.
            result += movable_armor_states_message(actor, d['states'], entity_id=client_actor)
        elif (event.kind=='provision_effect' and not (own and d.get('skip_owner'))
              and (not d.get('allies_only') or actor.team == player.team)):
            result += provision_effect_message(client_actor, d['compact_id'])
        elif event.kind=='boss_blast':
            result += provision_effect_message(event.actor, big_boss.BLAST_WAVE.compact_id)
        elif event.kind == 'ram_detonated':
            result += ammo_bay_explosion_message(client_target, client_actor)
        elif event.kind == 'ram_shockwave':
            result += provision_effect_message(client_actor, big_boss.BLAST_WAVE.compact_id)
        elif event.kind == 'ram_turret_created':
            turret = d['turret']
            result += detached_turret_message(turret, client_actor, turret.launched_at)
        elif event.kind == 'ram_turret_restored':
            result += restore_turret_message(client_actor)
        elif event.kind == 'ram_turret_removed':
            result += variable16(12, struct.pack('<I', d['turret_id']))
        elif event.kind in ('bomber_blast', 'boss_role_effect'):
            result += provision_effect_message(client_actor, d['compact_id'])
        elif event.kind in ('boss_vehicle_effects', 'mad_vehicle_effects', 'uprising_mark_fx'):
            result += vehicle_client_effects_message(arena, event.actor, d['effects'])
        elif event.kind == 'uprising_play_dead_fx' and actor.team != player.team:
            # Only the observer's model and HP marker change. Arena kill,
            # respawn and scoreboard messages still use the real tank state.
            health = 0 if d['active'] else d['health']
            result += vehicle_property(event.actor, 3, struct.pack('<h', health))
            result += vehicle_property(event.actor, 1, bytes([health > 0]))
            # Both edges must notify the native health listener. Calling
            # it only on revival leaves the enemy HP marker in its dead state.
            # Attacker zero gives the enemy observer the red damage marker
            # without crediting the visual hit to that player.
            result += health_changed(event.actor, health, 0)
            if not d['active'] and _can_view_reload(arena, player, actor):
                allied_reload.add(event.actor)
        elif event.kind == 'uprising_curse_fx':
            if not d['active'] or not mad_games.concealed(actor, player.team):
                # CurseEffectHandler (0x1db2d50) starts with argument zero.
                result += provision_effect_message(event.actor, uprising.CURSE.compact_id,
                                                   argument=int(not d['active']))
        elif event.kind in ('gravitrap', 'gravitrap_expired', 'gravitrap_visual_change'):
            update_gravity_traps = True
        elif event.kind=='explosion' and not (own and d.get('skip_owner')):
            # Avatar27 / decoder0x177eab3: shot, shell CD, material, position,
            # shot direction (not surface normal), unused byte. Keep stopTracer.
            result += avatar_method(27,struct.pack('<IIB6fB',d['shot'],d['shell_id'],d['material'],
                *d['point'],*d['direction'],0))
        elif event.kind in ('impact','shot_end','splash'):
            if event.kind != 'splash':
                result += stop_trace_message(d['shot'], d['point'], guided=d.get('guided', False))
            # HP hits are reported once, immediately before their health
            # update below. Blocked/external hits have no health event but
            # still need the native hit/ricochet screen reaction.
            if (event.kind != 'shot_end' and (event.target == player.vehicle_id or reflector_hit)
                    and event.target in arena.vehicles and actor is not None
                    and event.actor != event.target and not d.get('applied_damage')
                    and d['result'].hit is not None):
                outcome = d['result'].outcome
                blocked_damage = d.get('blocked_damage', 0)
                # The incoming HUD shows armor-block amounts for kind 1.
                if event.target == player.vehicle_id and (track_blocked := d.get('track_blocked_damage')):
                    outcome = 'not_penetrated'
                    blocked_damage = track_blocked
                result += received_damage_message(client_target,client_actor,0,
                    outcome=outcome,blocked_damage=min(0xffff, blocked_damage),
                    hit_effect=d.get('hit_effect'), reflector_hit=reflector_hit)
        elif event.kind=='feedback' and own:
            result += shot_feedback(client_target,d['outcome'])
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
            if (event.target == player.vehicle_id or reflector_hit) and d.get('received_damage'):
                result += received_damage_message(client_target, client_actor, d['received_damage'],
                                                   hit_effect=d.get('hit_effect'), reflector_hit=reflector_hit)
            result += vehicle_property(client_target,3,struct.pack('<h',d['health']))
            healed = d.get('healed')
            result += health_changed(client_target, d['health'], client_actor,
                healing_marker=bool(healed and event.target != player.vehicle_id
                                    and arena.vehicles[event.target].team == player.team))
            if healed and (own or event.target == player.vehicle_id):
                result += b'\x13' + variable8(96, b'\x03TFS1HEAL' + struct.pack('<I', healed) + b'\0')
        elif event.kind=='killed':
            victim = arena.vehicles.get(event.target)
            if own and actor is not None and victim is not None and victim.team != actor.team:
                result += crew_voice_message(VOICE_ENEMY_KILLED)
            result += vehicle_property(client_target,1,b'\0')
            # The vehicle still dies in the world. Keep its roster entry active
            # while it waits for the next life and update kills independently.
            if not d.get('keep_roster'):
                result += arena_update(6,pb_blob(6,pb_int(1,client_target)+pb_int(2,client_actor)))
            if actor is not None:
                result += arena_update(5,pb_blob(5,pb_blob(1,pb_int(1,client_actor)+pb_int(2,actor.kills))))
                update_team_score = update_team_score or (
                    getattr(arena, 'unlimited_lives', False) and arena.supremacy is None
                    and getattr(arena, 'big_boss', None) is None)
    if enemy_sighted:
        result += crew_voice_message(SOUND_ENEMY_SIGHTED)
    if update_revenge_targets:
        result += revenge_targets_message(arena, arena.vehicles[player.vehicle_id])
    if update_team_score:
        result += team_kill_score(arena, player)
    if update_gravity_traps:
        result += gravity_traps_message(public_gravity_traps(arena))
    # Reload and pump edges can share one step. Send one final authoritative
    # readiness per ally so a pump refill cannot overwrite an intershot gate.
    for entity in sorted(allied_reload):
        result += allied_reload_message(arena,player,arena.vehicles[entity])
    if changed_destructibles:
        state = destructible_state(arena)
        assets = arena.world.assets.destructibles
        state.dirty = {(key >> 16, 3 if assets[key].kind == 8 else 2 if assets[key].kind == 5 else 1)
                       for key in changed_destructibles}
        result += state.updates()
    return bytes(result)


def _pump_reload_parts(entity, nominal, remaining, refill, clip):
    if refill is not None:
        return (avatar_method(13, struct.pack('<IBf', entity, 6, refill)),
                arena_update(15, pb_blob(14, pb_blob(1, pb_int(1, entity)
                    + pb_int(2, 6) + varint((3 << 3) | 5) + struct.pack('<f', refill)
                    + pb_int(4, clip)))))
    ready = avatar_method(13, struct.pack('<IBf', entity, 0, nominal))
    if remaining:
        return ready, avatar_method(13, struct.pack('<IBf', entity, 4, remaining))
    return (ready,)


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
        nominal = (reload_limit(vehicle) if vehicle.sandbox_fast_reload else
                   vehicle.definition.weapon.interval / vehicle.reload_speed_factor * vehicle.interval_factor)
        refill = max(0., vehicle.pump_at-now) if vehicle.pump_at else None
        return (config, *_pump_reload_parts(vehicle.entity_id, nominal, remaining, refill, vehicle.clip))
    # A normal ready gun deliberately receives state0 plus its nominal next
    # reload value, matching the existing ready/reload path.
    return (config,
            avatar_method(13, struct.pack('<IBf', vehicle.entity_id,
                                           4 if remaining else 0,
                                           remaining or vehicle.reload_total)))


def reconnect_message_parts(arena, player, *, refresh_period=True):
    """Authoritative owner/private state that is not retained by entity spawn."""
    own = arena.vehicles[player.vehicle_id]
    stats = b''.join(pb_blob(1, pb_int(1, vehicle_wire_id(arena, v.entity_id))+pb_int(2, v.kills))
                     for _,v in present_vehicles(arena, player))
    parts = [arena_update(4, pb_blob(4, stats)),
             *base_capture_messages(arena), *supremacy_messages(arena, player),
             ammo_message(own), *provision_messages(own), b'\x13\xa8'+struct.pack('<I',own.damage),
             b'\x13\xa9'+struct.pack('<I',own.blocked_damage),
             b'\x13\xaa'+struct.pack('<I',own.assisted_damage)]
    if getattr(arena, 'mad_games_mode', False) and own.mad_revenge:
        parts.append(revenge_targets_message(arena, own))
    if (resurrection := getattr(arena, 'uprising', None)) is not None:
        parts.append(uprising_elixir_message(resurrection.elixir(own.entity_id)))
        time_left = max(0., arena.respawn_at.get(own.entity_id, 0.) - arena.simulation_time)
        if time_left and not arena.finished_at:
            parts.append(uprising_offers_message(resurrection.offers(own.entity_id)))
        parts.append(uprising_respawn_time_message(time_left))
    timeline = getattr(arena, 'time_stop', None)
    if timeline is not None and timeline.entity is not None and timeline.account != player.account_id:
        parts.append(time_stop_roster_message(timeline.entity))
        if (clock := timeline.peer_clock(player.account_id)) is not None:
            position, remaining = clock
            parts.append(time_stop_message(6, position, seconds=remaining))
    boss = getattr(arena, 'big_boss', None)
    if boss is not None:
        parts.extend(team_points_message(team, score) for team, score in enumerate(boss.tickets, 1))
        parts.extend(boss_role_messages(arena))
        parts.append(gravity_traps_message(public_gravity_traps(arena)))
        if player.vehicle_id in boss.bosses:
            parts.append(boss_lives_message(boss.lives[player.vehicle_id]))
    elif getattr(arena, 'unlimited_lives', False) and arena.supremacy is None:
        parts.append(team_kill_score(arena, player))
    battle_seconds = 0 if getattr(arena, 'endless', False) else arena.battle_seconds
    if refresh_period and arena.finished_at:
        parts.append(period_message(4, max(0., arena.finished_at+arena.POSTBATTLE_SECONDS-arena.simulation_time),
                                    arena.winner, battle_seconds, finish_reason=arena.finish_reason))
    elif refresh_period and arena.started and getattr(arena, 'endless', False):
        parts.append(period_message(3, 0, battle_seconds=0))
    elif refresh_period and arena.started:
        ends = getattr(arena, 'practice_timer_at', 0.) or arena.started+arena.battle_seconds
        parts.append(period_message(3, max(0., ends-arena.simulation_time),
                                    battle_seconds=battle_seconds))
    elif refresh_period and getattr(arena, 'prebattle_end', 0.):
        parts.append(period_message(2, max(0., arena.prebattle_end-arena.simulation_time),
                                    battle_seconds=battle_seconds))
    if getattr(arena, 'boss_role_picker', False) and not arena.finished_at:
        entity = player.vehicle_id
        picking = ((not arena.started and arena.prebattle_end and entity not in arena.boss_first_picks)
                   or (entity in arena.respawn_at and entity not in arena.boss_pending_roles))
        parts.append(boss_picker_message(arena, 2 if picking else 1))
    for state in own.consumables:
        if state and state.spec is uprising.RESPAWN_CLOAK:
            continue
        if state:
            view = state.state_at(arena.simulation_time)
            parts.append(consumable_message(state.spec.compact_id, view.state, view.remaining, view.total))
        else:
            parts.append(consumable_message(0, 0))
    if passive := mad_games_passive_message(own):
        parts.append(passive)
    if resurrection is not None:
        parts.append(uprising_passive_message(resurrection, own.entity_id))
        parts.append(uprising_shot_upgrade_messages(resurrection, own.entity_id, arena.simulation_time))
        if resurrection.has(own.entity_id, uprising.ENEMY_RELOAD):
            parts.append(enemy_reload_visibility_message(True))
    parts.extend((avatar_method(7, struct.pack('<BI', 0, own.shell_id)),
                  targeting_info(own), *_reconnect_reload_parts(arena, own)))
    parts.extend(_allied_reload_parts(arena, player))
    # Creation does carry these state vectors, but repeat full snapshots after
    # the new Avatar owns its vehicle.  This also covers client-side reset of
    # damaged-module visuals during a BaseApp replacement.
    for _, vehicle in present_vehicles(arena, player):
        entity = vehicle_wire_id(arena, vehicle.entity_id)
        health = _observer_health(arena, player, vehicle)
        parts.append(vehicle_property(entity, 1, bytes([vehicle.alive and health > 0])))
        parts.append(vehicle_property(entity, 3, struct.pack('<h', health)))
        critical, destroyed = module_snapshot(arena, vehicle)
        parts.append(module_state_messages(entity, critical, destroyed))
        parts.append(state_modifiers_message(entity,
                     getattr(arena, 'state_modifiers', lambda _: ())(vehicle)))
        if vehicle is own:
            # A reconnect creates new TrackHealth extras; let property7
            # register them before the owner-only Avatar19 lookup by index.
            for extra,progress,duration in getattr(
                    arena, 'track_repair_status', lambda _: ())(vehicle):
                parts.append(module_repair_message(vehicle.entity_id, extra, progress, duration))
        parts.append(active_equipment_message(arena, vehicle))
        if resurrection is not None and vehicle.team == player.team and vehicle.mad_cloaked:
            cloak = uprising.active_cloak(vehicle, arena.simulation_time)
            if cloak is not None:
                parts.append(provision_effect_message(vehicle.entity_id, cloak.compact_id, argument=1))
            if cloak is uprising.RESPAWN_CLOAK:
                state = next(slot for slot in vehicle.consumables if slot and slot.spec is cloak)
                parts.append(active_equipment_message(arena, vehicle,
                    ((cloak.ident, ACTIVE, state.activated_at, cloak.duration),)))
        armor_state = movable_armor_states_message(vehicle, entity_id=entity)
        if armor_state:
            parts.append(armor_state)
    if resurrection is not None and time_left and not arena.finished_at:
        parts.append(uprising_wait_respawn_message(own.entity_id, own.killer))
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
