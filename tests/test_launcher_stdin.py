"""The launcher's stop pipe must allow native Windows arena processes to start."""
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


@unittest.skipUnless(sys.platform == 'win32', 'Windows stdin and process startup')
class LauncherStdinTests(unittest.TestCase):
    def test_owner_starts_while_launcher_stop_pipe_is_empty(self):
        script = '''
import asyncio
from pathlib import Path
import sys
from unittest.mock import patch
from solbreaker.arena_owner import AsyncArenaOwner, Command
from solbreaker.server import serve_cli
from tests.test_native_windows import EchoHandler

async def simulated_server(lab):
    await asyncio.sleep(.1)
    async with await AsyncArenaOwner.create(EchoHandler, timeout=5) as owner:
        result = await owner.apply(Command('control', 1., 1, b'\\x01\\x00'))
        assert result.state == b'\\x01\\x00'
        print('owner_ready')

if __name__ == '__main__':
    with patch('solbreaker.server.serve', simulated_server):
        asyncio.run(serve_cli(Path(sys.argv[1]), stop_on_stdin=True))
'''
        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'private').mkdir()
            probe = root/'probe.py'
            probe.write_text(script)
            process = subprocess.Popen(
                [sys.executable, '-u', str(probe), str(root)], cwd=project,
                env=dict(os.environ, PYTHONPATH=str(project), SOLBREAKER_OWNER_IPC='socket'),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                # Keep stdin open and empty throughout the owner startup.
                code = process.wait(timeout=20)
                output = process.stdout.read()
                self.assertEqual(code, 0, output)
                self.assertIn('owner_ready', output)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdin.close()
                process.stdout.close()
