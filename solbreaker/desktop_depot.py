"""Steam download guidance and cancellable resource verification for the first-run wizard."""
import sys
import time

from PySide6.QtCore import QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QHBoxLayout, QLineEdit, QProgressBar, QVBoxLayout

from .desktop import button, label
from .desktop_i18n import tr
from .steam_depot import DOWNLOAD_COMMAND, find_depots, verify_depot


class Verification(QThread):
    progress = Signal(int, int)

    def __init__(self, game, parent):
        super().__init__(parent)
        self.game = game
        self.result = None
        self.error = ''
        self.last_report = 0.

    def report(self, done, total):
        now = time.monotonic()
        if now-self.last_report >= .2 or done == total:
            self.progress.emit(round(done/total*1000) if total else 1000, round(done/(1 << 20)))
            self.last_report = now

    def run(self):
        try:
            self.result = verify_depot(self.game, progress=self.report, cancelled=self.isInterruptionRequested)
        except Exception as error:
            self.error = str(error)


class DepotDialog(QDialog):
    def __init__(self, parent, steam=''):
        super().__init__(parent)
        self.steam = steam
        self.worker = None
        self.closing = False
        self.selected_game = ''
        self.setWindowTitle(tr('Скачать сборку — Solbreaker'))
        self.resize(760, 600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        layout.addWidget(label(tr('Скачивание через Steam'), 'heading'))
        layout.addWidget(label(tr('Steam загрузит совместимую Windows-сборку 11.21.0.1235_7376187. '
                                  'Около 21 ГиБ, примерно 30 минут при 100 Мбит/с без учёта проверки.'), 'muted'))
        if sys.platform == 'win32':
            layout.addWidget(label(tr('Для отдельного клиента оставь ещё около 22 ГиБ на диске назначения.'), 'muted'))
        layout.addWidget(label(tr('1. Войди в установленный Steam с аккаунтом, которому доступна игра.\n'
                               '2. Открой консоль Steam, вставь команду ниже и нажми Enter.\n'
                               '3. Дождись «Depot download complete», затем проверь файлы.')))
        actions = QHBoxLayout()
        actions.addWidget(button(tr('Открыть консоль Steam'), self.open_steam))
        actions.addWidget(button(tr('Установить Steam'), lambda: QDesktopServices.openUrl(QUrl('https://store.steampowered.com/about/'))))
        layout.addLayout(actions)
        row = QHBoxLayout()
        command = QLineEdit(DOWNLOAD_COMMAND)
        command.setReadOnly(True)
        command.setAccessibleName(tr('Команда скачивания сборки'))
        row.addWidget(command, 1)
        row.addWidget(button(tr('Скопировать'), lambda: QApplication.clipboard().setText(DOWNLOAD_COMMAND)))
        layout.addLayout(row)
        layout.addWidget(label(tr('Папка скачанного depot'), 'subheading'))
        row = QHBoxLayout()
        self.folder = QLineEdit()
        self.folder.setAccessibleName(tr('Папка скачанного depot'))
        self.folder.textChanged.connect(self.invalidate)
        row.addWidget(self.folder, 1)
        self.browse = button(tr('Выбрать'), self.choose)
        row.addWidget(self.browse)
        layout.addLayout(row)
        layout.addWidget(label(tr('Найдём папку Steam автоматически. Скорость и пауза загрузки настраиваются в Steam. Проверенная сборка станет источником для отдельной копии Solbreaker.'), 'muted'))
        self.status = label(tr('Ожидаем скачанный depot…'), 'muted')
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        layout.addStretch()
        row = QHBoxLayout()
        row.addWidget(button(tr('Закрыть'), self.reject))
        row.addStretch()
        self.check = button(tr('Проверить файлы'), self.verify)
        row.addWidget(self.check)
        self.use = button(tr('Использовать сборку'), self.accept, True)
        self.use.setEnabled(False)
        row.addWidget(self.use)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.detect)
        self.timer.start(3000)
        self.detect()

    def open_steam(self):
        QApplication.clipboard().setText(DOWNLOAD_COMMAND)
        if QDesktopServices.openUrl(QUrl('steam://open/console')):
            self.status.setText(tr('Команда скопирована. Вставь её в консоль Steam и нажми Enter.'))
        else:
            self.status.setText(tr('Установи Steam и войди в свой аккаунт, затем открой консоль повторно.'))

    def detect(self):
        if not self.folder.text():
            candidates = find_depots(self.steam)
            if candidates:
                self.folder.setText(str(candidates[0]))

    def choose(self):
        selected = QFileDialog.getExistingDirectory(self, tr('Выбрать скачанный depot'), self.folder.text())
        if selected:
            self.folder.setText(selected)

    def invalidate(self):
        self.selected_game = ''
        if hasattr(self, 'use'):
            self.use.setEnabled(False)

    def verify(self):
        if self.worker and self.worker.isRunning():
            return
        if not self.folder.text().strip():
            self.status.setText(tr('Дождись появления папки depot или выбери её вручную.'))
            return
        self.invalidate()
        self.folder.setEnabled(False)
        self.browse.setEnabled(False)
        self.check.setEnabled(False)
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self.status.setText(tr('Проверяем версию и комплектность файлов…'))
        self.worker = Verification(self.folder.text(), self)
        self.worker.progress.connect(self.update_progress)
        self.worker.finished.connect(self.verified)
        self.worker.start()

    def update_progress(self, value, mib):
        self.progress.setRange(0, 1000)
        self.progress.setValue(value)
        self.status.setText(tr('Проверяем SHA-256 файлов — прочитано {mib} МиБ').format(mib=mib))

    def verified(self):
        self.folder.setEnabled(True)
        self.browse.setEnabled(True)
        self.check.setEnabled(True)
        if self.closing:
            super().reject()
        elif self.worker.result:
            self.selected_game = self.worker.result['game']
            self.progress.setRange(0, 1000)
            self.progress.setValue(1000)
            self.status.setText(tr('Сборка проверена. Все {files} файлов совпали.').format(files=self.worker.result['files']))
            self.use.setEnabled(True)
        else:
            self.progress.setVisible(False)
            self.status.setText(tr(self.worker.error))

    def accept(self):
        if self.selected_game:
            super().accept()

    def reject(self):
        if self.worker and self.worker.isRunning():
            self.closing = True
            self.worker.requestInterruption()
        else:
            super().reject()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)
