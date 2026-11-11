"""Source packages retain launch permissions and exclude local runtime data."""
from pathlib import Path
import hashlib
import json
import stat
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import tomllib
from unittest.mock import patch
import zipfile

from tools.package_source import FILES, FOLDERS, GAME_CATALOG, SOURCE_CRATES, WHEEL, package
from solbreaker.resource_copy import copy_resource_tree, resource_tree_facts


class PackagingTests(unittest.TestCase):
    @staticmethod
    def write_file_list(root):
        names = list(FILES)
        for folder in FOLDERS:
            names.extend(path.relative_to(root).as_posix() for path in (root/folder).rglob('*')
                         if path.is_file() and not {'target', '__pycache__'} & set(path.parts)
                         and path.suffix not in ('.dll', '.so', '.dylib'))
        (root/'release-files.txt').write_text('\n'.join(sorted(set(names)))+'\n')

    def source_fixture(self, root):
        for name in FILES:
            (root/name).write_text('source')
        for folder in FOLDERS:
            (root/folder).mkdir()
        self.write_file_list(root)

    def test_unreviewed_private_and_game_files_fail_before_archive_creation(self):
        for name in ('solbreaker/.env', 'solbreaker/private-key.pem', 'tools/account.sqlite',
                     'tests/debug.log', 'solbreaker/assets/accidental-game.dvpl'):
            with self.subTest(name=name), TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.source_fixture(root)
                path = root/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic audit input')
                with patch('tools.package_source.ROOT', root):
                    with self.assertRaisesRegex(ValueError, 'Unreviewed'):
                        package(root/'result.zip')
                self.assertFalse((root/'result.zip').exists())

    def test_manifest_cannot_authorize_private_paths_or_escape(self):
        for name in ('solbreaker/private.pem', '../outside.py', '/etc/passwd',
                     'runtime/account.json', 'solbreaker\\private.py'):
            with self.subTest(name=name), TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.source_fixture(root)
                with (root/'release-files.txt').open('a') as stream:
                    stream.write(name+'\n')
                with patch('tools.package_source.ROOT', root):
                    with self.assertRaises(ValueError):
                        package(root/'result.zip')
                self.assertFalse((root/'result.zip').exists())

    def test_secret_in_reviewed_text_is_rejected_without_printing_value(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.source_fixture(root)
            (root/'README.md').write_bytes(b'-----BEGIN '+b'PRIVATE KEY-----\nsynthetic')
            with patch('tools.package_source.ROOT', root):
                with self.assertRaisesRegex(ValueError, 'Possible secret.*README'):
                    package(root/'result.zip')
            self.assertFalse((root/'result.zip').exists())

    def test_known_game_content_is_rejected_even_under_reviewed_name(self):
        import gzip
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.source_fixture(root)
            content = b'synthetic original game content'
            asset = root/'solbreaker/assets/cover.png'
            asset.parent.mkdir(parents=True)
            asset.write_bytes(content)
            (root/GAME_CATALOG).write_bytes(gzip.compress(json.dumps(
                [['original.dvpl', len(content), hashlib.sha256(content).hexdigest()]]).encode()))
            self.write_file_list(root)
            with patch('tools.package_source.ROOT', root):
                with self.assertRaisesRegex(ValueError, 'original game file'):
                    package(root/'result.zip')

    def test_source_symlink_is_rejected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.source_fixture(root)
            try:
                (root/'tools/linked.py').symlink_to(root/'README.md')
            except OSError:
                self.skipTest('Creating symlinks requires platform privileges')
            with patch('tools.package_source.ROOT', root):
                with self.assertRaisesRegex(ValueError, 'regular source'):
                    package(root/'result.zip')

    def test_native_wheel_must_match_accepted_digest(self):
        from tools.package_source import CRATES
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.source_fixture(root)
            binaries = root/'binaries'
            binaries.mkdir()
            for crate in CRATES:
                (binaries/('tfs_'+crate+'.dll')).write_bytes(b'synthetic library')
            wheel = binaries/WHEEL
            wheel.write_bytes(b'accepted synthetic wheel')
            expected = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in binaries.iterdir()}
            (root/'requirements/native-artifacts.json').write_text(json.dumps(dict(sha256=expected)))
            self.write_file_list(root)
            wheel.write_bytes(b'changed synthetic wheel')
            with patch('tools.package_source.ROOT', root):
                with self.assertRaisesRegex(ValueError, 'accepted SHA-256'):
                    package(root/'result.zip', windows_native=binaries, windows_wheel=wheel)
            self.assertFalse((root/'result.zip').exists())

    def test_write_failure_removes_partial_archive_and_existing_output_is_preserved(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.source_fixture(root)
            output = root/'result.zip'
            with patch('tools.package_source.ROOT', root):
                with patch('tools.package_source.zipfile.ZipFile.writestr', side_effect=OSError('synthetic failure')):
                    with self.assertRaises(OSError):
                        package(output)
                self.assertFalse(output.exists())
                output.write_bytes(b'original')
                with self.assertRaises(FileExistsError):
                    package(output)
                self.assertEqual(output.read_bytes(), b'original')

    def test_archive_bytes_do_not_depend_on_build_time(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.source_fixture(root)
            with patch('tools.package_source.ROOT', root):
                with patch('zipfile.time.localtime', return_value=(2020, 1, 1, 0, 0, 0)):
                    first = package(root/'first.zip')
                with patch('zipfile.time.localtime', return_value=(2026, 11, 10, 12, 0, 0)):
                    second = package(root/'second.zip')
            self.assertEqual(first['sha256'], second['sha256'])

    def test_only_source_and_explicit_binary_inputs_are_packaged(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in FILES:
                (root/name).write_text('source')
            (root/'launch-gui.sh').chmod(0o755)
            for name in FOLDERS:
                (root/name).mkdir()
            (root/'LICENSE').write_text('Owner-selected project license')
            (root/'licenses/Rapier-LICENSE').write_text('Dependency license')
            (root/'docs/SOURCES.md').write_text('Source attribution')
            for name in ('data', 'private_evidence', 'runtime', '.venv'):
                (root/name).mkdir()
                (root/name/'secret').write_text('private')
            for crate in SOURCE_CRATES:
                target = root/'solbreaker/native'/crate/'target'
                target.mkdir(parents=True)
                (target/'secret').write_text('build')
            (root/'solbreaker/native/rapier_dynamic/tfs_rapier_dynamic.dll').write_bytes(b'old')
            (root/'solbreaker/native/physics_core/libsolbreaker_physics_core.so').write_bytes(b'build')
            (root/'solbreaker/native/physics_core/solbreaker_physics_core.dll').write_bytes(b'build')
            self.write_file_list(root)
            with patch('tools.package_source.ROOT', root):
                result = package(root/'source.zip')
            with zipfile.ZipFile(result['archive']) as archive:
                self.assertFalse(any('secret' in name or name.endswith(('.dll', '.so'))
                                     for name in archive.namelist()))
                self.assertTrue(archive.getinfo('Solbreaker/launch-gui.sh').external_attr >> 16 & 0o100)
                self.assertIn('Solbreaker/LICENSE', archive.namelist())
                self.assertIn('Solbreaker/THIRD_PARTY_NOTICES.md', archive.namelist())
                self.assertIn('Solbreaker/licenses/Rapier-LICENSE', archive.namelist())
                self.assertIn('Solbreaker/docs/SOURCES.md', archive.namelist())

    def test_dependency_notices_match_locked_rust_sources(self):
        root = Path(__file__).resolve().parents[1]
        expected = set()
        for crate in SOURCE_CRATES:
            lock = tomllib.loads((root/'solbreaker/native'/crate/'Cargo.lock').read_text(encoding='utf-8'))
            expected.update((item['name'], item['version'], item['checksum'])
                            for item in lock['package'] if 'source' in item)
        manifest = json.loads((root/'licenses/manifest.json').read_text(encoding='utf-8'))
        actual = {(item['name'], item['version'], item['source_sha256'])
                  for item in manifest['components'] if item['ecosystem'] == 'crates.io'}
        self.assertEqual(actual, expected)
        for item in manifest['components']:
            self.assertTrue(item['texts'], item['name'])
            for text in item['texts']:
                relative = Path(text['path'])
                self.assertFalse(relative.is_absolute())
                self.assertNotIn('..', relative.parts)
                self.assertEqual(relative.parts[0], 'licenses')
                self.assertEqual(hashlib.sha256((root/relative).read_bytes()).hexdigest(),
                                 text['sha256'], text['path'])

    def test_copy_progress_reports_the_verified_file_volume(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'source'
            source.mkdir()
            (source/'first').write_bytes(b'ab')
            (source/'second').write_bytes(b'cde')
            calls = []
            copy_resource_tree(source, root/'copy', progress=lambda done, total: calls.append((done, total)))
            self.assertEqual(calls[-1], (5, 5))
            self.assertEqual((root/'copy/second').read_bytes(), b'cde')

    def test_directory_index_size_does_not_change_resource_fingerprint(self):
        with TemporaryDirectory() as temporary:
            source = Path(temporary)/'source'
            (source/'nested').mkdir(parents=True)
            resource = source/'nested/resource'
            resource.write_bytes(b'original')
            directory_size = 4096
            original_lstat = Path.lstat

            def lstat(path, *args, **kwargs):
                info = original_lstat(path, *args, **kwargs)
                if not stat.S_ISDIR(info.st_mode):
                    return info
                return SimpleNamespace(
                    st_mode=info.st_mode, st_size=directory_size,
                    st_mtime_ns=info.st_mtime_ns, st_ctime_ns=info.st_ctime_ns,
                    st_dev=info.st_dev, st_ino=info.st_ino,
                    st_file_attributes=getattr(info, 'st_file_attributes', 0))

            with patch.object(Path, 'lstat', lstat):
                before = resource_tree_facts(source)
                directory_size = 0
                self.assertEqual(resource_tree_facts(source), before)
                resource.write_bytes(b'changed content')
                self.assertNotEqual(resource_tree_facts(source), before)
                resource.unlink()
                self.assertNotEqual(resource_tree_facts(source), before)
