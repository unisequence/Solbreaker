"""Authoritative arena state. No Mercury IDs, packet bytes or sockets here."""
import dataclasses
from collections import Counter
import math
import os
import random
import time
import uuid

from . import diagnostics
from .ballistics import Projectile, ReconContact, SweepCache, TrackContact, advance, body_states, interpolate
from .armor import MovableArmorStateSnapshot
from .collision import CollisionWorld, Pose
from .marker_batch import try_markers as _try_marker_batch
from .entities import BattleEvent, BattleResult, MotionFrame, MotionSample, Player, Vehicle
from .motion import game_tick
from .aiming import GunAim, wrap
from .roster import TEAMS, validate_teams, training_spawn, training_yaw
from .orientation import matrix
from .consumables import ACTIVE, HEAL, RESTORE, ConsumableSpec, ConsumableState

from .loadout_effects import ANTI_HE_PROVISION
from .internal_geometry import GeometryConfiguration, PreparedInternalGeometry
from .modules import (AmmoRackState, CrewSpec, CrewState, EngineState, FuelTankState, GunState,
                      TrackState, crew_bindings_for_meshes)
from .fire import FirePolicy, FireState
from .track_fall_damage import evaluate_track_fall
from .landing_skid import MAX_DURATION as LANDING_MAX_DURATION, grip_fraction, touchdown_skid
from .track_drive import (TrackDriveMotion, passive_track_step,
                          COAST_DRAG_RATE, BOTH_DRAG_RATE)
from .mobility import (TerrainMobility,engine_acceleration,resolve_terrain_mobility,
                       rolling_acceleration)
from .recon import ReconState
from .bot_ai import TrainingBot
from .base_capture import BaseCapture
from .supremacy import Supremacy
from . import sandbox

GRAVITY_JUMP_SPEC = ConsumableSpec('gravityJump', 44, (44 << 8) | 0xfb, 0., 20.)

# Pinned common/vehicle.xml extra indices, confirmed against native registry.
_TRACK_EXTRAS = {'chassis_track_L': 34, 'chassis_track_R': 35}
_ENGINE_EXTRA = 31  # engineHealth
_AMMO_RACK_EXTRA = 32  # ammoBayHealth
_FUEL_EXTRA = 33  # fuelTankHealth; orange even at zero HP
_GUN_EXTRA = 36  # gunHealth: the external gun, not gunBreech
# commander/driver/gunner1/2/loader1/2 Health; all wire states native-verified.
# A crew member still needs its own geometry binding before it can be damaged.
_CREW_EXTRAS = frozenset((39, 40, 41, 42, 43, 44))
_SHIELDED_KINDS = ('ARMOR_PIERCING','ARMOR_PIERCING_CR','HOLLOW_CHARGE')
_INTERNAL_KINDS = ('ARMOR_PIERCING','ARMOR_PIERCING_CR','HOLLOW_CHARGE')


def _step_budget(value):
    if value is not None and (isinstance(value, bool) or
            not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0):
        raise ValueError('Invalid simulation work budget')
    return float(value) if value is not None else None


class Arena:
    """Shared world; participants and vehicles outlive individual UDP sessions."""
    STEP = 1 / 60
    MISSILE_UPDATE_STEPS = 6
    MAX_CATCHUP = .25
    POSTBATTLE_SECONDS = 5.
    CONTROL_INPUT_TIMEOUT = 3.
    AIRBORNE_CONTROL_GRACE = 8.
    # Local grip tuning: lower than the XML climb-angle boundary so a steep
    # slope starts winning against the tracks before the old 25-degree gate.
    SLOPE_GRIP_FACTOR = .8
    RECON_CAMOUFLAGE_STILL_SPEED = .2  # local sandbox tuning, m/s
    RECON_CAMOUFLAGE_STILL_YAW_RATE = .02  # local threshold, rad/s
    RECON_CAMOUFLAGE_STILL_SECONDS = 3.
    # Local sandbox ram rule (opt-in ground impacts only), not a client value:
    # HP per kJ of the normal impact energy share each participant absorbs.
    RAM_DAMAGE_PER_KILOJOULE = .5
    def __init__(self, accounts, descriptor, terrain, collision_scene=None, *,
                 space_id=1, entity_base=0, solo=True, now=None, shell=None, incremental=False,
                 vehicle_scenes=None, map_spec=None, recon_enabled=False, recon_game=None,
                 bot_enabled=False, training=None, step_budget=None, result_id=None,
                 build_world=True, bot_roster=None, bot_game=None, bot_scene_settings=(),
                 prebattle_seconds=0., parallel_native_build=False, sandbox_enabled=False,
                 room_mode='encounter', room_settings=None):
        if (isinstance(prebattle_seconds, bool) or not isinstance(prebattle_seconds, (int, float))
                or not math.isfinite(prebattle_seconds) or not 0 <= prebattle_seconds <= 60):
            raise ValueError('Invalid prebattle duration')
        self.prebattle_seconds = float(prebattle_seconds)
        if type(sandbox_enabled) is not bool:
            raise ValueError('Invalid sandbox setting')
        self.sandbox_enabled = sandbox_enabled
        self._sandbox_inputs = {}
        self._sandbox_flights = {}
        self.prebattle_end = 0.
        self.step_budget = _step_budget(step_budget)
        if type(recon_enabled) is not bool:
            raise ValueError('Invalid recon setting')
        if type(bot_enabled) is not bool or bot_enabled and not solo:
            raise ValueError('Training bot is available only in solo battles')
        from .training_config import BATTLE_MODES, RoomBattleSettings, TrainingConfig
        if training is not None and (not solo or not isinstance(training, TrainingConfig)):
            raise ValueError('Invalid solo training configuration')
        training = training or TrainingConfig(mode='bots' if bot_enabled else 'target')
        bot_enabled = training.bots_enabled or bool(bot_roster and not solo)
        if room_mode not in BATTLE_MODES:
            raise ValueError('Invalid training room mode')
        self.room_mode = room_mode
        if room_settings is not None and (solo or not isinstance(room_settings, RoomBattleSettings)):
            raise ValueError('Invalid training room settings')
        self.room_settings = room_settings
        settings = training.battle_settings if solo else room_settings or RoomBattleSettings()
        self.endless = settings.minutes == 0
        self.unlimited_lives = settings.unlimited_lives
        self.unlimited_ammo = settings.unlimited_ammo
        rules = (training.battle_mode if training.bots_enabled else 'elimination') if solo else room_mode
        self.gravity_mode = rules == 'gravity'
        self._gravity_game = bot_game if self.gravity_mode else None
        self.practice = sandbox_enabled and solo and not bot_enabled
        self.practice_clock_repeats = self.practice and settings.minutes is None
        self.practice_slots = set()
        self.practice_spawned = set()
        self.practice_active = set()
        self.practice_selection = 0
        self.practice_types = {}
        self.practice_return_fire = False
        self.practice_gunners = {}
        self.practice_next_spawn = 0.
        self.respawn_at = {}
        self.practice_timer_at = 0.
        if result_id is not None and (type(result_id) is not str or len(result_id) != 32
                                      or any(c not in '0123456789abcdef' for c in result_id)):
            raise ValueError('Invalid arena identity')
        self.result_id = uuid.uuid4().hex if result_id is None else result_id
        self.unique_id = int(self.result_id[:15],16) or 1
        self.created_at = time.time()
        self.space_id, self.entity_base, self.solo = space_id,entity_base,solo
        self.terrain = terrain
        self.map_spec = map_spec
        self.map_id = map_spec.map_id if map_spec is not None else 6
        point = getattr(map_spec, 'capture_point', None)
        capture_enabled = rules == 'encounter'
        self.base_capture = BaseCapture(point) if point is not None and capture_enabled else None
        self.supremacy = (Supremacy(getattr(map_spec, 'strategic_points', ()))
                         if rules == 'supremacy' else None)
        self.client_map_id = self.map_id | (0x10000 if self.supremacy is not None else 0)
        self.battle_seconds = 420 if self.base_capture is not None or self.supremacy is not None else 600
        if settings.minutes:
            self.battle_seconds = settings.minutes * 60
        self.epoch = time.monotonic() if now is None else now
        self.last_tick = self.epoch
        self.simulation_time = self.epoch
        self.module_clock = self.epoch
        self.track_fall_peaks = {}
        # Passive contact survives a broken-belt episode and its repair. It
        # never owns Bullet's flight state or changes an intact drivetrain.
        self._track_drive_motion = {}
        self._track_drive_yaw_rates = {}
        self._track_drive_track_rates = {}
        self._track_repair_hud_extra = {}
        # A short reduced-side-grip episode after a real flight touchdown.
        self._landing_airborne_since = {}
        self._landing_skids = {}
        self.accumulator = 0.
        self._input_only = False
        self.step_count = 0
        self.motion_frame = None
        self.dropped_time = 0.
        self.started = self.finished_at = 0.
        self.winner = 0
        # A solo victory opens free driving; publish its result on exit.
        self.solo_victory_team = 0
        self.finish_reason = ''
        self.players = {}
        self.vehicles = {}
        self.training_bot = None
        self.training_bots = {}
        self.bot_roster = ()
        self.bot_scene_settings = tuple(bot_scene_settings)
        self._owned_bot_scenes = []
        self.bot_director = None
        self._bot_navigation_builder = None
        self.events = []
        self.recorded_accounts = set()
        self.reports = {}
        self.projectiles = {}
        self.resolved_shots = set()
        self.destructions = {}
        self.rng = random.Random(self.result_id)
        # Separate stream: gun chance draws must not shift penetration rolls.
        self.module_rng = random.Random(self.result_id+':modules')
        # Recon is deliberately opt-in until native marker and aim inputs are
        # verified. Its dedicated stream must never alter ballistics or damage.
        self.recon_rng = random.Random(self.result_id+':recon') if recon_enabled else None
        self.recon = ReconState(random_sample=self.recon_rng.random) if recon_enabled else None
        self.recon_game = recon_game if recon_enabled else None
        self._recon_contacts = {} if recon_enabled else None  # active shot -> set(target IDs)
        self._recon_camouflage_still = {} if recon_enabled else None
        # Explicitly enabled research only; never a production visibility rule.
        self._recon_point_probe = None
        self._recon_client_aims = {}
        self.guns = {}  # entity -> GunState, independent per vehicle and arena
        self.engines = {}  # entity -> EngineState, independent per vehicle and arena
        self.ammo_racks = {}  # entity -> AmmoRackState, independent per vehicle and arena
        self.fuels = {}
        # entity -> {CrewMember index: CrewState}; never shared between entities.
        self.crews = {}
        # entity -> source/derived internal specs. Collision scenes can be
        # shared by vehicles, so loadout effects never mutate their specs.
        self.protected_specs = {}
        self.fires = {}
        self.fire_min_damage = {}
        self.fire_hp_carry = {}
        self.fall_hp_carry = {}
        self.fire_tick_indices = {}
        self.pending_ignitions = {}
        self.announced_fires = set()
        self.advancing_fires = set()
        # Factor already represented in active full-reload/pump timer work.
        # Boost rate stays on Vehicle; this prevents rack damage from changing
        # burst or intra-clip interval timing.
        self.applied_ammo_reload_factors = {}
        # Loader factor already represented in full-reload/pump timer work.
        # Keep it separate from timed boosts and mechanical intershot delays.
        self.applied_crew_reload_factors = {}
        self.next_shot = 0
        # Private native-lab instrumentation only.  It records the state after
        # every fixed drive integration without changing the protocol cadence
        # or emitting any client-visible battle event.
        self.drive_trace = os.environ.get('SOLBREAKER_DRIVE_TRACE') == '1'
        self.closed = False
        self.retiring = False
        self.world = None
        self.drive_workers = None
        self._drive_workers_attempted = False
        self._drive_workers_retry_at = 0.
        self._vehicles_configured = False
        self._incremental_loading = bool(incremental)
        self.parallel_drive_stats = Counter()
        self.building = False
        self.shell = collision_scene.armor.shell if collision_scene else shell
        teams = [a.get('team',i%2+1) for i,a in enumerate(accounts)]
        if solo:
            if len(accounts)!=1 or teams!=[1]:
                raise ValueError('Solo requires one participant on team 1')
        else:
            from .bot_roster import BotVehicle
            bot_roster = tuple(bot_roster or ())
            if not accounts or any(not isinstance(row, BotVehicle) for row in bot_roster):
                raise ValueError('Invalid room bot roster')
            validate_teams(teams + [row.team for row in bot_roster])
        if len({a['id'] for a in accounts}) != len(accounts):
            raise ValueError('Arena participants must have distinct accounts')
        scenes = dict(vehicle_scenes or {})
        if scenes and (set(scenes) != {a['id'] for a in accounts} or collision_scene is None):
            raise ValueError('Vehicle scenes must match the arena accounts and share a map')
        entity_assets = {}
        slots = dict.fromkeys(TEAMS,0)
        for i,account in enumerate(accounts):
            team = teams[i]
            player = Player(account['id'],account['nickname'],entity_base+101+i,entity_base+200+i,team)
            player.inventory_id = account.get('inventory_id')
            self.players[player.account_id] = player
            x,z = training_spawn(map_spec,team,slots[team])
            yaw = training_yaw(map_spec,team,slots[team])
            slots[team] += 1
            pose = Pose((x,terrain.height(x,z),z),yaw)
            assets = scenes.get(player.account_id,collision_scene)
            actual_descriptor = assets.definition.descriptor if assets else descriptor
            self.vehicles[player.vehicle_id] = Vehicle(player.vehicle_id,player.account_id,
                    player.nickname,team,actual_descriptor,pose)
            own = self.vehicles[player.vehicle_id]
            own.definition = assets.definition if assets else None
            controller = getattr(own.definition, 'armor_states_controller', None)
            if controller is not None:
                # The five geometry-complete Titan scenes have either fixed
                # default rows or an HP controller.  The latter's exact 50%
                # boundary and native update timing are still unproved, so it
                # stays outside battle instead of receiving a guessed rule.
                if any(state.kind != 'default' for state in controller.states):
                    raise ValueError('Movable armor health transitions are not implemented')
                own.movable_armor_state = MovableArmorStateSnapshot(
                    tuple((state.ident,state.enabled) for state in controller.states))
            own.loadout_ammo = account.get('ammo')
            own.consumables = tuple(ConsumableState(spec) if spec is not None else None
                                    for spec in account.get('consumables', ()))
            if self._gravity_game is not None:
                from .bot_roster import vehicle_class
                if vehicle_class(self._gravity_game, own.definition) in ('lightTank', 'mediumTank', 'AT-SPG'):
                    own.consumables += (None,) * max(0, 3-len(own.consumables))
                    own.consumables += (ConsumableState(GRAVITY_JUMP_SPEC),)
            own.consumable_clock = self.epoch
            raw_shells = account.get('shells')
            if raw_shells is not None:
                if not isinstance(raw_shells,(list,tuple)):
                    raise ValueError('Invalid ammunition pairs')
                own.loadout_shells = tuple(raw_shells)
            self._configure_ammunition(own)
            if assets is not None:entity_assets[player.vehicle_id] = assets
        try:
            if solo or bot_roster:
                own = self.vehicles[next(iter(self.vehicles))]
                if solo:
                    from .map_catalog import map_team_capacity
                    capacity = map_team_capacity(self.map_id)
                    enemies = min(capacity, training.enemies)
                    allies = min(capacity-1, training.active_allies)
                    slots = [(2, i) for i in range(capacity if self.practice else enemies)]
                    slots += [(1, i+1) for i in range(allies)]
                    if bot_roster is None and bot_enabled and bot_game is not None and own.definition is not None:
                        from .bot_roster import select_bot_roster
                        bot_roster = select_bot_roster(bot_game, own.definition,
                            enemies=enemies, allies=allies, seed=self.result_id)
                else:
                    slots = [(team, slot) for team in (2, 1)
                             for slot in range(slots[team], slots[team] +
                                               sum(row.team == team for row in bot_roster))]
                if bot_roster:
                    from .bot_roster import BotVehicle
                    self.bot_roster = tuple(bot_roster)
                    if (not bot_enabled or any(not isinstance(row, BotVehicle) for row in self.bot_roster)
                            or [(row.team, row.slot) for row in self.bot_roster] != slots):
                        raise ValueError('Bot vehicle recipe does not match the training roster')
                    if not build_world and own.definition is not None:
                        own.health = own.max_health = own.definition.health
                        own.gun = GunAim(own.definition.aiming)
                asset_cache = [(vehicle.definition, entity_assets.get(vehicle.entity_id))
                               for vehicle in self.vehicles.values()]
                bot_names = ()
                if bot_enabled:
                    from .bot_roster import BOT_NICKNAMES
                    occupied = {vehicle.nickname.casefold() for vehicle in self.vehicles.values()}
                    # Separate stream keeps names identical in the owner and
                    # mirror without consuming physics or combat randomness.
                    bot_names = random.Random(self.result_id + ':bot-names').sample(
                        [name for name in BOT_NICKNAMES if name.casefold() not in occupied], len(slots))
                if self.practice:
                    practice_definitions = {0: own.definition}
                    if bot_game is not None:
                        from .bot_roster import stock_vehicle
                        for choice, (nation, tank, _) in enumerate(sandbox.PRACTICE_TANKS, 1):
                            stock = stock_vehicle(bot_game, nation, tank)
                            if stock is not None:
                                practice_definitions[choice] = stock
                    practice_choices = tuple(practice_definitions)
                for index, (team, slot) in enumerate(slots):
                    eid = entity_base+200+len(accounts)+index
                    x,z = training_spawn(map_spec,team,slot)
                    yaw = training_yaw(map_spec,team,slot) if bot_enabled else 0.
                    pose = Pose((x,terrain.height(x,z),z),yaw)
                    name = bot_names[index] if bot_enabled else 'Учебная мишень'
                    if not bot_enabled and len(slots) > 1:
                        name += f' {slot+1}'
                    definition = self.bot_roster[index].definition if self.bot_roster else own.definition
                    if self.practice:
                        choice = practice_choices[index % len(practice_choices)]
                        self.practice_types.setdefault(choice, []).append(eid)
                        definition = practice_definitions[choice]
                        if choice:
                            name = sandbox.PRACTICE_TANKS[choice-1][2]
                    actual_descriptor = definition.descriptor if definition is not None else own.descriptor
                    target = self.vehicles[eid] = Vehicle(eid,None,name,team,actual_descriptor,pose)
                    if self.practice:
                        self.practice_slots.add(eid)
                        target.health = 0
                    if bot_enabled:
                        ai = self.training_bots[eid] = TrainingBot(eid, training.difficulty)
                        if self.training_bot is None:
                            self.training_bot = ai
                    target.definition = definition
                    if definition == own.definition:
                        target.movable_armor_state = own.movable_armor_state
                    assets = next((scene for entry,scene in asset_cache if entry == definition), None)
                    if assets is None and collision_scene is not None and build_world:
                        if bot_game is None:
                            raise ValueError('A different NPC vehicle requires its installed game assets')
                        from collections import ChainMap
                        from .mesh_files import MeshFiles
                        assets = collision_scene.for_vehicle(bot_game, definition)
                        # Share map arrays, but own the disposable vehicle OBJ
                        # files. A long-lived endpoint map cache must not grow
                        # for every random NPC type used by later battles.
                        assets.mesh_files = MeshFiles()
                        # Replica preparation asks every vehicle scene for
                        # static partitions too. Borrow those map paths and
                        # observe later primary-cache fills; all new writes
                        # remain in this scene's private first mapping.
                        assets.mesh_files.partitions = ChainMap({}, collision_scene.mesh_files.partitions)
                        assets.terrain_meshes = ChainMap({}, collision_scene.terrain_meshes)
                        assets._owns_mesh_files = True
                        self._owned_bot_scenes.append(assets)
                        self._configure_bot_scene(assets, definition)
                        asset_cache.append((definition, assets))
                    if assets is not None:
                        entity_assets[eid] = assets
                    if self.bot_roster and definition is not None:
                        # The no-world owner mirror must carry the same selected
                        # ammo, HP and gun definition before its child starts.
                        from types import SimpleNamespace
                        metadata = assets or SimpleNamespace(definition=definition)
                        if assets is None:
                            self._configure_bot_scene(metadata, definition)
                        target.health = target.max_health = definition.health
                        target.gun = GunAim(definition.aiming)
                        self._configure_tracks(target)
                        self._configure_gun(target)
                        self._configure_engine(target, metadata)
                        self._configure_ammo_rack(target, metadata)
                        self._configure_fuel_fire(target, metadata)
                        self._configure_crew(target, metadata)
                        self._configure_ammunition(target)
        except BaseException:
            for resource in self._owned_bot_scenes:
                resource.close()
            self._owned_bot_scenes.clear()
            raise
        # Owner-backed parent mirrors need the real vehicle definitions for
        # ammo validation; only the authoritative child builds a Bullet world.
        try:
            if collision_scene is not None and build_world:
                self.world = CollisionWorld(collision_scene,terrain,
                        vehicles={e:v.pose for e,v in self.vehicles.items()},incremental=incremental,
                        vehicle_assets=entity_assets, visibility=bool(self.training_bots),
                        parallel_native_build=parallel_native_build,
                        gravity=2.5 if self.gravity_mode else CollisionWorld.GRAVITY)
                self.building = True
                if self.world.build_ready:
                    while not self.advance_build():
                        if self._incremental_loading:break
        except BaseException:
            if self.world is not None:
                self.world.close()
            for resource in self._owned_bot_scenes:
                resource.close()
            self._owned_bot_scenes.clear()
            raise
        if self.training_bots and self.bot_director is None:
            from .bot_ai import BotDirector
            self.bot_director = BotDirector(self)

    def _configure_bot_scene(self, scene, definition):
        from .bot_roster import BOT_SCENE_FIELDS
        settings = next((values for entry,values in self.bot_scene_settings if entry == definition), {})
        if not isinstance(settings, dict) or set(settings)-set(BOT_SCENE_FIELDS):
            raise ValueError('Invalid NPC scene settings')
        for name in BOT_SCENE_FIELDS:
            setattr(scene, name, settings.get(name))

    def advance_build(self):
        if self.closed or self.retiring:
            raise ValueError('Arena is closed')
        if not self.building:
            return True
        if not self.world.advance_build():
            return False
        if not self._vehicles_configured:
            for entity,vehicle in self.vehicles.items():
                scene = self.world.assets_for(entity)
                vehicle.pose = self.world.poses[entity]
                self._configure_battle_vehicle(vehicle, scene)
                if entity in self.practice_slots:
                    self._park_practice_target(vehicle)
            self._vehicles_configured = True
        if self.training_bots:
            from .bot_ai import BotDirector
            from .bot_navigation import Navigation
            if self.bot_director is None:self.bot_director=BotDirector(self)
            if self.bot_director.navigation is None:
                if self._bot_navigation_builder is None:
                    self._navigation_build_started = time.monotonic()
                    self._navigation_cpu_ms = 0.
                    goals={eid:self.bot_director.patrol_goal(self.vehicles[eid],self.training_bots[eid])
                           for eid in self.training_bots}
                    self._bot_navigation_builder=Navigation.build(self.world,self.training_bots,goals)
                cpu = time.thread_time()
                try:
                    next(self._bot_navigation_builder)
                    return False
                except StopIteration as complete:
                    self.bot_director.navigation=complete.value
                    self._bot_navigation_builder=None
                    self.navigation_build_ms = (time.monotonic()-self._navigation_build_started)*1000
                finally:
                    self._navigation_cpu_ms += (time.thread_time()-cpu)*1000
        # Replica construction belongs to loading for both build modes. A
        # synchronous caller otherwise pays build_spec/spawn on its first
        # live physics step (55 ms in the Fort/14 startup trace).
        self._ensure_drive_workers()
        if self.drive_workers is not None:
            self.drive_workers.poll()
            # A ready world can start immediately. The regular drive path
            # runs on the owner until speculative workers become ready.
        self.building = False
        return True

    def _configure_battle_vehicle(self, vehicle, scene):
        vehicle.health = vehicle.max_health = scene.armor.health
        vehicle.definition = scene.definition
        vehicle.gun = GunAim(vehicle.definition.aiming)
        self.protected_specs.pop(vehicle.entity_id, None)
        self._configure_tracks(vehicle)
        self._configure_gun(vehicle)
        self._configure_engine(vehicle, scene)
        self._configure_ammo_rack(vehicle, scene)
        self._configure_fuel_fire(vehicle, scene)
        self._configure_crew(vehicle, scene)
        self._configure_ammunition(vehicle)

    def _configure_tracks(self, vehicle):
        spec = getattr(vehicle.definition, 'track_spec', None)
        vehicle.tracks = {part: TrackState(spec) for part in _TRACK_EXTRAS} if spec else {}

    def _configure_gun(self, vehicle):
        spec = getattr(vehicle.definition, 'gun_spec', None)
        if spec:
            self.guns[vehicle.entity_id] = GunState(spec)
        else:
            self.guns.pop(vehicle.entity_id, None)

    def _protected(self, vehicle, key, spec, factor_name):
        """Return a private spec with its projectile chance scaled once."""
        definition = getattr(vehicle, 'definition', None)
        protection = getattr(definition, 'module_protection', None)
        factor = getattr(protection, factor_name, 1.)
        chance = getattr(spec, 'projectile_chance', None)
        if factor == 1 or chance is None:
            return spec
        derived = dataclasses.replace(spec, projectile_chance=chance*factor)
        self.protected_specs.setdefault(vehicle.entity_id, {})[key] = (spec, derived)
        return derived

    def _scene_spec(self, vehicle, key, spec):
        """Map a derived live spec back to its immutable scene source."""
        source, derived = self.protected_specs.get(vehicle.entity_id, {}).get(key, (spec, spec))
        return source if derived is spec else spec

    def _configure_engine(self, vehicle, scene):
        """Copy an opt-in immutable scene spec into independent live HP."""
        spec = getattr(scene, 'engine_spec', None)
        if spec is not None:
            if spec.extra_index != _ENGINE_EXTRA:
                raise ValueError('Experimental engine spec must use extra index 31')
            self.engines[vehicle.entity_id] = EngineState(
                self._protected(vehicle, 'engine', spec, 'engine_chance_factor'))
        else:
            self.engines.pop(vehicle.entity_id, None)

    def _configure_ammo_rack(self, vehicle, scene):
        """Copy an opt-in immutable scene spec into independent live rack HP."""
        spec = getattr(scene, 'ammo_rack_spec', None)
        if spec is not None:
            if spec.extra_index != _AMMO_RACK_EXTRA:
                raise ValueError('Experimental ammo rack spec must use extra index 32')
            rack = AmmoRackState(
                self._protected(vehicle, 'ammo', spec, 'ammo_rack_chance_factor'))
            self.ammo_racks[vehicle.entity_id] = rack
            self.applied_ammo_reload_factors[vehicle.entity_id] = rack.reload_speed_factor
        else:
            self.ammo_racks.pop(vehicle.entity_id, None)
            self.applied_ammo_reload_factors.pop(vehicle.entity_id, None)

    def _configure_fuel_fire(self, vehicle, scene):
        spec = getattr(scene, 'fuel_tank_spec', None)
        if spec is not None:
            if spec.extra_index != _FUEL_EXTRA:
                raise ValueError('Experimental fuel spec must use extra index 33')
            self.fuels[vehicle.entity_id] = FuelTankState(spec)
        else:
            self.fuels.pop(vehicle.entity_id, None)
        policy = getattr(scene, 'fire_policy', None)
        if policy is not None:
            minimum = getattr(scene, 'fire_min_damage', None)
            if (not isinstance(policy, FirePolicy) or isinstance(minimum, bool)
                    or not isinstance(minimum, (int, float)) or not math.isfinite(minimum)
                    or minimum < 0):
                raise ValueError('Experimental fire requires a policy and minimum engine damage')
            self.fires[vehicle.entity_id] = FireState(policy)
            self.fire_min_damage[vehicle.entity_id] = minimum
        else:
            self.fires.pop(vehicle.entity_id, None)
            self.fire_min_damage.pop(vehicle.entity_id, None)
        self.fire_hp_carry[vehicle.entity_id] = 0.
        self.fire_tick_indices[vehicle.entity_id] = 0
        self.pending_ignitions.pop(vehicle.entity_id, None)
        self.announced_fires.discard(vehicle.entity_id)

    def _configure_crew(self, vehicle, scene):
        """Fresh boolean injury per entity for explicit opt-in scene bindings.

        Scenes without both crew_specs and crew_bindings keep crew disabled.
        A binding must name a mesh of the attached experimental geometry, a
        unique damageable person whose primary holder matches the mesh role,
        and a native-verified Health extra. Crew HP is never invented.
        """
        self.crews.pop(vehicle.entity_id, None)
        self.applied_crew_reload_factors.pop(vehicle.entity_id, None)
        specs = getattr(scene, 'crew_specs', None)
        bindings = getattr(scene, 'crew_bindings', None)
        if specs is None and bindings is None:
            return
        model = getattr(scene, 'internal_geometry', None)
        if (not isinstance(model, PreparedInternalGeometry) or not isinstance(specs, tuple)
                or not isinstance(bindings, tuple) or not bindings
                or any(not isinstance(spec, CrewSpec) for spec in specs)
                or len({spec.index for spec in specs}) != len(specs)):
            raise ValueError('Experimental crew requires geometry, unique specs and bindings')
        by_index = {spec.index: spec for spec in specs}
        meshes = {mesh.name for mesh in model.geometry.meshes}
        expected = dict(crew_bindings_for_meshes(specs,
                        tuple(sorted(mesh for mesh in meshes if mesh.startswith('crew_'))), _CREW_EXTRAS))
        seen = set()
        for binding in bindings:
            if not isinstance(binding, tuple) or len(binding) != 2:
                raise ValueError('Invalid experimental crew binding')
            mesh, index = binding
            spec = by_index.get(index) if type(index) is int else None
            if (not isinstance(mesh, str) or mesh not in meshes or not mesh.startswith('crew_')
                    or spec is None or not spec.damageable or expected.get(mesh) != index
                    or spec.material is None or spec.extra_health_index not in _CREW_EXTRAS
                    or mesh in seen or index in seen):
                raise ValueError('Invalid experimental crew binding')
            seen.update((mesh, index))
        self.crews[vehicle.entity_id] = {
            spec.index: CrewState(self._protected(vehicle, ('crew', spec.index), spec,
                                                   'crew_chance_factor'))
            for spec in specs}

    def _bound_crew(self, vehicle, scene):
        """mesh -> live CrewState, only for this scene's exact immutable specs."""
        crew = self.crews.get(vehicle.entity_id)
        if not crew:
            return {}
        specs = {spec.index: spec for spec in getattr(scene, 'crew_specs', None) or ()}
        return {mesh: crew[index] for mesh,index in getattr(scene, 'crew_bindings', None) or ()
                if index in crew and self._scene_spec(vehicle, ('crew', index), crew[index].spec)
                is specs.get(index)}

    def injured_crew(self, vehicle):
        return tuple(state for state in self.crews.get(vehicle.entity_id, {}).values() if state.injured)

    def _crew_role_factor(self, vehicle, role):
        # game support documents half reload/turret traverse speed for a
        # wounded loader/gunner. Combined roles belong to one person. LOCAL
        # multi-holder boundary: a role penalty is binary, never compounded
        # into a quarter rate when two people have the same role.
        return .5 if any(role in person.spec.roles for person in self.injured_crew(vehicle)) else 1.

    def state_modifiers(self, vehicle):
        """Complete immutable supported state-modifier snapshot."""
        modifiers = [4] if self._fire_active(vehicle) else []
        modifiers.extend(extra for part,extra in _TRACK_EXTRAS.items()
                         if part in vehicle.tracks and vehicle.tracks[part].broken)
        return tuple(modifiers)

    def _fire_active(self, vehicle):
        fire = self.fires.get(vehicle.entity_id)
        return fire is not None and fire.active

    def track_repair_status(self, vehicle):
        """Native HUD completion percent and remaining natural repair seconds."""
        rows = []
        for part,extra in _TRACK_EXTRAS.items():
            state = vehicle.tracks.get(part)
            if state is None or not state.broken:
                continue
            threshold = state.spec.hysteresis_health
            health = max(0., min(threshold, state.health))
            progress = min(99, math.floor(100.*health/threshold))
            rows.append((extra, progress, (threshold-health)/state.spec.regen_per_sec))
        # The native HUD has one chassis progress control for both belts.
        # Movement resumes only once the longer remaining repair completes.
        return (max(rows, key=lambda row: row[2]),) if rows else ()

    def _fire_modifiers(self, vehicle):
        self.emit('state_modifiers', vehicle.entity_id, modifiers=self.state_modifiers(vehicle))

    def module_state(self, vehicle):
        """Immutable common-extra snapshots; module HP remains authoritative.

        Public crew state is preserved for observers and entity creation.
        The owner's private Health/UI additionally needs Avatar16; a vector
        alone does not enable the Medkit in the pinned client.
        """
        critical, destroyed = [], []
        engine = self.engines.get(vehicle.entity_id)
        if engine is not None and engine.destroyed:
            destroyed.append(_ENGINE_EXTRA)
        elif engine is not None and engine.critical:
            critical.append(_ENGINE_EXTRA)
        rack = self.ammo_racks.get(vehicle.entity_id)
        if rack is not None and rack.destroyed:
            destroyed.append(_AMMO_RACK_EXTRA)
        elif rack is not None and rack.critical:
            critical.append(_AMMO_RACK_EXTRA)
        fuel = self.fuels.get(vehicle.entity_id)
        if fuel is not None and (fuel.critical or fuel.destroyed):
            critical.append(_FUEL_EXTRA)
        critical.extend(_TRACK_EXTRAS[p] for p,s in vehicle.tracks.items() if s.critical and not s.broken)
        destroyed.extend(_TRACK_EXTRAS[p] for p,s in vehicle.tracks.items() if s.broken)
        gun = self.guns.get(vehicle.entity_id)
        if gun is not None and gun.destroyed:
            destroyed.append(_GUN_EXTRA)
        elif gun is not None and gun.critical:
            critical.append(_GUN_EXTRA)
        critical.extend(person.spec.extra_health_index for person in self.injured_crew(vehicle))
        return tuple(critical), tuple(destroyed)

    def gun_destroyed(self, vehicle):
        gun = self.guns.get(vehicle.entity_id)
        return gun is not None and gun.destroyed

    def _module_changed(self, vehicle, part, reason, now, *, sync_track_modifiers=True,
                        track_just_broke=False):
        if part == _ENGINE_EXTRA:
            extra,health = _ENGINE_EXTRA,self.engines[vehicle.entity_id].health
        elif part == _AMMO_RACK_EXTRA:
            extra,health = _AMMO_RACK_EXTRA,self.ammo_racks[vehicle.entity_id].health
        elif part == _FUEL_EXTRA:
            extra,health = _FUEL_EXTRA,self.fuels[vehicle.entity_id].health
        elif part == _GUN_EXTRA:
            extra,health = _GUN_EXTRA,self.guns[vehicle.entity_id].health
        else:
            extra,health = _TRACK_EXTRAS[part],vehicle.tracks[part].health
        critical,destroyed = self.module_state(vehicle)
        track_broken = part in _TRACK_EXTRAS and vehicle.tracks[part].broken
        if part in _TRACK_EXTRAS and not track_broken:
            vehicle.track_assist_sources.pop(part,None)
        repair_status = self.track_repair_status(vehicle) if part in _TRACK_EXTRAS else ()
        previous_repair = self._track_repair_hud_extra.get(vehicle.entity_id)
        next_repair = repair_status[0][0] if repair_status else None
        if track_broken:
            # The native TrackHealth extra must be active before DeviceHealth
            # listeners receive the destroyed-module notification.
            self._fire_modifiers(vehicle)
        if part in _TRACK_EXTRAS and previous_repair is not None and previous_repair != next_repair:
            # The native HUD shares one chassis control between both belts.
            # Close its cached progress before selecting the other belt. At
            # 100% its initial/final rectangles are identical; use one finite
            # frame because the native animation tick divides by duration.
            self.emit('module_repair', vehicle.entity_id, extra=previous_repair,
                      progress=100, duration=self.STEP, at=now)
        self.emit('module_state', vehicle.entity_id, extra=extra, reason=reason,
                  critical=critical, destroyed=destroyed, health=health, at=now,
                  track_just_broke=track_just_broke)
        if part in _TRACK_EXTRAS:
            for repair_extra,progress,duration in repair_status:
                self.emit('module_repair', vehicle.entity_id, extra=repair_extra,
                          progress=progress, duration=duration, at=now)
            if next_repair is None:
                self._track_repair_hud_extra.pop(vehicle.entity_id, None)
            else:
                self._track_repair_hud_extra[vehicle.entity_id] = next_repair
        if part in _TRACK_EXTRAS and not track_broken and sync_track_modifiers:
            # DeviceHealth vectors/Avatar16 own HUD markers, while property7
            # starts and stops the actual TrackHealthEntityExtra animation.
            # Finish the repair notification before stopping the extra; the
            # complete snapshot preserves fire and any other torn belt.
            self._fire_modifiers(vehicle)

    def _crew_changed(self, vehicle, person, reason, now):
        """Owner Avatar16 for one person, with the full unchanged module snapshot."""
        # Ballistics may discover a contact inside an already advanced 60Hz
        # step. Effects start at the current effect clock, never rewind work.
        self._consumables(vehicle, max(vehicle.consumable_clock, now))
        critical,destroyed = self.module_state(vehicle)
        self.emit('module_state', vehicle.entity_id, extra=person.spec.extra_health_index,
                  reason=reason, critical=critical, destroyed=destroyed,
                  person=person.spec.index, injured=person.injured, at=now)

    def _heal_crew(self, vehicle, now):
        """Medkit: clear every injured person once; HP and modules are untouched."""
        # Finish expiries/refills under the old role state before changing it.
        self._consumables(vehicle, max(vehicle.consumable_clock, now))
        for person in self.injured_crew(vehicle):
            if person.heal():
                self._crew_changed(vehicle, person, 19, now)

    def _crew_contact(self, event, target, person, contact, path, source):
        """One boolean injury check; return whether the internal trace continues.

        The selected XML projectile chance is drawn once from the module
        stream; 0 never injures and 1 always does, both without a draw. An
        already injured person is not checked or injured again. LOCAL trace
        policy, not a recovered official rule: an injury continues the path
        like a damaged module; a failed or skipped check continues only if
        the person's pinned crew material sets continueTraceIfNoHit true.
        """
        spec = person.spec
        chance = spec.projectile_chance
        if (not person.injured and chance > 0
                and (chance >= 1 or self.module_rng.random() < chance)):
            self._consumables(target, max(target.consumable_clock, event.at))
            if not person.injure():
                return spec.continue_trace_if_no_hit is True
            self.emit('crew_hit',event.shooter,target.entity_id,shot=event.shot,
                      person=spec.holder,index=spec.index,roles=spec.roles,material=spec.material,
                      extra=spec.extra_health_index,mesh=contact.mesh,point=contact.point,
                      distance=contact.distance,path=path,source_classification=source,at=event.at)
            self._crew_changed(target, person, 0, event.at)
            return True
        return spec.continue_trace_if_no_hit is True

    def _advance_tracks(self, vehicle, now):
        changed = [part for part,state in vehicle.tracks.items() if state.advance(now)]
        for part in changed:
            self._module_changed(vehicle, part, 18 if vehicle.tracks[part].critical else 19, now,
                                 sync_track_modifiers=False)
        if changed:
            self._fire_modifiers(vehicle)

    def _gun_contact(self, contact, target, gun, at):
        """Sandbox: chance draw, then nominal device damage. No natural recovery.

        chance 0 never damages and 1 always damages, without drawing. Hull HP,
        hits and kills are untouched; zero dispersion and reload are unchanged.
        """
        chance = gun.spec.projectile_chance
        if gun.destroyed or chance <= 0 or chance < 1 and self.module_rng.random() >= chance:
            return
        before = gun.critical,gun.destroyed
        applied = gun.damage(contact.shell.damage_devices)
        self.emit('module_hit',contact.shooter,contact.target,shot=contact.shot,
                  part=contact.hit.part,damage=applied,health=gun.health,at=at)
        if before != (gun.critical,gun.destroyed):
            self._module_changed(target,_GUN_EXTRA,1 if gun.destroyed else 0,at)

    def _internal_candidate(self, event):
        """Whether this direct penetration can start an opt-in module trace."""
        shell = event.shell
        hit = event.result.hit
        target = self.vehicles.get(event.target)
        shooter = self.vehicles.get(event.shooter)
        return (event.shot not in self.resolved_shots and self.world is not None
                and shell is not None and shell.kind in _INTERNAL_KINDS and shell.damage_devices > 0
                and event.result.outcome == 'penetrated' and hit is not None and not hit.external
                and getattr(event, 'relative_direction', None) is not None
                and target is not None and shooter is not None and target.alive
                and target.team != shooter.team
                # Internal-volume transforms have not accepted the rigid
                # quaternion path yet. A flying target may take verified armor
                # damage, but may not gain speculative module damage through
                # a canonical Euler fallback.
                and target.entity_id not in getattr(self.world, 'dynamic_bodies', ()))

    def _internal_contact(self, event, state, now):
        """Run selected experimental engine/ammo/fuel/crew volumes in path order.

        A preceding unimplemented volume, including an unbound crew mesh,
        blocks this sandbox stage. This avoids letting an imported path pass
        through it to damage a later selected module as though the earlier
        volume had no effect. Bound crew follow _crew_contact. Returns
        whether an ammo-rack detonation must kill the target.
        """
        target = self.vehicles[event.target]
        scene = self.world.assets_for(target.entity_id)
        engine = self.engines.get(target.entity_id)
        rack = self.ammo_racks.get(target.entity_id)
        fuel = self.fuels.get(target.entity_id)
        model = getattr(scene, 'internal_geometry', None)
        if (not isinstance(model, PreparedInternalGeometry)
                or model.geometry.configuration != GeometryConfiguration.from_definition(scene.definition)):
            return False
        selected = {}
        if (engine is not None and getattr(scene, 'engine_spec', None)
                is self._scene_spec(target, 'engine', engine.spec)):
            selected['engine'] = engine,_ENGINE_EXTRA
        if (rack is not None and getattr(scene, 'ammo_rack_spec', None)
                is self._scene_spec(target, 'ammo', rack.spec)):
            selected['ammo'] = rack,_AMMO_RACK_EXTRA
        if fuel is not None and getattr(scene, 'fuel_tank_spec', None) is fuel.spec:
            selected['fuel'] = fuel,_FUEL_EXTRA
        crew = self._bound_crew(target, scene)
        if not selected and not crew:
            return False
        start = tuple(float(value) for value in event.point)
        length = 10 * event.shell.caliber / 1000
        end = tuple(point + length * direction for point,direction in zip(start,event.relative_direction))
        contacts = tuple(sorted(model.volume_segment(start,end,state.pose,state.turret,state.pitch),
                                key=lambda contact:(contact.distance,contact.mesh)))
        source = model.geometry.source_classification
        self.emit('internal_module_trace',event.shooter,target.entity_id,shot=event.shot,
                  state=dataclasses.asdict(state),start=start,end=end,
                  source_classification=source,
                  contacts=tuple(dataclasses.asdict(contact) for contact in contacts),at=event.at)
        hit_meshes = set()
        # The client permits at most ceil(crew size / 2) new injuries from one
        # shell (support/wotb/article/15901). Count XML people, including an
        # unbound radioman, rather than roles or reachable imported meshes.
        crew_limit = (len(self.crews.get(target.entity_id, {})) + 1) // 2
        injured_this_shot = 0
        for index,contact in enumerate(contacts):
            path = tuple(item.mesh for item in contacts[:index+1])
            person = crew.get(contact.mesh)
            if person is not None:
                if contact.mesh in hit_meshes:
                    return False
                hit_meshes.add(contact.mesh)
                if injured_this_shot >= crew_limit:
                    # A capped check is a skipped check: same trace policy.
                    if person.spec.continue_trace_if_no_hit is not True:
                        return False
                    continue
                injured_before = person.injured
                continues = self._crew_contact(event, target, person, contact, path, source)
                injured_this_shot += int(not injured_before and person.injured)
                if not continues:
                    return False
                continue
            selected_state = selected.get(contact.mesh)
            if selected_state is None:
                self.emit('internal_module_deferred',event.shooter,target.entity_id,shot=event.shot,
                          mesh=contact.mesh,point=contact.point,distance=contact.distance,
                          path=path,source_classification=source,at=event.at)
                return False
            module,extra = selected_state
            # Volume queries merge a module's independently anchored volumes.
            # Do not apply device damage twice if a future adapter reports
            # one more than once.
            if contact.mesh in hit_meshes or module.destroyed:
                return False
            hit_meshes.add(contact.mesh)
            chance = module.spec.projectile_chance
            if chance <= 0 or chance < 1 and self.module_rng.random() >= chance:
                return False
            before = module.critical,module.destroyed
            before_reload = module.reload_speed_factor if module is rack else None
            applied = module.damage(event.shell.damage_devices)
            self.emit('module_hit',event.shooter,target.entity_id,shot=event.shot,
                      part=contact.mesh,damage=applied,health=module.health,point=contact.point,
                      path=path,source_classification=source,at=event.at)
            if before != (module.critical,module.destroyed):
                self._module_changed(target,extra,1 if module.destroyed and module is not fuel else 0,event.at)
            if module is fuel and module.destroyed and not before[1]:
                # LOCAL: only a fresh transition to zero ignites. Further hits
                # on this destroyed tank cannot reignite until it is repaired.
                self._queue_ignition(target, event.shooter,
                                     max(self.module_clock, min(now, event.at)), 'fuel')
            elif (module is engine and applied >= self.fire_min_damage.get(target.entity_id, math.inf)
                  and self._can_ignite(target)):
                chance = engine.spec.fire_starting_chance
                if chance > 0 and (chance >= 1 or self.module_rng.random() < chance):
                    self._queue_ignition(target, event.shooter,
                                         max(self.module_clock, min(now, event.at)), 'engine')
            if module is rack:
                if module.destroyed:
                    self.emit('ammo_rack_detonated',event.shooter,target.entity_id,shot=event.shot,
                              point=contact.point,path=path,source_classification=source,at=event.at)
                    return True
                if module.reload_speed_factor != before_reload:
                    # _fixed_step has already advanced consumables to its end
                    # timestamp. Do not rewind that clock to event.at; apply
                    # remaining full-reload/pump work from this tick onward.
                    self._consumables(target, now)
        return False

    def _can_ignite(self, vehicle):
        fire = self.fires.get(vehicle.entity_id)
        return (vehicle.alive and fire is not None and not fire.active
                and vehicle.entity_id not in self.pending_ignitions)

    def _queue_ignition(self, vehicle, source, at, cause):
        if self._can_ignite(vehicle):
            self.pending_ignitions[vehicle.entity_id] = (source, max(self.module_clock, at), cause)

    def _start_pending_fire(self, vehicle):
        pending = self.pending_ignitions.pop(vehicle.entity_id, None)
        if pending is None or not vehicle.alive or self.finished_at:
            return
        source, at, cause = pending
        fire = self.fires.get(vehicle.entity_id)
        if fire is None or not fire.start(at, source):
            return
        self.fire_hp_carry[vehicle.entity_id] = 0.
        self.fire_tick_indices[vehicle.entity_id] = 0
        self.announced_fires.add(vehicle.entity_id)
        self.emit('fire_started', source, vehicle.entity_id, cause=cause, at=at)
        self._fire_modifiers(vehicle)
        # Automatic activation never calls the public entry point (which
        # steps the arena). It consumes one ready installed item per fire.
        state = next((s for s in vehicle.consumables if s and s.spec.action == 'extinguish'
                      and s.spec.automatic and (not s.uses or at >= s.ready_at)), None)
        if state is not None:
            self._activate_consumable(vehicle, state, at, fire_advanced=True)

    def _announce_fire_stopped(self, vehicle, at, reason):
        if vehicle.entity_id not in self.announced_fires:
            return
        self.announced_fires.remove(vehicle.entity_id)
        self.emit('fire_stopped', self.fires[vehicle.entity_id].source,
                  vehicle.entity_id, reason=reason, at=at)
        self._fire_modifiers(vehicle)

    def _stop_fire(self, vehicle, at, reason, *, fire_advanced=False):
        fire = self.fires.get(vehicle.entity_id)
        self.pending_ignitions.pop(vehicle.entity_id, None)
        if fire is None:
            return
        if not fire_advanced and vehicle.entity_id not in self.advancing_fires:
            self._advance_fire(vehicle, at)
        # The caller has processed all due ticks through this exact clock.
        # During a tick-induced death, the kernel is already at that tick.
        fire.advance(at)
        fire.stop(at)
        self._announce_fire_stopped(vehicle, at, reason)

    def _advance_fire(self, vehicle, until):
        """Exact due ticks, with LOCAL fire-before-projectile same-time ties.

        Advance one grid boundary at a time so death on an early tick cannot
        pre-advance the kernel beyond its actual death/stop time. LOCAL module
        burn damage targets only an existing ammo rack: never radio or crew.
        LOCAL automatic extinguishers also activate at their exact readiness
        boundary during a live fire. A coincident burn tick resolves first;
        death or natural expiry on that tick prevents spending the item.
        """
        fire = self.fires.get(vehicle.entity_id)
        if fire is None or not fire.active:
            return
        self.advancing_fires.add(vehicle.entity_id)
        try:
            while fire.active and vehicle.alive:
                index = self.fire_tick_indices[vehicle.entity_id] + 1
                due = fire.started_at + min(index * fire.policy.tick_interval, fire.policy.duration)
                automatic = [(max(fire.started_at, s.ready_at) if s.uses else fire.started_at, s)
                             for s in vehicle.consumables if s and s.spec.action == 'extinguish'
                             and s.spec.automatic] if self.started and not self.finished_at else []
                ready, state = min(automatic, key=lambda pair: pair[0]) if automatic else (math.inf, None)
                if ready < due and ready <= until:
                    # No burn boundary is crossed here. The explicit flag
                    # prevents activation from recursively advancing this fire.
                    fire.advance(ready)
                    self._activate_consumable(vehicle, state, ready, fire_advanced=True)
                    continue
                if due > until:
                    break
                self.fire_tick_indices[vehicle.entity_id] = index
                for tick in fire.advance(due):
                    total = self.fire_hp_carry[vehicle.entity_id] + tick.hp_damage
                    whole = math.floor(total)
                    self.fire_hp_carry[vehicle.entity_id] = total - whole
                    damage = self._damage_health(vehicle, whole, fire.source, tick.at, shell_hit=False)
                    self.emit('fire_tick', fire.source, vehicle.entity_id,
                              hp_damage=damage, nominal_hp_damage=tick.hp_damage,
                              module_damage=tick.module_damage, at=tick.at)
                    rack = self.ammo_racks.get(vehicle.entity_id)
                    if vehicle.alive and rack is not None and not rack.destroyed and tick.module_damage > 0:
                        before = rack.critical, rack.destroyed
                        before_reload = rack.reload_speed_factor
                        applied = rack.damage(tick.module_damage)
                        self.emit('module_hit', fire.source, vehicle.entity_id, part='ammo',
                                  damage=applied, health=rack.health, cause='fire', at=tick.at)
                        if before != (rack.critical, rack.destroyed):
                            self._module_changed(vehicle, _AMMO_RACK_EXTRA, 1 if rack.destroyed else 0, tick.at)
                        if rack.destroyed:
                            self.emit('ammo_rack_detonated', fire.source, vehicle.entity_id,
                                      cause='fire', at=tick.at)
                            self._damage_health(vehicle, vehicle.health, fire.source, tick.at, shell_hit=False)
                        elif before_reload != rack.reload_speed_factor:
                            self._consumables(vehicle, max(vehicle.consumable_clock, self.simulation_time))
                if fire.active and vehicle.alive and ready == due:
                    self._activate_consumable(vehicle, state, ready, fire_advanced=True)
                if not fire.active:
                    self._announce_fire_stopped(vehicle, due, 'expired')
            if fire.active:
                # Move the monotonic clock without generating a fractional tick.
                fire.advance(until)
        finally:
            self.advancing_fires.remove(vehicle.entity_id)

    def _track_contact(self, contact, now):
        target = self.vehicles.get(contact.target)
        shooter = self.vehicles.get(contact.shooter)
        if self.recon is not None:
            self._recon_impact(contact.shot, shooter, target, contact.at)
        if (not contact.penetrated or target is None or shooter is None or not target.alive
                or target.team == shooter.team):
            return
        gun = self.guns.get(target.entity_id)
        if (contact.hit.part not in target.tracks and gun is not None
                and contact.hit.part == target.definition.gun.geometry):
            if contact.shell.damage_devices > 0:
                self._gun_contact(contact,target,gun,max(self.module_clock,min(now,contact.at)))
            return
        if contact.hit.part not in target.tracks:
            return
        damage = contact.shell.damage_devices
        if damage <= 0:
            return
        # Projectile substeps accumulate floating-point time independently of
        # the fixed grid. Clip only that numerical boundary drift.
        at = max(self.module_clock, min(now, contact.at))
        self._advance_tracks(target, at)
        state = target.tracks[contact.hit.part]
        before = state.broken,state.critical
        applied = min(state.health,damage)
        state.damage(damage, at)
        if not before[0] and state.broken:
            target.track_assist_sources[contact.hit.part] = (contact.shooter,at)
        self.emit('module_hit',contact.shooter,contact.target,shot=contact.shot,
                  part=contact.hit.part,damage=applied,health=state.health,at=at)
        if before != (state.broken,state.critical) or state.broken and applied > 0:
            self._module_changed(target, contact.hit.part, 1 if state.broken else 0, at,
                                 track_just_broke=state.broken and not before[0])

    def _repair_tracks(self, vehicle, now):
        """Repair kit: full HP for supported local modules."""
        changed = [part for part,state in vehicle.tracks.items() if state.repair(now)]
        for part in changed:
            self._module_changed(vehicle,part,19,now,sync_track_modifiers=False)
        if changed:
            self._fire_modifiers(vehicle)
        gun = self.guns.get(vehicle.entity_id)
        if gun is not None and gun.repair():
            self._module_changed(vehicle,_GUN_EXTRA,19,now)
        engine = self.engines.get(vehicle.entity_id)
        if engine is not None and engine.health < engine.spec.max_health and engine.repair():
            self._module_changed(vehicle,_ENGINE_EXTRA,19,now)
        rack = self.ammo_racks.get(vehicle.entity_id)
        if rack is not None and rack.damaged and rack.repair():
            self._module_changed(vehicle,_AMMO_RACK_EXTRA,19,now)
        fuel = self.fuels.get(vehicle.entity_id)
        if fuel is not None and fuel.repair():
            self._module_changed(vehicle,_FUEL_EXTRA,19,now)

    def _full_reload_rate(self, vehicle):
        """Boost, rack and loader rates already represented in timer work."""
        return (vehicle.reload_speed_factor
                * self.applied_ammo_reload_factors.get(vehicle.entity_id, 1.)
                * self.applied_crew_reload_factors.get(vehicle.entity_id, 1.))

    def _reload_duration(self, vehicle, kind, nominal):
        if vehicle.sandbox_fast_reload:
            return sandbox.FAST_RELOAD_SECONDS
        if kind == 'interval':
            return nominal / vehicle.reload_speed_factor * vehicle.interval_factor
        return nominal / self._full_reload_rate(vehicle)

    def _set_fast_reload(self, vehicle, enabled):
        if vehicle.sandbox_fast_reload == enabled:
            return
        vehicle.sandbox_fast_reload = enabled
        now = self.simulation_time
        weapon = vehicle.definition.weapon if vehicle.definition else None
        kind = vehicle.reload_kind
        if not vehicle.burst_pending:
            if kind == 'interval':
                nominal = weapon.interval if weapon else .1
            else:
                nominal = weapon.reload if weapon else 6.
            duration = self._reload_duration(vehicle, kind, nominal)
            if vehicle.reload_at > now:
                vehicle.reload_at = now + (vehicle.reload_at-now) * duration / vehicle.reload_total
            vehicle.reload_total = duration
        if vehicle.pump_at > now:
            duration = self._reload_duration(vehicle, 'reload', vehicle.pump_times[vehicle.clip])
            vehicle.pump_at = now + (vehicle.pump_at-now) * duration / vehicle.pump_total
            vehicle.pump_total = duration
        if not vehicle.burst_pending:
            self.emit('reload', vehicle.entity_id, delay=max(0., vehicle.reload_at-now),
                      total=vehicle.reload_total)
        if vehicle.pump_times:
            if not vehicle.clip:
                self._emit_pump_gate(vehicle, now)
            self._emit_pump(vehicle, now)

    def _configure_ammunition(self, vehicle):
        """Copy the garage loadout; live counts never alias persistent data."""
        weapon = vehicle.definition.weapon if vehicle.definition else None
        shells = weapon.shells if weapon else ()
        default = shells[0].compact_id if shells else 513066
        capacity = weapon.ammo if weapon else 564
        supported = {s.compact_id for s in shells} or {default}
        allowed = (tuple(s.compact_id for s in weapon.ammunition) if weapon and weapon.ammunition
                   else tuple(s.compact_id for s in shells) or (default,))
        raw = vehicle.loadout_shells
        if raw is None:
            count = capacity if vehicle.loadout_ammo is None else vehicle.loadout_ammo
            raw = (default,count)
        if (not raw or len(raw)%2 or len(raw)>64 or
                any(type(n) is not int or n<0 for n in raw)):
            raise ValueError('Invalid ammunition pairs')
        counts = dict.fromkeys(allowed,0)
        seen = set()
        for cd,count in zip(raw[::2],raw[1::2]):
            if cd in seen or cd not in allowed or (count and cd not in supported):
                raise ValueError('Unsupported ammunition loadout')
            seen.add(cd);counts[cd] = count
        if sum(counts.values())>capacity:
            raise ValueError('Invalid vehicle ammunition loadout')
        vehicle.ammo_by_shell = counts
        vehicle.selected_shell = next((s.compact_id for s in shells
                                       if counts.get(s.compact_id,0)),default)
        vehicle.next_shell = 0
        vehicle.ammo = counts[vehicle.shell_id]
        vehicle.clip = self._loaded_clip(vehicle)
        vehicle.burst_shell = None
        vehicle.burst_effect = 0
        vehicle.burst_remaining = 0
        vehicle.burst_switch_shell = 0
        vehicle.reload_kind = 'reload'
        vehicle.reload_total = self._reload_duration(vehicle, 'reload', weapon.reload if weapon else 6.)
        if vehicle.pump_times and weapon:
            # A ready pump's next cycle is the intershot, never the full reload.
            vehicle.reload_kind = 'interval'
            vehicle.reload_total = self._reload_duration(vehicle, 'interval', weapon.interval)
        vehicle.pump_at = vehicle.pump_total = 0.
        vehicle.pump_waiting = False

    @staticmethod
    def _loaded_clip(vehicle):
        # A partly filled final magazine remains usable. Its sole round follows
        # the ordinary path rather than being stranded by a burst scheduler.
        return min(vehicle.clip_size,vehicle.ammo)

    def _cancel_burst(self, vehicle):
        """Drop scheduled burst rounds. Their unspent ammunition stays in the rack."""
        if vehicle.burst_pending:
            vehicle.burst_shell = None
            vehicle.burst_effect = 0
            vehicle.burst_remaining = 0
            vehicle.burst_switch_shell = 0
            vehicle.reload_at = 0.
            vehicle.reload_kind = 'reload'

    # Pump autoreloader (public game autoreloading-system article, XML times).
    # A shot leaving shells waits its intershot, then refills one; an empty
    # clip refills at once and its first shell can fire immediately. A shot
    # discards refill progress. Each refill takes times[clip] and only moves a
    # spare shell (ammo counts the magazine) into the clip: never free ammo.
    def _pump_segment(self, vehicle, start):
        times = vehicle.pump_times
        vehicle.pump_waiting = False
        if vehicle.clip < len(times) and vehicle.ammo > vehicle.clip:
            vehicle.pump_total = self._reload_duration(vehicle, 'reload', times[vehicle.clip])
            vehicle.pump_at = start+vehicle.pump_total
        else:
            vehicle.pump_at = vehicle.pump_total = 0.

    def _emit_pump(self, vehicle, now):
        """Immutable refill snapshot; readyGun is loaded with no intershot pending."""
        self.emit('pump_reload',vehicle.entity_id,
                  remaining=max(0.,vehicle.pump_at-now) if vehicle.pump_at else 0.,
                  total=vehicle.pump_total,clip=vehicle.clip,
                  readyGun=bool(vehicle.clip and not vehicle.reload_at and vehicle.alive
                                and not self.finished_at))

    def _emit_pump_gate(self, vehicle, now):
        # An empty pump clip fires when its first refill lands, never earlier.
        if vehicle.pump_at:
            self.emit('reload',vehicle.entity_id,delay=max(0.,vehicle.pump_at-now),total=vehicle.pump_total)

    def _advance_pump(self, vehicle, until, now):
        """Complete refills through until at their exact times, not tick ends."""
        if not vehicle.pump_times:
            return
        if vehicle.pump_waiting and vehicle.reload_at and vehicle.reload_at <= until:
            self._pump_segment(vehicle,vehicle.reload_at)
        while vehicle.pump_at and vehicle.pump_at <= until:
            at,total = vehicle.pump_at,vehicle.pump_total
            vehicle.clip += 1
            self._pump_segment(vehicle,at)
            self.emit('ammo',vehicle.entity_id)
            if vehicle.clip == 1 and not vehicle.reload_at:
                self.emit('reload',vehicle.entity_id,delay=0.,total=total)
            self._emit_pump(vehicle,now)

    def _stop_pump(self, vehicle):
        if vehicle.pump_at or vehicle.pump_waiting:
            vehicle.pump_at = vehicle.pump_total = 0.
            vehicle.pump_waiting = False
            self._emit_pump(vehicle,self.simulation_time)

    def emit(self, kind, actor=0, target=0, **data):
        if diagnostics.get_recorder() is not None:
            diagnostics.event(self, kind, actor, target, data)
        self.events.append(BattleEvent(kind,actor,target,data))
        if kind == 'module_hit' and data.get('damage', 0) > 0:
            vehicle = self.vehicles.get(target)
            if vehicle is not None:
                self._reset_base_capture(vehicle, actor)

    def _publish_base_capture(self):
        points, blocked = self.base_capture.view()
        self.emit('base_points', points=points, blocked=blocked,
                  base_id=self.base_capture.point.base_id)

    def _reset_base_capture(self, vehicle, source):
        supremacy = getattr(self, 'supremacy', None)
        if supremacy is not None:
            before = supremacy.state()
            dropped = supremacy.reset(vehicle.entity_id)
            shooter = self.vehicles.get(source)
            if (dropped and shooter is not None and shooter.team != vehicle.team
                    and any(point.contains(vehicle.pose.position) for point in supremacy.points)):
                shooter.defense_points += dropped
            if supremacy.state() != before:
                self.emit('supremacy', state=supremacy.state(), previous=before)
        capture = getattr(self, 'base_capture', None)
        if capture is None:
            return
        before = capture.view()
        dropped = capture.reset(vehicle.entity_id)
        shooter = self.vehicles.get(source)
        if (dropped and capture.point.contains(vehicle.pose.position)
                and shooter is not None and shooter.team != vehicle.team):
            shooter.defense_points += dropped
        if capture.view() != before:
            self._publish_base_capture()

    def _combat_can_finish(self):
        if self.endless or self.unlimited_lives:
            return False
        if self.room_settings is None:
            return True
        return ({p.team for p in self.players.values() if p.attached}
                == {p.team for p in self.players.values()})

    def _advance_base_capture(self, now):
        capture = self.base_capture
        if capture is None or self.finished_at or self.solo_victory_team:
            return
        before = capture.view()
        members = {vehicle.entity_id: vehicle.team for vehicle in self.vehicles.values()
                   if vehicle.alive and capture.point.contains(vehicle.pose.position)
                   and (vehicle.account_id is None or self.players[vehicle.account_id].attached)}
        winner = capture.advance(members, self.STEP, now)
        if capture.view() != before:
            self._publish_base_capture()
        if winner and self._combat_can_finish():
            for entity, points in capture.points.items():
                self.vehicles[entity].capture_points += math.floor(points + 1e-8)
            self.emit('base_captured', team=winner, base_id=capture.point.base_id)
            self.finish(winner, 'capture', now)

    def _advance_supremacy(self, now):
        if self.supremacy is None or self.finished_at:
            return
        before = self.supremacy.state()
        living = [v for v in self.vehicles.values() if v.alive
                  and (v.account_id is None or self.players[v.account_id].attached)]
        members = [{v.entity_id: v.team for v in living if point.contains(v.pose.position)}
                   for point in self.supremacy.points]
        for entity, points in self.supremacy.advance(members, self.STEP, now):
            self.vehicles[entity].capture_points += math.floor(points + 1e-8)
        if self.supremacy.state() != before:
            self.emit('supremacy', state=self.supremacy.state(), previous=before)
        winner = self.supremacy.winner()
        if winner is not None and self._combat_can_finish():
            self.finish(winner, 'supremacy', now)

    def drain_events(self):
        result,self.events = self.events,[]
        return result

    def close(self):
        while not self.advance_close():
            pass

    def advance_close(self):
        if self.closed:
            return True
        self.retiring = True
        if self._bot_navigation_builder is not None:
            self._bot_navigation_builder.close()
            self._bot_navigation_builder = None
        if self.drive_workers is not None:
            if not self.drive_workers.advance_close():
                return False
            self.drive_workers = None
        if self.world is None or self.world.advance_close():
            for scene in self._owned_bot_scenes:
                scene.close()
            self._owned_bot_scenes.clear()
            self._recon_point_probe = None
            if self.recon is not None:
                self._recon_contacts.clear()
                self._recon_camouflage_still.clear()
                self.recon = None
            self.closed = True
            return True
        return False

    def vehicle_for(self, account_id):
        return self.vehicles[self.players[account_id].vehicle_id]

    def enable_recon_point_probe(self, *, tolerance):
        """Enable a proximity experiment, not a proven visible-reticle test.

        No LOS, camera origin, visual mesh equivalence, or moving-target pose
        synchronization is established. The caller must select the tolerance;
        1 cm worked for one static Sheridan observation only.
        """
        if self.recon is None or self.closed or self.retiring or self.finished_at:
            raise ValueError('Active opt-in recon is required')
        if (isinstance(tolerance, bool) or not isinstance(tolerance, (int, float))
                or not math.isfinite(tolerance) or not 0 <= tolerance <= 5):
            raise ValueError('Invalid research tolerance')
        if self._recon_point_probe is not None:
            raise ValueError('Recon point probe is already enabled')
        self._recon_point_probe = dict(tolerance=float(tolerance), points={}, next={})

    def _recon_point_probe_update(self, now):
        probe = self._recon_point_probe
        if probe is None or self.recon is None:
            return
        from .recon_target import TargetGeometry, candidates_near_aim_point
        for account, player in self.players.items():
            if account in self._recon_client_aims:
                continue
            observer = self.vehicles[player.vehicle_id]
            if not player.attached or not observer.alive:
                probe['points'].pop(account, None)
                probe['next'].pop(account, None)
                continue
            if now + 1e-9 < probe['next'].get(account, -math.inf):
                continue
            probe['next'][account] = now + .1
            point = probe['points'].get(account)
            targets, unavailable = [], False
            if point is not None:
                for vehicle in self.vehicles.values():
                    if (not vehicle.alive or vehicle.team == observer.team or
                            vehicle.account_id is not None and
                            not self.players[vehicle.account_id].attached):
                        continue
                    try:
                        if self.world is None:
                            raise ValueError('No collision geometry')
                        armor = self.world.assets_for(vehicle.entity_id).armor
                        radius = float(armor.radius)
                        if not math.isfinite(radius) or radius < 0:
                            raise ValueError('Invalid geometry radius')
                        rotation = self.world._dynamic_rotation(vehicle.entity_id)
                        # Validate state before rejecting a distant candidate.
                        armor._parts_for_query(vehicle.movable_armor_state)
                        if math.dist(point, vehicle.pose.position) > radius + probe['tolerance']:
                            continue
                        targets.append(TargetGeometry(vehicle.entity_id, armor, vehicle.pose,
                            vehicle.gun.yaw, vehicle.gun.pitch, rotation, vehicle.movable_armor_state))
                    except (AttributeError, TypeError, ValueError, KeyError):
                        unavailable = True
            result = candidates_near_aim_point(point, targets, max_distance=probe['tolerance']) if point is not None else None
            target = (result.candidates[0].entity_id if result is not None and
                      not unavailable and not result.unavailable and len(result.candidates) == 1 else None)
            self.recon_aim(account, target)

    def recon_client_aim(self, account_id, target_id=None):
        """Refresh negotiated client reticle input; stale input loses lock.

        Keeping an expired entry prevents the approximate point experiment
        from silently taking over a negotiated observer's aim.
        """
        if self.recon is None or account_id not in self.players:
            raise ValueError('No recon observer')
        if target_id is not None and (type(target_id) is not int or target_id <= 0):
            raise ValueError('Invalid recon target ID')
        self._recon_client_aims[account_id] = (target_id, self.simulation_time + .35)
        return self.recon_aim(account_id, target_id)

    def recon_client_clear(self, account_id):
        self._recon_client_aims.pop(account_id, None)
        if self.recon is not None and account_id in self.players:
            self.recon_aim(account_id, None)

    def _recon_client_aim_update(self):
        for account, (target, until) in tuple(self._recon_client_aims.items()):
            self.recon_aim(account, target if self.simulation_time < until else None)

    def recon_aim(self, account_id, target_id=None):
        """Accept an externally verified reticle target at the current sim tick.

        The caller must provide a target every simulation step; None or an
        invalid/friendly/dead target breaks acquisition. This does not infer
        the reticle from the barrel or the server's approximate aim point.
        """
        if self.recon is None:
            raise ValueError('Recon is not enabled')
        if self.closed or self.retiring or self.finished_at or not self.started:
            return False
        player = self.players[account_id]
        observer = self.vehicles[player.vehicle_id]
        if not player.attached or not observer.alive:
            return False
        if target_id is not None and (type(target_id) is not int or target_id <= 0):
            raise ValueError('Invalid recon target ID')
        target = self.vehicles.get(target_id)
        if target is None or target.team == observer.team or not target.alive:
            target_id = None
        was_marked = (target_id is not None and
                      self.recon.personal_visible(observer.entity_id, target_id, self.simulation_time))
        locked = self.recon.aim(observer.entity_id, target_id, self.simulation_time)
        if locked and not was_marked:
            self.emit('recon_lock', observer.entity_id, target_id, at=self.simulation_time)
        return locked

    def _recon_impact(self, shot, shooter, target, at):
        """Exactly one roll per target and shot, including external contacts."""
        if (self.recon is None or not self.started or self.finished_at or
                shooter is None or target is None or not target.alive or
                shooter.team == target.team or shot in self.resolved_shots):
            return
        seen = self._recon_contacts.setdefault(shot, set())
        if target.entity_id in seen:
            return
        seen.add(target.entity_id)
        # 114 is the selected Coated Optics equipment ID. Vehicle class is
        # read from the pinned national list only if an explicit game root is
        # supplied. A local provisional rule activates installed camouflage
        # after three continuous seconds of near-zero movement.
        optics = 114 in getattr(shooter.definition, 'optional_devices', ())
        since = self._recon_camouflage_still.get(target.entity_id)
        camouflage = since is not None and at - since + 1e-9 >= self.RECON_CAMOUFLAGE_STILL_SECONDS
        light = False
        if self.recon_game is not None and shooter.definition is not None:
            from .recon_catalog import vehicle_class
            try:
                light = vehicle_class(self.recon_game, shooter.definition.nation,
                                      shooter.definition.name) == 'lightTank'
            except (OSError, ValueError, AttributeError):
                # Missing or malformed metadata must not invent a bonus or
                # disrupt the battle; the base probability remains available.
                light = False
        shared = self.recon.hit(shooter.entity_id, shooter.team, target.entity_id, target.team,
                                at, light=light, optics=optics, camouflage=camouflage)
        self.emit('recon_share', shooter.entity_id, target.entity_id,
                  at=at, shared=shared, light=light, optics=optics, camouflage=camouflage)

    def _update_recon_camouflage(self, vehicle, now):
        """Track continuous stillness for the selected camouflage device.

        This is a sandbox approximation, not recovered native behavior. Use
        world velocity when available so drifting or airborne bodies do not
        qualify merely because their forward speed is zero.
        """
        if self.recon is None:
            return
        entity = vehicle.entity_id
        stationary = vehicle.alive and 115 in getattr(vehicle.definition, 'optional_devices', ())
        speed = abs(vehicle.speed)
        motion = getattr(self.world, 'motion', {}).get(entity) if self.world else None
        if motion is not None:
            velocity = getattr(motion, 'velocity', None)
            if velocity is None or len(velocity) != 3:
                stationary = False
            else:
                speed = max(speed, math.sqrt(sum(v*v for v in velocity)))
        yaw_rate = abs(self._track_drive_yaw_rates.get(entity, 0.))
        stationary = (stationary and math.isfinite(speed) and math.isfinite(yaw_rate) and
                      speed < self.RECON_CAMOUFLAGE_STILL_SPEED and
                      yaw_rate < self.RECON_CAMOUFLAGE_STILL_YAW_RATE)
        if stationary:
            self._recon_camouflage_still.setdefault(entity, now)
        else:
            self._recon_camouflage_still.pop(entity, None)

    def ready(self, account_id, now=None):
        if self.building or self.closed or self.retiring:
            raise ValueError('Arena is not ready')
        player = self.players[account_id]
        if player.loaded:
            return
        player.loaded = True
        gun = self.vehicles[player.vehicle_id].gun
        self.emit('ready',actor=player.vehicle_id,yaw_rate=gun.yaw_rate,pitch_rate=gun.pitch_rate)
        self._start_if_ready(now)

    def _start_if_ready(self, now=None):
        if not self.started and not self.finished_at and all(p.loaded for p in self.players.values() if p.attached):
            now = time.monotonic() if now is None else now
            # Let replicas finish constructing before opening the live frame
            # loop. A failed pool already has the ordinary local-drive fallback.
            workers = self.drive_workers
            if self.prebattle_seconds and workers is not None and not workers.ready and not workers.failed:
                return
            if self.prebattle_seconds and not self.prebattle_end:
                self.prebattle_end = now + self.prebattle_seconds
                self.emit('period', period=2, seconds=self.prebattle_seconds, winner=0)
            if now < self.prebattle_end:
                return
            self.started = now
            self.last_tick = self.started
            self.simulation_time = self.started
            self.module_clock = self.started
            self.accumulator = 0.
            self.step_count = 0
            self.motion_frame = None
            if self.recon is not None:
                for vehicle in self.vehicles.values():
                    self._update_recon_camouflage(vehicle, self.started)
            self._record_motion_frame(self.started)
            self.emit('period',period=3,seconds=0 if self.endless else self.battle_seconds,winner=0)
            if self.practice_clock_repeats:
                self.practice_timer_at = self.started+self.battle_seconds
            if self.base_capture is not None:
                self._publish_base_capture()
            if self.supremacy is not None:
                self.emit('supremacy', state=self.supremacy.state())

    def control(self, account_id, flags, now=None):
        self.step(time.monotonic() if now is None else now)
        vehicle = self.vehicle_for(account_id)
        if self.players[account_id].attached and vehicle.alive and not self.finished_at:
            vehicle.flags = flags & 15
            vehicle.last_input = self.simulation_time
            airborne = self._vehicle_airborne(vehicle)
            vehicle.airborne_control_active = airborne
            vehicle.airborne_input_until = (
                self.simulation_time + self.AIRBORNE_CONTROL_GRACE
                if airborne and vehicle.flags else 0.)

    def aim_point(self, account_id, point, local=False):
        vehicle = self.vehicle_for(account_id)
        if vehicle.alive:
            # Follow-point RPCs keep the current lock and refine its aim point.
            if vehicle.autoaim_target:
                if self._recon_point_probe is not None:
                    self._recon_point_probe['points'].pop(account_id, None)
                world = tuple(a+b for a,b in zip(point,vehicle.pose.position)) if local else point
                return self.autoaim(account_id,vehicle.autoaim_target,world)
            accepted = vehicle.gun.set_point(point,local)
            if self._recon_point_probe is not None:
                if accepted and not local and len(point) == 3:
                    self._recon_point_probe['points'][account_id] = tuple(point)
                else:
                    self._recon_point_probe['points'].pop(account_id, None)
            return accepted
        return False

    def sandbox_control(self, account_id, control):
        player = self.players.get(account_id)
        if (not (self.sandbox_enabled or self.gravity_mode) or control.token != sandbox.token(self)
                or not player or not player.attached or not player.loaded
                or not self.started or self.finished_at or not self.vehicle_for(account_id).alive):
            return
        if self.sandbox_enabled:
            vehicle = self.vehicle_for(account_id)
            self._sandbox_inputs[account_id] = (self.simulation_time, control)
            self._set_fast_reload(vehicle, bool(control.flags & sandbox.FAST_RELOAD))
            if control.flags & sandbox.RESTORE_HP and vehicle.health < vehicle.max_health:
                vehicle.health = vehicle.max_health
                self.emit('sandbox_restore_hp', vehicle.entity_id, health=vehicle.health)
        if self.gravity_mode and control.flags & sandbox.GRAVITY_JUMP:
            self._gravity_jump(self.vehicle_for(account_id))
        if self.practice:
            vehicle = self.vehicle_for(account_id)
            if control.flags & sandbox.NEXT_TARGET:
                choices = tuple(self.practice_types)
                self.practice_selection = choices[(choices.index(self.practice_selection)+1) % len(choices)]
                self.emit('practice_notice', vehicle.entity_id, notice=self.practice_selection)
            if control.flags & sandbox.TOGGLE_TARGET_FIRE:
                self.practice_return_fire = not self.practice_return_fire
                self.emit('practice_notice', vehicle.entity_id, notice=(
                    sandbox.TARGET_FIRE_ON if self.practice_return_fire else sandbox.TARGET_FIRE_OFF))
            if control.flags & sandbox.CLEAR_TARGETS:
                self._clear_practice_targets(vehicle)
            elif control.flags & sandbox.SPAWN_TARGET:
                self._spawn_practice_target(vehicle)
        if self.sandbox_enabled and control.flags & sandbox.TELEPORT:
            self._teleport_vehicle(self.vehicle_for(account_id))

    def _teleport_vehicle(self, vehicle):
        if self.world is None:
            return
        entity = vehicle.entity_id
        _, (x, _, z) = self.aim_hit(vehicle)
        pose = self.world.training_placement(entity, x, z, vehicle.pose.yaw, stationary=True)
        if pose is None:
            return
        self.world.relocate_training_body(entity, pose)
        vehicle.pose, vehicle.speed = pose, 0.
        self.motion_frame = None
        for state in (self._sandbox_flights, self._landing_airborne_since, self._landing_skids,
                      self._track_drive_motion, self._track_drive_yaw_rates,
                      self._track_drive_track_rates, self.fall_hp_carry):
            state.pop(entity, None)
        for track in vehicle.tracks:
            self.track_fall_peaks.pop((entity, track), None)
        self.emit('sandbox_teleport', entity, position=pose.position,
                  yaw=pose.yaw, pitch=pose.pitch, roll=pose.roll)

    def _gravity_jump(self, vehicle):
        entity = vehicle.entity_id
        now = self.simulation_time
        state = next((s for s in vehicle.consumables if s and s.spec is GRAVITY_JUMP_SPEC), None)
        if (state is None or now < state.ready_at or self.world is None
                or not self.world.motion[entity].grounded):
            return False
        lightness = max(.65, min(1.4, math.sqrt(25000. / vehicle.definition.mass)))
        speed = abs(vehicle.speed)
        lift = (4.4 + min(1.4, speed * .07)) * lightness
        forward = (2.5 + min(4., speed * .18)) * lightness
        delta = (forward * math.sin(vehicle.pose.yaw), lift, forward * math.cos(vehicle.pose.yaw))
        if self.world.add_flight_velocity(entity, delta):
            state.activate(now)
            self.emit('gravity_jump', entity, velocity=delta)
            return True
        return False

    def _spawn_practice_target(self, player_vehicle):
        now = self.simulation_time
        if self.world is None or now < self.practice_next_spawn:
            return
        self.practice_next_spawn = now+.4
        entity = next((ident for ident in self.practice_types[self.practice_selection]
                       if not self.vehicles[ident].alive), None)
        if entity is None:
            self.emit('practice_notice', player_vehicle.entity_id, notice=sandbox.TARGET_SLOTS_FULL)
            return
        point = player_vehicle.gun.point
        if point is None:
            _, point = self.aim_hit(player_vehicle)
        elif player_vehicle.gun.local:
            point = tuple(a+b for a,b in zip(point, player_vehicle.pose.position))
        x, _, z = point
        yaw = player_vehicle.pose.yaw
        if self.practice_return_fire:
            px, _, pz = player_vehicle.pose.position
            yaw = math.atan2(px-x, pz-z)
        pose = self.world.training_placement(entity, x, z, yaw)
        if pose is None:
            return
        new = entity not in self.practice_spawned
        self._restore_vehicle(entity, pose)
        self.practice_gunners.pop(entity, None)
        if self.practice_return_fire:
            self.practice_gunners[entity] = TrainingBot(entity)
        self.practice_spawned.add(entity)
        self.practice_active.add(entity)
        self.emit('practice_spawn', entity, first=new, position=pose.position,
                  placed_by=player_vehicle.entity_id)

    def _park_practice_target(self, vehicle):
        entity = vehicle.entity_id
        low_x, low_z, _, _ = self.world.assets.bounds
        slot = entity-self.entity_base-201
        vehicle.pose = Pose((low_x-70., vehicle.pose.position[1], low_z-70.-20.*slot), 0.)
        self.world.relocate_training_body(entity, vehicle.pose)
        vehicle.health = vehicle.flags = 0
        vehicle.speed = 0.
        vehicle.engine_mode = (0, 0)

    def _clear_practice_targets(self, player_vehicle):
        if self.world is None:
            return
        for entity in sorted(self.practice_active):
            target = self.vehicles[entity]
            self._cancel_burst(target)
            self._stop_fire(target, self.simulation_time, 'practice_clear')
            self._stop_consumables(target)
            self._lose_target(entity)
            if self.recon is not None:
                self.recon.remove_vehicle(entity)
                self._recon_camouflage_still.pop(entity, None)
            self._park_practice_target(target)
            self.emit('practice_clear', entity, position=target.pose.position)
        for shot, projectile in tuple(self.projectiles.items()):
            if projectile.shooter in self.practice_active:
                self.emit('shot_end', projectile.shooter, shot=shot, point=projectile.position,
                          guided=projectile.shell.missile is not None)
                del self.projectiles[shot]
        self.practice_active.clear()
        self.practice_gunners.clear()
        self.practice_next_spawn = 0.
        self.motion_frame = None
        self.emit('practice_notice', player_vehicle.entity_id, notice=sandbox.TARGETS_CLEARED)

    def _restore_vehicle(self, entity, pose):
        previous = self.vehicles[entity]
        restored = Vehicle(entity, previous.account_id, previous.nickname, previous.team,
                           previous.descriptor, pose, loadout_ammo=previous.loadout_ammo,
                           loadout_shells=previous.loadout_shells,
                           consumables=tuple(ConsumableState(slot.spec) if slot else None
                                             for slot in previous.consumables))
        if self.unlimited_lives:
            for field in ('shots', 'hits', 'damage', 'blocked_damage', 'assisted_damage',
                          'kills', 'direct_hits', 'piercings', 'damage_received', 'hits_received',
                          'piercings_received', 'distance', 'capture_points', 'defense_points'):
                setattr(restored, field, getattr(previous, field))
        restored.consumable_clock = self.simulation_time
        restored.sandbox_fast_reload = previous.sandbox_fast_reload
        self._configure_battle_vehicle(restored, self.world.assets_for(entity))
        restored.movable_armor_state = self._movable_armor_snapshot_at(restored, self.simulation_time)
        self.world.relocate_training_body(entity, pose)
        self.world.set_turret(0., entity)
        self.vehicles[entity] = restored
        self.motion_frame = None
        self._sandbox_inputs.pop(restored.account_id, None)
        for state in (self._sandbox_flights, self._landing_airborne_since, self._landing_skids,
                      self._track_drive_motion, self._track_drive_yaw_rates, self._track_drive_track_rates,
                      self._track_repair_hud_extra, self.fall_hp_carry):
            state.pop(entity, None)
        for track in previous.tracks:
            self.track_fall_peaks.pop((entity, track), None)
        return restored

    def _advance_practice(self, now):
        if self.practice_timer_at and now >= self.practice_timer_at:
            self.practice_timer_at = now+self.battle_seconds
            self.emit('period', period=3, seconds=self.battle_seconds, winner=0)

    def _advance_respawns(self, now):
        if not self.respawn_at or self.world is None:
            return
        from .map_catalog import map_team_capacity
        for entity, due in tuple(self.respawn_at.items()):
            if now < due:
                continue
            previous = self.vehicles[entity]
            if not self._player_attached(previous):
                self.respawn_at.pop(entity)
                continue
            for slot in range(map_team_capacity(self.map_id)):
                x, z = training_spawn(self.map_spec, previous.team, slot)
                yaw = training_yaw(self.map_spec, previous.team, slot)
                pose = self.world.training_placement(entity, x, z, yaw)
                if pose is not None:
                    critical, destroyed = self.module_state(previous)
                    vehicle = self._restore_vehicle(entity, pose)
                    self.respawn_at.pop(entity)
                    self.emit('practice_spawn', entity, first=False, position=pose.position,
                              repaired=critical+destroyed)
                    self.emit('ready', entity, yaw_rate=vehicle.gun.yaw_rate,
                              pitch_rate=vehicle.gun.pitch_rate, respawn=True)
                    break
            else:
                self.respawn_at[entity] = now+.5

    def _active_sandbox_control(self, vehicle, now):
        if not (vehicle.alive and vehicle.account_id is not None and self._player_attached(vehicle)):
            return None
        entry = self._sandbox_inputs.get(vehicle.account_id)
        return entry[1] if entry and now-entry[0] <= sandbox.INPUT_LEASE else None

    def _sandbox_frame(self, now, dt):
        flying, piloted = set(), set()
        for vehicle in self.vehicles.values():
            if vehicle.account_id is None:
                continue
            control = self._active_sandbox_control(vehicle, now)
            flags = control.flags if control else 0
            vehicle.sandbox_boost = bool(flags & sandbox.BOOST)
            entity = vehicle.entity_id
            if not (self.world and self.world.supports_flight
                    and vehicle.alive and self._player_attached(vehicle)):
                self._sandbox_flights.pop(entity, None)
                continue
            state = self.world.motion[entity]
            flight = self._sandbox_flights.get(entity)
            if flags & sandbox.FLIGHT:
                if flight is None:
                    flight = self._sandbox_flights[entity] = sandbox.Flight(
                        vehicle.pose.position[1] + (4. if state.grounded else 0.))
                piloted.add(entity)
            elif flight is None or state.grounded:
                self._sandbox_flights.pop(entity, None)
                continue
            x, _, z = vehicle.pose.position
            drive = self._active_control_flags(vehicle, now)
            velocity, spin = sandbox.flight_velocity(flight, vehicle.pose, state.velocity,
                self.world.flight_spin(entity), self.terrain.height(x,z), drive, flags,
                self.world.GRAVITY, dt, bounds=self.world.assets.bounds)
            if self.world.set_flight_velocity(entity, velocity, spin):
                flying.add(entity)
                vehicle.engine_mode = (3, drive & 15) if drive else (1, 0)
        return flying, piloted

    def autoaim(self, account_id, target_id, point=None):
        if self._recon_point_probe is not None:
            self._recon_point_probe['points'].pop(account_id, None)
        vehicle = self.vehicle_for(account_id)
        if not vehicle.alive or not self.players[account_id].attached:
            return False
        if not target_id:
            vehicle.autoaim_target,vehicle.autoaim_offset = 0,None
            vehicle.gun.set_angles(vehicle.gun.yaw,vehicle.gun.pitch)
            return True
        target = self.vehicles.get(target_id)
        if (target is None or not target.alive or target.team==vehicle.team
                or target.account_id is not None and not self.players[target.account_id].attached):
            # A target can die/leave between the client's selection and receipt.
            # Acknowledge that loss instead of keeping a different old target.
            self.autoaim(account_id,0)
            self.emit('autoaim_lost',vehicle.entity_id,target_id)
            return False
        offset = None
        if point is not None:
            if len(point)!=3 or not all(math.isfinite(v) and abs(v)<10000 for v in point):
                return False
            if math.dist(point,target.pose.position)>15:
                return False
            p = target.pose
            offset = tuple(matrix(p.yaw,p.pitch,p.roll).T @ tuple(a-b for a,b in zip(point,p.position)))
        elif self.world:
            offset = self.world.assets_for(target_id).hull_center
        else:
            # Resource-free fixtures only; production uses the real hull mesh.
            offset = (0.,target.gun.TURRET[1]*.5,0.)
        vehicle.autoaim_target,vehicle.autoaim_offset = target_id,offset
        return True

    def _lose_target(self, entity):
        for vehicle in self.vehicles.values():
            if vehicle.autoaim_target and (vehicle.autoaim_target==entity or vehicle.entity_id==entity):
                lost = vehicle.autoaim_target
                vehicle.autoaim_target,vehicle.autoaim_offset = 0,None
                vehicle.gun.set_angles(vehicle.gun.yaw,vehicle.gun.pitch)
                self.emit('autoaim_lost',vehicle.entity_id,lost)

    def aim_angles(self, account_id, yaw, pitch):
        if self._recon_point_probe is not None:
            self._recon_point_probe['points'].pop(account_id, None)
        vehicle = self.vehicle_for(account_id)
        if vehicle.alive:
            return vehicle.gun.set_angles(yaw,pitch)
        return False

    def shot_ray(self, vehicle):
        p = vehicle.pose
        if self.world is not None:
            rotation = self.world._dynamic_rotation(vehicle.entity_id)
            if rotation is not None:
                return vehicle.gun.ray_rotation(p.position,rotation)
        return vehicle.gun.ray(p.position,p.yaw,p.pitch,p.roll)

    def shell_for(self, vehicle):
        if vehicle.definition:
            return next(s for s in vehicle.definition.weapon.shells if s.compact_id==vehicle.shell_id)
        return self.shell

    def select_shell(self, account_id, setting, shell_id, now=None):
        self.step(time.monotonic() if now is None else now)
        vehicle = self.vehicle_for(account_id)
        if setting == 17 and shell_id == 0:
            return self.reload_magazine(account_id)
        if (not self.started or self.finished_at or not vehicle.alive or setting not in (0,1)
                or type(shell_id) is not int or vehicle.ammo_by_shell.get(shell_id,0)<=0):
            return False
        if setting==1:
            vehicle.next_shell = shell_id
            return True
        if shell_id==vehicle.shell_id:
            vehicle.next_shell = 0
            vehicle.burst_switch_shell = 0
            self.emit('shell_selected',vehicle.entity_id,shell=shell_id)
            return True
        if vehicle.burst_pending:
            # Finish the already accepted group with its snapshotted shell.
            # Apply the new type immediately after its last scheduled round.
            vehicle.next_shell = 0
            vehicle.burst_switch_shell = shell_id
            return True
        old_clip, old_reload_at = vehicle.clip, vehicle.reload_at
        vehicle.ammo_by_shell[vehicle.shell_id] = vehicle.ammo
        vehicle.selected_shell = shell_id
        vehicle.next_shell = 0
        vehicle.ammo = vehicle.ammo_by_shell[shell_id]
        if vehicle.pump_times:
            # Local rule: a switch unloads the whole pump clip back into its
            # own count (already included in ammo) and restarts from index 0
            # with the new type. Any intershot and refill progress is dropped.
            vehicle.clip = 0
            vehicle.reload_at = 0.
            self._pump_segment(vehicle,self.simulation_time)
            self.emit('shell_selected',vehicle.entity_id,shell=shell_id)
            self.emit('ammo',vehicle.entity_id)
            self._emit_pump_gate(vehicle,self.simulation_time)
            self._emit_pump(vehicle,self.simulation_time)
            return True
        # Sandbox rule: selecting another available type changes the loaded
        # rounds without restarting an existing reload or intershot clock.
        # Counts include loaded rounds, so the new clip cannot exceed stock.
        vehicle.clip = min(old_clip if old_clip else (vehicle.clip_size if not old_reload_at else 0),
                           vehicle.ammo)
        self.emit('shell_selected',vehicle.entity_id,shell=shell_id)
        self.emit('ammo',vehicle.entity_id)
        self.emit('reload',vehicle.entity_id,
                  delay=max(0.,old_reload_at-self.simulation_time) if old_reload_at else 0.,
                  total=vehicle.reload_total)
        return True

    def reload_magazine(self, account_id):
        """Reload a partially spent magazine without spending stored shells."""
        vehicle = self.vehicle_for(account_id)
        player = self.players[account_id]
        if (not self.started or self.finished_at or not player.attached or not player.loaded
                or not vehicle.alive or vehicle.clip_size <= 1 or vehicle.pump_times
                or vehicle.burst_pending or vehicle.clip <= 0
                or vehicle.clip >= vehicle.clip_size or vehicle.clip >= vehicle.ammo
                or vehicle.reload_at and vehicle.reload_kind != 'interval'):
            return False
        weapon = vehicle.definition.weapon if vehicle.definition else None
        vehicle.clip = 0
        vehicle.reload_kind = 'reload'
        vehicle.reload_total = self._reload_duration(vehicle, 'reload', weapon.reload if weapon else 6.)
        vehicle.reload_at = self.simulation_time + vehicle.reload_total
        self.emit('ammo',vehicle.entity_id)
        self.emit('reload',vehicle.entity_id,delay=vehicle.reload_total)
        return True

    def aim_hit(self, vehicle):
        origin,direction = self.shot_ray(vehicle)
        if self.world is not None:
            # The collision scene is shared immutable geometry; controller
            # rows belong to each arena Vehicle and are snapshotted here for
            # the immediate aim query.
            armor_states = {entity: target.movable_armor_state
                            for entity,target in self.vehicles.items()
                            if target.movable_armor_state is not None}
            body,point,_ = self.world.shot_path(origin,direction,self.shell_for(vehicle).max_distance,
                                                ignore_entity=vehicle.entity_id,
                                                movable_armor_states=armor_states or None)
            entity = next((e for e,b in self.world.bodies.items() if b==body),None)
            return entity,point
        # Flat-world tests only. Production always has the installed collision scene.
        for distance in range(2,251,2):
            point = tuple(a+b*distance for a,b in zip(origin,direction))
            if point[1] <= self.terrain.height(point[0],point[2]):
                return None,point
        return None,tuple(a+b*250 for a,b in zip(origin,direction))

    def fire(self, account_id, now=None):
        self.step(time.monotonic() if now is None else now)
        vehicle = self.vehicle_for(account_id)
        return self._fire_vehicle(vehicle, self.simulation_time)

    def _fire_vehicle(self, vehicle, now):
        """Fire at the current simulation step for a player or opt-in bot."""
        if (not self.started or self.finished_at or not vehicle.alive or vehicle.reload_at or vehicle.ammo<=0
                or vehicle.pump_times and vehicle.clip<=0
                or vehicle.burst_gun and vehicle.burst_pending):
            return
        if self.gun_destroyed(vehicle):
            # Native 0x182c050 refuses a destroyed gun; never trust the client.
            self.emit('fire_rejected',vehicle.entity_id,reason='gun_destroyed',at=now)
            return
        shell = self.shell_for(vehicle)
        if shell is None:
            raise ValueError('No shell configuration for this vehicle')
        # Sandbox rule: an ACTIVE Tungsten Rounds snapshots its damage range into
        # this projectile's own Shell now; the impact still draws one uniform
        # roll. Its shot effect is only the tracer visual, carried by the shot
        # event: the Shell keeps its base effect_index for the impact/explosion.
        # Penetration, device damage and later shells are unchanged. One active
        # effect applies; duplicates never stack.
        boost = next((v for v in (s.state_at(now) for s in vehicle.consumables if s)
                      if v.damage_factors is not None or v.shot_effect is not None), None)
        shot_effect = shell.effect_index
        if boost is not None:
            shell = dataclasses.replace(shell,
                damage_factors=shell.damage_factors if boost.damage_factors is None else boost.damage_factors)
            if boost.shot_effect is not None and shell.missile is None:
                shot_effect = boost.shot_effect
        origin,velocity = self._launch(vehicle,shell,now)
        weapon = vehicle.definition.weapon if vehicle.definition else None
        if vehicle.pump_times:
            self._fire_pump(vehicle,weapon,now,shell,origin,velocity,shot_effect)
            return
        following = (min(weapon.burst_count-1,vehicle.clip,vehicle.ammo)
                     if vehicle.burst_gun else 0)
        if following:
            # One accepted Fire starts its whole XML group.  Only rounds which
            # really remain in the current clip and selected ammunition are
            # scheduled, so a short final loadout cannot create free shells.
            delay = weapon.burst_interval/vehicle.reload_speed_factor*vehicle.interval_factor
            vehicle.burst_shell,vehicle.burst_effect = shell,shot_effect
            vehicle.burst_remaining = following
            vehicle.reload_kind = 'interval'
            vehicle.reload_at = now+delay
            vehicle.reload_total = delay
            # Native own-vehicle FX accepts the whole group on its first
            # confirmation. Snapshot the actual size before later shots/switches.
            self._emit_shot(vehicle,shell,origin,velocity,now,shot_effect,following+1)
            self.emit('ammo',vehicle.entity_id)
            self.emit('reload',vehicle.entity_id,delay=delay)
            return
        # Berserk's speed applies to both cycles; NewAutoloader only to the intershot.
        vehicle.reload_kind = 'interval' if vehicle.clip else 'reload'
        nominal = (weapon.interval if vehicle.clip else weapon.reload) if weapon else (
            .1 if vehicle.clip else 6.)
        delay = self._reload_duration(vehicle, vehicle.reload_kind, nominal)
        vehicle.reload_at = now+delay
        vehicle.reload_total = delay
        self._emit_shot(vehicle,shell,origin,velocity,now,shot_effect)
        self.emit('ammo',vehicle.entity_id)
        self.emit('reload',vehicle.entity_id,delay=delay)

    def _training_bot_fire(self, bot, target, now, ai=None):
        """Fire only when the server collision query actually sees the target."""
        controller = ai or self.training_bots.get(bot.entity_id)
        if (controller is None or self.finished_at or not self.started
                or not bot.alive or target is None or not target.alive
                or self.world is None or bot.reload_at or bot.ammo <= 0
                or not controller.shot_check_due(now)):
            return
        visible,_ = self.aim_hit(bot)
        if (visible == target.entity_id if ai is None else
                controller.sight_confirmed(target.entity_id, visible == target.entity_id, now)):
            self._fire_vehicle(bot,now)

    def _training_bot_closing_speed(self, bot, target):
        """Relative horizontal approach speed from real motion, in m/s."""
        dx = target.pose.position[0]-bot.pose.position[0]
        dz = target.pose.position[2]-bot.pose.position[2]
        distance = math.hypot(dx,dz)
        if distance < 1e-9:
            return 0.
        def velocity(vehicle):
            motion = getattr(self.world,'motion',{}).get(vehicle.entity_id) if self.world else None
            values = getattr(motion,'velocity',None)
            if values is not None and len(values) == 3 and all(math.isfinite(v) for v in values):
                return values[0],values[2]
            return (vehicle.speed*math.sin(vehicle.pose.yaw),
                    vehicle.speed*math.cos(vehicle.pose.yaw))
        bx,bz = velocity(bot)
        tx,tz = velocity(target)
        return max(0.,((bx-tx)*dx+(bz-tz)*dz)/distance)

    def _training_bot_braking_acceleration(self, bot):
        """Conservative estimate of available horizontal braking on this slope."""
        braking = self._braking_acceleration(bot)
        pitch = self._drive_pitch(bot)
        gravity = getattr(self.world,'GRAVITY',None) if self.world else None
        if (not isinstance(pitch,(int,float)) or isinstance(pitch,bool)
                or not math.isfinite(pitch)):
            return braking
        surface = max(.05,math.cos(pitch))
        if isinstance(gravity,(int,float)) and not isinstance(gravity,bool) and math.isfinite(gravity) and gravity > 0:
            assets = self.world.assets_for(bot.entity_id)
            grip = self._slope_grip_acceleration(assets,pitch,gravity)
            if grip is not None:
                braking = min(braking,grip)
            # Taking either slope direction as adverse deliberately leaves an
            # extra safety margin; the actual physics remains untouched.
            return max(.5,braking*surface-abs(gravity*math.sin(pitch)*math.cos(pitch)))
        return braking*surface

    def _launch(self, vehicle, shell, now):
        """Spend one round and spawn its projectile from the live pose."""
        vehicle.shots += 1
        if not (self.unlimited_ammo or (self.practice and vehicle.account_id is not None)):
            vehicle.ammo -= 1
        vehicle.ammo_by_shell[vehicle.shell_id] = vehicle.ammo
        vehicle.clip -= 1
        self.next_shot += 1
        origin,direction = self.shot_ray(vehicle)
        velocity = tuple(v*shell.speed for v in direction)
        self.projectiles[self.next_shot] = Projectile(self.next_shot,vehicle.entity_id,
            shell,origin,velocity,now,recon_tracking=self.recon is not None)
        if self.gravity_mode:
            self._gravity_shell_impulse(vehicle, shell, direction, recoil=True)
        return origin,velocity

    def _gravity_shell_impulse(self, vehicle, shell, direction, *, recoil=False):
        if self.world is None:
            return
        mass = vehicle.definition.mass
        caliber = shell.caliber / 100.
        impulse = (42000. if recoil else 72000.) * caliber**1.2
        if not recoil and shell.missile is not None:
            impulse *= 1.7
        speed = min(9., impulse / mass)
        sign = -1. if recoil else 1.
        x, y, z = (sign * speed * axis for axis in direction)
        if self.world.motion[vehicle.entity_id].grounded:
            # Flat shots should shove the tracks across the ground. An upward
            # component can still release a tank, especially a light target.
            if y < 1.2 and self.world.add_ground_velocity(vehicle.entity_id, x, z):
                return
        self.world.add_flight_velocity(vehicle.entity_id, (x, y, z))

    def _gravity_air_control(self, vehicle, flags, dt):
        """Damp bank after takeoff while preserving ballistic movement."""
        entity = vehicle.entity_id
        if entity not in self.world.dynamic_bodies:
            return
        yaw, pitch, roll = vehicle.pose.yaw, vehicle.pose.pitch, vehicle.pose.roll
        pitch, roll = math.remainder(pitch, math.tau), math.remainder(roll, math.tau)
        # Give true rollovers to the contact solver.
        if max(abs(pitch), abs(roll)) >= math.pi/2:
            return
        steer = 0
        vx, vy, vz = self.world.motion[entity].velocity
        spin = self.world.flight_spin(entity)
        fx, fz = math.sin(yaw), math.cos(yaw)
        if flags and max(abs(pitch), abs(roll)) <= .9:
            steer = int(bool(flags & 8))-int(bool(flags & 4))
            tilt = int(bool(flags & 1))-int(bool(flags & 2))
            # Pitch and yaw input retain their existing response.
            wx, wy, wz = spin
            blend = min(1., dt * 3.)
            pitch_rate = tilt*.38
            spin = (wx + blend*(pitch_rate*fz-wx), wy + blend*(steer*.65-wy),
                    wz + blend*(-pitch_rate*fx-wz))
        # Euler roll is about the pitched forward axis. Correcting along that
        # axis keeps the nose pointed where the player aimed during the fall.
        cp = math.cos(pitch)
        axis = (fx*cp, -math.sin(pitch), fz*cp)
        roll_rate = sum(rate*component for rate, component in zip(spin, axis))
        target = max(-1.25, min(1.25, -3.5*roll))
        correction = (roll_rate-target)*math.expm1(-9.*dt)
        spin = tuple(rate+correction*component for rate, component in zip(spin, axis))
        drift = steer * min(1.5, math.hypot(vx, vz)*.2) * dt
        self.world.set_flight_velocity(entity, (vx+fz*drift, vy, vz-fx*drift), spin)

    def _emit_shot(self, vehicle, shell, origin, velocity, now, shot_effect, shooting_count=1):
        self.emit('shot',vehicle.entity_id,shot=self.next_shot,origin=origin,
                  velocity=velocity,gravity=shell.gravity,at=now,
                  shell_id=shell.compact_id,effect_index=shot_effect,
                  ammo_after=vehicle.ammo,
                  **({'shooting_count':shooting_count} if shooting_count != 1 else {}),
                  **({'guided':True} if shell.missile is not None else {}))

    def _missile_target(self, projectile):
        shooter = self.vehicles[projectile.shooter]
        player = self.players.get(shooter.account_id)
        if not (player and player.attached and shooter.alive):
            return None
        if self.sandbox_enabled:
            control = self._active_sandbox_control(shooter, self.simulation_time)
            if control and control.flags & sandbox.MISSILE_VIEW and control.shot == projectile.shot:
                return tuple(p + d*projectile.shell.max_distance
                             for p,d in zip(projectile.position,control.direction()))
        # _aim has already refreshed the gun's world point from the current
        # target pose when the player has an active lock.
        point = shooter.gun.point
        if point is None:
            origin,direction = self.shot_ray(shooter)
            return tuple(p + d*projectile.shell.max_distance for p,d in zip(origin,direction))
        if shooter.gun.local:
            point = tuple(p + offset for p,offset in zip(shooter.pose.position,point))
        if shooter.autoaim_target or sum((p-q)*v for p,q,v in
                                         zip(point,projectile.position,projectile.velocity)) > 0:
            return point
        # The reticle can land on a nearby wall while the missile is already
        # beyond it. Use that sight bearing to keep steering from its position.
        origin,_ = self.shot_ray(shooter)
        return tuple(q + p - o for q,p,o in zip(projectile.position,point,origin))

    def _fire_burst_next(self, vehicle, now):
        shell,shot_effect = vehicle.burst_shell,vehicle.burst_effect
        vehicle.reload_at = 0.
        weapon = vehicle.definition.weapon
        if self.gun_destroyed(vehicle):
            # Same refusal as Fire; all unfired group rounds are unloaded with
            # the rest of the clip, while their rack ammunition stays intact.
            self.emit('fire_rejected',vehicle.entity_id,reason='gun_destroyed',at=now)
            vehicle.burst_shell,vehicle.burst_effect = None,0
            vehicle.burst_remaining = 0
            vehicle.burst_switch_shell = 0
            vehicle.clip = 0
            vehicle.reload_kind = 'reload'
            vehicle.reload_total = self._reload_duration(vehicle, 'reload', weapon.reload)
            vehicle.reload_at = now+vehicle.reload_total
            self.emit('ammo',vehicle.entity_id)
            self.emit('reload',vehicle.entity_id,delay=vehicle.reload_total)
            return
        origin,velocity = self._launch(vehicle,shell,now)
        vehicle.burst_remaining -= 1
        if vehicle.burst_remaining:
            delay = weapon.burst_interval/vehicle.reload_speed_factor*vehicle.interval_factor
            vehicle.reload_kind = 'interval'
            vehicle.reload_at = now+delay
            vehicle.reload_total = delay
        else:
            vehicle.burst_shell,vehicle.burst_effect = None,0
            vehicle.reload_kind = 'interval' if vehicle.clip else 'reload'
            nominal = weapon.interval if vehicle.clip else weapon.reload
            delay = self._reload_duration(vehicle, vehicle.reload_kind, nominal)
            vehicle.reload_at = now+delay
            vehicle.reload_total = delay
        # The initial event already started this group's muzzle/audio sequence.
        # Its remaining physical projectiles still each need their own tracer.
        self._emit_shot(vehicle,shell,origin,velocity,now,shot_effect,0)
        if not vehicle.burst_pending:
            pending = vehicle.burst_switch_shell
            vehicle.burst_switch_shell = 0
            if pending and pending!=vehicle.shell_id and vehicle.ammo_by_shell.get(pending,0)>0:
                vehicle.ammo_by_shell[vehicle.shell_id] = vehicle.ammo
                vehicle.selected_shell = pending
                vehicle.ammo = vehicle.ammo_by_shell[pending]
                vehicle.clip = min(vehicle.clip,vehicle.ammo)
                self.emit('shell_selected',vehicle.entity_id,shell=pending)
        self.emit('ammo',vehicle.entity_id)
        self.emit('reload',vehicle.entity_id,delay=delay)

    def _fire_pump(self, vehicle, weapon, now, shell, origin, velocity, shot_effect):
        # The shot discards refill progress. Shells left: Berserk/NewAutoloader
        # intershot as for magazines, then refill. Empty: refill at once; a
        # queued shell type loads here, as a magazine applies it at reload.
        vehicle.pump_at = vehicle.pump_total = 0.
        switched = 0
        if vehicle.clip:
            delay = self._reload_duration(vehicle, 'interval', weapon.interval)
            vehicle.reload_kind = 'interval'
            vehicle.reload_at = now+delay
            vehicle.reload_total = delay
            vehicle.pump_waiting = True
        else:
            pending = vehicle.next_shell
            if pending and pending!=vehicle.shell_id and vehicle.ammo_by_shell.get(pending,0)>0:
                vehicle.ammo_by_shell[vehicle.shell_id] = vehicle.ammo
                vehicle.selected_shell = pending
                vehicle.ammo = vehicle.ammo_by_shell[pending]
                switched = pending
            vehicle.next_shell = 0
            vehicle.reload_at = 0.
            self._pump_segment(vehicle,now)
        self._emit_shot(vehicle,shell,origin,velocity,now,shot_effect)
        if switched:
            self.emit('shell_selected',vehicle.entity_id,shell=switched)
        self.emit('ammo',vehicle.entity_id)
        if vehicle.clip:
            self.emit('reload',vehicle.entity_id,delay=vehicle.reload_total)
        else:
            self._emit_pump_gate(vehicle,now)
        self._emit_pump(vehicle,now)

    def _has_damaged_modules(self, vehicle):
        return (any(s.critical for s in vehicle.tracks.values())
                or (vehicle.entity_id in self.guns and self.guns[vehicle.entity_id].damaged)
                or (vehicle.entity_id in self.engines and self.engines[vehicle.entity_id].health
                    < self.engines[vehicle.entity_id].spec.max_health)
                or (vehicle.entity_id in self.ammo_racks and self.ammo_racks[vehicle.entity_id].damaged)
                or (vehicle.entity_id in self.fuels and self.fuels[vehicle.entity_id].damaged))

    def _needs_restoration(self, vehicle):
        return bool(self._has_damaged_modules(vehicle) or self.injured_crew(vehicle)
                    or self.state_modifiers(vehicle))

    def use_consumable(self, account_id, ident, extra=b'', now=None):
        self.step(time.monotonic() if now is None else now)
        vehicle = self.vehicle_for(account_id)
        player = self.players[account_id]
        now = self.simulation_time
        reason = ''
        state = next((s for s in vehicle.consumables if s and s.spec.ident == ident), None)
        if not self.started or self.finished_at or not player.attached or not player.loaded or not vehicle.alive:
            reason = 'Vehicle cannot use consumables now'
        elif extra or type(ident) is not int or state is None:
            reason = 'Consumable is not installed or extra arguments are unsupported'
        elif state.spec.action == 'extinguish' and not self._fire_active(vehicle):
            reason = 'Vehicle is not burning'
        elif state.spec.interval_factor != 1 and vehicle.clip_size <= 1:
            reason = 'Consumable requires a magazine gun'
        elif state.spec.action == 'repair' and not self._has_damaged_modules(vehicle):
            reason = 'No damaged modules to repair'
        elif state.spec.action == HEAL and not self.injured_crew(vehicle):
            reason = 'No injured crew to heal'
        elif state.spec.action == RESTORE and not self._needs_restoration(vehicle):
            reason = 'No damaged modules, injured crew or fire to restore'
        if reason:
            self.emit('consumable_rejected', vehicle.entity_id, ident=ident, reason=reason, at=now)
            return False
        if state.spec is GRAVITY_JUMP_SPEC:
            return self._gravity_jump(vehicle)
        if not self._activate_consumable(vehicle, state, now):
            self.emit('consumable_rejected', vehicle.entity_id, ident=ident,
                      reason='Consumable is cooling down or has no live target', at=now)
            return False
        return True

    def _activate_consumable(self, vehicle, state, at, *, fire_advanced=False):
        """Private non-reentrant activation shared by UI and automatic use."""
        if not self.started or self.finished_at:
            return False
        if state.spec.action in ('extinguish', RESTORE):
            if not fire_advanced:
                self._advance_fire(vehicle, at)
        if not vehicle.alive:
            return False
        if state.spec.action == 'extinguish' and not self._fire_active(vehicle):
            return False
        if state.spec.action == 'repair' and not self._has_damaged_modules(vehicle):
            return False
        if state.spec.action == HEAL and not self.injured_crew(vehicle):
            return False
        if state.spec.action == RESTORE:
            # A due tick, natural expiry, or a ready automatic extinguisher
            # may have removed the last live target (or killed the vehicle).
            if not self._needs_restoration(vehicle):
                return False
            self._consumables(vehicle, max(vehicle.consumable_clock, self.simulation_time, at))
        if not state.activate(at):
            return False
        if state.spec.action == 'repair':
            self._repair_tracks(vehicle, at)
        elif state.spec.action == HEAL:
            self._heal_crew(vehicle, at)
        elif state.spec.action == 'extinguish':
            self._stop_fire(vehicle, at, 'extinguisher', fire_advanced=True)
        elif state.spec.action == RESTORE:
            # One activation and one cooldown for the composite action. Full
            # module repair reuses existing sandbox rules, never tank HP.
            self._repair_tracks(vehicle, at)
            self._heal_crew(vehicle, at)
            self._stop_fire(vehicle, at, 'recoverkit', fire_advanced=True)
        self._consumables(vehicle, max(vehicle.consumable_clock, self.simulation_time, at))
        self.emit('consumable_used', vehicle.entity_id, ident=state.spec.ident, uses=state.uses,
                  active_until=state.active_until, ready_at=state.ready_at, at=at,
                  engine_factor=vehicle.engine_factor, reload_speed_factor=vehicle.reload_speed_factor,
                  interval_factor=vehicle.interval_factor)
        return True

    def _consumables(self, vehicle, now, *, sync_movable=True):
        # Fixed steps cross expiry by at most STEP. Split the rate change at
        # the exact simulation timestamp, preserving completed reload work.
        # The interval factor only rescales an intershot (or its ready total);
        # a magazine reload keeps its timer through NewAutoloader changes.
        expiries = sorted({s.active_until for s in vehicle.consumables if s and s.uses and
                           vehicle.consumable_clock < s.active_until <= now})
        # Pump refills complete up to each split first; the running refill then
        # rescales by the reload rate only, never by the intershot factor.
        for at in (*expiries, now):
            self._advance_pump(vehicle, at, now)
            views = [s.state_at(at) for s in vehicle.consumables if s]
            rate = math.prod(v.reload_speed_factor for v in views)
            interval = math.prod(v.interval_factor for v in views)
            old_ammo_factor = self.applied_ammo_reload_factors.get(vehicle.entity_id, 1.)
            old_crew_factor = self.applied_crew_reload_factors.get(vehicle.entity_id, 1.)
            rack = self.ammo_racks.get(vehicle.entity_id)
            new_ammo_factor = rack.reload_speed_factor if rack is not None else 1.
            new_crew_factor = self._crew_role_factor(vehicle, 'loader')
            rate_scale = vehicle.reload_speed_factor/rate
            # Conservative loader boundary: full loading/refilling is known;
            # crew influence on burst/intra-clip mechanical delays is unproven.
            full_scale = ((vehicle.reload_speed_factor * old_ammo_factor * old_crew_factor)
                          / (rate * new_ammo_factor * new_crew_factor))
            scale = rate_scale
            pump_scale = full_scale
            if vehicle.reload_kind == 'interval':
                scale *= interval/vehicle.interval_factor
            else:
                scale = full_scale
            if vehicle.sandbox_fast_reload:
                pump_scale = 1.
                if not vehicle.burst_pending:
                    scale = 1.
            if scale != 1:
                vehicle.reload_total *= scale
                if vehicle.reload_at > at:
                    vehicle.reload_at = at + (vehicle.reload_at-at)*scale
                    self.emit('reload', vehicle.entity_id, delay=max(0.,vehicle.reload_at-now),
                              total=vehicle.reload_total)
                elif (not vehicle.reload_at and vehicle.alive and not self.finished_at
                      and (vehicle.clip or not vehicle.pump_times)):
                    self.emit('reload', vehicle.entity_id, delay=0., total=vehicle.reload_total)
            if pump_scale != 1 and vehicle.pump_at > at:
                vehicle.pump_total *= pump_scale
                vehicle.pump_at = at + (vehicle.pump_at-at)*pump_scale
                if not vehicle.clip:
                    self._emit_pump_gate(vehicle, now)
                self._emit_pump(vehicle, now)
            elif scale != 1 and vehicle.pump_at > at:
                # Interval-only change: the refill keeps its exact timer, but the
                # reload event above clears the native secondary timer, so resend
                # the unchanged pump snapshot after it.
                self._emit_pump(vehicle, now)
            vehicle.reload_speed_factor = rate
            vehicle.interval_factor = interval
            self.applied_ammo_reload_factors[vehicle.entity_id] = new_ammo_factor
            self.applied_crew_reload_factors[vehicle.entity_id] = new_crew_factor
            engine = self.engines.get(vehicle.entity_id)
            vehicle.engine_factor = math.prod(v.engine_factor for v in views) * (
                engine.power_factor if engine is not None else 1.)
            vehicle.rotation_factor = math.prod(v.rotation_factor for v in views)
            vehicle.forward_speed_bias = sum(v.forward_bias_kmh for v in views)/3.6
            vehicle.reverse_speed_bias = sum(v.reverse_bias_kmh for v in views)/3.6
        # Local Accelerated Aiming rule. _fixed_step runs this before _aim, so a
        # traverse change applies from the next slew; an expiry inside a step
        # is approximated at that step's end (at most one 60Hz step). The
        # owner's TargetingInfo is refreshed once per change with snapshots.
        views = [s.state_at(now) for s in vehicle.consumables if s]
        # Official "aiming speed" is not evidence for gun elevation speed.
        # Zero dispersion is intentional; only proven turret traverse changes.
        # GunAim keeps its existing hull counter-rotation allowance unscaled.
        if vehicle.gun.set_factors(math.prod(v.yaw_rate_factor for v in views)
                                   * self._crew_role_factor(vehicle, 'gunner'),
                                   math.prod(v.pitch_rate_factor for v in views)):
            self.emit('targeting',vehicle.entity_id,yaw_rate=vehicle.gun.yaw_rate,
                      pitch_rate=vehicle.gun.pitch_rate)
        if sync_movable:
            self._sync_movable_armor(vehicle, now)
        vehicle.consumable_clock = now
        for state in vehicle.consumables:
            if state is None:
                continue
            view = state.state_at(now)
            if vehicle.consumable_status.get(state.spec.ident) != view.state:
                vehicle.consumable_status[state.spec.ident] = view.state
                self.emit('consumable_state', vehicle.entity_id, ident=state.spec.ident,
                          compact_id=state.spec.compact_id, state=view.state,
                          remaining=view.remaining, total=view.total, at=now,
                          equipment=self.equipment_state(vehicle, now))

    def _movable_armor_snapshot_at(self, vehicle, now):
        """Return the complete controller configuration effective at ``now``."""
        controller = getattr(getattr(vehicle, 'definition', None),
                             'armor_states_controller', None)
        if controller is None:
            return None
        active = tuple(state for state in vehicle.consumables if state
                       and state.spec.armor_activate_state is not None
                       and state.state_at(now).state == ACTIVE)
        if len(active) > 1:
            raise ValueError('Multiple movable armor consumables are active')
        if any(getattr(row, 'kind', None) != 'default' for row in controller.states):
            raise ValueError('Movable armor controller has no fixed defaults')
        values = {row.ident: row.enabled for row in controller.states}
        if (any(type(value) is not bool for value in values.values())
                or sum(values.values()) != 1):
            raise ValueError('Movable armor controller has no fixed defaults')
        if active:
            spec = active[0].spec
            activate,deactivate = spec.armor_activate_state,spec.armor_deactivate_state
            if (type(activate) is not int or type(deactivate) is not int
                    or activate == deactivate or activate not in values or deactivate not in values):
                raise ValueError('Invalid movable armor consumable states')
            # Property16 represents independent flags, but the collision
            # contract accepts one selected supplemental group. Any future
            # controller rows must consequently be explicit disabled here.
            values = dict.fromkeys(values, False)
            values[activate] = True
        return MovableArmorStateSnapshot(tuple(values.items()))

    def _sync_movable_armor(self, vehicle, now):
        """Publish a changed ArmorMover controller state at its sim timestamp."""
        snapshot = self._movable_armor_snapshot_at(vehicle, now)
        if snapshot is None:
            return
        if vehicle.movable_armor_state != snapshot:
            vehicle.movable_armor_state = snapshot
            self.emit('armor_states', vehicle.entity_id, states=snapshot, at=now)

    def _armor_state_segments(self, start, end):
        """Fractions split at ArmorMover expiries strictly inside one sim step."""
        if not end > start:
            raise ValueError('Invalid armor-state time interval')
        transitions = set()
        for vehicle in self.vehicles.values():
            for state in vehicle.consumables:
                if (state and state.spec.armor_activate_state is not None and state.uses
                        and start < state.active_until < end):
                    transitions.add(state.active_until)
        points = (start, *sorted(transitions), end)
        span = end-start
        return tuple(((left-start)/span, (right-start)/span)
                     for left,right in zip(points,points[1:]))

    def _armor_frame_state(self, vehicle, before, after, fraction, at):
        """Interpolate motion while taking the controller snapshot at one time."""
        state = interpolate(before, after, fraction)
        snapshot = self._movable_armor_snapshot_at(vehicle, at)
        return state if snapshot is None else dataclasses.replace(state, armor_state=snapshot)

    def _armor_frame_states(self, before, after, fraction, at):
        return {entity:self._armor_frame_state(self.vehicles[entity], before[entity], after[entity],
                                                fraction, at)
                for entity in before}

    def equipment_state(self, vehicle, now=None):
        """Immutable start/stop snapshot, independent of network serialization."""
        now = self.simulation_time if now is None else now
        return tuple((s.spec.ident, s.state_at(now).state, s.activated_at,
                      s.active_until-s.activated_at) for s in vehicle.consumables
                     if s and s.spec is not GRAVITY_JUMP_SPEC and s.spec.action == 'boost'
                     and s.state_at(now).state in (2, 3))

    def _stop_consumables(self, vehicle):
        # A dead or finished vehicle has no future weapon state.  In
        # particular, do this before recomputing stopped consumable effects so
        # their expiry cannot rescale, complete, or announce a stale reload.
        vehicle.reload_at = 0.
        vehicle.next_shell = 0
        vehicle.burst_shell,vehicle.burst_effect,vehicle.burst_remaining = None,0,0
        vehicle.burst_switch_shell = 0
        vehicle.reload_kind = 'reload'
        self._stop_pump(vehicle)
        for state in vehicle.consumables:
            if state:
                state.stop(self.simulation_time)
        self._consumables(vehicle, self.simulation_time)

    def finish(self, winner=0, reason='timeout', now=None):
        if self.finished_at:
            return
        at = self.simulation_time if now is None else now
        for vehicle in self.vehicles.values():
            if vehicle.alive:
                self._advance_fire(vehicle, at)
        if self.finished_at:
            return
        self.finished_at = at
        self.winner,self.finish_reason = winner,reason
        for vehicle in self.vehicles.values():
            vehicle.flags = 0
            vehicle.speed = 0.
            vehicle.engine_mode = (1,0) if vehicle.alive else (0,0)
            self._stop_fire(vehicle, at, 'battle_end')
            self._stop_consumables(vehicle)
        for projectile in self.projectiles.values():
            self.emit('shot_end',projectile.shooter,shot=projectile.shot,point=projectile.position,
                      **({'guided':True} if projectile.shell.missile is not None else {}))
        self.projectiles.clear()
        self._recon_point_probe = None
        if self.recon is not None:
            self._recon_contacts.clear()
            self._recon_camouflage_still.clear()
            for entity in self.vehicles:
                self.recon.remove_vehicle(entity)
        self.emit('period',period=4,seconds=self.POSTBATTLE_SECONDS,winner=winner)

    def leave(self, account_id, now=None, *, reason='disconnect'):
        player = self.players[account_id]
        if not player.attached:
            return
        player.attached = False
        player.departure_reason = reason
        self._sandbox_inputs.pop(account_id, None)
        if self._recon_point_probe is not None:
            self._recon_point_probe['points'].pop(account_id, None)
            self._recon_point_probe['next'].pop(account_id, None)
        vehicle = self.vehicle_for(account_id)
        if vehicle.alive and not self.finished_at:
            self._set_fast_reload(vehicle, False)
        if self.recon is not None:
            self.recon.remove_vehicle(vehicle.entity_id)
            self._recon_camouflage_still.pop(vehicle.entity_id, None)
        vehicle.flags = 0
        vehicle.speed = 0.
        vehicle.engine_mode = (1,0) if vehicle.alive else (0,0)
        self._cancel_burst(vehicle)
        self._lose_target(vehicle.entity_id)
        if not self.finished_at:
            if self.solo:
                self.finish(self.solo_victory_team or 2,reason,now)
            elif self.room_settings is not None:
                if any(p.attached for p in self.players.values()):
                    self._start_if_ready(now)
                else:
                    self.finish(0, reason, now)
            else:
                active = {p.team for p in self.players.values()
                          if p.attached and self.vehicles[p.vehicle_id].alive}
                if len(active)<2:
                    self.finish(next(iter(active),0),reason,now)
                else:
                    self._start_if_ready(now)

    @property
    def postbattle_complete(self):
        return bool(self.finished_at and
                    self.simulation_time >= self.finished_at + self.POSTBATTLE_SECONDS)

    def results(self):
        if self.practice or not self.started or not self.finished_at:
            return []
        return [BattleResult(self.result_id,p.account_id,self.winner==p.team,
                self.vehicles[p.vehicle_id].shots,self.vehicles[p.vehicle_id].hits,
                self.vehicles[p.vehicle_id].damage,self.finish_reason,p.inventory_id,
                reward=self.vehicles[p.vehicle_id].battle_reward(self.winner==p.team))
                for p in self.players.values()]

    def _impact(self, event, states=None, *, detonate_ammo_rack=False, fire_at=None):
        if event.shot in self.resolved_shots:
            return
        if self.recon is not None:
            if event.result.hit is not None:
                self._recon_impact(event.shot, self.vehicles.get(event.shooter),
                                   self.vehicles.get(event.target), event.at)
            self._recon_contacts.pop(event.shot, None)
        self.resolved_shots.add(event.shot)
        shooter = self.vehicles[event.shooter]
        if event.obstacle is not None:
            shell = event.shell or self.shell_for(shooter)
            self.world.destroy(event.obstacle,event.direction[::2],damage=shell.damage)
        shell = event.shell or self.shell_for(shooter)
        if (shell and shell.missile is not None and shell.kind!='HIGH_EXPLOSIVE'
                and event.collision and event.result.hit is None):
            self.emit('explosion',event.shooter,shot=event.shot,effect_index=shell.effect_index,
                      shell_id=shell.compact_id,material=0,point=event.point,
                      direction=event.direction,at=event.at)
        if shell and shell.kind=='HIGH_EXPLOSIVE' and (event.collision or event.result.hit is not None):
            self.emit('explosion',event.shooter,shot=event.shot,effect_index=shell.effect_index,
                      shell_id=shell.compact_id,material=3 if event.result.hit else 0,
                      point=event.point,direction=event.direction,at=event.at)
            # A penetrating HE bursts inside the vehicle. Its blast does not
            # also damage neighbouring tanks. Otherwise evaluate every living
            # enemy at this collision time, never at the fire-time position.
            if event.result.outcome!='penetrated' and self.world is not None:
                primary = None
                extra = []
                for target in self.vehicles.values():
                    if not target.alive or target.team==shooter.team:
                        continue
                    state = states[target.entity_id] if states else None
                    pose = state.pose if state else target.pose
                    yaw = state.turret if state else target.gun.yaw
                    pitch = state.pitch if state else target.gun.pitch
                    armor_state = (event.target_armor_state
                                   if target.entity_id==event.target and event.target_armor_state is not None
                                   else state.armor_state if state is not None
                                   else target.movable_armor_state)
                    blast = self.world.assets_for(target.entity_id).armor.blast(
                        event.point,shell,pose,yaw,pitch,self.rng,
                        contact=event.result.hit if target.entity_id==event.target else None,
                        rotation=state.rotation if state else None,
                        movable_state=armor_state)
                    if target.entity_id==event.target:
                        primary = blast if blast.damage else event.result
                    elif blast.damage:
                        extra.append((target.entity_id,blast))
                self._apply_impact(event,primary or event.result,event.target,direct=True,
                                   shell=shell,fire_at=fire_at,
                                   hit_state=states.get(event.target) if states else None)
                for target,result in extra:
                    self._apply_impact(event,result,target,direct=False,shell=shell,fire_at=fire_at,
                                       hit_state=states.get(target) if states else None)
                return
        self._apply_impact(event,event.result,event.target,direct=True,shell=shell,
                           detonate_ammo_rack=detonate_ammo_rack,fire_at=fire_at)

    def _apply_impact(self, event, result, target_id, *, direct, shell=None,
                      detonate_ammo_rack=False, fire_at=None, hit_state=None):
        shooter = self.vehicles[event.shooter]
        target = self.vehicles.get(target_id)
        enemy = target is not None and target.team!=shooter.team
        # Fixed-step callers bound projectile floating-point drift to their
        # containing grid interval. Standalone callers retain event time.
        fire_at = max(self.module_clock, event.at if fire_at is None else fire_at)
        if enemy and target.alive:
            self._advance_fire(target, fire_at)
        # Local spall liner rule: the target's own factor scales the final rolled
        # HE HP damage (direct or splash) once, half-up, before the HP cap. A
        # copied result carries it; penetration, modules and the shell stay.
        # Reactive Armor does the same for AP/APCR/HEAT only. ArmorMover raw73
        # is separately documented by the pinned client as all-shell protection,
        # including direct and splash HE; its own catalog filter has no overlap
        # with ShieldKit or the implemented anti-HE provision.
        he = shell is not None and shell.kind=='HIGH_EXPLOSIVE'
        factor = self._armor_mover_factor(target,event.at) if enemy and shell is not None else 1.
        anti_he_factor = 1.
        if enemy and he:
            anti_he_factor = getattr(target.definition,'he_damage_factor',1.)
            factor *= anti_he_factor
        elif (not detonate_ammo_rack and enemy and shell is not None
              and shell.kind in _SHIELDED_KINDS):
            factor *= self._shield_factor(target,event.at)
        raw = result.damage
        mitigated = factor!=1 and raw>0 and target.alive
        if mitigated:
            result = dataclasses.replace(result,damage=int(factor*raw+.5))
        damage = (target.health if detonate_ammo_rack and enemy and target.alive
                  else min(target.health,result.damage) if enemy else 0)
        blocked = (int(shell.damage+.5) if direct and enemy and target.alive
                   and result.hit is not None and not result.hit.external and shell is not None
                   and shell.kind != 'HIGH_EXPLOSIVE'
                   and result.outcome in ('ricochet','not_penetrated') else 0)
        hit_effect = None
        state = (event.target_state if target_id == event.target and event.target_state is not None
                 else hit_state)
        if (target is not None and result.hit is not None and shell is not None
                and state is not None and self.world is not None):
            frames = getattr(self.world.assets_for(target_id),'hit_effects',None)
            if frames is not None:
                direction = ((event.contact_direction or event.direction) if direct else
                             tuple(a-b for a,b in zip(result.hit.point,event.point)))
                segment = frames.encode(result.hit.part,result.hit.point,direction,state)
                if segment is not None:
                    hit_effect = (shell.effect_index,segment)
        self.emit('impact' if direct else 'splash',event.shooter,target_id or 0,shot=event.shot,point=event.point,
                  result=result,applied_damage=damage,at=event.at,**({'raw_damage':raw} if mitigated else {}),
                  **({'blocked_damage':blocked} if blocked else {}),
                  **({'hit_effect':hit_effect} if hit_effect is not None else {}),
                  **({'guided':True} if direct and shell and shell.missile is not None else {}))
        if mitigated and damage and he and anti_he_factor != 1:
            # Semantic FX only: no timer or invulnerability; wire framing is root-owned.
            self.emit('provision_effect',target.entity_id,event.shooter,shot=event.shot,ident=ANTI_HE_PROVISION,
                      compact_id=(ANTI_HE_PROVISION<<8)|0xfd,duration=target.definition.he_effect_duration,
                      at=event.at)
        if result.hit is not None and target and target.alive and enemy:
            if self.gravity_mode and direct and shell is not None:
                self._gravity_shell_impulse(target, shell, event.contact_direction or event.direction)
            if direct:
                shooter.direct_hits += 1
                target.hits_received += 1
                if blocked:
                    target.blocked_damage += blocked
                    self.emit('blocked_statistics',target.entity_id,damage=target.blocked_damage)
            if result.outcome=='penetrated':
                shooter.piercings += 1
                target.piercings_received += 1
            self.emit('feedback',event.shooter,target.entity_id,outcome=result.outcome)
        if damage:
            self._damage_health(target, damage, event.shooter, fire_at, shell_hit=True,
                                hit_effect=hit_effect)

    def _damage_health(self, target, amount, source, at, *, shell_hit, hit_effect=None):
        """True clamped HP damage, shared death/stats, no invented impact RPC."""
        if not target.alive:
            return 0
        damage = min(target.health, amount)
        if damage <= 0:
            return 0
        shooter = self.vehicles[source] if source is not None else None
        enemy = shooter is not None and shooter.team != target.team
        if shell_hit and enemy:
            shooter.hits += 1
        if enemy:
            shooter.damage += damage
            self.emit('damage_statistics',shooter.entity_id,damage=shooter.damage)
            sources = [row for part,row in target.track_assist_sources.items()
                       if part in target.tracks and target.tracks[part].broken
                       and row[0] != source and row[0] in self.vehicles
                       and self.vehicles[row[0]].team == shooter.team]
            if sources:
                helper = self.vehicles[max(sources,key=lambda row:row[1])[0]]
                helper.assisted_damage += damage
                self.emit('assisted_statistics',helper.entity_id,damage=helper.assisted_damage)
        target.health -= damage
        self._reset_base_capture(target, source)
        target.damage_received += damage
        self.emit('health', source or 0, target.entity_id, health=target.health,
                  **({'received_damage': damage} if shell_hit and shooter is not None
                     and shooter.entity_id != target.entity_id else {}),
                  **({'hit_effect':hit_effect} if hit_effect is not None else {}))
        if not target.alive:
            if self.recon is not None:
                self.recon.remove_vehicle(target.entity_id)
                self._recon_camouflage_still.pop(target.entity_id, None)
            target.speed = 0.
            target.flags = 0
            target.engine_mode = (0,0)
            if enemy:
                shooter.kills += 1
            target.killer = source or 0
            target.death_at = at
            self._stop_fire(target, at, 'death')
            self._stop_consumables(target)
            respawning = ((self.unlimited_lives or (self.practice and target.account_id is not None))
                          and self._player_attached(target))
            if respawning:
                self.respawn_at[target.entity_id] = at+3.
            self.emit('killed', source or 0, target.entity_id,
                      keep_roster=self.unlimited_lives and respawning)
            if (self.supremacy is not None and not self.finished_at
                    and self.supremacy.finished_winner is None):
                before = self.supremacy.state()
                self.supremacy.destroyed(target.team, enemy)
                self.emit('supremacy', state=self.supremacy.state(), previous=before)
                winner = self.supremacy.winner()
                if winner is not None and self._combat_can_finish():
                    self.finish(winner, 'supremacy', at)
            if self.bot_director is not None:
                self.bot_director.orders.forget_enemy(target.entity_id)
            self._lose_target(target.entity_id)
        return damage

    def _fall_impacts(self, impacts, at):
        """Apply contact-episode fractions through the normal HP/death path."""
        for impact in impacts:
            vehicle = self.vehicles[impact['entity']]
            if not vehicle.alive:
                continue
            raw = (vehicle.max_health*impact['fraction']*(.25 if self.gravity_mode else 1.)
                   + self.fall_hp_carry.get(vehicle.entity_id,0.))
            damage = math.floor(raw+.5)
            self.fall_hp_carry[vehicle.entity_id] = raw-damage
            applied = self._damage_health(vehicle,damage,None,at,shell_hit=False)
            self.emit('fall_impact',vehicle.entity_id,side=impact['kind'],
                      speed=impact['speed'],damage_speed=impact.get('damage_speed',impact['speed']),
                      point=impact['point'],
                      normal=impact['normal'],damage=applied,at=at)

    def _track_fall_impacts(self, impacts, at):
        """Apply measured belt contacts through the ordinary module lifecycle."""
        for impact in impacts:
            vehicle = self.vehicles.get(impact['entity'])
            if vehicle is None or not vehicle.alive:
                continue
            part = impact['track']
            track = vehicle.tracks.get(part)
            if track is None:
                continue
            fraction = evaluate_track_fall(impact['speed'],impact['height'])
            if self.gravity_mode:
                fraction *= .25
            episode = impact.get('episode')
            key = vehicle.entity_id,part
            previous_episode,peak = self.track_fall_peaks.get(key,(None,0.))
            previous = peak if episode is not None and episode == previous_episode else 0.
            if episode is not None:
                self.track_fall_peaks[key] = episode,max(previous,fraction)
            if fraction <= previous:
                continue
            # Quantize the cumulative peak, then take its difference. Rounding
            # every tiny contact increment upward would drain one HP on each
            # solver substep of a single soft landing.
            amount = (math.ceil(track.spec.max_health*fraction)
                      - math.ceil(track.spec.max_health*previous))
            if amount <= 0:
                continue
            self._advance_tracks(vehicle,at)
            before = track.broken,track.critical
            if fraction >= 1.:
                # A new severe peak breaks the belt even if a few repair HP
                # regenerated since the earlier contact in this episode.
                amount = track.health
            applied = min(track.health,amount)
            track.damage(amount,at)
            if applied:
                self.emit('module_hit',0,vehicle.entity_id,shot=0,part=part,
                          damage=applied,health=track.health,at=at,cause='fall')
            if before != (track.broken,track.critical) or track.broken and applied:
                self._module_changed(vehicle, part, 1 if track.broken else 0, at,
                                     track_just_broke=track.broken and not before[0])

    @staticmethod
    def _skid_velocity(previous, yaw, speed, grip_step):
        """Preserve world-space momentum that tracks cannot turn in one tick."""
        fx,fz = math.sin(yaw),math.cos(yaw)
        along = previous[0]*fx+previous[1]*fz
        lateral_x,lateral_z = previous[0]-along*fx,previous[1]-along*fz
        lateral = math.hypot(lateral_x,lateral_z)
        residual = max(0.,lateral-grip_step)
        if lateral > 1e-8:
            factor = residual/lateral
            lateral_x,lateral_z = lateral_x*factor,lateral_z*factor
        vx,vz = speed*fx+lateral_x,speed*fz+lateral_z
        # Turning changes the direction of velocity, not its energy. Engine
        # acceleration may raise the limit through ``speed``; leftover slip
        # alone must never push the tank beyond that limit.
        limit = max(math.hypot(*previous),abs(speed))
        magnitude = math.hypot(vx,vz)
        if magnitude > limit and magnitude > 0.:
            vx,vz = vx*limit/magnitude,vz*limit/magnitude
        return (vx,vz),residual

    def _ground_impact(self, impact, at):
        """Emit one resolved tank-pair impact and apply its ram HP damage.

        Both hulls take the same simultaneous impact: each loses HP for its
        own absorbed energy share and credits the other. Allies and wrecks
        take or deal none; shell mitigation factors do not apply to rams.
        """
        details = dict(impact)
        actor = details.pop('entity_a')
        target = details.pop('entity_b')
        damage = {}
        if details.get('ram'):
            a, b = self.vehicles[actor], self.vehicles[target]
            if a.alive and b.alive and a.team != b.team:
                for vehicle, energy in ((a, details['ram_energy_a']),
                                        (b, details['ram_energy_b'])):
                    protection = getattr(getattr(vehicle.definition,
                                                 'module_protection', None),
                                         'ramming_absorption_percent', 0.)
                    damage[vehicle.entity_id] = min(vehicle.health,
                        int(self.RAM_DAMAGE_PER_KILOJOULE*energy/1000
                            *(1-protection/100)+.5))
            details.update(ram_damage_a=damage.get(actor, 0),
                           ram_damage_b=damage.get(target, 0))
        self.emit('ground_impact', actor, target, **details)
        for entity, source in ((actor, target), (target, actor)):
            if damage.get(entity):
                self._damage_health(self.vehicles[entity], damage[entity], source, at,
                                    shell_hit=False)

    @staticmethod
    def _shield_factor(vehicle, at):
        # Window activated_at <= at < active_until at the impact itself, never
        # the firing time or tick end; stop() on death/battle end truncates it.
        return min((s.state_at(at).damage_taken_factor for s in vehicle.consumables
                    if s and s.activated_at<=at), default=1.)

    @staticmethod
    def _armor_mover_factor(vehicle, at):
        """ArmorMover raw73 protects from every shell at impact time only."""
        return min((s.state_at(at).all_shell_damage_factor for s in vehicle.consumables
                    if s and s.activated_at<=at), default=1.)

    @staticmethod
    def _powered_acceleration(vehicle, forward, *, speed=None, resistance=1., mode_power=1.):
        """Return the grounded drive acceleration for a complete vehicle definition.

        XML exposes vehicle mass and engine horsepower, but not the native
        gearbox/traction equation.  This is deliberately a small, explicit
        local drivetrain rather than a claim to have recovered that equation:
        it caps launch acceleration at 5 m/s² and limits longitudinal force
        by engine power with a fixed low-gear speed. Thus a light, powerful
        vehicle keeps accelerating longer than a heavy vehicle with the same
        caps, while the existing collision and airborne solvers stay intact.

        ``None`` preserves the legacy kinematic path for minimal fixtures and
        incomplete definitions.  It is also used for braking: XML brake-force
        units have not yet been calibrated against a native trace, so applying
        them here would silently make stopping behaviour worse.
        """
        definition = getattr(vehicle, 'definition', None)
        drive = getattr(definition, 'drive', None)
        mass = getattr(definition, 'mass', None)
        power_hp = getattr(drive, 'engine_power_hp', None)
        if (forward not in (-1, 1) or isinstance(mass, bool)
                or not isinstance(mass, (int, float)) or not math.isfinite(mass) or mass <= 0
                or isinstance(power_hp, bool) or not isinstance(power_hp, (int, float))
                or not math.isfinite(power_hp) or power_hp <= 0):
            return None
        # Timed boosts multiply the currently installed engine only.  Permanent
        # fuel/oil modifications are already baked into ``engine_power_hp`` by
        # apply_definition(), so they must not be applied twice here.
        factor = vehicle.engine_factor * (sandbox.BOOST_POWER if getattr(vehicle,'sandbox_boost',False) else 1.)
        factor *= mode_power
        if (isinstance(factor, bool) or not isinstance(factor, (int, float))
                or not math.isfinite(factor) or factor <= 0):
            return None
        power_watts = power_hp * 735.49875 * factor
        # The fixed low gear avoids a singular P/v at rest while retaining
        # the effect of power/mass during launch. Terrain cannot amplify P.
        speed = vehicle.speed if speed is None else speed
        return forward*engine_acceleration(power_watts,mass,speed,resistance)

    def _terrain_mobility(self, vehicle):
        drive = getattr(vehicle.definition,'drive',None)
        coefficients = getattr(drive,'terrain_resistance',None)
        if coefficients is None:
            return TerrainMobility(1.,1.,1,'unavailable')
        sampler = getattr(self.terrain,'mobility_index',None)
        index = (sampler(vehicle.pose.position[0],vehicle.pose.position[2])
                 if callable(sampler) else None)
        try:
            fallback = resolve_terrain_mobility(coefficients)
        except ValueError:
            return TerrainMobility(1.,1.,1,'unavailable')
        if index is None:
            return fallback
        try:
            return resolve_terrain_mobility(coefficients,index)
        except ValueError:
            return fallback

    def _drive_pitch(self, vehicle):
        """Road slope for forces, independent of suspension body lean."""
        sampler = getattr(self.world,'drive_pitch',None) if self.world else None
        return sampler(vehicle.entity_id) if callable(sampler) else vehicle.pose.pitch

    @staticmethod
    def _braking_acceleration(vehicle):
        """Return a bounded local braking deceleration in m/s².

        Pinned 11.21 converts chassis ``brakeForce`` through ``g`` before its
        own controller, so force divided by assembled mass is a useful local
        starting point.  It is not the complete recovered controller: stock
        XML includes sentinel-scale outliers (near zero and hundreds of m/s²).
        Keep those on the established 12 m/s² fallback rather than allowing a
        malformed or not-yet-understood definition to make a vehicle coast
        indefinitely or stop in a single fixed step.
        """
        definition = getattr(vehicle, 'definition', None)
        drive = getattr(definition, 'drive', None)
        mass = getattr(definition, 'mass', None)
        brake_force = getattr(drive, 'brake_force', None)
        if (isinstance(mass, bool) or not isinstance(mass, (int, float))
                or not math.isfinite(mass) or mass <= 0
                or isinstance(brake_force, bool) or not isinstance(brake_force, (int, float))
                or not math.isfinite(brake_force) or brake_force <= 0):
            return 12.
        deceleration = 9.81 * brake_force / mass
        # The broad stock catalog has proven values below 0.001 and above 179.
        # 1–20 covers the measured M41/T30/Maus scale while retaining the
        # former behaviour for anomalous definitions and fast light vehicles.
        return deceleration if 1. <= deceleration <= 20. else 12.

    @staticmethod
    def _slope_grip_acceleration(assets, pitch, gravity):
        """Longitudinal grip limit beyond the pinned chassis climb angle.

        This is a local traction approximation, not a recovered surface or
        gearbox coefficient. Below the effective grip angle the ordinary
        drive and brake response is unchanged. Above it, tracks cannot hold against
        the downhill component of gravity, even with W or S held.
        """
        limit = getattr(assets, 'max_slope', None)
        if (not isinstance(limit, (int, float)) or isinstance(limit, bool)
                or not math.isfinite(limit) or not 0 < limit < math.pi/2
                or not math.isfinite(pitch) or not math.isfinite(gravity)
                or gravity <= 0):
            return None
        grip_coefficient = math.tan(limit)*Arena.SLOPE_GRIP_FACTOR
        if abs(pitch) <= math.atan(grip_coefficient):
            return None
        return gravity * grip_coefficient * math.cos(pitch)**2

    def _player_attached(self, vehicle):
        player = self.players.get(vehicle.account_id)
        return bool(player and player.attached or vehicle.entity_id in self.training_bots)

    def _vehicle_airborne(self, vehicle):
        return bool(self.world is not None
                    and not self.world.motion[vehicle.entity_id].grounded)

    def _observe_landing(self, vehicle, now):
        """Start a landing skid from the actual touchdown tangent velocity.

        Idempotent per tick. A flight is timed from the first tick it is
        seen airborne; a brief re-hop during a skid only reseeds its velocity.
        """
        if self.world is None:
            return
        entity = vehicle.entity_id
        if self._vehicle_airborne(vehicle):
            self._landing_airborne_since.setdefault(entity,now)
            return
        since = self._landing_airborne_since.pop(entity,None)
        if since is None:
            return
        if (any(recovered == entity for recovered,*_ in
                getattr(self.world,'dynamic_recoveries',()))
                or any(getattr(vehicle.tracks.get(part),'broken',False)
                       for part in ('chassis_track_L','chassis_track_R'))):
            self._landing_skids.pop(entity,None)
            return
        vx,_,vz = self.world.motion[entity].velocity
        skid = touchdown_skid((vx,vz),vehicle.pose.yaw,now-since)
        current = self._landing_skids.get(entity)
        if skid is not None:
            self._landing_skids[entity] = skid
        elif current is not None:
            current.velocity = (vx,vz)

    def _update_airborne_control_episode(self, vehicle, airborne, now):
        """Give a held key a bounded lease across flight and touchdown.

        The pinned client usually repeats control-state RPCs, but a jump can
        outlast the normal input lease.  In-air controls do not add track
        force; preserving the last held state lets the drivetrain restart on
        touchdown.  Explicit key-up, session detach, and the normal lease still
        stop the hull, while the episode grace is capped at eight seconds.
        """
        if not airborne:
            if (vehicle.airborne_control_active and vehicle.flags and vehicle.alive
                    and self._player_attached(vehicle)):
                # A long fall can outlast the flight lease before the first
                # grounded drive tick. Keep a continuously held key alive for
                # one bounded grace interval after touchdown; key-up still
                # clears flags in control() and does not receive this renewal.
                vehicle.airborne_input_until = max(
                    vehicle.airborne_input_until, now+self.AIRBORNE_CONTROL_GRACE)
            vehicle.airborne_control_active = False
            return
        if not vehicle.airborne_control_active:
            vehicle.airborne_control_active = True
            if vehicle.flags and vehicle.alive and self._player_attached(vehicle):
                vehicle.airborne_input_until = now + self.AIRBORNE_CONTROL_GRACE

    def _active_control_flags(self, vehicle, now):
        player_active = self._player_attached(vehicle)
        fresh = now - vehicle.last_input < self.CONTROL_INPUT_TIMEOUT
        airborne_grace = now < vehicle.airborne_input_until
        return (vehicle.flags if vehicle.alive and player_active
                and (fresh or airborne_grace) else 0)

    def _drive_observability(self, vehicle, now=None):
        """Return trace-only inputs to the local longitudinal model.

        This intentionally mirrors the current sandbox controller without
        changing it. Native traces can therefore distinguish a client-derived
        fact from a value selected by the local fallback.
        """
        definition = getattr(vehicle, 'definition', None)
        drive = getattr(definition, 'drive', None)
        mass = getattr(definition, 'mass', None)
        power_hp = getattr(drive, 'engine_power_hp', None)
        brake_force = getattr(drive, 'brake_force', None)
        finite_mass = (isinstance(mass, (int, float)) and not isinstance(mass, bool)
                       and math.isfinite(mass) and mass > 0)
        finite_brake = (isinstance(brake_force, (int, float)) and not isinstance(brake_force, bool)
                        and math.isfinite(brake_force) and brake_force > 0)
        raw_brake = 9.81 * brake_force / mass if finite_mass and finite_brake else None
        brake_fallback = raw_brake is None or not 1. <= raw_brake <= 20.
        flags = vehicle.flags if vehicle.alive else 0
        active_flags = self._active_control_flags(vehicle, now) if now is not None else flags
        forward = int(bool(flags & 1)) - int(bool(flags & 2))
        active_forward = int(bool(active_flags & 1)) - int(bool(active_flags & 2))
        assets = self.world.assets_for(vehicle.entity_id) if self.world else None
        driver_factor = self._crew_role_factor(vehicle, 'driver')
        if self.gravity_mode:
            driver_factor *= 1.35
        if getattr(vehicle, 'sandbox_boost', False):
            driver_factor *= sandbox.BOOST_SPEED
        top_forward = ((assets.forward_speed if assets else 11.5)
                       + vehicle.forward_speed_bias) * driver_factor
        top_reverse = ((assets.reverse_speed if assets else 5.)
                       + vehicle.reverse_speed_bias) * driver_factor
        gravity = getattr(self.world, 'GRAVITY', None) if self.world else None
        pitch = self._drive_pitch(vehicle)
        surface_cos = max(.05,math.cos(pitch))
        mobility = self._terrain_mobility(vehicle)
        slope = 0.
        if (isinstance(gravity, (int, float)) and not isinstance(gravity, bool)
                and math.isfinite(gravity) and gravity > 0):
            slope = gravity * math.sin(pitch) * math.cos(pitch)
        powered = self._powered_acceleration(vehicle,forward,
            speed=vehicle.speed/surface_cos,resistance=mobility.resistance,
            mode_power=1.5 if self.gravity_mode else 1.) if forward else None
        surface_code = None
        sampler = getattr(self.terrain, 'surface_code', None)
        if callable(sampler):
            surface_code = sampler(vehicle.pose.position[0], vehicle.pose.position[2])
        return dict(mass=mass if finite_mass else None,
                    engine_power_hp=(power_hp if isinstance(power_hp, (int, float))
                                     and not isinstance(power_hp, bool) and math.isfinite(power_hp)
                                     else None),
                    forward_input=forward,
                    effective_input_flags=active_flags,
                    effective_forward_input=active_forward,
                    input_age_seconds=(max(0., now - vehicle.last_input)
                                       if now is not None else None),
                    airborne_grace_seconds=(max(0., vehicle.airborne_input_until - now)
                                            if now is not None else None),
                    powered_acceleration=(powered*surface_cos if powered is not None else None),
                    raw_brake_acceleration=raw_brake,
                    brake_acceleration=self._braking_acceleration(vehicle),
                    brake_fallback=brake_fallback,
                    forward_cap=top_forward, reverse_cap=top_reverse,
                    driver_factor=driver_factor, slope_acceleration=slope,
                    road_pitch=pitch,surface_speed=vehicle.speed/surface_cos,
                    engine_mode=vehicle.engine_mode,
                    terrain_resistance=mobility.resistance,
                    terrain_class=mobility.surface_index,surface_source=mobility.source,
                    surface_code=surface_code)

    def _drive(self, vehicle, dt, now):
        """Original synchronous entry point, also used by focused fixtures."""
        flow = self._drive_steps(vehicle,dt,now)
        try:
            args,options = next(flow)
        except StopIteration as done:
            return done.value
        result = self.world.move(*args,**options)
        try:
            flow.send(result)
        except StopIteration as done:
            return done.value
        raise RuntimeError('Drive yielded more than one movement request')

    def _drive_steps(self, vehicle, dt, now):
        initial_yaw = vehicle.pose.yaw
        airborne = self._vehicle_airborne(vehicle)
        self._update_airborne_control_episode(vehicle, airborne, now)
        self._observe_landing(vehicle, now)
        player = self.players.get(vehicle.account_id)
        controlled = self._player_attached(vehicle)
        ground_mode = (self.world is not None and
                       getattr(self.world, 'experimental_ground_impacts', False))
        flags = (self._active_control_flags(vehicle, now)
                 if controlled or not ground_mode else 0)
        if self.gravity_mode and airborne and vehicle.alive:
            self._gravity_air_control(vehicle, flags, dt)
        turn = int(bool(flags&8))-int(bool(flags&4))
        forward = int(bool(flags&1))-int(bool(flags&2))
        if forward<0:
            turn = -turn
        ground_motion = (self.world.ground_motion.get(vehicle.entity_id)
                         if ground_mode and not airborne else None)
        ground_motion_active = bool(ground_motion and ground_motion.active
                                    and ground_motion.velocity is not None)
        if ground_motion_active:
            forward_axis = (math.sin(vehicle.pose.yaw), math.cos(vehicle.pose.yaw))
            vehicle.speed = (ground_motion.velocity[0]*forward_axis[0]
                             + ground_motion.velocity[1]*forward_axis[1])
        left_broken = bool(getattr(vehicle.tracks.get('chassis_track_L'),'broken',False))
        right_broken = bool(getattr(vehicle.tracks.get('chassis_track_R'),'broken',False))
        broken = left_broken or right_broken
        track_motion = self._track_drive_motion.get(vehicle.entity_id)
        if airborne:
            # Re-seed from the physical landing velocity, never a stale
            # pre-flight planar episode. No contact damping acts in flight.
            self._track_drive_motion.pop(vehicle.entity_id,None)
            track_motion = None
        elif broken and track_motion is None:
            fx,fz = math.sin(vehicle.pose.yaw),math.cos(vehicle.pose.yaw)
            physical = self.world.motion[vehicle.entity_id].velocity if self.world else (0.,0.,0.)
            lateral = physical[0]*fz-physical[2]*fx
            velocity = (ground_motion.velocity if ground_motion_active else
                        (fx*vehicle.speed+fz*lateral,fz*vehicle.speed-fx*lateral))
            yaw_rate = self._track_drive_yaw_rates.get(vehicle.entity_id,0.)
            if ground_motion_active:
                yaw_rate = ground_motion.yaw_rate+self._track_drive_track_rates.get(vehicle.entity_id,0.)
            track_motion = TrackDriveMotion(velocity,yaw_rate)
            self._track_drive_motion[vehicle.entity_id] = track_motion
        else:
            if track_motion is not None and ground_motion_active:
                track_motion.velocity = ground_motion.velocity
                track_motion.yaw_rate = ground_motion.yaw_rate
        if track_motion is not None:
            fx,fz = math.sin(vehicle.pose.yaw),math.cos(vehicle.pose.yaw)
            vehicle.speed = track_motion.velocity[0]*fx+track_motion.velocity[1]*fz
        # Broken-belt contact owns its own momentum; flight owns Bullet's.
        if broken or track_motion is not None:
            self._landing_skids.pop(vehicle.entity_id,None)
        landing = None if airborne else self._landing_skids.get(vehicle.entity_id)
        if landing is not None:
            if ground_motion_active:
                landing.velocity = ground_motion.velocity
            vehicle.speed = (landing.velocity[0]*math.sin(vehicle.pose.yaw)
                             + landing.velocity[1]*math.cos(vehicle.pose.yaw))
        previous_speed = vehicle.speed
        assets = self.world.assets_for(vehicle.entity_id) if self.world else None
        pitch = self._drive_pitch(vehicle)
        surface_cos = max(.05,math.cos(pitch)) if not airborne else 1.
        mobility = self._terrain_mobility(vehicle)
        if broken:
            forward = turn = 0
        engine = self.engines.get(vehicle.entity_id)
        if engine is not None and engine.destroyed:
            # A destroyed engine is immobilized on the ground. In flight,
            # retain Bullet's existing momentum/gravity path without a snap.
            forward = turn = 0
            if not airborne and not ground_motion_active and landing is None:
                vehicle.speed = 0.
        drive_mask = flags & 15 if forward or turn else 0
        vehicle.engine_mode = ((0,0) if not vehicle.alive or engine is not None and engine.destroyed
                               else (3,drive_mask) if drive_mask else (1,0))
        # Timed speed biases add once to the permanent caps (which already
        # carry gear oil); shared assets and the definition are never changed.
        # The pinned client exposes crew injury state. The official client
        # support rule explicitly halves a shell-shocked driver's top speed;
        # it does not provide a numeric acceleration or turn-rate multiplier.
        # Apply only the documented cap here. The factor is queried from the
        # per-vehicle CrewState on every fixed step, so a medkit restores the
        # current drive without mutating shared assets.
        driver_factor = self._crew_role_factor(vehicle, 'driver')
        if self.gravity_mode:
            driver_factor *= 1.35
        boost = getattr(vehicle, 'sandbox_boost', False)
        if boost:
            driver_factor *= sandbox.BOOST_SPEED
        top_forward = ((assets.forward_speed if assets else 11.5) + vehicle.forward_speed_bias) * driver_factor
        top_reverse = ((assets.reverse_speed if assets else 5.) + vehicle.reverse_speed_bias) * driver_factor
        target = forward*(top_forward if forward>0 else top_reverse)*surface_cos
        # Turn rate takes only the timed rotation factor, never the engine one.
        turn_rate = ((assets.rotation_speed if assets else math.radians(35))
                     *vehicle.rotation_factor*mobility.turn_factor)
        # A complete native definition uses the bounded local P/v drivetrain.
        # Braking now has a separate guarded force/mass approximation; neither
        # branch changes gravity or collision response.
        drive = getattr(vehicle.definition, 'drive', None)
        fuel = drive.power_bonus_factor if drive is not None else 1.
        acceleration = (self._braking_acceleration(vehicle)
                        if not forward or vehicle.speed*forward<0 else None)
        if acceleration is None:
            mode_power = 1.5 if self.gravity_mode else 1.
            powered = self._powered_acceleration(vehicle,forward,
                speed=vehicle.speed/surface_cos,resistance=mobility.resistance,mode_power=mode_power)
            acceleration = (abs(powered) if powered is not None else
                            5*fuel*vehicle.engine_factor*mode_power*(sandbox.BOOST_POWER if boost else 1.))
        if broken:
            # An input lease expiring is not a brake impulse. The broken
            # belts supply their passive contact drag below, including when
            # both are torn; they cannot power or pivot a stopped hull.
            acceleration = 0.
        # Engine/brake forces act along the road. `speed` is horizontal,
        # while engine power and the stock speed cap describe surface speed.
        acceleration *= surface_cos
        track_yaw_delta = turn*turn_rate*dt
        yaw_delta = track_yaw_delta
        planar_displacement = None
        planar_velocity = None
        damped_ground_yaw_rate = 0.
        if ground_mode and not airborne and track_motion is None:
            if ground_motion_active:
                damped_ground_yaw_rate = (ground_motion.yaw_rate *
                    math.exp(-self.world.GROUND_YAW_DAMPING*dt))
                yaw_delta += damped_ground_yaw_rate*dt
        drive_acceleration = 0.
        if not airborne:
            # `speed` is horizontal metres/second along the hull heading.  The
            # support solver already fitted the hull's pitch from its four
            # carrying points, so gravity's matching horizontal longitudinal
            # component is g*sin(pitch)*cos(pitch).  The opt-in lateral
            # momentum layer handles turning slip; cross-slope gravity and
            # ground material grip remain separate future work.
            slope = 0.
            rolling_step = 0.
            gravity = getattr(self.world, 'GRAVITY', None)
            if (isinstance(pitch, (int, float)) and not isinstance(pitch, bool)
                    and math.isfinite(pitch) and isinstance(gravity, (int, float))
                    and not isinstance(gravity, bool) and math.isfinite(gravity) and gravity > 0):
                slope = gravity*math.sin(pitch)*math.cos(pitch)
                grip = self._slope_grip_acceleration(assets, pitch, gravity)
                if grip is not None:
                    acceleration = min(acceleration, grip)
                rolling_step = rolling_acceleration(mobility.resistance,pitch,gravity)*dt
                if (broken and abs(vehicle.speed) < .05
                        and abs(track_motion.yaw_rate) < .02 and grip is None):
                    # A torn belt dissipates a moving hull's momentum, but a
                    # stopped hull still has static brake contact. Do not let
                    # an arbitrarily gentle grade start a perpetual pivot.
                    rolling_step += min(abs(slope), self._braking_acceleration(vehicle))*dt
            external = vehicle.speed+slope*dt
            external -= math.copysign(min(abs(external),rolling_step),external)
            traction_step = max(-acceleration*dt,min(acceleration*dt,target-external))
            drive_acceleration = traction_step/dt
            vehicle.speed = external+traction_step
        if track_motion is not None:
            before_drag = vehicle.speed
            passive = passive_track_step(vehicle.speed,track_motion.yaw_rate,
                                         left_broken,right_broken,dt)
            vehicle.speed = passive.speed
            drive_acceleration += (vehicle.speed-before_drag)/dt
            yaw_delta = track_yaw_delta+passive.yaw_delta
            track_motion.yaw_rate = passive.yaw_rate
            # Contact changes the existing world momentum, rather than
            # rotating its entire magnitude along with the hull in one tick.
            fx,fz = math.sin(vehicle.pose.yaw),math.cos(vehicle.pose.yaw)
            lateral = track_motion.velocity[0]*fz-track_motion.velocity[1]*fx
            rate = BOTH_DRAG_RATE if left_broken and right_broken else COAST_DRAG_RATE if broken else 0.
            lateral *= math.exp(-rate*dt)
            velocity = (fx*vehicle.speed+fz*lateral,fz*vehicle.speed-fx*lateral)
            middle_yaw = vehicle.pose.yaw+yaw_delta*.5
            along = velocity[0]*math.sin(middle_yaw)+velocity[1]*math.cos(middle_yaw)
            grip = getattr(self.world,'GROUND_LATERAL_GRIP',17.658) if self.world else 17.658
            planar_velocity,residual_slip = self._skid_velocity(
                velocity,middle_yaw,along,grip*dt)
            planar_displacement = tuple(v*dt for v in planar_velocity)
        effort = getattr(self.world,'set_drive_effort',None) if self.world else None
        if callable(effort):
            effort(vehicle.entity_id,
                   drive_acceleration,
                   0. if airborne else vehicle.speed*yaw_delta/dt,
                   forward)
        if track_motion is None:
            residual_slip = 0.
        if landing is not None:
            # Drivetrain, slope and rolling act along the hull as an impulse
            # on the touchdown momentum; reduced side grip then removes the
            # lateral part. Turning cannot re-label sideways speed as forward.
            fx,fz = math.sin(vehicle.pose.yaw),math.cos(vehicle.pose.yaw)
            push = vehicle.speed-previous_speed
            velocity = (landing.velocity[0]+fx*push,landing.velocity[1]+fz*push)
            middle_yaw = vehicle.pose.yaw+yaw_delta*.5
            along = velocity[0]*math.sin(middle_yaw)+velocity[1]*math.cos(middle_yaw)
            grip = getattr(self.world,'GROUND_LATERAL_GRIP',17.658) if self.world else 17.658
            planar_velocity,residual_slip = self._skid_velocity(velocity,middle_yaw,
                along,grip*grip_fraction(landing.elapsed)*dt)
            planar_displacement = (planar_velocity[0]*dt,planar_velocity[1]*dt)
        elif ground_mode and not airborne and track_motion is None:
            old_velocity = (ground_motion.velocity if ground_motion_active else
                (math.sin(vehicle.pose.yaw)*previous_speed,
                 math.cos(vehicle.pose.yaw)*previous_speed))
            planar_velocity,residual_slip = self._skid_velocity(old_velocity,
                vehicle.pose.yaw+yaw_delta*.5,vehicle.speed,
                self.world.GROUND_LATERAL_GRIP*dt)
            planar_displacement = (planar_velocity[0]*dt,planar_velocity[1]*dt)
        if self.world:
            start_pose = vehicle.pose
            if ground_mode or track_motion is not None or landing is not None:
                vehicle.pose,moved = yield ((vehicle.speed*dt,yaw_delta),
                    dict(entity=vehicle.entity_id,dt=dt,
                         planar_displacement=planar_displacement,
                         planar_velocity=planar_velocity,
                         track_yaw_rate=track_yaw_delta/dt))
            else:
                vehicle.pose,moved = yield ((vehicle.speed*dt,yaw_delta),
                    dict(entity=vehicle.entity_id,dt=dt))
            state = self.world.motion[vehicle.entity_id]
            self._update_airborne_control_episode(vehicle, not state.grounded, now)
            if airborne != (not state.grounded):
                self.emit('landed' if state.grounded else 'airborne',vehicle.entity_id,
                          position=vehicle.pose.position,velocity=state.velocity,at=now)
            if airborne or not state.grounded:
                # Tracks have no traction in flight. Keep the physical
                # horizontal velocity, including any loss against an obstacle.
                vx,_,vz = state.velocity
                vehicle.speed = vx*math.sin(vehicle.pose.yaw)+vz*math.cos(vehicle.pose.yaw)
            elif ground_mode:
                if callable(getattr(self.world,'drive_pitch',None)):
                    # The support solver rotates physical velocity at road
                    # curvature. A mean position delta would reinsert energy
                    # removed at the start of a ramp or a blocked contact.
                    actual_velocity = state.velocity[0],state.velocity[2]
                else:
                    actual_velocity = ((vehicle.pose.position[0]-start_pose.position[0])/dt,
                                       (vehicle.pose.position[2]-start_pose.position[2])/dt)
                actual_yaw_rate = (track_motion.yaw_rate if track_motion is not None
                                   else damped_ground_yaw_rate)
                ground_motion.velocity = actual_velocity
                ground_motion.yaw_rate = actual_yaw_rate
                ground_motion.active = (
                    residual_slip > .001 or abs(actual_yaw_rate) > .01)
                if not ground_motion.active:
                    ground_motion.velocity = (0.,0.)
                    ground_motion.yaw_rate = 0.
                vehicle.speed = (actual_velocity[0]*math.sin(vehicle.pose.yaw)
                                 + actual_velocity[1]*math.cos(vehicle.pose.yaw))
            elif callable(getattr(self.world,'drive_pitch',None)):
                vehicle.speed = (state.velocity[0]*math.sin(vehicle.pose.yaw)
                                 + state.velocity[2]*math.cos(vehicle.pose.yaw))
            elif not moved and vehicle.speed:
                vehicle.speed = 0.
        else:
            pose = vehicle.pose
            yaw = (pose.yaw+yaw_delta+math.pi)%math.tau-math.pi
            dx,dz = (planar_displacement if planar_displacement is not None
                     else (math.sin(yaw)*vehicle.speed*dt,math.cos(yaw)*vehicle.speed*dt))
            x,z = pose.position[0]+dx,pose.position[2]+dz
            vehicle.pose = Pose((x,self.terrain.height(x,z),z),yaw)
            moved = True
        actual_yaw_rate = wrap(vehicle.pose.yaw-initial_yaw)/dt
        self._track_drive_yaw_rates[vehicle.entity_id] = actual_yaw_rate
        self._track_drive_track_rates[vehicle.entity_id] = actual_yaw_rate-damped_ground_yaw_rate
        if track_motion is not None:
            # A wall that blocks this turn must also remove its stored yaw
            # momentum. Do not accumulate an invisible turn behind geometry.
            expected = yaw_delta/dt
            if abs(actual_yaw_rate-expected) > 1e-5:
                # Keep the end-of-step damping. A partially blocked turn
                # scales that end rate, rather than replacing it with this
                # slice's larger average angular rate.
                passive_average = passive.yaw_delta/dt
                observed = actual_yaw_rate-track_yaw_delta/dt
                fraction = (max(0., min(1., observed/passive_average))
                            if abs(passive_average) > 1e-9 else 0.)
                track_motion.yaw_rate = passive.yaw_rate*fraction
                if ground_motion is not None:
                    ground_motion.yaw_rate = track_motion.yaw_rate
            if self.world:
                state = self.world.motion[vehicle.entity_id]
                track_motion.velocity = (state.velocity[0],state.velocity[2])
            else:
                track_motion.velocity = planar_velocity
            vehicle.speed = (track_motion.velocity[0]*math.sin(vehicle.pose.yaw)
                             +track_motion.velocity[1]*math.cos(vehicle.pose.yaw))
            if not broken and abs(track_motion.yaw_rate) < .01 and residual_slip < .001:
                self._track_drive_motion.pop(vehicle.entity_id,None)
        if landing is not None:
            # Keep only the momentum that survived contact (walls, slopes).
            if self.world:
                state = self.world.motion[vehicle.entity_id]
                landing.velocity = (state.velocity[0],state.velocity[2])
            else:
                landing.velocity = planar_velocity
            landing.elapsed += dt
            if residual_slip < .001 or landing.elapsed >= LANDING_MAX_DURATION:
                self._landing_skids.pop(vehicle.entity_id,None)
        # Swept short flights can land inside move(), without dynamic_commit.
        self._observe_landing(vehicle, now)
        return moved

    def _ensure_drive_workers(self):
        if getattr(self.world, '_native_physics', None) is not None:
            return
        if (self._drive_workers_attempted or not isinstance(self.world,CollisionWorld)
                or not self.world.build_ready
                or sum(self._player_attached(v) for v in self.vehicles.values()) < 6):
            return
        now = time.monotonic()
        if now < self._drive_workers_retry_at:
            return
        from .drive_workers import configured_workers, DriveWorkers, WorkerCapacityError, has_worker_capacity
        count = configured_workers()
        adaptive = os.environ.get('SOLBREAKER_SIM_WORKERS', 'auto') == 'auto'
        if not count:
            self._drive_workers_attempted = True
            return
        # Another arena can temporarily own the bounded process capacity.
        # Avoid constructing a replica until it is available, and poll at a
        # bounded rate instead of doing work on every simulation step.
        if not has_worker_capacity(1 if adaptive else count):
            self._drive_workers_retry_at = now + 1.
            return
        try:
            from .drive_replica import build_spec
            spec = build_spec(self.world)
            self.drive_workers = (DriveWorkers(spec,count,allow_partial=True) if adaptive
                                  else DriveWorkers(spec,count))
        except WorkerCapacityError:
            # Capacity may have changed since the cheap check above.
            self._drive_workers_retry_at = now + 1.
        except Exception:
            self._drive_workers_attempted = True
            self.parallel_drive_stats['startup_failed'] += 1
        else:
            self._drive_workers_attempted = True

    def _drive_vehicles(self, vehicles, dt, now):
        world = self.world
        if getattr(world, '_native_physics', None) is not None:
            self._last_drive_parallel = False
            return {v.entity_id:self._drive(v,dt,now) for v in vehicles}
        adaptive = (os.environ.get('SOLBREAKER_SIM_WORKERS','auto') == 'auto'
            or os.environ.get('SOLBREAKER_ADAPTIVE_DRIVE') == '1')
        pool = self.drive_workers
        if (not adaptive or type(self) is not Arena or type(world) is not CollisionWorld
                or getattr(world,'_rapier',None) is None
                or not getattr(getattr(world,'_rust_drive',None),'_pipeline_enabled',False)
                or pool is None or not pool.ready):
            return Arena._drive_vehicles_impl(self,vehicles,dt,now)
        from .drive_workers import DriveDispatchPolicy
        policy = getattr(self,'_drive_dispatch_policy',None)
        if policy is None:
            policy = self._drive_dispatch_policy = DriveDispatchPolicy()
        mode = policy.choose()
        before = time.perf_counter_ns()
        if mode == 'serial':
            self._ensure_drive_workers()
            if self.drive_workers is not None:
                self.drive_workers.poll()
            self._last_drive_parallel = False
            result = {v.entity_id:self._drive(v,dt,now) for v in vehicles}
        else:
            result = Arena._drive_vehicles_impl(self,vehicles,dt,now)
        policy.observe('parallel' if self._last_drive_parallel else 'serial',
                       time.perf_counter_ns()-before)
        return result

    def _drive_vehicles_impl(self, vehicles, dt, now):
        """Prepare own-vehicle inputs, then commit moves in the original order.

        Workers execute the existing solver speculatively. Replies share one
        frame barrier of at most 2 ms; missing or rejected results use world.move.
        Damage, destruction, ramming and dynamic integration stay on the owner.
        """
        world = self.world
        self._ensure_drive_workers()
        pool = self.drive_workers
        if pool is not None:
            pool.poll()
        dynamic = (getattr(world, 'dynamic_bodies', None)
                   or getattr(world, 'pending_dynamics', None))
        independent_dynamic = (getattr(world, '_rapier', None) is not None
            and getattr(world, 'rapier_parallel_drive', False))
        if (pool is None or not pool.ready or (dynamic and not independent_dynamic)):
            self._last_drive_parallel = False
            return {v.entity_id:self._drive(v,dt,now) for v in vehicles}
        self._last_drive_parallel = True

        from dataclasses import replace
        from .drive_jobs import DriveJob, capture_state
        from .drive_replica import accept_reply, sensor_rect
        flows, jobs = [], []
        for vehicle in vehicles:
            flow = self._drive_steps(vehicle,dt,now)
            try:
                args,options = next(flow)
            except StopIteration as done:
                flows.append((vehicle,None,None,None,done.value))
                continue
            entity = vehicle.entity_id
            flows.append((vehicle,flow,args,options,None))
            state = world.motion[entity]
            seen = world._synced.get(entity)
            if (state.grounded and entity not in world.dynamic_bodies
                    and entity not in world.pending_dynamics and seen is not None
                    and seen[0] == world.poses[entity] and seen[1] is state):
                jobs.append(DriveJob(entity,world.poses[entity],replace(state),
                    args[0],args[1],options,capture_state(world,entity),
                    world._drive_efforts.get(entity,(0.,0.,0.)),world.gun_angles[entity]))
        # Reserve the head for the owner: its computation overlaps tail jobs.
        reserve = max(1,math.ceil(len(jobs)/(pool.count+1)))
        tail = jobs[reserve:]
        revision = frozenset(world.destroyed)
        # A speculative proposal whose current sensor already overlaps a
        # neighbour is very likely to fail the later footprint admission.
        # Leave such moves to the authoritative owner; never skip a move.
        # This is only a hint: neighbours can move before admission.
        bounds = None
        if tail:
            try:
                initial = {entity:sensor_rect(world,entity) for entity in world.bodies}
                gap = world.GROUND_CONTACT_GAP
                def separate(job):
                    x0,z0,x1,z1 = initial[job.entity]
                    return all(other == job.entity or x1 < a-gap or x0 > c+gap
                               or z1 < b-gap or z0 > d+gap
                               for other,(a,b,c,d) in initial.items())
                filtered = [job for job in tail if separate(job)]
            except Exception:
                # The speculative optimisation must not prevent the normal
                # submission path if the preliminary bounds query fails.
                self.parallel_drive_stats['prefilter_probe_failed'] += 1
            else:
                self.parallel_drive_stats['prefilter_skipped'] += len(tail)-len(filtered)
                tail = filtered
                bounds = initial
        generation = pool.submit(tail,dict(world.poses),dict(world.gun_angles),revision)
        # Only this owner changes sensors during the atomic drive phase.
        # Cache actual native bounds once, and refresh after every commit or
        # local move before admitting the next actor's proposal.
        if bounds is None:
            bounds = {entity:sensor_rect(world,entity) for entity in world.bodies}
        results = {}
        for vehicle,flow,args,options,done_value in flows:
            entity = vehicle.entity_id
            if flow is None:
                results[entity] = done_value
                continue
            reply = pool.take(generation,entity)
            accepted = (reply is not None and reply.accepted
                        and revision == world.destroyed
                        and accept_reply(world,reply,other_bounds=bounds,transfer=True))
            if accepted:
                self.parallel_drive_stats['accepted'] += 1
                result = reply.pose,reply.moved
            else:
                self.parallel_drive_stats['local'] += 1
                if reply is not None:
                    self.parallel_drive_stats['rejected'] += 1
                result = world.move(*args,**options)
            bounds[entity] = sensor_rect(world,entity)
            try:
                flow.send(result)
            except StopIteration as done:
                results[entity] = done.value
            else:
                raise RuntimeError('Drive yielded more than one movement request')
        return results

    def _aim(self, vehicle, dt, previous_yaw=None):
        if vehicle.autoaim_target:
            target = self.vehicles.get(vehicle.autoaim_target)
            if (target is None or not target.alive or
                    target.account_id is not None and not self.players[target.account_id].attached):
                lost = vehicle.autoaim_target
                vehicle.autoaim_target,vehicle.autoaim_offset = 0,None
                vehicle.gun.set_angles(vehicle.gun.yaw,vehicle.gun.pitch)
                self.emit('autoaim_lost',vehicle.entity_id,lost)
            else:
                p = target.pose
                offset = matrix(p.yaw,p.pitch,p.roll) @ vehicle.autoaim_offset
                vehicle.gun.set_point(tuple(a+b for a,b in zip(p.position,offset)),False)
        pose = vehicle.pose
        # Actual hull turn of this fixed step, after collisions, bounded by
        # the vehicle's own traverse so impulses cannot spin the turret.
        omega = 0.
        if (previous_yaw is not None and dt > 0
                and not (self.world is not None and vehicle.entity_id in
                         getattr(self.world, 'dynamic_bodies', ()))):
            cap = (self.world.assets_for(vehicle.entity_id).rotation_speed if self.world
                   else math.radians(35))*vehicle.rotation_factor  # same bound as _drive
            omega = max(-cap,min(cap,wrap(pose.yaw-previous_yaw)/dt))
        vehicle.gun.tick(dt,pose.position,pose.yaw,pose.pitch,pose.roll,omega)
        if self.world:
            self.world.set_turret(vehicle.gun.yaw,vehicle.entity_id,vehicle.gun.pitch)

    def step(self, now, *, deadline=None):
        if getattr(self, '_input_only', False):
            return
        if now < self.last_tick or self.closed or self.retiring or self.building:
            return
        if deadline is not None and (isinstance(deadline, bool) or
                not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
            raise ValueError('Invalid simulation work deadline')
        if self.step_budget is not None:
            own_deadline = time.monotonic()+self.step_budget
            deadline = own_deadline if deadline is None else min(deadline, own_deadline)
        elapsed = now-self.last_tick
        self.last_tick = now
        if self.drive_workers is not None:
            # Startup can fail after loading completes but before any player
            # is ready. Reap incrementally even when no fixed step can run.
            self.drive_workers.poll()
            if self.drive_workers.closed:
                self.drive_workers = None
        if not self.started:
            self.simulation_time = now
            if any(p.attached for p in self.players.values()):
                self._start_if_ready(now)
            return
        # Bound work after a stalled event loop. Excess wall time is reported,
        # never integrated as one large physics jump. Battle clocks use sim time.
        elapsed_bounded = min(elapsed,self.MAX_CATCHUP)
        self.dropped_time += elapsed-elapsed_bounded
        self.accumulator += elapsed_bounded
        while self.accumulator + 1e-9 >= self.STEP:
            # A fixed step and its motion sample are atomic. Never start the
            # next one after the callback's wall budget, including when step()
            # is entered by a control/fire RPC rather than the server timer.
            if deadline is not None and time.monotonic() >= deadline:
                break
            self.accumulator = max(0.,self.accumulator-self.STEP)
            self.step_count += 1
            self.simulation_time = self.started+self.step_count*self.STEP
            self._fixed_step(self.simulation_time)
            self._record_motion_frame(self.simulation_time)
        if deadline is not None and self.accumulator > self.MAX_CATCHUP:
            # Deferred work must not grow without bound during an overload.
            # Report only wall time that will never be integrated; retaining
            # the rest preserves the next fixed-step sequence and sim clock.
            self.dropped_time += self.accumulator-self.MAX_CATCHUP
            self.accumulator = self.MAX_CATCHUP

    def _record_motion_frame(self, now):
        """Capture the first complete simulation state for each 30 Hz label."""
        tick = game_tick(now,self.epoch)
        if self.motion_frame is not None and self.motion_frame.tick == tick:
            return
        markers = None
        if os.environ.get('SOLBREAKER_MARKER_BATCH') == '1':
            ready = [v for v in self.vehicles.values() if v.alive
                     and (p := self.players.get(v.account_id)) is not None and p.attached]
            markers = _try_marker_batch(self, ready, _MARKER_BATCH_ARENA_METHODS)
        samples = {}
        for vehicle in self.vehicles.values():
            marker = None
            player = self.players.get(vehicle.account_id)
            if vehicle.alive and player is not None and player.attached:
                if markers is not None:
                    marker = markers[vehicle.entity_id]
                else:
                    _,point = self.aim_hit(vehicle)
                    _,direction = self.shot_ray(vehicle)
                    marker = (point,direction)
            samples[vehicle.entity_id] = MotionSample(vehicle.pose,vehicle.gun.packed(),
                vehicle.gun.yaw,marker,vehicle.engine_mode if vehicle.alive else (0,0))
        self.motion_frame = MotionFrame(tick,now,self.step_count,samples)

    def _fixed_step(self, now):
        dt = self.STEP
        self._advance_supremacy(now)
        for vehicle in self.vehicles.values():
            if not self.finished_at and vehicle.alive:
                # A state expiry can lie inside this projectile frame. Delay
                # its public property16 event until every earlier contact has
                # been resolved; segment-local snapshots below still select
                # the exact configuration for each flight interval.
                self._consumables(vehicle, now, sync_movable=False)
            if vehicle.reload_at and now>=vehicle.reload_at and vehicle.pump_times:
                # Intershot end; _consumables already started the refill at it.
                vehicle.reload_at = 0.
                if vehicle.clip and vehicle.alive and not self.finished_at:
                    self.emit('reload',vehicle.entity_id,delay=0.,total=vehicle.reload_total)
                self._emit_pump(vehicle,now)
            elif vehicle.reload_at and now>=vehicle.reload_at and not vehicle.burst_pending:
                vehicle.reload_at = 0.
                if vehicle.clip==0:
                    pending = vehicle.next_shell
                    if pending and pending!=vehicle.shell_id and vehicle.ammo_by_shell.get(pending,0)>0:
                        vehicle.ammo_by_shell[vehicle.shell_id] = vehicle.ammo
                        vehicle.selected_shell = pending
                        vehicle.ammo = vehicle.ammo_by_shell[pending]
                        self.emit('shell_selected',vehicle.entity_id,shell=pending)
                    vehicle.next_shell = 0
                    vehicle.clip = self._loaded_clip(vehicle)
                self.emit('ammo',vehicle.entity_id)
                self.emit('reload',vehicle.entity_id,delay=0.,total=vehicle.reload_total)
        if self.practice and not self.finished_at:
            self._advance_practice(now)
        if (not self.practice_clock_repeats and not self.endless and not self.finished_at
                and not self.solo_victory_team
                and now-self.started>=self.battle_seconds):
            self.finish(self.supremacy.timeout_winner() if self.supremacy is not None else 0,
                        'supremacy_timeout' if self.supremacy is not None else 'timeout', now)
        if not self.finished_at:
            self._advance_respawns(now)
            before = body_states(self.vehicles)
            drive_results = {}
            bot_targets = self.bot_director.step(now) if self.bot_director is not None else {}
            if (self.world is not None and
                    getattr(self.world, 'experimental_ground_impacts', False)):
                self.world.begin_ground_step()
            flying, piloted = self._sandbox_frame(now, dt) if self.sandbox_enabled else ((), ())
            driving = []
            for vehicle in self.vehicles.values():
                if vehicle.entity_id in flying:
                    continue
                attached = self._player_attached(vehicle)
                airborne = self.world is not None and not self.world.motion[vehicle.entity_id].grounded
                skidding = (self.world is not None and
                    getattr(self.world, 'experimental_ground_impacts', False) and
                    self.world.ground_motion[vehicle.entity_id].active)
                self._observe_landing(vehicle, now)
                if attached or airborne or skidding or vehicle.entity_id in self._landing_skids:
                    # An abandoned hull must finish falling, too.
                    driving.append(vehicle)
            drive_results = self._drive_vehicles(driving,dt,now)
            if (self.world is not None
                    and (getattr(self.world, 'dynamic_bodies', ())
                         or getattr(self.world, 'pending_dynamics', ()))):
                # Experimental airborne bodies are deliberately integrated
                # once only after every vehicle has queued this fixed step.
                # Moving this into `_drive` would advance the first airborne
                # tank again for each later participant in the same arena.
                was_airborne = {entity:not state.grounded
                                for entity,state in self.world.motion.items()}
                if piloted:
                    self.world.dynamic_commit(dt, piloted=piloted)
                else:
                    self.world.dynamic_commit(dt)
                for entity, recovery_start, recovery_end in self.world.dynamic_recoveries:
                    # A recovery placement is not a touchdown to skid from.
                    self._landing_airborne_since.pop(entity,None)
                    self._landing_skids.pop(entity,None)
                    recovered_pose = self.world.poses[entity]
                    self.emit('auto_recovered',entity,from_position=recovery_start,
                              position=recovery_end,yaw=recovered_pose.yaw,
                              pitch=recovered_pose.pitch,roll=recovered_pose.roll,at=now)
                self.world.dynamic_recoveries.clear()
                orientation_resets = getattr(self.world, 'dynamic_orientation_resets', ())
                for entity, reset_pose in orientation_resets:
                    # A coordinate-branch change preserves physical rotation;
                    # the client must not interpolate its numerical full turn.
                    self.emit('orientation_rebased',entity,position=reset_pose.position,
                              yaw=reset_pose.yaw,pitch=reset_pose.pitch,roll=reset_pose.roll,at=now)
                if orientation_resets:
                    orientation_resets.clear()
                for entity, vehicle in self.vehicles.items():
                    state = self.world.motion[entity]
                    if not was_airborne.get(entity, False) and state.grounded:
                        continue
                    vehicle.pose = self.world.poses[entity]
                    vx, _, vz = state.velocity
                    vehicle.speed = (vx*math.sin(vehicle.pose.yaw)
                                     + vz*math.cos(vehicle.pose.yaw))
                    self._observe_landing(vehicle, now)
                    if was_airborne.get(entity, False) and state.grounded:
                        self.emit('landed', entity, position=vehicle.pose.position,
                                  velocity=state.velocity, at=now)
            if self.world is not None:
                # Swept short hops can land without ever creating a Bullet
                # body, and must deliver their module contacts as well.
                impacts = getattr(self.world,'fall_impacts',[])
                self._fall_impacts(impacts,now)
                impacts.clear()
                track_impacts = getattr(self.world,'track_impacts',[])
                self._track_fall_impacts(track_impacts,now)
                track_impacts.clear()
            if (self.world is not None and
                    getattr(self.world, 'experimental_ground_impacts', False)):
                ground_states = {}
                for entity, motion in self.world.ground_motion.items():
                    ground_states[entity] = (
                        motion.velocity if motion.active and motion.velocity is not None
                        else (0.,0.),
                        motion.yaw_rate if motion.active else 0.)
                updates = self.world.resolve_ground_impacts(ground_states)
                for entity, (velocity,yaw_rate) in updates.items():
                    motion = self.world.ground_motion[entity]
                    motion.velocity = velocity
                    motion.yaw_rate = yaw_rate-self.world.ground_track_rates.get(entity,0.)
                    motion.active = (math.hypot(*velocity) > .03 or
                                     abs(motion.yaw_rate) > .01)
                    if not motion.active:
                        motion.velocity = (0.,0.)
                        motion.yaw_rate = 0.
                    yaw = self.world.poses[entity].yaw
                    vehicle = self.vehicles[entity]
                    # Pair impulses own the latest momentum, including an
                    # exact stop below GroundMotion's active threshold. A
                    # landing cache must never undo that loss next tick.
                    self._observe_landing(vehicle, now)
                    landing = self._landing_skids.get(entity)
                    if landing is not None:
                        landing.velocity = motion.velocity
                    vehicle.speed = velocity[0]*math.sin(yaw)+velocity[1]*math.cos(yaw)
                for impact in self.world.last_ground_impacts:
                    self._ground_impact(impact, now)
            if self.drive_trace:
                for vehicle in self.vehicles.values():
                    if vehicle.account_id is None or not self.players[vehicle.account_id].attached:
                        continue
                    motion = self.world.motion[vehicle.entity_id] if self.world else None
                    self.emit('drive_trace', vehicle.entity_id, at=now,
                              step_count=self.step_count, speed=vehicle.speed,
                              flags=vehicle.flags, position=vehicle.pose.position,
                              yaw=vehicle.pose.yaw, roll=vehicle.pose.roll,
                              pitch=vehicle.pose.pitch,
                              vehicle=f'{vehicle.definition.nation}/{vehicle.definition.name}',
                              grounded=(motion.grounded if motion is not None else True),
                              motion_velocity=(motion.velocity if motion is not None else None),
                              contact=(self.world.last_contact if self.world else None),
                              drive_reject_reason=(self.world.last_reject_reason
                                                   if self.world else None),
                              dynamic_body_active=(vehicle.entity_id in self.world.dynamic_bodies
                                                   if self.world else False),
                              dynamic_contacts=(self.world.dynamic_contact_trace.get(
                                  vehicle.entity_id, ()) if self.world else ()),
                              drive_moved=drive_results.get(vehicle.entity_id),
                              step_distance=(math.dist(before[vehicle.entity_id].pose.position,
                                                       vehicle.pose.position)
                                             if vehicle.entity_id in drive_results else None),
                              **self._drive_observability(vehicle, now))
            for player in self.players.values():
                if player.attached:
                    self._aim(self.vehicles[player.vehicle_id],dt,before[player.vehicle_id].pose.yaw)
            for eid, target in bot_targets.items():
                bot = self.vehicles[eid]
                # A patrol look has no visible fire target, but still needs
                # the same physical gun slew and published joint angles.
                if bot.alive and (target is not None or self.training_bots[eid].travel_look_active):
                    self._aim(bot,dt,before[eid].pose.yaw)
            if self._recon_point_probe is not None:
                self._recon_point_probe_update(now)
            if self._recon_client_aims:
                self._recon_client_aim_update()
            after = body_states(self.vehicles)
            if self.recon is not None:
                for vehicle in self.vehicles.values():
                    self._update_recon_camouflage(vehicle, now)
            for entity,vehicle in self.vehicles.items():
                vehicle.distance += math.dist(before[entity].pose.position,after[entity].pose.position)
            contacts = []
            frame_start = now-dt
            armor_segments = self._armor_state_segments(frame_start, now)
            armor_frames = {}
            armor_sweeps = {}
            share_sweeps = len(self.projectiles) > 1
            for shot,projectile in list(self.projectiles.items()):
                impact = None
                guided = projectile.shell.missile is not None
                target = self._missile_target(projectile) if guided else None
                for left,right in armor_segments:
                    segment_start = frame_start+dt*left
                    segment_end = frame_start+dt*right
                    # The live vehicle may already contain the endpoint state:
                    # snapshots below are reconstructed at this interval's
                    # midpoint so an expiry never reaches backward in time.
                    midpoint = (segment_start+segment_end)/2
                    # No impact side effects are committed until every shell
                    # has advanced. These immutable observations belong to the
                    # segment, including an exact armor-controller expiry.
                    key = (left,right)
                    if key not in armor_frames:
                        armor_frames[key] = (
                            self._armor_frame_states(before,after,left,midpoint),
                            self._armor_frame_states(before,after,right,midpoint))
                    segment_before,segment_after = armor_frames[key]
                    if share_sweeps and key not in armor_sweeps:
                        armor_sweeps[key] = SweepCache(segment_before,segment_after)
                    impact = advance(projectile,self.world,segment_before,segment_after,
                                     segment_end-segment_start,self.rng,sweep_cache=armor_sweeps.get(key),
                                     missile_target=target)
                    if impact:
                        break
                contacts.extend((event.at,event.shot,0,event) for event in projectile.drain_track_contacts())
                if self.recon is not None:
                    contacts.extend((event.at,event.shot,0,event) for event in projectile.drain_recon_contacts())
                if impact:
                    contacts.append((impact.at,impact.shot,1,impact))
                    del self.projectiles[shot]
                elif guided and self.step_count % self.MISSILE_UPDATE_STEPS == 0:
                    self.emit('missile_move',projectile.shooter,shot=shot,origin=projectile.position,
                              velocity=projectile.velocity,effect_index=projectile.shell.effect_index,at=now)
            # All shells from this interval resolve before selecting a winner.
            # A shooter killed in this step still owns its already-fired shell.
            for _,_,_,impact in sorted(contacts,key=lambda event:event[:3]):
                if isinstance(impact,ReconContact):
                    self._recon_impact(impact.shot, self.vehicles.get(impact.shooter),
                                       self.vehicles.get(impact.target), impact.at)
                    continue
                if isinstance(impact,TrackContact):
                    self._track_contact(impact,now)
                    continue
                target = self.vehicles.get(impact.target)
                if target is not None and target.alive:
                    # LOCAL deterministic tie: due fire ticks precede an
                    # impact at the same timestamp, including its module hit.
                    self._advance_fire(target, max(self.module_clock, min(now, impact.at)))
                states = None
                if impact.shell and impact.shell.kind=='HIGH_EXPLOSIVE':
                    fraction = max(0.,min(1.,(impact.at-(now-dt))/dt))
                    states = self._armor_frame_states(before, after, fraction, impact.at)
                detonate_ammo_rack = False
                if self._internal_candidate(impact):
                    fraction = max(0.,min(1.,(impact.at-(now-dt))/dt))
                    detonate_ammo_rack = self._internal_contact(
                        impact,self._armor_frame_state(self.vehicles[impact.target],before[impact.target],
                                                       after[impact.target],fraction,impact.at),now)
                self._impact(impact,states,detonate_ammo_rack=detonate_ammo_rack,
                             fire_at=max(self.module_clock, min(now, impact.at)))
                if target is not None:
                    self._start_pending_fire(target)
            if self.recon is not None:
                # Contacts for a shell may span frames; retain only shots still
                # in flight and discard expired markers on simulation time.
                for shot in tuple(self._recon_contacts):
                    if shot not in self.projectiles:
                        del self._recon_contacts[shot]
                self.recon.expire(now)
            for vehicle in self.vehicles.values():
                if vehicle.alive:
                    self._advance_fire(vehicle, now)
                if vehicle.alive:
                    self._advance_tracks(vehicle,now)
                self._sync_movable_armor(vehicle, now)
            self.module_clock = now
            teams = {v.team for v in self.vehicles.values() if v.alive and
                     (v.account_id is None or self.players[v.account_id].attached)}
            # An accepted burst is one trigger, not independent future input.
            # Let its already committed partner leave the barrel before the
            # elimination result clears reload state.  Direct leave/timeout
            # still finishes immediately and explicitly cancels the burst.
            pending_burst = any(v.alive and v.burst_pending and
                                (v.account_id is None or self.players[v.account_id].attached)
                                for v in self.vehicles.values())
            if (not self.practice and self.solo and teams == {1} and self._combat_can_finish()
                    and self.base_capture is None and self.supremacy is None):
                self.solo_victory_team = 1
            if (not self.practice and not self.solo_victory_team and len(teams)<=1
                    and not pending_burst and self._combat_can_finish()):
                winner = next(iter(teams),0)
                # Abandoned live hulls remain collidable, but cannot keep a
                # defeated team in the match. Do not invent a kill for leaving.
                departed = next((p for p in self.players.values() if not p.attached
                    and p.team!=winner and self.vehicles[p.vehicle_id].alive),None)
                self.finish(winner,departed.departure_reason if departed else 'elimination',now)
            # Fire after projectile integration. A newly spawned shell must not
            # be advanced over the already completed fixed-step interval.
            for eid, target in bot_targets.items():
                self._training_bot_fire(self.vehicles[eid],target,now,self.training_bots[eid])
            if self.practice_gunners:
                target = self.vehicle_for(next(iter(self.players)))
                if target.alive and self._player_attached(target):
                    for entity, gunner in self.practice_gunners.items():
                        bot = self.vehicles[entity]
                        if bot.alive:
                            bot.gun.set_point(gunner.aim_point(target.entity_id, target.pose.position, now), False)
                            self._aim(bot, dt, before[entity].pose.yaw)
                            self._training_bot_fire(bot, target, now, gunner)
        for vehicle in self.vehicles.values():
            if not vehicle.burst_pending:
                continue
            attached = vehicle.account_id is None or self.players[vehicle.account_id].attached
            if self.finished_at or not vehicle.alive or not attached:
                self._cancel_burst(vehicle)
            elif now>=vehicle.reload_at:
                self._fire_burst_next(vehicle,now)
        self._advance_base_capture(now)
        if self.world:
            for obj,yaw,angle in self.world.destruction_events:
                if obj.client_registered:
                    self.destructions[obj.key] = (yaw,angle)
                    self.emit('destruction',key=obj.key,yaw=yaw,angle=angle)
            self.world.destruction_events.clear()


class ArenaManager:
    def __init__(self, terrain, collision_scene, descriptor, *, retire=None, can_retire=None,
                 map_spec=None, recon_enabled=False, recon_game=None, recon_point_tolerance=None,
                 step_budget=None, sandbox_enabled=False):
        self.step_budget = _step_budget(step_budget)
        self.sandbox_enabled = sandbox_enabled
        if type(recon_enabled) is not bool:
            raise ValueError('Invalid recon mode')
        if recon_point_tolerance is not None and (not recon_enabled or
                isinstance(recon_point_tolerance, bool) or
                not isinstance(recon_point_tolerance, (int, float)) or
                not math.isfinite(recon_point_tolerance) or not 0 <= recon_point_tolerance <= 5):
            raise ValueError('Invalid experimental aim-point tolerance')
        self.terrain,self.collision_scene,self.descriptor = terrain,collision_scene,descriptor
        self.map_spec = map_spec
        self.recon_enabled, self.recon_game = recon_enabled, recon_game
        self.recon_point_tolerance = recon_point_tolerance
        self.arenas = {}
        self.next_space = 1
        self.retire = retire
        self.can_retire = can_retire

    def create(self, accounts, *, solo=True, incremental=False, collision_scene=None, descriptor=None,
               vehicle_scenes=None, terrain=None, map_spec=None, bot_enabled=False, training=None,
               build_world=True, result_id=None, bot_roster=None, bot_game=None, bot_scene_settings=(),
               prebattle_seconds=0., room_mode='encounter', room_settings=None):
        space = self.next_space
        self.next_space += 1
        terrain = self.terrain if terrain is None else terrain
        map_spec = self.map_spec if map_spec is None else map_spec
        arena = Arena(accounts,self.descriptor if descriptor is None else descriptor,
                      terrain,self.collision_scene if collision_scene is None else collision_scene,
                      space_id=space,entity_base=(space-1)*1000,solo=solo,incremental=incremental,
                      vehicle_scenes=vehicle_scenes,map_spec=map_spec,bot_enabled=bot_enabled,training=training,
                      recon_enabled=self.recon_enabled, recon_game=self.recon_game,
                      step_budget=self.step_budget, build_world=build_world, result_id=result_id,
                      bot_roster=bot_roster, bot_game=bot_game, bot_scene_settings=bot_scene_settings,
                      prebattle_seconds=prebattle_seconds, sandbox_enabled=self.sandbox_enabled,
                      room_mode=room_mode, room_settings=room_settings)
        if self.recon_point_tolerance is not None:
            arena.enable_recon_point_probe(tolerance=self.recon_point_tolerance)
        self.arenas[arena.result_id] = arena
        return arena

    def get(self, arena_id):
        return self.arenas.get(arena_id)

    def remove_if_empty(self, arena):
        if self.can_retire is not None and not self.can_retire(arena):
            return
        if not any(p.attached for p in arena.players.values()):
            if self.retire is not None:
                self.retire(arena)
            else:
                arena.close()
            self.arenas.pop(arena.result_id,None)

    def close(self):
        for arena in self.arenas.values():
            arena.close()
        self.arenas.clear()


_MARKER_BATCH_ARENA_METHODS = {name: getattr(Arena, name) for name in (
    "aim_hit", "shot_ray", "shell_for")}
