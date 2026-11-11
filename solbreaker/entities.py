"""Battle state independent of connections and client wire messages."""
from dataclasses import dataclass, field

from .battle_rewards import BattleReward, calculate_rewards
from .aiming import GunAim
from .collision import Pose
from .vehicle_data import VehicleDefinition
from .consumables import ConsumableState
from .modules import TrackState


@dataclass
class Player:
    account_id: int
    nickname: str
    avatar_id: int
    vehicle_id: int
    team: int
    loaded: bool = False
    attached: bool = True
    departure_reason: str = ''
    inventory_id: int | None = None


@dataclass
class Vehicle:
    entity_id: int
    account_id: int | None
    nickname: str
    team: int
    descriptor: bytes
    pose: Pose
    max_health: int = 280
    health: int = 280
    ammo: int = 564
    clip: int = 4
    reload_at: float = 0.
    reload_total: float = 6.  # Current full-cycle duration, not elapsed history.
    reload_kind: str = 'reload'  # What reload_at/total time: 'reload' (magazine) or 'interval'.
    # Pump autoreloader refill of raw index clip, separate from the reload_at
    # fire gate. pump_at 0 is idle; pump_waiting resumes it after an intershot.
    pump_at: float = 0.
    pump_total: float = 0.
    pump_waiting: bool = False
    # Pending burst rounds: one accepted trigger can emit a group.  The Shell
    # (with any Tungsten snapshot) and effect belong to that group; the deadline
    # is reload_at, kind 'interval', so Fire stays gated between its rounds.
    burst_shell: object = None
    burst_effect: int = 0
    burst_remaining: int = 0
    burst_switch_shell: int = 0  # Apply a type change after the accepted group ends.
    speed: float = 0.
    engine_mode: tuple[int, int] = (1, 0)
    flags: int = 0
    last_input: float = 0.
    airborne_input_until: float = 0.
    airborne_control_active: bool = False
    shots: int = 0
    hits: int = 0
    damage: int = 0
    blocked_damage: int = 0
    assisted_damage: int = 0
    track_assist_sources: dict = field(default_factory=dict)
    kills: int = 0
    direct_hits: int = 0
    piercings: int = 0
    damage_received: int = 0
    hits_received: int = 0
    piercings_received: int = 0
    killer: int = 0
    distance: float = 0.
    death_at: float = 0.
    gun: GunAim = field(default_factory=GunAim)
    autoaim_target: int = 0
    autoaim_offset: tuple | None = None
    definition: VehicleDefinition | None = None
    loadout_ammo: int | None = None
    loadout_shells: tuple | None = None
    ammo_by_shell: dict[int,int] = field(default_factory=dict)
    selected_shell: int = 0
    next_shell: int = 0
    consumables: tuple[ConsumableState | None, ...] = ()
    consumable_status: dict[int, int] = field(default_factory=dict)
    # Immutable controller rows for a vehicle with verified MovableArmor.  It
    # is copied into each ballistic frame; ordinary and unsupported vehicles
    # retain None and therefore never enter the state-geometry query path.
    movable_armor_state: object | None = None
    engine_factor: float = 1.0
    rotation_factor: float = 1.0  # timed hull turn multiplier; also bounds the aim hull omega
    forward_speed_bias: float = 0.  # timed m/s added once to the forward/reverse caps
    reverse_speed_bias: float = 0.
    reload_speed_factor: float = 1.0
    interval_factor: float = 1.0
    consumable_clock: float = 0.0
    tracks: dict[str, TrackState] = field(default_factory=dict)
    capture_points: int = 0
    defense_points: float = 0.

    def battle_reward(self, won):
        return calculate_rewards(won=won, damage=self.damage,
            tier=getattr(self.definition, 'tier', 1), kills=self.kills, survived=self.alive)

    @property
    def shell_id(self):
        return self.selected_shell or (self.definition.weapon.shells[0].compact_id if self.definition else 513066)

    @property
    def clip_size(self):
        return self.definition.weapon.clip if self.definition else 4

    @property
    def pump_times(self):
        return getattr(self.definition.weapon,'pump_reload_times',()) if self.definition else ()

    @property
    def burst_pending(self):
        return self.burst_remaining > 0

    @property
    def burst_gun(self):
        return bool(self.definition and getattr(self.definition.weapon,'burst_count',1) > 1)

    @property
    def alive(self):
        return self.health > 0


@dataclass(frozen=True)
class MotionSample:
    pose: Pose
    gun_packed: int
    gun_yaw: float
    marker: tuple | None = None  # (target point, shot direction), no wire bytes.
    engine_mode: tuple[int, int] = (1, 0)


@dataclass(frozen=True)
class MotionFrame:
    tick: int
    at: float
    step_count: int
    samples: dict[int, MotionSample]


@dataclass(frozen=True)
class BattleEvent:
    kind: str
    actor: int = 0
    target: int = 0
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BattleResult:
    result_id: str
    account_id: int
    won: bool
    shots: int
    hits: int
    damage: int
    reason: str
    inventory_id: int | None = None
    reward: BattleReward | None = None  # None retains legacy receipt compatibility.
