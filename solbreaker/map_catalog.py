"""Validated local map assets for the pinned compatible build 11.21 build.

The catalog is read from the installed game's maps.yaml and scene metadata.
Only maps whose scene and terrain metadata and first-wave spawn records match
the loader's required structure are admitted here. Physical spawn clearance
and native driving are tracked separately in the map validation notes.
"""
from dataclasses import dataclass, replace
from functools import lru_cache
import math
from pathlib import Path, PurePosixPath
import struct

import yaml

from .dava_scene import active_nodes, scene
from .dvpl import read
from .base_capture import CapturePoint
from .supremacy import StrategicPoint


_SUPPORTED = {
    "karelia": 1, "desert_train": 2, "erlenberg": 3, "karieri": 4,
    "amigosville": 5, "rudniki": 6, "medvedkovo": 7, "fort": 8,
    "himmelsdorf": 9, "mountain": 10, "savanna": 11, "plant": 12,
    "idle": 13, "holland": 14, "port": 15, "malinovka": 19,
    "pliego": 20, "canal": 21, "italy": 23, "milbase": 25,
    "lagoon": 26, "canyon": 27, "rock": 30, "grossberg": 31,
    "skit": 35, "lumber": 38, "faust": 39, "forgecity": 40,
    "neptune": 42, "rift": 43, "erlenberg_old": 83,
    "amigosville_old": 85, "savanna_old": 89,
    "desert_train_02": 44, "desert_train_03": 45,
    "medvedkovo_02": 46, "medvedkovo_03": 47,
    "amigosville_02": 48, "amigosville_03": 49,
    "milbase_02": 50, "milbase_03": 51, "milbase_04": 52, "milbase_05": 53,
    "erlenberg_01": 54, "erlenberg_02": 55, "canal_01": 56, "lumber_01": 57,
    "neptune_01": 58, "neptune_02": 59,
    "rudniki_01": 60, "rudniki_02": 61, "rudniki_03": 62,
    "moon": 70, "holmeisk": 71, "iceworld": 72,
    # Registered in the private lab's maps.yaml overlay. The installed game
    # catalog does not contain this artist map.
    "user_map": 100,
}
MAP_CATALOG_RESOURCE = "maps.yaml.dvpl"
_HIDDEN_MAPS = frozenset(key for key, ident in _SUPPORTED.items() if 44 <= ident <= 72)
# Stable IDs also identify the variant in native replay recordings.
WEATHER_MAPS = {
    'rudniki_evening': (101, 'rudniki', 'evening'),
    'rudniki_fog': (102, 'rudniki', 'fog'),
    'amigosville_evening': (103, 'amigosville', 'evening'),
    'amigosville_fog': (104, 'amigosville', 'fog'),
    'italy_evening': (105, 'italy', 'evening'),
    'italy_fog': (106, 'italy', 'fog'),
}


def map_team_capacity(map_id):
    return 5 if map_id in (_SUPPORTED["rudniki_01"], _SUPPORTED["rudniki_02"]) else 7

# Local training placement, shared by solo and room runtimes. The source map
# tree stays intact: its canopy obscured the native camera behind this slot.
_LOCAL_SPAWN_ADJUSTMENTS = {
    "pliego": {(1, 3): ((69.106453, 239.036133), (61.106453, 239.036133))},
}


@dataclass(frozen=True)
class MapSpec:
    key: str
    map_id: int
    state_tag: str
    local_name: str
    map_root: Path
    scene_path: Path
    geometry_path: Path
    lka_path: Path
    heightmap_path: Path
    mkm_path: Path
    effects_path: Path
    bounds: tuple[float, float, float, float]
    landscape_bbox: tuple[float, float, float, float, float, float]
    team_spawns: tuple[tuple[tuple[float, float], ...],
                       tuple[tuple[float, float], ...]]
    spawn_source: str
    allow_empty_obstacles: bool = False
    capture_point: CapturePoint | None = None
    strategic_points: tuple[StrategicPoint, ...] = ()

    def spawn(self, team, slot):
        if type(team) is not int or team not in (1, 2):
            raise ValueError("Invalid map spawn team")
        points = self.team_spawns[team - 1]
        if type(slot) is not int or not 0 <= slot < len(points):
            raise ValueError("Map has no spawn for this team slot")
        return points[slot]

    def yaw(self, team, slot):
        x, z = self.spawn(team, slot)
        opposing = self.team_spawns[1 if team == 1 else 0]
        target_x = sum(point[0] for point in opposing) / len(opposing)
        target_z = sum(point[1] for point in opposing) / len(opposing)
        angle = math.atan2(target_x - x, target_z - z)
        # Match the pinned game's familiar negative-pi representation for a
        # vehicle facing exactly toward -Z.
        return -math.pi if math.isclose(angle, math.pi, abs_tol=1e-12) else angle


def supported_maps(game=None):
    """Training map keys admitted by this pinned build.

    When ``game`` is supplied, omit rows unavailable in this exact client
    tree. The private ``user_map`` becomes available only in a lab catalog
    overlay, so it naturally stays out of the pinned game view.
    """
    if game is None:
        return (*_SUPPORTED, *WEATHER_MAPS)
    return tuple(training_map_catalog(game).values())


def expose_training_maps(document):
    """Enable the same pinned hidden maps in a prepared client's picker."""
    changed = False
    for key in _HIDDEN_MAPS:
        row = document["maps"].get(key)
        if (isinstance(row, dict) and row.get("id") == _SUPPORTED[key]
                and row.get("avaliableInTrainingRoom") is not True):
            row["avaliableInTrainingRoom"] = True
            changed = True
    return changed


def weather_map_row(row, key):
    ident, _, _ = WEATHER_MAPS[key]
    scene = PurePosixPath(row['localName']).with_name(key + '.sc2')
    return dict(row, id=ident, localName=str(scene), avaliableInTrainingRoom=True)


def training_map_catalog(game):
    """Return the cheap ID -> key index without parsing every map scene.

    Scene, collision and heightmap resources are decoded only when a battle
    selects that map. This keeps server startup independent of catalog size.
    """
    rows = yaml.safe_load(read(Path(game) / "Data/maps.yaml.dvpl"))
    maps = rows.get("maps") if isinstance(rows, dict) else None
    if not isinstance(maps, dict):
        raise ValueError("Pinned map catalog is invalid")
    result = {}
    for key, expected_id in _SUPPORTED.items():
        row = maps.get(key)
        if (not isinstance(row, dict) or type(row.get("id")) is not int
                or row["id"] != expected_id
                or (key not in _HIDDEN_MAPS and row.get("avaliableInTrainingRoom") is not True)):
            continue
        if row["id"] in result:
            raise ValueError("Training map catalog contains a duplicate map ID")
        result[row["id"]] = key
    for key, (ident, original, _) in WEATHER_MAPS.items():
        if _SUPPORTED[original] in result:
            if ident in result or any(isinstance(row, dict) and row.get('id') == ident
                                      for name, row in maps.items() if name != key):
                raise ValueError(f'Weather map ID {ident} is already assigned')
            result[ident] = key
    return result


def load_map_spec(game, key="rudniki"):
    game = Path(game).resolve()
    if isinstance(key, str) and key in WEATHER_MAPS:
        ident, original, _ = WEATHER_MAPS[key]
        return replace(_load_map_spec(str(game), original), key=key, map_id=ident)
    if not isinstance(key, str) or key not in _SUPPORTED:
        raise ValueError("training_map must be one of: " + ", ".join(supported_maps()))
    return _load_map_spec(str(game), key)


@lru_cache(maxsize=4)
def _load_map_spec(game, key):
    game = Path(game)
    rows = yaml.safe_load(read(game / "Data/maps.yaml.dvpl"))
    maps = rows.get("maps") if isinstance(rows, dict) else None
    row = maps.get(key) if isinstance(maps, dict) else None
    if not isinstance(row, dict):
        raise ValueError(f"Pinned map catalog has no entry for {key}")
    if (row.get("id") != _SUPPORTED[key]
            or (key not in _HIDDEN_MAPS and row.get("avaliableInTrainingRoom") is not True)):
        raise ValueError(f"Pinned map metadata does not admit {key} for training")

    local_name = row.get("localName")
    if not isinstance(local_name, str):
        raise ValueError(f"Pinned map {key} has no local scene name")
    relative_scene = PurePosixPath(local_name)
    if (relative_scene.is_absolute() or ".." in relative_scene.parts
            or relative_scene.suffix != ".sc2" or not relative_scene.parts):
        raise ValueError(f"Invalid pinned scene path for {key}")
    map_root = game / "Data/3d/Maps" / Path(*relative_scene.parts[:-1])
    scene_path = map_root / (relative_scene.name + ".dvpl")
    geometry_path = map_root / (relative_scene.with_suffix(".scg").name + ".dvpl")
    for path in (scene_path, geometry_path):
        if not path.is_file():
            raise ValueError(f"Pinned map resource is missing: {path.name}")

    # Karelia omits a scene-state tag in maps.yaml; active_nodes uses the
    # empty tag for its default hierarchy state.
    tag = row.get("tags")
    if tag is None:
        tag = ""
    if not isinstance(tag, str) or "/" in tag or "\\" in tag:
        raise ValueError(f"Invalid pinned map state tag for {key}")
    data = scene(scene_path)
    borders, terrain_data, landscapes = [], [], []
    capture_points = []
    strategic_points = []
    for node, comps in active_nodes(data.get("#hierarchy", []), tag):
        properties = comps.get('CustomPropertiesComponent', {}).get('cpc.properties.archive', {})
        if properties.get('type') == 'strategicpoint':
            position = comps.get('TransformComponent', {}).get('tc.worldTranslation')
            if position is not None:
                strategic_points.append(StrategicPoint(
                    (float(position[0]), float(position[2]), float(position[1])),
                    radius=float(properties.get('radius', 15.)),
                    points_per_second=float(properties.get('pointsPerSecond', 1.)),
                    max_points_per_second=float(properties.get('maxPointsPerSecond', 3.)),
                    base_id=int(properties['baseID']),
                    capture_points=int(properties.get('capturePoints', 15)),
                    income=int(properties.get('incomeVictoryPoints', 5)),
                    income_seconds=float(properties.get('spawnVictoryPointsTime', 2.)),
                    capture_bonus=int(properties.get('bonusCapture', 0)),
                    loss_penalty=int(properties.get('penaltyLoss', 0))))
        if properties.get('type') == 'controlpoint':
            position = comps.get('TransformComponent', {}).get('tc.worldTranslation')
            if position is not None:
                capture_points.append(CapturePoint(
                    (float(position[0]), float(position[2]), float(position[1])),
                    radius=float(properties.get('radius', 25.)),
                    points_per_second=float(properties.get('pointsPerSecond', 1.)),
                    max_points_per_second=float(properties.get('maxPointsPerSecond', 3.2)),
                    base_id=int(properties.get('baseID', 0))))
        if "MapBorderComponent" in comps:
            borders.append(comps["MapBorderComponent"])
        if "TerrainDataComponent" in comps:
            terrain_data.append(comps["TerrainDataComponent"])
        if node.get("name") == "Landscape":
            render = comps.get("RenderComponent", {}).get("rc.renderObj", {})
            if isinstance(render.get("bbox"), bytes):
                landscapes.append(render)
    if len(borders) != 1 or len(terrain_data) != 1 or len(landscapes) != 1:
        raise ValueError(f"Pinned map {key} has ambiguous border or terrain metadata")
    border_raw = borders[0].get("mbc.rect")
    bbox_raw = landscapes[0]["bbox"]
    if not isinstance(border_raw, bytes) or len(border_raw) != 16:
        raise ValueError(f"Pinned map {key} has an invalid border")
    if len(bbox_raw) != 24:
        raise ValueError(f"Pinned map {key} has an invalid landscape bounds")
    bounds = struct.unpack("<4f", border_raw)
    bbox = struct.unpack("<6f", bbox_raw)
    if not all(math.isfinite(value) for value in (*bounds, *bbox)):
        raise ValueError(f"Pinned map {key} has non-finite bounds")
    x0, z0, y0, x1, z1, y1 = bbox
    if not (x0 < x1 and z0 < z1 and y0 < y1):
        raise ValueError(f"Pinned map {key} has empty landscape bounds")
    if not math.isclose(x1 - x0, z1 - z0, rel_tol=0, abs_tol=1e-4):
        raise ValueError(f"Pinned map {key} has a non-square heightmap domain")

    terrain = terrain_data[0]
    lka_paths = terrain.get("tdc.lkaPaths")
    lka_relative = lka_paths.get(tag) if isinstance(lka_paths, dict) else None
    if (lka_relative is None and isinstance(lka_paths, dict)
            and (set(lka_paths) == {""} or key == "lumber_01")):
        # Mines labels its active destruction state rd0 in the scene, while
        # its single TerrainData LKA path is the empty/default tag.
        lka_relative = lka_paths[""]
    mkm_relative = terrain.get("tdc.mkmPath")
    effects_relative = terrain.get("tdc.mapEffectsPath")
    for value in (lka_relative, mkm_relative, effects_relative):
        if not isinstance(value, str):
            raise ValueError(f"Pinned map {key} is missing a selected terrain resource")

    def terrain_path(relative):
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"Invalid terrain resource path for {key}")
        resolved = map_root / Path(*path.parts)
        return resolved if resolved.suffix == ".dvpl" else Path(str(resolved) + ".dvpl")

    lka_path, mkm_path, effects_path = map(terrain_path,
        (lka_relative, mkm_relative, effects_relative))
    for path in (lka_path, mkm_path, effects_path):
        if not path.is_file():
            raise ValueError(f"Pinned map resource is missing: {path.name}")

    # Several map variants can share a directory (including the lab's artist
    # map beside the original Rudniki heightmap). Select the heightmap named by
    # the active Landscape instead of guessing from every file in the folder.
    heightmap_relative = landscapes[0].get("hmap")
    if not isinstance(heightmap_relative, str):
        raise ValueError(f"Pinned map {key} has no Landscape heightmap")
    heightmap_path = terrain_path(heightmap_relative)
    if not heightmap_path.is_file():
        raise ValueError(f"Pinned map resource is missing: {heightmap_path.name}")
    heightmap = read(heightmap_path)
    if len(heightmap) < 8:
        raise ValueError(f"Pinned map {key} has a truncated heightmap")
    size, tile = struct.unpack_from("<II", heightmap)
    if (size < 2 or tile < 1 or size % tile
            or len(heightmap) != 8 + size * size * 2):
        raise ValueError(f"Pinned map {key} has an unsupported heightmap layout")

    mkm = read(mkm_path)
    if len(mkm) < 24:
        raise ValueError(f"Pinned map {key} has a truncated MKM")
    magic, version, surface_size, payload, reserved0, reserved1 = struct.unpack_from(
        "<4sIIIII", mkm)
    if (magic != b"kkm\0" or version != 1 or surface_size < 1
            or payload != surface_size * surface_size // 4
            or (reserved0, reserved1) != (0, 0) or len(mkm) != 24 + payload):
        raise ValueError(f"Pinned map {key} has an unsupported MKM layout")

    points = _spawn_points(key, row)
    min_x, min_z, max_x, max_z = bounds
    if min_x >= max_x or min_z >= max_z:
        raise ValueError(f"Pinned map {key} has empty map borders")
    if any(not (min_x <= x <= max_x and min_z <= z <= max_z)
           for team in points for x, z in team):
        raise ValueError(f"Pinned map {key} has a spawn outside its map border")
    minimum = map_team_capacity(_SUPPORTED[key])
    if any(len(team) < minimum for team in points):
        raise ValueError(f"Pinned map {key} has fewer than {minimum} team spawns")
    return MapSpec(key, _SUPPORTED[key], tag, local_name, map_root,
                   scene_path, geometry_path, lka_path, heightmap_path,
                   mkm_path, effects_path, bounds, bbox, points,
                   "maps.yaml with local camera-clearance adjustment" if key in _LOCAL_SPAWN_ADJUSTMENTS
                   else "maps.yaml", capture_point=capture_points[0] if len(capture_points) == 1 else None,
                   strategic_points=tuple(sorted(strategic_points, key=lambda p: p.base_id)))


def _spawn_points(key, row):
    metadata = row.get("assaultRespawnPoints")
    if not isinstance(metadata, dict):
        raise ValueError(f"Pinned map {key} has no assault respawn points")
    teams = []
    for side in ("allies", "enemies"):
        groups = metadata.get(side)
        group = next((item for item in groups if item.get("respawnNumber") == 1), None) \
            if isinstance(groups, list) else None
        if group is None and isinstance(groups, list):
            # Lagoon's training data contains allied first-wave points and
            # enemy second-wave points only. The latter still form a valid
            # opposing spawn set for the local training arena.
            valid = [item for item in groups if isinstance(item, dict)
                     and type(item.get("respawnNumber")) is int]
            group = min(valid, key=lambda item: item["respawnNumber"]) if valid else None
        raw_points = group.get("points") if isinstance(group, dict) else None
        if not isinstance(raw_points, list):
            raise ValueError(f"Pinned map {key} has no first-wave {side} spawns")
        points = []
        for point in raw_points:
            if (not isinstance(point, list) or len(point) != 2
                    or any(type(value) not in (int, float) or not math.isfinite(value)
                           for value in point)):
                raise ValueError(f"Pinned map {key} has malformed {side} spawns")
            points.append((float(point[0]), float(point[1])))
        teams.append(tuple(points))
    for (team, slot), (expected, replacement) in _LOCAL_SPAWN_ADJUSTMENTS.get(key, {}).items():
        points = list(teams[team-1])
        if slot >= len(points) or any(abs(a-b)>1e-5 for a,b in zip(points[slot],expected)):
            raise ValueError(f"Pinned map {key} spawn layout changed; review the local adjustment")
        points[slot] = replacement
        teams[team-1] = tuple(points)
    return tuple(teams)
