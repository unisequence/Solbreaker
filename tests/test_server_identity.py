"""Server naming reaches native locale overlays without changing region IDs."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from solbreaker import dvpl
from solbreaker.connection import connect_client
from solbreaker.localization import additional_labels
from solbreaker.management import Settings, command
from solbreaker.server_identity import configure_name, server_name, validate_name


class ServerIdentityTests(unittest.TestCase):
    def test_default_unicode_and_invalid_names(self):
        self.assertEqual(server_name({}), 'Solbreaker')
        self.assertEqual(validate_name('Танковый клуб «Уни»'), 'Танковый клуб «Уни»')
        for value in ('', ' spaced', 'x\nname', 'x\0name', 'x\u202ename', 'x'*49, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_name(value)

    def test_server_localizations_use_configured_name_for_both_region_keys(self):
        for locale in ('ru', 'en', 'de'):
            labels = yaml.safe_load(additional_labels(locale, 'Танковый клуб'))
            self.assertEqual(labels['Local PoC'], 'Танковый клуб')
            self.assertEqual(labels['Solbreaker'], 'Танковый клуб')

    def test_launcher_passes_name_as_one_argument(self):
        for windows in (False, True):
            args = command('server-name', Settings(server_name='Уни & друзья'), windows=windows)
            self.assertEqual(args[-2:], ['--name', 'Уни & друзья'])

    def test_rename_preserves_manifest_and_updates_managed_profile(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'private').mkdir()
            manifest = dict(game='owned', sentinel='preserved')
            path = root/'manifest.json'
            path.write_text(json.dumps(manifest))
            profile = dict(server_id='fingerprint', version=1)
            (root/'connection.json').write_text(json.dumps(profile))
            with patch('solbreaker.connection.ensure_lab', return_value=(root, manifest)), \
                    patch('solbreaker.connection.connection_profile', return_value=profile):
                configure_name(root, 'Уни & друзья')
            self.assertEqual(json.loads(path.read_text()), dict(manifest, server_name='Уни & друзья'))
            saved = json.loads((root/'connection.json').read_text())
            self.assertEqual(saved, dict(server_id='fingerprint', version=1, server_name='Уни & друзья'))

    def test_running_server_rejects_rename_before_writing(self):
        from solbreaker.storage import account_access
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'private').mkdir()
            path = root/'manifest.json'
            path.write_text('{}')
            with account_access(root/'private'), self.assertRaises(ValueError):
                configure_name(root, 'Changed')
            self.assertEqual(path.read_text(), '{}')

    def test_old_and_new_public_profiles_import_with_stable_region_identity(self):
        from solbreaker.build_info import SERVER_NAME
        from solbreaker.network import defaults
        manifest = dict(version='pinned', exe_sha256='sha', http_port=18080, login_port=20016)
        network = defaults(manifest)['advertise']
        for extra in ({}, {'server_name': 'Уни & друзья'}):
            with TemporaryDirectory() as temporary:
                root = Path(temporary)
                profile = dict(version=1, game_version='pinned', exe_sha256='sha',
                               server_id='id', advertise=network, login_public_key='key', **extra)
                path = root/'connection.json'
                path.write_text(json.dumps(profile))
                with patch('solbreaker.connection.ensure_lab', return_value=(root, manifest)), \
                        patch('solbreaker.connection.key_id', return_value='id'), \
                        patch('solbreaker.connection.configure', return_value={}) as configure:
                    connect_client(root, path)
                self.assertEqual(configure.call_args.kwargs['server_name'], extra.get('server_name', SERVER_NAME))

    def test_native_locale_overlay_preserves_other_labels_and_retries(self):
        from solbreaker.client_ui import CONSUMABLE_STRINGS, install_server_name
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = dict(game=str(root/'game'), server_name='Уни & друзья')
            (root/'game').mkdir()
            (root/'manifest.json').write_text(json.dumps(manifest))
            for relative in CONSUMABLE_STRINGS:
                path = root/'client/Data'/relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(dvpl.encode(yaml.safe_dump({'Local PoC': 'Solbreaker', 'Other': 'Keep me'}).encode()))
            with patch('solbreaker.connection.ensure_lab', return_value=(root, manifest)):
                install_server_name(root)
                install_server_name(root)
            for relative in CONSUMABLE_STRINGS:
                labels = yaml.safe_load(dvpl.read(root/'client/Data'/relative))
                self.assertEqual(labels, {'Local PoC': 'Уни & друзья', 'Solbreaker': 'Уни & друзья', 'Other': 'Keep me'})
