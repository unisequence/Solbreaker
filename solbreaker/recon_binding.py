"""In-memory, opt-in binding primitive for the experimental recon transport.

``issue_trusted`` must only be called by already trusted server code holding
the actual authenticated ClientSession. It does not authenticate a bridge
request or establish a channel to a particular client process. The owner must
revoke on every relevant session/battle transition, even if it is later
reversed between two reads. The server uses it only when the experimental
recon transport is enabled; it is not exposed over HTTP.
"""
from dataclasses import dataclass, field
import hashlib
import math
import secrets
import time
import uuid


class BindingUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class BoundBattle:
    client: object = field(repr=False)
    arena: object = field(repr=False)
    observer_id: int
    battle_id: uuid.UUID


@dataclass
class _Lease:
    client: object = field(repr=False)
    addr: object = field(repr=False)
    arena: object = field(repr=False)
    player: object = field(repr=False)
    battle_id: uuid.UUID
    account_id: int
    observer_id: int
    expires: float
    sequence: int = 0
    last_incoming: int = -1


class ReconBindings:
    """Limited lease registry for a single trusted server event loop.

    Raw secrets are returned once and never stored. This registry has no
    endpoint and does not make a token available to a client by itself.
    """
    def __init__(self, endpoint, *, clock=time.monotonic, capacity=64, ttl=30.):
        if (not callable(clock) or type(capacity) is not int or not 1 <= capacity <= 1024
                or isinstance(ttl, bool) or not isinstance(ttl, (int, float))
                or not math.isfinite(ttl) or not 0 < ttl <= 300):
            raise ValueError('Invalid binding limits')
        self._endpoint, self._clock, self._capacity, self._ttl = endpoint, clock, capacity, float(ttl)
        self._leases = {}

    def _now(self):
        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise BindingUnavailable('Invalid clock')
        return now

    @staticmethod
    def _digest(token):
        if not isinstance(token, str) or len(token) != 43 or not all(c.isascii() and (c.isalnum() or c in '-_') for c in token):
            raise BindingUnavailable('Invalid binding')
        return hashlib.sha256(token.encode('ascii')).digest()

    def _valid(self, lease):
        try:
            c, a = lease.client, lease.arena
            player = a.players.get(lease.account_id)
            v = a.vehicles.get(lease.observer_id)
            prebattle = not a.started and getattr(a, 'prebattle_seconds', 0.) > 0
            return (self._endpoint.clients.get(lease.addr) is c and
                    self._endpoint.arenas.get(c.arena_id) is a and
                    c.account_id == lease.account_id and c.arena_id == lease.battle_id.hex and
                    c.phase == 'battle' and c.account_created and c.battle_spawned and
                    not a.building and not a.closed and not a.retiring and not a.finished_at and
                    a.recon is not None and player is lease.player and player.attached and
                    (prebattle or a.started and player.loaded) and
                    player.vehicle_id == lease.observer_id and v is not None and v.alive and
                    v.account_id == lease.account_id and v.team == player.team)
        except (AttributeError, KeyError, TypeError):
            return False

    def issue_trusted(self, addr, client):
        """Issue only to an already authenticated, trusted caller; never to HTTP directly."""
        from .base import ClientSession
        now = self._now()
        if not isinstance(client, ClientSession) or self._endpoint.clients.get(addr) is not client:
            raise BindingUnavailable('No current session')
        try:
            arena = self._endpoint.arenas.get(client.arena_id)
            player = arena.players[client.account_id]
            battle_id = uuid.UUID(arena.result_id)
            lease = _Lease(client, addr, arena, player, battle_id, client.account_id,
                           player.vehicle_id, now + self._ttl)
        except (AttributeError, KeyError, TypeError, ValueError):
            raise BindingUnavailable('No current battle') from None
        if not self._valid(lease):
            raise BindingUnavailable('Battle is not ready')
        # Rotation revokes every earlier credential bound to this exact session.
        self.revoke_session(client)
        self._leases = {key: item for key, item in self._leases.items()
                        if item.expires > now and self._valid(item)}
        if len(self._leases) >= self._capacity:
            raise BindingUnavailable('Binding capacity reached')
        token = secrets.token_urlsafe(32)
        digest = self._digest(token)
        if digest in self._leases:
            raise BindingUnavailable('Binding collision')
        self._leases[digest] = lease
        return token

    def _lease(self, token):
        digest = self._digest(token)
        lease = self._leases.get(digest)
        if lease is None:
            raise BindingUnavailable('Invalid binding')
        if self._now() >= lease.expires or not self._valid(lease):
            del self._leases[digest]
            raise BindingUnavailable('Expired or invalid binding')
        return digest, lease

    def resolve(self, token):
        _, lease = self._lease(token)
        return BoundBattle(lease.client, lease.arena, lease.observer_id, lease.battle_id)

    def snapshot(self, token):
        """Build a recipient-scoped payload for a verified live battle lease.

        This is a producer only: the transport and capability negotiation must
        be established separately before any payload is sent to a client.
        Call from the owning server event loop, without yielding mid-snapshot.
        """
        from .recon_snapshot import RosterVehicle, build_recon_snapshot
        from .recon_wire import encode
        bound = self.resolve(token)
        arena = bound.arena
        roster = tuple(RosterVehicle(v.entity_id, v.team, v.alive)
                       for v in arena.vehicles.values())
        sequence = self.next_sequence(token)
        snapshot = build_recon_snapshot(arena.recon, roster, bound.observer_id,
                                        battle_id=bound.battle_id, sequence=sequence,
                                        now=arena.simulation_time)
        return encode(snapshot)

    def renew(self, token):
        """Extend a still-valid lease; expired or revoked leases cannot revive.

        The eventual transport must authenticate who requests renewal. A
        bearer capability alone does not identify the client process.
        """
        digest, lease = self._lease(token)
        now = self._now()
        if now >= lease.expires or not self._valid(lease):
            del self._leases[digest]
            raise BindingUnavailable('Expired or invalid binding')
        lease.expires = now + self._ttl

    def accept_incoming(self, token, sequence):
        """Accept a strictly increasing u32 message sequence for this lease.

        Gaps are allowed, while repeats, reordering and wrap are refused. This
        counter is independent of the server's outbound snapshot sequence.
        """
        if type(sequence) is not int or not 0 <= sequence <= 0xffffffff:
            raise BindingUnavailable('Invalid incoming sequence')
        _, lease = self._lease(token)
        if sequence <= lease.last_incoming:
            raise BindingUnavailable('Duplicate or stale incoming sequence')
        lease.last_incoming = sequence

    def next_sequence(self, token):
        digest, lease = self._lease(token)
        if lease.sequence > 0xffffffff:
            del self._leases[digest]
            raise BindingUnavailable('Sequence exhausted')
        sequence = lease.sequence
        lease.sequence += 1
        return sequence

    def revoke_session(self, client):
        self._leases = {key: value for key, value in self._leases.items() if value.client is not client}

    def revoke_all(self):
        self._leases.clear()
