"""Locked profile selection, hash enforcement and safe installation receipts."""
from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools import install_dependencies as dependencies


class DependencyTests(unittest.TestCase):
    def test_profiles_preserve_supported_platform_versions(self):
        self.assertIn('numpy-legacy', dependencies.selected_groups('server', 'linux', (3, 11), False))
        self.assertIn('numpy-modern', dependencies.selected_groups('server', 'linux', (3, 13), False))
        self.assertIn('native-windows-physics', dependencies.selected_groups('gui', 'win32', (3, 12), True))
        self.assertNotIn('physics', dependencies.selected_groups('gui', 'win32', (3, 12), False))
        with self.assertRaisesRegex(ValueError, '3.12'):
            dependencies.selected_groups('server', 'win32', (3, 13), False)
        with self.assertRaises(ValueError):
            dependencies.selected_groups('gui', 'linux', (3, 13), True)

    def test_pip_uses_hashes_and_wheels_with_no_implicit_build_dependencies(self):
        record = dict(name='example', version='1.2.3', files=[dict(sha256='a'*64)])
        calls = []
        def observe(arguments, **kwargs):
            calls.append(arguments)
            path = Path(arguments[arguments.index('-r')+1])
            self.assertEqual(path.read_text(), 'example==1.2.3 --hash=sha256:'+'a'*64+'\n')
        with patch.object(dependencies, 'pip_command', side_effect=observe):
            dependencies.install_records([record])
        self.assertIn('--require-hashes', calls[0])
        self.assertIn('--no-deps', calls[0])
        self.assertIn('--no-build-isolation', calls[0])
        self.assertIn('--only-binary=:all:', calls[0])
        with patch.object(dependencies, 'pip_command') as command:
            record['files'] = [dict(sha256='invalid')]
            with self.assertRaises(ValueError):
                dependencies.install_records([record])
            command.assert_not_called()

    def test_failed_install_does_not_commit_receipt(self):
        with TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            record = dict(name='pip', version='26.3', files=[dict(sha256='a'*64)])
            with patch.object(dependencies.sys, 'prefix', str(prefix)), \
                 patch.object(dependencies, 'records_for', return_value=([record], False)), \
                 patch('solbreaker.platform_io.private_lock', return_value=nullcontext()), \
                 patch.object(dependencies, 'install_records', side_effect=subprocess.CalledProcessError(1, 'pip')):
                with self.assertRaises(subprocess.CalledProcessError):
                    dependencies.install('gui')
            self.assertFalse((prefix/dependencies.STAMP_NAME).exists())

    def test_physics_build_uses_isolated_locked_tools_and_hashes_local_wheel(self):
        record = dict(name='pybullet', version='3.2.7', files=[dict(sha256='a'*64)])
        calls = []
        def observe(arguments, **kwargs):
            calls.append((arguments, kwargs))
            if arguments[0] == 'wheel':
                output = Path(arguments[arguments.index('--wheel-dir')+1])
                (output/'pybullet-test.whl').write_bytes(b'synthetic built wheel')
            if arguments[0] == 'install' and '--no-index' in arguments:
                text = Path(arguments[arguments.index('-r')+1]).read_text()
                self.assertTrue(text.startswith('file:///'))
                self.assertIn('--hash=sha256:', text)
        with patch.object(dependencies.venv, 'EnvBuilder'), \
             patch.object(dependencies, 'pip_command', side_effect=observe):
            dependencies.install_records([record], physics=True)
        self.assertEqual([args[0] for args, kwargs in calls], ['install', 'wheel', 'install'])
        builder = calls[0][1]['python']
        self.assertNotEqual(str(builder), dependencies.sys.executable)
        self.assertEqual(calls[1][1]['python'], builder)
        self.assertIn('--require-hashes', calls[1][0])
        self.assertIn('--no-build-isolation', calls[1][0])

    def test_check_only_never_installs_and_requires_approved_receipt(self):
        with TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            record = dict(name='example', version='1.2.3', files=[dict(sha256='a'*64)])
            with patch.object(dependencies.sys, 'prefix', str(prefix)), \
                 patch.object(dependencies, 'records_for', return_value=([record], False)), \
                 patch.object(dependencies.metadata, 'version', return_value='1.2.3'), \
                 patch.object(dependencies, 'project_matches', return_value=True), \
                 patch.object(dependencies, 'pip_command') as command:
                self.assertFalse(dependencies.install('gui', check=True))
                (prefix/dependencies.STAMP_NAME).write_text(json.dumps(
                    dict(packages={'example': dependencies.fingerprint(record)})))
                self.assertTrue(dependencies.install('gui', check=True))
                command.assert_not_called()

    def test_pip_ignores_user_index_environment(self):
        with patch.dict(dependencies.os.environ, PIP_EXTRA_INDEX_URL='https://untrusted.invalid'), \
             patch.object(dependencies.subprocess, 'run') as run:
            dependencies.pip_command(['check'])
        args, kwargs = run.call_args
        self.assertIn('--isolated', args[0])
        self.assertNotIn('PIP_EXTRA_INDEX_URL', kwargs['env'])
        self.assertEqual(kwargs['env']['PIP_CONFIG_FILE'], dependencies.os.devnull)
