"""Pure compositor of loadout effects from the pinned catalog.

Only fields grounded in the XML are composed; every other selected item (and
unread calibratedShells keys) is reported in ``Effects.unsupported`` instead of
being guessed or silently dropped. Percent values become multipliers
``1 + percent/100``. Dispersion devices are unsupported (sandbox has zero
dispersion). Crew bonuses are additive levels, kept separate from role factors.
Collecting a bonus here does not enable that item in authoritative battles.
"""
from collections.abc import Mapping
from dataclasses import dataclass, replace
import math
import struct

from .loadouts import OPTIONAL_DEVICE, PROVISION, COMPATIBLE, load_catalog

# (script, XML attribute) -> Effects field for StaticFactorDevice percent bonuses
_STATIC = {
    'rammer': ('miscAttrs/gunReloadTimeFactor', 'reload_time_factor'),
    'enhancedAimDrives': ('miscAttrs/gunAimingTimeFactor', 'aiming_time_factor'),
}
_PIERCING = {'ARMOR_PIERCING': 'ap_piercing_factor', 'ARMOR_PIERCING_CR': 'apcr_piercing_factor',
             'HOLLOW_CHARGE': 'heat_piercing_factor', 'HIGH_EXPLOSIVE': 'he_piercing_factor'}
_UNREAD_PIERCING = ('piercingFactors/ARMOR_PIERCING_HE',)


@dataclass(frozen=True)
class Effects:
    reload_time_factor: float = 1.0
    aiming_time_factor: float = 1.0
    ap_piercing_factor: float = 1.0
    apcr_piercing_factor: float = 1.0
    heat_piercing_factor: float = 1.0
    he_piercing_factor: float = 1.0
    armor_homogenization_factor: float = 1.0
    projectile_speed_factor: float = 1.0
    piercing_penalty_500m_percent: float = 0.0  # raw XML percent (-60), semantics not interpreted
    max_hp_bonus_percent: float = 0.0  # highQualityAssembly float32 percent
    hp_stock_percents: tuple[float, ...] = ()  # float32 HP-stock percents, provision order
    rotation_speed_factor: float = 1.0
    crew_level_increase: float = 0.0
    engine_power_factor: float = 1.0
    turret_rotation_factor: float = 1.0
    repair_speed_factor: float = 1.0  # track regeneration only; other modules not modelled
    equipment_duration_factor: float = 1.0  # consumable duration multiplier
    equipment_reload_boost: float = 0.0  # consumable cooldown divides by 1+boost
    forward_speed_bias_kmh: float = 0.0  # summed oil bias, km/h; applied once as /3.6 m/s
    reverse_speed_bias_kmh: float = 0.0  # positive reverse cap magnitude bias, km/h
    upper_pitch_limit_increase: float = 0.0  # degrees of extra elevation
    lower_pitch_limit_increase: float = 0.0  # degrees of extra depression
    he_damage_factor: float = 1.0  # spall liner: final incoming HE HP damage multiplier
    he_effect_duration: float = 0.0  # spall liner visual FX seconds, not a protection timer
    track_restore_full_on_repair: bool = False  # raw112 local rule; no numeric bonus
    track_health_factor: float = 1.0  # raw108 max/maxRegen/hysteresis HP multiplier
    ramming_absorption_percent: float = 0.0  # raw108, opt-in ram HP reduction
    engine_chance_factor: float = 1.0  # raw109 internal projectile chance multiplier
    ammo_rack_chance_factor: float = 1.0
    crew_chance_factor: float = 1.0
    covered: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()


def _number(text, what):
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f'Invalid {what}: {text!r}') from None
    if value != value or value in (float('inf'), float('-inf')):
        raise ValueError(f'Invalid {what}: {text!r}')
    return value


def _factor(text, what):
    return 1 + _number(text, what) / 100


_PITCH_KEYS = ('bonusValues/upperPitchLimitIncrease', 'bonusValues/lowerPitchLimitIncrease')
_FUEL_KEYS = ('bonusValues/enginePowerIncrease', 'bonusValues/turretRotationSpeedIncrease')
_PROJECTILE_SPEED_KEY = 'bonusValues/projectileSpeedFactor'
_PIERCING_PENALTY_KEY = 'bonusValues/piercingPenaltyFactor500m'


def _powder_speed(item, script, keys):
    if (item.script != script or len(item.parameters) != len(keys) or
            sorted(k for k, _ in item.parameters) != sorted(keys)):
        raise ValueError('Unexpected powder definition: ' + item.tag)
    percent = _number(dict(item.parameters)[_PROJECTILE_SPEED_KEY], item.tag)
    if percent <= 0:
        raise ValueError('Invalid projectile speed bonus: ' + item.tag)
    return percent


def _gun_powder(item):
    """Strict provision46 speed percent; other provision scripts remain unsupported."""
    if item.tag != 'gunPowder':
        return None
    return _powder_speed(item, 'GunPowder', (_PROJECTILE_SPEED_KEY,))


def _fuel(item):
    """Return (engine %, turret %) for a strict Fuel item, else None; bad numbers raise."""
    if item.script != 'Fuel' or sorted(k for k, _ in item.parameters) != sorted(_FUEL_KEYS):
        return None
    params = dict(item.parameters)
    engine, turret = (_number(params[k], item.tag) for k in _FUEL_KEYS)
    if engine < 0 or turret < 0:
        raise ValueError('Invalid fuel bonus: ' + item.tag)
    return engine, turret


_OIL_TAGS = ('gearOil', 'improvedGearOil')
_OIL_KEYS = ('bonusValues/fwdSpeedLimitBias', 'bonusValues/bkwdSpeedLimitBias',
             'bonusValues/enginePowerIncrease')


def _gear_oil(item):
    """(forward km/h, reverse km/h, engine %) of a strict GearOil item, else None.

    Tag or script selects the item; any deviation from the exact three keys
    raises instead of silently turning a malformed oil unsupported.
    """
    if item.tag not in _OIL_TAGS and item.script != 'GearOil':
        return None
    keys = [k for k, _ in item.parameters]
    if item.tag not in _OIL_TAGS or item.script != 'GearOil' or sorted(keys) != sorted(_OIL_KEYS):
        raise ValueError('Unexpected gear oil definition: ' + item.tag)
    params = dict(item.parameters)
    values = tuple(_number(params[k], item.tag) for k in _OIL_KEYS)
    if any(v < 0 for v in values):
        raise ValueError('Invalid gear oil bonus: ' + item.tag)
    return values


ANTI_HE_PROVISION = 101  # raw provision id; compact (101<<8)|0xfd
_ANTI_HE_KEY = 'bonusValues/factor'
_ANTI_HE_DURATION = 'extraEffectDuration'
_ANTI_HE_DISPLAY = 'display_params/antiHighExplosiveFactor'


def _anti_he(item):
    """(factor, FX seconds) of the strict spall liner, else None; bad shapes raise.

    Tag or script selects it. The factor must be 0<f<1 and its display
    percent must equal (f-1)*100; the duration lies outside <script>.
    """
    if item.tag != 'antiHighExplosive' and item.script != 'AntiHighExplosive':
        return None
    if (item.tag != 'antiHighExplosive' or item.script != 'AntiHighExplosive' or
            item.ident != ANTI_HE_PROVISION or tuple(k for k, _ in item.parameters) != (_ANTI_HE_KEY,)):
        raise ValueError('Unexpected antiHighExplosive definition')
    factor = _number(item.parameters[0][1], item.tag)
    if not 0 < factor < 1:
        raise ValueError('Invalid HE damage factor: ' + item.tag)
    durations = [v for k, v in item.extras if k == _ANTI_HE_DURATION]
    display = [v for k, v in item.extras if k == _ANTI_HE_DISPLAY]
    if len(durations) != 1 or len(display) != 1:
        raise ValueError('Unexpected antiHighExplosive definition')
    duration = _number(durations[0], item.tag)
    if duration <= 0:
        raise ValueError('Invalid HE effect duration: ' + item.tag)
    if _number(display[0], item.tag) != round((factor-1)*100, 6):
        raise ValueError('Unexpected HE display value: ' + item.tag)
    return factor, duration


REINFORCED_TRACKS_DEVICE = 112
_TRACK_MODULES = ('leftTrackHealth', 'rightTrackHealth')

_COMPARTMENT_SCRIPT = 'ImprovedBattleCompartment'
# Exact leaves from compatible build 11.21.0 optional_devices.xml. Empty parent leaves
# are deliberate XML shape markers, so they are checked as strings rather
# than treated as missing numeric values.
_COMPARTMENT_SHAPES = {
    'protectedModules': (108, {
        'modulesHealthFactors/modules/leftTrackHealth': 'track_health_factor',
        'modulesHealthFactors/modules/rightTrackHealth': 'track_health_factor',
        'modulesHealthFactors/modules/fuelTankHealth': 1.0,
        'modulesHealthFactors/modules/ammoBayHealth': 1.0,
        'rammingAbsorptionPercent': 'ramming_absorption_percent',
        'chancesToHit': '',
    }),
    'improvedBattleCompartment': (109, {
        'modulesHealthFactors': '',
        'chancesToHit/crew': 'crew_chance_factor',
        'chancesToHit/modules/engineHealth': 'engine_chance_factor',
        'chancesToHit/modules/ammoBayHealth': 'ammo_rack_chance_factor',
        **{'chancesToHit/modules/' + name: 1.0 for name in (
            'commanderHealth', 'driverHealth', 'gunner1Health', 'gunner2Health',
            'loader1Health', 'loader2Health')},
        'rammingAbsorptionPercent': '',
    }),
}


def _battle_compartment(item):
    """Read only the two exact pinned ImprovedBattleCompartment definitions."""
    if item.tag not in _COMPARTMENT_SHAPES and item.script != _COMPARTMENT_SCRIPT:
        return None
    ident, shape = _COMPARTMENT_SHAPES.get(item.tag, (None, None))
    keys = tuple(key for key, _ in item.parameters)
    if (shape is None or item.ident != ident or item.script != _COMPARTMENT_SCRIPT
            or len(keys) != len(set(keys)) or set(keys) != set(shape)):
        raise ValueError('Unexpected ImprovedBattleCompartment definition: ' + item.tag)
    values = {}
    for key, text in item.parameters:
        target = shape[key]
        if isinstance(target, str):
            if not target:
                if text:
                    raise ValueError('Unexpected ImprovedBattleCompartment marker: ' + item.tag)
            else:
                value = _number(text, item.tag)
                old = values.setdefault(target, value)
                if old != value:
                    raise ValueError('Inconsistent ImprovedBattleCompartment factor: ' + item.tag)
        elif _number(text, item.tag) != target:
            raise ValueError('Unsupported ImprovedBattleCompartment value: ' + item.tag)
    if ('track_health_factor' in values and values['track_health_factor'] <= 1):
        raise ValueError('Invalid track health factor: ' + item.tag)
    if ('ramming_absorption_percent' in values
            and not 0 < values['ramming_absorption_percent'] < 100):
        raise ValueError('Invalid ramming absorption: ' + item.tag)
    if any(not 0 < values[key] <= 1 for key in values if key.endswith('_chance_factor')):
        raise ValueError('Invalid projectile chance factor: ' + item.tag)
    return values

_RECYCLED_KEYS = tuple('enginePowerPercentageBonus/' + c
                       for c in ('lightTank', 'mediumTank', 'heavyTank', 'AT-SPG', 'SPG'))


def _recycled_engine(item):
    """Engine % of strict RecycledEngine; native maps it to enginePowerIncrease (0x858458).

    Values are per vehicle class, but definitions carry no proven class, so
    only a table with one equal value for all five classes is accepted.
    """
    if (item.script != 'RecycledEngine' or len(item.parameters) != len(_RECYCLED_KEYS) or
            sorted(k for k, _ in item.parameters) != sorted(_RECYCLED_KEYS)):
        raise ValueError('Unexpected recycledEngineSpeed definition')
    percents = {_number(v, item.tag) for _, v in item.parameters}
    if len(percents) != 1:
        raise ValueError('Class-specific engine bonus needs a vehicle class: ' + item.tag)
    percent = percents.pop()
    if percent < 0:
        raise ValueError('Invalid engine bonus: ' + item.tag)
    return percent


_HP_STOCK_SCRIPT = 'SmallHPStock'  # small and large stocks share script and key
_HP_STOCK_KEY = 'hpStockPercent'
_HP_DEVICE_KEY = 'maxHpBonusPercent'
_HP_DISPLAY_KEY = 'display_params/maxHpBonusPercent'


def _f32(value, what):
    """Round a double to the nearest finite float32 (as the native SSE result)."""
    try:
        result = struct.unpack('<f', struct.pack('<f', value))[0]
    except (OverflowError, struct.error):
        raise ValueError('Float32 overflow: ' + what) from None
    if not math.isfinite(result):
        raise ValueError('Float32 overflow: ' + what)
    return result


def _hp_percent(text, what):
    value = _f32(_number(text, what), what)
    if value <= 0:
        raise ValueError('Invalid HP bonus: ' + what)
    return value


def _hp_item(item, key):
    """Float32 percent of a script with exactly one key and a matching display value."""
    if tuple(k for k, _ in item.parameters) != (key,):
        raise ValueError(f'Unexpected {item.tag} definition')
    percent = _hp_percent(item.parameters[0][1], item.tag)
    display = [v for k, v in item.extras if k == _HP_DISPLAY_KEY]
    if len(display) != 1 or _hp_percent(display[0], item.tag) != percent:
        raise ValueError('Unexpected HP display value: ' + item.tag)
    return percent


def _hp_stock(item):
    """Return the float32 percent of a strict HP-stock item, else None; bad shapes raise."""
    return _hp_item(item, _HP_STOCK_KEY) if item.script == _HP_STOCK_SCRIPT else None


def _roundf(value):
    """C roundf: nearest integer, halves away from zero."""
    whole = math.floor(abs(value))
    whole += abs(value) - whole >= .5
    return int(math.copysign(whole, value))


def _max_health(base, percents):
    """Lobby HP (0x19ebfc2..0x19ec018): roundf(f32(base)*f32(1+f32(sum/100))).

    Percents add one entry at a time with addss (0x19ea58d), so every partial
    sum is float32. Double arithmetic on float32 operands rounds back to the
    same float32 result.
    """
    if type(base) is not int or base <= 0:
        raise ValueError('Invalid base health')
    total = 0.
    for percent in percents:
        total = _f32(total + percent, _HP_DEVICE_KEY)
    factor = _f32(1 + _f32(total / 100, _HP_DEVICE_KEY), _HP_DEVICE_KEY)
    health = _roundf(_f32(_f32(base, 'health') * factor, 'health'))
    if not 0 < health <= 32767:  # spawn properties carry signed i16 HP
        raise ValueError('Derived health is outside signed i16 range')
    return health


def _select(catalog, kind, tags):
    if isinstance(tags, str):
        raise ValueError('Tags must be a sequence of tag strings')
    tags = tuple(tags)
    if len(set(tags)) != len(tags):
        raise ValueError(f'Duplicate {kind} tag')
    by_tag = {i.tag: i for i in catalog._items(kind)}
    unknown = [t for t in tags if t not in by_tag]
    if unknown:
        raise ValueError(f'Unknown {kind} tag: {unknown[0]!r}')
    return [by_tag[t] for t in tags]


def _device(item, values):
    """Set grounded fields from one device into values; return unread unsupported keys."""
    params = dict(item.parameters)
    if item.tag in _STATIC:
        attribute, field = _STATIC[item.tag]
        if item.script != 'StaticFactorDevice' or params.get('attribute') != attribute:
            raise ValueError(f'Unexpected {item.tag} definition')
        values[field] = _factor(params['bonusValues/percent'], item.tag)
    elif item.tag == 'toolbox':
        if (item.script != 'StaticFactorDevice' or len(item.parameters) != 2 or
                sorted(params) != ['attribute', 'bonusValues/percent'] or
                params['attribute'] != 'miscAttrs/repairSpeedFactor'):
            raise ValueError('Unexpected toolbox definition')
        factor = _factor(params['bonusValues/percent'], item.tag)
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError('Invalid toolbox factor: ' + item.tag)
        values['repair_speed_factor'] = factor
    elif item.tag == 'multiLayerArmor':
        if (item.script != 'StaticFactorDevice' or len(item.parameters) != 2 or
                sorted(params) != ['attribute', 'bonusValues/percent'] or
                params['attribute'] != 'miscAttrs/armorHomogenizationFactor'):
            raise ValueError('Unexpected multiLayerArmor definition')
        percent = _number(params['bonusValues/percent'], item.tag)
        factor = 1 + percent / 100
        if percent <= 0 or not math.isfinite(factor):
            raise ValueError('Invalid armor homogenization bonus: ' + item.tag)
        values['armor_homogenization_factor'] = factor
    elif item.tag == 'perfectEquipment':
        if (item.script != 'StaticFactorDevice' or len(item.parameters) != 2 or
                sorted(params) != ['attribute', 'bonusValues/percent'] or
                params['attribute'] != 'miscAttrs/equipmentDurationFactor'):
            raise ValueError('Unexpected perfectEquipment definition')
        factor = _factor(params['bonusValues/percent'], item.tag)
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError('Invalid equipment duration factor: ' + item.tag)
        values['equipment_duration_factor'] = factor
    elif item.tag == 'equipmentStorageSystem':
        if (item.script != 'StaticAdditiveDevice' or len(item.parameters) != 2 or
                sorted(params) != ['attribute', 'bonusValues/value'] or
                params['attribute'] != 'miscAttrs/equipmentReloadBoost'):
            raise ValueError('Unexpected equipmentStorageSystem definition')
        boost = _number(params['bonusValues/value'], item.tag)
        if boost < 0:
            raise ValueError('Invalid equipment reload boost: ' + item.tag)
        values['equipment_reload_boost'] = boost
    elif item.tag == 'improvedPitchLimitsSystem':
        if (item.script != 'ImprovedPitchLimitsSystem' or len(item.parameters) != 2 or
                sorted(params) != sorted(_PITCH_KEYS)):
            raise ValueError('Unexpected improvedPitchLimitsSystem definition')
        upper, lower = (_number(params[k], item.tag) for k in _PITCH_KEYS)
        if upper < 0 or lower < 0:
            raise ValueError('Invalid pitch limit increase: ' + item.tag)
        values['upper_pitch_limit_increase'] = upper
        values['lower_pitch_limit_increase'] = lower
    elif item.tag == 'calibratedShells':
        if item.script != 'CalibratedShells':
            raise ValueError('Unexpected calibratedShells definition')
        for key, field in _PIERCING.items():
            values[field] = _factor(params['piercingFactors/' + key], item.tag)
        return tuple(f'{item.tag}/{k.split("/")[1]}' for k in _UNREAD_PIERCING if k in params)
    elif item.tag == 'highQualityAssembly':
        if item.script != 'HighQualityAssembly':
            raise ValueError('Unexpected highQualityAssembly definition')
        values['max_hp_bonus_percent'] = _hp_item(item, _HP_DEVICE_KEY)
    elif item.tag == 'improvedControls':
        if item.script != 'StaticPhysicsAttrsModifier':
            raise ValueError('Unexpected improvedControls definition')
        values['rotation_speed_factor'] = _factor(params['bonusPercentage/rotationSpeedFactor'], item.tag)
    elif item.tag == 'improvedVentilation':
        if (item.script != 'StaticAdditiveDevice' or len(item.parameters) != 2 or
                params.get('attribute') != 'miscAttrs/crewLevelIncrease'):
            raise ValueError('Unexpected improvedVentilation definition')
        values['crew_level_increase'] = _number(params['bonusValues/value'], item.tag)
    elif item.tag == 'reinforcedCaterpillars':
        # raw112 carries only a modules list naming exactly both tracks once.
        if (item.ident != REINFORCED_TRACKS_DEVICE or item.script != 'ModulesMaxRegenerator' or
                tuple(k for k, _ in item.parameters) != ('modules',) or
                sorted(item.parameters[0][1].split()) != sorted(_TRACK_MODULES)):
            raise ValueError('Unexpected reinforcedCaterpillars definition')
        values['track_restore_full_on_repair'] = True
    else:
        raise LookupError(item.tag)
    return ()


def compose(game, devices=(), provisions=()) -> Effects:
    """Compose catalog-tag selections; duplicates/unknown tags raise ValueError.

    Crew proficiency levels add before the role calculation; they are not
    direct reload or rotation percentages. HP-stock percents stay separate
    entries so maxHP can add them natively. Other provisions remain unsupported.
    """
    catalog = load_catalog(game)
    chosen = _select(catalog, OPTIONAL_DEVICE, devices)
    chosen_provisions = _select(catalog, PROVISION, provisions)
    values, covered, unsupported = {}, [], []
    engine_percent = turret_percent = projectile_percent = 0.
    forward_bias = reverse_bias = 0.
    stocks = []
    for item in chosen:
        compartment = _battle_compartment(item)
        if compartment is not None:
            values.update(compartment)
            covered.append(item.tag)
            continue
        if item.tag == 'recycledEngineSpeed':
            # Same enginePowerIncrease attribute as fuel (0x19ea577): percents add, one conversion.
            engine_percent += _recycled_engine(item)
            covered.append(item.tag)
            continue
        if item.tag == 'increasedPowderCharge':
            projectile_percent += _powder_speed(item, 'StaticMiscAttrsModifier',
                                               (_PROJECTILE_SPEED_KEY, _PIERCING_PENALTY_KEY))
            # Native overlay0x1e21da5 reads this key from device107 specifically;
            # provision46 has no penetration-loss bonus.
            values['piercing_penalty_500m_percent'] = _number(
                dict(item.parameters)[_PIERCING_PENALTY_KEY], item.tag)
            covered.append(item.tag)
            continue
        try:
            unread = _device(item, values)
        except LookupError:
            unsupported.append(item.tag)
            continue
        covered.append(item.tag)
        unsupported.extend(unread)
    for item in chosen_provisions:
        powder = _gun_powder(item)
        if powder is not None:
            projectile_percent += powder
            covered.append(item.tag)
            continue
        oil = _gear_oil(item)
        if oil is not None:
            # Speed biases are km/h and sum; engine percent joins the shared fuel/117 sum.
            forward_bias += oil[0]
            reverse_bias += oil[1]
            engine_percent += oil[2]
            covered.append(item.tag)
            continue
        fuel = _fuel(item)
        if fuel:
            # Identical native bonus keys sum first (0x19ec3af: hp*(1+sum/100)); 3+10 -> 1.13, +6 -> 1.19.
            engine_percent += fuel[0]
            turret_percent += fuel[1]
            covered.append(item.tag)
            continue
        stock = _hp_stock(item)
        if stock is not None:
            stocks.append(stock)
            covered.append(item.tag)
            continue
        anti_he = _anti_he(item)
        if anti_he is not None:
            values['he_damage_factor'], values['he_effect_duration'] = anti_he
            covered.append(item.tag)
            continue
        if item.script != 'Stimulator' or tuple(k for k,_ in item.parameters) != ('bonusValues/crewLevelIncrease',):
            unsupported.append(item.tag)
            continue
        bonus = _number(item.parameters[0][1], item.tag)
        if bonus < 0:
            raise ValueError('Invalid crew bonus: ' + item.tag)
        values['crew_level_increase'] = values.get('crew_level_increase', 0.) + bonus
        covered.append(item.tag)
    values['engine_power_factor'] = 1 + engine_percent / 100
    values['turret_rotation_factor'] = 1 + turret_percent / 100
    values['forward_speed_bias_kmh'] = forward_bias
    values['reverse_speed_bias_kmh'] = reverse_bias
    # Native Lobby0x19ec845..883 multiplies raw shot speed by 1+sum/100.
    # Both +35% items give1.70, not1.35*1.35. This does not prove the hidden
    # server's flight simulation; common miscParams0.8 is not mixed into it.
    values['projectile_speed_factor'] = 1 + projectile_percent / 100
    return Effects(**values, hp_stock_percents=tuple(stocks),
                   covered=tuple(covered), unsupported=tuple(unsupported))


# Effects integrated into authoritative state and native spawn. Other tags may
# be catalogued and persisted but must not be offered as working battle effects.
BATTLE_DEVICES = frozenset(('rammer', 'calibratedShells', 'highQualityAssembly',
                            'increasedPowderCharge', 'improvedControls', 'enhancedAimDrives',
                            'improvedVentilation', 'toolbox', 'perfectEquipment',
                            'equipmentStorageSystem', 'improvedPitchLimitsSystem',
                            'recycledEngineSpeed', 'multiLayerArmor', 'reinforcedCaterpillars',
                            'protectedModules', 'improvedBattleCompartment'))


def battle_effect_devices(devices):
    """Only reviewed device effects may influence authoritative simulation."""
    return tuple(tag for tag in devices if tag in BATTLE_DEVICES)


_ARMORED = ('hull', 'chassis', 'turret', 'gun', 'engine', 'fuel', 'radio')


def _homogenized(definition, factor):
    """Components with each useArmorHomogenization material's thickness * factor.

    Only thickness changes. Flag False (tracks) keeps the material; an unknown
    flag on nonzero armor raises instead of guessing eligibility.
    """
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError('Invalid armor homogenization factor')
    def scaled(material):
        if not material.use_armor_homogenization:
            return material
        thickness = material.thickness*factor
        if not math.isfinite(thickness) or thickness < 0:
            raise ValueError('Invalid derived armor thickness: ' + material.name)
        return replace(material, thickness=thickness)
    result = {}
    for slot in _ARMORED:
        component = getattr(definition, slot)
        for material in component.armor:
            if material.use_armor_homogenization is None and material.thickness:
                raise ValueError(f'Unknown armor homogenization flag: {component.name}/{material.name}')
        result[slot] = replace(component, armor=tuple(scaled(m) for m in component.armor))
    return result


def _loadout_mapping(loadout):
    if loadout is not None and not isinstance(loadout, Mapping):
        raise ValueError('Loadout must be a mapping')
    return {} if loadout is None else loadout


def _slot_tags(values, what, size=None):
    """Snapshot a slot sequence as a tuple of None/non-empty str; never raises TypeError."""
    if not isinstance(values, (list, tuple)) or (size is not None and len(values) != size):
        raise ValueError(f'Invalid {what} slots')
    if any(tag is not None and (type(tag) is not str or not tag) for tag in values):
        raise ValueError(f'Invalid {what} tag')
    chosen = [tag for tag in values if tag is not None]
    if len(set(chosen)) != len(chosen):
        raise ValueError(f'Duplicate {what} tag')
    return tuple(values)


def validate_devices(game, definition, loadout):
    """Revalidate selected optional devices against this vehicle's exact preset.

    Returns the selected tags in slot order. A loadout without a ``devices`` key
    (sparse internal timer/consumable use) selects nothing and needs no ``unlocked``.
    Selected devices need 9 slots, an integer (not bool) 9-bit ``unlocked`` mask with
    the slot bit set, a tag offered by that exact preset slot and a COMPATIBLE verdict.
    """
    loadout = _loadout_mapping(loadout)
    if 'devices' not in loadout:
        return ()
    tags = _slot_tags(loadout['devices'], 'device')
    if 'unlocked' in loadout:
        mask = loadout['unlocked']
        if type(mask) is not int or not 0 <= mask < 512:
            raise ValueError('Invalid unlocked device mask')
    elif any(tags):
        raise ValueError('Selected devices need an unlocked mask')
    else:
        mask = 0
    if not any(tags):
        return ()
    if len(tags) != 9:
        raise ValueError('Invalid device slots')
    catalog = load_catalog(game)
    by_tag = {item.tag: item for item in catalog.optional_devices}
    preset = catalog.equipment_slots(catalog.vehicle_preset(definition.nation, definition.name))
    for slot, tag in enumerate(tags):
        if tag is None:
            continue
        item = by_tag.get(tag)
        if item is None:
            raise ValueError(f'Unknown {OPTIONAL_DEVICE} tag: {tag!r}')
        if not mask >> slot & 1:
            raise ValueError('Device slot is locked: ' + tag)
        if item.ident not in preset[slot]:
            raise ValueError('Device does not belong to this vehicle slot: ' + tag)
        if catalog.compatibility(OPTIONAL_DEVICE, item.ident, definition.nation, definition.name,
                                 clip=definition.weapon.clip) != COMPATIBLE:
            raise ValueError('Device is incompatible or has an unsupported vehicle filter: ' + tag)
    return tuple(tag for tag in tags if tag)


def resolve_provisions(game, definition, loadout):
    """Validate saved food again at battle entry, including unused slot positions."""
    catalog = load_catalog(game)
    tags = _slot_tags(_loadout_mapping(loadout).get('provisions', (None, None, None)), 'provision', 3)
    _, slots = catalog.vehicle_slots(definition.nation, definition.name)
    if any(tag is not None for tag in tags[slots:]):
        raise ValueError('This vehicle has fewer provision slots')
    by_tag = {item.tag: item for item in catalog.provisions}
    result = []
    for tag in tags[:slots]:
        if tag is None:
            result.append(None)
            continue
        item = by_tag.get(tag)
        if item is None:
            raise ValueError('Provision effect is not implemented yet: ' + tag)
        if (item.tag, item.ident, item.script) in (('safety_set', 22, 'SafetySet'),
                                                   ('desertPower', 113, 'AttributesModifier')):
            # The catalog entry is installable, but its effects are not yet modelled.
            pass
        elif _gear_oil(item) is not None:
            pass
        elif (fuel := _fuel(item)):
            if not any(fuel):
                raise ValueError('Invalid fuel bonus: ' + tag)
        elif _hp_stock(item) is not None:
            pass
        elif _gun_powder(item) is not None:
            pass
        elif _anti_he(item) is not None:
            pass
        elif (item.script != 'Stimulator' or
                tuple(k for k,_ in item.parameters) != ('bonusValues/crewLevelIncrease',)):
            raise ValueError('Provision effect is not implemented yet: ' + tag)
        elif _number(item.parameters[0][1], tag) <= 0:
            raise ValueError('Invalid crew bonus: ' + tag)
        if catalog.provision_compatibility(item.ident, definition.nation, definition.name,
                                           clip=definition.weapon.clip) != COMPATIBLE:
            raise ValueError('Provision is incompatible or has an unsupported vehicle filter: ' + tag)
        result.append(item)
    return tuple(result)


def apply_definition(game, definition, loadout, *, crew_specs=None, crew_bindings=None):
    """Derive one immutable battle configuration, preserving the native descriptor.

    maxHP follows the pinned client Lobby arithmetic (_max_health): device
    and HP-stock percents add in float32, then roundf(f32(hull+turret)*factor).
    This proves client values, not the hidden official server.
    health_bonus is derived minus raw HP, so health, armor and spawn agree.
    Unsupported armor/module/fire effects are not silently accepted;
    timed consumables are validated here and simulated by the arena.
    Current accounts have mastery100. Absolute native crew factors calibrate
    raw XML parameters even with no equipment; native garage values confirm
    M41/cola reload6.5/1.0903 and turret48*1.0903 degrees/sec.
    Toolbox scales only track regen_per_sec (native repair time divides
    hysteresis by regen*factor); HP thresholds and repair kits are unchanged.
    Consumable timer devices change only resolve_loadout specs, not the definition.
    Pitch limits device (native 0x8698c0, 0x7ec560/0x7ec5c0): pitch pair
    (min,max) gets (-upper,+lower); front and rear sectors (0x70e205/0x70e213)
    get only -upper on their first component. Rates, sector high/arc and
    blend stay; no 90 degree clamp.
    Recycled engine adds its percent to fuel's enginePowerIncrease sum, so it
    scales only the existing kinematic engine power path (M41 500hp -> 530).
    Gear oils add enginePowerIncrease to that same sum; their km/h speed biases
    sum and are applied once (/3.6) to drive.forward/reverse. Caps do not scale
    with engine percent. This is the XML sandbox rule, not recovered native runtime.
    Multilayer armor (raw110) multiplies the nominal thickness of every material
    whose useArmorHomogenization is true by 1+percent/100, once, in cloned
    components; gun armor_N follows its common armor_N flag, tracks stay. The
    native UI applies this factor (TankVisual+18) before AP/HE thickness rules;
    using it for server hits is a local rule. Mass, HP and damage factors stay.
    Spall liner (provision101) only copies its factor and FX duration into the
    definition; the arena scales the target's final HE HP damage (local rule).
    Enhanced Tracks (raw112) has only a modules list and no numeric percent; it
    sets TrackSpec.restore_full_on_repair (LOCAL rule from the destroyed-tracks
    description clause, not official behaviour). HP thresholds, regen rate and
    device damage stay; only a broken track reaching hysteresis jumps to max.
    Protected Modules (raw108) scales only both track health thresholds once.
    Its ramming reduction applies to opt-in ram HP damage. Improved
    Battle Compartment (raw109) carries internal projectile chance factors;
    Arena applies them to independent live entity specs.
    """
    if (definition.health_bonus or definition.optional_devices or definition.provisions
            or definition.crew_mastery is not None or definition.he_damage_factor != 1
            or not definition.module_protection.identity
            or (definition.track_spec is not None and definition.track_spec.restore_full_on_repair)):
        raise ValueError('Loadout effects have already been applied')
    if loadout is None:
        loadout = dict(devices=(None,)*9, unlocked=0, provisions=(None,)*3, consumables=(None,)*3)
    elif not isinstance(loadout, Mapping) or any(
            key not in loadout for key in ('devices', 'unlocked', 'provisions', 'consumables')):
        raise ValueError('Loadout must contain devices, unlocked, provisions and consumables')
    if not isinstance(loadout['devices'], (list, tuple)) or len(loadout['devices']) != 9:
        raise ValueError('Invalid device slots')
    # Snapshot once so later mutation of the caller's lists cannot change what was validated.
    loadout = {key: tuple(value) if isinstance(value, list) else value for key, value in loadout.items()}
    devices = validate_devices(game, definition, loadout)
    provisions = tuple(tag for tag in _slot_tags(loadout['provisions'], 'provision', 3) if tag)
    from .consumables import resolve_loadout
    resolve_loadout(game, definition, loadout, crew_specs=crew_specs, crew_bindings=crew_bindings)
    resolved = resolve_provisions(game, definition, loadout)
    # Keep every valid selection on the vehicle, even when its battle effect
    # has not been implemented. Only pass reviewed devices to the compositor:
    # some unimplemented variants share scripts with supported devices but have
    # different XML shapes and must not accidentally acquire a partial effect.
    e = compose(game, battle_effect_devices(devices), provisions)
    track_spec = definition.track_spec
    if 'toolbox' in e.covered:
        if track_spec is None:
            raise ValueError('Toolbox requires track metadata')
        track_spec = replace(track_spec, regen_per_sec=track_spec.regen_per_sec*e.repair_speed_factor)
    if e.track_restore_full_on_repair:
        if track_spec is None:
            raise ValueError('Enhanced tracks require track metadata')
        track_spec = replace(track_spec, restore_full_on_repair=True)
    if e.track_health_factor != 1:
        if track_spec is None:
            raise ValueError('Protected Modules requires track metadata')
        track_spec = replace(track_spec,
                             max_health=track_spec.max_health*e.track_health_factor,
                             max_regen_health=track_spec.max_regen_health*e.track_health_factor,
                             hysteresis_health=track_spec.hysteresis_health*e.track_health_factor)
    from .modules import ModuleProtection
    protection = ModuleProtection(e.track_health_factor, e.ramming_absorption_percent,
                                  e.engine_chance_factor, e.ammo_rack_chance_factor,
                                  e.crew_chance_factor)
    from .crew import crew_role_factor
    from .vehicle_data import load_crew
    members = load_crew(game, definition.nation, definition.name)
    loader, gunner, driver = (crew_role_factor(members, role, bonus=e.crew_level_increase)
                              for role in ('loader','gunner','driver'))
    factors = {'ARMOR_PIERCING': e.ap_piercing_factor, 'ARMOR_PIERCING_CR': e.apcr_piercing_factor,
               'HOLLOW_CHARGE': e.heat_piercing_factor, 'HIGH_EXPLOSIVE': e.he_piercing_factor}
    shells = []
    for shell in definition.weapon.shells:
        first, last = shell.penetration
        factor = factors[shell.kind]
        last = first - (first-last) * (1+e.piercing_penalty_500m_percent/100)
        shells.append(replace(shell, penetration=(first*factor, last*factor),
                              speed=shell.speed*e.projectile_speed_factor))
    aiming = definition.aiming
    if 'improvedPitchLimitsSystem' in e.covered:
        upper = math.radians(e.upper_pitch_limit_increase)
        lower = math.radians(e.lower_pitch_limit_increase)
        low, high = aiming.pitch_limits
        packed_low, packed_high = aiming.packed_pitch_limits
        def sector(limits):
            return None if limits is None else (limits[0]-upper, *limits[1:])
        aiming = replace(aiming, pitch_limits=(low-upper, high+lower),
                         rear_limits=sector(aiming.rear_limits), front_limits=sector(aiming.front_limits),
                         packed_limits=(packed_low-upper, packed_high+lower))
    catalog = load_catalog(game)
    by_tag = {item.tag: item for item in catalog.optional_devices}
    hp_percents = ((e.max_hp_bonus_percent,) if 'highQualityAssembly' in e.covered else ()) + e.hp_stock_percents
    armored = _homogenized(definition, e.armor_homogenization_factor) if 'multiLayerArmor' in e.covered else {}
    return replace(definition, **armored,
        health_bonus=_max_health(definition.health, hp_percents)-definition.health,
        optional_devices=tuple(by_tag[tag].ident for tag in devices),
        provisions=tuple(item.ident for item in resolved if item),
        crew_mastery=100,
        track_spec=track_spec,
        he_damage_factor=e.he_damage_factor,
        he_effect_duration=e.he_effect_duration,
        module_protection=protection,
        # Pump refill seconds take the same rammer/loader factor once each;
        # the intershot stays raw, as for magazine guns.
        weapon=replace(definition.weapon, reload=definition.weapon.reload*e.reload_time_factor/loader,
                       pump_reload_times=tuple(t*e.reload_time_factor/loader
                                               for t in definition.weapon.pump_reload_times),
                       shells=tuple(shells)),
        drive=replace(definition.drive,
                      rotation=definition.drive.rotation*e.rotation_speed_factor*driver*e.engine_power_factor,
                      engine_power_hp=definition.drive.engine_power_hp*e.engine_power_factor,
                      power_bonus_factor=e.engine_power_factor,
                      forward=definition.drive.forward + e.forward_speed_bias_kmh/3.6,
                      reverse=definition.drive.reverse + e.reverse_speed_bias_kmh/3.6,
                      terrain_resistance=tuple(v/driver for v in definition.drive.terrain_resistance)),
        aiming=replace(aiming, yaw_rate=aiming.yaw_rate*gunner*e.turret_rotation_factor,
                       aiming_time=aiming.aiming_time*e.aiming_time_factor/gunner))
