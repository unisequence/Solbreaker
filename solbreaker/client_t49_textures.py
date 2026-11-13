"""Import the original T49 P atlas from a user's Steam depot files."""
import hashlib
from pathlib import Path

from . import dvpl
from .client_branding import replace_texture_names
from .client_lock import client_configuration_access
from .client_ui import local_directory
from .connection import atomic_write, ensure_lab
from .platform_io import read_regular_file
from .platform_runtime import require_executable_idle

DEPOT_MANIFEST = '8449057217499595862'
USA = Path('Data/3d/Tanks/USA')
SCENE = USA/'T49_Rocket.sc2.dvpl'
SCENE_SHA256 = 'caf81f9563f25a0fef3aee0a9048bbb81826ff403414603b9cf0a9b1db3106d4'
TEXTURES = {
    'images/A100_T49.dx11.dds.dvpl': '1c628de8afe1d0abfba0fa96dafb36683f93df6dbc1daced7f974b9f000502d6',
    'images/A100_T49_NM.dx11.dds.dvpl': '86587631a51055fbca9a9808170ba851a5094457276d757d870b680daf2fd5b2',
    'CamouflageMasks/A100_T49_CM.dx11.pvr.dvpl': 'dc5dfc35e04fa1d4dfeb00f8d40fe7e6b602258194f45b6f212f2eae19f83e7a',
}


def _alias(name):
    return name.replace('A100_T49', 'T49P_OLD')


def _read_textures(source):
    textures = {}
    for name, digest in TEXTURES.items():
        relative = USA/name
        candidates = (source/relative, source/Path(*relative.parts[1:]), source/relative.name)
        file = next((candidate for candidate in candidates if candidate.exists()), None)
        if file is None:
            raise ValueError(f'T49 P Steam depot {DEPOT_MANIFEST}: missing {relative.name}')
        content = read_regular_file(file, 4 << 20)
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError(f'T49 P Steam depot {DEPOT_MANIFEST}: SHA-256 mismatch for {relative.name}')
        textures[USA/_alias(name)] = content
    return textures


def _private_parent(client, relative, roots):
    directory = client
    for part in relative.parent.parts:
        directory /= part
        resolved = directory.resolve(strict=True)
        if directory.is_symlink():
            root = next((root for root in roots if resolved.is_relative_to(root)), None)
            if root is None:
                raise ValueError(f'T49 P resource directory is outside the pinned game: {directory}')
            local_directory(directory, root)
        elif resolved != directory or not directory.is_dir():
            raise ValueError(f'T49 P resource directory is not private: {directory}')


def restore(lab, source):
    textures = _read_textures(Path(source).expanduser().resolve(strict=True))
    with client_configuration_access(lab):
        lab, manifest = ensure_lab(lab)
        client = lab/'client'
        require_executable_idle(client/'wotblitz.exe')
        game = Path(manifest['game']).resolve(strict=True)
        original = Path(manifest.get('source_game', manifest['game'])).resolve(strict=True)
        roots = (game, original)
        stock = dvpl.read(original/SCENE)
        if hashlib.sha256(stock).hexdigest() != SCENE_SHA256:
            raise ValueError('Unsupported original T49 P scene')
        names = {name.split('.dx11.', 1)[0]+'.tex' for name in TEXTURES}
        restored = replace_texture_names(stock, {name: _alias(name) for name in sorted(names)}, version=41)
        scene = client/SCENE
        if not any(scene.resolve(strict=True).is_relative_to(root) for root in (client, *roots)):
            raise ValueError('T49 P scene is outside the prepared client and pinned game')
        current = dvpl.read(scene)
        if current not in (stock, restored):
            raise ValueError('Unsupported prepared T49 P scene')

        pending = {}
        for relative, content in textures.items():
            path = client/relative
            if path.is_symlink() or (path.exists() and read_regular_file(path, 4 << 20) != content):
                raise ValueError(f'Conflicting T49 P texture: {path.name}')
            if not path.exists():
                pending[relative] = content
        for relative in (*pending, SCENE):
            _private_parent(client, relative, roots)
        for relative, content in pending.items():
            atomic_write(client/relative, content)
        if current != restored:
            # The aliases must exist before the scene references them.
            atomic_write(scene, dvpl.encode(restored), replace_symlink=True)
    return {'changed': bool(pending or current != restored), 'depot_manifest': DEPOT_MANIFEST}
