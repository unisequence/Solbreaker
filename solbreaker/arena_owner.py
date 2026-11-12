"""Opt-in process-owner contract used by BaseEndpoint.

One request can be outstanding. Timeout or process failure makes its outcome
unknown and permanently retires the owner; callers must not replay it blindly.
The caller supplies a spawn-pickleable factory implementing ArenaHandler.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import multiprocessing as mp
import pickle
import queue
import struct
import time
from typing import Callable, Literal, Protocol

from .entities import BattleResult
from .owner_channel import FramedChannel, configured_ipc

Operation = Literal['ready', 'control', 'aim', 'fire', 'leave', 'disconnect', 'step', 'motion', 'motion_light', 'motions', 'spawn', 'reconnect', 'flush', 'aim_entity', 'aim_entity_local', 'autoaim', 'autoaim_point', 'aim_angles', 'consumable', 'select_shell', 'recon_aim', 'recon_clear', 'recon_snapshot']
_LENGTHS = {'ready': 0, 'control': 2, 'aim': 12, 'fire': 0, 'leave': 0, 'disconnect': 0, 'step': 0, 'motion': 0, 'motion_light': 0, 'motions': None, 'spawn': 0, 'reconnect': 0, 'flush': 0, 'aim_entity': 16, 'aim_entity_local': 16, 'autoaim': 8, 'autoaim_point': 20, 'aim_angles': 8, 'consumable': None, 'select_shell': 5, 'recon_aim': 4, 'recon_clear': 0, 'recon_snapshot': 4}
_MAX_MESSAGE = 1024 * 1024
_TIMED = frozenset(('ready', 'control', 'fire', 'leave', 'disconnect', 'step',
                    'consumable', 'select_shell'))


class OwnerError(RuntimeError):
    """Owner unavailable, faulty, or a command has an unknown outcome."""


class CommandRejected(ValueError):
    """Preflight rejected without mutation; sequence is acknowledged."""
    def __init__(self, message: str, sequence: int | None = None):
        super().__init__(message)
        self.sequence = sequence


@dataclass(frozen=True, slots=True)
class Command:
    op: Operation
    at: float
    account_id: int
    payload: bytes = b''
    deadline: float | None = None

    def validate(self) -> None:
        if type(self.op) is not str or self.op not in _LENGTHS:
            raise ValueError('unsupported arena operation')
        if isinstance(self.at, bool) or not isinstance(self.at, (int, float)) or not math.isfinite(self.at):
            raise ValueError('invalid timestamp')
        if self.deadline is not None and (self.op != 'step' or isinstance(self.deadline, bool)
                or not isinstance(self.deadline, (int, float)) or not math.isfinite(self.deadline)
                or self.deadline < self.at):
            raise ValueError('invalid simulation deadline')
        if type(self.account_id) is not int or (self.op in ('step', 'motions') and self.account_id != 0) or (self.op not in ('step', 'motions') and self.account_id <= 0):
            raise ValueError('invalid account id')
        if type(self.payload) is not bytes or len(self.payload) > _MAX_MESSAGE:
            raise ValueError('invalid operation payload')
        expected = _LENGTHS[self.op]
        if expected is not None and len(self.payload) != expected:
            raise ValueError('invalid operation payload')
        if self.op == 'motions':
            if not self.payload or len(self.payload) % 4 or len(self.payload) > 64*4:
                raise ValueError('invalid motion recipients')
            recipients = struct.unpack(f'<{len(self.payload)//4}I', self.payload)
            if not all(recipients) or len(set(recipients)) != len(recipients):
                raise ValueError('invalid motion recipients')
        if self.op == 'consumable':
            from .mercury import read_blob
            if len(self.payload) < 7:
                raise ValueError('invalid consumable payload')
            _, end = read_blob(self.payload, 6)
            if end != len(self.payload):
                raise ValueError('trailing consumable payload')
        if self.op in ('aim_entity','aim_entity_local') and not all(math.isfinite(v) for v in struct.unpack('<I3f', self.payload)[1:]):
            raise ValueError('nonfinite entity aim')
        if self.op == 'aim' and not all(math.isfinite(v) for v in struct.unpack('<3f', self.payload)):
            raise ValueError('nonfinite aim')
        if self.op == 'autoaim_point' and not all(math.isfinite(v) for v in struct.unpack('<II3f', self.payload)[2:]):
            raise ValueError('nonfinite autoaim point')
        if self.op == 'aim_angles' and not all(math.isfinite(v) for v in struct.unpack('<2f', self.payload)):
            raise ValueError('nonfinite aim angles')


@dataclass(frozen=True, slots=True)
class AddressedPayload:
    account_id: int
    body: bytes


@dataclass(frozen=True, slots=True)
class MotionPayload:
    account_id: int
    tick: int | None
    body: bytes


@dataclass(frozen=True, slots=True)
class SettlementRecord:
    result: BattleResult
    report: dict
    descriptor: bytes
    loadout_shells: tuple | None
    ammo_by_shell: tuple
    shell_id: int
    ammo: int


@dataclass(frozen=True, slots=True)
class SettlementData:
    arena_id: str
    results: tuple[BattleResult, ...]
    records: tuple[SettlementRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class SpawnData:
    arena_id: str
    account_id: int
    epoch: float
    initial: bytes
    batches: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class HandlerOutput:
    arena_id: str
    state: bytes
    events: tuple[AddressedPayload, ...]
    motion: MotionPayload | None = None
    settlement: SettlementData | None = None
    spawn: SpawnData | None = None
    recon: bytes | None = None
    motions: tuple[MotionPayload, ...] = ()


@dataclass(frozen=True, slots=True)
class Result:
    sequence: int
    state: bytes
    events: tuple[AddressedPayload, ...]
    arena_id: str = ''
    motion: MotionPayload | None = None
    settlement: SettlementData | None = None
    spawn: SpawnData | None = None
    recon: bytes | None = None
    motions: tuple[MotionPayload, ...] = ()


class ArenaHandler(Protocol):
    def apply(self, command: Command) -> HandlerOutput | tuple[bytes, tuple[AddressedPayload, ...]]: ...
    def close(self) -> None: ...


def _freeze(value: object) -> bytes:
    data = pickle.dumps(value, protocol=5)
    if len(data) > _MAX_MESSAGE:
        raise ValueError('owner message exceeds limit')
    return data


def _worker(factory: Callable[[], ArenaHandler], incoming, outgoing) -> None:
    handler = None
    sequence = 0
    last_at = None
    try:
        if isinstance(incoming,FramedChannel):
            incoming.close_sender()
        if isinstance(outgoing,FramedChannel):
            outgoing.close_receiver()
        handler = factory()
        metrics = getattr(handler, 'startup_metrics', None)
        outgoing.put(_freeze(('ready', metrics) if metrics else ('ready',)), timeout=1)
        while True:
            raw = incoming.get()
            if raw is None:
                break
            requested, command = pickle.loads(raw)
            if type(requested) is not int or requested != sequence + 1 or type(command) is not Command:
                raise ValueError('invalid sequence or command')
            command.validate()
            if command.op in _TIMED and last_at is not None and command.at < last_at:
                raise ValueError(f'timestamp moved backwards: {command.op} {command.at} < {last_at}')
            # Only explicit rejection from preflight is safe to recover from.
            # Any exception once apply starts quarantines the owner.
            preflight = getattr(handler, 'preflight', None)
            if preflight is not None:
                try:
                    preflight(command)
                except CommandRejected as exc:
                    sequence = requested
                    outgoing.put(_freeze(('rejected', sequence, str(exc)[:512])), timeout=1)
                    continue
            output = handler.apply(command)
            if type(output) is HandlerOutput:
                if type(output.arena_id) is not str or not output.arena_id:
                    raise ValueError('invalid arena identity')
                state, events = output.state, output.events
                if output.motion is not None and (type(output.motion) is not MotionPayload or
                        type(output.motion.account_id) is not int or output.motion.account_id <= 0 or
                        (output.motion.tick is not None and type(output.motion.tick) is not int) or
                        type(output.motion.body) is not bytes):
                    raise ValueError('invalid motion payload')
                if (type(output.motions) is not tuple or len(output.motions) > 64
                        or any(type(item) is not MotionPayload or type(item.account_id) is not int
                               or item.account_id <= 0 or type(item.body) is not bytes
                               or (item.tick is not None and type(item.tick) is not int)
                               for item in output.motions)):
                    raise ValueError('invalid motion batch')
                if output.settlement is not None and (type(output.settlement) is not SettlementData or
                        output.settlement.arena_id != output.arena_id or type(output.settlement.results) is not tuple or
                        any(type(item) is not BattleResult or item.result_id != output.arena_id for item in output.settlement.results) or
                        type(output.settlement.records) is not tuple or
                        any(type(item) is not SettlementRecord or type(item.result) is not BattleResult or item.result.result_id != output.arena_id for item in output.settlement.records) or
                        (output.settlement.records and tuple(item.result for item in output.settlement.records) != output.settlement.results)):
                    raise ValueError('invalid settlement data')
                if output.spawn is not None and (type(output.spawn) is not SpawnData or
                        output.spawn.arena_id != output.arena_id or type(output.spawn.account_id) is not int or
                        output.spawn.account_id != command.account_id or command.op not in ('spawn', 'reconnect') or isinstance(output.spawn.epoch, bool) or
                        not isinstance(output.spawn.epoch, (int, float)) or not math.isfinite(output.spawn.epoch) or
                        type(output.spawn.initial) is not bytes or type(output.spawn.batches) is not tuple or
                        any(type(part) is not bytes for part in output.spawn.batches)):
                    raise ValueError('invalid spawn data')
                if output.recon is not None and type(output.recon) is not bytes:
                    raise ValueError('invalid recon payload')
            else:
                state, events = output
            if type(state) is not bytes or type(events) is not tuple:
                raise ValueError('invalid handler result')
            for event in events:
                if type(event) is not AddressedPayload or type(event.account_id) is not int or event.account_id <= 0 or type(event.body) is not bytes:
                    raise ValueError('invalid addressed event')
            sequence = requested
            if command.op in _TIMED:
                last_at = command.at
            outgoing.put(_freeze(('result', Result(sequence, state, events, output.arena_id, output.motion, output.settlement, output.spawn, output.recon, output.motions) if type(output) is HandlerOutput else Result(sequence, state, events))), timeout=1)
    except BaseException as exc:
        try:
            outgoing.put(_freeze(('error', type(exc).__name__, str(exc)[:512])), timeout=0.2)
        except BaseException:
            pass
    finally:
        if handler is not None:
            try:
                handler.close()
            except BaseException:
                pass
        for channel in (incoming,outgoing):
            if isinstance(channel,FramedChannel):
                channel.close()


class ArenaOwner:
    """Synchronous isolated owner; methods must be called from one thread only."""
    def __init__(self, factory: Callable[[], ArenaHandler], *, queue_size: int = 4,
                 timeout: float = 5.0, startup_timeout: float | None = None, ipc: str | None = None):
        if type(queue_size) is not int or not 1 <= queue_size <= 64 or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('invalid owner limits')
        if startup_timeout is not None and (not math.isfinite(startup_timeout) or startup_timeout <= 0):
            raise ValueError('invalid owner startup timeout')
        ipc = configured_ipc(ipc)
        _freeze(factory)  # fail synchronously before starting a child
        self._ctx = mp.get_context('spawn')
        self.ipc_backend = ipc
        self._in = (FramedChannel(max_message=_MAX_MESSAGE) if ipc=='socket'
                    else self._ctx.Queue(maxsize=queue_size))
        self._out = (FramedChannel(max_message=_MAX_MESSAGE) if ipc=='socket'
                     else self._ctx.Queue(maxsize=queue_size))
        self._process = self._ctx.Process(target=_worker, args=(factory, self._in, self._out))
        self._timeout = timeout
        self._seq = 0
        self._last_at = None
        self._closed = False
        try:
            self._process.start()
            if ipc=='socket':
                self._in.close_receiver()
                self._out.close_sender()
        except BaseException:
            for q in (self._in, self._out):
                q.cancel_join_thread()
                q.close()
            raise
        try:
            reply = self._receive(timeout=startup_timeout)
            if reply == ('ready',):
                self.startup_metrics = {}
            elif type(reply) is tuple and len(reply) == 2 and reply[0] == 'ready' and type(reply[1]) is dict:
                self.startup_metrics = reply[1]
            else:
                raise OwnerError('owner failed to initialize')
        except BaseException:
            self.close()
            raise

    def _receive(self, *, timeout=None):
        deadline = time.monotonic() + (self._timeout if timeout is None else timeout)
        while True:
            try:
                return pickle.loads(self._out.get(timeout=min(.1, max(0., deadline-time.monotonic()))))
            except queue.Empty as exc:
                if not self._process.is_alive():
                    try:
                        return pickle.loads(self._out.get_nowait())
                    except queue.Empty:
                        raise OwnerError('owner timed out or exited; command outcome unknown') from exc
                if time.monotonic() >= deadline:
                    raise OwnerError('owner timed out or exited; command outcome unknown') from exc
            except (EOFError,OSError) as exc:
                raise OwnerError('owner channel failed; command outcome unknown') from exc

    def apply(self, command: Command) -> Result:
        if self._closed:
            raise OwnerError('owner is closed')
        if type(command) is not Command:
            raise ValueError('unsupported message')
        command.validate()
        if command.op in _TIMED and self._last_at is not None and command.at < self._last_at:
            raise ValueError(f'timestamp moved backwards: {command.op} {command.at} < {self._last_at}')
        sequence = self._seq + 1
        raw = _freeze((sequence, command))
        try:
            self._in.put(raw, timeout=self._timeout)
            reply = self._receive()
            if (isinstance(reply, tuple) and len(reply) == 3 and reply[0] == 'rejected'
                    and reply[1] == sequence and type(reply[2]) is str):
                self._seq = sequence
                raise CommandRejected(reply[2], sequence)
            if not isinstance(reply, tuple) or len(reply) != 2 or reply[0] != 'result' or type(reply[1]) is not Result or reply[1].sequence != sequence:
                raise OwnerError(f'owner failed; command outcome unknown: {reply!r}')
        except CommandRejected:
            raise
        except (queue.Full,EOFError,OSError) as exc:
            self.close()
            raise OwnerError('owner channel failed; command outcome unknown') from exc
        except BaseException:
            self.close()
            raise
        self._seq = sequence
        if command.op in _TIMED:
            self._last_at = command.at
        return reply[1]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._in.put_nowait(None)
        except (queue.Full, ValueError, OSError):
            pass
        self._process.join(min(self._timeout, 1.0))
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(1.0)
        if self._process.is_alive():
            self._process.kill()
            self._process.join(1.0)
        for q in (self._in, self._out):
            q.cancel_join_thread()
            q.close()

    def __enter__(self) -> ArenaOwner:
        return self

    def __exit__(self, *args) -> None:
        self.close()


class AsyncArenaOwner:
    """Async adapter for ArenaOwner; one request at a time, no automatic retry.

    A cancelled apply retires the adapter immediately. Its thread may finish
    the command before bounded shutdown, so the cancelled outcome is unknown.
    Creation and close also run outside the event loop.
    """
    def __init__(self, owner: ArenaOwner):
        self._owner = owner
        self._closed = False
        self._pending = None
        self._cleanup = None

    @property
    def startup_metrics(self):
        return self._owner.startup_metrics

    @classmethod
    async def create(cls, factory: Callable[[], ArenaHandler], *, queue_size: int = 4,
                     timeout: float = 5.0, startup_timeout: float | None = None) -> AsyncArenaOwner:
        import asyncio
        task = asyncio.create_task(asyncio.to_thread(ArenaOwner, factory, queue_size=queue_size,
                                                     timeout=timeout, startup_timeout=startup_timeout))
        try:
            return cls(await asyncio.shield(task))
        except asyncio.CancelledError:
            async def cleanup_created():
                try:
                    owner = await task
                    await asyncio.to_thread(owner.close)
                except Exception:
                    pass
            asyncio.create_task(cleanup_created())
            raise

    def _begin_cleanup(self, pending=None):
        import asyncio
        self._closed = True
        if self._cleanup is None:
            async def cleanup():
                if pending is not None:
                    try:
                        await pending
                    except Exception:
                        pass
                await asyncio.to_thread(self._owner.close)
            self._cleanup = asyncio.create_task(cleanup())
        return self._cleanup

    async def apply(self, command: Command) -> Result:
        import asyncio
        if self._closed:
            raise OwnerError('owner is closed; command outcome may be unknown')
        if self._pending is not None:
            raise OwnerError('another command is outstanding')
        if self._owner.ipc_backend == 'socket':
            from .owner_async import apply
            task = asyncio.create_task(apply(self._owner, command))
        else:
            task = asyncio.create_task(asyncio.to_thread(self._owner.apply, command))
        self._pending = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self._begin_cleanup(task)
            raise
        except OwnerError:
            self._begin_cleanup(task)
            raise
        finally:
            self._pending = None

    async def close(self) -> None:
        import asyncio
        # A shared cleanup task survives cancellation of any individual waiter.
        # It also joins an in-flight command before touching the owner's queues.
        await asyncio.shield(self._begin_cleanup(self._pending))

    async def __aenter__(self) -> AsyncArenaOwner:
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()
