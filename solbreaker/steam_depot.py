"""Locate Steam's pinned download and verify owned resources without modifying them."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys

from .build_info import STEAM_APP, STEAM_DEPOT, STEAM_MANIFEST
from .resource_copy import resource_tree_facts
from .resource_source import inspect_game

DOWNLOAD_COMMAND = f'download_depot {STEAM_APP} {STEAM_DEPOT} {STEAM_MANIFEST}'
CATALOG = Path(__file__).with_name('assets')/'build-11.21.0.1235_7376187-files.json.gz'


def steam_roots(hint=''):
    roots = [Path(hint).expanduser()] if hint else []
    if sys.platform == 'win32':
        import winreg
        for hive, key, value in ((winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
                                 (winreg.HKEY_LOCAL_MACHINE, r'Software\WOW6432Node\Valve\Steam', 'InstallPath')):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    roots.append(Path(winreg.QueryValueEx(handle, value)[0]))
            except OSError:
                pass
        if os.environ.get('PROGRAMFILES(X86)'):
            roots.append(Path(os.environ['PROGRAMFILES(X86)'])/'Steam')
    else:
        roots += [Path.home()/'.steam/root', Path.home()/'.local/share/Steam',
                  Path.home()/'.var/app/com.valvesoftware.Steam/.local/share/Steam']
    return list(dict.fromkeys(path.resolve() for path in roots if path.is_dir()))


def find_depots(hint=''):
    return [path for root in steam_roots(hint)
            if (path := root/'steamapps/content'/f'app_{STEAM_APP}'/f'depot_{STEAM_DEPOT}').is_dir()]


def verify_depot(game, *, progress=None, cancelled=lambda: False, catalog=CATALOG):
    game = Path(game).resolve(strict=True)
    facts = inspect_game(game)
    before = resource_tree_facts(game)
    files = json.loads(gzip.decompress(Path(catalog).read_bytes()))
    for name, size, checksum in files:
        if cancelled():
            raise InterruptedError('Проверка остановлена')
        path = game/name
        if not path.is_file() or path.stat().st_size != size:
            raise ValueError('Загрузка ещё не завершена или файл повреждён. Дождись «Depot download complete» в Steam. Файл '+name)
    total = sum(size for _, size, _ in files)
    completed = 0
    for name, size, checksum in files:
        digest = hashlib.sha256()
        with (game/name).open('rb') as stream:
            while chunk := stream.read(1 << 20):
                if cancelled():
                    raise InterruptedError('Проверка остановлена')
                digest.update(chunk)
                completed += len(chunk)
                if progress:
                    progress(completed, total)
        if digest.hexdigest() != checksum:
            raise ValueError('Хеш файла отличается от эталонной сборки. Дождись завершения загрузки или повтори её в Steam. Файл '+name)
    if resource_tree_facts(game) != before:
        raise ValueError('Файлы изменились во время проверки. Дождись окончания загрузки и повтори проверку')
    return dict(facts, files=len(files), bytes=total)
