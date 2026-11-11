"""Guarded controls and speed choices for the pinned native replay HUD.

The UI sends existing replay events. Native support for accelerated playback
and reverse seeking belongs to the controller and must be verified separately.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile

import yaml

from . import dvpl


BATTLE_SCREEN = 'UI/Screens/Battle/BattleScreenReplay.yaml.dvpl'
SPEED_PANEL = 'UI/Screens3/Battle/Replay/PlaybackSpeedPanel.yaml.dvpl'
SPEED_ACTIONS = 'UI/Screens3/Battle/Replay/PlaybackSpeedPanel.actions.dvpl'
RESOURCES = (BATTLE_SCREEN, SPEED_PANEL)
VERSION = 'replay-speeds-v1'
MULTIPLIERS = ((8., '8x'), (4., '4x'), (2., '2x'), (1., '1x'),
               (.5, '1/2x'), (.25, '1/4x'), (.125, '1/8x'), (.0625, '1/16x'))
DEFAULT_INDEX = 3
VISIBLE_CONTROLS = {
    'ButtonLeft': ('ON_REWIND', 'when not isNull(replayRewindKey) -> replayRewindKey.isVisible, false'),
    'ButtonRight': ('ON_FAST_FORWARD', 'when not isNull(replayForwardKey) -> replayForwardKey.isVisible, false'),
    'PlaybackMultiplier': (None, 'when not isNull(replayRewindKey) -> replayRewindKey.isVisible, false'),
}


def _vector(values):
    return '[ ' + ''.join(f'{{ "multiplier" = {value:.6f}; "text" = "{label}"; }}; '
                          for value, label in values) + ']'


STOCK_VECTOR = _vector(MULTIPLIERS[DEFAULT_INDEX:])
SPEED_VECTOR = _vector(MULTIPLIERS)


def _row(rows, kind, name, length):
    if not isinstance(rows, list):
        raise ValueError('Unsupported replay data rows')
    matches = [row for row in rows if isinstance(row, list) and len(row) >= 2
               and row[:2] == [kind, name]]
    if len(matches) != 1 or len(matches[0]) != length:
        raise ValueError(f'Unsupported replay data row: {name}')
    return matches[0]


def _document(game, relative):
    document = yaml.safe_load(dvpl.read(game/'Data'/relative))
    if not isinstance(document, dict) or document.get('Header') != {'version': 125}:
        raise ValueError(f'Unsupported replay resource version: {relative}')
    return document


def patched_resources(game):
    """Build the complete patch from the expected pinned source before writing."""
    from .client_ui import walk_dicts
    game = Path(game)
    screen = _document(game, BATTLE_SCREEN)
    panel = _document(game, SPEED_PANEL)
    try:
        screens = [p for p in screen['Prototypes']
                   if p.get('name') == 'Screen' and p.get('prototype') == 'BattleScreen/Screen']
        if len(screens) != 1:
            raise ValueError('Unsupported replay screen prototype')
        visible_bindings = []
        for name, (event, expression) in VISIBLE_CONTROLS.items():
            controls = [item for item in walk_dicts(screens[0]) if item.get('name') == name]
            if len(controls) != 1:
                raise ValueError(f'Unsupported replay control: {name}')
            control = controls[0]
            if event is not None:
                if control['components']['UIInputEventComponent']['onTouchUpInside'] != event:
                    raise ValueError(f'Unsupported replay control event: {name}')
            else:
                child = control['components']['UIChildComponent0']
                if (child.get('prototypePath') != '~res:/UI/Screens3/Battle/Replay/PlaybackSpeedPanel.yaml'
                        or child.get('args') != {'values': 'multipliers'}):
                    raise ValueError('Unsupported replay speed panel instance')
            bindings = [binding for binding in control.get('bindings', [])
                        if isinstance(binding, list) and binding[:1] == ['visible']]
            if len(bindings) != 1 or bindings[0] != ['visible', expression]:
                raise ValueError(f'Unsupported replay control visibility: {name}')
            visible_bindings.append(bindings[0])
        multipliers = _row(screens[0]['components']['UIDataLocalVarsComponent']['data'],
                           'vector<PlaybackMultiplier>', 'multipliers', 3)
        prototypes = panel['Prototypes']
        if len(prototypes) != 1 or prototypes[0].get('name') != 'Panel':
            raise ValueError('Unsupported replay speed panel prototype')
        components = prototypes[0]['components']
        values = _row(components['UIDataParamsComponent']['params'],
                      'vector<PlaybackMultiplier>', 'values', 4)
        selected = _row(components['UIDataLocalVarsComponent']['data'],
                        'int', 'selectedIndex', 3)
        size = _row(components['UIDataLocalVarsComponent']['data'], 'int', 'size', 3)
        event = components['UIEventOnDataComponent0']
        if (multipliers[2] != STOCK_VECTOR or values[2:] != ['[ ]', STOCK_VECTOR]
                or selected[2] != '0' or size[2] != 'values.Size()'
                or event.get('watchedDataExpression') != 'selectedIndex'
                or event.get('eventName') != 'ON_PLAYBACK_MULTIPLIER_CHANGED'
                or event.get('eventData') != 'values[selectedIndex].multiplier'
                or components['UIActionComponent']['actionsPath'] !=
                '~res:/UI/Screens3/Battle/Replay/PlaybackSpeedPanel.actions'):
            raise ValueError('Unsupported replay speed bindings')
        actions = dvpl.read(game/'Data'/SPEED_ACTIONS).decode('utf-8')
        compact = ''.join(actions.split())
        if ('actionPLAYBACK_SLOWDOWN(){ChangeData(selectedIndex,clamp(selectedIndex+1,0,size-1));}'
                not in compact or
                'actionPLAYBACK_SPEEDUP(){ChangeData(selectedIndex,clamp(selectedIndex-1,0,size-1));}'
                not in compact):
            raise ValueError('Unsupported replay speed actions')
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('Unsupported replay resource structure') from exc
    multipliers[2] = SPEED_VECTOR
    values[3] = SPEED_VECTOR
    selected[2] = str(DEFAULT_INDEX)
    for binding in visible_bindings:
        binding[1] = 'true'
    return {relative: dvpl.encode(yaml.safe_dump(document, sort_keys=False,
                          allow_unicode=True, width=1 << 30).encode('utf-8'))
            for relative, document in ((BATTLE_SCREEN, screen), (SPEED_PANEL, panel))}


def _atomic_write(path, content):
    descriptor, temporary = tempfile.mkstemp(prefix='.replay-ui-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install(lab):
    """Install into one private lab; return whether files or metadata changed."""
    from .client_ui import local_directory
    lab = Path(lab).resolve(strict=True)
    manifest_path = lab/'manifest.json'
    if manifest_path.is_symlink():
        raise ValueError('Replay UI manifest must be a private regular file')
    manifest = json.loads(manifest_path.read_text())
    game = Path(manifest['game']).resolve(strict=True)
    pinned = Path(manifest.get('source_game', game)).resolve(strict=True)
    client = lab/'client'
    if client.is_symlink() or not client.is_dir() or client.is_relative_to(pinned):
        raise ValueError('Replay UI client must be a private directory')
    patches = patched_resources(game)
    overrides = manifest.get('ui_overrides', {})
    if not isinstance(overrides, dict):
        raise ValueError('Unsupported UI override manifest')
    overrides = dict(overrides)
    changed = False
    parent_root = client/'Data'
    local_directory(parent_root, pinned)
    for relative, content in patches.items():
        parent = parent_root
        for component in Path(relative).parts[:-1]:
            parent /= component
            if not parent.exists():
                parent.mkdir()
            local_directory(parent, pinned)
        target = parent/Path(relative).name
        if target.is_symlink() or not target.is_file() or target.read_bytes() != content:
            _atomic_write(target, content)
            changed = True
        overrides[relative] = hashlib.sha256(content).hexdigest()
    settings = {'version': VERSION, 'default_multiplier': 1.,
                'multipliers': [value for value, _ in MULTIPLIERS]}
    if manifest.get('ui_overrides') != overrides or manifest.get('replay_ui') != settings:
        manifest.update(ui_overrides=overrides, replay_ui=settings)
        _atomic_write(manifest_path, (json.dumps(manifest, indent=2)+'\n').encode())
        changed = True
    return changed
