"""Pinned Steam selection, incomplete downloads and background verification."""
import gzip
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from solbreaker.steam_depot import DOWNLOAD_COMMAND, find_depots, verify_depot


class SteamDepotTests(unittest.TestCase):
    def test_command_requests_the_pinned_manifest(self):
        self.assertEqual(DOWNLOAD_COMMAND, 'download_depot 444200 444202 7369903689024228557')

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
