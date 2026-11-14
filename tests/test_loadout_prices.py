"""Native loadout prices and their corresponding garage transactions."""
import json
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from solbreaker.account import command_response, pickle_document
from solbreaker.base import BaseEndpoint
from solbreaker.catalog import SHOP_REVISION, _loadout_prices, equipment_slot_credits, supply_credits
from solbreaker.garage import GarageStore
from solbreaker.loadouts import COMPATIBLE, CONSUMABLE, OPTIONAL_DEVICE, PROVISION, ItemSpec, LoadoutCatalog
from solbreaker.storage import AccountStore


class LoadoutPricesTests(unittest.TestCase):
    def setUp(self):
        self.fuel = ItemSpec('regular_fuel', 18, PROVISION, None, (), 'common', (), extras=tuple(
            (f'price/level{tier}', str(price))
            for tier, price in enumerate((0, 0, 0, 150, 250, 1100, 1300, 1500, 1700, 2000), 1)))
        self.food = ItemSpec('food', 1, PROVISION, None, (), 'radical', ())
        self.basic_food = ItemSpec('basic_food', 4, PROVISION, None, (), 'common', ())
        self.free = ItemSpec('free_supply', 2, CONSUMABLE, None, (), 'free', ())
        self.kit = ItemSpec('repair_kit', 3, CONSUMABLE, None, (), 'common', ())
        self.catalog = LoadoutCatalog(Path('fixture'), (), (self.fuel, self.food, self.basic_food),
                                      (self.free, self.kit), ())

    def test_native_prices_keep_free_entries_and_prefer_xml_overrides(self):
        overrides, tables = _loadout_prices(self.catalog)
        self.assertEqual(overrides[11], ({},))
        self.assertEqual(overrides[13][0][0x12fd]['level8'], (1500, 0))
        self.assertEqual(tables['provisionCost']['common'][7], (2250, 0))
        self.assertEqual(tables['consumableCost']['free'], [(0, 0)] * 10)
        for item in (*self.catalog.provisions, *self.catalog.consumables):
            for tier in range(1, 11):
                if item is self.fuel:
                    wire_price = overrides[13][0][0x12fd][f'level{tier}']
                else:
                    wire_price = tables[item.kind + 'Cost'][item.category][tier - 1]
                self.assertEqual(wire_price, (supply_credits(item, tier), 0))
        self.assertTrue(pickle_document({'items': {15: overrides}, **tables}).startswith(b'\x80\x02'))

    def test_native_slot_purchase_replacement_and_sale_share_one_balance(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = AccountStore(Path(temporary.name))
        self.addCleanup(store.close)
        account = store.get(1)
        with store.db:
            store.db.execute('UPDATE accounts SET inventory=?, progress=? WHERE id=1', (
                json.dumps(dict(descriptor=(b'\x01\x01' + bytes(13)).hex(), shells=[10, 1])),
                json.dumps(dict(account.progress, credits=5000))))
        garage = GarageStore(store)
        garage.ensure(1)
        progression = SimpleNamespace(garage=garage, prepare=garage.snapshot,
                                      _decode=Mock(return_value=SimpleNamespace(weapon=SimpleNamespace(clip=1))),
                                      resolve_type=Mock(return_value=SimpleNamespace(tier=8)))
        endpoint = SimpleNamespace(progression=progression, progression_catalog={'rev': SHOP_REVISION},
                                   server=SimpleNamespace(manifest={'game': self.catalog.game}, event=Mock()),
                                   require_vehicle_available=Mock(), progression_update=Mock(return_value=b''))
        client = SimpleNamespace(phase='account', account_created=True, arena_id=None, account_id=1)

        def equip(item):
            cd = (item.ident << 8) | 0xfd if item is not None else 0
            body = struct.pack('<HHB5i', 42, 110, 5, SHOP_REVISION, 1, 0, cd, int(item is not None))
            return BaseEndpoint.vehicle_loadout(endpoint, client, 147, body, 42, 110)

        with (patch('solbreaker.loadouts.load_catalog', return_value=self.catalog),
              patch('solbreaker.loadouts.vehicle_key', return_value=('fixture', 'tank')),
              patch.object(LoadoutCatalog, 'vehicle_slots', return_value=(3, 3)),
              patch.object(LoadoutCatalog, 'provision_compatibility', return_value=COMPATIBLE),
              patch('solbreaker.loadout_effects.resolve_provisions')):
            self.assertIn(b'Not enough credits', equip(self.food))
            self.assertEqual(garage.loadout(1, 1)['provisions'], [None, None, None])
            self.assertEqual(store.get(1).progress['credits'], 5000)
            self.assertEqual(equip(self.fuel), command_response(42, 0))
            self.assertEqual(store.get(1).progress['credits'], 3500)
            self.assertEqual(equip(self.fuel), command_response(42, 0))
            self.assertEqual(store.get(1).progress['credits'], 3500)
            self.assertEqual(equip(self.basic_food), command_response(42, 0))
            self.assertEqual(garage.loadout(1, 1)['provisions'], ['basic_food', None, None])
            self.assertEqual(store.get(1).progress['credits'], 2750)
            self.assertEqual(equip(None), command_response(42, 0))
            self.assertEqual(store.get(1).progress['credits'], 5000)

    def test_equipment_unlock_charges_once_and_keeps_switching_free(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = AccountStore(Path(temporary.name))
        self.addCleanup(store.close)
        with store.db:
            store.db.execute('UPDATE accounts SET inventory=?, progress=? WHERE id=1', (
                json.dumps(dict(descriptor=(b'\x01\x01' + bytes(13)).hex(), shells=[10, 1])),
                json.dumps(dict(store.get(1).progress, credits=200001))))
        garage = GarageStore(store)
        garage.ensure(1)
        devices = tuple(ItemSpec(tag, ident, OPTIONAL_DEVICE, None, (), None, ())
                        for ident, tag in ((1, 'rammer'), (2, 'calibrated')))
        catalog = LoadoutCatalog(Path('fixture'), devices, (), (), ())
        progression = SimpleNamespace(garage=garage, prepare=garage.snapshot,
                                      resolve_type=Mock(return_value=SimpleNamespace(tier=8)))
        endpoint = SimpleNamespace(progression=progression, progression_catalog={'rev': SHOP_REVISION},
                                   server=SimpleNamespace(manifest={'game': catalog.game}, event=Mock()),
                                   require_vehicle_available=Mock(), progression_update=Mock(return_value=b''))
        client = SimpleNamespace(phase='account', account_created=True, arena_id=None, account_id=1)

        def unlock(level=0, group=0):
            body = struct.pack('<HHB5i', 42, 130, 5, SHOP_REVISION, 1, level, group, 0)
            return BaseEndpoint.vehicle_loadout(endpoint, client, 147, body, 42, 130)

        with (patch('solbreaker.loadouts.load_catalog', return_value=catalog),
              patch('solbreaker.loadouts.vehicle_key', return_value=('fixture', 'tank')),
              patch.object(LoadoutCatalog, 'vehicle_preset'),
              patch.object(LoadoutCatalog, 'equipment_slots', return_value=((1, 2),) * 9)):
            self.assertIn(b'previous levels', unlock(1))
            self.assertEqual(unlock(), command_response(42, 0))
            self.assertEqual(unlock(), command_response(42, 0))
            self.assertIn(b'Not enough credits', unlock(group=1))
            body = struct.pack('<HHq3i', 42, 133, 1, 0, 0, 1)
            self.assertEqual(BaseEndpoint.vehicle_loadout(endpoint, client, 141, body, 42, 133),
                             command_response(42, 0))
            self.assertEqual(garage.loadout(1, 1)['unlocked'], 1)
            self.assertEqual(garage.loadout(1, 1)['devices'][0], 'calibrated')
            self.assertEqual(store.get(1).progress['credits'], 200001 - equipment_slot_credits(8, 0))
