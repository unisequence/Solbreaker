"""Pinned Steam selection, incomplete downloads and background verification."""
import gzip
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import time
import unittest
from unittest.mock import Mock, patch

from solbreaker.steam_depot import DOWNLOAD_COMMAND, bundled_downloader, find_depots, verify_depot


class SteamDepotTests(unittest.TestCase):
    def test_command_requests_the_pinned_manifest(self):
        self.assertEqual(DOWNLOAD_COMMAND, 'download_depot 444200 444202 7369903689024228557')

    @unittest.skipUnless(sys.platform == 'linux', 'Linux executable permissions')
    def test_bundled_downloader_repairs_permissions_from_old_updater(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root/'tools/depotdownloader/DepotDownloader'
            self.assertIsNone(bundled_downloader(root))
            path.parent.mkdir(parents=True)
            path.write_bytes(b'bundled executable')
            path.chmod(0o644)
            self.assertEqual(bundled_downloader(root), str(path))
            self.assertTrue(path.stat().st_mode & 0o100)

    def test_discovery_keeps_the_exact_depot_folder(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            depot = root/'steamapps/content/app_444200/depot_444202'
            depot.mkdir(parents=True)
            with patch('solbreaker.steam_depot.steam_roots', return_value=[root]):
                self.assertEqual(find_depots(), [depot])

    def test_verification_rejects_same_size_corruption_and_missing_files(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root/'game'
            game.mkdir()
            original = b'expected-content'
            file = game/'resource'
            file.write_bytes(original)
            catalog = root/'files.gz'
            catalog.write_bytes(gzip.compress(json.dumps([
                ['resource', len(original), hashlib.sha256(original).hexdigest()]
            ]).encode()))
            with patch('solbreaker.steam_depot.inspect_game', return_value={'game': str(game)}):
                self.assertEqual(verify_depot(game, catalog=catalog)['files'], 1)
                self.assertEqual(file.read_bytes(), original)
                file.write_bytes(b'x'*len(original))
                with self.assertRaisesRegex(ValueError, 'Хеш'):
                    verify_depot(game, catalog=catalog)
                file.unlink()
                with self.assertRaisesRegex(ValueError, 'Загрузка'):
                    verify_depot(game, catalog=catalog)

    def test_closing_verification_keeps_the_event_loop_responsive(self):
        from PySide6.QtWidgets import QApplication
        from solbreaker.desktop_depot import DepotDialog
        app = QApplication.instance() or QApplication([])
        called = []

        def verify(game, *, progress, cancelled):
            called.append(True)
            while not cancelled():
                time.sleep(.01)
            raise InterruptedError('Проверка остановлена')

        with patch('solbreaker.desktop_depot.find_depots', return_value=[]), \
                patch('solbreaker.desktop_depot.verify_depot', side_effect=verify):
            dialog = DepotDialog(None)
            dialog.show()
            dialog.folder.setText('owned depot')
            dialog.verify()
            deadline = time.monotonic()+3
            while not called and time.monotonic()<deadline:
                app.processEvents()
                time.sleep(.01)
            self.assertTrue(called)
            dialog.reject()
            while dialog.worker.isRunning() and time.monotonic()<deadline:
                app.processEvents()
                time.sleep(.01)
            app.processEvents()
            self.assertFalse(dialog.worker.isRunning())
            self.assertFalse(dialog.isVisible())
            self.assertFalse(dialog.selected_game)

    def test_depot_output_handles_split_lines_and_out_of_order_percentages(self):
        from PySide6.QtWidgets import QApplication
        from solbreaker.desktop_depot import DepotDialog
        app = QApplication.instance() or QApplication([])
        with patch('solbreaker.desktop_depot.find_depots', return_value=[]):
            dialog = DepotDialog(None)
        process = Mock()
        process.readAllStandardOutput.side_effect = [b' 61,', b'25% Data/file\r\n 43.50% Data/other\n']
        dialog.downloader = process
        dialog.read_download()
        self.assertEqual(dialog.download_percent, -1.)
        dialog.read_download()
        self.assertEqual(dialog.download_percent, 61.25)
        self.assertEqual(dialog.progress.value(), 612)
        dialog.downloader = None
        dialog.deleteLater()
        app.processEvents()
