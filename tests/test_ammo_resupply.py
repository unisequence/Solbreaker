"""Default and migrated automatic ammunition replenishment."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from solbreaker.entities import BattleResult
from solbreaker.garage import AMMO_AUTO_RESUPPLY, GarageStore
from solbreaker.storage import AccountStore


class AmmoResupplyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = AccountStore(Path(temporary.name))
        self.addCleanup(self.store.close)
        self.descriptor = (b'\x01\x01'+bytes(13)).hex()
        self.shells = [10, 4, 266, 2]
        with self.store.db:
            self.store.db.execute('UPDATE accounts SET inventory=? WHERE id=1', (
                json.dumps(dict(descriptor=self.descriptor, shells=self.shells)),))
        self.garage = GarageStore(self.store)
        self.garage.ensure(1)

    def vehicle(self, inventory_id=1):
        return next(v for v in self.garage.snapshot(1)['vehicles'] if v['id'] == inventory_id)

    def record(self, result_id, initial, remaining):
        battle = BattleResult(result_id, 1, False, 2, 0, 0, 'test', 1)
        return self.store.record(battle, inventory_id=1, ammo=dict(
            expected_descriptor=self.descriptor, initial=initial, remaining=remaining,
            allowed_shells=[10, 266], capacity=6, layout_key=(3, 4),
            prices={10: ((50, 0), (50, 0), 25), 266: ((0, 5), (200, 0), 100)}))

    def test_first_battle_refills_default_loadout_and_keeps_gold(self):
        self.assertEqual(self.vehicle()['ammo_settings'], AMMO_AUTO_RESUPPLY)
        progress = self.store.get(1).progress
        result = self.record('first', self.shells, {10: 3, 266: 1})
        self.assertEqual(result['ammo']['refill'], 'refilled')
        self.assertEqual(result['ammo']['cost'], [250, 0])
        self.assertEqual(self.vehicle()['shells'], self.shells)
        self.assertEqual(self.vehicle()['ammo_layouts'][0]['shells'], [10, 4, -266, 2])
        self.assertEqual(self.store.get(1).progress['credits'], progress['credits'] + result['credits'] - 250)
        self.assertEqual(self.store.get(1).progress['gold'], progress['gold'])
        self.assertIsNone(self.record('first', self.shells, {10: 3, 266: 1}))

    def test_legacy_flags_migrate_once_and_new_tanks_inherit_default(self):
        self.garage.set_ammo_setting(1, 1, AMMO_AUTO_RESUPPLY, False)
        self.garage.set_ammo_setting(1, 1, 8, True)
        with self.store.db:
            self.store.db.execute('PRAGMA user_version=0')
        self.garage = GarageStore(self.store)
        self.assertEqual(self.vehicle()['ammo_settings'], 8 | AMMO_AUTO_RESUPPLY)
        self.garage.set_ammo_setting(1, 1, AMMO_AUTO_RESUPPLY, False)
        self.garage = GarageStore(self.store)
        self.assertEqual(self.vehicle()['ammo_settings'], 8)
        result = self.record('disabled', self.shells, {10: 3, 266: 1})
        self.assertEqual(result['ammo']['refill'], 'disabled')
        self.assertEqual(self.vehicle()['shells'], [10, 3, 266, 1])

        descriptor = (b'\x01\x02'+bytes(13)).hex()
        self.garage.grant_vehicle(1, 513, dict(descriptor=descriptor, shells=self.shells))
        self.assertEqual(self.vehicle(2)['ammo_settings'], AMMO_AUTO_RESUPPLY)
