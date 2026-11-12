"""One server-owned process for speculative static map preparation."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import multiprocessing as mp
import os
from pathlib import Path
import time

from .map_cache import DEFAULT_BYTES, MapCache


def settings(lab):
    enabled = os.environ.get('SOLBREAKER_MAP_PREWARM', '1')
    limit = int(os.environ.get('SOLBREAKER_MAP_CACHE_MIB', str(DEFAULT_BYTES >> 20)))
    if enabled not in ('0', '1') or not 0 <= limit <= 65536:
        raise ValueError('Expected SOLBREAKER_MAP_PREWARM=0 or 1 and SOLBREAKER_MAP_CACHE_MIB=0..65536')
    return (MapCache(Path(lab) / 'cache' / 'maps', limit << 20) if limit else None,
            enabled == '1')


def _prepare(cache, spec, map_key, game, urgent, result):
    from .map_catalog import load_map_spec
    from .navigation_cache import prewarm
    from .terrain import MapTerrain

    try:
        if spec is None:
            spec = load_map_spec(game, map_key)
        terrain = MapTerrain(game, spec)
        prepared, metrics = cache.acquire(spec, terrain, may_evict=lambda: urgent.value)
        if prepared is None:
            result.send(('failed', {'reason': 'Cache capacity or entry is unavailable', **metrics}))
        else:
            try:
                metrics.update(prewarm(prepared, terrain, may_evict=lambda: urgent.value))
            finally:
                prepared.close()
            result.send(('ready', metrics))
    except Exception as error:
        result.send(('failed', {'reason': f'{type(error).__name__}: {error}'[:512]}))
    finally:
        result.close()


@dataclass
class Preparation:
    map_id: int
    map_key: str
    spec: object | None
    state: str = 'missing'
    requested: Demand | None = None


@dataclass
class Demand:
    job: Preparation
    ready: asyncio.Future
    waiters: int = 0


class MapPrewarm:
    def __init__(self, cache, game, catalog, specs, default, *, enabled, busy, event):
        self.cache, self.game = cache, game
        self.busy, self.event = busy, event
        self.jobs = {map_id: Preparation(map_id, key, specs.get(map_id))
                     for map_id, key in catalog.items()}
        self.last_file = cache.root / 'last-map'
        try:
            with self.last_file.open('rb') as stream:
                last = int(stream.read(32))
        except (OSError, ValueError):
            last = default
        if last not in self.jobs:
            last = default
        self.order = ([last] if last in self.jobs else []) + [key for key in self.jobs if key != last]
        self.enabled = enabled
        self.wakeup = asyncio.Event()
        self.runner = None
        self.process = None
        self.current = None
        self.interrupted = False
        self.closing = False
        self.full = False
        self.urgent = None

    def start(self):
        if self.enabled:
            for job in self.jobs.values():
                job.state = 'queued'
            self.runner = asyncio.create_task(self._run())

    def selected(self, map_id):
        self.selected_id = map_id
        if self.current is not None and self.current.map_id != map_id:
            self.pause()
        self.wakeup.set()

    def pause(self):
        if self.current is not None and self.current.requested is None and not self.interrupted:
            self.interrupted = True
            if self.process.is_alive():
                self.process.terminate()

    def request(self, spec):
        self.selected(spec.map_id)
        if self.runner is None or self.closing:
            return None
        job = self.jobs.setdefault(spec.map_id, Preparation(spec.map_id, spec.key, spec))
        if job.state == 'ready':
            return None
        if job.requested is None:
            job.requested = Demand(job, asyncio.get_running_loop().create_future())
            if job is self.current:
                self.urgent.value = True
            else:
                job.state = 'queued'
        self.wakeup.set()
        job.requested.waiters += 1
        return job.requested

    def release(self, demand):
        job = demand.job
        demand.waiters -= 1
        if demand.waiters == 0 and job.requested is demand:
            job.requested = None
            demand.ready.cancel()
            if job is self.current:
                self.urgent.value = False

    def _next(self):
        urgent = next((job for job in self.jobs.values()
                       if job.state == 'queued' and job.requested is not None), None)
        if urgent is not None or self.full or self.busy():
            return urgent
        return next((self.jobs[key] for key in self.order if self.jobs[key].state == 'queued'), None)

    async def _run(self):
        context = mp.get_context('spawn')
        while not self.closing:
            job = self._next()
            if job is None:
                self.wakeup.clear()
                try:
                    await asyncio.wait_for(self.wakeup.wait(), .25)
                except TimeoutError:
                    pass
                continue
            reader, writer = context.Pipe(duplex=False)
            urgent = context.RawValue('b', job.requested is not None)
            process = context.Process(target=_prepare,
                                      args=(self.cache, job.spec, job.map_key, self.game, urgent, writer),
                                      name='Solbreaker map preparation', daemon=True)
            started = time.monotonic()
            job.state = 'preparing'
            self.process, self.current, self.urgent = process, job, urgent
            self.event('map_preparation_started', map_id=job.map_id, map_key=job.map_key)
            try:
                process.start()
                writer.close()
                while process.is_alive():
                    if self.busy() and job.requested is None:
                        self.pause()
                    await asyncio.sleep(.05)
                if reader.poll():
                    state, metrics = reader.recv()
                else:
                    state, metrics = 'failed', {'reason': f'Map worker exited with {process.exitcode}'}
            except (OSError, EOFError, ValueError) as error:
                state, metrics = 'failed', {'reason': str(error)[:512]}
            finally:
                reader.close()
                writer.close()
                if process.pid is not None:
                    if process.is_alive():
                        process.terminate()
                    await asyncio.to_thread(process.join, 1.)
                    if process.is_alive():
                        process.kill()
                        await asyncio.to_thread(process.join)
                process.close()
                self.current = self.process = self.urgent = None
            if self.closing:
                break
            if self.interrupted:
                self.interrupted = False
                job.state = 'queued'
                self.event('map_preparation_paused', map_id=job.map_id, map_key=job.map_key)
                continue
            job.state = state
            if job.requested is None and metrics.get('cache_unavailable') == 'budget':
                # Warming another map must never evict the preferred map
                # prepared at startup. Foreground requests can evict idle entries.
                self.full = True
            if job.requested is not None:
                job.requested.ready.set_result(state)
                job.requested = None
            self.event('map_preparation', map_id=job.map_id, map_key=job.map_key,
                       phase=state, duration_ms=(time.monotonic()-started)*1000, **metrics)

    async def close(self):
        self.closing = True
        self.wakeup.set()
        if self.process is not None and self.process.is_alive():
            self.process.terminate()
        try:
            if self.runner is not None:
                await self.runner
        finally:
            for job in self.jobs.values():
                if job.requested is not None:
                    job.requested.ready.cancel()
            selected_id = getattr(self, 'selected_id', None)
            if selected_id is not None:
                await asyncio.to_thread(self._remember, selected_id)
            await asyncio.to_thread(self.cache.cleanup_stages)

    def _remember(self, selected_id):
        import tempfile

        try:
            self.cache.root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.cache.root, prefix='.last-map-',
                                             mode='w', delete=False) as stream:
                path = Path(stream.name)
                stream.write(str(selected_id))
            try:
                path.replace(self.last_file)
            finally:
                path.unlink(missing_ok=True)
        except OSError:
            pass
