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

    def test_verified_download_survives_failed_setup_and_is_removed_after_setup(self):
        from solbreaker import managed_depot
        from solbreaker.resource_copy import resource_tree_facts

        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            original = root/'Downloads'/managed_depot.DEPOT_FOLDER
            metadata = original/managed_depot.METADATA
            metadata.mkdir(parents=True)
            (metadata/'depot.config').write_bytes(b'downloader data')
            binary = original/'wotblitz.exe'
            binary.write_bytes(b'game binary')
            catalog = root/'catalog.gz'
            catalog.write_bytes(gzip.compress(json.dumps([['wotblitz.exe', 11, 'digest']]).encode()))
            record = {'source': str(original), 'fingerprint': resource_tree_facts(
                original, include_change_time=False)['fingerprint']}
            client = root/'Install/client'
            managed = managed_depot.location(record, client, windows=True)
            check_game = lambda path: {'game': str(path)}
            with patch.object(managed_depot, 'CATALOG', catalog), \
                 patch.object(managed_depot, 'inspect_game', side_effect=check_game), \
                 patch.object(managed_depot, 'validate_snapshot'), \
                 patch.object(managed_depot, 'ensure_lab', return_value=(client, {'game': str(original)})) as lab:
                managed_depot.stage(record, managed)
                self.assertTrue(binary.samefile(managed/'wotblitz.exe'))
                self.assertFalse((managed/managed_depot.METADATA).exists())
                with self.assertRaisesRegex(ValueError, 'ещё использует'):
                    managed_depot.cleanup(record, managed, client)
                self.assertTrue(original.is_dir())
                lab.return_value = client, {'game': str(managed)}
                managed_depot.cleanup(record, managed, client)
                self.assertFalse(original.exists())
                self.assertEqual((managed/'wotblitz.exe').read_bytes(), b'game binary')

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

    def test_depot_shows_qr_in_utf8_and_windows_console_encoding(self):
        from PySide6.QtWidgets import QApplication
        from solbreaker.desktop_depot import DepotDialog
        app = QApplication.instance() or QApplication([])
        size = 29
        rows = ['  '*size]*4 + ['  '*4+'██'*(size-8)+'  '*4]*(size-8) + ['  '*size]*4
        for encoding in ('utf-8', 'cp437'):
            with self.subTest(encoding=encoding), patch('solbreaker.desktop_depot.find_depots', return_value=[]):
                dialog = DepotDialog(None)
                process = Mock()
                process.readAllStandardOutput.return_value = (
                    b'Use the Steam Mobile App to sign in with this QR code:\r\n'
                    + ('\r\n'.join(rows)+'\r\n').encode(encoding))
                dialog.downloader = process
                dialog.read_download()
                self.assertFalse(dialog.qr.isHidden())
                self.assertFalse(dialog.qr.pixmap().isNull())
                dialog.downloader = None
                dialog.deleteLater()
                app.processEvents()

    def test_resume_remembers_the_depot_folder_and_reuses_it(self):
        from PySide6.QtWidgets import QApplication
        from solbreaker.desktop_depot import DEPOT_FOLDER, DepotDialog
        app = QApplication.instance() or QApplication([])
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            depot = root/'downloads'/DEPOT_FOLDER
            depot.mkdir(parents=True)
            receipt = root/'runtime/depot-download.json'
            tool = root/'DepotDownloader.exe'
            tool.touch()
            with patch('solbreaker.desktop_depot.find_depots', return_value=[]), \
                 patch('solbreaker.desktop_depot.bundled_downloader', return_value=str(tool)), \
                 patch('solbreaker.desktop_depot.QProcess') as child:
                first = DepotDialog(None, resume_file=receipt)
                first.download_to(depot)
                first.download_lock.unlock()
                first.downloader_session.cleanup()
                first.downloader = None
                first.deleteLater()

                reopened = DepotDialog(None, resume_file=receipt)
                with patch('solbreaker.desktop_depot.QFileDialog.getExistingDirectory') as choose:
                    reopened.start_download()
                    choose.assert_not_called()
                args = child.return_value.start.call_args.args[1]
                self.assertEqual(args[args.index('-dir')+1], str(depot))
                self.assertIn('-validate', args)
                reopened.download_lock.unlock()
                reopened.downloader_session.cleanup()
                reopened.downloader = None
                reopened.worker = Mock(result={'game': str(depot), 'files': 1})
                reopened.verified()
                self.assertFalse(receipt.exists())
                reopened.deleteLater()
                app.processEvents()
