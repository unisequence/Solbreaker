from contextlib import closing
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from solbreaker.arena_owner import SettlementData, SettlementRecord
from solbreaker.arena_owner_service import ArenaOwnerService
from solbreaker.arena_owner_settlement import journal_path
from solbreaker.entities import BattleResult
from solbreaker.storage import AccountStore


class OwnerSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovers_partial_settlement_after_restart(self):
        lab = Path(self.enterContext(TemporaryDirectory()))
        private = lab / 'private'
        private.mkdir()
        store = self.enterContext(closing(AccountStore(private)))
        first = store.get(1).id
        second = store.complete_registration('second@example.test', 'Second', b'salt', b'hash').id
        before = {account: store.get(account).progress for account in (first, second)}
        results = (BattleResult('arena-retry', first, True, 2, 1, 300, 'finished'),
                   BattleResult('arena-retry', second, False, 1, 0, 0, 'finished'))
        records = tuple(SettlementRecord(result, {'account_id': result.account_id, 'unique_id': 21,
                                                    'credits': 500 if result.won else 100},
                                         b'\0\xff', None, (), 0, 0)
                        for result in results)
        data = SettlementData('arena-retry', results, records)
        server = SimpleNamespace(lab=lab, accounts=SimpleNamespace(store=store), event=Mock())
        endpoint = SimpleNamespace(server=server, clients={})
        service = ArenaOwnerService(endpoint)
        arena = SimpleNamespace(result_id=data.arena_id, reports={}, recorded_accounts=set(),
                                finished_at=None)
        record = store.record

        def temporarily_locked(result, **kwargs):
            if result.account_id == second:
                raise sqlite3.OperationalError('database is locked')
            return record(result, **kwargs)

        with patch.object(store, 'record', side_effect=temporarily_locked):
            service._on_result(SimpleNamespace(arena=arena),
                               SimpleNamespace(op='motion_light'),
                               SimpleNamespace(settlement=data))
            await service.close()
        spool = journal_path(private / 'owner-settlements', data.arena_id)
        self.assertTrue(spool.is_file())
        self.assertIsNotNone(store.battle_report(first, 21))
        self.assertIsNone(store.battle_report(second, 21))

        store.close()
        recovered = self.enterContext(closing(AccountStore(private)))
        server.accounts.store = recovered
        restarted = ArenaOwnerService(endpoint)
        restarted.recover_settlements()
        await restarted.close()
        self.assertFalse(spool.exists())
        for account in (first, second):
            self.assertIsNotNone(recovered.battle_report(account, 21))
            after = recovered.get(account).progress
            self.assertEqual(after['battles'], before[account]['battles'] + 1)
