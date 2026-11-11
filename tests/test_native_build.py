"""Normal builds and setup include the required native ground controller."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from tools import build_native
from tools.setup_state import verify_native
from solbreaker.revision import implementation_revision


class NativeBuildTests(unittest.TestCase):
    def test_default_build_installs_all_five_libraries_on_each_platform(self):
        for platform in ('linux', 'win32'):
            with self.subTest(platform=platform), TemporaryDirectory() as temporary:
                root = Path(temporary)
                calls = []

                def run(command, **kwargs):
                    calls.append(command)
                    if command[0] != 'cargo':
                        return
                    self.assertIn('--locked', command)
                    crate = Path(command[command.index('--manifest-path')+1]).parent
                    name = 'solbreaker_physics_core' if crate.name == 'physics_core' else 'tfs_'+crate.name
                    filename = name+'.dll' if platform == 'win32' else 'lib'+name+'.so'
                    output = crate/'target/release'/filename
                    output.parent.mkdir(parents=True)
                    output.write_bytes(b'synthetic native output')

                with patch.object(build_native, '__file__', str(root/'tools/build_native.py')), \
                     patch('tools.build_native.sys.platform', platform), \
                     patch('tools.build_native.platform.machine', return_value='x86_64'), \
                     patch('tools.build_native.sys.argv', ['build_native.py']), \
                     patch('tools.build_native.subprocess.run', side_effect=run), \
                     patch.dict(os.environ, clear=True):
                    build_native.main()
                self.assertEqual(len([call for call in calls if call[0] == 'cargo']), 5)
                filename = ('solbreaker_physics_core.dll' if platform == 'win32'
                            else 'libsolbreaker_physics_core.so')
                self.assertEqual((root/'solbreaker/native/physics_core'/filename).read_bytes(),
                                 b'synthetic native output')
                self.assertEqual(calls[-1][-1], '--optional')

    def test_reused_setup_requires_the_ground_controller(self):
        with patch('solbreaker.sim_native.load_library', return_value=object()), \
             patch('solbreaker.impact_native.load_library', return_value=object()), \
             patch('solbreaker.armor_native.load_library', return_value=object()), \
             patch('tools.setup_state.ctypes.CDLL', return_value=MagicMock()), \
             patch('solbreaker.native_physics.load_library', side_effect=RuntimeError('missing physics_core')) as load:
            with self.assertRaisesRegex(RuntimeError, 'missing physics_core'):
                verify_native()
            load.assert_called_once_with()

    def test_revision_changes_when_ground_controller_changes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'manifest.json').write_text(json.dumps(dict(version='synthetic')))
            physics = root/'solbreaker/native/physics_core'
            (physics/'src').mkdir(parents=True)
            source = physics/'src/ground.rs'
            source.write_text('first source')
            library = physics/'libsolbreaker_physics_core.so'
            library.write_bytes(b'first build')
            with patch('solbreaker.revision.__file__', str(root/'solbreaker/revision.py')), \
                 patch('solbreaker.revision.sys.platform', 'linux'):
                first = implementation_revision(root)
                source.write_text('changed source')
                second = implementation_revision(root)
                self.assertNotEqual(first, second)
                library.write_bytes(b'changed build')
                self.assertNotEqual(second, implementation_revision(root))
