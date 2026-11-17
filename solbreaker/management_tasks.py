"""User-requested client launch, profile import and offline state backup."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

from .connection import (CONNECTION_PROFILE_MAX_BYTES, apply_client_profile, atomic_write, connect_client,
                         connection_profile, ensure_lab, read_connection_profile, validate_connection_profile)
from .client_lock import client_configuration_access
from .platform_runtime import require_executable_idle
from .storage import account_access


def _forward_replay_arguments(script):
    """Update the previous generated Proton wrapper on first replay launch."""
    previous = b' run "$PWD/wotblitz.exe"\n'
    content = script.read_bytes()
    if content.endswith(previous):
        atomic_write(script, content[:-1] + b' "$@"\n')
        script.chmod(0o755)


def _launch_proton(args, lab, manifest, environment):
    opener = shutil.which('xdg-open')
    documents = manifest.get('documents')
    if opener is None or not documents:
        return subprocess.call(args, cwd=lab, env=environment)
    folder = Path(documents).resolve()/'DAVAProject/replays'
    with tempfile.TemporaryDirectory(prefix='replay-folder-', dir=lab/'private') as private:
        request = Path(private)/'open'
        environment = dict(environment,
            SOLBREAKER_REPLAY_FOLDER='Z:' + str(folder).replace('/', '\\'),
            SOLBREAKER_REPLAY_REQUEST='Z:' + str(request).replace('/', '\\'))
        opened = None
        with subprocess.Popen(args, cwd=lab, env=environment) as client:
            while True:
                try:
                    return client.wait(timeout=.25)
                except subprocess.TimeoutExpired:
                    if request.exists():
                        request.unlink(missing_ok=True)
                        if opened is None or opened.poll() is not None:
                            try:
                                opened = subprocess.Popen([opener, str(folder)], stdin=subprocess.DEVNULL)
                            except OSError as error:
                                print(f'Could not open replay folder: {error}', file=sys.stderr)


def launch_client(lab, *, auto_login=None, profile=None, url=None, lighting=None, replay=None, refresh_lan=False):
    if replay is not None:
        replay = Path(replay).expanduser().resolve()
        if not replay.is_file() or replay.suffix.lower() != '.wotbreplay':
            raise ValueError('Выбери существующий файл .wotbreplay')
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
        if profile is not None and refresh_lan and replay is None:
            selected = read_connection_profile(profile)
            active_profile = refresh_lan_profile(lab, manifest, selected)
            apply_client_profile(lab, manifest, active_profile)
            lab, manifest = ensure_lab(lab)
        elif profile is not None or (url and not connection_url_matches(lab, url)):
            _apply_connection(lab, profile, url)
            lab, manifest = ensure_lab(lab)
        from .client_battle_settings import upgrade_form
        manifest = upgrade_form(lab, manifest)
        from .client_ui import install_sandbox_bindings, install_server_name, install_training_maps
        manifest = install_sandbox_bindings(lab, manifest)
        from .client_reticle import install as install_reticle
        manifest = install_reticle(lab, manifest)
        from .client_time_stop import install as install_time_stop
        install_time_stop(lab)
        install_training_maps(lab, manifest)
        from .client_atmosphere import apply_preset, install_weather_maps
        manifest = install_weather_maps(lab, manifest)
        install_server_name(lab)
        legacy_lighting = os.environ.get('SOLBREAKER_MINES_LIGHTING', 'day') if lighting is None else 'day'
        if legacy_lighting not in ('day', 'evening'):
            raise ValueError('SOLBREAKER_MINES_LIGHTING must be day or evening')
        lighting = lighting or ('evening' if legacy_lighting == 'evening' else 'original')
        if (lighting != 'original' or manifest.get('mines_evening_overlay')
                or manifest.get('map_lighting_overlays')):
            _, manifest = ensure_lab(lab)
            apply_preset(lab, manifest, lighting, mines_only=legacy_lighting == 'evening')
        if manifest.get('client_exe_sha256') == client_sandbox.OUTPUT_SHA256:
            from .client_motion_blur import install_motion_blur
            _, manifest = ensure_lab(lab)
            install_motion_blur(lab, manifest)
        if auto_login is not None:
            from .login_preferences import configure_automatic_login
            configure_automatic_login(manifest['documents'], auto_login)
        from .client_tls import configure_environment
        environment = dict(os.environ, SteamAppId='444200', SteamGameId='444200')
        configure_environment(environment, lab, manifest)
        if sys.platform == 'win32':
            args = [str(executable)]
            if replay is not None:
                args.append(str(replay))
            return subprocess.call(args, cwd=executable.parent, env=environment)
        script = lab/'start-client.sh'
        args = ['bash', str(script)]
        if replay is not None:
            _forward_replay_arguments(script)
        from .client_x11_input import preload
        with preload(environment, lab, executable) as environment:
            if replay is None:
                return _launch_proton(args, lab, manifest, environment)
            # The pinned client's command-line parser can drop Unicode or
            # whitespace in a replay path. Give it an ASCII, unquoted path.
            with tempfile.TemporaryDirectory(prefix='solbreaker-replay-', dir='/tmp') as directory:
                with replay.open('rb') as source:
                    digest = hashlib.file_digest(source, 'sha256').hexdigest()
                    source.seek(0)
                    staged = Path(directory)/f'{digest[:20]}.wotbreplay'
                    with staged.open('wb') as output:
                        shutil.copyfileobj(source, output)
                return _launch_proton([*args, 'Z:' + str(staged).replace('/', '\\')],
                                      lab, manifest, environment)


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


def refresh_lan_profile(lab, manifest, selected):
    from . import discovery

    def endpoint(profile):
        network, _, _, _ = validate_connection_profile(profile, manifest)
        http = urlsplit(network.advertise_http)
        if (network.version == 1 and http.scheme == 'http' and discovery.discoverable_ip(http.hostname)
                and http.hostname == network.advertise_login.host == network.advertise_base.host):
            return http.hostname, http.port
        return None

    selected_endpoint = endpoint(selected)
    if selected_endpoint is None:
        return selected
    identity = selected['server_id']
    candidates = {selected_endpoint: selected}
    if manifest.get('server_id') == identity:
        active = connection_profile(lab)
        if (active_endpoint := endpoint(active)) is not None:
            candidates[active_endpoint] = active
    offline = None
    for address, profile in reversed(candidates.items()):
        server = discovery.fetch_server(*address)
        if server is not None and server.server_id == identity:
            return json.loads(server.profile)
        if server is None and offline is None:
            offline = profile
    interfaces = [interface for _, interface, _ in discovery.local_interfaces()]
    for server in discovery.discover(interfaces):
        if server.server_id == identity:
            return json.loads(server.profile)
    if offline is not None:
        return offline
    raise ValueError('По сохранённому адресу отвечает другой сервер Solbreaker')


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
