"""Standalone offline inspector for .wotbreplay capture files.

Reads the Solbreaker RC1 replay container (a ZIP with root meta.json and
data.wotreplay) without extracting anything to disk, parses the pinned
capture stream format incrementally, and offers:

  summary  -- overview and per-entity statistics
  export   -- timestamps and world positions of one entity as CSV
  html     -- self-contained offline playback view (map-aware V2 viewer
              with top-down and 3D modes)
  compare  -- positional comparison of one entity from each of two replays

Supported stream evidence (pinned protocol):
  * little-endian magic 0x12345678 followed by an 8-byte prefix (12 bytes total)
  * u8-length byte string, u8-length ASCII client version, one more byte
  * records: little-endian <IIf payload length, packet kind, seconds> + payload
  * kind 0x0a: 49-byte little-endian <III9fB>; entity id = first uint32,
    world x/y/z = the first three float32 after the three uint32s
All other packet kinds and record fields are opaque and only counted.

Positions are evidence of where entities were, nothing more: this tool never
infers hits, damage, or causality from positions.

Standard library only; no network access, no archive extraction. The html
command additionally supports an optional local client root (--game-dir);
without it the generated page runs on a generic coordinate grid.
"""
import argparse
import csv
import io
import json
import math
import struct
import sys
import zipfile
import zlib
from collections import Counter
from itertools import chain, pairwise

MAGIC = 0x12345678
HEADER_STRUCT = struct.Struct('<IIf')
KIND_POSITION = 0x0A
POSITION_STRUCT = struct.Struct('<III9fB')
PREFIX_SIZE = 12
META_MEMBER = 'meta.json'
DATA_MEMBER = 'data.wotreplay'
# Hard bounds so hostile or corrupt archives cannot exhaust memory. Values
# count uncompressed bytes read from a single member.
DEFAULT_MAX_BYTES = 64 << 20
DEFAULT_MAX_SAMPLES = 2_000_000
MAX_META_BYTES = 1 << 20
MAX_META_STRING = 4000


class ReplayError(Exception):
    """Fatal, user-facing replay problem (bad archive, missing members...)."""


class PositionSample:
    __slots__ = ('time', 'x', 'y', 'z')

    def __init__(self, time, x, y, z):
        self.time = time
        self.x = x
        self.y = y
        self.z = z


class ReplayData:
    def __init__(self, source):
        self.source = source
        self.meta = {}
        self.client_version = ''
        self.records_by_kind = Counter()
        self.truncated_records = 0
        self.skipped_position_records = 0
        self.warnings = []
        # entity id -> list[PositionSample], kept sorted by time
        self.entities = {}

    def entity_ids(self):
        return sorted(self.entities)


def _member(archive, name, max_bytes):
    try:
        info = archive.getinfo(name)
    except KeyError:
        raise ReplayError(f'archive is missing {name!r} at the root')
    if info.file_size > max_bytes:
        raise ReplayError(f'{name!r} is {info.file_size} bytes, over the {max_bytes}-byte limit')
    return info


def _sanitize_meta(meta):
    """Keep only printable scalar meta values, bounded in size.

    meta.json comes from an untrusted capture: drop nested structures and
    anything non-finite so downstream consumers (terminal, CSV, HTML) only
    ever see plain text-friendly scalars.
    """
    clean = {}
    for key, value in meta.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, bool):
            clean[key] = value
        elif isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                continue
            clean[key] = value
        elif isinstance(value, str):
            clean[key] = value[:MAX_META_STRING]
    return clean


def _load_meta(archive, max_bytes, warnings):
    try:
        raw = archive.read(_member(archive, META_MEMBER, min(max_bytes, MAX_META_BYTES)))
    except ReplayError as error:
        warnings.append(str(error))
        return {}
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        warnings.append('meta.json is not valid UTF-8; metadata ignored')
        return {}
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        warnings.append('meta.json is not valid JSON; metadata ignored')
        return {}
    if not isinstance(parsed, dict):
        warnings.append('meta.json root is not an object; metadata ignored')
        return {}
    return _sanitize_meta(parsed)


def _read_u8_blob(stream):
    length = stream.read(1)
    if not length:
        raise ReplayError('stream header is truncated')
    size = length[0]
    blob = stream.read(size)
    if len(blob) != size:
        raise ReplayError('stream header blob is truncated')
    return blob


def parse_stream(stream, file_size, max_samples, warnings):
    """Parse the data.wotreplay payload; returns (client_version, entities, counts)."""
    prefix = stream.read(PREFIX_SIZE)
    if len(prefix) < PREFIX_SIZE:
        raise ReplayError('capture stream is shorter than the 12-byte prefix')
    magic = struct.unpack_from('<I', prefix)[0]
    if magic != MAGIC:
        raise ReplayError(f'bad stream magic 0x{magic:08x}, expected 0x{MAGIC:08x}')
    _read_u8_blob(stream)                    # unknown byte string
    version_blob = _read_u8_blob(stream)
    try:
        client_version = version_blob.decode('ascii')
    except UnicodeDecodeError:
        client_version = ''
        warnings.append('client version is not ASCII')
    if not stream.read(1):                   # trailing byte
        raise ReplayError('stream header is truncated')

    entities = {}
    counts = Counter()
    truncated = 0
    skipped = 0
    bad_sizes = 0
    samples = 0
    while header := stream.read(HEADER_STRUCT.size):
        if len(header) != HEADER_STRUCT.size:
            truncated += 1
            warnings.append('trailing bytes after the last complete record header')
            break
        length, kind, seconds = HEADER_STRUCT.unpack(header)
        remaining = file_size - stream.tell()
        if length > remaining:
            truncated += 1
            warnings.append(f'record of kind 0x{kind:02x} is truncated '
                            f'({length} bytes declared, {remaining} available)')
            break
        counts[kind] += 1
        if kind != KIND_POSITION or length != POSITION_STRUCT.size:
            if kind == KIND_POSITION:
                skipped += 1
                bad_sizes += 1
            stream.seek(length, io.SEEK_CUR)
            continue
        payload = stream.read(length)
        if len(payload) != length:
            raise ReplayError('position record ended before the advertised member size')
        (entity, _a, _b, x, y, z, *_opaque) = POSITION_STRUCT.unpack(payload)
        # Pinned layout: <III9fB>. The remaining six float32 and the final
        # u8 are opaque in the public source and intentionally unused.
        values = (seconds, x, y, z)
        if not all(math.isfinite(value) for value in values):
            skipped += 1
            continue
        if samples >= max_samples:
            warnings.append(f'sample limit of {max_samples} reached; remaining records ignored')
            break
        entities.setdefault(entity, []).append(PositionSample(seconds, x, y, z))
        samples += 1
    if bad_sizes:
        warnings.append(f'{bad_sizes} position record(s) with unexpected size; skipped')
    for series in entities.values():
        series.sort(key=lambda sample: sample.time)
    return client_version, entities, counts, truncated, skipped


def load_replay(path, max_bytes=DEFAULT_MAX_BYTES, max_samples=DEFAULT_MAX_SAMPLES):
    """Open a .wotbreplay ZIP and parse it without extracting to disk."""
    replay = ReplayData(str(path))
    try:
        with zipfile.ZipFile(path) as archive:
            replay.meta = _load_meta(archive, max_bytes, replay.warnings)
            info = _member(archive, DATA_MEMBER, max_bytes)
            with archive.open(info) as stream:
                parsed = parse_stream(stream, info.file_size, max_samples, replay.warnings)
        (replay.client_version, replay.entities, replay.records_by_kind,
         replay.truncated_records, replay.skipped_position_records) = parsed
    except zipfile.BadZipFile as error:
        raise ReplayError(f'not a valid ZIP archive: {error}') from error
    except (OSError, EOFError, RuntimeError, zlib.error) as error:
        raise ReplayError(f'cannot read replay: {error}') from error
    return replay


# ---------------------------------------------------------------- statistics

def series_stats(series):
    first, last = series[0], series[-1]
    duration = last.time - first.time
    xs = [s.x for s in series]
    ys = [s.y for s in series]
    zs = [s.z for s in series]
    distance = 0.0
    for prev, nxt in zip(series, series[1:]):
        distance += math.dist((prev.x, prev.y, prev.z), (nxt.x, nxt.y, nxt.z))
    return {
        'samples': len(series),
        'start': first.time,
        'end': last.time,
        'duration': duration,
        'x_bounds': (min(xs), max(xs)),
        'y_bounds': (min(ys), max(ys)),
        'z_bounds': (min(zs), max(zs)),
        'path_length': distance,
        'mean_speed': distance / duration if duration > 0 else 0.0,
    }


def window(series, start=None, end=None):
    if start is None and end is None:
        return list(series)
    return [s for s in series
            if (start is None or s.time >= start) and (end is None or s.time <= end)]


def format_seconds(value):
    return f'{value:.3f}'


def _load_for_command(path, max_bytes):
    replay = load_replay(path, max_bytes)
    for warning in replay.warnings:
        print(f'warning: {warning}', file=sys.stderr)
    return replay


def cmd_summary(args):
    replay = _load_for_command(args.replay, args.max_bytes)
    print(f'Replay: {replay.source}')
    if replay.meta:
        print('Metadata:')
        for key in sorted(replay.meta):
            print(f'  {key}: {replay.meta[key]}')
    else:
        print('Metadata: none (missing or unusable meta.json)')
    print(f'Client version: {replay.client_version or "unknown"}')
    print(f'Records by kind: {", ".join(f"0x{kind:02x} x{count}" for kind, count in sorted(replay.records_by_kind.items())) or "none"}')
    print(f'Position records skipped: {replay.skipped_position_records}')
    if replay.truncated_records:
        print(f'Truncated records: {replay.truncated_records}')
    ids = replay.entity_ids()
    print(f'Entities with position samples: {len(ids)}')
    for entity in ids:
        stats = series_stats(replay.entities[entity])
        print(f'\nEntity {entity}:')
        print(f'  samples: {stats["samples"]}')
        print(f'  time span: {format_seconds(stats["start"])} .. {format_seconds(stats["end"])} s'
              f' (duration {format_seconds(stats["duration"])} s)')
        print(f'  x range: {stats["x_bounds"][0]:.3f} .. {stats["x_bounds"][1]:.3f}')
        print(f'  y range: {stats["y_bounds"][0]:.3f} .. {stats["y_bounds"][1]:.3f}')
        print(f'  z range: {stats["z_bounds"][0]:.3f} .. {stats["z_bounds"][1]:.3f}')
        print(f'  path length: {stats["path_length"]:.3f} m, mean speed {stats["mean_speed"]:.3f} m/s')
    return 0


def _select_entity(replay, entity_id):
    try:
        entity = int(entity_id, 0)
    except ValueError:
        raise ReplayError(f'invalid entity id {entity_id!r}')
    if entity not in replay.entities:
        available = ', '.join(str(i) for i in replay.entity_ids()) or 'none'
        raise ReplayError(f'entity {entity} has no position samples (available: {available})')
    return entity


def cmd_export(args):
    replay = _load_for_command(args.replay, args.max_bytes)
    entity = _select_entity(replay, args.entity)
    series = window(replay.entities[entity], args.start, args.end)
    if not series:
        raise ReplayError('no samples in the requested time window')
    out = open(args.output, 'w', newline='', encoding='utf-8') if args.output else sys.stdout
    close = out is not sys.stdout
    try:
        writer = csv.writer(out)
        writer.writerow(['time_s', 'entity_id', 'x', 'y', 'z'])
        for sample in series:
            writer.writerow([format_seconds(sample.time), entity,
                             f'{sample.x:.6f}', f'{sample.y:.6f}', f'{sample.z:.6f}'])
    finally:
        if close:
            out.close()
    return 0


# ------------------------------------------------------------- HTML playback
#
# The page itself lives in replay_viewer.py and is imported lazily so
# the summary/export/compare commands and the no-client html path stay
# standard-library only.

def _load_viewer():
    from . import replay_viewer
    return replay_viewer


def build_html(replay, selected_entity=None):
    """Self-contained V2 viewer page (generic grid when no map is available)."""
    viewer = _load_viewer()
    context = viewer.resolve_map(replay)
    return viewer.build_viewer_html(replay, context, selected_entity=selected_entity)


def cmd_html(args):
    replay = _load_for_command(args.replay, args.max_bytes)
    selected = None
    if args.entity is not None:
        selected = _select_entity(replay, args.entity)
    viewer = _load_viewer()
    player = None
    if args.player_entity is not None:
        try:
            player = _select_entity(replay, args.player_entity)
        except ReplayError as error:
            raise ReplayError(str(error).replace('entity', '--player-entity entity',
                                                 1)) from error
    dims = None
    if args.vehicle_dims is not None:
        try:
            dims = viewer.parse_vehicle_dims(args.vehicle_dims)
        except viewer.ViewerError as error:
            raise ReplayError(str(error)) from error
    terrain_grid = args.terrain_grid
    if terrain_grid is None:
        terrain_grid = viewer.DEFAULT_GRID
    elif not viewer.MIN_GRID <= terrain_grid <= viewer.MAX_GRID:
        raise ReplayError(f'--terrain-grid must be between {viewer.MIN_GRID} and '
                          f'{viewer.MAX_GRID}')
    try:
        context = viewer.resolve_map(replay, game_dir=args.game_dir,
                                     map_key=args.map,
                                     max_grid=terrain_grid)
    except viewer.ViewerError as error:
        raise ReplayError(str(error)) from error
    for note in context.diagnostics:
        print(f'note: {note}', file=sys.stderr)
    try:
        page = viewer.build_viewer_html(replay, context, selected_entity=selected,
                                        player_entity=player, dims=dims)
    except viewer.ViewerError as error:
        raise ReplayError(str(error)) from error
    out = open(args.output, 'w', encoding='utf-8') if args.output else sys.stdout
    close = out is not sys.stdout
    try:
        out.write(page)
    finally:
        if close:
            out.close()
    return 0


# ---------------------------------------------------------------- comparison

def _linear(series):
    """Return f(t) interpolating (x, y, z) linearly; clamps outside the span."""
    times = [s.time for s in series]
    pts = [(s.x, s.y, s.z) for s in series]

    def at(t):
        if len(times) == 1:
            return pts[0]
        if t <= times[0]:
            return pts[0]
        if t >= times[-1]:
            return pts[-1]
        lo, hi = 0, len(times) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if times[mid] <= t:
                lo = mid
            else:
                hi = mid
        span = times[hi] - times[lo]
        f = (t - times[lo]) / span if span > 0 else 0.0
        a, b = pts[lo], pts[hi]
        return tuple(a[i] + (b[i] - a[i]) * f for i in range(3))

    return at


def _shift(series, offset):
    return [PositionSample(s.time + offset, s.x, s.y, s.z) for s in series]


def _overlap_window(series_a, series_b):
    start = max(series_a[0].time, series_b[0].time)
    end = min(series_a[-1].time, series_b[-1].time)
    return start, end


def _mean_speed_in_overlap(series, at, start, end):
    if start == end:
        return 0.0
    points = chain(
        (at(start),),
        ((s.x, s.y, s.z) for s in series if start < s.time < end),
        (at(end),),
    )
    return sum(math.dist(a, b) for a, b in pairwise(points)) / (end - start)


def cmd_compare(args):
    replay_a = _load_for_command(args.replay_a, args.max_bytes)
    replay_b = _load_for_command(args.replay_b, args.max_bytes)
    entity_a = _select_entity(replay_a, args.entity_a)
    entity_b = _select_entity(replay_b, args.entity_b)
    series_a = replay_a.entities[entity_a]
    series_b = replay_b.entities[entity_b]
    if args.align == 'offset':
        series_a = _shift(series_a, args.offset_a)
        series_b = _shift(series_b, args.offset_b)
    else:  # start: move each series' first sample to t=0
        series_a = _shift(series_a, -series_a[0].time)
        series_b = _shift(series_b, -series_b[0].time)
    start, end = _overlap_window(series_a, series_b)
    if end < start:
        raise ReplayError(f'no overlapping interval after alignment '
                          f'([{format_seconds(series_a[0].time)}, {format_seconds(series_a[-1].time)}] vs '
                          f'[{format_seconds(series_b[0].time)}, {format_seconds(series_b[-1].time)}])')
    probes = (start,) if start == end else chain(
        (start,), (s.time for s in series_a if start < s.time < end), (end,),
    )
    at_a, at_b = _linear(series_a), _linear(series_b)
    distances = []
    for t in probes:
        pa, pb = at_a(t), at_b(t)
        horizontal = math.hypot(pa[0] - pb[0], pa[2] - pb[2])
        spatial = math.dist(pa, pb)
        distances.append((t, horizontal, spatial))
    mean_h = sum(d[1] for d in distances) / len(distances)
    max_h = max(distances, key=lambda d: d[1])
    min_h = min(distances, key=lambda d: d[1])
    mean_s = sum(d[2] for d in distances) / len(distances)
    speed_a = _mean_speed_in_overlap(series_a, at_a, start, end)
    speed_b = _mean_speed_in_overlap(series_b, at_b, start, end)
    print(f'A: {replay_a.source} entity {entity_a}')
    print(f'B: {replay_b.source} entity {entity_b}')
    print(f'Alignment: {args.align}' +
          (f' (offset_a={args.offset_a:+.3f}s, offset_b={args.offset_b:+.3f}s)'
           if args.align == 'offset' else ' (each series shifted so its first sample is t=0)'))
    print(f'Overlap: {format_seconds(start)} .. {format_seconds(end)} s '
          f'({format_seconds(end - start)} s, {len(distances)} probe times)')
    print(f'Mean horizontal distance (x/z plane): {mean_h:.3f} m')
    print(f'Min horizontal distance: {min_h[1]:.3f} m at t={format_seconds(min_h[0])} s')
    print(f'Max horizontal distance: {max_h[1]:.3f} m at t={format_seconds(max_h[0])} s')
    print(f'Mean 3D distance: {mean_s:.3f} m')
    print(f'Mean speed in overlap: A {speed_a:.3f} m/s, B {speed_b:.3f} m/s')
    print('Note: distances compare positions only; they say nothing about hits or causality.')
    return 0


# -------------------------------------------------------------------- entry

def _entity_arg(parser):
    parser.add_argument('--entity', required=True,
                        help='entity id (decimal or 0x-prefixed hex); see summary for available ids')


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--max-bytes', type=int, default=DEFAULT_MAX_BYTES,
                        help=f'max bytes read from one archive member (default {DEFAULT_MAX_BYTES})')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('summary', help='overview and per-entity statistics')
    p.add_argument('replay')
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser('export', help='CSV of timestamps and world positions for one entity')
    p.add_argument('replay')
    _entity_arg(p)
    p.add_argument('--start', type=float, default=None, help='window start in replay seconds')
    p.add_argument('--end', type=float, default=None, help='window end in replay seconds')
    p.add_argument('-o', '--output', help='output file (default: stdout)')
    p.set_defaults(func=cmd_export)

    p = sub.add_parser('html', help='self-contained offline playback view (top-down + 3D)')
    p.add_argument('replay')
    p.add_argument('--entity', help='preselect this entity id')
    p.add_argument('--game-dir', help='optional local client root used to resolve map terrain')
    p.add_argument('--map', help='validated map key override (see the map catalog)')
    p.add_argument('--player-entity',
                   help='explicitly designate the player entity id; the verified '
                        'playerVehicleName metadata is then used as its label')
    p.add_argument('--vehicle-dims', metavar='L,W,H',
                   help='schematic tank dimensions in metres (default 6.6,3.2,2.4)')
    p.add_argument('--terrain-grid', type=int, default=None,
                   help='max terrain samples per axis embedded in the page (2..512)')
    p.add_argument('-o', '--output', help='output file (default: stdout)')
    p.set_defaults(func=cmd_html)

    p = sub.add_parser('compare', help='compare one entity from each of two replays')
    p.add_argument('replay_a')
    p.add_argument('replay_b')
    p.add_argument('--entity-a', required=True)
    p.add_argument('--entity-b', required=True)
    p.add_argument('--align', choices=('start', 'offset'), default='start',
                   help='start: both series shifted to begin at t=0; '
                        'offset: apply --offset-a/--offset-b (default: start)')
    p.add_argument('--offset-a', type=float, default=0.0, help='seconds added to A when --align offset')
    p.add_argument('--offset-b', type=float, default=0.0, help='seconds added to B when --align offset')
    p.set_defaults(func=cmd_compare)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_bytes <= 0:
        parser.error('--max-bytes must be positive')
    for setting in ('start', 'end', 'offset_a', 'offset_b'):
        value = getattr(args, setting, None)
        if value is not None and not math.isfinite(value):
            parser.error(f'--{setting.replace("_", "-")} must be finite')
    try:
        return args.func(args)
    except (OSError, ReplayError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
