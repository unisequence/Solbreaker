"""Bounded nonblocking process workers for isolated grounded movement.

Pipe I/O never blocks the owner. A shared, bounded frame barrier allows ready
replies to arrive; unfinished/failed speculation uses the local solver.
Packets contain geometry/drive state only, never sessions or credentials.
"""
from collections import Counter
import multiprocessing as mp
import os
from pathlib import Path
import pickle
import queue
import tempfile
import threading
import time
import weakref


_POOLS = weakref.WeakSet()


def _acquire_capacity(count):
    if count == 0:
        return []
    try:
        import fcntl
    except ImportError:
        return [] if sum(p.count for p in _POOLS if not p.closed) + count <= 4 else None
    directory = Path(tempfile.gettempdir())/f'tfs-drive-capacity-{os.getuid()}'
    directory.mkdir(mode=0o700, exist_ok=True)
    acquired = []
    for slot in range(4):
        fd = os.open(directory/f'{slot}.lock', os.O_CREAT|os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        acquired.append(fd)
        if len(acquired) == count:
            return acquired
    for fd in acquired:
        os.close(fd)
    return None


def _release_capacity(fds):
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


class WorkerCapacityError(ValueError):
    """Other arenas currently hold the bounded worker capacity."""


def has_worker_capacity(count):
    """Check local and cross-process capacity without building a replica."""
    if sum(p.count for p in _POOLS if not p.closed) + count > 4:
        return False
    acquired = _acquire_capacity(count)
    if acquired is None:
        return False
    _release_capacity(acquired)
    return True


def _pump(connection, commands, replies, available):
    """Blocking pipe I/O belongs to this thread, never to the network owner."""
    try:
        packet = connection.recv()
        replies.put(packet)
        available.set()
        if packet[0] != 'ready':
            return
        while True:
            command = commands.get()
            connection.send(command)
            if command is None:
                return
            while True:
                packet = connection.recv()
                replies.put(packet)
                available.set()
                if packet[0] in ('done', 'error'):
                    break
    except (EOFError, BrokenPipeError, OSError):
        try:
            replies.put_nowait(('error', 'pipe_closed'))
            available.set()
        except queue.Full:
            pass
    finally:
        connection.close()


def _worker(connection, spec_path):
    replica = None
    try:
        # Tiny 3x3 support fits are the unit of work. A BLAS pool per process
        # would oversubscribe cores; these settings affect this child only.
        for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
            os.environ[name] = '1'
        from .drive_replica import DriveReplica
        with open(spec_path, 'rb') as stream:
            replica = DriveReplica(pickle.load(stream))
        connection.send(('ready', os.getpid()))
        while True:
            packet = connection.recv()
            if packet is None:
                return
            packet = pickle.loads(packet)
            generation, poses, angles, destroyed, jobs = packet
            replica.sync(poses, angles, destroyed)
            for job in jobs:
                connection.send(('reply', generation, replica.run(job)))
            connection.send(('done', generation))
    except (EOFError, BrokenPipeError):
        pass
    except BaseException as exc:
        try:
            connection.send(('error', type(exc).__name__))
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        if replica is not None:
            replica.close()
        connection.close()


def _worker_memory_budget():
    try:
        relative = next(line.split(':', 2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines()
                        if line.startswith('0::'))
        root = Path('/sys/fs/cgroup')/relative.lstrip('/')
        high = (root/'memory.high').read_text().strip()
        if high == 'max':
            return None
        current = int((root/'memory.current').read_text())
        return max(0, int(high)-current)
    except (OSError, StopIteration, ValueError):
        return None


def _memory_limited_workers(count, budget):
    if budget is None:
        return count
    gib = 1024**3
    return max(0, min(count, (budget-gib)//gib))


def configured_workers():
    raw = os.environ.get('SOLBREAKER_SIM_WORKERS', 'auto')
    if raw == 'auto':
        cores = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count() or 1
        count = max(0, min(3, cores-1))
        return _memory_limited_workers(count, _worker_memory_budget())
    try:
        count = int(raw)
    except (ValueError, TypeError):
        raise ValueError('SOLBREAKER_SIM_WORKERS must be auto or an integer from 0 to 4') from None
    if not 0 <= count <= 4:
        raise ValueError('SOLBREAKER_SIM_WORKERS must be auto or an integer from 0 to 4')
    return count


class DriveWorkers:
    """One arena's workers, with one in-flight generation and a bounded inbox.

    Start one child at a time. Arena loading polls startup incrementally,
    yielding to network callbacks until workers are ready or have failed.
    A slow generation prevents another submission, so no frame queue can grow.
    """
    def __init__(self, spec, count, *, context=None, startup_timeout=60.,
                 task_timeout=2., wait_budget=.002, allow_partial=False):
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 4:
            raise ValueError('Invalid simulation worker count')
        self.context = context or mp.get_context('spawn')
        self.startup_timeout = startup_timeout
        self.task_timeout = task_timeout
        self.wait_budget = wait_budget
        self._wait_deadline = None
        self._available = threading.Event()
        # Reserve the selected size atomically. An auto pool may shrink to
        # available capacity; an explicitly requested integer remains exact.
        for candidate in (range(count,0,-1) if allow_partial else (count,)):
            if sum(p.count for p in _POOLS if not p.closed)+candidate > 4:
                continue
            capacity = _acquire_capacity(candidate)
            if capacity is not None:
                self.count,self._capacity = candidate,capacity
                break
        else:
            raise WorkerCapacityError('Simulation worker capacity already in use')
        try:
            self.directory = tempfile.TemporaryDirectory(prefix='tfs-drive-workers-')
            self.spec_path = Path(self.directory.name)/'geometry.pickle'
            self.children = []
            self.generation = 0
            self.active = None
            self.replies = {}
            self.expected = set()
            self.closed = False
            self.closing = False
            self.failed = False
            self.stats = Counter()
            self._writer_error = None
            self._close_started = None
            self._writer = threading.Thread(target=self._write_spec,args=(spec,),
                                            name='tfs-drive-geometry',daemon=True)
            _POOLS.add(self)
            self._writer.start()
        except BaseException:
            self.closed = self.closing = True
            if hasattr(self, 'directory'):
                self.directory.cleanup()
            _POOLS.discard(self)
            _release_capacity(self._capacity)
            self._capacity = []
            raise

    def _write_spec(self, spec):
        try:
            with self.spec_path.open('xb') as stream:
                pickle.dump(spec, stream, protocol=pickle.HIGHEST_PROTOCOL)
        except BaseException as exc:
            self._writer_error = type(exc).__name__

    @property
    def ready(self):
        return (not self.closed and not self.closing and not self.failed and len(self.children) == self.count
                and all(child['ready'] for child in self.children))

    def poll(self):
        if self.closed:
            return
        if self.failed:
            self.advance_close()
            return
        if self.closing:
            return
        now = time.monotonic()
        for child in self.children:
            process = child['process']
            try:
                # At most one bounded generation can be in this pipe.
                while True:
                    try:
                        packet = child['replies'].get_nowait()
                    except queue.Empty:
                        break
                    if not isinstance(packet,tuple) or not packet:
                        self._fail('malformed_packet')
                        return
                    kind = packet[0]
                    if kind == 'ready':
                        if len(packet) != 2 or child['ready']:
                            self._fail('malformed_packet')
                            return
                        child['ready'] = True
                    elif kind == 'reply':
                        from .drive_jobs import DriveReply
                        if len(packet) != 3 or not isinstance(packet[2],DriveReply):
                            self._fail('malformed_packet')
                            return
                        if packet[1] == self.active:
                            reply = packet[2]
                            if (reply.entity not in child['expected']
                                    or reply.entity in child['seen']):
                                self._fail('invalid_reply')
                                return
                            child['seen'].add(reply.entity)
                            self.replies[reply.entity] = reply
                    elif kind == 'done':
                        if len(packet) != 2:
                            self._fail('malformed_packet')
                            return
                        if packet[1] == self.active:
                            if child['seen'] != child['expected']:
                                self._fail('incomplete_batch')
                                return
                            child['busy'] = False
                    else:
                        self._fail('worker_error')
                        return
            except (EOFError, BrokenPipeError, OSError):
                self._fail('worker_pipe')
                return
            if process.exitcode is not None:
                self._fail('worker_exit')
                return
            if not child['ready'] and now-child['started'] > self.startup_timeout:
                self._fail('startup_timeout')
                return
            if child['busy'] and now-child['submitted'] > self.task_timeout:
                self._fail('task_timeout')
                return
        if self.failed or self._writer.is_alive():
            return
        if self._writer_error:
            self._fail('geometry_snapshot')
            return
        if len(self.children) < self.count and all(c['ready'] for c in self.children):
            parent, child = self.context.Pipe(duplex=True)
            process = self.context.Process(target=_worker, args=(child, str(self.spec_path)),
                                           name='tfs-ground-drive', daemon=True)
            try:
                process.start()
            except BaseException:
                parent.close()
                child.close()
                self._fail('worker_start')
                return
            child.close()
            commands, replies = queue.Queue(maxsize=1), queue.Queue(maxsize=32)
            pump = threading.Thread(target=_pump,args=(parent,commands,replies,self._available),
                                    name='tfs-drive-io',daemon=True)
            self.children.append(dict(connection=parent, process=process,
                                      commands=commands,replies=replies,pump=pump,
                                      ready=False, busy=False, expected=set(), seen=set(),
                                      started=now, submitted=0.))
            pump.start()

    def submit(self, jobs, poses, angles, destroyed):
        if len(jobs) > 14:
            raise ValueError('Too many speculative drive jobs')
        self.poll()
        if not self.ready or any(child['busy'] for child in self.children):
            self.stats['unavailable'] += 1
            return None
        self.generation += 1
        self.active = self.generation
        self.replies.clear()
        self.expected = {job.entity for job in jobs}
        self._wait_deadline = None
        batches = [[] for _ in self.children]
        # The owner solves the reserved head locally. Within the remaining
        # tail, workers prepare the earliest commit deadlines first.
        for index, job in enumerate(jobs):
            batches[index % len(batches)].append(job)
        for child, batch in zip(self.children, batches):
            if not batch:
                continue
            try:
                # Serialize before handing immutable bytes to a pump thread:
                # no worker may see owner caches mutated by the local solver.
                packet = pickle.dumps((self.active, poses, angles, destroyed, batch),
                                      protocol=pickle.HIGHEST_PROTOCOL)
                if len(packet) > 256*1024:
                    self._fail('oversized_batch')
                    return None
                child['commands'].put_nowait(packet)
            except (queue.Full, EOFError, BrokenPipeError, OSError):
                self._fail('worker_send')
                return None
            child['busy'] = True
            child['expected'] = {job.entity for job in batch}
            child['seen'] = set()
            child['submitted'] = time.monotonic()
        self.stats['submitted'] += len(jobs)
        return self.active

    def take(self, generation, entity):
        self.poll()
        if self.failed or generation is None or generation != self.active:
            return None
        if entity in self.expected and entity not in self.replies:
            if self._wait_deadline is None:
                self._wait_deadline = time.monotonic()+self.wait_budget
            # One shared barrier for the frame. This wait releases the GIL
            # for I/O pumps too; a separate sleep(0) would yield outside the
            # budget and repeat for later actors after the deadline expired.
            while not self.failed and entity not in self.replies:
                remaining = self._wait_deadline-time.monotonic()
                if remaining <= 0.:
                    break
                self._available.clear()
                self.poll()
                if entity in self.replies or self.failed:
                    break
                self._available.wait(remaining)
                self.poll()
        reply = self.replies.pop(entity, None)
        if reply is not None:
            self.stats['received'] += 1
        return reply

    def _fail(self, reason):
        self.failed = True
        self.stats[reason] += 1
        self.replies.clear()
        # Terminate without joining here: failure is handled from the network
        # owner callback. Reaping is polled/closed outside the physics phase.
        for child in self.children:
            if child['process'].is_alive():
                child['process'].terminate()

    def advance_close(self):
        if self.closed:
            return True
        self.closing = True
        if self._close_started is None:
            self._close_started = time.monotonic()
        for child in self.children:
            process = child['process']
            if process.is_alive():
                if time.monotonic()-self._close_started > 1.:
                    process.kill()
                else:
                    process.terminate()
        for child in self.children:
            process = child['process']
            process.join(timeout=0)
            if process.is_alive():
                return False
            try:
                child['commands'].put_nowait(None)
            except queue.Full:
                pass
            child['pump'].join(timeout=0)
            if child['pump'].is_alive():
                # Drain bounded replies so a pump can finish publishing EOF.
                while True:
                    try:
                        child['replies'].get_nowait()
                    except queue.Empty:
                        break
                return False
        if self._writer.is_alive():
            return False
        for child in self.children:
            child['process'].close()
        self.replies.clear()
        self.directory.cleanup()
        _release_capacity(self._capacity)
        self._capacity = []
        self.closed = True
        return True

    def close(self):
        while not self.advance_close():
            time.sleep(.001)


class DriveDispatchPolicy:
    """Choose a compute path from observed cost; never skip a simulation step."""
    def __init__(self):
        self.costs = {}
        self.counts = {'serial':0,'parallel':0}
        self.frames = 0

    def choose(self):
        self.frames += 1
        if 'serial' not in self.costs:
            return 'serial'
        if 'parallel' not in self.costs:
            return 'parallel'
        fastest = min(self.costs, key=self.costs.get)
        # Refresh the other observation periodically as scene cost changes.
        if self.frames % 60 == 0:
            return 'parallel' if fastest == 'serial' else 'serial'
        return fastest

    def observe(self,mode,elapsed_ns):
        previous = self.costs.get(mode)
        self.costs[mode] = elapsed_ns if previous is None else .75*previous+.25*elapsed_ns
        self.counts[mode] += 1
