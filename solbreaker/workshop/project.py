"""The versioned, engine-independent part of a tank project."""
from dataclasses import asdict, dataclass, field, fields
import json
import math
from pathlib import Path, PurePosixPath
import re
import tomllib

from ..connection import atomic_write


PARTS = ('hull', 'turret', 'gun', 'muzzle')
DONOR_PARTS = PARTS[:3]
FIT_DEFAULTS = dict(width=1., height=1., length=1., lift=0., shift=0.)
ASSET_FIELDS = ('visual_scene', 'visual_geometry', 'collision_scene', 'collision_geometry')
NUMBER_FIELDS = {
    'hit_points': (1, 30000),
    'shell_damage': (1, 5000),
    'tier': (1, 10),
    'vehicle_id': (1, 255),
    'forward_kmh': (1, 120),
    'reverse_kmh': (1, 80),
    'engine_hp': (1, 5000),
    'reload_seconds': (.05, 300),
    'armor_hull_mm': (0, 500),
    'armor_turret_mm': (0, 500),
    'armor_gun_mm': (0, 500),
}
VEHICLE_CLASSES = ('lightTank', 'mediumTank', 'heavyTank', 'AT-SPG')
MAX_PROJECT_BYTES = 65536
WINDOWS_RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {
    f'{prefix}{number}' for prefix in ('COM', 'LPT') for number in range(1, 10)
}


def asset_path(value):
    """Portable project-relative file name, also safe as a ZIP member."""
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_@. /-]{1,220}', value):
        raise ValueError('Asset path must use letters, digits, spaces, underscores or hyphens')
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or '..' in path.parts or not path.parts:
        raise ValueError(f'Asset path must stay inside the project: {value}')
    if any(part.startswith('.') or part.endswith((' ', '.')) for part in path.parts):
        raise ValueError(f'Invalid portable asset path: {value}')
    if any(part.split('.')[0].upper() in WINDOWS_RESERVED for part in path.parts):
        raise ValueError(f'Invalid portable asset path: {value}')
    return path


def local_asset(root, value):
    path = root.joinpath(*asset_path(value).parts)
    if not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)) or not path.is_file():
        raise ValueError(f'Asset must be a file inside the project: {value}')
    return path


@dataclass
class TankProject:
    schema: int = 1
    id: str = 'author.new-tank'
    name: str = 'New tank'
    base_vehicle: str = 'ussr/R43_T-70_57'
    stock_model: bool = False
    vehicle_id: int = 128
    tier: int = 5
    vehicle_class: str = 'mediumTank'
    hit_points: int = 1200
    shell_damage: int = 160
    forward_kmh: float = 50
    reverse_kmh: float = 20
    engine_hp: int = 500
    reload_seconds: float = 5
    armor_hull_mm: int = 0
    armor_turret_mm: int = 0
    armor_gun_mm: int = 0
    visual_scene: str = ''
    visual_geometry: str = ''
    collision_scene: str = ''
    collision_geometry: str = ''
    running_gear: str = ''
    part_donors: dict = field(default_factory=dict)
    original_points: dict = field(default_factory=dict)
    gear_fit: dict = field(default_factory=FIT_DEFAULTS.copy)
    points: dict = field(default_factory=lambda: {
        'hull': (0., 0., 0.), 'turret': (0., 1.5, 0.), 'gun': (0., 1.8, .8), 'muzzle': (0., 1.8, 3.),
    })
    textures: dict = field(default_factory=dict)

    @property
    def nation(self):
        return self.base_vehicle.split('/')[0]

    @property
    def native_name(self):
        return 'SB_' + self.id.replace('.', '_').replace('-', '_')

    @classmethod
    def decode(cls, raw):
        if len(raw) > MAX_PROJECT_BYTES:
            raise ValueError('Tank project exceeds 64 KiB')
        values = tomllib.loads(raw.decode('utf-8'))
        if type(values.get('schema')) is not int or values['schema'] != 1:
            raise ValueError('Tank project requires schema = 1')
        unknown = values.keys() - {item.name for item in fields(cls)}
        if unknown:
            raise ValueError('Unknown project field: ' + ', '.join(sorted(unknown)))
        project = cls(**values)
        project.validate()
        return project

    @classmethod
    def open(cls, path):
        with Path(path).open('rb') as stream:
            return cls.decode(stream.read(MAX_PROJECT_BYTES + 1))

    def validate(self, *, complete=False):
        if type(self.schema) is not int or self.schema != 1:
            raise ValueError('Tank project requires schema = 1')
        if not isinstance(self.id, str) or not re.fullmatch(
                r'[a-z][a-z0-9-]{0,23}\.[a-z][a-z0-9-]{0,23}', self.id):
            raise ValueError('Tank ID must use author.tank-name in lowercase Latin letters')
        if (not isinstance(self.name, str) or not 1 <= len(self.name) <= 48
                or self.name != self.name.strip() or not self.name.isprintable()):
            raise ValueError('Tank name must contain 1 to 48 printable characters')
        if not isinstance(self.base_vehicle, str) or not re.fullmatch(
                r'[a-z]{2,16}/[A-Za-z0-9_-]{1,80}', self.base_vehicle):
            raise ValueError('Base tank must use nation/vehicle')
        if self.vehicle_class not in VEHICLE_CLASSES:
            raise ValueError('Choose a vehicle class')
        if type(self.stock_model) is not bool:
            raise ValueError('Invalid game model option')
        if self.stock_model and (any(getattr(self, key) for key in ASSET_FIELDS) or self.textures):
            raise ValueError('Import a complete model to use custom model files or textures')
        if not isinstance(self.running_gear, str) or (self.running_gear and not re.fullmatch(
                r'[a-z]{2,16}/[A-Za-z0-9_-]{1,80}', self.running_gear)):
            raise ValueError('Running gear donor must use nation/vehicle')
        if (type(self.part_donors) is not dict or self.part_donors.keys()-set(DONOR_PARTS)
                or any(not isinstance(value, str) or not re.fullmatch(r'[a-z]{2,16}/[A-Za-z0-9_-]{1,80}', value)
                       for value in self.part_donors.values())):
            raise ValueError('Part donors must use nation/vehicle')
        if type(self.gear_fit) is not dict or self.gear_fit.keys() != FIT_DEFAULTS.keys():
            raise ValueError('Missing running gear fit settings')
        for key, value in self.gear_fit.items():
            minimum, maximum = (-5., 5.) if key in ('lift', 'shift') else (.25, 4.)
            if type(value) not in (int, float) or not minimum <= value <= maximum:
                raise ValueError(f'Invalid running gear adjustment: {key}')
        for key, (minimum, maximum) in NUMBER_FIELDS.items():
            value = getattr(self, key)
            types = (int, float) if key in ('forward_kmh', 'reverse_kmh', 'reload_seconds') else (int,)
            if type(value) not in types or not minimum <= value <= maximum:
                raise ValueError(f'{key} must be between {minimum} and {maximum}')
        if type(self.points) is not dict or set(self.points) != set(PARTS):
            raise ValueError('Set the hull, turret, gun and muzzle points')
        if (type(self.original_points) is not dict
                or self.original_points and self.original_points.keys() != set(PARTS)):
            raise ValueError('Invalid original mounting points')
        for part, point in (*self.points.items(), *self.original_points.items()):
            if (type(point) not in (list, tuple) or len(point) != 3
                    or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 50 for v in point)):
                raise ValueError(f'Invalid position for {part}')
        if max(abs(tip-pivot) for tip, pivot in zip(self.points['muzzle'], self.points['gun'])) > 30:
            raise ValueError('Muzzle must be within 30 m of the gun pivot')
        for key in ASSET_FIELDS:
            value = getattr(self, key)
            if value or (complete and not self.stock_model):
                path = asset_path(value)
                suffix = 'sc2' if key.endswith('scene') else 'scg'
                if not path.name.lower().endswith((f'.{suffix}', f'.{suffix}.dvpl')):
                    raise ValueError(f'{key} must be a .{suffix} file')
        if type(self.textures) is not dict or len(self.textures) > 64:
            raise ValueError('A project can use up to 64 textures')
        for name, value in self.textures.items():
            if not asset_path(name).name.endswith('.tex'):
                raise ValueError(f'Invalid material texture name: {name}')
            if not asset_path(value).name.lower().endswith(('.png', '.jpg', '.jpeg', '.dds', '.dds.dvpl')):
                raise ValueError(f'Unsupported texture file: {value}')

    def to_bytes(self):
        self.validate()
        values = asdict(self)
        tables = ('points', 'textures', 'gear_fit', 'part_donors', 'original_points')
        table_values = {key: values.pop(key) for key in tables}
        lines = [f'{key} = {json.dumps(value, ensure_ascii=False)}' for key, value in values.items()]
        for section, entries in table_values.items():
            lines.extend(('', f'[{section}]'))
            lines.extend(f'{json.dumps(key)} = {json.dumps(value, ensure_ascii=False)}'
                         for key, value in sorted(entries.items()))
        return ('\n'.join(lines) + '\n').encode('utf-8')

    def save(self, path):
        path = Path(path)
        atomic_write(path, self.to_bytes())

    def assets(self):
        return {getattr(self, key) for key in ASSET_FIELDS if getattr(self, key)} | set(self.textures.values())
