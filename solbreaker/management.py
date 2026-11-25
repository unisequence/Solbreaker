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

from .benchmark_report import html_output
from .connection import atomic_write
from .client_atmosphere import PRESETS

PROJECT = Path(__file__).resolve().parents[1]


# Match the error text emitted by Python, the launch scripts and the physics worker.
DIAGNOSTIC_MESSAGES = (
    (('certificate_verify_failed', 'unable to get local issuer certificate'),
     'Не удалось проверить сертификат сервера. Проверь дату и время, обнови корневые сертификаты системы и повтори попытку.'),
    (('no space left on device', 'disk full', 'not enough space on the disk', 'plus 1 gib of free space',
      '[errno 28]', '[winerror 112]', 'недостаточно места на диске'),
     'На диске не хватает свободного места. Освободи место на диске установки и повтори попытку.'),
    (('address already in use', '[winerror 10048]', 'only one usage of each socket address'),
     'Порт сервера уже занят. Закрой другой экземпляр сервера или поменяй порты в параметрах.'),
    (('proving ground requires a private solbreaker installation', 'client directory must be separate from game resources',
      'the lab must be separate from game resources', 'lab client directories must not alias',
      'an internal game view requires its pinned external source snapshot'),
     'Пути к игре и клиенту Solbreaker конфликтуют. Укажи исходный Blitz и отдельную папку клиента Solbreaker в параметрах.'),
    (('supports paths shorter than 260', '[winerror 206]'),
     'Путь к клиенту слишком длинный. Выбери папку ближе к корню диска и повтори подготовку.'),
    (('createcollisionshape failed', 'createcollisionshapearray failed',
      'in any directory in urdf path'),
     'Физика карты не загрузилась. На Windows попробуй перенести Solbreaker и папку данных по пути без кириллицы, например C:/Games/Solbreaker.'),
    (('requires compatible resource build', 'server resource build differs', 'game resources changed during client preparation',
      'incompatible executable', 'requires a different pinned client build'),
     'Выбрана несовместимая сборка Blitz. Укажи папку поддерживаемой версии игры или скачай её через лаунчер.'),
    (('update download failed sha-256', 'invalid sha-256 for', 'zip files differ from the release checksum',
      'incomplete update package', 'update download exceeds the published size'),
     'Файлы обновления неполные или повреждены. Скачай обновление ещё раз.'),
    (('[winerror 32]', 'being used by another process'),
     'Не удалось заменить файл. Закрой игру и другие экземпляры Solbreaker перед повторной попыткой.'),
    (('update conflicts with a local file', 'update destination uses a symbolic link or junction'),
     'В папке установки есть файл или ссылка, мешающие обновлению. Распакуй новую версию в отдельную папку и укажи прежние пути к клиенту и данным в параметрах.'),
    (('permissionerror:', '[errno 13]', '[winerror 5]', 'permission denied', '800a0046', 'access is denied'),
     'Системе не удалось открыть или записать файл. Проверь права на папку Solbreaker и повтори попытку.'),
    (('temporary failure in name resolution', 'getaddrinfo failed', 'network is unreachable',
      'connection timed out', 'connection reset by peer', 'connection refused', 'the read operation timed out'),
     'Не удалось подключиться по сети. Проверь подключение и адрес сервера, затем повтори попытку.'),
    (('requires --proton and --steam', 'provide an installed proton executable and steam directory'),
     'Не найдены Steam или Proton. Проверь пути к Steam и Proton в параметрах.'),
    (('expected a separate prepared lab client directory', 'клиент отличается от подготовленной копии'),
     'Подготовленный клиент отсутствует или был изменён. Проверь путь к клиенту или подготовь новую копию через лаунчер.'),
    (('no subscription', 'does not own app', 'missing license', 'accessdenied'),
     'Steam не дал доступ к загрузке. Войди в аккаунт, на котором доступен Blitz, и повтори попытку.'),
    (('choose an absent client directory', 'client directory already exists'),
     'Папка клиента уже существует. Выбери новый путь для подготовки клиента.'),
)


def friendly_error(detail):
    detail = str(detail).casefold()
    for markers, message in DIAGNOSTIC_MESSAGES:
        if any(marker in detail for marker in markers):
            return message
    return ''


def log_tail(path, *, start=0):
    try:
        with Path(path).open('rb') as stream:
            bom = stream.read(2)
            end = stream.seek(0, os.SEEK_END)
            offset = min(end, max(start, end - 32768))
            if bom in (b'\xff\xfe', b'\xfe\xff'):
                stream.seek(max(2, offset + offset % 2))
                return stream.read().decode('utf-16-le' if bom == b'\xff\xfe' else 'utf-16-be', 'replace')
            stream.seek(offset)
            return stream.read().decode('utf-8-sig', 'replace')
    except OSError:
        return ''


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
                maximum = 65533 if name == 'login_port' else 65535
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


def command(action, settings, *, windows=None, interface=None, replay=None, depot=None, benchmark=None):
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
    if action == 'benchmark':
        output, options = benchmark
        game = required(settings.game, 'папку оригинальной игры')
        if not (Path(game)/'wotblitz.exe').is_file():
            raise ValueError('Укажи папку совместимой сборки игры в параметрах.')
        output = Path(output).resolve()
        html = html_output(output)
        if (not output.parent.is_dir() or output.exists() or html.exists() or html.is_symlink()):
            raise ValueError('Выбери новое имя для отчётов HTML и JSON.')
        args = cli + ['benchmark', '--game', game, '--output', str(output)]
        for name in ('maps', 'scenarios', 'arenas', 'tanks', 'seconds', 'warmup', 'repeat',
                     'engine', 'input-pattern', 'vehicle', 'seed'):
            if name in options:
                args.extend((f'--{name}', str(options[name])))
        if options.get('cold'):
            args.append('--cold')
        return args
    if action in ('stage-depot', 'cleanup-depot'):
        receipt, resources = depot
        phase = 'stage' if action == 'stage-depot' else 'cleanup'
        args = cli + ['manage-depot', phase, '--receipt', str(receipt), '--resources', str(resources),
                      '--client', required(settings.client, 'папку клиента')]
        if action == 'cleanup-depot' and settings.local_connection:
            args += ['--state', required(settings.state, 'папку данных сервера')]
        return args
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
        if replay is not None:
            args += ['--replay', str(replay)]
        args.append('--auto-login' if settings.remember_login else '--no-auto-login')
        args += ['--lighting', settings.map_lighting]
        if server_url:
            args += ['--url', server_url]
        elif connection:
            args += ['--connection', required(connection, 'файл подключения')]
            if not settings.local_connection and replay is None:
                args.append('--refresh-lan')
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
        self.resource_error = ''
        self.failure_notice = ''
        self._stop_requested = False
        self.process = subprocess.Popen(argv, cwd=PROJECT, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8', errors='replace',
            env=dict(os.environ, PYTHONUNBUFFERED='1', PYTHONUTF8='1'), bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        self.thread = threading.Thread(target=self._read, args=(output,), daemon=True)
        self.thread.start()

    def _read(self, output):
        for line in self.process.stdout:
            self.failure_notice = friendly_error(line) or self.failure_notice
            try:
                event = json.loads(line)
                if event.get('event') == 'started':
                    self.started = True
                    if self._stop_requested:
                        self._send_stop()
                elif event.get('event') == 'vehicle_catalog_error' and isinstance(event.get('message'), str):
                    self.resource_error = event['message']
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
        self.benchmark_report = None
        self.server = None
        self.client = None
        self._client_log = None
        self.notice = 'Выбери действие'
        self._server_exit_reported = False
        self._pending_play = None
        self._map_preparation = None
        self._last_prepared_map = None
        self._battle_failure = ''

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

    def run(self, action, *, output=None, interface=None, replay=None, depot=None, benchmark=None):
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
            self._battle_failure = ''
        elif action == 'play':
            if self.client is not None and self.client.poll() is None:
                raise ValueError('Этот клиент уже запущен')
            lab = Path(required(self.settings.client, 'папку клиента'))
            if not (lab/'manifest.json').is_file() or not (lab/'client/wotblitz.exe').is_file():
                raise ValueError('Сначала подготовь отдельный игровой клиент')
            local = self.settings.local_connection
            if local and self.server_status() in ('Сервер остановлен', 'Ошибка запуска сервера'):
                self.run('server')
                self._pending_play = {'replay': replay}
                return
            if local and self.server is not None and self.server.running and not self.server.started:
                self._pending_play = {'replay': replay}
                self.notice = 'После запуска сервера откроется игра'
                return
            self._pending_play = None
            private = lab/'private'
            from .platform_io import open_private_file
            descriptor = open_private_file(private, 'launcher-client.log', create=True, writable=True)
            self._client_log = (private/'launcher-client.log', os.lseek(descriptor, 0, os.SEEK_END))
            with os.fdopen(descriptor, 'a') as log:
                self.client = subprocess.Popen(command(action, self.settings, replay=replay), cwd=PROJECT,
                    env=dict(os.environ, PYTHONUTF8='1', PYTHONUNBUFFERED='1'), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            self.notice = 'Клиент запускается. Журнал в private/launcher-client.log'
        else:
            if self.server is not None and self.server.running and action in ('prepare-server', 'network', 'lan', 'server-name', 'backup'):
                raise ValueError('Сначала останови локальный сервер')
            self.job = Job(action, command(action, job_settings, interface=interface, depot=depot,
                                           benchmark=benchmark), self.output)
            if action == 'benchmark':
                self.benchmark_report = html_output(Path(benchmark[0]).resolve())
        self.notice = {'server': 'Сервер запускается', 'prepare-server': 'Подготовка сервера',
                       'prepare-client': 'Подготовка клиента', 'play': self.notice,
                       'benchmark': 'Бенчмарк выполняется'}.get(action, 'Операция выполняется')

    def stop_server(self):
        self._pending_play = None
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
            if not isinstance(detail, dict):
                continue
            if detail.get('event') == 'arena_owner_start_failed':
                self._battle_failure = (friendly_error(detail.get('reason', '')) or
                                        'Бой не запустился. Попробуй другую карту. Если ошибка повторится, открой журнал.')
                self.notice = self._battle_failure
            elif detail.get('event') == 'arena_world_ready' and self._battle_failure:
                if self.notice == self._battle_failure:
                    self.notice = 'Готово'
                self._battle_failure = ''
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
            if not self.server.running and self.server.returncode is not None and not self._server_exit_reported:
                self.notice = ('Сервер остановлен' if self.server.returncode == 0
                               else self.server.resource_error or self.server.failure_notice or
                               f'Сервер завершился с ошибкой {self.server.returncode}. Открой журнал')
                self._server_exit_reported = True
        if self.job is not None and self.job.returncode is not None:
            success = 'Отчёты HTML и JSON сохранены. Пути указаны в журнале.' if self.job.action == 'benchmark' else 'Готово'
            self.notice = (success if self.job.returncode == 0
                           else self.job.resource_error or self.job.failure_notice or
                           f'Операция завершилась с ошибкой {self.job.returncode}. Подробности в журнале')
            self.job = None
        if self.client is not None and (code := self.client.poll()) is not None:
            self.client = None
            if code != 0:
                detail = ''
                if self._client_log is not None:
                    path, start = self._client_log
                    detail = log_tail(path, start=start)
                    if detail:
                        lines.extend(detail.splitlines())
                        self.history.extend(detail.splitlines())
                self.notice = friendly_error(detail) or 'Не удалось запустить клиент. Открой журнал.'
            self._client_log = None
        if self._pending_play is not None and self.server is not None:
            if not self.server.running:
                self._pending_play = None
            elif self.server.started and not self.busy:
                try:
                    self.run('play', **self._pending_play)
                except (OSError, ValueError) as error:
                    self._pending_play = None
                    self.notice = friendly_error(error) or str(error)
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
