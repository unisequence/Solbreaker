"""Paint follows the owned tank through persistent and native battle state."""
import json
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from solbreaker.account import command_response
from solbreaker.arena import Arena
from solbreaker.base import BaseEndpoint
from solbreaker.battle import pb_int, spawn_message_parts
from solbreaker.garage import GarageStore
from solbreaker.profile import account_snapshot
from solbreaker.storage import AccountStore


class CamouflageTests(unittest.TestCase):
    def test_native_request_equips_removes_and_rejects_incompatible_paint(self):
        paint = (60 << 8) | 0xf8
        endpoint = BaseEndpoint.__new__(BaseEndpoint)
        endpoint.server = SimpleNamespace(manifest={'game': '/game'}, event=Mock())
        endpoint.progression = Mock()
        endpoint.progression.prepare.return_value = {'vehicles': [{'id': 306, 'type_cd': 0x4f21}]}
        endpoint.progression_catalog = {'rev': 13}
        endpoint.require_vehicle_available = Mock()
        endpoint.progression_update = Mock(return_value=b'update')
        client = SimpleNamespace(phase='account', account_created=True, arena_id=None, account_id=2)

        def request(value):
            body = struct.pack('<HHqiii', 239, 112, 13, 306, 1, value)
            return endpoint.vehicle_camouflage(client, 141, body, 239)

        with patch('solbreaker.camouflage.compatible_paints', return_value={'inferno': paint}):
            self.assertEqual(request(paint), b'update' + command_response(239, 0))
            endpoint.progression.garage.set_camouflage.assert_called_once_with(2, 306, paint)
            endpoint.progression.garage.set_camouflage.reset_mock()
            self.assertEqual(request(0), b'update' + command_response(239, 0))
            endpoint.progression.garage.set_camouflage.assert_called_once_with(2, 306, 0)
            endpoint.progression.garage.set_camouflage.reset_mock()
            self.assertEqual(request(0xffff), command_response(
                239, -1, message='This paint is incompatible with the vehicle'))
            endpoint.progression.garage.set_camouflage.assert_not_called()

    def test_paint_survives_reopen_and_can_be_cleared(self):
        paint = (60 << 8) | 0xf8
        with TemporaryDirectory() as directory:
            private = Path(directory)
            store = AccountStore(private)
            try:
                descriptor = (b'\x01\x01' + bytes(13)).hex()
                with store.db:
                    store.db.execute('UPDATE accounts SET inventory=? WHERE id=1', (
                        json.dumps(dict(descriptor=descriptor, shells=[10, 4])),))
                garage = GarageStore(store)
                garage.ensure(1)
                start = store.get(1).progress
                garage.set_camouflage(1, 1, paint)
                garage.set_camouflage(1, 1, paint)
                updated = store.get(1).progress
                self.assertEqual(updated['revision'], start['revision'] + 1)
                self.assertEqual(updated['credits'], start['credits'])
            finally:
                store.close()

            store = AccountStore(private)
            try:
                garage = GarageStore(store)
                snapshot = garage.snapshot(1)
                self.assertEqual(snapshot['vehicles'][0]['camouflage'], paint)
                with patch('solbreaker.loadouts.native_inventory', return_value=({}, {})):
                    profile = account_snapshot(None, private, updated, garage=snapshot)
                self.assertEqual(profile['inventory'][1]['camos'][1], [paint, paint, paint, 0, 0, 0])
                garage.set_camouflage(1, 1, 0)
                self.assertEqual(garage.snapshot(1)['vehicles'][0]['camouflage'], 0)
            finally:
                store.close()

    def test_battle_uses_descriptor_in_native_vehicle_info(self):
        paint = (60 << 8) | 0xf8
        accounts = [dict(id=1, nickname='Painted', camouflage=paint), dict(id=2, nickname='Stock')]
        arena = Arena(accounts, b'', SimpleNamespace(height=lambda x, z: 0.), now=1., solo=False)
        self.addCleanup(arena.close)
        self.assertEqual(arena.vehicle_for(1).camouflage, paint)
        self.assertEqual(arena.vehicle_for(2).camouflage, 0)
        for player in arena.players.values():
            roster = spawn_message_parts(arena, player)[0]
            self.assertEqual(roster.count(pb_int(13, paint)), 1)
