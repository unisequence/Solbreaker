"""Journal completed owner battles and apply the ordinary account transaction."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from types import SimpleNamespace

from .arena_owner import SettlementData, SettlementRecord
from .battle_rewards import BattleReward
from .connection import atomic_write
from .entities import BattleResult


def journal_path(directory, arena_id):
    return directory / (hashlib.sha256(arena_id.encode()).hexdigest() + '.json')


def journal_result(directory, data):
    directory.mkdir(mode=0o700, exist_ok=True)
    rows = [{
        'result': asdict(record.result), 'report': record.report,
        'descriptor': record.descriptor.hex(), 'loadout_shells': record.loadout_shells,
        'ammo_by_shell': record.ammo_by_shell, 'shell_id': record.shell_id, 'ammo': record.ammo,
    } for record in data.records]
    if tuple(record.result for record in data.records) != data.results:
        raise ValueError('owner results do not match the journal records')
    atomic_write(journal_path(directory, data.arena_id),
                 json.dumps({'version': 1, 'arena_id': data.arena_id, 'records': rows},
                            allow_nan=False, separators=(',', ':')).encode())


def read_journal(path):
    if path.is_symlink() or path.stat().st_size > 8 << 20:
        raise ValueError('invalid owner settlement journal')
    data = json.loads(path.read_bytes())
    if data['version'] != 1 or journal_path(path.parent, data['arena_id']) != path:
        raise ValueError('owner settlement journal identity mismatch')
    records = []
    for row in data['records']:
        result = row['result']
        if result['reward'] is not None:
            result['reward'] = BattleReward(**result['reward'])
        shells = row['loadout_shells']
        records.append(SettlementRecord(BattleResult(**result), row['report'],
            bytes.fromhex(row['descriptor']), None if shells is None else tuple(shells),
            tuple(tuple(pair) for pair in row['ammo_by_shell']), row['shell_id'], row['ammo']))
    return SettlementData(data['arena_id'], tuple(row.result for row in records), tuple(records))


def commit(endpoint, data: SettlementData) -> dict[int, dict]:
    """Commit an entire validated batch; safely retry after a partial commit.

    Each account is committed atomically by AccountStore.record, keyed by
    (arena_id, account_id).  A batch is not a multi-account transaction: if one
    account fails, previously committed accounts remain committed and callers
    must retry the original data, not request a new owner mutation.
    """
    if type(data) is not SettlementData or type(data.arena_id) is not str or not data.arena_id:
        raise ValueError('invalid owner settlement')
    if type(data.results) is not tuple or type(data.records) is not tuple or len(data.records) != len(data.results):
        raise ValueError('incomplete owner settlement')
    results = {}
    for result in data.results:
        if (type(result) is not BattleResult or result.result_id != data.arena_id
                or type(result.account_id) is not int or result.account_id <= 0
                or result.account_id in results):
            raise ValueError('invalid or duplicate owner result')
        results[result.account_id] = result

    # Validate the entire envelope and prepare ammunition terms before writing
    # any accounts. The store still validates and commits each account itself.
    prepared = []
    seen = set()
    report_id = None
    for record in data.records:
        if type(record) is not SettlementRecord or type(record.result) is not BattleResult:
            raise ValueError('invalid owner settlement record')
        result = record.result
        if result.account_id in seen or results.get(result.account_id) != result:
            raise ValueError('owner result and record mismatch')
        seen.add(result.account_id)
        report = deepcopy(record.report)
        if type(report) is not dict or report.get('account_id') != result.account_id:
            raise ValueError('owner report account mismatch')
        unique_id = report.get('unique_id')
        if type(unique_id) is not int or not 0 < unique_id < 2**63:
            raise ValueError('invalid owner report identity')
        if report_id is not None and report_id != unique_id:
            raise ValueError('owner report battle identity mismatch')
        report_id = unique_id
        rewards = {}
        if result.inventory_id is not None:
            if record.loadout_shells is None or getattr(endpoint, 'progression', None) is None:
                raise ValueError('owner settlement needs ammunition snapshot and progression')
            try:
                vehicle = SimpleNamespace(descriptor=record.descriptor,
                    loadout_shells=record.loadout_shells, ammo_by_shell=dict(record.ammo_by_shell),
                    shell_id=record.shell_id, ammo=record.ammo)
                rewards = dict(inventory_id=result.inventory_id, vehicle_xp=report['vehicle_xp'],
                               free_xp=report['free_xp'],
                               ammo=endpoint.progression.battle_ammunition(vehicle))
            except (KeyError, TypeError) as exc:
                raise ValueError('invalid owner ammunition or report') from exc
        prepared.append((result, report, rewards))
    if seen != set(results):
        raise ValueError('owner results are missing records')

    store = endpoint.server.accounts.store
    reports = {}
    for result, report, rewards in prepared:
        saved = store.record(result, report=report, **rewards)
        if saved is None:
            # battle_report is indexed by account/unique_id, not arena_id. A
            # mismatched retry must not accidentally return another arena's
            # report as confirmation of this already-recorded result.
            row = store.db.execute('SELECT arena_id FROM battle_reports WHERE account_id=? AND unique_id=?',
                                   (result.account_id, report['unique_id'])).fetchone()
            if row is None or row[0] != data.arena_id:
                raise ValueError('settled result has no matching arena report')
            report = store.battle_report(result.account_id, report['unique_id'])
            if report is None:
                raise ValueError('settled result has no matching report')
        reports[result.account_id] = report
    return reports
