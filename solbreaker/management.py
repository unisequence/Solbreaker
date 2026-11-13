"""Shared commands, settings and owned processes for the desktop and terminal UI."""
from collections import deque
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

from .connection import atomic_write
from .client_atmosphere import PRESETS

PROJECT = Path(__file__).resolve().parents[1]


@dataclass
class Settings:
    game: str = ''
    state: str = str(PROJECT/'data')
    client: str = str(PROJECT/'lab-client')
    proton: str = ''
    steam: str = str(Path.home()/'.local/share/Steam')
    connection: str = ''
    server_url: str = ''
    network: str = ''
    backups: str = str(PROJECT/'backups')
    host: str = '127.0.0.1'
    http_port: int = 18080
    login_port: int = 20016
    distribution: str = 'Ubuntu'
    reuse_native: bool = False
    play_mode: str = 'auto'
    gui_theme: str = 'dark'
    gui_language: str = 'ru'
    remember_login: bool = True
    server_name: str = 'Solbreaker'
    sandbox: bool = False
    map_lighting: str = 'original'

    def __post_init__(self):
        for name, value in vars(self).items():
            if name in ('http_port', 'login_port'):
                if isinstance(value, str) and value.isdecimal():
                    value = int(value)
                    setattr(self, name, value)
                maximum = 65534 if name == 'login_port' else 65535
                if type(value) is not int or not 1024 <= value <= maximum:
                    raise ValueError('Порт должен быть целым числом от 1024 до '+str(maximum))
            elif name == 'gui_theme':
                if value not in ('light', 'dark'):
                    raise ValueError('Выбери светлую или тёмную тему')
            elif name == 'gui_language':
                if value not in ('ru', 'en'):
                    raise ValueError('Выбери русский или английский язык')
            elif name == 'play_mode':
                if value not in ('auto', 'local', 'remote'):
                    raise ValueError('Выбери свой или внешний сервер')
            elif name == 'map_lighting':
                if value not in PRESETS:
                    raise ValueError('Выбери освещение карт')
            elif name == 'server_name':
                from .server_identity import validate_name
                validate_name(value)
            elif name in ('reuse_native', 'remember_login', 'sandbox'):
                if type(value) is not bool:
                    raise ValueError('Неверное значение логической настройки')
            elif not isinstance(value, str) or '\0' in value:
                raise ValueError('Неверное значение настройки '+name)

    @property
    def local_profile_path(self):
        return Path(self.state).expanduser()/'connection.json'

    @property
    def local_connection(self):
        if self.play_mode != 'auto':
            return self.play_mode == 'local'
        return (not self.server_url.strip() and bool(self.connection.strip())
                and Path(self.connection).expanduser().resolve() == self.local_profile_path.resolve())

    @classmethod
    def load(cls, path):
        path = Path(path)
        if path.exists():
            if path.stat().st_size > 65536:
                raise ValueError('Файл настроек слишком большой')
            values = json.loads(path.read_text())
            if isinstance(values, dict):
                values.pop('sunset_intensity', None)
            if not isinstance(values, dict) or set(values)-set(cls.__dataclass_fields__):
                raise ValueError('Неизвестные поля в файле настроек')
            return cls(**values)
        result = cls()
        manifest = Path(result.state)/'manifest.json'
        if manifest.is_file():
            data = json.loads(manifest.read_text())
            result.game = data.get('game', '')
            result.http_port = data.get('http_port', 18080)
            result.login_port = data.get('login_port', 20016)
        result.connection = str(Path(result.state)/'connection.json')
        result.network = str(Path(result.state)/'network.toml')
        return result

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, (json.dumps(vars(self), ensure_ascii=False, indent=2)+'\n').encode())


def required(value, label):
    if not value.strip():
        raise ValueError('Укажи '+label)
    return str(Path(value).expanduser().absolute())


def command(action, settings, *, windows=None, interface=None):
    """Build argv without a shell; both UIs use the exact same operations."""
    windows = sys.platform == 'win32' if windows is None else windows
    python = sys.executable
    if windows and Path(python).name.lower() == 'pythonw.exe':
        python = str(Path(python).with_name('python.exe'))
    state = (required(settings.state, 'папку данных сервера')
             if action in ('prepare-server', 'server', 'network', 'lan', 'server-name', 'export', 'backup') else '')
    client = required(settings.client, 'папку клиента') if action in ('prepare-client', 'play', 'connect') else ''
    if client:
        if settings.local_connection:
            connection = str(Path(required(settings.state, 'папку данных сервера'))/'connection.json')
            server_url = ''
        else:
            connection, server_url = settings.connection.strip(), settings.server_url.strip()
    cli = [python, '-u', '-m', 'solbreaker']
    if action == 'lan':
        address, subnet = interface
        return cli + ['configure-lan', '--state', state, '--address', address, '--subnet', subnet]
    if action == 'server-name':
        return cli + ['configure-server', '--state', state, '--name', settings.server_name]
    if action == 'prepare-server':
        game = required(settings.game, 'папку оригинальной игры')
        if windows:
            args = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                    str(PROJECT/'setup.ps1'), '-Game', game, '-State', state,
                    '-Distribution', settings.distribution, '-HttpPort', str(settings.http_port),
                    '-LoginPort', str(settings.login_port), '-BindAddress', settings.host]
            if Path(state).exists():
                args.append('-Resume')
            if settings.reuse_native:
                args.append('-SkipBuild')
            return args
        args = [python, str(PROJECT/'tools/setup.py'), '--game', game, '--state', state,
                '--host', settings.host, '--http-port', str(settings.http_port),
                '--login-port', str(settings.login_port), '--skip-dependencies']
        if Path(state).exists():
            args.append('--resume')
        if settings.reuse_native:
            args.append('--skip-build')
        return args
    if action == 'prepare-client':
        args = cli + ['prepare-client', '--game', required(settings.game, 'папку оригинальной игры'), '--output', client]
        args += (['--url', server_url] if server_url
                 else ['--connection', required(connection, 'файл подключения')])
        if windows:
            args.append('--windows')
        else:
            args += ['--proton', required(settings.proton, 'исполняемый файл Proton'),
                     '--steam', required(settings.steam, 'папку Steam')]
        return args
    if action == 'server':
        if windows:
            args = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                    str(PROJECT/'start-server.ps1'), '-Distribution', settings.distribution,
                    '-State', state, '-Owned']
            return args + (['-Sandbox'] if settings.sandbox else [])
        args = ['bash', str(PROJECT/'start-server.sh'), '--state', state,
                '--log-file', str(Path(state)/'server.log'), '--stop-on-stdin']
        return args + (['--sandbox'] if settings.sandbox else [])
    if action == 'play':
        args = cli + ['launch-client', '--lab', client]
        args.append('--auto-login' if settings.remember_login else '--no-auto-login')
        args += ['--lighting', settings.map_lighting]
        if server_url:
            args += ['--url', server_url]
        elif connection:
            args += ['--connection', required(connection, 'файл подключения')]
        return args
    if action == 'connect':
        args = cli + ['import-connection', '--lab', client]
        if server_url:
            return args + ['--url', server_url]
        return args + ['--connection', required(connection, 'файл подключения')]
    if action == 'network':
        return cli + ['configure-network', '--state', state,
                      '--config', required(settings.network, 'сетевой TOML-файл')]
    if action == 'export':
        return cli + ['export-connection', '--state', state,
                      '--output', required(settings.connection, 'новый файл подключения')]
    if action == 'backup':
        return cli + ['backup-state', '--state', state,
                      '--output', required(settings.backups, 'папку резервных копий')]
    raise ValueError('Неизвестная операция')


class Job:
    def __init__(self, action, argv, output):
        self.action = action
        self.started = False
        self.returncode = None
        self._stop_requested = False
        self.process = subprocess.Popen(argv, cwd=PROJECT, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8', errors='replace',
            env=dict(os.environ, PYTHONUNBUFFERED='1', PYTHONUTF8='1'), bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        self.thread = threading.Thread(target=self._read, args=(output,), daemon=True)
        self.thread.start()

    def _read(self, output):
        for line in self.process.stdout:
            try:
                if json.loads(line).get('event') == 'started':
                    self.started = True
                    if self._stop_requested:
                        self._send_stop()
            except (ValueError, AttributeError):
                pass
            try:
                output.put_nowait(line.rstrip())
            except queue.Full:
                pass
        self.process.stdout.close()
        self.returncode = self.process.wait()
        if self.process.stdin is not None:
            self.process.stdin.close()

    @property
    def running(self):
        return self.process.poll() is None

    def _send_stop(self):
        try:
            self.process.stdin.write('stop\n')
            self.process.stdin.flush()
            self.process.stdin.close()
        except (OSError, ValueError):
            pass

    def stop(self):
        self._stop_requested = True
        # WSL helper commands also inherit stdin. Wait until the real server
        # owns the pipe so an early stop cannot be consumed by a helper.
        if self.running and self.started:
            self._send_stop()


class Manager:
    def __init__(self, settings):
        self.settings = settings
        self.output = queue.Queue(maxsize=2000)
        self.history = deque(maxlen=2000)
        self.job = None
        self.server = None
        self.client = None
        self.notice = 'Выбери действие'
        self._server_exit_reported = False
        self._play_when_ready = False
        self._map_preparation = None
        self._last_prepared_map = None

    def update_settings(self, settings):
        if settings == self.settings:
            return
        if self.busy and settings != replace(self.settings, gui_theme=settings.gui_theme):
            raise ValueError('Дождись завершения текущей операции')
        server_fields = ('state', 'host', 'http_port', 'login_port', 'distribution', 'sandbox')
        if (self.server is not None and self.server.running
                and any(getattr(settings, key) != getattr(self.settings, key) for key in server_fields)):
            raise ValueError('Останови локальный сервер перед изменением его путей и адресов')
        self.settings = settings

    @property
    def busy(self):
        return self.job is not None and self.job.running

    @property
    def can_prepare_maps(self):
        try:
            capacity = int(os.environ.get('SOLBREAKER_MAP_CACHE_MIB', '1024'))
        except ValueError:
            return False
        return (self.settings.local_connection and (Path(self.settings.state)/'manifest.json').is_file()
                and os.environ.get('SOLBREAKER_MAP_PREWARM', '1') == '1'
                and 0 < capacity <= 65536)

    def map_preparation_status(self, translate=str):
        if not (self.server is not None and self.server.running and self._map_preparation):
            return ''
        detail = self._map_preparation

        def name(key):
            return {'rudniki': 'Mines'}.get(key, key.replace('_', ' ').title())

        if detail.get('cache_unavailable') == 'budget':
            return translate('Достигнут лимит кэша карт.')
        text = {'preparing': 'Подготавливаем карту {map}…',
                'paused': 'Подготовка карт приостановлена.',
                'ready': 'Карта {map} подготовлена.',
                'failed': 'Не удалось подготовить карту {map}. См. журнал.'}.get(detail['phase'], '')
        status = translate(text).format(map=name(detail['map_key']))
        if detail['phase'] == 'preparing' and self._last_prepared_map is not None:
            ready = translate('Карта {map} подготовлена.').format(map=name(self._last_prepared_map))
            return f'{ready} {status}'
        return status

    def run(self, action, *, output=None, interface=None):
        if self.busy:
            raise ValueError('Дождись завершения текущей операции')
        job_settings = replace(self.settings, connection=output) if action == 'export' and output else self.settings
        if action == 'prepare-maps':
            if not self.can_prepare_maps:
                raise ValueError('Подготовка карт доступна для своего сервера с включённым кэшем.')
            status = self.server_status()
            if status in ('Сервер остановлен', 'Ошибка запуска сервера'):
                self.run('server')
                self.notice = 'Сервер запускается для подготовки карт…'
            elif status not in ('Сервер запускается', 'Сервер работает'):
                raise ValueError('Подготовкой карт управляет запущенный сервер.')
            return
        if action == 'server':
            if self.server is not None and self.server.running:
                raise ValueError('Сервер уже запущен из этого окна')
            self.server = Job(action, command(action, self.settings), self.output)
            self._server_exit_reported = False
            self._map_preparation = None
            self._last_prepared_map = None
        elif action == 'play':
            if self.client is not None and self.client.poll() is None:
                raise ValueError('Этот клиент уже запущен')
            lab = Path(required(self.settings.client, 'папку клиента'))
            if not (lab/'manifest.json').is_file() or not (lab/'client/wotblitz.exe').is_file():
                raise ValueError('Сначала подготовь отдельный игровой клиент')
            local = self.settings.local_connection
            if local and self.server_status() in ('Сервер остановлен', 'Ошибка запуска сервера'):
                self.run('server')
                self._play_when_ready = True
                return
            if local and self.server is not None and self.server.running and not self.server.started:
                self._play_when_ready = True
                self.notice = 'После запуска сервера откроется игра'
                return
            self._play_when_ready = False
            private = lab/'private'
            from .platform_io import open_private_file
            descriptor = open_private_file(private, 'launcher-client.log', create=True, writable=True)
            os.lseek(descriptor, 0, os.SEEK_END)
            with os.fdopen(descriptor, 'a') as log:
                self.client = subprocess.Popen(command(action, self.settings), cwd=PROJECT,
                    env=dict(os.environ, PYTHONUTF8='1', PYTHONUNBUFFERED='1'), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            self.notice = 'Клиент запускается. Журнал в private/launcher-client.log'
        else:
            if self.server is not None and self.server.running and action in ('prepare-server', 'network', 'lan', 'server-name', 'backup'):
                raise ValueError('Сначала останови локальный сервер')
            self.job = Job(action, command(action, job_settings, interface=interface), self.output)
        self.notice = {'server': 'Сервер запускается', 'prepare-server': 'Подготовка сервера',
                       'prepare-client': 'Подготовка клиента', 'play': self.notice}.get(action, 'Операция выполняется')

    def stop_server(self):
        self._play_when_ready = False
        if self.server is not None:
            self.server.stop()
            self.notice = 'Сервер завершает работу'

    def drain(self):
        lines = []
        while True:
            try:
                lines.append(self.output.get_nowait())
            except queue.Empty:
                break
        self.history.extend(lines)
        for line in lines:
            if not line.startswith('{'):
                continue
            try:
                detail = json.loads(line)
            except ValueError:
                continue
            phase = {'map_preparation_started': 'preparing', 'map_preparation_paused': 'paused'}
            if detail.get('event') in phase:
                detail['phase'] = phase[detail['event']]
            if (detail.get('event') in (*phase, 'map_preparation')
                    and isinstance(detail.get('map_key'), str) and detail.get('phase')):
                self._map_preparation = detail
                if detail['phase'] == 'ready':
                    self._last_prepared_map = detail['map_key']
        if self.server is not None:
            if self.server.running and self.server.started and self.notice in (
                    'Сервер запускается', 'Сервер запускается для подготовки карт…'):
                self.notice = 'Сервер готов к подключениям'
            if not self.server.running and not self._server_exit_reported:
                self.notice = ('Сервер остановлен' if self.server.process.returncode == 0
                               else f'Сервер завершился с ошибкой {self.server.process.returncode}. Открой журнал')
                self._server_exit_reported = True
        if self.job is not None and self.job.returncode is not None:
            self.notice = ('Готово' if self.job.returncode == 0
                           else f'Операция завершилась с ошибкой {self.job.returncode}. Подробности в журнале')
            self.job = None
        if self.client is not None and (code := self.client.poll()) is not None:
            self.client = None
            if code != 0:
                self.notice = 'Не удалось запустить клиент. См. private/launcher-client.log.'
        if self._play_when_ready and self.server is not None:
            if not self.server.running:
                self._play_when_ready = False
            elif self.server.started and not self.busy:
                self._play_when_ready = False
                try:
                    self.run('play')
                except (OSError, ValueError) as error:
                    self.notice = str(error)
        return lines

    def server_status(self):
        if self.server is not None:
            if self.server.running:
                return 'Сервер работает' if self.server.started else 'Сервер запускается'
        state = Path(self.settings.state)
        if not (state/'manifest.json').is_file():
            return 'Сервер не подготовлен'
        from .platform_io import private_lock, PrivateFileBusy
        try:
            with private_lock(state/'private', 'server.lock'):
                pass
        except PrivateFileBusy:
            return 'Сервер запущен отдельно'
        except (OSError, ValueError):
            return 'Не удалось проверить состояние сервера'
        if self.server is not None and self.server.process.returncode:
            return 'Ошибка запуска сервера'
        return 'Сервер остановлен'

    def close(self):
        if self.busy:
            raise ValueError('Подготовка ещё выполняется. Дождись завершения')
        self.stop_server()
        if self.server is not None and self.server.running:
            try:
                self.server.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                raise ValueError('Сервер ещё завершает работу. Повтори закрытие через несколько секунд') from None


def read_log(state):
    path = Path(state)/'server.log'
    if not path.is_file():
        return 'Журнал сервера появится после первого запуска'
    with path.open('rb') as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell()-65536))
        return stream.read().decode('utf-8', errors='replace')
