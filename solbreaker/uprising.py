"""Rules for the pinned client's Uprising battle mode."""

from collections import deque
from dataclasses import dataclass

from .consumables import ConsumableSpec, ConsumableState
from .mad_games import active, vehicle_abilities

MODE = 'uprising'
CLIENT_GAME_MODE = 23
CLIENT_GAMEPLAY = b'hellgames'
STARTING_LIVES = 3
CHOICE_SECONDS = 10.
OFFER_COUNT = 3
MAX_ELIXIR = 0xffff

# Pinned respawnMoonwalk is a hidden automatic Moonwalk consumable.
RESPAWN_CLOAK = ConsumableSpec('respawnMoonwalk', 40, (40 << 8) | 0xfb, 10., 1.,
                               engine_factor=4., forward_bias_kmh=20.)

MOONWALK = ConsumableSpec('moonwalkHellgames', 41, (41 << 8) | 0xfb, 10., 35.,
                          engine_factor=4., forward_bias_kmh=30.)
BERSERK = ConsumableSpec('berserkHellgames', 74, (74 << 8) | 0xfb, 15., 50.,
                         reload_speed_factor=1. / .7)
HUNTERS_MARK = ConsumableSpec('huntersMarkHellgames', 42, (42 << 8) | 0xfb, 10., 30.)
CURSE = ConsumableSpec('curse', 36, (36 << 8) | 0xfb, 10., 45.)
TORNADO = ConsumableSpec('tornadoHellgames', 43, (43 << 8) | 0xfb, 0., 40.)
TIME_JUMP = ConsumableSpec('returnInTime', 37, (37 << 8) | 0xfb, 0., 40.)
PLAY_DEAD = ConsumableSpec('pretendDead', 38, (38 << 8) | 0xfb, 15., 35.)
REWIND_SECONDS = 4.
REWIND_SAMPLE_STEPS = 6
SPOTTING_SAMPLE_STEPS = 3

MARK_EFFECT = 5  # VehicleClientEffect.huntersMarkHellgamesEffect
MARK_DAMAGE = 1.6
CURSE_RANGE = 100.
CURSE_DISTANCE = 10.
CURSE_DAMAGE = .1
BERSERK_OUTGOING = 1.5
BERSERK_INCOMING = 1.4
CLOAKS = (RESPAWN_CLOAK, MOONWALK)
TARGETED_ABILITIES = (HUNTERS_MARK, CURSE, TORNADO)
ACTIVE_ABILITIES = (MOONWALK, BERSERK, TIME_JUMP, PLAY_DEAD, *TARGETED_ABILITIES)


def install_ability(vehicle, game, *, cooldown_limit=None):
    slots = tuple(slot for slot in vehicle.consumables
                  if slot is None or slot.spec not in ACTIVE_ABILITIES)
    tier = getattr(vehicle.definition, 'tier', None)
    if tier is None or not 5 <= tier <= 10:
        vehicle.consumables = slots
        return
    abilities = vehicle_abilities(game, vehicle.definition, mode='hellgames')
    for spec in ACTIVE_ABILITIES:
        if (spec.tag in abilities and (spec is not PLAY_DEAD
                or getattr(getattr(vehicle.definition, 'weapon', None), 'clip', 1) <= 1)):
            vehicle.consumables = slots + (None,) * max(0, 3-len(slots)) + (
                ConsumableState(spec, cooldown_limit=cooldown_limit),)
            return
    vehicle.consumables = slots


# The original client describes the sources of elixir but does not define
# the server reward rates. Keep the Solbreaker rates together for tuning.
ELIXIR_PER_SECOND = 2
HP_PER_ELIXIR = 10
ELIXIR_PER_KILL = 30
ELIXIR_PER_SPOT = 20


@dataclass
class TargetAbility:
    source: int
    team: int
    start: float
    end: float
    distance: float = 0.


@dataclass(frozen=True)
class RewindFrame:
    at: float
    pose: object
    motion: object
    speed: float
    health: int
    tracked: tuple
    modules: tuple
    crew: tuple


@dataclass(frozen=True)
class Upgrade:
    compact_id: int
    family: str
    grade: int
    cost: int
    engine: float = 1.
    speed_kmh: float = 0.
    damage: float = 1.
    reload: float = 1.
    ram_out: float = 1.
    ram_in: float = 1.
    fortune_chance: float = 0.
    minimum_tier: int = 5
    radius: float = 0.
    cooldown: float = 0.
    damage_factors: tuple[float, ...] = ()


_UNOWNED = Upgrade(0, '', 0, 0)
ENEMY_RELOAD = Upgrade(0x21fd, 'enemy_reload', 1, 100)
DUPLET = Upgrade(0x22fd, 'duplet', 3, 400, minimum_tier=1)
DUPLET_DELAY = .3
DUPLET_DISPERSION_FACTOR = 2.2
DUPLET_AIM_FACTOR = 1.6
BERSERK_DISPERSION_FACTOR = .67
GHOST = Upgrade(0x23fd, 'ghost', 1, 100)
GHOST_WARNING_DELAY = .15
GHOST_RETENTION_FACTOR = .015
INFERNO = Upgrade(0x22fb, 'inferno', 3, 400, radius=25., cooldown=30., damage_factors=(.3,))
CHAIN_LIGHTNING = Upgrade(0x23fb, 'lightning', 2, 200, radius=60., cooldown=6.,
                          damage_factors=(.25, .15, .1))
CHAIN_LIGHTNING_II = Upgrade(0x27fb, 'lightning', 3, 400, radius=70., cooldown=6.,
                             damage_factors=(.45, .35, .25))
SHOT_UPGRADE_START_COOLDOWN = 5.
CHAIN_HOP_SECONDS = .2


# Raw provision IDs, grades, costs and modifiers from the pinned Hellgames
# catalog. An enhanced grade takes the place of its basic grade.
_UPGRADES = (
    Upgrade(0x1efd, 'engine', 1, 100, engine=2.1, speed_kmh=10., minimum_tier=1),
    Upgrade(0x27fd, 'engine', 2, 200, engine=3.1, speed_kmh=20.),
    Upgrade(0x1ffd, 'damage', 1, 100, damage=1.2),
    Upgrade(0x25fd, 'damage', 2, 200, damage=1.4),
    Upgrade(0x20fd, 'reload', 1, 100, reload=.8),
    Upgrade(0x26fd, 'reload', 2, 200, reload=.65),
    Upgrade(0x24fd, 'ram', 1, 100, ram_out=1.6, ram_in=.65),
    Upgrade(0x28fd, 'ram', 2, 200, ram_out=2.1, ram_in=.35),
    Upgrade(0x6ffd, 'fortune', 1, 100, fortune_chance=.5),
    Upgrade(0x70fd, 'fortune', 2, 200, fortune_chance=.75),
    ENEMY_RELOAD,
    DUPLET,
    GHOST,
    INFERNO,
    CHAIN_LIGHTNING,
    CHAIN_LIGHTNING_II,
)


class BattleState:
    def __init__(self, rng):
        # The entity ID survives respawn, while Vehicle instances are replaced.
        self._remaining = {}
        self._elixir = {}
        self._rng = rng
        self._offers = {}
        self._selected = {}
        self._upgrades = {}
        self._shot_upgrade_cooldowns = {}
        self.lightning_hits = []
        self.elapsed_seconds = 0
        self.marks = {}
        self.curses = {}
        self.history = {}
        self.protected_shots = set()
        self._scouted_lives = set()

    def remaining(self, entity):
        return self._remaining.get(entity, STARTING_LIVES)

    def destroyed(self, entity):
        remaining = max(0, self.remaining(entity) - 1)
        self._remaining[entity] = remaining
        return remaining

    def elixir(self, entity):
        return self._elixir.get(entity, 0)

    def first_spot(self, entity):
        life = entity, self.remaining(entity)
        if life in self._scouted_lives:
            return False
        self._scouted_lives.add(life)
        return True

    def earn(self, entity, amount):
        before = self.elixir(entity)
        if amount <= 0 or before == MAX_ELIXIR or not self.remaining(entity):
            return None
        balance = min(before + amount, MAX_ELIXIR)
        self._elixir[entity] = balance
        return balance

    def prepare_offers(self, entity, tier=10):
        owned = self._upgrades.get(entity, {})
        candidates = {}
        for upgrade in _UPGRADES:
            if not upgrade.minimum_tier <= tier <= 10:
                continue
            previous = owned.get(upgrade.family)
            if previous is None or previous.grade < upgrade.grade:
                candidates.setdefault(upgrade.family, []).append(upgrade)
        families = self._rng.sample(tuple(candidates), min(OFFER_COUNT, len(candidates)))
        offers = (self._rng.choice(candidates[family]) for family in families)
        self._offers[entity] = {offer.compact_id: offer for offer in offers}
        self._selected.pop(entity, None)

    def offers(self, entity):
        selected = self._selected.get(entity, ())
        balance = self.elixir(entity)
        return tuple((offer.compact_id, 1 if offer.compact_id in selected else
                      2 if offer.cost > balance else 0)
                     for offer in self._offers.get(entity, {}).values())

    def select(self, entity, compact_id, selected):
        upgrade = self._offers.get(entity, {}).get(compact_id)
        if upgrade is None:
            return None
        choices = self._selected.setdefault(entity, set())
        if (compact_id in choices) == selected:
            return None
        balance = self.elixir(entity)
        if selected:
            if balance < upgrade.cost:
                return None
            choices.add(compact_id)
            balance -= upgrade.cost
        else:
            choices.remove(compact_id)
            balance = min(balance + upgrade.cost, MAX_ELIXIR)
        self._elixir[entity] = balance
        return balance

    def on_respawn(self, entity, *, bot=False, now=0., cooldown_limit=None):
        offers = self._offers.pop(entity, {})
        chosen = self._selected.pop(entity, ())
        if bot:
            for item in sorted(offers.values(), key=lambda offer: offer.cost, reverse=True):
                if item.compact_id not in chosen and self.elixir(entity) >= item.cost:
                    self._elixir[entity] -= item.cost
                    chosen = (*chosen, item.compact_id)
        if chosen:
            upgrades = self._upgrades.setdefault(entity, {})
            for compact_id in chosen:
                upgrade = offers[compact_id]
                upgrades[upgrade.family] = upgrade
        for upgrade in self._upgrades.get(entity, {}).values():
            if upgrade.cooldown:
                duration = SHOT_UPGRADE_START_COOLDOWN
                self._start_shot_upgrade_cooldown(entity, upgrade.family, now, duration, cooldown_limit)

    def trigger_shot_upgrade(self, entity, family, now, cooldown_limit=None):
        upgrade = self.upgrade(entity, family)
        ready_at, _, _ = self._shot_upgrade_cooldowns.get((entity, family), (0., 0., 0.))
        if not upgrade.cooldown or now < ready_at:
            return None
        duration = self._start_shot_upgrade_cooldown(entity, family, now, upgrade.cooldown, cooldown_limit)
        return upgrade, duration

    def _start_shot_upgrade_cooldown(self, entity, family, now, nominal, limit):
        duration = nominal if limit is None else min(nominal, limit)
        self._shot_upgrade_cooldowns[entity, family] = now + duration, duration, nominal
        return duration

    def set_shot_upgrade_cooldown_limit(self, entity, now, limit):
        for family in self._upgrades.get(entity, {}):
            key = entity, family
            ready_at, previous, nominal = self._shot_upgrade_cooldowns.get(key, (0., 0., 0.))
            if ready_at > now:
                duration = nominal if limit is None else min(nominal, limit)
                self._shot_upgrade_cooldowns[key] = now + (ready_at - now) * duration / previous, duration, nominal

    def shot_upgrade_statuses(self, entity, now):
        for upgrade in self._upgrades.get(entity, {}).values():
            if upgrade.cooldown:
                ready_at, duration, _ = self._shot_upgrade_cooldowns.get((entity, upgrade.family), (0., 0., 0.))
                yield upgrade.compact_id, max(0., ready_at - now), duration

    def advance_shot_upgrades(self, now):
        for (entity, family), (ready_at, _, _) in tuple(self._shot_upgrade_cooldowns.items()):
            if now >= ready_at:
                del self._shot_upgrade_cooldowns[entity, family]
                yield entity, self.upgrade(entity, family).compact_id

    def upgrade(self, entity, family):
        owned = self._upgrades.get(entity)
        return owned.get(family, _UNOWNED) if owned else _UNOWNED

    def has(self, entity, upgrade):
        return self.upgrade(entity, upgrade.family) is upgrade

    def record(self, entity, frame):
        self.history.setdefault(entity, deque(maxlen=48)).append(frame)

    def rewind_frame(self, entity, now):
        before = now - REWIND_SECONDS
        return next((frame for frame in reversed(self.history.get(entity, ())) if frame.at <= before), None)

    def vehicle_effects(self, entity):
        mark = self.marks.get(entity)
        return ((MARK_EFFECT, MARK_EFFECT, mark.start, mark.end),) if mark else ()

    def exposes(self, entity, now):
        return any(mark.source == entity and now < mark.end for mark in self.marks.values())

    def mark_damage_factor(self, shooter, target, at):
        mark = self.marks.get(target.entity_id)
        return MARK_DAMAGE if mark and mark.team == shooter.team and mark.start <= at < mark.end else 1.

    def shell_damage_factor(self, entity, random_sample):
        modifier = self.upgrade(entity, 'damage').damage
        chance = self.upgrade(entity, 'fortune').fortune_chance
        if chance and random_sample() < chance:
            modifier *= 1.25
        return modifier

    def income_since_tick(self, elapsed):
        elapsed = int(elapsed)
        if elapsed <= self.elapsed_seconds:
            return 0
        gained = (elapsed - self.elapsed_seconds) * ELIXIR_PER_SECOND
        self.elapsed_seconds = elapsed
        return gained


def damage_reward(total, actual_damage):
    return total // HP_PER_ELIXIR - (total - actual_damage) // HP_PER_ELIXIR
