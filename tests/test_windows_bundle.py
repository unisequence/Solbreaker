"""Portable releases reject wrong architectures and untrusted runtime inputs."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from tools import windows_bundle as bundle
from tools.package_source import read_input


class WindowsBundleTests(unittest.TestCase):
    def test_wheel_abi_and_architecture(self):
        for name in ('a-1-py3-none-any.whl', 'a-1-cp312-cp312-win_amd64.whl',
                     'a-1-cp310-abi3-win_amd64.whl'):
            self.assertTrue(bundle.compatible_wheel(name))
        for name in ('a-1-cp313-cp313-win_amd64.whl', 'a-1-cp312-cp312-win_arm64.whl',
                     'a-1-cp312-cp312-manylinux_x86_64.whl'):
            self.assertFalse(bundle.compatible_wheel(name))

    def test_runtime_and_wheel_hashes_and_relocatable_paths(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode='w:gz') as stream:
                member = tarfile.TarInfo('python/python.exe')
                member.size = 3
                stream.addfile(member, io.BytesIO(b'exe'))
            runtime = dict(filename='runtime.tar.gz', sha256=hashlib.sha256(archive.getvalue()).hexdigest())
            (directory/runtime['filename']).write_bytes(archive.getvalue())
            wheel = directory/'example-1-py3-none-any.whl'
            with zipfile.ZipFile(wheel, 'w') as stream:
                stream.writestr('example/__init__.py', 'value = 1\n')
            record = dict(name='example', version='1', files=[dict(filename=wheel.name,
                          sha256=hashlib.sha256(wheel.read_bytes()).hexdigest())])
            source = {'requirements/lock.json': b'{}', 'pyproject.toml': b'project'}
            with patch.object(bundle, 'RUNTIME', runtime), patch.object(bundle, 'records', return_value=[record]):
                result = bundle.payloads(directory, source, read_input)
                self.assertEqual(result['runtime/python/Lib/site-packages/example/__init__.py'], b'value = 1\n')
                self.assertIn(b'..\\..\n', result['runtime/python/python312._pth'])
                stamp = json.loads(result['runtime/python/solbreaker-bundle.json'])
                self.assertIn('example', stamp['packages'])
                wheel.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'locked SHA-256'):
                    bundle.payloads(directory, source, read_input)
                (directory/runtime['filename']).write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'pinned SHA-256'):
                    bundle.payloads(directory, source, read_input)

    def test_archive_paths_cannot_escape_or_alias_on_windows(self):
        for name in ('../private', '/private', 'python/../private', 'python/C:private',
                     'python\\private', 'python/private.', 'python/private '):
            with self.subTest(name=name), self.assertRaises(ValueError):
                bundle.safe_name(name)
