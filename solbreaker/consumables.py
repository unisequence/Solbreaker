import math
from dataclasses import dataclass, replace
from pathlib import Path

from .loadouts import load_catalog

READY, ACTIVE, COOLDOWN = 1, 2, 3
BOOST, REPAIR, EXTINGUISH, HEAL, RESTORE = 'boost', 'repair', 'extinguish', 'heal', 'restore'
BATTLE_CONSUMABLES = frozenset(('afterburning', 'freeAfterburning', 'berserk', 'largeRepairkit',
                                'newAutoloader', 'tungstenTip', 'improvedAfterburning', 'shieldKit',
                                'fireControlSystem', 'autoExtinguisher', 'largeMedkit', 'largeRecoverkit',
                                'armorMover'))

# tag -> (script, {parameter path: required text}, factor path, factor attribute scale)
_RULES = {
    'afterburning': ('Afterburning', {}, 'bonusValues/enginePowerIncrease', 'engine', 100),
    'freeAfterburning': ('ImprovedAfterburning', {'rotationSpeedFactor': 1.0, 'fwdSpeedLimitBias': 0.0,
                                                 'bkwdSpeedLimitBias': 0.0},
                         'enginePowerIncrease', 'engine', None),
    'berserk': ('Berserk', {}, 'bonusValues/gunReloadSpeedIncrease', 'reload', 100),
    # Native raw29/CD(29<<8)|0xfb. The localized description scales the time
    # of each shell IN the magazine: GunParams intershot (+0xc0), not the
    # magazine reload (+0xac), which gunReloadTimeFactor1 leaves unchanged.
    'newAutoloader': ('NewAutoloader', {'gunReloadTimeFactor': 1.0}, 'clipReloadTimeFactor', 'interval', None),
}
# Scripts that must carry exactly these parameters; anything else is an
# unsupported variant, never a guessed slower or faster autoloader.
_EXACT_PARAMETERS = {'newAutoloader': ('clipReloadTimeFactor', 'cooldown', 'duration', 'gunReloadTimeFactor')}
# Track repair: native raw5/CD1531 with an empty use blob. The pinned XML
# script carries only a cooldown; repair effects are applied by the arena.
_REPAIRS = {'largeRepairkit': 'Repairkit'}
# Large First Aid Kit: pinned raw3/CD1019, the same Repairkit script with one
# cooldown leaf. Native raw3 use and two real heals verified on M41; Arena owns injuries.
_HEALS = {'largeMedkit': ('Repairkit', 3, (('cooldown', 75.0),))}
HEAL_CONSUMABLES = frozenset(_HEALS)
# Pinned description promises repair + crew healing + extinguishing together.
# Raw20's native manual input carries the same empty blob as raw3/raw5.
_RESTORES = {'largeRecoverkit': ('Recoverkit', 20, (('cooldown', 100.0),))}
# Auto extinguisher is an automatic zero-duration consumable.  This preserves
# its exact catalog contract only; Arena owns the later fire-triggered use.
_AUTO_EXTINGUISHERS = {
    'autoExtinguisher': (1, 'Extinguisher', (('automatic', 'true'), ('cooldown', '70'))),
}
# Tungsten Rounds: native raw45/CD11771. Only the HP damage roll bounds and
# the shot effect of shells fired while ACTIVE change (local sandbox rule).
_SHOT_BOOSTS = {'tungstenTip': 'TungstenTip'}
_SHOT_PARAMETERS = ('cooldown', 'duration', 'maxDamageFactor', 'minDamageFactor', 'shotEffect')
# Improved Afterburning: native raw27/CD7163, the same script and six direct
# leaves as freeAfterburning but with a real turn factor and km/h speed biases.
_DRIVE_BOOSTS = {'improvedAfterburning': 'ImprovedAfterburning'}
_DRIVE_PARAMETERS = ('bkwdSpeedLimitBias', 'cooldown', 'duration', 'enginePowerIncrease',
                     'fwdSpeedLimitBias', 'rotationSpeedFactor')
# Reactive Armor: pinned id33 ShieldKit. While ACTIVE at the impact time, enemy
# non-HE HP damage taken scales by 1-shieldCoef (local sandbox rule).
_SHIELDS = {'shieldKit': ('ShieldKit', 33)}
_SHIELD_PARAMETERS = ('cooldown', 'duration', 'shieldCoef')
# Armor Mover raw73 is restricted by the pinned catalog to five Titan vehicles.
# Its localization explicitly says “any shell”, unlike ShieldKit's non-HE-only
# behavior.  activate/deactivate state fields are validated as the exact XML
# wire shape but remain client-side state metadata in this sandbox.
_ARMOR_MOVERS = {'armorMover': ('ArmorMover', 73)}
_ARMOR_MOVER_PARAMETERS = ('activateStates', 'bonusValues/damageReductionPercent', 'cooldown',
                           'deactivateStates', 'duration')
# Accelerated Aiming: pinned id28 FireControlSystem (Reticle Calibration),
# repurposed by a local sandbox rule. While ACTIVE, turret yaw traverse and gun
# vertical traverse are multiplied by AIMING_RATE_FACTOR. Its accuracy leaves
# are verified exactly but intentionally never applied: aiming time,
# dispersion, reload, camera and hull turn stay unchanged.
_AIMING_BOOSTS = {'fireControlSystem': ('FireControlSystem', 28)}
_AIMING_PARAMETERS = {'aimingTimeFactor': 0.7, 'cooldown': 55.0, 'duration': 20.0,
                      'shotDispersionAngleFactor': 0.7}
AIMING_RATE_FACTOR = 1.30


def _damage_factors(value):
    return (type(value) is tuple and len(value) == 2
            and all(not isinstance(f, bool) and isinstance(f, (int, float)) and math.isfinite(f) for f in value)
            and 0 < value[0] <= value[1])


def _real(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def boosts_drive(item):
    """True for a spec or view that changes engine power, turn rate or speed caps."""
    return (item.engine_factor != 1 or item.rotation_factor != 1
            or item.forward_bias_kmh != 0 or item.reverse_bias_kmh != 0)


@dataclass(frozen=True)
class ConsumableSpec:
    tag: str
    ident: int
    compact_id: int
    duration: float
    cooldown: float
    engine_factor: float = 1.0
    reload_speed_factor: float = 1.0
    action: str = BOOST
    interval_factor: float = 1.0  # Intershot time multiplier; magazine reload unaffected.
    # Shells fired while ACTIVE: HP roll bounds and native shot-effect index.
    damage_factors: tuple | None = None
    shot_effect: int | None = None
    # While ACTIVE: hull turn multiplier and km/h added once to the speed caps.
    rotation_factor: float = 1.0
    forward_bias_kmh: float = 0.0
    reverse_bias_kmh: float = 0.0
    # While ACTIVE: multiplier on enemy non-HE HP damage this vehicle takes.
    damage_taken_factor: float = 1.0
    # While ACTIVE: multiplier on enemy HP damage from every shell, including HE splash.
    all_shell_damage_factor: float = 1.0
    # While ACTIVE: turret yaw and gun pitch traverse rate multipliers.
    yaw_rate_factor: float = 1.0
    pitch_rate_factor: float = 1.0
    # ArmorMover's verified controller IDs. They remain explicit so Arena can
    # publish the same complete property16 snapshot it uses for collision.
    armor_activate_state: int | None = None
    armor_deactivate_state: int | None = None
    # Raw automatic XML flag; no automatic behavior belongs in this data layer.
    automatic: bool = False

    def __post_init__(self):
        if self.action not in (BOOST, REPAIR, EXTINGUISH, HEAL, RESTORE):
            raise ValueError(f'Consumable {self.tag}: unknown action {self.action!r}')
        if type(self.automatic) is not bool:
            raise ValueError(f'Consumable {self.tag}: automatic must be a bool')
        if not (math.isfinite(self.interval_factor) and 0 < self.interval_factor <= 1):
            raise ValueError(f'Consumable {self.tag}: invalid interval factor')
        if self.damage_factors is not None and not _damage_factors(self.damage_factors):
            raise ValueError(f'Consumable {self.tag}: invalid damage factors')
        if self.shot_effect is not None and (type(self.shot_effect) is not int or not 0 <= self.shot_effect < 256):
            raise ValueError(f'Consumable {self.tag}: invalid shot effect')
        if not (_real(self.rotation_factor) and self.rotation_factor > 0 and _real(self.forward_bias_kmh)
                and self.forward_bias_kmh >= 0 and _real(self.reverse_bias_kmh) and self.reverse_bias_kmh >= 0):
            raise ValueError(f'Consumable {self.tag}: invalid drive modifiers')
        if not (_real(self.damage_taken_factor) and 0 < self.damage_taken_factor <= 1
                and _real(self.all_shell_damage_factor) and 0 < self.all_shell_damage_factor <= 1):
            raise ValueError(f'Consumable {self.tag}: invalid damage taken factor')
        if not all(_real(f) and f > 0 for f in (self.yaw_rate_factor, self.pitch_rate_factor)):
            raise ValueError(f'Consumable {self.tag}: invalid aiming rate factors')
        states = self.armor_activate_state,self.armor_deactivate_state
        if (states[0] is None) != (states[1] is None):
            raise ValueError(f'Consumable {self.tag}: incomplete armor state pair')
        if states[0] is not None and (any(type(value) is not int or not 0 <= value <= 0xffff
                                          for value in states)
                                      or states[0] == states[1]):
            raise ValueError(f'Consumable {self.tag}: invalid armor state pair')
        if self.action in (REPAIR, EXTINGUISH, HEAL, RESTORE) and (self.duration != 0 or boosts_drive(self)
                                                     or self.reload_speed_factor != 1 or self.interval_factor != 1
                                                     or self.damage_factors is not None or self.shot_effect is not None
                                                     or self.damage_taken_factor != 1 or self.all_shell_damage_factor != 1
                                                     or self.yaw_rate_factor != 1
                                                     or self.pitch_rate_factor != 1):
            # Instant use: the HUD goes straight to COOLDOWN, never ACTIVE.
            raise ValueError(f'Consumable {self.tag}: instant action has no timed effect')
        if self.action == EXTINGUISH and not self.automatic:
            raise ValueError(f'Consumable {self.tag}: extinguisher must be automatic')
        if self.action != EXTINGUISH and self.automatic:
            raise ValueError(f'Consumable {self.tag}: automatic requires extinguisher action')


@dataclass(frozen=True)
class ConsumableView:
    state: int
    remaining: int
    total: int
    engine_factor: float
    reload_speed_factor: float
    interval_factor: float = 1.0
    damage_factors: tuple | None = None  # ACTIVE only; None keeps each shell's own range.
    shot_effect: int | None = None
    rotation_factor: float = 1.0
    forward_bias_kmh: float = 0.0
    reverse_bias_kmh: float = 0.0
    damage_taken_factor: float = 1.0  # ACTIVE only; the victim's own shield.
    all_shell_damage_factor: float = 1.0  # ACTIVE only; ArmorMover's all-shell reduction.
    yaw_rate_factor: float = 1.0  # ACTIVE only; turret and gun traverse.
    pitch_rate_factor: float = 1.0


def _number(params, path, tag):
    values = [text for key, text in params if key == path]
    if len(values) != 1:
        raise ValueError(f'Consumable {tag}: expected one {path}')
    try:
        value = float(values[0])
    except ValueError:
        raise ValueError(f'Consumable {tag}: invalid {path}') from None
    if not math.isfinite(value):
        raise ValueError(f'Consumable {tag}: invalid {path}')
    return value


def resolve_consumable(game, tag) -> ConsumableSpec:
    """Build a spec from pinned XML; runtime behavior is owned by Arena."""
    instant = _HEALS.get(tag) or _RESTORES.get(tag)
    if instant is not None:
        script, ident, expected = instant
        item = next((i for i in load_catalog(Path(game)).consumables if i.tag == tag), None)
        if item is None:
            raise ValueError(f'Unknown consumable: {tag}')
        if item.script != script or item.ident != ident:
            raise ValueError(f'Consumable {tag}: unexpected script or id')
        if (tuple(key for key, _ in item.parameters) != tuple(key for key, _ in expected)
                or any(_number(item.parameters, key, tag) != value for key, value in expected)):
            raise ValueError(f'Consumable {tag}: unsupported variant')
        return ConsumableSpec(tag, ident, (ident << 8) | 0xfb, 0.0, expected[0][1],
                              action=RESTORE if tag in _RESTORES else HEAL)
    rule = _RULES.get(tag)
    extinguisher = _AUTO_EXTINGUISHERS.get(tag)
    if (rule is None and tag not in _REPAIRS and tag not in _SHOT_BOOSTS and tag not in _DRIVE_BOOSTS
            and tag not in _SHIELDS and tag not in _ARMOR_MOVERS and tag not in _AIMING_BOOSTS
            and extinguisher is None):
        raise ValueError(f'Unsupported consumable: {tag}')
    script = (rule[0] if rule else _REPAIRS.get(tag) or _SHOT_BOOSTS.get(tag) or _DRIVE_BOOSTS.get(tag)
              or (extinguisher[1] if extinguisher is not None else None)
              or (_SHIELDS.get(tag) or _ARMOR_MOVERS.get(tag) or _AIMING_BOOSTS[tag])[0])
    item = next((i for i in load_catalog(Path(game)).consumables if i.tag == tag), None)
    if item is None:
        raise ValueError(f'Unknown consumable: {tag}')
    if item.script != script:
        raise ValueError(f'Consumable {tag}: unexpected script {item.script}')
    params = item.parameters
    if extinguisher is not None:
        ident, _, expected = extinguisher
        if item.ident != ident or params != expected:
            raise ValueError(f'Consumable {tag}: unsupported Extinguisher XML shape')
        cooldown = _number(params, 'cooldown', tag)
        if cooldown <= 0:
            raise ValueError(f'Consumable {tag}: invalid cooldown')
        return ConsumableSpec(tag, ident, (ident << 8) | 0xfb, 0.0, cooldown,
                              action=EXTINGUISH, automatic=True)
    if tag in _AIMING_BOOSTS:
        if item.ident != _AIMING_BOOSTS[tag][1]:
            raise ValueError(f'Consumable {tag}: unexpected id {item.ident}')
        if tuple(sorted(key for key, _ in params)) != tuple(sorted(_AIMING_PARAMETERS)):
            raise ValueError(f'Consumable {tag}: unsupported variant')
        # Every pinned leaf must match, the ignored accuracy factors included.
        for key, expected in _AIMING_PARAMETERS.items():
            if _number(params, key, tag) != expected:
                raise ValueError(f'Consumable {tag}: unsupported variant ({key})')
        return ConsumableSpec(tag, item.ident, (item.ident << 8) | 0xfb, _AIMING_PARAMETERS['duration'],
                              _AIMING_PARAMETERS['cooldown'], yaw_rate_factor=AIMING_RATE_FACTOR,
                              pitch_rate_factor=AIMING_RATE_FACTOR)
    if tag in _SHIELDS:
        if item.ident != _SHIELDS[tag][1]:
            raise ValueError(f'Consumable {tag}: unexpected id {item.ident}')
        if tuple(sorted(key for key, _ in params)) != _SHIELD_PARAMETERS:
            raise ValueError(f'Consumable {tag}: unsupported variant')
        coef, duration, cooldown = (_number(params, key, tag) for key in ('shieldCoef', 'duration', 'cooldown'))
        # A shield must reduce but never nullify damage.
        if not 0 < coef < 1 or duration <= 0 or cooldown < duration:
            raise ValueError(f'Consumable {tag}: invalid parameters')
        return ConsumableSpec(tag, item.ident, (item.ident << 8) | 0xfb, duration, cooldown,
                              damage_taken_factor=1 - coef)
    if tag in _ARMOR_MOVERS:
        if item.ident != _ARMOR_MOVERS[tag][1] or tuple(sorted(key for key, _ in params)) != _ARMOR_MOVER_PARAMETERS:
            raise ValueError(f'Consumable {tag}: unsupported variant')
        values = {key: _number(params, key, tag) for key in _ARMOR_MOVER_PARAMETERS}
        if (values['activateStates'] != 1 or values['deactivateStates'] != 0
                or not 0 < values['bonusValues/damageReductionPercent'] < 100
                or values['duration'] <= 0 or values['cooldown'] < values['duration']):
            raise ValueError(f'Consumable {tag}: unsupported variant')
        return ConsumableSpec(
            tag, item.ident, (item.ident << 8) | 0xfb,
            values['duration'], values['cooldown'],
            all_shell_damage_factor=1-values['bonusValues/damageReductionPercent']/100,
            armor_activate_state=int(values['activateStates']),
            armor_deactivate_state=int(values['deactivateStates']))
    if tag in _SHOT_BOOSTS:
        if tuple(sorted(key for key, _ in params)) != _SHOT_PARAMETERS:
            raise ValueError(f'Consumable {tag}: unsupported variant')
        factors = (_number(params, 'minDamageFactor', tag), _number(params, 'maxDamageFactor', tag))
        duration, cooldown = _number(params, 'duration', tag), _number(params, 'cooldown', tag)
        if not _damage_factors(factors) or duration <= 0 or cooldown < duration:
            raise ValueError(f'Consumable {tag}: invalid parameters')
        from .vehicle_data import _shell_effect
        # Unknown names or an explicit non-shell tracer fail; no substitute effect.
        effect = _shell_effect(game, next(text for key, text in params if key == 'shotEffect'))
        return ConsumableSpec(tag, item.ident, (item.ident << 8) | 0xfb, duration, cooldown,
                              damage_factors=factors, shot_effect=effect)
    if tag in _DRIVE_BOOSTS:
        if tuple(sorted(key for key, _ in params)) != _DRIVE_PARAMETERS:
            raise ValueError(f'Consumable {tag}: unsupported variant')
        engine, rotation, forward, reverse, duration, cooldown = (_number(params, key, tag) for key in (
            'enginePowerIncrease', 'rotationSpeedFactor', 'fwdSpeedLimitBias', 'bkwdSpeedLimitBias',
            'duration', 'cooldown'))
        # The engine must boost; turn and speed biases may only boost or stay neutral.
        if engine <= 1 or rotation < 1 or forward < 0 or reverse < 0 or duration <= 0 or cooldown < duration:
            raise ValueError(f'Consumable {tag}: invalid parameters')
        return ConsumableSpec(tag, item.ident, (item.ident << 8) | 0xfb, duration, cooldown, engine,
                              rotation_factor=rotation, forward_bias_kmh=forward, reverse_bias_kmh=reverse)
    exact = _EXACT_PARAMETERS.get(tag)
    if exact is not None and tuple(sorted(key for key, _ in params)) != exact:
        raise ValueError(f'Consumable {tag}: unsupported variant')
    if rule is None:
        # Exactly one cooldown leaf; any other XML parameter is an unsupported variant.
        if tuple(key for key, _ in params) != ('cooldown',):
            raise ValueError(f'Consumable {tag}: unsupported variant')
        cooldown = _number(params, 'cooldown', tag)
        if cooldown <= 0:
            raise ValueError(f'Consumable {tag}: invalid parameters')
        return ConsumableSpec(tag, item.ident, (item.ident << 8) | 0xfb, 0.0, cooldown, action=REPAIR)
    _, fixed, path, kind, scale = rule
    for key, expected in fixed.items():
        if _number(params, key, tag) != expected:
            raise ValueError(f'Consumable {tag}: unsupported variant ({key})')
    value = _number(params, path, tag)
    factor = 1 + value/scale if scale else value
    duration, cooldown = _number(params, 'duration', tag), _number(params, 'cooldown', tag)
    # Speed factors must boost (>1); an interval time factor must shorten (<1).
    if (not 0 < factor < 1 if kind == 'interval' else factor <= 1) or duration <= 0 or cooldown < duration:
        raise ValueError(f'Consumable {tag}: invalid parameters')
    return ConsumableSpec(tag, item.ident, (item.ident << 8) | 0xfb, duration, cooldown,
                          factor if kind == 'engine' else 1.0, factor if kind == 'reload' else 1.0,
                          interval_factor=factor if kind == 'interval' else 1.0)


def derive_consumable(spec, effects) -> ConsumableSpec:
    """Apply device timer effects once to a raw spec, returning a new spec.

    Native tooltip0x1d3dcc0: duration*(1+percent/100), cooldown/(1+boost);
    repair keeps duration0 but its cooldown shrinks too. Factors are unchanged.
    """
    factor, boost = effects.equipment_duration_factor, effects.equipment_reload_boost
    if not (math.isfinite(factor) and math.isfinite(boost) and factor > 0 and boost >= 0):
        raise ValueError(f'Consumable {spec.tag}: invalid timer effects')
    duration = spec.duration * factor
    cooldown = spec.cooldown / (1 + boost)
    if (not math.isfinite(duration) or not math.isfinite(cooldown) or cooldown <= 0
            or not 0 <= duration <= cooldown):
        raise ValueError(f'Consumable {spec.tag}: invalid derived timers')
    return replace(spec, duration=duration, cooldown=cooldown)


def _clock(now):
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError('Invalid simulation timestamp')
    return float(now)


class ConsumableState:
    """Per-vehicle timed consumable driven by the simulation clock; cooldown starts at activation.

    A zero-duration repair/extinguisher/heal/restore spec is never ACTIVE: activation goes
    straight to COOLDOWN, its factors stay 1.0 and stop() has nothing to end.
    This generic clock never chooses an automatic fire-triggered activation.
    """

    def __init__(self, spec: ConsumableSpec):
        self.spec = spec
        self.activated_at = 0.0
        self.active_until = 0.0
        self.ready_at = 0.0
        self.uses = 0

    def activate(self, now) -> bool:
        now = _clock(now)
        if self.uses and now < self.ready_at:
            return False
        self.activated_at = now
        self.active_until = now + self.spec.duration
        self.ready_at = now + self.spec.cooldown
        self.uses += 1
        return True

    def stop(self, now) -> bool:
        """End a live effect on death/battle end, preserving its original start."""
        now = _clock(now)
        if self.uses and self.activated_at <= now < self.active_until:
            self.active_until = now
            return True
        return False

    def state_at(self, now) -> ConsumableView:
        now = _clock(now)
        spec = self.spec
        if self.uses and now < self.active_until:
            return ConsumableView(ACTIVE, min(math.ceil(spec.duration), math.ceil(self.active_until - now)), math.ceil(spec.duration),
                                  spec.engine_factor, spec.reload_speed_factor, spec.interval_factor,
                                  spec.damage_factors, spec.shot_effect, spec.rotation_factor,
                                  spec.forward_bias_kmh, spec.reverse_bias_kmh, spec.damage_taken_factor,
                                  spec.all_shell_damage_factor, spec.yaw_rate_factor, spec.pitch_rate_factor)
        if self.uses and now < self.ready_at:
            return ConsumableView(COOLDOWN, min(math.ceil(spec.cooldown), math.ceil(self.ready_at - now)), math.ceil(spec.cooldown), 1.0, 1.0)
        return ConsumableView(READY, 0, 0, 1.0, 1.0)


def _crew_supported(crew_specs, crew_bindings):
    """Whether explicit bindings reach at least one damageable crew person."""
    from .modules import CrewSpec
    if (not isinstance(crew_specs, tuple) or not isinstance(crew_bindings, tuple) or not crew_bindings
            or any(not isinstance(spec, CrewSpec) for spec in crew_specs)):
        return False
    damageable = {spec.index for spec in crew_specs if spec.damageable}
    return all(isinstance(binding, tuple) and len(binding) == 2 and binding[1] in damageable
               for binding in crew_bindings)


def resolve_loadout(game, definition, loadout, *, crew_specs=None, crew_bindings=None):
    """Validate persistent slots again at battle entry (modules may have changed).

    Live state is deliberately not cached: every arena creates its own clocks.
    Timer devices come from the given loadout only and apply to fresh raw specs,
    so a definition already derived by apply_definition is never scaled twice.
    A Medkit is accepted only with the selected vehicle's immutable crew specs
    and explicit mesh bindings. A Recoverkit also works on vehicles with a
    supported repairable module; missing internal geometry is never invented.
    """
    from .loadouts import CONSUMABLE, COMPATIBLE
    from .loadout_effects import compose, validate_devices, battle_effect_devices, _loadout_mapping, _slot_tags
    catalog = load_catalog(Path(game))
    loadout = _loadout_mapping(loadout)
    tags = _slot_tags(loadout.get('consumables', (None, None, None)), 'consumable', 3)
    # Devices are checked against this vehicle's preset only when the loadout carries them.
    effects = compose(game, battle_effect_devices(validate_devices(game, definition, loadout)))
    slots, _ = catalog.vehicle_slots(definition.nation, definition.name)
    if any(tags[slots:]):
        raise ValueError('Invalid consumable slots')
    result = []
    for tag in tags:
        if tag is None:
            result.append(None)
            continue
        spec = resolve_consumable(game, tag)
        if (spec.action == REPAIR and getattr(definition, 'track_spec', None) is None
                and getattr(definition, 'gun_spec', None) is None):
            # A kit needs metadata for at least one simulated repairable module.
            raise ValueError('Repair kit is unsupported for this vehicle: ' + tag)
        if spec.action == HEAL and not _crew_supported(crew_specs, crew_bindings):
            # Never offer a kit without a modelled, bound crew person.
            raise ValueError('Medkit is unsupported for this vehicle: ' + tag)
        if (spec.action == RESTORE and getattr(definition, 'track_spec', None) is None
                and getattr(definition, 'gun_spec', None) is None
                and not _crew_supported(crew_specs, crew_bindings)):
            raise ValueError('Restoration pack is unsupported for this vehicle: ' + tag)
        if catalog.compatibility(CONSUMABLE, spec.ident, definition.nation, definition.name,
                                 clip=definition.weapon.clip) != COMPATIBLE:
            raise ValueError('Consumable is incompatible with the installed vehicle: ' + tag)
        if spec.interval_factor != 1 and definition.weapon.clip <= 1:
            # Intershot time exists only inside a real magazine.
            raise ValueError('Consumable requires a magazine gun: ' + tag)
        result.append(derive_consumable(spec, effects))
    return tuple(result[:slots])
