"""Bounded token issuance must never revoke another account's live credential."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from solbreaker.account import spa_token_response
from solbreaker.auth import LocalAccounts
from solbreaker.market import market_response


class TokenCapacityTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.private = Path(temporary.name)
        self.accounts = LocalAccounts(self.private, 'http://127.0.0.1', autocreate=False)
        self.addCleanup(self.accounts.close)
        self.other = self.accounts.store.authenticate('other@example.test', 'password', autocreate=True).id
        self.third = self.accounts.store.authenticate('third@example.test', 'password', autocreate=True).id

    def test_repeated_issuance_keeps_peer_tokens_and_bounds_each_scope(self):
        for scope in ('access', 'game', 'web', 'market'):
            with self.subTest(scope=scope):
                collection = getattr(self.accounts, scope)
                victim = self.accounts.issue(collection, 1)
                issued = [self.accounts.issue(collection, self.other) for _ in range(140)]
                self.assertTrue(self.accounts.valid(collection, victim))
                self.assertEqual(len(collection), 1 + self.accounts.ACCOUNT_TOKEN_LIMIT)
                self.assertEqual(set(collection), {victim, *issued[-8:]})
                if scope in ('access', 'game'):
                    rows = self.accounts.store.db.execute('SELECT token FROM tokens WHERE kind=?', (scope,))
                    self.assertEqual({row['token'] for row in rows}, set(collection))

    def test_full_pool_rejects_new_owner_and_allows_existing_owner_to_rotate(self):
        with patch.object(LocalAccounts, 'TOKEN_LIMIT', 2), patch.object(LocalAccounts, 'ACCOUNT_TOKEN_LIMIT', 1):
            for scope in ('access', 'game', 'web', 'market'):
                collection = getattr(self.accounts, scope)
                first = self.accounts.issue(collection, 1)
                second = self.accounts.issue(collection, self.other)
                self.assertIsNone(self.accounts.issue(collection, self.third))
                self.assertEqual(set(collection), {first, second})
                replacement = self.accounts.issue(collection, self.other)
                self.assertEqual(set(collection), {first, replacement})

    def test_capacity_failures_have_native_http_and_spa_responses(self):
        with patch.object(LocalAccounts, 'TOKEN_LIMIT', 2), patch.object(LocalAccounts, 'ACCOUNT_TOKEN_LIMIT', 1):
            access = self.accounts.issue(self.accounts.access, self.third)
            for scope in ('game', 'web', 'market'):
                collection = getattr(self.accounts, scope)
                self.accounts.issue(collection, 1)
                self.accounts.issue(collection, self.other)
            response = self.accounts.handle('POST', '/id/api/v2/account/credentials/create/token1/', {},
                                            {'authorization': 'Bearer '+access})
            self.assertEqual(response, (429, {'error': 'local_token_capacity'}, {'Retry-After': '1'}))
            market = market_response(self.accounts, 'POST', '/market/api/v1/oauth/token/',
                                     json.dumps({'token': access}).encode(), {})
            self.assertEqual(market, (429, {'error': 'local_token_capacity'}))
            token = self.accounts.issue(self.accounts.web, self.third)
            self.assertIn(b'LOCAL_SERVICE_UNAVAILABLE', spa_token_response(1, 1, self.third, token))
            self.accounts.issue(self.accounts.access, 1)
            self.assertEqual(self.accounts._access_response(self.other)[0], 429)

    def test_legacy_persisted_tokens_are_normalized_per_owner_and_survive_restart(self):
        victim = self.accounts.issue(self.accounts.game, self.other)
        with self.accounts.store.db:
            self.accounts.store.db.executemany('INSERT INTO tokens VALUES (?,?,?,?)',
                [(f'legacy-{index}', 'game', 1, time.time()+3600) for index in range(120)])
        self.accounts.close()
        restored = LocalAccounts(self.private, 'http://127.0.0.1', autocreate=False)
        self.addCleanup(restored.close)
        self.assertTrue(restored.valid(restored.game, victim))
        self.assertEqual(set(restored.game), {victim, *(f'legacy-{i}' for i in range(112, 120))})
        self.assertEqual(restored.store.db.execute("SELECT count(*) FROM tokens WHERE kind='game'").fetchone()[0], 9)

    def test_failed_persistence_rolls_back_retirement_before_mutating_memory(self):
        victim = self.accounts.issue(self.accounts.game, 1)
        for _ in range(self.accounts.ACCOUNT_TOKEN_LIMIT):
            self.accounts.issue(self.accounts.game, self.other)
        before = dict(self.accounts.game)
        with patch('solbreaker.auth.secrets.token_urlsafe', return_value=victim), self.assertRaises(sqlite3.IntegrityError):
            self.accounts.issue(self.accounts.game, self.other)
        self.assertEqual(self.accounts.game, before)
        rows = self.accounts.store.db.execute("SELECT token FROM tokens WHERE kind='game'")
        self.assertEqual({row['token'] for row in rows}, set(before))
