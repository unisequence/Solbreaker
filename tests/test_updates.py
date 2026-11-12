"""Release selection, package validation and rollback for the launcher updater."""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo

from solbreaker import updates


def checksums(files):
    return ''.join(f'{hashlib.sha256(content).hexdigest()}  {name}\n'
                   for name, content in files.items()).encode()


def zip_package(path, files, *, extra=None):
    with ZipFile(path, 'w') as archive:
        entries = {**files, updates.CHECKSUMS: checksums(files), **(extra or {})}
        for name, content in entries.items():
            entry = ZipInfo('Solbreaker/'+name)
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(entry, content)


def release(tag, *, kind='source', draft=False, prerelease=False):
    filename = f'Solbreaker-{tag[1:]}-{kind}.zip'
    url = f'https://github.com/{updates.REPOSITORY}/releases/download/{tag}/{filename}'
    return dict(tag_name=tag, draft=draft, prerelease=prerelease, assets=[
        dict(name=filename, state='uploaded', size=42, digest='sha256:'+'a'*64,
             browser_download_url=url)])


class UpdateTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.stage = self.root/updates.STAGING
        self.current = {'pyproject.toml': b'old', 'solbreaker/__init__.py': b'old',
                        'tools/obsolete.py': b'old', 'launch-gui.sh': b'old'}
        self.next = {'pyproject.toml': b'new', 'solbreaker/__init__.py': b'new',
                     'tools/install_dependencies.py': b'new', 'launch-gui.sh': b'new'}
        for name, content in {**self.current, 'runtime/manager.json': b'user preferences',
                              'data/private/accounts.db': b'user data'}.items():
            target = self.root/name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (self.root/updates.CHECKSUMS).write_bytes(checksums(self.current))

    def test_stable_ignores_prereleases_and_alpha_gets_latest_compatible_asset(self):
        releases = [release('v0.2.0-alpha.2', prerelease=True), release('v0.1.2', draft=True),
                    release('v0.1.0'), release('v0.2.0-alpha.10', prerelease=True),
                    release('v0.3.0', kind='windows-x64')]
        for current, expected in (('0.1.0a1', '0.2.0a10'), ('0.1.0', None), ('0.0.9', '0.1.0')):
            with patch.object(updates, 'request', return_value=io.BytesIO(json.dumps(releases).encode())):
                candidate = updates.check_release(current, 'source')
            self.assertEqual(candidate.version if candidate else None, expected)

    def test_bad_download_does_not_touch_the_installation_and_can_be_retried(self):
        candidate = updates.Release('0.1.1', 'https://example.test', 3, 'a'*64)
        with patch.object(updates, 'check_release', return_value=candidate), \
             patch.object(updates, 'request', return_value=io.BytesIO(b'bad')):
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                updates.prepare(self.root, '0.1.0', 'source')
        self.assertFalse(self.stage.exists())
        self.assertEqual((self.root/'pyproject.toml').read_bytes(), b'old')

    def test_rejects_paths_outside_the_package_before_extracting(self):
        archive = self.root/'update.zip'
        zip_package(archive, self.next, extra={'solbreaker/../data/private/accounts.db': b'bad'})
        with self.assertRaisesRegex(ValueError, 'Invalid update path'):
            updates.unpack(archive, self.stage, 'source')
        self.assertFalse(self.stage.exists())

    def stage_update(self):
        archive = self.root/'update.zip'
        zip_package(archive, self.next)
        self.stage.mkdir()
        updates.unpack(archive, self.stage/'payload', 'source')

    def test_replaces_managed_files_and_keeps_user_data(self):
        self.stage_update()
        with patch.object(updates.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            updates.replace_release(self.root, self.stage, portable=False)
        self.assertEqual((self.root/'solbreaker/__init__.py').read_bytes(), b'new')
        self.assertFalse((self.root/'tools/obsolete.py').exists())
        if os.name != 'nt':
            self.assertTrue((self.root/'launch-gui.sh').stat().st_mode & stat.S_IXUSR)
        self.assertEqual((self.root/'runtime/manager.json').read_bytes(), b'user preferences')
        self.assertEqual((self.root/'data/private/accounts.db').read_bytes(), b'user data')

    def test_failed_dependency_install_restores_the_previous_release(self):
        self.stage_update()
        with patch.object(updates.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 1), subprocess.CalledProcessError(1, [])]):
            with self.assertRaises(subprocess.CalledProcessError):
                updates.replace_release(self.root, self.stage, portable=False)
        self.assertFalse((self.stage/'transaction').exists())
        self.assertFalse((self.root/'tools/install_dependencies.py').exists())
        self.assertEqual((self.root/updates.CHECKSUMS).read_bytes(), checksums(self.current))
        for name, content in self.current.items():
            self.assertEqual((self.root/name).read_bytes(), content)

    def test_checkout_installs_release_beside_working_files(self):
        (self.root/'.git').mkdir()
        self.stage_update()
        with patch.object(updates.subprocess, 'run') as run:
            updates.install_from_checkout(self.root, self.stage)
        installed = self.root/updates.CHECKOUT_RELEASE
        self.assertEqual((installed/'pyproject.toml').read_bytes(), b'new')
        self.assertEqual((self.root/'pyproject.toml').read_bytes(), b'old')
        self.assertEqual((self.root/'runtime/manager.json').read_bytes(), b'user preferences')
        self.assertEqual(run.call_count, 2)

    def test_failed_first_install_can_be_retried_from_checkout(self):
        (self.root/'.git').mkdir()
        self.stage_update()
        with patch.object(updates.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, [])):
            with self.assertRaises(subprocess.CalledProcessError):
                updates.install_from_checkout(self.root, self.stage)
        self.assertFalse((self.root/updates.CHECKOUT_RELEASE).exists())
        self.assertFalse((self.stage/'transaction').exists())
        self.assertEqual((self.root/'pyproject.toml').read_bytes(), b'old')
