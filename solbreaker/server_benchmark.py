"""Repeatable load on the production local server with private test state."""
from array import array
import asyncio
from collections import Counter
from itertools import islice, product
import json
import math
import os
from pathlib import Path
import platform
import struct
import sys
import tempfile
import time

from . import __version__
from .base import BaseEndpoint, ChannelOverflow, ClientSession
from .bootstrap import prepare
from .bot_ai import TrainingBot
from .cipher import PacketCipher, decrypt_packet, encrypt_packet
from .datagram import open_datagram
from .build_info import BUILD_ID
from .map_catalog import map_team_capacity
from .mercury import RELIABLE, encode_packet, parse_packet
from .motion import SIMULATION_INTERVAL, UPDATE_HZ
from .server import LocalServer
from .sim_native import runtime_status
from .training_config import RoomBattleSettings, TrainingConfig

SCENARIOS = ('solo', 'bots', 'players')
CONTROL_INTERVAL = 1 / UPDATE_HZ
STEP_HZ = round(1 / SIMULATION_INTERVAL)
ROUTE = (TrainingBot.FORWARD, TrainingBot.FORWARD | TrainingBot.LEFT,
         TrainingBot.FORWARD, TrainingBot.FORWARD | TrainingBot.RIGHT,
         TrainingBot.REVERSE)


def durations(values):
    if not values:
        return {'samples': 0, 'p50_ms': None, 'p95_ms': None, 'p99_ms': None, 'max_ms': None}
    values = sorted(values)
    result = {'samples': len(values)}
    for percent in (50, 95, 99, 100):
        sample = values[math.ceil(len(values) * percent / 100) - 1]
        result[f'p{percent}_ms' if percent < 100 else 'max_ms'] = round(sample * 1000, 3)
    return result


def host_info():
    cpu = platform.processor() or platform.machine()
    ram = None
    if sys.platform == 'win32':
        import ctypes
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
                cpu = winreg.QueryValueEx(key, 'ProcessorNameString')[0].strip()
        except OSError:
            pass
        query = ctypes.WinDLL('kernel32').GetPhysicallyInstalledSystemMemory
        query.argtypes = (ctypes.POINTER(ctypes.c_ulonglong),)
        installed = ctypes.c_ulonglong()
        if query(ctypes.byref(installed)):
            ram = installed.value * 1024
    elif sys.platform == 'linux':
        try:
            cpu = next(value.strip() for row in Path('/proc/cpuinfo').read_text().splitlines()
                       for name, _, value in (row.partition(':'),) if name.strip() == 'model name')
        except (OSError, StopIteration):
            pass
        try:
            with Path('/proc/meminfo').open() as meminfo:
                ram = int(next(row for row in meminfo if row.startswith('MemTotal:')).split()[1]) * 1024
        except (OSError, StopIteration, ValueError):
            pass
    return {'os': platform.system(), 'os_release': platform.release(), 'cpu': cpu,
            'logical_cpus': os.cpu_count(),
            'available_cpus': len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None,
            'physical_memory_bytes': ram, 'python': platform.python_version()}


def process_memory(pid=None):
    """Current/peak resident bytes for the benchmark process, excluding children."""
    if sys.platform == 'win32' and pid is None:
        import ctypes as c
        from ctypes import wintypes as w

        class Counters(c.Structure):
            _fields_ = [('cb', w.DWORD), ('faults', w.DWORD),
                        ('peak', c.c_size_t), ('current', c.c_size_t),
                        ('quota_and_pagefile', c.c_size_t * 6)]

        kernel = c.WinDLL('kernel32')
        current_process = kernel.GetCurrentProcess
        current_process.restype = w.HANDLE
        query = kernel.K32GetProcessMemoryInfo
        query.argtypes = (w.HANDLE, c.POINTER(Counters), w.DWORD)
        counters = Counters(cb=c.sizeof(Counters))
        if query(current_process(), c.byref(counters), counters.cb):
            return counters.current, counters.peak
    elif sys.platform == 'linux':
        try:
            fields = dict((key, value) for row in Path(f'/proc/{pid or "self"}/status').read_text().splitlines()
                          for key, _, value in (row.partition(':'),) if key in ('VmRSS', 'VmHWM'))
            return tuple(int(fields[key].split()[0]) * 1024 for key in ('VmRSS', 'VmHWM'))
        except (OSError, KeyError, ValueError):
            pass
    return None, None


FAULT_EVENTS = ('base_fault', 'arena_fault', 'base_rejected', 'base_disconnected',
                'arena_retirement_failed', 'arena_owner_start_failed')


def udp_receive_drops(transports):
    """Read only the selected IPv4 sockets' kernel receive-drop counters."""
    if sys.platform != 'linux':
        return None
    try:
        inodes = {str(os.fstat(transport.get_extra_info('socket').fileno()).st_ino)
                  for transport in transports}
        with Path('/proc/net/udp').open() as sockets:
            next(sockets)
            drops = {row[9]: int(row[-1]) for line in sockets
                     if len(row := line.split()) >= 13 and row[9] in inodes}
        if drops.keys() == inodes:
            return sum(drops.values())
    except (OSError, StopIteration, ValueError):
        pass
    return None


def owner_cpu_times(pids):
    if sys.platform != 'linux':
        return {}
    frequency = os.sysconf('SC_CLK_TCK')
    result = {}
    for pid in pids:
        try:
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            result[pid] = (int(fields[11]) + int(fields[12])) / frequency
        except (OSError, ValueError, IndexError):
            continue
    return result


class MeasuredServer(LocalServer):
    def __init__(self, state):
        self.fault_reasons = Counter()
        super().__init__(state)

    def event(self, name, **fields):
        super().event(name, **fields)
        if name in FAULT_EVENTS:
            reason = name, fields.get('stage', ''), fields.get('reason', '')
            if len(self.fault_reasons) < 20 or reason in self.fault_reasons:
                self.fault_reasons[reason] += 1


class Peer(asyncio.DatagramProtocol):
    """Valid synthetic battle traffic on loopback after session creation."""
    def __init__(self, client):
        self.cipher = PacketCipher(client.key)
        self.sequence = 0
        self.packets = 0
        self.bytes = 0
        self.sent = 0
        self.invalid = 0
        self.server_address = None

    def connection_made(self, transport):
        self.transport = transport

    def send(self, body=b'', *, acks=()):
        packet = encode_packet(body, sequence=self.sequence, reliable=False, acks=acks)
        self.sequence = (self.sequence + 1) & 0x0fffffff
        data = bytes(4) + encrypt_packet(packet, self.cipher.key, codec=self.cipher)
        self.transport.sendto(data, self.server_address)
        self.sent += 1

    def controls(self, flags, target):
        body = struct.pack('<BHH', 145, 2, flags)
        if target is not None:
            x, y, z = target.pose.position
            body += struct.pack('<BH3fBH', 149, 12, x, y + 1., z, 138, 0)
        self.send(body)

    def datagram_received(self, data, address):
        if address != self.server_address:
            return
        self.packets += 1
        self.bytes += len(data)
        try:
            packet = parse_packet(decrypt_packet(data, self.cipher.key, codec=self.cipher))
        except ValueError:
            self.invalid += 1
            return
        if packet.flags & RELIABLE:
            self.send(acks=(packet.sequence,))


class MeasuredEndpoint(BaseEndpoint):
    def __init__(self, server, *, cold):
        self.cold = cold
        self.measuring = False
        self.ticks = array('f')
        self.input_wait = {}
        self.motion_gaps = {}
        self.last_motion = {}
        self.submitted = Counter()
        self.queued = Counter()
        self.shots = Counter()
        self.rejected = Counter()
        super().__init__(server)

    async def _preload_map_metadata(self):
        # A steady-state run uses the selected map only. Cold runs also load
        # the full launcher map catalog in the background.
        if self.cold:
            await super()._preload_map_metadata()

    def tick(self):
        start = time.monotonic()
        super().tick()
        if self.measuring:
            self.ticks.append(time.monotonic() - start)

    def queue_arena_input(self, addr, client, arena, command):
        try:
            super().queue_arena_input(addr, client, arena, command)
        except ChannelOverflow:
            if self.measuring:
                self.rejected[arena.result_id] += 1
            raise
        if self.measuring and command is not None and command.action == 'control':
            self.queued[arena.result_id] += 1

    def _apply_arena_inputs(self, arena, deadline):
        if not self.measuring:
            return super()._apply_arena_inputs(arena, deadline)
        queued = arena.pending_inputs
        before = len(queued)
        # The real queue processes at most INPUTS_PER_TICK. Only record
        # commands that were removed, leaving the rest for the next tick.
        batch = tuple(islice(queued, self.INPUTS_PER_TICK))
        super()._apply_arena_inputs(arena, deadline)
        now = time.monotonic()
        self.input_wait[arena.result_id].extend(
            now - command.now for _, _, command in batch[:before - len(queued)]
            if command.action == 'control')

    def send_packet(self, address, client, body, acks=(), fragment=None, *, reliable=True):
        super().send_packet(address, client, body, acks, fragment, reliable=reliable)
        if self.measuring and not reliable and body[:1] == b'\x0d' and client.phase == 'battle':
            stamp = time.monotonic()
            last = self.last_motion.get(address)
            self.last_motion[address] = stamp
            if last is not None:
                self.motion_gaps[client.arena_id].append(stamp - last)

    def observe_owner_inputs(self):
        submit = self.owner_service.submit

        def timed_submit(address, client, kind, body):
            if not self.measuring or kind != 145:
                return submit(address, client, kind, body)
            key = client.arena_id
            start = time.monotonic()
            future = submit(address, client, kind, body)
            if future is not None:
                self.queued[key] += 1

                def complete(result):
                    if self.measuring and not result.cancelled() and result.exception() is None:
                        self.input_wait[key].append(time.monotonic() - start)

                future.add_done_callback(complete)
            return future

        self.owner_service.submit = timed_submit

    def log_arena_events(self, arena, events):
        if self.measuring:
            self.shots[arena.result_id] += sum(event.kind == 'shot' for event in events)
        super().log_arena_events(arena, events)


def tank_count(scenario, capacity, requested):
    if requested is None:
        return 2 if scenario == 'solo' else 2 * capacity
    if (scenario == 'solo' and requested != 2) or requested > 2 * capacity:
        raise ValueError(f'{scenario} cannot field {requested} tanks on this map')
    return requested


def _arena(endpoint, scenario, number, seed, requested_tanks):
    spec, definition = endpoint.map_spec, endpoint.definition
    tanks = tank_count(scenario, map_team_capacity(spec.map_id), requested_tanks)
    capacity = tanks // 2
    count = tanks if scenario == 'players' else 1
    accounts = []
    for seat in range(count):
        ident = number * 100 + seat
        name = f'benchmark{ident}'
        account = endpoint.server.accounts.store.authenticate(f'{name}@example.test',
                                                               'local-benchmark', autocreate=True)
        accounts.append({'id': account.id, 'nickname': name, 'team': 1 + seat % 2})
    options = dict(result_id=f'{(seed << 32) | (number + 1):032x}')
    if scenario == 'players':
        options.update(solo=False, room_settings=RoomBattleSettings(
            minutes=0, unlimited_lives=True, unlimited_ammo=True))
    else:
        options['training'] = TrainingConfig(
            mode='bots' if scenario == 'bots' else 'target',
            allies=capacity - 1 if scenario == 'bots' else 0,
            enemies=capacity if scenario == 'bots' else 1,
            minutes=0, unlimited_lives=True, unlimited_ammo=True)
        if scenario == 'bots':
            from .bot_roster import select_bot_roster
            roster = select_bot_roster(endpoint.game, definition, enemies=capacity,
                                       allies=capacity - 1, seed=options['result_id'])
            options.update(bot_roster=roster, bot_game=endpoint.game, bot_scene_settings=
                           endpoint.bot_scene_settings(roster, endpoint.collision_scene))
    arena = endpoint.arenas.create(accounts, build_world=not endpoint.arena_owners_enabled, **options)
    return arena, accounts, options.get('training')


class MotionSample:
    def __init__(self):
        self.nonfinite = 0
        self.below_samples = 0
        self.max_below_m = 0.
        self._since = {}
        self.max_below_seconds = 0.

    def observe(self, arena, now):
        for entity, vehicle in arena.vehicles.items():
            if not vehicle.alive:
                self._since.pop(entity, None)
                continue
            pose = vehicle.pose
            x, y, z = pose.position
            if not all(math.isfinite(value) for value in (x, y, z, pose.pitch, pose.yaw, pose.roll)):
                self.nonfinite += 1
                self._since.pop(entity, None)
                continue
            if not arena.terrain.MIN <= x <= arena.terrain.MAX_X or not arena.terrain.MIN_Z <= z <= arena.terrain.MAX_Z:
                self._since.pop(entity, None)
                continue
            depth = arena.terrain.height(x, z) - y
            if depth <= 1.:
                self._since.pop(entity, None)
                continue
            self.below_samples += 1
            self.max_below_m = max(self.max_below_m, depth)
            self.max_below_seconds = max(self.max_below_seconds, now - self._since.setdefault(entity, now))

    def report(self):
        return {'nonfinite_samples': self.nonfinite, 'below_heightmap_samples': self.below_samples,
                'max_below_heightmap_m': round(self.max_below_m, 3),
                'max_below_heightmap_seconds': round(self.max_below_seconds, 3)}


def _control_offsets(actors):
    """Interleave arenas so every arena receives input across the whole interval."""
    seats = Counter()
    slots = []
    for index, (arena, *_) in enumerate(actors):
        slots.append((seats[arena.result_id], index))
        seats[arena.result_id] += 1
    return {index: CONTROL_INTERVAL * slot / len(slots)
            for slot, (_, index) in enumerate(sorted(slots))}


class Timeline:
    """Record one-second windows during the measured part of a case."""

    def __init__(self, endpoint, arenas, pids):
        self.endpoint = endpoint
        self.arenas = arenas
        self.pids = pids
        self.rows = []
        self.started = self.last = time.monotonic()
        self.cpu = time.process_time()
        self.worker_cpu = owner_cpu_times(pids)
        self.steps = [arena.step_count for arena in arenas]
        self.inputs = [0] * len(arenas)
        self.ticks = self.lags = 0

    def sample(self, lags, *, final=False):
        now = time.monotonic()
        span = now - self.last
        if span < (.25 if final else 1.0):
            return
        cpu = time.process_time()
        workers = owner_cpu_times(self.pids)
        worker_rss = [process_memory(pid)[0] for pid in self.pids]
        steps = [arena.step_count for arena in self.arenas]
        inputs = [self.endpoint.input_wait[arena.result_id] for arena in self.arenas]
        ticks = self.endpoint.ticks
        self.rows.append({
            'elapsed_seconds': round(now - self.started, 3),
            'window_seconds': round(span, 3),
            'server_cpu_cores': round((cpu - self.cpu) / span, 3),
            'server_rss_bytes': process_memory()[0],
            'worker_cpu_cores': (round(sum(workers[pid] - self.worker_cpu[pid]
                                           for pid in self.pids) / span, 3)
                                 if self.pids and len(workers) == len(self.worker_cpu) == len(self.pids)
                                 else None),
            'worker_rss_bytes_sum': (sum(worker_rss) if worker_rss and None not in worker_rss else None),
            'steps_per_second': [round((value - previous) / span, 3)
                                 for value, previous in zip(steps, self.steps, strict=True)],
            'input_p99_ms': [durations(values[offset:])['p99_ms']
                             for values, offset in zip(inputs, self.inputs, strict=True)],
            'tick_p99_ms': durations(ticks[self.ticks:])['p99_ms'],
            'loop_delay_p99_ms': durations(lags[self.lags:])['p99_ms'],
        })
        self.last, self.cpu, self.worker_cpu, self.steps = now, cpu, workers, steps
        self.inputs = [len(values) for values in inputs]
        self.ticks, self.lags = len(ticks), len(lags)


async def _drive(endpoint, actors, arenas, samples, duration, started, *, measure=False,
                 windows=None, timeline=None, pattern='burst'):
    first = scheduled = time.monotonic()
    end = first + duration
    probe = first
    lags = array('f')
    next_fire = first
    loop = asyncio.get_running_loop()
    pending = []
    offsets = _control_offsets(actors) if pattern == 'spread' else {}

    def send_control(arena, address, peer, flags, target):
        if address not in endpoint.clients or time.monotonic() >= end:
            return
        peer.controls(flags, target)
        if measure:
            endpoint.submitted[arena.result_id] += 1

    window_start = first
    window_steps = [arena.step_count for arena in arenas] if windows is not None else []
    window_inputs = {key: 0 for key in endpoint.input_wait} if windows is not None else {}
    window_motion = {key: 0 for key in endpoint.motion_gaps} if windows is not None else {}
    while scheduled < end:
        await asyncio.sleep(max(0., scheduled - time.monotonic()))
        now = time.monotonic()
        if now >= end:
            if measure:
                lags.append(now - scheduled)
            break
        if measure:
            lags.append(max(0., now - scheduled))
        phase = int((now - started) / 2)
        fire = now >= next_fire
        if fire:
            next_fire = now + .9
        for handle in pending:
            handle.cancel()
        pending.clear()
        for index, (arena, address, client, peer) in enumerate(actors):
            if address not in endpoint.clients:
                continue
            flags = ROUTE[(phase + index % 3) % len(ROUTE)]
            target = None
            if fire:
                own = arena.vehicle_for(client.account_id)
                target = next((v for v in arena.vehicles.values() if v.team != own.team and v.alive), None)
            if pattern == 'spread':
                offset = offsets[index]
                if now + offset < end:
                    pending.append(loop.call_later(offset, send_control, arena, address, peer, flags, target))
            else:
                send_control(arena, address, peer, flags, target)
        if measure and now >= probe:
            probe = now + .1
            for arena in arenas:
                samples[arena.result_id].observe(arena, now)
        if timeline is not None:
            timeline.sample(lags)
        if windows is not None and now - window_start >= 10:
            deltas = [(arena.step_count - last) / (now - window_start)
                      for arena, last in zip(arenas, window_steps, strict=True)]
            windows.append({'elapsed_seconds': round(now - first, 3),
                            'steps_per_second': [round(rate, 3) for rate in deltas],
                            'sessions': len(endpoint.clients),
                            'input_p99_ms': [durations(values[window_inputs[key]:])['p99_ms']
                                             for key, values in endpoint.input_wait.items()],
                            'motion_max_ms': [round(max(values[window_motion[key]:], default=0.) * 1000, 3)
                                              for key, values in endpoint.motion_gaps.items()],
                            'dropped_seconds_total': [round(arena.dropped_time, 3) for arena in arenas]})
            window_inputs = {key: len(values) for key, values in endpoint.input_wait.items()}
            window_motion = {key: len(values) for key, values in endpoint.motion_gaps.items()}
            window_start = now
            window_steps = [arena.step_count for arena in arenas]
        scheduled += CONTROL_INTERVAL * max(1, math.floor((now - scheduled) / CONTROL_INTERVAL) + 1)
    try:
        await asyncio.sleep(max(0., end - time.monotonic()))
        if timeline is not None:
            timeline.sample(lags, final=True)
    finally:
        for handle in pending:
            handle.cancel()
    return lags


def _verdict(arenas, faults):
    if faults or any(a['nonfinite_samples'] for a in arenas):
        return 'fault'
    if any(a['steps_per_second'] < 59 or a['dropped_seconds'] > .05 or a['input_overflows']
           or a['controls_sent'] - a['controls_queued'] > max(2, .01 * a['controls_sent']) for a in arenas):
        return 'overloaded'
    if any(a['input_queue']['p99_ms'] is None or a['input_queue']['p99_ms'] > 50
           or a['motion_gap']['p99_ms'] is None or a['motion_gap']['p99_ms'] > 67
           or a['motion_gap']['max_ms'] > 150 for a in arenas):
        return 'stalls'
    return 'ok'


async def _wait_for_battle(endpoint, arenas, deadline):
    # Drive traffic only after READY is confirmed by every owner. Otherwise
    # a startup measurement includes pre-battle input floods.
    while not all(arena.started for arena in arenas):
        if (time.monotonic() >= deadline or endpoint.arena_faults
                or any(endpoint.arenas.get(arena.result_id) is not arena for arena in arenas)):
            raise RuntimeError('Benchmark battle did not start')
        await asyncio.sleep(.025)


async def _scenario(state, map_key, scenario, count, requested_tanks, seconds, warmup, seed, *, cold, pattern):
    manifest_path = state / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['training_map'] = map_key
    manifest_path.write_text(json.dumps(manifest))
    start = time.monotonic()
    case_epoch = time.time()
    server = MeasuredServer(state)
    endpoint = None
    transport = None
    peers = []
    shutdown_by_arena = {}
    try:
        endpoint = MeasuredEndpoint(server, cold=cold)
        loop = asyncio.get_running_loop()
        startup = time.monotonic() - start
        arenas, builds, actors, owner_recipes = [], [], [], []
        for index in range(count):
            before = time.monotonic()
            arena, accounts, training = _arena(endpoint, scenario, index, seed, requested_tanks)
            builds.append(round(time.monotonic() - before, 3))
            arenas.append(arena)
            selected = []
            for account_id in arena.players:
                client = ClientSession(b'sb-bench', account_id, phase='battle', account_id=account_id,
                                       arena_id=arena.result_id, account_created=True, battle_spawned=True)
                local, peer = await loop.create_datagram_endpoint(lambda: Peer(client), local_addr=('127.0.0.1', 0))
                peers.append(local)
                address = local.get_extra_info('sockname')
                endpoint.clients[address] = client
                actors.append((arena, address, client, peer))
                selected.append((address, client))
            if endpoint.arena_owners_enabled:
                ids = arena.players
                recipe = endpoint.owner_service.make_factory(
                    arena, accounts, dict.fromkeys(ids, endpoint.definition),
                    dict.fromkeys(ids, endpoint.collision_scene), training)
                owner_recipes.append((arena, selected, recipe))
        transport, _ = await open_datagram(lambda: endpoint, local_addr=('127.0.0.1', 0), batch_receive=True)
        for _, _, _, peer in actors:
            peer.server_address = transport.get_extra_info('sockname')
        if owner_recipes:
            endpoint.observe_owner_inputs()
            service = endpoint.owner_service
            close_owner = service._close_owner

            async def timed_close(job):
                close_started = time.monotonic()
                try:
                    await close_owner(job)
                finally:
                    shutdown_by_arena.setdefault(job.arena.result_id, round(time.monotonic() - close_started, 3))

            service._close_owner = timed_close
            build_started = time.monotonic()
            async def start_owner(index, recipe):
                await endpoint.owner_service.start(*recipe)
                builds[index] = round(builds[index] + time.monotonic() - build_started, 3)

            await asyncio.gather(*(start_owner(index, recipe) for index, recipe in enumerate(owner_recipes)))
            for arena in arenas:
                if arena.result_id not in endpoint.owner_service.jobs or arena.building:
                    raise RuntimeError('Benchmark arena worker failed during setup')
            for _, _, client, peer in actors:
                # Synthetic clients skip native entity construction after the real owner starts.
                client.phase = 'battle'
                client.battle_spawned = True
                peer.send(struct.pack('<BH', 135, 0))
        started = time.monotonic()
        if not owner_recipes:
            for arena in arenas:
                for account_id in arena.players:
                    arena.ready(account_id, now=started)
        try:
            await _wait_for_battle(endpoint, arenas, started + 60)
        except RuntimeError:
            for (name, stage, reason), occurrences in server.fault_reasons.most_common():
                print(f'  {occurrences} x {name} {stage}: {reason}', file=sys.stderr, flush=True)
            raise
        countdown = time.monotonic() - started
        await _drive(endpoint, actors, arenas, {}, warmup, started, pattern=pattern)
        before = [(a.step_count, a.dropped_time) for a in arenas]
        baseline_packets = sum(p.packets for _, _, _, p in actors)
        baseline_bytes = sum(p.bytes for _, _, _, p in actors)
        baseline_sent = sum(p.sent for _, _, _, p in actors)
        server_drops_before = udp_receive_drops((transport,))
        peer_drops_before = udp_receive_drops(peers)
        retry_baseline = sum(c.retransmissions for _, _, c, _ in actors)
        samples = {a.result_id: MotionSample() for a in arenas}
        endpoint.input_wait = {a.result_id: array('f') for a in arenas}
        endpoint.motion_gaps = {a.result_id: array('f') for a in arenas}
        owners = {key: job.owner._owner._process.pid for key, job in endpoint.owner_service.jobs.items()
                  if job.owner is not None} if owner_recipes else {}
        worker_pids = list(owners.values())
        owner_ticks = {}
        if owner_recipes:
            def capture_tick(transport, samples):
                deliver = transport.on_delivered

                def timed(command, result):
                    deliver(command, result)
                    if endpoint.measuring and command.op == 'step':
                        samples.append(time.monotonic() - command.at)

                transport.on_delivered = timed

            for key, job in endpoint.owner_service.jobs.items():
                if job.transport is not None:
                    owner_ticks[key] = array('f')
                    capture_tick(job.transport, owner_ticks[key])
        owner_cpu_before = owner_cpu_times(worker_pids)
        endpoint.measuring = True
        measure_epoch = time.time()
        begin, cpu = time.monotonic(), time.process_time()
        windows = []
        timeline = Timeline(endpoint, arenas, worker_pids)
        lags = await _drive(endpoint, actors, arenas, samples, seconds, started,
                            measure=True, windows=windows, timeline=timeline, pattern=pattern)
        wall = time.monotonic() - begin
        cpu = (time.process_time() - cpu) / wall
        endpoint.measuring = False
        details = []
        for arena, (steps, lost), build in zip(arenas, before, builds, strict=True):
            key = arena.result_id
            details.append({'vehicles': len(arena.vehicles), 'bots': len(arena.training_bots),
                            'players': len(arena.players), 'alive': sum(v.alive for v in arena.vehicles.values()),
                            'build_seconds': build, 'steps_per_second': round((arena.step_count - steps) / wall, 3),
                            'dropped_seconds': round(arena.dropped_time - lost, 4),
                            'remaining_step_debt_ms': round(arena.accumulator * 1000, 3),
                            'controls_sent': endpoint.submitted[key], 'controls_queued': endpoint.queued[key],
                            'input_overflows': endpoint.rejected[key],
                            'pending_input_commands': (endpoint.owner_service.jobs[key].transport._queue.qsize()
                                                       if owner_recipes and key in endpoint.owner_service.jobs
                                                       and endpoint.owner_service.jobs[key].transport is not None
                                                       else len(arena.pending_inputs)),
                            'shots': endpoint.shots[key],
                            'input_queue': durations(endpoint.input_wait[key]),
                            'owner_step_roundtrip': durations(owner_ticks.get(key, ())),
                            'motion_gap': durations(endpoint.motion_gaps[key]),
                            'physics': (getattr(arena, 'owner_simulation', {}) if owner_recipes
                                        else runtime_status(arena.world, arena.drive_workers)),
                            **samples[key].report()})
        rss, peak = process_memory()
        owner_cpu_after = owner_cpu_times(worker_pids)
        comparable_pids = owner_cpu_before.keys() & owner_cpu_after.keys()
        owner_cpu = sum(owner_cpu_after[pid] - owner_cpu_before[pid] for pid in comparable_pids) / wall
        owner_rss = [value for pid in worker_pids if (value := process_memory(pid)[0]) is not None]
        faults = {name: server.counters[name] for name in FAULT_EVENTS if server.counters[name]}
        for (name, stage, reason), occurrences in server.fault_reasons.most_common():
            # The local console can contain paths in an exception message.
            print(f'  {occurrences} x {name} {stage}: {reason}', file=sys.stderr, flush=True)
        server_drops_after = udp_receive_drops((transport,))
        peer_drops_after = udp_receive_drops(peers)
        bad_packets = sum(peer.invalid for _, _, _, peer in actors)
        if bad_packets:
            faults['invalid_peer_packets'] = bad_packets
        if endpoint.arena_faults:
            faults['quarantined_arenas'] = len(endpoint.arena_faults)
        if len(endpoint.clients) != len(actors):
            faults['missing_test_clients'] = len(actors) - len(endpoint.clients)
        if any(not a.started or a.finished_at for a in arenas):
            faults['inactive_arenas'] = sum(not a.started or bool(a.finished_at) for a in arenas)
        for arena, data in zip(arenas, details, strict=True):
            pid = owners.get(arena.result_id)
            if pid is not None:
                job = endpoint.owner_service.jobs.get(arena.result_id)
                if job is not None and job.owner is not None:
                    data['build_profile'] = job.owner.startup_metrics
                data['worker_pid'] = pid
                if pid in comparable_pids:
                    data['worker_cpu_cores_avg'] = round((owner_cpu_after[pid] - owner_cpu_before[pid]) / wall, 3)
        report = {'map': map_key, 'scenario': scenario, 'concurrent_arenas': count, 'requested_tanks': requested_tanks, 'seed': seed,
                'case_started_epoch': case_epoch, 'measurement_started_epoch': measure_epoch,
                'seconds': round(wall, 3), 'server_setup_seconds': round(startup, 3),
                'battle_start_seconds': round(countdown, 3),
                'cpu_cores_avg': round(cpu, 3), 'process_rss_bytes': rss, 'process_lifetime_peak_rss_bytes': peak,
                'arena_worker_cpu_cores_avg': round(owner_cpu, 3) if comparable_pids else None,
                'arena_worker_rss_bytes_sum': sum(owner_rss) if owner_rss else None,
                'arena_worker_memory_samples': len(owner_rss),
                'tick_work': durations(endpoint.ticks), 'loop_delay': durations(lags),
                'loopback_packets': sum(p.packets for _, _, _, p in actors) - baseline_packets,
                'loopback_bytes': sum(p.bytes for _, _, _, p in actors) - baseline_bytes,
                'server_transport': type(transport).__name__,
                'loopback_packets_sent': sum(p.sent for _, _, _, p in actors) - baseline_sent,
                'server_udp_receive_drops': None if None in (server_drops_before, server_drops_after)
                else server_drops_after - server_drops_before,
                'peer_udp_receive_drops': None if None in (peer_drops_before, peer_drops_after)
                else peer_drops_after - peer_drops_before,
                'reliable_retransmissions': sum(c.retransmissions for _, _, c, _ in actors) - retry_baseline,
                'server_rtt_at_end': durations([c.smoothed_rtt for _, _, c, _ in actors
                                                if c.smoothed_rtt is not None]),
                'windows': windows, 'timeline': timeline.rows, 'faults': faults, 'verdict': _verdict(details, faults), 'arenas': details}
    finally:
        cleanup_started = time.monotonic()
        try:
            for local in peers:
                local.close()
            if transport is not None:
                transport.close()
                await asyncio.sleep(0)
                closing = getattr(endpoint, 'owner_close_task', None)
                if closing is not None:
                    await closing
            elif endpoint is not None:
                endpoint.arenas.close()
                for context in endpoint.map_contexts.values():
                    endpoint._close_map_context(context)
        finally:
            server.accounts.close()
    for arena, data in zip(arenas, details, strict=True):
        data['shutdown_seconds'] = shutdown_by_arena.get(arena.result_id)
    report['shutdown_seconds'] = round(time.monotonic() - cleanup_started, 3)
    report['case_seconds'] = round(time.monotonic() - start, 3)
    return report


def run(args):
    maps = args.maps.split(',')
    scenarios = args.scenarios.split(',')
    counts = [int(value) for value in args.arenas.split(',')]
    tanks = [None] if args.tanks == 'auto' else [int(value) for value in args.tanks.split(',')]
    if (not maps or any(not key for key in maps) or set(scenarios) - set(SCENARIOS)
            or any(not 1 <= count <= 16 for count in counts)
            or any(tank is not None and tank not in range(2, 15, 2) for tank in tanks)
            or not 5 <= args.seconds <= 3600 or not 0 <= args.warmup <= 120
            or not 1 <= args.repeat <= 10 or not 0 <= args.seed <= 2**63 - args.repeat):
        raise ValueError('Invalid benchmark ranges or scenario. See benchmark --help')
    if not args.output.parent.is_dir():
        raise ValueError('Benchmark output directory does not exist')
    if args.output.exists():
        raise ValueError('Choose a new output file for the benchmark report')
    from .benchmark_report import html_output, write_html
    html = html_output(args.output)
    if html.exists() or html.is_symlink():
        raise ValueError('Choose a new HTML file for the benchmark report')
    # Launcher defaults for the owner profile. In-process is retained as a control.
    os.environ['SOLBREAKER_ARENA_OWNERS'] = '1' if args.engine == 'owner' else '0'
    if args.engine == 'owner':
        defaults = {
            'SIM_BACKEND': 'rust', 'DYNAMIC_BACKEND': 'rapier', 'OWNER_IPC': 'socket',
            'SIM_WORKERS': '0', 'RAPIER_PARALLEL_DRIVE': '1', 'RAPIER_QUERIES': 'bullet',
            'DRIVE_PREFIX': '1', 'ROTATION_PREFIX': '1', 'SUPPORT_PIPELINE': '1',
            'REPLICA_PIPELINE': '1', 'GROUND_RAY_BATCH': '1',
        }
        for name, value in defaults.items():
            os.environ.setdefault(f'SOLBREAKER_{name}', value)
    os.environ['SOLBREAKER_MAP_PREWARM'] = '0'
    os.environ.pop('SOLBREAKER_DIAGNOSTIC_DIR', None)
    if args.diagnostic_dir is not None:
        from .diagnostics import install
        args.diagnostic_dir.mkdir()
        install(args.diagnostic_dir)
    report = {'schema': 1, 'version': __version__, 'game_build': BUILD_ID, 'host': host_info(),
              'mode': f'{args.engine}_udp_loopback_scripted_clients', 'target_step_hz': STEP_HZ,
              'requested_seconds': args.seconds, 'warmup_seconds': args.warmup, 'vehicle': args.vehicle,
              'cold_map_metadata': args.cold, 'input_pattern': args.input_pattern, 'diagnostics_enabled': args.diagnostic_dir is not None,
              'dynamic_backend': os.environ.get('SOLBREAKER_DYNAMIC_BACKEND', 'bullet'),
              'complete': False, 'runs': []}
    if 'solo' in scenarios and any(tank not in (2, None) for tank in tanks):
        raise ValueError('Solo benchmark uses exactly two tanks. Select bots or players for denser battles')
    cases = product(maps, scenarios, counts, tanks, range(args.repeat))
    with (tempfile.TemporaryDirectory(prefix='solbreaker-benchmark-', dir=args.work_dir) as temporary,
          args.output.open('x', encoding='utf-8') as output):
        def checkpoint():
            output.seek(0)
            json.dump(report, output, indent=2, ensure_ascii=False, allow_nan=False)
            output.write('\n')
            output.truncate()
            output.flush()

        checkpoint()
        state = Path(temporary) / 'state'
        prepare(args.game, state, vehicle=args.vehicle)
        for map_key, scenario, count, requested_tanks, repetition in cases:
            label = requested_tanks or 'auto'
            print(f'{map_key} / {scenario} / {count} arena(s) x {label} tanks, run {repetition + 1}',
                  file=sys.stderr, flush=True)
            try:
                detail = asyncio.run(_scenario(state, map_key, scenario, count, requested_tanks, args.seconds, args.warmup,
                                               args.seed + repetition, cold=args.cold, pattern=args.input_pattern))
            except (OSError, ValueError, RuntimeError) as error:
                # Resource errors may contain absolute install paths. Keep
                # those in the local console, outside the shareable report.
                detail = {'map': map_key, 'scenario': scenario, 'concurrent_arenas': count,
                          'requested_tanks': requested_tanks, 'seed': args.seed + repetition, 'verdict': 'error', 'error': type(error).__name__}
                print(f'  {error}', file=sys.stderr, flush=True)
            else:
                lowest = min(a['steps_per_second'] for a in detail['arenas'])
                print(f'  {detail["verdict"]}: lowest {lowest:g} steps/s, '
                      f'tick p99 {detail["tick_work"]["p99_ms"]} ms', file=sys.stderr, flush=True)
            report['runs'].append(detail)
            checkpoint()
        report['complete'] = True
        checkpoint()
    print(f'Benchmark JSON: {args.output}')
    write_html(report, html)
    print(f'Benchmark HTML: {html}')
    return 1 if any(run['verdict'] == 'error' for run in report['runs']) else 0
