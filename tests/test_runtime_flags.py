"""Solbreaker runtime settings reach the server and inherited child environment."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from solbreaker.drive_workers import configured_workers
from solbreaker.owner_channel import configured_ipc
from solbreaker.recon_config import requested_enabled
from solbreaker.sim_native import backend_status


class RuntimeFlagsTests(unittest.TestCase):
    def test_explicit_runtime_flags_and_validation(self):
        with patch.dict(os.environ, SOLBREAKER_SIM_BACKEND='python',
                        SOLBREAKER_SIM_WORKERS='3', SOLBREAKER_OWNER_IPC='socket',
                        SOLBREAKER_RECON_ENABLED='0'):
            self.assertEqual(backend_status(), 'python')
            self.assertEqual(configured_workers(), 3)
            self.assertEqual(configured_ipc(), 'socket')
            self.assertFalse(requested_enabled())
        with patch.dict(os.environ, SOLBREAKER_SIM_WORKERS='5'):
            with self.assertRaises(ValueError):
                configured_workers()
        with patch.dict(os.environ, SOLBREAKER_OWNER_IPC='unknown'):
            with self.assertRaises(ValueError):
                configured_ipc()

    def test_child_process_inherits_runtime_flags(self):
        code = '''
import json
from solbreaker.drive_workers import configured_workers
from solbreaker.owner_channel import configured_ipc
from solbreaker.sim_native import backend_status
print(json.dumps([configured_workers(), configured_ipc(), backend_status()]))
'''
        result = subprocess.run(
            [sys.executable, '-c', code],
            cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ, SOLBREAKER_SIM_WORKERS='2',
                     SOLBREAKER_OWNER_IPC='socket', SOLBREAKER_SIM_BACKEND='python'),
            capture_output=True, text=True, check=True, timeout=15)
        self.assertEqual(json.loads(result.stdout), [2, 'socket', 'python'])
