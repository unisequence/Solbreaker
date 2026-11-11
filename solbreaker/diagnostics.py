"""Opt-in bounded flight recorder for server and authoritative arena processes."""
from collections import deque
from contextvars import ContextVar
from functools import wraps
import atexit
import gc
import inspect
import json
import logging
import math
import os
from pathlib import Path
import queue
import sys
import threading
import traceback
import time
import uuid

ENVIRONMENT = 'SOLBREAKER_DIAGNOSTIC_DIR'
FILE_BYTES = 16 * 1024**2
FILE_BACKUPS = 3
QUEUE_RECORDS = 256
PEAK_RECORDS = 16
PEAK_EVENTS = frozenset(('process_start', 'slow_frame', 'operation_error'))
MAX_RECORD_BYTES = 256 * 1024
SLOW_NS = 16_666_667
_CURRENT = ContextVar('solbreaker_diagnostic_trace', default=None)
_RECORDER = None
_PATCHES = []
_PREVIOUS_PATH = None
_GC_START = {}
_GC_EVENTS = deque(maxlen=32)
_LOG = logging.getLogger(__name__)
_SECRET_FIELDS = frozenset(('password', 'password_hash', 'token', 'access_token', 'refresh_token',
                            'authorization', 'cookie', 'key', 'salt', 'secret', 'payload', 'body', 'raw'))


def directory(lab, requested):
    if requested is None:
        return None
    path = Path(lab) / 'private/diagnostics' if str(requested) == 'auto' else Path(requested)
    return path.expanduser().resolve()


def _plain(value, depth=0):
    """Copy bounded JSON values without repr(), object internals or wire bodies."""
    if depth > 8:
        return '<depth limit>'
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return value[:1024]
    if isinstance(value, bytes):
        return {'bytes': len(value)}
    if isinstance(value, dict):
        return {str(k)[:128]: ('<redacted>' if str(k).lower() in _SECRET_FIELDS else _plain(v, depth + 1))
                for k, v in list(value.items())[:128]}
    if isinstance(value, (list, tuple, deque)):
        return [_plain(item, depth + 1) for item in list(value)[:128]]
    # numpy scalars are used in physics, but importing numpy is unnecessary here.
    if type(value).__module__ == 'numpy' and hasattr(value, 'item'):
        return _plain(value.item(), depth + 1)
    return f'<{type(value).__name__}>'


def _resources():
    result = {'process_cpu_ns': time.process_time_ns(), 'threads': threading.active_count()}
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        result.update(user_seconds=usage.ru_utime, system_seconds=usage.ru_stime,
                      max_rss_native_units=usage.ru_maxrss, minor_faults=usage.ru_minflt,
                      major_faults=usage.ru_majflt, voluntary_switches=usage.ru_nvcsw,
                      involuntary_switches=usage.ru_nivcsw)
    except ImportError:
        pass
    for name in ('status', 'schedstat', 'io'):
        try:
            text = Path('/proc/self', name).read_text()
            if name == 'status':
                text = '\n'.join(line for line in text.splitlines() if line.split(':', 1)[0] in
                                 ('VmRSS', 'VmHWM', 'VmSize', 'Threads', 'voluntary_ctxt_switches',
                                  'nonvoluntary_ctxt_switches'))
            result[name] = text[:4096]
        except OSError:
            pass
    try:
        group = next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines()
                     if line.startswith('0::'))
        root = Path('/sys/fs/cgroup') / group.lstrip('/')
        result['cgroup'] = {name: (root / name).read_text()[:2048] for name in
                            ('memory.current', 'memory.peak', 'memory.events', 'cpu.stat', 'cpu.pressure')
                            if (root / name).exists()}
    except (OSError, StopIteration):
        pass
    return result


class Recorder:
    def __init__(self, path, *, file_bytes=FILE_BYTES, backups=FILE_BACKUPS, capacity=QUEUE_RECORDS):
        self.path = Path(path)
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.file = self.path / f'process-{os.getpid()}-{uuid.uuid4().hex[:8]}.jsonl'
        # Fail explicitly at startup if the destination cannot be opened.
        descriptor = os.open(self.file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.stream = os.fdopen(descriptor, 'wb')
        self.peak_file = self.file.with_suffix('.peaks.jsonl')
        try:
            descriptor = os.open(self.peak_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.peak_stream = os.fdopen(descriptor, 'wb')
        except OSError:
            self.stream.close()
            raise
        self.file_bytes, self.backups = file_bytes, backups
        self.queue = queue.Queue(capacity)
        self.peak_queue = queue.Queue(PEAK_RECORDS)
        self.dropped_peak_records = 0
        self.dropped_records = 0
        self.written_records = 0
        self.failed = None
        self.closed = False
        self.stop = threading.Event()
        self.sequence = 0
        self.lock = threading.RLock()
        self.started_pid = os.getpid()
        self.thread = threading.Thread(target=self._write, name='solbreaker-diagnostics', daemon=True)
        self.thread.start()
        self.record('process_start', python=sys.version, platform=sys.platform, parent_pid=os.getppid(),
                    argv=[Path(sys.argv[0]).name], resource_sample=_resources(),
                    flags={k: v for k, v in os.environ.items()
                           if k.startswith(('SOLBREAKER_SIM_', 'SOLBREAKER_DYNAMIC_', 'SOLBREAKER_ARENA_',
                                            'SOLBREAKER_OWNER_', 'SOLBREAKER_RAPIER_', 'SOLBREAKER_'))
                           and k != ENVIRONMENT and not any(part in k for part in
                               ('KEY', 'TOKEN', 'PASSWORD', 'SECRET', 'AUTH', 'COOKIE'))},
                    limits=dict(file_bytes=file_bytes, backups=backups, queue_records=capacity,
                                max_record_bytes=MAX_RECORD_BYTES, peak_queue_records=PEAK_RECORDS, slow_frame_ns=SLOW_NS))

    def record(self, event_name, **fields):
        if self.closed or self.failed is not None or self.started_pid != os.getpid():
            return
        try:
            producer_started = time.perf_counter_ns()
            values = _plain(fields)
            with self.lock:
                if self.closed:
                    return
                reserved = {'schema', 'event', 'pid', 'sequence', 'monotonic_ns', 'unix_ns',
                            'logger_dropped_records', 'logger_dropped_peak_records', 'producer_ns'}
                conflicts = {key: values.pop(key) for key in reserved if key in values}
                if conflicts:
                    values['header_conflicts'] = conflicts
                self.sequence += 1
                record = dict(schema=1, event=event_name, pid=os.getpid(), sequence=self.sequence,
                              monotonic_ns=time.monotonic_ns(), unix_ns=time.time_ns(),
                              logger_dropped_records=self.dropped_records,
                              logger_dropped_peak_records=self.dropped_peak_records,
                              producer_ns=time.perf_counter_ns()-producer_started, **values)
                try:
                    self.queue.put_nowait(record)
                except queue.Full:
                    self.dropped_records += 1
                if event_name in PEAK_EVENTS:
                    try:
                        self.peak_queue.put_nowait(record)
                    except queue.Full:
                        self.dropped_peak_records += 1
        except Exception:
            # A diagnostics failure must not replace a gameplay exception.
            self.dropped_records += 1

    def _rotate(self, stream, filename):
        stream.close()
        Path(f'{filename}.{self.backups}').unlink(missing_ok=True)
        for index in range(self.backups - 1, 0, -1):
            source = Path(f'{filename}.{index}')
            if source.exists():
                source.replace(f'{filename}.{index + 1}')
        filename.replace(f'{filename}.1')
        descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return os.fdopen(descriptor, 'wb')

    def _line(self, record, *, peak=False):
        if peak:
            record = dict(record, stream='peaks')
        raw = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode() + b'\n'
        if len(raw) > MAX_RECORD_BYTES:
            raw = json.dumps(dict(schema=1, event='record_oversize', pid=os.getpid(),
                                  original_event=record['event'], sequence=record['sequence'],
                                  monotonic_ns=record.get('monotonic_ns'), unix_ns=record.get('unix_ns'),
                                  logger_dropped_records=self.dropped_records, bytes=len(raw))).encode() + b'\n'
            self.dropped_records += 1
        stream, filename = (self.peak_stream, self.peak_file) if peak else (self.stream, self.file)
        if stream.tell() + len(raw) > self.file_bytes:
            stream = self._rotate(stream, filename)
            if peak:
                self.peak_stream = stream
            else:
                self.stream = stream
        stream.write(raw)
        self.written_records += 1

    def _write(self):
        sample_at = time.monotonic() + 1
        try:
            while not self.stop.is_set() or not self.queue.empty() or not self.peak_queue.empty():
                try:
                    item = self.queue.get(timeout=.1)
                except queue.Empty:
                    item = None
                if item is not None:
                    try:
                        self._line(item)
                    finally:
                        self.queue.task_done()
                for _ in range(PEAK_RECORDS):
                    try:
                        peak = self.peak_queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self._line(peak, peak=True)
                    finally:
                        self.peak_queue.task_done()
                _drain_gc(self)
                now = time.monotonic()
                if now >= sample_at:
                    self.record('resources', resource_sample=_resources(), queued_records=self.queue.qsize(),
                                written_records=self.written_records)
                    sample_at = now + 1
                if item is None or self.queue.empty():
                    self.stream.flush()
                    self.peak_stream.flush()
            stopped = dict(schema=1, event='process_stop', pid=os.getpid(),
                            monotonic_ns=time.monotonic_ns(), unix_ns=time.time_ns(),
                            sequence=self.sequence + 1, logger_dropped_records=self.dropped_records,
                           logger_dropped_peak_records=self.dropped_peak_records)
            self._line(stopped)
            self._line(stopped, peak=True)
        except Exception as exc:
            self.failed = type(exc).__name__
            _LOG.error('Solbreaker diagnostic writer stopped (%s)', self.failed)
        finally:
            self.stream.close()
            self.peak_stream.close()

    def close(self):
        if not self.closed:
            _drain_gc(self)
            with self.lock:
                self.closed = True
            self.stop.set()
            self.thread.join(.75)


def get_recorder():
    return _RECORDER


def _gc_sample(phase, info):
    recorder = get_recorder()
    if recorder is None:
        return
    generation = info['generation']
    if phase == 'start':
        _GC_START[generation] = (time.perf_counter_ns(), time.thread_time_ns())
    else:
        start = _GC_START.pop(generation, None)
        if start is not None:
            # GC can interrupt queue or recorder locks. Defer queue access.
            if len(_GC_EVENTS) == _GC_EVENTS.maxlen:
                recorder.dropped_records += 1
            _GC_EVENTS.append(dict(generation=generation,
                                   wall_ns=time.perf_counter_ns()-start[0],
                                   thread_cpu_ns=time.thread_time_ns()-start[1],
                                   collected=info.get('collected'), uncollectable=info.get('uncollectable')))


def _drain_gc(recorder):
    for _ in range(len(_GC_EVENTS)):
        try:
            sample = _GC_EVENTS.popleft()
        except IndexError:
            break
        recorder.record('garbage_collection', **sample)


def close():
    global _RECORDER, _PREVIOUS_PATH
    if _RECORDER is not None:
        _RECORDER.close()
        if os.environ.get(ENVIRONMENT) == str(_RECORDER.path.resolve()):
            if _PREVIOUS_PATH is None:
                os.environ.pop(ENVIRONMENT, None)
            else:
                os.environ[ENVIRONMENT] = _PREVIOUS_PATH
        _RECORDER = None
    if _gc_sample in gc.callbacks:
        gc.callbacks.remove(_gc_sample)
    _GC_START.clear()
    _GC_EVENTS.clear()
    while _PATCHES:
        owner, name, original = _PATCHES.pop()
        setattr(owner, name, original)


def _vehicle_state(arena):
    from .orientation import quaternion
    world = arena.world
    rows = []
    for vehicle in arena.vehicles.values():
        pose = vehicle.pose
        motion = world.motion.get(vehicle.entity_id) if world is not None else None
        dynamic = world.dynamic_bodies.get(vehicle.entity_id) if world is not None else None
        definition = vehicle.definition
        bot = getattr(arena, 'training_bots', {}).get(vehicle.entity_id)
        rows.append(dict(entity=vehicle.entity_id, account=vehicle.account_id, team=vehicle.team,
                         vehicle=f'{definition.nation}/{definition.name}' if definition is not None else None,
                         position=pose.position, yaw=pose.yaw, pitch=pose.pitch, roll=pose.roll,
                         orientation=quaternion(pose.yaw, pose.pitch, pose.roll),
                         speed=vehicle.speed, health=vehicle.health, flags=vehicle.flags,
                         bot={name: getattr(bot, name, None) for name in
                              ('target_id', 'reverse_until', 'braking', 'cover_goal', 'recoveries', 'passing')}
                         if bot is not None else None,
                         gun_yaw=vehicle.gun.yaw, gun_pitch=vehicle.gun.pitch,
                         center_of_mass=getattr(dynamic, 'center_of_mass', None),
                         inertia_diagonal=getattr(dynamic, 'inertia_diagonal', None),
                         angular_velocity=getattr(world, '_rapier_angular_states', {}).get(vehicle.entity_id),
                         velocity=getattr(motion, 'velocity', None), grounded=getattr(motion, 'grounded', None),
                         dynamic=dynamic is not None,
                         dynamic_contacts=getattr(world, 'dynamic_contact_trace', {}).get(vehicle.entity_id, ())
                         if world is not None else ()))
    return rows


class Trace:
    def __init__(self, label, arena=None):
        self.label, self.arena = label, arena
        self.phases = {}
        self.start = time.perf_counter_ns()
        self.cpu = time.thread_time_ns()
        self.process_cpu = time.process_time_ns()
        self.events = {}
        self.before_step = getattr(arena, 'step_count', None)

    def finish(self, error=None):
        wall, cpu = time.perf_counter_ns() - self.start, time.thread_time_ns() - self.cpu
        capture_started = time.perf_counter_ns()
        record = dict(operation=self.label, wall_ns=wall, thread_cpu_ns=cpu,
                      process_cpu_ns=time.process_time_ns() - self.process_cpu,
                      non_thread_cpu_ns=max(0, wall - cpu), phases=self.phases,
                      error=error, events=self.events)
        arena = self.arena
        if arena is not None:
            record.update(arena_id=arena.result_id, step=arena.step_count,
                          fixed_steps=arena.step_count - (self.before_step or 0),
                          simulation_time=arena.simulation_time, accumulator=arena.accumulator,
                          dropped_seconds=arena.dropped_time, projectiles=len(arena.projectiles),
                          projectile_states=[dict(shot=p.shot, shooter=p.shooter, position=p.position,
                                                  velocity=p.velocity) for p in list(arena.projectiles.values())[:128]],
                          modules={name: {entity: state.health for entity, state in getattr(arena, attr, {}).items()}
                                   for name, attr in (('engine', 'engines'), ('ammo', 'ammo_racks'), ('fuel', 'fuels'))},
                          vehicles=_vehicle_state(arena))
            from .sim_native import runtime_status
            record['runtime'] = runtime_status(arena.world, arena.drive_workers)
        record['capture_ns'] = time.perf_counter_ns()-capture_started
        recorder = get_recorder()
        if recorder is not None:
            recorder.record('frame' if arena is not None else 'operation', **record)
            if wall > SLOW_NS or error is not None:
                history = getattr(arena, '_diagnostic_history', ())
                commands = getattr(arena, '_diagnostic_commands', ())
                recorder.record('slow_frame' if error is None else 'operation_error',
                                **record, recent_frames=list(history), recent_commands=list(commands))
            if arena is not None:
                if not hasattr(arena, '_diagnostic_history'):
                    arena._diagnostic_history = deque(maxlen=8)
                arena._diagnostic_history.append(dict(step=arena.step_count, wall_ns=wall,
                                                       vehicles=record['vehicles']))


def _wrap(owner, name, label, *, arena=False):
    original = getattr(owner, name, None)
    if original is None:
        return

    def begin(obj):
        root = _CURRENT.get()
        trace = None
        token = None
        selected = obj if arena is True else getattr(obj, arena, None) if arena else None
        if root is None or selected is not None and root.arena is not selected:
            trace = Trace(label, selected)
            token = _CURRENT.set(trace)
            root = trace
        return root, trace, token, time.perf_counter_ns(), time.thread_time_ns()

    def end(state, error):
        root, trace, token, wall, cpu = state
        try:
            elapsed = time.perf_counter_ns() - wall
            phase = root.phases.setdefault(label, dict(calls=0, wall_ns=0, thread_cpu_ns=0, max_ns=0))
            phase['calls'] += 1
            phase['wall_ns'] += elapsed
            phase['thread_cpu_ns'] += time.thread_time_ns() - cpu
            phase['max_ns'] = max(phase['max_ns'], elapsed)
            if trace is not None:
                trace.finish(error)
        except Exception:
            recorder = get_recorder()
            if recorder is not None:
                recorder.dropped_records += 1
        finally:
            if token is not None:
                _CURRENT.reset(token)

    if inspect.iscoroutinefunction(original):
        @wraps(original)
        async def wrapped(self, *args, **kwargs):
            state = begin(self)
            error = None
            try:
                return await original(self, *args, **kwargs)
            except BaseException as exc:
                error = dict(type=type(exc).__name__, stack=[dict(file=f.filename, line=f.lineno, function=f.name)
                             for f in traceback.extract_tb(exc.__traceback__)[-32:]])
                raise
            finally:
                end(state, error)
    else:
        @wraps(original)
        def wrapped(self, *args, **kwargs):
            state = begin(self)
            error = None
            try:
                return original(self, *args, **kwargs)
            except BaseException as exc:
                error = dict(type=type(exc).__name__, stack=[dict(file=f.filename, line=f.lineno, function=f.name)
                             for f in traceback.extract_tb(exc.__traceback__)[-32:]])
                raise
            finally:
                end(state, error)
    _PATCHES.append((owner, name, original))
    setattr(owner, name, wrapped)


def event(arena, kind, actor, target, data):
    trace = _CURRENT.get()
    if trace is not None:
        trace.events[kind] = trace.events.get(kind, 0) + 1
    recorder = get_recorder()
    if recorder is not None:
        recorder.record('battle_event', arena_id=arena.result_id, step=arena.step_count,
                        kind=kind, actor=actor, target=target, data=data)


def endpoint_sample(endpoint, now):
    recorder = get_recorder()
    if recorder is None:
        return
    scheduled = getattr(endpoint, 'next_tick', now)
    recorder.record('server_tick', started_at=now, scheduled_at=scheduled,
                    late_ns=max(0., now-scheduled)*1e9)
    if now < getattr(endpoint, '_diagnostic_next_sample', 0.):
        return
    endpoint._diagnostic_next_sample = now + 1.
    try:
        recorder.record('endpoint', metrics=endpoint.metrics())
    except Exception:
        recorder.dropped_records += 1


def command(arena, item):
    recorder = get_recorder()
    if recorder is None:
        return
    if not hasattr(arena, '_diagnostic_commands'):
        arena._diagnostic_commands = deque(maxlen=64)
    data = dict(operation=item.op, at=item.at, account=item.account_id,
                payload_bytes=len(item.payload), deadline=item.deadline)
    # Only gameplay control packets have decoded input bytes in this recorder.
    if item.op in ('control', 'aim', 'aim_angles', 'select_shell', 'consumable'):
        data['input_hex'] = item.payload[:32].hex()
    arena._diagnostic_commands.append(data)
    recorder.record('command', arena_id=arena.result_id, **data)


def install(path=None):
    """Install timing hooks only when requested, including in spawned owners."""
    global _RECORDER, _PREVIOUS_PATH
    if _RECORDER is not None:
        return _RECORDER
    path = path or os.environ.get(ENVIRONMENT)
    if not path:
        return None
    recorder = Recorder(path)
    _RECORDER = recorder
    _PREVIOUS_PATH = os.environ.get(ENVIRONMENT)
    os.environ[ENVIRONMENT] = str(Path(path).resolve())
    atexit.register(close)
    gc.callbacks.append(_gc_sample)
    from multiprocessing.util import Finalize
    Finalize(None, close, exitpriority=10)
    from . import arena as arena_module
    from . import arena_owner_game as owner_module
    from .arena import Arena
    from .arena_owner_game import GameArenaHandler
    from .arena_owner_registry import ArenaRoute
    from .arena_owner_transport import ArenaOwnerTransport
    from . import collision as collision_module
    from .collision import CollisionScene, CollisionWorld
    from .bot_ai import BotDirector
    from .bot_navigation import Navigation
    from .drive_workers import DriveWorkers
    from .rapier_dynamic import RapierDynamic
    from .static_visibility import VisibilityWorld
    from .arena_loading import ArenaLoader
    from .armor import ArmorModel
    from .internal_geometry import PreparedInternalGeometry
    from .base import BaseEndpoint
    original_contacts = CollisionWorld._contacts
    original_rays = CollisionWorld._static_rays
    for owner, names in (
        (Arena, ('advance_build', '_fixed_step', '_drive_vehicles', '_impact', '_internal_contact', '_fall_impacts',
                 '_track_fall_impacts', '_record_motion_frame', '_recon_point_probe_update')),
        (ArenaRoute, ('request',)),
        (ArenaOwnerTransport, ('_deliver',)),
        (CollisionScene, ('__init__', 'prepare')),
        (ArenaLoader, ('_advance',)),
        (CollisionWorld, ('__init__', 'move', 'dynamic_commit', 'resolve_ground_impacts',
                          '_static_rays', '_visibility_rays', '_contacts')),
        (VisibilityWorld, ('_mesh', 'rays', 'destroy', 'close')),
        (ArmorModel, ('intersections',)),
        (PreparedInternalGeometry, ('volume_segment',)),
        (BotDirector, ('step',)),
        (Navigation, ('request', 'step')),
        (DriveWorkers, ('poll', 'submit', 'take')),
        (RapierDynamic, ('step', 'static_rays')),
        (BaseEndpoint, ('tick', 'tick_client', 'send_packet', 'datagram_received')),
    ):
        for name in names:
            _wrap(owner, name, f'{owner.__name__}.{name}')
    for module, name in ((arena_module, 'advance'), (owner_module, 'snapshot'),
                         (owner_module, 'event_messages'), (owner_module, 'motion_messages')):
        _wrap(module, name, f'{module.__name__}.{name}')
    _wrap(GameArenaHandler, 'apply', 'GameArenaHandler.apply', arena='arena')
    _wrap(Arena, 'step', 'Arena.step', arena=True)
    # Trust only the wrappers installed here around the guarded originals.
    # Unrelated replacements must still decline speculative native execution.
    contract = collision_module._DRIVE_PREFIX_METHODS
    if contract.get('_contacts') is original_contacts:
        _PATCHES.append((collision_module, '_DRIVE_PREFIX_METHODS', contract))
        collision_module._DRIVE_PREFIX_METHODS = dict(contract, _contacts=CollisionWorld._contacts)
    if collision_module._GROUND_RAYS_IMPL is original_rays:
        _PATCHES.append((collision_module, '_GROUND_RAYS_IMPL', original_rays))
        collision_module._GROUND_RAYS_IMPL = CollisionWorld._static_rays
    return recorder
