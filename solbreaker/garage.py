import json
import re
from .economy import STARTING_FREE_XP

MAX_INT = 2**31 - 1


def _int(value, name, *, low=0):
    if type(value) is not int or not low <= value <= MAX_INT:
        raise ValueError(f'{name} must be an integer in {low}..{MAX_INT}')
    return value


def _ints(values, name):
    if isinstance(values, (str, bytes)) or not hasattr(values, '__iter__'):
        raise ValueError(f'{name} must be a sequence of integers')
    return sorted({_int(v, name) for v in values})


def _config(configuration):
    if configuration is None:
        return {}
    if not isinstance(configuration, dict):
        raise ValueError('configuration must be an object')
    try:
        return json.loads(json.dumps(configuration, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise ValueError('configuration must be JSON-serializable') from error


def _inventory(inventory):
    """Validate a {descriptor: hex, shells: list} inventory; return (hex, shells, type_cd)."""
    if not isinstance(inventory, dict):
        raise ValueError('inventory must be an object')
    raw = inventory.get('descriptor')
    try:
        data = bytes(raw) if isinstance(raw, (bytes, bytearray)) else bytes.fromhex(raw)
    except (TypeError, ValueError) as error:
        raise ValueError('inventory descriptor must be hex') from error
    if len(data) != 15 or data[0] & 0x0F != 1:
        raise ValueError('inventory descriptor must be a 15-byte vehicle descriptor')
    shells = inventory.get('shells')
    if not isinstance(shells, list) or any(type(s) is not int or s < 0 for s in shells):
        raise ValueError('inventory shells must be a list of non-negative integers')
    return data.hex(), list(shells), (data[1] << 8) | data[0]


LOADOUT_SIZES = dict(devices=9, provisions=3, consumables=3)
_TAG = re.compile(r'[A-Za-z0-9_-]{1,80}')


def default_loadout():
    return dict(devices=[None] * 9, unlocked=0, provisions=[None] * 3, consumables=[None] * 3)


def _loadout_row(row):
    return dict(devices=json.loads(row['devices']), unlocked=row['unlocked'],
                provisions=json.loads(row['provisions']), consumables=json.loads(row['consumables']))


def _tags(category, values, allowed_tags):
    if category not in LOADOUT_SIZES:
        raise ValueError('unknown loadout category')
    if not isinstance(values, (list, tuple)):
        raise ValueError('values must be a list or tuple')
    if len(values) != LOADOUT_SIZES[category]:
        raise ValueError(f'{category} must have exactly {LOADOUT_SIZES[category]} entries')
    if isinstance(allowed_tags, (str, bytes)) or not hasattr(allowed_tags, '__iter__'):
        raise ValueError('allowed_tags must be a collection of tags')
    allowed = set(allowed_tags)
    seen = set()
    for tag in values:
        if tag is None:
            continue
        if type(tag) is not str or not tag.isascii() or _TAG.fullmatch(tag) is None:
            raise ValueError('loadout tags must be ASCII [A-Za-z0-9_-] strings of 1..80 characters')
        if tag in seen:
            raise ValueError('duplicate loadout tag')
        if tag not in allowed:
            raise ValueError('loadout tag is not allowed')
        seen.add(tag)
    return list(values)


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


# inventory[1]['settings'][invID] flag of native automatic ammunition resupply.
AMMO_AUTO_RESUPPLY = 4


def _counts(shells):
    """Stored flat [cd, count, ...] as an ordered {cd: count}; reject malformed rows."""
    if (not isinstance(shells, list) or len(shells) % 2
            or any(type(v) is not int or v < 0 for v in shells) or len(set(shells[::2])) * 2 != len(shells)):
        raise ValueError('Vehicle does not have a shell loadout')
    return dict(zip(shells[::2], shells[1::2]))


def _conserve(free, old, new):
    """Free-loaded counts after `old` -> `new` changes made without money.

    Increases are trusted grants; decreases may only drop granted shells, so
    paid ammunition is never discarded outside the exchange. This provenance
    bookkeeping does not affect the native cash refund for owned ammunition.
    """
    free = dict(free)
    for cd in set(old) | set(new):
        have, want, gratis = old.get(cd, 0), new.get(cd, 0), free.get(cd, 0)
        if want < have and have - want > gratis:
            raise ValueError('Paid ammunition must be unloaded through the ammunition exchange first')
        free[cd] = gratis + want - have
    return {cd: n for cd, n in free.items() if n}


def _unit_prices(allowed, prices):
    """{cd: ((credits, gold) positive CD, (credits, gold) negative CD, refund)} for exactly `allowed`."""
    if (not isinstance(prices, dict) or any(type(cd) is not int for cd in prices)
            or set(prices) != set(allowed)):
        raise ValueError('prices must cover exactly the compatible shells')
    costs = {}
    for cd in allowed:
        entry = prices[cd]
        if not isinstance(entry, (list, tuple)) or len(entry) != 3 or any(
                not isinstance(p, (list, tuple)) or len(p) != 2 for p in entry[:2]):
            raise ValueError('Invalid shell price')
        costs[cd] = (tuple(_int(v, 'shell price') for v in entry[0]),
                     tuple(_int(v, 'shell price') for v in entry[1]), _int(entry[2], 'shell refund'))
    return costs


def _layout_key(layout_key):
    if not isinstance(layout_key, (list, tuple)) or len(layout_key) != 2:
        raise ValueError('layout_key must be a (turret_cd, gun_cd) pair')
    return tuple(_int(v, 'layout_key') for v in layout_key)


def _load_prefs(db, account_id, inventory_id):
    row = db.execute('SELECT * FROM garage_ammo_prefs WHERE account_id=? AND inventory_id=?',
                     (account_id, inventory_id)).fetchone()
    if row is None:
        return None
    free = json.loads(row['free'])
    return dict(settings=row['settings'], layouts=json.loads(row['layouts']),
                free=dict(zip(free[::2], free[1::2])))


def _default_prefs(counts):
    return dict(settings=AMMO_AUTO_RESUPPLY, layouts=[], free={cd: n for cd, n in counts.items() if n})


def _ammo_layout(prefs, key):
    return next((entry for entry in prefs['layouts'] if (entry['turret_cd'], entry['gun_cd']) == key), None)


def _seed_ammo_layout(prefs, key, allowed, counts, *, credit_shells=()):
    if _ammo_layout(prefs, key) is not None:
        return False
    if any(n and cd not in allowed for cd, n in counts.items()):
        raise ValueError('Stored loadout does not match the installed gun')
    prefs['layouts'].append(dict(turret_cd=key[0], gun_cd=key[1],
        shells=[v for cd in allowed for v in (-cd if cd in credit_shells else cd, counts.get(cd, 0))]))
    return True


def _store_prefs(db, account_id, inventory_id, prefs):
    free = [v for cd in sorted(prefs['free']) for v in (cd, prefs['free'][cd])]
    layouts = sorted(prefs['layouts'], key=lambda e: (e['turret_cd'], e['gun_cd']))
    db.execute('INSERT OR REPLACE INTO garage_ammo_prefs VALUES (?,?,?,?,?)',
               (account_id, inventory_id, prefs['settings'], _dump(layouts), _dump(free)))


_SETTLEMENT_FIELDS = frozenset(('expected_descriptor', 'initial', 'remaining', 'allowed_shells',
                                'capacity', 'prices', 'layout_key'))


def battle_ammo_settlement(ammo):
    """Validate a battle ammunition payload outside any transaction; return a normalized copy.

    Fields, all required and prepared by the caller before AccountStore.record():
      expected_descriptor  stored vehicle descriptor hex at battle entry
      initial              stored garage shells [cd, count, ...] copied at battle entry
      remaining            live {cd: count} (Vehicle.ammo_by_shell with the selected
                           shell's `ammo` written back); every initial CD is present,
                           no count exceeds its initial count, other CDs are 0
      allowed_shells       ordered compatible CDs of the installed gun
      capacity             gun ammunition capacity
      prices               exchange_ammo_loadout() unit prices for exactly allowed_shells
      layout_key           (turret_cd, gun_cd) of the installed configuration
    """
    if not isinstance(ammo, dict) or set(ammo) != _SETTLEMENT_FIELDS:
        raise ValueError('ammo settlement must have exactly: ' + ', '.join(sorted(_SETTLEMENT_FIELDS)))
    if not isinstance(ammo['expected_descriptor'], str):
        raise ValueError('expected_descriptor must be a hex string')
    allowed = GarageStore._shell_ids(ammo['allowed_shells'], 'allowed_shells', 32)
    capacity = _int(ammo['capacity'], 'capacity')
    costs = _unit_prices(allowed, ammo['prices'])
    initial = _counts(ammo['initial'])
    if any(n and cd not in allowed for cd, n in initial.items()) or sum(initial.values()) > capacity:
        raise ValueError('Initial ammunition does not match the installed gun')
    remaining = ammo['remaining']
    if not isinstance(remaining, dict) or any(type(cd) is not int for cd in remaining):
        raise ValueError('remaining must map shell CDs to counts')
    for cd, n in remaining.items():
        if _int(n, 'remaining') > initial.get(cd, 0):
            raise ValueError('Remaining ammunition exceeds the initial loadout')
    if not set(initial) <= set(remaining):
        raise ValueError('Remaining ammunition omits a loaded shell')
    return dict(descriptor=ammo['expected_descriptor'], initial=list(ammo['initial']),
                left={cd: remaining[cd] for cd in initial}, allowed=allowed, capacity=capacity,
                costs=costs, key=_layout_key(ammo['layout_key']))


def settle_battle_ammo(db, account_id, inventory_id, settlement, progress, inventory):
    """Apply a normalized settlement inside the caller's open transaction; never commits.

    Consumption always applies; free (granted) shells are consumed first. With prefs whose
    settings have AMMO_AUTO_RESUPPLY, shortfalls against the current turret/gun desired
    layout are bought all-or-nothing at its signed-CD currency, after battle rewards.
    Refusal never blocks consumption. Mutates `progress` and the selected `inventory`
    projection in place; returns the outcome stored with the battle result.
    """
    if not db.in_transaction:
        raise RuntimeError('Battle ammunition settlement requires an open transaction')
    vehicle = db.execute('SELECT descriptor,shells FROM garage_vehicles WHERE account_id=? AND inventory_id=?',
                         (account_id, inventory_id)).fetchone()
    if vehicle is None:
        raise ValueError('Vehicle is not in the garage')
    if (vehicle['descriptor'] != settlement['descriptor']
            or json.loads(vehicle['shells']) != settlement['initial']):
        raise ValueError('Vehicle ammunition changed since the battle started')
    initial, left = _counts(settlement['initial']), settlement['left']
    used = {cd: initial[cd] - left[cd] for cd in initial if initial[cd] != left[cd]}
    shells = [v for cd in initial for v in (cd, left[cd])]
    outcome = dict(consumed=[v for cd in used for v in (cd, used[cd])], cost=[0, 0])
    prefs = _load_prefs(db, account_id, inventory_id)
    created = prefs is None
    if created:
        prefs = _default_prefs(initial)
    seeded = False
    if prefs['settings'] & AMMO_AUTO_RESUPPLY:
        # Preserve the pre-battle loadout on first use, even if funds run out.
        # An implicit layout uses credits for premium rounds. Saved signed
        # layouts retain the player's currency selection.
        credit_shells = {cd for cd, (price, _, _) in settlement['costs'].items() if price[1]}
        seeded = _seed_ammo_layout(prefs, settlement['key'], settlement['allowed'], initial,
                                   credit_shells=credit_shells)
    free = {cd: n-used.get(cd, 0) for cd, n in prefs['free'].items() if n > used.get(cd, 0)}
    if created or seeded or free != prefs['free']:
        _store_prefs(db, account_id, inventory_id, dict(prefs, free=free))
    outcome['refill'], refilled = _refill(prefs, settlement, progress, outcome)
    if refilled is not None:
        shells = refilled
    outcome['shells'] = shells
    if shells != settlement['initial']:
        db.execute('UPDATE garage_vehicles SET shells=? WHERE account_id=? AND inventory_id=?',
                   (_dump(shells), account_id, inventory_id))
        selected = db.execute('SELECT selected_id FROM garage_profiles WHERE account_id=?',
                              (account_id,)).fetchone()
        if selected is not None and selected[0] == inventory_id:
            inventory.update(descriptor=vehicle['descriptor'], shells=list(shells))
    return outcome


def _refill(prefs, settlement, progress, outcome):
    """(status, shells or None); charges `progress` only when the whole refill is affordable."""
    if not prefs['settings'] & AMMO_AUTO_RESUPPLY:
        return 'disabled', None
    entry = _ammo_layout(prefs, settlement['key'])
    layout = entry['shells']
    targets = {abs(signed): (signed, n) for signed, n in zip(layout[::2], layout[1::2])}
    allowed, left = settlement['allowed'], settlement['left']
    if set(targets) != set(allowed):
        return 'stale_layout', None
    # Refill only tops up shortfalls; surplus shells are never unloaded implicitly.
    want = {cd: max(left.get(cd, 0), targets[cd][1]) for cd in allowed}
    if sum(want.values()) > settlement['capacity']:
        return 'over_capacity', None
    debit_credits = debit_gold = 0
    for cd in allowed:
        signed, target = targets[cd]
        missing = target - left.get(cd, 0)
        if missing > 0:
            positive, negative, _ = settlement['costs'][cd]
            unit_credits, unit_gold = negative if signed < 0 else positive
            debit_credits += missing * unit_credits
            debit_gold += missing * unit_gold
    outcome['cost'] = [debit_credits, debit_gold]
    if not debit_credits and not debit_gold and all(want[cd] == left.get(cd, 0) for cd in allowed):
        return 'full', None
    gold = progress.get('gold', 0)
    if progress['credits'] < debit_credits:
        return 'insufficient_credits', None
    if debit_gold and (type(gold) is not int or gold < debit_gold):
        return 'insufficient_gold', None
    progress['credits'] -= debit_credits
    if debit_gold:
        progress['gold'] = gold - debit_gold
    return 'refilled', [v for cd in allowed for v in (cd, want[cd])]


class _Context:
    """Mutable account state of one transaction; written back once if `changed`."""

    def __init__(self, store, account_id, row):
        self.db = store.db
        self.account_id = account_id
        self.inventory = json.loads(row['inventory'])
        self.progress = json.loads(row['progress'])
        self.changed = False

    def project(self, descriptor, shells):
        self.inventory['descriptor'] = descriptor
        self.inventory['shells'] = list(shells)

    def charge(self, cost):
        if self.progress['credits'] < cost:
            raise ValueError('Not enough credits')
        if cost:
            self.progress['credits'] -= cost
            self.changed = True


class GarageStore:
    def __init__(self, account_store):
        self.db = account_store.db
        if self.db.in_transaction:
            raise RuntimeError('GarageStore cannot be created inside an active transaction')
        for statement in (
            '''CREATE TABLE IF NOT EXISTS garage_vehicles (
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                inventory_id INTEGER NOT NULL, type_cd INTEGER NOT NULL,
                descriptor TEXT NOT NULL, shells TEXT NOT NULL,
                configuration TEXT NOT NULL, xp INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(account_id,inventory_id), UNIQUE(account_id,type_cd))''',
            '''CREATE TABLE IF NOT EXISTS garage_profiles (
                account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
                selected_id INTEGER NOT NULL, free_xp INTEGER NOT NULL DEFAULT 0)''',
            '''CREATE TABLE IF NOT EXISTS garage_research (
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                item_cd INTEGER NOT NULL, PRIMARY KEY(account_id,item_cd))''',
            '''CREATE TABLE IF NOT EXISTS garage_operations (
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                operation_id TEXT NOT NULL, request_json TEXT NOT NULL,
                result_json TEXT NOT NULL, PRIMARY KEY(account_id,operation_id))''',
            '''CREATE TABLE IF NOT EXISTS garage_counters (
                account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
                next_inventory_id INTEGER NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS garage_type_xp (
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                type_cd INTEGER NOT NULL, xp INTEGER NOT NULL,
                PRIMARY KEY(account_id,type_cd))''',
            '''CREATE TABLE IF NOT EXISTS garage_catalog_grants (
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                type_cd INTEGER NOT NULL, PRIMARY KEY(account_id,type_cd))''',
            '''CREATE TABLE IF NOT EXISTS garage_loadouts (
                account_id INTEGER NOT NULL, inventory_id INTEGER NOT NULL,
                devices TEXT NOT NULL, unlocked INTEGER NOT NULL CHECK(unlocked BETWEEN 0 AND 511),
                provisions TEXT NOT NULL, consumables TEXT NOT NULL,
                PRIMARY KEY(account_id,inventory_id),
                FOREIGN KEY(account_id,inventory_id) REFERENCES garage_vehicles(account_id,inventory_id)
                    ON DELETE CASCADE)''',
            # Native ammunition preferences. A row also marks the inventory as using the paid
            # exchange: `free` tracks trusted grants for configuration-conservation
            # checks only. All owned shells retain their native cash resale value.
            '''CREATE TABLE IF NOT EXISTS garage_ammo_prefs (
                account_id INTEGER NOT NULL, inventory_id INTEGER NOT NULL,
                settings INTEGER NOT NULL CHECK(settings BETWEEN 0 AND 2147483647),
                layouts TEXT NOT NULL, free TEXT NOT NULL,
                PRIMARY KEY(account_id,inventory_id),
                FOREIGN KEY(account_id,inventory_id) REFERENCES garage_vehicles(account_id,inventory_id)
                    ON DELETE CASCADE)'''):
            self.db.execute(statement)
        # Legacy garages have vehicles but no counter; seed it before any sale can delete the highest ID.
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO garage_counters '
                            'SELECT account_id,MAX(inventory_id)+1 FROM garage_vehicles GROUP BY account_id')
            # Enable it once in old preference rows. Subsequent restarts
            # leave explicit per-tank changes to the flag alone.
            if self.db.execute('PRAGMA user_version').fetchone()[0] == 0:
                self.db.execute('UPDATE garage_ammo_prefs SET settings=settings | ?',
                                (AMMO_AUTO_RESUPPLY,))
                self.db.execute('PRAGMA user_version=1')

    # -- reads ---------------------------------------------------------
    def snapshot(self, account_id):
        if self.db.in_transaction:
            return self._snapshot(account_id)
        self.db.execute('BEGIN')
        try:
            return self._snapshot(account_id)
        finally:
            self.db.rollback()

    def loadout(self, account_id, inventory_id):
        _int(inventory_id, 'inventory_id', low=1)
        if not self.db.in_transaction:
            self.db.execute('BEGIN')
            try:
                return self.loadout(account_id, inventory_id)
            finally:
                self.db.rollback()
        if not self._initialized(account_id):
            raise ValueError('Garage is not initialized')
        if self.db.execute('SELECT 1 FROM garage_vehicles WHERE account_id=? AND inventory_id=?',
                           (account_id, inventory_id)).fetchone() is None:
            raise ValueError('Vehicle is not in the garage')
        return self._loadout(account_id, inventory_id)

    def _loadout(self, account_id, inventory_id):
        row = self.db.execute('SELECT * FROM garage_loadouts WHERE account_id=? AND inventory_id=?',
                              (account_id, inventory_id)).fetchone()
        return default_loadout() if row is None else _loadout_row(row)

    def _save_loadout(self, account_id, inventory_id, loadout):
        self.db.execute('INSERT OR REPLACE INTO garage_loadouts VALUES (?,?,?,?,?,?)',
                        (account_id, inventory_id, _dump(loadout['devices']), loadout['unlocked'],
                         _dump(loadout['provisions']), _dump(loadout['consumables'])))

    def _snapshot(self, account_id):
        profile = self.db.execute('SELECT selected_id,free_xp FROM garage_profiles WHERE account_id=?',
                                  (account_id,)).fetchone()
        if profile is None:
            raise ValueError('Garage is not initialized')
        research = [r[0] for r in self.db.execute(
            'SELECT item_cd FROM garage_research WHERE account_id=? ORDER BY item_cd', (account_id,))]
        loadouts = {r['inventory_id']: _loadout_row(r) for r in self.db.execute(
            'SELECT * FROM garage_loadouts WHERE account_id=?', (account_id,))}
        vehicles = [dict(id=r['inventory_id'], type_cd=r['type_cd'], descriptor=r['descriptor'],
                         shells=json.loads(r['shells']), configuration=json.loads(r['configuration']),
                         xp=r['xp'])
                    for r in self.db.execute(
                        'SELECT * FROM garage_vehicles WHERE account_id=? ORDER BY inventory_id',
                        (account_id,))]
        prefs = {r['inventory_id']: r for r in self.db.execute(
            'SELECT * FROM garage_ammo_prefs WHERE account_id=?', (account_id,))}
        for v in vehicles:
            if v['id'] in loadouts:
                v['loadout'] = loadouts[v['id']]
            row = prefs.get(v['id'])
            v['ammo_settings'] = AMMO_AUTO_RESUPPLY if row is None else row['settings']
            if row is not None:
                v.update(ammo_layouts=json.loads(row['layouts']),
                         ammo_free=json.loads(row['free']))
        type_xp = {v['type_cd']: v['xp'] for v in vehicles}
        for r in self.db.execute('SELECT type_cd,xp FROM garage_type_xp WHERE account_id=?', (account_id,)):
            if r['type_cd'] in type_xp:
                raise ValueError('Vehicle XP exists in both owned and retired storage')
            type_xp[r['type_cd']] = r['xp']
        return dict(selected_id=profile['selected_id'], free_xp=profile['free_xp'],
                    researched=research, vehicles=vehicles,
                    type_xp=[dict(type_cd=k, xp=type_xp[k]) for k in sorted(type_xp)])

    # -- transaction machinery -----------------------------------------
    def _run(self, account_id, operation_id, request, action):
        if operation_id is not None and (not isinstance(operation_id, str)
                                         or not 1 <= len(operation_id) <= 128):
            raise ValueError('operation_id must be a non-empty string of at most 128 characters')
        if self.db.in_transaction:
            raise RuntimeError('Garage mutation refused: a transaction is already active')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT inventory,progress FROM accounts WHERE id=?',
                                  (account_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown local account')
            request_json = _dump(request)
            if operation_id is not None:
                receipt = self.db.execute(
                    'SELECT request_json,result_json FROM garage_operations '
                    'WHERE account_id=? AND operation_id=?', (account_id, operation_id)).fetchone()
                if receipt is not None:
                    if receipt['request_json'] != request_json:
                        raise ValueError('operation_id was already used for a different request')
                    self.db.commit()
                    return json.loads(receipt['result_json'])
            ctx = _Context(self, account_id, row)
            action(ctx)
            if ctx.changed:
                ctx.progress['revision'] += 1
                self.db.execute('UPDATE accounts SET inventory=?,progress=? WHERE id=?',
                                (json.dumps(ctx.inventory), json.dumps(ctx.progress), account_id))
            result = self.snapshot(account_id) if self._initialized(account_id) else None
            if operation_id is not None:
                self.db.execute('INSERT INTO garage_operations VALUES (?,?,?,?)',
                                (account_id, operation_id, request_json, _dump(result)))
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def _initialized(self, account_id):
        return self.db.execute('SELECT 1 FROM garage_profiles WHERE account_id=?',
                               (account_id,)).fetchone() is not None

    def _require(self, ctx):
        if not self._initialized(ctx.account_id):
            raise ValueError('Garage is not initialized')

    def _vehicle(self, ctx, inventory_id):
        _int(inventory_id, 'inventory_id', low=1)
        row = self.db.execute('SELECT * FROM garage_vehicles WHERE account_id=? AND inventory_id=?',
                              (ctx.account_id, inventory_id)).fetchone()
        if row is None:
            raise ValueError('Vehicle is not in the garage')
        return row

    def _prefs(self, account_id, inventory_id):
        return _load_prefs(self.db, account_id, inventory_id)

    def _enter_prefs(self, account_id, inventory_id, shells):
        """(prefs, created); legacy loaded assets count as grants for conservation only."""
        prefs = self._prefs(account_id, inventory_id)
        if prefs is not None:
            return prefs, False
        return _default_prefs(_counts(shells)), True

    def _save_prefs(self, account_id, inventory_id, prefs):
        _store_prefs(self.db, account_id, inventory_id, prefs)

    def _free_change(self, account_id, inventory_id, old_shells, new_shells):
        """Unpaid (legacy/admin) shell change; exchange inventories may only lose free shells."""
        prefs = self._prefs(account_id, inventory_id)
        if prefs is not None:
            prefs['free'] = _conserve(prefs['free'], _counts(old_shells), _counts(new_shells))
            self._save_prefs(account_id, inventory_id, prefs)

    def _add_research(self, ctx, items):
        for item in items:
            if self.db.execute('INSERT OR IGNORE INTO garage_research VALUES (?,?)',
                               (ctx.account_id, item)).rowcount:
                ctx.changed = True

    def _allocate_inventory_id(self, account_id):
        # Runs inside the caller's BEGIN IMMEDIATE, so the counter is read and advanced atomically.
        # Legacy accounts have no counter yet: start above their current highest vehicle.
        row = self.db.execute('SELECT next_inventory_id FROM garage_counters WHERE account_id=?',
                              (account_id,)).fetchone()
        next_id = row[0] if row is not None else self.db.execute(
            'SELECT COALESCE(MAX(inventory_id),0)+1 FROM garage_vehicles WHERE account_id=?',
            (account_id,)).fetchone()[0]
        if next_id > MAX_INT:
            raise ValueError('Inventory ID space exhausted')
        self.db.execute('INSERT OR REPLACE INTO garage_counters VALUES (?,?)', (account_id, next_id + 1))
        return next_id

    def _insert_vehicle(self, ctx, type_cd, descriptor, shells, configuration):
        next_id = self._allocate_inventory_id(ctx.account_id)
        retired = self.db.execute('SELECT xp FROM garage_type_xp WHERE account_id=? AND type_cd=?',
                                  (ctx.account_id, type_cd)).fetchone()
        self.db.execute('INSERT INTO garage_vehicles VALUES (?,?,?,?,?,?,?)',
                        (ctx.account_id, next_id, type_cd, descriptor, _dump(shells), _dump(configuration),
                         retired[0] if retired else 0))
        if retired:
            self.db.execute('DELETE FROM garage_type_xp WHERE account_id=? AND type_cd=?',
                            (ctx.account_id, type_cd))
        ctx.changed = True
        return next_id

    @staticmethod
    def _checked(type_cd, inventory):
        _int(type_cd, 'type_cd')
        descriptor, shells, derived = _inventory(inventory)
        if derived != type_cd:
            raise ValueError('Inventory descriptor does not match type_cd')
        return descriptor, shells

    # -- mutations -----------------------------------------------------
    def provision_catalog(self, account_id, entries):
        """Grant each catalog type once; later sales must stay sold.

        Existing vehicles retain IDs/configuration/XP. Missing types restore
        their retired XP through the normal insertion path. The ledger and
        inventory commit together, without per-vehicle transactions/receipts.
        """
        entries = tuple(entries)
        def action(ctx):
            self._require(ctx)
            applied = {r[0] for r in self.db.execute(
                'SELECT type_cd FROM garage_catalog_grants WHERE account_id=?', (account_id,))}
            for entry in entries:
                type_cd = _int(entry['type_cd'], 'type_cd')
                if type_cd in applied:
                    continue
                descriptor, shells = self._checked(type_cd, entry['inventory'])
                configuration = _config(entry.get('configuration'))
                researched = _ints(entry.get('researched', ()), 'researched')
                self._grant(ctx, type_cd, descriptor, shells, configuration, researched)
                self.db.execute('INSERT INTO garage_catalog_grants VALUES (?,?)', (account_id, type_cd))
                applied.add(type_cd)
        return self._run(account_id, None, dict(op='provision_catalog'), action)

    def ensure(self, account_id, *, researched=()):
        items = _ints(researched, 'researched')

        def action(ctx):
            if self._initialized(account_id) or self.db.execute(
                    'SELECT 1 FROM garage_vehicles WHERE account_id=?', (account_id,)).fetchone():
                return
            descriptor, shells, type_cd = _inventory(ctx.inventory)
            self.db.execute('INSERT INTO garage_profiles VALUES (?,1,?)',
                            (account_id, STARTING_FREE_XP))
            self._insert_vehicle(ctx, type_cd, descriptor, shells, {})
            self._add_research(ctx, sorted({type_cd, *items}))
        return self._run(account_id, None, dict(op='ensure'), action)

    def grant_vehicle(self, account_id, type_cd, inventory, *, configuration=None,
                      researched=(), operation_id=None):
        descriptor, shells = self._checked(type_cd, inventory)
        configuration = _config(configuration)
        items = _ints(researched, 'researched')
        request = dict(op='grant_vehicle', type_cd=type_cd, descriptor=descriptor, shells=shells,
                       configuration=configuration, researched=items)

        def action(ctx):
            self._require(ctx)
            self._grant(ctx, type_cd, descriptor, shells, configuration, items)
        return self._run(account_id, operation_id, request, action)

    def _grant(self, ctx, type_cd, descriptor, shells, configuration, items):
        owned = self.db.execute('SELECT 1 FROM garage_vehicles WHERE account_id=? AND type_cd=?',
                                (ctx.account_id, type_cd)).fetchone()
        if owned:
            self._add_research(ctx, items)
        else:
            self._insert_vehicle(ctx, type_cd, descriptor, shells, configuration)
            self._add_research(ctx, sorted({type_cd, *items}))

    def select_vehicle(self, account_id, inventory_id, *, operation_id=None):
        def action(ctx):
            self._require(ctx)
            vehicle = self._vehicle(ctx, inventory_id)
            current = self.db.execute('SELECT selected_id FROM garage_profiles WHERE account_id=?',
                                      (account_id,)).fetchone()[0]
            if current == inventory_id:
                return
            self.db.execute('UPDATE garage_profiles SET selected_id=? WHERE account_id=?',
                            (inventory_id, account_id))
            ctx.project(vehicle['descriptor'], json.loads(vehicle['shells']))
            ctx.changed = True
        return self._run(account_id, operation_id, dict(op='select_vehicle', inventory_id=inventory_id),
                         action)

    def set_ammo(self, account_id, inventory_id, shell_cd, count, *, capacity, operation_id=None):
        """Set the loaded quantity of the vehicle's current shell (sandbox loadout, no money)."""
        _int(inventory_id, 'inventory_id', low=1)
        _int(shell_cd, 'shell_cd')
        _int(capacity, 'capacity')
        _int(count, 'count')
        if count > capacity:
            raise ValueError('count exceeds the ammunition capacity')
        request = dict(op='set_ammo', inventory_id=inventory_id, shell_cd=shell_cd, count=count,
                       capacity=capacity)

        def action(ctx):
            self._require(ctx)
            vehicle = self._vehicle(ctx, inventory_id)
            current = json.loads(vehicle['shells'])
            if not isinstance(current, list) or len(current) != 2:
                raise ValueError('Vehicle does not have a single shell slot')
            if current[0] != shell_cd:
                raise ValueError('Shell is not the vehicle\'s current shell')
            shells = [shell_cd, count]
            if shells == current:
                return
            self._free_change(ctx.account_id, inventory_id, current, shells)
            self.db.execute('UPDATE garage_vehicles SET shells=? WHERE account_id=? AND inventory_id=?',
                            (_dump(shells), ctx.account_id, inventory_id))
            ctx.changed = True
            selected = self.db.execute('SELECT selected_id FROM garage_profiles WHERE account_id=?',
                                       (ctx.account_id,)).fetchone()[0]
            if selected == inventory_id:
                ctx.project(vehicle['descriptor'], shells)
        return self._run(account_id, operation_id, request, action)

    def set_ammo_loadout(self, account_id, inventory_id, pairs, *, allowed_shells, capacity,
                         operation_id=None):
        """Store a multi-shell loadout as flat [cd, count, ...] in `allowed_shells` order.

        `pairs` is a flat sequence of shell CD/count; shells it omits are stored with zero.
        """
        _int(inventory_id, 'inventory_id', low=1)
        _int(capacity, 'capacity')
        allowed = self._shell_ids(allowed_shells, 'allowed_shells', 32)
        flat = self._shell_ids(pairs, 'pairs', 64, distinct=False)
        if len(flat) % 2:
            raise ValueError('pairs must hold shell/count pairs')
        counts = {}
        for cd, count in zip(flat[::2], flat[1::2]):
            if cd in counts:
                raise ValueError('Duplicate shell in the loadout')
            if cd not in allowed:
                raise ValueError('Shell is not compatible with the vehicle')
            counts[cd] = count
        if sum(counts.values()) > capacity:
            raise ValueError('Loadout exceeds the ammunition capacity')
        shells = [v for cd in allowed for v in (cd, counts.get(cd, 0))]
        request = dict(op='set_ammo_loadout', inventory_id=inventory_id, shells=shells,
                       capacity=capacity)

        def action(ctx):
            self._require(ctx)
            vehicle = self._vehicle(ctx, inventory_id)
            current = json.loads(vehicle['shells'])
            if not isinstance(current, list) or not current or len(current) % 2:
                raise ValueError('Vehicle does not have a shell loadout')
            if shells == current:
                return
            self._free_change(ctx.account_id, inventory_id, current, shells)
            self.db.execute('UPDATE garage_vehicles SET shells=? WHERE account_id=? AND inventory_id=?',
                            (_dump(shells), ctx.account_id, inventory_id))
            ctx.changed = True
            selected = self.db.execute('SELECT selected_id FROM garage_profiles WHERE account_id=?',
                                       (ctx.account_id,)).fetchone()[0]
            if selected == inventory_id:
                ctx.project(vehicle['descriptor'], shells)
        return self._run(account_id, operation_id, request, action)

    def exchange_ammo_loadout(self, account_id, inventory_id, signed_pairs, *, allowed_shells, capacity,
                              prices, layout_key, expected_descriptor, operation_id=None):
        """Buy/unload shells to reach signed target counts and store them as the desired layout.

        `signed_pairs` is native cmd109 [signedCD, count, ...]: a negative CD selects the
        credit price. `prices` maps every allowed CD to server-resolved unit prices
        ((credits, gold) for a positive CD, (credits, gold) for a negative CD, refund credits);
        they are not part of the receipt identity. Unloading refunds every owned shell
        at that server price, including legacy/admin grants; `ammo_free` is conservation
        bookkeeping only. Deltas use rows read in the transaction.
        """
        _int(inventory_id, 'inventory_id', low=1)
        _int(capacity, 'capacity')
        allowed = self._shell_ids(allowed_shells, 'allowed_shells', 32)
        if not isinstance(expected_descriptor, str):
            raise ValueError('expected_descriptor must be a hex string')
        turret_cd, gun_cd = _layout_key(layout_key)
        costs = _unit_prices(allowed, prices)
        targets = {}
        for signed, count in self._signed_pairs(signed_pairs):
            if abs(signed) in targets:
                raise ValueError('Duplicate shell in the loadout')
            if abs(signed) not in allowed:
                raise ValueError('Shell is not compatible with the vehicle')
            targets[abs(signed)] = (signed, count)
        if sum(count for _, count in targets.values()) > capacity:
            raise ValueError('Loadout exceeds the ammunition capacity')
        layout = [v for cd in allowed for v in targets.get(cd, (cd, 0))]
        shells = [v for cd in allowed for v in (cd, targets.get(cd, (cd, 0))[1])]
        request = dict(op='exchange_ammo_loadout', inventory_id=inventory_id, layout=layout)

        def action(ctx):
            self._require(ctx)
            vehicle = self._vehicle(ctx, inventory_id)
            if vehicle['descriptor'] != expected_descriptor:
                raise ValueError('Vehicle changed since the exchange was prepared')
            current = json.loads(vehicle['shells'])
            loaded = _counts(current)
            if any(n and cd not in allowed for cd, n in loaded.items()):
                raise ValueError('Stored loadout does not match the installed gun')
            prefs, created = self._enter_prefs(ctx.account_id, inventory_id, current)
            free = dict(prefs['free'])
            debit_credits = debit_gold = refund = 0
            for cd, (signed, want) in ((cd, targets.get(cd, (cd, 0))) for cd in allowed):
                have = loaded.get(cd, 0)
                positive, negative, unit_refund = costs[cd]
                if want > have:
                    unit_credits, unit_gold = negative if signed < 0 else positive
                    debit_credits += (want - have) * unit_credits
                    debit_gold += (want - have) * unit_gold
                elif want < have:
                    refund += (have - want) * unit_refund
                    # Keep the existing paid-first conservation rule; provenance
                    # never reduces the native monetary value of owned shells.
                    free[cd] = min(free.get(cd, 0), want)
            credits = ctx.progress['credits'] + refund - debit_credits
            gold = ctx.progress.get('gold', 0)
            if credits < 0:
                raise ValueError('Not enough credits')
            if debit_gold and (type(gold) is not int or gold < debit_gold):
                raise ValueError('Not enough gold')
            if credits > MAX_INT:
                raise ValueError('Credits total out of range')
            if credits != ctx.progress['credits']:
                ctx.progress['credits'] = credits
                ctx.changed = True
            if debit_gold:
                ctx.progress['gold'] = gold - debit_gold
                ctx.changed = True
            entry = dict(turret_cd=turret_cd, gun_cd=gun_cd, shells=layout)
            layouts = [e for e in prefs['layouts'] if (e['turret_cd'], e['gun_cd']) != (turret_cd, gun_cd)]
            updated = dict(prefs, layouts=layouts + [entry], free={cd: n for cd, n in free.items() if n})
            if created or entry not in prefs['layouts'] or updated['free'] != prefs['free']:
                self._save_prefs(ctx.account_id, inventory_id, updated)
                ctx.changed = True
            if shells != current:
                self.db.execute('UPDATE garage_vehicles SET shells=? WHERE account_id=? AND inventory_id=?',
                                (_dump(shells), ctx.account_id, inventory_id))
                ctx.changed = True
                selected = self.db.execute('SELECT selected_id FROM garage_profiles WHERE account_id=?',
                                           (ctx.account_id,)).fetchone()[0]
                if selected == inventory_id:
                    ctx.project(vehicle['descriptor'], shells)
        return self._run(account_id, operation_id, request, action)

    def set_ammo_setting(self, account_id, inventory_id, setting, enabled, *, operation_id=None,
                         layout_key=None, allowed_shells=(), expected_descriptor=None):
        """Set or clear one native settings flag (e.g. AMMO_AUTO_RESUPPLY), keeping the others."""
        _int(inventory_id, 'inventory_id', low=1)
        if type(setting) is not int or not 0 < setting <= 1 << 30 or setting & (setting - 1):
            raise ValueError('setting must be a single native flag bit')
        if type(enabled) is not bool:
            raise ValueError('enabled must be a bool')
        key = None if layout_key is None else _layout_key(layout_key)
        allowed = self._shell_ids(allowed_shells, 'allowed_shells', 32) if key is not None else ()
        if key is not None and not isinstance(expected_descriptor, str):
            raise ValueError('A layout seed requires the expected vehicle descriptor')
        request = dict(op='set_ammo_setting', inventory_id=inventory_id, setting=setting, enabled=enabled)

        def action(ctx):
            self._require(ctx)
            vehicle = self._vehicle(ctx, inventory_id)
            prefs, created = self._enter_prefs(ctx.account_id, inventory_id, json.loads(vehicle['shells']))
            seeded = False
            if enabled and setting == AMMO_AUTO_RESUPPLY and key is not None:
                if vehicle['descriptor'] != expected_descriptor:
                    raise ValueError('Vehicle changed since the setting was prepared')
                seeded = _seed_ammo_layout(prefs, key, allowed, _counts(json.loads(vehicle['shells'])))
            flags = prefs['settings'] | setting if enabled else prefs['settings'] & ~setting
            if flags == prefs['settings'] and not created and not seeded:
                return
            self._save_prefs(ctx.account_id, inventory_id, dict(prefs, settings=flags))
            ctx.changed = True
        return self._run(account_id, operation_id, request, action)

    @staticmethod
    def _signed_pairs(values):
        if isinstance(values, (str, bytes, dict)) or not hasattr(values, '__iter__'):
            raise ValueError('signed_pairs must be a sequence of integers')
        values = list(values)
        if not values or len(values) > 64 or len(values) % 2:
            raise ValueError('signed_pairs must hold 1..32 shell/count pairs')
        for signed in values[::2]:
            if type(signed) is not int or not signed or not -MAX_INT <= signed <= MAX_INT:
                raise ValueError('signed shell CD must be a non-zero 32-bit integer')
        return [(signed, _int(count, 'count')) for signed, count in zip(values[::2], values[1::2])]

    @staticmethod
    def _shell_ids(values, name, limit, distinct=True):
        if isinstance(values, (str, bytes, dict)) or not hasattr(values, '__iter__'):
            raise ValueError(f'{name} must be a sequence of integers')
        values = [_int(v, name) for v in values]
        if not values or len(values) > limit or (distinct and len(set(values)) != len(values)):
            raise ValueError(f'{name} must be a non-empty list of at most {limit} distinct values')
        return values

    def grant_xp(self, account_id, amount, *, inventory_id=None, operation_id=None):
        _int(amount, 'amount')
        if inventory_id is not None:
            _int(inventory_id, 'inventory_id', low=1)

        def action(ctx):
            self._require(ctx)
            if inventory_id is None:
                table, column, where, args = 'garage_profiles', 'free_xp', 'account_id=?', (account_id,)
            else:
                self._vehicle(ctx, inventory_id)
                table, column, where, args = ('garage_vehicles', 'xp',
                                              'account_id=? AND inventory_id=?', (account_id, inventory_id))
            current = self.db.execute(f'SELECT {column} FROM {table} WHERE {where}', args).fetchone()[0]
            if current + amount > MAX_INT:
                raise ValueError('XP total out of range')
            if amount:
                self.db.execute(f'UPDATE {table} SET {column}=? WHERE {where}', (current + amount, *args))
                ctx.changed = True
        return self._run(account_id, operation_id,
                         dict(op='grant_xp', amount=amount, inventory_id=inventory_id), action)

    def research_item(self, account_id, item_cd, xp_cost, *, inventory_id, prerequisites=(),
                      allow_free_xp=True, operation_id=None):
        spec = self._research_spec(item_cd, xp_cost, prerequisites, allow_free_xp)
        request = dict(op='research_item', item_cd=item_cd, xp_cost=xp_cost, inventory_id=inventory_id,
                       prerequisites=spec[2], allow_free_xp=allow_free_xp)

        def action(ctx):
            self._require(ctx)
            self._research(ctx, inventory_id, *spec)
        return self._run(account_id, operation_id, request, action)

    @staticmethod
    def _research_spec(item_cd, xp_cost, prerequisites, allow_free_xp):
        _int(item_cd, 'item_cd')
        _int(xp_cost, 'xp_cost')
        parents = _ints(prerequisites, 'prerequisites')
        if type(allow_free_xp) is not bool:
            raise ValueError('allow_free_xp must be a bool')
        return item_cd, xp_cost, parents, allow_free_xp

    def _research(self, ctx, inventory_id, item_cd, xp_cost, parents, allow_free_xp):
        account_id = ctx.account_id
        vehicle = self._vehicle(ctx, inventory_id)
        have = {r[0] for r in self.db.execute(
            'SELECT item_cd FROM garage_research WHERE account_id=?', (account_id,))}
        if item_cd in have:
            return
        if not set(parents) <= have:
            raise ValueError('Prerequisites are not researched')
        from_vehicle = min(vehicle['xp'], xp_cost)
        from_free = xp_cost - from_vehicle
        free_xp = self.db.execute('SELECT free_xp FROM garage_profiles WHERE account_id=?',
                                  (account_id,)).fetchone()[0]
        if from_free and (not allow_free_xp or free_xp < from_free):
            raise ValueError('Not enough XP')
        self.db.execute('UPDATE garage_vehicles SET xp=xp-? WHERE account_id=? AND inventory_id=?',
                        (from_vehicle, account_id, inventory_id))
        self.db.execute('UPDATE garage_profiles SET free_xp=free_xp-? WHERE account_id=?',
                        (from_free, account_id))
        self._add_research(ctx, [item_cd])

    def buy_vehicle(self, account_id, type_cd, inventory, credits_cost, *, configuration=None,
                    researched=(), operation_id=None):
        descriptor, shells = self._checked(type_cd, inventory)
        _int(credits_cost, 'credits_cost')
        configuration = _config(configuration)
        items = _ints(researched, 'researched')
        request = dict(op='buy_vehicle', type_cd=type_cd, descriptor=descriptor, shells=shells,
                       credits_cost=credits_cost, configuration=configuration, researched=items)

        def action(ctx):
            self._require(ctx)
            if self.db.execute('SELECT 1 FROM garage_vehicles WHERE account_id=? AND type_cd=?',
                               (account_id, type_cd)).fetchone():
                self._add_research(ctx, items)
                return
            if not self.db.execute('SELECT 1 FROM garage_research WHERE account_id=? AND item_cd=?',
                                   (account_id, type_cd)).fetchone():
                raise ValueError('Vehicle is not researched')
            ctx.charge(credits_cost)
            self._grant(ctx, type_cd, descriptor, shells, configuration, items)
        return self._run(account_id, operation_id, request, action)

    def sell_vehicle(self, account_id, inventory_id, credits_return, *, expected_descriptor=None,
                     operation_id=None):
        _int(inventory_id, 'inventory_id', low=1)
        _int(credits_return, 'credits_return')
        if expected_descriptor is not None and not isinstance(expected_descriptor, str):
            raise ValueError('expected_descriptor must be a hex string')

        def action(ctx):
            self._require(ctx)
            vehicle = self._vehicle(ctx, inventory_id)
            if self.db.execute('SELECT COUNT(*) FROM garage_vehicles WHERE account_id=?',
                               (account_id,)).fetchone()[0] < 2:
                raise ValueError('The last vehicle cannot be sold')
            if expected_descriptor is not None and vehicle['descriptor'] != expected_descriptor:
                raise ValueError('Vehicle changed since the sale was prepared')
            if ctx.progress['credits'] + credits_return > MAX_INT:
                raise ValueError('Credits total out of range')
            # Local restriction, not native sale policy: paid shells are never sold implicitly.
            prefs = self._prefs(account_id, inventory_id)
            if prefs is not None:
                _conserve(prefs['free'], _counts(json.loads(vehicle['shells'])), {})
            self.db.execute('INSERT INTO garage_type_xp VALUES (?,?,?)',
                            (account_id, vehicle['type_cd'], vehicle['xp']))
            for table in ('garage_loadouts', 'garage_ammo_prefs'):
                self.db.execute(f'DELETE FROM {table} WHERE account_id=? AND inventory_id=?',
                                (account_id, inventory_id))
            self.db.execute('DELETE FROM garage_vehicles WHERE account_id=? AND inventory_id=?',
                            (account_id, inventory_id))
            ctx.progress['credits'] += credits_return
            ctx.changed = True
            selected = self.db.execute('SELECT selected_id FROM garage_profiles WHERE account_id=?',
                                       (account_id,)).fetchone()[0]
            if selected == inventory_id:
                fallback = self.db.execute('SELECT * FROM garage_vehicles WHERE account_id=? '
                                           'ORDER BY inventory_id LIMIT 1', (account_id,)).fetchone()
                self.db.execute('UPDATE garage_profiles SET selected_id=? WHERE account_id=?',
                                (fallback['inventory_id'], account_id))
                ctx.project(fallback['descriptor'], json.loads(fallback['shells']))
        return self._run(account_id, operation_id, dict(op='sell_vehicle', inventory_id=inventory_id),
                         action)

    def set_loadout(self, account_id, inventory_id, category, values, *, allowed_tags,
                    credits_cost=0, credits_refund=0, expected_values=None, operation_id=None):
        """Replace one loadout category with catalog tags the caller already verified."""
        _int(inventory_id, 'inventory_id', low=1)
        _int(credits_cost, 'credits_cost')
        _int(credits_refund, 'credits_refund')
        tags = _tags(category, values, allowed_tags)
        expected = list(expected_values) if expected_values is not None else None
        request = dict(op='set_loadout', inventory_id=inventory_id, category=category, values=tags,
                       credits_cost=credits_cost)
        if credits_refund:
            request['credits_refund'] = credits_refund

        def action(ctx):
            self._require(ctx)
            self._vehicle(ctx, inventory_id)
            current = self._loadout(account_id, inventory_id)
            if category == 'devices' and any(
                    tag is not None and not current['unlocked'] >> slot & 1
                    for slot, tag in enumerate(tags)):
                raise ValueError('Device slot is locked')
            if current[category] == tags:
                return
            if expected is not None and current[category] != expected:
                raise ValueError('Loadout changed since the exchange was prepared')
            credits = ctx.progress['credits'] + credits_refund - credits_cost
            if credits < 0:
                raise ValueError('Not enough credits')
            if credits > MAX_INT:
                raise ValueError('Credits total out of range')
            ctx.progress['credits'] = credits
            current[category] = tags
            self._save_loadout(account_id, inventory_id, current)
            ctx.changed = True
        return self._run(account_id, operation_id, request, action)

    def unlock_device_slot(self, account_id, inventory_id, slot, *, credits_cost=0, operation_id=None,
                           device_tag=None, require_previous_levels=False):
        _int(inventory_id, 'inventory_id', low=1)
        if type(slot) is not int or not 0 <= slot <= 8:
            raise ValueError('slot must be an integer in 0..8')
        _int(credits_cost, 'credits_cost')
        if device_tag is not None:
            _tags('devices', [device_tag] + [None] * 8, [device_tag])
        request = dict(op='unlock_device_slot', inventory_id=inventory_id, slot=slot,
                       credits_cost=credits_cost)
        if device_tag is not None:
            request['device_tag'] = device_tag
        if require_previous_levels:
            request['require_previous_levels'] = True

        def action(ctx):
            self._require(ctx)
            self._vehicle(ctx, inventory_id)
            current = self._loadout(account_id, inventory_id)
            if current['unlocked'] >> slot & 1:
                return
            required = (1 << (slot // 3 * 3)) - 1
            if require_previous_levels and current['unlocked'] & required != required:
                raise ValueError('Unlock all slots of the previous levels first')
            ctx.charge(credits_cost)
            current['unlocked'] |= 1 << slot
            current['devices'][slot] = device_tag
            self._save_loadout(account_id, inventory_id, current)
            ctx.changed = True
        return self._run(account_id, operation_id, request, action)

    def install_configuration(self, account_id, inventory_id, inventory, *, configuration,
                              required_research=(), credits_cost=0, operation_id=None,
                              expected_descriptor=None, operation_request=None):
        spec, request = self._install_spec(
            inventory_id, inventory, configuration, required_research, credits_cost,
            expected_descriptor, operation_request)

        def action(ctx):
            self._require(ctx)
            self._install(ctx, inventory_id, *spec)
        return self._run(account_id, operation_id, request, action)

    def research_and_install(self, account_id, inventory_id, inventory, *, configuration, item_cd,
                             xp_cost, prerequisites=(), allow_free_xp=True, credits_cost=0,
                             operation_id=None, expected_descriptor=None, operation_request=None):
        """Research `item_cd` and install the configuration in one transaction and revision."""
        research = self._research_spec(item_cd, xp_cost, prerequisites, allow_free_xp)
        spec, request = self._install_spec(
            inventory_id, inventory, configuration, (item_cd,), credits_cost, expected_descriptor,
            operation_request, op='research_and_install')
        if operation_request is None:
            request.update(xp_cost=research[1], prerequisites=research[2],
                           allow_free_xp=research[3])

        def action(ctx):
            self._require(ctx)
            self._research(ctx, inventory_id, *research)
            self._install(ctx, inventory_id, *spec)
        return self._run(account_id, operation_id, request, action)

    @staticmethod
    def _install_spec(inventory_id, inventory, configuration, required_research, credits_cost,
                      expected_descriptor, operation_request, op='install_configuration'):
        descriptor, shells, derived = _inventory(inventory)
        configuration = _config(configuration)
        required = _ints(required_research, 'required_research')
        _int(credits_cost, 'credits_cost')
        if expected_descriptor is not None and not isinstance(expected_descriptor, str):
            raise ValueError('expected_descriptor must be a hex string')
        if operation_request is not None and not isinstance(operation_request, dict):
            raise ValueError('operation_request must be an object')
        # An explicit operation_request is a stable logical identity for receipts: derived
        # fields (descriptor, cost, ...) may change between a call and its replay.
        request = dict(operation_request) if operation_request is not None else dict(
            op=op, inventory_id=inventory_id, descriptor=descriptor,
            shells=shells, configuration=configuration, required_research=required,
            credits_cost=credits_cost)
        return (descriptor, shells, derived, configuration, required, credits_cost,
                expected_descriptor), request

    def _install(self, ctx, inventory_id, descriptor, shells, derived, configuration, required,
                 credits_cost, expected_descriptor):
        account_id = ctx.account_id
        vehicle = self._vehicle(ctx, inventory_id)
        if vehicle['type_cd'] != derived:
            raise ValueError('Inventory descriptor does not match the vehicle')
        if expected_descriptor is not None and vehicle['descriptor'] != expected_descriptor:
            raise ValueError('Vehicle changed since the install was prepared')
        have = {r[0] for r in self.db.execute(
            'SELECT item_cd FROM garage_research WHERE account_id=?', (account_id,))}
        if not set(required) <= have:
            raise ValueError('Required research is missing')
        old_shells = json.loads(vehicle['shells'])
        prefs = self._prefs(account_id, inventory_id)
        if (isinstance(old_shells, list) and old_shells and not len(old_shells) % 2
                and old_shells[0] in shells[::2]
                and vehicle['descriptor'][24:28] == descriptor[24:28]):
            # Same gun: preserve amounts, including legacy one-pair inventories.
            room = sum(shells[1::2])
            counts = dict(zip(old_shells[::2],old_shells[1::2]))
            ids = old_shells[::2] if len(shells)==2 and len(old_shells)>2 else shells[::2]
            shells = [v for cd in ids for v in (cd,counts.get(cd,0))]
            for i in range(1, len(shells), 2):
                shells[i] = min(shells[i], room)
                room -= shells[i]
        elif prefs is not None:
            # Exchange inventories get no free default refill after a gun change.
            shells = [v for cd in shells[::2] for v in (cd, 0)]
        if (vehicle['descriptor'], old_shells,
                json.loads(vehicle['configuration'])) == (descriptor, shells, configuration):
            return
        if prefs is not None:
            # Gun changes and capacity cuts may drop only free shells; paid ones need unloading.
            prefs['free'] = _conserve(prefs['free'], _counts(old_shells), _counts(shells))
            self._save_prefs(account_id, inventory_id, prefs)
        ctx.charge(credits_cost)
        self.db.execute('UPDATE garage_vehicles SET descriptor=?,shells=?,configuration=? '
                        'WHERE account_id=? AND inventory_id=?',
                        (descriptor, _dump(shells), _dump(configuration), account_id, inventory_id))
        ctx.changed = True
        selected = self.db.execute('SELECT selected_id FROM garage_profiles WHERE account_id=?',
                                   (account_id,)).fetchone()[0]
        if selected == inventory_id:
            ctx.project(descriptor, shells)
