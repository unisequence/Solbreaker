"""Pure crew role factor/ratio helpers (standard library only)."""

from math import fsum, isfinite

KNOWN_ROLES = ("commander", "gunner", "driver", "loader", "radioman")
_COMMANDER = "commander"
_BASE = 0.57
_SPAN = 0.43


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a real number" % name)
    try:
        value = float(value)
    except OverflowError:
        raise ValueError("%s must be finite" % name) from None
    if not isfinite(value):
        raise ValueError("%s must be finite" % name)
    return value


def _holders(members, role, mastery, bonus):
    mastery = _number(mastery, "mastery")
    bonus = _number(bonus, "bonus")
    if not 0.0 <= mastery <= 100.0:
        raise ValueError("mastery must be within 0..100")
    if bonus < 0.0:
        raise ValueError("bonus must be >= 0")
    if not isinstance(members, tuple) or not members:
        raise ValueError("members must be a nonempty tuple")
    layout = []
    for index, member in enumerate(members):
        if not isinstance(member, tuple) or len(member) != 2:
            raise ValueError("member %d must be a (primary, roles) tuple" % index)
        primary, roles = member
        if not isinstance(primary, str) or primary not in KNOWN_ROLES:
            raise ValueError("member %d has an unknown primary role" % index)
        if not isinstance(roles, tuple) or not roles:
            raise ValueError("member %d roles must be a nonempty tuple" % index)
        seen = set()
        for held in roles:
            if not isinstance(held, str) or held not in KNOWN_ROLES:
                raise ValueError("member %d has an unknown role" % index)
            if held in seen:
                raise ValueError("member %d repeats role %r" % (index, held))
            seen.add(held)
        if primary not in seen:
            raise ValueError("member %d primary role missing from roles" % index)
        layout.append((primary, seen))
    if not isinstance(role, str) or role not in KNOWN_ROLES:
        raise ValueError("unknown requested role")
    holders = [(primary, seen) for primary, seen in layout if role in seen]
    if not holders:
        raise ValueError("no member holds the requested role")
    return holders, mastery, bonus


def _factor(holders, level):
    # Divide before summation so a finite mean does not overflow its total.
    value = fsum((_BASE + _SPAN * (level if primary == _COMMANDER else
                                   level + level / 10.0)) / len(holders)
                 for primary, _ in holders)
    if not isfinite(value) or value <= 0:
        raise ValueError("crew factor must be finite and positive")
    return value


def crew_role_factor(members, role, mastery=100, bonus=0):
    holders, mastery, bonus = _holders(members, role, mastery, bonus)
    return _factor(holders, (mastery + bonus) / 100.0)


def crew_role_ratio(members, role, mastery=100, bonus=0):
    holders, mastery, bonus = _holders(members, role, mastery, bonus)
    if bonus == 0.0:
        return 1.0
    value = _factor(holders, (mastery + bonus) / 100.0) / _factor(holders, mastery / 100.0)
    if not isfinite(value) or value <= 0:
        raise ValueError("crew ratio must be finite and positive")
    return value
