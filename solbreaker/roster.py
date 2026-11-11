"""Sandbox training-room limits shared by membership and simulation."""
import math

TEAMS = (1, 2)
TEAM_SIZE = 7
ROOM_CAPACITY = 20
WAITING = 17


def validate_teams(teams):
    if (not 2 <= len(teams) <= 2*TEAM_SIZE
            or any(type(t) is not int or t not in TEAMS for t in teams)
            or any(not 1 <= teams.count(t) <= TEAM_SIZE for t in TEAMS)):
        raise ValueError('A shared battle requires 1 to 7 participants on each team')


def mines_spawn(team, slot):
    """Local training layout; slot zero preserves the verified 1v1 positions.

    These are sandbox positions, not recovered official server spawn data.
    Ground alignment and obstruction checks use the pinned map resources.
    """
    if team not in TEAMS or not 0 <= slot < TEAM_SIZE:
        raise ValueError('Invalid spawn slot')
    x,depth = ((0,0),(-8,0),(8,0),(-16,0),(16,0),(-4,8),(4,8))[slot]
    if team == 2 and slot == 6:
        # Move this one local slot clear of tree_k1.sc2 on Rudniki.
        x = 12
    return 99.384636+x, (-190-depth if team==1 else -160-depth)


def training_spawn(map_spec, team, slot):
    """Select the checked local spawn for a map, preserving legacy callers."""
    if map_spec is None:
        return mines_spawn(team, slot)
    return map_spec.spawn(team, slot)


def training_yaw(map_spec, team, slot):
    """Face a map spawn toward the opposing side; retain the Mines fallback."""
    if map_spec is None:
        if team not in TEAMS:
            raise ValueError('Invalid spawn team')
        return 0. if team == 1 else -math.pi
    return map_spec.yaw(team, slot)
