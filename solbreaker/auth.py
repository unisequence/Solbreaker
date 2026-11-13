"""Local account authentication and account-bound credentials."""
from dataclasses import dataclass
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import secrets
import time


class PasswordQueueFull(ValueError):
    pass


@dataclass(frozen=True)
class Session:
    account_id: int
    expires: float


class LocalAccounts:
    PASSWORD_JOB_LIMIT = 16  # Includes queued/running work; accommodates a 7x7 login batch.
    TOKEN_LIMIT = 128
    ACCOUNT_TOKEN_LIMIT = 8
    SESSION_LIFETIME = 3600
    # The native client stores the game token as WGNtoken2 for the next launch.
    SAVED_LOGIN_LIFETIME = 30 * 24 * 3600

    def __init__(self, private: Path, origin: str, *, autocreate=True):
        from .storage import AccountStore
        self.origin, self.autocreate = origin, autocreate
        self.store = AccountStore(private)
        self.access, self.game, self.web, self.market = {}, {}, {}, {}
        self._password_pool = None
        self._password_jobs = set()
        self._closed = False
        now = time.time()
        with self.store.db:
            self.store.db.execute('DELETE FROM tokens WHERE expires<=?', (now,))
            rows = self.store.db.execute(
                "SELECT * FROM tokens WHERE kind IN ('access','game') ORDER BY rowid").fetchall()
            for row in rows:
                collection = getattr(self, row['kind'])
                removed = self._retired_tokens(collection, row['account_id'], now)
                if removed is None:
                    self.store.db.execute('DELETE FROM tokens WHERE token=? AND kind=?',
                                          (row['token'], row['kind']))
                    continue
                for old in removed:
                    del collection[old]
                    self.store.db.execute('DELETE FROM tokens WHERE token=? AND kind=?',
                                          (old, row['kind']))
                collection[row['token']] = Session(row['account_id'], row['expires'])

    @property
    def account(self):
        return self.get(1).identity()

    def get(self, account_id):
        return self.store.get(account_id)

    @classmethod
    def _retired_tokens(cls, collection, account_id, now):
        """Plan expiry and issuer-only eviction, or refuse a pool held by peers."""
        expired = [token for token, session in collection.items() if session.expires <= now]
        live = [(token, session) for token, session in collection.items() if session.expires > now]
        owned = [token for token, session in live if session.account_id == account_id]
        count = max(0, len(owned) - cls.ACCOUNT_TOKEN_LIMIT + 1, len(live) - cls.TOKEN_LIMIT + 1)
        if count > len(owned):
            return None
        return expired + owned[:count]

    def issue(self, collection, account_id=1):
        """Return a new token, or None when other accounts hold all capacity."""
        self.get(account_id)
        now = time.time()
        removed = self._retired_tokens(collection, account_id, now)
        if removed is None:
            return None
        token = secrets.token_urlsafe(32)
        expires = now + (self.SAVED_LOGIN_LIFETIME if collection is self.game else self.SESSION_LIFETIME)
        if collection is self.access or collection is self.game:
            kind = 'access' if collection is self.access else 'game'
            with self.store.db:
                self.store.db.execute('DELETE FROM tokens WHERE expires<=?', (now,))
                self.store.db.executemany('DELETE FROM tokens WHERE token=? AND kind=?',
                                         ((old, kind) for old in removed))
                self.store.db.execute('INSERT INTO tokens VALUES (?,?,?,?)',
                                     (token, kind, account_id, expires))
        for old in removed:
            del collection[old]
        collection[token] = Session(account_id, expires)
        return token

    @staticmethod
    def valid(collection, token):
        if not isinstance(token, str):
            return False
        session = collection.get(token)
        return session is not None and session.expires > time.time()

    async def _password_hash(self, password, salt):
        if len(self._password_jobs) >= self.PASSWORD_JOB_LIMIT:
            raise PasswordQueueFull('Local password queue is full')
        if self._password_pool is None:
            self._password_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='local-password')
        future = asyncio.get_running_loop().run_in_executor(
            self._password_pool, self.store.password_hash, password, salt)
        self._password_jobs.add(future)
        future.add_done_callback(self._password_jobs.discard)
        # A disconnected HTTP caller cannot release the slot while its hash
        # still runs. Workers never read SQLite or mutate accounts/tokens.
        return await asyncio.shield(future)

    async def handle_async(self, method, path, fields, headers):
        if self._closed:
            return 503, {'error': 'local_server_stopping'}, {}
        if method == 'POST' and path == '/registration/api/v3/account/basic/':
            from .storage import RegistrationError
            def registration_value(name):
                values = fields.get(name, [])
                return values[0] if len(values) == 1 else ''
            try:
                account = await self.register(registration_value('login'),
                                              registration_value('password'), registration_value('name'))
            except RegistrationError as error:
                return 400, {'error': str(error)}, {}
            except PasswordQueueFull:
                return 429, {'error': 'Сервер занят. Попробуй ещё раз через несколько секунд.'}, {'Retry-After': '1'}
            return 200, {'status': 'ok', 'spa_id': account.id}, {}
        if (method != 'POST' or path != '/id/api/v2/account/credentials/create/oauth/token/'
                or fields.get('grant_type') == ['urn:wargaming:params:oauth:grant-type:token1']):
            return self.handle(method, path, fields, headers)
        def value(name):
            values = fields.get(name, [])
            return values[0] if len(values) == 1 else ''
        username, password = value('username'), value('password')
        seed = self.store.prepare_login(username, password, autocreate=self.autocreate)
        account = None
        if seed is not None:
            try:
                digest = await self._password_hash(password, seed[1])
                if self._closed:
                    return 503, {'error': 'local_server_stopping'}, {}
                # Another login may have created this username while hashing.
                # Rehash against its committed salt, never overwrite its password.
                current = self.store.db.execute('SELECT salt FROM accounts WHERE username=?',
                                                (seed[0],)).fetchone()
                if current is not None and current['salt'] != seed[1]:
                    seed = seed[0], current['salt']
                    digest = await self._password_hash(password, seed[1])
                    if self._closed:
                        return 503, {'error': 'local_server_stopping'}, {}
                account = self.store.complete_login(*seed, digest, autocreate=self.autocreate)
            except PasswordQueueFull:
                return 429, {'error': 'local_login_busy'}, {'Retry-After': '1'}
        if account is None:
            return 401, {'error': 'invalid_grant', 'error_description': 'Invalid local credentials'}, {}
        return self._access_response(account.id)

    async def register(self, email, password, nickname):
        if self._closed:
            raise ValueError('Local server is stopping')
        username, nickname, salt = self.store.prepare_registration(email, password, nickname)
        digest = await self._password_hash(password, salt)
        if self._closed:
            raise ValueError('Local server is stopping')
        return self.store.complete_registration(username, nickname, salt, digest)

    def _access_response(self, account_id):
        token = self.issue(self.access, account_id)
        if token is None:
            return 429, {'error': 'local_token_capacity'}, {'Retry-After': '1'}
        return 200, {'access_token': token, 'token_type': 'Bearer', 'scope': 'wotb',
                     'user': account_id, 'expires_in': str(self.SESSION_LIFETIME)}, {}

    def close(self):
        self._closed = True
        if self._password_pool is not None:
            self._password_pool.shutdown(wait=False, cancel_futures=True)
        self.store.close()

    def handle(self, method, path, fields, headers):
        def value(name):
            values = fields.get(name, [])
            return values[0] if len(values) == 1 else ""

        if method == 'POST' and path == '/registration/api/v3/settings/':
            return 200, {}, {}
        if method == 'GET' and path == '/registration/api/v3/account/basic/challenge/':
            return 202, {}, {'Location': self.origin + '/pow'}
        if method == 'GET' and path.startswith('/personal/account/nicknames/'):
            from .storage import RegistrationError
            from urllib.parse import unquote
            nickname = unquote(path.removeprefix('/personal/account/nicknames/').removesuffix('/'))
            try:
                self.store.registration_nickname(nickname)
            except RegistrationError as error:
                return 400, {'error': str(error)}, {}
            # The native precheck discards server error text. Check syntax here,
            # then report uniqueness with its full message on account creation.
            return 200, {'status': 'ok'}, {}
        prefix = "/id/api/v2/account/credentials/create/"
        if method == "GET" and path == prefix + "oauth/token/challenge/create/":
            return 202, {}, {"Location": self.origin + "/pow"}
        if method == "GET" and path == "/pow":
            # The local PoC does not require costly anti-bot work. This object
            # satisfies the native client's mandatory challenge step.
            return 200, {"pow": {"complexity": 0, "random_string": "local-poc",
                "timestamp": int(time.time()), "algorithm": {"version": 1,
                "extension": "", "resourse": "local-poc"}}}, {}
        if method == "POST" and path == prefix + "oauth/token/":
            if value("grant_type") == "urn:wargaming:params:oauth:grant-type:token1":
                if not self.valid(self.web, value("token")):
                    return 401, {"error": "invalid_grant"}, {}
                account_id = self.web[value("token")].account_id
                if value("account_id") != str(account_id):
                    return 401, {"error": "invalid_grant"}, {}
            else:
                account = self.store.authenticate(value("username"), value("password"),
                                                  autocreate=self.autocreate)
                if account is None:
                    return 401, {"error": "invalid_grant", "error_description": "Invalid local credentials"}, {}
                account_id = account.id
            return self._access_response(account_id)
        if method == "POST" and path in (prefix + "token1/", "/id/api/v2/account/info/"):
            scheme, _, token = headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not self.valid(self.access, token):
                return 401, {"error": "invalid_token"}, {}
            account = self.get(self.access[token].account_id)
            if path == prefix + "token1/":
                token = self.issue(self.game, account.id)
                if token is None:
                    return 429, {'error': 'local_token_capacity'}, {'Retry-After': '1'}
                return 200, {"account_id": account.id, "token": token}, {}
            return 200, {"email": account.username, "phone": "",
                "minor_restrictions": {"lootboxes_denied": False, "communication_denied": False}}, {}
        return None
