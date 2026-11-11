"""Picklable contracts for speculative, read-only ground-drive workers.

The authority remains CollisionWorld. A reply is usable only after the owner
validates its map revision and complete contact-query footprint, in drive order.
No Bullet connection, body, RNG or account state crosses this boundary.
"""
from dataclasses import dataclass


# Missing entries are tombstones, rather than a request to keep an older cache.
DRIVE_STATE = (
    '_road_poses', '_support_memory', '_support_hints', '_support_bounds',
    '_spring_rates', '_lean_states', '_drive_blocked', '_carrying_supported',
    '_contact_support_cache', '_wall_recoil_latches', 'ground_commands',
    'ground_track_rates',
)


@dataclass(frozen=True)
class DriveJob:
    entity: int
    pose: object
    motion: object
    distance: float
    yaw_delta: float
    options: dict
    state: dict
    effort: tuple
    gun_angles: tuple


@dataclass(frozen=True)
class DriveReply:
    entity: int
    accepted: bool
    pose: object = None
    moved: bool = False
    motion: object = None
    state: dict = None
    # min_x, min_z, max_x, max_z; covers every actual contact query, not
    # merely the start/end poses of the proposed displacement.
    footprint: tuple = None
    diagnostics: tuple = None
    reason: str = ''


def capture_state(world, entity):
    return {name: getattr(world, name)[entity]
            for name in DRIVE_STATE if entity in getattr(world, name)}


def restore_state(world, entity, state):
    for name in DRIVE_STATE:
        values = getattr(world, name)
        if name in state:
            values[entity] = state[name]
        else:
            values.pop(entity, None)
