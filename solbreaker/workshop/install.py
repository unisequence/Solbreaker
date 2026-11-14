"""Install a package in an idle prepared client or an offline server state."""
from pathlib import Path
from dataclasses import replace
import hashlib
import json
import os
import shutil
import stat
import tempfile
import xml.etree.ElementTree as ET

from ..armor import ArmorModel, load_battle_vehicle
from ..client_lock import client_configuration_access
from ..client_ui import local_directory
from ..connection import atomic_write, ensure_lab, export_connection
from ..platform_runtime import require_executable_idle
from ..resource_copy import activate_no_replace
from ..resource_source import inspect_game
from ..storage import account_access
from .package import XML, export, overlay_changes, unpack


def linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def current_manifest(state):
    path = state/'manifest.json'
    if linked(path) or path.stat().st_nlink != 1:
        raise ValueError('Expected a private prepared manifest')
    return json.loads(path.read_text(encoding='utf-8'))


def install(package, *, client=None, state=None):
    if (client is None) == (state is None):
        raise ValueError('Choose either a prepared client or a server state')
    with unpack(package) as (project, root):
        if state is not None:
            lab = Path(state).resolve(strict=True)
            with account_access(lab/'private'):
                manifest = current_manifest(lab)
                if manifest.get('client_only'):
                    raise ValueError('Expected a server state')
                definition = install_state(lab, manifest, project, root)
        else:
            with client_configuration_access(client):
                root_lab = Path(client).expanduser().resolve(strict=True)
                require_executable_idle(root_lab/'client/wotblitz.exe')
                recover_client_data(root_lab)
                lab, manifest = ensure_lab(client)
                definition = install_client(lab, manifest, project, root)
    return dict(tank=project.id, compact_id=(definition.ident<<8)|(definition.nation_id<<4)|1,
                hit_points=definition.health, shell_damage=definition.weapon.shells[0].damage,
                path=str(lab))


def install_test_vehicle(settings, project, root):
    """Prepare this project in the launcher profiles before starting their processes."""
    from .. import dvpl
    if os.name == 'nt':
        raise ValueError('Для установки танка на сервер Workshop нужен Linux.')
    state, client = (Path(value).expanduser().resolve(strict=True)
                     for value in (settings.state, settings.client))
    with account_access(state/'private'), client_configuration_access(client):
        _, server_manifest = ensure_lab(state)
        if server_manifest.get('client_only') or not server_manifest.get('vehicle_progression', True):
            raise ValueError('Для тестового заезда нужен свой сервер с ангаром.')
        require_executable_idle(client/'client/wotblitz.exe')
        recover_client_data(client)
        _, client_manifest = ensure_lab(client)
        if not (state/'connection.json').is_file():
            export_connection(state, state/'connection.json')
        relative = Path('Data')/XML/project.nation/'list.xml.dvpl'
        original = source_game(server_manifest)
        locations = (original, Path(server_manifest['game']), client/'client')
        catalogs = [ET.fromstring(dvpl.read(location/relative)) for location in locations]
        used = {int(row.findtext('id')) for catalog in catalogs for row in catalog if row.tag != project.native_name}
        installed = {int(row.findtext('id')) for catalog in catalogs
                     if (row := catalog.find(project.native_name)) is not None}
        if len(installed) > 1 or installed & used:
            raise ValueError('На сервере и клиенте конфликтует ID этого танка.')
        available = next((ident for ident in range(128, 256) if ident not in used), None)
        ident = next(iter(installed)) if installed else (
            project.vehicle_id if project.vehicle_id not in used else available)
        if ident is None:
            raise ValueError('Нет свободного ID для танка этой нации.')
        project = replace(project, vehicle_id=ident)
        with tempfile.TemporaryDirectory(prefix='solbreaker-workshop-drive-') as temporary:
            archive = Path(temporary)/'test.tank.zip'
            export(project, root, archive)
            with unpack(archive) as (candidate, files):
                original_manifest = server_manifest.copy()
                definition = install_state(state, server_manifest, candidate, files, keep_previous=True)
                try:
                    install_client(client, client_manifest, candidate, files)
                except BaseException:
                    atomic_write(state/'manifest.json', (json.dumps(original_manifest, indent=2)+'\n').encode())
                    shutil.rmtree(server_manifest['game'])
                    raise
                previous = Path(original_manifest['game'])
                if previous != original:
                    shutil.rmtree(previous, ignore_errors=True)
    return project, (definition.ident<<8)|(definition.nation_id<<4)|1


def source_game(manifest):
    source = Path(manifest.get('source_game', manifest['game'])).resolve(strict=True)
    facts = inspect_game(source)
    if (facts['version'], facts['exe_sha256']) != (manifest['version'], manifest['exe_sha256']):
        raise ValueError('Tank package source differs from the prepared build')
    return source


def source_snapshot(source, manifest):
    """Let older launchers recognize the private view of a pinned Steam snapshot."""
    snapshot = source.parent/'snapshot.json'
    if source.name != 'game' or not snapshot.is_file() or linked(snapshot):
        return {}
    with snapshot.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        return {}
    try:
        document = json.loads(raw)
        version = document['version']
        executable = document['game']['exe_sha256']
    except (ValueError, TypeError, KeyError):
        return {}
    if (version, executable) != (manifest['version'], manifest['exe_sha256']):
        return {}
    return dict(source_snapshot=str(snapshot), source_snapshot_sha256=hashlib.sha256(raw).hexdigest())


def link_game_view(source, destination):
    destination.mkdir()
    data = destination/'Data'
    data.mkdir()
    for item in (source/'Data').iterdir():
        (data/item.name).symlink_to(item.resolve(strict=True), target_is_directory=item.is_dir())
    (destination/'wotblitz.exe').symlink_to(source/'wotblitz.exe')


def put_changes(data, source, changes):
    for relative, payload in changes.items():
        parent = data
        for name in relative.parts[:-1]:
            parent /= name
            if parent.is_symlink():
                local_directory(parent, source)
            else:
                parent.mkdir(exist_ok=True)
            if linked(parent) or parent.resolve(strict=True) != parent:
                raise ValueError(f'Expected a private resource directory: {parent.name}')
        atomic_write(data/relative, payload, replace_symlink=True)


def validate_definition(view, project):
    from ..tech_tree import lookup_vehicle
    definition = load_battle_vehicle(view, project.nation, project.native_name)
    if (definition.ident, definition.health) != (project.vehicle_id, project.hit_points):
        raise ValueError('Compiled tank does not match its project ID or HP')
    research = lookup_vehicle(view, project.nation, project.native_name)
    shell = next((shell for shell in definition.weapon.shells if shell.tag == research.stock.shell), None)
    if shell is None or shell.damage != project.shell_damage:
        raise ValueError('Compiled gun does not match the project ammunition')
    ArmorModel(view, definition)
    return definition


CLIENT_DATA_BACKUP = 'Data.before-workshop'


def recover_client_data(lab):
    """Recover the directory switch if the installer exited between renames."""
    client = lab/'client'
    backup = client/CLIENT_DATA_BACKUP
    if not os.path.lexists(backup):
        return
    data = client/'Data'
    if linked(client) or linked(backup) or not backup.is_dir():
        raise ValueError('Invalid Workshop client backup directory')
    if not os.path.lexists(data):
        backup.rename(data)
    elif linked(data) or not data.is_dir():
        raise ValueError('Invalid Workshop client Data directory')
    else:
        shutil.rmtree(backup)


def install_state(lab, manifest, project, root, *, keep_previous=False):
    source = source_game(manifest)
    current = Path(manifest['game']).resolve(strict=True)
    if current != source and (current.parent != lab/'tank-views'
                              or not current.name.startswith('view-')
                              or current.name != manifest.get('tank_workshop_view')):
        raise ValueError('This state has a different kind of game resource overlay')
    if os.name == 'nt':
        raise ValueError('Server game views currently require Linux directory symlinks')
    views = lab/'tank-views'
    views.mkdir(exist_ok=True)
    if linked(views):
        raise ValueError('Expected a private tank-views directory')
    provenance = source_snapshot(source, manifest)
    changes = overlay_changes(source, current/'Data', project, root)
    with tempfile.TemporaryDirectory(prefix='.build-', dir=views) as temporary:
        stage = Path(temporary)/'view'
        if current == source:
            link_game_view(source, stage)
        else:
            shutil.copytree(current, stage, symlinks=True)
        put_changes(stage/'Data', source, changes)
        definition = validate_definition(stage, project)
        version = tempfile.mkdtemp(prefix='view-', dir=views)
        destination = Path(version)
        destination.rmdir()
        activate_no_replace(stage, destination)
        manifest.update(game=str(destination), source_game=str(source), tank_workshop_view=destination.name)
        manifest.update(provenance)
        try:
            atomic_write(lab/'manifest.json', (json.dumps(manifest, indent=2)+'\n').encode())
        except BaseException:
            shutil.rmtree(destination)
            raise
    if current != source and not keep_previous:
        shutil.rmtree(current)
    return definition


def install_client(lab, manifest, project, root):
    source = source_game(manifest)
    data = lab/'client/Data'
    changes = overlay_changes(source, data, project, root, client_labels=True)
    with tempfile.TemporaryDirectory(prefix='.tank-build-', dir=lab) as temporary:
        root = Path(temporary)
        view = root/'view'
        view.mkdir()
        stage = view/'Data'
        shutil.copytree(data, stage, symlinks=True)
        put_changes(stage, source, changes)
        definition = validate_definition(view, project)
        backup = data.with_name(CLIENT_DATA_BACKUP)
        data.rename(backup)
        try:
            stage.rename(data)
        except BaseException:
            backup.rename(data)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    return definition
