"""Regressions for migrating prepared clients without running the native game."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from solbreaker import client_atgm_aim, client_atmosphere, client_sandbox, client_t49_textures as t49, dvpl
from solbreaker.client_patch import BUILD_SHA256
from solbreaker.management_tasks import launch_client


def digest(data):
    return hashlib.sha256(data).hexdigest()


class ClientRepairs(unittest.TestCase):
    def test_sandbox_blob_matches_native_room_parser(self):
        blob, entries = client_sandbox.build_blob()
        self.assertEqual(blob, client_sandbox.BLOB)
        self.assertEqual(entries, client_sandbox.ENTRIES)
        self.assertIn(client_sandbox.PREVIOUS_SANDBOX_SHA256,
                      client_sandbox.PREVIOUS_ENTRIES)
        self.assertIn(client_sandbox.PREVIOUS_CONTROLS_SHA256,
                      client_sandbox.PREVIOUS_ENTRIES)

    def test_battle_settings_adapters_match_assembly(self):
        from solbreaker import client_battle_settings

        source = Path(client_battle_settings.__file__).with_name('native')
        with TemporaryDirectory() as directory:
            obj, linked, code = (Path(directory) / name for name in
                                 ('settings.o', 'settings.elf', 'settings.bin'))
            for defines, expected in (
                ([], client_battle_settings.DIRECT_HANGAR_EXTENSION),
                (['--defsym', 'standalone_settings=1'],
                 client_battle_settings.STANDALONE_SETTINGS_EXTENSION),
            ):
                with self.subTest(defines=defines):
                    subprocess.run(['as', '--32', '-I', source, '--defsym', 'direct_hangar=1',
                                    *defines, source / 'battle_settings_client.S', '-o', obj],
                                   check=True)
                    subprocess.run(['ld', '-m', 'elf_i386', '-Ttext=0x4b1de00',
                                    '--section-start=.battle_maps=0x4b21a00',
                                    '-e', 'settings_get', '-o', linked, obj], check=True)
                    subprocess.run(['objcopy', '-O', 'binary', '--only-section=.text', linked, code],
                                   check=True)
                    self.assertEqual(code.read_bytes(), expected)

    def test_standalone_battle_settings_header_routes_back_to_the_lobby(self):
        from solbreaker import client_battle_settings

        source = self.root/'Training.yaml.dvpl'
        template = {'Controls': [{'name': 'Screen', 'children': [{'path': 'Content'}]}]}
        source.write_bytes(dvpl.encode(yaml.safe_dump(template).encode()))
        standalone = yaml.safe_load(dvpl.decode(client_battle_settings.standalone_screen(source)))
        self.assertEqual(standalone['Controls'][0]['children'], [
            {'path': 'Content'},
            {'path': 'Header/menuButton', 'input': True,
             'components': {'UIInputEventComponent': {'onTouchUpInside': 'BACK_EVENT'}}},
        ])
        self.assertEqual(yaml.safe_load(dvpl.read(source)), template)

    def test_chat_filter_is_disabled_only_in_the_prepared_client(self):
        from solbreaker.client_ui import install_chat_filter

        source = self.game/'Data/MessageFilters'
        (source/'V2').mkdir(parents=True)
        legacy = {'BadWords':['bad'], 'Spacious':['spaced'], 'Domains':['example.test']}
        modern = {'badwords':['bad'], 'domains':['example.test']}
        legacy_path = source/'en_messenger_oldictionary.yaml.dvpl'
        modern_path = source/'V2/en.badwords.yaml.dvpl'
        legacy_path.write_bytes(dvpl.encode(yaml.safe_dump(legacy).encode()))
        modern_path.write_bytes(dvpl.encode(yaml.safe_dump(modern).encode()))
        (self.client/'Data').rmdir()
        (self.client/'Data').symlink_to(self.game/'Data', target_is_directory=True)
        self.write_manifest(ui_overrides={'unrelated':'digest'})

        manifest = install_chat_filter(self.lab, json.loads(self.manifest.read_text()))

        self.assertEqual(manifest['chat_filter_version'], 2)
        self.assertEqual(manifest['ui_overrides']['unrelated'], 'digest')
        self.assertEqual(yaml.safe_load(dvpl.read(legacy_path)), legacy)
        self.assertEqual(yaml.safe_load(dvpl.read(modern_path)), modern)
        prepared_legacy = yaml.safe_load(dvpl.read(self.client/'Data/MessageFilters'/legacy_path.name))
        prepared_modern = yaml.safe_load(dvpl.read(self.client/'Data/MessageFilters/V2'/modern_path.name))
        self.assertEqual(prepared_legacy, {'BadWords':[], 'Spacious':[], 'Domains':['example.test']})
        self.assertEqual(prepared_modern, {'badwords':[], 'domains':['example.test']})
        self.assertIs(install_chat_filter(self.lab, manifest), manifest)

    def test_reticle_upgrade_restores_stock_sights_and_keeps_the_continuous_ring(self):
        from solbreaker import client_reticle

        legacy_overrides = {'unrelated.yaml.dvpl': 'unrelated-hash'}
        for relative in client_reticle._STOCK_RESOURCES:
            source = self.game/'Data'/relative
            prepared = self.client/'Data'/relative
            source.parent.mkdir(parents=True, exist_ok=True)
            prepared.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(dvpl.encode(relative.encode()))
            prepared.write_bytes(dvpl.encode(b'old overlay'))
            legacy_overrides[relative] = digest(prepared.read_bytes())
        self.write_manifest(reticle_ui_version=21, ui_overrides=legacy_overrides)
        result = client_reticle.install(self.lab, json.loads(self.manifest.read_text()))
        self.assertEqual(result['reticle_ui_version'], 22)
        self.assertEqual(result['ui_overrides']['unrelated.yaml.dvpl'], 'unrelated-hash')
        for relative in client_reticle._STOCK_RESOURCES:
            self.assertNotIn(relative, result['ui_overrides'])
            self.assertEqual(dvpl.read(self.client/'Data'/relative), relative.encode())
        for suffix in ('', '@2x'):
            relative = f'Gfx/Battle/HUD/aim/GunSpread{suffix}.packed.webp.dvpl'
            sprite = self.client/'Data'/relative
            expected = Path(client_reticle.__file__).with_name('assets')/f'solbreaker-spread{suffix}.packed.webp'
            self.assertEqual(dvpl.read(sprite), expected.read_bytes())
            self.assertEqual(result['ui_overrides'][relative], digest(sprite.read_bytes()))
        self.assertIs(client_reticle.install(self.lab, result), result)

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

    def test_copied_client_launches_without_the_original(self):
        from solbreaker.build_info import VERSION

        image = b'private executable'
        executable = self.client/'wotblitz.exe'
        executable.write_bytes(image)
        self.write_manifest(resource_policy='copy', version=VERSION, client_exe_sha256=digest(image))
        self.game.rmdir()
        data = self.client/'Data'
        (data/'version.txt.dvpl').write_bytes(dvpl.encode(VERSION.encode()))
        (data/'maps.yaml.dvpl').write_bytes(dvpl.encode(b'maps: {}'))
        catalog = data/'XML/item_defs/vehicles'
        (catalog/'common').mkdir(parents=True)
        (catalog/'usa').mkdir()
        (catalog/'common/nations.xml.dvpl').write_bytes(dvpl.encode(b'<nations><usa/></nations>'))
        (catalog/'usa/list.xml.dvpl').write_bytes(dvpl.encode(b'<list><tank/></list>'))
        (catalog/'usa/tank.xml.dvpl').write_bytes(dvpl.encode(b'<tank/>'))
        with (patch('solbreaker.management_tasks.require_executable_idle'),
              patch('solbreaker.client_ui.install_server_name'),
              patch('solbreaker.client_tls.configure_environment'),
              patch('solbreaker.management_tasks.subprocess.call', return_value=0) as launch):
            self.assertEqual(launch_client(self.lab), 0)
            self.assertEqual(launch.call_args.kwargs['cwd'], self.lab)
            (data/'alias').symlink_to(data, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'refuses links'):
                launch_client(self.lab)

    def test_polygon_menu_serializes_without_yaml_aliases(self):
        from solbreaker.client_practice_picker import patch_menu

        root = {'components': {'UIDataLocalVarsComponent': {'data': []}}, 'children': []}
        document = {'ImportedPackages': [], 'Prototypes': [root]}
        patch_menu(document, root, [])
        events = yaml.parse(yaml.safe_dump(document))
        self.assertFalse(any(isinstance(event, yaml.AliasEvent) for event in events))

    def test_weather_maps_have_separate_client_rows_and_use_pinned_scenes(self):
        from solbreaker.map_catalog import training_map_catalog

        data = self.game/'Data'
        mines = client_atmosphere.MINES
        scene = client_atmosphere.scene_path(mines)
        row = {'id': 6, 'localName': f'{mines}/{mines}.sc2', 'avaliableInTrainingRoom': True}
        original = dvpl.encode(bytes(1024))
        resources = {
            Path('maps.yaml.dvpl'): dvpl.encode(yaml.safe_dump({'maps': {'rudniki': row}}).encode()),
            scene: original,
            scene.with_name(mines+'.scg.dvpl'): b'geometry',
            Path('Gfx/UI/BattleLoadingScreen/rudniki/BackgroundLoading.packed.webp.dvpl'): b'loading',
            Path('Gfx/UI/BattleScreenHUD/minimap/rudniki/MiniMapSmall.packed.webp.dvpl'): b'minimap',
            Path('Gfx/UI/BattleScreenHUD/minimap/rudniki/MiniMapSmall@2x.packed.webp.dvpl'): b'minimap2x',
        }
        for relative, content in resources.items():
            target = data/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        prepared = self.client/'Data'
        prepared.rmdir()
        prepared.symlink_to(data, target_is_directory=True)
        self.write_manifest()
        layout = (digest(original), 32, 150, 200, 320, 8.)
        with patch.dict(client_atmosphere.SCENES, {mines: layout}):
            manifest = client_atmosphere.install_weather_maps(self.lab, json.loads(self.manifest.read_text()))
        self.assertEqual(manifest['weather_maps_version'], 1)
        self.assertEqual(training_map_catalog(self.game), {6: 'rudniki', 101: 'rudniki_evening', 102: 'rudniki_fog'})
        maps = yaml.safe_load(dvpl.read(prepared/'maps.yaml.dvpl'))['maps']
        self.assertEqual(maps['rudniki'], row)
        self.assertEqual(maps['rudniki_evening'], dict(row, id=101, localName=f'{mines}/rudniki_evening.sc2'))
        self.assertNotEqual((prepared/scene.with_name('rudniki_evening.sc2.dvpl')).read_bytes(), original)
        self.assertEqual((prepared/scene.with_name('rudniki_fog.scg.dvpl')).read_bytes(), b'geometry')
        self.assertEqual((data/'maps.yaml.dvpl').read_bytes(), resources[Path('maps.yaml.dvpl')])
        self.assertFalse((data/scene.with_name('rudniki_evening.sc2.dvpl')).exists())

    def test_lighting_restores_originals_after_interrupted_preset_switch(self):
        maps = (client_atmosphere.MINES, '05_amigosville_am')
        original = dvpl.encode(bytes(1024))
        legacy = dvpl.encode(bytes(1023)+b'\x01')
        layout = (digest(original), 32, 150, 200, 320, 8.0)
        self.write_manifest(mines_evening_overlay=True)
        with (patch.object(client_atmosphere, 'SCENES', dict.fromkeys(maps, layout)),
              patch.object(client_atmosphere, 'SUMMER_SCENES', frozenset(maps)),
              patch.object(client_atmosphere, 'LEGACY_MINES_EVENING_SHA256',
                           digest(legacy))):
            for name in maps:
                relative = Path('Data')/client_atmosphere.scene_path(name)
                for root in (self.game, self.client):
                    (root/relative).parent.mkdir(parents=True, exist_ok=True)
                (self.game/relative).write_bytes(original)
                (self.client/relative).write_bytes(legacy if name == client_atmosphere.MINES else original)

            from solbreaker.connection import atomic_write

            def fail_after_one_scene(path, data, **kwargs):
                if path.suffix == '.dvpl' and path.stem == '05_amigosville_am.sc2':
                    raise OSError('interrupted')
                return atomic_write(path, data, **kwargs)

            with patch('solbreaker.connection.atomic_write', side_effect=fail_after_one_scene):
                with self.assertRaises(OSError):
                    client_atmosphere.apply_preset(self.lab, json.loads(self.manifest.read_text()), 'fog')
            self.assertEqual(
                (self.client/'Data'/client_atmosphere.scene_path(client_atmosphere.MINES)).read_bytes(),
                client_atmosphere.scene_preset(original, client_atmosphere.MINES, 'fog'))
            client_atmosphere.apply_preset(self.lab, json.loads(self.manifest.read_text()), 'original')
            for name in maps:
                relative = Path('Data')/client_atmosphere.scene_path(name)
                self.assertEqual((self.client/relative).read_bytes(), original)
                self.assertEqual((self.game/relative).read_bytes(), original)
            self.assertNotIn('map_lighting_overlays', json.loads(self.manifest.read_text()))
            self.assertNotIn('mines_evening_overlay', json.loads(self.manifest.read_text()))

    def test_sunset_replaces_old_overlay_and_restores_excluded_maps(self):
        mines, canyon = client_atmosphere.MINES, '25_canyon_ca'
        scene = bytearray(1024)
        struct.pack_into('<f', scene, 32, 1.)
        original = dvpl.encode(scene)
        layout = (digest(original), 32, 150, 200, 320, 8.)
        with (patch.object(client_atmosphere, 'SCENES', dict.fromkeys((mines, canyon), layout)),
              patch.object(client_atmosphere, 'SUMMER_SCENES', frozenset({mines}))):
            old = dvpl.encode(bytes(1024))
            self.write_manifest(map_lighting_overlays={name: [digest(old)] for name in (mines, canyon)})
            for name in (mines, canyon):
                relative = Path('Data')/client_atmosphere.scene_path(name)
                for root, content in ((self.game, original), (self.client, old)):
                    target = root/relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)

            client_atmosphere.apply_preset(
                self.lab, json.loads(self.manifest.read_text()), 'evening')
            mines_path = self.client/'Data'/client_atmosphere.scene_path(mines)
            canyon_path = self.client/'Data'/client_atmosphere.scene_path(canyon)
            self.assertAlmostEqual(struct.unpack_from('<f', dvpl.read(mines_path), 32)[0], .79)
            self.assertEqual(canyon_path.read_bytes(), original)
            client_atmosphere.apply_preset(
                self.lab, json.loads(self.manifest.read_text()), 'original')
            self.assertEqual(mines_path.read_bytes(), original)

    def test_atgm_patch_recovers_an_interrupted_launch_upgrade(self):
        target = self.client/'wotblitz.exe'
        before, after = b'old image', b'new image'
        target.write_bytes(before)
        self.write_manifest(client_exe_sha256=digest(before))

        with (patch.object(client_atgm_aim, 'INPUT_SHA256', digest(before)),
              patch.object(client_atgm_aim, 'OUTPUT_SHA256', digest(after)),
              patch.object(client_atgm_aim, 'PATCHES', ((0, b'old', b'new'),)),
              patch('solbreaker.management_tasks.require_executable_idle'),
              patch('solbreaker.client_reticle.install', side_effect=lambda lab, manifest: manifest),
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
              patch('solbreaker.client_reticle.install', side_effect=lambda lab, manifest: manifest),
              patch('solbreaker.client_motion_blur.install_motion_blur'),
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
