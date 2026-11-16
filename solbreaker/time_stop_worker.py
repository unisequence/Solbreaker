"""Run a private time branch without waiting on physics from an arena tick."""
from dataclasses import dataclass
import multiprocessing
from queue import Empty, SimpleQueue
from threading import Thread
import time

from .collision import Pose
from .time_stop import (LEVELS, BurstState, EffectLedger, Level, MergeVisual, ProjectileTrace,
                        ReturnMotion, Snapshot, TimelineSimulation, prepare_world)


@dataclass(frozen=True)
class Step:
    flags: int = 0
    aim: tuple | None = None
    point: tuple | None = None
    actions: tuple = ()  # Ordered (action ID, name, arguments) commands.
    finish: bool = False
    lock: tuple | None = None
    missile_camera: tuple | None = None


@dataclass(frozen=True)
class Frame:
    branch: str
    seconds: float
    pose: Pose
    aim: tuple
    gun_packed: bytes
    engine_mode: tuple[int, int]
    energy: int
    ammo: int
    clip: int
    selected_shell: int
    next_shell: int
    ammo_by_shell: tuple
    gun_revision: int
    shots_fired: int
    shots: tuple
    marker: tuple | None
    reload_at: float
    reload_remaining: float
    reload_total: float
    reload_kind: str
    pump_at: float
    pump_remaining: float
    pump_total: float
    pump_waiting: bool
    projectiles: tuple
    shot_ends: tuple
    abilities_used: int
    last_ability: tuple | None
    ability_views: tuple
    heal_wave: tuple | None
    traps: tuple
    consumables: tuple
    aim_lock: tuple | None
    autoaim_lost: bool
    ledger: EffectLedger | None = None
    return_motion: ReturnMotion | None = None
    merge_visuals: tuple[MergeVisual, ...] = ()
    burst: BurstState | None = None
    shot_age: float | None = None
    movement_dispersion: float = 1.


@dataclass(frozen=True)
class Failure:
    reason: str


@dataclass(frozen=True)
class Prepared:
    """The Bullet world is ready to receive its activation snapshot."""


@dataclass(frozen=True)
class Prepare:
    """Build the next private world with the geometry already in the worker."""


@dataclass(frozen=True)
class Activate:
    snapshot: Snapshot
    level: Level
    wall_origin: float


def _frame(branch):
    views = []
    for slot in branch.consumables.values():
        view = slot.state_at(branch.time, cooldown_multiplier=branch.level.cooldown_multiplier)
        views.append((slot.spec.compact_id, view.state, view.remaining, view.total, slot.uses))
    shot_ends = tuple(branch.shot_ends)
    branch.shot_ends.clear()
    shots = tuple(branch.shots)
    branch.shots.clear()
    autoaim_lost, branch.autoaim_lost = branch.autoaim_lost, False
    return_motion = None
    if branch.ended and not branch.ledger.aborted:
        world, entity = branch.world, branch.entity
        spin = world.flight_spin(entity) if entity in world.dynamic_bodies else None
        return_motion = ReturnMotion(world.motion[entity], world._road_poses[entity],
                                     world._lean_states.get(entity), spin, branch.ground_motion)
    return Frame(
        branch=branch.snapshot.ident, seconds=branch.time, pose=branch.pose,
        aim=branch.world.gun_angles[branch.entity], gun_packed=branch.gun.packed(),
        engine_mode=branch.engine_mode, energy=branch.ledger.energy,
        ammo=branch.ammo, clip=branch.clip, selected_shell=branch.shell_id, next_shell=branch.next_shell,
        ammo_by_shell=tuple(branch.ammo_by_shell.items()), gun_revision=branch.gun_revision,
        shots_fired=branch.shots_fired, shots=shots,
        marker=branch.aim_marker() if not branch.ended else None,
        reload_at=branch.reload_at, reload_remaining=max(0., branch.reload_at-branch.time),
        reload_total=branch.reload_total, reload_kind=branch.reload_kind,
        pump_at=branch.pump_at, pump_remaining=max(0., branch.pump_at-branch.time),
        pump_total=branch.pump_total, pump_waiting=branch.pump_waiting,
        projectiles=tuple(ProjectileTrace.capture(p) for p in branch.projectiles.values()),
        shot_ends=shot_ends, abilities_used=branch.abilities_used, last_ability=branch.last_ability,
        ability_views=tuple(views), heal_wave=branch.heal_wave,
        traps=tuple(trap for _, trap in branch.traps.values()),
        consumables=tuple(branch.consumables.values()) if branch.ended else (),
        aim_lock=branch.aim_lock, autoaim_lost=autoaim_lost,
        ledger=branch.ledger if branch.ended else None, return_motion=return_motion,
        merge_visuals=tuple(branch.merge_visuals) if return_motion is not None else (),
        burst=BurstState.capture(branch) if branch.ended else None,
        shot_age=None if branch.last_shot_at is None else branch.time - branch.last_shot_at,
        movement_dispersion=branch.gun.movement_dispersion)


def _run_branch(requests, replies, geometry, rules, activation, world=None):
    snapshot, level, wall_origin = activation.snapshot, activation.level, activation.wall_origin
    branch = None
    try:
        branch = TimelineSimulation(snapshot, geometry, level=level, rules=rules, world=world)
        replies.send(_frame(branch))
        pending = None
        flags = 0
        missile_camera = None
        drive_until = wall_origin
        while not branch.ended:
            now = time.monotonic()
            deadline = wall_origin+rules.duration
            if now >= deadline:
                branch.finish()
                break
            next_tick = wall_origin+branch.time+branch.STEP
            wait = min(next_tick, deadline)-now
            if pending is None and requests.poll(max(0., wait)):
                pending = requests.recv()
                # A manual exit can share a packet with commands accepted before it.
                if (pending.finish and not pending.actions and pending.aim is None
                        and pending.point is None and pending.lock is None):
                    branch.finish()
                continue
            if wait > 0.:
                if pending is not None:
                    time.sleep(wait)
                continue
            if pending is not None:
                flags = pending.flags
                missile_camera = pending.missile_camera
                drive_until = now + .25
                if pending.lock is not None:
                    branch.autoaim(*pending.lock)
                if pending.aim is not None:
                    branch.aim(*pending.aim)
                if pending.point is not None:
                    branch.aim_point(*pending.point)
                for sequence, name, arguments in pending.actions:
                    if name == 'ability':
                        branch.activate(sequence, *arguments)
                    elif name == 'fire':
                        branch.fire(sequence)
                    elif name == 'select_shell':
                        branch.select_shell(*arguments)
            if now >= drive_until:
                flags = 0
                missile_camera = None
            branch.tick(snapshot.captured_at+branch.time+branch.STEP,
                        flags=flags, missile_camera=missile_camera)
            if pending is not None and not branch.ended:
                if pending.finish:
                    branch.finish()
                else:
                    replies.send(_frame(branch))
                pending = None
        replies.send(_frame(branch))
    finally:
        if branch is not None:
            branch.close()
        elif world is not None:
            world.close()


def _run(requests, replies):
    try:
        snapshot, geometry, level, rules, wall_origin = requests.recv()
        if snapshot is not None:
            _run_branch(requests, replies, geometry, rules, Activate(snapshot, level, wall_origin))
            return
        while True:
            world = prepare_world(geometry)
            try:
                replies.send(Prepared())
                activation = requests.recv()
                if not isinstance(activation, Activate):
                    raise ValueError('Expected a Time Stop activation')
            except BaseException:
                world.close()
                raise
            _run_branch(requests, replies, geometry, rules, activation, world)
            # The wall clock can end a branch while its last input is in the
            # pipe. Drain that input before looking for the next prepare.
            request = requests.recv()
            while isinstance(request, Step):
                request = requests.recv()
            if not isinstance(request, Prepare):
                raise ValueError('Expected a Time Stop prepare request')
    except (EOFError, BrokenPipeError):
        pass
    except Exception as error:
        try:
            replies.send(Failure(f'{type(error).__name__}: {error}'))
        except (OSError, EOFError):
            pass
    finally:
        requests.close()
        replies.close()


def _send_requests(connection, outgoing, incoming):
    """Serialize the snapshot and inputs off the arena thread."""
    try:
        while (packet := outgoing.get()) is not None:
            connection.send(packet)
    except (OSError, EOFError):
        # The reader still receives a completed ledger if the worker ended
        # between the last step reply and this request.
        pass
    except Exception as error:
        incoming.put(Failure(f'{type(error).__name__}: {error}'))
    finally:
        connection.close()


def _receive_replies(connection, incoming, reusable):
    """Forward independent deadline replies as soon as the worker sends them."""
    try:
        while True:
            reply = connection.recv()
            incoming.put(reply)
            if (isinstance(reply, Failure)
                    or not reusable and isinstance(reply, Frame) and reply.ledger is not None):
                break
    except (OSError, EOFError) as error:
        incoming.put(Failure(type(error).__name__))
    except Exception as error:
        incoming.put(Failure(f'{type(error).__name__}: {error}'))
    finally:
        connection.close()


class Worker:
    """One in-flight step per branch. A slow worker cannot grow an input queue."""

    CLOSE_GRACE = .5

    def __init__(self, snapshot, geometry, *, rules, level=LEVELS[0]):
        wall_origin = time.monotonic()
        context = multiprocessing.get_context('spawn')
        request_rx, request_tx = context.Pipe(duplex=False)
        reply_rx, reply_tx = context.Pipe(duplex=False)
        self.process = context.Process(target=_run, args=(request_rx, reply_tx), name='solbreaker-time-stop')
        self._outgoing, self._incoming = SimpleQueue(), SimpleQueue()
        self._threads = (
            Thread(target=_send_requests, args=(request_tx, self._outgoing, self._incoming),
                   name='solbreaker-time-stop-tx', daemon=True),
            Thread(target=_receive_replies, args=(reply_rx, self._incoming, snapshot is None),
                   name='solbreaker-time-stop-rx', daemon=True),
        )
        self._pending = True
        self._terminal = False
        self._closing = self.closed = False
        self._prepared = False
        self._reusable = snapshot is None
        self._activated = snapshot is not None
        self._completed = False
        started = []
        try:
            self.process.start()
            for thread in self._threads:
                thread.start()
                started.append(thread)
        except BaseException:
            self._outgoing.put(None)
            if self.process.pid is not None:
                self.process.terminate()
            for thread, connection in zip(self._threads, (request_tx, reply_rx)):
                if thread not in started:
                    connection.close()
            raise
        finally:
            request_rx.close()
            reply_tx.close()
        self._outgoing.put((snapshot, geometry, level, rules, wall_origin))

    def submit(self, step):
        if self._pending or self._terminal or self._closing or not self._activated:
            return False
        self._pending = True
        self._outgoing.put(step)
        return True

    def activate(self, snapshot, *, level):
        if not self._prepared or self._pending or self._terminal or self._closing or self._activated:
            return False
        self._activated = self._pending = True
        self._outgoing.put(Activate(snapshot, level, time.monotonic()))
        return True

    def prepare_next(self):
        if not self._reusable or not self._completed or self._closing or not self.process.is_alive():
            return False
        self._pending = True
        self._terminal = self._prepared = self._activated = self._completed = False
        self._outgoing.put(Prepare())
        return True

    def poll(self):
        if self._terminal or self._closing:
            return None
        try:
            reply = self._incoming.get_nowait()
        except Empty:
            return None
        self._pending = False
        if isinstance(reply, Prepared):
            self._prepared = True
        else:
            self._completed = isinstance(reply, Frame) and reply.ledger is not None
            self._terminal = isinstance(reply, Failure) or self._completed
        return reply

    def advance_close(self):
        if self.closed:
            return True
        if not self._closing:
            self._closing = True
            self._kill_at = time.monotonic() + self.CLOSE_GRACE
            self._outgoing.put(None)
            # Cancellation must discard any computed frame, including a reply
            # still in transit. Only an owner-polled ledger can be merged.
            if self.process.is_alive() and not self._terminal:
                self.process.terminate()
        if self.process.is_alive():
            if self._kill_at is not None and time.monotonic() >= self._kill_at:
                self.process.kill()
                self._kill_at = None
            return False
        if any(thread.is_alive() for thread in self._threads):
            return False
        self.process.close()
        self.closed = True
        return True

    def close(self):
        """Blocking shutdown for a test or application exit, outside an arena tick."""
        if self.advance_close():
            return
        self.process.join(timeout=2.)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=2.)
        for thread in self._threads:
            thread.join(timeout=2.)
        if not self.advance_close():
            raise RuntimeError('Time Stop worker did not terminate')
