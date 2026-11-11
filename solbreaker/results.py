"""Frozen arena statistics and the pinned native battle-results resource.

Fetching and serializing a report never award credits. The simulator owns the
counters; this adapter snapshots them before the world is released.
"""
from dataclasses import asdict
import math
import struct
import zlib

from .account import account_method, pickle_document, variable16
from .battle import FINISH_REASONS, pb_int, pb_blob, participant_ids
from .mercury import packed_blob


def snapshot(arena, account_id, credits=None):
    if not arena.started or not arena.finished_at:
        raise ValueError('Results require a finished battle')
    player = arena.players[account_id]
    own_reward = arena.vehicle_for(account_id).battle_reward(arena.winner == player.team)
    if credits is not None and credits != own_reward.credits:
        raise ValueError('Explicit credits do not match the battle reward')
    credits = own_reward.credits
    vehicles = []
    for v in arena.vehicles.values():
        reward = v.battle_reward(arena.winner == v.team)
        vehicles.append(dict(entity_id=v.entity_id, account_id=v.account_id or 0,
            name=v.nickname, team=v.team, vehicle_type=int.from_bytes(v.descriptor[:2], 'little'),
            health=v.health, max_health=v.max_health, shots=v.shots, hits=v.hits,
            credits=reward.credits if v.account_id in arena.players else 0,
            economy=asdict(reward),
            damage=v.damage, kills=v.kills, direct_hits=v.direct_hits,
            piercings=v.piercings, damage_received=v.damage_received,
            hits_received=v.hits_received, piercings_received=v.piercings_received,
            killer=v.killer, mileage=int(v.distance),
            capture_points=getattr(v, 'capture_points', 0),
            defense_points=math.floor(getattr(v, 'defense_points', 0.) + 1e-8),
            life_time=max(0,int((v.death_at or arena.finished_at)-arena.started))))
        participant = arena.players.get(v.account_id)
        if participant is not None and participant.inventory_id is not None:
            vehicles[-1].update(xp=reward.vehicle_xp, free_xp=reward.free_xp)
    report = dict(unique_id=arena.unique_id, account_id=account_id,
        vehicle_id=player.vehicle_id, team=player.team,
        arena_type=getattr(arena, 'client_map_id', arena.map_id), bonus_type=2,
        created=int(arena.created_at), duration=max(0, int(arena.finished_at-arena.started)),
        winner=arena.winner, reason=arena.finish_reason, credits=credits, vehicles=vehicles)
    if player.inventory_id is not None:
        own = next(v for v in vehicles if v['entity_id']==player.vehicle_id)
        report.update(inventory_id=player.inventory_id, vehicle_xp=own['xp'], free_xp=own['free_xp'])
    return report


def _xp_components(v, xp):
    economy = v.get('economy')
    damage = economy.get('damage_xp') if economy is not None else min(xp, v['damage'])
    if type(damage) is not int or not 0 <= damage <= xp:
        raise ValueError('Invalid battle XP breakdown')
    if economy is None or 'base_xp' not in economy:
        return damage, xp-damage, 0, xp
    damage, activity, victory, base = (economy.get(key) for key in
        ('damage_base_xp','other_base_xp','victory_xp','base_xp'))
    if (any(type(value) is not int or value < 0 for value in (damage,activity,victory,base))
            or damage+activity != base or base+victory != xp):
        raise ValueError('Invalid battle XP breakdown')
    return damage, activity, victory, base


def _public(v, credits=0, *, participant_id):
    fields = {1:v['health'], 2:credits, 4:v['shots'], 5:v['direct_hits'],
        7:v['piercings'], 8:v['damage'], 11:v['damage_received'],
        12:v['hits_received'], 13:max(0,v['hits_received']-v['piercings_received']),
        15:v['piercings_received'], 18:v['kills'], 23:v.get('mileage',0),24:v.get('life_time',0),
        25:v['killer'], 101:participant_id, 102:v['team'],
        103:v['vehicle_type'], 125:v['max_health']}
    if v.get('capture_points'):
        fields[21] = v['capture_points']
    if v.get('defense_points'):
        fields[22] = v['defense_points']
    if 'xp' in v:
        fields.update({3:v['xp'], 124:v['xp']})
        economy = v.get('economy')
        if economy is not None and 'base_xp' in economy:
            damage, activity, victory, base = _xp_components(v, v['xp'])
            # Native economics.pureXp reads battleXP (124); xp (3) is the
            # awarded total. Verified separately from the FinalXp breakdown.
            fields.update({29:damage, 31:victory, 36:activity, 124:base})
    # Gun mark progression is unavailable in this training sandbox (status 5).
    return b''.join(pb_int(k, x) for k, x in fields.items()) + pb_blob(122,pb_int(5,5))


def protobuf(report):
    own = next(v for v in report['vehicles'] if v['entity_id']==report['vehicle_id'])
    participants = participant_ids((v['entity_id'],v['account_id']) for v in report['vehicles'])
    # Pinned FinishReason reflection registration at0x706868/0x70687b.
    reason = FINISH_REASONS.get(report['reason'],1)
    data = b''.join(pb_int(k,v) for k,v in {1:report['arena_type'],
        2:report['created'], 3:report['winner'], 4:reason,
        5:report['duration'], 6:report['bonus_type']}.items())
    # Fields from the pinned BattleResultsClient.proto / BattleResultsFinalXp.
    final_xp = b''
    if 'vehicle_xp' in report:
        xp, free = report['vehicle_xp'], report['free_xp']
        damage_xp, activity_xp, victory_xp, no_modifiers = _xp_components(own, xp)
        fields = {1:damage_xp, 2:damage_xp, 5:activity_xp, 6:activity_xp,
                  7:victory_xp, 8:victory_xp,
                  13:free, 14:free, 17:no_modifiers, 18:xp, 19:xp, 20:free, 21:free}
        final_xp = b''.join(pb_int(k,v) for k,v in fields.items())
        data += pb_int(137,free)+pb_int(181,xp)+pb_int(182,free)
    data += pb_blob(8, _public(own, report['credits'],
                             participant_id=participants[own['entity_id']])) + pb_blob(11, final_xp)
    # Dossier v3 validates u16 version 37 and parses protobuf after four prefix
    # bytes. Missing strings make the native parser subtract 4 from zero.
    # Dossier progress is outside the current sandbox economy.
    update = b''.join(pb_int(k,10) for k in (13,14,15,16,17))
    update += b''.join(pb_blob(k,b'\x25\0\0\0') for k in (20,21,22,23))
    if 'auto_load_cost' in report:
        cost = report['auto_load_cost']
        if (not isinstance(cost, (list, tuple)) or len(cost) != 2
                or any(type(value) is not int or not 0 <= value < 2**31 for value in cost)):
            raise ValueError('Invalid committed ammunition cost')
        # BattleResultsUpdate.autoLoadCost: credits, gold. The client derives
        # its net total from these actual post-battle charges.
        update += b''.join(pb_int(8, value) for value in cost)
    data += pb_blob(150,update) + pb_int(183,report['credits'])
    for v in report['vehicles']:
        info = pb_blob(1,v['name'].encode()) + pb_int(3,v['team'])
        participant = participants[v['entity_id']]
        data += pb_blob(201,pb_int(1,participant) + pb_blob(2,info))
        credits = v['credits']
        data += pb_blob(301,pb_int(1,v['entity_id'])
                        + pb_blob(2,_public(v,credits,participant_id=participant)))
    return data


def document(report):
    return report['unique_id'], protobuf(report)


def account_notification(report, created):
    own = next(v for v in report['vehicles'] if v['entity_id']==report['vehicle_id'])
    # The native short-result enum is 0=win, 1=defeat, 2=draw; not a bool.
    outcome = 2 if report['winner']==0 else int(report['winner']!=report['team'])
    short = dict(arenaUniqueID=report['unique_id'],arenaTypeID=report['arena_type'],
        serializedVehType=own['vehicle_type'],arenaCreateTime=report['created'],
        isWinner=outcome,credits=report['credits'],xp=report.get('vehicle_xp',0))
    message = dict(active=True,importance=1,type=2,id=report['unique_id'],
                   created_at=int(created),data=short)
    # Account method 37, generated ChatCommand reader at 0x177c050.
    # Opcode 21 is a pushed system message; both packed strings are mandatory.
    body = (struct.pack('<QBBddIQ',0,21,0,created,created,0,0)
            + packed_blob(b'') + b'\0' + packed_blob(pickle_document(message)) + b'\0')
    return account_method(37,body)


def avatar_resource(report):
    # Avatar subscribes to resource type 100 before battle. This uses the
    # RES_STREAM/RES_DATA transport independently of entity method responses.
    content = zlib.compress(pickle_document(document(report)))
    if len(content)>60000:
        raise ValueError('Battle report exceeds the resource limit')
    return (variable16(52,struct.pack('<H',100)+packed_blob(b''))
        + variable16(53,struct.pack('<HBB',100,0,1)+content))
