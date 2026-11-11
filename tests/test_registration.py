"""Registration validation, uniqueness and async commit races."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from solbreaker.auth import LocalAccounts
from solbreaker.storage import RegistrationError


class RegistrationNavigationTests(unittest.TestCase):
    def test_7x7_notification_uses_its_native_close_event(self):
        import yaml
        from solbreaker import dvpl
        from solbreaker.client_ui import patched_resource, NOTIFICATION_7X7
        source = {'Prototypes': [{'name': '7x7Enabled', 'components': {
            'UIDataParamsComponent': {'events': ['CLOSE_NOTIFICATION']}}}]}
        with patch('solbreaker.client_ui.dvpl.read', return_value=yaml.safe_dump(source).encode()):
            document = yaml.safe_load(dvpl.decode(patched_resource(Path('source'), NOTIFICATION_7X7)))
        components = document['Prototypes'][0]['components']
        self.assertIn('_INIT,~res:', components['UIAnimationComponent']['animations'])
        self.assertEqual(components['UIDataParamsComponent']['events'], ['CLOSE_NOTIFICATION'])

    def test_welcome_new_game_opens_the_email_registration_form(self):
        import yaml
        from solbreaker import dvpl
        from solbreaker.client_registration import resources, _controls, FORM, ACTIONS, COUNTRY, PRELOGIN, PRELOGIN_ACTIONS
        documents = {
            FORM: {'name': 'Email', 'components': {'UIDataParamsComponent': {'args': {}}}},
            COUNTRY: {'Prototypes': [{'components': {}}]},
            PRELOGIN: {'components': {'UIChildComponent': {'eventActions': [
                ['NEW_GAME_PRESSED', 'NEW_GAME_PRESSED', ''],
                ['LOGIN_PRESSED', 'LOGIN_PRESSED', '']]}}},
        }
        def read(path):
            name = path.relative_to(Path('game/Data')).as_posix()
            return b'action SAVE {}' if name in (ACTIONS, PRELOGIN_ACTIONS) else yaml.safe_dump(documents[name]).encode()
        with patch('solbreaker.client_registration.dvpl.read', side_effect=read):
            output = resources(Path('game'))
        prelogin = yaml.safe_load(dvpl.decode(output[PRELOGIN]))
        actions = [a for node in _controls(prelogin) for a in node.get('eventActions', [])]
        self.assertIn(['NEW_GAME_PRESSED', 'SOLBREAKER_CREATE_ACCOUNT', ''], actions)
        self.assertIn(['LOGIN_PRESSED', 'LOGIN_PRESSED', ''], actions)
        script = dvpl.decode(output[PRELOGIN_ACTIONS]).decode()
        self.assertLess(script.index('Event("LOGIN_PRESSED")'), script.index('Event("NEW_ACCOUNT_PRESSED")'))


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.accounts = LocalAccounts(Path(temporary.name), 'http://127.0.0.1:18080', autocreate=False)
        self.addCleanup(self.accounts.close)

    async def test_email_password_create_account_and_allow_case_insensitive_login(self):
        account = await self.accounts.register(' Pilot@Example.test ', 'sample-password', 'pilot')
        self.assertEqual((account.username, account.nickname), ('pilot@example.test', 'pilot'))
        self.assertEqual(self.accounts.store.authenticate('PILOT@EXAMPLE.TEST', 'sample-password').id, account.id)
        self.assertIsNone(self.accounts.store.authenticate(account.username, 'wrong-password'))
        row = self.accounts.store.db.execute('SELECT password_hash FROM accounts WHERE id=?', (account.id,)).fetchone()
        self.assertNotEqual(row['password_hash'], b'sample-password')

    async def test_invalid_inputs_do_not_start_password_work_or_resolve_dns(self):
        with patch('socket.getaddrinfo', side_effect=AssertionError('registration must not resolve email DNS')):
            for email in (None, '', 'pilot', 'a@@example.test', 'a..b@example.test', 'a@-bad.test', 'a@host', 'a@host.1'):
                with self.subTest(email=email), self.assertRaises(RegistrationError):
                    await self.accounts.register(email, 'sample-password', 'pilot')
            for value in (None, '', 'x'*257):
                with self.assertRaises(RegistrationError):
                    await self.accounts.register('pilot@example.test', value, 'pilot')
        self.assertIsNone(self.accounts._password_pool)

    async def test_existing_email_is_reported_and_password_is_preserved(self):
        account = await self.accounts.register('pilot@example.test', 'original-password', 'pilot')
        with self.assertRaises(RegistrationError) as error:
            await self.accounts.register('PILOT@example.test', 'replacement-password', 'pilot')
        self.assertEqual(error.exception.code, 'email_exists')
        self.assertEqual(self.accounts.store.authenticate(account.username, 'original-password').id, account.id)
        self.assertIsNone(self.accounts.store.authenticate(account.username, 'replacement-password'))

    async def test_existing_nickname_is_reported_for_another_email(self):
        await self.accounts.register('pilot@example.test', 'sample-password', 'pilot')
        with self.assertRaises(RegistrationError) as error:
            await self.accounts.register('pilot@another.test', 'sample-password', 'pilot')
        self.assertEqual(error.exception.code, 'nickname_exists')
        self.assertIn('pilot', str(error.exception))

    async def test_concurrent_same_email_commits_only_once(self):
        results = await asyncio.gather(*(self.accounts.register('pilot@example.test', 'sample-password', 'pilot')
                                        for _ in range(2)), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, RegistrationError) for result in results), 1)
        rows = self.accounts.store.db.execute('SELECT count(*) FROM accounts WHERE username=?',
                                              ('pilot@example.test',)).fetchone()[0]
        self.assertEqual(rows, 1)

    async def test_concurrent_nickname_collision_commits_only_once(self):
        results = await asyncio.gather(self.accounts.register('pilot-one@example.test', 'sample-password', 'pilot_one'),
                                       self.accounts.register('pilot_one@another.test', 'sample-password', 'pilot_one'),
                                       return_exceptions=True)
        errors = [result for result in results if isinstance(result, RegistrationError)]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, 'nickname_exists')

    async def test_legacy_duplicate_nicknames_do_not_require_destructive_migration(self):
        store = self.accounts.store
        store.authenticate('old@one.test', 'sample-password', autocreate=True)
        store.authenticate('old@two.test', 'sample-password', autocreate=True)
        with self.assertRaises(RegistrationError) as error:
            await self.accounts.register('old@three.test', 'sample-password', 'old')
        self.assertEqual(error.exception.code, 'nickname_exists')
        self.assertEqual(store.db.execute('SELECT count(*) FROM accounts WHERE nickname=?', ('old',)).fetchone()[0], 2)

    async def test_native_registration_fields_and_duplicate_response(self):
        fields = {'login': ['native@example.test'], 'password': ['sample-password'],
                  'name': ['Solbreaker'], 'game': ['wotb']}
        response = await self.accounts.handle_async('POST', '/registration/api/v3/account/basic/', fields, {})
        self.assertEqual(response[0], 200)
        self.assertEqual(response[1]['status'], 'ok')
        duplicate = await self.accounts.handle_async('POST', '/registration/api/v3/account/basic/', fields, {})
        self.assertEqual(duplicate[0], 400)
        self.assertIn('почта', duplicate[1]['error'])

    async def test_chosen_nickname_preserves_case_and_rejects_case_insensitive_duplicate(self):
        account = await self.accounts.register('named@example.test', 'sample-password', 'Steel_Fox')
        self.assertEqual(account.nickname, 'Steel_Fox')
        with self.assertRaises(RegistrationError) as error:
            await self.accounts.register('other@example.test', 'sample-password', 'steel_fox')
        self.assertEqual(error.exception.code, 'nickname_exists')
        for nickname in ('ab', 'x'*25, 'bad name', 'Танк', ''):
            with self.subTest(nickname=nickname), self.assertRaises(RegistrationError):
                await self.accounts.register('unused@example.test', 'sample-password', nickname)

    async def test_native_name_precheck_and_commit_report_uniqueness_on_submission(self):
        await self.accounts.register('owner@example.test', 'sample-password', 'Steel_Fox')
        response = self.accounts.handle('GET', '/personal/account/nicknames/steel_fox/', {}, {})
        self.assertEqual(response[0], 200)
        fields = {'login': ['second@example.test'], 'password': ['sample-password'], 'name': ['steel_fox']}
        response = await self.accounts.handle_async('POST', '/registration/api/v3/account/basic/', fields, {})
        self.assertEqual(response[0], 400)
        self.assertIn('занято', response[1]['error'])
        fields['name'] = []
        response = await self.accounts.handle_async('POST', '/registration/api/v3/account/basic/', fields, {})
        self.assertEqual(response[0], 400)
