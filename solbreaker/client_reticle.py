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
_VERSION = 21
_INK = [.94, .97, .98, 1.]
_READY = [.62, 1., .72, 1.]
_EDGE = [.025, .035, .045, .94]
_RELOADING = [1., .78, .43, 1.]
# The native aim atlas resolves this stock resource slot in both camera modes.
# The prepared copy supplies Solbreaker's own artwork in the slot.
_SPRITE_ROOT = 'Gfx/Battle/HUD/aim'
_SPRITE = 'aim_camera-marker_snipe'
_SPRITE_PATH = f'~res:/{_SPRITE_ROOT}/{_SPRITE}.psd'
_READOUT_WIDTH = 52
_READOUT_HEIGHT = 25
_READOUT_X = 29
_READOUT_Y = 45


def _control(parent, name):
    return next(child for child in parent['children'] if child.get('name') == name)


def _patch_aim(document):
    sight = _control(document['Prototypes'][0], 'UISightCursor')
    rotate = _control(sight, 'rotateControl')
    # Style and visibility of the native controls still drive scope changes,
    # gun position, autolock and missile state.
    zoom = _control(rotate, 'sightCursorZoom')
    for child in zoom.get('children', ()):
        if child.get('name') == 'Decoration':
            child['components']['UIOpacityComponent'] = {'opacity': 0.}
    reload_group = _control(rotate, 'reloadAndFuelContainer')
    reload_container = _control(reload_group, 'reloadTimeContainer')
    distance = _control(rotate, 'distanceContainer')
    width = 2 * _READOUT_X + _READOUT_WIDTH
    # One shared backing keeps the readouts below the aiming area in both
    # views. The original text controls still own their live values and states.
    rotate['children'].insert(rotate['children'].index(reload_group), {
        'class': 'UIControl', 'name': 'solbreakerReadoutBar', 'input': False,
        'size': [width, _READOUT_HEIGHT],
        'components': {
            'Anchor': {'hCenterAnchorEnabled': True, 'vCenterAnchorEnabled': True,
                       'vCenterAnchor': _READOUT_Y},
            'Background': {'drawType': 'DRAW_FILL', 'color': [.035, .052, .060, .74]},
        },
        'children': [{
            'class': 'UIControl', 'name': 'divider', 'input': False, 'size': [1, 13],
            'components': {
                'Anchor': {'hCenterAnchorEnabled': True, 'vCenterAnchorEnabled': True},
                'Background': {'drawType': 'DRAW_FILL', 'color': [.65, .77, .79, .47]},
            },
        }],
    })
    for control in (reload_group, reload_container, distance):
        control['size'] = [width, _READOUT_HEIGHT]
        control['components'].pop('LinearLayout', None)
    for control in (reload_group, distance):
        control['components']['Anchor'] = {'hCenterAnchorEnabled': True,
                                           'vCenterAnchorEnabled': True, 'vCenterAnchor': _READOUT_Y}
    for container in (reload_container, distance):
        _control(container, 'atgm-bg')['components']['UIOpacityComponent'] = {'opacity': 0.}
    reload_left = -_READOUT_X - _READOUT_WIDTH // 2
    distance_left = _READOUT_X - _READOUT_WIDTH // 2
    for container, name, width, align, left in (
            (reload_container, 'reloadTime', _READOUT_WIDTH, 'HCENTER', reload_left),
            (distance, 'distance', 33, 'RIGHT', distance_left),
            (distance, 'm', 15, 'LEFT', distance_left + 35)):
        readout = _control(container, name)['components']
        readout['UIDynamicAtlasTextComponent'].update(align=[align, 'VCENTER'])
        if name != 'm':
            readout['UIDynamicAtlasTextComponent']['fontSize'] = 21.
        readout['Anchor'] = {'leftAnchorEnabled': True, 'leftAnchor': left,
                             'vCenterAnchorEnabled': True}
        readout['SizePolicy'] = {'horizontalPolicy': 'FixedSize', 'horizontalValue': width,
                                 'verticalPolicy': 'FixedSize', 'verticalValue': _READOUT_HEIGHT}
    fuel = _control(reload_group, 'fuelIcon')
    fuel['components']['Anchor'] = {'leftAnchorEnabled': True, 'leftAnchor': -_READOUT_X - 7,
                                   'topAnchorEnabled': True, 'topAnchor': _READOUT_HEIGHT + 3}
    drum = _control(sight, 'DrumReload')['components']['Anchor']
    drum.update(hCenterAnchor=-_READOUT_X, vCenterAnchor=_READOUT_Y + 32)


def _patch_aim_style(document):
    for rule in document['StyleSheets']:
        selectors = rule['selectors']
        properties = rule['properties']
        if 'atlasImage-imagePath' in properties and all(
                selector.startswith(('.sight.shell.', '.sight.atgm', '.sight.default-aim.atgm'))
                for selector in selectors):
            properties['atlasImage-imagePath'] = _SPRITE_PATH
            properties['atlasImage-color'] = [1., 1., 1., 1.]
        if 'atlasImage-frame' in properties and all(
                selector.startswith('.sight.shell.aimed ') for selector in selectors):
            properties['atlasImage-frame'] = 0
        if all(selector.endswith(('#reloadAndFuelContainer', '#distanceContainer'))
               for selector in selectors):
            properties['anchor-hCenterAnchor'] = 0
            properties['anchor-vCenterAnchor'] = _READOUT_Y


def _patch_readouts(document):
    colors = {
        '.cont.loading ?': _RELOADING,
        '.cont.loaded ?': _READY,
        '.cont.over ?': [.74, .81, .83, 1.],
    }
    for rule in document['StyleSheets']:
        selector, = rule['selectors']
        properties = rule['properties']
        properties['atlasText-color'] = list(colors.get(selector, _INK))
        properties['atlasText-shadowColor'] = list(_EDGE)
        properties['atlasText-shadowOffset'] = [1, 1]
        if selector.endswith(' ?'):
            names = ('distance', 'm') if selector == '.cont.rangefinder ?' else ('reloadTime',)
            rule['selectors'] = [selector[:-1] + '#' + name for name in names]


def _show_spread(document):
    # Keep the native controls visible for the continuous ring.
    document['Prototypes'][0]['components'].pop('UIOpacityComponent', None)


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
        f'{_ROOT}/Aim.style.yaml.dvpl': _patch_aim_style,
        f'{_ROOT}/AimCont.style.yaml.dvpl': _patch_readouts,
        f'{_ROOT}/AimSpread.yaml.dvpl': _show_spread,
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

    asset_directory = Path(__file__).with_name('assets')
    for slot, asset in ((_SPRITE, 'solbreaker-reticle'), ('GunSpread', 'solbreaker-spread')):
        for suffix in ('', '@2x'):
            relative = f'{_SPRITE_ROOT}/{slot}{suffix}.packed.webp.dvpl'
            artwork = asset_directory/f'{asset}{suffix}.packed.webp'
            content = dvpl.encode(artwork.read_bytes())
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
