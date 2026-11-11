"""Native first-run discovery with a registered interpreter outside PATH."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipUnless(sys.platform == 'win32', 'Windows PowerShell discovery')
class WindowsPythonTests(unittest.TestCase):
    def probe(self, bundled):
        helper = Path(__file__).resolve().parents[1]/'tools/windows_python.ps1'
        quote = lambda value: "'"+str(value).replace("'", "''")+"'"
        script = f'''
$ErrorActionPreference = 'Stop'
. {quote(helper)}
function Get-Command {{ return $null }}
function Get-SolbreakerRegisteredPython {{ {quote(sys.executable)} }}
$resolved = Find-SolbreakerPython -Bundled:${str(bundled).lower()}
ConvertTo-Json -Compress -InputObject $resolved
'''
        powershell = Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run([str(powershell), '-NoProfile', '-ExecutionPolicy',
                                 'Bypass', '-Command', script],
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_registered_interpreter_is_found_without_launcher_or_path(self):
        self.assertEqual(Path(self.probe(False)).resolve(), Path(sys.executable).resolve())

    def test_native_bundle_requires_matching_version(self):
        selected = self.probe(True)
        if sys.version_info[:2] == (3, 12):
            self.assertEqual(Path(selected).resolve(), Path(sys.executable).resolve())
        else:
            self.assertIsNone(selected)
