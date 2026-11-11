"""Validate the resource build used by this headless server."""
import hashlib
from pathlib import Path

from . import dvpl
from .build_info import BUILD_SHA256, VERSION


def inspect_game(game):
    game = Path(game).expanduser().resolve(strict=True)
    with (game / 'wotblitz.exe').open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    version = dvpl.read(game / 'Data/version.txt.dvpl').decode().strip()
    if digest != BUILD_SHA256 or version != VERSION:
        raise ValueError('Solbreaker requires compatible resource build 11.21.0.1235_7376187')
    return dict(game=str(game), version=version, exe_sha256=digest)


def validate_snapshot(manifest):
    facts = inspect_game(manifest['game'])
    if (manifest['version'], manifest['exe_sha256']) != (facts['version'], facts['exe_sha256']):
        raise ValueError('Server resource build differs from its manifest')
