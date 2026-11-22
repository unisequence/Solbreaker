"""Download the pinned Steam depot and verify it for the first-run wizard."""
import json
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory
import time

from PySide6.QtCore import QLockFile, QProcess, QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QVBoxLayout, QWidget

from .build_info import STEAM_APP, STEAM_DEPOT, STEAM_MANIFEST
from .connection import atomic_write
from .desktop import button, document, label
from .desktop_i18n import tr
from .management import friendly_error
from .steam_depot import DEPOT_FOLDER, DOWNLOAD_COMMAND, bundled_downloader, find_depots, verify_depot

DEPOT_PROGRESS = re.compile(r'^\s*(\d{1,3}[.,]\d{2})%\s')
QR_ROW = re.compile(r'(?:██|  ){21,100}')


def qr_image(rows):
    size = len(rows)
    image = QImage(size, size, QImage.Format_RGB32)
    image.fill(Qt.white)
    for y, row in enumerate(rows):
        for x in range(size):
            if row[2*x] == '█':
                image.setPixel(x, y, 0xff000000)
    return image.scaled(size*max(1, 240//size), size*max(1, 240//size), Qt.KeepAspectRatio, Qt.FastTransformation)


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
    def __init__(self, parent, steam='', *, resume_file=None):
        super().__init__(parent)
        self.steam = steam
        self.resume_file = resume_file
        saved = document(resume_file).get('folder') if resume_file else None
        self.resume_parent = Path(saved) if isinstance(saved, str) and saved else None
        self.worker = None
        self.downloader = None
        self.downloader_session = None
        self.download_lock = None
        self.download_target = None
        self.download_cancelled = False
        self.download_percent = -1.
        self.download_output = b''
        self.download_error = ''
        self.qr_rows = None
        self.closing = False
        self.selected_game = ''
        self.setWindowTitle(tr('Скачать сборку — Solbreaker'))
        self.resize(760, 600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        layout.addWidget(label(tr('Скачать совместимую сборку'), 'heading'))
        layout.addWidget(label(tr('Windows-сборка 11.21.0.1235_7376187 занимает около 21 ГиБ. '
                                  'Для загрузки нужен аккаунт Steam с доступом к игре.'), 'muted'))
        if sys.platform == 'win32':
            layout.addWidget(label(tr('При установке из папки Steam понадобится ещё около 22 ГиБ на диске назначения.'), 'muted'))
        depot_actions = QHBoxLayout()
        self.download = button(tr('Скачать через DepotDownloader'), self.start_download, True)
        depot_actions.addWidget(self.download)
        self.change_folder = button(tr('Выбрать другую папку'), self.choose_download_folder)
        depot_actions.addWidget(self.change_folder)
        if bundled_downloader() is None:
            depot_actions.addWidget(button(tr('Получить DepotDownloader'), lambda: QDesktopServices.openUrl(
                QUrl('https://github.com/SteamRE/DepotDownloader/releases/latest'))))
        depot_actions.addStretch()
        layout.addLayout(depot_actions)
        self.manual = QWidget()
        manual = QVBoxLayout(self.manual)
        manual.setContentsMargins(0, 0, 0, 0)
        manual.setSpacing(16)
        layout.addWidget(self.manual)
        manual.addWidget(label(tr('Через консоль Steam'), 'subheading'))
        manual.addWidget(label(tr('Вставь команду в консоль, дождись «Depot download complete» и проверь файлы.'), 'muted'))
        actions = QHBoxLayout()
        actions.addWidget(button(tr('Открыть консоль Steam'), self.open_steam))
        actions.addWidget(button(tr('Установить Steam'), lambda: QDesktopServices.openUrl(QUrl('https://store.steampowered.com/about/'))))
        manual.addLayout(actions)
        row = QHBoxLayout()
        command = QLineEdit(DOWNLOAD_COMMAND)
        command.setReadOnly(True)
        command.setAccessibleName(tr('Команда скачивания сборки'))
        row.addWidget(command, 1)
        row.addWidget(button(tr('Скопировать'), lambda: QApplication.clipboard().setText(DOWNLOAD_COMMAND)))
        manual.addLayout(row)
        manual.addWidget(label(tr('Папка скачанного depot'), 'subheading'))
        row = QHBoxLayout()
        self.folder = QLineEdit()
        self.folder.setAccessibleName(tr('Папка скачанного depot'))
        self.folder.textChanged.connect(self.invalidate)
        row.addWidget(self.folder, 1)
        self.browse = button(tr('Выбрать'), self.choose)
        row.addWidget(self.browse)
        manual.addLayout(row)
        self.status = label(tr('Ожидаем скачанный depot…'), 'muted')
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self.qr = QLabel()
        self.qr.setAccessibleName(tr('QR-код входа в Steam'))
        self.qr.hide()
        layout.addWidget(self.qr, 0, Qt.AlignHCenter)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1000)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        layout.addStretch()
        row = QHBoxLayout()
        row.addWidget(button(tr('Закрыть'), self.reject))
        row.addStretch()
        self.stop = button(tr('Остановить загрузку'), self.cancel_download)
        self.stop.hide()
        row.addWidget(self.stop)
        self.check = button(tr('Проверить файлы'), self.verify)
        row.addWidget(self.check)
        self.use = button(tr('Использовать сборку'), self.accept, True)
        self.use.setEnabled(False)
        row.addWidget(self.use)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.detect)
        self.timer.start(3000)
        self.refresh_resume()
        self.detect()

    def start_download(self):
        if self.refresh_resume():
            self.download_to(self.resume_parent)
        else:
            self.choose_download_folder()

    def choose_download_folder(self):
        selected = QFileDialog.getExistingDirectory(self, tr('Куда скачать сборку?'),
                                                     str(self.resume_parent or ''))
        if selected:
            self.download_to(Path(selected))

    def refresh_resume(self):
        available = self.resume_parent is not None and (self.resume_parent/DEPOT_FOLDER).is_dir()
        self.download.setText(tr('Продолжить загрузку' if available else 'Скачать через DepotDownloader'))
        self.download.setToolTip(str(self.resume_parent/DEPOT_FOLDER) if available else '')
        self.change_folder.setVisible(available)
        return available

    def remember_download(self, folder):
        self.resume_parent = folder
        if self.resume_file:
            self.resume_file.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.resume_file, (json.dumps({'folder': str(folder)})+'\n').encode())

    def download_to(self, parent):
        if self.downloader is not None or (self.worker and self.worker.isRunning()):
            return
        executable = bundled_downloader() or shutil.which('DepotDownloader') or shutil.which('depotdownloader')
        if executable is None:
            executable, _ = QFileDialog.getOpenFileName(self, tr('Выбери исполняемый файл DepotDownloader'))
        if not executable:
            return
        parent = parent.resolve()
        if parent.name == DEPOT_FOLDER:
            parent = parent.parent
        target = parent/DEPOT_FOLDER
        if target.resolve() != target:
            self.status.setText(tr('Выбери папку без символической ссылки на каталог загрузки.'))
            return
        lock = QLockFile(str(target.with_suffix('.lock')))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            self.status.setText(tr('Папка недоступна для записи или в неё уже скачивается сборка.'))
            return
        try:
            self.downloader_session = TemporaryDirectory(prefix='solbreaker-depot-')
            self.remember_download(parent)
        except OSError as error:
            lock.unlock()
            if self.downloader_session is not None:
                self.downloader_session.cleanup()
                self.downloader_session = None
            self.status.setText(str(error))
            return
        self.download_lock = lock
        self.invalidate()
        self.download_target = target
        self.download_cancelled = False
        self.download_percent = -1.
        self.download_output = b''
        self.download_error = ''
        self.status.setToolTip('')
        self.qr_rows = None
        self.set_working(True)
        self.manual.hide()
        self.stop.setEnabled(True)
        self.stop.show()
        self.progress.setRange(0, 0)
        self.progress.show()
        self.status.setText(tr('Подключаемся к Steam…'))
        process = self.downloader = QProcess(self)
        process.setWorkingDirectory(self.downloader_session.name)
        process.setProcessChannelMode(QProcess.MergedChannels)
        process.setStandardInputFile(QProcess.nullDevice())
        process.readyReadStandardOutput.connect(self.read_download)
        process.finished.connect(self.download_finished)
        process.errorOccurred.connect(self.download_process_error)
        arguments = ['-app', str(STEAM_APP), '-depot', str(STEAM_DEPOT),
                     '-manifest', str(STEAM_MANIFEST), '-qr', '-dir', str(target)]
        if target.is_dir():
            arguments.append('-validate')
        process.start(str(Path(executable).resolve()), arguments)

    def read_download(self, final=False):
        self.download_output += bytes(self.downloader.readAllStandardOutput())
        lines = self.download_output.split(b'\n')
        remaining = lines.pop()
        if final and remaining:
            lines.append(remaining)
        self.download_output = remaining[-8192:] if not final else b''
        for raw in lines:
            line = raw.decode('utf-8', errors='replace').rstrip('\r')
            if line.startswith('Use the Steam Mobile App to sign in with this QR code:'):
                self.qr_rows = []
                self.status.setText(tr('Отсканируй QR-код в мобильном приложении Steam.'))
                continue
            if self.qr_rows is not None:
                # The Windows console can emit full blocks as OEM byte 0xDB.
                if b'\xdb' in raw:
                    line = raw.decode('cp437').rstrip('\r')
                rows = self.qr_rows
                if QR_ROW.fullmatch(line) and (not rows or len(line) == len(rows[0])):
                    rows.append(line)
                    if len(rows)*2 == len(line):
                        code = QPixmap.fromImage(qr_image(rows))
                        self.qr.setFixedSize(code.size())
                        self.qr.setPixmap(code)
                        self.qr.show()
                        self.qr_rows = None
                    continue
                self.qr_rows = None
            if found := DEPOT_PROGRESS.match(line):
                percent = float(found[1].replace(',', '.'))
                if self.download_percent <= percent <= 100:
                    self.download_percent = percent
            elif line.startswith('Downloading depot '):
                self.qr.hide()
                self.status.setText(tr('Загружаем манифест сборки…'))
            elif line.startswith(('Error', 'Failed', 'Depot', 'App')):
                self.download_error = line[:260]
        if self.download_percent >= 0:
            self.qr.hide()
            self.progress.setRange(0, 1000)
            self.progress.setValue(round(self.download_percent*10))
            self.status.setText(tr('Скачиваем сборку — {percent:.1f}%').format(percent=self.download_percent))

    def download_process_error(self, error):
        if error == QProcess.FailedToStart:
            self.download_error = self.downloader.errorString()
            self.download_finished(-1, QProcess.CrashExit)

    def cancel_download(self):
        if self.downloader is not None:
            self.download_cancelled = True
            self.stop.setEnabled(False)
            self.downloader.kill()

    def download_finished(self, code, exit_status):
        self.read_download(final=True)
        self.downloader.deleteLater()
        self.downloader = None
        self.download_lock.unlock()
        self.download_lock = None
        self.downloader_session.cleanup()
        self.downloader_session = None
        self.stop.hide()
        self.qr.clear()
        self.qr.hide()
        self.manual.show()
        self.set_working(False)
        if self.closing:
            super().reject()
        elif self.download_cancelled:
            self.progress.hide()
            self.status.setText(tr('Загрузка остановлена.'))
        elif code or exit_status != QProcess.NormalExit:
            self.progress.hide()
            detail = self.download_error
            self.status.setText(tr(friendly_error(detail) or
                                   'Steam не завершил загрузку. Проверь соединение и повтори попытку. Можно продолжить через консоль Steam.'))
            self.status.setToolTip(detail)
        else:
            self.folder.setText(str(self.download_target))
            self.verify()

    def set_working(self, active):
        for widget in (self.folder, self.browse, self.check, self.download, self.change_folder):
            widget.setEnabled(not active)
        if not active:
            self.refresh_resume()

    def open_steam(self):
        QApplication.clipboard().setText(DOWNLOAD_COMMAND)
        if QDesktopServices.openUrl(QUrl('steam://open/console')):
            self.status.setText(tr('Команда скопирована. Вставь её в консоль Steam и нажми Enter.'))
        else:
            self.status.setText(tr('Установи Steam и войди в свой аккаунт, затем открой консоль повторно.'))

    def detect(self):
        if self.downloader is None and not self.folder.text():
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
            self.progress.hide()
            self.status.setText(tr('Ожидаем скачанный depot…'))

    def verify(self):
        if self.downloader is not None or (self.worker and self.worker.isRunning()):
            return
        if not self.folder.text().strip():
            self.status.setText(tr('Дождись появления папки depot или выбери её вручную.'))
            return
        self.invalidate()
        self.set_working(True)
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
        self.status.setText(tr('Проверяем SHA-256 файлов — {percent}% · прочитано {mib} МиБ').format(
            percent=value//10, mib=mib))

    def verified(self):
        self.set_working(False)
        if self.closing:
            super().reject()
        elif self.worker.result:
            self.selected_game = self.worker.result['game']
            if self.resume_parent is not None and Path(self.selected_game) == self.resume_parent/DEPOT_FOLDER:
                self.resume_parent = None
                if self.resume_file:
                    try:
                        self.resume_file.unlink(missing_ok=True)
                    except OSError:
                        pass
                self.refresh_resume()
            self.progress.setRange(0, 1000)
            self.progress.setValue(1000)
            self.status.setText(tr('Сборка проверена. Все {files} файлов совпали.').format(files=self.worker.result['files']))
            self.use.setEnabled(True)
        else:
            self.progress.setVisible(False)
            self.status.setText(tr(self.worker.error))

    def accept(self):
        if self.selected_game:
            if self.resume_file:
                from .managed_depot import RECEIPT, record_download
                try:
                    record_download(self.worker.result, self.resume_file.with_name(RECEIPT))
                except OSError as error:
                    self.status.setText(str(error))
                    return
            super().accept()

    def reject(self):
        if self.downloader is not None:
            self.closing = True
            self.cancel_download()
        elif self.worker and self.worker.isRunning():
            self.closing = True
            self.worker.requestInterruption()
        else:
            super().reject()

    def closeEvent(self, event):
        if self.downloader is not None or (self.worker and self.worker.isRunning()):
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)
