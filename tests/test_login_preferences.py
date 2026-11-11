"""Native automatic-login preference and compatible server display names."""
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from solbreaker.connection import regions_config
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
