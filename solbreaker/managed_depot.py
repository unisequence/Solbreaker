"""Adopt a verified launcher download and retire its temporary directory."""
import gzip
import json
from pathlib import Path
import re
import shutil
import tempfile

from .build_info import STEAM_MANIFEST
from .connection import atomic_write, ensure_lab
from .resource_copy import activate_no_replace, resource_tree_facts, copy_resource_tree
from .resource_source import inspect_game, validate_snapshot
from .steam_depot import CATALOG, DEPOT_FOLDER

METADATA = '.DepotDownloader'
RECEIPT = 'verified-depot.json'
MARKER = '.solbreaker-managed-depot.json'


def is_download(path):
    return path.name == DEPOT_FOLDER and (path/METADATA/'depot.config').is_file()


def record_download(result, receipt):
    if not is_download(Path(result['game'])) or not result.get('tree_fingerprint'):
        return
    receipt.parent.mkdir(parents=True, exist_ok=True)
    record = {'source': result['game'], 'fingerprint': result['tree_fingerprint']}
    atomic_write(receipt, (json.dumps(record)+'\n').encode())


def read_record(receipt):
    if not receipt.is_file() or receipt.is_symlink() or receipt.stat().st_size > 16384:
        return None
    try:
        record = json.loads(receipt.read_text(encoding='utf-8'))
        source = Path(record['source'])
        fingerprint = record['fingerprint']
        if source.is_absolute() and source.name == DEPOT_FOLDER and re.fullmatch(r'[a-f0-9]{64}', fingerprint):
            return record
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def location(record, client, *, windows):
    source = Path(record['source'])
    parent = client.expanduser().resolve().parent if windows else source.parent
    return parent/'.solbreaker-resources'/f'{STEAM_MANIFEST}-{record["fingerprint"][:16]}'


def _marker(path):
    item = path/MARKER
    if not item.is_file() or item.is_symlink() or item.stat().st_size > 16384:
        return None
    try:
        return json.loads(item.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def is_managed_source(path):
    marker = _marker(path)
    return (path.parent.name == '.solbreaker-resources' and isinstance(marker, dict)
            and re.fullmatch(r'[a-f0-9]{64}', str(marker.get('fingerprint', ''))) is not None
            and path.name == f'{STEAM_MANIFEST}-{marker["fingerprint"][:16]}')


def _tree(path):
    return resource_tree_facts(path, include_change_time=False)['fingerprint']


def _only_depot_files(path, *, complete):
    names = {name for name, _, _ in json.loads(gzip.decompress(CATALOG.read_bytes()))}
    actual = {file.relative_to(path).as_posix() for file in path.rglob('*')
              if file.is_file() and file.relative_to(path).parts[0] != METADATA}
    if actual-names or (complete and actual != names):
        raise ValueError('В папке депота есть лишние или отсутствуют игровые файлы.')


def stage(record, destination):
    source = Path(record['source'])
    if destination.resolve() != destination or source.resolve(strict=True) != source:
        raise ValueError('Папки депота и хранилища должны находиться на диске без ссылок.')
    if destination.exists():
        if _marker(destination) != record:
            raise ValueError('Папка ресурсов уже занята.')
        inspect_game(destination)
        return
    if not is_download(source) or _tree(source) != record['fingerprint']:
        raise ValueError('Депот изменился. Проверь скачанные файлы ещё раз.')
    _only_depot_files(source, complete=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.depot-', dir=destination.parent) as scratch:
        built = Path(scratch)/'resources'
        copy_resource_tree(source, built, share=lambda _: True, ignore=(METADATA,))
        if _tree(source) != record['fingerprint']:
            raise ValueError('Депот изменился во время подготовки ресурсов.')
        inspect_game(built)
        atomic_write(built/MARKER, (json.dumps(record)+'\n').encode())
        activate_no_replace(built, destination)


def cleanup(record, destination, client, state=None):
    source = Path(record['source'])
    if destination.resolve(strict=True) != destination or _marker(destination) != record:
        raise ValueError('Хранилище ресурсов не найдено.')
    facts = inspect_game(destination)
    _, recorded_client = ensure_lab(client)
    validate_snapshot(recorded_client)
    if recorded_client['game'] != facts['game']:
        raise ValueError('Клиент ещё использует исходную папку депота.')
    if state is not None:
        recorded_server = json.loads((state/'manifest.json').read_text(encoding='utf-8'))
        validate_snapshot(recorded_server)
        if recorded_server['game'] != facts['game']:
            raise ValueError('Сервер ещё использует исходную папку депота.')
    retired = source.with_name(f'.solbreaker-delete-{record["fingerprint"][:16]}')
    if source.exists():
        if source.resolve(strict=True) != source or _tree(source) != record['fingerprint']:
            raise ValueError('Депот изменился после установки. Папка оставлена на месте.')
        activate_no_replace(source, retired)
    if retired.exists():
        if retired.resolve(strict=True) != retired:
            raise ValueError('Ожидалась обычная папка для удаления депота.')
        _tree(retired)
        _only_depot_files(retired, complete=False)
        shutil.rmtree(retired)
