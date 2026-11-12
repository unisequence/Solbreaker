"""Guided game setup and desktop management for Solbreaker."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
import os
import re
from pathlib import Path
import sys
from urllib.parse import urlsplit

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QRadioButton, QScrollArea, QSpinBox, QStackedWidget,
    QVBoxLayout, QWidget)

from . import __version__, updates
from .management import PROJECT, Manager, Settings, read_log
from .build_info import COMPATIBILITY
from .desktop_i18n import tr

ASSETS = Path(__file__).with_name('assets')


def application_icon():
    source = QPixmap(str(ASSETS/'solbreaker.png'))
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        canvas = QPixmap(size, size)
        canvas.fill(Qt.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor('#211a2b'))
        painter.drawRoundedRect(QRectF(0, 0, size, size), size*.18, size*.18)
        painter.drawPixmap(0, 0, source.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        painter.end()
        icon.addPixmap(canvas)
    return icon


STYLE = '''
QWidget { background: #f8f7fb; color: #312c3c; font-size: 14px; }
QWidget#sidebar { background: #efedf5; }
QLabel { background: transparent; }
QLabel#mark { background: #28232f; border-radius: 9px; padding: 6px; }
QLabel#brand { font-size: 21px; font-weight: 700; }
QLabel#heading { font-size: 27px; font-weight: 700; }
QLabel#muted { color: #776d85; }
QLabel#subheading { font-size: 19px; font-weight: 600; }
QLabel#ready { color: #715593; font-weight: 600; }
QLabel#error { color: #a34d67; background: #fbecf2; padding: 12px; border-radius: 6px; }
QFrame#panel { background: #ffffff; border: 1px solid #e3deec; border-radius: 10px; }
QPushButton { background: #ffffff; border: 1px solid #d5cce2; padding: 11px 17px; border-radius: 6px; }
QPushButton:hover { background: #f0ebf7; border-color: #b29cc9; }
QPushButton:focus { border: 2px solid #9675b4; }
QPushButton:disabled { color: #a39aaa; background: #f1eef5; border-color: #e7e1ee; }
QPushButton#primary { background: #8364a4; color: #ffffff; border-color: #8364a4; font-weight: 600; }
QPushButton#primary:hover { background: #70518e; }
QPushButton#primary:disabled { background: #c8bbd7; border-color: #c8bbd7; }
QPushButton#nav { text-align: left; background: transparent; border: 0; padding: 13px 15px; color: #7c708c; }
QPushButton#nav:checked { background: #e5dcee; color: #54396f; font-weight: 600; }
QLineEdit, QSpinBox, QComboBox { background: #ffffff; border: 1px solid #d5cce2; border-radius: 5px; padding: 9px; selection-background-color: #dac8ee; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #9675b4; }
QRadioButton { padding: 8px 0; spacing: 10px; }
QRadioButton::indicator { width: 15px; height: 15px; border: 1px solid #b6a4c9; border-radius: 8px; background: #ffffff; }
QRadioButton::indicator:checked { background: #9675b4; border: 3px solid #eee6f7; }
QCheckBox { padding: 7px 0; }
QPlainTextEdit { background: #f1edf6; border: 1px solid #ddd3e7; border-radius: 6px; padding: 10px; }
QProgressBar { border: 0; background: #e6deef; height: 5px; border-radius: 2px; }
QProgressBar::chunk { background: #9675b4; }
QScrollArea { border: 0; }
QScrollBar:vertical { background: #f4eff8; width: 9px; }
QScrollBar::handle:vertical { background: #cbbbdc; min-height: 30px; border-radius: 4px; }
'''


DARK_PALETTE = {'#f8f7fb': '#0b0a0e',
 '#312c3c': '#f0ecf5',
 '#efedf5': '#08070b',
 '#28232f': '#08070b',
 '#776d85': '#aaa3b6',
 '#715593': '#d3b6f0',
 '#a34d67': '#e4a6ad',
 '#fbecf2': '#35212b',
 '#ffffff': '#131017',
 '#e3deec': '#302a39',
 '#d5cce2': '#393240',
 '#f0ebf7': '#211a2b',
 '#b29cc9': '#9c83bd',
 '#9675b4': '#ccb1ed',
 '#a39aaa': '#82788f',
 '#f1eef5': '#18151f',
 '#e7e1ee': '#2b2534',
 '#8364a4': '#cfb4ef',
 '#70518e': '#e2cdf7',
 '#c8bbd7': '#514359',
 '#7c708c': '#b0a8bc',
 '#e5dcee': '#251d32',
 '#54396f': '#e5d1fa',
 '#dac8ee': '#534265',
 '#b6a4c9': '#927fa7',
 '#eee6f7': '#30243d',
 '#f1edf6': '#0e0c12',
 '#ddd3e7': '#342a40',
 '#e6deef': '#2d233a',
 '#f4eff8': '#141019',
 '#cbbbdc': '#4a3b5b'}


def theme_style(name):
    if name == 'light':
        return STYLE
    style = re.sub(r'#[0-9a-fA-F]{6}', lambda match: DARK_PALETTE.get(match[0], match[0]), STYLE)
    return style + '\nQPushButton#primary { color: #1b1524; }\nQPushButton#primary:disabled { color: #a497b2; }'


def label(text, role=None):
    result = QLabel(text)
    result.setWordWrap(True)
    result.setTextFormat(Qt.PlainText)
    if role:
        result.setObjectName(role)
    return result


def button(text, callback, primary=False):
    result = QPushButton(text)
    if primary:
        result.setObjectName('primary')
    result.clicked.connect(callback)
    return result


def document(path):
    try:
        path = Path(path)
        if not path.is_file() or path.stat().st_size > 1 << 20:
            return {}
        result = json.loads(path.read_text())
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def prepared_client(settings):
    root = Path(settings.client)
    return bool(document(root/'manifest.json')) and (root/'client/wotblitz.exe').is_file()


def local_profile(settings):
    return settings.local_connection


def connection_matches(settings, local):
    client = document(Path(settings.client)/'manifest.json')
    profile_path = Path(settings.state)/'connection.json' if local else Path(settings.connection)
    if local or not settings.server_url.strip():
        profile = document(profile_path)
        return bool(profile.get('server_id')) and profile['server_id'] == client.get('server_id')
    try:
        from .network import load_network, origin
        target = urlsplit(settings.server_url)
        requested = origin(f'{target.scheme}://{target.netloc}', public=True)
        return load_network(Path(settings.client)).advertise_http == requested
    except (OSError, ValueError, KeyError):
        return False


class LauncherBanner(QFrame):
    def __init__(self):
        super().__init__()
        self.cover = QPixmap(str(ASSETS/'launcher-cover.png'))
        self.setFixedHeight(190)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addStretch()
        self.setAccessibleName('Solbreaker — ' + COMPATIBILITY)
        version = label(COMPATIBILITY)
        version.setStyleSheet('color: #ddd6e8; background: transparent;')
        layout.addWidget(version)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        shape = QPainterPath()
        shape.addRoundedRect(QRectF(self.rect()), 9, 9)
        painter.setClipPath(shape)
        painter.fillRect(self.rect(), QColor('#15111b'))
        if not self.cover.isNull():
            image = self.cover.scaled(self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            painter.drawPixmap((self.width()-image.width())//2, (self.height()-image.height())//2, image)


class SetupWizard(QDialog):
    """Three concrete steps ending in a serial, observable preparation plan."""
    def __init__(self, window, local):
        super().__init__(window)
        self.window = window
        self.local = local
        self.fields = {}
        self.pending = []
        self.job = None
        self.failed = False
        self.complete = False
        self.setWindowTitle(tr('Настройка игры — Solbreaker'))
        self.setWindowModality(Qt.ApplicationModal)
        self.resize(760, 650)
        self.setMinimumSize(680, 600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)
        self.step_label = label('', 'muted')
        layout.addWidget(self.step_label)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        settings = window.manager.settings
        first = self.add_page()
        first.addWidget(label(tr('Откуда взять файлы игры?'), 'heading'))
        first.addWidget(label(tr('Укажи установленную копию совместимой сборки. Лаунчер создаст отдельный клиент Solbreaker.'), 'muted'))
        first.addWidget(button(tr('Скачать совместимую сборку через Steam'), self.download_depot))
        form = QFormLayout()
        self.path_field(form, 'game', tr('Папка игры'), settings.game)
        self.path_field(form, 'client', tr('Куда установить Solbreaker'), settings.client)
        first.addLayout(form)
        first.addWidget(label(tr('Для новой копии выбери родительскую папку. Можно также указать уже подготовленный клиент.'), 'muted'))
        if sys.platform != 'win32':
            first.addWidget(label(tr('Запуск игры в Linux'), 'subheading'))
            first.addWidget(label(tr('Proton запускает Windows-клиент в Linux. Он устанавливается вместе со Steam.'), 'muted'))
            linux = QFormLayout()
            self.path_field(linux, 'proton', tr('Файл Proton'), settings.proton, 'file')
            self.path_field(linux, 'steam', tr('Папка Steam'), settings.steam)
            first.addLayout(linux)
        first.addStretch()
        second = self.add_page()
        if local:
            second.addWidget(label(tr('Сервер на этом компьютере'), 'heading'))
            second.addWidget(label(tr('Лаунчер подготовит свой сервер. При нажатии «Играть» он будет запускаться автоматически.'), 'muted'))
            second.addWidget(label(tr('После входа выбери «Бой с ботами» в самой игре. Там же доступны настройки боя и регистрация аккаунта.')))
            second.addSpacing(15)
            second.addWidget(label(tr('Где хранятся данные'), 'subheading'))
            second.addWidget(label(settings.state, 'muted'))
            second.addWidget(label(tr('Другую папку и сетевые параметры можно выбрать в разделе «Параметры».'), 'muted'))
        else:
            second.addWidget(label(tr('К какому серверу подключиться?'), 'heading'))
            second.addWidget(label(tr('Адрес или файл подключения даст владелец сервера.'), 'muted'))
            self.by_url = QRadioButton(tr('У меня есть адрес сервера'))
            self.by_file = QRadioButton(tr('У меня есть файл подключения'))
            self.by_url.setChecked(bool(settings.server_url.strip()) or local_profile(settings))
            self.by_file.setChecked(not self.by_url.isChecked())
            second.addWidget(self.by_url)
            self.url = QLineEdit(settings.server_url)
            self.url.setPlaceholderText('https://server.example.org')
            self.url.setAccessibleName(tr('Адрес игрового сервера'))
            second.addWidget(self.url)
            second.addWidget(self.by_file)
            profile = QFormLayout()
            self.path_field(profile, 'connection', tr('Файл подключения'), '' if local_profile(settings) else settings.connection, 'file')
            second.addLayout(profile)
            self.by_url.toggled.connect(self.toggle_connection)
            self.toggle_connection()
        second.addStretch()
        third = self.add_page()
        third.addWidget(label(tr('Подготовка к первому запуску'), 'heading'))
        self.plan = label(tr('Проверим файлы, подготовим клиент и сохраним подключение.'))
        third.addWidget(self.plan)
        self.activity = label('', 'ready')
        third.addWidget(self.activity)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        third.addWidget(self.progress)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumBlockCount(400)
        self.details.setFont(QFont('Monospace', 9))
        third.addWidget(self.details, 1)
        self.error_label = label('', 'error')
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)
        navigation = QHBoxLayout()
        self.back = button(tr('Назад'), self.previous)
        self.cancel = button(tr('Отмена'), self.reject)
        self.next = button(tr('Далее'), self.advance, True)
        navigation.addWidget(self.back)
        navigation.addStretch()
        navigation.addWidget(self.cancel)
        navigation.addWidget(self.next)
        layout.addLayout(navigation)
        self.show_step(0)

    def download_depot(self):
        from .desktop_depot import DepotDialog
        steam = self.fields['steam'].text() if 'steam' in self.fields else ''
        dialog = DepotDialog(self, steam)
        if dialog.exec() == QDialog.Accepted:
            self.fields['game'].setText(dialog.selected_game)
            self.fields['game'].setCursorPosition(0)
            self.fields['game'].setToolTip(dialog.selected_game)

    def add_page(self):
        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(16)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        self.pages.addWidget(scroll)
        return column

    def path_field(self, form, key, title, value, kind='directory'):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        field = QLineEdit(value)
        field.setCursorPosition(0)
        field.setToolTip(value)
        field.setAccessibleName(title)
        self.fields[key] = field
        layout.addWidget(field, 1)
        layout.addWidget(button(tr('Выбрать'), lambda: self.choose(key, kind)))
        form.addRow(title, row)

    def choose(self, key, kind):
        current = self.fields[key].text()
        if kind == 'file':
            selected, _ = QFileDialog.getOpenFileName(self, tr('Выбрать файл'), current)
        else:
            selected = QFileDialog.getExistingDirectory(self, tr('Выбрать папку'), current)
            if selected and key == 'client' and not (Path(selected)/'manifest.json').is_file():
                selected = str(Path(selected)/'SolbreakerClient')
        if selected:
            self.fields[key].setText(selected)
            self.fields[key].setCursorPosition(0)
            self.fields[key].setToolTip(selected)
            if key == 'game' and 'proton' in self.fields and not self.fields['proton'].text():
                candidate = Path(selected).parent/'proton/proton'
                if candidate.is_file():
                    self.fields['proton'].setText(str(candidate))

    def toggle_connection(self):
        self.url.setEnabled(self.by_url.isChecked())
        self.fields['connection'].setEnabled(self.by_file.isChecked())

    def show_step(self, index):
        self.pages.setCurrentIndex(index)
        self.step_label.setText((tr('Шаг 1 из 3 — файлы игры'), tr('Шаг 2 из 3 — сервер'), tr('Шаг 3 из 3 — подготовка'))[index])
        self.back.setVisible(index > 0)
        self.next.setText(tr('Подготовить') if index == 2 else tr('Далее'))
        self.error_label.setVisible(False)

    def previous(self):
        self.show_step(max(0, self.pages.currentIndex()-1))

    def advance(self):
        self.error_label.setVisible(False)
        try:
            index = self.pages.currentIndex()
            if self.complete:
                self.accept()
                self.window.refresh_status()
                self.window.play()
                return
            if index == 0:
                game = Path(self.fields['game'].text()).expanduser()
                if not (game/'wotblitz.exe').is_file() or not (game/'Data').is_dir():
                    raise ValueError(tr('Выбери папку игры, в которой находятся wotblitz.exe и Data.'))
                if not self.fields['client'].text().strip():
                    raise ValueError(tr('Укажи папку для отдельного клиента Solbreaker.'))
                if 'proton' in self.fields and not prepared_client(self.settings()):
                    if not Path(self.fields['proton'].text()).expanduser().is_file():
                        raise ValueError(tr('Выбери файл proton из установленного Steam Proton.'))
                    if not Path(self.fields['steam'].text()).expanduser().is_dir():
                        raise ValueError(tr('Укажи папку установленного Steam.'))
            elif index == 1 and not self.local:
                if self.by_url.isChecked() and not self.url.text().strip():
                    raise ValueError(tr('Введи адрес сервера.'))
                if self.by_file.isChecked() and not Path(self.fields['connection'].text()).expanduser().is_file():
                    raise ValueError(tr('Выбери файл подключения от владельца сервера.'))
            if index < 2:
                self.show_step(index+1)
                if index == 1:
                    self.plan.setText(tr('Подготовим свой сервер и отдельный клиент.') if self.local
                                      else tr('Подготовим клиент для выбранного сервера. Локальный сервер для этого не требуется.'))
            else:
                self.prepare()
        except (OSError, ValueError) as error:
            self.show_error(error)

    def settings(self):
        values = asdict(self.window.manager.settings)
        for key, field in self.fields.items():
            values[key] = field.text().strip()
        values['play_mode'] = 'local' if self.local else 'remote'
        if self.local:
            values['server_url'] = ''
            values['connection'] = str(Path(values['state'])/'connection.json')
        elif hasattr(self, 'by_url'):
            values['server_url'] = self.url.text().strip() if self.by_url.isChecked() else ''
        return Settings(**values)

    def prepare(self):
        settings = self.settings()
        client = document(Path(settings.client)/'manifest.json')
        if client and Path(client.get('game', '')).resolve() != Path(settings.game).expanduser().resolve():
            raise ValueError(tr('Этот клиент создан из другой папки игры. Выбери новую папку для клиента.'))
        self.window.manager.update_settings(settings)
        settings.save(self.window.config)
        self.window.sync_fields()
        self.pending = []
        if self.local and (not document(Path(settings.state)/'manifest.json')
                           or not (Path(settings.state)/'connection.json').is_file()):
            self.pending.append('prepare-server')
        if not prepared_client(settings):
            self.pending.append('prepare-client')
        elif not connection_matches(settings, self.local):
            self.pending.append('connect')
        self.failed = False
        self.next.setEnabled(False)
        self.back.setEnabled(False)
        self.cancel.setEnabled(False)
        self.progress.setVisible(bool(self.pending))
        self.start_next()

    def start_next(self):
        if not self.pending:
            self.complete = True
            self.activity.setText(tr('Всё готово. Можно начинать игру.'))
            self.progress.setVisible(False)
            self.next.setText(tr('Играть'))
            self.next.setEnabled(True)
            self.cancel.setText(tr('Готово'))
            self.cancel.setEnabled(True)
            self.window.refresh_status()
            return
        action = self.pending.pop(0)
        self.progress.setRange(0, 0)
        self.activity.setText({'prepare-server': tr('Подготавливаем свой сервер…'),
                               'prepare-client': tr('Создаём отдельный клиент Solbreaker…'),
                               'connect': tr('Сохраняем подключение к серверу…')}[action])
        try:
            self.window.manager.run(action)
            self.job = self.window.manager.job
        except (OSError, ValueError) as error:
            self.show_error(error)

    def update_job(self, lines):
        if lines:
            visible = []
            for line in lines:
                try:
                    event = json.loads(line)
                except ValueError:
                    event = None
                if isinstance(event, dict) and event.get('event') == 'setup_progress':
                    line = tr(str(event.get('message', '')))
                    self.activity.setText(line)
                    completed, total = event.get('completed'), event.get('total')
                    if type(completed) is int and type(total) is int and 0 <= completed <= total and total > 0:
                        self.progress.setRange(0, 1000)
                        self.progress.setValue(completed*1000//total)
                    else:
                        self.progress.setRange(0, 0)
                visible.append(line)
            self.details.appendPlainText('\n'.join(visible))
        if self.job is not None and self.job.returncode is not None:
            result = self.job.returncode
            self.job = None
            if result == 0:
                self.start_next()
            else:
                self.show_error(tr('Подготовка завершилась с ошибкой. Причина указана в подробностях выше. Можно исправить параметры и повторить.'))

    def show_error(self, error):
        self.failed = True
        self.pending.clear()
        self.job = None
        self.error_label.setText(tr(str(error)))
        self.error_label.setVisible(True)
        self.progress.setVisible(False)
        self.next.setEnabled(True)
        self.back.setEnabled(self.pages.currentIndex() > 0)
        self.cancel.setEnabled(True)

    def reject(self):
        if self.job is None:
            super().reject()

    def closeEvent(self, event):
        if self.job is not None:
            event.ignore()
        else:
            super().closeEvent(event)


class Window(QMainWindow):
    def __init__(self, config):
        super().__init__()
        self.config = Path(config)
        self.manager = Manager(Settings.load(config))
        self.update_kind = updates.package_kind(PROJECT)
        self.update_task = None
        self.update_result = os.environ.pop('SOLBREAKER_UPDATE_RESULT', '')
        QApplication.instance().setStyleSheet(theme_style(self.manager.settings.gui_theme))
        self.build_ui()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(250)
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.refresh_status)
        self.status_timer.start(1500)
        self.refresh_status()
        self.poll()

    def build_ui(self):
        QApplication.instance().setProperty('solbreaker_language', self.manager.settings.gui_language)
        self.fields = {}
        self.wizard = None
        self.setWindowTitle('Solbreaker')
        icon = application_icon()
        QApplication.instance().setWindowIcon(icon)
        self.setWindowIcon(icon)
        self.resize(1120, 700)
        self.setMinimumSize(920, 640)
        root = QWidget()
        self.setCentralWidget(root)
        horizontal = QHBoxLayout(root)
        horizontal.setContentsMargins(0, 0, 0, 0)
        horizontal.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName('sidebar')
        sidebar.setMinimumWidth(200)
        navigation = QVBoxLayout(sidebar)
        navigation.setContentsMargins(20, 24, 20, 24)
        logo = QLabel()
        logo.setObjectName('mark')
        logo.setFixedSize(66, 66)
        logo.setAlignment(Qt.AlignCenter)
        logo.setPixmap(QPixmap(str(ASSETS/'solbreaker.png')).scaled(54, 54, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        navigation.addWidget(logo, 0, Qt.AlignHCenter)
        brand = label('Solbreaker', 'brand')
        brand.setWordWrap(False)
        brand.setAlignment(Qt.AlignCenter)
        navigation.addWidget(brand)
        version = label(COMPATIBILITY, 'muted')
        version.setAlignment(Qt.AlignCenter)
        navigation.addWidget(version)
        navigation.addSpacing(24)
        self.nav = []
        self.stack = QStackedWidget()
        for index, name in enumerate((tr('Игра'), tr('Мой сервер'), tr('Параметры'), tr('Журнал'))):
            item = QPushButton(name)
            item.setObjectName('nav')
            item.setCheckable(True)
            item.clicked.connect(lambda checked=False, page=index: self.select(page))
            navigation.addWidget(item)
            self.nav.append(item)
        navigation.addStretch()
        if self.update_kind:
            installed_version = label(f'Solbreaker {__version__}', 'muted')
            installed_version.setAlignment(Qt.AlignCenter)
            navigation.addWidget(installed_version)
            navigation.addWidget(button(tr('Обновить'), self.start_update))
        sidebar.setFixedWidth(max(200, sidebar.minimumSizeHint().width()))
        horizontal.addWidget(sidebar)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(12)
        self.heading = label('', 'heading')
        header = QHBoxLayout()
        header.addWidget(self.heading)
        header.addStretch()
        header.addWidget(label(tr('Тема'), 'muted'))
        self.theme_picker = QComboBox()
        self.theme_picker.addItems([tr('Светлая'), tr('Тёмная')])
        self.theme_picker.setAccessibleName(tr('Тема интерфейса'))
        self.theme_picker.setCurrentIndex(1 if self.manager.settings.gui_theme == 'dark' else 0)
        self.theme_picker.currentIndexChanged.connect(self.change_theme)
        header.addWidget(self.theme_picker)
        header.addWidget(label(tr('Язык'), 'muted'))
        self.language_picker = QComboBox()
        self.language_picker.addItem('Русский', 'ru')
        self.language_picker.addItem('English', 'en')
        self.language_picker.setAccessibleName(tr('Язык интерфейса'))
        self.language_picker.setCurrentIndex(1 if self.manager.settings.gui_language == 'en' else 0)
        self.language_picker.currentIndexChanged.connect(self.change_language)
        header.addWidget(self.language_picker)
        layout.addLayout(header)
        layout.addWidget(self.stack, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(5)
        layout.addWidget(self.progress)
        self.notice = label('', 'muted')
        layout.addWidget(self.notice)
        horizontal.addWidget(body, 1)
        self.home()
        self.server_page()
        self.settings_page()
        self.log_page()
        self.select(0)

    def change_language(self, index):
        previous = self.manager.settings
        if self.wizard is not None and self.wizard.isVisible():
            self.language_picker.blockSignals(True)
            self.language_picker.setCurrentIndex(1 if previous.gui_language == 'en' else 0)
            self.language_picker.blockSignals(False)
            return
        try:
            settings = replace(self.collect_settings(), gui_language='en' if index else 'ru')
            self.manager.update_settings(settings)
            settings.save(self.config)
        except (OSError, ValueError) as error:
            self.manager.settings = previous
            self.language_picker.blockSignals(True)
            self.language_picker.setCurrentIndex(1 if previous.gui_language == 'en' else 0)
            self.language_picker.blockSignals(False)
            self.error(error)
            return
        geometry = self.saveGeometry()
        size = self.size()
        page = self.stack.currentIndex()
        local = self.own.isChecked()
        log = self.log.toPlainText()
        old_root = self.takeCentralWidget()
        self.build_ui()
        self.sync_fields()
        self.restoreGeometry(geometry)
        if not self.isMaximized():
            self.resize(size)
        self.own.setChecked(local)
        self.select(page)
        self.log.setPlainText(log)
        self.refresh_status()
        self.poll()
        old_root.deleteLater()

    def change_theme(self, index):
        try:
            settings = replace(self.manager.settings, gui_theme='dark' if index else 'light')
            self.manager.update_settings(settings)
            settings.save(self.config)
            QApplication.instance().setStyleSheet(theme_style(settings.gui_theme))
        except (OSError, ValueError) as error:
            self.error(error)

    def page(self):
        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(16)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        self.stack.addWidget(scroll)
        return column

    def select(self, index):
        self.stack.setCurrentIndex(index)
        self.heading.setText(self.nav[index].text())
        for number, item in enumerate(self.nav):
            item.setChecked(number == index)

    def home(self):
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(12)
        column.addWidget(LauncherBanner())
        self.own = QRadioButton(tr('На своём компьютере'))
        self.remote = QRadioButton(tr('На другом сервере'))
        self.own.setChecked(local_profile(self.manager.settings))
        self.remote.setChecked(not self.own.isChecked())
        choice = QHBoxLayout()
        choice.addWidget(label(tr('Подключение'), 'muted'))
        choice.addSpacing(10)
        choice.addWidget(self.own)
        choice.addWidget(self.remote)
        choice.addStretch()
        column.addLayout(choice)
        self.mode_help = label('', 'muted')
        column.addWidget(self.mode_help)
        self.checks = []
        for unused in range(3):
            row = QHBoxLayout()
            mark = label('○', 'ready')
            mark.setFixedWidth(22)
            text = label('')
            row.addWidget(mark)
            row.addWidget(text, 1)
            column.addLayout(row)
            self.checks.append((mark, text))
        column.addWidget(label(tr('Режим, карта и настройки ботов выбираются в игре.'), 'muted'))
        self.remember_login = QCheckBox(tr('Входить автоматически в последний аккаунт'))
        self.remember_login.setStyleSheet('padding: 0;')
        self.remember_login.setToolTip(tr('Использовать сохранённый аккаунт при следующем запуске игры.'))
        self.remember_login.setChecked(self.manager.settings.remember_login)
        self.remember_login.toggled.connect(self.change_login_preference)
        column.addWidget(self.remember_login)
        column.addStretch()
        dock = QFrame()
        dock.setObjectName('panel')
        actions = QHBoxLayout(dock)
        actions.setContentsMargins(18, 16, 18, 16)
        actions.setSpacing(22)
        self.play_button = button('', self.primary_action, True)
        self.play_button.setMinimumSize(230, 58)
        self.play_button.setStyleSheet('font-size: 18px;')
        actions.addWidget(self.play_button)
        status = QVBoxLayout()
        status.setSpacing(6)
        self.readiness_title = label('', 'subheading')
        self.launch_help = label('', 'muted')
        status.addWidget(self.readiness_title)
        status.addWidget(self.launch_help)
        actions.addLayout(status, 1)
        column.addWidget(dock)
        self.stack.addWidget(page)
        self.own.toggled.connect(self.refresh_status)

    def change_login_preference(self, checked):
        try:
            settings = replace(self.manager.settings, remember_login=checked)
            self.manager.update_settings(settings)
            settings.save(self.config)
        except (OSError, ValueError) as error:
            self.remember_login.blockSignals(True)
            self.remember_login.setChecked(self.manager.settings.remember_login)
            self.remember_login.blockSignals(False)
            self.error(error)

    def server_page(self):
        column = self.page()
        column.addWidget(label(tr('Нужен, когда играешь на своём компьютере или принимаешь других игроков.'), 'muted'))
        identity = QFormLayout()
        name = self.manager.settings.server_name
        manifest = Path(self.manager.settings.state)/'manifest.json'
        if manifest.is_file():
            from .server_identity import server_name
            name = server_name(json.loads(manifest.read_text()))
        self.fields['server_name'] = QLineEdit(name)
        self.fields['server_name'].setMaxLength(48)
        self.fields['server_name'].setAccessibleName(tr('Название сервера'))
        identity.addRow(tr('Название сервера'), self.fields['server_name'])
        column.addLayout(identity)
        column.addWidget(button(tr('Сохранить название'), lambda: self.execute('server-name')))
        column.addWidget(label(tr('Сначала подготовь сервер. Для смены названия останови его, сохрани имя и запусти снова. Игроки увидят его после переподключения.'), 'muted'))
        self.server_label = label('', 'subheading')
        column.addWidget(self.server_label)
        column.addWidget(label(tr('Кнопка «Играть» сама запускает свой сервер. Здесь можно управлять им вручную.'), 'muted'))
        row = QHBoxLayout()
        self.start_button = button(tr('Запустить сервер'), lambda: self.execute('server'), True)
        self.stop_button = button(tr('Остановить сервер'), self.stop_server)
        row.addWidget(self.start_button)
        row.addWidget(self.stop_button)
        row.addStretch()
        column.addLayout(row)
        column.addWidget(label(tr('Обслуживание'), 'subheading'))
        column.addWidget(label(tr('Резервная копия содержит аккаунты, прогресс и ключ сервера. Перед копированием останови сервер.'), 'muted'))
        tools = QHBoxLayout()
        tools.addWidget(button(tr('Создать резервную копию'), lambda: self.execute('backup')))
        tools.addWidget(button(tr('Подключение для друга'), self.export))
        tools.addStretch()
        column.addLayout(tools)
        column.addWidget(label(tr('Сервер, запущенный из этого окна, остановится при его закрытии.'), 'muted'))
        column.addStretch()

    def path_field(self, form, key, title, kind='directory'):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        field = QLineEdit(str(getattr(self.manager.settings, key)))
        field.setCursorPosition(0)
        field.setAccessibleName(title)
        self.fields[key] = field
        layout.addWidget(field, 1)
        layout.addWidget(button(tr('Выбрать'), lambda: self.browse(key, kind)))
        form.addRow(title, row)

    def settings_page(self):
        column = self.page()
        column.addWidget(label(tr('Дополнительные параметры'), 'subheading'))
        column.addWidget(label(tr('Для первого запуска достаточно мастера на главном экране. Здесь можно изменить пути, порты и сетевую конфигурацию.'), 'muted'))
        column.addWidget(button(tr('Открыть мастер установки'), self.start_wizard))
        form = QFormLayout()
        form.setVerticalSpacing(12)
        for key, title in (('game', tr('Оригинальная игра')), ('client', tr('Клиент Solbreaker')), ('state', tr('Данные сервера'))):
            self.path_field(form, key, title)
        if sys.platform != 'win32':
            self.path_field(form, 'proton', tr('Файл Proton'), 'file')
            self.path_field(form, 'steam', tr('Папка Steam'))
        self.path_field(form, 'backups', tr('Резервные копии'))
        self.path_field(form, 'network', tr('Сетевой TOML'), 'file')
        self.fields['host'] = QLineEdit(self.manager.settings.host)
        form.addRow(tr('Локальный IPv4'), self.fields['host'])
        for key, title, maximum in (('http_port', tr('HTTP-порт'), 65535), ('login_port', tr('UDP-порт входа'), 65534)):
            spin = QSpinBox()
            spin.setRange(1024, maximum)
            spin.setValue(getattr(self.manager.settings, key))
            spin.setAccessibleName(title)
            self.fields[key] = spin
            form.addRow(title, spin)
        column.addLayout(form)
        self.reuse = QCheckBox(tr('Использовать уже собранные Rust-библиотеки'))
        self.reuse.setChecked(self.manager.settings.reuse_native)
        column.addWidget(self.reuse)
        actions = QHBoxLayout()
        actions.addWidget(button(tr('Сохранить параметры'), self.save, True))
        actions.addWidget(button(tr('Применить сетевой TOML'), lambda: self.execute('network')))
        actions.addStretch()
        column.addLayout(actions)
        column.addStretch()

    def log_page(self):
        column = self.page()
        column.addWidget(label(tr('Подробности подготовки и запуска'), 'muted'))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setFont(QFont('Monospace', 10))
        column.addWidget(self.log, 1)
        column.addWidget(button(tr('Прочитать журнал сервера'), self.load_log))

    def browse(self, key, kind):
        current = self.fields[key].text()
        if kind == 'file':
            path, _ = QFileDialog.getOpenFileName(self, tr('Выбрать файл'), current)
        else:
            path = QFileDialog.getExistingDirectory(self, tr('Выбрать папку'), current)
            if path and key in ('client', 'state') and not (Path(path)/'manifest.json').is_file():
                path = str(Path(path)/('SolbreakerClient' if key == 'client' else 'SolbreakerData'))
        if path:
            self.fields[key].setText(path)

    def collect_settings(self):
        values = asdict(self.manager.settings)
        for key, field in self.fields.items():
            values[key] = field.value() if isinstance(field, QSpinBox) else field.text().strip()
        values['reuse_native'] = self.reuse.isChecked()
        return Settings(**values)

    def sync_fields(self):
        for key, field in self.fields.items():
            value = getattr(self.manager.settings, key)
            if isinstance(field, QSpinBox):
                field.setValue(value)
            else:
                field.setText(value)
        self.reuse.setChecked(self.manager.settings.reuse_native)

    def save(self):
        try:
            settings = self.collect_settings()
            self.manager.update_settings(settings)
            settings.save(self.config)
            self.manager.notice = tr('Параметры сохранены')
            self.refresh_status()
            return True
        except (OSError, ValueError) as error:
            self.error(error)
            return False

    def execute(self, action):
        if self.save():
            try:
                self.manager.run(action)
            except (OSError, ValueError) as error:
                self.error(error)
        self.poll()

    def start_wizard(self):
        if self.manager.busy:
            self.error(tr('Дождись завершения текущей операции.'))
            return
        if self.save():
            self.wizard = SetupWizard(self, self.own.isChecked())
            self.wizard.finished.connect(self.wizard_closed)
            self.language_picker.setEnabled(False)
            self.wizard.open()

    def wizard_closed(self):
        if self.wizard is not None:
            self.wizard.deleteLater()
            self.wizard = None
        self.refresh_status()

    def primary_action(self):
        if self.ready_to_play():
            self.play()
        else:
            self.start_wizard()

    def play(self):
        self.execute('play')

    def stop_server(self):
        self.manager.stop_server()
        self.poll()

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, tr('Сохранить подключение для друга'), 'solbreaker-connection.json',
                                             tr('Файл подключения (*.json)'), options=QFileDialog.DontConfirmOverwrite)
        if path:
            if Path(path).exists():
                self.error(tr('Выбери новое имя файла подключения.'))
                return
            try:
                self.manager.update_settings(self.collect_settings())
                self.manager.run('export', output=path)
            except (OSError, ValueError) as error:
                self.error(error)

    def load_log(self):
        try:
            self.log.setPlainText(tr(read_log(self.manager.settings.state)))
        except OSError as error:
            self.error(error)

    def ready_to_play(self):
        settings = self.manager.settings
        client = document(Path(settings.client)/'manifest.json')
        game = Path(client.get('game') or settings.game)
        return (self.own.isChecked() == local_profile(settings) and (game/'wotblitz.exe').is_file()
                and prepared_client(settings) and connection_matches(settings, self.own.isChecked()))

    def start_update(self):
        if not self.update_kind or self.update_task is not None:
            return
        if self.manager.busy or self.wizard is not None:
            self.error(tr('Дождись завершения текущей операции.'))
            return
        if not self.save():
            return
        client = self.manager.client
        if ((client is not None and client.poll() is None)
                or self.manager.server_status() in ('Сервер работает', 'Сервер запускается',
                                                    'Сервер запущен отдельно',
                                                    'Не удалось проверить состояние сервера')):
            self.error(tr('Закрой игру и останови локальный сервер перед обновлением.'))
            return
        self.centralWidget().setEnabled(False)
        self.manager.notice = 'Проверяем и скачиваем обновление…'
        executor = ThreadPoolExecutor(max_workers=1)
        self.update_task = executor.submit(updates.prepare, PROJECT, __version__, self.update_kind)
        executor.shutdown(wait=False)
        self.poll()

    def finish_update(self):
        task = self.update_task
        self.update_task = None
        self.centralWidget().setEnabled(True)
        try:
            release = task.result()
        except Exception as error:
            self.log.appendPlainText(str(error))
            self.error('Не удалось подготовить обновление. Подробности в журнале.')
            return
        if release is None:
            self.manager.notice = 'Установлена последняя доступная версия.'
        elif self.close():
            QApplication.instance().exit(updates.RESTART)

    def poll(self):
        lines = self.manager.drain()
        if lines:
            self.log.appendPlainText('\n'.join(lines))
        if self.wizard is not None and self.wizard.isVisible():
            self.wizard.update_job(lines)
        starting = self.manager.server is not None and self.manager.server.running and not self.manager.server.started
        if self.update_task is not None and self.update_task.done():
            self.finish_update()
        if self.update_result:
            self.manager.notice = ('Solbreaker обновлён.' if self.update_result == 'success'
                                   else 'Установка обновления не удалась. См. runtime/launcher-update.log.')
            self.update_result = ''
        self.progress.setVisible(self.manager.busy or starting or self.update_task is not None)
        self.notice.setText(tr(self.manager.notice))
        self.notice.setVisible(self.manager.notice not in ('Выбери действие', 'Готово'))

    def refresh_status(self):
        settings = self.manager.settings
        local = self.own.isChecked()
        ready = self.ready_to_play()
        client = document(Path(settings.client)/'manifest.json')
        game = Path(client.get('game') or settings.game)
        states = [(game/'wotblitz.exe').is_file(), prepared_client(settings), connection_matches(settings, local)]
        descriptions = (tr('Файлы игры найдены') if states[0] else tr('Нужно выбрать папку совместимой сборки'),
                        tr('Клиент Solbreaker установлен') if states[1] else tr('Нужно установить отдельный клиент Solbreaker'),
                        (tr('Подключение к своему серверу настроено') if local else tr('Подключение к серверу настроено'))
                        if states[2] else (tr('Нужно подготовить свой сервер') if local else tr('Нужно указать адрес или файл подключения')))
        for (mark, text), complete, description in zip(self.checks, states, descriptions):
            mark.setText('✓' if complete else '○')
            text.setText(description)
        self.mode_help.setText(tr('Для боёв с ботами свой сервер запускается автоматически вместе с игрой.') if local
                               else tr('Для боёв с ботами используется выбранный сервер. Здесь запускается клиент.'))
        self.readiness_title.setText(tr('Можно начинать') if ready else tr('Подготовим всё для первого запуска'))
        self.play_button.setText(tr('Играть') if ready else tr('Настроить игру'))
        wizard_open = self.wizard is not None and self.wizard.isVisible()
        self.language_picker.setEnabled(not self.manager.busy and not wizard_open)
        self.play_button.setEnabled(not self.manager.busy)
        self.remember_login.setEnabled(not self.manager.busy)
        self.launch_help.setText((tr('Сервер запустится автоматически, затем откроется игра.') if local
                                  else tr('Откроется клиент с выбранным подключением.')) if ready
                                 else (tr('Мастер подготовит сервер для боёв с ботами и отдельный клиент.') if local
                                       else tr('Мастер подготовит клиент и подключение к выбранному серверу.')))
        status = self.manager.server_status()
        self.server_label.setText(tr(status))
        owned = self.manager.server is not None and self.manager.server.running
        self.start_button.setEnabled(not owned and not self.manager.busy and status in ('Сервер остановлен', 'Ошибка запуска сервера'))
        self.stop_button.setEnabled(owned)

    def error(self, error):
        self.manager.notice = str(error)
        QMessageBox.warning(self, 'Solbreaker', tr(str(error)))

    def closeEvent(self, event):
        if self.update_task is not None:
            event.ignore()
            return
        try:
            self.manager.close()
        except ValueError as error:
            event.ignore()
            self.error(error)
        else:
            event.accept()


def run(config):
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName('Solbreaker')
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    app.setFont(QFont('Segoe UI' if sys.platform == 'win32' else 'DejaVu Sans', 10))
    window = Window(config)
    window.show()
    return app.exec()
