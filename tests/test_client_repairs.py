"""Regressions for migrating prepared clients without running the native game."""
import hashlib
import json
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from solbreaker import client_atgm_aim, client_sandbox, client_t49_textures as t49, dvpl
from solbreaker.client_patch import BUILD_SHA256
from solbreaker.management_tasks import launch_client


def digest(data):
    return hashlib.sha256(data).hexdigest()


class ClientRepairs(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name).resolve()
        self.lab = self.root/'lab'
        self.client = self.lab/'client'
        (self.client/'Data').mkdir(parents=True)
        self.game = self.root/'game'
        self.game.mkdir()
        self.manifest = self.lab/'manifest.json'

    def write_manifest(self, **fields):
        self.manifest.write_text(json.dumps(dict(game=str(self.game), client=str(self.client),
                                                  exe_sha256=BUILD_SHA256, **fields)))

    def test_atgm_patch_recovers_an_interrupted_launch_upgrade(self):
        target = self.client/'wotblitz.exe'
        before, after = b'old image', b'new image'
        target.write_bytes(before)
        self.write_manifest(client_exe_sha256=digest(before))

        with (patch.object(client_atgm_aim, 'INPUT_SHA256', digest(before)),
              patch.object(client_atgm_aim, 'OUTPUT_SHA256', digest(after)),
              patch.object(client_atgm_aim, 'PATCHES', ((0, b'old', b'new'),)),
              patch('solbreaker.management_tasks.require_executable_idle'),
              patch('solbreaker.client_ui.install_server_name'),
              patch('solbreaker.client_tls.configure_environment'),
              patch('solbreaker.management_tasks.subprocess.call', return_value=0) as launch):
            with patch('solbreaker.client_patch._write_manifest', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    launch_client(self.lab)
                launch.assert_not_called()
            self.assertEqual(target.read_bytes(), after)
            self.assertEqual(launch_client(self.lab), 0)
            self.assertEqual(json.loads(self.manifest.read_text())['client_exe_sha256'], digest(after))
            launch.assert_called_once()

    def test_sandbox_v1_client_upgrades_and_recovers_if_manifest_write_fails(self):
        target = self.client/'wotblitz.exe'
        before, after = b'previous sandbox', b'updated sandbox'
        target.write_bytes(before)
        self.write_manifest(client_exe_sha256=digest(before))

        with (patch.dict(client_sandbox.PREVIOUS_ENTRIES, {digest(before): {}}),
              patch.object(client_sandbox, 'OUTPUT_SHA256', digest(after)),
              patch.object(client_sandbox, 'patch', return_value=after) as upgrade_image,
              patch('solbreaker.management_tasks.require_executable_idle'),
              patch('solbreaker.client_ui.install_server_name'),
              patch('solbreaker.client_tls.configure_environment'),
              patch('solbreaker.management_tasks.subprocess.call', return_value=0) as launch):
            with patch('solbreaker.client_patch._write_manifest', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    launch_client(self.lab)
                launch.assert_not_called()
            self.assertEqual(target.read_bytes(), after)
            self.assertEqual(launch_client(self.lab), 0)
            self.assertEqual(json.loads(self.manifest.read_text())['client_exe_sha256'], digest(after))
            upgrade_image.assert_called_once_with(before)
            launch.assert_called_once()

    def test_bad_depot_leaves_client_untouched_and_correct_import_is_repeatable(self):
        self.write_manifest()
        source = self.root/'depot'
        source.mkdir()
        originals = {}
        raw = b'SFV2'+struct.pack('<I', 41)
        for relative in t49.TEXTURES:
            name = relative.split('.dx11.', 1)[0].encode()+b'.tex'
            raw += struct.pack('<H', len(name))+name
            original = source/Path(relative).name
            original.write_bytes(name)
            originals[relative] = digest(name)
            (self.client/t49.USA/relative).parent.mkdir(parents=True, exist_ok=True)
            (self.client/t49.USA/relative).write_bytes(b'modern atlas')
        scene = self.client/t49.SCENE
        (self.game/t49.SCENE).parent.mkdir(parents=True)
        for file in (scene, self.game/t49.SCENE):
            file.write_bytes(dvpl.encode(raw))
        original.write_bytes(b'corrupt')

        with (patch.object(t49, 'TEXTURES', originals), patch.object(t49, 'SCENE_SHA256', digest(raw)),
              patch.object(t49, 'require_executable_idle')):
            with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
                t49.restore(self.lab, source)
            self.assertEqual(dvpl.read(scene), raw)
            self.assertFalse(list(self.client.rglob('T49P_OLD*')))

            original.write_bytes(name)
            self.assertTrue(t49.restore(self.lab, source)['changed'])
            self.assertFalse(t49.restore(self.lab, source)['changed'])
            self.assertIn(b'images/T49P_OLD.tex', dvpl.read(scene))
            self.assertEqual(dvpl.read(self.game/t49.SCENE), raw)
            for relative in originals:
                self.assertEqual((self.client/t49.USA/relative).read_bytes(), b'modern atlas')
                alias = self.client/t49.USA/relative.replace('A100_T49', 'T49P_OLD')
                self.assertEqual(digest(alias.read_bytes()), originals[relative])
