"""A compact reticle overlay for the prepared Blitz client."""
from hashlib import sha256
import json
from pathlib import Path

import yaml

from . import dvpl
from .connection import atomic_write


_ROOT = 'UI/Screens/Battle/Aims'
_HUD_OPTIONS = 'UI/Screens/Common/Settings/HUDOptionsPage.yaml.dvpl'
_AIMS = ('AimDefault', 'Aim1', 'Aim2')
_VERSION = 5
_INK = [.94, .97, .98, 1.]
_READY = [.62, 1., .72, 1.]
_EDGE = [.025, .035, .045, .94]


def _control(parent, name):
    return next(child for child in parent['children'] if child.get('name') == name)


def _rectangle(name, x, y, width, height, color, *, classes=None):
    control = {'class': 'UIControl', 'name': name, 'input': False,
               'position': [x, y], 'size': [width, height],
               'components': {'Background': {'drawType': 'DRAW_FILL', 'color': list(color)}}}
    if classes:
        control['classes'] = classes
    return control


def _crosshair():
    strokes = ((0, 11, 9, 3), (11, 0, 3, 9), (16, 11, 9, 3),
               (11, 16, 3, 9), (11, 11, 3, 3))
    outlines = [_rectangle(f'Outline{i}', x-1, y-1, w+2, h+2, _EDGE)
                for i, (x, y, w, h) in enumerate(strokes)]
    lines = [_rectangle(f'Stroke{i}', x, y, w, h, _INK, classes='solbreaker-sight-line')
             for i, (x, y, w, h) in enumerate(strokes)]
    return {'class': 'UIControl', 'name': 'SolbreakerCrosshair', 'input': False,
            'visible': False, 'size': [25, 25],
            'components': {'Anchor': {'hCenterAnchorEnabled': True, 'vCenterAnchorEnabled': True}},
            'children': outlines + lines}


def _patch_aim(document):
    sight = _control(document['Prototypes'][0], 'UISightCursor')
    rotate = _control(sight, 'rotateControl')
    # Style and visibility of the native controls still drive scope changes,
    # gun position, autolock and missile state.
    zoom = _control(rotate, 'sightCursorZoom')
    for child in zoom.get('children', ()):
        if child.get('name') == 'Decoration':
            child['components']['UIOpacityComponent'] = {'opacity': 0.}
    sight['children'].append(_crosshair())


def _patch_aim_style(document, *, custom=False):
    rules = document['StyleSheets']
    for rule in rules:
        selectors = rule['selectors']
        properties = rule['properties']
        if 'atlasImage-imagePath' in properties and all(
                selector.startswith('.sight.shell.') for selector in selectors):
            # Hide the stock shell reticle, including its old ring and ears.
            properties.pop('atlasImage-imagePath')
            properties['atlasImage-color'] = [1., 1., 1., 0.]
        if all('.shell' in selector and selector.endswith('Container') for selector in selectors):
            reload = selectors[0].endswith('#reloadAndFuelContainer')
            properties['anchor-hCenterAnchor'] = -56 if reload else (65 if custom else 44)
            properties['anchor-vCenterAnchor'] = 9 if custom and not reload else 16
    rules.extend([
        {'selectors': ['.sight.shell * #SolbreakerCrosshair'], 'properties': {'visible': True}},
        {'selectors': ['.sight.shell.aimed * .solbreaker-sight-line'],
         'properties': {'bg-color': _READY}},
    ])


def _patch_readouts(document):
    for rule in document['StyleSheets']:
        selector, = rule['selectors']
        properties = rule['properties']
        if selector == '.cont.loading ?':
            properties['atlasText-color'] = [1., .76, .38, 1.]
        else:
            properties['atlasText-color'] = [1., 1., 1., .74 if selector == '.cont.over ?' else 1.]
        properties['atlasText-shadowColor'] = list(_EDGE)
        properties['atlasText-shadowOffset'] = [1, 1]
    document['StyleSheets'].append({
        'selectors': ['.cont.loaded #reloadTime'],
        'properties': {'atlasText-color': [1., 1., 1., 0.], 'atlasText-shadowColor': [0., 0., 0., 0.]},
    })


def _hide_spread(document):
    document['Prototypes'][0]['components']['UIOpacityComponent'] = {'opacity': 0.}


def _hide_style_picker(document):
    from .client_ui import walk_dicts

    picker = next(node for node in walk_dicts(document) if node.get('name') == 'CustomAim')
    picker['visible'] = False
    picker['components']['IgnoreLayout'] = {}


def install(lab, manifest):
    if manifest.get('reticle_ui_version') == _VERSION:
        return manifest
    from .client_ui import local_directory

    source = Path(manifest.get('source_game', manifest['game'])).resolve(strict=True)
    data = lab/'client/Data'
    transforms = {
        **{f'{_ROOT}/{name}.yaml.dvpl': _patch_aim for name in _AIMS},
        f'{_ROOT}/AimDefault.style.yaml.dvpl': _patch_aim_style,
        f'{_ROOT}/Aim.style.yaml.dvpl': lambda document: _patch_aim_style(document, custom=True),
        f'{_ROOT}/AimCont.style.yaml.dvpl': _patch_readouts,
        f'{_ROOT}/AimSpread.yaml.dvpl': _hide_spread,
        _HUD_OPTIONS: _hide_style_picker,
    }
    resources = {}
    overrides = dict(manifest.get('ui_overrides', {}))
    for relative, transform in transforms.items():
        document = yaml.safe_load(dvpl.read(source/'Data'/relative))
        transform(document)
        content = dvpl.encode(yaml.safe_dump(document, sort_keys=False, allow_unicode=True).encode())
        resources[data/relative] = content
        overrides[relative] = sha256(content).hexdigest()

    for parent in {target.parent.relative_to(data) for target in resources}:
        directory = data
        for part in parent.parts:
            directory /= part
            local_directory(directory, source)
            if directory.resolve(strict=True) != directory:
                raise ValueError(f'Expected a private client UI directory: {directory}')
    for target, content in resources.items():
        atomic_write(target, content, replace_symlink=True)
    updated = dict(manifest, ui_overrides=overrides, reticle_ui_version=_VERSION)
    atomic_write(lab/'manifest.json', (json.dumps(updated, indent=2)+'\n').encode())
    return updated
