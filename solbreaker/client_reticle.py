"""Keep the stock sights and replace only the dispersion artwork."""
from hashlib import sha256
import json
from pathlib import Path

from . import dvpl
from .connection import atomic_write


_AIM_ROOT = 'UI/Screens/Battle/Aims'
_SPRITE_ROOT = 'Gfx/Battle/HUD/aim'
_SUFFIXES = ('', '@2x')
_VERSION = 22
# Earlier versions replaced these stock resources in prepared clients.
_STOCK_RESOURCES = (
    *(f'{_AIM_ROOT}/{name}.yaml.dvpl' for name in
      ('AimDefault', 'Aim1', 'Aim2', 'AimDefault.style', 'Aim.style', 'AimCont.style', 'AimSpread')),
    'UI/Screens/Common/Settings/HUDOptionsPage.yaml.dvpl',
    *(f'{_SPRITE_ROOT}/aim_camera-marker_snipe{suffix}.packed.webp.dvpl' for suffix in _SUFFIXES),
)


def install(lab, manifest):
    if manifest.get('reticle_ui_version') == _VERSION:
        return manifest
    from .client_ui import local_directory

    source = Path(manifest.get('source_game', manifest['game'])).resolve(strict=True)
    data = lab/'client/Data'
    resources = {}
    overrides = dict(manifest.get('ui_overrides', {}))
    if manifest.get('reticle_ui_version'):
        for relative in _STOCK_RESOURCES:
            resources[data/relative] = (source/'Data'/relative).read_bytes()
            overrides.pop(relative, None)

    assets = Path(__file__).with_name('assets')
    for suffix in _SUFFIXES:
        relative = f'{_SPRITE_ROOT}/GunSpread{suffix}.packed.webp.dvpl'
        content = dvpl.encode((assets/f'solbreaker-spread{suffix}.packed.webp').read_bytes())
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
