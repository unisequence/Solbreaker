"""Build recipient-scoped experimental recon snapshots without transport.

The caller supplies a checked roster from the current arena. The v1 wire
format does not include last-known coordinates, source of a marker, or an
expiry; this builder must not pretend to provide those missing facts.
"""
from dataclasses import dataclass
import math

from .recon import ReconState
from .recon_wire import ReconSnapshot
from .roster import TEAM_SIZE, TEAMS


@dataclass(frozen=True)
class RosterVehicle:
    entity_id: int
    team: int
    alive: bool

    def __post_init__(self):
        if type(self.entity_id) is not int or not 1 <= self.entity_id <= 0xffffffff:
            raise ValueError('Invalid roster entity ID')
        if type(self.team) is not int or self.team not in TEAMS:
            raise ValueError('Invalid roster team')
        if type(self.alive) is not bool:
            raise ValueError('Invalid roster alive state')


def build_recon_snapshot(state, roster, observer_id, *, battle_id, sequence, now):
    """Return a complete wire-v1 snapshot; does not mutate ``state``.

    Roster includes the observer, both allies and enemies, even if dead. It
    must be captured consistently with the state and current simulation time.
    A dead observer receives an empty snapshot with its lamp off.
    """
    if not isinstance(state, ReconState):
        raise ValueError('Invalid recon state')
    if not isinstance(roster, (tuple, list)) or not 1 <= len(roster) <= TEAM_SIZE*len(TEAMS):
        raise ValueError('Invalid roster')
    if any(not isinstance(vehicle, RosterVehicle) for vehicle in roster):
        raise ValueError('Invalid roster vehicle')
    ids = [vehicle.entity_id for vehicle in roster]
    if len(set(ids)) != len(ids) or any(sum(v.team == team for v in roster) > TEAM_SIZE for team in TEAMS):
        raise ValueError('Duplicate entity or oversized team')
    if type(observer_id) is not int or observer_id not in ids:
        raise ValueError('Observer not in roster')
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError('Invalid simulation time')
    observer = roster[ids.index(observer_id)]
    markers = tuple(sorted(vehicle.entity_id for vehicle in roster
        if observer.alive and vehicle.alive and vehicle.team != observer.team and
        state.visible(observer_id, observer.team, vehicle.entity_id, now)))
    lamp = observer.alive and state.alert_active(observer_id, now)
    return ReconSnapshot(battle_id, sequence, lamp, markers)
