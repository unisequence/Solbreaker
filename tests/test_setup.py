"""Installer preflight and resumable setup regressions."""
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.setup import install, parser


class SetupTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.game = self.root/'owned game'
        self.game.mkdir()

    def options(self, *extra):
        return parser().parse_args(['--game', str(self.game), '--state', str(self.root/'state'),
                                    '--skip-build', '--skip-dependencies', *extra])

    def test_additional_client_locales_keep_stock_labels_and_get_solbreaker_keys(self):
        import yaml
        from solbreaker import client_ui, dvpl

        strings = self.game/'Data/Strings'
        strings.mkdir(parents=True)
        original = dvpl.encode('Cancel: Annuler\nNumeric: "001"\n'.encode())
        french = strings/'fr.yaml.dvpl'
        french.write_bytes(original)
        (strings/'en.yaml.dvpl').write_bytes(dvpl.encode(b'Cancel: Cancel\n'))

        patches = client_ui._fallback_locale_patches(self.game, 'Local server')
        self.assertEqual(set(patches), {'Strings/fr.yaml.dvpl'})
        labels = yaml.load(dvpl.decode(patches['Strings/fr.yaml.dvpl']), Loader=client_ui._StringsLoader)
        self.assertEqual(labels['Cancel'], 'Annuler')
        self.assertEqual(labels['Numeric'], '001')
        self.assertEqual(labels['Local/BotsTitle'], 'BATTLE WITH BOTS')
        self.assertEqual(labels['gameplaymode/Supremacy'], 'SUPREMACY')
        self.assertEqual(labels['Local PoC'], 'Local server')
        self.assertIn('Local/SbSetupRandom', labels)
        self.assertEqual(french.read_bytes(), original)

    def test_existing_state_requires_explicit_resume_before_mutation(self):
        state = self.root/'state'
        state.mkdir()
        account = state/'existing-account'
        account.write_text('original')
        with self.assertRaisesRegex(ValueError, 'resume'):
            install(self.options())
        self.assertEqual(account.read_text(), 'original')

    def test_linux_client_requires_runtime_paths_before_creating_environment(self):
        with self.assertRaisesRegex(ValueError, '--proton and --steam'):
            install(self.options('--client', str(self.root/'client')))
        self.assertFalse((self.root/'client').exists())
        self.assertFalse((self.root/'state').exists())

    def test_existing_client_is_preserved(self):
        client = self.root/'client'
        client.mkdir()
        (client/'sentinel').write_text('original')
        with self.assertRaisesRegex(ValueError, 'Client directory already exists'):
            install(self.options('--client', str(client), '--windows-client'))
        self.assertEqual((client/'sentinel').read_text(), 'original')

    def test_unsupported_platform_is_rejected_with_supported_entry_point(self):
        with patch('tools.setup.sys.platform', 'darwin'):
            with self.assertRaisesRegex(ValueError, 'setup.ps1'):
                install(self.options())

    def test_windows_copy_requires_client_destination(self):
        with self.assertRaisesRegex(ValueError, '--windows-client requires --client'):
            install(self.options('--windows-client'))

    def test_missing_listed_vehicle_is_reported_before_server_or_client_preparation(self):
        from solbreaker import dvpl
        from solbreaker.bootstrap import prepare as prepare_server
        from solbreaker.build_info import VERSION
        from solbreaker.client_setup import prepare as prepare_client
        from solbreaker.resource_source import VehicleCatalogError, inspect_game

        game = self.game
        vehicle_root = game/'Data/XML/item_defs/vehicles'
        (vehicle_root/'common').mkdir(parents=True)
        (vehicle_root/'other').mkdir()
        (game/'wotblitz.exe').write_bytes(b'fixture')
        (game/'Data/version.txt.dvpl').write_bytes(dvpl.encode(VERSION.encode()))
        (vehicle_root/'common/nations.xml.dvpl').write_bytes(dvpl.encode(b'<nations><other/></nations>'))
        listing = vehicle_root/'other/list.xml.dvpl'
        listing.write_bytes(dvpl.encode(b'<vehicles><Oth99_Relic/></vehicles>'))
        state, client = self.root/'state', self.root/'client'

        with patch('solbreaker.resource_source.BUILD_SHA256', hashlib.sha256(b'fixture').hexdigest()):
            for prepare in (lambda: prepare_server(game, state),
                            lambda: prepare_client(game, client, state/'connection.json')):
                with self.assertRaisesRegex(VehicleCatalogError, 'other/Oth99_Relic.xml.dvpl'):
                    prepare()
            self.assertFalse(state.exists())
            self.assertFalse(client.exists())

            (vehicle_root/'other/Oth99_Relic.xml.dvpl').write_bytes(dvpl.encode(b'<vehicle/>'))
            self.assertEqual(inspect_game(game)['game'], str(game))
            listing.write_bytes(dvpl.encode(b'<broken'))
            with self.assertRaisesRegex(VehicleCatalogError, 'other/list.xml.dvpl'):
                inspect_game(game)
