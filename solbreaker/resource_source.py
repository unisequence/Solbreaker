"""Validate the resource build used by this headless server."""
import hashlib
from pathlib import Path
import re
import stat
from xml.etree import ElementTree as ET

from .build_info import BUILD_SHA256, VERSION

_VEHICLES = Path('Data/XML/item_defs/vehicles')
_NAME = re.compile(r'[A-Za-z0-9_-]+')


class VehicleCatalogError(ValueError):
    def __init__(self, path, *, unreadable=False):
        message = ('Не удалось прочитать каталог техники. Файл ' if unreadable
                   else 'В выбранной сборке отсутствует файл каталога техники ')
        super().__init__(message + path.as_posix())


def validate_vehicle_catalog(game):
    """Check every listed vehicle definition before copying or starting a server."""
    from lz4.block import LZ4BlockError
    from . import dvpl

    def listing(path):
        try:
            entries = ET.fromstring(dvpl.read(game / path))
        except FileNotFoundError as error:
            raise VehicleCatalogError(path) from error
        except (OSError, ValueError, ET.ParseError, LZ4BlockError) as error:
            raise VehicleCatalogError(path, unreadable=True) from error
        if not len(entries) or any(not _NAME.fullmatch(entry.tag) for entry in entries):
            raise VehicleCatalogError(path, unreadable=True)
        return entries

    for nation in listing(_VEHICLES / 'common/nations.xml.dvpl'):
        country = _VEHICLES / nation.tag
        for vehicle in listing(country / 'list.xml.dvpl'):
            path = country / (vehicle.tag + '.xml.dvpl')
            try:
                info = (game / path).stat()
            except FileNotFoundError as error:
                raise VehicleCatalogError(path) from error
            except OSError as error:
                raise VehicleCatalogError(path, unreadable=True) from error
            if not stat.S_ISREG(info.st_mode) or info.st_size < dvpl.FOOTER.size:
                raise VehicleCatalogError(path, unreadable=True)


def inspect_game(game):
    from . import dvpl

    game = Path(game).expanduser().resolve(strict=True)
    with (game / 'wotblitz.exe').open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    version = dvpl.read(game / 'Data/version.txt.dvpl').decode().strip()
    if digest != BUILD_SHA256 or version != VERSION:
        raise ValueError('Solbreaker requires compatible resource build 11.21.0.1235_7376187')
    validate_vehicle_catalog(game)
    return dict(game=str(game), version=version, exe_sha256=digest)


def validate_snapshot(manifest):
    facts = inspect_game(manifest['game'])
    if (manifest['version'], manifest['exe_sha256']) != (facts['version'], facts['exe_sha256']):
        raise ValueError('Server resource build differs from its manifest')
