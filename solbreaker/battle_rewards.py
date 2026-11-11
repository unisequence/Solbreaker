"""Local approximate battle reward policy.

Not the exact official formula. Structure follows game support articles
(credits: tier base, victory x3, damage; XP: victory x1.5; free XP: 5%);
all numeric coefficients are our own. Only already-accounted real enemy
damage counts. Kills add XP but no separate credits. No premium, boosters,
spotting, capture, costs or AFK penalties.
"""

from dataclasses import dataclass

INT32_MAX = 2**31 - 1
POLICY = 'solbreaker-rewards-v1'

CREDITS_PER_TIER = 500
CREDITS_PER_DAMAGE = 10
VICTORY_CREDITS_MULTIPLIER = 3
XP_PER_TIER = 10
XP_PER_KILL = 50
XP_SURVIVED = 25
DAMAGE_XP_NUMERATOR = 3
DAMAGE_XP_TIER_OFFSET = 2
MIN_TIER = 1
MAX_TIER = 10


@dataclass(frozen=True)
class BattleReward:
    credits: int
    vehicle_xp: int
    free_xp: int
    damage_xp: int
    other_xp: int
    policy: str = POLICY
    # UI-only decomposition before/after the existing victory multiplier.
    base_xp: int = 0
    damage_base_xp: int = 0
    other_base_xp: int = 0
    victory_xp: int = 0


def _int_field(name, value, minimum):
    if type(value) is not int:
        raise TypeError(f'{name} must be int, got {type(value).__name__}')
    if value < minimum:
        raise ValueError(f'{name} must be >= {minimum}')
    return value


def _bool_field(name, value):
    if type(value) is not bool:
        raise TypeError(f'{name} must be bool, got {type(value).__name__}')
    return value


def calculate_rewards(*, won, damage, tier=1, kills=0, survived=False):
    won = _bool_field('won', won)
    survived = _bool_field('survived', survived)
    tier = _int_field('tier', tier, MIN_TIER)
    if tier > MAX_TIER:
        raise ValueError(f'tier must be <= {MAX_TIER}')
    damage = _int_field('damage', damage, 0)
    kills = _int_field('kills', kills, 0)

    credits = CREDITS_PER_TIER * tier * (VICTORY_CREDITS_MULTIPLIER if won else 1)
    credits += CREDITS_PER_DAMAGE * damage

    damage_base = damage * DAMAGE_XP_NUMERATOR // (tier + DAMAGE_XP_TIER_OFFSET)
    other_base = XP_PER_TIER * tier + XP_PER_KILL * kills
    if survived:
        other_base += XP_SURVIVED
    total_base = damage_base + other_base

    if won:
        xp = total_base * 3 // 2
        damage_xp = damage_base * 3 // 2
    else:
        xp = total_base
        damage_xp = damage_base

    xp = min(xp, INT32_MAX)
    damage_xp = min(damage_xp, xp)
    other_xp = xp - damage_xp
    return BattleReward(
        credits=min(credits, INT32_MAX),
        vehicle_xp=xp,
        free_xp=xp // 20,
        damage_xp=damage_xp,
        other_xp=other_xp,
        base_xp=min(total_base, xp),
        damage_base_xp=min(damage_base, total_base, xp),
        other_base_xp=min(total_base, xp)-min(damage_base, total_base, xp),
        victory_xp=xp-min(total_base, xp),
    )
