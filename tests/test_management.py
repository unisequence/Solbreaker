"""Management command contracts and consistent offline backups."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import zipfile

from solbreaker.management import Job, Manager, Settings, command, friendly_error, log_tail
from solbreaker.management_tasks import backup_state


class ManagementTests(unittest.TestCase):
    def test_failed_job_surfaces_diagnosis_and_keeps_technical_details(self):
        manager = Manager(Settings())
        error = 'OSError: [WinError 112] There is not enough space on the disk'
        manager.job = Job('prepare-client',
                          [sys.executable, '-c', f'raise SystemExit({error!r})'], manager.output)
        manager.job.thread.join(5)
        lines = manager.drain()
        self.assertIn(error, lines)
        self.assertIn('Освободи место', manager.notice)

    def test_battle_launch_failure_does_not_require_the_server_to_exit(self):
        manager = Manager(Settings())
        manager.output.put(json.dumps({
            'event': 'arena_owner_start_failed', 'reason': 'pybullet.error: createCollisionShape failed',
        }))
        manager.drain()
        self.assertIn('Физика карты не загрузилась', manager.notice)
        manager.output.put(json.dumps({'event': 'arena_world_ready'}))
        manager.drain()
        self.assertEqual(manager.notice, 'Готово')

    def test_log_tails_support_powershell_encoding_and_ignore_previous_attempt(self):
        with TemporaryDirectory() as folder:
            log = Path(folder)/'update.log'
            log.write_text('Permission denied', encoding='utf-16')
            self.assertIn('Проверь права', friendly_error(log_tail(log)))
            log.write_text('No space left on device\n', encoding='utf-8')
            offset = log.stat().st_size
            with log.open('a', encoding='utf-8') as stream:
                stream.write('CERTIFICATE_VERIFY_FAILED\n')
            self.assertIn('сертификат', friendly_error(log_tail(log, start=offset)))
            self.assertNotIn('space', log_tail(log, start=offset))

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

    def test_replay_file_is_forwarded_after_the_local_server_starts(self):
        with TemporaryDirectory() as temporary:
            client = Path(temporary)/'client'
            (client/'client').mkdir(parents=True)
            (client/'manifest.json').touch()
            (client/'client/wotblitz.exe').touch()
            manager = Manager(Settings(client=str(client), play_mode='local'))
            replay = str(Path(temporary)/'Пример & бой.wotbreplay')
            server = Mock(running=True, started=False)
            with (patch.object(manager, 'server_status', return_value='Сервер остановлен') as status,
                  patch('solbreaker.management.Job', return_value=server),
                  patch('solbreaker.management.subprocess.Popen') as play):
                manager.run('play', replay=replay)
                play.assert_not_called()
                server.started = True
                status.return_value = 'Сервер работает'
                manager.drain()
            argv = play.call_args.args[0]
            self.assertEqual(argv[argv.index('--replay')+1], replay)
            self.assertIsNone(manager._pending_play)

    def test_windows_server_uses_selected_state_and_owned_stdin(self):
        settings = Settings(state='/tmp/custom data')
        args = command('server', settings, windows=True)
        self.assertEqual(args[args.index('-State')+1], str(Path(settings.state).absolute()))
        self.assertIn('-Owned', args)

    def test_url_profile_is_shared_by_preparation_and_import(self):
        settings = Settings(game='/tmp/game', server_url='https://example.test', proton='/tmp/proton')
        for action in ('prepare-client', 'connect', 'play'):
            args = command(action, settings, windows=False)
            self.assertEqual(args[args.index('--url')+1], settings.server_url)
            self.assertNotIn('--connection', args)

    def test_local_mode_uses_state_profile_with_remote_server_still_saved(self):
        settings = Settings(state='/tmp/local state', game='/tmp/game', proton='/tmp/proton',
                            play_mode='local', connection='/tmp/friend.json', server_url='https://friend.test')
        for action in ('prepare-client', 'connect', 'play'):
            args = command(action, settings, windows=False)
            self.assertEqual(args[args.index('--connection')+1], str(settings.local_profile_path.absolute()))
            self.assertNotIn('--url', args)
        args = command('play', replace(settings, play_mode='remote', server_url=''), windows=False)
        self.assertEqual(args[args.index('--connection')+1], str(Path(settings.connection).absolute()))

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

    def test_map_preparation_starts_the_owned_server_and_reports_progress(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state/'manifest.json').write_text('{}')
            manager = Manager(Settings(state=str(state), play_mode='local'))
            job = Mock(running=True, started=True)
            with patch.dict(os.environ, {'SOLBREAKER_MAP_PREWARM': '1', 'SOLBREAKER_MAP_CACHE_MIB': '1024'}):
                with patch.object(manager, 'server_status', return_value='Сервер остановлен'):
                    with patch('solbreaker.management.Job', return_value=job) as start:
                        manager.run('prepare-maps')
            self.assertEqual(start.call_args.args[0], 'server')
            self.assertIs(manager.server, job)
            self.assertFalse(manager.busy)
            self.assertIsNone(manager.client)
            for event, phase, expected in (
                ('map_preparation_started', None, 'Подготавливаем карту Mines…'),
                ('map_preparation', 'ready', 'Карта Mines подготовлена.'),
                ('map_preparation_paused', None, 'Подготовка карт приостановлена.'),
            ):
                manager.output.put(json.dumps(dict(event=event, map_key='rudniki', phase=phase)))
                manager.drain()
                self.assertEqual(manager.map_preparation_status(), expected)
            manager.output.put(json.dumps(dict(event='map_preparation_started', map_key='fort')))
            manager.drain()
            self.assertEqual(manager.map_preparation_status(),
                             'Карта Mines подготовлена. Подготавливаем карту Fort…')

    def test_map_preparation_is_unavailable_for_remote_connections_and_disabled_cache(self):
        with TemporaryDirectory() as temporary:
            (Path(temporary)/'manifest.json').write_text('{}')
            manager = Manager(Settings(state=temporary, play_mode='remote'))
            with self.assertRaises(ValueError):
                manager.run('prepare-maps')
            manager.settings = replace(manager.settings, play_mode='local')
            with patch.dict(os.environ, {'SOLBREAKER_MAP_PREWARM': '0'}):
                with self.assertRaises(ValueError):
                    manager.run('prepare-maps')

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

    def test_benchmark_can_stop_before_the_measured_phase(self):
        job = Job.__new__(Job)
        job.action = 'benchmark'
        job.started = False
        job.process = Mock()
        job.process.poll.return_value = None
        job.stop()
        self.assertTrue(job.stopping)
        job.process.stdin.write.assert_called_once_with('stop\n')

    def test_early_stop_waits_for_the_actual_server_to_own_stdin(self):
        job = Job.__new__(Job)
        job.action = 'server'
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
