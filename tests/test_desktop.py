"""First-run guidance and explicit local/remote GUI choices."""
import importlib.util
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'Install the GUI extra to exercise Qt')
class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        if os.name == 'nt':
            os.environ.setdefault('QT_QPA_FONTDIR', str(Path(os.environ['WINDIR'])/'Fonts'))
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from solbreaker.management import Settings
        from solbreaker.desktop import Window
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root/'preferences.json'
        Settings(state=str(self.root/'state'), client=str(self.root/'client'),
                 connection=str(self.root/'state/connection.json'), gui_theme='light').save(self.config)
        self.window = Window(self.config)
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self.window.close)

    def test_first_run_has_one_setup_action_and_explains_missing_client(self):
        self.assertEqual(self.window.play_button.text(), 'Настроить игру')
        self.assertTrue(self.window.play_button.isEnabled())
        self.assertIn('Нужно установить', self.window.checks[1][1].text())
        self.window.primary_action()
        self.app.processEvents()
        wizard = self.window.wizard
        self.assertIsNotNone(wizard)
        self.assertEqual(wizard.pages.currentIndex(), 0)
        wizard.advance()
        self.assertEqual(wizard.pages.currentIndex(), 0)
        self.assertTrue(wizard.error_label.isVisible())
        self.assertFalse(wizard.back.isEnabled())
        wizard.previous()
        self.assertEqual(wizard.pages.currentIndex(), 0)
        wizard.reject()

    def test_exact_build_compatibility_caption_in_both_languages(self):
        from PySide6.QtWidgets import QLabel
        from solbreaker.build_info import COMPATIBILITY
        self.assertEqual(COMPATIBILITY, 'Compatible with build 11.21.0.1235_7376187')
        for language in (0, 1):
            self.window.language_picker.setCurrentIndex(language)
            self.app.processEvents()
            captions = [item for item in self.window.findChildren(QLabel)
                        if item.text() == COMPATIBILITY]
            self.assertEqual(len(captions), 2)
    def test_preparation_stage_updates_activity(self):
        import json
        self.window.start_wizard()
        wizard = self.window.wizard
        wizard.update_job([json.dumps(dict(event='setup_progress', message='Копируем ресурсы…'))])
        self.assertEqual(wizard.activity.text(), 'Копируем ресурсы…')
        self.assertIn('Копируем ресурсы…', wizard.details.toPlainText())
        wizard.update_job([json.dumps(dict(event='setup_progress', message='Копируем',
                                          completed=10 << 30, total=20 << 30))])
        self.assertEqual(wizard.progress.value(), 500)
        wizard.update_job([json.dumps(dict(event='setup_progress', message='Настраиваем клиент'))])
        self.assertEqual(wizard.progress.maximum(), 0)
        wizard.reject()

    def test_remote_choice_shows_address_or_file_and_persists_explicit_mode(self):
        self.window.remote.setChecked(True)
        self.window.start_wizard()
        self.app.processEvents()
        wizard = self.window.wizard
        self.assertFalse(wizard.local)
        wizard.by_url.setChecked(True)
        wizard.url.setText('https://example.test')
        settings = wizard.settings()
        self.assertEqual(settings.play_mode, 'remote')
        self.assertFalse(settings.local_connection)
        self.assertEqual(settings.server_url, 'https://example.test')
        wizard.reject()

    def install_client_fixture(self):
        import json
        from dataclasses import replace

        game = self.root/'game'
        game.mkdir()
        (game/'wotblitz.exe').touch()
        client = self.root/'client'
        (client/'client').mkdir(parents=True)
        (client/'client/wotblitz.exe').touch()
        (client/'manifest.json').write_text(json.dumps({'game': str(game), 'server_id': 'remote'}))
        self.window.manager.update_settings(replace(self.window.manager.settings, game=str(game)))
        self.window.sync_fields()

    def test_switching_between_saved_servers_plays_without_opening_setup(self):
        from dataclasses import replace
        from unittest.mock import patch
        from solbreaker.management import Settings

        self.install_client_fixture()
        settings = self.window.manager.settings
        remote = self.root/'friend.json'
        remote.write_text('{"server_id":"remote"}')
        settings.local_profile_path.parent.mkdir()
        settings.local_profile_path.with_name('manifest.json').write_text('{"game":"example"}')
        settings.local_profile_path.write_text('{"server_id":"local"}')
        self.window.manager.update_settings(replace(settings, play_mode='remote', connection=str(remote)))
        self.window.remote.setChecked(True)

        with patch.object(self.window.manager, 'run') as run:
            for mode, radio in (('local', self.window.own), ('remote', self.window.remote)):
                radio.setChecked(True)
                self.assertEqual(self.window.play_button.text(), 'Играть')
                self.window.play_button.click()
                self.assertIsNone(self.window.wizard)
                run.assert_called_once_with('play')
                run.reset_mock()
                saved = Settings.load(self.config)
                self.assertEqual(saved.play_mode, mode)
                self.assertEqual(saved.connection, str(remote))

    def test_installed_client_opens_setup_at_the_missing_server_step(self):
        from unittest.mock import patch
        from solbreaker.desktop import SetupWizard

        self.install_client_fixture()
        with patch.object(SetupWizard, 'search_discovery'):
            for radio, action in ((self.window.remote, 'Выбрать сервер'),
                                  (self.window.own, 'Настроить сервер')):
                radio.setChecked(True)
                self.window.refresh_status()
                self.assertEqual(self.window.play_button.text(), action)
                self.window.play_button.click()
                wizard = self.window.wizard
                self.assertEqual(wizard.pages.currentIndex(), 1)
                self.assertFalse(wizard.back.isVisible())
                wizard.previous()
                self.assertEqual(wizard.pages.currentIndex(), 1)
                wizard.reject()

    def test_selecting_a_discovered_server_uses_the_existing_profile_workflow(self):
        from concurrent.futures import Future
        from solbreaker.discovery import DiscoveredServer

        self.window.remote.setChecked(True)
        self.window.start_wizard()
        wizard = self.window.wizard
        server = DiscoveredServer('Nearby server', '192.168.1.10', 18080, 'test-id', b'{}')
        wizard.lan_scan = Future()
        wizard.lan_scan.set_result([server])
        wizard.show_discovered()
        self.assertIn('Nearby server', wizard.lan_results.itemText(0))
        wizard.choose_discovered(0)
        settings = wizard.settings()
        self.assertTrue(wizard.by_file.isChecked())
        self.assertEqual(settings.server_url, '')
        self.assertEqual(Path(settings.connection).read_bytes(), server.profile)
        wizard.reject()

    def test_theme_switch_is_saved_without_changing_connection(self):
        from solbreaker.management import Settings
        before = self.window.manager.settings.connection
        self.window.theme_picker.setCurrentIndex(1)
        self.app.processEvents()
        saved = Settings.load(self.config)
        self.assertEqual(saved.gui_theme, 'dark')
        self.assertEqual(saved.connection, before)

    def test_home_fits_without_scrolling_and_uses_one_primary_action(self):
        from PySide6.QtWidgets import QScrollArea, QPushButton
        from solbreaker.desktop import LauncherBanner
        for width, height in ((920, 640), (1120, 700), (1280, 740)):
            self.window.resize(width, height)
            self.app.processEvents()
            self.assertEqual(self.window.height(), height)
            home = self.window.stack.widget(0)
            banner = home.findChild(LauncherBanner)
            dock = self.window.play_button.parentWidget()
            self.assertFalse(home.findChildren(QScrollArea))
            self.assertEqual([item for item in home.findChildren(QPushButton) if item.isVisible()],
                             [self.window.play_button])
            self.assertEqual(banner.geometry().left(), dock.geometry().left())
            self.assertEqual(banner.geometry().right(), dock.geometry().right())
            self.assertTrue(home.rect().contains(dock.geometry()),
                            (width, height, home.rect(), dock.geometry()))
            self.assertLess(banner.geometry().bottom(), dock.geometry().top())

    def test_preparation_action_is_secondary_and_local(self):
        from unittest.mock import patch

        state = self.root/'state'
        state.mkdir()
        (state/'manifest.json').write_text('{}')
        with patch.dict(os.environ, {'SOLBREAKER_MAP_PREWARM': '1', 'SOLBREAKER_MAP_CACHE_MIB': '1024'}):
            with patch.object(self.window.manager, 'server_status', return_value='Сервер остановлен'):
                self.window.own.setChecked(True)
                self.window.refresh_status()
                self.assertTrue(self.window.prepare_maps_button.isVisible())
                self.assertTrue(self.window.prepare_maps_button.isEnabled())
                self.assertTrue(self.window.play_button.isEnabled())
                self.window.resize(920, 640)
                self.app.processEvents()
                dock = self.window.play_button.parentWidget()
                self.assertTrue(self.window.stack.widget(0).rect().contains(dock.geometry()))
                with patch.object(self.window, 'execute') as execute:
                    self.window.prepare_maps_button.click()
                    execute.assert_called_once_with('prepare-maps')
                self.window.remote.setChecked(True)
                self.assertFalse(self.window.prepare_maps_button.isVisible())

    def test_small_window_icon_has_a_contrasting_background(self):
        from solbreaker.desktop import application_icon
        image = application_icon().pixmap(16, 16).toImage()
        colors = {image.pixelColor(x, y).rgba() for x in range(16) for y in range(16)}
        self.assertGreater(len(colors), 4)
        background = image.pixelColor(3, 3)
        self.assertGreater(background.alpha(), 200)
        self.assertLess(background.lightness(), 100)

    def test_automatic_login_checkbox_is_saved_immediately(self):
        from solbreaker.management import Settings
        self.window.remember_login.setChecked(False)
        self.app.processEvents()
        self.assertFalse(Settings.load(self.config).remember_login)
        self.window.remember_login.setChecked(True)
        self.assertTrue(Settings.load(self.config).remember_login)

    def test_english_switch_preserves_processes_edits_and_selected_page(self):
        from unittest.mock import Mock
        from solbreaker.management import Settings
        manager = self.window.manager
        self.addCleanup(setattr, manager, 'server', None)
        server = Mock(running=True, started=True)
        manager.server = server
        self.window.fields['server_name'].setText('My test server')
        lighting = self.window.fields['map_lighting']
        lighting.setCurrentIndex(lighting.findData('evening'))
        self.window.log.setPlainText('raw diagnostic log')
        self.window.select(1)
        self.window.resize(1000, 680)
        self.window.language_picker.setCurrentIndex(1)
        self.app.processEvents()
        self.assertIs(self.window.manager, manager)
        self.assertIs(manager.server, server)
        server.stop.assert_not_called()
        self.assertEqual(self.window.stack.currentIndex(), 1)
        self.assertEqual(self.window.heading.text(), 'My server')
        self.assertEqual(self.window.log.toPlainText(), 'raw diagnostic log')
        self.assertEqual(self.window.fields['server_name'].text(), 'My test server')
        self.assertEqual(self.window.fields['map_lighting'].currentText(), 'Sunset')
        self.assertEqual(self.window.server_label.text(), 'Server running')
        self.assertEqual(self.window.size().width(), 1000)
        saved = Settings.load(self.config)
        self.assertEqual(saved.gui_language, 'en')
        self.assertEqual(saved.server_name, 'My test server')
        self.assertEqual(saved.map_lighting, 'evening')
        manager.server = None
        self.window.language_picker.setCurrentIndex(0)
        self.app.processEvents()
        self.assertEqual(self.window.heading.text(), 'Мой сервер')
        self.assertEqual(Settings.load(self.config).gui_language, 'ru')

    def test_english_first_run_wizard_and_download_dialog(self):
        import json
        from PySide6.QtWidgets import QLabel, QPushButton
        from solbreaker.desktop_depot import DepotDialog
        self.window.language_picker.setCurrentIndex(1)
        self.app.processEvents()
        self.assertEqual(self.window.play_button.text(), 'Set up game')
        self.assertEqual([item.text() for item in self.window.nav], ['Game', 'My server', 'Settings', 'Logs'])
        self.window.remote.setChecked(True)
        self.window.start_wizard()
        wizard = self.window.wizard
        self.assertEqual(wizard.windowTitle(), 'Game setup — Solbreaker')
        self.assertEqual(wizard.next.text(), 'Next')
        wizard.advance()
        self.assertEqual(wizard.error_label.text(), 'Choose the game folder containing wotblitz.exe and Data.')
        wizard.show_step(1)
        self.assertEqual(wizard.step_label.text(), 'Step 2 of 3 — server')
        self.assertEqual(wizard.by_url.text(), 'I have a server address')
        wizard.update_job([json.dumps(dict(event='setup_progress', message='Копируем ресурсы игры — 10 из 20 МиБ'))])
        self.assertEqual(wizard.activity.text(), 'Copying game resources — 10 of 20 MiB')
        dialog = DepotDialog(wizard)
        self.assertEqual(dialog.check.text(), 'Verify files')
        dialog.update_progress(500, 123)
        self.assertEqual(dialog.status.text(), 'Verifying file SHA-256 hashes — 123 MiB read')
        for container in (self.window, wizard, dialog):
            texts = [widget.text() for widget in container.findChildren(QLabel)]
            texts += [widget.text() for widget in container.findChildren(QPushButton)]
            self.assertFalse(any(any('а' <= char.lower() <= 'я' or char.lower() == 'ё' for char in text)
                                 for text in texts), texts)
        dialog.reject()
        wizard.reject()

    def test_english_home_fits_supported_window_sizes(self):
        self.window.language_picker.setCurrentIndex(1)
        self.app.processEvents()
        self.test_home_fits_without_scrolling_and_uses_one_primary_action()

    def test_english_selection_survives_reopening_and_old_config_defaults_to_russian(self):
        from solbreaker.desktop import Window
        from solbreaker.management import Settings
        self.assertEqual(Settings().gui_language, 'ru')
        with self.assertRaises(ValueError):
            Settings(gui_language='de')
        self.window.language_picker.setCurrentIndex(1)
        self.window.close()
        reopened = Window(self.config)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.play_button.text(), 'Set up game')
        self.assertEqual(reopened.language_picker.currentData(), 'en')

    def test_catalog_covers_gui_source_strings_and_formats_dynamic_messages(self):
        import ast
        import re
        from solbreaker.desktop_i18n import ENGLISH, tr
        from solbreaker import desktop
        source = Path(desktop.__file__).parent
        for filename in ('desktop.py', 'desktop_depot.py'):
            tree = ast.parse((source/filename).read_text(encoding='utf-8'))
            strings = {node.value for node in ast.walk(tree)
                       if isinstance(node, ast.Constant) and isinstance(node.value, str)
                       and re.search('[А-Яа-яЁё]', node.value)}
            self.assertEqual(strings-set(ENGLISH)-{'Русский'}, set(), filename)
        self.window.language_picker.setCurrentIndex(1)
        self.assertEqual(tr('Сервер завершился с ошибкой 17. Открой журнал'),
                         'Server exited with code 17. Open the log for details')
        self.assertEqual(tr('Укажи папку клиента'), 'Choose a client folder')
        self.assertEqual(tr('Порт должен быть целым числом от 1024 до 65535'),
                         'The port must be an integer from 1024 to 65535')

    def test_language_switch_preserves_pending_name_when_server_manifest_has_old_name(self):
        import json
        state = self.root/'state'
        state.mkdir()
        manifest = state/'manifest.json'
        manifest.write_text(json.dumps({'server_name': 'Applied server name'}))
        self.window.fields['server_name'].setText('Pending server name')
        self.window.language_picker.setCurrentIndex(1)
        self.app.processEvents()
        self.assertEqual(self.window.fields['server_name'].text(), 'Pending server name')
        self.assertEqual(json.loads(manifest.read_text())['server_name'], 'Applied server name')
        self.window.language_picker.setCurrentIndex(0)
        self.app.processEvents()
        self.assertEqual(self.window.fields['server_name'].text(), 'Pending server name')

    def test_open_wizard_blocks_language_switch_without_losing_its_inputs(self):
        self.window.start_wizard()
        self.app.processEvents()
        wizard = self.window.wizard
        wizard.fields['game'].setText('/pending/game')
        self.assertFalse(self.window.language_picker.isEnabled())
        self.window.refresh_status()
        self.assertFalse(self.window.language_picker.isEnabled())
        self.window.language_picker.setCurrentIndex(1)
        self.app.processEvents()
        self.assertIs(self.window.wizard, wizard)
        self.assertEqual(self.window.language_picker.currentData(), 'ru')
        self.assertEqual(self.window.manager.settings.gui_language, 'ru')
        self.assertEqual(wizard.fields['game'].text(), '/pending/game')
        self.assertEqual(wizard.next.text(), 'Далее')
        self.assertTrue(wizard.isVisible())
        wizard.reject()
        self.app.processEvents()
        self.assertTrue(self.window.language_picker.isEnabled())
        self.window.language_picker.setCurrentIndex(1)
        self.assertEqual(self.window.manager.settings.gui_language, 'en')
