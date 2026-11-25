"""Index completed, comparable Solbreaker Linux release benchmarks."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

from report import write_html

REPO = 'unisequence/Solbreaker'
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'docs' / 'data'
INDEX = DATA / 'index.json'
TAG = re.compile(r'v(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?')
PROFILE = dict(id='rudniki-players-6x-20s-v1', map='rudniki', scenario='players',
               arenas=[1, 2], tanks=6, seconds=20, warmup=5, seed=42, target_hz=60)
TIMINGS = ('samples', 'p50_ms', 'p95_ms', 'p99_ms', 'max_ms')
NUMERIC_RUN = ('concurrent_arenas', 'requested_tanks', 'seed', 'seconds', 'case_started_epoch',
               'measurement_started_epoch', 'server_setup_seconds', 'battle_start_seconds', 'cpu_cores_avg',
               'process_rss_bytes', 'process_lifetime_peak_rss_bytes', 'arena_worker_cpu_cores_avg',
               'arena_worker_rss_bytes_sum', 'arena_worker_memory_samples', 'loopback_packets', 'loopback_bytes',
               'loopback_packets_sent', 'server_udp_receive_drops', 'peer_udp_receive_drops',
               'reliable_retransmissions', 'shutdown_seconds', 'case_seconds')
NUMERIC_ARENA = ('vehicles', 'bots', 'players', 'alive', 'build_seconds', 'steps_per_second', 'dropped_seconds',
                 'remaining_step_debt_ms', 'controls_sent', 'controls_queued', 'input_overflows',
                 'pending_input_commands', 'shots', 'worker_cpu_cores_avg', 'shutdown_seconds',
                 'nonfinite_samples', 'below_heightmap_samples', 'max_below_heightmap_m',
                 'max_below_heightmap_seconds')
NUMERIC_SERIES = ('steps_per_second', 'input_p99_ms', 'motion_max_ms', 'dropped_seconds_total')
WINDOW_VALUES = ('elapsed_seconds', 'window_seconds', 'sessions', 'server_cpu_cores', 'server_rss_bytes',
                 'worker_cpu_cores', 'worker_rss_bytes_sum', 'tick_p99_ms', 'loop_delay_p99_ms')
VERDICTS = {'ok', 'stalls', 'overloaded', 'fault', 'error'}


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()


def gh(path):
    return json.loads(subprocess.check_output(['gh', 'api', path]))


def parts(tag):
    match = TAG.fullmatch(tag)
    if match is None:
        raise ValueError(f'Invalid version tag: {tag}')
    major, minor, patch, stage, revision = match.groups()
    return int(major), int(minor), int(patch), {'alpha': 0, 'beta': 1, 'rc': 2, None: 3}[stage], int(revision or 0)


def python_version(tag):
    match = TAG.fullmatch(tag)
    return tag[1:].split('-', 1)[0] + (
        {'alpha': 'a', 'beta': 'b', 'rc': 'rc'}[match[4]] + match[5] if match[4] else '')


def release_metadata(tag):
    parts(tag)
    info = gh(f'repos/{REPO}/releases/tags/{tag}')
    if info['draft'] or not info['published_at']:
        raise ValueError(f'{tag} is not published')
    ref = gh(f'repos/{REPO}/git/ref/tags/{tag}')['object']
    if ref['type'] == 'tag':
        ref = gh(f'repos/{REPO}/git/tags/{ref["sha"]}')['object']
    if ref['type'] != 'commit' or not re.fullmatch(r'[0-9a-f]{40}', ref['sha']):
        raise ValueError(f'{tag} does not resolve to a commit')
    assets = [asset for asset in info['assets']
              if asset['name'] == f'Solbreaker-{tag[1:]}-server-linux-x86_64.zip'
              and asset['state'] == 'uploaded']
    if len(assets) != 1 or not re.fullmatch(r'sha256:[0-9a-f]{64}', assets[0].get('digest') or ''):
        raise ValueError(f'{tag} has no verified Linux server archive')
    return dict(tag=tag, commit=ref['sha'], prerelease=info['prerelease'],
                asset=assets[0]['name'], digest=assets[0]['digest'])


def numeric(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def fields(row, names):
    return {name: numeric(row[name]) for name in names if name in row}


def timed(row, names):
    return {name: fields(row.get(name, {}), TIMINGS) for name in names}


def windows(rows):
    return [fields(row, WINDOW_VALUES) | {key: [numeric(value) for value in row[key]]
                                         for key in NUMERIC_SERIES if key in row} for row in rows]


def published_report(raw, release):
    expected = dict(schema=1, version=python_version(release['tag']), complete=True,
                    mode='owner_udp_loopback_scripted_clients', requested_seconds=PROFILE['seconds'],
                    warmup_seconds=PROFILE['warmup'], target_step_hz=PROFILE['target_hz'],
                    input_pattern='burst', cold_map_metadata=False, diagnostics_enabled=False,
                    dynamic_backend='rapier')
    if any(raw.get(key) != value for key, value in expected.items()):
        raise ValueError('Report does not match the selected release and workload')
    host = raw['host']
    machine = {key: host[key] for key in ('cpu', 'os', 'os_release', 'logical_cpus', 'available_cpus',
                                          'physical_memory_bytes')}
    if not isinstance(machine['cpu'], str) or not 0 < len(machine['cpu']) <= 140 or machine['os'] != 'Linux':
        raise ValueError('Report does not describe the Linux test host')
    if not re.fullmatch(r'[0-9.]+_[0-9]+', raw['game_build']):
        raise ValueError('Invalid game build ID')
    runs = raw['runs']
    if [run.get('concurrent_arenas') for run in runs] != PROFILE['arenas']:
        raise ValueError('The report does not contain the expected arena counts')
    clean = expected | dict(game_build=raw['game_build'], host=machine, release=release, runs=[])
    for run in runs:
        arenas = run.get('arenas', [])
        if (run.get('map') != PROFILE['map'] or run.get('scenario') != PROFILE['scenario']
                or run.get('requested_tanks') != PROFILE['tanks'] or run.get('seed') != PROFILE['seed']
                or run.get('verdict') not in VERDICTS or len(arenas) != run['concurrent_arenas']
                or any(arena.get('vehicles') != PROFILE['tanks'] or arena.get('players') != PROFILE['tanks']
                       for arena in arenas)):
            raise ValueError('A run did not measure the expected workload')
        clean['runs'].append(fields(run, NUMERIC_RUN) | timed(run, ('tick_work', 'loop_delay', 'server_rtt_at_end'))
            | dict(map=PROFILE['map'], scenario=PROFILE['scenario'], verdict=run['verdict'],
                   faults={key: numeric(value) for key, value in run.get('faults', {}).items()
                           if re.fullmatch(r'[a-z_]{1,64}', key)},
                   windows=windows(run.get('windows', [])), timeline=windows(run.get('timeline', [])),
                   arenas=[fields(arena, NUMERIC_ARENA) | timed(arena, ('input_queue', 'motion_gap',
                                                                      'owner_step_roundtrip')) for arena in arenas]))
    return clean


def combined(run, server, workers):
    values = run.get(server), run.get(workers)
    return round(sum(values), 3) if None not in values else None


def row(run):
    latencies = [arena['input_queue'].get('p99_ms') for arena in run['arenas']]
    rss = combined(run, 'process_rss_bytes', 'arena_worker_rss_bytes_sum')
    return dict(arenas=run['concurrent_arenas'], verdict=run['verdict'],
                hz=min(arena['steps_per_second'] for arena in run['arenas']),
                input_ms=max(latencies) if None not in latencies else None,
                tick_ms=run['tick_work'].get('p99_ms'),
                cpu=combined(run, 'cpu_cores_avg', 'arena_worker_cpu_cores_avg'),
                ram_mib=round(rss / 2**20, 2) if rss is not None else None,
                drops=combined(run, 'server_udp_receive_drops', 'peer_udp_receive_drops'))


def index_data():
    return json.loads(INDEX.read_text()) if INDEX.exists() else {'schema': 1, 'releases': []}


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def add_result(report, release):
    data = index_data()
    tag = release['tag']
    if any(item['tag'] == tag for item in data['releases']):
        raise ValueError(f'{tag} already has a published result')
    clean = published_report(report, release)
    host = clean['host']
    profile = PROFILE | dict(cpu=host['cpu'], host=host, ram_gib=round(host['physical_memory_bytes'] / 2**30, 1),
                             game_build=clean['game_build'])
    if data.get('profile', profile) != profile:
        raise ValueError('The machine or game build differs from the comparison profile')
    epoch = min(run['measurement_started_epoch'] for run in clean['runs'])
    measured = datetime.fromtimestamp(epoch, timezone.utc).date().isoformat()
    summary = dict(tag=tag, commit=release['commit'], prerelease=release['prerelease'],
                   measured=measured, runs=[row(run) for run in clean['runs']])
    data['releases'].append(summary)
    data['releases'].sort(key=lambda item: parts(item['tag']))
    data['profile'] = profile
    write_html(clean, DATA/f'{tag}.html')
    save_json(DATA/f'{tag}.json', clean)
    save_json(INDEX, data)
    print(f'Indexed {tag} ({release["commit"][:8]})', flush=True)


def collect(release, args):
    from solbreaker.updates import unpack
    with TemporaryDirectory(prefix='solbreaker-release-benchmark-') as temp:
        folder = Path(temp)
        archive = folder/release['asset']
        subprocess.run(['gh', 'release', 'download', release['tag'], '-R', REPO,
                        '--pattern', release['asset'], '--output', str(archive)], check=True)
        with archive.open('rb') as stream:
            digest = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != release['digest']:
            raise ValueError('Linux release archive does not match the published SHA-256')
        package = folder/'package'
        unpack(archive, package, 'server-linux-x86_64')
        if (package/'requirements/lock.json').read_bytes() != (args.engine_tools/'requirements/lock.json').read_bytes():
            raise ValueError('The release requires a different benchmark Python environment')
        output = folder/'benchmark.json'
        cli = [sys.executable, str(args.native_lab), '--no-displays', '--', 'bash', '-c',
               'cd "$1" && shift && exec "$@"', 'solbreaker-benchmark', str(package), sys.executable,
               '-m', 'solbreaker', 'benchmark', '--game', str(args.game), '--output', str(output),
               '--maps', PROFILE['map'], '--scenarios', PROFILE['scenario'], '--arenas',
               ','.join(map(str, PROFILE['arenas'])), '--tanks', str(PROFILE['tanks']),
               '--seconds', str(PROFILE['seconds']), '--warmup', str(PROFILE['warmup']), '--seed', str(PROFILE['seed'])]
        clean_env = {key: value for key, value in os.environ.items()
                     if not key.startswith('SOLBREAKER_') and key != 'PYTHONPATH'}
        subprocess.run(cli, check=True, env=clean_env)
        return json.loads(output.read_text())


def sync(args):
    if git('branch', '--show-current') != 'benchmarks' or git('status', '--porcelain'):
        raise ValueError('Run sync from a clean benchmarks checkout')
    git('pull', '--ff-only', 'origin', 'benchmarks')
    # Retry a previously committed update if a push failed.
    git('push', 'origin', 'HEAD:refs/heads/benchmarks')
    indexed = {item['tag'] for item in index_data()['releases']}
    all_releases = gh(f'repos/{REPO}/releases?per_page=100')
    tags = [row['tag_name'] for row in all_releases if not row['draft']
            and TAG.fullmatch(row['tag_name']) and parts(row['tag_name']) >= parts(args.from_tag)
            and row['tag_name'] not in indexed
            and any(asset['name'].endswith('-server-linux-x86_64.zip') for asset in row['assets'])]
    sys.path.insert(0, str(args.engine_tools))
    for tag in sorted(tags, key=parts):
        release = release_metadata(tag)
        add_result(collect(release, args), release)
        git('add', '--', 'docs/data')
        git('commit', '-m', f'Publish benchmark results for {tag}')
        git('push', 'origin', 'HEAD:refs/heads/benchmarks')
    if not tags:
        print('All eligible releases are indexed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest='action', required=True)
    record = subcommands.add_parser('record', help='Verify and index an existing report for this test host')
    record.add_argument('tag')
    record.add_argument('json', type=Path)
    automatic = subcommands.add_parser('sync', help='Benchmark and publish newly released Linux packages')
    automatic.add_argument('--game', required=True, type=Path)
    automatic.add_argument('--engine-tools', required=True, type=Path)
    automatic.add_argument('--native-lab', required=True, type=Path)
    automatic.add_argument('--from-tag', default='v0.2.1')
    args = parser.parse_args()
    try:
        if args.action == 'record':
            add_result(json.loads(args.json.read_text()), release_metadata(args.tag))
        else:
            sync(args)
    except (ValueError, OSError, subprocess.CalledProcessError, KeyError, TypeError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
