"""Run a private time branch without waiting on physics from an arena tick."""
from dataclasses import dataclass
import multiprocessing
from queue import Empty, SimpleQueue
from threading import Thread
import time

from .collision import Pose
from .time_stop import EffectLedger, Snapshot, TimelineSimulation, prepare_world


@dataclass(frozen=True)
class Step:
    distance: float = 0.
    yaw_delta: float = 0.
    aim: tuple | None = None
    point: tuple | None = None
    fire: tuple | None = None  # Action ID, server shell, nominal cooldown.
    ability: tuple | None = None  # Action ID and authoritative slot ID.
    finish: bool = False


@dataclass(frozen=True)
class Frame:
    branch: str
    seconds: float
    pose: Pose
    aim: tuple
    gun_packed: bytes
    energy: int
    ammo: int
    clip: int
    shots_fired: int
    last_shot: tuple | None
    marker: tuple | None
    reload_remaining: float
    projectiles: tuple
    shot_ends: tuple
    abilities_used: int
    last_ability: tuple | None
    ability_views: tuple
    heal_wave: tuple | None
    traps: tuple
    consumables: tuple
    ledger: EffectLedger | None = None


@dataclass(frozen=True)
class Failure:
    reason: str


@dataclass(frozen=True)
class Prepared:
    """The Bullet world is ready to receive its activation snapshot."""


@dataclass(frozen=True)
class Activate:
    snapshot: Snapshot
    wall_origin: float


def _frame(branch):
    views = tuple((slot.spec.compact_id, slot.state_at(branch.time)) for slot in branch.consumables.values())
    shot_ends = tuple(branch.shot_ends)
    branch.shot_ends.clear()
    return Frame(branch.snapshot.ident, branch.time, branch.pose,
                 branch.world.gun_angles[branch.entity], branch.gun.packed(), branch.ledger.energy,
                 branch.ammo, branch.clip, branch.shots_fired, branch.last_shot,
                 branch.aim_marker() if not branch.ended else None,
                 max(0., branch.reload_at-branch.time),
                 tuple((shot, p.position, p.velocity) for shot, p in branch.projectiles.items()),
                 shot_ends,
                 branch.abilities_used, branch.last_ability, views, branch.heal_wave,
                 tuple(trap for _, trap in branch.traps.values()),
                 tuple(branch.consumables.values()) if branch.ended else (),
                 branch.ledger if branch.ended else None)


def _run(connection):
    branch = None
    world = None
    try:
        snapshot, geometry, level, rules, wall_origin = connection.recv()
        if snapshot is None:
            world = prepare_world(geometry)
            connection.send(Prepared())
            activation = connection.recv()
            snapshot, wall_origin = activation.snapshot, activation.wall_origin
        branch = TimelineSimulation(snapshot, geometry, level=level, rules=rules, world=world)
        connection.send(_frame(branch))
        pending = None
        distance = yaw_delta = 0.
        drive_until = wall_origin
        while not branch.ended:
            now = time.monotonic()
            deadline = wall_origin+rules.duration
            if now >= deadline:
                branch.finish()
                break
            next_tick = wall_origin+branch.time+branch.STEP
            wait = min(next_tick, deadline)-now
            if pending is None and connection.poll(max(0., wait)):
                pending = connection.recv()
                if pending.finish:
                    branch.finish()
                continue
            if wait > 0.:
                if pending is not None:
                    time.sleep(wait)
                continue
            if pending is not None:
                distance, yaw_delta = pending.distance, pending.yaw_delta
                drive_until = now + .25
                if pending.aim is not None:
                    branch.aim(*pending.aim)
                if pending.point is not None:
                    branch.gun.set_point(*pending.point)
                actions = (action for action in (pending.ability, pending.fire) if action is not None)
                for action in sorted(actions, key=lambda value: value[0]):
                    if action is pending.ability:
                        branch.activate(*action)
                    else:
                        sequence, shell, cooldown = action
                        branch.fire(sequence, shell, cooldown=cooldown)
            if now >= drive_until:
                distance = yaw_delta = 0.
            branch.tick(snapshot.captured_at+branch.time+branch.STEP,
                        distance=distance, yaw_delta=yaw_delta)
            if pending is not None and not branch.ended:
                connection.send(_frame(branch))
                pending = None
        connection.send(_frame(branch))
    except (EOFError, BrokenPipeError):
        pass
    except Exception as error:
        try:
            connection.send(Failure(f'{type(error).__name__}: {error}'))
        except (OSError, EOFError):
            pass
    finally:
        if branch is not None:
            branch.close()
        elif world is not None:
            world.close()
        connection.close()


def _exchange(connection, outgoing, incoming):
    """Pipe I/O and geometry serialization stay off the arena thread."""
    try:
        while True:
            try:
                packet = outgoing.get(timeout=.004)
            except Empty:
                pass
            else:
                if packet is None:
                    break
                connection.send(packet)
            if connection.poll():
                reply = connection.recv()
                incoming.put(reply)
                if isinstance(reply, Failure) or (isinstance(reply, Frame) and reply.ledger is not None):
                    break
    except (OSError, EOFError) as error:
        incoming.put(Failure(type(error).__name__))
    except Exception as error:
        incoming.put(Failure(f'{type(error).__name__}: {error}'))
    finally:
        connection.close()


class Worker:
    """One in-flight step per branch. A slow worker cannot grow an input queue."""

    def __init__(self, snapshot, geometry, *, level, rules):
        wall_origin = time.monotonic()
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        self.process = context.Process(target=_run, args=(child,), name='solbreaker-time-stop')
        self._outgoing, self._incoming = SimpleQueue(), SimpleQueue()
        self._thread = Thread(target=_exchange, args=(parent, self._outgoing, self._incoming),
                              name='solbreaker-time-stop-ipc', daemon=True)
        self._pending = True
        self._terminal = False
        self._closing = self.closed = False
        self._prepared = False
        self._activated = snapshot is not None
        try:
            self.process.start()
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self._outgoing.put((snapshot, geometry, level, rules, wall_origin))
        self._thread.start()

    def submit(self, step):
        if self._pending or self._terminal or self._closing or not self._activated:
            return False
        self._pending = True
        self._outgoing.put(step)
        return True

    def activate(self, snapshot):
        if not self._prepared or self._pending or self._terminal or self._closing or self._activated:
            return False
        self._activated = self._pending = True
        self._outgoing.put(Activate(snapshot, time.monotonic()))
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
            self._terminal = isinstance(reply, Failure) or reply.ledger is not None
        return reply

    def advance_close(self):
        if self.closed:
            return True
        if not self._closing:
            self._closing = True
            self._outgoing.put(None)
            # Cancellation must discard any computed frame, including a reply
            # still in transit. Only an owner-polled ledger can be merged.
            if self.process.is_alive() and not self._terminal:
                self.process.terminate()
        if self.process.is_alive() or self._thread.is_alive():
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
        self._thread.join(timeout=2.)
        if not self.advance_close():
            raise RuntimeError('Time Stop worker did not terminate')
