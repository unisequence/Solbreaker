"""Persistent settings for the local solo training battle.

Kept in its own small JSON file beside the lab's private state, not in
manifest.json, so the hangar can change it without touching lab setup.
Loading is strict: unknown fields, wrong types and out-of-range values are
rejected instead of silently replaced. A missing file yields the defaults.
"""
from dataclasses import asdict, dataclass, fields, replace
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .bot_ai import DEFAULT_DIFFICULTY, DIFFICULTIES, training_bot_requested
from .roster import TEAM_SIZE

SCHEMA_VERSION = 1
FILE_NAME = 'training.json'
MAX_BYTES = 4096
MODES = ('target', 'bots')
BATTLE_MODES = ('encounter', 'elimination', 'supremacy')
# The player occupies one slot of team 1.
MAX_ENEMIES = TEAM_SIZE
MAX_ALLIES = TEAM_SIZE-1
# Keys come from the runtime map catalog; only their shape is checked here.
_MAP_KEY = re.compile(r'[a-z0-9_]{1,64}')


@dataclass(frozen=True)
class TrainingConfig:
    """``target``: stationary targets; ``bots``: driving enemy bots.

    ``allies`` is kept while ``target`` mode is selected so toggling the mode
    in a UI does not lose it, but only ``active_allies`` is spawned.
    ``map`` is a training catalog key; ``None`` selects the server default.
    """
    mode: str = 'target'
    enemies: int = 1
    allies: int = 0
    difficulty: str = DEFAULT_DIFFICULTY
    map: str | None = None
    battle_mode: str = 'encounter'

    def __post_init__(self):
        if type(self.battle_mode) is not str or self.battle_mode not in BATTLE_MODES:
            raise ValueError('Battle mode must be encounter, elimination or supremacy')
        if type(self.mode) is not str or self.mode not in MODES:
            raise ValueError('Training mode must be target or bots')
        if type(self.enemies) is not int or not 1 <= self.enemies <= MAX_ENEMIES:
            raise ValueError(f'Training enemies must be 1 to {MAX_ENEMIES}')
        if type(self.allies) is not int or not 0 <= self.allies <= MAX_ALLIES:
            raise ValueError(f'Training allies must be 0 to {MAX_ALLIES}')
        if type(self.difficulty) is not str or self.difficulty not in DIFFICULTIES:
            raise ValueError('Training difficulty must be one of ' + ', '.join(DIFFICULTIES))
        if self.map is not None and (type(self.map) is not str
                                     or not _MAP_KEY.fullmatch(self.map)):
            raise ValueError('Invalid training map key')

    @property
    def bots_enabled(self):
        return self.mode == 'bots'

    @property
    def active_allies(self):
        return self.allies if self.bots_enabled else 0

    def to_dict(self):
        return {'version': SCHEMA_VERSION, **asdict(self)}

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError('Training config must be a JSON object')
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(data) - known - {'version'})
        if unknown:
            raise ValueError('Unknown training config field: ' + ', '.join(map(str, unknown)))
        version = data.get('version', SCHEMA_VERSION)
        if type(version) is not int or version != SCHEMA_VERSION:
            raise ValueError('Unsupported training config version')
        return cls(**{key: value for key, value in data.items() if key != 'version'})

    def map_id(self, catalog):
        """Resolve ``map`` against the runtime ID -> key catalog
        (``Base.map_catalog``); ``None`` keeps the server default."""
        if self.map is None:
            return None
        matches = [ident for ident, key in catalog.items() if key == self.map]
        if len(matches) != 1:
            raise ValueError(f'Training map {self.map} is not in the map catalog')
        return matches[0]

    def for_game_mode(self, mode):
        """Map the two native picker IDs to TFS solo rules, per request.

        The pinned client already transmits mode 1/7 in command 700. TFS
        labels these cards Training ground / Battle with bots; no ranked
        match or shared server setting is created by selecting either card.
        """
        if type(mode) is not int or mode not in (1, 7):
            raise ValueError('Unsupported TFS solo game mode')
        return replace(self, mode='bots' if mode == 7 else 'target')


def training_config_path(lab):
    return Path(lab)/'private'/FILE_NAME


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate training config field: ' + key)
        result[key] = value
    return result


def _reject_constant(name):
    raise ValueError('Invalid training config number: ' + name)


_MISSING = object()


def _read(path):
    """Return the parsed document, or ``_MISSING`` when the file is absent."""
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    if path.is_symlink():
        raise ValueError('Training config must be a regular file')
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return _MISSING
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError('Training config must be a regular file')
    with os.fdopen(descriptor, 'rb') as stream:
        raw = stream.read(MAX_BYTES+1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Training config is too large')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except UnicodeDecodeError as exc:
        raise ValueError('Training config is not UTF-8') from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f'Training config is not valid JSON: {exc.msg}') from exc


def load_training_config(path):
    document = _read(path)
    return TrainingConfig() if document is _MISSING else TrainingConfig.from_dict(document)


def save_training_config(path, config):
    """Write via a same-directory temporary file and an atomic replace."""
    if not isinstance(config, TrainingConfig):
        raise ValueError('Invalid training config')
    path = Path(path)
    if path.is_symlink() or path.exists() and not path.is_file():
        raise ValueError('Training config must be a regular file')
    data = (json.dumps(config.to_dict(), indent=2, sort_keys=True)+'\n').encode('utf-8')
    descriptor, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', suffix='.tmp',
                                             dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    if os.name == 'posix':
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return config


def effective_training_config(manifest, path, environ=None):
    """Combine the saved config with the legacy ``training_bot`` switches.

    ``SOLBREAKER_TRAINING_BOT`` still forces the mode for one session. The
    manifest flag only applies while no config file has been saved.
    """
    document = _read(path)
    config = TrainingConfig() if document is _MISSING else TrainingConfig.from_dict(document)
    legacy = training_bot_requested(manifest, environ)
    env = os.environ if environ is None else environ
    if env.get('SOLBREAKER_TRAINING_BOT') is not None or document is _MISSING:
        config = replace(config, mode='bots' if legacy else 'target')
    return config


def verify_running_training_config(running, requested):
    """Runner check that a reused server serves the requested settings."""
    try:
        actual = TrainingConfig.from_dict(running.get('training_config'))
    except (TypeError, ValueError):
        actual = None
    if actual != requested:
        raise ValueError('The running server has different or unknown training settings. '
                         'Stop that server and restart the requested session.')
