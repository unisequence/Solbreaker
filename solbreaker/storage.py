"""Persistent local identities, inventories and exactly-once battle results."""
from dataclasses import asdict, dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time

from .garage import battle_ammo_settlement, settle_battle_ammo
from .economy import STARTING_CREDITS, STARTING_GOLD


@contextmanager
def account_access(private):
    """Exclude offline account edits while the production CLI server is live."""
    from .platform_io import PrivateFileBusy, private_lock
    try:
        with private_lock(private, 'server.lock'):
            yield
    except PrivateFileBusy:
        raise ValueError('This lab is in use; stop its server before offline garage edits') from None


def initial_progress():
    return dict(revision=1, credits=STARTING_CREDITS, gold=STARTING_GOLD,
                battles=0, wins=0, shots=0, hits=0,
                last_result=None)


@dataclass(frozen=True)
class Account:
    id: int
    username: str
    nickname: str
    inventory: dict
    progress: dict

    def identity(self):
        return dict(id=self.id, username=self.username, nickname=self.nickname)


class RegistrationError(ValueError):
    def __init__(self, field, code, message):
        super().__init__(message)
        self.field, self.code = field, code


class AccountStore:
    def __init__(self, private):
        self.path = private / 'accounts.sqlite3'
        self.db = sqlite3.connect(self.path)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
                nickname TEXT NOT NULL, salt BLOB NOT NULL, password_hash BLOB NOT NULL,
                inventory TEXT NOT NULL, progress TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS results (
                arena_id TEXT NOT NULL, account_id INTEGER NOT NULL REFERENCES accounts(id),
                payload TEXT NOT NULL, PRIMARY KEY(arena_id,account_id));
            CREATE TABLE IF NOT EXISTS tokens (
                token TEXT PRIMARY KEY, kind TEXT NOT NULL,
                account_id INTEGER NOT NULL REFERENCES accounts(id), expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS battle_reports (
                arena_id TEXT NOT NULL, account_id INTEGER NOT NULL,
                unique_id INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(arena_id,account_id), UNIQUE(account_id,unique_id),
                FOREIGN KEY(arena_id,account_id) REFERENCES results(arena_id,account_id));
            CREATE TABLE IF NOT EXISTS result_notifications (
                account_id INTEGER NOT NULL, unique_id INTEGER NOT NULL,
                created REAL NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(account_id,unique_id),
                FOREIGN KEY(account_id,unique_id) REFERENCES battle_reports(account_id,unique_id));
            CREATE INDEX IF NOT EXISTS pending_result_notifications
                ON result_notifications(account_id,delivered);
            CREATE TABLE IF NOT EXISTS training_settings (
                account_id INTEGER PRIMARY KEY REFERENCES accounts(id), payload TEXT NOT NULL);
        ''')
        if not self.db.execute('SELECT 1 FROM accounts LIMIT 1').fetchone():
            self._migrate(private)

    @staticmethod
    def password_hash(password, salt):
        return hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 200_000)

    def _migrate(self, private):
        old = private / 'account.json'
        if old.exists():
            identity = json.loads(old.read_text())
        else:
            salt = secrets.token_bytes(16)
            identity = dict(id=1, username='local@solbreaker.test', nickname='LocalCommander',
                            salt=salt.hex(), password_hash=self.password_hash('localpoc', salt).hex())
        progress = initial_progress()
        if (private / 'progress.json').exists():
            progress.update(json.loads((private / 'progress.json').read_text()))
        recent = progress.pop('recent_ids', [])
        with self.db:
            self.db.execute('INSERT INTO accounts VALUES (?,?,?,?,?,?,?)',
                (identity['id'], identity['username'].casefold(), identity['nickname'],
                 bytes.fromhex(identity['salt']), bytes.fromhex(identity['password_hash']),
                 '{}', json.dumps(progress)))
            for result_id in recent:
                self.db.execute('INSERT OR IGNORE INTO results VALUES (?,?,?)',
                                (result_id, identity['id'], '{}'))
            sessions = private / 'sessions.json'
            if sessions.exists():
                for kind, tokens in json.loads(sessions.read_text()).items():
                    if kind in ('access', 'game'):
                        for token, session in tokens.items():
                            if session['account_id'] == identity['id'] and session['expires'] > time.time():
                                self.db.execute('INSERT OR IGNORE INTO tokens VALUES (?,?,?,?)',
                                    (token, kind, session['account_id'], session['expires']))

    def get(self, account_id):
        row = self.db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown local account')
        return Account(row['id'], row['username'], row['nickname'],
                       json.loads(row['inventory']), json.loads(row['progress']))

    def training_config(self, account_id, default=None):
        from .training_config import TrainingConfig
        row = self.db.execute('SELECT payload FROM training_settings WHERE account_id=?',
                              (account_id,)).fetchone()
        return TrainingConfig.from_dict(json.loads(row['payload'])) if row else default

    def save_training_config(self, account_id, config):
        from .training_config import TrainingConfig
        if not isinstance(config, TrainingConfig):
            raise ValueError('Invalid account training settings')
        with self.db:
            self.db.execute('INSERT INTO training_settings VALUES (?,?) '
                            'ON CONFLICT(account_id) DO UPDATE SET payload=excluded.payload',
                            (account_id, json.dumps(config.to_dict())))

    def _registration_available(self, username, nickname):
        row = self.db.execute('SELECT username FROM accounts WHERE username=? OR lower(nickname)=? '
                              'ORDER BY username=? DESC LIMIT 1',
                              (username, nickname.casefold(), username)).fetchone()
        if row is not None:
            if row['username'] == username:
                raise RegistrationError('email', 'email_exists', 'Эта почта уже зарегистрирована')
            raise RegistrationError('nickname', 'nickname_exists',
                                    f'Игровое имя «{nickname}» уже занято. Выбери другое имя')

    @staticmethod
    def registration_nickname(nickname):
        if not isinstance(nickname, str) or re.fullmatch(r'[A-Za-z0-9_]{3,24}', nickname) is None:
            raise RegistrationError('nickname', 'invalid_nickname',
                                    'Ник должен содержать от 3 до 24 латинских букв, цифр или подчёркиваний')
        return nickname

    def prepare_registration(self, email, password, nickname):
        if not isinstance(email, str):
            raise RegistrationError('email', 'invalid_email', 'Укажи корректную почту')
        username = email.strip().casefold()
        if (not 3 <= len(username) <= 80 or username.count('@') != 1
                or re.fullmatch(r'[a-z0-9][a-z0-9_.+\-]*@[a-z0-9.-]+', username) is None):
            raise RegistrationError('email', 'invalid_email', 'Укажи корректную почту')
        local, domain = username.split('@')
        labels = domain.split('.')
        if ('..' in local or local.endswith('.') or len(labels) < 2
                or not labels[-1].isalpha() or len(labels[-1]) < 2
                or any(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) is None
                       for label in labels)):
            raise RegistrationError('email', 'invalid_email', 'Укажи корректную почту')
        if not isinstance(password, str) or not 1 <= len(password) <= 256:
            raise RegistrationError('password', 'invalid_password', 'Укажи пароль длиной от 1 до 256 символов')
        nickname = self.registration_nickname(nickname)
        self._registration_available(username, nickname)
        return username, nickname, secrets.token_bytes(16)

    def complete_registration(self, username, nickname, salt, digest):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._registration_available(username, nickname)
            cursor = self.db.execute('''INSERT INTO accounts
                (username,nickname,salt,password_hash,inventory,progress) VALUES (?,?,?,?,?,?)''',
                (username, nickname, salt, digest, '{}', json.dumps(initial_progress())))
        return self.get(cursor.lastrowid)

    def prepare_login(self, username, password, *, autocreate=False):
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        username = username.strip().casefold()
        if not re.fullmatch(r'[a-z0-9_.@+\-]{3,80}', username) or not 1 <= len(password) <= 256:
            return None
        row = self.db.execute('SELECT salt FROM accounts WHERE username=?', (username,)).fetchone()
        if row is None and not autocreate:
            return None
        return username, row['salt'] if row is not None else secrets.token_bytes(16)

    def complete_login(self, username, salt, digest, *, autocreate=False):
        # Called on the SQLite owner's thread after the expensive hash is done.
        row = self.db.execute('SELECT * FROM accounts WHERE username=?', (username,)).fetchone()
        if row is None:
            if not autocreate:
                return None
            with self.db:
                cursor = self.db.execute('''INSERT INTO accounts
                    (username,nickname,salt,password_hash,inventory,progress) VALUES (?,?,?,?,?,?)''',
                    (username, username.split('@')[0][:24], salt, digest, '{}', json.dumps(initial_progress())))
            return self.get(cursor.lastrowid)
        if row['salt'] != salt or not hmac.compare_digest(digest, row['password_hash']):
            return None
        return self.get(row['id'])

    def authenticate(self, username, password, *, autocreate=False):
        seed = self.prepare_login(username, password, autocreate=autocreate)
        if seed is None:
            return None
        return self.complete_login(*seed, self.password_hash(password, seed[1]),
                                   autocreate=autocreate)

    def inventory(self, account_id, game, definition=None):
        account = self.get(account_id)
        if not account.inventory:
            from pathlib import Path
            from .vehicle_data import load_vehicle
            self.save_inventory(account_id, (definition or load_vehicle(Path(game))).inventory)
            return self.get(account_id).inventory
        return account.inventory

    def save_inventory(self, account_id, inventory):
        with self.db:
            account = self.get(account_id)
            progress = account.progress
            progress['revision'] += 1
            self.db.execute('UPDATE accounts SET inventory=?,progress=? WHERE id=?',
                (json.dumps(inventory), json.dumps(progress), account_id))

    def save_diary(self, account_id, data):
        # Pinned Diary.proto is client-owned UI progress, never a Python pickle.
        # Retain the native bytes, including fields unknown to this server.
        if not isinstance(data, bytes) or len(data) > 4096:
            raise ValueError('Invalid native diary size')
        with self.db:
            progress = self.get(account_id).progress
            encoded = data.hex()
            if progress.get('diary', '') == encoded:
                return
            progress.update(diary=encoded, revision=progress['revision']+1)
            self.db.execute('UPDATE accounts SET progress=? WHERE id=?',
                            (json.dumps(progress), account_id))

    def record(self, battle, *, report=None, inventory_id=None, vehicle_xp=0, free_xp=0, ammo=None):
        """Record one battle result exactly once; None if it was already recorded.

        `ammo` (see garage.battle_ammo_settlement) settles the vehicle's ammunition in the
        same transaction after rewards; its outcome is stored as result['ammo']. A changed
        garage vehicle or an impossible remaining count rolls back the whole result.
        """
        for name, value in (('vehicle_xp', vehicle_xp), ('free_xp', free_xp)):
            if type(value) is not int or not 0 <= value <= 2**31-1:
                raise ValueError(f'{name} must be a non-negative integer')
        if inventory_id is None:
            if vehicle_xp or free_xp:
                raise ValueError('Battle XP requires an inventory_id')
            if ammo is not None:
                raise ValueError('Battle ammunition requires an inventory_id')
        elif type(inventory_id) is not int or not 0 < inventory_id <= 2**31-1:
            raise ValueError('inventory_id must be a positive integer')
        if ammo is not None:
            ammo = battle_ammo_settlement(ammo)
        earned = battle.reward
        reward = earned.credits if earned is not None else (500 if battle.won else 100)
        if earned is not None and inventory_id is not None and (
                inventory_id != battle.inventory_id or
                vehicle_xp != earned.vehicle_xp or free_xp != earned.free_xp):
            raise ValueError('Battle XP does not match its reward')
        if report is not None and (report['account_id']!=battle.account_id
                or report['credits']!=reward or not 0<report['unique_id']<2**63):
            raise ValueError('Battle report does not match its result')
        if earned is not None and report is not None and inventory_id is not None and (
                report.get('vehicle_xp') != vehicle_xp or report.get('free_xp') != free_xp
                or report.get('inventory_id') != inventory_id):
            raise ValueError('Battle report XP does not match its result')
        if earned is not None and report is not None:
            own = next((v for v in report['vehicles'] if v['entity_id']==report['vehicle_id']), None)
            if (own is None or own.get('economy') != asdict(earned)
                    or own.get('credits') != earned.credits):
                raise ValueError('Battle report breakdown does not match its reward')
        result = dict(id=battle.result_id, time=datetime.now(timezone.utc).isoformat(),
                      won=battle.won, shots=battle.shots, hits=battle.hits,
                      damage=battle.damage, credits=reward, reason=battle.reason)
        if earned is not None:
            result['economy'] = asdict(earned)
        if inventory_id is not None:
            result.update(inventory_id=inventory_id, vehicle_xp=vehicle_xp, free_xp=free_xp)
        with self.db:
            inserted = self.db.execute('INSERT OR IGNORE INTO results VALUES (?,?,?)',
                (battle.result_id, battle.account_id, json.dumps(result))).rowcount
            if not inserted:
                return None
            if inventory_id is not None:
                applied_xp, applied_free = self._award_xp(
                    battle.account_id, inventory_id, vehicle_xp, free_xp)
                if (applied_xp, applied_free) != (vehicle_xp, free_xp):
                    result.update(applied_vehicle_xp=applied_xp, applied_free_xp=applied_free)
            account = self.get(battle.account_id)
            data = account.progress
            # Native balances are signed int32. Reaching the balance limit
            # must not roll back a completed battle, its report or credits.
            # Results retain earned rewards; applied_* records any cap loss.
            applied_credits = min(reward, max(0, 2**31-1-data['credits']))
            if applied_credits != reward:
                result['applied_credits'] = applied_credits
            data['credits'] += applied_credits
            if ammo is not None:
                result['ammo'] = settle_battle_ammo(self.db, battle.account_id, inventory_id,
                                                    ammo, data, account.inventory)
            if 'ammo' in result or any(key.startswith('applied_') for key in result):
                self.db.execute('UPDATE results SET payload=? WHERE arena_id=? AND account_id=?',
                    (json.dumps(result), battle.result_id, battle.account_id))
            if report is not None:
                # Only actual debits belong in the financial report. A failed
                # refill also contains a quote, but did not charge the account.
                ammunition = result.get('ammo', {})
                charged = (list(ammunition['cost']) if ammunition.get('refill') == 'refilled'
                           else [0, 0])
                settled_report = dict(report)
                if 'ammo' in result or 'auto_load_cost' in report:
                    settled_report['auto_load_cost'] = charged
                self.db.execute('INSERT INTO battle_reports VALUES (?,?,?,?)',
                    (battle.result_id,battle.account_id,report['unique_id'],json.dumps(settled_report)))
                self.db.execute('INSERT INTO result_notifications(account_id,unique_id,created) VALUES (?,?,?)',
                    (battle.account_id,report['unique_id'],time.time()))
            data.update(revision=data['revision']+1,
                        battles=data['battles']+1, wins=data['wins']+int(battle.won),
                        shots=data['shots']+battle.shots, hits=data['hits']+battle.hits,
                        last_result=result)
            self.db.execute('UPDATE accounts SET inventory=?,progress=? WHERE id=?',
                            (json.dumps(account.inventory), json.dumps(data), battle.account_id))
        if report is not None and 'auto_load_cost' in settled_report:
            # Publish the committed costs to the live send only after commit.
            # Exceptions and duplicate receipts leave the supplied report alone.
            report.update(auto_load_cost=settled_report['auto_load_cost'])
        return result

    def _award_xp(self, account_id, inventory_id, vehicle_xp, free_xp):
        # Runs inside record()'s transaction; any error there rolls back the whole result.
        tables = self.db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' "
                                 "AND name IN ('garage_profiles','garage_vehicles')").fetchone()[0]
        profile = self.db.execute('SELECT free_xp FROM garage_profiles WHERE account_id=?',
                                  (account_id,)).fetchone() if tables == 2 else None
        if profile is None:
            raise ValueError('Garage is not initialized')
        vehicle = self.db.execute('SELECT xp FROM garage_vehicles WHERE account_id=? AND inventory_id=?',
                                  (account_id, inventory_id)).fetchone()
        if vehicle is None:
            raise ValueError('Unknown garage vehicle')
        vehicle_xp = min(vehicle_xp, max(0, 2**31-1-vehicle[0]))
        free_xp = min(free_xp, max(0, 2**31-1-profile[0]))
        self.db.execute('UPDATE garage_vehicles SET xp=xp+? WHERE account_id=? AND inventory_id=?',
                        (vehicle_xp, account_id, inventory_id))
        self.db.execute('UPDATE garage_profiles SET free_xp=free_xp+? WHERE account_id=?',
                        (free_xp, account_id))
        return vehicle_xp, free_xp

    def battle_report(self, account_id, unique_id):
        if not 0<unique_id<2**63:
            return None
        row = self.db.execute('SELECT payload FROM battle_reports WHERE account_id=? AND unique_id=?',
                              (account_id,unique_id)).fetchone()
        return json.loads(row[0]) if row else None

    def pending_notification(self, account_id):
        row = self.db.execute('''SELECT b.payload,n.created FROM result_notifications n
            JOIN battle_reports b ON b.account_id=n.account_id AND b.unique_id=n.unique_id
            WHERE n.account_id=? AND n.delivered=0 ORDER BY n.rowid LIMIT 1''',
            (account_id,)).fetchone()
        return (json.loads(row['payload']),row['created']) if row else None

    def acknowledge_notification(self, account_id, unique_id):
        # Transport acknowledgment never changes a report or its award.
        with self.db:
            return bool(self.db.execute('''UPDATE result_notifications SET delivered=1
                WHERE account_id=? AND unique_id=? AND delivered=0''',
                (account_id,unique_id)).rowcount)

    def close(self):
        self.db.close()
