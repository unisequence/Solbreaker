"""Pure local fire-tick state; Arena decides ignition, HP, and wire effects.

This is an explicit LOCAL linear-decay sandbox policy, not a recovered client
server fire formula.  Every damage rate and the duration are supplied by the
caller.  It has no default burn damage, duration, crew damage, module recovery,
RNG, vehicle ownership, or network behavior.
"""
from dataclasses import dataclass
import math


_MAX_TICKS = 4096


def _finite(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a finite number')
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f'{name} must be a finite number') from None
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise ValueError(f'{name} must be a finite {"positive" if positive else "nonnegative"} number')
    return result


@dataclass(frozen=True)
class FirePolicy:
    """Explicit LOCAL linear-decay fire configuration.

    Damage rate starts at each ``initial_*`` value and declines linearly to
    zero at ``duration``.  ``tick_interval`` is a fixed grid for one policy;
    its default is the published half-second fire tick cadence, never a recovered
    server damage formula.  All damage rates and duration are required.
    """
    initial_hp_damage_per_second: float
    initial_module_damage_per_second: float
    duration: float
    tick_interval: float = .5

    def __post_init__(self):
        hp = _finite(self.initial_hp_damage_per_second, 'initial_hp_damage_per_second', positive=True)
        module = _finite(self.initial_module_damage_per_second,
                         'initial_module_damage_per_second')
        duration = _finite(self.duration, 'duration', positive=True)
        interval = _finite(self.tick_interval, 'tick_interval', positive=True)
        ratio = duration / interval
        if not math.isfinite(ratio) or ratio > _MAX_TICKS:
            raise ValueError(f'Fire policy exceeds {_MAX_TICKS} ticks')
        if not math.isfinite(hp * duration) or not math.isfinite(module * duration):
            raise ValueError('Fire policy total damage must be finite')
        object.__setattr__(self, 'initial_hp_damage_per_second', hp)
        object.__setattr__(self, 'initial_module_damage_per_second', module)
        object.__setattr__(self, 'duration', duration)
        object.__setattr__(self, 'tick_interval', interval)


@dataclass(frozen=True)
class FireTick:
    """One immutable damage interval ending at ``at``; no crew damage exists."""
    at: float
    hp_damage: float
    module_damage: float


class FireState:
    """Independent state for one vehicle under ``FirePolicy``.

    ``advance(now)`` is the sole source of damage ticks and has a monotonic
    clock.  It emits only complete fixed-grid intervals, except the bounded
    final partial interval at natural expiry.  Thus the same policy produces
    the same ticks whether callers advance in short or long steps.

    ``stop(now)`` requires ``advance(now)`` first and returns only whether it
    ended an active fire.  An early stop cancels the not-yet-due fractional
    interval; it never invents, reorders, or repeats a tick.  A second stop at
    the same clock is a no-op.  A live fire ignores a further ``start`` call,
    preserving the original positive source entity and duration.
    """
    __slots__ = ('_policy', '_active', '_started_at', '_finished_at', '_source',
                 '_clock', '_next_tick')

    def __init__(self, policy):
        if not isinstance(policy, FirePolicy):
            raise ValueError('Expected FirePolicy')
        self._policy = policy
        self._active = False
        self._started_at = None
        self._finished_at = None
        self._source = None
        self._clock = None
        self._next_tick = 0

    @property
    def policy(self):
        return self._policy

    @property
    def active(self):
        return self._active

    @property
    def started_at(self):
        return self._started_at

    @property
    def finished_at(self):
        return self._finished_at

    @property
    def source(self):
        """Original source entity for the current or most recently ended fire."""
        return self._source

    def _time(self, now):
        result = _finite(now, 'time')
        if self._clock is not None and result < self._clock:
            raise ValueError('Fire clock cannot go backwards')
        return result

    @staticmethod
    def _source_entity(source):
        if isinstance(source, bool) or not isinstance(source, int) or source <= 0:
            raise ValueError('Fire source must be a positive integer entity')
        return source

    def start(self, now, source):
        """Start one fire, or leave a live fire unchanged and return ``False``."""
        now = self._time(now)
        source = self._source_entity(source)
        if self._active:
            return False
        finished = now + self._policy.duration
        if not math.isfinite(finished) or finished <= now:
            raise ValueError('Fire finish timestamp must be finite and later than start')
        tick_count = math.ceil(self._policy.duration / self._policy.tick_interval)
        # A fixed grid must have distinct absolute timestamps.  Without this,
        # a huge simulation clock can collapse small intervals and emit many
        # same-time ticks despite the bounded policy count.
        previous = now
        for index in range(1, tick_count + 1):
            elapsed = min(index * self._policy.tick_interval, self._policy.duration)
            tick_at = now + elapsed
            if not math.isfinite(tick_at) or tick_at <= previous:
                raise ValueError('Fire tick interval is not representable at start timestamp')
            previous = tick_at
        self._active = True
        self._started_at = now
        self._finished_at = finished
        self._source = source
        self._clock = now
        self._next_tick = 0
        return True

    def _integrated_damage(self, rate, lower, upper):
        """Stable integral of rate * (1 - elapsed / duration) over [lower,upper]."""
        width = upper - lower
        average_factor = 1. - .5 * (lower / self._policy.duration + upper / self._policy.duration)
        return max(0., (rate * width) * average_factor)

    def advance(self, now):
        """Return due immutable ticks through ``now`` without rewinding time."""
        now = self._time(now)
        if not self._active:
            self._clock = now
            return ()
        cutoff = min(now, self._finished_at)
        ticks = []
        while True:
            start = self._next_tick * self._policy.tick_interval
            if start >= self._policy.duration:
                break
            end = min((self._next_tick + 1) * self._policy.tick_interval, self._policy.duration)
            at = self._started_at + end
            if at > cutoff:
                break
            ticks.append(FireTick(at,
                                  self._integrated_damage(self._policy.initial_hp_damage_per_second, start, end),
                                  self._integrated_damage(self._policy.initial_module_damage_per_second, start, end)))
            self._next_tick += 1
        self._clock = now
        if now >= self._finished_at:
            self._active = False
        return tuple(ticks)

    def stop(self, now):
        """Stop at an already advanced timestamp; this method never emits ticks."""
        now = self._time(now)
        if self._clock != now:
            raise ValueError('FireState.stop requires advance(now) first')
        if not self._active:
            return False
        self._active = False
        self._finished_at = now
        return True
