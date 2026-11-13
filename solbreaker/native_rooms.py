"""Training Rooms wire adapter for the pinned compatible build 11.21 client.

RoomManager owns membership and authority; this module encodes Account RPCs.
See docs/native-training-rooms.md for the recovered layouts and evidence.
"""
from fnmatch import fnmatchcase
import re
import sqlite3
import struct
import time

from .account import account_method, command_response, pickle_document, resource_response
from .battle_settings_wire import ROOM_FORM_PREFIX, ROOM_OPTIONS_PREFIX
from .mercury import packed_blob
from .map_catalog import map_team_capacity
from .roster import TEAMS
from .training_config import BATTLE_MODES, RoomBattleSettings

# ClientPrebattle::IsTrainingCreator (0x171cd40) checks the entire mask.
OWNER_ROLE = 0x27e7f
NO_RESPONSE = 202
COMMANDS = {400,401,402,403,404,406,411,414,415,523,524}
BOT_PREFIX = re.compile(r'^\[bots\s+([0-7])\s*:\s*([0-7])\]', re.IGNORECASE)
SETTING_PREFIX = re.compile(r'^\[(mode|lives|time)\b', re.IGNORECASE)
SETTING_VALUE = re.compile(r'^\[(?:mode|lives|time)\s+([a-z]+|[0-9]{1,2})\]', re.IGNORECASE)


def varint(value):
    if not 0 <= value <= 0xffffffffffffffff:
        raise ValueError('Unsigned protobuf integer out of range')
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def field(number,value):
    if isinstance(value,str):value=value.encode()
    if isinstance(value,bytes):return varint(number*8+2)+varint(len(value))+value
    return varint(number*8)+varint(value)


def update(opcode,data):
    return account_method(39,bytes([opcode])+packed_blob(data))


def read_blob(body,offset):
    if offset>=len(body):raise ValueError('Missing room string')
    size=body[offset];offset+=1
    if size==255:
        if offset+3>len(body):raise ValueError('Truncated room string length')
        size=int.from_bytes(body[offset:offset+3],'little');offset+=3
    end=offset+size
    if end>len(body):raise ValueError('Truncated room string')
    return body[offset:end],end


def room_text(title,description):
    title=title.decode('utf-8').strip();description=description.decode('utf-8')
    if not 1<=len(title)<=64 or len(description)>512 or '\0' in title+description:
        raise ValueError('Invalid room text length or NUL character')
    return title,description


def room_bot_options(description):
    """Read the optional counts at the start of the stock description field."""
    text = description.lstrip()
    if not re.match(r'^\[bots\b', text, re.IGNORECASE):
        return description, (0, 0)
    match = BOT_PREFIX.match(text)
    if match is None:
        raise ValueError('Use [bots 2:3] for Team 1 and Team 2, with 0 to 7 per team')
    return text[match.end():].lstrip(), tuple(map(int, match.groups()))


def room_comment(room, *, include_rules=True):
    first, second = room.bot_counts
    options = []
    if include_rules and room.battle_mode != 'encounter':
        options.append(f'[mode {room.battle_mode}]')
    if include_rules and room.battle_settings.unlimited_lives:
        options.append('[lives unlimited]')
    minutes = room.battle_settings.minutes
    if include_rules and minutes is not None:
        options.append(f'[time {minutes or "endless"}]')
    if include_rules or first or second:
        options.append(f'[bots {first}:{second}]')
    if include_rules:
        options.append('[notes]')
    return ' '.join(filter(None, (*options, room.description)))


def room_options(description, previous=None):
    values = {}
    text = description.lstrip()
    controls = text.startswith(ROOM_OPTIONS_PREFIX)
    full_form = text.startswith(ROOM_FORM_PREFIX)
    for marker in (ROOM_OPTIONS_PREFIX, ROOM_FORM_PREFIX):
        if text.startswith(marker):
            text = description = text.removeprefix(marker).lstrip()
            break
    existing = previous if controls else None
    while prefix := SETTING_PREFIX.match(text):
        match = SETTING_VALUE.match(text)
        key = prefix[1].lower()
        if match is None:
            raise ValueError(f'Invalid room setting: {key}')
        values.setdefault(key, match[1].lower())
        text = description = text[match.end():].lstrip()
    mode = values.get('mode', existing.battle_mode if existing else None)
    if mode is not None and mode not in BATTLE_MODES:
        raise ValueError('Choose a supported room battle mode')
    lives = values.get('lives', 'unlimited' if existing and existing.battle_settings.unlimited_lives
                       else 'single')
    if lives not in ('single', 'unlimited'):
        raise ValueError('Choose single or unlimited lives')
    duration = values.get('time')
    if duration is None:
        minutes = existing.battle_settings.minutes if existing else None
    elif duration == 'default':
        minutes = None
    elif duration == 'endless':
        minutes = 0
    elif duration.isdecimal() and int(duration) > 0:
        minutes = int(duration)
    else:
        raise ValueError('Choose a duration in minutes or an endless battle')
    # The current form submits the roster before an explicit notes boundary.
    # Older clients can still supply the bot prefix in their description.
    bot_prefix = BOT_PREFIX.match(text)
    if full_form and bot_prefix and text[bot_prefix.end():].lstrip().startswith('[notes]'):
        description = text[bot_prefix.end():].lstrip().removeprefix('[notes]').removeprefix(' ')
        bots = tuple(map(int, bot_prefix.groups()))
        return description, bots, mode, RoomBattleSettings(lives == 'unlimited', minutes)
    if existing and not description:
        description, bots = existing.description, existing.bot_counts
    else:
        description, bots = room_bot_options(description)
    return description, bots, mode, RoomBattleSettings(lives == 'unlimited', minutes)


def create_request(body,*,with_vehicle=False,with_map=False,with_mode=False,configured_map_id=6,
                   available_map_ids=None):
    # Generated Account base method 15 writer 0x1779ac0: IIHHssBBI.
    if len(body)<20:raise ValueError('Truncated training room request')
    if type(configured_map_id) is not int or not 1<=configured_map_id<=0xffff:
        raise ValueError('Invalid configured map ID')
    map_selector,command,battle_type,selection_mask=struct.unpack_from('<IIHH',body)
    title,offset=read_blob(body,12);description,offset=read_blob(body,offset)
    if len(body)-offset!=6:raise ValueError('Invalid training room request size')
    teamkill,opened,vehicle=struct.unpack_from('<BBI',body,offset)
    if command!=420:raise ValueError('Unsupported room creation operation')
    if map_selector==0xffffffff:
        map_id=0
    elif map_selector>>16==0xffff and 0<(map_selector&0xffff)<=0xffff:
        map_id=map_selector&0xffff
    else:
        raise ValueError('Invalid room map selector')
    allowed = tuple(available_map_ids) if available_map_ids is not None else (configured_map_id,)
    if map_id != 0 and map_id not in allowed:
        raise ValueError('Room map is not available in this lab')
    from .battle_settings_wire import TITLE, GET_TITLE
    settings_get = title == GET_TITLE.encode()
    bot_setup = title == TITLE.encode()
    invalid_options = ((battle_type not in (0, 1) or teamkill not in (0, 1)) if bot_setup
                       else (battle_type not in (0, 1, 2) or bool(teamkill)))
    valid_vehicle = vehicle == 0 if settings_get else (vehicle >= 1 if with_vehicle else vehicle == 1)
    if (invalid_options or selection_mask!=0x3ff or opened!=1 or not valid_vehicle
            or (settings_get and (map_id != 0 or description))):
        raise ValueError('Use default room options and a valid inventory vehicle')
    text = room_text(title,description)
    if with_mode:
        text += (battle_type,)
    if with_vehicle and with_map:return (*text,vehicle,map_id)
    if with_vehicle:return (*text,vehicle)
    if with_map:return (*text,map_id)
    return text


def arguments(kind,body):
    """Account base method 5: qii; method 14: vector<int64>, vector<string>."""
    if kind==140 and len(body)==20:
        return struct.unpack_from('<qii',body,4),()
    if kind!=149 or len(body)<6:raise ValueError('Invalid room command envelope')
    count=body[4]
    if count>4:raise ValueError('Too many room arguments')
    offset=5+count*8
    if offset>=len(body):raise ValueError('Truncated room arguments')
    numbers=struct.unpack_from('<'+'q'*count,body,5)
    count=body[offset];offset+=1
    if count>2:raise ValueError('Too many room strings')
    strings=[]
    for _ in range(count):
        value,offset=read_blob(body,offset)
        if len(value)>2048:raise ValueError('Room string is too long')
        strings.append(value)
    if offset!=len(body):raise ValueError('Trailing room command bytes')
    return numbers,tuple(strings)


def room_id(value):
    if not 0<value<=0xffffff:raise ValueError('Invalid room ID')
    return f'{value:06X}'


class NativeRooms:
    def __init__(self,endpoint):self.endpoint=endpoint

    @property
    def map_id(self):
        value=getattr(getattr(self.endpoint,'map_spec',None),'map_id',6)
        if type(value) is not int or not 1<=value<=0xffff:
            raise ValueError('Invalid configured map ID')
        return value

    @property
    def map_ids(self):
        return tuple(self.endpoint.map_catalog)

    @property
    def manager(self):return self.endpoint.rooms

    def settings(self,room):
        owner=self.endpoint.server.accounts.get(room.owner)
        map_id=room.map_id
        roles=b''.join(field(1,field(1,m.account_id)+field(2,field(1,field(1,
            OWNER_ROLE if m.account_id==room.owner else 0)))) for m in room.members.values())
        occupied = {member.team for member in room.members.values()}
        limits = {team: dict(minCount=0 if bots and team not in occupied else 1,
                             maxCount=(map_team_capacity(map_id),)*3, level=(1,10))
                  for team, bots in zip(TEAMS, room.active_bot_counts)}
        minutes = room.battle_settings.minutes
        seconds = 600 if minutes is None else minutes * 60
        return dict(type=2,startType=1,defaultRoster=0,comment=room_comment(room),
            creator=owner.nickname,creatorDBID=owner.id,creatorClanDBID=0,creatorClanTag='',
            isOpened=True,arenaTypeID=map_id,roundLength=seconds,lifeTime=3600,gameModeID=2,
            title=room.title,isTeamKill=False,password='',startTime=0,matchID='',
            teamRoles={1:0,2:0},limits=limits,
            roles=roles,spectatorsMaxScope=0,extraData=None,extraDataQuick=None)

    def member(self,member,in_battle=False):
        account=self.endpoint.server.accounts.get(member.account_id)
        state=8 if in_battle else 4 if member.ready else 0
        descriptor=member.descriptor
        if descriptor is None:
            if self.progression is not None:raise ValueError('Garage vehicle snapshot is unavailable')
            descriptor=self.endpoint.definition.descriptor
        data=b''.join(field(k,v) for k,v in ((1,account.id),(2,account.nickname),(3,account.id),
            (4,member.team),(5,state),(7,descriptor),
            (8,0),(9,''),(10,1),(11,1),(12,0),(14,6),(16,1),(17,0),(18,0),(19,0),
            (20,0),(21,0),(22,0),(23,0),(24,0),(26,0),(27,0),(28,''),(29,0)))
        return data+varint(6*8+1)+struct.pack('<d',time.time())

    @property
    def progression(self):return getattr(self.endpoint,'progression',None)

    def refresh_vehicle(self,account_id,inventory_id=None):
        """Refresh a member's cached garage vehicle; only idle rooms are touched."""
        progression=self.progression
        room=self.manager.current(account_id)
        if progression is None or room is None or room.arena_id or account_id not in room.members:return False
        if inventory_id is not None:self.manager.editable(account_id)
        state=progression.prepare(account_id)
        if inventory_id is not None and state['selected_id']!=inventory_id:
            progression.select(account_id,inventory_id)
            state=progression.prepare(account_id)
        descriptor=bytes.fromhex(progression.selected_inventory(account_id)['descriptor'])
        member=room.members[account_id]
        changed=member.descriptor is not None and (member.inventory_id,member.descriptor)!=(state['selected_id'],descriptor)
        member.inventory_id,member.descriptor=state['selected_id'],descriptor
        if changed:self.manager.reset_ready(room)
        return True

    def roster(self,room):
        # Members that joined through the CLI have no snapshot yet; fill it once, idle rooms only.
        if self.progression is not None and not room.arena_id:
            for m in room.members.values():
                if m.descriptor is None:self.refresh_vehicle(m.account_id)
        arena = self.endpoint.arenas.get(room.arena_id) if room.arena_id else None
        return b''.join(field(1,self.member(m,arena is not None and
            m.account_id in arena.players and arena.players[m.account_id].attached)) for m in room.members.values())

    def snapshot(self,room):
        # Type-2 settings can start XChatServiceAgent, which dereferences this
        # statistics structure. Opcode 10 initializes it without settings.
        return (update(10,pickle_document(self.statistics(room)))+
                update(7,pickle_document(self.settings(room)))+
                update(1,self.roster(room))+update(6,pickle_document((0,0))))

    def statistics(self,room):
        self.manager.sync()
        arena = self.endpoint.arenas.get(room.arena_id) if room.arena_id else None
        waiting = bool(arena is not None and not getattr(arena,'finished_at',0)
                       and not getattr(arena,'closed',False) and not getattr(arena,'retiring',False)
                       and (getattr(arena,'building',False) or not getattr(arena,'started',0)))
        return dict(wins=dict(room.wins),battlesCount=room.battles_count,
                    createTime=room.created_at,waitPrepareClients=waiting)

    def version(self,room):
        statistics = self.statistics(room)
        arena = self.endpoint.arenas.get(room.arena_id) if room.arena_id else None
        return (room.id,room.owner,room.title,room.description,room.map_id,room.arena_id,room.bot_counts,
                room.battle_mode,room.battle_settings,
                statistics['createTime'],statistics['battlesCount'],
                tuple(statistics['wins'][team] for team in (1,2)),statistics['waitPrepareClients'],
                tuple((m.account_id,m.team,m.ready,m.inventory_id,m.descriptor) for m in room.members.values()),
                tuple((p.account_id,p.attached) for p in arena.players.values()) if arena else ())

    def send(self,client,payload):
        for addr,session in self.endpoint.clients.items():
            if session is client and client.phase=='account':
                self.endpoint.send(addr,client,b'\x13'+payload)
                return

    def reply(self,client,request,result=0,data=None,message=''):
        if request!=NO_RESPONSE:self.send(client,command_response(request,result,data,message))

    def attach(self,client,room):
        self.send(client,account_method(17,struct.pack('<I',int(room.id,16)))+self.snapshot(room))
        client.native_room_id=room.id
        client.native_room_version=self.version(room)

    def sync_client(self,addr,client):
        """Send only actual changes, including CLI edits, kicks and disconnects."""
        if client.phase!='account' or not client.native_room_id:return
        room=self.manager.rooms.get(client.native_room_id)
        if room is None or client.account_id not in room.members:
            self.endpoint.send(addr,client,b'\x13'+account_method(5,b''))
            client.native_room_id=client.native_room_version=None
        elif self.version(room)!=client.native_room_version:
            self.endpoint.send(addr,client,b'\x13'+self.snapshot(room))
            client.native_room_version=self.version(room)

    def create(self,client,body):
        try:
            account=client.account_id
            if self.progression is None:
                title,description,mode,map_id=create_request(body,with_map=True,with_mode=True,
                    configured_map_id=self.map_id,available_map_ids=self.map_ids);vehicle=None
            else:
                title,description,mode,vehicle,map_id=create_request(body,with_vehicle=True,with_mode=True,
                    with_map=True,configured_map_id=self.map_id,
                    available_map_ids=self.map_ids)
                self.manager.hangar(account)
                if self.manager.current(account):raise ValueError('Already in a room')
                from .battle_settings_wire import GET_TITLE
                if title != GET_TITLE and not any(v['id']==vehicle for v in self.progression.prepare(account)['vehicles']):
                    raise ValueError('Vehicle is not in the garage')
            from .battle_settings import decode, packed_recipe
            from .battle_settings_wire import TITLE, SAVED_RESULT, GET_TITLE, LOADED_RESULT
            if title == GET_TITLE:
                from .training_config import TrainingConfig
                fallback = getattr(self.endpoint.server, 'training_config', None) or TrainingConfig()
                config = self.endpoint.server.accounts.store.training_config(account, fallback).for_game_mode(7)
                self.send(client, account_method(19, struct.pack('<IB',
                    packed_recipe(config, self.endpoint.map_catalog), LOADED_RESULT)))
                self.endpoint.server.event('native_bot_settings_loaded', account_id=account,
                                           settings=config.to_dict())
                return
            if title == TITLE:
                config = decode(title, description, map_id, self.endpoint.map_catalog)
                if (map_id != 0 and config.battle_mode == 'supremacy'
                        and not self.endpoint.resolve_map_spec(map_id).strategic_points):
                    raise ValueError('This map has no Supremacy capture points')
                try:
                    self.endpoint.server.accounts.store.save_training_config(account, config)
                except sqlite3.Error as exc:
                    raise ValueError('Cannot save bot battle settings') from exc
                self.send(client, account_method(19, struct.pack('<IB',
                    packed_recipe(config, self.endpoint.map_catalog), SAVED_RESULT)))
                self.endpoint.server.event('native_bot_settings_saved', account_id=account,
                                           settings=config.to_dict())
                return
            description,bot_counts,override,settings=room_options(description)
            room=self.manager.create(account,map_id=self.map_id if map_id==0 else map_id,
                                     bot_counts=bot_counts,
                                     battle_mode=override or ('supremacy' if mode==1 else 'encounter'),
                                     battle_settings=settings)
            room.title,room.description=title,description
            if vehicle is not None:
                try:self.refresh_vehicle(account,vehicle)
                except ValueError:
                    self.manager.disband(account);raise
            self.attach(client,room)
            self.endpoint.server.event('native_room_created',account_id=client.account_id,
                room_id=room.id,map_id=room.map_id,
                map_key=self.endpoint.map_catalog[room.map_id],battle_mode=room.battle_mode)
        except ValueError as exc:
            self.send(client,account_method(19,struct.pack('<IB',0xffffffff,1)))
            self.endpoint.server.event('native_room_rejected',account_id=client.account_id,reason=str(exc))

    def listing(self,room):
        # PrebattleInfo integer-key dictionary, parsed at 0x17284c0.
        map_id=room.map_id
        minutes = room.battle_settings.minutes
        seconds = 600 if minutes is None else minutes * 60
        return (int(room.id,16),{1:2,2:bool(room.arena_id),3:0,4:True,5:len(room.members),
            6:self.endpoint.server.accounts.get(room.owner).nickname,7:room.owner,8:0,
            9:room_comment(room, include_rules=False),11:map_id,12:seconds,14:2,15:'',16:room.title,
            17:(0,0,0),18:0x3ff,21:False,22:False,24:0})

    def command(self,client,kind,body,request,command):
        if command not in COMMANDS:return False
        ident=0xffffffff
        try:
            if client.phase!='account' or not client.account_created:
                raise ValueError('Wait for the hangar to load')
            numbers,strings=arguments(kind,body)
            if command==524:
                if kind!=149 or numbers!=(0,) or len(strings)!=1 or len(strings[0])>256:
                    raise ValueError('Unsupported room search')
                pattern=strings[0].decode('utf-8').casefold()
                self.manager.sync()
                found=[self.listing(r) for r in self.manager.rooms.values()
                       if fnmatchcase(r.title.casefold(),pattern or '*')]
                self.reply(client,request,data=found)
                room=self.manager.current(client.account_id)
                if room and client.native_room_id!=room.id:
                    # Account recreation after a battle clears ClientPrebattle.
                    # The player remains in their own hangar until this request.
                    self.attach(client,room)
                return True
            if command==406:
                if (kind!=149 or len(numbers)!=4 or numbers[0] not in (0,*self.map_ids)
                        or numbers[1] not in (0,1,2) or numbers[2:]!=(0,0) or len(strings)!=2):
                    raise ValueError('Choose a supported map and default battle options')
                from .battle_settings_wire import TITLE
                if strings[0] == TITLE.encode():
                    raise ValueError('Leave the training room before starting a bot battle')
                title,description=room_text(*strings)
                room = self.manager.owned(client.account_id)
                description,bot_counts,mode,settings=room_options(description, room)
                changed=self.manager.rename(client.account_id,title,description,
                    map_id=numbers[0] or None,bot_counts=bot_counts,
                    battle_mode=mode or ('supremacy' if numbers[1]==1 else 'encounter'),
                    battle_settings=settings)
                if numbers[0]:
                    self.endpoint.server.event('native_room_map_changed',account_id=client.account_id,
                        room_id=changed.id,map_id=changed.map_id,
                        map_key=self.endpoint.map_catalog[changed.map_id],battle_mode=changed.battle_mode)
                self.reply(client,request)
                return True
            if kind!=140 or strings:raise ValueError('Invalid room command layout')
            target,arg1,arg2=numbers
            if command in (400,523):
                name=room_id(target);ident=target
                if command==523:
                    if (arg1,arg2)!=(0,0):raise ValueError('Unsupported roster arguments')
                elif self.progression is None:
                    if (arg1,arg2)!=(0,1):raise ValueError('Unsupported join arguments')
                else:
                    if arg1!=0 or arg2<=0:raise ValueError('Invalid join inventory vehicle')
                    if not any(v['id']==arg2 for v in self.progression.prepare(client.account_id)['vehicles']):
                        raise ValueError('Vehicle is not in the garage')
                if command==400:
                    fresh=self.manager.current(client.account_id) is None
                    room=self.manager.join(client.account_id,name)
                    try:self.refresh_vehicle(client.account_id,arg2 if self.progression is not None else None)
                    except ValueError:
                        if fresh:self.manager.leave(client.account_id)
                        raise
                    self.attach(client,room)
                    self.endpoint.server.event('native_room_joined',account_id=client.account_id,room_id=room.id)
                else:
                    room=self.manager.rooms.get(name)
                    if room is None:raise ValueError('Room does not exist')
                    # ReceivePrebattleRoster expects RES_STREAM and a pickle
                    # tuple (room ID, protobuf Roster), not a plain CmdResponse.
                    self.send(client,resource_response(request,(ident,self.roster(room))))
                return True
            room=self.manager.current(client.account_id)
            if room is None:raise ValueError('Join a room first')
            if command==401:
                if numbers!=(0,0,0):raise ValueError('Unsupported leave arguments')
                if room.owner==client.account_id and not room.arena_id:self.manager.disband(client.account_id)
                else:self.manager.leave(client.account_id)
            elif command in (402,403):
                if self.progression is None:
                    if numbers!=(1,0,0):raise ValueError('Only the first inventory vehicle is supported')
                else:
                    if numbers[0]<=0 or numbers[1:]!=(0,0):raise ValueError('Invalid inventory vehicle')
                    self.refresh_vehicle(client.account_id,numbers[0])
                self.manager.ready(client.account_id,command==402)
            elif command==404:
                if arg2:raise ValueError('Unsupported roster flags')
                self.manager.move(client.account_id,target,arg1)
            elif command==414:
                if arg1 or arg2:raise ValueError('Unsupported kick flags')
                self.manager.kick(client.account_id,target)
            elif command==415:
                # The client reports observer lock for its Account entity 100.
                # Spectators are disabled; accept only the observed unlocked form.
                if numbers!=(100,0,0):raise ValueError('Spectator mode is unavailable')
                self.manager.editable(client.account_id)
            elif command==411:
                if target not in (1,2) or (arg1,arg2)!=(1,0):
                    raise ValueError('Unsupported battle start arguments')
                self.manager.start(client.account_id)
                # A queued owner remains in Account. Actual participants receive
                # ResetEntities instead of a reply addressed to the old entity.
                if client.phase=='account':self.reply(client,request)
                self.endpoint.server.event('native_room_started',account_id=client.account_id,room_id=room.id)
                return True
            self.reply(client,request)
        except ValueError as exc:
            if command==400:
                self.send(client,account_method(19,struct.pack('<IB',ident,1)))
            else:
                self.reply(client,request,-1,message=str(exc))
                if command==411:
                    # Native UI locally latches TEAM_STATES while starting.
                    self.send(client,update(6,pickle_document((0,0))))
            self.endpoint.server.event('native_room_rejected',account_id=client.account_id,
                                       command=command,reason=str(exc))
        return True
