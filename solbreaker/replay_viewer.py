"""V2 map-aware replay viewer: payload builder and self-contained HTML page.

The desktop launcher and the inspector's ``html`` command share this viewer.
Importing this module itself never requires third-party packages; the local
client-data path (``--game-dir``) imports ``solbreaker`` terrain/catalog code
inside the functions that need it and degrades to diagnostics when anything is
unavailable.

Evidence policy (shared with the inspector):

- ``mapName`` and ``mapId`` are independently verified metadata keys and are
  only used to *resolve a map* against the validated local catalog. They never
  identify which entity is the player.
- ``playerVehicleName`` / ``vehicleCompDescriptor`` are likewise verified keys
  but say nothing about entity ownership; the ``playerVehicleName`` label is
  attached only to an entity the user explicitly designates with
  ``--player-entity``.
- Recorded ``x``/``y``/``z`` are world metres, seconds are replay seconds.
- Heading, speed, teleports and height-above-terrain are *inferred* from
  measured movement and local terrain sampling; every such value is labelled
  approximate in the viewer.

No game meshes, textures, scenes or original art are embedded or distributed:
terrain shading and contours are generated from numeric elevation, tanks are
original schematic boxes, and an optional minimap image is chosen locally in
the browser (never uploaded, never copied into the HTML).
"""
import json
import math
import struct
from array import array
from dataclasses import dataclass, field

DEFAULT_GRID = 160          # terrain samples per axis embedded in the page
MIN_GRID = 2                # tiny values exist for synthetic test fixtures
MAX_GRID = 512
MAX_HEIGHTMAP_SIZE = 2048   # raw client heightmap edge, samples
MAX_HTML_BYTES = 32 << 20   # hard cap on the generated page
MAX_VIEWER_SAMPLES = 400_000  # above this, series are evenly thinned

# Inferred-motion thresholds.
MOVE_EPS = 0.05            # metres; smaller steps count as a stop
TELEPORT_DISTANCE = 60.0   # metres in one step
TELEPORT_SPEED = 45.0      # metres per second sustained between two samples

# Schematic tank dimensions (metres). Explicit defaults only; real hull,
# turret and barrel shapes/angles are not implied.
DEFAULT_VEHICLE = {'length': 6.6, 'width': 3.2, 'height': 2.4}

VERIFIED_META_KEYS = (
    'arenaUniqueId', 'battleStartTime', 'battleDuration',
    'mapName', 'mapId', 'playerVehicleName', 'vehicleCompDescriptor',
)


class ViewerError(Exception):
    """Fatal, user-facing viewer problem (bad override, oversized output...)."""


@dataclass
class MapContext:
    """How the viewer grounds the replay: real terrain or a generic grid."""
    mode: str = 'grid'                  # 'terrain' or 'grid'
    key: str | None = None
    map_id: int | None = None
    bounds: tuple | None = None         # map border (min_x, min_z, max_x, max_z)
    bbox: tuple | None = None           # landscape bbox (x0, z0, y0, x1, z1, y1)
    grid: dict | None = None            # embedded elevation grid (see _sample_terrain)
    diagnostics: list = field(default_factory=list)


# ------------------------------------------------------------ map resolution

def _meta_map_name(meta):
    value = meta.get('mapName')
    return value if isinstance(value, str) else None


def _meta_map_id(meta):
    # bool is a subclass of int; reject it explicitly.
    value = meta.get('mapId')
    return value if type(value) is int else None


def _default_catalog(diagnostics):
    """Validated name -> id table from the installed-build catalog module."""
    try:
        from solbreaker.map_catalog import _SUPPORTED, WEATHER_MAPS
    except ImportError as error:
        diagnostics.append(
            f'map catalog unavailable ({error}); map metadata cannot be validated')
        return {}
    catalog = dict(_SUPPORTED)
    catalog.update({key: ident for key, (ident, _base, _tag) in WEATHER_MAPS.items()})
    return catalog


def validate_map_reference(meta, catalog):
    """Validate mapName/mapId metadata against a name -> id catalog table.

    Returns ``(key, map_id)`` or ``None`` when the metadata names no map.
    Raises ViewerError when a present value cannot be validated.
    """
    name = _meta_map_name(meta)
    ident = _meta_map_id(meta)
    if name is None and ident is None:
        return None
    if name is not None and name not in catalog:
        raise ViewerError(
            f'mapName {name!r} is not in the validated map catalog')
    if ident is not None and ident not in catalog.values():
        raise ViewerError(f'mapId {ident} is not in the validated map catalog')
    if name is not None:
        key = name
        if ident is not None and catalog[key] != ident:
            raise ViewerError(
                f'map metadata conflict: mapName {name!r} is id {catalog[key]}, '
                f'but mapId says {ident}')
        return key, catalog[key]
    key = next(k for k, v in catalog.items() if v == ident)
    return key, ident


def _default_loader():
    from solbreaker.map_catalog import load_map_spec

    def load(game, key):
        return load_map_spec(game, key)
    return load


def _default_reader():
    from solbreaker.dvpl import read
    return read


def resolve_map(replay, *, game_dir=None, map_key=None, catalog=None,
                loader=None, reader=None, max_grid=DEFAULT_GRID):
    """Resolve the replay's map against the local catalog and sample terrain.

    ``loader(game, key)`` must return a MapSpec-like object (attributes
    ``key``, ``map_id``, ``bounds``, ``landscape_bbox``, ``heightmap_path``);
    ``reader(path)`` must return the decoded resource bytes. Both are
    injectable so tests need no client tree. Without a usable client tree the
    result is a generic-grid context whose diagnostics say exactly why.
    """
    diagnostics = []
    if catalog is None:
        catalog = _default_catalog(diagnostics)
    context = MapContext(diagnostics=diagnostics)
    key = None
    if map_key is not None:
        if catalog and map_key not in catalog:
            raise ViewerError(
                f'--map {map_key!r} is not in the validated map catalog')
        key = map_key
        if game_dir is None:
            diagnostics.append(
                f'map override {map_key!r} accepted, but without --game-dir '
                'the background stays a generic coordinate grid')
    elif catalog:
        try:
            reference = validate_map_reference(replay.meta, catalog)
        except ViewerError as error:
            diagnostics.append(f'{error}; falling back to the generic grid')
        else:
            if reference is not None:
                key, context.map_id = reference
    elif _meta_map_name(replay.meta) is not None or _meta_map_id(replay.meta) is not None:
        diagnostics.append('map metadata is present but the map catalog is '
                           'unavailable; the map was not validated')
    context.key = key
    if game_dir is None:
        if key is None:
            diagnostics.append('no --game-dir and no usable map metadata: '
                               'showing a generic coordinate grid')
        else:
            diagnostics.append('no --game-dir: showing a generic coordinate '
                               f'grid ({key} noted from validated metadata)')
        return context
    if key is None:
        diagnostics.append('--game-dir given, but neither the metadata nor '
                           '--map identifies a map; terrain not loaded '
                           '(generic grid). Pass --map KEY to override')
        return context
    if loader is None:
        loader = _default_loader()
    if reader is None:
        reader = _default_reader()
    try:
        spec = loader(game_dir, key)
    except (ValueError, OSError) as error:
        diagnostics.append(f'map {key!r} could not be loaded: {error}; '
                           'generic grid fallback')
        return context
    try:
        context.grid = sample_terrain(spec, max_grid=max_grid, reader=reader)
    except (ValueError, OSError, struct.error) as error:
        diagnostics.append(f'terrain sampling failed for {key!r}: {error}; '
                           'generic grid fallback')
        return context
    context.mode = 'terrain'
    context.key = spec.key
    context.map_id = spec.map_id
    context.bounds = tuple(float(v) for v in spec.bounds)
    context.bbox = tuple(float(v) for v in spec.landscape_bbox)
    return context


# ------------------------------------------------------------------ terrain

def _decode_heightmap(raw):
    """Untile a client heightmap into row-major uint16 (row = z, col = x)."""
    if len(raw) < 8:
        raise ValueError('truncated heightmap')
    size, tile = struct.unpack_from('<II', raw)
    if size < 2 or tile < 1 or size % tile or len(raw) != 8 + size * size * 2:
        raise ValueError('unsupported heightmap layout')
    if size > MAX_HEIGHTMAP_SIZE:
        raise ValueError(f'heightmap edge {size} exceeds the '
                         f'{MAX_HEIGHTMAP_SIZE} limit')
    heights = array('H', bytes(2 * size * size))
    offset = 8
    for ty in range(0, size, tile):
        for tx in range(0, size, tile):
            for y in range(tile):
                at = (ty + y) * size + tx
                heights[at:at + tile] = array('H', raw[offset:offset + 2 * tile])
                offset += 2 * tile
    return heights, size


def sample_terrain(spec, *, max_grid=DEFAULT_GRID, reader=None):
    """Sample a validated MapSpec heightmap into a bounded embedding grid.

    Returns a dict with the grid size, the exact world coordinates of the
    first/last node on each axis, the landscape y range and the raw uint16
    elevation rows (row = z, col = x). Sampling is strided, so hostile sizes
    stay bounded; ``max_grid`` is clamped to MAX_GRID.
    """
    if reader is None:
        reader = _default_reader()
    if not MIN_GRID <= max_grid <= MAX_GRID:
        raise ValueError(f'max_grid must be between {MIN_GRID} and {MAX_GRID}')
    x0, z0, y0, x1, z1, y1 = (float(v) for v in spec.landscape_bbox)
    if not (x0 < x1 and z0 < z1 and y0 < y1):
        raise ValueError('empty landscape bounds')
    heights, size = _decode_heightmap(reader(spec.heightmap_path))
    step = max(1, math.ceil(size / max_grid))
    nodes = list(range(0, size, step))
    data = []
    for iz in nodes:
        base = iz * size
        data.extend(heights[base + ix] for ix in nodes)
    return {
        'size': len(nodes),
        'x0': x0, 'z0': z0,
        'x1': x0 + nodes[-1] * (x1 - x0) / size,
        'z1': z0 + nodes[-1] * (z1 - z0) / size,
        'y0': y0, 'y1': y1,
        'data': data,
    }


def grid_height_function(grid):
    """Bilinear height sampler over an embedded grid (viewer convention).

    Node i on each axis sits at world = first + i * (last - first) / (n - 1),
    matching how the page renders the grid. This is an approximation of the
    client's own bilinear sampling (agreement is within one grid cell).
    """
    n = grid['size']
    if (n < 2 or not grid['x1'] > grid['x0'] or not grid['z1'] > grid['z0']
            or not grid['y1'] > grid['y0'] or len(grid['data']) != n * n):
        raise ValueError('invalid elevation grid')
    data = grid['data']
    sx = (n - 1) / (grid['x1'] - grid['x0'])
    sz = (n - 1) / (grid['z1'] - grid['z0'])
    span = grid['y1'] - grid['y0']

    def height(x, z):
        if not (math.isfinite(x) and math.isfinite(z)):
            raise ValueError('invalid terrain coordinate')
        fx = min(max((x - grid['x0']) * sx, 0.0), n - 1.0)
        fz = min(max((z - grid['z0']) * sz, 0.0), n - 1.0)
        ix, iz = int(fx), int(fz)
        if ix > n - 2:
            ix = n - 2
        if iz > n - 2:
            iz = n - 2
        dx, dz = fx - ix, fz - iz

        def h(a, b):
            return data[b * n + a]
        top = h(ix, iz) * (1 - dx) + h(ix + 1, iz) * dx
        bottom = h(ix, iz + 1) * (1 - dx) + h(ix + 1, iz + 1) * dx
        return grid['y0'] + (top * (1 - dz) + bottom * dz) * span / 65535

    return height


# ------------------------------------------------------- inferred motion

def movement_frames(series, terrain_height=None):
    """Annotate a position series with inferred yaw and height above terrain.

    Returns ``(frames, teleports)`` where each frame is
    ``[t, x, y, z, yaw_degrees_or_None, above_terrain_or_None]`` and
    ``teleports`` holds frame indexes whose step in looks discontinuous.

    Yaw convention matches the map catalog's spawn yaw: degrees of atan2(dx,
    dz), i.e. 0 means moving toward +z, 90 toward +x. Stops keep the previous
    yaw; a jump beyond the teleport thresholds resets it to None. Because a
    symmetric hull looks identical nose-first or tail-first, yaw and yaw+180
    are indistinguishable (reverse ambiguity, documented).
    """
    frames = []
    teleports = []
    yaw = None
    previous = None
    for index, sample in enumerate(series):
        if previous is not None:
            dx = sample.x - previous.x
            dz = sample.z - previous.z
            dist = math.hypot(dx, dz)
            dt = sample.time - previous.time
            if dist > TELEPORT_DISTANCE or (dt <= 0 and dist > MOVE_EPS) \
                    or (dt > 0 and dist / dt > TELEPORT_SPEED):
                teleports.append(index)
                yaw = None
            elif dist >= MOVE_EPS:
                yaw = math.degrees(math.atan2(dx, dz))
        above = None
        if terrain_height is not None:
            try:
                above = sample.y - terrain_height(sample.x, sample.z)
            except ValueError:
                above = None
        frames.append([round(sample.time, 6), round(sample.x, 4),
                       round(sample.y, 4), round(sample.z, 4),
                       None if yaw is None else round(yaw, 1),
                       None if above is None else round(above, 3)])
        previous = sample
    return frames, teleports


def parse_vehicle_dims(text):
    """Parse a 'length,width,height' override in metres."""
    try:
        parts = [float(p) for p in str(text).split(',')]
    except ValueError:
        raise ViewerError(f'--vehicle-dims expects length,width,height, got {text!r}')
    if len(parts) != 3 or not all(math.isfinite(v) and v > 0 for v in parts):
        raise ViewerError(f'--vehicle-dims expects three positive numbers, got {text!r}')
    return {'length': parts[0], 'width': parts[1], 'height': parts[2]}


# ------------------------------------------------------------------ payload

def _html_json(payload):
    # Escape '<' so the JSON can never terminate the enclosing <script> and
    # inject markup; keep the document valid and self-contained.
    return json.dumps(payload, ensure_ascii=True).replace('<', '\\u003c')


def build_payload(replay, context, *, player_entity=None, dims=None):
    terrain_height = grid_height_function(context.grid) if context.grid else None
    meta = {key: replay.meta[key] for key in VERIFIED_META_KEYS if key in replay.meta}
    total = sum(len(series) for series in replay.entities.values())
    stride = max(1, math.ceil(total / MAX_VIEWER_SAMPLES))
    if stride > 1:
        context.diagnostics.append(
            f'{total} samples exceed the viewer cap; series thinned by {stride}')
    entities = {}
    for entity, series in sorted(replay.entities.items()):
        frames, teleports = movement_frames(series[::stride], terrain_height)
        entities[str(entity)] = {'samples': frames, 'teleports': teleports}
    player_name = None
    if player_entity is not None:
        if player_entity not in replay.entities:
            available = ', '.join(str(i) for i in replay.entity_ids()) or 'none'
            raise ViewerError(f'--player-entity {player_entity} has no position '
                              f'samples (available: {available})')
        raw_name = replay.meta.get('playerVehicleName')
        if isinstance(raw_name, str):
            player_name = raw_name
    return {
        'meta': meta,
        'clientVersion': replay.client_version,
        'map': {
            'mode': context.mode,
            'key': context.key,
            'mapId': context.map_id,
            'bounds': context.bounds,
            'bbox': context.bbox,
            'grid': context.grid,
            'diagnostics': context.diagnostics,
        },
        'vehicles': {
            'dims': dict(dims or DEFAULT_VEHICLE),
            'playerEntity': None if player_entity is None else str(player_entity),
            'playerName': player_name,
        },
        'entities': entities,
    }


def build_viewer_html(replay, context, *, selected_entity=None,
                      player_entity=None, dims=None):
    payload = build_payload(replay, context, player_entity=player_entity, dims=dims)
    page = VIEWER_PAGE.replace(
        '__SELECTED__', str(selected_entity) if selected_entity is not None else '')
    page = page.replace('__DATA__', _html_json(payload))
    if len(page.encode('utf-8')) > MAX_HTML_BYTES:
        raise ViewerError(
            f'viewer page is larger than the {MAX_HTML_BYTES}-byte limit; '
            'use export --start/--end windows to shorten the recording')
    return page


# -------------------------------------------------------------------- page
#
# Self-contained offline page: no external libraries, fonts, textures or
# network references. Two stacked canvases keep per-frame work bounded: the
# static layer (terrain, contours, trajectories) redraws only on camera,
# view, resize or track changes; the live layer redraws the moving markers
# each animation frame.

VIEWER_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Replay inspector v2</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; margin: 0; padding: 12px;
         display: flex; flex-direction: column; gap: 8px; height: 100vh;
         box-sizing: border-box; }
  h1 { font-size: 1.1rem; margin: 0; }
  #meta { font-size: .78rem; opacity: .75; margin: 0; white-space: pre-wrap;
          overflow-wrap: anywhere; }
  #diag { font-size: .78rem; margin: 0; white-space: pre-wrap; color: #c60; }
  #controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
              font-size: .85rem; }
  #controls label { display: flex; gap: 4px; align-items: center; }
  #scrubber { flex: 1 1 160px; }
  #tracks button { margin-right: 4px; border: 1px solid #8888; border-radius: 4px;
                   background: none; color: inherit; opacity: .4; cursor: pointer; }
  #tracks button.on { opacity: 1; }
  #stage { position: relative; flex: 1; min-height: 260px; border: 1px solid #8884;
           border-radius: 6px; overflow: hidden; }
  #stage canvas { position: absolute; inset: 0; width: 100%; height: 100%;
                  touch-action: none; }
  #readout { font-variant-numeric: tabular-nums; font-size: .85rem; margin: 0;
             min-height: 1.2em; }
</style>
</head>
<body>
<h1>Replay inspector v2 &mdash; map-aware playback <span id="modetag" style="font-weight:normal"></span></h1>
<p id="meta"></p>
<p id="diag"></p>
<div id="controls">
  <label>View <select id="view"><option value="top">top-down</option>
    <option value="3d">3D</option></select></label>
  <label>Entity <select id="entity"></select></label>
  <span id="tracks"></span>
  <button id="play" type="button">play</button>
  <label>Speed <select id="speed"><option>0.5</option><option selected>1</option>
    <option>2</option><option>4</option></select></label>
  <input id="scrubber" type="range" min="0" max="0" value="0" step="any">
  <label><input id="follow" type="checkbox"> follow</label>
  <label>Minimap <input id="minimap" type="file" accept="image/png,image/jpeg,image/webp"></label>
  <label><input id="flip" type="checkbox"> flip N/S</label>
</div>
<div id="stage"><canvas id="static"></canvas><canvas id="live"></canvas></div>
<p id="readout"></p>
<script id="replay-data" type="application/json">__DATA__</script>
<script>
'use strict';
const D = JSON.parse(document.getElementById('replay-data').textContent);
const $ = id => document.getElementById(id);
const staticC = $('static'), liveC = $('live');
const sctx = staticC.getContext('2d'), lctx = liveC.getContext('2d');
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

// ---- header ----------------------------------------------------------------
$('modetag').textContent = D.map.mode === 'terrain'
  ? '(map: ' + (D.map.key || '?') + (D.map.mapId !== null ? ', id ' + D.map.mapId : '') + ')'
  : '(generic coordinate grid)';
$('meta').textContent = 'client ' + (D.clientVersion || 'unknown') +
  Object.entries(D.meta).map(kv => '\\n' + kv[0] + ': ' + kv[1]).join('');
$('diag').textContent = (D.map.diagnostics || []).join('\\n');

const ids = Object.keys(D.entities);
const palette = ['#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4',
                 '#46f0f0', '#f032e6', '#bcf60c', '#008080', '#9a6324'];
const colorOf = id => palette[(ids.indexOf(id) % palette.length + palette.length) % palette.length];
const DIMS = D.vehicles.dims;
const G = D.map.grid;

// ---- world helpers ---------------------------------------------------------
function terrainH(x, z) {           // bilinear over the embedded grid; null in grid mode
  if (!G) return null;
  const n = G.size;
  const fx = clamp((x - G.x0) * (n - 1) / (G.x1 - G.x0), 0, n - 1.001);
  const fz = clamp((z - G.z0) * (n - 1) / (G.z1 - G.z0), 0, n - 1.001);
  const ix = Math.floor(fx), iz = Math.floor(fz);
  const dx = fx - ix, dz = fz - iz;
  const h = (a, b) => G.data[b * n + a];
  return G.y0 + (h(ix, iz) * (1 - dx) + h(ix + 1, iz) * dx) * (1 - dz) * (G.y1 - G.y0) / 65535
              + (h(ix, iz + 1) * (1 - dx) + h(ix + 1, iz + 1) * dx) * dz * (G.y1 - G.y0) / 65535;
}

function worldBounds() {
  let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
  let minY = Infinity, maxY = -Infinity;
  for (const id of ids) {
    for (const s of D.entities[id].samples) {
      minX = Math.min(minX, s[1]); maxX = Math.max(maxX, s[1]);
      minY = Math.min(minY, s[2]); maxY = Math.max(maxY, s[2]);
      minZ = Math.min(minZ, s[3]); maxZ = Math.max(maxZ, s[3]);
    }
  }
  if (D.map.bounds) {  // ground the view in the map border, not just the tracks
    minX = Math.min(minX, D.map.bounds[0]); minZ = Math.min(minZ, D.map.bounds[1]);
    maxX = Math.max(maxX, D.map.bounds[2]); maxZ = Math.max(maxZ, D.map.bounds[3]);
  }
  if (G) { minY = Math.min(minY, G.y0); maxY = Math.max(maxY, G.y1); }
  if (!isFinite(minX)) { minX = -1; maxX = 1; minZ = -1; maxZ = 1; minY = 0; maxY = 10; }
  if (G) { minX = Math.min(minX, G.x0); maxX = Math.max(maxX, G.x1);
           minZ = Math.min(minZ, G.z0); maxZ = Math.max(maxZ, G.z1); }
  const padX = Math.max((maxX - minX) * 0.03, 1e-3), padZ = Math.max((maxZ - minZ) * 0.03, 1e-3);
  const padY = Math.max((maxY - minY) * 0.1, 1.0);
  return {minX: minX - padX, maxX: maxX + padX, minZ: minZ - padZ, maxZ: maxZ + padZ,
          minY: minY - padY, maxY: maxY + padY};
}
const WB = worldBounds();

function interpAt(entry, t) {   // -> {t,x,y,z,yaw,above,teleport} or null
  const samples = entry.samples;
  if (!samples.length) return null;
  const first = samples[0], last = samples[samples.length - 1];
  const mk = (a, b, f) => {
    const lp = (u, v) => u + (v - u) * f;
    const above = (a[5] === null || b[5] === null) ? null : lp(a[5], b[5]);
    return {t: lp(a[0], b[0]), x: lp(a[1], b[1]), y: lp(a[2], b[2]), z: lp(a[3], b[3]),
            yaw: a[4] === null ? b[4] : a[4], above, teleport: false};
  };
  if (t <= first[0] || samples.length === 1) return mk(first, first, 0);
  if (t >= last[0]) return mk(last, last, 0);
  let lo = 0, hi = samples.length - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (samples[mid][0] <= t) lo = mid; else hi = mid; }
  if (entry.teleports.indexOf(hi) !== -1) {
    const s = samples[lo];
    return {t, x: s[1], y: s[2], z: s[3], yaw: s[4], above: s[5], teleport: true};
  }
  return mk(samples[lo], samples[hi], (t - samples[lo][0]) / (samples[hi][0] - samples[lo][0]));
}

// ---- controls ---------------------------------------------------------------
const entitySel = $('entity');
for (const id of ids) {
  const opt = document.createElement('option');
  opt.value = id;
  opt.textContent = 'entity ' + id +
    (D.vehicles.playerEntity === id ? ' (player ' + (D.vehicles.playerName || '') + ' \u2014 designated)' : '');
  entitySel.appendChild(opt);
}
const visible = new Set(ids);
const tracks = $('tracks');
for (const id of ids) {
  const b = document.createElement('button');
  b.type = 'button'; b.textContent = id; b.className = 'on';
  b.style.borderColor = colorOf(id);
  b.addEventListener('click', () => {
    if (visible.has(id)) { visible.delete(id); b.className = ''; }
    else { visible.add(id); b.className = 'on'; }
    needStatic = true;
  });
  tracks.appendChild(b);
}
let timeMin = Infinity, timeMax = -Infinity;
for (const id of ids) {
  const s = D.entities[id].samples;
  if (s.length) { timeMin = Math.min(timeMin, s[0][0]); timeMax = Math.max(timeMax, s[s.length - 1][0]); }
}
if (!isFinite(timeMin)) { timeMin = 0; timeMax = 0; }
const scrubber = $('scrubber');
scrubber.min = timeMin; scrubber.max = timeMax;
scrubber.value = timeMax;
let tCur = timeMax, playing = false, needStatic = true;
const viewSel = $('view'), follow = $('follow'), readout = $('readout');
const preferred = '__SELECTED__';
if (ids.includes(preferred)) entitySel.value = preferred;
if (!ids.length) {
  $('meta').textContent += '\\nno position samples in this replay';
  readout.textContent = 'no position samples';
}

$('play').addEventListener('click', () => { playing = !playing; $('play').textContent = playing ? 'pause' : 'play'; });
window.addEventListener('keydown', e => {
  if (e.code === 'Space' && !/^(BUTTON|SELECT|INPUT)$/.test(e.target.tagName)) {
    e.preventDefault(); $('play').click();
  }
});
scrubber.addEventListener('input', () => { tCur = parseFloat(scrubber.value); playing = false; $('play').textContent = 'play'; });
entitySel.addEventListener('change', () => { needStatic = true; });
viewSel.addEventListener('change', () => { needStatic = true; });

// ---- top-down projection -----------------------------------------------------
let TD = null;
function tdFit() {
  const w = liveC.clientWidth, h = liveC.clientHeight, m = 30;
  const scale = Math.min((w - 2 * m) / (WB.maxX - WB.minX), (h - 2 * m) / (WB.maxZ - WB.minZ));
  TD = {scale,
        ox: m + (w - 2 * m - (WB.maxX - WB.minX) * scale) / 2 - WB.minX * scale,
        oy: m + (h - 2 * m - (WB.maxZ - WB.minZ) * scale) / 2 - WB.minZ * scale};
  // screen: x -> right, z -> down (matches the inspector v1 plot)
}
const tdX = x => TD.ox + x * TD.scale;
const tdZ = z => TD.oy + z * TD.scale;
const tdRect = () => [tdX(WB.minX), tdZ(WB.minZ), (WB.maxX - WB.minX) * TD.scale, (WB.maxZ - WB.minZ) * TD.scale];

// generated elevation shading + contours, baked once per payload
let shadeCanvas = null;
function buildShadeCanvas() {
  const n = G.size;
  const c = document.createElement('canvas');
  c.width = n; c.height = n;
  const g = c.getContext('2d');
  const img = g.createImageData(n, n);
  const span = G.y1 - G.y0;
  const wy = (ix, iz) => G.y0 + G.data[iz * n + ix] * span / 65535;
  for (let iz = 0; iz < n; iz++) for (let ix = 0; ix < n; ix++) {
    const l = 34 + 52 * (G.data[iz * n + ix] / 65535);
    const o = (iz * n + ix) * 4;
    img.data[o] = l * 0.82; img.data[o + 1] = l * 0.92; img.data[o + 2] = l * 1.08;
    img.data[o + 3] = 255;
  }
  g.putImageData(img, 0, 0);
  g.strokeStyle = 'rgba(20,20,30,0.4)';
  g.lineWidth = 1;
  for (let k = 1; k <= 4; k++) {
    const level = G.y0 + span * k / 5;
    g.beginPath();
    for (let iz = 0; iz < n - 1; iz++) for (let ix = 0; ix < n - 1; ix++) {
      const hs = [wy(ix, iz), wy(ix + 1, iz), wy(ix + 1, iz + 1), wy(ix, iz + 1)];
      const pts = [];
      for (let e = 0; e < 4; e++) {
        const a = hs[e], b = hs[(e + 1) % 4];
        if ((a - level) * (b - level) < 0) {
          const f = (level - a) / (b - a);
          const ax = [ix, ix + 1, ix + 1, ix][e], ay = [iz, iz, iz + 1, iz + 1][e];
          const bx = [ix + 1, ix + 1, ix, ix][e], by = [iz, iz + 1, iz + 1, iz][e];
          pts.push([ax + (bx - ax) * f + 0.5, ay + (by - ay) * f + 0.5]);
        }
      }
      for (let p = 0; p + 1 < pts.length; p += 2) {
        g.moveTo(pts[p][0], pts[p][1]); g.lineTo(pts[p + 1][0], pts[p + 1][1]);
      }
    }
    g.stroke();
  }
  return c;
}

let minimapImg = null;
$('minimap').addEventListener('change', async e => {
  const file = e.target.files && e.target.files[0];
  if (!file) { minimapImg = null; needStatic = true; return; }
  try {
    minimapImg = await createImageBitmap(file);
    e.target.value = '';
  } catch (err) {
    $('diag').textContent += '\\nminimap image could not be decoded: ' + err;
    minimapImg = null;
  }
  needStatic = true;
});
$('flip').addEventListener('change', () => { needStatic = true; });

function drawStaticTop() {
  const w = liveC.clientWidth, h = liveC.clientHeight;
  sctx.clearRect(0, 0, w, h);
  if (G) {
    const [rx, ry, rw, rh] = tdRect();
    if (minimapImg) {
      sctx.save();
      if ($('flip').checked) { sctx.translate(0, ry + rh + ry); sctx.scale(1, -1); }
      sctx.drawImage(minimapImg, rx, ry, rw, rh);
      sctx.restore();
      sctx.strokeStyle = 'rgba(255,160,40,0.8)';
      sctx.strokeRect(rx, ry, rw, rh);
    } else {
      if (!shadeCanvas) shadeCanvas = buildShadeCanvas();
      sctx.drawImage(shadeCanvas, rx, ry, rw, rh);
    }
    if (D.map.bounds) {
      sctx.strokeStyle = sctx.fillStyle = getComputedStyle(document.body).color;
      sctx.globalAlpha = 0.5;
      sctx.strokeRect(tdX(D.map.bounds[0]), tdZ(D.map.bounds[1]),
        (D.map.bounds[2] - D.map.bounds[0]) * TD.scale,
        (D.map.bounds[3] - D.map.bounds[1]) * TD.scale);
      sctx.globalAlpha = 1;
    }
  } else {
    // generic coordinate grid with nice-step lines
    sctx.strokeStyle = sctx.fillStyle = getComputedStyle(document.body).color;
    sctx.globalAlpha = 0.25;
    sctx.font = '11px system-ui, sans-serif';
    const raw = Math.max(WB.maxX - WB.minX, WB.maxZ - WB.minZ) / 6;
    const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    const step = [1, 2, 5, 10].map(m => m * mag).find(s => s >= raw) || mag * 10;
    for (let x = Math.ceil(WB.minX / step) * step; x <= WB.maxX; x += step) {
      sctx.beginPath(); sctx.moveTo(tdX(x), 8); sctx.lineTo(tdX(x), h - 8); sctx.stroke();
      sctx.globalAlpha = 0.6; sctx.fillText(x.toPrecision(3), tdX(x) - 12, h - 12); sctx.globalAlpha = 0.25;
    }
    for (let z = Math.ceil(WB.minZ / step) * step; z <= WB.maxZ; z += step) {
      sctx.beginPath(); sctx.moveTo(8, tdZ(z)); sctx.lineTo(w - 8, tdZ(z)); sctx.stroke();
      sctx.globalAlpha = 0.6; sctx.fillText(z.toPrecision(3), 6, tdZ(z) + 4); sctx.globalAlpha = 0.25;
    }
    sctx.globalAlpha = 1;
    sctx.fillText('+x', w - 24, h - 12);
    sctx.fillText('+z', 6, 20);
  }
  for (const id of ids) {
    if (!visible.has(id)) continue;
    const {samples, teleports} = D.entities[id];
    const breaks = new Set(teleports);
    if (!samples.length) continue;
    sctx.strokeStyle = colorOf(id);
    sctx.lineWidth = id === entitySel.value ? 2 : 1.2;
    sctx.globalAlpha = id === entitySel.value ? 0.9 : 0.25;
    sctx.beginPath();
    samples.forEach((s, i) => i && !breaks.has(i)
      ? sctx.lineTo(tdX(s[1]), tdZ(s[3])) : sctx.moveTo(tdX(s[1]), tdZ(s[3])));
    sctx.stroke();
  }
  sctx.globalAlpha = 1;
}

// ---- 3D projection ------------------------------------------------------------
const cam = {yaw: 0.8, elev: 0.85, dist: 400, tx: 0, ty: 0, tz: 0};
function camInit() {
  cam.tx = (WB.minX + WB.maxX) / 2; cam.tz = (WB.minZ + WB.maxZ) / 2;
  cam.ty = (WB.minY + WB.maxY) / 2;
  cam.dist = clamp(1.4 * Math.max(WB.maxX - WB.minX, WB.maxZ - WB.minZ), 30, 6000);
}
function camBasis() {
  const ce = Math.cos(cam.elev), se = Math.sin(cam.elev);
  const ox = Math.sin(cam.yaw) * ce, oy = se, oz = Math.cos(cam.yaw) * ce;
  const cx = cam.tx + ox * cam.dist, cy = cam.ty + oy * cam.dist, cz = cam.tz + oz * cam.dist;
  let fx = -ox, fy = -oy, fz = -oz;
  let rx = -fz, rz = fx;
  const rl = Math.hypot(rx, rz) || 1; rx /= rl; rz /= rl;
  const ux = -rz * fy, uy = rz * fx - rx * fz, uz = rx * fy;
  return {cx, cy, cz, fx, fy, fz, rx, rz, ux, uy, uz};
}
function project3(B, x, y, z) {
  const vx = x - B.cx, vy = y - B.cy, vz = z - B.cz;
  const depth = vx * B.fx + vy * B.fy + vz * B.fz;
  if (depth < 1) return null;
  const f = 0.9 * liveC.clientHeight;
  const sx = vx * B.rx + vz * B.rz;
  const sy = vx * B.ux + vy * B.uy + vz * B.uz;
  return [liveC.clientWidth / 2 + f * sx / depth, liveC.clientHeight / 2 - f * sy / depth, depth];
}

let quadCache = null, nodeProj = null;
function buildQuadCache() {
  const n = G.size, cell = n - 1, span = G.y1 - G.y0;
  const count = cell * cell;
  const shade = new Float32Array(count), bright = new Float32Array(count);
  const idx = new Uint32Array(count);
  const cellW = (G.x1 - G.x0) / (n - 1);
  const h = (a, b) => G.data[b * n + a];
  const Lx = -0.35, Ly = 0.85, Lz = -0.4;
  const Ll = Math.sqrt(Lx * Lx + Ly * Ly + Lz * Lz);
  for (let iz = 0; iz < cell; iz++) for (let ix = 0; ix < cell; ix++) {
    const i = iz * cell + ix;
    idx[i] = i;
    shade[i] = 0.36 + 0.38 * ((h(ix, iz) + h(ix + 1, iz) + h(ix, iz + 1) + h(ix + 1, iz + 1))
                              / 4 / 65535);
    const gradientScale = span / (65535 * 2 * cellW);
    const hx = (h(ix + 1, iz) + h(ix + 1, iz + 1) - h(ix, iz) - h(ix, iz + 1)) * gradientScale;
    const hz = (h(ix, iz + 1) + h(ix + 1, iz + 1) - h(ix, iz) - h(ix + 1, iz)) * gradientScale;
    const nl = Math.sqrt(hx * hx + 1 + hz * hz);
    const dot = (-hx * Lx + Ly + -hz * Lz) / (nl * Ll);
    bright[i] = 0.5 + 0.5 * Math.max(0, dot);
  }
  return {idx, shade, bright, cell};
}
function nodeWorld(ix, iz) {
  const n = G.size;
  return [G.x0 + ix * (G.x1 - G.x0) / (n - 1),
          G.y0 + G.data[iz * n + ix] * (G.y1 - G.y0) / 65535,
          G.z0 + iz * (G.z1 - G.z0) / (n - 1)];
}
function drawStatic3d(B) {
  const w = liveC.clientWidth, h = liveC.clientHeight;
  sctx.clearRect(0, 0, w, h);
  if (G) {
    const n = G.size, cell = n - 1;
    if (!quadCache) quadCache = buildQuadCache();
    if (!nodeProj || nodeProj.length !== n * n * 3) nodeProj = new Float64Array(n * n * 3);
    for (let iz = 0; iz < n; iz++) for (let ix = 0; ix < n; ix++) {
      const p = project3(B, ...nodeWorld(ix, iz));
      const o = (iz * n + ix) * 3;
      if (p) { nodeProj[o] = p[0]; nodeProj[o + 1] = p[1]; nodeProj[o + 2] = p[2]; }
      else nodeProj[o + 2] = -1;
    }
    const depth = new Float64Array(cell * cell);
    for (let qz = 0; qz < cell; qz++) for (let qx = 0; qx < cell; qx++) {
      const i = qz * cell + qx;
      const d = [0, 1, n + 1, n].map(k => nodeProj[(qz * n + qx + (k >= n ? n : 0) + (k === 1 || k === n + 1 ? 1 : 0)) * 3 + 2]);
      depth[i] = (d[0] + d[1] + d[2] + d[3]) / 4;
    }
    const order = Array.from(quadCache.idx).sort((a, b) => depth[b] - depth[a]);
    for (const i of order) {
      if (depth[i] < 1) continue;
      const qx = i % cell, qz = (i / cell) | 0;
      const corner = k => ((qz + (k >= 2 ? 1 : 0)) * n + qx + (k === 1 || k === 2 ? 1 : 0)) * 3;
      if (nodeProj[corner(0) + 2] < 1 || nodeProj[corner(1) + 2] < 1 ||
          nodeProj[corner(2) + 2] < 1 || nodeProj[corner(3) + 2] < 1) continue;
      const light = clamp(quadCache.shade[i] * quadCache.bright[i], 0.05, 0.8);
      sctx.fillStyle = 'hsl(205, 12%, ' + (light * 100).toFixed(1) + '%)';
      sctx.beginPath();
      sctx.moveTo(nodeProj[corner(0)], nodeProj[corner(0) + 1]);
      for (let k = 1; k < 4; k++) sctx.lineTo(nodeProj[corner(k)], nodeProj[corner(k) + 1]);
      sctx.closePath();
      sctx.fill();
    }
  } else {
    // generic ground plane grid at the lowest sample height
    const gy = WB.minY + 1;
    sctx.strokeStyle = getComputedStyle(document.body).color;
    sctx.globalAlpha = 0.2;
    const step = Math.max((WB.maxX - WB.minX) / 10, 10);
    for (let x = Math.ceil(WB.minX / step) * step; x <= WB.maxX; x += step) {
      const a = project3(B, x, gy, WB.minZ), b = project3(B, x, gy, WB.maxZ);
      if (a && b) { sctx.beginPath(); sctx.moveTo(a[0], a[1]); sctx.lineTo(b[0], b[1]); sctx.stroke(); }
    }
    for (let z = Math.ceil(WB.minZ / step) * step; z <= WB.maxZ; z += step) {
      const a = project3(B, WB.minX, gy, z), b = project3(B, WB.maxX, gy, z);
      if (a && b) { sctx.beginPath(); sctx.moveTo(a[0], a[1]); sctx.lineTo(b[0], b[1]); sctx.stroke(); }
    }
    sctx.globalAlpha = 1;
  }
  for (const id of ids) {
    if (!visible.has(id)) continue;
    const {samples, teleports} = D.entities[id];
    const breaks = new Set(teleports);
    if (!samples.length) continue;
    sctx.strokeStyle = colorOf(id);
    sctx.lineWidth = id === entitySel.value ? 2 : 1.2;
    sctx.globalAlpha = id === entitySel.value ? 0.9 : 0.3;
    sctx.beginPath();
    let pen = false;
    for (const [i, s] of samples.entries()) {
      if (breaks.has(i)) pen = false;
      const p = project3(B, s[1], s[2], s[3]);
      if (p) { if (pen) sctx.lineTo(p[0], p[1]); else sctx.moveTo(p[0], p[1]); pen = true; }
      else pen = false;
    }
    sctx.stroke();
  }
  sctx.globalAlpha = 1;
}

// ---- live layer (per-frame) ----------------------------------------------------
function drawLiveTop() {
  const w = liveC.clientWidth, h = liveC.clientHeight;
  lctx.clearRect(0, 0, w, h);
  for (const id of ids) {
    if (!visible.has(id)) continue;
    const cur = interpAt(D.entities[id], tCur);
    if (!cur) continue;
    lctx.fillStyle = colorOf(id);
    const r = id === entitySel.value ? 6 : 3.5;
    if (cur.teleport) {
      lctx.strokeStyle = '#ff9d00';
      lctx.beginPath(); lctx.arc(tdX(cur.x), tdZ(cur.z), r + 4, 0, 7); lctx.stroke();
    }
    lctx.beginPath(); lctx.arc(tdX(cur.x), tdZ(cur.z), r, 0, 7); lctx.fill();
    if (id === entitySel.value && cur.yaw !== null) {
      const a = cur.yaw * Math.PI / 180;
      lctx.beginPath();
      lctx.moveTo(tdX(cur.x), tdZ(cur.z));
      lctx.lineTo(tdX(cur.x) + Math.sin(a) * DIMS.length * TD.scale,
                  tdZ(cur.z) + Math.cos(a) * DIMS.length * TD.scale);
      lctx.strokeStyle = colorOf(id); lctx.stroke();
    }
  }
}

function hexRgb(hex) {
  return [1, 3, 5].map(k => parseInt(hex.slice(k, k + 2), 16));
}
function shadeHex(hex, f) {
  const c = hexRgb(hex).map(v => clamp(Math.round(v * f), 0, 255));
  return 'rgb(' + c[0] + ',' + c[1] + ',' + c[2] + ')';
}
// original schematic tank: tracks + hull + turret + barrel boxes, no real model
function tankFaces(x, y, z, yawDeg, dims) {
  const yaw = (yawDeg === null ? 0 : yawDeg) * Math.PI / 180;
  const cy = Math.cos(yaw), sy = Math.sin(yaw);
  const rot = (lx, lz) => [x + lx * cy + lz * sy, z - lx * sy + lz * cy];
  const faces = [];
  const box = (lx0, lx1, ly0, ly1, lz0, lz1, color, top) => {
    const corner = (a, b, c2) => {
      const [wx, wz] = rot(a, c2);
      return [wx, y + b, wz];
    };
    const quads = [
      [[corner(lx0, ly1, lz0), corner(lx1, ly1, lz0), corner(lx1, ly1, lz1), corner(lx0, ly1, lz1)], top],
      [[corner(lx0, ly0, lz0), corner(lx1, ly0, lz0), corner(lx1, ly1, lz0), corner(lx0, ly1, lz0)], 0.8],
      [[corner(lx1, ly0, lz0), corner(lx1, ly0, lz1), corner(lx1, ly1, lz1), corner(lx1, ly1, lz0)], 0.7],
      [[corner(lx1, ly0, lz1), corner(lx0, ly0, lz1), corner(lx0, ly1, lz1), corner(lx1, ly1, lz1)], 0.75],
      [[corner(lx0, ly0, lz1), corner(lx0, ly0, lz0), corner(lx0, ly1, lz0), corner(lx0, ly1, lz1)], 0.7],
    ];
    for (const [pts, f] of quads) faces.push({pts, color: shadeHex(color, f)});
  };
  const L = dims.length, W = dims.width, H = dims.height;
  const tw = 0.24 * W;
  box(0.5 * L - 0.05, 0.5 * L, 0, 0.22 * H, -(0.5 * W), -0.5 * W + tw, '#333333', 0.55);
  box(0.5 * L - 0.05, 0.5 * L, 0, 0.22 * H, 0.5 * W - tw, 0.5 * W, '#333333', 0.55);
  box(-0.5 * L, 0.5 * L, 0.22 * H, 0.68 * H, -0.5 * W + tw * 0.5, 0.5 * W - tw * 0.5, 'HULL', 1.0);
  box(-0.28 * L, 0.18 * L, 0.68 * H, H, -0.3 * W, 0.3 * W, 'HULL', 1.05);
  if (yawDeg !== null) {
    box(0.18 * L, 0.18 * L + 0.85 * L, 0.8 * H, 0.88 * H, -0.045 * W, 0.045 * W, '#222222', 0.9);
  }
  return faces;
}
function drawLive3d(B) {
  const w = liveC.clientWidth, h = liveC.clientHeight;
  lctx.clearRect(0, 0, w, h);
  const draws = [];
  for (const id of ids) {
    if (!visible.has(id)) continue;
    const cur = interpAt(D.entities[id], tCur);
    if (!cur) continue;
    const base = colorOf(id);
    for (const f of tankFaces(cur.x, cur.y, cur.z, cur.yaw, DIMS)) {
      const pts = [], proj = [];
      let ok = true;
      for (const p of f.pts) {
        const q = project3(B, p[0], p[1], p[2]);
        if (!q) { ok = false; break; }
        proj.push(q);
      }
      if (!ok) continue;
      const depth = proj.reduce((s, q) => s + q[2], 0) / proj.length;
      draws.push({depth, pts: proj, color: f.color === 'HULL' ? base : f.color});
    }
    if (cur.above !== null && cur.above > 1.5 && G) {
      const a = project3(B, cur.x, cur.y - cur.above, cur.z);
      const b = project3(B, cur.x, cur.y - 0.5, cur.z);
      if (a && b) {
        lctx.strokeStyle = '#ff9d00';
        lctx.setLineDash([5, 4]);
        lctx.beginPath(); lctx.moveTo(a[0], a[1]); lctx.lineTo(b[0], b[1]); lctx.stroke();
        lctx.setLineDash([]);
        lctx.fillStyle = '#ff9d00';
        lctx.font = '11px system-ui, sans-serif';
        lctx.fillText('airborne (approx)', b[0] + 6, b[1]);
      }
    }
    if (cur.teleport) {
      const p = project3(B, cur.x, cur.y + DIMS.height + 1, cur.z);
      if (p) {
        lctx.fillStyle = '#ff9d00';
        lctx.font = '11px system-ui, sans-serif';
        lctx.fillText('teleport (inferred)', p[0] + 6, p[1]);
      }
    }
  }
  draws.sort((a, b) => b.depth - a.depth);
  for (const d of draws) {
    lctx.fillStyle = d.color;
    lctx.beginPath();
    lctx.moveTo(d.pts[0][0], d.pts[0][1]);
    for (let i = 1; i < d.pts.length; i++) lctx.lineTo(d.pts[i][0], d.pts[i][1]);
    lctx.closePath();
    lctx.fill();
  }
}

// ---- readout ---------------------------------------------------------------------
function updateReadout() {
  const id = entitySel.value;
  const cur = ids.length ? interpAt(D.entities[id], tCur) : null;
  if (!cur) { readout.textContent = ids.length ? '' : 'no position samples'; return; }
  const parts = ['t=' + cur.t.toFixed(3) + 's', 'x=' + cur.x.toFixed(2), 'y=' + cur.y.toFixed(2),
                 'z=' + cur.z.toFixed(2), 'entity ' + id];
  if (cur.above !== null) parts.push('above terrain \\u2248 ' + cur.above.toFixed(2) + ' m (approx)');
  if (cur.yaw !== null) parts.push('yaw ' + cur.yaw.toFixed(0) + '\\u00b0 (inferred, reverse-ambiguous)');
  if (cur.teleport) parts.push('teleport (inferred)');
  if (D.vehicles.playerEntity === id) parts.push('player ' + (D.vehicles.playerName || '') + ' (designated)');
  readout.textContent = parts.join('   ');
}

// ---- main loop ---------------------------------------------------------------------
function resize() {
  const dpr = window.devicePixelRatio || 1;
  for (const c of [staticC, liveC]) {
    c.width = Math.max(1, c.clientWidth * dpr);
    c.height = Math.max(1, c.clientHeight * dpr);
  }
  sctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  lctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  tdFit();
  needStatic = true;
}
window.addEventListener('resize', resize);

let drag = null, followStatic = false;
liveC.addEventListener('pointerdown', e => {
  drag = {x: e.clientX, y: e.clientY, pan: e.shiftKey || e.button === 2};
  liveC.setPointerCapture(e.pointerId);
});
liveC.addEventListener('pointermove', e => {
  if (!drag) return;
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  drag.x = e.clientX; drag.y = e.clientY;
  if (viewSel.value !== '3d') return;
  if (drag.pan) {
    const B = camBasis();
    const k = cam.dist * 0.0016;
    cam.tx -= (B.rx * dx - B.ux * dy) * k;
    cam.ty += B.uy * dy * k;
    cam.tz -= (B.rz * dx - B.uz * dy) * k;
  } else {
    cam.yaw += dx * 0.008;
    cam.elev = clamp(cam.elev + dy * 0.008, 0.08, 1.5);
  }
  needStatic = true;
});
liveC.addEventListener('pointerup', () => { drag = null; });
liveC.addEventListener('wheel', e => {
  if (viewSel.value !== '3d') return;
  e.preventDefault();
  cam.dist = clamp(cam.dist * Math.exp(e.deltaY * 0.0012), 10, 6000);
  needStatic = true;
}, {passive: false});
liveC.addEventListener('contextmenu', e => e.preventDefault());

camInit();
resize();
let lastNow = null, lastStatic = -Infinity, drawnBasis = null;
function frame(now) {
  if (playing) {
    const dt = lastNow === null ? 0 : (now - lastNow) / 1000;
    tCur += dt * parseFloat($('speed').value);
    if (tCur > timeMax) tCur = timeMin;
    scrubber.value = tCur;
  }
  lastNow = now;
  const is3d = viewSel.value === '3d';
  if (is3d && follow.checked && ids.includes(entitySel.value)) {
    const cur = interpAt(D.entities[entitySel.value], tCur);
    if (cur && (cam.tx !== cur.x || cam.ty !== cur.y || cam.tz !== cur.z)) {
      cam.tx = cur.x; cam.ty = cur.y; cam.tz = cur.z;
      needStatic = true; followStatic = drawnBasis !== null;
    }
  }
  // Redraws of the static layer are throttled while the follow camera moves,
  // so terrain re-projection stays bounded; live markers still draw at full
  // frame rate on the cheap overlay canvas.
  if (needStatic && (now - lastStatic > (followStatic ? 120 : 0))) {
    if (is3d) {
      drawnBasis = camBasis();
      drawStatic3d(drawnBasis);
    } else {
      drawStaticTop();
      drawnBasis = null;
    }
    lastStatic = now;
    needStatic = false;
    followStatic = false;
  }
  if (is3d) drawLive3d(drawnBasis || camBasis()); else drawLiveTop();
  updateReadout();
}
function loop(now) {
  frame(now);
  requestAnimationFrame(loop);
}
frame(performance.now());   // first frame synchronously, so file:// pages paint even without rAF
requestAnimationFrame(loop);
</script>
</body>
</html>
"""
