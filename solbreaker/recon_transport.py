"""Experimental, negotiated recon snapshot delivery for one BaseApp loop.

Only a live client explicitly sending the versioned sentinel receives this
extension. This is not a claim that the native client hook is implemented.
"""
from dataclasses import dataclass, field
import struct

from .mercury import variable8
from .recon_binding import BindingUnavailable, ReconBindings
from .recon_wire import MAGIC, decode

HELLO_PREFIX = b'TFS-LAB-H'
HELLO_SIZE = len(HELLO_PREFIX) + 5
AIM_PREFIX = b'TFS-LAB-A'
AIM_SIZE = len(AIM_PREFIX) + 13
METHOD = 96  # 59 + Avatar client method 37
POLL_SECONDS = .25
HEARTBEAT_SECONDS = 1.
MAX_RELIABLE_WINDOW = 200


def hello_epoch(body):
    """Recognise only the exact version-one capability announcement."""
    if not isinstance(body, bytes) or len(body) != HELLO_SIZE:
        return None
    if not body.startswith(HELLO_PREFIX) or body[-1] != 1:
        return None
    epoch = struct.unpack_from('<I', body, len(HELLO_PREFIX))[0]
    return epoch or None


def ack_message(epoch, battle_id):
    words = (*MAGIC, 1, epoch, *struct.unpack('<4I', battle_id.bytes))
    body = bytes([len(words)]) + struct.pack('<8I', *words) + b'\0'
    return b'\x13' + variable8(METHOD, body)


@dataclass
class _Delivery:
    client: object
    addr: object
    token: str = field(repr=False)
    epoch: int
    next_poll: float
    last_sent: float
    state: object = None
    last_aim: float | None = None


class ReconTransport:
    def __init__(self, endpoint, *, clock):
        self.endpoint = endpoint
        self.bindings = ReconBindings(endpoint, clock=clock)
        self._deliveries = {}

    def revoke(self, client):
        item = self._deliveries.get(id(client))
        if item is not None and item.client is client:
            try:
                bound = self.bindings.resolve(item.token)
            except BindingUnavailable:
                pass
            else:
                bound.arena.recon_client_clear(client.account_id)
            del self._deliveries[id(client)]
        self.bindings.revoke_session(client)

    def receive(self, addr, client, body, now):
        """Consume only exact versioned recon messages on the trusted session."""
        if self.hello(addr, client, body, now):
            return True
        if not isinstance(body, bytes) or len(body) != AIM_SIZE or not body.startswith(AIM_PREFIX) or body[-1] != 1:
            return False
        epoch, sequence, target = struct.unpack_from('<III', body, len(AIM_PREFIX))
        delivery = self._deliveries.get(id(client))
        if (delivery is None or delivery.client is not client or delivery.addr != addr
                or epoch != delivery.epoch):
            return True
        try:
            bound = self.bindings.resolve(delivery.token)
            self.bindings.accept_incoming(delivery.token, sequence)
        except BindingUnavailable:
            return True
        if delivery.last_aim is not None and now - delivery.last_aim >= .35:
            bound.arena.recon_aim(client.account_id, None)
        bound.arena.recon_client_aim(client.account_id, target or None)
        delivery.last_aim = now
        return True

    def hello(self, addr, client, body, now):
        """Consume recognised hellos even when the battle is not yet ready."""
        epoch = hello_epoch(body)
        if epoch is None:
            return False
        if client.reliable_window_used >= MAX_RELIABLE_WINDOW:
            return True
        existing = self._deliveries.get(id(client))
        if existing and existing.client is client and existing.addr == addr and existing.epoch == epoch:
            try:
                bound = self.bindings.resolve(existing.token)
            except BindingUnavailable:
                self.revoke(client)
            else:
                self.endpoint.send(addr, client, ack_message(epoch, bound.battle_id))
                return True
        try:
            token = self.bindings.issue_trusted(addr, client)
            bound = self.bindings.resolve(token)
        except BindingUnavailable:
            self.revoke(client)
            return True
        delivery = _Delivery(client, addr, token, epoch, now, now - HEARTBEAT_SECONDS)
        self._deliveries[id(client)] = delivery
        try:
            # Negotiation takes ownership immediately; approximate server
            # point targeting must not create marks before client reports.
            bound.arena.recon_client_aim(client.account_id, None)
            self.endpoint.send(addr, client, ack_message(epoch, bound.battle_id))
        except Exception:
            self.revoke(client)
            raise
        return True

    def tick_client(self, addr, client, now):
        delivery = self._deliveries.get(id(client))
        if not delivery or delivery.client is not client or delivery.addr != addr:
            return
        if now < delivery.next_poll:
            return
        delivery.next_poll = now + POLL_SECONDS
        try:
            self.bindings.renew(delivery.token)
            if client.reliable_window_used >= MAX_RELIABLE_WINDOW:
                return
            bound = self.bindings.resolve(delivery.token)
            if getattr(bound.arena, 'owner_backed', False):
                self.endpoint.owner_service.recon_poll(addr, client, delivery)
                return
            payload = self.bindings.snapshot(delivery.token)
            snapshot = decode(payload)
        except BindingUnavailable:
            self.revoke(client)
            return
        state = (snapshot.lamp, snapshot.markers)
        if state != delivery.state or now - delivery.last_sent >= HEARTBEAT_SECONDS:
            self.endpoint.send(addr, client, b'\x13' + variable8(METHOD, payload))
            delivery.state, delivery.last_sent = state, now
