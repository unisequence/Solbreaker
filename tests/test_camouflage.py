"""Paint follows the owned tank through persistent and native battle state."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from solbreaker.arena import Arena
from solbreaker.battle import pb_int, spawn_message_parts
from solbreaker.garage import GarageStore
from solbreaker.profile import account_snapshot
from solbreaker.storage import AccountStore


class CamouflageTests(unittest.TestCase):
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
