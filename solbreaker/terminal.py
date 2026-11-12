"""Keyboard-first server and client management over a local terminal or SSH."""
from dataclasses import asdict
from pathlib import Path
import sys

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, RichLog, Static, TabbedContent, TabPane

from .management import Manager, Settings, read_log
from .build_info import COMPATIBILITY


class ExportPath(ModalScreen):
    CSS = '''
    ExportPath { align: center middle; background: $background 80%; }
    #export-dialog { width: 70; height: auto; padding: 2 3; background: #1b2b39; border: solid #627f94; }
    #export-dialog Input { margin: 1 0; }
    #export-dialog Horizontal { height: auto; }
    '''

    def compose(self):
        with VerticalScroll(id='export-dialog'):
            yield Label('Новый файл подключения')
            yield Input(str(Path.home()/'solbreaker-connection.json'), id='export-path')
            with Horizontal():
                yield Button('Экспортировать', id='confirm', variant='primary')
                yield Button('Отмена', id='cancel')

    def on_button_pressed(self, event):
        event.stop()
        value = self.query_one('#export-path', Input).value.strip()
        if event.button.id == 'cancel':
            self.dismiss(None)
        elif not value or Path(value).exists():
            self.notify('Выбери новое имя файла', severity='warning')
        else:
            self.dismiss(value)


class Terminal(App):
    TITLE = 'Solbreaker'
    SUB_TITLE = COMPATIBILITY
    BINDINGS = [('ctrl+q', 'quit', 'Выход'), ('f5', 'refresh_log', 'Журнал сервера')]
    CSS = '''
    Screen { background: #14202b; color: #e9eef2; }
    Header { background: #0c1117; }
    Footer { background: #0c1117; }
    #status { height: 3; padding: 1 2; background: #1b2b39; color: #f1cb88; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 1 2; }
    .help { color: #9cabb9; margin-bottom: 1; height: auto; }
    .section { margin-top: 1; text-style: bold; color: #e8b866; height: auto; }
    .buttons { height: auto; margin: 1 0; }
    Button { margin-right: 1; background: #253a4b; color: #e9eef2; border: tall #3b5264; }
    Button.-primary { background: #e8b866; color: #17212a; border: tall #e8b866; }
    Button:focus { border: tall #f2cb87; }
    Input { margin: 0 0 1 0; background: #0e1821; border: tall #3a4d5d; }
    Input:focus { border: tall #e8b866; }
    RichLog { background: #0b131a; border: solid #344a5b; height: 1fr; }
    #notice { height: 2; padding: 0 2; color: #aebdca; }
    '''

    def __init__(self, config):
        super().__init__()
        self.config = Path(config)
        self.manager = Manager(Settings.load(config))

    def input_field(self, key, title):
        yield Label(title)
        yield Input(str(getattr(self.manager.settings, key)), id='field-'+key)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static('', id='status', markup=False)
        with TabbedContent():
            with TabPane('Сервер', id='server-tab'):
                with VerticalScroll():
                    yield Label('Локальный сервер', classes='section')
                    yield Label('При выходе из TUI локальный сервер будет остановлен.\nОтдельно запущенные серверы управляются из их терминала.', classes='help')
                    with Horizontal(classes='buttons'):
                        yield Button('Запустить', id='server', variant='primary')
                        yield Button('Остановить', id='stop')
                        yield Button('Подготовить', id='prepare-server')
                    yield Label('Обслуживание', classes='section')
                    yield Label('Резервное копирование выполняется с остановленным сервером.', classes='help')
                    with Horizontal(classes='buttons'):
                        yield Button('Резервная копия', id='backup')
                        yield Button('Экспорт подключения', id='export')
            with TabPane('Клиент', id='client-tab'):
                yield Label('Отдельный клиент Solbreaker', classes='section')
                yield Label('Для Linux нужны установленный Steam и Proton.\nРегистрация и настройки боя доступны в игре.', classes='help')
                with Horizontal(classes='buttons'):
                    yield Button('Играть', id='play', variant='primary')
                    yield Button('Подготовить карты', id='prepare-maps')
                    yield Button('Подготовить клиент', id='prepare-client')
                yield Static('', id='map-preparation', markup=False)
            with TabPane('Пути', id='paths-tab'):
                with VerticalScroll():
                    for key, title in (('game', 'Папка совместимой сборки'), ('state', 'Данные сервера'),
                                       ('client', 'Отдельный клиент'), ('backups', 'Резервные копии')):
                        yield from self.input_field(key, title)
                    if sys.platform != 'win32':
                        yield from self.input_field('proton', 'Исполняемый файл Proton')
                        yield from self.input_field('steam', 'Папка Steam')
                    yield Checkbox('Использовать готовые Rust-библиотеки', self.manager.settings.reuse_native, id='reuse_native')
                    yield Button('Сохранить', id='save')
            with TabPane('Сеть', id='network-tab'):
                with VerticalScroll():
                    yield Label('Адрес имеет приоритет над файлом подключения.', classes='help')
                    for key, title in (('server_url', 'Адрес сервера для игры'), ('connection', 'JSON-профиль подключения'),
                                       ('host', 'Локальный IPv4'), ('http_port', 'HTTP-порт'),
                                       ('login_port', 'Login UDP-порт'), ('network', 'Сетевой TOML')):
                        yield from self.input_field(key, title)
                    with Horizontal(classes='buttons'):
                        yield Button('Подключить клиент', id='connect', variant='primary')
                        yield Button('Применить TOML', id='network')
            with TabPane('Журнал', id='log-tab'):
                yield RichLog(id='log', max_lines=2000, markup=False, wrap=True)
        yield Static('Выбери действие', id='notice', markup=False)
        yield Footer()

    def on_mount(self):
        self.set_interval(.25, self.poll)
        self.set_interval(1.5, self.refresh_status)
        self.refresh_status()

    def save_settings(self):
        values = asdict(self.manager.settings)
        for field in self.query(Input):
            values[field.id.removeprefix('field-')] = field.value.strip()
        values['reuse_native'] = self.query_one('#reuse_native', Checkbox).value
        if any(values[key] != getattr(self.manager.settings, key) for key in ('connection', 'server_url')):
            values['play_mode'] = 'auto'
        settings = Settings(**values)
        self.manager.update_settings(settings)
        settings.save(self.config)

    def on_button_pressed(self, event):
        action = event.button.id
        try:
            if action == 'stop':
                self.manager.stop_server()
            elif action == 'export':
                self.push_screen(ExportPath(), self.export_profile)
            else:
                self.save_settings()
                if action == 'save':
                    self.manager.notice = 'Настройки сохранены'
                else:
                    self.manager.run(action)
                    if action not in ('play', 'prepare-maps'):
                        self.query_one(TabbedContent).active = 'log-tab'
        except (OSError, ValueError) as error:
            self.notify(str(error), severity='error', timeout=8)
        self.poll()

    def export_profile(self, path):
        if path:
            try:
                self.save_settings()
                self.manager.run('export', output=path)
                self.query_one(TabbedContent).active = 'log-tab'
            except (OSError, ValueError) as error:
                self.notify(str(error), severity='error')

    def refresh_status(self):
        status = self.manager.server_status()
        self.query_one('#status', Static).update('Solbreaker  /  '+status)
        owned = self.manager.server is not None and self.manager.server.running
        self.query_one('#server', Button).disabled = owned or self.manager.busy or status not in ('Сервер остановлен', 'Ошибка запуска сервера')
        self.query_one('#stop', Button).disabled = not owned
        prepare_maps = self.query_one('#prepare-maps', Button)
        prepare_maps.display = self.manager.can_prepare_maps
        prepare_maps.disabled = self.manager.busy or status not in (
            'Сервер остановлен', 'Ошибка запуска сервера')

    def poll(self):
        for line in self.manager.drain():
            self.query_one('#log', RichLog).write(Text(line))
        self.query_one('#notice', Static).update(self.manager.notice)
        self.query_one('#map-preparation', Static).update(
            self.manager.map_preparation_status() if self.manager.settings.local_connection else '')

    def action_refresh_log(self):
        try:
            self.query_one(TabbedContent).active = 'log-tab'
            self.query_one('#log', RichLog).write(Text(read_log(self.manager.settings.state)))
        except OSError as error:
            self.notify(str(error), severity='error')

    def action_quit(self):
        if self.manager.busy:
            self.notify('Дождись завершения текущей операции', severity='warning')
            return
        self.manager.stop_server()
        if self.manager.server is not None and self.manager.server.running:
            self.notify('Сервер завершает работу. Нажми Ctrl+Q ещё раз после остановки')
            return
        self.exit()

    def on_unmount(self):
        self.manager.stop_server()


def run(config):
    Terminal(config).run()
