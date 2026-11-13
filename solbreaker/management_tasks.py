"""User-requested client launch, profile import and offline state backup."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

from .connection import CONNECTION_PROFILE_MAX_BYTES, atomic_write, connect_client, ensure_lab
from .client_lock import client_configuration_access
from .platform_runtime import require_executable_idle
from .storage import account_access


def launch_client(lab, *, auto_login=None, profile=None, url=None):
    with client_configuration_access(lab):
        lab, manifest = ensure_lab(lab)
        executable = lab/'client/wotblitz.exe'
        require_executable_idle(executable)
        with executable.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        if executable.is_symlink():
            raise ValueError('Клиент отличается от подготовленной копии')
        from . import client_atgm_aim, client_sandbox
        if digest != client_sandbox.OUTPUT_SHA256 and digest not in client_sandbox.PREVIOUS_ENTRIES:
            manifest = client_atgm_aim.upgrade(lab, manifest, digest)
            digest = manifest['client_exe_sha256']
        manifest = client_sandbox.upgrade(lab, manifest, digest)
        if profile is not None or (url and not connection_url_matches(lab, url)):
            _apply_connection(lab, profile, url)
            lab, manifest = ensure_lab(lab)
        from .client_battle_settings import upgrade_form
        manifest = upgrade_form(lab, manifest)
        from .client_ui import install_sandbox_bindings, install_server_name, install_training_maps
        manifest = install_sandbox_bindings(lab, manifest)
        install_training_maps(lab, manifest)
        install_server_name(lab)
        if auto_login is not None:
            from .login_preferences import configure_automatic_login
            configure_automatic_login(manifest['documents'], auto_login)
        from .client_tls import configure_environment
        environment = dict(os.environ, SteamAppId='444200', SteamGameId='444200')
        configure_environment(environment, lab, manifest)
        if sys.platform == 'win32':
            args = [str(executable)]
            cwd = executable.parent
        else:
            args = ['bash', str(lab/'start-client.sh')]
            cwd = lab
        return subprocess.call(args, cwd=cwd, env=environment)


class _ProfileRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if urlsplit(request.full_url).scheme == 'https' and urlsplit(new_url).scheme != 'https':
            response.close()
            raise ValueError('Сервер перенаправил HTTPS-подключение на HTTP')
        return super().redirect_request(request, response, code, message, headers, new_url)


def _parse_profile_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ('', '/', '/tfs/connection/v1')):
        raise ValueError('Укажи HTTP(S)-адрес сервера без пароля и параметров')
    return parsed


def connection_url_matches(lab, url):
    from .network import load_network, origin
    try:
        if not (Path(lab)/'network.toml').is_file():
            return False
        parsed = _parse_profile_url(url)
        requested = origin(f'{parsed.scheme}://{parsed.netloc}', public=True)
        return load_network(lab).advertise_http == requested
    except (OSError, ValueError, KeyError):
        return False


def fetch_profile(url):
    parsed = _parse_profile_url(url)
    address = url.rstrip('/')
    if not parsed.path.rstrip('/'):
        address += '/tfs/connection/v1'
    request = Request(address, headers={'Accept': 'application/json'})
    with build_opener(_ProfileRedirectHandler()).open(request, timeout=8) as response:
        data = response.read(CONNECTION_PROFILE_MAX_BYTES+1)
    if len(data) > CONNECTION_PROFILE_MAX_BYTES:
        raise ValueError('Профиль подключения слишком большой')
    return data


def _apply_connection(lab, profile, url):
    if url:
        profile = lab/'private/import-connection.json'
        atomic_write(profile, fetch_profile(url))
    if profile is None:
        raise ValueError('Выбери файл подключения или укажи адрес сервера')
    return connect_client(lab, profile)


def import_connection(lab, *, profile=None, url=None):
    with client_configuration_access(lab):
        lab, _ = ensure_lab(lab)
        require_executable_idle(lab/'client/wotblitz.exe')
        result = _apply_connection(lab, profile, url)
        from .client_tls import configure_environment
        configure_environment({}, lab, json.loads((lab/'manifest.json').read_text()))
        return result


def backup_state(state, output):
    state = Path(state).resolve(strict=True)
    output = Path(output).expanduser().absolute()
    if output == state or state in output.parents:
        raise ValueError('Папка резервных копий должна находиться вне данных сервера')
    with account_access(state/'private'):
        _, manifest = ensure_lab(state)
        if manifest.get('client_only'):
            raise ValueError('Выбери данные сервера, а не игровой клиент')
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.solbreaker-backup-', dir=output) as temporary:
            work = Path(temporary)
            database = state/'private/accounts.sqlite3'
            snapshot = work/'accounts.sqlite3'
            if database.exists():
                source = sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)
                target = sqlite3.connect(snapshot)
                try:
                    source.backup(target)
                finally:
                    target.close()
                    source.close()
            archive = work/'state.zip'
            excluded = {'server.lock', 'client-config.lock', 'accounts.sqlite3',
                        'accounts.sqlite3-wal', 'accounts.sqlite3-shm'}
            with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
                for root, directories, filenames in os.walk(state, followlinks=False):
                    directories[:] = [name for name in directories if name not in
                                      ('battle-captures', 'rpc-captures', 'diagnostics', 'runtime', '__pycache__')]
                    for name in directories:
                        if (Path(root)/name).is_symlink():
                            raise ValueError('Резервное копирование требует обычные папки данных')
                    for name in filenames:
                        path = Path(root)/name
                        if name in excluded or name.endswith('.log'):
                            continue
                        if not stat.S_ISREG(path.lstat().st_mode) or path.is_symlink():
                            raise ValueError('Резервное копирование требует обычные файлы данных')
                        bundle.write(path, path.relative_to(state))
                if snapshot.exists():
                    bundle.write(snapshot, 'private/accounts.sqlite3')
            archive.chmod(0o600)
            name = datetime.now(timezone.utc).strftime('solbreaker-%Y%m%d-%H%M%S-')+os.urandom(3).hex()+'.zip'
            from .resource_copy import activate_no_replace
            activate_no_replace(archive, output/name)
    return dict(backup=str(output/name))
