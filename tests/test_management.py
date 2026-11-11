"""Management command contracts and consistent offline backups."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import zipfile

from solbreaker.management import Job, Manager, Settings, command
from solbreaker.management_tasks import backup_state


class ManagementTests(unittest.TestCase):
    def test_windowless_gui_uses_console_python_for_captured_jobs(self):
        with patch('solbreaker.management.sys.executable', str(Path('runtime')/'pythonw.exe')):
            args = command('prepare-client', Settings(game='/tmp/game', connection='/tmp/profile'), windows=True)
        self.assertEqual(Path(args[0]).name, 'python.exe')

    def test_paths_and_shell_metacharacters_remain_single_arguments(self):
        settings = Settings(game='/tmp/owned game; echo surprise', client='/tmp/client folder',
                            connection='/tmp/server profile.json', proton='/tmp/Proton/proton')
        args = command('prepare-client', settings, windows=False)
        self.assertEqual(args[args.index('--game')+1], str(Path(settings.game).absolute()))
        self.assertEqual(args[args.index('--connection')+1], str(Path(settings.connection).absolute()))
        self.assertNotIn('sh', args)

    def test_windows_server_uses_selected_state_and_owned_stdin(self):
        settings = Settings(state='/tmp/custom data')
        args = command('server', settings, windows=True)
        self.assertEqual(args[args.index('-State')+1], str(Path(settings.state).absolute()))
        self.assertIn('-Owned', args)

    def test_url_profile_is_shared_by_preparation_and_import(self):
        settings = Settings(game='/tmp/game', server_url='https://example.test', proton='/tmp/proton')
        for action in ('prepare-client', 'connect'):
            args = command(action, settings, windows=False)
            self.assertEqual(args[args.index('--url')+1], settings.server_url)
            self.assertNotIn('--connection', args)

    def test_settings_validation_and_round_trip(self):
        for value in (True, 1023, 65536, 'abc'):
            with self.assertRaises(ValueError):
                Settings(http_port=value)
        with TemporaryDirectory() as temporary:
            path = Path(temporary)/'settings.json'
            settings = Settings(http_port='49080')
            settings.save(path)
            self.assertEqual(Settings.load(path), settings)
            path.write_text(json.dumps({'unknown': 'value'}))
            with self.assertRaises(ValueError):
                Settings.load(path)

    def test_unprepared_play_does_not_create_client_directory(self):
        with TemporaryDirectory() as temporary:
            client = Path(temporary)/'absent'
            manager = Manager(Settings(client=str(client)))
            with self.assertRaises(ValueError):
                manager.run('play')
            self.assertFalse(client.exists())

    def test_same_settings_can_be_saved_while_owned_server_runs(self):
        manager = Manager(Settings())
        manager.server = type('Server', (), {'running': True})()
        manager.update_settings(replace(manager.settings))
        with self.assertRaises(ValueError):
            manager.update_settings(replace(manager.settings, state='/tmp/other'))

    def test_backup_merges_sqlite_wal_and_keeps_credentials(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root/'state'
            private = state/'private'
            private.mkdir(parents=True)
            (private/'login.pem').write_text('fixture key')
            (state/'manifest.json').write_text('{}')
            database = sqlite3.connect(private/'accounts.sqlite3')
            self.addCleanup(database.close)
            database.execute('PRAGMA journal_mode=WAL')
            database.execute('CREATE TABLE accounts (id INTEGER)')
            database.execute('INSERT INTO accounts VALUES (1)')
            database.commit()
            with patch('solbreaker.management_tasks.ensure_lab', return_value=(state, {})):
                result = backup_state(state, root/'backups')
            database.close()
            archive = Path(result['backup'])
            if os.name == 'posix':
                self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(bundle.read('private/login.pem'), b'fixture key')
                restored = root/'restored.sqlite3'
                restored.write_bytes(bundle.read('private/accounts.sqlite3'))
                self.assertNotIn('private/accounts.sqlite3-wal', bundle.namelist())
            copied = sqlite3.connect(restored)
            try:
                self.assertEqual(copied.execute('SELECT id FROM accounts').fetchone(), (1,))
            finally:
                copied.close()

    def test_server_command_does_not_require_a_client_directory(self):
        args = command('server', Settings(client=''), windows=False)
        self.assertIn('--stop-on-stdin', args)

    def test_early_stop_waits_for_the_actual_server_to_own_stdin(self):
        job = Job.__new__(Job)
        job.process = Mock()
        job.process.poll.return_value = None
        job.started = False
        job.stop()
        job.process.stdin.write.assert_not_called()
        self.assertTrue(job._stop_requested)
        job.started = True
        job.stop()
        job.process.stdin.write.assert_called_once_with('stop\n')
        job.process.stdin.close.assert_called_once()
