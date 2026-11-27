"""Native automatic-login preference and compatible server display names."""
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from solbreaker.connection import regions_config
from solbreaker.control_preferences import restore_mouse_sensitivity
from solbreaker.dava_scene import Archive
from solbreaker.login_preferences import automatic_login_archive, configure_automatic_login
from solbreaker.management import Settings, command


def string(value):
    raw = value.encode()
    return b'\x04'+struct.pack('<I', len(raw))+raw


def device_archive():
    entries = [(string('UseAutoLogin'), b'\x01\x01'),
               (string('WGNtoken2'), string('fixture-token')),
               (string('Region'), string('Local PoC'))]
    return b'KA\x01\x00'+struct.pack('<I', len(entries))+b''.join(key+value for key, value in entries)


class LoginPreferencesTests(unittest.TestCase):
    def test_only_the_native_boolean_changes_and_round_trips(self):
        original = device_archive()
        disabled = automatic_login_archive(original, False)
        self.assertEqual(sum(a != b for a, b in zip(original, disabled)), 1)
        reader = Archive(disabled)
        values = reader.load()
        self.assertEqual(values, {'UseAutoLogin': 0, 'WGNtoken2': 'fixture-token', 'Region': 'Local PoC'})
        self.assertEqual(automatic_login_archive(disabled, True), original)

    def test_missing_field_is_added_without_reencoding_existing_values(self):
        original = b'KA\x01\x00'+struct.pack('<I', 1)+string('Region')+string('Local PoC')
        enabled = automatic_login_archive(original, True)
        self.assertEqual(enabled[8:len(original)], original[8:])
        self.assertEqual(Archive(enabled).load()['UseAutoLogin'], 1)

    def test_corrupt_backup_cannot_partially_modify_the_live_archive(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(configure_automatic_login(root, False), 0)
            cache = root/'DAVAProject/cache'
            cache.mkdir(parents=True)
            primary = cache/'device_data.dat'
            primary.write_bytes(device_archive())
            (cache/'device_data.dat.bk').write_bytes(b'unsupported')
            with self.assertRaises(ValueError):
                configure_automatic_login(root, False)
            self.assertEqual(primary.read_bytes(), device_archive())

    def test_mouse_sensitivity_restores_saved_values_into_the_native_cache(self):
        with TemporaryDirectory() as temporary:
            documents = Path(temporary)
            cache = documents/'DAVAProject/cache'
            cache.mkdir(parents=True)
            entries = {
                'MouseSensitivity': '0.403109',
                'MouseSensitivityInSnipperMode': '0.575907',
                'ControlType': 'MouseKeyboard',
            }
            archive = b'KA\x01\x00' + struct.pack('<I', len(entries)) + b''.join(
                string(key) + string(value) for key, value in entries.items())
            main = cache/'game_options_local_options.dat'
            backup = cache/'game_options_local_options.dat.bk'
            main.write_bytes(archive)
            backup.write_bytes(archive)
            controls = documents/'solbreaker-controls.bin'
            controls.write_bytes(struct.pack('<4sIIff', b'SBC1', 0x30, 0, .925259, .752891))

            restore_mouse_sensitivity(documents)
            expected = dict(entries, MouseSensitivity='0.925259', MouseSensitivityInSnipperMode='0.752891')
            self.assertEqual(Archive(main.read_bytes()).load(), expected)
            self.assertEqual(backup.read_bytes(), main.read_bytes())

            modified = main.stat().st_mtime_ns
            controls.write_bytes(struct.pack('<4sIIff', b'SBC1', 0x10, 0, float('nan'), .2))
            restore_mouse_sensitivity(documents)
            self.assertEqual(main.stat().st_mtime_ns, modified)

    def test_launcher_passes_both_checkbox_states_to_the_normal_client_command(self):
        for enabled in (False, True):
            args = command('play', Settings(remember_login=enabled), windows=True)
            self.assertIn('--auto-login' if enabled else '--no-auto-login', args)

    def test_legacy_region_identity_and_input_are_preserved(self):
        original = {'Common': {}, 'Regions': {'Local PoC': {'wgnwn': 'http://127.0.0.1:1'}}}
        network = SimpleNamespace(advertise_http='http://127.0.0.1:18080', advertise_login='127.0.0.1:20016')
        configured = regions_config(original, network)
        self.assertEqual(set(configured['Regions']), {'Local PoC'})
        self.assertEqual(original['Regions']['Local PoC']['wgnwn'], 'http://127.0.0.1:1')
        self.assertEqual(configured['Regions']['Local PoC']['wgextsVersion'], 3)
        self.assertEqual(configured['Common']['wgextsVersion'], 3)

    def test_github_uses_the_unused_community_slot(self):
        urls = {'discord': 'https://example.org/discord'}
        original = {'Common': {}, 'Regions': {'Local PoC': {
            'wgnwn': 'http://127.0.0.1:1', 'communityUrls': {'http': urls}}}}
        network = SimpleNamespace(advertise_http='http://127.0.0.1:18080',
                                  advertise_login='127.0.0.1:20016')
        region = regions_config(original, network)['Regions']['Local PoC']
        self.assertEqual(region['communityUrls']['http'], {
            **urls, 'viber': 'https://github.com/unisequence/Solbreaker'})
        self.assertNotIn('viber', urls)
