"""Experimental imported or locally authored triangle queries; no battle semantics."""
from dataclasses import dataclass, field
import hashlib
import json
import math
from numbers import Real
from pathlib import Path
import re

import numpy as np

from .orientation import matrix

MAX_FILE_BYTES = 8 * 1024**2
MAX_TRIANGLES = 20000
MAX_VERTICES = MAX_TRIANGLES * 3
MAX_MESHES = 32
SOURCE_CLASSIFICATION = 'transferred_viewer'
LOCAL_AUTHORED = 'local_authored'
SOURCE_CLASSIFICATIONS = frozenset((SOURCE_CLASSIFICATION, LOCAL_AUTHORED))
COORDINATE_SYSTEM = 'bigworld_y_up_meters'
AUTHORED_VERTEX_FRAME = 'anchor_local_zero_pose'
AUTHORED_KEYS = frozenset(('schema', 'coordinate_system', 'source_classification', 'vertex_frame',
                           'configuration', 'provenance', 'turret_position', 'gun_position',
                           'meshes', 'geometry_sha256'))
AUTHORED_COORDINATE_BOUND = 20.  # Meters from the anchor; larger than any tank.
AUTHORED_MIN_TRIANGLE_AREA = 1e-8  # Square meters; rejects slivers below 0.1 mm x 0.2 mm.
AUTHORED_MIN_COMPONENT_VOLUME = 1e-6  # Cubic meters (1 cm^3) per closed component.
NAMES = frozenset(('fuel', 'engine', 'ammo', 'crew_driver', 'crew_gunner',
                   'crew_loader', 'crew_commander', 'crew_gunner_1', 'crew_gunner_2',
                   'crew_loader_1', 'crew_loader_2'))
BOUNDARY_TOLERANCE = 1e-8  # Meters perpendicular to a triangle plane.
BARYCENTRIC_TOLERANCE = 1e-9


def _closed_components(triangles):
    """Exact-position welding; closed oriented two-manifold components only."""
    edges, links, faces = {}, {}, set()
    for index, triangle in enumerate(triangles):
        face = tuple(sorted(triangle))
        if face in faces:
            raise ValueError('Duplicate faces cannot define an internal volume')
        faces.add(face)
        for a, b, c in (triangle, (triangle[1], triangle[2], triangle[0]),
                        (triangle[2], triangle[0], triangle[1])):
            edge = tuple(sorted((a, b)))
            edges.setdefault(edge, []).append((index, 1 if a < b else -1))
            link = links.setdefault(a, {})
            link.setdefault(b, set()).add(c)
            link.setdefault(c, set()).add(b)
    neighbors = [set() for _ in triangles]
    for owners in edges.values():
        if len(owners) != 2 or owners[0][1] + owners[1][1] != 0:
            raise ValueError('Volume query requires closed consistently oriented mesh edges')
        a, b = owners[0][0], owners[1][0]
        neighbors[a].add(b)
        neighbors[b].add(a)
    for link in links.values():
        if any(len(adjacent) != 2 for adjacent in link.values()):
            raise ValueError('Nonmanifold volume vertex')
        visited, pending = set(), [next(iter(link))]
        while pending:
            vertex = pending.pop()
            if vertex not in visited:
                visited.add(vertex)
                pending.extend(link[vertex] - visited)
        if len(visited) != len(link):
            raise ValueError('Pinched or disconnected volume vertex link')
    remaining, components = set(range(len(triangles))), []
    while remaining:
        pending, component = [min(remaining)], []
        while pending:
            index = pending.pop()
            if index in remaining:
                remaining.remove(index)
                component.append(index)
                pending.extend(neighbors[index] & remaining)
        points = np.asarray([triangles[i] for i in component])
        shifted = points - points[0, 0]
        signed_volume = np.einsum('ij,ij->i', shifted[:, 0],
                                 np.cross(shifted[:, 1], shifted[:, 2])).sum() / 6
        if abs(signed_volume) <= 1e-12:
            raise ValueError('Closed component has no measurable signed volume')
        components.append(tuple(component))
    return tuple(components)


def _boundary_triangle(point, triangles):
    a = triangles[:, 0]
    e1, e2 = triangles[:, 1] - a, triangles[:, 2] - a
    normals = np.cross(e1, e2)
    squared = np.einsum('ij,ij->i', normals, normals)
    delta = point - a
    plane = np.einsum('ij,ij->i', delta, normals) / np.sqrt(squared)
    u = np.einsum('ij,ij->i', np.cross(delta, e2), normals) / squared
    v = np.einsum('ij,ij->i', np.cross(e1, delta), normals) / squared
    valid = ((np.abs(plane) <= BOUNDARY_TOLERANCE) & (u >= -BARYCENTRIC_TOLERANCE)
             & (v >= -BARYCENTRIC_TOLERANCE) & (u + v <= 1 + BARYCENTRIC_TOLERANCE))
    indices = np.flatnonzero(valid)
    return int(indices[0]) if len(indices) else None


def _inside_component(point, triangles):
    """Signed solid-angle winding number, independent of global face winding."""
    vectors = triangles - point
    a, b, c = vectors[:, 0], vectors[:, 1], vectors[:, 2]
    la, lb, lc = (np.linalg.norm(x, axis=1) for x in (a, b, c))
    numerator = np.einsum('ij,ij->i', a, np.cross(b, c))
    denominator = (la * lb * lc + np.einsum('ij,ij->i', a, b) * lc
                   + np.einsum('ij,ij->i', b, c) * la
                   + np.einsum('ij,ij->i', c, a) * lb)
    winding = float((2 * np.arctan2(numerator, denominator)).sum() / (4 * math.pi))
    integer = round(winding)
    if abs(winding - integer) > 1e-6 or abs(integer) > 1:
        raise ValueError('Ambiguous solid-angle winding at volume-query start')
    return integer != 0


def _coplanar_contacts(origin, direction, length, a, e1, e2):
    """Clip a coplanar segment against actual triangle barycentric halfplanes."""
    normals = np.cross(e1, e2)
    squared = np.einsum('ij,ij->i', normals, normals)
    magnitudes = np.sqrt(squared)
    plane = np.einsum('ij,ij->i', origin - a, normals) / magnitudes
    parallel = np.abs(normals @ direction) <= 1e-12 * magnitudes
    candidates = np.flatnonzero(parallel & (np.abs(plane) <= BOUNDARY_TOLERANCE))
    found = []
    for i in candidates:
        u = float(np.dot(np.cross(origin - a[i], e2[i]), normals[i]) / squared[i])
        v = float(np.dot(np.cross(e1[i], origin - a[i]), normals[i]) / squared[i])
        du = float(np.dot(np.cross(direction, e2[i]), normals[i]) / squared[i])
        dv = float(np.dot(np.cross(e1[i], direction), normals[i]) / squared[i])
        lo, hi = 0., length
        for value, slope in ((u, du), (v, dv), (1 - u - v, -du - dv)):
            if abs(slope) <= 1e-15:
                if value < -BARYCENTRIC_TOLERANCE:
                    hi = -1.
                    break
            elif slope > 0:
                lo = max(lo, (-BARYCENTRIC_TOLERANCE - value) / slope)
            else:
                hi = min(hi, (-BARYCENTRIC_TOLERANCE - value) / slope)
        if lo <= hi:
            found.append((lo, int(i)))
    return found


def finite_vector(value, length, *, bound=1000.):
    if not isinstance(value, (tuple, list)) or len(value) != length:
        raise ValueError('Invalid geometry vector length')
    if any(isinstance(x, bool) or not isinstance(x, Real)
           or not math.isfinite(x) or abs(x) > bound for x in value):
        raise ValueError('Invalid finite geometry coordinate')
    return tuple(float(x) for x in value)


def read_document(path, limit=MAX_FILE_BYTES):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Geometry document exceeds size limit')
    try:
        document = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(
            ValueError('Nonfinite JSON number')))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid geometry JSON') from exc
    if not isinstance(document, dict):
        raise ValueError('Geometry document must be an object')
    return document, raw


@dataclass(frozen=True)
class GeometryConfiguration:
    nation: str
    vehicle: str
    turret: str
    gun: str

    def __post_init__(self):
        for value in (self.nation, self.vehicle, self.turret, self.gun):
            if (not isinstance(value, str) or value in ('.', '..')
                    or not re.fullmatch(r'[A-Za-z0-9_.-]+', value)):
                raise ValueError('Explicit geometry configuration is required')

    @classmethod
    def from_definition(cls, definition):
        return cls(definition.nation, definition.name,
                   definition.configuration.turret, definition.configuration.gun)


@dataclass(frozen=True)
class GeometryProvenance:
    url: str
    captured_at: str
    source_notice: str
    input_sha256: tuple

    def __post_init__(self):
        from datetime import datetime
        from urllib.parse import urlsplit
        if not isinstance(self.url, str):
            raise ValueError('Missing geometry provenance URL')
        url = urlsplit(self.url)
        if (url.scheme != 'https' or url.hostname != 'armor.wotinspector.com'
                or '/blitz11.21.0/' not in url.path or url.username or url.password):
            raise ValueError('Unsupported geometry provenance URL')
        if not isinstance(self.captured_at, str):
            raise ValueError('Missing capture timestamp')
        try:
            stamp = datetime.fromisoformat(self.captured_at.replace('Z', '+00:00'))
        except ValueError as exc:
            raise ValueError('Invalid capture timestamp') from exc
        if stamp.tzinfo is None:
            raise ValueError('Capture timestamp needs a timezone')
        if (not isinstance(self.source_notice, str) or len(self.source_notice) > 1000
                or 'Tanks Blitz' not in self.source_notice):
            raise ValueError('Transferred-source notice is required')
        if (not isinstance(self.input_sha256, (list, tuple)) or len(self.input_sha256) != 2 or any(
                not isinstance(pair, (list, tuple)) for pair in self.input_sha256)):
            raise ValueError('Invalid input evidence hashes')
        entries = tuple(tuple(pair) for pair in self.input_sha256)
        if (len(entries) != 2
                or any(len(p) != 2 or not isinstance(p[0], str) or not isinstance(p[1], str)
                       or not re.fullmatch('[0-9a-f]{64}', p[1]) for p in entries)):
            raise ValueError('Missing input evidence hashes')
        if {p[0] for p in entries} != {'draws', 'palette'}:
            raise ValueError('Invalid input evidence hash names')
        object.__setattr__(self, 'input_sha256', entries)


def _aware_timestamp(value, name):
    from datetime import datetime
    if not isinstance(value, str):
        raise ValueError(f'Missing {name} timestamp')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(f'Invalid {name} timestamp') from exc
    if stamp.tzinfo is None:
        raise ValueError(f'{name.capitalize()} timestamp needs a timezone')


@dataclass(frozen=True)
class AuthoredProvenance:
    """Who made a hand-authored local approximation, with which tool and when.

    This deliberately has no URL or input-evidence fields: an authored model
    is not a transfer from a viewer and must not claim to be one.
    """
    author: str
    tool: str
    tool_version: str
    authored_at: str
    approximation_notice: str

    def __post_init__(self):
        for value, limit in ((self.author, 200), (self.tool, 200), (self.tool_version, 64),
                             (self.approximation_notice, 1000)):
            if (not isinstance(value, str) or not value.strip() or len(value) > limit
                    or any(ord(c) < 32 for c in value)):
                raise ValueError('Local authored provenance needs bounded author/tool/version/notice')
            if '://' in value or 'wotinspector' in value.lower():
                raise ValueError('Local authored provenance must not reference a transferred source')
        _aware_timestamp(self.authored_at, 'authoring')


@dataclass(frozen=True)
class InternalMesh:
    name: str
    anchor: str
    triangles: tuple

    def __post_init__(self):
        if self.name not in NAMES or self.anchor not in ('hull', 'turret', 'gun'):
            raise ValueError('Unknown internal mesh or anchor')
        if not isinstance(self.triangles, (tuple, list)) or not 0 < len(self.triangles) <= MAX_TRIANGLES:
            raise ValueError('Invalid triangle count')
        converted = []
        for triangle in self.triangles:
            if not isinstance(triangle, (tuple, list)) or len(triangle) != 3:
                raise ValueError('A triangle needs three vertices')
            points = tuple(finite_vector(p, 3) for p in triangle)
            if np.linalg.norm(np.cross(np.subtract(points[1], points[0]),
                                       np.subtract(points[2], points[0]))) <= 1e-12:
                raise ValueError('Degenerate internal triangle')
            converted.append(points)
        object.__setattr__(self, 'triangles', tuple(converted))
        points = np.asarray(converted).reshape(-1, 3)
        low, high = points.min(axis=0), points.max(axis=0)
        # Derived immutable cache, excluded from the serialized dataclass
        # schema. Cover the plane and barycentric slack of exact queries.
        padding = BOUNDARY_TOLERANCE + 2 * float(np.max(high - low)) * BARYCENTRIC_TOLERANCE
        object.__setattr__(self, '_bounds', (tuple(low - padding), tuple(high + padding)))


@dataclass(frozen=True)
class InternalContact:
    mesh: str
    distance: float
    point: tuple
    normal: tuple | None
    kind: str = 'surface'


def _check_authored_meshes(meshes):
    """Stricter topology for hand-authored volumes than for transferred data.

    Every mesh must be a union of closed oriented two-manifolds (as required
    by volume_segment), wound outward (positive signed volume with the
    cross(b - a, c - a) face normal in game coordinates), with no slivers,
    and each component must enclose at least 1 cm^3 near its anchor.
    """
    for mesh in meshes:
        triangles = np.asarray(mesh.triangles)
        if np.max(np.abs(triangles)) > AUTHORED_COORDINATE_BOUND:
            raise ValueError(f'Authored mesh {mesh.name} is not anchor-local '
                             f'(coordinate beyond {AUTHORED_COORDINATE_BOUND:g} m)')
        normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
        if np.min(np.linalg.norm(normals, axis=1)) / 2 < AUTHORED_MIN_TRIANGLE_AREA:
            raise ValueError(f'Authored mesh {mesh.name} has a degenerate sliver triangle')
        try:
            components = _closed_components(mesh.triangles)
        except ValueError as exc:
            raise ValueError(f'Authored mesh {mesh.name}: {exc}') from exc
        for component in components:
            points = triangles[list(component)]
            shifted = points - points[0, 0]
            volume = np.einsum('ij,ij->i', shifted[:, 0],
                               np.cross(shifted[:, 1], shifted[:, 2])).sum() / 6
            if volume < 0:
                raise ValueError(f'Authored mesh {mesh.name} is wound inward')
            if volume < AUTHORED_MIN_COMPONENT_VOLUME:
                raise ValueError(f'Authored mesh {mesh.name} encloses a negligible volume')


@dataclass(frozen=True)
class InternalGeometry:
    configuration: GeometryConfiguration
    source_classification: str
    provenance: GeometryProvenance | AuthoredProvenance
    turret_position: tuple
    gun_position: tuple
    meshes: tuple

    def __post_init__(self):
        # Each source has its own provenance type; neither can borrow the other's.
        provenance = {SOURCE_CLASSIFICATION: GeometryProvenance, LOCAL_AUTHORED: AuthoredProvenance}.get(
            self.source_classification if isinstance(self.source_classification, str) else None)
        if (not isinstance(self.configuration, GeometryConfiguration)
                or provenance is None or not isinstance(self.provenance, provenance)):
            raise ValueError('Unsupported internal geometry source')
        meshes = tuple(self.meshes)
        if (not 0 < len(meshes) <= MAX_MESHES or any(not isinstance(m, InternalMesh) for m in meshes)
                or len({(m.name, m.anchor) for m in meshes}) != len(meshes)
                or sum(len(m.triangles) for m in meshes) > MAX_TRIANGLES):
            raise ValueError('Invalid internal geometry mesh set')
        crew_names = [mesh.name for mesh in meshes if mesh.name.startswith('crew_')]
        if len(set(crew_names)) != len(crew_names):
            raise ValueError('A crew identity requires one anchored mesh')
        object.__setattr__(self, 'meshes', meshes)
        object.__setattr__(self, 'turret_position', finite_vector(self.turret_position, 3))
        object.__setattr__(self, 'gun_position', finite_vector(self.gun_position, 3))
        if self.source_classification == LOCAL_AUTHORED:
            _check_authored_meshes(meshes)

    def geometry_digest(self):
        """SHA-256 of canonical configuration, pivots and meshes.

        Detects accidental edits of a manifest. It is not a signature and
        says nothing about where the geometry came from.
        """
        canonical = dict(configuration=[self.configuration.nation, self.configuration.vehicle,
                                        self.configuration.turret, self.configuration.gun],
                         turret_position=list(self.turret_position),
                         gun_position=list(self.gun_position),
                         meshes=[[m.name, m.anchor, [[list(p) for p in t] for t in m.triangles]]
                                 for m in sorted(self.meshes, key=lambda m: (m.name, m.anchor))])
        raw = json.dumps(canonical, allow_nan=False, separators=(',', ':'), ensure_ascii=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _frame(self, mesh, turret_yaw, gun_pitch):
        turret = matrix(turret_yaw)
        if mesh.anchor == 'turret':
            return turret, np.asarray(self.turret_position)
        if mesh.anchor == 'gun':
            return (turret @ matrix(0., gun_pitch),
                    np.asarray(self.turret_position) + turret @ self.gun_position)
        return np.eye(3), np.zeros(3)

    def volume_segment(self, start, end, pose, turret_yaw=0., gun_pitch=0.):
        """Boundary-inclusive intersection with closed oriented mesh interiors.

        Exact-welded disconnected components represent a union of interiors,
        not nested cavity subtraction. Initial boundary points (plane distance
        <=1e-8 m and barycentric slack <=1e-9) return kind='boundary'. Strict
        interior starts return distance=0, kind='inside', normal=None. Even a
        zero-length segment can have these initial contacts. Open, nonmanifold,
        duplicated or inconsistently oriented surfaces raise ValueError; use
        segment() when only surface crossings are intended.
        """
        components = tuple(_closed_components(mesh.triangles) for mesh in self.meshes)
        return self._volume_segment(components, start, end, pose, turret_yaw, gun_pitch)

    def prepare_volume(self):
        """Validate topology once for repeated immutable volume queries."""
        return PreparedInternalGeometry(self)

    def _volume_segment(self, components, start, end, pose, turret_yaw, gun_pitch):
        start = np.asarray(finite_vector(start, 3, bound=1e9))
        end = finite_vector(end, 3, bound=1e9)
        position = np.asarray(finite_vector(tuple(pose.position), 3, bound=1e9))
        angles = finite_vector((pose.yaw, pose.pitch, pose.roll, turret_yaw, gun_pitch), 5)
        hull = matrix(*angles[:3])
        origin = (start - position) @ hull
        surfaces = {contact.mesh: contact for contact in self.segment(
            tuple(start), end, pose, turret_yaw, gun_pitch)}
        for mesh, groups in zip(self.meshes, components):
            rotation, offset = self._frame(mesh, turret_yaw, gun_pitch)
            local = (origin - offset) @ rotation
            low, high = mesh._bounds
            if any(point < lo or point > hi for point, lo, hi in zip(local, low, high)):
                continue
            triangles = np.asarray(mesh.triangles)
            boundary, inside = None, False
            for group in groups:
                component = triangles[list(group)]
                candidate = _boundary_triangle(local, component)
                if candidate is not None:
                    boundary = group[candidate]
                elif _inside_component(local, component):
                    inside = True
                    break
            if inside:
                surfaces[mesh.name] = InternalContact(mesh.name, 0., tuple(start), None, 'inside')
            elif boundary is not None and (mesh.name not in surfaces
                                           or surfaces[mesh.name].kind != 'inside'):
                triangle = triangles[boundary]
                normal = np.cross(triangle[1] - triangle[0], triangle[2] - triangle[0])
                normal = normal / np.linalg.norm(normal) @ rotation.T @ hull.T
                surfaces[mesh.name] = InternalContact(mesh.name, 0., tuple(start),
                                                      tuple(normal), 'boundary')
        return tuple(sorted(surfaces.values(), key=lambda h: (h.distance, h.mesh)))

    def segment(self, start, end, pose, turret_yaw=0., gun_pitch=0.):
        """Surface crossings, both windings, one nearest contact per module.

        A module may occupy separate hull/turret volumes. Each volume keeps
        its own frame, while the module receives only its first intersection.

        This does not test volume containment. A segment wholly inside a
        closed mesh has no surface contact. Distances are in world meters.
        """
        start = np.asarray(finite_vector(start, 3, bound=1e9))
        end = np.asarray(finite_vector(end, 3, bound=1e9))
        position = np.asarray(finite_vector(tuple(pose.position), 3, bound=1e9))
        angles = finite_vector((pose.yaw, pose.pitch, pose.roll, turret_yaw, gun_pitch), 5)
        length = float(np.linalg.norm(end - start))
        if length <= 1e-12:
            return ()
        hull = matrix(*angles[:3])
        origin = (start - position) @ hull
        direction = (end - start) / length @ hull
        contacts = {}
        for mesh in self.meshes:
            rotation, offset = self._frame(mesh, turret_yaw, gun_pitch)
            o, d = (origin - offset) @ rotation, direction @ rotation
            local_end = o + d * length
            low, high = mesh._bounds
            if any(max(a, b) < lo or min(a, b) > hi
                   for a, b, lo, hi in zip(o, local_end, low, high)):
                continue
            triangles = np.asarray(mesh.triangles)
            a = triangles[:, 0]
            e1, e2 = triangles[:, 1] - a, triangles[:, 2] - a
            cross = np.cross(d, e2)
            det = np.einsum('ij,ij->i', e1, cross)
            valid = np.abs(det) > 1e-12
            inv = np.divide(1., det, out=np.zeros_like(det), where=valid)
            delta = o - a
            u = np.einsum('ij,ij->i', delta, cross) * inv
            q = np.cross(delta, e1)
            v = q @ d * inv
            t = np.einsum('ij,ij->i', e2, q) * inv
            valid &= (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9) & (t >= 0) & (t <= length)
            indices = np.flatnonzero(valid)
            candidates = [(float(t[i]), int(i)) for i in indices]
            candidates.extend(_coplanar_contacts(o, d, length, a, e1, e2))
            if not candidates:
                continue
            distance, i = min(candidates)
            normal = np.cross(e1[i], e2[i])
            normal = normal / np.linalg.norm(normal) @ rotation.T @ hull.T
            point = start + (end - start) * (distance / length)
            previous = contacts.get(mesh.name)
            if previous is None or distance < previous.distance:
                contacts[mesh.name] = InternalContact(mesh.name, distance, tuple(point), tuple(normal))
        return tuple(sorted(contacts.values(), key=lambda h: (h.distance, h.mesh)))


@dataclass(frozen=True)
class PreparedInternalGeometry:
    """Closed-volume adapter retaining the exact immutable geometry validated.

    Construction validates every mesh. Queries reuse the resulting component
    indices directly, without topology validation, hashing or cache lookups.
    The source geometry and schema-1 serialization remain unchanged.
    """
    geometry: InternalGeometry
    _components: tuple = field(init=False, repr=False)

    def __post_init__(self):
        if not isinstance(self.geometry, InternalGeometry):
            raise ValueError('Prepared volume requires immutable internal geometry')
        object.__setattr__(self, '_components', tuple(
            _closed_components(mesh.triangles) for mesh in self.geometry.meshes))

    def volume_segment(self, start, end, pose, turret_yaw=0., gun_pitch=0.):
        """Use the same boundary and containment contract as volume_segment()."""
        return self.geometry._volume_segment(
            self._components, start, end, pose, turret_yaw, gun_pitch)


def load_internal_geometry(path, definition, *, expected_source_classification):
    """Load one manifest for the exact selected vehicle configuration.

    The caller states which source it accepts; the document must carry the
    same classification. transferred_viewer keeps the original schema-1
    contract. local_authored additionally has a closed key set, its own
    provenance, anchor_local_zero_pose vertices and a matching geometry digest.
    """
    document, _ = read_document(path)
    if (document.get('schema') != 1 or document.get('coordinate_system') != COORDINATE_SYSTEM
            or document.get('source_classification') != expected_source_classification
            or not isinstance(expected_source_classification, str)
            or expected_source_classification not in SOURCE_CLASSIFICATIONS):
        raise ValueError('Unexpected internal geometry format or source classification')
    authored = expected_source_classification == LOCAL_AUTHORED
    if authored and (set(document) != AUTHORED_KEYS
                     or document['vertex_frame'] != AUTHORED_VERTEX_FRAME):
        raise ValueError('Local authored geometry needs exactly the authored manifest fields')
    try:
        configuration = GeometryConfiguration(**document['configuration'])
        if configuration != GeometryConfiguration.from_definition(definition):
            raise ValueError('Internal geometry configuration mismatch')
        provenance = (AuthoredProvenance if authored else GeometryProvenance)(**document['provenance'])
        records = document['meshes']
        if (not isinstance(records, list) or not 0 < len(records) <= MAX_MESHES
                or any(not isinstance(m, dict) or not isinstance(m.get('triangles'), list) for m in records)
                or sum(len(m['triangles']) for m in records) > MAX_TRIANGLES):
            raise ValueError('Internal geometry exceeds mesh/triangle limits')
        meshes = tuple(InternalMesh(**m) for m in records)
        geometry = InternalGeometry(configuration, document['source_classification'], provenance,
                                    document['turret_position'], document['gun_position'], meshes)
    except (KeyError, TypeError) as exc:
        raise ValueError('Malformed internal geometry manifest') from exc
    if (geometry.turret_position != tuple(definition.aiming.turret_position)
            or geometry.gun_position != tuple(definition.aiming.gun_position)):
        raise ValueError('Internal geometry pivots disagree with selected vehicle')
    if authored and document['geometry_sha256'] != geometry.geometry_digest():
        raise ValueError('Local authored geometry digest mismatch (manifest edited after export?)')
    return geometry


def load_experimental_models(game, lab, records):
    """Opt-in manifests prepared before arena ticks, keyed by exact gun layout.

    Paths are operator-owned local files, relative to the lab if not absolute.
    The source acknowledgement is mandatory for each entry and must equal the
    manifest's own classification (transferred_viewer or local_authored).
    Other vehicles remain unaffected; duplicate configurations and invalid
    inputs fail startup.
    """
    from .vehicle_data import load_vehicle, VehicleConfiguration

    if not isinstance(records, list) or len(records) > MAX_MESHES:
        raise ValueError('experimental_internal_modules must be a list of at most 32 manifests')
    models = {}
    for entry in records:
        if (not isinstance(entry, dict) or set(entry) != {'path', 'source_classification'}
                or not isinstance(entry['source_classification'], str)
                or entry['source_classification'] not in SOURCE_CLASSIFICATIONS
                or not isinstance(entry['path'], str) or not entry['path'].strip()):
            raise ValueError('Experimental internal module entry needs path and '
                             'transferred_viewer or local_authored acknowledgement')
        path = Path(entry['path'])
        if not path.is_absolute():
            path = Path(lab) / path
        document, _ = read_document(path)
        try:
            configuration = GeometryConfiguration(**document['configuration'])
        except (KeyError, TypeError) as exc:
            raise ValueError('Missing experimental internal geometry configuration') from exc
        definition = load_vehicle(Path(game), configuration.nation, configuration.vehicle,
                                  VehicleConfiguration(turret=configuration.turret, gun=configuration.gun))
        model = load_internal_geometry(path, definition,
            expected_source_classification=entry['source_classification']).prepare_volume()
        if configuration in models:
            raise ValueError('Duplicate experimental internal geometry configuration')
        if not any(mesh.name == 'engine' for mesh in model.geometry.meshes):
            raise ValueError('Experimental engine damage needs an engine volume')
        models[configuration] = model
    return models
