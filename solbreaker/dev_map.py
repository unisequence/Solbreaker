"""Load a hash-checked development map from one isolated lab client tree."""
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat

from .dava_scene import active_nodes, scene
from .map_catalog import MapSpec, load_map_spec


def _relative_path(value):
    if not isinstance(value, str) or not value or any(c in value for c in ("\\", ":", "\0")):
        raise ValueError("Development map paths must be safe relative POSIX paths")
    path = PurePosixPath(value)
    if (path.is_absolute() or ".." in path.parts or not path.parts
            or str(path) != value):
        raise ValueError("Development map paths must be safe relative POSIX paths")
    return Path(*path.parts)


def _regular_file(root, relative):
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Development map overlay must not use symlinks: {relative}")
    try:
        regular = stat.S_ISREG(path.stat().st_mode)
        contained = path.resolve(strict=True).is_relative_to(root)
    except OSError as exc:
        raise ValueError(f"Development map file is missing: {relative}") from exc
    if not regular or not contained:
        raise ValueError(f"Development map file must be ordinary and inside its lab: {relative}")
    return path


def _client_relative(client, path):
    try:
        relative = Path(path).relative_to(client)
    except ValueError as exc:
        raise ValueError("Development map resource is outside the lab client") from exc
    return _relative_path(relative.as_posix()).as_posix()


def _team_spawns(value, bounds):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("Development map needs exactly two spawn teams")
    minimum_x, minimum_z, maximum_x, maximum_z = bounds
    teams, seen = [], set()
    for team in value:
        if not isinstance(team, list) or len(team) != 7:
            raise ValueError("Development map needs exactly seven spawns per team")
        points = []
        for pair in team:
            if (not isinstance(pair, list) or len(pair) != 2
                    or any(type(coordinate) not in (int, float) for coordinate in pair)):
                raise ValueError("Development map spawn must be a finite XZ pair")
            try:
                point = tuple(float(coordinate) for coordinate in pair)
            except OverflowError as exc:
                raise ValueError("Development map spawn must be a finite XZ pair") from exc
            if not all(math.isfinite(coordinate) for coordinate in point):
                raise ValueError("Development map spawn must be a finite XZ pair")
            x, z = point
            if not (minimum_x <= x <= maximum_x and minimum_z <= z <= maximum_z):
                raise ValueError("Development map spawn is outside the map border")
            if point in seen:
                raise ValueError("Development map spawns must not coincide")
            seen.add(point)
            points.append(point)
        teams.append(tuple(points))
    return tuple(teams)


def _check_landscape_heightmap(spec):
    heightmaps = []
    data = scene(spec.scene_path)
    for node, components in active_nodes(data.get("#hierarchy", []), spec.state_tag):
        if node.get("name") == "Landscape":
            render = components.get("RenderComponent", {}).get("rc.renderObj", {})
            heightmaps.append(render.get("hmap"))
    if len(heightmaps) != 1:
        raise ValueError("Development map must have exactly one active Landscape")
    relative = _relative_path(heightmaps[0])
    selected = spec.map_root / relative
    if selected.suffix != ".dvpl":
        selected = Path(str(selected) + ".dvpl")
    if selected != spec.heightmap_path:
        raise ValueError("Development map Landscape.hmap differs from the selected heightmap")


def _dev_map_paths(manifest):
    plural = manifest.get("dev_map_manifests")
    if plural is not None:
        if (not isinstance(plural, list) or not plural
                or any(not isinstance(item, str) or not item for item in plural)
                or len(set(plural)) != len(plural)):
            raise ValueError("dev_map_manifests must be a nonempty unique path list")
        return tuple(plural)
    single = manifest.get("dev_map_manifest")
    return (single,) if single is not None else ()


def _load_dev_map(manifest, lab, descriptor_path) -> MapSpec:
    lab = Path(lab).resolve(strict=True)
    client = lab / "client"
    configured_client = manifest.get("client")
    if (not isinstance(configured_client, str) or not configured_client
            or not Path(configured_client).is_absolute()
            or Path(configured_client).absolute() != client
            or client.is_symlink() or not client.is_dir()
            or client.resolve(strict=True) != client):
        raise ValueError("Development map client must be exactly lab/client inside the lab")
    descriptor = _regular_file(lab, _relative_path(descriptor_path))
    with descriptor.open(encoding="utf-8") as stream:
        document = json.load(stream)
    if (not isinstance(document, dict) or document.get("format") not in
            ("solbreaker-dev-map/v1", "blitz-dev-map/v1")
            or document.get("name") not in ("physics_lab", "user_map")):
        raise ValueError("Unsupported development map manifest")
    base_key = document.get("base_key")
    if not isinstance(base_key, str):
        raise ValueError("Development map needs a base_key")
    wire_id = document.get("wire_id")
    if type(wire_id) is not int or not 1 <= wire_id <= 0xffff:
        raise ValueError("Invalid development map wire_id")
    files = document.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Development map manifest needs resource hashes")
    checked = set()
    for relative, expected in files.items():
        path = _relative_path(relative)
        if not isinstance(expected, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected) is None:
            raise ValueError(f"Invalid development map SHA-256: {relative}")
        resource = _regular_file(client, path)
        with resource.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected.lower():
            raise ValueError(f"Development map SHA-256 mismatch: {relative}")
        checked.add(path.as_posix())

    spec = load_map_spec(client, base_key)
    if wire_id != spec.map_id:
        raise ValueError("Development map wire_id differs from the selected map")
    required = {_client_relative(client, path) for path in (
        spec.scene_path, spec.geometry_path, spec.heightmap_path)}
    if not required <= checked:
        raise ValueError("Development map must hash its scene, geometry and heightmap")
    _check_landscape_heightmap(spec)
    spawns = _team_spawns(document.get("team_spawns"), spec.bounds)
    return replace(spec, team_spawns=spawns,
                   spawn_source=f"{document['name']} dev overlay",
                   allow_empty_obstacles=document['name'] == 'user_map')


def dev_map_specs_for_lab(manifest, lab) -> tuple[MapSpec, ...]:
    """Load every hash-checked development map opted into this isolated lab."""
    specs = tuple(_load_dev_map(manifest, lab, path)
                  for path in _dev_map_paths(manifest))
    ids = [spec.map_id for spec in specs]
    keys = [spec.key for spec in specs]
    if len(set(ids)) != len(ids) or len(set(keys)) != len(keys):
        raise ValueError("Development map IDs and keys must be unique")
    return specs


def map_spec_for_lab(manifest, lab) -> MapSpec:
    """Resolve the manifest's default map, including a matching lab overlay."""
    training_map = manifest.get("training_map", "rudniki")
    for spec in dev_map_specs_for_lab(manifest, lab):
        if spec.key == training_map:
            return spec
    return load_map_spec(manifest["game"], training_map)
