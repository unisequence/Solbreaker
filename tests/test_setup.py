"""Installer preflight and resumable setup regressions."""
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
