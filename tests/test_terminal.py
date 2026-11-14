"""The terminal interface uses the same neutral compatibility caption as the GUI."""
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


@unittest.skipUnless(importlib.util.find_spec('textual'), 'Install the TUI profile')
class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_compatible_build_is_visible_and_paths_use_neutral_labels(self):
        from textual.widgets import Label
        from solbreaker import __version__
        from solbreaker.build_info import COMPATIBILITY
        from solbreaker.management import Settings
        from solbreaker.terminal import Terminal
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root/'manager.json'
            Settings(state=str(root/'data'), client=str(root/'client')).save(config)
            app = Terminal(config)
            async with app.run_test(size=(120, 40)):
                self.assertEqual(app.title, f'Solbreaker {__version__}')
                self.assertEqual(app.sub_title, COMPATIBILITY)
                labels = [str(widget.content) for widget in app.query(Label)]
                self.assertIn('Папка совместимой сборки', labels)
