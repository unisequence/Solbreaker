"""Small account-owned rooms; arenas remain owned by ArenaManager."""
from dataclasses import dataclass,field
import json
import re
import secrets
import time
from .battle_setup import RoomLineup
from .roster import TEAMS, TEAM_SIZE, ROOM_CAPACITY, WAITING
from .map_catalog import room_team_capacity
from .training_config import BATTLE_MODES, PROVING_GROUND_ID, RoomBattleSettings


@dataclass
class Member:
    account_id: int
    team: int
    ready: bool = False
    # Cached garage snapshot; filled by NativeRooms.refresh_vehicle.
    inventory_id: int = 1
    descriptor: bytes | None = None


@dataclass
class Room:
    id: str
    owner: int
    members: dict[int,Member] = field(default_factory=dict)
    arena_id: str | None = None
    title: str = "Local room"
    description: str = ""
    map_id: int = 6
    created_at: int = field(default_factory=lambda: int(time.time()))
    battles_count: int = 0
    wins: dict[int,int] = field(default_factory=lambda: {team:0 for team in TEAMS})
    bot_counts: tuple[int, int] = (0, 0)
    lineup: RoomLineup = field(default_factory=RoomLineup)
    battle_mode: str = 'encounter'
    battle_settings: RoomBattleSettings = field(default_factory=RoomBattleSettings)
    # Only result IDs are retained, never worlds or departed player objects.
    _counted_results: set[str] = field(default_factory=set,repr=False)

    def apply_map_rules(self):
        if self.map_id == PROVING_GROUND_ID:
            self.battle_settings = RoomBattleSettings(unlimited_lives=True, minutes=0, unlimited_ammo=True)
            self.battle_mode = 'encounter'
            self.bot_counts = (0, 0)
            self.lineup = RoomLineup()

    @property
    def team_capacity(self):
        return room_team_capacity(self.map_id, self.battle_mode, self.battle_settings)

    @property
    def active_bot_counts(self):
        capacity = self.team_capacity
        free = tuple(capacity-sum(m.team == team for m in self.members.values()) for team in TEAMS)
        if self.lineup.custom:
            return tuple(len(self.lineup.team_slots(team, available))
                         for team, available in zip(TEAMS, free))
        return tuple(max(0, min(count, available)) for count, available in zip(self.bot_counts, free))


def room_bot_counts(values, capacity=TEAM_SIZE):
    if (not isinstance(values, (tuple, list)) or len(values) != len(TEAMS)
            or any(type(count) is not int or not 0 <= count <= capacity for count in values)):
        raise ValueError(f'Choose 0 to {capacity} bots for each team')
    return tuple(values)


class RoomManager:
    def __init__(self,endpoint, *, room_changed=None):
        self.endpoint = endpoint
        self.rooms = {}
        self.memberships = {}
        self.room_changed = room_changed or (lambda account_id: None)

    def sync(self):
        for room in self.rooms.values():
            if not room.arena_id:
                continue
            arena = self.endpoint.arenas.get(room.arena_id)
            if arena is None:
                room.arena_id = None
                self.reset_ready(room)
            elif (getattr(arena,'result_id',None)==room.arena_id
                    and getattr(arena,'started',0) and getattr(arena,'finished_at',0)
                    and type(getattr(arena,'winner',None)) is int and arena.winner in (0,*TEAMS)
                    and arena.result_id not in room._counted_results):
                # Loading cancellation and quarantined unfinished worlds have
                # no started+finished result and must not fabricate a battle.
                room._counted_results.add(arena.result_id)
                room.battles_count += 1
                if arena.winner in TEAMS:
                    room.wins[arena.winner] += 1

    @staticmethod
    def reset_ready(room):
        for member in room.members.values():member.ready=False

    def hangar(self,account):
        sessions=[s for s in self.endpoint.clients.values() if s.account_id==account]
        if len(sessions)!=1 or sessions[0].phase!='account' or not sessions[0].account_created or sessions[0].arena_id:
            raise ValueError('A connected game client must be in its hangar')

    def current(self,account):
        self.sync()
        return self.membership(account)

    def membership(self, account):
        return self.rooms.get(self.memberships.get(account))

    def editable(self,account):
        room=self.current(account)
        if room is None:raise ValueError('Join a room first')
        if room.arena_id:raise ValueError('Wait until all participants return to their hangars')
        self.hangar(account)
        return room

    @staticmethod
    def team(value):
        if type(value) is not int or value not in (1,2):raise ValueError('Team must be 1 or 2')
        return value

    def create(self,account,team=1,map_id=None,*,bot_counts=(0,0),battle_mode='encounter',
               battle_settings=RoomBattleSettings(),lineup=None):
        team=self.team(team);self.hangar(account)
        if self.current(account):raise ValueError('Already in a room')
        if len(self.rooms)>=32:raise ValueError('Room limit reached')
        if map_id is None:
            map_id=self.endpoint.map_spec.map_id
        self.validate_map_id(map_id)
        capacity = self.validate_settings(map_id, battle_mode, battle_settings)
        bot_counts=room_bot_counts(bot_counts, max(TEAM_SIZE, capacity))
        while True:
            ident=secrets.token_hex(3).upper()
            # Native prebattleID 0 means no room.
            if ident!='000000' and ident not in self.rooms:break
        room=Room(ident,account,{account:Member(account,team)},map_id=map_id,bot_counts=bot_counts,
                  battle_mode=battle_mode,battle_settings=battle_settings,lineup=lineup or RoomLineup())
        room.apply_map_rules()
        self.rooms[ident]=room;self.memberships[account]=ident
        self.room_changed(account)
        return room

    def join(self,account,ident,team=None):
        if not isinstance(ident,str) or not re.fullmatch('[A-Fa-f0-9]{6}',ident):
            raise ValueError('Room ID must contain six hex characters')
        ident=ident.upper();self.hangar(account);existing=self.current(account)
        if existing:
            if existing.id==ident:return existing
            raise ValueError('Already in another room')
        room=self.rooms.get(ident)
        if room is None:raise ValueError('Room does not exist')
        if room.arena_id or len(room.members)>=max(ROOM_CAPACITY, 2*room.team_capacity):raise ValueError('Room is busy or full')
        counts = {t:sum(m.team==t for m in room.members.values()) for t in TEAMS}
        capacity = room.team_capacity
        if team is None:
            available = [t for t in TEAMS if counts[t]<capacity]
            team = min(available,key=counts.get) if available else WAITING
        else:
            team = self.team(team)
            if counts[team]>=capacity:raise ValueError('Team is full')
        room.members[account]=Member(account,team);self.memberships[account]=ident
        self.reset_ready(room)
        self.room_changed(account)
        return room

    def choose_team(self,account,team):
        team=self.team(team);room=self.editable(account)
        if sum(m.account_id!=account and m.team==team for m in room.members.values())>=room.team_capacity:
            raise ValueError('Team is full')
        if room.members[account].team!=team:
            room.members[account].team=team;self.reset_ready(room)
        return room

    def ready(self,account,value):
        if type(value) is not bool:raise ValueError('Ready must be boolean')
        room=self.editable(account)
        if value and room.members[account].team not in (1,2):
            raise ValueError('Choose a team before becoming ready')
        room.members[account].ready=value
        return room

    def owned(self,account):
        room=self.editable(account)
        if room.owner!=account:raise ValueError('Only the room owner may manage participants')
        return room

    def validate_map_id(self,map_id):
        if type(map_id) is not int or map_id not in self.endpoint.map_catalog:
            raise ValueError('Choose a supported training map')
        return map_id

    def set_map(self,account,map_id):
        room=self.owned(account)
        map_id=self.validate_map_id(map_id)
        self.validate_settings(map_id, room.battle_mode, room.battle_settings, room.members.values())
        if room.map_id!=map_id:
            room.map_id=map_id
            room.apply_map_rules()
            self.reset_ready(room)
        return room

    def validate_settings(self, map_id, battle_mode, settings, members=()):
        if battle_mode not in BATTLE_MODES:
            raise ValueError('Choose a supported room battle mode')
        capacity = room_team_capacity(map_id, battle_mode, settings)
        if any(sum(member.team == team for member in members) > capacity for team in TEAMS):
            raise ValueError(f'This map supports up to {capacity} participants per team')
        if map_id != PROVING_GROUND_ID and battle_mode == 'supremacy' and not self.endpoint.resolve_map_spec(map_id).strategic_points:
            raise ValueError('This map has no Supremacy capture points')
        return capacity

    def move(self,account,target,roster):
        """Native roster 17 is the waiting queue; it never enters an arena."""
        room=self.owned(account)
        if target not in room.members:raise ValueError('Player is not in this room')
        if type(roster) is not int or roster not in (1,2,17):raise ValueError('Unsupported roster')
        if roster in TEAMS and sum(m.account_id!=target and m.team==roster for m in room.members.values())>=room.team_capacity:
            raise ValueError('Team is full; move a player to the queue first')
        if room.members[target].team!=roster:
            room.members[target].team=roster;self.reset_ready(room)
        return room

    def kick(self,account,target):
        room=self.owned(account)
        if target==account or target not in room.members:raise ValueError('Choose another room member')
        self.leave(target)
        return room

    def disband(self,account):
        room=self.owned(account)
        for target in list(room.members):self.leave(target)

    def rename(self,account,title,description,*,map_id=None,bot_counts=None,battle_mode=None,
               battle_settings=None,lineup=None):
        room=self.owned(account)
        if map_id is not None:
            map_id=self.validate_map_id(map_id)
        settings = room.battle_settings if battle_settings is None else battle_settings
        capacity = self.validate_settings(map_id or room.map_id,
            room.battle_mode if battle_mode is None else battle_mode, settings, room.members.values())
        if bot_counts is not None:
            bot_counts=room_bot_counts(bot_counts, max(TEAM_SIZE, capacity))
        elif capacity < room.team_capacity:
            bot_counts=tuple(min(count, capacity) for count in room.bot_counts)
        if map_id is not None:
            room.map_id=map_id
        if bot_counts is not None:
            room.bot_counts=bot_counts
        if battle_mode is not None:
            room.battle_mode=battle_mode
        if battle_settings is not None:
            room.battle_settings=battle_settings
        if lineup is not None:
            room.lineup=lineup
        room.apply_map_rules()
        room.title,room.description=title,description
        self.reset_ready(room)
        return room

    def set_bots(self,account,counts):
        room=self.owned(account)
        counts=room_bot_counts(counts, max(TEAM_SIZE, room.team_capacity))
        if room.map_id == PROVING_GROUND_ID and any(counts):
            raise ValueError('Place targets from the proving ground menu during the session')
        if room.bot_counts!=counts or room.lineup.custom:
            room.bot_counts=counts
            room.lineup=RoomLineup()
            self.reset_ready(room)
        return room

    def start(self,account):
        room=self.editable(account)
        if room.owner!=account:raise ValueError('Only the room owner may start the battle')
        members=[m for m in room.members.values() if m.team in TEAMS]
        from .roster import validate_teams
        teams=[m.team for m in members]
        bot_counts=room.active_bot_counts
        if not members:
            raise ValueError('At least one player must join a team')
        validate_teams(teams + [team for team, count in zip(TEAMS, bot_counts) for _ in range(count)],
                       practice=room.map_id == PROVING_GROUND_ID, capacity=room.team_capacity)
        if not all(m.ready for m in members):
            raise ValueError('Every participant on both teams must be ready')
        # Endpoint validates both sessions before creating or attaching anything.
        options={'room_settings':room.battle_settings}
        if any(bot_counts):
            options['bot_counts']=bot_counts
        if room.lineup.custom and any(bot_counts):
            options['room_lineup']=room.lineup
        if room.battle_mode != 'encounter':
            options['room_mode'] = room.battle_mode
        arena=self.endpoint.start_shared([m.account_id for m in members],teams,map_id=room.map_id,**options)
        room.arena_id=arena.result_id
        self.reset_ready(room)
        return room

    def leave(self,account,*,disconnected=False):
        room=self.current(account)
        if room is None:return None
        if room.arena_id and not disconnected:
            arena = self.endpoint.arenas.get(room.arena_id)
            player = arena.players.get(account) if arena is not None else None
            if player is not None and player.attached:
                raise ValueError('Leave the battle in the game first')
            self.hangar(account)
        del room.members[account];del self.memberships[account]
        self.reset_ready(room)
        if not room.members:del self.rooms[room.id]
        elif room.owner==account:room.owner=next(iter(room.members))
        self.room_changed(account)
        return None

    def describe(self,room):
        if room is None:return None
        return {'id':room.id,'owner':room.owner,'arena_id':room.arena_id,
                'map_id':room.map_id,'map_key':self.endpoint.map_catalog[room.map_id],
                'battle_mode':room.battle_mode,'large_teams':room.battle_settings.large_teams,
                'unlimited_lives':room.battle_settings.unlimited_lives,
                'unlimited_ammo':room.battle_settings.unlimited_ammo,
                'battle_minutes':room.battle_settings.minutes,
                'bot_counts':list(room.bot_counts),'active_bot_counts':list(room.active_bot_counts),
                'state':'battle' if room.arena_id else 'waiting',
                'members':[{'account_id':m.account_id,
                    'nickname':self.endpoint.server.accounts.get(m.account_id).nickname,
                    'team':m.team,'ready':m.ready} for m in room.members.values()]}

    def http(self,method,path,headers,body):
        scheme,_,token=headers.get('authorization','').partition(' ')
        accounts=self.endpoint.server.accounts
        if scheme.lower()!='bearer' or not accounts.valid(accounts.access,token):
            return 401,{'error':'A local account bearer token is required'}
        account=accounts.access[token].account_id
        if method=='GET' and path=='/rooms':
            return 200,{'account_id':account,'room':self.describe(self.current(account))}
        if method=='GET' and path=='/maps':
            return 200,{'default_map_id':self.endpoint.map_spec.map_id,
                'maps':[{'id':map_id,'key':key,
                         'default':map_id==self.endpoint.map_spec.map_id}
                        for map_id,key in self.endpoint.map_catalog.items()]}
        if method!='POST':return 405,{'error':'Use GET /rooms or POST /rooms/action'}
        try:
            data=json.loads(body or b'{}')
            if not isinstance(data,dict):raise ValueError('Expected a JSON object')
            if path=='/rooms/create':room=self.create(account,data.get('team',1),data.get('map_id'),
                bot_counts=data.get('bot_counts',(0,0)), battle_mode=data.get('battle_mode','encounter'),
                battle_settings=RoomBattleSettings(data.get('unlimited_lives',False),
                    data.get('battle_minutes'),data.get('unlimited_ammo',False),data.get('large_teams',False)))
            elif path=='/rooms/join':room=self.join(account,data.get('id'),data.get('team'))
            elif path=='/rooms/map':room=self.set_map(account,data.get('map_id'))
            elif path=='/rooms/bots':room=self.set_bots(account,data.get('bot_counts'))
            elif path=='/rooms/team':room=self.choose_team(account,data.get('team'))
            elif path=='/rooms/ready':room=self.ready(account,data.get('ready',True))
            elif path=='/rooms/start':room=self.start(account)
            elif path=='/rooms/leave':room=self.leave(account)
            else:return 404,{'error':'Unknown room action'}
        except (ValueError,UnicodeError) as exc:
            return 409,{'error':str(exc)}
        self.endpoint.server.event('room_action',account_id=account,action=path.rsplit('/',1)[-1],
                                   room_id=room.id if room else None)
        return 200,{'account_id':account,'room':self.describe(room)}
