"""Authoritative arena state. No Mercury IDs, packet bytes or sockets here."""
import dataclasses
from collections import Counter, deque
from copy import copy
import math
import os
import random
import time
import uuid
from xml.etree.ElementTree import ParseError

from . import big_boss, diagnostics, mad_games, uprising
from .ballistics import (Projectile, ReconContact, SweepCache, TrackContact, advance, body_states,
                         interpolate, missile_guidance_target)
from .arena_commands import decode_graviton_aim, decode_uprising_target
from .armor import MovableArmorStateSnapshot
from .ammunition import (advance_pump, apply_burst_shell_switch, apply_queued_shell,
                         finish_magazine_reload, magazine_reload_allowed, spend_round,
                         start_pump_refill, switch_shell)
from .collision import CollisionWorld, GroundMotion, Motion, Pose
from .entities import BattleEvent, BattleResult, MotionFrame, MotionSample, Player, Vehicle
from .motion import game_tick
from .aiming import GunAim, disperse_shot, dispersion_tangent, wrap
from .roster import TEAMS, validate_teams, training_spawn, training_yaw
from .orientation import matrix
from .consumables import (ACTIVE, COOLDOWN, HEAL, READY, RESTORE, SHIELDED_SHELL_KINDS,
                          ConsumableSpec, ConsumableState)

from .loadout_effects import ANTI_HE_PROVISION
from .internal_geometry import GeometryConfiguration, PreparedInternalGeometry
from .modules import (AmmoRackState, CrewSpec, CrewState, EngineState, FuelTankState, GunState,
                      TrackState, crew_bindings_for_meshes)
from .fire import FirePolicy, FireState
from .track_fall_damage import evaluate_track_fall
from .landing_skid import MAX_DURATION as LANDING_MAX_DURATION, grip_fraction, touchdown_skid
from .track_drive import (TrackDriveMotion, passive_track_step,
                          COAST_DRAG_RATE, BOTH_DRAG_RATE)
from .mobility import (advance_speed, braking_acceleration, powered_acceleration,
                       rolling_acceleration, skid_velocity, slope_grip_acceleration,
                       steering_skid, terrain_at)
from .recon import ReconState
from .bot_ai import BotDirector, TrainingBot
from .bot_roster import vehicle_class
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
_INTERNAL_KINDS = ('ARMOR_PIERCING','ARMOR_PIERCING_CR','HOLLOW_CHARGE')


def _step_budget(value):
    if value is not None and (isinstance(value, bool) or
            not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0):
        raise ValueError('Invalid simulation work budget')
    return float(value) if value is not None else None


class TrainingSpawnUnavailable(ValueError):
    """Every team spawn is occupied and the original hull is still intact."""


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
                 build_world=True, bot_roster=None, bot_game=None, bot_scene_settings=(), custom_bot_slots=False,
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
        self._resolved_time_branches = set()
        self.time_stop = None
        self.time_stop_enabled = (sandbox_enabled and os.environ.get('SOLBREAKER_TIME_STOP') == '1')
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
        self.big_boss_mode = rules == big_boss.MODE
        self.mad_games_mode = rules == mad_games.MODE
        if result_id is not None and (type(result_id) is not str or len(result_id) != 32
                                      or any(c not in '0123456789abcdef' for c in result_id)):
            raise ValueError('Invalid arena identity')
        self.result_id = uuid.uuid4().hex if result_id is None else result_id
        self.uprising = (uprising.BattleState(random.Random(self.result_id + ':uprising'))
                         if rules == uprising.MODE else None)
        self.mad_irradiated = {}
        self._mad_games_game = (bot_game or recon_game) if self.mad_games_mode else None
        self._uprising_game = (bot_game or recon_game) if self.uprising is not None else None
        self.time_stop_enabled &= not (self.gravity_mode or self.mad_games_mode or self.uprising is not None)
        self.big_boss = None
        self.boss_role_picker = self.big_boss_mode and not solo and room_settings is not None
        self.boss_pending_roles = {}
        self.boss_first_picks = set()
        self.boss_picker_refresh_at = 0.
        self._boss_game = (bot_game or recon_game) if self.big_boss_mode else None
        self.unlimited_lives |= self.big_boss_mode
        if self.uprising is not None:
            self.unlimited_lives = False
        self._gravity_game = bot_game if self.gravity_mode else None
        self.practice = sandbox_enabled and solo and not bot_enabled
        self.practice_clock_repeats = self.practice and settings.minutes is None
        self.practice_slots = set()
        self.practice_numbers = {}
        self.practice_client_ids = {}
        self.practice_next_client_id = sandbox.PRACTICE_ENTITY_BASE
        self.ram_turrets = {}
        self.ram_detached_hulls = set()
        self.next_ram_turret = mad_games.RAM_TURRET_ENTITY_BASE
        self.practice_reuse_slot = None
        self.practice_spawned = set()
        self.practice_active = set()
        self.practice_selection = 0
        self.practice_types = {}
        self.practice_game = bot_game if self.practice else None
        self.practice_target = None
        self.practice_next_pick = 0.
        self.practice_default_scenes = {}
        self.practice_scenes = {}
        self.practice_return_fire = False
        self.practice_gunners = {}
        self.practice_next_spawn = 0.
        self.respawn_at = {}
        self.hangar_players = set()
        self.training_swappers = set()
        self.practice_timer_at = 0.
        self.unique_id = int(self.result_id[:15],16) or 1
        self.created_at = time.time()
        self.space_id, self.entity_base, self.solo = space_id,entity_base,solo
        self.terrain = terrain
        self.map_spec = map_spec
        self.map_id = map_spec.map_id if map_spec is not None else 6
        point = getattr(map_spec, 'capture_point', None)
        strategic_points = getattr(map_spec, 'strategic_points', ())
        capture_enabled = rules in ('encounter', uprising.MODE) or (self.mad_games_mode and not strategic_points)
        self.base_capture = BaseCapture(point) if point is not None and capture_enabled else None
        self.supremacy = (Supremacy(strategic_points)
                         if rules == 'supremacy' or (self.mad_games_mode and strategic_points) else None)
        self.client_map_id = self.map_id | (0x10000 if self.supremacy is not None or self.big_boss_mode else 0)
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
        self.pending_inputs = deque()
        self.pending_input_counts = Counter()
        self.step_count = 0
        self.motion_frame = None
        self.dropped_time = 0.
        self.started = self.finished_at = 0.
        self.winner = 0
        # After destroying stationary targets, allow free driving until exit.
        self.solo_victory_team = 0
        self.finish_reason = ''
        self.players = {}
        self.vehicles = {}
        self.training_bot = None
        self.training_bots = {}
        self.disabled_training_bots = set()
        self.bot_roster = ()
        self.custom_bot_slots = custom_bot_slots
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
        self.dispersion_rng = random.Random(self.result_id + ':dispersion')
        # Separate stream: gun chance draws must not shift penetration rolls.
        self.module_rng = random.Random(self.result_id+':modules')
        # Both special modes supply team spotting and the native detection lamp.
        # Recon's personal locks would mask those markers and replace the lamp.
        recon_enabled = recon_enabled and not (self.mad_games_mode or self.uprising is not None)
        self.team_spotting = not recon_enabled
        self._spotting_checks = {}
        # The dedicated stream must never alter ballistics or damage.
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
                    player.nickname,team,actual_descriptor,pose,
                    camouflage=account.get('camouflage', 0))
            own = self.vehicles[player.vehicle_id]
            own.definition = assets.definition if assets else None
            if self.sandbox_enabled:
                own.sandbox_preset = tuple(account.get('sandbox_preset', ()))
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
                    if custom_bot_slots:
                        if not bot_enabled or bot_roster is None:
                            raise ValueError('Custom bot slots require a bot battle')
                        counts = {team: sum(row.team == team for row in bot_roster) for team in (1, 2)}
                        if counts[1] >= capacity or counts[2] > capacity:
                            raise ValueError('Custom bot slots exceed map capacity')
                        slots = [(2, i) for i in range(counts[2])]
                        slots += [(1, i+1) for i in range(counts[1])]
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
                    if self.sandbox_enabled and self.bot_roster:
                        target.sandbox_preset = self.bot_roster[index].sandbox_preset
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
        if self.mad_games_mode:
            for vehicle in self.vehicles.values():
                self._install_mad_games_ability(vehicle)
        if self.uprising is not None:
            for vehicle in self.vehicles.values():
                self._install_uprising_ability(vehicle)
        if self.team_spotting and collision_scene is not None:
            for vehicle in self.vehicles.values():
                vehicle.mad_unspotted = True
        if self.big_boss_mode:
            self.big_boss = big_boss.BigBoss(self.vehicles, self.result_id, endless=self.endless)
            for vehicle in self.vehicles.values():
                self._set_boss_role_ability(vehicle)
                if self._is_boss(vehicle):
                    vehicle.max_health *= big_boss.HEALTH_MULTIPLIER
                    vehicle.health = vehicle.max_health
                    vehicle.reload_speed_factor = big_boss.RELOAD_RATE
                    vehicle.reload_total /= big_boss.RELOAD_RATE
                    vehicle.engine_factor = big_boss.ENGINE_POWER
        if not build_world or collision_scene is None:
            for vehicle in self.vehicles.values():
                self._apply_initial_preset(vehicle)
        # Owner-backed parent mirrors need the real vehicle definitions for
        # ammo validation; only the authoritative child builds a Bullet world.
        try:
            if collision_scene is not None and build_world:
                self.world = CollisionWorld(collision_scene,terrain,
                        vehicles={e:v.pose for e,v in self.vehicles.items()},incremental=incremental,
                        vehicle_assets=entity_assets, visibility=bool(self.training_bots),
                        parallel_native_build=parallel_native_build,
                        gravity=2.5 if self.gravity_mode else CollisionWorld.GRAVITY,
                        # Rapier has long collision stalls with simultaneous
                        # fast ragdolls, reproduced on Holland with seven targets.
                        dynamic_backend='bullet' if self.big_boss_mode else None)
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
                self._apply_initial_preset(vehicle)
                if entity in self.practice_slots:
                    self.practice_default_scenes[entity] = scene
                    self._park_practice_target(vehicle)
            self._vehicles_configured = True
            if self.time_stop_enabled:
                from .arena_time_stop import BattleTimeStop
                try:
                    self.time_stop = BattleTimeStop(self)
                except (OSError, RuntimeError, ValueError) as error:
                    self.time_stop_enabled = False
                    self.emit('time_stop_worker_error', reason=str(error))
        if self.training_bots:
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

    def _is_boss(self, vehicle):
        return self.big_boss is not None and vehicle.entity_id in self.big_boss.bosses

    def _new_consumable(self, vehicle, spec):
        limit = sandbox.reload_limit(vehicle)
        return ConsumableState(spec, cooldown_limit=limit)

    def _set_boss_role_ability(self, vehicle):
        entity = vehicle.entity_id
        role, level = self.big_boss.roles.get(entity), self.big_boss.levels.get(entity, 0)
        poisoned_barrel = any(slot and slot.spec in big_boss.ROLE_ACTIVE[big_boss.CATALYST]
                              and vehicle.consumable_status.get(slot.spec.ident) == ACTIVE
                              for slot in vehicle.consumables)
        ability_specs = big_boss.ROLE_SPECS | {big_boss.BLAST_WAVE, big_boss.MEGASHOT}
        previous = tuple(slot.spec.compact_id for slot in vehicle.consumables
                         if slot and slot.spec in ability_specs)
        slots = tuple(slot for slot in vehicle.consumables if slot is None or slot.spec not in ability_specs)
        abilities = ((big_boss.BLAST_WAVE, big_boss.MEGASHOT) if role is None else
                     (big_boss.ROLE_ACTIVE[role][level],))
        if role == big_boss.REGENERATOR:
            abilities += (big_boss.SHIELD_SPECS[level],)
        vehicle.consumables = slots + (None,) * max(0, 3 - len(slots)) + tuple(
            self._new_consumable(vehicle, spec) for spec in abilities)
        vehicle.aiming_time_factor = big_boss.CATALYST_AIM[level] if role == big_boss.CATALYST else 1.
        for spec in ability_specs:
            vehicle.consumable_status.pop(spec.ident, None)
        if poisoned_barrel:
            self._publish_state_modifiers(vehicle)
        return previous

    def _emit_boss_role(self, entity, previous):
        abilities = self.big_boss.abilities(entity)
        self.emit('boss_role', entity, role=self.big_boss.roles.get(entity), level=self.big_boss.levels.get(entity, 0),
                  progress=self.big_boss.role_progress(entity, self.vehicles[entity].max_health),
                  abilities=tuple((compact_id, 0) for compact_id in abilities),
                  previous=tuple(compact_id for compact_id in previous if compact_id not in abilities))

    def _pin_boss_role(self, vehicle, role, level):
        boss = self.big_boss
        entity = vehicle.entity_id
        was_boss = entity in boss.bosses
        if role == big_boss.BOSS and not was_boss:
            boss.bosses.add(entity)
            boss.lives.setdefault(entity, big_boss.LIVES)
            boss.roles.pop(entity, None)
        elif role != big_boss.BOSS:
            boss.bosses.discard(entity)
            boss.roles[entity] = role
            if level is None:
                boss.levels.setdefault(entity, 0)
            else:
                boss.levels[entity] = level
                boss.experience[entity] = float(level)
        boss.selected[entity] = role
        previous = self._set_boss_role_ability(vehicle)
        self.emit('boss_lives', entity, lives=boss.lives[entity] if role == big_boss.BOSS else 1)
        return previous

    def _pick_boss_role(self, player, number):
        if not self.boss_role_picker or self.finished_at:
            return
        vehicle = self.vehicles[player.vehicle_id]
        try:
            selection = sandbox.decode_boss_pick(number)
        except ValueError:
            return
        if self.started:
            if vehicle.entity_id in self.respawn_at:
                if vehicle.entity_id in self.boss_pending_roles:
                    return
                self.boss_pending_roles[vehicle.entity_id] = selection
                self.respawn_at[vehicle.entity_id] = min(self.respawn_at[vehicle.entity_id],
                                                       self.simulation_time + .25)
                self.emit('boss_picker', vehicle.entity_id, state=1)
            return
        if not self.prebattle_end or vehicle.entity_id in self.boss_first_picks:
            return
        self.boss_first_picks.add(vehicle.entity_id)
        was_boss = self._is_boss(vehicle)
        previous = self._pin_boss_role(vehicle, *selection)
        is_boss = self._is_boss(vehicle)
        if is_boss != was_boss:
            multiplier = big_boss.HEALTH_MULTIPLIER
            vehicle.max_health = vehicle.max_health * multiplier if is_boss else vehicle.max_health // multiplier
            vehicle.health = vehicle.max_health
            self.emit('boss_health', vehicle.entity_id, health=vehicle.health, max_health=vehicle.max_health)
            self._publish_state_modifiers(vehicle)
        self._emit_boss_role(vehicle.entity_id, previous)
        self._consumables(vehicle, self.simulation_time)
        self.emit('ready', vehicle.entity_id, yaw_rate=vehicle.gun.yaw_rate,
                  pitch_rate=vehicle.gun.pitch_rate, respawn=True)
        self.emit('boss_picker', vehicle.entity_id, state=1)

    def _configure_battle_vehicle(self, vehicle, scene):
        boss = self._is_boss(vehicle)
        multiplier = big_boss.HEALTH_MULTIPLIER if boss else 1
        vehicle.health = vehicle.max_health = scene.armor.health * multiplier
        if boss:
            vehicle.reload_speed_factor = big_boss.RELOAD_RATE
            vehicle.engine_factor = big_boss.ENGINE_POWER
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
        if vehicle.alive and self.mad_games_mode:
            if vehicle.entity_id in self.mad_irradiated:
                modifiers.append(mad_games.IRRADIATED_MODIFIER)
            if vehicle.mad_coordinator_allies:
                modifiers.append(mad_games.COORDINATOR_MODIFIER)
            if vehicle.consumable_status.get(mad_games.REFLECTOR.ident) == ACTIVE:
                modifiers.append(mad_games.REFLECTOR_MODIFIER)
            if vehicle.mad_revenge:
                modifiers.append(mad_games.TOXIC_STARE_MODIFIER)
        if vehicle.alive and self.big_boss is not None:
            if vehicle.entity_id in self.big_boss.bosses:
                modifiers.append(big_boss.BOSS_AURA_MODIFIER)
            wave = self.big_boss.heal_waves.get(vehicle.entity_id)
            if wave is not None:
                modifiers.append(big_boss.HEAL_AURA_MODIFIERS[wave[2]])
            if self.big_boss.roles.get(vehicle.entity_id) == big_boss.CATALYST:
                level = self.big_boss.levels[vehicle.entity_id]
                spec = big_boss.ROLE_ACTIVE[big_boss.CATALYST][level]
                if vehicle.consumable_status.get(spec.ident) == ACTIVE:
                    modifiers.append(big_boss.POISONED_SHELL_MODIFIERS[level])
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

    def _publish_state_modifiers(self, vehicle):
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
            self._publish_state_modifiers(vehicle)
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
            self._publish_state_modifiers(vehicle)

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
            self._publish_state_modifiers(vehicle)

    def _gun_contact(self, contact, target, gun, at, *, probability_checked=False):
        """Sandbox: chance draw, then nominal device damage. No natural recovery.

        chance 0 never damages and 1 always damages, without drawing. Hull HP,
        hits and kills are untouched.
        """
        chance = gun.spec.projectile_chance
        if gun.destroyed or not probability_checked and (chance <= 0 or
                chance < 1 and self.module_rng.random() >= chance):
            return False
        before = gun.critical,gun.destroyed
        applied = gun.damage(contact.shell.damage_devices)
        self.emit('module_hit',contact.shooter,contact.target,shot=contact.shot,
                  part=contact.hit.part,damage=applied,health=gun.health,at=at)
        if before != (gun.critical,gun.destroyed):
            self._module_changed(target,_GUN_EXTRA,1 if gun.destroyed else 0,at)
        return applied > 0

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
                and target.team != shooter.team and not target.sandbox_god_mode
                and (self.uprising is None or (event.shot, target.entity_id) not in self.uprising.protected_shots
                     and not uprising.active(target, uprising.PLAY_DEAD, event.at))
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
        self._publish_state_modifiers(vehicle)
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
        self._publish_state_modifiers(vehicle)

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

    def _track_contact(self, contact, now, *, probability_checked=False):
        target = self.vehicles.get(contact.target)
        shooter = self.vehicles.get(contact.shooter)
        if self.recon is not None and not probability_checked:
            self._recon_impact(contact.shot, shooter, target, contact.at)
        if (not contact.penetrated or target is None or shooter is None or not target.alive
                or target.team == shooter.team or target.sandbox_god_mode):
            return
        if self.uprising is not None and self._uprising_blocks_shell(target, contact.shot, contact.at):
            return
        gun = self.guns.get(target.entity_id)
        if (contact.hit.part not in target.tracks and gun is not None
                and contact.hit.part == target.definition.gun.geometry):
            if contact.shell.damage_devices > 0:
                return self._gun_contact(contact, target, gun, max(self.module_clock,min(now,contact.at)),
                                         probability_checked=probability_checked)
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
        if self.mad_games_mode:
            damage = mad_games.track_damage(target, damage, at)
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
        return applied > 0

    def _repair_tracks(self, vehicle, now):
        """Repair kit: full HP for supported local modules."""
        changed = [part for part,state in vehicle.tracks.items() if state.repair(now)]
        for part in changed:
            self._module_changed(vehicle,part,19,now,sync_track_modifiers=False)
        if changed:
            self._publish_state_modifiers(vehicle)
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
            return sandbox.reload_limit(vehicle)
        if kind == 'interval':
            return nominal / vehicle.reload_speed_factor * vehicle.interval_factor
        return nominal / self._full_reload_rate(vehicle)

    def _magazine_duration(self, vehicle, kind):
        weapon = vehicle.definition.weapon if vehicle.definition else None
        if kind == 'interval':
            nominal = weapon.interval if weapon else .1
        else:
            nominal = weapon.reload if weapon else 6.
        return self._reload_duration(vehicle, kind, nominal)

    @staticmethod
    def _burst_duration(vehicle):
        return vehicle.definition.weapon.burst_interval / vehicle.reload_speed_factor * vehicle.interval_factor

    def _set_fast_reload(self, vehicle, enabled, *, override=None):
        previous = sandbox.reload_limit(vehicle)
        if override is not None:
            vehicle.sandbox_reload_override = override
        vehicle.sandbox_fast_reload = enabled
        limit = sandbox.reload_limit(vehicle)
        if previous == limit:
            return
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
        for state in vehicle.consumables:
            if state is not None:
                state.set_cooldown_limit(now, limit)
        if self.uprising is not None:
            self.uprising.set_shot_upgrade_cooldown_limit(vehicle.entity_id, now, limit)
            for compact_id, remaining, total in self.uprising.shot_upgrade_statuses(vehicle.entity_id, now):
                if remaining:
                    self.emit('uprising_shot_upgrade_cooldown', vehicle.entity_id, compact_id=compact_id,
                              remaining=remaining, total=total)
        self._consumables(vehicle, now, refresh_cooldowns=True)

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
        vehicle.duplet_pending = False
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
            vehicle.duplet_pending = False
            vehicle.reload_at = 0.
            vehicle.reload_kind = 'reload'

    # Pump autoreloader (public game autoreloading-system article, XML times).
    # A shot leaving shells waits its intershot, then refills one; an empty
    # clip refills at once and its first shell can fire immediately. A shot
    # discards refill progress. Each refill takes times[clip] and only moves a
    # spare shell (ammo counts the magazine) into the clip: never free ammo.
    def _pump_segment(self, vehicle, start):
        start_pump_refill(vehicle, start, lambda nominal: self._reload_duration(vehicle, 'reload', nominal))

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
        for total in advance_pump(vehicle, until, lambda at: self._pump_segment(vehicle, at)):
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
        ids = self.practice_client_ids
        self.events.append(BattleEvent(kind, actor, target, data, ids.get(actor), ids.get(target)))
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
        if self.unlimited_lives or (self.endless and self.uprising is None):
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
        self.pending_inputs.clear()
        self.pending_input_counts.clear()
        if self.time_stop is not None:
            if not self.time_stop.abort():
                return False
            self.time_stop = None
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
            for scene in self.practice_scenes.values():
                scene.close()
            self.practice_scenes.clear()
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
        if target_id is not None:
            target_id = sandbox.vehicle_arena_id(self, target_id)
            if target_id < 0:
                target_id = None
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
        if target is None or target.team == observer.team or not target.alive or mad_games.concealed(target, observer.team):
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

    def _refresh_boss_picker(self, now):
        # The native HUD and local avatar can initialize after the first packet.
        if not self.boss_role_picker or self.finished_at or now < self.boss_picker_refresh_at:
            return
        self.boss_picker_refresh_at = now + 1.
        for player in self.players.values():
            entity = player.vehicle_id
            picking = (entity in self.respawn_at and entity not in self.boss_pending_roles
                       if self.started else entity not in self.boss_first_picks)
            if player.attached and player.loaded and picking:
                self.emit('boss_picker', entity, state=2)

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
                if self.boss_role_picker:
                    for player in self.players.values():
                        if player.attached:
                            role = self.big_boss.roles.get(player.vehicle_id, big_boss.BOSS)
                            self.big_boss.selected.setdefault(player.vehicle_id, role)
                    self.boss_picker_refresh_at = now
            if now < self.prebattle_end:
                self._refresh_boss_picker(now)
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
            if self.mad_games_mode:
                for vehicle in self.vehicles.values():
                    mad_games.charge_shield(vehicle, now)
                self._update_mad_games(now)
            elif self.team_spotting and self.world is not None:
                self._update_team_spotting(now)
            self._record_motion_frame(self.started)
            self.emit('period',period=3,seconds=0 if self.endless else self.battle_seconds,winner=0)
            if self.boss_role_picker:
                for player in self.players.values():
                    self.emit('boss_picker', player.vehicle_id, state=1)
            if self.big_boss is not None:
                if self.big_boss.tickets:
                    self.emit('boss_score', tickets=tuple(self.big_boss.tickets))
                for entity_id, role in self.big_boss.roles.items():
                    if role == big_boss.REGENERATOR:
                        shield = self.vehicles[entity_id].consumables[-1]
                        shield.start_cooldown(now, shield.spec.cooldown)
                for entity_id in sorted(self.big_boss.bosses):
                    boss = self.vehicles[entity_id]
                    charge = next(s for s in boss.consumables if s and s.spec is big_boss.MEGASHOT)
                    charge.start_cooldown(now, big_boss.MEGASHOT_INITIAL_COOLDOWN)
                    self._publish_state_modifiers(boss)
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
        timeline = getattr(self, 'time_stop', None)
        if control.flags == sandbox.TIME_STOP:
            if timeline and control.token == sandbox.token(self) and player is not None:
                timeline.toggle(account_id)
            return
        if timeline and account_id == timeline.account:
            if control.token == sandbox.token(self):
                timeline.missile_control(control)
                if control.flags & sandbox.GRAVITY_JUMP:
                    timeline.request_ability()
            return
        if control.flags in (sandbox.BOSS_PICK_ROLE, sandbox.BOSS_RESPAWN):
            if (self.boss_role_picker and player and player.attached and player.loaded
                    and control.token == sandbox.token(self) and not self.finished_at):
                if control.flags == sandbox.BOSS_PICK_ROLE:
                    self._pick_boss_role(player, control.shot)
                elif self.started and self.world is not None:
                    vehicle = self.vehicles[player.vehicle_id]
                    if vehicle.alive:
                        self._damage_health(vehicle, vehicle.health, None, self.simulation_time,
                                            shell_hit=False, manual_respawn=True)
            return
        if (not (self.sandbox_enabled or self.gravity_mode or self.big_boss_mode or self.mad_games_mode)
                or control.token != sandbox.token(self)
                or not player or not player.attached or not player.loaded
                or not self.started or self.finished_at or not self.vehicle_for(account_id).alive):
            return
        if control.flags == sandbox.PRACTICE_MENU:
            if self.sandbox_enabled and self.practice and self.world is not None:
                self._practice_menu_action(self.vehicle_for(account_id), control.shot)
            return
        if control.flags == sandbox.PICK_TARGET:
            if self.practice and self.practice_game is not None and self.world is not None:
                self._pick_practice_target(self.vehicle_for(account_id), control.shot)
            return
        if control.flags == sandbox.TARGET_AI:
            if sandbox.target_ai_available(self) and self.world is not None:
                self._set_enemy_ai(self.vehicle_for(account_id), control.shot)
            return
        if control.flags == sandbox.TANK_STATS:
            vehicle = self.vehicle_for(account_id)
            if self.sandbox_enabled and vehicle.alive:
                self._set_sandbox_stat(vehicle, *divmod(control.shot, 256))
            return
        if control.flags == sandbox.TOGGLE_MAP_BORDER:
            if self.sandbox_enabled and self.world is not None:
                self._toggle_map_border(self.vehicle_for(account_id))
            return
        if self.sandbox_enabled:
            vehicle = self.vehicle_for(account_id)
            self._sandbox_inputs[account_id] = (self.simulation_time, control)
            if vehicle.sandbox_reload_override is None:
                self._set_fast_reload(vehicle, bool(control.flags & sandbox.FAST_RELOAD))
            vehicle.sandbox_god_mode = bool(control.flags & sandbox.GOD_MODE)
            if control.flags & sandbox.RESTORE_HP and vehicle.health < vehicle.max_health:
                vehicle.health = vehicle.max_health
                self.emit('sandbox_restore_hp', vehicle.entity_id, health=vehicle.health)
        if control.flags & sandbox.GRAVITY_JUMP:
            if self.big_boss_mode:
                vehicle = self.vehicle_for(account_id)
                if self._is_boss(vehicle):
                    self._boss_blast(vehicle)
                else:
                    state = next((s for s in vehicle.consumables if s and s.spec in big_boss.ACTIVE_SPECS), None)
                    if state is not None:
                        self._activate_boss_role_ability(vehicle, state, self.simulation_time)
            elif self.gravity_mode:
                self._gravity_jump(self.vehicle_for(account_id))
            elif self.mad_games_mode:
                vehicle = self.vehicle_for(account_id)
                if (state := mad_games.irradiator_state(vehicle)) is not None:
                    self._activate_mad_irradiator(vehicle, state, self.simulation_time, None)
        if self.practice:
            vehicle = self.vehicle_for(account_id)
            if control.flags & sandbox.NEXT_TARGET:
                choices = tuple(self.practice_types)
                self.practice_target = None
                self.practice_selection = choices[(choices.index(self.practice_selection)+1) % len(choices)]
                self.emit('practice_notice', vehicle.entity_id, notice=self.practice_selection)
            if control.flags & sandbox.TOGGLE_TARGET_FIRE:
                self._toggle_practice_return_fire(vehicle)
            if control.flags & sandbox.CLEAR_TARGETS:
                self._clear_practice_targets(vehicle)
            elif control.flags & sandbox.REMOVE_TARGET and self.world is not None:
                entity, _ = self.aim_hit(vehicle)
                if entity in self.practice_active:
                    self._remove_practice_targets({entity})
                    self.practice_reuse_slot = entity
            elif control.flags & sandbox.SPAWN_TARGET:
                self._spawn_practice_target(vehicle)
        if self.sandbox_enabled and control.flags & sandbox.TELEPORT:
            self._teleport_vehicle(self.vehicle_for(account_id))

    def _toggle_map_border(self, vehicle):
        world = self.world
        entity = vehicle.entity_id
        if entity not in world.open_map_borders:
            world.open_map_borders = world.open_map_borders | {entity}
            notice = sandbox.MAP_BORDER_OPEN
        elif world._inside(vehicle.pose, entity, bounds=world.assets.bounds):
            world.open_map_borders = world.open_map_borders - {entity}
            notice = sandbox.MAP_BORDER_CLOSED
        else:
            notice = sandbox.MAP_BORDER_OUTSIDE
        self.emit('sandbox_map_border', entity, notice=notice)

    def _set_enemy_ai(self, player_vehicle, selection):
        everyone, level = divmod(selection, sandbox.AI_APPLY_ALL)
        difficulty = sandbox.AI_LEVELS[level]
        group = self.practice_active if self.practice else self.training_bots
        if not everyone:
            entity, _ = self.aim_hit(player_vehicle)
            group = (entity,) if entity in group else ()
        targets = [self.vehicles[entity] for entity in sorted(group)
                   if self.vehicles[entity].team != player_vehicle.team
                   and (self.vehicles[entity].alive or everyone and not self.practice)]
        for target in targets:
            entity = target.entity_id
            if self.practice:
                if difficulty is None:
                    self.practice_gunners.pop(entity, None)
                elif entity in self.practice_gunners:
                    self.practice_gunners[entity].set_difficulty(difficulty)
                else:
                    self.practice_gunners[entity] = TrainingBot(entity, difficulty)
            elif difficulty is None:
                self.disabled_training_bots.add(entity)
            else:
                self.disabled_training_bots.discard(entity)
                self.training_bots[entity].set_difficulty(difficulty)
            if difficulty is None:
                target.flags = 0
                self._cancel_burst(target)
                self._stop_fire(target, self.simulation_time, 'sandbox_ai')
        self.emit('sandbox_ai', player_vehicle.entity_id, count=len(targets),
                  difficulty=difficulty, everyone=bool(everyone))

    def _teleport_vehicle(self, vehicle):
        if self.world is None:
            return
        entity = vehicle.entity_id
        _, (x, _, z) = self.aim_hit(vehicle, ray=self._ability_ray(vehicle))
        pose = self.world.training_placement(entity, x, z, vehicle.pose.yaw, stationary=True)
        if pose is None:
            return
        vx, _, vz = self.world.motion[entity].velocity
        ground = self.world.ground_motion.get(entity)
        if ground is not None and ground.active and ground.velocity is not None:
            vx,vz = ground.velocity
        forward_x, forward_z = math.sin(pose.yaw), math.cos(pose.yaw)
        self.world.relocate_training_body(entity, pose,
            motion=Motion(CollisionWorld._road_velocity(pose, vx, vz)))
        if abs(vx*forward_z-vz*forward_x) > .001:
            self.world.ground_motion[entity] = GroundMotion((vx, vz), active=True)
        vehicle.pose = pose
        vehicle.speed = vx*forward_x+vz*forward_z
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

    def _boss_blast(self, source):
        now = self.simulation_time
        state = next((slot for slot in source.consumables if slot and slot.spec is big_boss.BLAST_WAVE), None)
        if (self.big_boss is None or self.world is None or state is None or not self.started
                or self.finished_at or not source.alive or not self._player_attached(source)
                or not state.activate(now)):
            return False
        origin = source.pose.position
        affected = []
        for target in self.vehicles.values():
            if not target.alive or target.team == source.team or not self._player_attached(target):
                continue
            target_class = vehicle_class(self._boss_game, target.definition)
            impulse = big_boss.blast_impulse(origin, target.pose.position,
                                            target.definition.mass, source.pose.yaw, target_class)
            if impulse is None:
                continue
            entity = target.entity_id
            velocity, spin = impulse
            grounded = self.world.motion[entity].grounded
            sliding = spin is None and self.world.add_ground_velocity(entity, velocity[0], velocity[2])
            light = target_class == 'lightTank'
            if sliding or self.world.add_flight_velocity(
                    entity, velocity, angular_delta=spin,
                    linear_damping=big_boss.LIGHT_BLAST_AIR_DAMPING if light else None):
                self.big_boss.impulses[entity] = source.entity_id, now + (12. if light else 4.)
                if not sliding and entity not in self.big_boss.blast_landings:
                    self.big_boss.blast_landings[entity] = target.pose if grounded else None
            self._damage_health(target, max(1, round(target.max_health * big_boss.BLAST_DAMAGE)),
                                source.entity_id, now, shell_hit=False, kinetic=True)
            affected.append(entity)
        self._consumables(source, now)
        self.emit('boss_blast', source.entity_id, targets=tuple(affected), position=source.pose.position)
        return True

    def _recover_blast_landings(self):
        world = self.world
        low_x, low_z, high_x, high_z = world.assets.bounds
        for entity, origin in tuple(self.big_boss.blast_landings.items()):
            if world.motion[entity].grounded:
                del self.big_boss.blast_landings[entity]
                continue
            current = world.poses[entity]
            x, y, z = current.position
            if low_x <= x <= high_x and low_z <= z <= high_z and y >= self.terrain.height(x, z) - 8.:
                continue
            for margin in (5., 10., 18.):
                safe_x = min(high_x-margin, max(low_x+margin, x))
                safe_z = min(high_z-margin, max(low_z+margin, z))
                pose = world.training_placement(entity, safe_x, safe_z, current.yaw, stationary=True)
                if pose is not None:
                    break
            else:
                pose = None
                if origin is not None:
                    origin_x, _, origin_z = origin.position
                    pose = world.training_placement(entity, origin_x, origin_z, origin.yaw, stationary=True)
                if pose is None:
                    # Use the same validated team spawns as a normal respawn.
                    from .map_catalog import map_team_capacity
                    team = self.vehicles[entity].team
                    for slot in range(map_team_capacity(self.map_id)):
                        spawn_x, spawn_z = training_spawn(self.map_spec, team, slot)
                        yaw = training_yaw(self.map_spec, team, slot)
                        pose = world.training_placement(entity, spawn_x, spawn_z, yaw, stationary=True)
                        if pose is not None:
                            break
                    else:
                        continue
            world.relocate_training_body(entity, pose)
            world.dynamic_recoveries.append((entity, current.position, pose.position))
            del self.big_boss.blast_landings[entity]

    def _bomber_blast(self, source, level, health, at):
        origin = source.pose.position
        start = (origin[0], origin[1]+1., origin[2])
        damage = max(1, round(health * big_boss.BOMB_DAMAGE[level]))
        targets = []
        for target in self.vehicles.values():
            if (not target.alive or target.team == source.team or not self._player_attached(target)
                    or math.dist(origin, target.pose.position) > big_boss.BOMB_RADIUS):
                continue
            x,y,z = target.pose.position
            if self.world.static_segment(start, (x,y+1.,z))[0] is None:
                targets.append(target.entity_id)
        self.emit('bomber_blast', source.entity_id, targets=tuple(targets), position=origin,
                  compact_id=big_boss.ROLE_ACTIVE[big_boss.BOMBER][level].compact_id)
        for entity in targets:
            if self._consume_boss_shield(self.vehicles[entity], at):
                continue
            self._damage_health(self.vehicles[entity], damage, source.entity_id, at,
                                shell_hit=False, kinetic=True)

    def _activate_boss_role_ability(self, vehicle, state, now, view=None):
        if self.world is None or now < state.ready_at:
            return False
        entity = vehicle.entity_id
        role, level = self.big_boss.roles[entity], self.big_boss.levels[entity]
        if role == big_boss.GRAVITON:
            # A miss at the sky cannot put a trap at arbitrary range.
            ray = self._ability_ray(vehicle) if view is None else None
            center = big_boss.graviton_center(self.world, vehicle.pose.position, ray,
                                              self.shell_for(vehicle).max_distance, view)
            if center is None or not state.activate(now):
                return False
            trap = big_boss.GravityTrap(self.big_boss.next_trap, entity, vehicle.team, center, level,
                                       now+big_boss.TRAP_DURATION)
            self.big_boss.next_trap += 1
            self.big_boss.traps.append(trap)
            self.emit('gravitrap', entity, position=center, expires_at=trap.expires_at,
                      compact_id=state.spec.compact_id, traps=tuple(self.big_boss.traps))
        elif not state.activate(now):
            return False
        elif role == big_boss.BOMBER:
            self._bomber_blast(vehicle, level, vehicle.health, now)
        elif role == big_boss.REGENERATOR:
            self.big_boss.heal_waves[entity] = (now+big_boss.HEAL_INTERVAL, level+2, level)
            self._publish_state_modifiers(vehicle)
        self._consumables(vehicle, now)
        return True

    def _emit_boss_vehicle_effects(self, vehicle):
        effects = self.big_boss.vehicle_effects(vehicle.entity_id)
        self.emit('boss_vehicle_effects', vehicle.entity_id, effects=effects)

    def _consume_boss_shield(self, target, at, *, ident=None):
        if self.big_boss is None or not self.big_boss.take_shield(target.entity_id, at, ident=ident):
            return False
        self._emit_boss_vehicle_effects(target)
        return True

    def _suspended_entities(self):
        return self.world.suspended_entities if isinstance(self.world, CollisionWorld) else ()

    def _heal_health(self, source, target, amount):
        if not target.alive or target.entity_id in self._suspended_entities():
            return 0
        healed = min(target.max_health-target.health, round(amount))
        if healed > 0:
            target.health += healed
            self.emit('health', source.entity_id, target.entity_id, health=target.health, healed=healed)
        return max(0, healed)

    def _mad_heal(self, source, target, amount):
        if self._heal_health(source, target, amount):
            self.emit('mad_regenerator_fx', target.entity_id, source.entity_id)

    def _boss_heal(self, source, target, amount):
        healed = self._heal_health(source, target, amount)
        if self.big_boss.poison.pop(target.entity_id, None):
            self._emit_boss_vehicle_effects(target)
            return True
        return healed > 0

    def _grant_regenerator_shield(self, target, level, at):
        if self._fire_active(target):
            self._stop_fire(target, at, 'energy_shield')
        if target.alive:
            self.big_boss.grant_shield(target.entity_id, at, big_boss.SHIELD_DURATION[level])
            self._emit_boss_vehicle_effects(target)

    def _boss_healing_shot(self, shooter, target, shell, at):
        level = shell.healing_level
        self._boss_heal(shooter, target,
                        shell.damage * (big_boss.HEAL_SHOT[level]+self.rng.uniform(-.15, .15)))
        self.emit('boss_role_effect', target.entity_id, compact_id=big_boss.HEAL_SHOT_EFFECTS[level])
        if not shooter.alive:
            return
        shield = next((slot for slot in shooter.consumables
                       if slot and slot.spec in big_boss.SHIELD_SPECS), None)
        if shield is not None and shield.activate(at):
            self._grant_regenerator_shield(target, self.big_boss.levels[shooter.entity_id], at)

    def _boss_poison(self, shooter, target, level, at):
        effects = self.big_boss.poison.setdefault(target.entity_id, {})
        previous = effects.get(shooter.entity_id)
        damage = max(1, round(target.max_health*big_boss.POISON_DAMAGE[level]))
        poison = big_boss.Poison(previous.damage if previous else damage,
                                previous.next_tick if previous else at+big_boss.POISON_INTERVAL,
                                big_boss.POISON_TICKS, previous.started if previous else at)
        effects[shooter.entity_id] = poison
        self._emit_boss_vehicle_effects(target)

    def _advance_boss_roles(self, now):
        boss = self.big_boss
        for entity, (due, count, level) in tuple(boss.heal_waves.items()):
            if now < due:
                continue
            source = self.vehicles[entity]
            if count == 1:
                del boss.heal_waves[entity]
                self._publish_state_modifiers(source)
            else:
                boss.heal_waves[entity] = (due+big_boss.HEAL_INTERVAL, count-1, level)
            for target in self.vehicles.values():
                if (target.alive and target.team == source.team and self._player_attached(target)
                        and math.dist(source.pose.position, target.pose.position) <= big_boss.HEAL_RADIUS):
                    fraction = big_boss.HEAL_FRACTION * (.5 if target is source else 1.)
                    if target.health < target.max_health:
                        self.emit('boss_role_effect', target.entity_id,
                                  compact_id=big_boss.HEAL_SHOT_EFFECTS[boss.levels[source.entity_id]])
                    self._boss_heal(source, target, target.max_health*fraction)
        for entity, shield in tuple(boss.shields.items()):
            if now >= shield.expires:
                del boss.shields[entity]
                self._emit_boss_vehicle_effects(self.vehicles[entity])
        for entity, poisons in tuple(boss.poison.items()):
            target = self.vehicles[entity]
            for source, poison in tuple(poisons.items()):
                if not target.alive or self.finished_at:
                    break
                if now < poison.next_tick:
                    continue
                if poison.remaining == 1:
                    del poisons[source]
                    if not poisons:
                        del boss.poison[entity]
                        self._emit_boss_vehicle_effects(target)
                else:
                    poisons[source] = poison._replace(next_tick=poison.next_tick+big_boss.POISON_INTERVAL,
                                                      remaining=poison.remaining-1)
                self._damage_health(target, poison.damage, source, poison.next_tick, shell_hit=False)

    def _toggle_practice_return_fire(self, player):
        self.practice_return_fire = not self.practice_return_fire
        notice = sandbox.TARGET_FIRE_ON if self.practice_return_fire else sandbox.TARGET_FIRE_OFF
        self.emit('practice_notice', player.entity_id, notice=notice)

    def _practice_menu_action(self, player, action):
        if action == sandbox.MENU_SPAWN:
            self._spawn_practice_target(player)
            return
        if action == sandbox.MENU_CLEAR:
            self._clear_practice_targets(player)
            return
        if action == sandbox.MENU_RETURN_FIRE:
            self._toggle_practice_return_fire(player)
            return
        entity, _ = self.aim_hit(player, ray=self._ability_ray(player))
        target = self.vehicles.get(entity) if entity in self.practice_active else None
        notice = sandbox.MENU_MISS
        if target is not None and action == sandbox.MENU_REMOVE:
            self._remove_practice_targets({entity})
            self.practice_reuse_slot = entity
            notice = sandbox.MENU_REMOVED
        elif target is not None and target.alive:
            if action == sandbox.MENU_HEAL:
                notice = sandbox.MENU_FULL_HEALTH
                if target.health < target.max_health:
                    target.health = target.max_health
                    self.emit('health', player.entity_id, entity, health=target.health)
                    notice = sandbox.MENU_HEALED
            elif action == sandbox.MENU_DESTROY and self._damage_health(
                    target, target.health, None, self.simulation_time, shell_hit=False):
                notice = sandbox.MENU_DESTROYED
        self.emit('practice_feedback', player.entity_id, notice=notice)

    def _pick_practice_target(self, player, selection):
        from .practice_catalog import catalog, definition

        now = self.simulation_time
        if now < self.practice_next_pick:
            return
        self.practice_next_pick = now + .4
        try:
            target = catalog(self.practice_game)[selection & 0xffff]
            if not target.installed:
                raise ValueError('Missing Polygon vehicle resources')
            selected = definition(self.practice_game, target, selection >> 16)
        except (KeyError, OSError, ValueError, ParseError):
            self.emit('practice_catalog_status', player.entity_id, selected=False)
            return
        self.practice_target = selected
        self.emit('practice_catalog_status', player.entity_id, selected=True)

    def _make_practice_scene(self, definition):
        from collections import ChainMap
        from .mesh_files import MeshFiles

        root = self.world.assets
        scene = root.for_vehicle(self.practice_game, definition)
        scene.practice_shape_key = definition.descriptor
        scene.mesh_files = MeshFiles()
        scene.mesh_files.partitions = ChainMap({}, root.mesh_files.partitions)
        scene.terrain_meshes = ChainMap({}, root.terrain_meshes)
        scene._owns_mesh_files = True
        try:
            self._configure_bot_scene(scene, definition)
            scene.prepare(self.terrain)
        except BaseException:
            scene.close()
            raise
        return scene

    def _spawn_practice_target(self, player_vehicle):
        now = self.simulation_time
        if self.world is None or now < self.practice_next_spawn:
            return
        self.practice_next_spawn = now+.4
        selected = self.practice_target
        presets = self.practice_types[self.practice_selection]
        preset = self.practice_default_scenes.get(presets[0])
        if selected is None and preset is None:
            # Resource-free simulation fixtures do not have stock scenes.
            slots = presets
        else:
            requested = selected if selected is not None else preset.definition
            slots = sorted(self.practice_slots, key=lambda ident: (
                ident != self.practice_reuse_slot,
                ident not in self.practice_spawned,
                self.vehicles[ident].definition != requested,
                self.practice_numbers.get(ident, ident)))
        entity = next((ident for ident in slots if not self.vehicles[ident].alive), None)
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
        current = self.world.assets_for(entity)
        assets = preset if selected is None and preset is not None and current.definition != preset.definition else current
        new_scene = selected is not None and current.definition != selected
        if new_scene:
            try:
                assets = self._make_practice_scene(selected)
            except (OSError, ValueError, ParseError):
                self.emit('practice_catalog_status', player_vehicle.entity_id, selected=False)
                return
        changed = assets is not current
        try:
            pose = (self.world.replace_training_body(entity, assets, ((x, z, yaw),)) if changed
                    else self.world.training_placement(entity, x, z, yaw))
        finally:
            if new_scene and self.world.assets_for(entity) is not assets:
                self.world.release_practice_scene(assets)
        if pose is None:
            return
        new = entity not in self.practice_spawned
        new_model = changed and not new and self.vehicles[entity].descriptor != assets.definition.descriptor
        if new_model:
            self._cancel_practice_projectiles({entity})
            self.emit('practice_retire', entity)
            self.practice_client_ids[entity] = self.practice_next_client_id
            self.practice_next_client_id += 1
        if changed:
            self._restore_vehicle(entity, pose, replacement=(assets.definition.descriptor, None, (), 0))
            previous_scene = self.practice_scenes.pop(entity, None)
            if previous_scene is not None:
                self.world.release_practice_scene(previous_scene)
            if new_scene:
                self.practice_scenes[entity] = assets
            self._refresh_training_geometry()
        else:
            self._restore_vehicle(entity, pose)
        target = self.vehicles[entity]
        target.mad_unspotted = False
        if new:
            number = self.practice_numbers[entity] = len(self.practice_numbers)+1
            target.nickname = f'dummy {number}'
        if self.practice_return_fire:
            self.practice_gunners[entity] = TrainingBot(entity)
        else:
            self.practice_gunners.pop(entity, None)
        self.practice_spawned.add(entity)
        self.practice_active.add(entity)
        self.motion_frame = None
        if self.practice_reuse_slot == entity:
            self.practice_reuse_slot = None
        self.emit('practice_spawn', entity, first=new or new_model, placed_by=player_vehicle.entity_id,
                  position=pose.position)

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
        self.practice_reuse_slot = None
        self._remove_practice_targets(self.practice_active.copy())
        self.emit('practice_notice', player_vehicle.entity_id, notice=sandbox.TARGETS_CLEARED)

    def _remove_practice_targets(self, entities):
        for entity in sorted(entities):
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
            self.practice_gunners.pop(entity, None)
            self.respawn_at.pop(entity, None)
        self._cancel_practice_projectiles(entities)
        self.practice_active.difference_update(entities)
        self.practice_next_spawn = 0.
        self.motion_frame = None

    def _refresh_sandbox_targeting(self, vehicle):
        vehicle.gun.set_factors(self._crew_role_factor(vehicle, 'gunner')
                                * sandbox.stat_bonus(vehicle, sandbox.STAT_TURRET), 1.)
        self.emit('targeting', vehicle.entity_id, yaw_rate=vehicle.gun.yaw_rate,
                  pitch_rate=vehicle.gun.pitch_rate)

    def _update_sandbox_health(self, vehicle):
        base = vehicle.sandbox_base_health or vehicle.max_health
        bonus = sandbox.stat_bonus(vehicle, sandbox.STAT_HEALTH)
        new_max = min(32767, int(base * bonus + .5))
        vehicle.sandbox_base_health = base if bonus != 1. else None
        if new_max == vehicle.max_health:
            return False
        if vehicle.alive:
            vehicle.health = max(1, int(vehicle.health * new_max / vehicle.max_health + .5))
        vehicle.max_health = new_max
        return True

    def _set_sandbox_stat(self, vehicle, kind, strength):
        if kind == sandbox.STAT_BOOST:
            vehicle.sandbox_boost_strength = strength
            vehicle.sandbox_boost = bool(strength)
        elif kind == sandbox.STAT_RELOAD:
            # 0 restores the stock cycle, 1–100 selects 5.0–0.5 s.
            seconds = 5. - 4.5*(strength-1)/99 if strength else 0.
            self._set_fast_reload(vehicle, bool(strength), override=seconds)
        else:
            if strength:
                vehicle.sandbox_stats[kind] = strength
            else:
                vehicle.sandbox_stats.pop(kind, None)
            if kind in (sandbox.STAT_AIM, sandbox.STAT_ACCURACY, sandbox.STAT_TURRET):
                self._refresh_sandbox_targeting(vehicle)
            elif kind == sandbox.STAT_HEALTH and self._update_sandbox_health(vehicle):
                self.emit('max_health', vehicle.entity_id, max_health=vehicle.max_health, health=vehicle.health)

    def _apply_initial_preset(self, vehicle):
        for stat, strength in enumerate(vehicle.sandbox_preset):
            if strength:
                self._set_sandbox_stat(vehicle, stat, strength)

    def _clear_sandbox_stats(self, vehicle):
        if not getattr(vehicle, 'sandbox_stats', None):
            return
        refresh_aim = bool(vehicle.sandbox_stats.keys() &
                           {sandbox.STAT_AIM, sandbox.STAT_ACCURACY, sandbox.STAT_TURRET})
        vehicle.sandbox_stats.clear()
        self._update_sandbox_health(vehicle)
        if refresh_aim:
            self._refresh_sandbox_targeting(vehicle)

    def _cancel_practice_projectiles(self, entities):
        for shot, projectile in tuple(self.projectiles.items()):
            if projectile.shooter in entities:
                self.emit('shot_end', projectile.shooter, shot=shot, point=projectile.position,
                          guided=projectile.shell.missile is not None)
                del self.projectiles[shot]

    def _restore_vehicle(self, entity, pose, *, replacement=None):
        previous = self.vehicles[entity]
        descriptor, shells, specs, camouflage = replacement if replacement is not None else (
            previous.descriptor, previous.loadout_shells,
            tuple(slot.spec if slot else None for slot in previous.consumables
                  if slot is None or slot.spec is not uprising.RESPAWN_CLOAK), previous.camouflage)
        restored = Vehicle(entity, previous.account_id, previous.nickname, previous.team,
                           descriptor, pose, loadout_ammo=previous.loadout_ammo,
                           loadout_shells=shells, camouflage=camouflage,
                           generation=previous.generation + 1,
                           consumables=tuple(self._new_consumable(previous, spec) if spec else None
                                             for spec in specs))
        restored.sandbox_fast_reload = previous.sandbox_fast_reload
        restored.sandbox_reload_override = previous.sandbox_reload_override
        restored.sandbox_boost_strength = previous.sandbox_boost_strength
        restored.sandbox_god_mode = previous.sandbox_god_mode
        restored.sandbox_stats = dict(previous.sandbox_stats)
        previous_abilities = None
        if (selection := self.boss_pending_roles.pop(entity, None)) is not None:
            previous_abilities = self._pin_boss_role(restored, *selection)
        elif self.big_boss is not None and (replacement is not None or entity in self.big_boss.roles):
            if replacement is None:
                self.big_boss.assign_role(entity)
            previous_abilities = self._set_boss_role_ability(restored)
        if replacement is not None or self.unlimited_lives or self.uprising is not None:
            for field in ('shots', 'hits', 'damage', 'blocked_damage', 'assisted_damage',
                          'kills', 'direct_hits', 'piercings', 'damage_received', 'hits_received',
                          'piercings_received', 'distance', 'capture_points', 'defense_points'):
                setattr(restored, field, getattr(previous, field))
        restored.consumable_clock = self.simulation_time
        self._configure_battle_vehicle(restored, self.world.assets_for(entity))
        if restored.sandbox_stats:
            self._update_sandbox_health(restored)
            restored.gun.set_factors(self._crew_role_factor(restored, 'gunner')
                                     * sandbox.stat_bonus(restored, sandbox.STAT_TURRET), 1.)
        if self.mad_games_mode:
            self._clear_mad_irradiator(restored)
            self._install_mad_games_ability(restored)
            mad_games.charge_shield(restored, self.simulation_time)
        if self.uprising is not None:
            self._install_uprising_ability(restored)
        if self.team_spotting:
            restored.mad_unspotted = True
        restored.movable_armor_state = self._movable_armor_snapshot_at(restored, self.simulation_time)
        self.world.relocate_training_body(entity, pose)
        self.world.set_turret(0., entity)
        if entity in self.ram_detached_hulls:
            self.ram_detached_hulls.remove(entity)
            self.emit('ram_turret_restored', entity)
        self._retire_ram_turret(entity)
        self.vehicles[entity] = restored
        if self.uprising is not None:
            self._clear_uprising_mark(entity)
            self._clear_uprising_curse(entity)
            self.uprising.history.pop(entity, None)
            self.uprising.protected_shots = {key for key in self.uprising.protected_shots if key[1] != entity}
        if previous_abilities is not None:
            self._emit_boss_role(entity, previous_abilities)
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

    def reserve_training_seat(self, account_id):
        self.cancel_time_stop(account_id)
        vehicle = self.vehicle_for(account_id)
        self.hangar_players.add(account_id)
        self.players[account_id].loaded = False
        vehicle.flags = 0
        vehicle.airborne_input_until = 0.
        vehicle.sandbox_god_mode = False
        vehicle.sandbox_boost = False
        vehicle.sandbox_boost_strength = None
        self._set_fast_reload(vehicle, False)
        vehicle.sandbox_reload_override = None
        self._clear_sandbox_stats(vehicle)
        self._cancel_burst(vehicle)
        vehicle.autoaim_target, vehicle.autoaim_offset = 0, None
        vehicle.gun.set_angles(vehicle.gun.yaw, vehicle.gun.pitch)
        self.respawn_at.pop(vehicle.entity_id, None)
        self.boss_pending_roles.pop(vehicle.entity_id, None)
        self._sandbox_inputs.pop(account_id, None)

    def training_tank_available(self, account_id):
        vehicle = self.vehicle_for(account_id)
        if (resurrection := getattr(self, 'uprising', None)) is not None:
            return (resurrection.remaining(vehicle.entity_id) > 0
                    and (vehicle.alive or account_id in self.hangar_players))
        boss = self.big_boss
        if boss is not None and boss.tickets and boss.lives.get(vehicle.entity_id, 1) <= 0:
            return False
        return self.unlimited_lives or vehicle.alive

    def cancel_time_stop(self, account_id):
        if (timeline := getattr(self, 'time_stop', None)) is not None and timeline.cancel(account_id):
            self.motion_frame = None

    def reenter_training(self, account_id, scene, shells, consumables, *, camouflage=0):
        from .map_catalog import map_team_capacity

        previous = self.vehicle_for(account_id)
        entity = previous.entity_id
        if self.time_stop is not None and self.time_stop.entity == entity:
            raise TrainingSpawnUnavailable('Time Stop return is waiting for a safe spawn')
        # Validate the persistent loadout before changing the physics body.
        candidate = Vehicle(entity, account_id, previous.nickname, previous.team,
                            scene.definition.descriptor, previous.pose,
                            definition=scene.definition, loadout_shells=shells)
        self._configure_ammunition(candidate)
        if self.gravity_mode and vehicle_class(self._gravity_game, scene.definition) in (
                'lightTank', 'mediumTank', 'AT-SPG'):
            consumables += (None,) * max(0, 3-len(consumables)) + (GRAVITY_JUMP_SPEC,)
        spawns = ((*training_spawn(self.map_spec, previous.team, slot),
                   training_yaw(self.map_spec, previous.team, slot))
                  for slot in range(map_team_capacity(self.map_id)))
        enemy_positions = [vehicle.pose.position for vehicle in self.vehicles.values()
                           if vehicle.team != previous.team and vehicle.alive
                           and self._player_attached(vehicle)]
        if enemy_positions:
            def clearance(spawn):
                x, z, _ = spawn
                return min((x-enemy_x)**2 + (z-enemy_z)**2
                           for enemy_x, _, enemy_z in enemy_positions)

            spawns = sorted(spawns, key=clearance, reverse=True)
        changed_geometry = scene is not self.world.assets_for(entity)
        if not changed_geometry:
            pose = next((placement for x, z, yaw in spawns if
                         (placement := self.world.training_placement(entity, x, z, yaw,
                                                                     stationary=True)) is not None), None)
        else:
            pose = self.world.replace_training_body(entity, scene, spawns)
        if pose is None:
            raise TrainingSpawnUnavailable('All team spawn points are occupied')
        self._lose_target(entity)
        vehicle = self._restore_vehicle(entity, pose,
                                        replacement=(scene.definition.descriptor, shells, consumables, camouflage))
        if previous.alive and (not self.unlimited_lives or self.big_boss and self.big_boss.tickets):
            fraction = previous.health / previous.max_health
            vehicle.health = max(1, int(vehicle.max_health * fraction + .5))
        if self.uprising is not None:
            if not previous.alive:
                self._add_uprising_respawn_cloak(vehicle, self.simulation_time)
            self._consumables(vehicle, self.simulation_time)
        if changed_geometry:
            self._refresh_training_geometry()
        self.hangar_players.remove(account_id)
        self.emit('training_reentry', entity)

    def _refresh_training_geometry(self):
        if self.drive_workers is not None:
            # The main simulation handles driving while the replicas rebuild.
            self.drive_workers.advance_close()
            self._drive_workers_attempted = False
        if self.time_stop is not None:
            self.time_stop.refresh_geometry()

    def _advance_respawns(self, now):
        if not self.respawn_at or self.world is None:
            return
        from .map_catalog import map_team_capacity
        for entity, due in tuple(self.respawn_at.items()):
            if now < due:
                if self.uprising is not None and self.vehicles[entity].account_id is not None:
                    remaining = due - now
                    if math.ceil(remaining) < math.ceil(remaining + self.STEP):
                        # The native timer listener may be created after the
                        # death packet. Refresh the value on every second edge.
                        self.emit('uprising_clock', entity, seconds=remaining)
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
                    if self.uprising is not None:
                        limit = sandbox.reload_limit(vehicle)
                        self.uprising.on_respawn(entity, bot=vehicle.account_id is None, now=now,
                                                 cooldown_limit=limit)
                        self._add_uprising_respawn_cloak(vehicle, now)
                    if self.boss_role_picker and vehicle.account_id is not None:
                        self.emit('boss_picker', entity, state=1)
                    self.emit('practice_spawn', entity, first=False, position=pose.position,
                              repaired=critical+destroyed)
                    if self.uprising is not None:
                        self._consumables(vehicle, now)
                    self.emit('ready', entity, yaw_rate=vehicle.gun.yaw_rate,
                              pitch_rate=vehicle.gun.pitch_rate, respawn=True)
                    break
            else:
                self.respawn_at[entity] = now+.5

    @staticmethod
    def _add_uprising_respawn_cloak(vehicle, now):
        cloak = ConsumableState(uprising.RESPAWN_CLOAK)
        cloak.activate(now)
        vehicle.consumables = tuple(slot for slot in vehicle.consumables
                                    if slot is None or slot.spec is not uprising.RESPAWN_CLOAK) + (cloak,)

    def _award_elixir(self, vehicle, amount):
        state = self.uprising
        entity = vehicle.entity_id
        choosing = vehicle.account_id is not None and entity in self.respawn_at
        previous = state.offers(entity) if choosing else ()
        balance = state.earn(entity, amount)
        if balance is not None and vehicle.account_id is not None:
            self.emit('uprising_elixir', entity, balance=balance)
            if choosing and (offers := state.offers(entity)) != previous:
                self.emit('uprising_offers', entity, offers=offers)

    def select_uprising_upgrade(self, account_id, compact_id, selected, now=None):
        if self.uprising is None:
            return False
        self.step(time.monotonic() if now is None else now)
        player = self.players[account_id]
        entity = player.vehicle_id
        if (self.finished_at or not player.attached or not player.loaded
                or self.vehicles[entity].alive or entity not in self.respawn_at):
            return False
        balance = self.uprising.select(entity, compact_id, selected)
        if balance is None:
            return False
        self.emit('uprising_elixir', entity, balance=balance)
        self.emit('uprising_offers', entity, offers=self.uprising.offers(entity))
        return True

    def _advance_uprising_income(self, now):
        if not self.started:
            return
        points = self.uprising.income_since_tick(now - self.started)
        if points:
            for vehicle in self.vehicles.values():
                if self._player_attached(vehicle):
                    self._award_elixir(vehicle, points)

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
            strength = vehicle.sandbox_boost_strength
            vehicle.sandbox_boost = bool(flags & sandbox.BOOST) if strength is None else bool(strength)
            entity = vehicle.entity_id
            if not (self.world and self.world.supports_flight
                    and vehicle.alive and self._player_attached(vehicle)):
                self._sandbox_flights.pop(entity, None)
                continue
            state = self.world.motion[entity]
            flight = self._sandbox_flights.get(entity)
            if control is None:
                # Focus loss pauses input reports. Keep the flight switch,
                # while thrust, climb and steering still require fresh input.
                last = self._sandbox_inputs.get(vehicle.account_id)
                if last is not None:
                    flags |= last[1].flags & sandbox.FLIGHT
            if flags & sandbox.FLIGHT:
                if flight is None:
                    flight = self._sandbox_flights[entity] = sandbox.Flight(
                        vehicle.pose.position[1] + (4. if state.grounded else 0.))
                piloted.add(entity)
            else:
                self._sandbox_flights.pop(entity, None)
                if flight is not None and not state.grounded:
                    vx,vy,vz = state.velocity
                    self.world.set_flight_velocity(entity, (vx,min(vy,-4.),vz), (0.,0.,0.))
                continue
            x, _, z = vehicle.pose.position
            drive = self._active_control_flags(vehicle, now) if control is not None else 0
            velocity, spin = sandbox.flight_velocity(flight, vehicle.pose, state.velocity,
                self.world.flight_spin(entity), self.terrain.height(x,z), drive, flags,
                self.world.GRAVITY, dt, bounds=self.world.movement_bounds(entity))
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
        if (target is None or not target.alive or target.team==vehicle.team or mad_games.concealed(target, vehicle.team)
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

    def _lose_target(self, entity, *, include_owner=True):
        for vehicle in self.vehicles.values():
            if vehicle.autoaim_target and (vehicle.autoaim_target == entity
                                           or include_owner and vehicle.entity_id == entity):
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

    def _ability_ray(self, vehicle):
        pose = vehicle.pose
        rotation = self.world._dynamic_rotation(vehicle.entity_id) if self.world is not None else None
        if rotation is None:
            rotation = matrix(pose.yaw, pose.pitch, pose.roll)
        point = None
        if target := self.vehicles.get(vehicle.autoaim_target):
            if (target.alive and vehicle.autoaim_offset is not None
                    and not mad_games.concealed(target, vehicle.team)):
                p = target.pose
                offset = matrix(p.yaw, p.pitch, p.roll) @ vehicle.autoaim_offset
                point = tuple(a + b for a, b in zip(p.position, offset))
        return vehicle.gun.target_ray(pose.position, rotation, point)

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
        switch_shell(vehicle, shell_id)
        vehicle.next_shell = 0
        if vehicle.pump_times:
            # Keep any intershot and refill already in progress.
            if not (vehicle.reload_at or vehicle.pump_waiting or vehicle.pump_at):
                self._pump_segment(vehicle,self.simulation_time)
            self.emit('shell_selected',vehicle.entity_id,shell=shell_id)
            self.emit('ammo',vehicle.entity_id)
            if vehicle.reload_at:
                self.emit('reload', vehicle.entity_id,
                          delay=max(0., vehicle.reload_at-self.simulation_time),
                          total=vehicle.reload_total)
            elif not vehicle.clip:
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
                or not vehicle.alive or vehicle.pump_times or vehicle.burst_pending
                or not magazine_reload_allowed(vehicle)):
            return False
        vehicle.clip = 0
        vehicle.reload_kind = 'reload'
        vehicle.reload_total = self._magazine_duration(vehicle, 'reload')
        vehicle.reload_at = self.simulation_time + vehicle.reload_total
        self.emit('ammo',vehicle.entity_id)
        self.emit('reload',vehicle.entity_id,delay=vehicle.reload_total)
        return True

    def aim_hit(self, vehicle, *, ray=None):
        origin,direction = self.shot_ray(vehicle) if ray is None else ray
        if self.world is not None:
            # The collision scene is shared immutable geometry; controller
            # rows belong to each arena Vehicle and are snapshotted here for
            # the immediate aim query.
            armor_states = {entity: target.movable_armor_state
                            for entity,target in self.vehicles.items()
                            if target.movable_armor_state is not None}
            concealed = ({entity for entity, target in self.vehicles.items()
                          if mad_games.concealed(target, vehicle.team)} if self.team_spotting else ())
            options = {'ignore_entities': concealed} if concealed else {}
            body,point,_ = self.world.shot_path(origin,direction,self.shell_for(vehicle).max_distance,
                                                ignore_entity=vehicle.entity_id,
                                                movable_armor_states=armor_states or None, **options)
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
                or vehicle.burst_pending):
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
        if self._is_boss(vehicle):
            shell = dataclasses.replace(shell, damage=shell.damage * big_boss.SHELL_DAMAGE)
        origin,velocity,fired_shell = self._launch(vehicle,shell,now)
        weapon = vehicle.definition.weapon if vehicle.definition else None
        following = (min(weapon.burst_count-1,vehicle.clip,vehicle.ammo)
                     if vehicle.burst_gun else 0)
        duplet = self.uprising is not None and self.uprising.has(vehicle.entity_id, uprising.DUPLET)
        unlimited = self.unlimited_ammo or self.practice and vehicle.account_id is not None
        vehicle.duplet_pending = bool(duplet and (unlimited or vehicle.ammo > following))
        if following or vehicle.duplet_pending:
            # One accepted Fire starts its whole XML group.  Only rounds which
            # really remain in the current clip and selected ammunition are
            # scheduled, so a short final loadout cannot create free shells.
            delay = self._burst_duration(vehicle) if following else uprising.DUPLET_DELAY
            vehicle.burst_shell,vehicle.burst_effect = shell,shot_effect
            vehicle.burst_remaining = following + vehicle.duplet_pending
            vehicle.pump_at = vehicle.pump_total = 0.
            vehicle.pump_waiting = False
            vehicle.reload_kind = 'interval'
            vehicle.reload_at = now+delay
            vehicle.reload_total = delay
            # Native own-vehicle FX accepts the whole group on its first
            # confirmation. Snapshot the actual size before later shots/switches.
            self._emit_shot(vehicle, fired_shell, origin, velocity, now, shot_effect,
                            following+1+int(vehicle.duplet_pending))
            self.emit('ammo',vehicle.entity_id)
            self.emit('reload',vehicle.entity_id,delay=delay)
            return
        if vehicle.pump_times:
            self._fire_pump(vehicle,weapon,now,fired_shell,origin,velocity,shot_effect)
            return
        # Berserk's speed applies to both cycles; NewAutoloader only to the intershot.
        vehicle.reload_kind = 'interval' if vehicle.clip else 'reload'
        delay = self._magazine_duration(vehicle, vehicle.reload_kind)
        vehicle.reload_at = now+delay
        vehicle.reload_total = delay
        self._emit_shot(vehicle,fired_shell,origin,velocity,now,shot_effect)
        self.emit('ammo',vehicle.entity_id)
        self.emit('reload',vehicle.entity_id,delay=delay)

    def _training_bot_fire(self, bot, target, now, ai=None):
        """Fire only when the server collision query actually sees the target."""
        controller = ai or self.training_bots.get(bot.entity_id)
        if (controller is None or self.finished_at or not self.started
                or not bot.alive or target is None or not target.alive or mad_games.concealed(target, bot.team)
                or self.world is None or bot.reload_at or bot.ammo <= 0
                or not controller.shot_check_due(now)):
            return
        if bot.mad_cloaked and bot.flags & TrainingBot.REVERSE:
            return
        visible,_ = self.aim_hit(bot)
        if (visible == target.entity_id if ai is None else
                controller.sight_confirmed(target.entity_id, visible == target.entity_id, now)):
            if self.mad_games_mode:
                self._training_bot_mad_attack(bot, target, now)
            elif self.uprising is not None:
                ability = next((slot for slot in bot.consumables if slot and slot.spec in uprising.ACTIVE_ABILITIES), None)
                if ability is not None and now >= ability.ready_at:
                    if ability.spec in uprising.TARGETED_ABILITIES:
                        self._activate_uprising_targeted(bot, ability, now, target.entity_id)
                    elif ability.spec is uprising.BERSERK and bot.health > bot.max_health * .3:
                        self._activate_consumable(bot, ability, now)
                    elif ability.spec is uprising.PLAY_DEAD and bot.health < bot.max_health * .75:
                        self._activate_uprising_play_dead(bot, ability, now)
                    elif ability.spec is uprising.TIME_JUMP:
                        frame = self.uprising.rewind_frame(bot.entity_id, now)
                        if frame is not None and frame.health - bot.health >= bot.max_health * .15:
                            self._activate_uprising_time_jump(bot, ability, now)
            self._fire_vehicle(bot,now)

    def _training_bot_mad_attack(self, bot, target, now):
        if self.gun_destroyed(bot) or bot.pump_times and not bot.clip or bot.burst_pending:
            return
        state = next((slot for slot in bot.consumables if slot and slot.spec in (
            mad_games.DISCHARGE, mad_games.IRRADIATOR)), None)
        if state is None or now < state.ready_at:
            return
        if state.spec is mad_games.IRRADIATOR:
            self._activate_mad_irradiator(bot, state, now, target.entity_id)
        elif bot.health > bot.max_health * .30:
            self._activate_consumable(bot, state, now)

    def _training_bot_mad_mobility(self, bot, target, now):
        state = next((slot for slot in bot.consumables if slot and slot.spec in (
            mad_games.HIGH_OCTANE, mad_games.ACCELERATOR, mad_games.CHAMELEON)), None)
        if state is None or now < state.ready_at:
            return
        if state.spec is mad_games.CHAMELEON:
            if (target is not None and bot.flags & TrainingBot.REVERSE
                    and bot.health <= bot.max_health * .50 and bot.reload_at > now + 1.):
                self._activate_consumable(bot, state, now)
            return
        if bot.flags != TrainingBot.FORWARD or bot.speed < 2.:
            return
        if state.spec is mad_games.HIGH_OCTANE:
            self._activate_consumable(bot, state, now)
        elif target is not None:
            dx = target.pose.position[0] - bot.pose.position[0]
            dz = target.pose.position[2] - bot.pose.position[2]
            if 12. <= math.hypot(dx, dz) <= 48. and abs(wrap(math.atan2(dx, dz) - bot.pose.yaw)) < .18:
                self._activate_mad_dash(bot, state, now)

    def _training_bot_uprising_mobility(self, bot, target, now):
        if (target is None or bot.mad_cloaked or not bot.flags & TrainingBot.REVERSE
                or bot.health > bot.max_health * .5 or bot.reload_at <= now + 1.):
            return
        shadow = next((slot for slot in bot.consumables if slot and slot.spec is uprising.MOONWALK), None)
        if shadow is not None and now >= shadow.ready_at:
            self._activate_consumable(bot, shadow, now)

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

    def _launch(self, vehicle, shell, now, *, from_reserve=False):
        """Spend one round and spawn its projectile from the live pose."""
        if self._is_boss(vehicle):
            charge = next((s for s in vehicle.consumables if s and s.spec is big_boss.MEGASHOT), None)
            if charge is not None and charge.activate(now):
                shell = big_boss.megashot(shell)
        elif self.big_boss is not None:
            role = self.big_boss.roles.get(vehicle.entity_id)
            level = self.big_boss.levels.get(vehicle.entity_id, 0)
            poisoned = role == big_boss.CATALYST and any(
                s.uses and now < s.active_until for s in vehicle.consumables
                if s and s.spec in big_boss.ROLE_ACTIVE[role])
            shell = big_boss.role_shell(shell, role, level, poisoned=poisoned)
        if self.uprising is not None:
            factor = self.uprising.shell_damage_factor(vehicle.entity_id, self.rng.random)
            if uprising.active(vehicle, uprising.BERSERK, now):
                factor *= uprising.BERSERK_OUTGOING
            if factor != 1.:
                shell = dataclasses.replace(shell, damage=shell.damage * factor)
        factor = sandbox.stat_bonus(vehicle, sandbox.STAT_DAMAGE)
        if factor != 1.:
            shell = dataclasses.replace(shell, damage=shell.damage * factor)
        vehicle.shots += 1
        spend_round(vehicle, self.unlimited_ammo or self.practice and vehicle.account_id is not None,
                    from_reserve=from_reserve)
        self.next_shot += 1
        origin,direction = self.shot_ray(vehicle)
        direction = disperse_shot(direction, self.shot_dispersion(vehicle, now), self.dispersion_rng,
                                  vehicle.gun.aim_progress)
        vehicle.gun.aim_progress = 0.
        vehicle.last_shot_at = now
        velocity = tuple(v*shell.speed for v in direction)
        self.projectiles[self.next_shot] = Projectile(self.next_shot,vehicle.entity_id,
            shell,origin,velocity,now,recon_tracking=self.recon is not None)
        if self.gravity_mode:
            self._gravity_shell_impulse(vehicle, shell, direction, recoil=True)
        return origin,velocity,shell

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
        self._end_vehicle_cloak(vehicle, now)
        if getattr(shell, 'railgun', False):
            shot_effect = shell.effect_index
        self.projectiles[self.next_shot].trace_effect = shot_effect
        self.emit('shot',vehicle.entity_id,shot=self.next_shot,origin=origin,
                  velocity=velocity,gravity=shell.gravity,at=now,
                  shell_id=shell.compact_id,effect_index=shot_effect,
                  ammo_after=vehicle.ammo,
                  **({'shooting_count':shooting_count} if shooting_count != 1 else {}),
                  **({'guided':True} if shell.missile is not None else {}))

    def _missile_target(self, projectile):
        shooter = self.vehicles[projectile.shooter]
        player = self.players.get(shooter.account_id)
        if not (player and self._player_attached(shooter) and shooter.alive):
            return None
        camera = None
        if self.sandbox_enabled:
            control = self._active_sandbox_control(shooter, self.simulation_time)
            if control and control.flags & sandbox.MISSILE_VIEW and control.shot == projectile.shot:
                camera = control.direction()
        return missile_guidance_target(projectile, shooter.gun, shooter.pose.position,
                                       lambda: self.shot_ray(shooter), locked=bool(shooter.autoaim_target),
                                       camera=camera)

    def _fire_burst_next(self, vehicle, now):
        shell,shot_effect = vehicle.burst_shell,vehicle.burst_effect
        vehicle.reload_at = 0.
        weapon = vehicle.definition.weapon if vehicle.definition else None
        if self.gun_destroyed(vehicle):
            # Same refusal as Fire; all unfired group rounds are unloaded with
            # the rest of the clip, while their rack ammunition stays intact.
            self.emit('fire_rejected',vehicle.entity_id,reason='gun_destroyed',at=now)
            self._cancel_burst(vehicle)
            vehicle.clip = 0
            if vehicle.pump_times:
                self._pump_segment(vehicle, now)
                self.emit('ammo', vehicle.entity_id)
                self._emit_pump_gate(vehicle, now)
                self._emit_pump(vehicle, now)
                return
            vehicle.reload_kind = 'reload'
            vehicle.reload_total = self._magazine_duration(vehicle, 'reload')
            vehicle.reload_at = now+vehicle.reload_total
            self.emit('ammo',vehicle.entity_id)
            self.emit('reload',vehicle.entity_id,delay=vehicle.reload_total)
            return
        extra = vehicle.duplet_pending and vehicle.burst_remaining == 1
        origin,velocity,shell = self._launch(vehicle,shell,now,from_reserve=extra)
        vehicle.burst_remaining -= 1
        if vehicle.burst_remaining:
            delay = (uprising.DUPLET_DELAY if vehicle.duplet_pending and vehicle.burst_remaining == 1
                     else self._burst_duration(vehicle))
            vehicle.reload_kind = 'interval'
            vehicle.reload_at = now+delay
            vehicle.reload_total = delay
        else:
            vehicle.burst_shell,vehicle.burst_effect = None,0
            vehicle.duplet_pending = False
            if vehicle.pump_times:
                self._fire_pump(vehicle, weapon, now, shell, origin, velocity, shot_effect,
                                shooting_count=0)
                if pending := apply_burst_shell_switch(vehicle):
                    self.emit('shell_selected',vehicle.entity_id,shell=pending)
                    self.emit('ammo',vehicle.entity_id)
                    if not vehicle.clip and not vehicle.pump_at:
                        self._pump_segment(vehicle, now)
                        self._emit_pump_gate(vehicle, now)
                        self._emit_pump(vehicle, now)
                return
            vehicle.reload_kind = 'interval' if vehicle.clip else 'reload'
            delay = self._magazine_duration(vehicle, vehicle.reload_kind)
            vehicle.reload_at = now+delay
            vehicle.reload_total = delay
        # The initial event already started this group's muzzle/audio sequence.
        # Its remaining physical projectiles still each need their own tracer.
        self._emit_shot(vehicle, shell, origin, velocity, now, shot_effect, 0)
        if not vehicle.burst_pending and (pending := apply_burst_shell_switch(vehicle)):
            self.emit('shell_selected',vehicle.entity_id,shell=pending)
        self.emit('ammo',vehicle.entity_id)
        self.emit('reload',vehicle.entity_id,delay=delay)

    def _fire_pump(self, vehicle, weapon, now, shell, origin, velocity, shot_effect, *, shooting_count=1):
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
            switched = apply_queued_shell(vehicle)
            vehicle.reload_at = 0.
            self._pump_segment(vehicle,now)
        self._emit_shot(vehicle, shell, origin, velocity, now, shot_effect, shooting_count)
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
        # The pinned HUD can keep sending the original role's item ID after
        # respawn. Always resolve that slot to the server's current ability.
        role_input = (type(ident) is int and self.big_boss is not None
                      and vehicle.entity_id in self.big_boss.roles and ident in big_boss.ROLE_INPUT_IDS)
        if state is None and role_input:
            state = next((s for s in vehicle.consumables if s and s.spec in big_boss.ACTIVE_SPECS), None)
        if not self.started or self.finished_at or not player.attached or not player.loaded or not vehicle.alive:
            reason = 'Vehicle cannot use consumables now'
        elif (type(ident) is not int or state is None or extra and not (
                role_input and ident in big_boss.GRAVITON_INPUT_IDS
                or state.spec is mad_games.IRRADIATOR and len(extra) == 4
                or state.spec in uprising.TARGETED_ABILITIES)):
            reason = 'Consumable is not installed or extra arguments are unsupported'
        elif state.spec is mad_games.URANIUM_SHIELD:
            reason = 'Uranium Shield activates automatically'
        elif state.spec is uprising.RESPAWN_CLOAK:
            reason = 'Respawn cloak activates automatically'
        elif state.spec is big_boss.MEGASHOT or state.spec in big_boss.SHIELD_SPECS:
            reason = 'This ability activates on firing'
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
        if state.spec is mad_games.ACCELERATOR:
            return self._activate_mad_dash(vehicle, state, now)
        if state.spec is mad_games.IRRADIATOR:
            requested = int.from_bytes(extra, 'little') if extra else None
            return self._activate_mad_irradiator(vehicle, state, now, requested)
        if state.spec is uprising.TIME_JUMP:
            return self._activate_uprising_time_jump(vehicle, state, now)
        if state.spec is uprising.PLAY_DEAD:
            return self._activate_uprising_play_dead(vehicle, state, now)
        if state.spec in uprising.TARGETED_ABILITIES:
            try:
                requested = decode_uprising_target(ident, extra)
            except ValueError:
                self.emit('consumable_rejected', vehicle.entity_id, ident=ident,
                          reason='Invalid Uprising ability target', at=now)
                return False
            return self._activate_uprising_targeted(vehicle, state, now, requested)
        if state.spec is big_boss.BLAST_WAVE:
            return self._boss_blast(vehicle)
        if state.spec in big_boss.ACTIVE_SPECS:
            view = None
            if state.spec.ident in big_boss.GRAVITON_INPUT_IDS:
                try:
                    view = decode_graviton_aim(extra)
                except ValueError as error:
                    self.emit('consumable_rejected', vehicle.entity_id, ident=ident, reason=str(error), at=now)
                    return False
            return self._activate_boss_role_ability(vehicle, state, now, view)
        if not self._activate_consumable(vehicle, state, now):
            self.emit('consumable_rejected', vehicle.entity_id, ident=ident,
                      reason='Consumable is cooling down or has no live target', at=now)
            return False
        return True

    def _activate_uprising_play_dead(self, vehicle, ability, now):
        if not self._activate_consumable(vehicle, ability, now):
            return False
        self._stop_fire(vehicle, now, 'play_dead', fire_advanced=True)
        self._clear_uprising_mark(vehicle.entity_id)
        self._clear_uprising_curse(vehicle.entity_id)
        return True

    def _end_uprising_play_dead(self, vehicle, at):
        for slot in vehicle.consumables:
            if slot and slot.spec is uprising.PLAY_DEAD and slot.stop(at):
                self._consumables(vehicle, max(self.simulation_time, vehicle.consumable_clock, at))
                return True
        return False

    def _uprising_blocks_shell(self, target, shot, at):
        key = (shot, target.entity_id)
        if key in self.uprising.protected_shots:
            return True
        if self._end_uprising_play_dead(target, at):
            self.uprising.protected_shots.add(key)
            return True
        return False

    def _rewind_modules(self):
        return ((_ENGINE_EXTRA, self.engines), (_AMMO_RACK_EXTRA, self.ammo_racks),
                (_FUEL_EXTRA, self.fuels), (_GUN_EXTRA, self.guns))

    def _record_uprising_rewinds(self, now):
        if self.world is None:
            return
        for tank in self.vehicles.values():
            if (not tank.alive or not self._player_attached(tank)
                    or not any(slot and slot.spec is uprising.TIME_JUMP for slot in tank.consumables)):
                continue
            entity = tank.entity_id
            self.uprising.record(entity, uprising.RewindFrame(
                now, tank.pose, copy(self.world.motion[entity]), tank.speed, tank.health,
                tuple((part, state.shifted(0.)) for part, state in tank.tracks.items()),
                tuple((part, copy(states[entity])) for part, states in self._rewind_modules() if entity in states),
                tuple((index, person.injured) for index, person in self.crews.get(entity, {}).items()),
            ))

    def _activate_uprising_time_jump(self, tank, ability, now):
        entity = tank.entity_id
        frame = self.uprising.rewind_frame(entity, now)
        world = self.world
        if (not tank.alive or frame is None or world is None or now < ability.ready_at
                or not world.can_resume_pose(entity, frame.pose, grounded=frame.motion.grounded)):
            return False
        if not self._activate_consumable(tank, ability, now):
            return False
        world.relocate_training_body(entity, frame.pose, motion=copy(frame.motion))
        tank.pose, tank.speed = frame.pose, frame.speed
        self.motion_frame = None
        for states in (self._sandbox_flights, self._landing_airborne_since, self._landing_skids,
                       self._track_drive_motion, self._track_drive_yaw_rates, self._track_drive_track_rates,
                       self.fall_hp_carry):
            states.pop(entity, None)
        for part in tank.tracks:
            self.track_fall_peaks.pop((entity, part), None)
        hp = min(frame.health, tank.max_health)
        if hp > tank.health:
            self._heal_health(tank, tank, hp - tank.health)
        elif hp != tank.health:
            tank.health = hp
            self.emit('health', entity, entity, health=hp)
        self._stop_fire(tank, now, 'time_jump', fire_advanced=True)
        self._clear_uprising_mark(entity)
        self._clear_uprising_curse(entity)
        self._restore_uprising_rewind_modules(tank, frame, now)
        self.emit('orientation_rebased', entity, position=tank.pose.position,
                  yaw=tank.pose.yaw, pitch=tank.pose.pitch, roll=tank.pose.roll)
        return True

    def _restore_uprising_rewind_modules(self, tank, frame, now):
        entity = tank.entity_id
        previous_critical, previous_destroyed = map(set, self.module_state(tank))
        shift = now - frame.at
        tank.tracks = {part: state.shifted(shift) for part, state in frame.tracked}
        tank.track_assist_sources.clear()
        modules = dict(frame.modules)
        for part, states in self._rewind_modules():
            if part in modules:
                states[entity] = copy(modules[part])
        critical, destroyed = map(set, self.module_state(tank))
        for part, extra in (*_TRACK_EXTRAS.items(), *((key, key) for key in modules)):
            before = 1 if extra in previous_destroyed else 0 if extra in previous_critical else 19
            after = 1 if extra in destroyed else 0 if extra in critical else 19
            if before != after or extra in _TRACK_EXTRAS.values() and after == 1:
                self._module_changed(tank, part, after, now)
        for index, injured in frame.crew:
            person = self.crews[entity][index]
            if (person.injure() if injured else person.heal()):
                self._crew_changed(tank, person, 0 if injured else 19, now)
        self._consumables(tank, now)

    def _activate_uprising_targeted(self, vehicle, state, now, requested):
        ray = self._ability_ray(vehicle)
        entity, point = self.aim_hit(vehicle, ray=ray)
        target = self.vehicles.get(entity)
        if (target is None or not target.alive or target.team == vehicle.team
                or not self._player_attached(target) or mad_games.concealed(target, vehicle.team)
                or requested not in (None, entity) or uprising.active(target, uprising.PLAY_DEAD, now)):
            return False
        if state.spec is uprising.TORNADO:
            return self._activate_uprising_tornado(vehicle, state, target, now)
        curse = state.spec is uprising.CURSE
        effects = self.uprising.curses if curse else self.uprising.marks
        if curse and math.dist(ray[0], point) > uprising.CURSE_RANGE:
            return False
        if (effect := effects.get(entity)) and now < effect.end:
            return False
        if not self._activate_consumable(vehicle, state, now):
            return False
        effects[entity] = uprising.TargetAbility(vehicle.entity_id, vehicle.team, now, state.active_until)
        if curse:
            self.emit('uprising_curse_fx', entity, active=True)
        else:
            self.emit('uprising_mark_fx', entity, effects=self.uprising.vehicle_effects(entity))
            self._consumables(vehicle, now)
        return True

    def _activate_uprising_tornado(self, source, ability, target, now):
        entity = target.entity_id
        world = self.world
        if world is None or entity in world.dynamic_bodies or now < ability.ready_at:
            return False
        motion = world.motion[entity]
        current = target.pose
        x, y, z = current.position
        angle = math.radians(self.rng.uniform(120., 180.))
        if self.rng.getrandbits(1):
            angle = -angle
        for turn in (angle, -angle):
            yaw = wrap(current.yaw + turn)
            if motion.grounded:
                pose = world.training_placement(entity, x, z, yaw, stationary=True, reference=y)
            else:
                candidate = dataclasses.replace(current, yaw=yaw)
                pose = candidate if world.can_resume_pose(entity, candidate, grounded=False) else None
            if pose is not None:
                break
        else:
            return False
        if not self._activate_consumable(source, ability, now):
            return False
        stopped = dataclasses.replace(motion, velocity=(0., 0. if motion.grounded else motion.velocity[1], 0.))
        world.relocate_training_body(entity, pose, motion=stopped)
        target.speed = 0.
        target.pose = pose
        self.motion_frame = None
        self.emit('orientation_rebased', entity, position=pose.position,
                  yaw=pose.yaw, pitch=pose.pitch, roll=pose.roll)
        return True

    def _clear_uprising_mark(self, entity):
        if self.uprising.marks.pop(entity, None) is not None:
            self.emit('uprising_mark_fx', entity, effects=())

    def _clear_uprising_curse(self, entity):
        if self.uprising.curses.pop(entity, None) is not None:
            self.emit('uprising_curse_fx', entity, active=False)

    def _advance_uprising_targeted(self, now, before):
        state = self.uprising
        for entity, compact_id in state.advance_shot_upgrades(now):
            if (tank := self.vehicles.get(entity)) is not None and tank.alive and tank.account_id is not None:
                self.emit('uprising_shot_upgrade_ready', entity, compact_id=compact_id)
        if state.lightning_hits:
            for at, source, entity, generation, damage, arc, origin_entity, origin in sorted(state.lightning_hits):
                if at > now + 1e-9:
                    break
                target = self.vehicles.get(entity)
                if (source in self.vehicles and target is not None and target.generation == generation
                        and target.alive and not target.sandbox_god_mode and self._player_attached(target)):
                    x, y, z = target.pose.position
                    self.emit('uprising_lightning_fx', source, entity, arc=arc,
                              start_entity=origin_entity, start=origin, end=(x, y + 1., z))
                    self._damage_health(target, damage, source, now, shell_hit=False)
            state.lightning_hits[:] = (hit for hit in state.lightning_hits if hit[0] > now + 1e-9)
        for entity, mark in tuple(state.marks.items()):
            target = self.vehicles[entity]
            if (not target.alive or not self._player_attached(target)
                    or mad_games.concealed(target, mark.team) or now >= mark.end):
                self._clear_uprising_mark(entity)
        for entity, curse in tuple(state.curses.items()):
            target = self.vehicles[entity]
            if not target.alive or not self._player_attached(target) or now >= curse.end:
                self._clear_uprising_curse(entity)
                continue
            previous = before[entity].pose.position
            position = target.pose.position
            curse.distance += math.hypot(position[0] - previous[0], position[2] - previous[2])
            pulses, curse.distance = divmod(curse.distance, uprising.CURSE_DISTANCE)
            if pulses and not target.sandbox_god_mode:
                damage = int(pulses * target.max_health * uprising.CURSE_DAMAGE + .5)
                self._damage_health(target, damage, curse.source, now, shell_hit=False)

    def _install_uprising_ability(self, vehicle):
        limit = sandbox.reload_limit(vehicle)
        uprising.install_ability(vehicle, self._uprising_game, cooldown_limit=limit)

    def _install_mad_games_ability(self, vehicle):
        game = self._mad_games_game
        limit = sandbox.reload_limit(vehicle)
        mad_games.install_ability(vehicle, vehicle_class(game, vehicle.definition), game, cooldown_limit=limit)
        vehicle.mad_unspotted = True

    def _activate_mad_irradiator(self, vehicle, state, now, requested):
        entity, _ = self.aim_hit(vehicle, ray=self._ability_ray(vehicle))
        target = self.vehicles.get(entity)
        if (target is None or not target.alive or target.team == vehicle.team or mad_games.concealed(target, vehicle.team)
                or not self._player_attached(target) or requested not in (None, entity)):
            return False
        if (mark := self.mad_irradiated.get(entity)) and now < mark.end:
            return False
        if not self._activate_consumable(vehicle, state, now):
            return False
        mark = mad_games.Irradiated(vehicle.team, now, state.active_until)
        self.mad_irradiated[entity] = mark
        self._set_mad_spotted(vehicle, True, now)
        self._publish_state_modifiers(target)
        self.emit('mad_vehicle_effects', entity, effects=mad_games.irradiation_effects(mark))
        return True

    def _clear_mad_irradiator(self, vehicle):
        if self.mad_irradiated.pop(vehicle.entity_id, None) is not None:
            self._publish_state_modifiers(vehicle)
            self.emit('mad_vehicle_effects', vehicle.entity_id, effects=())

    def _mad_outgoing_factor(self, shooter, target, at):
        factor = mad_games.discharge_factor(shooter, at)
        mark = self.mad_irradiated.get(target.entity_id)
        if mark is not None and mark.team == shooter.team and mark.start <= at < mark.end:
            factor *= mad_games.IRRADIATED_DAMAGE
        return factor

    def _update_mad_games(self, now):
        living = tuple(vehicle for vehicle in self.vehicles.values()
                       if vehicle.alive and self._player_attached(vehicle))
        for vehicle in living:
            if vehicle.mad_revenge:
                self._expire_mad_revenge(vehicle, now)
            if vehicle.mad_passive != mad_games.COORDINATOR:
                continue
            allies = min(mad_games.COORDINATOR_LIMIT, sum(
                other is not vehicle and other.team == vehicle.team
                and math.dist(vehicle.pose.position, other.pose.position) <= mad_games.COORDINATOR_RADIUS
                for other in living))
            if allies != vehicle.mad_coordinator_allies:
                vehicle.mad_coordinator_allies = allies
                vehicle.aiming_time_factor = 1. / (1. + allies * mad_games.COORDINATOR_BONUS)
                self._publish_state_modifiers(vehicle)
                self.emit('targeting', vehicle.entity_id, yaw_rate=vehicle.gun.yaw_rate,
                          pitch_rate=vehicle.gun.pitch_rate)
        self._update_mad_spotting(living, now)
        for entity, mark in tuple(self.mad_irradiated.items()):
            target = self.vehicles[entity]
            if (not target.alive or mad_games.concealed(target, mark.team)
                    or not self._player_attached(target) or now >= mark.end):
                self._clear_mad_irradiator(target)

    def _set_mad_spotted(self, vehicle, spotted, now=None):
        was_hidden = vehicle.mad_cloaked or vehicle.mad_unspotted
        vehicle.mad_unspotted = not spotted
        self._publish_mad_visibility(vehicle, was_hidden, self.simulation_time if now is None else now)

    def _publish_mad_visibility(self, vehicle, was_hidden, now):
        hidden = vehicle.mad_cloaked or vehicle.mad_unspotted
        if hidden != was_hidden:
            if hidden:
                self._lose_target(vehicle.entity_id, include_owner=False)
            self.emit('mad_cloak', vehicle.entity_id, enabled=hidden, pose=vehicle.pose)
        ghost = self.uprising is not None and self.uprising.has(vehicle.entity_id, uprising.GHOST)
        if not self.team_spotting:
            return
        if hidden or not vehicle.alive:
            vehicle.mad_observation_due = None
            if vehicle.mad_observation_warning:
                vehicle.mad_observation_warning = False
                self.emit('mad_observation_warning', vehicle.entity_id, observed=False)
        elif not vehicle.mad_observation_warning:
            if vehicle.mad_observation_due is None:
                delay = mad_games.OBSERVATION_WARNING_DELAY
                if ghost:
                    delay = uprising.GHOST_WARNING_DELAY
                elif vehicle.mad_passive == mad_games.SABOTEUR:
                    delay = mad_games.SABOTEUR_WARNING_DELAY
                vehicle.mad_observation_due = now + delay
            if now >= vehicle.mad_observation_due:
                vehicle.mad_observation_due = None
                vehicle.mad_observation_warning = True
                self.emit('mad_observation_warning', vehicle.entity_id, observed=True)

    def _update_mad_spotting(self, living, now, targets=None, *, force=False):
        targets = living if targets is None else targets
        spotted = {}
        candidates, starts, ends = [], [], []
        proximity = mad_games.PROXIMITY_RANGE
        concealment = {target.entity_id: self._spotting_concealment(target, now) for target in targets}
        short_retention = {t.entity_id for t in targets if t.mad_passive == mad_games.SABOTEUR
                           or self.uprising is not None and self.uprising.has(t.entity_id, uprising.GHOST)}
        previous_checks = self._spotting_checks
        checks = {} if not force else previous_checks
        for observer in living:
            x, y, z = observer.pose.position
            sight = mad_games.view_range(observer)
            optics = getattr(observer.definition, 'spotting', None)
            if optics is not None and 114 in observer.definition.optional_devices:
                sight *= 1. + optics.optics_bonus
            for target in targets:
                if target.team == observer.team or target.mad_cloaked or target.entity_id in spotted:
                    continue
                distance = math.dist(observer.pose.position, target.pose.position)
                effective_sight = min(445., sight - (sight - proximity) * concealment[target.entity_id])
                if distance <= proximity or self.world is None and distance <= effective_sight:
                    spotted[target.entity_id] = observer
                elif distance <= effective_sight:
                    pair = (observer.entity_id, observer.generation, target.entity_id, target.generation)
                    last, previous_shot = previous_checks.get(pair, (-math.inf, None))
                    fired = target.last_shot_at is not None and target.last_shot_at != previous_shot
                    stopped = (not target.spotting_moving and target.spotting_last_motion_at is not None
                               and target.spotting_last_motion_at >= last)
                    interval = (.1 if target.entity_id in short_retention else
                                .5 if distance <= 150. else 1. if distance <= 270. else 2.)
                    if not (force or fired or stopped or now + 1e-9 >= last + interval):
                        checks[pair] = last, previous_shot
                        continue
                    checks[pair] = now, target.last_shot_at
                    tx, ty, tz = target.pose.position
                    candidates.append((target.entity_id, observer))
                    starts.append((x, y + 1.4, z))
                    ends.append((tx, ty + 1.1, tz))
        self._spotting_checks = checks
        if candidates:
            hits = self.world._visibility_rays(starts, ends)
            for (entity, observer), hit in zip(candidates, hits):
                if hit[0] < 0 or hit[2] >= .999:
                    spotted.setdefault(entity, observer)
        for vehicle in targets:
            if vehicle.mad_cloaked:
                vehicle.mad_spotted_until = 0.
                self._set_mad_spotted(vehicle, False, now)
                continue
            if (observer := spotted.get(vehicle.entity_id)) is not None:
                retention = mad_games.SPOT_RETENTION
                if vehicle.entity_id in short_retention:
                    retention *= (mad_games.SABOTEUR_RETENTION_FACTOR if vehicle.mad_passive == mad_games.SABOTEUR
                                  else uprising.GHOST_RETENTION_FACTOR)
                vehicle.mad_spotted_until = now + retention
                if self.uprising is not None and self.uprising.first_spot(vehicle.entity_id):
                    self._award_elixir(observer, uprising.ELIXIR_PER_SPOT)
            visible = (now < vehicle.mad_spotted_until
                       or mad_games.active(vehicle, mad_games.IRRADIATOR, now)
                       or self.uprising is not None and self.uprising.exposes(vehicle.entity_id, now))
            self._set_mad_spotted(vehicle, visible, now)

    def _spotting_concealment(self, vehicle, now):
        profile = getattr(vehicle.definition, 'spotting', None)
        if profile is None:
            return 0.
        concealment = profile.moving if vehicle.spotting_moving else profile.still
        if 115 in vehicle.definition.optional_devices:
            still_since = vehicle.spotting_last_motion_at
            if still_since is None:
                still_since = self.started if self.started is not None else now
            stationary_net = (not vehicle.spotting_moving and
                              now - still_since >= profile.net_still_time)
            concealment += profile.net_bonus * (profile.net_still_factor if stationary_net else 1.)
        if vehicle.last_shot_at is not None and 0. <= now - vehicle.last_shot_at < 5.:
            concealment *= profile.at_shot
        return max(0., min(1., concealment))

    def _update_team_spotting(self, now):
        living = tuple(v for v in self.vehicles.values() if v.alive and self._player_attached(v))
        targets = tuple(v for v in living if v.entity_id not in self.practice_slots) if self.practice else living
        observers = tuple(v for v in living if v.account_id not in self.hangar_players)
        if self.practice_gunners:
            observers += tuple(self.vehicles[entity] for entity in self.practice_gunners
                               if self.vehicles[entity].alive)
        self._update_mad_spotting(observers, now, targets)

    def _expire_mad_revenge(self, vehicle, now):
        before = len(vehicle.mad_revenge)
        for key, end in tuple(vehicle.mad_revenge.items()):
            entity, generation = key
            target = self.vehicles[entity]
            if now >= end or not target.alive or target.generation != generation:
                del vehicle.mad_revenge[key]
        if len(vehicle.mad_revenge) != before:
            self._publish_mad_revenge(vehicle)

    def _publish_mad_revenge(self, vehicle):
        self._publish_state_modifiers(vehicle)
        self.emit('mad_revenge_targets', vehicle.entity_id)

    def _activate_mad_dash(self, vehicle, state, now):
        entity = vehicle.entity_id
        engine = self.engines.get(entity)
        if (self.world is None
                or any(track.critical or track.broken for track in vehicle.tracks.values())
                or engine is not None and (engine.critical or engine.destroyed)):
            return False
        flags = self._active_control_flags(vehicle, now)
        direction = -1 if flags & 2 and not flags & 1 else 1
        impulse = direction * mad_games.DASH_VELOCITY_BOOST
        yaw = self.world.poses[entity].yaw
        dx, dz = math.sin(yaw) * impulse, math.cos(yaw) * impulse
        airborne = self._vehicle_airborne(vehicle)
        if airborne:
            if not self.started or self.finished_at or not vehicle.alive or state.state_at(now).state != READY:
                return False
            if not self.world.add_airborne_velocity(entity, (dx, 0., dz)):
                return False
        if not self._activate_consumable(vehicle, state, now):
            return False
        if not airborne:
            motion = self.world.motion[entity]
            vx, vy, vz = motion.velocity
            motion.velocity = vx + dx, vy, vz + dz
            vehicle.speed += impulse
            ground = self.world.ground_motion.get(entity)
            for drive in (ground if ground and ground.active else None,
                          self._landing_skids.get(entity), self._track_drive_motion.get(entity)):
                if drive is not None:
                    vx, vz = drive.velocity
                    drive.velocity = vx + dx, vz + dz
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
        if state.spec is mad_games.URANIUM_SHIELD:
            return False
        cost = (max(1, round(vehicle.max_health * mad_games.DISCHARGE_HP_COST))
                if state.spec is mad_games.DISCHARGE else 0)
        if vehicle.health <= cost or not state.activate(at):
            return False
        if cost:
            vehicle.health -= cost
            self.emit('health', vehicle.entity_id, vehicle.entity_id, health=vehicle.health)
            self.emit('provision_effect', vehicle.entity_id, compact_id=state.spec.compact_id,
                      allies_only=mad_games.concealed(vehicle, 3 - vehicle.team))
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

    def _end_vehicle_cloak(self, vehicle, at):
        if vehicle.mad_cloaked or self.uprising is not None and any(
                uprising.active(vehicle, spec, at) for spec in uprising.CLOAKS):
            at = max(at, vehicle.consumable_clock)
            for state in vehicle.consumables:
                if state and (state.spec in uprising.CLOAKS or state.spec is mad_games.CHAMELEON):
                    state.stop(at)
            self._consumables(vehicle, at)

    def _consumables(self, vehicle, now, *, sync_movable=True, refresh_cooldowns=False):
        # Fixed steps cross expiry by at most STEP. Split the rate change at
        # the exact simulation timestamp, preserving completed reload work.
        # The interval factor only rescales an intershot (or its ready total);
        # a magazine reload keeps its timer through NewAutoloader changes.
        expiries = sorted({s.active_until for s in vehicle.consumables if s and s.uses and
                           vehicle.consumable_clock < s.active_until <= now})
        # Pump refills complete up to each split first; the running refill then
        # rescales by the reload rate only, never by the intershot factor.
        boss = self._is_boss(vehicle)
        role_rate = big_boss.RELOAD_RATE if boss else 1.
        if self.big_boss is not None and self.big_boss.roles.get(vehicle.entity_id) == big_boss.CATALYST:
            role_rate = 1. / big_boss.CATALYST_RELOAD[self.big_boss.levels[vehicle.entity_id]]
        if vehicle.mad_coordinator_allies:
            role_rate *= 1. + vehicle.mad_coordinator_allies * mad_games.COORDINATOR_BONUS
        engine_bonus, speed_bonus = 1., 0.
        if self.uprising is not None:
            role_rate /= self.uprising.upgrade(vehicle.entity_id, 'reload').reload
            mobility = self.uprising.upgrade(vehicle.entity_id, 'engine')
            engine_bonus = mobility.engine
            speed_bonus = mobility.speed_kmh
        for at in (*expiries, now):
            self._advance_pump(vehicle, at, now)
            manual_cloak = self.uprising is not None and uprising.active(vehicle, uprising.MOONWALK, at)
            views = [s.state_at(at) for s in vehicle.consumables
                     if s and not (manual_cloak and s.spec is uprising.RESPAWN_CLOAK)]
            rate = math.prod((v.reload_speed_factor for v in views),
                             start=role_rate)
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
            if vehicle.duplet_pending and vehicle.burst_remaining == 1:
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
            role_mobility = self.big_boss.mobility(vehicle.entity_id) if self.big_boss is not None else 1.
            vehicle.engine_factor = math.prod((v.engine_factor for v in views),
                                             start=big_boss.ENGINE_POWER if boss else role_mobility * engine_bonus) * (
                engine.power_factor if engine is not None else 1.)
            vehicle.rotation_factor = math.prod(v.rotation_factor for v in views)
            vehicle.forward_speed_bias = (speed_bonus + sum(v.forward_bias_kmh for v in views))/3.6
            vehicle.reverse_speed_bias = sum(v.reverse_bias_kmh for v in views)/3.6
        targeting_changed = vehicle.gun.set_factors(self._crew_role_factor(vehicle, 'gunner')
                                                   * sandbox.stat_bonus(vehicle, sandbox.STAT_TURRET), 1.)
        aim_factor = math.prod(v.aiming_time_factor for v in views)
        dispersion_factor = math.prod(v.dispersion_factor for v in views)
        targeting_changed |= (aim_factor, dispersion_factor) != (vehicle.consumable_aiming_time_factor,
                                                                vehicle.consumable_dispersion_factor)
        vehicle.consumable_aiming_time_factor = aim_factor
        vehicle.consumable_dispersion_factor = dispersion_factor
        if self.uprising is not None:
            duplet = self.uprising.has(vehicle.entity_id, uprising.DUPLET)
            aim_factor = uprising.DUPLET_AIM_FACTOR if duplet else 1.
            dispersion_factor = uprising.DUPLET_DISPERSION_FACTOR if duplet else 1.
            if uprising.active(vehicle, uprising.BERSERK, now):
                dispersion_factor *= uprising.BERSERK_DISPERSION_FACTOR
            targeting_changed |= (aim_factor, dispersion_factor) != (vehicle.aiming_time_factor,
                                                                    vehicle.shot_dispersion_factor)
            vehicle.aiming_time_factor = aim_factor
            vehicle.shot_dispersion_factor = dispersion_factor
        if targeting_changed:
            self.emit('targeting',vehicle.entity_id,yaw_rate=vehicle.gun.yaw_rate,
                      pitch_rate=vehicle.gun.pitch_rate)
        if sync_movable:
            self._sync_movable_armor(vehicle, now)
        vehicle.consumable_clock = now
        if self.uprising is not None:
            was_cloaked = vehicle.mad_cloaked
            was_hidden = was_cloaked or vehicle.mad_unspotted
            exposed = self.uprising.exposes(vehicle.entity_id, now)
            cloak = uprising.active_cloak(vehicle, now)
            vehicle.mad_cloaked = cloak is not None and not exposed
            if exposed:
                vehicle.mad_unspotted = False
            if vehicle.mad_cloaked != was_cloaked:
                if cloak is None:
                    cloak = next((spec for spec in reversed(uprising.CLOAKS)
                                  if vehicle.consumable_status.get(spec.ident) == ACTIVE),
                                 uprising.RESPAWN_CLOAK)
                self.emit('uprising_cloak_fx', vehicle.entity_id, compact_id=cloak.compact_id,
                          active=vehicle.mad_cloaked, at=now)
            self._publish_mad_visibility(vehicle, was_hidden, now)
        for state in vehicle.consumables:
            if state is None:
                continue
            view = state.state_at(now)
            previous = vehicle.consumable_status.get(state.spec.ident)
            if state.spec is uprising.PLAY_DEAD and (previous == ACTIVE) != (view.state == ACTIVE):
                self.emit('uprising_play_dead_fx', vehicle.entity_id, active=view.state == ACTIVE, health=vehicle.health)
            if state.spec is uprising.RESPAWN_CLOAK:
                vehicle.consumable_status[state.spec.ident] = view.state
                continue
            if previous != view.state or (refresh_cooldowns and view.state == COOLDOWN):
                vehicle.consumable_status[state.spec.ident] = view.state
                if state.spec is mad_games.CHAMELEON and vehicle.mad_cloaked != (view.state == ACTIVE):
                    was_hidden = vehicle.mad_cloaked or vehicle.mad_unspotted
                    vehicle.mad_cloaked = view.state == ACTIVE
                    if vehicle.mad_cloaked:
                        self.emit('mad_chameleon_fx', vehicle.entity_id, active=True, spotted=not was_hidden)
                        vehicle.mad_unspotted = True
                        vehicle.mad_spotted_until = 0.
                        self._clear_mad_irradiator(vehicle)
                    self._publish_mad_visibility(vehicle, was_hidden, now)
                    if not vehicle.mad_cloaked and vehicle.alive:
                        observers = tuple(v for v in self.vehicles.values()
                                          if v.alive and self._player_attached(v))
                        self._update_mad_spotting(observers, now, targets=(vehicle,), force=True)
                        self.emit('mad_chameleon_fx', vehicle.entity_id, active=False,
                                  spotted=not vehicle.mad_unspotted)
                if (ACTIVE in (previous, view.state)
                        and (state.spec in big_boss.ROLE_ACTIVE[big_boss.CATALYST]
                             or state.spec is mad_games.REFLECTOR)):
                    self._publish_state_modifiers(vehicle)
                    if state.spec is mad_games.REFLECTOR:
                        self.emit('mad_reflector_fx', vehicle.entity_id, active=view.state == ACTIVE,
                                  spotted=not mad_games.concealed(vehicle, 3 - vehicle.team))
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
        snapshot = self._movable_armor_snapshot_at(vehicle, at)
        # A full interval already has both motion endpoints. The controller
        # still uses the interval's armor state, including at an expiry.
        if fraction == 0:
            state = before
        elif fraction == 1:
            state = after
        else:
            state = interpolate(before, after, fraction)
        armor_state = snapshot if snapshot is not None else (
            before.armor_state if before.armor_state == after.armor_state else None)
        return state if state.armor_state == armor_state else dataclasses.replace(
            state, armor_state=armor_state)

    def _armor_frame_states(self, before, after, fraction, at):
        suspended = self._suspended_entities()
        return {entity:self._armor_frame_state(self.vehicles[entity], before[entity], after[entity],
                                                fraction, at)
                for entity in before if entity not in suspended}

    def equipment_state(self, vehicle, now=None):
        """Immutable start/stop snapshot, independent of network serialization."""
        now = self.simulation_time if now is None else now
        # With fast cooldowns a running boost may go straight to READY.
        # The client's public indicator needs the item ID to remove its icon.
        return tuple((s.spec.ident, view.state, s.activated_at, s.active_until-s.activated_at)
                     for s in vehicle.consumables
                     if s and s.spec not in (GRAVITY_JUMP_SPEC, big_boss.BLAST_WAVE, big_boss.MEGASHOT)
                     and s.spec not in big_boss.ROLE_SPECS
                     and s.spec is not uprising.RESPAWN_CLOAK and s.spec not in uprising.ACTIVE_ABILITIES
                     and s.spec.action == 'boost'
                     and ((view := s.state_at(now)).state in (ACTIVE, COOLDOWN) or s.uses))

    def _stop_consumables(self, vehicle):
        # A dead or finished vehicle has no future weapon state.  In
        # particular, do this before recomputing stopped consumable effects so
        # their expiry cannot rescale, complete, or announce a stale reload.
        vehicle.reload_at = 0.
        vehicle.next_shell = 0
        vehicle.burst_shell,vehicle.burst_effect,vehicle.burst_remaining = None,0,0
        vehicle.burst_switch_shell = 0
        vehicle.duplet_pending = False
        vehicle.reload_kind = 'reload'
        self._stop_pump(vehicle)
        for state in vehicle.consumables:
            if state:
                state.stop(self.simulation_time)
        self._consumables(vehicle, self.simulation_time)

    def finish(self, winner=0, reason='timeout', now=None):
        if self.finished_at:
            return
        timeline = getattr(self, 'time_stop', None)
        if timeline is not None:
            if timeline.defer_finish(winner, reason, now):
                return
            timeline.abort(battle_ended=True)
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
            if vehicle.alive:
                vehicle.speed = 0.
            vehicle.engine_mode = (1,0) if vehicle.alive else (0,0)
            self._stop_fire(vehicle, at, 'battle_end')
            self._stop_consumables(vehicle)
        for projectile in self.projectiles.values():
            self.emit('shot_end',projectile.shooter,shot=projectile.shot,point=projectile.position,
                      **({'guided':True} if projectile.shell.missile is not None else {}))
        self.projectiles.clear()
        if self.uprising is not None:
            self.uprising.protected_shots.clear()
            self.uprising.history.clear()
            for entity in tuple(self.uprising.marks):
                self._clear_uprising_mark(entity)
            for entity in tuple(self.uprising.curses):
                self._clear_uprising_curse(entity)
        for entity in tuple(self.mad_irradiated):
            self._clear_mad_irradiator(self.vehicles[entity])
        if self.big_boss is not None:
            for entity in tuple(self.big_boss.heal_waves):
                del self.big_boss.heal_waves[entity]
                self._publish_state_modifiers(self.vehicles[entity])
            effect_targets = self.big_boss.shields.keys() | self.big_boss.poison.keys()
            self.big_boss.shields.clear()
            self.big_boss.poison.clear()
            for entity in sorted(effect_targets):
                self._emit_boss_vehicle_effects(self.vehicles[entity])
        if self.big_boss is not None and self.big_boss.traps:
            self.big_boss.traps.clear()
            self.emit('gravitrap_expired', traps=())
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
        self.cancel_time_stop(account_id)
        player.attached = False
        self.hangar_players.discard(account_id)
        player.departure_reason = reason
        self._sandbox_inputs.pop(account_id, None)
        if self._recon_point_probe is not None:
            self._recon_point_probe['points'].pop(account_id, None)
            self._recon_point_probe['next'].pop(account_id, None)
        vehicle = self.vehicle_for(account_id)
        vehicle.sandbox_god_mode = False
        vehicle.sandbox_boost = False
        vehicle.sandbox_boost_strength = None
        if vehicle.alive and not self.finished_at:
            self._set_fast_reload(vehicle, False)
        vehicle.sandbox_fast_reload = False
        vehicle.sandbox_reload_override = None
        self._clear_sandbox_stats(vehicle)
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
                self.vehicles[p.vehicle_id].damage,self.finish_reason,
                None if p.account_id in getattr(self, 'training_swappers', ()) else p.inventory_id,
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
        target = self.vehicles.get(event.target)
        heals_ally = shell is not None and (shell.healing_level is not None or
            shooter.mad_passive == mad_games.REGENERATOR and shell.kind in mad_games.HEALING_SHELLS)
        if (heals_ally and event.result.hit is not None
                and target is not None and target.alive and target.team == shooter.team
                and target is not shooter):
            self._apply_impact(event, event.result, event.target, direct=True, shell=shell, fire_at=fire_at)
            return
        if target is not None and target.sandbox_god_mode and target.team != shooter.team and event.result.hit is not None:
            event = dataclasses.replace(event, result=dataclasses.replace(
                event.result, outcome='not_penetrated', damage=0))
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
                suspended = self._suspended_entities()
                for target in self.vehicles.values():
                    if not target.alive or target.team==shooter.team or target.entity_id in suspended:
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
                self._apply_uprising_inferno(shooter, event, fire_at)
                return
        self._apply_impact(event,event.result,event.target,direct=True,shell=shell,
                           detonate_ammo_rack=detonate_ammo_rack,fire_at=fire_at)
        self._apply_uprising_inferno(shooter, event, fire_at)

    def _apply_uprising_inferno(self, shooter, event, fire_at):
        if self.uprising is None or not shooter.alive or not (event.collision or event.result.hit):
            return
        limit = sandbox.reload_limit(shooter)
        result = self.uprising.trigger_shot_upgrade(shooter.entity_id, 'inferno', event.at, limit)
        if result is None:
            return
        upgrade, cooldown = result
        damage = int(shooter.health * upgrade.damage_factors[0] + .5)
        now = max(self.module_clock, event.at if fire_at is None else fire_at)
        self.emit('uprising_shot_upgrade', shooter.entity_id, point=event.point,
                  compact_id=upgrade.compact_id, cooldown=cooldown)
        for target in self.vehicles.values():
            if (target.alive and target.team != shooter.team and not target.sandbox_god_mode
                    and math.dist(target.pose.position, event.point) <= upgrade.radius):
                self._damage_health(target, damage, shooter.entity_id, now, shell_hit=False)

    def _queue_uprising_lightning(self, shooter, primary, shell, upgrade, cooldown, event, now):
        suspended = self._suspended_entities()
        nearby = []
        for target in self.vehicles.values():
            if (target is primary or target.team != primary.team or not target.alive
                    or target.sandbox_god_mode or target.entity_id in suspended):
                continue
            distance = math.dist(target.pose.position, primary.pose.position)
            if distance <= upgrade.radius:
                nearby.append((distance, target.entity_id, target.generation))
        hops = sorted(nearby)[:len(upgrade.damage_factors) - 1]
        self.emit('uprising_shot_upgrade', shooter.entity_id, primary.entity_id,
                  compact_id=upgrade.compact_id, cooldown=cooldown,
                  targets=tuple(entity for _, entity, _ in hops))
        origin_entity, origin = primary.entity_id, event.point
        for i, ((_, entity, generation), factor) in enumerate(zip(hops, upgrade.damage_factors[1:]), 1):
            # A chain segment is a separate native tracer with no shell collision.
            arc = 0x50000000 | ((event.shot & 0x03ffffff) << 2) | i
            self.uprising.lightning_hits.append((now + i * uprising.CHAIN_HOP_SECONDS,
                shooter.entity_id, entity, generation, int(shell.damage * factor + .5),
                arc, origin_entity, origin))
            x, y, z = self.vehicles[entity].pose.position
            origin_entity, origin = entity, (x, y + 1., z)

    def _apply_impact(self, event, result, target_id, *, direct, shell=None,
                      detonate_ammo_rack=False, fire_at=None, hit_state=None):
        shooter = self.vehicles[event.shooter]
        target = self.vehicles.get(target_id)
        enemy = target is not None and target.team!=shooter.team
        # Fixed-step callers bound projectile floating-point drift to their
        # containing grid interval. Standalone callers retain event time.
        fire_at = max(self.module_clock, event.at if fire_at is None else fire_at)
        if (direct and target is not None and target.alive and not enemy and target is not shooter
                and shell is not None and shell.healing_level is not None and result.hit is not None):
            self._boss_healing_shot(shooter, target, shell, fire_at)
            self.emit('shot_end', event.shooter, shot=event.shot, point=event.point,
                      guided=shell.missile is not None)
            return
        if (direct and target is not None and target.alive and not enemy and target is not shooter
                and shell is not None and shell.kind in mad_games.HEALING_SHELLS
                and shooter.mad_passive == mad_games.REGENERATOR and result.hit is not None):
            self._mad_heal(shooter, target, shell.damage * mad_games.ALLY_HEAL)
            self.emit('shot_end', event.shooter, shot=event.shot, point=event.point,
                      guided=shell.missile is not None)
            return
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
        if enemy and shell is not None and self.big_boss is not None:
            factor *= self.big_boss.damage_factor(target.entity_id)
        if enemy and shell is not None and self.uprising is not None:
            factor *= self.uprising.mark_damage_factor(shooter, target, event.at)
            if uprising.active(target, uprising.BERSERK, event.at):
                factor *= uprising.BERSERK_INCOMING
        anti_he_factor = 1.
        if enemy and he:
            anti_he_factor = getattr(target.definition,'he_damage_factor',1.)
            factor *= anti_he_factor
        elif (not detonate_ammo_rack and enemy and shell is not None
              and shell.kind in SHIELDED_SHELL_KINDS):
            factor *= self._shield_factor(target,event.at)
        raw = result.damage
        reflected = 0.
        if self.mad_games_mode and enemy and target.alive:
            factor *= self._mad_outgoing_factor(shooter, target, event.at)
            if direct and (result.hit is not None or raw > 0):
                expiry = shooter.mad_revenge.pop((target.entity_id, target.generation), None)
                if expiry is not None:
                    if event.at < expiry:
                        factor *= mad_games.REVENGE_DAMAGE
                    self._publish_mad_revenge(shooter)
            if (shell is not None and (raw > 0 or direct and result.hit is not None)
                    and mad_games.active(target, mad_games.REFLECTOR, event.at)):
                reflected = shell.damage * mad_games.REFLECTED_DAMAGE
        mitigated = factor!=1 and raw>0 and target.alive
        if mitigated:
            result = dataclasses.replace(result,damage=int(factor*raw+.5))
        if (enemy and self.uprising is not None and shell is not None and target.alive
                and (raw > 0 or direct and result.hit is not None)
                and self._uprising_blocks_shell(target, event.shot, event.at)):
            result = dataclasses.replace(result, outcome='not_penetrated', damage=0)
            detonate_ammo_rack = False
        if enemy and target.sandbox_god_mode:
            result = dataclasses.replace(result, outcome='not_penetrated', damage=0)
            detonate_ammo_rack = False
        if (self.uprising is not None and direct and enemy and target.alive and shooter.alive
                and shell is not None and result.outcome == 'penetrated' and result.damage > 0):
            limit = sandbox.reload_limit(shooter)
            triggered = self.uprising.trigger_shot_upgrade(shooter.entity_id, 'lightning', event.at, limit)
            if triggered is not None:
                upgrade, cooldown = triggered
                self._queue_uprising_lightning(shooter, target, shell, upgrade, cooldown, event, fire_at)
                bonus = int(shell.damage * upgrade.damage_factors[0] * factor + .5)
                result = dataclasses.replace(result, damage=result.damage + bonus)
        damage = (target.health if detonate_ammo_rack and enemy and target.alive
                  else result.damage if enemy else 0)
        if self.mad_games_mode and enemy and target.alive and not detonate_ammo_rack:
            shielded = self._mad_shield_damage(target, damage, fire_at)
            if shielded != damage:
                result = dataclasses.replace(result, damage=shielded)
                damage = shielded
        if enemy:
            damage = min(target.health, damage)
        if damage > 0 and self._consume_boss_shield(target, fire_at):
            damage = 0
            result = dataclasses.replace(result, damage=0, outcome='not_penetrated')
        blocked = (int(shell.damage+.5) if direct and enemy and target.alive
                   and result.hit is not None and not result.hit.external and shell is not None
                   and shell.kind != 'HIGH_EXPLOSIVE'
                   and result.outcome in ('ricochet','not_penetrated') else 0)
        track_blocked = 0
        if (direct and enemy and target.alive and not damage and shell is not None
                and result.hit is not None and result.hit.external
                and result.hit.part in _TRACK_EXTRAS
                and result.outcome in ('external', 'not_penetrated')):
            track_blocked = int(shell.damage + .5)
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
                  **({'track_blocked_damage':track_blocked} if track_blocked else {}),
                  **({'hit_effect':hit_effect} if hit_effect is not None else {}),
                  **({'reflector_hit':True} if reflected and hit_effect is not None else {}),
                  **({'guided':True} if direct and shell and shell.missile is not None else {}))
        if mitigated and damage and he and anti_he_factor != 1:
            # Semantic FX only: no timer or invulnerability; wire framing is root-owned.
            self.emit('provision_effect',target.entity_id,event.shooter,shot=event.shot,ident=ANTI_HE_PROVISION,
                      compact_id=(ANTI_HE_PROVISION<<8)|0xfd,duration=target.definition.he_effect_duration,
                      at=event.at)
        if result.hit is not None and target and target.alive and enemy:
            if self.gravity_mode and direct and shell is not None and not target.sandbox_god_mode:
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
            damage = self._damage_health(target, damage, event.shooter, fire_at, shell_hit=True,
                                         hit_effect=hit_effect, reflector_hit=bool(reflected and hit_effect))
            if target.alive and shell is not None and shell.poison_level is not None:
                self._boss_poison(shooter, target, shell.poison_level, fire_at)
            if direct and target.alive and shooter.alive and target.mad_passive == mad_games.TOXIC_STARE:
                key = (shooter.entity_id, shooter.generation)
                already_marked = key in target.mad_revenge
                target.mad_revenge[key] = fire_at + mad_games.REVENGE_DURATION
                if not already_marked:
                    self._publish_mad_revenge(target)
        if reflected:
            self._apply_mad_reflected_damage(target, shooter,
                damage * mad_games.REFLECTED_DAMAGE if damage else reflected, fire_at)
        if (self.mad_games_mode and enemy and direct and shell is not None
                and target.entity_id in self.training_bots and target.alive
                and (damage or result.hit is not None and not result.hit.external)):
            # React to the completed hit. Only later projectiles can be
            # reflected, even if the first hit arrived during our reload.
            state = next((slot for slot in target.consumables
                          if slot and slot.spec is mad_games.REFLECTOR), None)
            if state is not None:
                self._activate_consumable(target, state, fire_at)

    def _apply_mad_reflected_damage(self, reflector, attacker, amount, at):
        if attacker.alive and not attacker.sandbox_god_mode:
            self._damage_health(attacker, int(amount+.5), reflector.entity_id, at,
                                shell_hit=False, reflected=True)

    def _damage_health(self, target, amount, source, at, *, shell_hit, hit_effect=None,
                       kinetic=False, manual_respawn=False, reflected=False, reflector_hit=False,
                       ram_blast_energy=0.):
        """True clamped HP damage, shared death/stats, no invented impact RPC."""
        if not target.alive or target.entity_id in self._suspended_entities():
            return 0
        if kinetic and self.big_boss is not None:
            amount = int(amount * self.big_boss.damage_factor(target.entity_id) + .5)
        shooter = self.vehicles[source] if source is not None else None
        enemy = shooter is not None and shooter.team != target.team
        if self.mad_games_mode and enemy and not (shell_hit or kinetic or reflected):
            amount = int(amount * self._mad_outgoing_factor(shooter, target, at) + .5)
        if self.uprising is not None and not shell_hit and amount > 0:
            if self._end_uprising_play_dead(target, at) and enemy and not kinetic:
                return 0
        if self.uprising is not None and enemy and not shell_hit:
            factor = self.uprising.mark_damage_factor(shooter, target, at)
            if uprising.active(shooter, uprising.BERSERK, at):
                factor *= uprising.BERSERK_OUTGOING
            if uprising.active(target, uprising.BERSERK, at):
                factor *= uprising.BERSERK_INCOMING
            amount = int(amount * factor + .5)
        damage = min(target.health, amount)
        if damage <= 0:
            return 0
        self._end_vehicle_cloak(target, at)
        if shell_hit and enemy:
            shooter.hits += 1
        if enemy:
            shooter.damage += damage
            self.emit('damage_statistics',shooter.entity_id,damage=shooter.damage)
            if self.uprising is not None:
                reward = uprising.damage_reward(shooter.damage, damage)
                if damage == target.health:
                    reward += uprising.ELIXIR_PER_KILL
                self._award_elixir(shooter, reward)
            sources = [row for part,row in target.track_assist_sources.items()
                       if part in target.tracks and target.tracks[part].broken
                       and row[0] != source and row[0] in self.vehicles
                       and self.vehicles[row[0]].team == shooter.team]
            if sources:
                helper = self.vehicles[max(sources,key=lambda row:row[1])[0]]
                helper.assisted_damage += damage
                self.emit('assisted_statistics',helper.entity_id,damage=helper.assisted_damage)
        health_before_hit = target.health
        target.health -= damage
        if ram_blast_energy and not target.alive:
            self.emit('ram_detonated', source, target.entity_id, energy=ram_blast_energy,
                      position=target.pose.position, at=at)
        self._reset_base_capture(target, source)
        target.damage_received += damage
        self.emit('health', source or 0, target.entity_id, health=target.health,
                  **({'received_damage': damage} if shell_hit and shooter is not None
                     and shooter.entity_id != target.entity_id else {}),
                  **({'reflector_hit':True} if reflector_hit else {}),
                  **({'hit_effect':hit_effect} if hit_effect is not None else {}))
        if enemy and not kinetic and shooter.mad_passive == mad_games.REGENERATOR:
            self._mad_heal(shooter, shooter, damage * mad_games.LIFESTEAL)
        if enemy and self.big_boss is not None and shooter.entity_id in self.big_boss.roles:
            entity = shooter.entity_id
            progress = self.big_boss.role_progress(entity, shooter.max_health)
            leveled_up = self.big_boss.advance_level(entity, damage, shooter.max_health)
            if leveled_up and shooter.alive:
                self._emit_boss_role(entity, self._set_boss_role_ability(shooter))
                self._consumables(shooter, max(shooter.consumable_clock, at))
                self.emit('targeting', entity, yaw_rate=shooter.gun.yaw_rate,
                          pitch_rate=shooter.gun.pitch_rate)
            else:
                updated = self.big_boss.role_progress(entity, shooter.max_health)
                if leveled_up or updated != progress:
                    self.emit('boss_role_progress', entity, role=self.big_boss.roles[entity],
                              level=self.big_boss.levels[entity], progress=updated)
        if not target.alive:
            if self.mad_games_mode:
                self._set_mad_spotted(target, True, at)
                if target.mad_revenge:
                    target.mad_revenge.clear()
                    self._publish_mad_revenge(target)
                self._clear_mad_irradiator(target)
            death_blast = None
            if (not manual_respawn and self.big_boss is not None and self.world is not None
                    and self.big_boss.roles.get(target.entity_id) == big_boss.BOMBER):
                level = self.big_boss.levels[target.entity_id]
                spec = big_boss.ROLE_ACTIVE[big_boss.BOMBER][level]
                state = next((slot for slot in target.consumables if slot and slot.spec is spec), None)
                if state is not None and state.activate(at):
                    death_blast = level
            if self.recon is not None:
                self.recon.remove_vehicle(target.entity_id)
                self._recon_camouflage_still.pop(target.entity_id, None)
            target.flags = 0
            target.engine_mode = (0,0)
            if enemy:
                shooter.kills += 1
            target.killer = source or 0
            target.death_at = at
            if self.uprising is not None:
                self._clear_uprising_mark(target.entity_id)
                self._clear_uprising_curse(target.entity_id)
                self.uprising.history.pop(target.entity_id, None)
            self._stop_fire(target, at, 'death')
            self._stop_consumables(target)
            if self.big_boss is not None:
                if self.big_boss.heal_waves.pop(target.entity_id, None):
                    self._publish_state_modifiers(target)
                shield = self.big_boss.shields.pop(target.entity_id, None)
                poison = self.big_boss.poison.pop(target.entity_id, None)
                if shield or poison:
                    self._emit_boss_vehicle_effects(target)
            respawning = ((self.unlimited_lives or (self.practice and target.account_id is not None))
                          and target.account_id not in self.hangar_players and self._player_attached(target))
            if self.big_boss is not None and not manual_respawn:
                respawning = self.big_boss.destroyed(target) and respawning
                if target.entity_id in self.big_boss.bosses:
                    self._publish_state_modifiers(target)
                    self.emit('boss_lives', target.entity_id, lives=self.big_boss.lives[target.entity_id])
                if self.big_boss.tickets:
                    self.emit('boss_score', tickets=tuple(self.big_boss.tickets))
            if self.uprising is not None:
                lives = self.uprising.destroyed(target.entity_id)
                respawning = (lives > 0 and target.account_id not in self.hangar_players
                              and self._player_attached(target))
            if respawning:
                delay = 3.
                if self.uprising is not None:
                    self.uprising.prepare_offers(target.entity_id, getattr(target.definition, 'tier', 10))
                    if target.account_id is not None:
                        delay = uprising.CHOICE_SECONDS
                elif self.boss_role_picker and target.account_id is not None:
                    delay = 8.
                self.respawn_at[target.entity_id] = at + delay
            self.emit('killed', source or 0, target.entity_id,
                      keep_roster=respawning and (self.unlimited_lives or self.uprising is not None))
            if respawning and self.uprising is not None:
                self.emit('uprising_respawn', target.entity_id, source or 0, seconds=delay,
                          offers=self.uprising.offers(target.entity_id))
            if self.boss_role_picker and target.account_id is not None:
                self.emit('boss_picker', target.entity_id, state=2 if respawning else 0)
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
            if death_blast is not None:
                self._bomber_blast(target, death_blast, health_before_hit, at)
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
            source = None
            if self.big_boss is not None:
                if vehicle.entity_id in self.big_boss.bosses:
                    damage = min(damage, round(vehicle.max_health * .2))
                pushed_by, expires = self.big_boss.impulses.get(vehicle.entity_id, (None, 0.))
                if at < expires:
                    source = pushed_by
                if impact['kind'] == 'terrain' or at >= expires:
                    self.big_boss.impulses.pop(vehicle.entity_id, None)
            applied = self._damage_health(vehicle,damage,source,at,shell_hit=False,kinetic=True)
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
                amount = max(track.health, amount)
            if self.mad_games_mode:
                amount = mad_games.track_damage(vehicle, amount, at)
            applied = min(track.health,amount)
            track.damage(amount,at)
            if applied:
                self.emit('module_hit',0,vehicle.entity_id,shot=0,part=part,
                          damage=applied,health=track.health,at=at,cause='fall')
            if before != (track.broken,track.critical) or track.broken and applied:
                self._module_changed(vehicle, part, 1 if track.broken else 0, at,
                                     track_just_broke=track.broken and not before[0])

    def _ground_impact(self, impact, at):
        """Emit a ground or airborne tank impact and apply its ram HP damage.

        Both hulls take the same simultaneous impact: each loses HP for its
        own absorbed energy share and credits the other. Allies and wrecks
        take or deal none; shell mitigation factors do not apply to rams.
        """
        details = dict(impact)
        actor = details.pop('entity_a')
        target = details.pop('entity_b')
        damage = {}
        healing = []
        reflectors = set()
        reflected = []
        detonations = []
        if details.get('ram'):
            a, b = self.vehicles[actor], self.vehicles[target]
            if a.alive and b.alive and (a.team != b.team or self.mad_games_mode):
                if self.mad_games_mode:
                    self._end_vehicle_cloak(a, at)
                    self._end_vehicle_cloak(b, at)
                for vehicle, other, energy in ((a, b, details['ram_energy_a']),
                                               (b, a, details['ram_energy_b'])):
                    protection = getattr(getattr(vehicle.definition,
                                                 'module_protection', None),
                                         'ramming_absorption_percent', 0.)
                    nominal = self.RAM_DAMAGE_PER_KILOJOULE*energy/1000 * (1-protection/100)
                    if vehicle.team == other.team:
                        if other.mad_passive == mad_games.REGENERATOR:
                            healing.append((other, vehicle, nominal * mad_games.ALLY_HEAL))
                        continue
                    if other.mad_passive == mad_games.RAMMER:
                        nominal *= mad_games.RAM_OUTGOING
                    if vehicle.mad_passive == mad_games.RAMMER:
                        nominal *= mad_games.RAM_INCOMING
                    if self.uprising is not None:
                        nominal *= self.uprising.upgrade(other.entity_id, 'ram').ram_out
                        nominal *= self.uprising.upgrade(vehicle.entity_id, 'ram').ram_in
                    if self.mad_games_mode:
                        # Driving can stop the dash at this tick's end when it
                        # hits a hull. The collision still happened during it.
                        if mad_games.active(vehicle, mad_games.ACCELERATOR, math.nextafter(at, -math.inf)):
                            nominal *= mad_games.DASH_RAM_DAMAGE_FACTOR
                        nominal *= self._mad_outgoing_factor(other, vehicle, at)
                    amount = int(nominal+.5)
                    if self.mad_games_mode:
                        amount = self._mad_shield_damage(vehicle, amount, at)
                        if mad_games.active(vehicle, mad_games.REFLECTOR, at):
                            reflectors.add(vehicle.entity_id)
                    damage[vehicle.entity_id] = min(vehicle.health, amount)
            details.update(ram_damage_a=damage.get(actor, 0),
                           ram_damage_b=damage.get(target, 0))
        self.emit('ground_impact', actor, target, **details)
        for entity, source in ((actor, target), (target, actor)):
            if damage.get(entity):
                defender = self.vehicles[entity]
                energy = details['ram_energy_a' if entity == actor else 'ram_energy_b']
                blast_energy = (energy if self.mad_games_mode
                                and -details.get('closing_speed', 0.) >= mad_games.RAM_BLAST_MIN_CLOSING_SPEED
                                and energy >= mad_games.RAM_BLAST_MIN_ENERGY else 0.)
                applied = self._damage_health(defender, damage[entity], source, at,
                                              shell_hit=False, kinetic=True,
                                              ram_blast_energy=blast_energy)
                attacker = self.vehicles[source]
                if blast_energy and applied and not defender.alive:
                    detonations.append((defender, attacker))
                if attacker.mad_passive == mad_games.REGENERATOR:
                    healing.append((attacker, attacker, applied * mad_games.LIFESTEAL))
                if entity in reflectors:
                    reflected.append((defender, attacker, applied * mad_games.REFLECTED_DAMAGE))
        for wreck, attacker in detonations:
            self._detach_ram_turret(wreck, attacker, at)
            self._ram_shockwave(wreck, at)
        for defender, attacker, amount in reflected:
            self._apply_mad_reflected_damage(defender, attacker, amount, at)
        for source, receiver, amount in healing:
            self._mad_heal(source, receiver, amount)

    def _detach_ram_turret(self, wreck, attacker, at):
        pose = wreck.pose
        x, y, z = pose.position
        dx, dz = x - attacker.pose.position[0], z - attacker.pose.position[2]
        distance = math.hypot(dx, dz)
        if distance < 1e-6:
            dx, dz, distance = math.sin(pose.yaw), math.cos(pose.yaw), 1.
        lx, lz = x + 7. * dx / distance, z + 7. * dz / distance
        parent = wreck.entity_id
        self._retire_ram_turret(parent)
        turret = mad_games.RamTurret(self.next_ram_turret, at, (x, y + 1.6, z),
                                    (lx, self.terrain.height(lx, lz) + .6, lz),
                                    pose.yaw, pose.pitch, pose.roll)
        self.next_ram_turret += 1
        self.ram_turrets[parent] = turret
        self.ram_detached_hulls.add(parent)
        self.emit('ram_turret_created', parent, turret=turret)

    def _retire_ram_turret(self, parent):
        turret = self.ram_turrets.pop(parent, None)
        if turret is not None:
            self.emit('ram_turret_removed', parent, turret_id=turret.entity)

    def _ram_shockwave(self, wreck, at):
        if self.world is None:
            return
        origin = wreck.pose.position
        affected = []
        for vehicle in self.vehicles.values():
            if not vehicle.alive or not self._player_attached(vehicle):
                continue
            distance = math.dist(vehicle.pose.position, origin)
            dx = vehicle.pose.position[0] - origin[0]
            dz = vehicle.pose.position[2] - origin[2]
            horizontal = math.hypot(dx, dz)
            if distance >= mad_games.RAM_BLAST_RADIUS or horizontal < 1e-6:
                continue
            mass = getattr(vehicle.definition, 'mass', 30_000.) or 30_000.
            mass_factor = min(1.4, math.sqrt(30_000. / mass))
            speed = mad_games.RAM_BLAST_SPEED * mass_factor * (1. - distance / mad_games.RAM_BLAST_RADIUS)
            push = (speed * dx / horizontal, 0., speed * dz / horizontal)
            entity = vehicle.entity_id
            if (self.world.add_ground_velocity(entity, push[0], push[2])
                    or self.world.add_airborne_velocity(entity, push)):
                affected.append(entity)
        self.emit('ram_shockwave', wreck.entity_id, position=origin, targets=tuple(affected), at=at)

    def _mad_shield_damage(self, target, damage, at):
        if damage <= target.max_health * mad_games.SHIELD_THRESHOLD:
            return damage
        for slot in target.consumables:
            if slot and slot.spec is mad_games.URANIUM_SHIELD and slot.activate(at):
                self.emit('provision_effect', target.entity_id, compact_id=slot.spec.compact_id)
                return int(damage * mad_games.SHIELD_DAMAGE + .5)
        return damage

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
        if forward not in (-1, 1):
            return None
        # Installed power includes permanent fuel/oil. Only timed boosts apply here.
        factor = getattr(vehicle, 'engine_factor', 1.) * sandbox.boost_factor(vehicle, sandbox.BOOST_POWER)
        speed = vehicle.speed if speed is None else speed
        acceleration = powered_acceleration(getattr(vehicle, 'definition', None), speed, resistance, factor * mode_power)
        return forward * acceleration if acceleration is not None else None

    def _terrain_mobility(self, vehicle):
        mobility = terrain_at(self.terrain, vehicle.definition, vehicle.pose.position)
        if self.mad_games_mode and mad_games.active(vehicle, mad_games.HIGH_OCTANE, self.simulation_time):
            mobility = dataclasses.replace(mobility, resistance=mobility.resistance*mad_games.TERRAIN_RESISTANCE_FACTOR)
        return mobility

    def _drive_pitch(self, vehicle):
        """Road slope for forces, independent of suspension body lean."""
        sampler = getattr(self.world,'drive_pitch',None) if self.world else None
        return sampler(vehicle.entity_id) if callable(sampler) else vehicle.pose.pitch

    @staticmethod
    def _braking_acceleration(vehicle):
        return braking_acceleration(getattr(vehicle, 'definition', None))

    @staticmethod
    def _slope_grip_acceleration(assets, pitch, gravity):
        return slope_grip_acceleration(assets, pitch, gravity, Arena.SLOPE_GRIP_FACTOR)

    def _player_attached(self, vehicle):
        if (timeline := getattr(self, 'time_stop', None)) is not None and timeline.entity == vehicle.entity_id:
            return False
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

    def _drive_speed_factor(self, vehicle):
        factor = self._crew_role_factor(vehicle, 'driver')
        if self._is_boss(vehicle):
            factor *= big_boss.TOP_SPEED
        elif self.big_boss is not None:
            factor *= self.big_boss.mobility(vehicle.entity_id)
        if self.gravity_mode:
            factor *= 1.35
        if self.mad_games_mode and mad_games.active(vehicle, mad_games.HIGH_OCTANE, self.simulation_time):
            factor *= mad_games.SPEED_FACTOR
        if self.mad_games_mode and vehicle.mad_cloaked:
            factor *= mad_games.CHAMELEON_SPEED_FACTOR
        factor *= sandbox.boost_factor(vehicle, sandbox.BOOST_SPEED)
        return factor

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
        driver_factor = self._drive_speed_factor(vehicle)
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
        pull = (self.big_boss.pull(vehicle.team, vehicle.pose.position,
                                  getattr(vehicle.definition, 'mass', big_boss.BLAST_REFERENCE_MASS))
                if self.big_boss is not None and self.big_boss.traps and vehicle.alive and self.world
                else None)
        if pull is not None:
            trap_response = -math.expm1(-big_boss.TRAP_RESPONSE*dt)
            if airborne:
                vx, _, vz = self.world.motion[vehicle.entity_id].velocity
                self.world.add_flight_velocity(vehicle.entity_id,
                    ((pull[0]-vx)*trap_response, 0., (pull[1]-vz)*trap_response))
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
        if broken:
            forward = turn = 0
        engine = self.engines.get(vehicle.entity_id)
        if engine is not None and engine.destroyed:
            # A destroyed engine is immobilized on the ground. In flight,
            # retain Bullet's existing momentum/gravity path without a snap.
            forward = turn = 0
            if vehicle.alive and not airborne and not ground_motion_active and landing is None:
                vehicle.speed = 0.
        # Held track input still drives engine audio during flight.
        drive_mask = flags & 15 if forward or turn else 0
        vehicle.engine_mode = ((0,0) if not vehicle.alive or engine is not None and engine.destroyed
                               else (3,drive_mask) if drive_mask else (1,0))
        track_yaw_delta = yaw_delta = drive_acceleration = 0.
        planar_displacement = planar_velocity = None
        damped_ground_yaw_rate = 0.
        instant_brake = False
        if not airborne:
            assets = self.world.assets_for(vehicle.entity_id) if self.world else None
            pitch = self._drive_pitch(vehicle)
            surface_cos = max(.05, math.cos(pitch))
            mobility = self._terrain_mobility(vehicle)
            # Timed speed biases add once to the permanent caps (which already
            # carry gear oil); shared assets and the definition are never changed.
            # The pinned client exposes crew injury state. The official client
            # support rule explicitly halves a shell-shocked driver's top speed;
            # it does not provide a numeric acceleration or turn-rate multiplier.
            # Apply only the documented cap here. The factor is queried from the
            # per-vehicle CrewState on every fixed step, so a medkit restores the
            # current drive without mutating shared assets.
            driver_factor = self._drive_speed_factor(vehicle)
            top_forward = ((assets.forward_speed if assets else 11.5) + vehicle.forward_speed_bias) * driver_factor
            top_reverse = (((assets.reverse_speed if assets else 5.) + vehicle.reverse_speed_bias) *
                           driver_factor * sandbox.stat_bonus(vehicle, sandbox.STAT_REVERSE))
            target = forward*(top_forward if forward>0 else top_reverse)*surface_cos
            # Turn rate takes only the timed rotation factor, never the engine one.
            turn_rate = ((assets.rotation_speed if assets else math.radians(35))
                         *vehicle.effective_hull_rotation_factor*mobility.turn_factor)
            if pull is not None:
                target = math.copysign(min(abs(target)*big_boss.TRAP_DRIVE_SPEED_FACTOR,
                                           big_boss.TRAP_DRIVE_SPEED_CAP), target)
                turn_rate *= big_boss.TRAP_TURN_FACTOR
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
                                5*fuel*vehicle.engine_factor*mode_power*sandbox.boost_factor(vehicle, sandbox.BOOST_POWER))
            if not vehicle.alive:
                acceleration = 5.
            elif broken:
                # An input lease expiring is not a brake impulse. The broken
                # belts supply their passive contact drag below, including when
                # both are torn; they cannot power or pivot a stopped hull.
                acceleration = 0.
            # Engine/brake forces act along the road. `speed` is horizontal,
            # while engine power and the stock speed cap describe surface speed.
            acceleration *= surface_cos
            track_yaw_delta = turn*turn_rate*dt
            yaw_delta = track_yaw_delta
            if ground_motion_active and track_motion is None:
                damped_ground_yaw_rate = (ground_motion.yaw_rate *
                    math.exp(-self.world.GROUND_YAW_DAMPING*dt))
                yaw_delta += damped_ground_yaw_rate*dt
            # `speed` is horizontal metres/second along the hull heading.  The
            # support solver already fitted the hull's pitch from its four
            # carrying points, so gravity's matching horizontal longitudinal
            # component is g*sin(pitch)*cos(pitch). The ground momentum layer
            # handles turning slip separately from these longitudinal forces.
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
            if not broken and not (engine and engine.destroyed) and vehicle.alive:
                target, acceleration, instant_brake = sandbox.brake_assist(
                    sandbox.stat_percent(vehicle, sandbox.STAT_BRAKE),
                    vehicle.speed, forward, target, acceleration, slope, dt)
            vehicle.speed, drive_acceleration = advance_speed(
                vehicle.speed, target, acceleration, slope, rolling_step, dt)
            if instant_brake:
                vehicle.speed = 0.
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
            planar_velocity,residual_slip = skid_velocity(
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
            grip *= sandbox.stat_bonus(vehicle, sandbox.STAT_GRIP, maximum=3.)
            planar_velocity,residual_slip = skid_velocity(velocity,middle_yaw,
                0. if instant_brake else along,
                math.inf if instant_brake else grip*grip_fraction(landing.elapsed)*dt)
            planar_displacement = (planar_velocity[0]*dt,planar_velocity[1]*dt)
        elif ground_mode and not airborne and track_motion is None:
            old_velocity = (ground_motion.velocity if ground_motion_active else
                (math.sin(vehicle.pose.yaw)*previous_speed,
                 math.cos(vehicle.pose.yaw)*previous_speed))
            grip = self.world.GROUND_LATERAL_GRIP * sandbox.stat_bonus(vehicle, sandbox.STAT_GRIP, maximum=3.)
            if self.mad_games_mode and mad_games.active(vehicle, mad_games.HIGH_OCTANE, self.simulation_time):
                grip *= mad_games.HIGH_OCTANE_LATERAL_GRIP
            planar_velocity,residual_slip = steering_skid(
                old_velocity, vehicle.pose.yaw, yaw_delta, vehicle.speed,
                math.inf if instant_brake else grip*dt, mobility.turn_factor)
            planar_displacement = (planar_velocity[0]*dt,planar_velocity[1]*dt)
        if pull is not None and not airborne:
            # The field damps incoming momentum toward its pull plus the
            # reduced track input. Contacts still resolve the resulting move.
            heading = vehicle.pose.yaw+yaw_delta*.5
            fx, fz = math.sin(heading), math.cos(heading)
            vx, vz = planar_velocity if planar_velocity is not None else (
                fx*vehicle.speed, fz*vehicle.speed)
            vx += (pull[0]+fx*target-vx)*trap_response
            vz += (pull[1]+fz*target-vz)*trap_response
            planar_velocity = (vx, vz)
            planar_displacement = (vx*dt, vz*dt)
            residual_slip = abs(vx*fz-vz*fx)
        if self.world:
            start_pose = vehicle.pose
            if ground_mode or track_motion is not None or landing is not None or pull is not None:
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
        if (self.drive_workers is not None or self._drive_workers_attempted
                or not isinstance(self.world,CollisionWorld)
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
        native = getattr(world, '_native_physics', None)
        if native is not None:
            self._last_drive_parallel = False
            results = {}
            # Each ordered drive can change its own hull, including when it
            # uses the Python fallback. Publish those changes before the next
            # native move, with one full sync at the phase boundary.
            with native.sensor_batch(world) as dirty:
                for vehicle in vehicles:
                    entity = vehicle.entity_id
                    results[entity] = self._drive(vehicle, dt, now)
                    dirty.add(entity)
            return results
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
                    and entity not in world.open_map_borders
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
            if (target is None or not target.alive or mad_games.concealed(target, vehicle.team) or
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
                   else math.radians(35))*vehicle.effective_hull_rotation_factor  # same bound as _drive
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
            if self.drive_workers.closing:
                self.drive_workers.advance_close()
            else:
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
            if self.time_stop is not None and not self.finished_at:
                # Return effects belong to the completed boundary. A rigid
                # hull launched here first moves in the next physics step.
                self.time_stop.tick(self.simulation_time)
            self._record_motion_frame(self.simulation_time)
        if deadline is not None and self.accumulator > self.MAX_CATCHUP:
            # Deferred work must not grow without bound during an overload.
            # Report only wall time that will never be integrated; retaining
            # the rest preserves the next fixed-step sequence and sim clock.
            self.dropped_time += self.accumulator-self.MAX_CATCHUP
            self.accumulator = self.MAX_CATCHUP

    def shot_dispersion(self, vehicle, now):
        shot_age = None if vehicle.last_shot_at is None else now - vehicle.last_shot_at
        gun_state = self.guns.get(vehicle.entity_id)
        return dispersion_tangent(getattr(vehicle.definition, 'aiming', None), shot_age=shot_age,
                                  factor=vehicle.effective_dispersion_factor,
                                  aim_factor=vehicle.effective_aiming_time_factor,
                                  movement_factor=vehicle.gun.movement_dispersion,
                                  gun_damaged=gun_state is not None and gun_state.damaged)

    def aim_marker(self, vehicle, now):
        origin, direction = self.shot_ray(vehicle)
        dispersion = self.shot_dispersion(vehicle, now)
        if not dispersion:
            return origin, direction, 0.
        shell = self.shell_for(vehicle)
        speed = shell.speed if shell is not None else 0.
        if self._is_boss(vehicle) and any(
                s and s.spec is big_boss.MEGASHOT and s.state_at(now).state == READY
                for s in vehicle.consumables):
            speed = big_boss.MEGASHOT_SPEED
        return origin, tuple(axis * speed for axis in direction), dispersion

    def _record_motion_frame(self, now):
        """Capture the first complete simulation state for each 30 Hz label."""
        tick = game_tick(now,self.epoch)
        if self.motion_frame is not None and self.motion_frame.tick == tick:
            return
        timeline = getattr(self, 'time_stop', None)
        phased = timeline.entity if timeline is not None else None
        samples = {}
        for vehicle in self.vehicles.values():
            marker = None
            player = self.players.get(vehicle.account_id)
            if vehicle.alive and vehicle.entity_id != phased and player is not None and player.attached:
                marker = self.aim_marker(vehicle, now)
            samples[vehicle.entity_id] = MotionSample(vehicle.pose,vehicle.gun.packed(),
                vehicle.gun.yaw,marker,vehicle.engine_mode if vehicle.alive else (0,0))
        self.motion_frame = MotionFrame(tick,now,self.step_count,samples)

    def _fixed_step(self, now):
        dt = self.STEP
        for parent, turret in tuple(self.ram_turrets.items()):
            if (now >= turret.launched_at + mad_games.RAM_TURRET_LIFETIME
                    or self.vehicles[parent].alive):
                self._retire_ram_turret(parent)
        phased = self.time_stop.entity if getattr(self, 'time_stop', None) is not None else None
        self._advance_supremacy(now)
        if self.mad_games_mode and not self.finished_at and self.step_count % 6 == 0:
            self._update_mad_games(now)
        for vehicle in self.vehicles.values():
            if vehicle.entity_id == phased:
                continue
            if not self.finished_at and vehicle.alive:
                # A state expiry can lie inside this projectile frame. Delay
                # its public property16 event until every earlier contact has
                # been resolved; segment-local snapshots below still select
                # the exact configuration for each flight interval.
                self._consumables(vehicle, now, sync_movable=False)
            if vehicle.reload_at and now>=vehicle.reload_at and vehicle.pump_times and not vehicle.burst_pending:
                # Intershot end; _consumables already started the refill at it.
                vehicle.reload_at = 0.
                if vehicle.clip and vehicle.alive and not self.finished_at:
                    self.emit('reload',vehicle.entity_id,delay=0.,total=vehicle.reload_total)
                self._emit_pump(vehicle,now)
            elif vehicle.reload_at and now>=vehicle.reload_at and not vehicle.burst_pending:
                switched = finish_magazine_reload(vehicle)
                if switched:
                    self.emit('shell_selected',vehicle.entity_id,shell=switched)
                self.emit('ammo',vehicle.entity_id)
                self.emit('reload',vehicle.entity_id,delay=0.,total=vehicle.reload_total)
        if self.practice and not self.finished_at:
            self._advance_practice(now)
        if (not self.practice_clock_repeats and not self.endless and not self.finished_at
                and not self.solo_victory_team
                and now-self.started>=self.battle_seconds):
            winner = (self.big_boss.leading_team() if self.big_boss is not None else
                      self.supremacy.timeout_winner() if self.supremacy is not None else 0)
            self.finish(winner, 'supremacy_timeout' if self.supremacy is not None else 'timeout', now)
        if not self.finished_at:
            if self.uprising is not None:
                self._advance_uprising_income(now)
            self._advance_respawns(now)
            self._refresh_boss_picker(now)
            if self.big_boss is not None:
                self._advance_boss_roles(now)
            if self.big_boss is not None and self.big_boss.traps:
                remaining = [trap for trap in self.big_boss.traps if now < trap.expires_at]
                if len(remaining) != len(self.big_boss.traps):
                    self.big_boss.traps[:] = remaining
                    self.emit('gravitrap_expired', traps=tuple(remaining))
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
                coasting = not vehicle.alive and abs(vehicle.speed) > .001
                if attached or airborne or skidding or coasting or vehicle.entity_id in self._landing_skids:
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
                if self.big_boss is not None and self.big_boss.blast_landings:
                    self._recover_blast_landings()
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
                for impact in getattr(self.world, 'air_ram_impacts', {}).values():
                    self._ground_impact(impact, now)
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
                if player.attached and player.vehicle_id != phased:
                    self._aim(self.vehicles[player.vehicle_id],dt,before[player.vehicle_id].pose.yaw)
            for eid, target in bot_targets.items():
                bot = self.vehicles[eid]
                # A patrol look has no visible fire target, but still needs
                # the same physical gun slew and published joint angles.
                if bot.alive and (target is not None or self.training_bots[eid].travel_look_active):
                    self._aim(bot,dt,before[eid].pose.yaw)
            if self.practice_gunners:
                target = self.vehicle_for(next(iter(self.players)))
                if target.alive and self._player_attached(target):
                    for entity, gunner in self.practice_gunners.items():
                        bot = self.vehicles[entity]
                        if bot.alive:
                            bot.gun.set_point(gunner.aim_point(target.entity_id, target.pose.position, now), False)
                            self._aim(bot, dt, before[entity].pose.yaw)
            if self._recon_point_probe is not None:
                self._recon_point_probe_update(now)
            if self._recon_client_aims:
                self._recon_client_aim_update()
            if self.uprising is not None:
                self._advance_uprising_targeted(now, before)
            after = body_states(self.vehicles)
            if self.recon is not None:
                for vehicle in self.vehicles.values():
                    self._update_recon_camouflage(vehicle, now)
            for entity,vehicle in self.vehicles.items():
                previous, current = before[entity].pose, after[entity].pose
                distance = math.dist(previous.position, current.position)
                vehicle.distance += distance
                if vehicle.alive and entity != phased:
                    vehicle.gun.update_dispersion(getattr(vehicle.definition, 'aiming', None),
                                                  previous, current, before[entity].turret, dt,
                                                  vehicle.effective_aiming_time_factor,
                                                  previous_pitch=before[entity].pitch)
                if self.team_spotting and vehicle.alive and entity != phased:
                    vehicle.spotting_moving = (
                        distance >= self.RECON_CAMOUFLAGE_STILL_SPEED * dt or
                        abs(wrap(current.yaw - previous.yaw)) >= self.RECON_CAMOUFLAGE_STILL_YAW_RATE * dt)
                    if vehicle.spotting_moving:
                        vehicle.spotting_last_motion_at = now
            contacts = []
            frame_start = now-dt
            armor_segments = self._armor_state_segments(frame_start, now) if self.projectiles else ()
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
                shield = self.big_boss.active_shield(target.entity_id, impact.at) if self.big_boss and target else None
                if (shield is None
                        and self._internal_candidate(impact)):
                    fraction = max(0.,min(1.,(impact.at-(now-dt))/dt))
                    detonate_ammo_rack = self._internal_contact(
                        impact,self._armor_frame_state(self.vehicles[impact.target],before[impact.target],
                                                       after[impact.target],fraction,impact.at),now)
                self._impact(impact,states,detonate_ammo_rack=detonate_ammo_rack,
                             fire_at=max(self.module_clock, min(now, impact.at)))
                if target is not None:
                    self._start_pending_fire(target)
            if self.uprising is not None and self.uprising.protected_shots:
                self.uprising.protected_shots = {
                    key for key in self.uprising.protected_shots if key[0] in self.projectiles}
            if self.recon is not None:
                # Contacts for a shell may span frames; retain only shots still
                # in flight and discard expired markers on simulation time.
                for shot in tuple(self._recon_contacts):
                    if shot not in self.projectiles:
                        del self._recon_contacts[shot]
                self.recon.expire(now)
            for vehicle in self.vehicles.values():
                if vehicle.entity_id == phased:
                    continue
                if vehicle.alive:
                    self._advance_fire(vehicle, now)
                if vehicle.alive:
                    self._advance_tracks(vehicle,now)
                self._sync_movable_armor(vehicle, now)
            self.module_clock = now
            if self.uprising is not None and self.step_count % uprising.REWIND_SAMPLE_STEPS == 0:
                self._record_uprising_rewinds(now)
            if self.team_spotting and not self.mad_games_mode and self.world is not None:
                spacing = uprising.SPOTTING_SAMPLE_STEPS if self.uprising is not None else 6
                if self.step_count % spacing == 0:
                    self._update_team_spotting(now)
            teams = {v.team for v in self.vehicles.values()
                     if (v.alive or self.uprising is not None and (
                         v.entity_id in self.respawn_at or v.account_id in self.hangar_players
                         and self.uprising.remaining(v.entity_id) > 0)) and
                     (v.account_id is None or self.players[v.account_id].attached)}
            # An accepted burst is one trigger, not independent future input.
            # Let its already committed partner leave the barrel before the
            # elimination result clears reload state.  Direct leave/timeout
            # still finishes immediately and explicitly cancels the burst.
            pending_burst = any(v.entity_id != phased and v.alive and v.burst_pending and
                                (v.account_id is None or self.players[v.account_id].attached)
                                for v in self.vehicles.values())
            if (self.solo and not self.practice and not self.training_bots and teams == {1}
                    and self._combat_can_finish()
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
                bot = self.vehicles[eid]
                if (not self.finished_at and self.big_boss is not None and bot.alive and target is not None
                        and target.alive and math.dist(bot.pose.position, target.pose.position) < 26.):
                    if self._is_boss(bot):
                        self._boss_blast(bot)
                    elif eid in self.big_boss.roles:
                        state = next(slot for slot in bot.consumables
                                     if slot and slot.spec in big_boss.ACTIVE_SPECS)
                        role = self.big_boss.roles[eid]
                        if now >= state.ready_at and (role != big_boss.BOMBER
                                                      or self.aim_hit(bot)[0] == target.entity_id):
                            self._activate_boss_role_ability(bot, state, now)
                self._training_bot_fire(bot,target,now,self.training_bots[eid])
            if self.practice_gunners:
                target = self.vehicle_for(next(iter(self.players)))
                if target.alive and self._player_attached(target):
                    for entity, gunner in self.practice_gunners.items():
                        bot = self.vehicles[entity]
                        if bot.alive:
                            self._training_bot_fire(bot, target, now, gunner)
        elif self.world is not None:
            # Let a wreck settle during the final battle countdown as well.
            wrecks = [vehicle for vehicle in self.vehicles.values()
                      if not vehicle.alive and abs(vehicle.speed) > .001
                      and self.world.motion[vehicle.entity_id].grounded]
            if wrecks:
                self.world.begin_ground_step()
                for vehicle in wrecks:
                    self._drive(vehicle, dt, now)
        for vehicle in self.vehicles.values():
            if vehicle.entity_id == phased or not vehicle.burst_pending:
                continue
            attached = vehicle.account_id is None or self.players[vehicle.account_id].attached
            if self.finished_at or not vehicle.alive or not attached:
                self._cancel_burst(vehicle)
            elif now>=vehicle.reload_at:
                self._fire_burst_next(vehicle,now)
        if self.big_boss is not None and 0 in self.big_boss.tickets and not self.finished_at:
            self.finish(self.big_boss.leading_team(), 'boss_score', now)
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
               custom_bot_slots=False,
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
                      custom_bot_slots=custom_bot_slots,
                      prebattle_seconds=prebattle_seconds, sandbox_enabled=self.sandbox_enabled,
                      room_mode=room_mode, room_settings=room_settings)
        if self.recon_point_tolerance is not None and arena.recon is not None:
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
